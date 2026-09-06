"""Content hashing helpers. All hashes are hex strings."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_CHUNK = 1 << 20


def _digest_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    return _digest_file(path, "sha256")


def sha256_prefix(path: Path, n: int) -> str:
    """sha256 of the first ``n`` bytes: an append-only ledger that only grew still matches the
    manifest that hashed it shorter, and the first ``n`` bytes are what the manifest describes."""
    h = hashlib.sha256()
    left = n
    with path.open("rb") as f:
        while left > 0:
            chunk = f.read(min(_CHUNK, left))
            if not chunk:
                break
            h.update(chunk)
            left -= len(chunk)
    return h.hexdigest()


def md5_file(path: Path) -> str:
    return _digest_file(path, "md5")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, unicode kept as-is."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


MANIFEST_MODES = ("full", "sizes")


def dir_manifest(root: Path, *, mode: str = "full") -> list[str]:
    """One line per file under ``root``: ``relpath<TAB>size<TAB>md5`` in mode ``full``, or
    ``relpath<TAB>size<TAB>-`` in mode ``sizes`` (no hashing; for very large raw trees).
    Sorted by posix relpath."""
    if mode not in MANIFEST_MODES:
        raise ValueError(f"manifest mode must be one of {MANIFEST_MODES}, got {mode!r}")

    def digest(p: Path) -> str:
        return md5_file(p) if mode == "full" else "-"

    entries = sorted((p.relative_to(root).as_posix(), p) for p in root.rglob("*") if p.is_file())
    return [f"{rel}\t{p.stat().st_size}\t{digest(p)}" for rel, p in entries]


def manifest_hash(lines: list[str]) -> str:
    return sha256_text("\n".join(lines) + "\n")


def write_manifest(lines: list[str], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return manifest_hash(lines)
