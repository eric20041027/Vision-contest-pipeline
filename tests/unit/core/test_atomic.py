import errno
import hashlib
import json
import re
from pathlib import Path

import pytest

from helpers import full_disk_on_append
from vcp.core import atomic
from vcp.core.errors import ValidationFailed, VcpError


def test_write_once_publishes_bytes_and_returns_sha(tmp_path):
    target = tmp_path / "a" / "b.json"
    data = b'{"x": 1}\n'
    assert atomic.write_once(target, data) == hashlib.sha256(data).hexdigest()
    assert target.read_bytes() == data
    assert [p.name for p in target.parent.iterdir()] == ["b.json"]


def test_second_write_is_refused_and_the_original_is_untouched(tmp_path):
    target = tmp_path / "once.txt"
    atomic.write_once_text(target, "first\n")
    with pytest.raises(ValidationFailed, match="^exists: ") as ei:
        atomic.write_once_text(target, "second\n")
    assert ei.value.location == str(target)
    assert target.read_text(encoding="utf-8") == "first\n"
    assert [p.name for p in tmp_path.iterdir()] == ["once.txt"]


def test_stream_returns_size_and_sha_and_leaves_no_temp(tmp_path):
    target = tmp_path / "big.bin"
    size, sha = atomic.write_once_stream(target, iter([b"a" * 10, b"b" * 5]))
    assert size == 15 and sha == hashlib.sha256(b"a" * 10 + b"b" * 5).hexdigest()
    assert target.read_bytes() == b"a" * 10 + b"b" * 5
    assert not [p for p in tmp_path.iterdir() if atomic.is_tmp_name(p.name)]


def test_failed_publish_leaves_no_target_and_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "x.txt"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(atomic.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        atomic.write_once_text(target, "x\n")
    assert not target.exists() and list(tmp_path.iterdir()) == []


def test_failing_chunk_source_leaves_no_temp(tmp_path):
    target = tmp_path / "y.bin"

    def chunks():
        yield b"partial"
        raise RuntimeError("source died")

    with pytest.raises(RuntimeError, match="source died"):
        atomic.write_once_stream(target, chunks())
    assert not target.exists() and list(tmp_path.iterdir()) == []


def test_text_is_utf8_with_the_given_line_ends(tmp_path):
    target = tmp_path / "t.txt"
    atomic.write_once_text(target, "中文\nline2\n")
    assert target.read_bytes() == "中文\nline2\n".encode()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (".manifest.json.0a1b2c3d.tmp", True),
        (".a.b.c.deadbeef.tmp", True),
        ("manifest.json", False),
        (".x.tmp", False),
        (".a.b.tmp", False),
        (".a.0A1B2C3D.tmp", False),
        ("a.0a1b2c3d.tmp", False),
    ],
)
def test_is_tmp_name(name, expected):
    assert atomic.is_tmp_name(name) is expected


def _full(*_):
    raise OSError(errno.ENOSPC, "No space left on device")


def _flaky(monkeypatch, fail_times, *, windows):
    """``os.replace`` fails ``fail_times`` times with a sharing violation, then really replaces.
    Returns ``(calls, sleeps)``: the source of every call, and every retry delay slept."""
    real_replace = atomic.os.replace
    calls: list[str] = []
    sleeps: list[float] = []

    def replace(src, dst):
        calls.append(str(src))
        if len(calls) <= fail_times:
            raise PermissionError(13, "used by another process")
        real_replace(src, dst)

    monkeypatch.setattr(atomic.os, "replace", replace)
    monkeypatch.setattr(atomic, "IS_WINDOWS", windows)
    monkeypatch.setattr(atomic.time, "sleep", sleeps.append)
    return calls, sleeps


def test_replace_text_overwrites_and_leaves_no_temp(tmp_path):
    target = tmp_path / "a" / "t.yaml"
    atomic.replace_text(target, "one\n")
    atomic.replace_text(target, "中文\ntwo\n")
    assert target.read_bytes() == "中文\ntwo\n".encode()
    assert [p.name for p in target.parent.iterdir()] == ["t.yaml"]


@pytest.mark.parametrize("step", ["fsync", "replace"])
def test_a_failed_replace_keeps_the_old_file_whole_and_no_temp(tmp_path, monkeypatch, step):
    target = tmp_path / "t.yaml"
    target.write_bytes(b"old\n")
    monkeypatch.setattr(atomic.os, step, _full)
    with pytest.raises(OSError) as ei:
        atomic.replace_text(target, "new\n")
    assert ei.value.errno == errno.ENOSPC and not isinstance(ei.value, VcpError)  # C2
    assert target.read_bytes() == b"old\n" and [p.name for p in tmp_path.iterdir()] == ["t.yaml"]


