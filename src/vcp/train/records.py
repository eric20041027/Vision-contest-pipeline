"""``train.yaml`` (a snapshot, rewritten whole) and ``train.log.jsonl`` (append-only events).

Both live in the run directory next to ``run.yaml``. The yaml answers "what is the state now";
the events answer "how did it get there" -- every checkpoint, upload and attempt is a row that
is never rewritten, so a machine reclaimed mid-training still leaves a readable history.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.core.time import stamp
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
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_events(data_root: Path, run_id: str) -> list[dict[str, Any]]:
    path = events_path(data_root, run_id)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
