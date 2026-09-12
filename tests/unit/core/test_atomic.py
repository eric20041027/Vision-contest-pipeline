import hashlib
from pathlib import Path

import pytest

from vcp.core import atomic
from vcp.core.errors import ValidationFailed


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
