"""Optional PostgreSQL provenance backend with atomic generation publication."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from threading import RLock
from time import perf_counter_ns
from typing import Any
from uuid import UUID, uuid4

from vcp.artifact import store
from vcp.core.build import build_string
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_text
from vcp.core.time import utc_now
from vcp.provenance.backend import (
    BackendConfig,
    BackendName,
    MaintenanceResult,
    ProvenanceReader,
    RequestedStrategy,
    SelectedStrategy,
)
from vcp.provenance.diff import KIND as DIFF_KIND
from vcp.provenance.diff import load_dataset_diff
from vcp.provenance.graph import (
    ProvenanceGraph,
    add_artifact,
    add_dataset_diff_transition,
    build_graph,
    dataset_version_id,
)
from vcp.provenance.index import (
    RebuildResult,
    VerifyIndexResult,
    _canonical_snapshot,
    _checkpoint_files,
    _fingerprint,
    _fingerprint_hash,
    _last_event_id,
    _path_for_checkpoint,
    _prefix_hash,
    dataset_heads,
    graph_hash,
)
from vcp.provenance.postgres_schema import (
    POSTGRES_SCHEMA_VERSION,
    SCHEMA_NAME,
    WRITER_LOCK_KEY,
    install_schema,
    install_schema_in_transaction,
    validate_schema,
    validate_schema_in_transaction,
)
from vcp.provenance.schema import ProvenanceEdge, ProvenanceEntity, SampleChange, StatusRecord
from vcp.provenance.strategy import (
    BENCHMARK_SCHEMA_VERSION,
    MaintenanceFeatures,
    StrategyDecision,
    load_policy_artifact,
    select_strategy,
)
from vcp.provenance.views import compute_statuses, compute_statuses_for_entities

_SERVICE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_SQLSTATE = re.compile(r"[0-9A-Z]{5}")
_MISSING_DEPENDENCY = "missing_dependency: install with `uv sync --extra postgres`"
_DIAGNOSTIC_SCOPE = ContextVar("vcp_postgres_diagnostics", default=False)
_DIAGNOSTIC_LOCK = RLock()
_diagnostic_users = 0
_diagnostic_factory = None
_previous_record_factory = None
_ADVISORY_LOCK_KEY = WRITER_LOCK_KEY
_FINGERPRINT_MODULUS = 1 << 256
_TABLES = (
    "entities",
    "provenance_edges",
    "sample_changes",
    "dataset_edges",
    "entity_status",
    "ingest_checkpoints",
    "ingested_artifacts",
)

DATASET_ANCESTORS_SQL = f"""WITH RECURSIVE ancestors(entity_id) AS (
    SELECT %s::text
    UNION
    SELECT edge.source_id FROM {SCHEMA_NAME}.dataset_edges AS edge
    JOIN ancestors ON edge.target_id=ancestors.entity_id
    WHERE edge.generation_id=%s
) SELECT entity_id FROM ancestors ORDER BY entity_id"""

DIRTY_CLOSURE_SQL = f"""WITH RECURSIVE ancestors(entity_id) AS (
    SELECT %s::text
    UNION
    SELECT edge.source_id FROM {SCHEMA_NAME}.dataset_edges AS edge
    JOIN ancestors ON edge.target_id=ancestors.entity_id
    WHERE edge.generation_id=%s
), roots(entity_id) AS (
    SELECT entity_id FROM ancestors WHERE %s
    UNION
    SELECT unnest(%s::text[])
), dirty(entity_id) AS (
    SELECT entity_id FROM roots
    UNION
    SELECT edge.target_id FROM {SCHEMA_NAME}.provenance_edges AS edge
    JOIN dirty ON edge.source_id=dirty.entity_id
    WHERE edge.generation_id=%s
) SELECT entity_id FROM dirty ORDER BY entity_id"""

DATASET_DESCENDANTS_SQL = f"""WITH RECURSIVE descendants(entity_id) AS (
    SELECT %s::text
    UNION
    SELECT edge.target_id FROM {SCHEMA_NAME}.dataset_edges AS edge
    JOIN descendants ON edge.source_id=descendants.entity_id
    WHERE edge.generation_id=%s
) SELECT entity_id FROM descendants ORDER BY entity_id"""

AFFECTED_PATH_CHANGES_SQL = f"""WITH RECURSIVE descendants(entity_id) AS (
    SELECT %s::text
    UNION
    SELECT edge.target_id FROM {SCHEMA_NAME}.dataset_edges AS edge
    JOIN descendants ON edge.source_id=descendants.entity_id
    WHERE edge.generation_id=%s
), ancestors(entity_id) AS (
    SELECT %s::text
    UNION
    SELECT edge.source_id FROM {SCHEMA_NAME}.dataset_edges AS edge
    JOIN ancestors ON edge.target_id=ancestors.entity_id
    WHERE edge.generation_id=%s
)
SELECT DISTINCT change.generation_id, change.change_id, change.schema_version,
    change.source_id, change.target_id, change.from_dataset, change.from_samples_hash,
    change.to_dataset, change.to_samples_hash, change.sample_id, change.change_type,
    change.changed_domains, change.changed_fields, change.semantic_effects,
    change.before_row_hash, change.after_row_hash
FROM {SCHEMA_NAME}.dataset_edge_changes AS link
JOIN descendants ON link.source_id=descendants.entity_id
JOIN ancestors ON link.target_id=ancestors.entity_id
JOIN {SCHEMA_NAME}.sample_changes AS change
    ON change.generation_id=link.generation_id AND change.change_id=link.change_id
WHERE link.generation_id=%s ORDER BY change.change_id"""


def _changes_on_paths(
    connection: Any,
    generation_id: UUID | str,
    source: str,
    target: str,
) -> dict[str, SampleChange]:
    rows = tuple(
        connection.execute(
            AFFECTED_PATH_CHANGES_SQL,
            (source, generation_id, target, generation_id, generation_id),
        ).fetchall()
    )
    return deserialize_graph(
        GraphRows(
            generation_id=generation_id,
            gaps=(),
            entities=(),
            provenance_edges=(),
            sample_changes=rows,
            dataset_edges=(),
            dataset_edge_changes=(),
        )
    ).changes


def _merge_status_bases(
    source: dict[str, StatusRecord],
    target: dict[str, StatusRecord],
) -> dict[str, StatusRecord]:
    """Union materialized path severities using the shared view's deterministic tie order."""
    priorities = {"VALID": 0, "REVIEW": 1, "STALE": 2, "BROKEN": 3}
    merged = dict(target)
    for ident, record in source.items():
        previous = merged.get(ident)
        if previous is None or (
            -priorities[record.status.value],
            record.reason,
            record.predecessor_id or "",
        ) < (-priorities[previous.status.value], previous.reason, previous.predecessor_id or ""):
            merged[ident] = record
    return merged


def _reading_repair_seeds(
    graph: ProvenanceGraph,
    base: dict[str, StatusRecord],
    selected: dict[str, StatusRecord],
    *,
    force: set[str] | None = None,
) -> set[str]:
    """Identify reading repair from materialized/selected bases without reading history."""
    current = {**base, **selected}
    seeds = set(force or ())
    for ident, record in selected.items():
        entity = graph.entities[ident]
        if entity.entity_type != "reading" or entity.broken_reason:
            continue
        run_id = f"run:{entity.attributes.get('run_id')}"
        run = current.get(run_id)
        if run_id in graph.entities and graph.entities[run_id].entity_type == "fusion_run":
            # A materialized fusion status includes the final member-propagation pass;
            # it is not the direct run status seen by canonical reading evaluation.
            seeds.add(ident)
        if run is not None and run.status.value != "VALID":
            expected = StatusRecord(
                entity_id=ident,
                status=run.status,
                reason=f"upstream {run_id} is {run.status.value}",
                predecessor_id=run_id,
            )
            if record != expected:
                seeds.add(ident)
    return seeds