def test_windows_retries_a_shared_target_then_replaces(tmp_path, monkeypatch):
    target = tmp_path / "t.yaml"
    target.write_bytes(b"old\n")
    calls, sleeps = _flaky(monkeypatch, 1, windows=True)
    atomic.replace_text(target, "new\n")
    assert target.read_bytes() == b"new\n" and len(calls) == 2 and sleeps == [0.05]


def test_windows_gives_up_after_five_retries_and_keeps_the_old_file(tmp_path, monkeypatch):
    target = tmp_path / "t.yaml"
    target.write_bytes(b"old\n")
    calls, sleeps = _flaky(monkeypatch, 99, windows=True)
    with pytest.raises(PermissionError):
        atomic.replace_text(target, "new\n")
    assert sleeps == list(atomic.REPLACE_RETRY_DELAYS) == [0.05, 0.1, 0.2, 0.4, 0.8]
    assert len(calls) == 6 and target.read_bytes() == b"old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["t.yaml"]


def test_posix_does_not_retry_a_permission_error(tmp_path, monkeypatch):
    target = tmp_path / "t.yaml"
    target.write_bytes(b"old\n")
    calls, sleeps = _flaky(monkeypatch, 99, windows=False)
    with pytest.raises(PermissionError):
        atomic.replace_text(target, "new\n")
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize("windows", [True, False])
def test_the_directory_fsync_asks_the_same_question_as_the_replace_retry(
    tmp_path, monkeypatch, windows
):
    """One Windows test for both: `IS_WINDOWS` decides whether the rename's directory is fsynced
    (a directory cannot be opened for it on Windows) exactly as it decides the retry."""
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(atomic, "IS_WINDOWS", windows)
    monkeypatch.setattr(atomic.os, "open", lambda path, flags: calls.append(("open", path)) or 7)
    monkeypatch.setattr(atomic.os, "fsync", lambda fd: calls.append(("fsync", fd)))
    monkeypatch.setattr(atomic.os, "close", lambda fd: calls.append(("close", fd)))
    atomic._fsync_dir(tmp_path)
    expected = [] if windows else [("open", tmp_path), ("fsync", 7), ("close", 7)]
    assert calls == expected


def test_the_replace_temp_name_is_one_is_tmp_name_knows(tmp_path, monkeypatch):
    target = tmp_path / "t.yaml"
    target.write_bytes(b"old\n")
    calls, _ = _flaky(monkeypatch, 0, windows=False)
    atomic.replace_text(target, "new\n")
    assert atomic.is_tmp_name(Path(calls[0]).name) and Path(calls[0]).name.startswith(".t.yaml.")


def _rows(path):
    return [json.loads(line) for line in path.read_bytes().decode("utf-8").split("\n")[:-1]]


def _two_lines(tmp_path):
    target = tmp_path / "ledger.jsonl"
    atomic.append_whole_line(target, '{"n": 1}\n')
    atomic.append_whole_line(target, '{"n": 2}\n')
    return target


def test_append_whole_line_adds_the_line_as_given_and_creates_the_file(tmp_path):
    target = tmp_path / "ledger.jsonl"
    atomic.append_whole_line(target, '{"k": "\u4e2d\u6587"}\n')
    atomic.append_whole_line(target, '{"k": 2}\n')
    assert target.read_bytes() == '{"k": "\u4e2d\u6587"}\n{"k": 2}\n'.encode()
    assert b"\r" not in target.read_bytes()  # a bare \n stays a bare \n on Windows too


@pytest.mark.parametrize(
    ("at", "fraction"),
    [("write", 0.0), ("write", 0.5), ("write", 0.99), ("close", 1.0)],
    ids=["no-byte-written", "half-a-line", "all-but-the-newline", "fails-on-close"],
)
def test_a_failed_append_leaves_the_file_as_it_was_and_the_error_unchanged(
    tmp_path, monkeypatch, at, fraction
):
    """An ENOSPC halfway through a row must not leave half a line: the next append would join
    onto it and every strict reader (and ``backup verify``) would fail on that file for ever."""
    target = _two_lines(tmp_path)
    before = target.read_bytes()
    error = full_disk_on_append(monkeypatch, target, fraction=fraction, at=at)

    with pytest.raises(OSError) as ei:
        atomic.append_whole_line(target, json.dumps({"n": 3, "pad": "x" * 300}) + "\n")

    assert ei.value is error and error.errno == errno.ENOSPC
    assert target.read_bytes() == before


def test_an_append_after_a_failed_one_leaves_every_line_parsable(tmp_path, monkeypatch):
    target = _two_lines(tmp_path)
    with monkeypatch.context() as m:
        full_disk_on_append(m, target, fraction=0.5)
        with pytest.raises(OSError):
            atomic.append_whole_line(target, json.dumps({"n": 3, "pad": "x" * 300}) + "\n")

    atomic.append_whole_line(target, '{"n": 4}\n')

    assert target.read_bytes().endswith(b"\n")
    assert [r["n"] for r in _rows(target)] == [1, 2, 4]


