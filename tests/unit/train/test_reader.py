import numpy as np
import pytest

from helpers import det_samples, make_card, write_dicom_study, write_images
from vcp.artifact import store
from vcp.core.errors import AccessDeniedError, IntegrityError, SealedSubsetError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.access.access import DatasetAccess
from vcp.data.access.receipt import read_receipt
from vcp.data.dataset import Dataset
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.train import MaterializedReader, Record
from vcp.train.records import load_record, save_record
from vcp.train.schema import Attempt, TrainRecord


def _image_ds(roots, name="tiny", n=8):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def _dicom_ds(roots, name="dcm"):
    write_dicom_study(roots.data / "raw" / name, study_uid="1.2.1", series=2, slices=3)
    get_importer("dicom").run(
        ImportSpec(
            importer="dicom",
            src=roots.data / "raw" / name,
            name=name,
            options={},
            license="CC0",
            url="u",
            downloaded_at="2026-09-03",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )


def _mat(roots, name, **kw):
    return materialize(
        MaterializeSpec(name=name, data_root=roots.data, configs_root=roots.configs, **kw)
    )


def test_reader_iterates_npy_rows_per_view(roots):
    ds, plan, paths = _image_ds(roots)
    res = _mat(roots, "tiny", mode="npy")
    assert res.failed == 0
    reader = MaterializedReader("tiny", "npy", data_root=roots.data, configs_root=roots.configs)
    assert len(reader) == 8 and reader.ids == [s.sample_id for s in ds.samples]
    recs = list(reader)
    assert all(isinstance(r, Record) for r in recs)
    first = recs[0]
    assert first.sample_id == "s0000" and first.labels is not None and set(first.arrays) == {"0"}
    row = reader.rows("s0000")[0]
    assert list(first.arrays["0"].shape) == row.shape and str(first.arrays["0"].dtype) == row.dtype
    assert reader["s0003"].sample.sample_id == "s0003"


def test_reader_png_resized_and_subset_restriction(roots):
    ds, plan, paths = _image_ds(roots)
    _mat(roots, "tiny", mode="png", resize=4)
    with MaterializedReader(
        "tiny",
        "png-r4",
        plan_id="fixed-v1",
        subset="train",
        data_root=roots.data,
        configs_root=roots.configs,
    ) as reader:
        assert set(reader.ids) == plan.ids_in("train") and len(reader) == len(plan.ids_in("train"))
        arr = next(iter(reader)).arrays["0"]
        assert arr.dtype == np.uint8 and max(arr.shape[:2]) == 4
    with pytest.raises(SealedSubsetError):
        MaterializedReader(
            "tiny",
            "png-r4",
            plan_id="fixed-v1",
            subset="holdout",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    with MaterializedReader(
        "tiny",
        "png-r4",
        plan_id="fixed-v1",
        subset="holdout",
        unseal=True,
        reason="test",
        data_root=roots.data,
        configs_root=roots.configs,
    ) as sealed:
        assert len(sealed) == len(plan.ids_in("holdout"))
    assert sealed.access.receipt.sealed_accessed is True
    with pytest.raises(ValidationFailed, match="plan_id and subset"):
        MaterializedReader(
            "tiny", "png-r4", subset="train", data_root=roots.data, configs_root=roots.configs
        )


def test_reader_stacked_sequences_keyed_by_seq_id(roots):
    _dicom_ds(roots)
    res = _mat(roots, "dcm", mode="npy", stack_seq=True)
    assert res.failed == 0
    reader = MaterializedReader("dcm", "npy", data_root=roots.data, configs_root=roots.configs)
    rec = next(iter(reader))
    assert len(rec.arrays) == 2 and all(
        a.ndim == 3 and a.shape[0] == 3 for a in rec.arrays.values()
    )
    assert set(rec.arrays) == {r.seq_id for r in reader.rows(rec.sample_id)}


def test_reader_failures(roots):
    ds, plan, paths = _image_ds(roots)
    with pytest.raises(ValidationFailed, match="materialize cache not found"):
        MaterializedReader("tiny", "npy", data_root=roots.data, configs_root=roots.configs)
    _mat(roots, "tiny", mode="npy")
    manifest = paths.cache_dir / "materialize" / "npy" / "manifest.jsonl"
    lines = manifest.read_text(encoding="utf-8").splitlines()
    without = [line for line in lines if '"s0000"' not in line]  # drop s0000's row(s)
    assert len(without) < len(lines)
    manifest.write_text("\n".join(without) + "\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="no materialized rows") as ei:
        MaterializedReader("tiny", "npy", data_root=roots.data, configs_root=roots.configs)
    assert ei.value.location == "s0000"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    reader = MaterializedReader(
        "tiny", "npy", verify=True, data_root=roots.data, configs_root=roots.configs
    )
    row = reader.rows("s0001")[0]
    target = paths.cache_dir / "materialize" / "npy" / row.out
    np.save(target, np.zeros((2, 2), dtype=np.uint8))
    with pytest.raises(IntegrityError):
        reader["s0001"]


def test_reader_with_a_subset_reads_only_that_subset_and_leaves_a_receipt(roots):
    ds, plan, paths = _image_ds(roots, n=40)
    assert _mat(roots, "tiny", mode="npy").failed == 0
    with MaterializedReader(
        "tiny",
        "npy",
        plan_id="fixed-v1",
        subset="train",
        purpose="custom",
        data_root=roots.data,
        configs_root=roots.configs,
    ) as reader:
        assert reader.ids == sorted(plan.ids_in("train")) and reader.card.name == "tiny"
        assert reader.access is not None and reader.access.allowed == frozenset({"train"})
        with pytest.raises(KeyError):
            reader[sorted(plan.ids_in("valA"))[0]]  # not even in the reader's index
        first = reader[reader.ids[0]]
        assert first.labels is not None and set(first.arrays) == {"0"}
        rid = reader.access.receipt_id
        assert store.is_partial(roots.data, "access_receipt", rid)
    receipt = read_receipt(roots.data, rid).receipt
    assert receipt.purpose == "custom" and set(receipt.accessed) == {"train"}
    assert receipt.accessed["train"].ids_count == len(plan.ids_in("train"))


def test_reader_under_a_training_session_registers_its_receipt(roots, monkeypatch):
    ds, plan, paths = _image_ds(roots, n=40)
    assert _mat(roots, "tiny", mode="npy").failed == 0
    save_record(
        roots.data,
        TrainRecord(
            run_id="r1",
            dataset="tiny",
            plan_id="fixed-v1",
            trained_on=["train"],
            config_hash="ab" * 32,
            cwd="work",
            command=["python"],
            attempts=[Attempt(n=1, started_at="2026-09-12T00:00:00.000Z", console="c")],
        ),
    )
    monkeypatch.setenv("VCP_RUN_ID", "r1")
    monkeypatch.setenv("VCP_DATA_ROOT", str(roots.data))
    monkeypatch.setenv("VCP_CONFIGS_ROOT", str(roots.configs))
    with pytest.raises(AccessDeniedError, match="^denied: a training reader must name plan_id"):
        MaterializedReader("tiny", "npy")
    with MaterializedReader("tiny", "npy", plan_id="fixed-v1", subset="train") as reader:
        assert reader.access.receipt_id == "r1-a1-1"
        for rec in reader:
            assert rec.sample_id in plan.ids_in("train")
    record = load_record(roots.data, "r1")
    assert [r.artifact_id for r in record.access] == ["r1-a1-1"]
    assert record.access[0].purpose == "train" and record.access[0].subsets == ["train"]
    # an injected, already-open access is used as-is and NOT closed by the reader
    with DatasetAccess.open(
        "tiny",
        "fixed-v1",
        subsets={"valA"},
        purpose="custom",
        data_root=roots.data,
        configs_root=roots.configs,
    ) as access:
        reader = MaterializedReader(
            "tiny",
            "npy",
            plan_id="fixed-v1",
            subset="valA",
            access=access,
            data_root=roots.data,
            configs_root=roots.configs,
        )
        reader.close()
        assert access.receipt is None
    assert access.receipt is not None and set(access.receipt.accessed) == {"valA"}


def test_reader_under_a_run_refuses_a_name_or_plan_mismatch(roots, monkeypatch):
    """F4 (final review Important #4): under a run, the session binds the receipt to
    record.dataset/plan_id regardless of what this reader asked for -- a caller passing another
    plan_id (or dataset name) must be refused, not silently trained on the run's own plan."""
    ds, plan, paths = _image_ds(roots, n=40)
    assert _mat(roots, "tiny", mode="npy").failed == 0
    save_record(
        roots.data,
        TrainRecord(
            run_id="r1",
            dataset="tiny",
            plan_id="fixed-v1",
            trained_on=["train"],
            config_hash="ab" * 32,
            cwd="work",
            command=["python"],
            attempts=[Attempt(n=1, started_at="2026-09-12T00:00:00.000Z", console="c")],
        ),
    )
    monkeypatch.setenv("VCP_RUN_ID", "r1")
    monkeypatch.setenv("VCP_DATA_ROOT", str(roots.data))
    monkeypatch.setenv("VCP_CONFIGS_ROOT", str(roots.configs))
    with pytest.raises(ValidationFailed, match="^mismatch: reader asked for") as ei:
        MaterializedReader("tiny", "npy", plan_id="other-v2", subset="train")
    assert ei.value.fields == {"run": "r1"}
    assert not (roots.data / "artifacts" / "access_receipt").exists()


def test_reader_without_a_plan_keeps_the_full_dataset_outside_a_run(roots):
    ds, plan, paths = _image_ds(roots)
    assert _mat(roots, "tiny", mode="npy").failed == 0
    reader = MaterializedReader("tiny", "npy", data_root=roots.data, configs_root=roots.configs)
    assert reader.access is None and len(reader) == 8 and reader.card.name == "tiny"
    reader.close()


def test_a_construction_failure_after_open_still_commits_a_failed_receipt(roots):
    """A `MaterializedReader.__init__` that opened its own access and then raises (here: the
    materialize cache is missing a row for one of the subset's samples) must not abandon the
    receipt claim -- there must be no directory with a spec.json but no manifest.json/failure.json
    left behind, and the committed receipt must record the failure."""
    ds, plan, paths = _image_ds(roots, n=40)
    assert _mat(roots, "tiny", mode="npy").failed == 0
    manifest = paths.cache_dir / "materialize" / "npy" / "manifest.jsonl"
    sid = sorted(plan.ids_in("train"))[0]
    lines = manifest.read_text(encoding="utf-8").splitlines()
    without = [line for line in lines if f'"{sid}"' not in line]
    assert len(without) < len(lines)
    manifest.write_text("\n".join(without) + "\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="materialized rows"):
        MaterializedReader(
            "tiny",
            "npy",
            plan_id="fixed-v1",
            subset="train",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    receipt_dirs = list((roots.data / "artifacts" / "access_receipt").iterdir())
    assert len(receipt_dirs) == 1
    receipt = read_receipt(roots.data, receipt_dirs[0].name).receipt
    assert receipt.outcome == "failed" and receipt.exception == "ValidationFailed"
