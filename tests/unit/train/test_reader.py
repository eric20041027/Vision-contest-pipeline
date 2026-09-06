import numpy as np
import pytest

from helpers import det_samples, make_card, write_dicom_study, write_images
from vcp.core.errors import IntegrityError, SealedSubsetError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.train import MaterializedReader, Record


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
    reader = MaterializedReader(
        "tiny",
        "png-r4",
        plan_id="fixed-v1",
        subset="train",
        data_root=roots.data,
        configs_root=roots.configs,
    )
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
    sealed = MaterializedReader(
        "tiny",
        "png-r4",
        plan_id="fixed-v1",
        subset="holdout",
        unseal=True,
        reason="test",
        data_root=roots.data,
        configs_root=roots.configs,
    )
    assert len(sealed) == len(plan.ids_in("holdout"))
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
