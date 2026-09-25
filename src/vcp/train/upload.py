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
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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


def _parts(path: str) -> list[str]:
    """Folder names and file name of a stored path, without a root or a drive colon."""
    parts = PurePosixPath(path.replace("\\", "/")).parts
    kept = [part.rstrip(":") for part in parts if part.strip("/")]
    if not kept:
        raise ValidationFailed(
            f"checkpoint path {path!r} has no file name", fields={"checkpoint": path}
        )
    return kept


def remote_names(paths: Iterable[str]) -> dict[str, str]:
    """The name each checkpoint path is uploaded under, inside ``<dest>/<run_id>/``.

    A file name nobody else in the run uses stays as it is. Checkpoints that share a file name
    (five folds that each write ``model.pt``) take the fewest trailing folders that tell them
    apart, joined with ``__``: ``fold-0__model.pt``. Paths no folder can tell apart are a
    ``name_collision``.
    """
    groups: dict[str, list[str]] = {}
    for path in paths:
        groups.setdefault(_parts(path)[-1], []).append(path)
    names: dict[str, str] = {}
    for base, group in groups.items():
        if len(group) == 1:
            names[group[0]] = base
            continue
        split = {path: _parts(path) for path in group}
        chosen = {path: "__".join(parts) for path, parts in split.items()}  # every folder
        for depth in range(2, max(len(parts) for parts in split.values())):
            candidate = {path: "__".join(parts[-depth:]) for path, parts in split.items()}
            if len(set(candidate.values())) == len(group):
                chosen = candidate
                break
        names.update(chosen)
    clashes = sorted(name for name, count in Counter(names.values()).items() if count > 1)
    if clashes:
        raise ValidationFailed(
            f"{NAME_COLLISION}: checkpoints {sorted(p for p, n in names.items() if n in clashes)} "
            "cannot be told apart by their folders; rename one before uploading",
            fields={"checkpoint": clashes[0]},
        )
    return names


def _targets(record: TrainRecord, only_final: bool) -> dict[str, CheckpointRecord]:
    """Checkpoints by remote name: the newest record of every path.

    ``record.checkpoints`` is chronological (registration appends), so a later record of the SAME
    path is a ``--resume`` that changed the weights -- history, not a second checkpoint. Names
    are worked out over every path of the run, so ``--final`` uploads under the same name a full
    upload would.
    """
    current: dict[str, CheckpointRecord] = {}
    for c in record.checkpoints:
        current[c.path] = c
    names = remote_names(current)
    return {names[path]: c for path, c in current.items() if c.final or not only_final}


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
    targets = _targets(record, only_final)
    target = open_dest(dest, runner)
    if isinstance(target, LocalDest):
        return _upload_local(record, dest, targets, data_root)
    return _upload_rclone(record, dest, targets, data_root, target)


def merge_uploads(record: TrainRecord, new: list[UploadRecord]) -> TrainRecord:
    """Newest record per (dest, name, sha256) wins; everything else is kept."""
    fresh = {(r.dest, r.name, r.sha256): r for r in new}
    kept = [r for r in record.uploads if (r.dest, r.name, r.sha256) not in fresh]
    return record.model_copy(update={"uploads": [*kept, *fresh.values()]})
