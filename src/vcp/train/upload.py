"""Upload registered checkpoints to an rclone remote or a local directory, and verify (spec 6.3).

A copy is not a backup until its bytes are known to match: rclone destinations are checked with
``rclone hashsum sha256``, local ones by reading the copy back. The rclone side goes through
``vcp.backup.dest`` (the same ``RcloneDest`` push / verify / pull use, command prefix
``vcp.backup.dest.RCLONE``), so its failure modes are theirs now, not this module's own: rclone
missing (and no runner injected) is still ``VcpError("rclone_not_found: ...")`` (ABORT), but an
rclone command that runs and fails is a ``PlatformError`` (FAIL, its last line redacted) rather
than the ``VcpError`` this module used to raise directly; ``hashsum`` exit 3 / 4 means "nothing
there yet", any other non-zero exit -- or a hash column that is not a sha256 -- is an error where
it used to be silently treated as an empty listing.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

# `dest_kind` moved to `backup/dest.py` (spec 14); re-exported so `vcp.train.upload.dest_kind`
# keeps working for anything that still imports it from here.
from vcp.backup.dest import LocalDest, RcloneDest, open_dest
from vcp.backup.dest import dest_kind as dest_kind
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import resolve_stored_path
from vcp.core.proc import Runner
from vcp.core.time import stamp
from vcp.train.schema import CheckpointRecord, TrainRecord, UploadRecord

NAME_COLLISION = "name_collision"


@dataclass(frozen=True)
class UploadOutcome:
    records: list[UploadRecord]
    uploaded: int
    skipped: int


def _targets(checkpoints: list[CheckpointRecord]) -> dict[str, CheckpointRecord]:
    """Checkpoints by destination file name: the newest record of a path wins.

    ``checkpoints`` is chronological (registration appends), so a later record of the SAME path
    with different bytes is a ``--resume`` that changed the weights -- history, not a collision --
    and simply replaces the earlier entry. Two DIFFERENT paths sharing a name with different
    bytes is a genuine collision.
    """
    by_name: dict[str, CheckpointRecord] = {}
    for c in checkpoints:
        name = Path(c.path).name
        existing = by_name.get(name)
        if existing is not None and existing.path != c.path and existing.sha256 != c.sha256:
            raise ValidationFailed(
                f"{NAME_COLLISION}: two checkpoints named {name!r} "
                f"({existing.path} and {c.path}); rename one before uploading",
                fields={"checkpoint": name},
            )
        by_name[name] = c
    return by_name


def _source(c: CheckpointRecord, data_root: Path) -> Path:
    """The registered file, still holding the registered bytes."""
    src = resolve_stored_path(c.path, data_root)
    if not src.is_file():
        raise ValidationFailed(f"checkpoint missing: {src}", fields={"checkpoint": c.path})
    if sha256_file(src) != c.sha256:
        raise ValidationFailed(
            f"checkpoint {c.path} changed since it was registered (sha256 differs); "
            "register the new file with a new attempt instead of uploading it under the old sha",
            fields={"checkpoint": c.path},
        )
    return src


def _record(dest: str, kind: str, name: str, sha: str, verified: bool) -> UploadRecord:
    return UploadRecord(
        dest=dest, kind=kind, name=name, sha256=sha, verified=verified, uploaded_at=stamp()
    )


def _upload_local(
    record: TrainRecord, dest: str, targets: dict[str, CheckpointRecord], data_root: Path
) -> UploadOutcome:
    sources = {name: _source(c, data_root) for name, c in targets.items()}  # all checks first
    base = Path(dest) / record.run_id
    base.mkdir(parents=True, exist_ok=True)
    records: list[UploadRecord] = []
    uploaded = skipped = 0
    for name, c in targets.items():
        target = base / name
        if target.is_file() and sha256_file(target) == c.sha256:
            skipped += 1
            records.append(_record(dest, "local", name, c.sha256, True))
            continue
        shutil.copy2(sources[name], target)
        uploaded += 1
        records.append(_record(dest, "local", name, c.sha256, sha256_file(target) == c.sha256))
    return UploadOutcome(records, uploaded, skipped)


def _upload_rclone(
    record: TrainRecord,
    dest: str,
    targets: dict[str, CheckpointRecord],
    data_root: Path,
    target: RcloneDest,
) -> UploadOutcome:
    sources = {name: _source(c, data_root) for name, c in targets.items()}  # all checks first
    before = target.hashes(record.run_id, list(targets))
    uploaded = skipped = 0
    for name, c in targets.items():
        if before.get(name) == c.sha256:
            skipped += 1
            continue
        target.put(sources[name], record.run_id, name)
        uploaded += 1
    after = target.hashes(record.run_id, list(targets))
    records = [
        _record(dest, "rclone", name, c.sha256, after.get(name) == c.sha256)
        for name, c in targets.items()
    ]
    return UploadOutcome(records, uploaded, skipped)


def upload(
    record: TrainRecord,
    dest: str,
    *,
    data_root: Path,
    only_final: bool = False,
    runner: Runner | None = None,
) -> UploadOutcome:
    """Copy the run's registered checkpoints to ``dest/<run_id>/`` and verify every one."""
    chosen = [c for c in record.checkpoints if c.final] if only_final else list(record.checkpoints)
    targets = _targets(chosen)
    target = open_dest(dest, runner)
    if isinstance(target, LocalDest):
        return _upload_local(record, dest, targets, data_root)
    return _upload_rclone(record, dest, targets, data_root, target)


def merge_uploads(record: TrainRecord, new: list[UploadRecord]) -> TrainRecord:
    """Newest record per (dest, name, sha256) wins; everything else is kept."""
    fresh = {(r.dest, r.name, r.sha256): r for r in new}
    kept = [r for r in record.uploads if (r.dest, r.name, r.sha256) not in fresh]
    return record.model_copy(update={"uploads": [*kept, *fresh.values()]})
