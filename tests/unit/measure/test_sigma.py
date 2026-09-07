from typing import Any

import pytest

from helpers import det_with_runs, perfect_predictions
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.core.time import stamp
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.anchors import anchor_key, set_anchor
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.ledger import ReadingsLedger, append_row, read_rows, reading_id
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.measure.metrics import params_key
from vcp.measure.predictions import write_predictions
from vcp.measure.runs import load_run
from vcp.measure.schema import Anchor, Reading, SigmaEstimate
from vcp.measure.sigma import (
    SIGMA_ESTIMATORS,
    Estimator,
    SigmaContext,
    SigmaSpec,
    estimate_sigma,
    estimate_sigma_result,
    latest_sigma,
    register_sigma_method,
)

PARAMS = {"iou": "50:95", "max_dets": "100"}
PK = "iou=50:95,max_dets=100"


def _fake_reading(run, subset, value, ts, *, sha=None):
    # reading_id is derived from (run, plan, subset, metric, version, params, prediction_sha) --
    # never from value or ts (that is what makes re-measuring an unchanged prediction a no-op
    # append) -- so a genuinely SECOND reading for the same run/subset needs its own sha, as a
    # re-measurement after new predictions would have.
    sha = sha or f"sha-{run}"
    return Reading(
        reading_id=reading_id(run, "fixed-v1", subset, "coco_map", "1", PK, sha),
        ts=ts,
        run_id=run,
        dataset="tiny",
        samples_hash="h",
        plan_id="fixed-v1",
        subset=subset,
        metric="coco_map",
        metric_version="1",
        params=PARAMS,
        value=value,
        per_class=None,
        n_samples=6,
        prediction_sha=sha,
    )


def _spec(roots, **kw):
    return SigmaSpec(
        dataset="tiny",
        plan_id="fixed-v1",
        metric="coco_map",
        data_root=roots.data,
        configs_root=roots.configs,
        **kw,
    )


def _sigma_rows(paths):
    return read_rows(paths.measure_dir / "sigma.jsonl", SigmaEstimate)


def test_sigma_methods_are_the_estimator_registry_keys():
    """Dispatch is by name through one mapping, so a fourth method is one entry -- not a
    tuple, a chain of elifs and a message to keep in step."""
    assert tuple(SIGMA_ESTIMATORS) == ("splithalf", "bootstrap", "prior")


@pytest.mark.parametrize("bad", ["split half", "half=x", "-lead", "", "sigma[x]"])
def test_register_sigma_method_rejects_a_name_that_cannot_go_in_a_verdict(bad):
    """3-5: the method name lands in `eval status`'s `sigma[<metric>/<method>]=` field name,
    which `Verdict.line()` does not escape. Same rule, same place, as `register_metric`."""

    def estimator(ctx: SigmaContext) -> tuple[float, dict[str, Any]]:
        raise NotImplementedError

    try:
        with pytest.raises(RegistryError, match="name"):
            register_sigma_method(bad, estimator)
    finally:
        SIGMA_ESTIMATORS.pop(bad, None)
    assert bad not in SIGMA_ESTIMATORS


