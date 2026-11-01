import errno
import hashlib
from pathlib import Path

import pytest

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


SRC = Path(__file__).resolve().parents[3] / "src" / "vcp"
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