def _repair_reading_statuses(
    connection: Any,
    generation_id: UUID | str,
    graph: ProvenanceGraph,
    head: str,
    base: dict[str, StatusRecord],
    selected: dict[str, StatusRecord],
    *,
    force: set[str] | None = None,
) -> None:
    """Repair the view's non-monotonic reading short-circuit without global replay.

    A reading formerly invalidated by evaluation data may instead inherit its run's
    new status. Severity and reason can both decrease; preserving its old base is
    incorrect. Recompute those readings, their run/fusion predecessors, and their
    descendants together so the oracle evaluates readings before fusion propagation.
    Only evidence on paths relevant to that closure is loaded.
    """
    current = {**base, **selected}
    seeds = _reading_repair_seeds(graph, base, selected, force=force)
    if not seeds:
        return
    runs = [f"run:{graph.entities[ident].attributes.get('run_id')}" for ident in seeds]
    while runs:
        run_id = runs.pop()
        if run_id in seeds or run_id not in graph.entities:
            continue
        seeds.add(run_id)
        if graph.entities[run_id].entity_type == "fusion_run":
            runs.extend(
                edge.source_id
                for edge in graph.incoming(run_id)
                if graph.entities[edge.source_id].entity_type in {"run", "fusion_run"}
            )
    repair = _dirty_closure(connection, generation_id, head, seeds, semantic=False)
    exact = ProvenanceGraph(
        entities=dict(graph.entities),
        transitions={transition: [] for transition in graph.transitions},
    )
    for edge in graph.edges.values():
        exact.add_existing_edge(edge)
    datasets = {graph.entities[ident].dataset_version_id for ident in repair}
    for dataset in sorted(item for item in datasets if item is not None):
        exact.changes.update(_changes_on_paths(connection, generation_id, dataset, head))
    for change in exact.changes.values():
        transition = (
            dataset_version_id(change.from_dataset, change.from_samples_hash),
            dataset_version_id(change.to_dataset, change.to_samples_hash),
        )
        exact.transitions[transition].append(change.change_id)
    selected.update(compute_statuses_for_entities(exact, head, repair, base=current))


def _dataset_ancestors(connection: Any, generation_id: UUID | str, source: str) -> set[str]:
    return {
        row[0]
        for row in connection.execute(DATASET_ANCESTORS_SQL, (source, generation_id)).fetchall()
    }


def _dirty_closure(
    connection: Any,
    generation_id: UUID | str,
    source: str,
    delta_ids: set[str],
    *,
    semantic: bool = True,
) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            DIRTY_CLOSURE_SQL, (source, generation_id, semantic, sorted(delta_ids), generation_id)
        ).fetchall()
    }


@dataclass(frozen=True)
class GraphRows:
    """Exact normalized relational representation of one graph generation."""

    generation_id: UUID | str
    gaps: tuple[str, ...]
    entities: tuple[tuple[Any, ...], ...]
    provenance_edges: tuple[tuple[Any, ...], ...]
    sample_changes: tuple[tuple[Any, ...], ...]
    dataset_edges: tuple[tuple[Any, ...], ...]
    dataset_edge_changes: tuple[tuple[Any, ...], ...]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        loaded = json.loads(value)
        if not isinstance(loaded, dict):
            raise IntegrityError("mismatch: PostgreSQL JSON object row")
        return loaded
    if isinstance(value, Mapping):
        return dict(value)
    raise IntegrityError("mismatch: PostgreSQL JSON object row")


def _transition_artifact(graph: ProvenanceGraph, source: str, target: str) -> str:
    artifacts = {
        str(edge.attributes["artifact"])
        for edge in graph.edges.values()
        if edge.edge_type == "DERIVED_FROM"
        and edge.source_id == source
        and edge.target_id == target
        and "artifact" in edge.attributes
    }
    if not artifacts:
        raise IntegrityError(f"mismatch: transition artifact {source} -> {target}")
    return min(artifacts)


def serialize_graph(graph: ProvenanceGraph, *, generation_id: UUID | str) -> GraphRows:
    """Serialize every graph domain field without changing canonical graph semantics."""
    entities = tuple(
        (
            generation_id,
            entity.entity_id,
            entity.entity_type,
            entity.key,
            entity.dataset_version_id,
            _json(entity.attributes),
            entity.broken_reason,
        )
        for entity in (graph.entities[ident] for ident in sorted(graph.entities))
    )
    edges = tuple(
        (
            generation_id,
            edge.edge_id,
            edge.source_id,
            edge.target_id,
            edge.edge_type,
            _json(edge.attributes),
        )
        for edge in (graph.edges[ident] for ident in sorted(graph.edges))
    )
    changes = []
    transition_by_change: dict[str, tuple[str, str]] = {}
    for transition, change_ids in sorted(graph.transitions.items()):
        for change_id in change_ids:
            previous = transition_by_change.setdefault(change_id, transition)
            if previous != transition:
                raise IntegrityError(f"change_conflict: {change_id}")
    for change in (graph.changes[ident] for ident in sorted(graph.changes)):
        try:
            source, target = transition_by_change[change.change_id]
        except KeyError:
            source = dataset_version_id(change.from_dataset, change.from_samples_hash)
            target = dataset_version_id(change.to_dataset, change.to_samples_hash)
        changes.append(
            (
                generation_id,
                change.change_id,
                change.schema_version,
                source,
                target,
                change.from_dataset,
                change.from_samples_hash,
                change.to_dataset,
                change.to_samples_hash,
                change.sample_id,
                change.change_type.value,
                [item.value for item in change.changed_domains],
                list(change.changed_fields),
                [item.value for item in change.semantic_effects],
                change.before_row_hash,
                change.after_row_hash,
            )
        )
    dataset_edges = []
    edge_changes = []
    for (source, target), change_ids in sorted(graph.transitions.items()):
        artifact_id = _transition_artifact(graph, source, target)
        dataset_edges.append((generation_id, source, target, artifact_id))
        edge_changes.extend(
            (generation_id, source, target, change_id) for change_id in sorted(change_ids)
        )
    return GraphRows(
        generation_id=generation_id,
        gaps=tuple(sorted(graph.gaps)),
        entities=entities,
        provenance_edges=edges,
        sample_changes=tuple(changes),
        dataset_edges=tuple(dataset_edges),
        dataset_edge_changes=tuple(edge_changes),
    )


def deserialize_graph(rows: GraphRows) -> ProvenanceGraph:
    """Rebuild the domain graph from normalized relational rows."""
    graph = ProvenanceGraph(gaps=list(rows.gaps))
    for row in rows.entities:
        graph.add_entity(
            ProvenanceEntity(
                entity_id=row[1],
                entity_type=row[2],
                key=row[3],
                dataset_version_id=row[4],
                attributes=_json_object(row[5]),
                broken_reason=row[6],
            )
        )
    for row in rows.provenance_edges:
        graph.add_existing_edge(
            ProvenanceEdge(
                edge_id=row[1],
                source_id=row[2],
                target_id=row[3],
                edge_type=row[4],
                attributes=_json_object(row[5]),
            )
        )
    for row in rows.sample_changes:
        change = SampleChange(
            schema_version=row[2],
            change_id=row[1],
            from_dataset=row[5],
            from_samples_hash=row[6],
            to_dataset=row[7],
            to_samples_hash=row[8],
            sample_id=row[9],
            change_type=row[10],
            changed_domains=list(row[11]),
            changed_fields=list(row[12]),
            semantic_effects=list(row[13]),
            before_row_hash=row[14],
            after_row_hash=row[15],
        )
        graph.changes[change.change_id] = change
    transitions: dict[tuple[str, str], list[str]] = {
        (row[1], row[2]): [] for row in rows.dataset_edges
    }
    for row in rows.dataset_edge_changes:
        transition = (row[1], row[2])
        if transition not in transitions:
            raise IntegrityError(f"mismatch: orphan dataset edge change {row[3]}")
        transitions[transition].append(row[3])
    graph.transitions = {
        transition: sorted(change_ids) for transition, change_ids in sorted(transitions.items())
    }
    return graph


def validate_pg_service(value: str | None) -> str | None:
    """Accept only a libpq service name, never conninfo or credentials."""
    if value is None:
        return None
    if not _SERVICE_NAME.fullmatch(value):
        raise ValidationFailed("invalid_postgres_service")
    return value


