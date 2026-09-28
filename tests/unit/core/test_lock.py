"""``vcp.core.lock`` (spec 2026-09-28 §3.3, §4.2): one exclusive lock per ledger, between
processes. A waiter gives up after the wait and names the holder; the operating system drops a
dead holder's lock."""

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


def test_a_second_holder_waits_then_aborts_naming_the_first(tmp_path, quick):
    path = tmp_path / "locks" / "x.lock"
    with file_lock(path, command="submit.upload", label="the ledger") as me:
        assert read_holder(path) == me and me.pid == os.getpid()
        with pytest.raises(VcpError) as ei:
            with file_lock(path, command="submit.sync", label="the ledger"):
                pass
    assert ei.value.status == "ABORT"
    assert str(ei.value) == (
        f"locked: the ledger held by submit.upload (pid {me.pid} on {me.host} since {me.since})"
    )
    with file_lock(path, command="submit.sync", label="the ledger") as again:
        assert read_holder(path) == again


def test_the_wait_is_a_minute_in_half_second_steps(tmp_path, monkeypatch):
    assert (lock.WAIT_SECONDS, lock.RETRY_SECONDS) == (60.0, 0.5)
    slept: list[float] = []
    monkeypatch.setattr(lock.time, "sleep", slept.append)
    path = tmp_path / "x.lock"
    with file_lock(path, command="a", label="l"):
        with pytest.raises(VcpError, match="locked: l held by a "):
            with file_lock(path, command="b", label="l"):
                pass
    assert slept == [0.5] * 120


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
