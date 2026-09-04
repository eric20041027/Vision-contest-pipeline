"""Where things live: data root (big, not in git) and configs root (small, in git)."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vcp.core.errors import ValidationFailed

if TYPE_CHECKING:
    from vcp.data.schema import DatasetCard

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
    raise ValidationFailed(
        f"cannot locate configs root: no ancestor of {here} contains pyproject.toml and configs/; "
        f"set {ENV_CONFIGS_ROOT} or pass --configs-root"
    )


def validate_name(name: str) -> None:
    """Dataset / plan ids become path segments; keep them boring."""
    if not _NAME_RE.match(name):
        raise ValidationFailed(
            f"invalid name {name!r}: must match {_NAME_RE.pattern} (letters, digits, . _ -)"
        )


def logs_dir(data_root: Path) -> Path:
    return data_root / "logs"


def runs_root(data_root: Path) -> Path:
    return data_root / "runs"


def run_path(data_root: Path, run_id: str) -> Path:
    validate_name(run_id)
    return runs_root(data_root) / run_id


def store_path(path: Path, data_root: Path) -> str:
    """How a filesystem path is written into a git-tracked card.

    Inside ``data_root`` -> posix path relative to it (portable across machines that follow the
    data-root convention); elsewhere -> absolute posix path.
    """
    resolved = Path(path).expanduser().resolve()
    root = Path(data_root).expanduser().resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def resolve_stored_path(stored: str, data_root: Path) -> Path:
    """Inverse of ``store_path``: relative values are re-anchored at ``data_root``."""
    p = Path(stored)
    return p if p.is_absolute() else Path(data_root) / p


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

    @property
    def runs_dir(self) -> Path:
        return runs_root(self.data_root)

    def run_dir(self, run_id: str) -> Path:
        return run_path(self.data_root, run_id)

    @property
    def measure_dir(self) -> Path:
        return self.data_root / "measure" / self.name

    @property
    def prereg_dir(self) -> Path:
        return self.config_dir / "prereg"

    @property
    def prereg_log(self) -> Path:
        return self.config_dir / "prereg.log.jsonl"

    def resolve_image_root(self, card: DatasetCard) -> Path:
        """Absolute image root for this dataset on this machine (see ``store_path``)."""
        return resolve_stored_path(card.image_root, self.data_root)
