"""``vcp submit status`` / ``report``: read-only views over the ledger. ``report`` is
last-vs-last (spec 2): every upload against the one before it, never against the best."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, utc_now
from vcp.data.split import load_plan
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.metrics import effective_params, get_metric, params_key
from vcp.measure.report import READINGS_LEDGER
from vcp.measure.runs import load_run
from vcp.submit.final import sealed_reading
from vcp.submit.guards import QuotaState, quota_state
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow


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


def _label(r: LedgerRow) -> str:
    return r.submission_id if r.submission_id else f"foreign:{r.platform_ref}"


def _public(ledger: SubmissionLedger, r: LedgerRow) -> float | None:
    if r.event == "foreign":
        return r.public
    latest = ledger.latest_score(str(r.submission_id))
    return latest.public if latest is not None else None


def status(
    dataset: str, *, data_root: Path | None = None, configs_root: Path | None = None
) -> StatusView:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    ledger = SubmissionLedger(paths.submissions_log)
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
    unscored = [
        sid for sid in ledger.ids() if ledger.uploads(sid) and ledger.latest_score(sid) is None
    ]
    return StatusView(
        staged=len(ledger.ids()),
        uploaded=len(ledger.of("uploaded")),
        foreign=len(ledger.of("foreign")),
        quota=quota_state(ledger, profile, now),
        deadline_in_hours=deadline_in,
        locked=ledger.lock_state(),
        current=current,
        unscored=unscored,
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


def report(
    dataset: str, *, data_root: Path | None = None, configs_root: Path | None = None
) -> list[ReportRow]:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    ledger = SubmissionLedger(paths.submissions_log)
    eval_paths = DatasetPaths.resolve(
        profile.eval_dataset, data_root=data_root, configs_root=configs_root
    )
    sealed_size = len(load_plan(eval_paths, profile.plan_id).ids_in(profile.sealed_subset))
    params_hash = params_key(effective_params(get_metric(profile.metric), profile.metric_params))
    readings = ReadingsLedger(eval_paths.measure_dir / READINGS_LEDGER)
    rows: list[ReportRow] = []
    prev_public: float | None = None
    for r in ledger.arrivals():
        if r.event == "foreign":
            kind, public, private, sealed = "foreign", r.public, r.private, None
        else:
            st = ledger.staged(str(r.submission_id))
            kind = str(st.kind) if st is not None else "?"
            latest = ledger.latest_score(str(r.submission_id))
            public = latest.public if latest is not None else None
            private = latest.private if latest is not None else None
            sealed = None
            if st is not None:
                card = load_run(paths.data_root, str(st.eval_run))
                reading, why = sealed_reading(readings, card, profile, params_hash, sealed_size)
                sealed = reading.value if reading is not None and why == "" else None
        delta = None if public is None or prev_public is None else public - prev_public
        shift = None if public is None or private is None else private - public
        rows.append(ReportRow(_label(r), str(r.at), kind, public, delta, sealed, private, shift))
        prev_public = public
    return rows