def _load_psycopg() -> Any:
    """Load the optional driver only when PostgreSQL is explicitly selected."""
    try:
        return import_module("psycopg")
    except ImportError:
        raise ValidationFailed(_MISSING_DEPENDENCY) from None


def _safe_sqlstate(error: BaseException) -> str:
    try:
        value = getattr(error, "sqlstate", None)
    except Exception:
        return "unknown"
    if isinstance(value, str) and _SQLSTATE.fullmatch(value):
        return value
    return "unknown"


def raise_redacted_database_error(error: BaseException) -> None:
    """Raise a stable database failure without exposing driver-provided text."""
    raise ValidationFailed(
        "database_connection_failed",
        fields={"backend": BackendName.POSTGRESQL.value, "sqlstate": _safe_sqlstate(error)},
    ) from None


def _is_driver_error(driver: Any, error: BaseException) -> bool:
    error_type = getattr(driver, "Error", None)
    if not isinstance(error_type, type):
        return False
    return isinstance(error, error_type)


@contextmanager
def _driver_diagnostics() -> Iterator[None]:
    """Redact Psycopg records before host factories, handlers or stderr see them.

    Psycopg logs conninfo and rollback exceptions before returning control to us.
    A context-local flag covers nested calls and concurrent VCP operations without
    suppressing another thread's diagnostics. The lock covers registration only,
    never database work. No connection metadata is inspected or retained here.
    """
    global _diagnostic_users, _diagnostic_factory, _previous_record_factory
    token = _DIAGNOSTIC_SCOPE.set(True)
    with _DIAGNOSTIC_LOCK:
        if _diagnostic_users == 0:
            previous = logging.getLogRecordFactory()

            def factory(name, level, path, line, message, args, exc_info, func=None, sinfo=None):
                if _DIAGNOSTIC_SCOPE.get() and (name == "psycopg" or name.startswith("psycopg.")):
                    message, args, exc_info, sinfo = (
                        "PostgreSQL driver diagnostic redacted",
                        (),
                        None,
                        None,
                    )
                return previous(name, level, path, line, message, args, exc_info, func, sinfo)

            _previous_record_factory = previous
            _diagnostic_factory = factory
            logging.setLogRecordFactory(factory)
        _diagnostic_users += 1
    try:
        yield
    finally:
        _DIAGNOSTIC_SCOPE.reset(token)
        with _DIAGNOSTIC_LOCK:
            _diagnostic_users -= 1
            if _diagnostic_users == 0:
                # Do not overwrite a logging reconfiguration made by the host.
                if logging.getLogRecordFactory() is _diagnostic_factory:
                    logging.setLogRecordFactory(_previous_record_factory)
                _diagnostic_factory = _previous_record_factory = None


@contextmanager
def _database_operation(driver: Any, cleanup: Callable[[], None]) -> Iterator[None]:
    """Keep operation/rollback failures primary; redact a standalone cleanup failure."""
    with _driver_diagnostics():
        failed = False
        try:
            yield
        except BaseException as error:
            failed = True
            if _is_driver_error(driver, error):
                raise_redacted_database_error(error)
            raise
        finally:
            try:
                cleanup()
            except BaseException as error:
                if not failed:
                    if isinstance(error, Exception):
                        raise_redacted_database_error(error)
                    raise


@contextmanager
def connection_lifecycle(open_connection: Callable[[], Any], driver: Any) -> Iterator[Any]:
    """Own one autocommit connection from connect through its last close attempt."""
    with _driver_diagnostics():
        try:
            connection = open_connection()
        except ValidationFailed:
            raise
        except Exception as error:
            raise_redacted_database_error(error)
        with _database_operation(driver, connection.close):
            yield connection


def _connection(config: BackendConfig, driver: Any):
    return connection_lifecycle(lambda: _connect(config), driver)


def _connect(config: BackendConfig) -> Any:
    """Open one autocommit connection through libpq's standard resolution."""
    service = validate_pg_service(config.pg_service)
    psycopg = _load_psycopg()
    with _driver_diagnostics():
        try:
            if service is None:
                return psycopg.connect(autocommit=True)
            return psycopg.connect(service=service, autocommit=True)
        except Exception as error:
            raise_redacted_database_error(error)


def _scalar(row: Sequence[Any] | Mapping[str, Any] | None) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()), None)
    return row[0]


def _snapshot_hash(snapshot: dict[str, tuple[int, str]]) -> str:
    return sha256_text(_json(snapshot))


def _generation_fingerprint(graph: ProvenanceGraph) -> tuple[int, str]:
    count, total = _fingerprint(graph)
    return count, f"{total % _FINGERPRINT_MODULUS:064x}"


def _status_rows(graph: ProvenanceGraph, generation_id: UUID | str) -> tuple[tuple[Any, ...], ...]:
    rows = []
    for head in _status_heads(graph):
        statuses = compute_statuses(graph, head)
        rows.extend(
            (
                generation_id,
                head,
                ident,
                record.status.value,
                record.reason,
                record.predecessor_id,
            )
            for ident, record in sorted(statuses.items())
        )
    return tuple(rows)


def _status_heads(graph: ProvenanceGraph) -> list[str]:
    """SQLite materializes every dataset version, including historical sources."""
    return sorted(
        ident for ident, entity in graph.entities.items() if entity.entity_type == "dataset"
    )


def _checkpoint_rows(
    generation_id: UUID | str,
    data_root: Path,
    configs_root: Path,
    snapshot: dict[str, tuple[int, str]],
) -> tuple[tuple[Any, ...], ...]:
    rows = []
    for key, path in _checkpoint_files(data_root, configs_root):
        size, digest = (
            snapshot[key] if key in snapshot else (path.stat().st_size, sha256_file(path))
        )
        last_event_id = _last_event_id(path) if size == path.stat().st_size else None
        rows.append((generation_id, key, size, digest, last_event_id))
    return tuple(rows)


def _ingested_artifact_rows(
    generation_id: UUID | str, graph: ProvenanceGraph, data_root: Path
) -> tuple[tuple[Any, ...], ...]:
    artifacts = {
        str(edge.attributes["artifact"])
        for edge in graph.edges.values()
        if edge.edge_type == "DERIVED_FROM" and "artifact" in edge.attributes
    }
    return tuple(
        (
            generation_id,
            artifact_id,
            sha256_file(store.manifest_path(data_root, DIFF_KIND, artifact_id)),
        )
        for artifact_id in sorted(artifacts)
    )


def _execute_many(connection: Any, query: str, rows: Sequence[Sequence[Any]]) -> None:
    if not rows:
        return
    with connection.cursor() as cursor:
        cursor.executemany(query, rows)


