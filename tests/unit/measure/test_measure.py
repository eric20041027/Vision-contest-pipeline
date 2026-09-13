import shutil

import pytest

from helpers import (
    ML_CATS,
    SEG_CATS,
    cls_samples,
    dataset_with_perfect_run,
    det_samples,
    det_with_runs,
    make_card,
    multilabel_samples,
    perfect_predictions,
    seg_samples,
    write_images,
)
from vcp.core.errors import (
    GuardrailError,
    IntegrityError,
    PlanMismatchError,
    RegistryError,
    SealedSubsetError,
    ValidationFailed,
)
from vcp.data.access.access import DatasetAccess
from vcp.data.dataset import Dataset
from vcp.data.source_audit import KIND as AUDIT_KIND
from vcp.measure.anchors import anchor_key, set_anchor
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.measure import MeasureSpec, default_subsets, measure_run
from vcp.measure.metrics import METRICS, get_metric, register_metric
from vcp.measure.predictions import write_predictions
from vcp.measure.provenance import attach_receipts, provenance
from vcp.measure.runs import load_run, prediction_path, save_run
from vcp.measure.schema import Anchor, MetricResult

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


# spec 13.2 wants readings from all five task shapes; det is covered by every test above, and
# regression by test_prereg_judge's rmse fixture. cls / multilabel / seg had never been driven
# through measure_run at all -- their metrics were only ever called directly -- so nothing
# pinned the path from a real ingested prediction file to a row in readings.jsonl for them.
# Perfect predictions make the expected value exact, which is what makes this an assertion
# rather than a smoke test: log_loss (lower is better) bottoms out at 0, the rest top out at 1.
_TASK_SHAPES = {
    "cls": (
        None,
        lambda: cls_samples(120, seed=1),
        {"accuracy": 1.0, "macro_f1": 1.0, "log_loss": 0.0},
    ),
    "multilabel": (
        ML_CATS,
        lambda: multilabel_samples(120, seed=2, probs=(0.5, 0.4, 0.3)),
        {"macro_auc": 1.0},
    ),
    "seg": (SEG_CATS, lambda: seg_samples(60, seed=3), {"dice": 1.0, "miou": 1.0}),
}


@pytest.mark.parametrize("task", list(_TASK_SHAPES))
def test_measure_run_on_cls_multilabel_and_seg_datasets(roots, tmp_path, task):
    """spec 13.2: a perfect run scores perfectly on every task shape, and re-measuring caches."""
    categories, make_samples, expected = _TASK_SHAPES[task]
    _, _, paths = dataset_with_perfect_run(
        roots, tmp_path, name=task, task=task, samples=make_samples(), categories=categories
    )
    res = measure_run(_spec(roots, "perfect"))
    assert {r.subset for r in res.readings} == {"valA", "valB"}
    assert {r.metric for r in res.readings} == set(expected)
    for r in res.readings:
        assert r.value == pytest.approx(expected[r.metric], abs=1e-9), (r.metric, r.subset)
        assert r.n_samples > 0 and r.dataset == task
    assert res.new == len(expected) * 2 == len(_rows(paths))
    again = measure_run(_spec(roots, "perfect"))
    assert again.new == 0 and again.cached == res.new and len(_rows(paths)) == res.new


def test_params_only_reach_metrics_that_declare_them(roots, tmp_path):
    det_with_runs(roots, tmp_path, n=40)
    res = measure_run(_spec(roots, "perfect", params={"iou": "50"}))
    assert all(r.params == {"iou": "50", "max_dets": "100"} for r in res.readings)
    with pytest.raises(ValidationFailed, match="params"):
        measure_run(_spec(roots, "perfect", params={"nms": "1"}))


class _StubDetMetric:
    """A throwaway second `det` metric, registered only for the duration of one test.

    `det` has exactly one built-in applicable metric (coco_map), so no existing test can tell
    "a param reached the metric that declares it" apart from "a param reached every metric" --
    both look identical with a single metric in play. A second metric with a *different*
    declared param makes the two behaviours distinguishable (Minor 1)."""

    name = "stub_det"
    version = "1"
    tasks = frozenset({"det"})
    defaults = {"alpha": "1"}
    higher_is_better = True

    def compute(self, samples, predictions, card, params):
        return MetricResult(value=1.0, per_class=None, n=len(samples))


def test_params_reach_only_the_metric_that_declares_them_with_two_applicable_metrics(
    roots, tmp_path
):
    det_with_runs(roots, tmp_path, n=40)
    stub = _StubDetMetric()
    register_metric(stub)
    try:
        res = measure_run(
            _spec(
                roots,
                "perfect",
                metrics=["coco_map", "stub_det"],
                subsets=["valA"],
                params={"iou": "50", "alpha": "2"},
            )
        )
    finally:
        METRICS.pop("stub_det", None)
    coco_rows = [r for r in res.readings if r.metric == "coco_map"]
    stub_rows = [r for r in res.readings if r.metric == "stub_det"]
    assert coco_rows and all(r.params == {"iou": "50", "max_dets": "100"} for r in coco_rows)
    assert stub_rows and all(r.params == {"alpha": "2"} for r in stub_rows)


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


