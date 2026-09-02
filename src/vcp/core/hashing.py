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


def md5_file(path: Path) -> str:
    return _digest_file(path, "md5")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, unicode kept as-is."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def dir_manifest(root: Path) -> list[str]:
    """One line per file under ``root``: ``relpath<TAB>size<TAB>md5``, sorted by posix relpath."""
    entries = sorted(
        (p.relative_to(root).as_posix(), p) for p in root.rglob("*") if p.is_file()
    )
    return [f"{rel}\t{p.stat().st_size}\t{md5_file(p)}" for rel, p in entries]


def manifest_hash(lines: list[str]) -> str:
    return sha256_text("\n".join(lines) + "\n")


def write_manifest(lines: list[str], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return manifest_hash(lines)
