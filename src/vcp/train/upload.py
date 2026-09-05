"""Upload registered checkpoints to an rclone remote or a local directory, and
verify (spec 6.3).

A copy is not a backup until its bytes are known to match: rclone destinations
are checked with ``rclone hashsum sha256``, local ones by reading the copy back.
rclone is not a dependency; it is shelled out through an injectable runner so
the whole path is testable without a remote.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.hashing import sha256_file
from vcp.core.paths import resolve_stored_path
from vcp.core.time import stamp
from vcp.train.schema import CheckpointRecord, TrainRecord, UploadRecord

NAME_COLLISION = "name_collision"
Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

_REMOTE = re.compile(r"^[A-Za-z0-9_-]+:")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class UploadOutcome:
    records: list[UploadRecord]
    uploaded: int
    skipped: int


def dest_kind(dest: str) -> Literal["rclone", "local"]:
    """``remote:path`` is rclone unless it is a Windows drive like ``C:/``."""
    return "rclone" if _REMOTE.match(dest) and not _DRIVE.match(dest) else "local"


def default_runner(
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _targets(
    checkpoints: list[CheckpointRecord],
) -> dict[str, CheckpointRecord]:
    """Checkpoints by destination file name; two different files with one name
    cannot coexist."""
    by_name: dict[str, CheckpointRecord] = {}
    for c in checkpoints:
        name = Path(c.path).name
        if name in by_name and by_name[name].sha256 != c.sha256:
            raise ValidationFailed(
                f"{NAME_COLLISION}: two checkpoints named {name!r} "
                f"({by_name[name].path} and {c.path}); rename one before uploading",
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
            "register the new file with a new attempt instead of uploading it "
            "under the old sha",
            fields={"checkpoint": c.path},
        )
    return src


def _record(dest: str, kind: str, name: str, sha: str, verified: bool) -> UploadRecord:
    return UploadRecord(
        dest=dest,
        kind=kind,
        name=name,
        sha256=sha,
        verified=verified,
        uploaded_at=stamp(),
    )


def _upload_local(
    record: TrainRecord,
    dest: str,
    targets: dict[str, CheckpointRecord],
    data_root: Path,
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


def _hashsum(runner: Runner, base: str) -> dict[str, str]:
    """``rclone hashsum sha256 <base>`` as {name: sha}; an unlistable base is
    simply empty."""
    proc = runner(["rclone", "hashsum", "sha256", base])
    if proc.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            out[parts[1].strip()] = parts[0]
    return out


def _upload_rclone(
    record: TrainRecord,
    dest: str,
    targets: dict[str, CheckpointRecord],
    data_root: Path,
    runner: Runner,
) -> UploadOutcome:
    sources = {name: _source(c, data_root) for name, c in targets.items()}
    base = f"{dest.rstrip('/')}/{record.run_id}"
    before = _hashsum(runner, base)
    uploaded = skipped = 0
    for name, c in targets.items():
        if before.get(name) == c.sha256:
            skipped += 1
            continue
        proc = runner(
            [
                "rclone",
                "copyto",
                str(sources[name]),
                f"{base}/{name}",
                "--checksum",
            ]
        )
        if proc.returncode != 0:
            raise VcpError(
                f"rclone copyto failed (exit {proc.returncode}) for {name}: "
                f"{proc.stderr.strip()[-300:]}"
            )
        uploaded += 1
    after = _hashsum(runner, base)
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
    """Copy the run's registered checkpoints to ``dest/<run_id>/`` and verify
    every one."""
    chosen = [c for c in record.checkpoints if c.final] if only_final else list(record.checkpoints)
    targets = _targets(chosen)
    if dest_kind(dest) == "local":
        return _upload_local(record, dest, targets, data_root)
    if runner is None:
        if shutil.which("rclone") is None:
            raise VcpError("rclone not found on PATH; install it or use a local --upload directory")
        runner = default_runner
    return _upload_rclone(record, dest, targets, data_root, runner)


def merge_uploads(record: TrainRecord, new: list[UploadRecord]) -> TrainRecord:
    """Newest record per (dest, name, sha256) wins; everything else is kept."""
    fresh = {(r.dest, r.name, r.sha256): r for r in new}
    kept = [r for r in record.uploads if (r.dest, r.name, r.sha256) not in fresh]
    return record.model_copy(update={"uploads": [*kept, *fresh.values()]})
