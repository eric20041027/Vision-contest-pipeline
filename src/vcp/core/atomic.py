"""Write-once files: a same-directory temp file, fsync, then one ``os.replace`` into a name that
must not exist yet. The primitive under every formal file vcp writes exactly once (split plans,
pre-registrations, fuse recipes, backup manifests) and under every file of an immutable artifact.

Not a cross-process lock: the existence check and the ``os.replace`` are two steps. The artifact
layer gets its mutual exclusion from ``os.mkdir`` of the artifact directory; the four write-once
sites accept the window (spec 15).

``replace_text`` is the sibling that is allowed to overwrite: the same temp file, fsync and one
``os.replace``, for the yaml files vcp rewrites in place (``dump_yaml_model``)."""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path

from vcp.core.errors import ValidationFailed

_TMP = re.compile(r"^\.(?P<name>.+)\.(?P<nonce>[0-9a-f]{8})\.tmp$")

# Tests patch this, not ``os.name``: flipping ``os.name`` makes ``Path()`` build the wrong class.
IS_WINDOWS: bool = os.name == "nt"
# A Windows sharing violation (a reader, an indexer, antivirus) is brief: five retries, ~1.55 s.
REPLACE_RETRY_DELAYS: tuple[float, ...] = (0.05, 0.1, 0.2, 0.4, 0.8)


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
    if IS_WINDOWS:
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


def _replace_retrying(src: Path, dst: Path) -> None:
    """``os.replace``; on Windows a ``PermissionError`` (another process has the target open) is
    retried after each of ``REPLACE_RETRY_DELAYS``. On POSIX it is a real permission problem."""
    for delay in REPLACE_RETRY_DELAYS:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not IS_WINDOWS:
                raise
            time.sleep(delay)
    os.replace(src, dst)


def replace_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` (UTF-8, line ends as given), replacing a file that is there.

    The bytes go to ``.<name>.<nonce>.tmp`` beside the target, are fsynced, and are published by
    one ``os.replace``: a reader sees the whole old file or the whole new one. Any failure removes
    the temp file and is raised as it came (an ``OSError`` stays an ``OSError``)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(target)
    try:
        with tmp.open("wb") as f:
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        _replace_retrying(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(target.parent)


_NEWLINE = 0x0A


def _truncate_retrying(path: Path, size: int) -> None:
    """``os.truncate``; on Windows a ``PermissionError`` (a reader, an indexer or antivirus has
    the file open) is retried after each of ``REPLACE_RETRY_DELAYS``, as ``_replace_retrying``
    does. On POSIX it is a real permission problem."""
    for delay in REPLACE_RETRY_DELAYS:
        try:
            os.truncate(path, size)
            return
        except PermissionError:
            if not IS_WINDOWS:
                raise
            time.sleep(delay)
    os.truncate(path, size)


def _own_bytes_start(path: Path, start: int, data: bytes) -> int | None:
    """Where to cut ``path`` after a failed append of ``data`` that was opened at ``start``,
    judged by the bytes that are there now; ``None`` when there is nothing of ours to cut or
    cutting could take another writer's row with it. The rules are in ``append_whole_line``."""
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        if size < start:
            return None  # the file shrank under us: not ours to second-guess
        window = max(start, size - len(data) - 1)
        f.seek(window)
        tail = f.read()  # [start, EOF) when that is short enough to be (a prefix of) ours
    end = window + len(tail)
    if window == start and data.startswith(tail):
        return start if tail else None
    for k in range(min(len(data), len(tail) - 1), 0, -1):
        if tail.endswith(data[:k]) and tail[-k - 1] == _NEWLINE:
            return end - k
    return None


def append_whole_line(path: Path, line: str) -> None:
    """Append ``line`` (which ends in ``\\n``) to ``path``, or leave no part of it behind.

    The one way vcp appends to an append-only jsonl (a ledger, an event log, the daily log). A
    write that dies partway -- a full disk is the usual cause -- would leave half a line, the
    next append would join onto it, every strict reader would then fail on that file for ever
    (``bad ledger row``) and ``backup verify`` would report ``bad_stamps:`` (only the
    submissions ledger has a ``partial_tail`` allowance). So any exception, including one that
    only shows when the file is closed, cuts our own bytes back out. The cleanup is best effort
    (an ``OSError`` while reading or truncating is swallowed): the caller always gets the
    exception that started it, unchanged.

    The file is in append mode, so our bytes land at the end of the file *when they are written*,
    which may be after rows another writer appended since we opened it. The cut is therefore
    decided by content, not by the offset ``start`` taken at open. Let ``tail`` be what is in the
    file from ``start`` to its end:

    - ``tail`` is a prefix of ``line`` (empty, partial, or all of it when only closing failed):
      cut back to ``start``;
    - otherwise, if ``tail`` ends with a non-empty prefix ``p`` of ``line`` and the byte just
      before ``p`` is ``\\n`` (whole rows of another writer landed before our bytes): cut ``p``
      only, those rows stay;
    - otherwise (another writer's row landed after our partial bytes): leave the file alone.
      Cutting would take their complete row with ours; the torn bytes stay, the next strict
      read fails loudly at them, and the file is repaired by hand.

    There is no lock, so a short window remains between reading ``tail`` and truncating: a row
    another writer appends inside it is cut with ours. Writers that must never lose a row
    serialise on a lock (the submissions ledger does). On Windows a ``PermissionError`` from the
    truncate is retried like the one from ``replace_text``.

    The line is encoded before the file is opened, so a payload that cannot be written as UTF-8
    fails with nothing touched. The file is opened in binary append mode, so the bytes are
    exactly ``line`` encoded (no newline translation) on every platform. Nothing is created
    above ``path``: the caller makes the directory.
    """
    data = line.encode("utf-8")
    start: int | None = None
    try:
        with path.open("ab") as f:
            start = f.tell()
            f.write(data)
    except BaseException:
        if start is not None:
            with suppress(OSError):
                cut = _own_bytes_start(path, start, data)
                if cut is not None:
                    _truncate_retrying(path, cut)
        raise


def write_once(path: Path, data: bytes) -> str:
    """``write_once_stream`` for bytes already in memory. Returns the sha256."""
    return write_once_stream(path, (data,))[1]


def write_once_text(path: Path, text: str) -> str:
    """UTF-8, line ends as given: the caller builds the text with ``\\n``."""
    return write_once(path, text.encode("utf-8"))