def test_a_failed_first_append_leaves_no_row_behind(tmp_path, monkeypatch):
    target = tmp_path / "ledger.jsonl"
    full_disk_on_append(monkeypatch, target, fraction=0.5)
    with pytest.raises(OSError):
        atomic.append_whole_line(target, '{"n": 1}\n')
    assert not target.exists() or target.read_bytes() == b""


def test_a_line_that_cannot_be_encoded_fails_before_the_file_is_touched(tmp_path, monkeypatch):
    """A lone surrogate cannot be written as UTF-8. That is bad input, not a torn write: the
    line is encoded before the file is opened, so there is nothing to truncate."""
    target = _two_lines(tmp_path)
    before = target.read_bytes()
    truncations = []
    monkeypatch.setattr(atomic.os, "truncate", lambda *args: truncations.append(args))

    with pytest.raises(UnicodeEncodeError):
        atomic.append_whole_line(target, '{"n": "\ud800"}\n')

    assert target.read_bytes() == before and truncations == []
    fresh = tmp_path / "fresh.jsonl"
    with pytest.raises(UnicodeEncodeError):
        atomic.append_whole_line(fresh, '{"n": "\ud800"}\n')
    assert not fresh.exists()


OUR_LINE = json.dumps({"n": 3, "pad": "x" * 300}) + "\n"


def _foreign_row(pad: int) -> bytes:
    """Another writer's whole row: ~21 bytes for ``pad=0``, far more than ours for 600."""
    return (json.dumps({"n": 99, "pad": "z" * pad}) + "\n").encode()


@pytest.mark.parametrize("pad", [0, 600], ids=["short-foreign-row", "long-foreign-row"])
@pytest.mark.parametrize(
    ("at", "fraction"),
    [("write", 0.5), ("write", 0.0), ("close", 1.0)],
    ids=["half-a-line", "no-byte-written", "fails-on-close"],
)
def test_a_row_that_landed_before_our_bytes_stays_and_only_our_bytes_go(
    tmp_path, monkeypatch, at, fraction, pad
):
    """The file is in append mode, so our bytes land at EOF at *write* time, behind a row another
    writer appended after our open. Cutting back to the offset taken at open would delete that
    row -- the old size check (``st_size <= start + len(data)``) did exactly that whenever the
    foreign row was shorter than what we had not yet written. Only our own bytes are cut."""
    target = _two_lines(tmp_path)
    before = target.read_bytes()
    foreign = _foreign_row(pad)
    error = full_disk_on_append(
        monkeypatch, target, fraction=fraction, at=at, append_before=foreign
    )

    with pytest.raises(OSError) as ei:
        atomic.append_whole_line(target, OUR_LINE)

    assert ei.value is error
    assert target.read_bytes() == before + foreign
    assert [row["n"] for row in _rows(target)] == [1, 2, 99]  # every line parses


