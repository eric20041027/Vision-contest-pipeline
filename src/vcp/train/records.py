"""``train.yaml`` (a snapshot, rewritten whole) and ``train.log.jsonl`` (append-only events).

Both live in the run directory next to ``run.yaml``. The yaml answers "what is the state now";
the events answer "how did it get there" -- every checkpoint, upload and attempt is a row that
is never rewritten, so a machine reclaimed mid-training still leaves a readable history.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.core.time import stamp
from vcp.data.evidence_ref import EvidenceRef, add_ref, current
from vcp.measure.runs import run_dir
from vcp.train.schema import EVENTS, TrainRecord

TRAIN_YAML = "train.yaml"
EVENTS_LOG = "train.log.jsonl"
TRAIN_DIR = "train"
# Keys ``append_event`` writes itself; a payload carrying one would forge the row's timestamp,
# kind or attempt number, because ``**payload`` comes last (5-6).
RESERVED_EVENT_KEYS = ("ts", "event", "attempt")


def train_yaml(data_root: Path, run_id: str) -> Path:
    return run_dir(data_root, run_id) / TRAIN_YAML


def events_path(data_root: Path, run_id: str) -> Path:
    return run_dir(data_root, run_id) / EVENTS_LOG


def train_dir(data_root: Path, run_id: str) -> Path:
    return run_dir(data_root, run_id) / TRAIN_DIR


def has_record(data_root: Path, run_id: str) -> bool:
    return train_yaml(data_root, run_id).is_file()


def load_record(data_root: Path, run_id: str) -> TrainRecord:
    path = train_yaml(data_root, run_id)
    if not path.is_file():
        raise ValidationFailed(f"training record not found: {path}", fields={"run": run_id})
    record = load_yaml_model(path, TrainRecord)
    if record.run_id != run_id:
        raise ValidationFailed(
            f"train.yaml names run {record.run_id!r}, not {run_id!r}",
            location=str(path),
            fields={"run": run_id},
        )
    return record


def save_record(data_root: Path, record: TrainRecord) -> Path:
    path = train_yaml(data_root, record.run_id)
    dump_yaml_model(record, path)
    return path


def current_attempt(record: TrainRecord) -> int:
    """The attempt a write during the command belongs to: the last one, or 1 before any exists.

    Shared by ``Session.note`` and ``Session.register_checkpoint`` (5-5) so the two cannot
    number the same loop's writes differently.
    """
    return record.attempts[-1].n if record.attempts else 1


def append_event(data_root: Path, run_id: str, event: str, attempt: int, /, **payload: Any) -> None:
    """One row of ``train.log.jsonl``; ``ts`` is this module's clock, never the caller's.

    The four leading arguments are positional-only so ``event=`` / ``attempt=`` in a payload
    reach the reserved-key check below rather than colliding with them as parameters (5-6).
    """
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}; known: {EVENTS}")
    reserved = [k for k in RESERVED_EVENT_KEYS if k in payload]
    if reserved:
        raise ValueError(f"event payload must not carry {reserved}; they are this row's own")
    path = events_path(data_root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": stamp(), "event": event, "attempt": attempt, **payload}
    _append_whole_line(path, json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _append_whole_line(path: Path, line: str) -> None:
    """Append ``line`` (which ends in ``\\n``) to ``path``, or leave the file as it was.

    A write that dies partway -- a full disk is the usual cause -- would leave half a line, the
    next append would join onto it, and ``backup verify`` would then report ``bad_stamps:`` for
    this file for ever (only the submissions ledger has a ``partial_tail`` allowance). So the
    position before the write is remembered and any exception, including one that only shows
    when the file is closed, truncates back to it. The truncate is best effort: if it fails too,
    the caller still gets the exception that started it, unchanged.
    """
    start: int | None = None
    try:
        with path.open("ab") as f:
            start = f.tell()
            f.write(line.encode("utf-8"))
    except BaseException:
        if start is not None:
            with suppress(OSError):
                os.truncate(path, start)
        raise


def read_events(data_root: Path, run_id: str) -> list[dict[str, Any]]:
    path = events_path(data_root, run_id)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_evidence_event(data_root: Path, run_id: str, ref: EvidenceRef, attempt: int) -> None:
    append_event(
        data_root,
        run_id,
        "evidence",
        attempt,
        name=ref.name,
        role=ref.role,
        kind=ref.kind,
        artifact_id=ref.artifact_id,
        manifest_sha256=ref.manifest_sha256,
        binding=ref.binding,
    )


def bind_ref(
    data_root: Path, record: TrainRecord, ref: EvidenceRef, attempt: int
) -> tuple[TrainRecord, EvidenceRef]:
    """Add ``ref`` to ``train.yaml`` (saved) and the event log unless it is already there (spec
    2026-09-26 §3.3); returns the record and the reference now current under that name."""
    refs = add_ref(record.evidence, ref)
    if len(refs) > len(record.evidence):
        record = record.model_copy(update={"evidence": refs})
        save_record(data_root, record)
        append_evidence_event(data_root, record.run_id, ref, attempt)
    return record, next(r for r in current(refs) if r.name == ref.name)
