"""Materialize contract: spec, result, output naming."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from vcp.core.hashing import sha256_text
from vcp.core.paths import DatasetPaths

MODES = ("npy", "png")
_SAFE = re.compile(r"[A-Za-z0-9._@+=,-]+")


class MaterializeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    mode: str
    resize: int | None = None
    stack_seq: bool = False
    window: str | None = None
    workers: int = 1
    force: bool = False
    decoder: str | None = None
    data_root: Path | None = None
    configs_root: Path | None = None

    def paths(self) -> DatasetPaths:
        return DatasetPaths.resolve(
            self.name, data_root=self.data_root, configs_root=self.configs_root
        )


class MaterializeResult(BaseModel):
    out_dir: Path
    manifest_path: Path
    materialized: int
    skipped: int
    failed: int
    warnings: list[str]
    orphans_removed: int = 0


def mode_dir_name(mode: str, resize: int | None) -> str:
    return f"{mode}-r{resize}" if resize else mode


def safe_dir_name(sample_id: str) -> str:
    """``/`` -> ``__``; anything else unsafe for a file name -> first 16 hex of sha256."""
    flat = sample_id.replace("/", "__")
    if _SAFE.fullmatch(flat) and flat not in (".", ".."):
        return flat
    return sha256_text(sample_id)[:16]
