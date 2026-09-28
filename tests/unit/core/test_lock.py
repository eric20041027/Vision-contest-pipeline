"""``vcp.core.lock`` (spec 2026-09-28 §3.3, §4.2): one exclusive lock per ledger, between
processes. A waiter gives up after the wait and names the holder; the operating system drops a
dead holder's lock; a process that asks again for a lock it holds is refused at once."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vcp.core import lock
from vcp.core.errors import VcpError
from vcp.core.lock import Holder, file_lock, lock_path, read_holder

# Holds the lock named by argv[1] until a line arrives on stdin. It prints its own pid: on
# Windows the venv's python.exe is a launcher, so Popen.pid is not the interpreter's pid.
HOLDER = """
import os, sys
from pathlib import Path
from vcp.core.lock import file_lock
with file_lock(Path(sys.argv[1]), command="test.holder", label=sys.argv[2]):
    print(f"held {os.getpid()}", flush=True)
    sys.stdin.readline()
"""

# Asks for the lock named by argv[1] with a wait of a fraction of a second, and prints the
# status and the message of the refusal (or "got it").
WAITER = """
import sys
from pathlib import Path
from vcp.core import lock
from vcp.core.errors import VcpError
lock.WAIT_SECONDS, lock.RETRY_SECONDS = 0.3, 0.05
try:
    with lock.file_lock(Path(sys.argv[1]), command="submit.sync", label="the ledger"):
        print("got it")
except VcpError as e:
    print(e.status)
    print(e)
"""


@pytest.fixture
def quick(monkeypatch):
    """A wait of a fraction of a second instead of a minute."""
    monkeypatch.setattr(lock, "WAIT_SECONDS", 0.3)
    monkeypatch.setattr(lock, "RETRY_SECONDS", 0.05)


def _hold(path):
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(path), "the ledger"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    words = proc.stdout.readline().split()
    assert words[:1] == ["held"], words
    return proc, int(words[1])


def _release(proc):
    proc.stdin.write("\n")
    proc.stdin.flush()
    proc.wait(timeout=30)


def _no_wait(seconds):
    raise AssertionError(f"slept {seconds}s: a lock this process holds is refused at once")


def test_the_lock_file_is_in_the_data_root_named_by_the_ledgers_path(tmp_path):
    data = tmp_path / "data"
    ledger = tmp_path / "configs" / "datasets" / "t" / "submissions.jsonl"
    path = lock_path(data, "submissions", ledger)
    key = os.path.normcase(str(ledger.resolve()))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    assert path == data / "locks" / f"submissions-{digest}.lock"
    assert lock_path(data, "submissions", ledger.parent / ".." / "t" / ledger.name) == path
    assert lock_path(data, "submissions", ledger.with_name("other.jsonl")) != path
    if sys.platform == "win32":  # one file, one lock, however its path is spelled
        assert lock_path(data, "submissions", Path(str(ledger).upper())) == path
    with file_lock(path, command="test", label="the ledger"):
        assert path.is_file()
    assert not ledger.parent.exists()  # nothing beside the ledger: it may sit in a git tree


def test_a_second_holder_waits_then_aborts_naming_the_first(tmp_path):
    """The second holder is another process: this one holds the lock, the child waits for it,
    gives up and names who holds it."""
    path = tmp_path / "locks" / "x.lock"
    with file_lock(path, command="submit.upload", label="the ledger") as me:
        assert read_holder(path) == me and me.pid == os.getpid()
        out = subprocess.run(
            [sys.executable, "-c", WAITER, str(path)], capture_output=True, text=True, timeout=60
        )
    assert out.stdout.splitlines() == [
        "ABORT",
        f"locked: the ledger held by submit.upload (pid {me.pid} on {me.host} since {me.since})",
    ], out.stderr
    with file_lock(path, command="submit.sync", label="the ledger") as again:
        assert read_holder(path) == again


def test_the_wait_is_a_minute_in_half_second_steps(tmp_path, monkeypatch):
    assert (lock.WAIT_SECONDS, lock.RETRY_SECONDS) == (60.0, 0.5)
    slept: list[float] = []
    monkeypatch.setattr(lock.time, "sleep", slept.append)
    path = tmp_path / "x.lock"
    proc, pid = _hold(path)
    try:
        with pytest.raises(VcpError, match=rf"locked: l held by test\.holder \(pid {pid} on "):
            with file_lock(path, command="b", label="l"):
                pass
    finally:
        _release(proc)
    assert slept == [0.5] * 120


def test_a_lock_this_process_holds_is_refused_at_once(tmp_path, monkeypatch):
    """Recommendation 1 of the final review: a second ``with`` on a lock this process already
    holds can never succeed, so it is refused without the minute's wait -- however the path is
    spelled -- and the lock is free again once the first ``with`` ends, even by an exception."""
    monkeypatch.setattr(lock.time, "sleep", _no_wait)
    path = tmp_path / "locks" / "x.lock"
    spellings = [path, path.parent / ".." / "locks" / "x.lock"]
    if sys.platform == "win32":
        spellings.append(Path(str(path).upper()))
    with file_lock(path, command="submit.upload", label="the ledger"):
        for spelled in spellings:
            with pytest.raises(VcpError) as ei:
                with file_lock(spelled, command="submit.sync", label="the ledger"):
                    pass
            assert ei.value.status == "ABORT"
            assert str(ei.value) == (
                "locked: the ledger is already held by this process (submit.sync)"
            )
    with pytest.raises(RuntimeError, match="boom"):
        with file_lock(path, command="submit.stage", label="the ledger"):
            raise RuntimeError("boom")
    with file_lock(path, command="submit.sync", label="the ledger") as again:
        assert read_holder(path) == again


def test_a_holder_in_another_process_blocks_until_it_is_killed(tmp_path, monkeypatch, quick):
    path = tmp_path / "locks" / "x.lock"
    proc, pid = _hold(path)
    try:
        expected = rf"locked: the ledger held by test\.holder \(pid {pid} on "
        with pytest.raises(VcpError, match=expected):
            with file_lock(path, command="me", label="the ledger"):
                pass
        assert read_holder(path).pid == pid
    finally:
        proc.kill()
        proc.wait(timeout=30)
    monkeypatch.setattr(lock, "WAIT_SECONDS", 30.0)  # the OS may take a moment to let go
    monkeypatch.setattr(lock, "RETRY_SECONDS", 0.1)
    with file_lock(path, command="me", label="the ledger") as me:
        assert read_holder(path) == me


def test_a_holder_line_that_is_missing_or_half_written_reads_as_unknown(tmp_path):
    path = tmp_path / "x.lock"
    assert read_holder(path) == Holder()
    path.write_bytes(b"")
    assert read_holder(path).describe() == "? (pid ? on ? since ?)"
    path.write_bytes(b'\n{"pid": 7, "host": "h", "comm')
    assert read_holder(path) == Holder()
