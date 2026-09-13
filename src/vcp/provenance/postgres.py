"""Optional PostgreSQL provenance backend with atomic generation publication."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from vcp.artifact import store
from vcp.core.build import build_string
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_text
from vcp.core.time import utc_now
from vcp.provenance.backend import BackendConfig, BackendName
from vcp.provenance.diff import KIND as DIFF_KIND
from vcp.provenance.graph import ProvenanceGraph, build_graph, dataset_version_id
from vcp.provenance.index import (
    RebuildResult,
    VerifyIndexResult,
    _canonical_snapshot,
    _checkpoint_files,
    _fingerprint,
    _last_event_id,
    dataset_heads,
    graph_hash,
)
from vcp.provenance.postgres_schema import (
    POSTGRES_SCHEMA_VERSION,
    SCHEMA_NAME,
    install_schema,
    validate_schema,
)
from vcp.provenance.schema import ProvenanceEdge, ProvenanceEntity, SampleChange, StatusRecord
from vcp.provenance.views import compute_statuses

_SERVICE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_SQLSTATE = re.compile(r"[0-9A-Z]{5}")
_MISSING_DEPENDENCY = "missing_dependency: install with `uv sync --extra postgres`"
_ADVISORY_LOCK_KEY = 0x56435050524F5631
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


def _connect(config: BackendConfig) -> Any:
    """Open one autocommit connection through libpq's standard resolution."""
    service = validate_pg_service(config.pg_service)
    psycopg = _load_psycopg()
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
    for head in dataset_heads(graph):
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


def _load_graph_rows(connection: Any, generation_id: UUID | str) -> GraphRows:
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
        name: tuple(connection.execute(query, (generation_id,)).fetchall())
        for name, query in queries
    }
    return GraphRows(generation_id=generation_id, gaps=gaps, **loaded)


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
    connection = _connect(config)
    try:
        validate_schema(connection)
        with connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            generation_id = _active_generation(connection)
            yield connection, generation_id
    except Exception as error:
        if _is_driver_error(driver, error):
            raise_redacted_database_error(error)
        raise
    finally:
        connection.close()


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
        connection = _connect(self.config)
        try:
            install_schema(connection)
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
                previous = _scalar(
                    connection.execute(
                        f"SELECT generation_id FROM {SCHEMA_NAME}.active_generation "
                        "WHERE singleton=TRUE"
                    ).fetchone()
                )
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
        except Exception as error:
            if _is_driver_error(self._psycopg, error):
                raise_redacted_database_error(error)
            raise
        finally:
            connection.close()
        return RebuildResult(
            entities=len(canonical.entities),
            edges=len(canonical.edges),
            changes=len(canonical.changes),
            heads=len(dataset_heads(canonical)),
            graph_hash=digest,
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
        expected_heads = set(dataset_heads(canonical))
        if actual_heads != expected_heads:
            raise IntegrityError("mismatch: rebuilt PostgreSQL dataset heads")
        for head in sorted(expected_heads):
            actual = _status_dict(_read_status_rows(connection, generation_id, head))
            if actual != compute_statuses(canonical, head):
                raise IntegrityError(f"mismatch: rebuilt PostgreSQL statuses for head {head}")

    def sync(self, data_root: Path, configs_root: Path) -> RebuildResult:
        """PostgreSQL v1 sync deliberately uses the same atomic full publication path."""
        return self.rebuild(data_root, configs_root)

    def load_graph(self) -> ProvenanceGraph:
        with _read_transaction(self.config, self._psycopg) as (connection, generation_id):
            return deserialize_graph(_load_graph_rows(connection, generation_id))

    def statuses(self, head_id: str) -> dict[str, StatusRecord]:
        with _read_transaction(self.config, self._psycopg) as (connection, generation_id):
            return _status_dict(_read_status_rows(connection, generation_id, head_id))

    def normalized(self) -> dict[str, Any]:
        with _read_transaction(self.config, self._psycopg) as (connection, generation_id):
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
        with _read_transaction(self.config, self._psycopg) as (connection, generation_id):
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
        with _read_transaction(self.config, self._psycopg) as (connection, generation_id):
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
            expected_heads = set(dataset_heads(canonical))
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
