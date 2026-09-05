"""status / report: read-only views over the ledgers, and the --plugin loader they share."""

import sys
from datetime import timedelta

import pytest

from helpers import det_with_runs
from vcp.core.errors import VcpError
from vcp.core.time import parse_stamp, stamp
from vcp.measure.judge import JudgeSpec, judge_prereg
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.measure.metrics import METRICS, applicable_metrics
from vcp.measure.plugins import load_plugins
from vcp.measure.prereg import create_prereg, prereg_time
from vcp.measure.report import last_vs_last, report_rows, status
from vcp.measure.schema import PreRegistration
from vcp.measure.sigma import SigmaSpec, estimate_sigma_result

# ruling 2: a distinct module name per plugin test, so a leaked det metric from one cannot
# break the other's (or Task 6/7's) exact-equality applicable_metrics("det") assertions.
PLUGIN = "myplug_report"
PLUGIN_SOURCE = "\n".join(
    [
        "from vcp.measure.metrics import register_metric",
        "from vcp.measure.schema import MetricResult",
        "",
        "",
        "class One:",
        "    name, version, tasks, defaults = 'constant_one', '1', frozenset({'det'}), {}",
        "    higher_is_better = True",
        "",
        "    def compute(self, samples, predictions, card, params):",
        "        return MetricResult(value=1.0, n=len(samples))",
        "",
        "",
        "register_metric(One())",
        "",
    ]
)


def _measure(roots, run_id: str) -> None:
    measure_run(MeasureSpec(run_id=run_id, data_root=roots.data, configs_root=roots.configs))


def test_status_orphans_and_report(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=40)
    _measure(roots, "perfect")
    empty = status(paths)
    assert (empty.orphans, empty.preregs, empty.judged, empty.anchors, empty.runs) == (
        [],
        0,
        0,
        0,
        2,
    )
    pr = PreRegistration(
        prereg_id="p1",
        claim="c",
        component="x",
        component_class="model",
        baseline_run="perfect",
        candidate_run="noisy",
        metric="coco_map",
        subsets=["valA", "valB"],
        created_at="2026-09-01T00:00:00.000Z",
    )
    create_prereg(paths, pr, ReadingsLedger(paths.measure_dir / "readings.jsonl"))
    # The age of a claim is measured from the moment it became BINDING -- the prereg log's own
    # clock (Task 11 ruling 5) -- never from the caller-supplied created_at in the yaml.
    logged = parse_stamp(prereg_time(paths, "p1"))
    late, early = stamp(logged + timedelta(hours=49)), stamp(logged + timedelta(hours=47))
    st = status(paths, max_age_hours=48, now=late)
    assert st.orphans == ["p1"] and st.preregs == 1
    assert status(paths, max_age_hours=48, now=early).orphans == []
    _measure(roots, "noisy")
    judge_prereg(
        JudgeSpec(
            dataset="tiny",
            prereg_id="p1",
            resamples=20,
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    st = status(paths, max_age_hours=48, now=late)
    assert st.orphans == [] and st.judged == 1
    rows = report_rows(paths)
    assert {(r["run_id"], r["subset"]) for r in rows} == {
        ("perfect", "valA"),
        ("perfect", "valB"),
        ("noisy", "valA"),
        ("noisy", "valB"),
    }
    assert report_rows(paths, metric="accuracy") == []
    assert report_rows(paths, plan_id="other-v1") == []
    lvl = last_vs_last(paths)
    assert len(lvl) == 2 and {r["subset"] for r in lvl} == {"valA", "valB"}
    assert all(r["verdict"] == "FAIL" and r["delta"] < 0 for r in lvl)


def test_status_reports_the_latest_sigma_per_metric_and_method(roots, tmp_path):
    """Two estimates of the same metric/method: the newest is the one a judge would use, so it
    is the one status shows."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    assert status(paths).sigma == {}
    for prior in (0.008, 0.012):
        estimate_sigma_result(
            SigmaSpec(
                dataset="tiny",
                plan_id="fixed-v1",
                metric="coco_map",
                method="prior",
                prior=prior,
                note="history",
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )
    assert status(paths).sigma == {"coco_map/prior": 0.012}


def test_load_plugins(tmp_path, monkeypatch):
    before = applicable_metrics("det")
    (tmp_path / f"{PLUGIN}.py").write_text(PLUGIN_SOURCE, encoding="utf-8", newline="\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        assert load_plugins([PLUGIN]) == [PLUGIN]
        assert "constant_one" in METRICS and "constant_one" in applicable_metrics("det")
        with pytest.raises(VcpError, match="cannot import plugin"):
            load_plugins(["definitely_missing_module"])
        assert load_plugins(None) == []
    finally:
        # ruling 2: METRICS and sys.modules are process-global; a leaked entry would break every
        # later exact-equality assertion about the registry for the rest of the session.
        METRICS.pop("constant_one", None)
        sys.modules.pop(PLUGIN, None)
    assert applicable_metrics("det") == before
    assert not [name for name in sys.modules if name.startswith("myplug_")]
