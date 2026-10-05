"""Which checkout a provenance index serves (spec 2026-10-04 §3, VCP-044).

A ledger checkpoint is keyed ``configs/<path>`` or ``data/<path>`` and re-anchored at the roots
of the command that reads it, so an index built from one checkout and read from another compares
two different ledgers and reports their differences as a shortened or rewritten ledger. An index
therefore records the roots it was built for -- the ids by ``vcp.core.paths.path_id``, the paths
for people -- and every path that opens it compares them first, before any replay or prefix
check.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.paths import path_id

ROOT_KEYS = ("configs_root_id", "configs_root", "data_root_id", "data_root")


@dataclass(frozen=True)
class IndexRoots:
    """The (data root, configs root) pair one index serves, both resolved."""

    data_root: Path
    configs_root: Path

    @classmethod
    def of(cls, data_root: Path, configs_root: Path) -> IndexRoots:
        return cls(Path(data_root).resolve(), Path(configs_root).resolve())

    @property
    def configs_root_id(self) -> str:
        return path_id(self.configs_root)

    @property
    def data_root_id(self) -> str:
        return path_id(self.data_root)

    def metadata(self) -> dict[str, str]:
        """What an index records: the two ids, and the two paths (for messages only)."""
        return {
            "configs_root_id": self.configs_root_id,
            "configs_root": self.configs_root.as_posix(),
            "data_root_id": self.data_root_id,
            "data_root": self.data_root.as_posix(),
        }

    def matches(self, recorded: Mapping[str, str]) -> bool:
        return (
            recorded.get("configs_root_id") == self.configs_root_id
            and recorded.get("data_root_id") == self.data_root_id
        )


def check_roots(recorded: Mapping[str, str], roots: IndexRoots, *, remedy: str) -> None:
    """``root_mismatch:`` (FAIL) unless the index records exactly these roots. The failure
    carries ``index_root=``: the configs root id the index records, or ``none``."""
    if roots.matches(recorded):
        return
    index_root = recorded.get("configs_root_id")
    if not index_root:
        raise ValidationFailed(
            "root_mismatch: this provenance index records no root (it was built before vcp "
            f"0.13.0); {remedy}",
            fields={"index_root": "none"},
        )
    raise ValidationFailed(
        "root_mismatch: this provenance index was built for configs root "
        f"{recorded.get('configs_root')} and data root {recorded.get('data_root')}, not for "
        f"configs root {roots.configs_root.as_posix()} and data root "
        f"{roots.data_root.as_posix()}; {remedy}",
        fields={"index_root": index_root},
    )