def test_register_sigma_method_extends_the_axis_and_refuses_a_duplicate(roots, tmp_path):
    """spec 2.1 makes the sigma_p method an extension axis, so a fourth one is registered the
    same way a metric or a converter is: one public call, the same duplicate guard, and a
    public context type to write the estimator against."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    seen: list[SigmaContext] = []

    def constant_quarter(ctx: SigmaContext) -> tuple[float, dict[str, Any]]:
        seen.append(ctx)
        return 0.25, {"note": "a throwaway estimator"}

    estimator: Estimator = constant_quarter
    register_sigma_method("constant_quarter", estimator)
    try:
        for taken in ("constant_quarter", "prior"):  # a plugin may shadow neither
            with pytest.raises(RegistryError, match="already registered"):
                register_sigma_method(taken, estimator)
        est = estimate_sigma(_spec(roots, method="constant_quarter"))
        assert est.value == 0.25 and est.method == "constant_quarter"
        assert est.inputs == {"note": "a throwaway estimator"}
        assert latest_sigma(paths, "fixed-v1", "coco_map", PK, "constant_quarter") is not None
        # The estimator is handed the RESOLVED context, not the raw spec: the paths, the plan
        # and the metric's effective params (with their key) are already worked out for it.
        (ctx,) = seen
        assert ctx.spec.method == "constant_quarter" and ctx.paths.name == "tiny"
        assert ctx.plan.plan_id == "fixed-v1" and ctx.params == PARAMS and ctx.pk == PK
        # The unknown-method message reads the live registry, so a plugin's method is offered
        # while it is registered -- an import-time snapshot could never mention it.
        with pytest.raises(ValidationFailed, match="constant_quarter"):
            estimate_sigma(_spec(roots, method="magic"))
    finally:
        SIGMA_ESTIMATORS.pop("constant_quarter", None)
    assert tuple(SIGMA_ESTIMATORS) == ("splithalf", "bootstrap", "prior")


def test_splithalf_needs_three_runs_then_estimates(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    assert params_key(PARAMS) == PK
    ledger = ReadingsLedger(paths.measure_dir / "readings.jsonl")
    ledger.append(_fake_reading("r1", "valA", 0.50, "2026-09-04T00:00:01.000Z"))
    ledger.append(_fake_reading("r1", "valB", 0.52, "2026-09-04T00:00:02.000Z"))
    ledger.append(_fake_reading("r2", "valA", 0.60, "2026-09-04T00:00:03.000Z"))
    with pytest.raises(ValidationFailed, match="at least 3 runs"):
        estimate_sigma(_spec(roots, method="splithalf", subsets=["valA", "valB"]))
    ledger.append(_fake_reading("r2", "valB", 0.58, "2026-09-04T00:00:04.000Z"))
    ledger.append(_fake_reading("r3", "valA", 0.70, "2026-09-04T00:00:05.000Z"))
    ledger.append(_fake_reading("r3", "valB", 0.71, "2026-09-04T00:00:06.000Z"))
    est = estimate_sigma(_spec(roots, method="splithalf", subsets=["valA", "valB"]))
    # d = [-0.02, 0.02, -0.01] -> std(ddof=1) = 0.020816659994661 -> / sqrt(2)
    assert est.value == pytest.approx(0.020816659994661 / 2**0.5)
    assert est.method == "splithalf" and est.inputs["runs"] == ["r1", "r2", "r3"]
    latest = latest_sigma(paths, "fixed-v1", "coco_map", PK, "splithalf")
    assert latest is not None and latest.estimate_id == est.estimate_id
    assert latest_sigma(paths, "fixed-v1", "coco_map", PK, "prior") is None
    # with no --subsets the pair comes from the plan's own first two eval subsets, so this is
    # the same estimate again: same id, already in the ledger, nothing appended.
    assert estimate_sigma(_spec(roots, method="splithalf")).estimate_id == est.estimate_id
    assert len(_sigma_rows(paths)) == 1
    # `_latest_per_run` must take the newest reading per run, not just any -- with one reading
    # per run that rule is untested (earliest and latest are the same row). A second, later
    # reading for r1's valA changes the diff outright: it must be the one used.
    assert est.inputs["diffs"] == pytest.approx([-0.02, 0.02, -0.01])
    ledger.append(_fake_reading("r1", "valA", 0.90, "2026-09-04T00:00:07.000Z", sha="sha-r1-v2"))
    est2 = estimate_sigma(_spec(roots, method="splithalf", subsets=["valA", "valB"]))
    assert est2.inputs["diffs"] == pytest.approx([0.38, 0.02, -0.01])
    assert est2.estimate_id != est.estimate_id


def test_splithalf_rejects_duplicate_subsets(roots, tmp_path):
    """Two identical subsets would make both halves the same mapping, so every diff is exactly
    0 -- a sigma_p of 0 indistinguishable in the ledger from a genuinely noiseless pair (I2)."""
    det_with_runs(roots, tmp_path, n=40)
    with pytest.raises(ValidationFailed, match="different"):
        estimate_sigma(_spec(roots, method="splithalf", subsets=["valA", "valA"]))


def test_prior_and_bootstrap(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    with pytest.raises(ValidationFailed, match="note"):
        estimate_sigma(_spec(roots, method="prior", prior=0.008))
    est = estimate_sigma(_spec(roots, method="prior", prior=0.008, note="platform history"))
    assert est.value == 0.008 and est.inputs["note"] == "platform history"
    measure_run(MeasureSpec(run_id="noisy", data_root=roots.data, configs_root=roots.configs))
    # valB, not valA: the noisy run's shifted boxes miss every gold box in valA at every IoU,
    # so its mAP there is 0.0 on every draw and the spread really is nothing.
    boot = estimate_sigma(
        _spec(roots, method="bootstrap", run_id="noisy", subsets=["valB"], resamples=20)
    )
    assert boot.value > 0
    # 3-3: `resamples` is what was asked for, `used` / `skipped` what the number is made of.
    # 3-8: `prediction_sha` is the file it was computed from (pinned by its own test below).
    assert boot.inputs == {
        "run_id": "noisy",
        "subset": "valB",
        "resamples": 20,
        "used": 20,
        "skipped": 0,
        "seed": 0,
        "prediction_sha": load_run(roots.data, "noisy").predictions["valB"].sha256,
    }
    with pytest.raises(ValidationFailed, match="--run"):
        estimate_sigma(_spec(roots, method="bootstrap", subsets=["valA"]))
    with pytest.raises(ValidationFailed, match="method"):
        estimate_sigma(_spec(roots, method="magic"))
    rows = (paths.measure_dir / "sigma.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2


def test_bootstrap_records_the_prediction_sha_so_a_replace_invalidates_it(roots, tmp_path):
    """3-8: the estimate is a number computed FROM a prediction file. Without that file's sha in
    its inputs, `ingest --replace` leaves an estimate nobody can check and the cache hands the
    stale one back for ever -- the identity of an estimate must include what it was read from."""
    ds, plan, paths = det_with_runs(roots, tmp_path, n=40)
    spec = _spec(roots, method="bootstrap", run_id="noisy", subsets=["valB"], resamples=20)
    first = estimate_sigma_result(spec)
    card = load_run(roots.data, "noisy")
    assert first.estimate.inputs["prediction_sha"] == card.predictions["valB"].sha256
    assert estimate_sigma_result(spec).cached is True  # nothing changed -> the stored row

    src = tmp_path / "noisy-valB-again.jsonl"
    write_predictions(src, perfect_predictions(ds.subset("valB", plan), ds.card))
    ingest(
        IngestSpec(
            run_id="noisy",
            dataset="tiny",
            plan_id="fixed-v1",
            subset="valB",
            format="jsonl",
            src=src,
            replace=True,
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    second = estimate_sigma_result(spec)
    assert second.cached is False
    assert second.estimate.estimate_id != first.estimate.estimate_id
    assert second.estimate.inputs["prediction_sha"] != first.estimate.inputs["prediction_sha"]
    # append-only: the estimate taken on the old predictions is still in the ledger.
    ids = [row.estimate_id for row in _sigma_rows(paths)]
    assert first.estimate.estimate_id in ids and second.estimate.estimate_id in ids


def test_repeating_an_estimate_is_cached_and_appends_nothing(roots, tmp_path):
    """The identity of an estimate is what it IS, never when it was taken: re-running one must
    find the stored row, not append a second copy to an append-only ledger."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    first = estimate_sigma_result(_spec(roots, method="prior", prior=0.008, note="history"))
    again = estimate_sigma_result(_spec(roots, method="prior", prior=0.008, note="history"))
    assert first.cached is False and again.cached is True
    assert again.estimate.estimate_id == first.estimate.estimate_id
    assert again.estimate.ts == first.estimate.ts  # the stored row, not a restamped one
    assert len(_sigma_rows(paths)) == 1
    other = estimate_sigma(_spec(roots, method="prior", prior=0.009, note="history"))
    assert other.estimate_id != first.estimate.estimate_id
    assert len(_sigma_rows(paths)) == 2
    latest = latest_sigma(paths, "fixed-v1", "coco_map", PK, "prior")
    assert latest is not None and latest.value == 0.009


