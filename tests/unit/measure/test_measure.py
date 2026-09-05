import pytest

from helpers import det_samples, det_with_runs, make_card, perfect_predictions, write_images
from vcp.core.errors import (
    GuardrailError,
    IntegrityError,
    PlanMismatchError,
    RegistryError,
    SealedSubsetError,
    ValidationFailed,
)
from vcp.data.dataset import Dataset
from vcp.measure.anchors import anchor_key, set_anchor
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.measure import MeasureSpec, default_subsets, measure_run
from vcp.measure.metrics import get_metric
from vcp.measure.predictions import write_predictions
from vcp.measure.runs import load_run, prediction_path
from vcp.measure.schema import Anchor

ANCHORED_PARAMS = "iou=50:95,max_dets=100"


def _spec(roots, run_id, **kw):
    return MeasureSpec(run_id=run_id, data_root=roots.data, configs_root=roots.configs, **kw)


def _rows(paths):
    return ReadingsLedger(paths.measure_dir / "readings.jsonl").rows


def test_measure_default_subsets_and_cache(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    res = measure_run(_spec(roots, "perfect"))
    assert {r.subset for r in res.readings} == {"valA", "valB"}  # holdout sealed, train excluded
    assert {r.metric for r in res.readings} == {"coco_map"}
    assert all(r.value == pytest.approx(1.0) for r in res.readings)
    assert res.new == 2 and res.cached == 0 and res.guardrail == "none" and res.warnings
    again = measure_run(_spec(roots, "perfect"))
    assert again.new == 0 and again.cached == 2 and again.guardrail == "cached"
    assert len(_rows(paths)) == 2 and _rows(paths)[0].prediction_sha
    noisy = measure_run(_spec(roots, "noisy"))
    assert all(r.value < 1.0 for r in noisy.readings)
    with pytest.raises(ValidationFailed, match="trained_on"):
        measure_run(_spec(roots, "perfect", subsets=["train"]))
    with pytest.raises(ValidationFailed, match="no predictions"):
        measure_run(_spec(roots, "noisy", subsets=["holdout"]))
    with pytest.raises(SealedSubsetError):
        measure_run(_spec(roots, "perfect", subsets=["holdout"]))
    unseal_log = paths.unseal_jsonl("fixed-v1")
    before = unseal_log.read_text(encoding="utf-8").splitlines()
    sealed = measure_run(_spec(roots, "perfect", subsets=["holdout"], unseal=True, reason="final"))
    assert sealed.new == 1
    after = unseal_log.read_text(encoding="utf-8").splitlines()
    assert len(after) == len(before) + 1 and '"caller": "vcp eval measure"' in after[-1]
    with pytest.raises(ValidationFailed, match="not applicable"):
        measure_run(_spec(roots, "perfect", metrics=["accuracy"]))
    card = load_run(roots.data, "perfect")
    assert default_subsets(plan, card, unseal=False) == ["valA", "valB"]
    # spec 6.1: with no trained_on every eval subset is clean, and sealed ones join only when
    # the caller is unsealing.
    fresh = card.model_copy(update={"trained_on": []})
    assert default_subsets(plan, fresh, unseal=False) == ["valA", "valB"]
    assert default_subsets(plan, fresh, unseal=True) == ["valA", "valB", "holdout"]


def test_params_only_reach_metrics_that_declare_them(roots, tmp_path):
    det_with_runs(roots, tmp_path, n=40)
    res = measure_run(_spec(roots, "perfect", params={"iou": "50"}))
    assert all(r.params == {"iou": "50", "max_dets": "100"} for r in res.readings)
    with pytest.raises(ValidationFailed, match="params"):
        measure_run(_spec(roots, "perfect", params={"nms": "1"}))


def test_guardrail_aborts_before_writing(roots, tmp_path, monkeypatch):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=40)
    first = measure_run(_spec(roots, "perfect"))
    valA = next(r for r in first.readings if r.subset == "valA")
    set_anchor(
        paths,
        anchor_key("fixed-v1", "valA", "coco_map", ANCHORED_PARAMS),
        Anchor(
            run_id="perfect",
            reading_id=valA.reading_id,
            value=valA.value,
            tolerance=1e-6,
            set_at="2026-09-04T00:00:00.000Z",
        ),
    )
    anchored = measure_run(_spec(roots, "noisy", subsets=["valA"]))
    assert anchored.new == 1 and anchored.guardrail == "OK" and not anchored.warnings
    ok = measure_run(_spec(roots, "noisy"))
    assert ok.guardrail == "partial"  # valA anchored, valB not
    assert next(r for r in ok.readings if r.subset == "valA").guardrail.ok is True
    before = len(_rows(paths))
    metric = get_metric("coco_map")
    real = metric.compute

    def drifted(samples, predictions, card, params):
        r = real(samples, predictions, card, params)
        return r.model_copy(update={"value": r.value - 0.01})

    monkeypatch.setattr(type(metric), "compute", lambda self, *a, **k: drifted(*a, **k))
    # Every reading of this call is already cached, so the guardrail must run *before* the cache
    # short-circuit or the drift would go unnoticed (ruling 3).
    with pytest.raises(GuardrailError, match="anchor"):
        measure_run(_spec(roots, "noisy", subsets=["valA", "valB"], metrics=["coco_map"]))
    assert len(_rows(paths)) == before


