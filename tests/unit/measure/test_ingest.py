import json

import pytest

from helpers import cls_samples, det_samples, make_card, perfect_predictions, write_images
from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.converters import get_converter
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.predictions import read_predictions, write_predictions
from vcp.measure.runs import load_run


def _det(roots, name="tiny", n=40):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def _spec(roots, **kw):
    base = dict(
        run_id="m1",
        dataset="tiny",
        plan_id="fixed-v1",
        subset="valA",
        format="jsonl",
        data_root=roots.data,
        configs_root=roots.configs,
    )
    return IngestSpec(**{**base, **kw})


def test_ingest_jsonl_creates_run_and_records_sha(roots, tmp_path):
    ds, plan, _ = _det(roots)
    val = ds.subset("valA", plan)
    preds = perfect_predictions(val[:-1], ds.card)  # one sample left without predictions
    write_predictions(tmp_path / "valA.jsonl", preds)
    res = ingest(_spec(roots, src=tmp_path / "valA.jsonl", trained_on=["train"], framework="test"))
    assert res.created_run and not res.replaced
    assert (res.samples, res.predicted, res.empty, res.unknown) == (len(val), len(val) - 1, 1, [])
    run = load_run(roots.data, "m1")
    assert run.dataset == "tiny" and run.samples_hash == ds.card.samples_hash
    assert run.plan_id == "fixed-v1" and run.trained_on == ["train"]
    assert run.predictions["valA"].sha256 == res.sha256 and run.predictions["valA"].empty == 1
    # PredictionFile.samples means rows actually written (ruling Task2#8), not the subset size:
    # pin that meaning here so it is not confused with IngestResult.samples (== len(val) above).
    assert run.predictions["valA"].samples == len(val) - 1 == res.predicted
    # F4: runs.prediction_path is the one owner of the run-relative layout; ingest() must derive
    # PredictionFile.path from it rather than re-spelling "predictions/<subset>.jsonl" itself.
    assert run.predictions["valA"].path == "predictions/valA.jsonl"
    assert res.path == roots.data / "runs" / "m1" / "predictions" / "valA.jsonl"
    assert len(read_predictions(res.path)) == len(val) - 1


def test_ingest_second_subset_replace_and_mismatches(roots, tmp_path):
    ds, plan, paths = _det(roots)
    for subset in ("valA", "valB"):
        write_predictions(
            tmp_path / f"{subset}.jsonl", perfect_predictions(ds.subset(subset, plan), ds.card)
        )
    ingest(_spec(roots, src=tmp_path / "valA.jsonl", trained_on=["train"]))
    res = ingest(_spec(roots, subset="valB", src=tmp_path / "valB.jsonl"))
    assert not res.created_run and set(res.run.predictions) == {"valA", "valB"}
    with pytest.raises(ValidationFailed, match="--replace"):
        ingest(_spec(roots, src=tmp_path / "valA.jsonl"))
    res = ingest(_spec(roots, src=tmp_path / "valA.jsonl", replace=True))
    assert res.replaced
    history = (roots.data / "runs" / "m1" / "history.jsonl").read_text(encoding="utf-8")
    assert '"event": "replace"' in history and '"subset": "valA"' in history
    with pytest.raises(ValidationFailed, match="trained_on"):
        ingest(_spec(roots, run_id="m2", src=tmp_path / "valA.jsonl", trained_on=["nope"]))
    with pytest.raises(PlanMismatchError, match="subset"):
        ingest(_spec(roots, run_id="m3", subset="ghost", src=tmp_path / "valA.jsonl"))
    # a second dataset must not be mixed into an existing run
    _det(roots, name="other", n=10)
    with pytest.raises(PlanMismatchError, match="dataset"):
        ingest(
            _spec(
                roots,
                dataset="other",
                subset="train",
                src=tmp_path / "valA.jsonl",
                plan_id="fixed-v1",
            )
        )


def test_ingest_scores_csv_with_unknown_ids(roots, tmp_path):
    paths = DatasetPaths.resolve("c", data_root=roots.data, configs_root=roots.configs)
    samples = cls_samples(20, seed=0)
    ds = Dataset.from_parts(make_card("cls", name="c"), samples)
    ds.save(paths)
    plan = build_plan(
        ds, plan_id="p", subsets=parse_subsets("train:train:0.5,val:eval:0.5"), seed=0
    )
    save_plan(plan, paths)
    val = ds.subset("val", plan)
    rows = ["id,cat,dog,bird"] + [f"{s.sample_id},1,0,0" for s in val] + ["stranger,1,0,0"]
    (tmp_path / "s.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    spec = _spec(
        roots,
        run_id="c1",
        dataset="c",
        plan_id="p",
        subset="val",
        format="scores_csv",
        src=tmp_path / "s.csv",
    )
    with pytest.raises(ValidationFailed, match="unknown"):
        ingest(spec)
    # ruling Task5#3: allow_unknown uses the converters' _TRUE set, not the literal "skip".
    res = ingest(spec.model_copy(update={"options": {"allow_unknown": "true"}}))
    assert res.unknown == ["stranger"] and res.predicted == len(val)
    entry = res.run.predictions["val"]
    # 3-7: the converter's NAME alone does not say which implementation ran. A converter that
    # changes how it reads a framework's output bumps its version, and the run card has to be
    # able to say which one produced these predictions.
    assert entry.format_in == "scores_csv"
    assert entry.format_version == get_converter("scores_csv").version
    assert json.loads(res.path.read_text(encoding="utf-8").splitlines()[0])["scores"]["cat"] == 1.0