def test_latest_sigma_breaks_a_timestamp_tie_by_ledger_order(roots, tmp_path):
    """Stamps are millisecond precision, so two estimates can share one. In an append-only
    ledger the row appended last is the latest, whatever the clock managed to record."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    path = paths.measure_dir / "sigma.jsonl"
    for value in (0.001, 0.002):
        append_row(
            path,
            SigmaEstimate(
                estimate_id=f"id-{value}",
                ts="2026-09-04T00:00:00.000Z",
                plan_id="fixed-v1",
                metric="coco_map",
                params=PARAMS,
                method="prior",
                value=value,
                inputs={"note": "tie"},
                note="tie",
            ),
        )
    latest = latest_sigma(paths, "fixed-v1", "coco_map", PK, "prior")
    assert latest is not None and latest.value == 0.002


def test_non_finite_and_negative_sigma_are_refused(roots, tmp_path):
    """sigma_p is a magnitude. nan/inf would be written as JSON null and fail to load back,
    and a negative one would let every candidate clear the judge's sigma_p bar."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    for bad in (float("nan"), float("inf"), -0.01):
        with pytest.raises(ValidationFailed, match="magnitude"):
            estimate_sigma(_spec(roots, method="prior", prior=bad, note="bad"))
    assert _sigma_rows(paths) == []


