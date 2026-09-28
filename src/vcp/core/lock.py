"""An exclusive lock between processes on one machine (spec 2026-09-28 §4.2), standard library
only: ``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere. The operating system drops the
lock when its process ends, so a killed holder never leaves a stale lock behind.

The lock covers byte 0 of the lock file. Once it has the lock, the holder writes who it is --
pid, host, command and a UTC stamp -- as one JSON line after byte 0, where a waiter can read it
even on Windows (a locked byte range cannot be read through another handle; the rest of the file
can). That line only feeds the ``locked:`` message: the byte-range lock is the lock.

No clock is read to time the wait: it is ``WAIT_SECONDS / RETRY_SECONDS`` tries with a sleep
between them, both read at call time so a test can shrink them. A lock file is never deleted:
removing one while it is held would let a second process lock a new file of the same name."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import socket
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import VcpError
from vcp.core.time import stamp

WAIT_SECONDS = 60.0
RETRY_SECONDS = 0.5
LOCKS_DIR = "locks"


@dataclass(frozen=True)
class Holder:
    """Who holds a lock, by its own account; a field it could not tell is None."""

    pid: int | None = None
    host: str | None = None
    command: str | None = None
    since: str | None = None

    def describe(self) -> str:
        pid = "?" if self.pid is None else str(self.pid)
        return f"{self.command or '?'} (pid {pid} on {self.host or '?'} since {self.since or '?'})"


def lock_path(data_root: Path, prefix: str, target: Path) -> Path:
    """``<data_root>/locks/<prefix>-<first 16 hex of sha256(target's absolute path)>.lock``: one
    lock file per locked file, in the data root and never beside the file (spec §3.3). The path
    is resolved and case-folded the platform's way (``os.path.normcase``) before hashing, so two
    spellings of one Windows path share a lock."""
    key = os.path.normcase(str(Path(target).resolve()))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return Path(data_root) / LOCKS_DIR / f"{prefix}-{digest}.lock"


def read_holder(path: Path) -> Holder:
    """The holder line of a lock file; an empty ``Holder`` when there is none yet or it is half
    written (the waiter's message then says ``?``)."""
    try:
        with Path(path).open("rb") as f:
            f.seek(1)
            doc = json.loads(f.readline().decode("utf-8"))
    except (OSError, ValueError):
        return Holder()
    if not isinstance(doc, dict):
        return Holder()
    pid = doc.get("pid")
    return Holder(
        pid=pid if isinstance(pid, int) else None,
        host=str(doc["host"]) if doc.get("host") else None,
        command=str(doc["command"]) if doc.get("command") else None,
        since=str(doc["since"]) if doc.get("since") else None,
    )


def _write_holder(fd: int, holder: Holder) -> None:
    """Byte 0 is a newline (the locked byte; the holder may write it), then one JSON line."""
    doc = {"pid": holder.pid, "host": holder.host, "command": holder.command, "since": holder.since}
    data = b"\n" + json.dumps(doc).encode("utf-8") + b"\n"
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, data)
    os.ftruncate(fd, len(data))


@contextmanager
def file_lock(path: Path, *, command: str, label: str) -> Iterator[Holder]:
    """Hold ``path`` exclusively for the ``with`` block. Waits up to ``WAIT_SECONDS``, trying
    every ``RETRY_SECONDS``; then ``VcpError`` (ABORT) ``locked: <label> held by <command> (pid
    <n> on <host> since <stamp>)``."""
    lock_file = Path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        tries = max(1, math.ceil(WAIT_SECONDS / RETRY_SECONDS))
        for attempt in range(tries + 1):
            if _try_lock(fd):
                break
            if attempt == tries:
                raise VcpError(f"locked: {label} held by {read_holder(lock_file).describe()}")
            time.sleep(RETRY_SECONDS)
        me = Holder(pid=os.getpid(), host=socket.gethostname(), command=command, since=stamp())
        try:
            _write_holder(fd, me)
            yield me
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


# The platform half comes last on purpose: ruff's E402 would take an `if` block among the
# imports for the end of the import section.
if sys.platform == "win32":
    import msvcrt

    _BUSY = {errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)}

    def _try_lock(fd: int) -> bool:
        """One non-blocking try on byte 0: ``msvcrt.locking`` locks from the file position, and
        locking past the end of an empty file is allowed."""
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            if e.errno in _BUSY:
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)  # writing the holder line moved the position
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        """One non-blocking try; ``flock`` covers the whole file."""
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
