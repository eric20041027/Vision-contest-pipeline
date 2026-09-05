"""Checkpoint registration (spec 6.2): identity is (path, sha256); the file is never moved.

A framework writes checkpoints wherever its config says; vcp only records where they are and
what bytes they hold, so a later upload, a status check or a reproduction can tell whether the
file on disk is still the one that was trained.
"""

from __future__ import annotations

import glob as globlib
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import resolve_stored_path, store_path
from vcp.core.time import stamp
from vcp.train.schema import CheckpointRecord, CheckpointSource, TrainRecord

FINAL_AMBIGUOUS = "final_ambiguous"


def expand(patterns: list[str], cwd: Path) -> list[Path]:
    """Files matched by any pattern (relative to ``cwd``, ``**`` allowed), sorted, deduplicated."""
    out: set[Path] = set()
    for pattern in patterns:
        for hit in globlib.glob(str(cwd / pattern), recursive=True):
            path = Path(hit)
            if path.is_file():
                out.add(path.resolve())
    return sorted(out)


def register(
    record: TrainRecord,
    files: list[Path],
    *,
    data_root: Path,
    attempt: int,
    source: CheckpointSource = "glob",
) -> tuple[TrainRecord, list[CheckpointRecord]]:
    """Add every file not already known by (path, sha256); return the record and what was new."""
    known = {(c.path, c.sha256) for c in record.checkpoints}
    added: list[CheckpointRecord] = []
    for f in files:
        stored = store_path(f, data_root)
        digest = sha256_file(f)
        if (stored, digest) in known:
            continue
        added.append(
            CheckpointRecord(
                path=stored,
                sha256=digest,
                bytes=f.stat().st_size,
                registered_at=stamp(),
                attempt=attempt,
                source=source,
            )
        )
        known.add((stored, digest))
    if not added:
        return record, []
    return record.model_copy(update={"checkpoints": [*record.checkpoints, *added]}), added


def mark_final(record: TrainRecord, path: str, sha256: str) -> TrainRecord:
    """Exactly one checkpoint carries ``final=True``: the one with this stored path and sha."""
    marks = [
        c.model_copy(update={"final": c.path == path and c.sha256 == sha256})
        for c in record.checkpoints
    ]
    return record.model_copy(update={"checkpoints": marks})


def resolve_final(
    record: TrainRecord, pattern: str | None, *, cwd: Path, data_root: Path
) -> tuple[TrainRecord, CheckpointRecord | None]:
    """spec 6.1 step 8: ``--final`` must match exactly one file; without it, a session-marked
    final (if any) is the answer. The matched file is already registered because the run
    treats ``--final`` as one more ``--checkpoints`` glob."""
    if pattern is None:
        finals = [c for c in record.checkpoints if c.final]
        return record, (finals[-1] if finals else None)
    hits = expand([pattern], cwd)
    if len(hits) != 1:
        raise ValidationFailed(
            f"{FINAL_AMBIGUOUS}: --final {pattern!r} matched {len(hits)} files, need exactly 1",
            fields={"checkpoint": pattern},
        )
    stored, digest = store_path(hits[0], data_root), sha256_file(hits[0])
    record = mark_final(record, stored, digest)
    final = next((c for c in record.checkpoints if c.final), None)
    if final is None:
        raise ValidationFailed(
            f"{FINAL_AMBIGUOUS}: --final {pattern!r} matched {stored}, which is not registered",
            fields={"checkpoint": pattern},
        )
    return record, final


def missing(record: TrainRecord, data_root: Path) -> list[str]:
    """Registered checkpoints whose file is gone (cheap: no hashing)."""
    return [
        c.path for c in record.checkpoints if not resolve_stored_path(c.path, data_root).is_file()
    ]


def drift(record: TrainRecord, data_root: Path) -> list[str]:
    """Registered checkpoints whose file is gone or hashes differently now (status --verify)."""
    bad: list[str] = []
    for c in record.checkpoints:
        path = resolve_stored_path(c.path, data_root)
        if not path.is_file() or sha256_file(path) != c.sha256:
            bad.append(c.path)
    return bad
