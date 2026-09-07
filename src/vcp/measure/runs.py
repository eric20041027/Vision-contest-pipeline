"""Run directories: <data_root>/runs/<run_id>/{run.yaml, predictions/<subset>.jsonl, history}."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import IntegrityError, PlanMismatchError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import run_path, validate_name
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.measure.schema import RunCard

# The `source.framework` a run card carries when `vcp fuse build` wrote it. Owned here, not by
# the fusion layer: the measurement layer must recognise a fused run without importing fusion.
FUSE_FRAMEWORK = "vcp.fuse"


def run_dir(data_root: Path, run_id: str) -> Path:
    return run_path(data_root, run_id)


def prediction_path(data_root: Path, run_id: str, subset: str) -> Path:
    validate_name(subset)
    return run_dir(data_root, run_id) / "predictions" / f"{subset}.jsonl"


def load_run(data_root: Path, run_id: str) -> RunCard:
    path = run_dir(data_root, run_id) / "run.yaml"
    if not path.is_file():
        raise ValidationFailed(f"run not found: {path}")
    card = load_yaml_model(path, RunCard)
    if card.run_id != run_id:
        raise ValidationFailed(f"run.yaml run_id {card.run_id!r} != {run_id!r}", location=str(path))
    return card


def save_run(data_root: Path, card: RunCard) -> Path:
    path = run_dir(data_root, card.run_id) / "run.yaml"
    dump_yaml_model(card, path)
    return path


def assert_run_matches(card: RunCard, dataset: Dataset) -> None:
    """A run must keep pointing at the dataset (by name and content) it was created on.

    Shared by ``eval ingest`` (Task 5) and ``eval measure`` (Task 9) so this check has exactly
    one implementation.
    """
    if card.dataset != dataset.card.name:
        raise PlanMismatchError(
            f"run {card.run_id!r} belongs to dataset {card.dataset!r}, not {dataset.card.name!r}"
        )
    if card.samples_hash != dataset.card.samples_hash:
        raise PlanMismatchError(
            f"run {card.run_id!r} was created on samples_hash {card.samples_hash[:12]}, "
            f"dataset now has {dataset.card.samples_hash[:12]}"
        )


def verify_prediction(data_root: Path, card: RunCard, subset: str) -> Path:
    """Path of the subset's prediction file after checking its recorded sha256."""
    entry = card.predictions.get(subset)
    if entry is None:
        raise ValidationFailed(f"run {card.run_id!r} has no predictions for subset {subset!r}")
    path = run_dir(data_root, card.run_id) / entry.path
    if not path.is_file():
        raise ValidationFailed(f"prediction file missing: {path}")
    actual = sha256_file(path)
    if actual != entry.sha256:
        raise IntegrityError(
            f"predictions {subset!r} sha256 {actual[:12]} != recorded {entry.sha256[:12]}",
            location=str(path),
        )
    return path


def append_history(data_root: Path, run_id: str, row: dict[str, Any]) -> None:
    path = run_dir(data_root, run_id) / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({**row, "ts": stamp()}, ensure_ascii=False) + "\n")
