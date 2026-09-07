"""Canonical prediction files: read / write / validate against a dataset subset."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.data.dataset import Dataset
from vcp.measure.schema import Prediction, payload_field

# A mapping payload (scores / targets) has no legitimate empty value -- _check_categories
# already requires every category name to be present -- so every subset sample must carry a
# row. A list payload (boxes / masks) may legitimately be empty, so a missing row means
# "predicted nothing". Derived from the task registry so that registering a task stays a
# one-place change; a hand-written task set here would silently give a new task det/seg
# semantics and fold its missing rows into ``empty``.
MAPPING_PAYLOADS = frozenset({"scores", "targets"})


def read_predictions(path: Path) -> list[Prediction]:
    if not path.is_file():
        raise ValidationFailed(f"prediction file not found: {path}")
    out: list[Prediction] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            text = line.rstrip("\r\n")
            if not text.strip():
                continue
            try:
                out.append(Prediction.model_validate_json(text))
            except ValidationError as e:
                raise ValidationFailed(str(e), location=f"{path.name}:{lineno}") from e
    return out


def predictions_text(preds: Iterable[Prediction]) -> str:
    """The exact text ``write_predictions`` writes: sorted by sample_id, one JSON object per
    line (``exclude_none``), LF. Shared with ``fuse.build.content_sha``, which hashes this same
    text without touching disk, so the two can never silently drift apart."""
    ordered = sorted(preds, key=lambda p: p.sample_id)
    return "".join(p.model_dump_json(exclude_none=True) + "\n" for p in ordered)


def write_predictions(path: Path, preds: Iterable[Prediction]) -> str:
    """Sorted by sample_id, one JSON object per line, LF. Returns the file's sha256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(predictions_text(preds))
    return sha256_file(path)


def predictions_by_id(preds: Iterable[Prediction]) -> dict[str, Prediction]:
    return {p.sample_id: p for p in preds}


@dataclass(frozen=True)
class PredictionStats:
    samples: int
    predicted: int
    empty: int
    unknown: list[str] = field(default_factory=list)


def check_predictions(
    preds: list[Prediction], dataset: Dataset, subset_ids: set[str], *, allow_unknown: bool = False
) -> tuple[list[Prediction], PredictionStats]:
    """Enforce spec §4.1: payload matches the task, categories known, ids in the subset,
    no duplicates; cls-like tasks need every sample, det/seg may omit (= empty)."""
    task = dataset.card.task
    field_name = payload_field(task)
    names = {c.name for c in dataset.card.categories}
    ids = {c.id for c in dataset.card.categories}
    seen: set[str] = set()
    kept: list[Prediction] = []
    unknown: list[str] = []
    for p in preds:
        if p.sample_id in seen:
            raise ValidationFailed(f"duplicate prediction for sample {p.sample_id!r}")
        seen.add(p.sample_id)
        if p.sample_id not in subset_ids:
            if allow_unknown:
                unknown.append(p.sample_id)
                continue
            # 4-3: this check also runs under `vcp fuse build`, which has no --opt; the hint
            # names the option a converter reads, not the command line that would set it.
            raise ValidationFailed(
                f"unknown sample_id {p.sample_id!r} (not in this subset); "
                "a converter can allow them with option allow_unknown=true at ingest"
            )
        if p.payload_field() != field_name:
            raise ValidationFailed(
                f"sample {p.sample_id!r}: task {task!r} expects {field_name!r} predictions, "
                f"got {p.payload_field()!r}"
            )
        _check_categories(p, names, ids)
        kept.append(p)
    missing = sorted(subset_ids - seen)
    if field_name in MAPPING_PAYLOADS and missing:
        raise ValidationFailed(f"missing predictions for {len(missing)} samples: {missing[:5]}")
    stats = PredictionStats(
        samples=len(subset_ids), predicted=len(kept), empty=len(missing), unknown=unknown
    )
    return kept, stats


def _check_categories(p: Prediction, names: set[str], ids: set[int]) -> None:
    if p.boxes is not None:
        bad = sorted({b.category_id for b in p.boxes} - ids)
    elif p.masks is not None:
        bad = sorted({m.category_id for m in p.masks} - ids)
    elif p.scores is not None:
        if set(p.scores) != names:
            raise ValidationFailed(
                f"sample {p.sample_id!r}: score keys {sorted(p.scores)} must equal the "
                f"category names {sorted(names)}"
            )
        return
    else:
        extra = sorted(set(p.targets or {}) - names)
        if extra:
            raise ValidationFailed(f"sample {p.sample_id!r}: unknown targets {extra}")
        return
    if bad:
        raise ValidationFailed(f"sample {p.sample_id!r}: unknown category ids {bad}")
