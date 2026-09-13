"""Backend contract and compatibility adapter for provenance indexes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Protocol

from vcp.core.errors import ValidationFailed
from vcp.core.paths import provenance_index_path
from vcp.provenance.diff import load_dataset_diff
from vcp.provenance.graph import ProvenanceGraph
from vcp.provenance.index import (
    IngestResult,
    ProvenanceIndex,
    RebuildResult,
    VerifyIndexResult,
)
from vcp.provenance.schema import StatusRecord


class BackendName(StrEnum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


@dataclass(frozen=True)
class BackendConfig:
    name: BackendName = BackendName.SQLITE
    pg_service: str | None = None


@dataclass(frozen=True)
class MaintenanceResult:
    artifact_id: str
    inserted: bool
    backend: str
    requested_strategy: str
    selected_strategy: str
    strategy_reason: str
    changed_samples: int
    dirty_entities: int
    total_entities: int
    dirty_ratio: float
    estimated_incremental_ms: float | None
    estimated_full_ms: float | None
    policy_version: str
    elapsed_ms: float
    graph_hash: str


class ProvenanceBackend(Protocol):
    name: BackendName
    location_label: str

    def rebuild(self, data_root: Path, configs_root: Path) -> RebuildResult: ...

    def sync(self, data_root: Path, configs_root: Path) -> RebuildResult: ...

    def ingest_diff(
        self,
        artifact_id: str,
        data_root: Path,
        configs_root: Path,
        *,
        requested_strategy: str = "incremental",
        policy_id: str | None = None,
    ) -> MaintenanceResult: ...

    def load_graph(self) -> ProvenanceGraph: ...

    def statuses(self, head_id: str) -> dict[str, StatusRecord]: ...

    def normalized(self) -> dict[str, Any]: ...

    def stats(self) -> dict[str, Any]: ...

    def verify(self, data_root: Path, configs_root: Path) -> VerifyIndexResult: ...


class SQLiteBackend:
    """Adapter exposing the backend contract over the existing SQLite index."""

    name = BackendName.SQLITE

    def __init__(self, index: ProvenanceIndex) -> None:
        self.index = index
        self.location_label = str(index.path)

    def rebuild(self, data_root: Path, configs_root: Path) -> RebuildResult:
        return self.index.rebuild(data_root, configs_root)

    def sync(self, data_root: Path, configs_root: Path) -> RebuildResult:
        return self.index.sync(data_root, configs_root)

    def ingest_diff(
        self,
        artifact_id: str,
        data_root: Path,
        configs_root: Path,
        *,
        requested_strategy: str = "incremental",
        policy_id: str | None = None,
    ) -> MaintenanceResult:
        del policy_id
        if requested_strategy != "incremental":
            raise ValidationFailed(f"unsupported_strategy: {requested_strategy}")
        verified = load_dataset_diff(Path(data_root), artifact_id, verify_inputs=True)
        changed_samples = len(verified.changes)
        started = perf_counter_ns()
        result: IngestResult = self.index.ingest_diff(artifact_id, data_root, configs_root)
        elapsed_ms = (perf_counter_ns() - started) / 1_000_000
        total_entities = len(self.index.load_graph().entities)
        dirty_ratio = result.dirty_entities / total_entities if total_entities else 0.0
        if not result.inserted:
            selected = "NO_OP"
            reason = "duplicate_artifact_no_op"
        elif changed_samples == 0:
            selected = "NO_OP"
            reason = "verified_zero_semantic_changes"
        else:
            selected = "INCREMENTAL"
            reason = "requested_incremental"
        return MaintenanceResult(
            artifact_id=result.artifact_id,
            inserted=result.inserted,
            backend=self.name.value,
            requested_strategy=requested_strategy,
            selected_strategy=selected,
            strategy_reason=reason,
            changed_samples=changed_samples,
            dirty_entities=result.dirty_entities,
            total_entities=total_entities,
            dirty_ratio=dirty_ratio,
            estimated_incremental_ms=None,
            estimated_full_ms=None,
            policy_version="sqlite-compat-v1",
            elapsed_ms=elapsed_ms,
            graph_hash=result.graph_hash,
        )

    def load_graph(self) -> ProvenanceGraph:
        return self.index.load_graph()

    def statuses(self, head_id: str) -> dict[str, StatusRecord]:
        return self.index.statuses(head_id)

    def normalized(self) -> dict[str, Any]:
        return self.index.normalized()

    def stats(self) -> dict[str, Any]:
        return self.index.stats()

    def verify(self, data_root: Path, configs_root: Path) -> VerifyIndexResult:
        return self.index.verify(data_root, configs_root)


def parse_backend(value: BackendName | str) -> BackendName:
    if isinstance(value, BackendName):
        return value
    try:
        return BackendName(value.lower())
    except (AttributeError, ValueError) as exc:
        raise ValidationFailed(f"unsupported_backend: {value}") from exc


def make_backend(config: BackendConfig, data_root: Path) -> ProvenanceBackend:
    name = parse_backend(config.name)
    if name is BackendName.SQLITE:
        return SQLiteBackend(ProvenanceIndex(provenance_index_path(Path(data_root))))
    try:
        module = import_module("vcp.provenance.postgres")
    except ImportError:
        raise ValidationFailed(
            "missing_dependency: install with `uv sync --extra postgres`"
        ) from None
    return module.PostgresProvenanceBackend(config)


__all__ = [
    "BackendConfig",
    "BackendName",
    "MaintenanceResult",
    "ProvenanceBackend",
    "SQLiteBackend",
    "make_backend",
    "parse_backend",
]