def _write_graph_rows(connection: Any, rows: GraphRows) -> None:
    _execute_many(
        connection,
        f"""INSERT INTO {SCHEMA_NAME}.entities(
            generation_id, entity_id, entity_type, key_value, dataset_version_id,
            attributes, broken_reason
        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)""",
        rows.entities,
    )
    _execute_many(
        connection,
        f"""INSERT INTO {SCHEMA_NAME}.provenance_edges(
            generation_id, edge_id, source_id, target_id, edge_type, attributes
        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)""",
        rows.provenance_edges,
    )
    _execute_many(
        connection,
        f"""INSERT INTO {SCHEMA_NAME}.sample_changes(
            generation_id, change_id, schema_version, source_id, target_id,
            from_dataset, from_samples_hash, to_dataset, to_samples_hash, sample_id,
            change_type, changed_domains, changed_fields, semantic_effects,
            before_row_hash, after_row_hash
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        rows.sample_changes,
    )
    _execute_many(
        connection,
        f"""INSERT INTO {SCHEMA_NAME}.dataset_edges(
            generation_id, source_id, target_id, artifact_id
        ) VALUES (%s, %s, %s, %s)""",
        rows.dataset_edges,
    )
    _execute_many(
        connection,
        f"""INSERT INTO {SCHEMA_NAME}.dataset_edge_changes(
            generation_id, source_id, target_id, change_id
        ) VALUES (%s, %s, %s, %s)""",
        rows.dataset_edge_changes,
    )


def _load_graph_rows(
    connection: Any, generation_id: UUID | str, *, topology_only: bool = False
) -> GraphRows:
    gaps_row = connection.execute(
        f"SELECT value FROM {SCHEMA_NAME}.metadata WHERE generation_id=%s AND key=%s",
        (generation_id, "graph_gaps"),
    ).fetchone()
    gaps_value = _scalar(gaps_row)
    gaps = tuple(json.loads(gaps_value)) if isinstance(gaps_value, str) else ()
    queries = (
        (
            "entities",
            f"""SELECT generation_id, entity_id, entity_type, key_value,
                dataset_version_id, attributes, broken_reason
                FROM {SCHEMA_NAME}.entities WHERE generation_id=%s ORDER BY entity_id""",
        ),
        (
            "provenance_edges",
            f"""SELECT generation_id, edge_id, source_id, target_id, edge_type, attributes
                FROM {SCHEMA_NAME}.provenance_edges
                WHERE generation_id=%s ORDER BY edge_id""",
        ),
        (
            "sample_changes",
            f"""SELECT generation_id, change_id, schema_version, source_id, target_id,
                from_dataset, from_samples_hash, to_dataset, to_samples_hash, sample_id,
                change_type, changed_domains, changed_fields, semantic_effects,
                before_row_hash, after_row_hash
                FROM {SCHEMA_NAME}.sample_changes
                WHERE generation_id=%s ORDER BY change_id""",
        ),
        (
            "dataset_edges",
            f"""SELECT generation_id, source_id, target_id, artifact_id
                FROM {SCHEMA_NAME}.dataset_edges
                WHERE generation_id=%s ORDER BY source_id, target_id""",
        ),
        (
            "dataset_edge_changes",
            f"""SELECT generation_id, source_id, target_id, change_id
                FROM {SCHEMA_NAME}.dataset_edge_changes
                WHERE generation_id=%s ORDER BY source_id, target_id, change_id""",
        ),
    )
    loaded = {
        name: (
            ()
            if topology_only and name in {"sample_changes", "dataset_edge_changes"}
            else tuple(connection.execute(query, (generation_id,)).fetchall())
        )
        for name, query in queries
    }
    return GraphRows(generation_id=generation_id, gaps=gaps, **loaded)


# These identifiers and columns are fixed module constants, never caller input.
_DELTA_TABLES = {
    "entities": (
        "generation_id entity_id entity_type key_value dataset_version_id attributes broken_reason",
        2,
        {5},
    ),
    "provenance_edges": ("generation_id edge_id source_id target_id edge_type attributes", 2, {5}),
    "sample_changes": (
        "generation_id change_id schema_version source_id target_id from_dataset "
        "from_samples_hash to_dataset to_samples_hash sample_id change_type changed_domains "
        "changed_fields semantic_effects before_row_hash after_row_hash",
        2,
        set(),
    ),
    "dataset_edges": ("generation_id source_id target_id artifact_id", 3, set()),
    "dataset_edge_changes": ("generation_id source_id target_id change_id", 4, set()),
    "ingested_artifacts": ("generation_id artifact_id manifest_sha256", 2, set()),
}


def _insert_immutable(connection: Any, table: str, row: Sequence[Any]) -> bool:
    columns_text, key_count, json_columns = _DELTA_TABLES[table]
    columns = columns_text.split()
    placeholders = ["%s::jsonb" if index in json_columns else "%s" for index in range(len(row))]
    inserted = connection.execute(
        f"INSERT INTO {SCHEMA_NAME}.{table} ({', '.join(columns)}) "
        f"VALUES ({', '.join(placeholders)}) ON CONFLICT DO NOTHING RETURNING generation_id",
        tuple(row),
    ).fetchone()
    if inserted is not None:
        return True
    existing = connection.execute(
        f"SELECT {', '.join(columns)} FROM {SCHEMA_NAME}.{table} WHERE "
        + " AND ".join(f"{column}=%s" for column in columns[:key_count]),
        tuple(row[:key_count]),
    ).fetchone()
    if existing is None or any(
        (_json_object(existing[i]) != _json_object(value))
        if i in json_columns
        else existing[i] != value
        for i, value in enumerate(row)
    ):
        raise IntegrityError(f"{table}_conflict: immutable PostgreSQL row")
    return False


def _insert_delta(connection: Any, rows: GraphRows) -> ProvenanceGraph:
    inserted = {}
    for table in (
        "entities",
        "provenance_edges",
        "sample_changes",
        "dataset_edges",
        "dataset_edge_changes",
    ):
        inserted[table] = tuple(
            row for row in getattr(rows, table) if _insert_immutable(connection, table, row)
        )
    return deserialize_graph(GraphRows(generation_id=rows.generation_id, gaps=(), **inserted))


def _verify_incremental_evidence(
    connection: Any, generation_id: UUID | str, data_root: Path, configs_root: Path
) -> tuple[tuple[str, int, str], ...]:
    checkpoints = tuple(
        connection.execute(
            "SELECT source_path, consumed_bytes, prefix_sha256 "
            f"FROM {SCHEMA_NAME}.ingest_checkpoints "
            "WHERE generation_id=%s ORDER BY source_path",
            (generation_id,),
        ).fetchall()
    )
    _verify_checkpoint_prefixes(checkpoints, data_root, configs_root)
    for artifact_id, digest in connection.execute(
        f"SELECT artifact_id, manifest_sha256 FROM {SCHEMA_NAME}.ingested_artifacts "
        "WHERE generation_id=%s ORDER BY artifact_id",
        (generation_id,),
    ).fetchall():
        path = store.manifest_path(data_root, DIFF_KIND, artifact_id)
        if not path.is_file() or sha256_file(path) != digest:
            raise IntegrityError(
                "canonical_drift: ingested dataset diff manifest changed or missing"
            )
        if store.verify(data_root, DIFF_KIND, artifact_id).failed:
            raise IntegrityError("canonical_drift: ingested dataset diff payload changed")
    return checkpoints


def _verify_checkpoint_prefixes(
    checkpoints: Sequence[tuple[str, int, str]],
    data_root: Path,
    configs_root: Path,
) -> None:
    """Verify retained consumed prefixes independently of newly written checkpoints."""
    for key, length, digest in checkpoints:
        path = _path_for_checkpoint(key, data_root, configs_root)
        if not path.is_file() or _prefix_hash(path, length) != digest:
            raise IntegrityError("prefix_drift: PostgreSQL provenance checkpoint changed")


def _write_selected_statuses(
    connection: Any, generation_id: UUID | str, head: str, statuses: dict[str, StatusRecord]
) -> None:
    _execute_many(
        connection,
        f"""INSERT INTO {SCHEMA_NAME}.entity_status
            (generation_id, head_id, entity_id, status, reason, predecessor_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (generation_id, head_id, entity_id) DO UPDATE SET
            status=EXCLUDED.status, reason=EXCLUDED.reason,
            predecessor_id=EXCLUDED.predecessor_id""",
        [
            (generation_id, head, ident, record.status.value, record.reason, record.predecessor_id)
            for ident, record in sorted(statuses.items())
        ],
    )


def _read_status_rows(
    connection: Any, generation_id: UUID | str, head_id: str | None = None
) -> tuple[tuple[Any, ...], ...]:
    query = f"""SELECT head_id, entity_id, status, reason, predecessor_id
        FROM {SCHEMA_NAME}.entity_status WHERE generation_id=%s"""
    params: tuple[Any, ...] = (generation_id,)
    if head_id is not None:
        query += " AND head_id=%s"
        params += (head_id,)
    query += " ORDER BY head_id, entity_id"
    return tuple(connection.execute(query, params).fetchall())


def _status_dict(rows: Sequence[Sequence[Any]]) -> dict[str, StatusRecord]:
    return {
        row[1]: StatusRecord(
            entity_id=row[1],
            status=row[2],
            reason=row[3],
            predecessor_id=row[4],
        )
        for row in rows
    }


def _active_generation(connection: Any) -> UUID | str:
    row = connection.execute(
        f"SELECT generation_id FROM {SCHEMA_NAME}.active_generation WHERE singleton=TRUE"
    ).fetchone()
    generation_id = _scalar(row)
    if generation_id is None:
        raise ValidationFailed("not_found: PostgreSQL provenance generation; run rebuild")
    return generation_id


@contextmanager
def _read_transaction(config: BackendConfig, driver: Any) -> Iterator[tuple[Any, UUID | str]]:
    with _connection(config, driver) as connection:
        validate_schema(connection)
        with connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            generation_id = _active_generation(connection)
            yield connection, generation_id


def maintenance_environment(connection: Any) -> tuple[int, str]:
    """Return live server major and an allowlisted, non-identifying runtime fingerprint.

    Read only the driver's numeric server version. Never enumerate ConnectionInfo,
    libpq parameters, environment variables, host names, or service configuration.
    This descriptor is shared with calibration callers for exact compatibility.
    """
    version = connection.info.server_version
    if not isinstance(version, int) or isinstance(version, bool) or version < 100000:
        raise ValidationFailed("incompatible_policy: unsupported PostgreSQL server version")
    major = version // 10000
    descriptor = {
        "fingerprint_version": 1,
        "postgresql_major": major,
        "system": platform.system(),
        "machine": platform.machine(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }
    return major, sha256_text(_json(descriptor))


def _planned_dirty(
    connection: Any,
    generation_id: UUID | str,
    graph: ProvenanceGraph,
    source: str,
    delta_ids: set[str],
    *,
    semantic: bool,
    join_readings: set[str],
) -> set[str]:
    """Plan the recursive closure and possible reading repair before persistent mutation.

    SQL follows indexed existing edges; the in-memory delta completes the closure.
    Reading repair can require run/fusion predecessors outside the initial closure.
    Include those components in the cost feature without retrieving history payloads.
    """
    dirty = _dirty_closure(connection, generation_id, source, delta_ids) if semantic else set()
    queue = list(dirty)
    while queue:
        ident = queue.pop()
        neighbors = {edge.target_id for edge in graph.outgoing(ident)}
        queue.extend(neighbors - dirty)
        dirty.update(neighbors)
    readings = join_readings | {
        ident
        for ident in dirty
        if graph.entities[ident].entity_type == "reading"
        and graph.entities[ident].broken_reason is None
    }
    repair = set(readings)
    runs = [f"run:{graph.entities[ident].attributes.get('run_id')}" for ident in readings]
    while runs:
        run_id = runs.pop()
        if run_id in repair or run_id not in graph.entities:
            continue
        repair.add(run_id)
        if graph.entities[run_id].entity_type == "fusion_run":
            runs.extend(
                edge.source_id
                for edge in graph.incoming(run_id)
                if graph.entities[edge.source_id].entity_type in {"run", "fusion_run"}
            )
    queue = list(repair)
    while queue:
        neighbors = {edge.target_id for edge in graph.outgoing(queue.pop())}
        queue.extend(neighbors - repair)
        repair.update(neighbors)
    dirty.update(repair)
    return dirty


def _record_decision(
    connection: Any,
    generation_id: UUID | str,
    artifact_id: str,
    *,
    inserted: bool,
    decision: StrategyDecision,
    digest: str,
    started: int,
) -> MaintenanceResult:
    result = MaintenanceResult(
        artifact_id=artifact_id,
        inserted=inserted,
        backend=BackendName.POSTGRESQL.value,
        requested_strategy=decision.requested_strategy,
        selected_strategy=decision.selected_strategy,
        strategy_reason=decision.reason,
        changed_samples=decision.changed_samples,
        dirty_entities=decision.dirty_entities,
        total_entities=decision.total_entities,
        dirty_ratio=decision.dirty_ratio,
        estimated_incremental_ms=decision.estimated_incremental_ms,
        estimated_full_ms=decision.estimated_full_ms,
        policy_version=decision.policy_version,
        elapsed_ms=(perf_counter_ns() - started) / 1_000_000,
        graph_hash=digest,
    )
    connection.execute(
        f"""INSERT INTO {SCHEMA_NAME}.maintenance_decisions (
            generation_id, decision_id, artifact_id, requested_strategy, selected_strategy,
            reason_code, changed_samples, dirty_entities, total_entities, dirty_ratio,
            total_edges, historical_changes, head_count, estimated_incremental_ms,
            estimated_full_ms, policy_version, elapsed_ms, decided_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            generation_id,
            uuid4(),
            artifact_id,
            result.requested_strategy,
            result.selected_strategy,
            result.strategy_reason,
            result.changed_samples,
            result.dirty_entities,
            result.total_entities,
            result.dirty_ratio,
            decision.total_edges,
            decision.historical_changes,
            decision.head_count,
            result.estimated_incremental_ms,
            result.estimated_full_ms,
            result.policy_version,
            result.elapsed_ms,
            utc_now(),
        ),
    )
    return result


class PostgresProvenanceBackend:
    """Normalized PostgreSQL index with generation-based atomic publication."""

    name = BackendName.POSTGRESQL
    location_label = BackendName.POSTGRESQL.value

    def __init__(self, config: BackendConfig) -> None:
        self.config = BackendConfig(
            name=self.name, pg_service=validate_pg_service(config.pg_service)
        )
        self._psycopg = _load_psycopg()

    def rebuild(self, data_root: Path, configs_root: Path) -> RebuildResult:
        data_root = Path(data_root).resolve()
        configs_root = Path(configs_root).resolve()
        before = _canonical_snapshot(data_root, configs_root)
        canonical = build_graph(data_root, configs_root)
        if _canonical_snapshot(data_root, configs_root) != before:
            raise IntegrityError("canonical_drift: inputs changed during provenance rebuild")
        generation_id = uuid4()
        digest = graph_hash(canonical)
        record_count, record_sum = _generation_fingerprint(canonical)
        snapshot_hash = _snapshot_hash(before)
        graph_rows = serialize_graph(canonical, generation_id=generation_id)
        status_rows = _status_rows(canonical, generation_id)
        checkpoint_rows = _checkpoint_rows(generation_id, data_root, configs_root, before)
        artifact_rows = _ingested_artifact_rows(generation_id, canonical, data_root)
        with _connection(self.config, self._psycopg) as connection:
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
                install_schema_in_transaction(connection)
                previous = _scalar(
                    connection.execute(
                        f"SELECT generation_id FROM {SCHEMA_NAME}.active_generation "
                        "WHERE singleton=TRUE"
                    ).fetchone()
                )
                self._publish_generation(
                    connection,
                    generation_id,
                    previous,
                    canonical,
                    before,
                    data_root,
                    configs_root,
                    digest,
                    record_count,
                    record_sum,
                    snapshot_hash,
                    graph_rows,
                    status_rows,
                    checkpoint_rows,
                    artifact_rows,
                )
        return RebuildResult(
            entities=len(canonical.entities),
            edges=len(canonical.edges),
            changes=len(canonical.changes),
            heads=len(dataset_heads(canonical)),
            graph_hash=digest,
        )

    def _publish_generation(
        self,
        connection: Any,
        generation_id: UUID | str,
        previous: UUID | str | None,
        canonical: ProvenanceGraph,
        before: dict[str, tuple[int, str]],
        data_root: Path,
        configs_root: Path,
        digest: str,
        record_count: int,
        record_sum: str,
        snapshot_hash: str,
        graph_rows: GraphRows,
        status_rows: Sequence[Sequence[Any]],
        checkpoint_rows: Sequence[Sequence[Any]],
        artifact_rows: Sequence[Sequence[Any]],
    ) -> None:
        """Publish the fixed full path inside the caller's already locked transaction."""
        connection.execute(
            f"""INSERT INTO {SCHEMA_NAME}.generations(
                generation_id, state, schema_version, built_at, graph_hash,
                graph_record_count, graph_record_sum, canonical_snapshot_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                generation_id,
                "building",
                POSTGRES_SCHEMA_VERSION,
                utc_now(),
                digest,
                record_count,
                record_sum,
                snapshot_hash,
            ),
        )
        _execute_many(
            connection,
            f"INSERT INTO {SCHEMA_NAME}.metadata VALUES (%s, %s, %s)",
            (
                (generation_id, "backend_schema_version", str(POSTGRES_SCHEMA_VERSION)),
                (generation_id, "build_string", build_string()),
                (generation_id, "graph_gaps", _json(graph_rows.gaps)),
                (generation_id, "canonical_snapshot", _json(before)),
            ),
        )
        _write_graph_rows(connection, graph_rows)
        _execute_many(
            connection,
            f"INSERT INTO {SCHEMA_NAME}.entity_status VALUES (%s, %s, %s, %s, %s, %s)",
            status_rows,
        )
        _execute_many(
            connection,
            f"INSERT INTO {SCHEMA_NAME}.ingest_checkpoints VALUES (%s, %s, %s, %s, %s)",
            checkpoint_rows,
        )
        _execute_many(
            connection,
            f"INSERT INTO {SCHEMA_NAME}.ingested_artifacts VALUES (%s, %s, %s)",
            artifact_rows,
        )
        self._assert_generation_parity(
            connection,
            generation_id,
            canonical,
            digest,
            record_count,
            record_sum,
        )
        if _canonical_snapshot(data_root, configs_root) != before:
            raise IntegrityError(
                "canonical_drift: inputs changed before provenance generation publication"
            )
        connection.execute(
            f"UPDATE {SCHEMA_NAME}.generations SET state=%s WHERE generation_id=%s",
            ("ready", generation_id),
        )
        connection.execute(
            f"""INSERT INTO {SCHEMA_NAME}.active_generation(singleton, generation_id)
                VALUES (TRUE, %s)
                ON CONFLICT (singleton) DO UPDATE
                SET generation_id=EXCLUDED.generation_id""",
            (generation_id,),
        )
        if previous is not None:
            connection.execute(
                f"DELETE FROM {SCHEMA_NAME}.generations WHERE generation_id=%s",
                (previous,),
            )

    def _assert_generation_parity(
        self,
        connection: Any,
        generation_id: UUID | str,
        canonical: ProvenanceGraph,
        digest: str,
        record_count: int,
        record_sum: str,
    ) -> None:
        indexed = deserialize_graph(_load_graph_rows(connection, generation_id))
        if indexed.normalized() != canonical.normalized():
            raise IntegrityError("mismatch: rebuilt PostgreSQL provenance graph")
        if graph_hash(indexed) != digest:
            raise IntegrityError("mismatch: rebuilt PostgreSQL provenance graph hash")
        generation = connection.execute(
            f"""SELECT state, schema_version, built_at, graph_hash, graph_record_count,
                graph_record_sum, canonical_snapshot_hash
                FROM {SCHEMA_NAME}.generations WHERE generation_id=%s""",
            (generation_id,),
        ).fetchone()
        if generation is None or generation[:2] != ("building", POSTGRES_SCHEMA_VERSION):
            raise IntegrityError("mismatch: rebuilt PostgreSQL generation state")
        if generation[3:6] != (digest, record_count, record_sum):
            raise IntegrityError("mismatch: rebuilt PostgreSQL generation fingerprint")
        actual_heads = {
            row[0]
            for row in connection.execute(
                f"SELECT DISTINCT head_id FROM {SCHEMA_NAME}.entity_status "
                "WHERE generation_id=%s ORDER BY head_id",
                (generation_id,),
            ).fetchall()
        }
        expected_heads = set(_status_heads(canonical))
        if actual_heads != expected_heads:
            raise IntegrityError("mismatch: rebuilt PostgreSQL dataset heads")
        for head in sorted(expected_heads):
            actual = _status_dict(_read_status_rows(connection, generation_id, head))
            if actual != compute_statuses(canonical, head):
                raise IntegrityError(f"mismatch: rebuilt PostgreSQL statuses for head {head}")

    def sync(self, data_root: Path, configs_root: Path) -> RebuildResult:
        """PostgreSQL v1 sync deliberately uses the same atomic full publication path."""
        return self.rebuild(data_root, configs_root)

    def ingest_diff(
        self,
        artifact_id: str,
        data_root: Path,
        configs_root: Path,
        *,
        requested_strategy: RequestedStrategy | str = RequestedStrategy.INCREMENTAL,
        policy_id: str | None = None,
    ) -> MaintenanceResult:
        """Select once from verified features, then atomically execute the fixed path."""
        try:
            requested_strategy = RequestedStrategy(requested_strategy)
        except (TypeError, ValueError):
            raise ValidationFailed("unsupported_strategy: PostgreSQL maintenance request") from None
        started = perf_counter_ns()
        data_root = Path(data_root).resolve()
        configs_root = Path(configs_root).resolve()
        with _connection(self.config, self._psycopg) as connection:
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
                validate_schema_in_transaction(connection)
                major, environment = maintenance_environment(connection)

                def load_policy():
                    return (
                        load_policy_artifact(
                            data_root,
                            policy_id,
                            backend_schema_version=POSTGRES_SCHEMA_VERSION,
                            postgresql_major=major,
                            benchmark_schema_version=BENCHMARK_SCHEMA_VERSION,
                            environment_fingerprint=environment,
                        )
                        if policy_id is not None
                        else None
                    )

                policy = load_policy()
                generation_id = _active_generation(connection)
                original_checkpoints = _verify_incremental_evidence(
                    connection, generation_id, data_root, configs_root
                )
                loaded = load_dataset_diff(data_root, artifact_id, verify_inputs=True)
                manifest_path = store.manifest_path(data_root, DIFF_KIND, artifact_id)
                manifest_hash = sha256_file(manifest_path)
                before = _canonical_snapshot(data_root, configs_root)
                generation = connection.execute(
                    f"SELECT state, graph_hash, graph_record_count, graph_record_sum "
                    f"FROM {SCHEMA_NAME}.generations WHERE generation_id=%s",
                    (generation_id,),
                ).fetchone()
                if generation is None or generation[0] != "ready":
                    raise IntegrityError("mismatch: active PostgreSQL generation is not ready")
                prior = connection.execute(
                    f"SELECT manifest_sha256 FROM {SCHEMA_NAME}.ingested_artifacts "
                    "WHERE generation_id=%s AND artifact_id=%s",
                    (generation_id, artifact_id),
                ).fetchone()
                snapshot_row = connection.execute(
                    f"SELECT value FROM {SCHEMA_NAME}.metadata WHERE generation_id=%s AND key=%s",
                    (generation_id, "canonical_snapshot"),
                ).fetchone()
                if snapshot_row is None:
                    raise IntegrityError("mismatch: canonical snapshot missing; rebuild required")
                previous_snapshot = _json_object(_scalar(snapshot_row))
                if any(before.get(key) != tuple(value) for key, value in previous_snapshot.items()):
                    raise IntegrityError(
                        "canonical_drift: indexed evidence changed; rebuild required"
                    )
                artifact_prefix = f"data/artifacts/{DIFF_KIND}/{artifact_id}/"
                if any(
                    not key.startswith(artifact_prefix)
                    for key in before.keys() - previous_snapshot.keys()
                ):
                    raise IntegrityError(
                        "canonical_drift: unindexed evidence; run sync before ingest"
                    )
                graph = deserialize_graph(
                    _load_graph_rows(connection, generation_id, topology_only=True)
                )
                historical_changes = _scalar(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {SCHEMA_NAME}.sample_changes WHERE generation_id=%s",
                        (generation_id,),
                    ).fetchone()
                )

                def decide(dirty_ids: set[str]) -> StrategyDecision:
                    features = MaintenanceFeatures(
                        changed_samples=len(loaded.changes),
                        dirty_entities=len(dirty_ids),
                        total_entities=len(graph.entities),
                        dirty_ratio=len(dirty_ids) / len(graph.entities) if graph.entities else 0.0,
                        total_edges=len(graph.edges),
                        historical_changes=historical_changes,
                        head_count=len(dataset_heads(graph)),
                    )
                    return select_strategy(requested_strategy, features, policy)

                if prior is not None:
                    if prior[0] != manifest_hash:
                        raise IntegrityError(
                            "canonical_drift: duplicate dataset diff manifest changed"
                        )
                    decision = decide(set()).model_copy(
                        update={
                            "selected_strategy": SelectedStrategy.NO_OP,
                            "reason": "duplicate_artifact_no_op",
                        }
                    )
                    result = _record_decision(
                        connection,
                        generation_id,
                        artifact_id,
                        inserted=False,
                        decision=decision,
                        digest=generation[1],
                        started=started,
                    )
                    self._recheck_diff(
                        data_root, configs_root, artifact_id, loaded, manifest_hash, before
                    )
                    _verify_checkpoint_prefixes(original_checkpoints, data_root, configs_root)
                    if load_policy() != policy:
                        raise IntegrityError("mismatch: provenance policy changed during ingest")
                    return result

                source = dataset_version_id(
                    loaded.summary.from_dataset, loaded.summary.from_samples_hash
                )
                target = dataset_version_id(
                    loaded.summary.to_dataset, loaded.summary.to_samples_hash
                )
                if (source, target) in graph.transitions:
                    raise IntegrityError(f"transition_conflict: {source} -> {target}")
                joining_histories = any(
                    old_target == target for _old, old_target in graph.transitions
                )
                before_entities, before_edges = set(graph.entities), set(graph.edges)
                add_artifact(
                    graph, data_root, store.load_manifest(data_root, DIFF_KIND, artifact_id)
                )
                add_dataset_diff_transition(graph, data_root, artifact_id)
                delta = ProvenanceGraph(
                    entities={
                        ident: graph.entities[ident]
                        for ident in graph.entities.keys() - before_entities
                    },
                    changes={change.change_id: change for change in loaded.changes},
                    transitions={(source, target): [change.change_id for change in loaded.changes]},
                )
                for ident in graph.edges.keys() - before_edges:
                    delta.add_existing_edge(graph.edges[ident])
                # serialize_graph needs the transition's DERIVED_FROM edge, present in delta.
                base = _status_dict(_read_status_rows(connection, generation_id, source))
                if set(base) != before_entities:
                    raise IntegrityError(
                        "mismatch: source status materialization; rebuild required"
                    )
                ancestors = _dataset_ancestors(connection, generation_id, source)
                affected_heads = {
                    row[0]
                    for row in connection.execute(
                        DATASET_DESCENDANTS_SQL, (target, generation_id)
                    ).fetchall()
                }
                semantic = loaded.summary.total_changes > 0 or affected_heads != {target}
                join_readings = set()
                if not semantic and joining_histories:
                    # Terminal zero-event joins can still repair existing reading
                    # semantics. Include this work in selection before any write.
                    closure = _dirty_closure(connection, generation_id, source, set())
                    closure.update(graph.descendants(source))
                    join_readings = {
                        ident
                        for ident in closure
                        if graph.entities[ident].entity_type == "reading"
                        and graph.entities[ident].broken_reason is None
                    }
                if not semantic:
                    # Even a simple status copy can carry an incompatible reading
                    # base. Use the same pure seed detection as the fixed repair path.
                    target_base = _status_dict(_read_status_rows(connection, generation_id, target))
                    combined = _merge_status_bases(base, target_base)
                    copied = {
                        ident: record
                        for ident, record in combined.items()
                        if record != target_base.get(ident)
                    }
                    join_readings = _reading_repair_seeds(
                        graph, combined, copied, force=join_readings
                    )
                planned_dirty = _planned_dirty(
                    connection,
                    generation_id,
                    graph,
                    source,
                    set(delta.entities),
                    semantic=semantic,
                    join_readings=join_readings,
                )
                decision = decide(planned_dirty)
                if decision.selected_strategy is SelectedStrategy.FULL:
                    canonical = build_graph(data_root, configs_root)
                    new_generation = uuid4()
                    digest = graph_hash(canonical)
                    record_count, record_sum = _generation_fingerprint(canonical)
                    graph_rows = serialize_graph(canonical, generation_id=new_generation)
                    status_rows = _status_rows(canonical, new_generation)
                    checkpoint_rows = _checkpoint_rows(
                        new_generation, data_root, configs_root, before
                    )
                    artifact_rows = _ingested_artifact_rows(new_generation, canonical, data_root)
                    self._recheck_diff(
                        data_root, configs_root, artifact_id, loaded, manifest_hash, before
                    )
                    _verify_checkpoint_prefixes(original_checkpoints, data_root, configs_root)
                    self._publish_generation(
                        connection,
                        new_generation,
                        generation_id,
                        canonical,
                        before,
                        data_root,
                        configs_root,
                        digest,
                        record_count,
                        record_sum,
                        _snapshot_hash(before),
                        graph_rows,
                        status_rows,
                        checkpoint_rows,
                        artifact_rows,
                    )
                    result = _record_decision(
                        connection,
                        new_generation,
                        artifact_id,
                        inserted=True,
                        decision=decision,
                        digest=digest,
                        started=started,
                    )
                    self._recheck_diff(
                        data_root, configs_root, artifact_id, loaded, manifest_hash, before
                    )
                    _verify_checkpoint_prefixes(original_checkpoints, data_root, configs_root)
                    if load_policy() != policy:
                        raise IntegrityError("mismatch: provenance policy changed during ingest")
                    return result

                inserted_delta = _insert_delta(
                    connection, serialize_graph(delta, generation_id=generation_id)
                )
                dirty = (
                    planned_dirty
                    if semantic
                    else _dirty_closure(
                        connection, generation_id, source, set(delta.entities), semantic=False
                    )
                )
                for head in sorted(affected_heads):
                    head_base = _status_dict(_read_status_rows(connection, generation_id, head))
                    combined_base = _merge_status_bases(base, head_base)
                    # A late connecting edge exposes existing downstream changes to newly
                    # connected ancestors. Only rows on target -> head paths are needed;
                    # unrelated history and source-side history remain materialized bases.
                    history = (
                        _changes_on_paths(connection, generation_id, target, head)
                        if head != target
                        else {}
                    )
                    status_graph = ProvenanceGraph(
                        entities=dict(graph.entities),
                        changes={**history, **delta.changes},
                    )
                    for edge in graph.edges.values():
                        status_graph.add_existing_edge(edge)
                    status_graph.transitions = (
                        {(old, head): list(status_graph.changes) for old in ancestors}
                        if semantic
                        else {}
                    )
                    if head == target:
                        connection.execute(
                            f"""INSERT INTO {SCHEMA_NAME}.entity_status
                                (generation_id, head_id, entity_id, status, reason, predecessor_id)
                                SELECT generation_id, %s, entity_id, status, reason, predecessor_id
                                FROM {SCHEMA_NAME}.entity_status
                                WHERE generation_id=%s AND head_id=%s
                                ON CONFLICT (generation_id, head_id, entity_id) DO NOTHING""",
                            (target, generation_id, source),
                        )
                    selected = compute_statuses_for_entities(
                        status_graph,
                        head,
                        dirty,
                        base=combined_base,
                        preserve_selected_base=True,
                    )
                    # Copy source-history severities for clean rows too (notably NO_OP),
                    # while retaining a joining target's pre-existing lineage statuses.
                    selected.update(
                        {
                            ident: record
                            for ident, record in combined_base.items()
                            if ident not in dirty and record != head_base.get(ident)
                        }
                    )
                    # Carried NO_OP/join statuses can contain incompatible reading bases;
                    # repair after collecting them, before writing any selected statuses.
                    _repair_reading_statuses(
                        connection,
                        generation_id,
                        graph,
                        head,
                        combined_base,
                        selected,
                        force=join_readings,
                    )
                    _write_selected_statuses(connection, generation_id, head, selected)
                neutral_graph = ProvenanceGraph(entities=dict(graph.entities))
                for edge in graph.edges.values():
                    neutral_graph.add_existing_edge(edge)
                delta_dirty = _dirty_closure(
                    connection, generation_id, source, set(delta.entities), semantic=False
                )
                for head in _status_heads(graph):
                    if head in affected_heads:
                        continue
                    head_base = _status_dict(_read_status_rows(connection, generation_id, head))
                    selected = compute_statuses_for_entities(
                        neutral_graph,
                        head,
                        delta_dirty,
                        base=head_base,
                        preserve_selected_base=True,
                    )
                    _write_selected_statuses(connection, generation_id, head, selected)

                delta_count, delta_sum = _fingerprint(inserted_delta)
                count = generation[2] + delta_count
                total = (int(generation[3], 16) + delta_sum) % _FINGERPRINT_MODULUS
                digest = _fingerprint_hash(count, total)
                _insert_immutable(
                    connection, "ingested_artifacts", (generation_id, artifact_id, manifest_hash)
                )
                connection.execute(
                    f"UPDATE {SCHEMA_NAME}.generations SET graph_hash=%s, graph_record_count=%s, "
                    "graph_record_sum=%s, canonical_snapshot_hash=%s WHERE generation_id=%s",
                    (digest, count, f"{total:064x}", _snapshot_hash(before), generation_id),
                )
                connection.execute(
                    f"UPDATE {SCHEMA_NAME}.metadata SET value=%s WHERE generation_id=%s AND key=%s",
                    (_json(before), generation_id, "canonical_snapshot"),
                )
                _verify_checkpoint_prefixes(original_checkpoints, data_root, configs_root)
                connection.execute(
                    f"DELETE FROM {SCHEMA_NAME}.ingest_checkpoints WHERE generation_id=%s",
                    (generation_id,),
                )
                _execute_many(
                    connection,
                    f"INSERT INTO {SCHEMA_NAME}.ingest_checkpoints VALUES (%s, %s, %s, %s, %s)",
                    _checkpoint_rows(generation_id, data_root, configs_root, before),
                )
                result = _record_decision(
                    connection,
                    generation_id,
                    artifact_id,
                    inserted=True,
                    decision=decision,
                    digest=digest,
                    started=started,
                )
                _verify_incremental_evidence(connection, generation_id, data_root, configs_root)
                self._recheck_diff(
                    data_root, configs_root, artifact_id, loaded, manifest_hash, before
                )
                _verify_checkpoint_prefixes(original_checkpoints, data_root, configs_root)
                if load_policy() != policy:
                    raise IntegrityError("mismatch: provenance policy changed during ingest")
                return result

    @staticmethod
    def _recheck_diff(
        data_root: Path,
        configs_root: Path,
        artifact_id: str,
        loaded: Any,
        manifest_hash: str,
        before: dict[str, tuple[int, str]],
    ) -> None:
        if (
            load_dataset_diff(data_root, artifact_id, verify_inputs=True) != loaded
            or sha256_file(store.manifest_path(data_root, DIFF_KIND, artifact_id)) != manifest_hash
            or _canonical_snapshot(data_root, configs_root) != before
        ):
            raise IntegrityError(
                "canonical_drift: dataset diff or its inputs changed during ingest"
            )

    @contextmanager
    def read_snapshot(self) -> Iterator[ProvenanceReader]:
        """Pin one generation and MVCC snapshot for all reads in this context."""
        with _read_transaction(self.config, self._psycopg) as (connection, generation_id):
            yield _PostgresReader(connection, generation_id)

    def load_graph(self) -> ProvenanceGraph:
        with self.read_snapshot() as reader:
            return reader.load_graph()

    def statuses(self, head_id: str) -> dict[str, StatusRecord]:
        with self.read_snapshot() as reader:
            return reader.statuses(head_id)

    def normalized(self) -> dict[str, Any]:
        with self.read_snapshot() as reader:
            return reader.normalized()

    def stats(self) -> dict[str, Any]:
        with self.read_snapshot() as reader:
            return reader.stats()

    def verify(self, data_root: Path, configs_root: Path) -> VerifyIndexResult:
        with self.read_snapshot() as reader:
            return reader.verify(data_root, configs_root)


class _PostgresReader:
    """Read-only view borrowing a connection for its surrounding snapshot context."""

    def __init__(self, connection: Any, generation_id: UUID | str) -> None:
        self.connection = connection
        self.generation_id = generation_id

    @contextmanager
    def _read(self) -> Iterator[tuple[Any, UUID | str]]:
        yield self.connection, self.generation_id

    def load_graph(self) -> ProvenanceGraph:
        with self._read() as (connection, generation_id):
            return deserialize_graph(_load_graph_rows(connection, generation_id))

    def statuses(self, head_id: str) -> dict[str, StatusRecord]:
        with self._read() as (connection, generation_id):
            return _status_dict(_read_status_rows(connection, generation_id, head_id))

    def normalized(self) -> dict[str, Any]:
        with self._read() as (connection, generation_id):
            graph = deserialize_graph(_load_graph_rows(connection, generation_id)).normalized()
            statuses = [
                {
                    "head_id": row[0],
                    "entity_id": row[1],
                    "status": row[2],
                    "reason": row[3],
                    "predecessor_id": row[4],
                }
                for row in _read_status_rows(connection, generation_id)
            ]
            return {"graph": graph, "statuses": statuses}

    def stats(self) -> dict[str, Any]:
        with self._read() as (connection, generation_id):
            counts = {
                table: _scalar(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {SCHEMA_NAME}.{table} WHERE generation_id=%s",
                        (generation_id,),
                    ).fetchone()
                )
                for table in _TABLES
            }
            metadata = {
                row[0]: row[1]
                for row in connection.execute(
                    f"SELECT key, value FROM {SCHEMA_NAME}.metadata "
                    "WHERE generation_id=%s ORDER BY key",
                    (generation_id,),
                ).fetchall()
            }
            generation = connection.execute(
                f"""SELECT state, schema_version, built_at, graph_hash, graph_record_count,
                    graph_record_sum, canonical_snapshot_hash
                    FROM {SCHEMA_NAME}.generations WHERE generation_id=%s""",
                (generation_id,),
            ).fetchone()
            size = _scalar(
                connection.execute("SELECT pg_database_size(current_database())").fetchone()
            )
            core = {
                "state": generation[0],
                "schema_version": generation[1],
                "built_at": generation[2],
                "graph_hash": generation[3],
                "graph_record_count": generation[4],
                "graph_record_sum": generation[5],
                "canonical_snapshot_hash": generation[6],
            }
            return {**counts, **metadata, **core, "bytes": size}

    def verify(self, data_root: Path, configs_root: Path) -> VerifyIndexResult:
        data_root = Path(data_root).resolve()
        configs_root = Path(configs_root).resolve()
        canonical_snapshot = _canonical_snapshot(data_root, configs_root)
        canonical = build_graph(data_root, configs_root)
        issues: list[str] = []
        with self._read() as (connection, generation_id):
            indexed = deserialize_graph(_load_graph_rows(connection, generation_id))
            if indexed.normalized() != canonical.normalized():
                issues.append("graph differs from canonical replay")
            digest = graph_hash(indexed)
            count, record_sum = _generation_fingerprint(indexed)
            generation = connection.execute(
                f"""SELECT state, schema_version, built_at, graph_hash, graph_record_count,
                    graph_record_sum, canonical_snapshot_hash
                    FROM {SCHEMA_NAME}.generations WHERE generation_id=%s""",
                (generation_id,),
            ).fetchone()
            if generation[0] != "ready":
                issues.append("active generation is not ready")
            if generation[1] != POSTGRES_SCHEMA_VERSION:
                issues.append("active generation schema version differs")
            if generation[3] != digest:
                issues.append("recorded graph hash differs")
            if generation[4] != count:
                issues.append("recorded graph record count differs")
            if generation[5] != record_sum:
                issues.append("recorded graph record sum differs")
            if generation[6] != _snapshot_hash(canonical_snapshot):
                issues.append("canonical snapshot differs")
            actual_heads = {
                row[0]
                for row in connection.execute(
                    f"SELECT DISTINCT head_id FROM {SCHEMA_NAME}.entity_status "
                    "WHERE generation_id=%s ORDER BY head_id",
                    (generation_id,),
                ).fetchall()
            }
            expected_heads = set(_status_heads(canonical))
            if actual_heads != expected_heads:
                issues.append("dataset heads differ")
            for head in sorted(expected_heads):
                wanted = compute_statuses(canonical, head)
                present = _status_dict(_read_status_rows(connection, generation_id, head))
                if present != wanted:
                    issues.append(f"status differs for head {head}")
        return VerifyIndexResult(not issues, issues, graph_hash(indexed))


__all__ = [
    "GraphRows",
    "PostgresProvenanceBackend",
    "_connect",
    "_load_psycopg",
    "deserialize_graph",
    "install_schema",
    "raise_redacted_database_error",
    "serialize_graph",
    "validate_pg_service",
    "validate_schema",
]