@pytest.mark.parametrize("pad", [0, 600], ids=["short-foreign-row", "long-foreign-row"])
def test_a_row_that_landed_after_our_half_line_is_never_cut(tmp_path, monkeypatch, pad):
    """Cutting back to our start would take the foreign row with our bytes, whatever its length
    (a short one used to pass the old size check). So the file is left as it is: our torn bytes
    stay, the next strict read fails loudly at them, and a person repairs the file."""
    target = _two_lines(tmp_path)
    before = target.read_bytes()
    foreign = _foreign_row(pad)
    error = full_disk_on_append(monkeypatch, target, fraction=0.5, also_append=foreign)

    with pytest.raises(OSError) as ei:
        atomic.append_whole_line(target, OUR_LINE)

    assert ei.value is error
    ours = OUR_LINE.encode()
    assert target.read_bytes() == before + ours[: len(ours) // 2] + foreign  # untouched


def test_a_failing_truncate_does_not_replace_the_original_error(tmp_path, monkeypatch):
    """Cleaning up is best effort: if the truncate fails too, the caller still gets the ENOSPC
    that started it, not the cleanup's error."""
    target = _two_lines(tmp_path)
    error = full_disk_on_append(monkeypatch, target, fraction=0.5)
    monkeypatch.setattr(atomic, "IS_WINDOWS", False)  # a permission error is final on POSIX

    attempts = []

    def refuse(*args, **_kwargs):
        attempts.append(args)
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(atomic.os, "truncate", refuse)
    with pytest.raises(OSError) as ei:
        atomic.append_whole_line(target, OUR_LINE)
    assert ei.value is error
    assert len(attempts) == 1  # it did try to clean up


def _busy_truncate(monkeypatch, times):
    """``os.truncate`` that fails ``times`` times with a sharing violation, then really cuts.
    Returns the list of sizes it was asked for."""
    real = atomic.os.truncate
    asked: list[int] = []

    def truncate(path, size):
        asked.append(size)
        if len(asked) <= times:
            raise PermissionError(errno.EACCES, "used by another process")
        real(path, size)

    monkeypatch.setattr(atomic.os, "truncate", truncate)
    return asked


def test_windows_retries_a_busy_truncate_then_cuts(tmp_path, monkeypatch):
    """A reader, an indexer or antivirus can hold the file for a moment; the cleanup waits it
    out the way ``replace_text`` does, and the caller still sees only the ENOSPC."""
    target = _two_lines(tmp_path)
    before = target.read_bytes()
    error = full_disk_on_append(monkeypatch, target, fraction=0.5)
    sleeps: list[float] = []
    monkeypatch.setattr(atomic, "IS_WINDOWS", True)
    monkeypatch.setattr(atomic, "REPLACE_RETRY_DELAYS", (0.0,) * 5)
    monkeypatch.setattr(atomic.time, "sleep", sleeps.append)
    asked = _busy_truncate(monkeypatch, 2)

    with pytest.raises(OSError) as ei:
        atomic.append_whole_line(target, OUR_LINE)

    assert ei.value is error
    assert len(asked) == 3 and sleeps == [0.0, 0.0]
    assert target.read_bytes() == before


def test_windows_gives_up_on_a_busy_truncate_and_raises_the_original_error(tmp_path, monkeypatch):
    target = _two_lines(tmp_path)
    before = target.read_bytes()
    error = full_disk_on_append(monkeypatch, target, fraction=0.5)
    sleeps: list[float] = []
    monkeypatch.setattr(atomic, "IS_WINDOWS", True)
    monkeypatch.setattr(atomic, "REPLACE_RETRY_DELAYS", (0.0,) * 5)
    monkeypatch.setattr(atomic.time, "sleep", sleeps.append)
    asked = _busy_truncate(monkeypatch, 99)

    with pytest.raises(OSError) as ei:
        atomic.append_whole_line(target, OUR_LINE)

    assert ei.value is error  # not the PermissionError the cleanup ended on
    assert len(asked) == 6 and len(sleeps) == 5
    assert len(target.read_bytes()) > len(before)  # the torn bytes are still there


def test_an_interrupt_halfway_through_a_line_also_truncates_back(tmp_path, monkeypatch):
    """``BaseException``, not ``Exception``: Ctrl-C between the two halves of a write is the
    other way a half line gets left behind."""
    target = _two_lines(tmp_path)
    before = target.read_bytes()
    real_open = Path.open

    class Interrupted:
        def __init__(self, f):
            self._f = f

        def write(self, data):
            self._f.write(data[: len(data) // 2])
            self._f.flush()
            raise KeyboardInterrupt

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()
            return False

        def __getattr__(self, name):
            return getattr(self._f, name)

    def patched(self, mode="r", *args, **kwargs):
        f = real_open(self, mode, *args, **kwargs)
        return Interrupted(f) if self == target and "a" in mode else f

    monkeypatch.setattr(Path, "open", patched)
    with pytest.raises(KeyboardInterrupt):
        atomic.append_whole_line(target, json.dumps({"n": 3, "pad": "x" * 300}) + "\n")
    assert target.read_bytes() == before


SRC = Path(__file__).resolve().parents[3] / "src" / "vcp"
# open("a" / "ab" / "at" / "a+" ...), open(mode="a..."), logging.FileHandler( (append by default)
APPEND_OPEN = re.compile(
    r"""open\([^)]*["'](?:a|ab|at|a\+|ab\+)["']|mode=["'](?:a|ab|at|a\+)["']|logging\.FileHandler\("""
)


def test_no_module_but_atomic_opens_a_file_for_appending():
    """Every append-only file is written through ``append_whole_line``: a plain ``open("a")``
    plus one ``write`` is the half-line bug the primitive exists to end (the daily log has its
    own handler for the same reason: ``logging.FileHandler`` swallows a failed write)."""
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in sorted(SRC.rglob("*.py"))
        if path.name != "atomic.py" and APPEND_OPEN.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


WRITE_ONCE_SITES = (
    "data/split.py",
    "measure/prereg.py",
    "fuse/recipes.py",
    "backup/manifest.py",
)


@pytest.mark.parametrize("rel", WRITE_ONCE_SITES)
def test_the_four_write_once_sites_use_the_primitive(rel):
    text = (SRC / rel).read_text(encoding="utf-8")
    assert "from vcp.core.atomic import write_once_text" in text, rel
    assert "write_once_text(" in text, rel
