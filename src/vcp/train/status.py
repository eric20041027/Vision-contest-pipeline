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


def status(data_root: Path, run_id: str, *, verify: bool = False) -> StatusResult:
    record = load_record(data_root, run_id)
    # Backed-ness is keyed by bytes (sha256), not path: a --resume that changes a checkpoint's
    # bytes adds a second CheckpointRecord for the same path, and a verified upload of the OLD
    # bytes must not mark the NEW record backed. A path can appear twice in ``unbacked`` if two
    # records of it (e.g. the old and the new) are both unbacked.
    verified = {u.sha256 for u in record.uploads if u.verified}
    return StatusResult(
        record=record,
        backed=sum(1 for c in record.checkpoints if c.sha256 in verified),
        unbacked=[c.path for c in record.checkpoints if c.sha256 not in verified],
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
    before = {(u.dest, u.name, u.sha256) for u in record.uploads}
    outcome = upload(record, dest, data_root=data_root, only_final=only_final, runner=runner)
    record = merge_uploads(record, outcome.records)
    save_record(data_root, record)
    attempt = record.attempts[-1].n if record.attempts else 0
    for r in outcome.records:
        if (r.dest, r.name, r.sha256) not in before:
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
