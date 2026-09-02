"""Where things live: data root (big, not in git) and configs root (small, in git)."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import ValidationFailed

ENV_DATA_ROOT = "VCP_DATA_ROOT"
ENV_CONFIGS_ROOT = "VCP_CONFIGS_ROOT"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def default_data_root() -> Path:
    if sys.platform == "win32":
        return Path("C:/vcp-data")
    return Path.home() / "vcp-data"


def resolve_data_root(override: Path | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve()
    env = os.environ.get(ENV_DATA_ROOT)
    if env:
        return Path(env).expanduser().resolve()
    return default_data_root()


def resolve_configs_root(override: Path | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve()
    env = os.environ.get(ENV_CONFIGS_ROOT)
    if env:
        return Path(env).expanduser().resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "configs").is_dir():
            return candidate / "configs"
    repo_root = Path(__file__).resolve().parents[3]  # src/vcp/core/paths.py -> repo
    return repo_root / "configs"


def validate_name(name: str) -> None:
    """Dataset / plan ids become path segments; keep them boring."""
    if not _NAME_RE.match(name):
        raise ValidationFailed(
            f"invalid name {name!r}: must match {_NAME_RE.pattern} (letters, digits, . _ -)"
        )


def logs_dir(data_root: Path) -> Path:
    return data_root / "logs"


@dataclass(frozen=True)
class DatasetPaths:
    name: str
    data_root: Path
    configs_root: Path

    @classmethod
    def resolve(
        cls, name: str, *, data_root: Path | None = None, configs_root: Path | None = None
    ) -> DatasetPaths:
        validate_name(name)
        return cls(name, resolve_data_root(data_root), resolve_configs_root(configs_root))

    @property
    def raw_dir(self) -> Path:
        return self.data_root / "raw" / self.name

    @property
    def dataset_dir(self) -> Path:
        return self.data_root / "datasets" / self.name

    @property
    def samples_jsonl(self) -> Path:
        return self.dataset_dir / "samples.jsonl"

    @property
    def raw_manifest(self) -> Path:
        return self.dataset_dir / "raw_manifest.txt"

    @property
    def cache_dir(self) -> Path:
        return self.dataset_dir / "cache"

    @property
    def config_dir(self) -> Path:
        return self.configs_root / "datasets" / self.name

    @property
    def card_yaml(self) -> Path:
        return self.config_dir / "dataset.yaml"

    @property
    def splits_dir(self) -> Path:
        return self.config_dir / "splits"

    def plan_json(self, plan_id: str) -> Path:
        validate_name(plan_id)
        return self.splits_dir / f"{plan_id}.json"

    def unseal_jsonl(self, plan_id: str) -> Path:
        validate_name(plan_id)
        return self.splits_dir / f"{plan_id}.unseal.jsonl"
