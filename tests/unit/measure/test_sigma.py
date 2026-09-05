import pytest

from helpers import det_with_runs
from vcp.core.errors import ValidationFailed
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.ledger import ReadingsLedger, append_row, read_rows, reading_id
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.measure.metrics import params_key
from vcp.measure.schema import Reading, SigmaEstimate
from vcp.measure.sigma import (
    SIGMA_ESTIMATORS,
    SIGMA_METHODS,
    SigmaSpec,
    estimate_sigma,
    estimate_sigma_result,
    latest_sigma,
)

PARAMS = {"iou": "50:95", "max_dets": "100"}
PK = "iou=50:95,max_dets=100"


def _fake_reading(run, subset, value, ts):
    return Reading(
        reading_id=reading_id(run, "fixed-v1", subset, "coco_map", "1", PK, f"sha-{run}"),
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
        prediction_sha=f"sha-{run}",
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
    assert SIGMA_METHODS == ("splithalf", "bootstrap", "prior") == tuple(SIGMA_ESTIMATORS)


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
    assert boot.inputs == {"run_id": "noisy", "subset": "valB", "resamples": 20, "seed": 0}
    with pytest.raises(ValidationFailed, match="--run"):
        estimate_sigma(_spec(roots, method="bootstrap", subsets=["valA"]))
    with pytest.raises(ValidationFailed, match="method"):
        estimate_sigma(_spec(roots, method="magic"))
    rows = (paths.measure_dir / "sigma.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2


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
