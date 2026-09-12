"""Write-once files: a same-directory temp file, fsync, then one ``os.replace`` into a name that
must not exist yet. The primitive under every formal file vcp writes exactly once (split plans,
pre-registrations, fuse recipes, backup manifests) and under every file of an immutable artifact.

Not a cross-process lock: the existence check and the ``os.replace`` are two steps. The artifact
layer gets its mutual exclusion from ``os.mkdir`` of the artifact directory; the four write-once
sites accept the window (spec 15)."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path

from vcp.core.errors import ValidationFailed

_TMP = re.compile(r"^\.(?P<name>.+)\.(?P<nonce>[0-9a-f]{8})\.tmp$")


def is_tmp_name(name: str) -> bool:
    """``.<name>.<8 hex>.tmp``: what an interrupted ``write_once`` leaves behind, and the only
    file name ``vcp artifact clean`` may remove on its own."""
    return _TMP.match(name) is not None


def _tmp_path(target: Path) -> Path:
    return target.parent / f".{target.name}.{os.urandom(4).hex()}.tmp"


def _exists_error(target: Path) -> ValidationFailed:
    return ValidationFailed(
        f"exists: {target} is already written; a write-once file is never rewritten",
        location=str(target),
    )


def _fsync_dir(directory: Path) -> None:
    """Persist the rename itself. Windows cannot open a directory for fsync: skipped there."""
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_once_stream(path: Path, chunks: Iterable[bytes]) -> tuple[int, str]:
    """Write ``chunks`` to ``path``, which must not exist. Returns ``(bytes, sha256)``.

    The bytes go to ``.<name>.<nonce>.tmp`` beside the target, are fsynced, and are published by
    one ``os.replace``; any failure removes the temp file and leaves the target absent."""
    target = Path(path)
    if target.exists():
        raise _exists_error(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(target)
    digest = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as f:
            for chunk in chunks:
                f.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            f.flush()
            os.fsync(f.fileno())
        if target.exists():
            raise _exists_error(target)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(target.parent)
    return size, digest.hexdigest()


def write_once(path: Path, data: bytes) -> str:
    """``write_once_stream`` for bytes already in memory. Returns the sha256."""
    return write_once_stream(path, (data,))[1]


def write_once_text(path: Path, text: str) -> str:
    """UTF-8, line ends as given: the caller builds the text with ``\\n``."""
    return write_once(path, text.encode("utf-8"))
