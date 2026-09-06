"""``vcp train status`` (read-only) and ``vcp train upload`` (a later or second backup)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.train.checkpoints import drift as _drift
from vcp.train.checkpoints import missing as _missing
from vcp.train.records import append_event, load_record, save_record
from vcp.train.schema import TrainRecord
from vcp.train.upload import Runner, UploadOutcome, merge_uploads, upload


@dataclass(frozen=True)
class StatusResult:
    record: TrainRecord
    backed: int
    unbacked: list[str]
    missing: list[str]
    drift: list[str]
    running: int


def _backed_paths(record: TrainRecord) -> set[str]:
    """Checkpoints that have at least one verified upload of exactly their bytes."""
    verified = {u.sha256 for u in record.uploads if u.verified}
    return {c.path for c in record.checkpoints if c.sha256 in verified}


def status(data_root: Path, run_id: str, *, verify: bool = False) -> StatusResult:
    record = load_record(data_root, run_id)
    backed = _backed_paths(record)
    return StatusResult(
        record=record,
        backed=len(backed),
        unbacked=[c.path for c in record.checkpoints if c.path not in backed],
        missing=_missing(record, data_root),
        drift=_drift(record, data_root) if verify else [],
        running=sum(1 for a in record.attempts if a.status == "running"),
    )


def upload_run(
    data_root: Path,
    run_id: str,
    dest: str,
    *,
    only_final: bool = False,
    runner: Runner | None = None,
) -> tuple[TrainRecord, UploadOutcome]:
    """Upload a recorded run's checkpoints now; the record and the event log both learn of it."""
    record = load_record(data_root, run_id)
    outcome = upload(record, dest, data_root=data_root, only_final=only_final, runner=runner)
    record = merge_uploads(record, outcome.records)
    save_record(data_root, record)
    attempt = record.attempts[-1].n if record.attempts else 0
    for r in outcome.records:
        append_event(
            data_root,
            run_id,
            "uploaded",
            attempt,
            dest=dest,
            name=r.name,
            sha256=r.sha256,
            verified=r.verified,
        )
    return record, outcome
