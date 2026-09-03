from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.data.audit import dhash as dh


def _gradient(w=32, h=32, transpose=False) -> np.ndarray:
    x = np.linspace(0, 255, w, dtype=np.float64)
    img = np.tile(x, (h, 1))
    if transpose:
        img = img.T
    return np.stack([img, img, img], axis=-1).astype(np.uint8)


def _save(path: Path, arr: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(path)
    return path


@pytest.fixture
def images(tmp_path):
    a = _gradient()
    near = a.copy()
    near[0:2, 0:2] = 200
    files = {
        "a.png": _save(tmp_path / "a.png", a),
        "dup.png": _save(tmp_path / "dup.png", a),
        "near.png": _save(tmp_path / "near.png", near),
        "other.png": _save(tmp_path / "other.png", _gradient(transpose=True)),
    }
    return tmp_path, files


def test_dhash_identity_and_distance(images):
    root, files = images
    h = {k: dh.dhash64(p) for k, p in files.items()}
    assert 0 <= h["a.png"] < 2**64
    assert h["a.png"] == h["dup.png"]
    assert bin(h["a.png"] ^ h["near.png"]).count("1") <= 4
    assert bin(h["a.png"] ^ h["other.png"]).count("1") > 8


def test_pearson(images):
    root, files = images
    a, near, other = (dh.gray64(files[k]) for k in ("a.png", "near.png", "other.png"))
    assert dh.pearson(a, a) == pytest.approx(1.0)
    assert dh.pearson(a, near) > 0.95
    assert dh.pearson(a, other) < 0.5
    flat = np.zeros(4096)
    assert dh.pearson(flat, flat) == 1.0 and dh.pearson(flat, a) == 0.0


def test_pairs(images):
    root, files = images
    keys = list(files)
    hashes = [dh.dhash64(files[k]) for k in keys]
    pairs = dh.near_pairs(keys, hashes, 4)
    assert {(a, b) for a, b, _ in pairs} == {
        ("a.png", "dup.png"),
        ("a.png", "near.png"),
        ("dup.png", "near.png"),
    }
    cross = dh.cross_pairs(["x"], [hashes[0]], keys, hashes, 0)
    assert sorted(b for _, b, _ in cross) == ["a.png", "dup.png"]
    assert dh.near_pairs([], [], 4) == []


def test_cache_reuse_and_recompute(images, monkeypatch):
    root, files = images
    cache = root / "dhash.jsonl"
    first = dh.compute_hashes(root, ["a.png", "near.png"], cache)
    assert cache.is_file() and len(cache.read_text(encoding="utf-8").splitlines()) == 2

    def boom(path):
        raise AssertionError(f"recomputed {path}")

    monkeypatch.setattr(dh, "dhash64", boom)
    assert dh.compute_hashes(root, ["a.png", "near.png"], cache) == first
    with pytest.raises(AssertionError):
        dh.compute_hashes(root, ["a.png"], cache, recompute=True)
    monkeypatch.undo()
    with pytest.raises(ValidationFailed, match="image not found"):
        dh.compute_hashes(root, ["missing.png"], cache)
