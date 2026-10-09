"""``vcp submit status`` / ``report``: read-only views over the ledger. ``report`` is
last-vs-last (spec 2): every upload against the one before it, never against the best. What
became of each upload -- scored, errored, or nothing yet -- is the newest of the ``scored`` and
``errored`` rows ``assign_scores`` places on it (spec 2026-10-09 §4.2)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, utc_now
from vcp.data.split import load_plan
from vcp.measure.ledger import READINGS_LEDGER, ReadingsLedger
from vcp.measure.metrics import effective_params, get_metric, params_key
from vcp.measure.runs import load_run
from vcp.submit.final import sealed_reading
from vcp.submit.guards import QuotaState, quota_state
from vcp.submit.ledger import SubmissionLedger, assign_scores
from vcp.submit.location import read_only
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow
from vcp.submit.stage import load_staged

# assign_scores moved to vcp.submit.ledger (final needs it too); still importable from here
__all__ = ["ReportRow", "StatusView", "assign_scores", "report", "status"]


@dataclass(frozen=True)
class StatusView:
    staged: int
    uploaded: int
    foreign: int
    quota: QuotaState | None
    deadline_in_hours: float | None
    locked: LedgerRow | None
    current: str | None
    unscored: list[str]
    provenance: dict[str, str]
    errored: list[str]  # ids whose newest upload ended without a score (spec 2026-10-09 §4.3)


def _label(r: LedgerRow) -> str:
    return r.submission_id if r.submission_id else f"foreign:{r.platform_ref}"


def _outcome(ledger: SubmissionLedger, r: LedgerRow) -> LedgerRow | None:
    """What became of this particular ``uploaded`` row: the ``scored`` or ``errored`` row that
    ``assign_scores`` gives it among its submission id's, or None (no result yet)."""
    sid = str(r.submission_id)
    uploads = ledger.uploads(sid)
    assigned = ledger.assigned_outcomes(sid)
    return next((a for u, a in zip(uploads, assigned, strict=True) if u is r), None)


def _score(outcome: LedgerRow | None) -> LedgerRow | None:
    """The outcome when it is a score; an errored upload has none (spec 2026-10-09 §4.4)."""
    return outcome if outcome is not None and outcome.event == "scored" else None


def _public(ledger: SubmissionLedger, r: LedgerRow) -> float | None:
    if r.event == "foreign":
        return r.public
    scored = _score(_outcome(ledger, r))
    return scored.public if scored is not None else None


def _grade(paths: DatasetPaths, sid: str) -> str:
    """The grade stage.json recorded; "-" when it has none or the directory is gone (status
    is read-only and must not depend on the data root being complete)."""
    try:
        return load_staged(paths, sid).provenance or "-"
    except ValidationFailed:
        return "-"


def status(
    dataset: str, *, data_root: Path | None = None, configs_root: Path | None = None
) -> StatusView:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    ledger = read_only(paths, profile)  # spec 2026-09-28 §4.2: no lock, whole rows only
    now = utc_now()
    deadline_in = None
    if profile.deadline is not None:
        deadline_in = (parse_stamp(profile.deadline) - now).total_seconds() / 3600
    arrivals = ledger.arrivals()
    current: str | None = None
    if profile.board_rule == "last":
        current = _label(arrivals[-1]) if arrivals else None
    else:
        best: float | None = None
        for r in arrivals:
            public = _public(ledger, r)
            if public is not None and (best is None or public > best):
                best, current = public, _label(r)
    unscored: list[str] = []
    errored: list[str] = []  # a finished state: listed, never a WARN (spec 2026-10-09 §4.3)
    for sid in ledger.ids():
        if not ledger.uploads(sid):
            continue
        outcome = ledger.latest_outcome(sid)
        if outcome is None:
            unscored.append(sid)
        elif outcome.event == "errored":
            errored.append(sid)
    grades = {sid: _grade(paths, sid) for sid in ledger.ids()}
    return StatusView(
        staged=len(ledger.ids()),
        uploaded=len(ledger.of("uploaded")),
        foreign=sum(1 for r in arrivals if r.event == "foreign"),  # VCP-038: not our own
        quota=quota_state(ledger, profile, now),
        deadline_in_hours=deadline_in,
        locked=ledger.lock_state(),
        current=current,
        unscored=unscored,
        provenance=grades,
        errored=errored,
    )


@dataclass(frozen=True)
class ReportRow:
    submission_id: str
    at: str
    kind: str
    public: float | None
    delta: float | None
    sealed_value: float | None
    private: float | None
    shift: float | None
    # our upload whose outcome is an errored row; a foreign row never is (spec 2026-10-09 §4.4)
    errored: bool = False


def report(
    dataset: str, *, data_root: Path | None = None, configs_root: Path | None = None
) -> list[ReportRow]:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    ledger = read_only(paths, profile)  # spec 2026-09-28 §4.2: no lock, whole rows only
    eval_paths = DatasetPaths.resolve(
        profile.eval_dataset, data_root=data_root, configs_root=configs_root
    )
    sealed_size = len(load_plan(eval_paths, profile.plan_id).ids_in(profile.sealed_subset))
    params_hash = params_key(effective_params(get_metric(profile.metric), profile.metric_params))
    readings = ReadingsLedger(eval_paths.measure_dir / READINGS_LEDGER)
    rows: list[ReportRow] = []
    prev_public: float | None = None
    for r in ledger.arrivals():
        errored = False
        if r.event == "foreign":
            kind, public, private, sealed = "foreign", r.public, r.private, None
        else:
            st = ledger.staged(str(r.submission_id))
            kind = str(st.kind) if st is not None else "?"
            outcome = _outcome(ledger, r)
            errored = outcome is not None and outcome.event == "errored"
            scored = _score(outcome)
            public = scored.public if scored is not None else None
            private = scored.private if scored is not None else None
            sealed = None
            if st is not None:
                card = load_run(paths.data_root, str(st.eval_run))
                reading, why = sealed_reading(readings, card, profile, params_hash, sealed_size)
                sealed = reading.value if reading is not None and why == "" else None
        delta = None if public is None or prev_public is None else public - prev_public
        shift = None if public is None or private is None else private - public
        rows.append(
            ReportRow(_label(r), str(r.at), kind, public, delta, sealed, private, shift, errored)
        )
        prev_public = public
    return rows