def test_ingest_keep_input_copies_source_and_replaces_wholesale(roots, tmp_path):
    ds, plan, _ = _det(roots)
    val = ds.subset("valA", plan)
    src1 = tmp_path / "valA.jsonl"
    write_predictions(src1, perfect_predictions(val, ds.card))
    ingest(_spec(roots, src=src1, keep_input=True))
    kept_dir = roots.data / "runs" / "m1" / "inputs" / "valA"
    assert (kept_dir / "valA.jsonl").is_file()
    assert (kept_dir / "valA.jsonl").read_bytes() == src1.read_bytes()

    # replacing with a differently-named source must not leave the old copy lingering alongside
    # the new one -- the kept input mirrors the *current* prediction file, not a history of them.
    src2 = tmp_path / "valA_v2.jsonl"
    write_predictions(src2, perfect_predictions(val, ds.card))
    ingest(_spec(roots, src=src2, replace=True, keep_input=True))
    assert not (kept_dir / "valA.jsonl").exists()
    assert (kept_dir / "valA_v2.jsonl").is_file()


def test_ingest_export_manifest_missing_manifest_json_fails_for_any_format(roots, tmp_path):
    """F1(a): --export-manifest pointing at a directory without manifest.json must fail even for
    a converter (jsonl) that never itself reads export_dir -- the check belongs to ingest(), not
    to whichever converter happens to be selected."""
    ds, plan, _ = _det(roots)
    write_predictions(
        tmp_path / "valA.jsonl", perfect_predictions(ds.subset("valA", plan), ds.card)
    )
    wrong_export = tmp_path / "not_an_export_dir"
    wrong_export.mkdir()
    with pytest.raises(ValidationFailed, match="manifest.json not found"):
        ingest(_spec(roots, src=tmp_path / "valA.jsonl", export_dir=wrong_export))


def test_ingest_second_subset_source_metadata_conflicts_and_match(roots, tmp_path):
    """F1(b): a second-subset ingest into an existing run must not silently drop a differing
    --framework / --notes (they used to vanish into the unchanged loaded card); the same values
    on a second subset must still be accepted, not rejected.

    ``--export-manifest`` is NOT one of them: ``vcp data export`` writes one export directory
    per SUBSET, so a run covering two eval subsets always has two manifests, and the sha is
    recorded per prediction file rather than compared against the run's."""
    ds, plan, _ = _det(roots)
    for subset in ("valA", "valB"):
        write_predictions(
            tmp_path / f"{subset}.jsonl", perfect_predictions(ds.subset(subset, plan), ds.card)
        )
    export1 = tmp_path / "export1"
    export1.mkdir()
    (export1 / "manifest.json").write_text("{}", encoding="utf-8", newline="\n")
    ingest(
        _spec(roots, src=tmp_path / "valA.jsonl", export_dir=export1, framework="fw1", notes="n1")
    )
    # same export dir / framework / notes on a second subset: accepted, not silently dropped
    res = ingest(
        _spec(
            roots,
            subset="valB",
            src=tmp_path / "valB.jsonl",
            export_dir=export1,
            framework="fw1",
            notes="n1",
        )
    )
    assert not res.created_run and set(res.run.predictions) == {"valA", "valB"}
    run_sha = res.run.source.export_manifest_sha
    assert run_sha and res.run.predictions["valB"].export_manifest_sha == run_sha

    # A second subset exported separately (the only shape `vcp data export` produces) is the
    # normal case, not a conflict: its own manifest sha goes on its own prediction file, and
    # the run-level sha -- the export the run was created from -- is left alone.
    export2 = tmp_path / "export2"
    export2.mkdir()
    (export2 / "manifest.json").write_text('{"x": 1}', encoding="utf-8", newline="\n")
    res = ingest(
        _spec(roots, subset="valB", src=tmp_path / "valB.jsonl", export_dir=export2, replace=True)
    )
    entry = res.run.predictions["valB"]
    assert entry.export_manifest_sha != run_sha
    assert res.run.source.export_manifest_sha == run_sha
    assert res.run.predictions["valA"].export_manifest_sha == run_sha
    with pytest.raises(ValidationFailed, match="framework"):
        ingest(_spec(roots, subset="valB", src=tmp_path / "valB.jsonl", framework="fw2"))
    with pytest.raises(ValidationFailed, match="notes"):
        ingest(_spec(roots, subset="valB", src=tmp_path / "valB.jsonl", notes="n2"))