def test_bootstrap_refuses_a_run_from_another_plan(roots, tmp_path):
    """A sigma row is filed under a plan id and the judge looks it up by one, so resampling a
    run that was made under a different plan would file noise under the wrong plan."""
    ds, _, paths = det_with_runs(roots, tmp_path, n=40)
    save_plan(
        build_plan(ds, plan_id="fixed-v2", subsets=parse_subsets(DEFAULT_SUBSETS), seed=1), paths
    )
    with pytest.raises(ValidationFailed, match="fixed-v2"):
        estimate_sigma(
            SigmaSpec(
                dataset="tiny",
                plan_id="fixed-v2",
                metric="coco_map",
                method="bootstrap",
                run_id="noisy",
                subsets=["valA"],
                resamples=20,
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )


def test_bootstrap_explicit_subset_must_have_eval_role(roots, tmp_path):
    """Only the default subset path (no --subsets) enforced role == eval; an explicit --subsets
    naming train or a sealed subset must be refused too, or a run trained on it would report
    its own training noise as sigma_p."""
    det_with_runs(roots, tmp_path, n=40)
    with pytest.raises(ValidationFailed, match="role"):
        estimate_sigma(_spec(roots, method="bootstrap", run_id="noisy", subsets=["train"]))


def test_bootstrap_defaults_to_the_anchor_run_when_no_run_is_given(roots, tmp_path):
    """spec 6.3: bootstrap resamples --run, defaulting to the anchor run for this
    (plan, subset, metric, params) when --run is omitted."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    key = anchor_key("fixed-v1", "valB", "coco_map", PK)
    set_anchor(
        paths,
        key,
        Anchor(run_id="noisy", reading_id="fake", value=0.0, tolerance=0.0, set_at=stamp()),
    )
    est = estimate_sigma(_spec(roots, method="bootstrap", subsets=["valB"], resamples=20))
    assert est.value > 0
    assert est.inputs["run_id"] == "noisy"
    assert est.inputs["run_source"] == "anchor"
    # How the run id was resolved is provenance, not identity: naming the anchor's run
    # explicitly is the SAME estimate, so it is recognised as cached and appended nowhere.
    ledger = paths.measure_dir / "sigma.jsonl"
    before = len(ledger.read_text(encoding="utf-8").splitlines())
    explicit = estimate_sigma(
        _spec(roots, method="bootstrap", run_id="noisy", subsets=["valB"], resamples=20)
    )
    assert explicit.estimate_id == est.estimate_id and explicit.value == est.value
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == before
