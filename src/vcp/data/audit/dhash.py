"""Perceptual hashing (64-bit dHash) with a jsonl cache, plus numpy Hamming pair search."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from vcp.core.errors import ValidationFailed

_POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def dhash64(path: Path) -> int:
    """Grey 9x8 thumbnail, adjacent-pixel comparison -> 64-bit integer."""
    with Image.open(path) as im:
        px = np.asarray(im.convert("L").resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    bits = (px[:, 1:] > px[:, :-1]).ravel()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def gray64(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(
            im.convert("L").resize((64, 64), Image.Resampling.LANCZOS), dtype=np.float64
        ).ravel()


def pearson(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    if denom == 0.0:
        return 1.0 if np.array_equal(a, b) else 0.0
    return float((a * b).sum() / denom)


def load_cache(path: Path) -> dict[str, tuple[int, int]]:
    """rel path -> (file size, hash)."""
    if not path.is_file():
        return {}
    out: dict[str, tuple[int, int]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["path"]] = (int(row["size"]), int(row["hash"]))
    return out


def save_cache(path: Path, entries: dict[str, tuple[int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for rel in sorted(entries):
            size, value = entries[rel]
            f.write(json.dumps({"path": rel, "size": size, "hash": value}) + "\n")


def compute_hashes(
    image_root: Path, rel_paths: list[str], cache_path: Path, *, recompute: bool = False
) -> dict[str, int]:
    cache = {} if recompute else load_cache(cache_path)
    out: dict[str, int] = {}
    changed = recompute
    for rel in rel_paths:
        file = image_root / rel
        if not file.is_file():
            raise ValidationFailed(f"image not found: {file}")
        size = file.stat().st_size
        hit = cache.get(rel)
        if hit is not None and hit[0] == size:
            out[rel] = hit[1]
            continue
        out[rel] = dhash64(file)
        cache[rel] = (size, out[rel])
        changed = True
    if changed:
        save_cache(cache_path, cache)
    return out


def _popcount64(x: np.ndarray) -> np.ndarray:
    return _POP[np.ascontiguousarray(x).view(np.uint8)].reshape(*x.shape, 8).sum(axis=-1)


def _pairs(
    keys_a: list[str],
    hashes_a: list[int],
    keys_b: list[str],
    hashes_b: list[int],
    max_dist: int,
    *,
    same_set: bool,
    chunk: int = 512,
) -> list[tuple[str, str, int]]:
    if not keys_a or not keys_b:
        return []
    arr_a = np.array(hashes_a, dtype=np.uint64)
    arr_b = np.array(hashes_b, dtype=np.uint64)
    pairs: list[tuple[str, str, int]] = []
    for start in range(0, len(arr_a), chunk):
        block = arr_a[start : start + chunk]
        dist = _popcount64(block[:, None] ^ arr_b[None, :])
        ii, jj = np.nonzero(dist <= max_dist)
        for i, j in zip(ii.tolist(), jj.tolist(), strict=True):
            gi = start + i
            if same_set and gi >= j:
                continue
            pairs.append((keys_a[gi], keys_b[j], int(dist[i, j])))
    return pairs


def near_pairs(keys: list[str], hashes: list[int], max_dist: int) -> list[tuple[str, str, int]]:
    """Within one set: (key_i, key_j, hamming) for i < j with hamming <= max_dist."""
    return _pairs(keys, hashes, keys, hashes, max_dist, same_set=True)


def cross_pairs(
    keys_a: list[str], hashes_a: list[int], keys_b: list[str], hashes_b: list[int], max_dist: int
) -> list[tuple[str, str, int]]:
    """Across two sets: (key_a, key_b, hamming) with hamming <= max_dist."""
    return _pairs(keys_a, hashes_a, keys_b, hashes_b, max_dist, same_set=False)
