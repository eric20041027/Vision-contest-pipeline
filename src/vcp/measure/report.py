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
from vcp.core.errors import ValidationFailed
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
    newest estimate's value; ``unreadable`` names the run cards that could not be read at all."""

    orphans: list[str]
    preregs: int
    judged: int
    anchors: int
    runs: int
    sigma: dict[str, float] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)


def _runs_for(paths: DatasetPaths) -> tuple[int, list[str]]:
    """Runs of this dataset, and the run cards that could not be read.

    Runs are stored per data root, not per dataset, so this has to open every card under the
    shared ``runs/`` root just to see which ones name this dataset -- and a card belonging to
    another project, half-written by a crashed ingest, or hand-edited would then take a
    read-only view of an unrelated dataset down with it. Counting the unreadable ones instead
    keeps the answer honest (the count is visibly incomplete) without letting one stranger's
    file decide whether this dataset can be looked at.
    """
    if not paths.runs_dir.is_dir():
        return 0, []
    runs, unreadable = 0, []
    for p in sorted(paths.runs_dir.glob("*/run.yaml")):
        try:
            card = load_yaml_model(p, RunCard)
        except (ValidationFailed, OSError):
            unreadable.append(str(p))
            continue
        if card.dataset == paths.name:
            runs += 1
    return runs, unreadable


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
    runs, unreadable = _runs_for(paths)
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
        runs=runs,
        sigma=sigma,
        unreadable=unreadable,
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


def last_vs_last(
    paths: DatasetPaths, *, plan_id: str | None = None, metric: str | None = None
) -> list[dict]:
    """One row per CLAIM per judged subset: what the candidate did to the baseline it was
    registered against, as of the newest judgement of that claim (spec 6's last-vs-last).

    Judging the same claim again appends a second row -- a judgement is an event, and the
    ledger keeps every one -- but a report showing both would put two verdicts for one claim
    side by side with nothing saying which still holds. The newest wins; stamps have
    millisecond precision, so a tie goes to the row appended last, as it does everywhere else
    in this layer.

    ``delta`` is already signed so that positive means better, whichever way the metric runs
    (the judge applies the metric's direction once, when it judges). ``plan_id`` is not on a
    judgement row, so it is resolved through the readings the judgement names: every reading in
    one judgement was taken under the same plan (``judge._assert_one_plan``).
    """
    readings = ReadingsLedger(paths.measure_dir / READINGS_LEDGER).rows
    plan_of = {r.reading_id: r.plan_id for r in readings}
    rows = read_rows(paths.measure_dir / JUDGEMENTS_LEDGER, Judgement)
    latest: dict[str, Judgement] = {}
    for j in sorted(rows, key=lambda j: j.ts):
        if metric and j.metric != metric:
            continue
        if plan_id and plan_id not in {plan_of.get(rid) for rid in j.reading_ids}:
            continue
        latest[j.prereg_id] = j
    out: list[dict] = []
    for j in latest.values():
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
