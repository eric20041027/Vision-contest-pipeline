"""``vcp eval status`` / ``vcp eval report``: read-only views over the ledgers.

Nothing here writes. These two commands open the same append-only files every other measurement
command appends to and answer questions about them: what is outstanding (a claim registered long
ago and never judged, how many anchors and runs this dataset has, the newest sigma_p per metric
and method), and what has been measured (every run x subset reading at full precision, plus the
last-vs-last delta of every judgement). A reading is never recomputed and a claim is never judged
from here -- judging appends a row, and a view that changes what it looks at is not a view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from vcp.core.config import load_yaml_model
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, utc_now
from vcp.measure.anchors import load_anchors
from vcp.measure.ledger import ReadingsLedger, read_rows
from vcp.measure.metrics import params_key
from vcp.measure.prereg import list_preregs, prereg_time
from vcp.measure.schema import Judgement, Reading, RunCard, SigmaEstimate

JUDGEMENTS_LEDGER = "judgements.jsonl"
READINGS_LEDGER = "readings.jsonl"
SIGMA_LEDGER = "sigma.jsonl"


@dataclass(frozen=True)
class StatusResult:
    """What is outstanding for one dataset. ``sigma`` maps ``"<metric>/<method>"`` to the
    newest estimate's value."""

    orphans: list[str]
    preregs: int
    judged: int
    anchors: int
    runs: int
    sigma: dict[str, float] = field(default_factory=dict)


def _runs_for(paths: DatasetPaths) -> int:
    """Runs are stored per data root, not per dataset, so only the ones whose card names this
    dataset are counted."""
    if not paths.runs_dir.is_dir():
        return 0
    cards = sorted(paths.runs_dir.glob("*/run.yaml"))
    return sum(1 for p in cards if load_yaml_model(p, RunCard).dataset == paths.name)


def status(paths: DatasetPaths, *, max_age_hours: int = 48, now: str | None = None) -> StatusResult:
    """Orphan pre-registrations, plus the counts a reader needs to know where a dataset stands.

    An orphan is a claim that became binding more than ``max_age_hours`` ago and has never been
    judged: the claim was written down and then quietly abandoned, which is exactly the shape a
    pre-registration exists to make visible. The age is measured from the prereg log's own
    clock, never from the yaml's caller-supplied ``created_at``; a yaml with no log line was
    never made binding at all, so it has no age to measure and is not reported here.
    """
    current = parse_stamp(now) if now else utc_now()
    judged_ids = {j.prereg_id for j in read_rows(paths.measure_dir / JUDGEMENTS_LEDGER, Judgement)}
    preregs = list_preregs(paths)
    orphans: list[str] = []
    for pid in preregs:
        ts = prereg_time(paths, pid)
        if (
            pid not in judged_ids
            and ts
            and current - parse_stamp(ts) > timedelta(hours=max_age_hours)
        ):
            orphans.append(pid)
    sigma: dict[str, float] = {}
    for est in sorted(
        read_rows(paths.measure_dir / SIGMA_LEDGER, SigmaEstimate), key=lambda e: e.ts
    ):
        # Stamps have millisecond precision and sorted() is stable, so two estimates in the same
        # millisecond keep ledger order -- the last row appended wins, as it does in latest_sigma.
        sigma[f"{est.metric}/{est.method}"] = est.value
    return StatusResult(
        orphans=orphans,
        preregs=len(preregs),
        judged=len(judged_ids),
        anchors=len(load_anchors(paths)),
        runs=_runs_for(paths),
        sigma=sigma,
    )


def report_rows(
    paths: DatasetPaths, *, plan_id: str | None = None, metric: str | None = None
) -> list[dict]:
    """The newest reading of every (run, subset, metric, params) cell, at full precision.

    Rounding is what turns a real difference into "the same number", so the value is passed
    through unrounded and the caller decides how to print it.
    """
    latest: dict[tuple[str, str, str, str], Reading] = {}
    ledger = ReadingsLedger(paths.measure_dir / READINGS_LEDGER)
    for r in sorted(ledger.rows, key=lambda r: r.ts):
        if (plan_id and r.plan_id != plan_id) or (metric and r.metric != metric):
            continue
        latest[(r.run_id, r.subset, r.metric, params_key(r.params))] = r
    return [
        {
            "run_id": r.run_id,
            "subset": r.subset,
            "metric": r.metric,
            "params": params_key(r.params),
            "value": r.value,
            "ts": r.ts,
            "reading_id": r.reading_id,
        }
        for r in sorted(latest.values(), key=lambda r: (r.run_id, r.subset, r.metric))
    ]


def last_vs_last(paths: DatasetPaths) -> list[dict]:
    """One row per judged subset: what the candidate did to the baseline it was registered
    against. ``delta`` is already signed so that positive means better, whichever way the
    metric runs (the judge applies the metric's direction once, when it judges)."""
    out: list[dict] = []
    for j in read_rows(paths.measure_dir / JUDGEMENTS_LEDGER, Judgement):
        for subset, s in j.per_subset.items():
            out.append(
                {
                    "prereg_id": j.prereg_id,
                    "baseline_run": j.baseline_run,
                    "candidate_run": j.candidate_run,
                    "subset": subset,
                    "delta": s.delta,
                    "t": s.t,
                    "verdict": j.verdict,
                    "ts": j.ts,
                }
            )
    return out