def test_duplicate_subsets_and_metrics_yield_one_reading(roots, tmp_path):
    """spec 4.3: one row per identity. Without a dedup the second cell computes the same
    reading again and the ledger append fails *after* the first row was already written."""
    _, _, paths = det_with_runs(roots, tmp_path, n=30)
    res = measure_run(
        _spec(roots, "perfect", subsets=["valA", "valA"], metrics=["coco_map", "coco_map"])
    )
    assert res.new == 1 and len(res.readings) == 1 and len(_rows(paths)) == 1


def test_run_dataset_mismatch(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=30)
    samples = det_samples(31, seed=9)
    write_images(roots.data / "raw" / "tiny", samples)
    Dataset.from_parts(make_card("det", image_root="raw/tiny"), samples).save(paths)  # re-import
    with pytest.raises(PlanMismatchError, match="samples_hash"):
        measure_run(_spec(roots, "perfect"))


def test_unknown_run_and_unknown_metric_fail_distinctly(roots, tmp_path):
    """A typo in --run is the user's data problem (FAIL); a typo in --metrics asks for something
    the registry does not have (ABORT)."""
    det_with_runs(roots, tmp_path, n=30)
    with pytest.raises(ValidationFailed, match="run not found"):
        measure_run(_spec(roots, "ghost"))
    with pytest.raises(RegistryError, match="unknown metric"):
        measure_run(_spec(roots, "perfect", metrics=["nope"]))


def test_tampered_predictions_are_an_integrity_error(roots, tmp_path):
    """A prediction file edited after ingest must never be scored (spec 9)."""
    det_with_runs(roots, tmp_path, n=30)
    path = prediction_path(roots.data, "perfect", "valA")
    original = path.read_text(encoding="utf-8")
    tampered = original.replace('"score":1.0', '"score":0.9', 1)
    assert tampered != original  # the fixture really does have a scored box to tamper with
    path.write_text(tampered, encoding="utf-8", newline="\n")
    with pytest.raises(IntegrityError, match="sha256"):
        measure_run(_spec(roots, "perfect", subsets=["valA"]))


def test_run_with_no_clean_subset_fails(roots, tmp_path):
    """A run trained on every eval subset has nothing clean left to read: a loud failure, not a
    silent OK with zero readings."""
    ds, plan, paths = det_with_runs(roots, tmp_path, n=30)
    src = tmp_path / "alltrain-valA.jsonl"
    write_predictions(src, perfect_predictions(ds.subset("valA", plan), ds.card))
    ingest(
        IngestSpec(
            run_id="alltrain",
            dataset="tiny",
            plan_id="fixed-v1",
            subset="valA",
            format="jsonl",
            src=src,
            trained_on=["train", "valA", "valB"],
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    with pytest.raises(ValidationFailed, match="no clean eval subset"):
        measure_run(_spec(roots, "alltrain"))