def test_guardrail_checks_the_anchor_run_against_the_dataset(roots, tmp_path):
    """Minor 4: the anchor names a run by id, and that run's own card can go stale relative to
    the dataset (e.g. the dataset was re-imported after the anchor was set) without the
    currently-measured run being affected at all. That must surface as the PlanMismatchError it
    is, not a confusing GuardrailError from recomputing a metric against a mismatched dataset."""
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
    # Corrupt the anchor run's own card only -- the dataset and the "noisy" run being measured
    # are untouched, so nothing about *this* command's own run is mismatched.
    stale = load_run(roots.data, "perfect").model_copy(update={"samples_hash": "0" * 64})
    save_run(roots.data, stale)
    with pytest.raises(PlanMismatchError, match="samples_hash"):
        measure_run(_spec(roots, "noisy"))


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


def _receipt(roots, subsets, *, purpose, run_id):
    with DatasetAccess.open(
        "tiny",
        "fixed-v1",
        subsets=set(subsets),
        purpose=purpose,
        run_id=run_id,
        data_root=roots.data,
        configs_root=roots.configs,
    ) as access:
        for s in subsets:
            list(access.iter(s))
    return access.receipt_id


def test_observed_subsets_are_not_clean_bases(roots, tmp_path):
    _, plan, paths = det_with_runs(roots, tmp_path, n=40)
    noisy = load_run(roots.data, "noisy")
    save_run(
        roots.data,
        attach_receipts(
            noisy,
            [_receipt(roots, ["valA"], purpose="custom", run_id="noisy")],
            data_root=roots.data,
        ),
    )
    card = load_run(roots.data, "noisy")
    assert default_subsets(plan, card, unseal=False, observed=["valA"]) == ["valB"]
    res = measure_run(_spec(roots, "noisy"))
    assert {r.subset for r in res.readings} == {"valB"}
    assert res.provenance == "declared" and res.observed == ["valA"]
    assert all(r.provenance == "declared" for r in res.readings)
    with pytest.raises(
        ValidationFailed, match="^contaminated: subset 'valA' was read by the run"
    ) as ei:
        measure_run(_spec(roots, "noisy", subsets=["valA"]))
    assert ei.value.fields == {"subset": "valA"}


def test_an_invalidated_receipts_subset_still_stays_out_of_the_clean_base(roots, tmp_path):
    """F3 (final review Important #3): declaration must not undo observation. Once the receipt
    that read "valA" is invalidated (here: the plan file rewritten), `provenance().observed`
    must still carry "valA" -- otherwise `default_subsets` would revive it as a clean base."""
    _, plan, paths = det_with_runs(roots, tmp_path, n=40)
    noisy = load_run(roots.data, "noisy")
    save_run(
        roots.data,
        attach_receipts(
            noisy,
            [_receipt(roots, ["valA"], purpose="custom", run_id="noisy")],
            data_root=roots.data,
        ),
    )
    card = load_run(roots.data, "noisy")
    rid = card.access[0].artifact_id
    pj = paths.plan_json("fixed-v1")
    pj.write_bytes(pj.read_bytes() + b"\n")
    info = provenance(card, data_root=roots.data, configs_root=roots.configs)
    assert info.invalid == [rid] and info.observed == ["valA"]
    assert default_subsets(plan, card, unseal=False, observed=info.observed) == ["valB"]


def test_readings_carry_the_runs_grade_and_measure_leaves_its_own_receipt(roots, tmp_path):
    _, plan, paths = det_with_runs(roots, tmp_path, n=40)
    perfect = load_run(roots.data, "perfect")
    save_run(
        roots.data,
        attach_receipts(
            perfect,
            [_receipt(roots, ["train"], purpose="train", run_id="perfect")],
            data_root=roots.data,
        ),
    )
    res = measure_run(_spec(roots, "perfect"))
    assert res.provenance == "receipt" and res.observed == ["train"]
    assert {r.provenance for r in res.readings} == {"receipt"}
    receipts = sorted(
        p.name for p in (roots.data / "artifacts" / "access_receipt").iterdir() if p.is_dir()
    )
    measure_receipts = [r for r in receipts if r.startswith("measure-tiny-fixed-v1-")]
    assert len(measure_receipts) == 1
    from vcp.data.access.receipt import read_receipt

    receipt = read_receipt(roots.data, measure_receipts[0]).receipt
    assert receipt.run_id == "perfect" and set(receipt.accessed) == {"valA", "valB"}
    # a stale receipt warns and the grade falls back, but F3 says it must not un-observe "train"
    pj = paths.plan_json("fixed-v1")
    pj.write_bytes(pj.read_bytes() + b"\n")
    res = measure_run(_spec(roots, "perfect"))
    assert res.receipt_invalid == 1 and res.provenance == "declared"
    assert res.observed == ["train"]
    assert "receipt_invalid=1" in res.warnings


def test_measure_reports_its_identity_and_warns_without_an_audit(roots, tmp_path):
    _, plan, paths = det_with_runs(roots, tmp_path, n=40)
    res = measure_run(_spec(roots, "perfect"))
    assert res.identity == "source_audit" and "source_audit=missing" not in res.warnings
    shutil.rmtree(roots.data / "artifacts" / AUDIT_KIND)
    res = measure_run(_spec(roots, "perfect"))
    assert res.identity == "full_hash" and "source_audit=missing" in res.warnings
