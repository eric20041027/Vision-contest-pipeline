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
from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, utc_now
from vcp.measure.anchors import load_anchors
from vcp.measure.ledger import (
    JUDGEMENTS_LEDGER,
    READINGS_LEDGER,
    SIGMA_LEDGER,
    ReadingsLedger,
    read_rows,
)
from vcp.measure.metrics import params_key
from vcp.measure.prereg import list_preregs, prereg_time
from vcp.measure.provenance import provenance
from vcp.measure.schema import Judgement, Reading, RunCard, SigmaEstimate, SubsetJudgement


@dataclass(frozen=True)
class StatusResult:
    """What is outstanding for one dataset. ``sigma`` maps ``"<metric>/<method>"`` to the
    newest estimate's value; ``unreadable`` names the run cards that could not be read at all.
    ``provenance`` / ``observed`` map each run id to its grade (spec 7.2) and the subsets its
    receipts show it read; ``provenance_failed`` names the runs (counted in ``runs``, absent
    from ``provenance`` / ``observed``) whose grade could not be computed at all -- a fused run
    naming a member that can no longer be read is the case this exists for (a read-only view
    must survive that the way it survives an unreadable run card)."""

    orphans: list[str]
    preregs: int
    judged: int
    anchors: int
    runs: int
    sigma: dict[str, float] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)
    provenance: dict[str, str] = field(default_factory=dict)
    observed: dict[str, list[str]] = field(default_factory=dict)
    provenance_failed: dict[str, str] = field(default_factory=dict)


def _runs_for(paths: DatasetPaths) -> tuple[list[RunCard], list[str]]:
    """Run cards of this dataset, and the run cards that could not be read.

    Runs are stored per data root, not per dataset, so this has to open every card under the
    shared ``runs/`` root just to see which ones name this dataset -- and a card belonging to
    another project, half-written by a crashed ingest, or hand-edited would then take a
    read-only view of an unrelated dataset down with it. Counting the unreadable ones instead
    keeps the answer honest (the count is visibly incomplete) without letting one stranger's
    file decide whether this dataset can be looked at.
    """
    if not paths.runs_dir.is_dir():
        return [], []
    cards: list[RunCard] = []
    unreadable: list[str] = []
    for p in sorted(paths.runs_dir.glob("*/run.yaml")):
        try:
            card = load_yaml_model(p, RunCard)
        except (ValidationFailed, OSError, UnicodeDecodeError):
            # UnicodeDecodeError: a binary-corrupt card is decoded before load_yaml_model's own
            # wrapping runs, and it is a ValueError, not an OSError -- it must count, not abort.
            unreadable.append(str(p))
            continue
        if card.dataset == paths.name:
            cards.append(card)
    return cards, unreadable


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
    cards, unreadable = _runs_for(paths)
    grades: dict[str, str] = {}
    observed: dict[str, list[str]] = {}
    provenance_failed: dict[str, str] = {}
    for card in sorted(cards, key=lambda c: c.run_id):
        try:
            info = provenance(card, data_root=paths.data_root, configs_root=paths.configs_root)
        except (VcpError, OSError, UnicodeDecodeError) as e:
            # A fused run's member can go missing or unreadable without the fused card itself
            # changing at all -- `provenance()` must still FAIL for measure/judge/stage (a
            # broken fusion cannot silently grade as anything), but a read-only view over every
            # run must survive it exactly as it survives an unreadable run card (`unreadable`
            # above): count the run, name what went wrong, and move on.
            provenance_failed[card.run_id] = f"{type(e).__name__}: {e}"
            continue
        grades[card.run_id] = info.grade
        observed[card.run_id] = info.observed
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
        runs=len(cards),
        sigma=sigma,
        unreadable=unreadable,
        provenance=grades,
        observed=observed,
        provenance_failed=provenance_failed,
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
            "provenance": r.provenance or "-",
        }
        for r in sorted(latest.values(), key=lambda r: (r.run_id, r.subset, r.metric))
    ]


def _plan_of_run(paths: DatasetPaths, run_id: str) -> str | None:
    """The plan a run was ingested against, from its card; ``None`` when it cannot be read.

    A read-only view never dies on one unreadable file (the same discipline ``_runs_for`` keeps),
    so a missing or mangled run card simply contributes no plan.
    """
    try:
        return load_yaml_model(paths.runs_dir / run_id / "run.yaml", RunCard).plan_id
    except (ValidationFailed, OSError, UnicodeDecodeError):
        return None


def _judgement_plans(
    paths: DatasetPaths,
    judgement: Judgement,
    plan_of: dict[str, str],
    cache: dict[str, str | None],
) -> set[str]:
    """The plans a judgement belongs to: its readings' plan, or -- when it has none (3-11) --
    the plan of the runs it compares. ``cache`` keeps one card read per run per call."""
    plans = {plan_of[rid] for rid in judgement.reading_ids if rid in plan_of}
    if plans:
        return plans
    for run_id in (judgement.baseline_run, judgement.candidate_run):
        if run_id not in cache:
            cache[run_id] = _plan_of_run(paths, run_id)
    return {p for p in (cache[judgement.baseline_run], cache[judgement.candidate_run]) if p}


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

    3-11: a judgement blocked by ``missing_readings`` names no readings and has no per-subset
    result either, so on both counts it used to be invisible here -- and `--plan` hid exactly
    the claims a reader is looking for. Such a judgement gets one row with ``subset``,
    ``delta`` and ``t`` set to ``None``, and its plan is resolved from the two runs it compares.
    """
    readings = ReadingsLedger(paths.measure_dir / READINGS_LEDGER).rows
    plan_of = {r.reading_id: r.plan_id for r in readings}
    run_plans: dict[str, str | None] = {}
    rows = read_rows(paths.measure_dir / JUDGEMENTS_LEDGER, Judgement)
    latest: dict[str, Judgement] = {}
    for j in sorted(rows, key=lambda j: j.ts):
        if metric and j.metric != metric:
            continue
        if plan_id and plan_id not in _judgement_plans(paths, j, plan_of, run_plans):
            continue
        latest[j.prereg_id] = j
    out: list[dict] = []
    for j in latest.values():
        results: list[tuple[str | None, SubsetJudgement | None]] = list(j.per_subset.items()) or [
            (None, None)
        ]
        for subset, s in results:
            out.append(
                {
                    "prereg_id": j.prereg_id,
                    "baseline_run": j.baseline_run,
                    "candidate_run": j.candidate_run,
                    "subset": subset,
                    "delta": None if s is None else s.delta,
                    "t": None if s is None else s.t,
                    "verdict": j.verdict,
                    "ts": j.ts,
                }
            )
    return out
