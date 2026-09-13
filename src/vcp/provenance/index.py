"""Disposable SQLite index with transactional, idempotent provenance maintenance."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from vcp.artifact import store
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_text
from vcp.core.time import stamp
from vcp.provenance.diff import KIND as DIFF_KIND
from vcp.provenance.diff import load_dataset_diff
from vcp.provenance.graph import (
    ProvenanceGraph,
    add_artifact,
    add_dataset_diff_transition,
    build_graph,
    dataset_version_id,
)
from vcp.provenance.schema import ProvenanceEdge, ProvenanceEntity, SampleChange, StatusRecord
from vcp.provenance.views import compute_statuses, compute_statuses_for_entities

SCHEMA_VERSION = 2
_FINGERPRINT_MODULUS = 1 << 256


@dataclass(frozen=True)
class IngestResult:
    artifact_id: str
    inserted: bool
    dirty_entities: int
    graph_hash: str


@dataclass(frozen=True)
class RebuildResult:
    entities: int
    edges: int
    changes: int
    heads: int
    graph_hash: str


@dataclass(frozen=True)
class VerifyIndexResult:
    ok: bool
    issues: list[str]
    graph_hash: str


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _graph_records(graph: ProvenanceGraph):
    for ident in sorted(graph.entities):
        yield {"kind": "entity", "value": graph.entities[ident].model_dump(mode="json")}
    for ident in sorted(graph.edges):
        yield {"kind": "edge", "value": graph.edges[ident].model_dump(mode="json")}
    for ident in sorted(graph.changes):
        yield {"kind": "change", "value": graph.changes[ident].model_dump(mode="json")}
    for (source, target), change_ids in sorted(graph.transitions.items()):
        yield {
            "kind": "transition",
            "value": {"from": source, "to": target, "changes": sorted(change_ids)},
        }


def _fingerprint(graph: ProvenanceGraph) -> tuple[int, int]:
    count = 0
    total = 0
    for record in _graph_records(graph):
        count += 1
        total = (total + int(sha256_text(_json(record)), 16)) % _FINGERPRINT_MODULUS
    return count, total


def _fingerprint_hash(count: int, total: int) -> str:
    return sha256_text(f"v1:{count}:{total:064x}")


def graph_hash(graph: ProvenanceGraph) -> str:
    """Order-independent synchronization fingerprint; exact verify still compares records."""
    return _fingerprint_hash(*_fingerprint(graph))


def dataset_heads(graph: ProvenanceGraph) -> list[str]:
    datasets = {
        ident for ident, entity in graph.entities.items() if entity.entity_type == "dataset"
    }
    old = {source for source, _target in graph.transitions}
    return sorted(datasets - old)


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entities (
            entity_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            key_value TEXT NOT NULL,
            dataset_version_id TEXT,
            attributes_json TEXT NOT NULL,
            broken_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS entities_type ON entities(entity_type);
        CREATE INDEX IF NOT EXISTS entities_dataset ON entities(dataset_version_id);
        CREATE TABLE IF NOT EXISTS provenance_edges (
            edge_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            attributes_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS edges_source ON provenance_edges(source_id, edge_type);
        CREATE INDEX IF NOT EXISTS edges_target ON provenance_edges(target_id, edge_type);
        CREATE TABLE IF NOT EXISTS sample_changes (
            change_id TEXT PRIMARY KEY,
            sample_id TEXT NOT NULL,
            from_samples_hash TEXT NOT NULL,
            to_samples_hash TEXT NOT NULL,
            change_type TEXT NOT NULL,
            event_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS changes_sample ON sample_changes(sample_id);
        CREATE INDEX IF NOT EXISTS changes_transition
            ON sample_changes(from_samples_hash, to_samples_hash);
        CREATE INDEX IF NOT EXISTS changes_type ON sample_changes(change_type);
        CREATE TABLE IF NOT EXISTS dataset_edges (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            change_ids_json TEXT NOT NULL,
            PRIMARY KEY(source_id, target_id)
        );
        CREATE TABLE IF NOT EXISTS entity_status (
            head_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            predecessor_id TEXT,
            PRIMARY KEY(head_id, entity_id)
        );
        CREATE INDEX IF NOT EXISTS status_head_state ON entity_status(head_id, status);
        CREATE TABLE IF NOT EXISTS ingest_checkpoints (
            source_path TEXT PRIMARY KEY,
            consumed_bytes INTEGER NOT NULL,
            prefix_sha256 TEXT NOT NULL,
            last_event_id TEXT
        );
        CREATE TABLE IF NOT EXISTS ingested_artifacts (
            artifact_id TEXT PRIMARY KEY,
            manifest_sha256 TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)",
        (str(SCHEMA_VERSION),),
    )


def _connection(path: Path, *, wal: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA journal_mode={'WAL' if wal else 'DELETE'}")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _canonical_snapshot(data_root: Path, configs_root: Path) -> dict[str, tuple[int, str]]:
    """Exact file boundary for one full replay; the derived index itself is excluded."""
    patterns = [
        (configs_root / "datasets", ("*.yaml", "*.json", "*.jsonl"), "configs"),
        (
            data_root / "datasets",
            ("samples.jsonl", "*/cache/materialize/**/*"),
            "data",
        ),
        (data_root / "runs", ("*.yaml", "*.json", "*.jsonl"), "data"),
        (data_root / "measure", ("*.json", "*.jsonl"), "data"),
        (data_root / "artifacts", ("*",), "data"),
        (data_root / "submit", ("*.json", "*.jsonl"), "data"),
    ]
    result: dict[str, tuple[int, str]] = {}
    for root, globs, prefix in patterns:
        if not root.is_dir():
            continue
        files: set[Path] = set()
        for pattern in globs:
            files.update(path for path in root.rglob(pattern) if path.is_file())
        base = configs_root if prefix == "configs" else data_root
        for path in sorted(files):
            key = f"{prefix}/{path.relative_to(base).as_posix()}"
            result[key] = (path.stat().st_size, sha256_file(path))
    return result


def _insert_entity(connection: sqlite3.Connection, entity: ProvenanceEntity) -> None:
    connection.execute(
        """INSERT INTO entities(
               entity_id,entity_type,key_value,dataset_version_id,attributes_json,broken_reason
           ) VALUES(?,?,?,?,?,?)""",
        (
            entity.entity_id,
            entity.entity_type,
            entity.key,
            entity.dataset_version_id,
            _json(entity.attributes),
            entity.broken_reason,
        ),
    )


def _insert_edge(connection: sqlite3.Connection, edge: ProvenanceEdge) -> None:
    connection.execute(
        "INSERT INTO provenance_edges VALUES(?,?,?,?,?)",
        (
            edge.edge_id,
            edge.source_id,
            edge.target_id,
            edge.edge_type,
            _json(edge.attributes),
        ),
    )


def _insert_change(connection: sqlite3.Connection, change: SampleChange) -> None:
    connection.execute(
        "INSERT INTO sample_changes VALUES(?,?,?,?,?,?)",
        (
            change.change_id,
            change.sample_id,
            change.from_samples_hash,
            change.to_samples_hash,
            change.change_type.value,
            _json(change.model_dump(mode="json")),
        ),
    )


def _write_graph(connection: sqlite3.Connection, graph: ProvenanceGraph) -> None:
    for ident in sorted(graph.entities):
        _insert_entity(connection, graph.entities[ident])
    for ident in sorted(graph.edges):
        _insert_edge(connection, graph.edges[ident])
    for ident in sorted(graph.changes):
        _insert_change(connection, graph.changes[ident])
    for (source, target), change_ids in sorted(graph.transitions.items()):
        connection.execute(
            "INSERT INTO dataset_edges VALUES(?,?,?)",
            (source, target, _json(sorted(change_ids))),
        )


def _load_graph(
    connection: sqlite3.Connection,
    *,
    include_changes: bool = True,
    include_change_ids: bool = True,
    include_transitions: bool = True,
) -> ProvenanceGraph:
    graph = ProvenanceGraph()
    for row in connection.execute("SELECT * FROM entities ORDER BY entity_id"):
        graph.add_entity(
            ProvenanceEntity(
                entity_id=row["entity_id"],
                entity_type=row["entity_type"],
                key=row["key_value"],
                dataset_version_id=row["dataset_version_id"],
                attributes=json.loads(row["attributes_json"]),
                broken_reason=row["broken_reason"],
            )
        )
    for row in connection.execute("SELECT * FROM provenance_edges ORDER BY edge_id"):
        edge = ProvenanceEdge(
            edge_id=row["edge_id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            edge_type=row["edge_type"],
            attributes=json.loads(row["attributes_json"]),
        )
        graph.add_existing_edge(edge)
    if include_changes:
        for row in connection.execute("SELECT event_json FROM sample_changes ORDER BY change_id"):
            change = SampleChange.model_validate_json(row["event_json"])
            graph.changes[change.change_id] = change
    if include_transitions:
        columns = "*" if include_change_ids else "source_id,target_id"
        for row in connection.execute(
            f"SELECT {columns} FROM dataset_edges ORDER BY source_id,target_id"
        ):
            graph.transitions[(row["source_id"], row["target_id"])] = (
                json.loads(row["change_ids_json"]) if include_change_ids else []
            )
    return graph


def _store_fingerprint(connection: sqlite3.Connection, graph: ProvenanceGraph) -> str:
    count, total = _fingerprint(graph)
    digest = _fingerprint_hash(count, total)
    connection.execute(
        "INSERT OR REPLACE INTO metadata VALUES('graph_record_count',?)", (str(count),)
    )
    connection.execute(
        "INSERT OR REPLACE INTO metadata VALUES('graph_record_sum',?)", (f"{total:064x}",)
    )
    connection.execute("INSERT OR REPLACE INTO metadata VALUES('graph_hash',?)", (digest,))
    return digest


def _advance_fingerprint(connection: sqlite3.Connection, delta: ProvenanceGraph) -> str:
    rows = {
        row["key"]: row["value"]
        for row in connection.execute(
            "SELECT key,value FROM metadata WHERE key IN ('graph_record_count','graph_record_sum')"
        )
    }
    if set(rows) != {"graph_record_count", "graph_record_sum"}:
        raise IntegrityError("mismatch: provenance fingerprint metadata; rebuild required")
    delta_count, delta_total = _fingerprint(delta)
    count = int(rows["graph_record_count"]) + delta_count
    total = (int(rows["graph_record_sum"], 16) + delta_total) % _FINGERPRINT_MODULUS
    digest = _fingerprint_hash(count, total)
    connection.execute(
        "INSERT OR REPLACE INTO metadata VALUES('graph_record_count',?)", (str(count),)
    )
    connection.execute(
        "INSERT OR REPLACE INTO metadata VALUES('graph_record_sum',?)", (f"{total:064x}",)
    )
    connection.execute("INSERT OR REPLACE INTO metadata VALUES('graph_hash',?)", (digest,))
    return digest


def _write_statuses(
    connection: sqlite3.Connection,
    graph: ProvenanceGraph,
    *,
    dirty: set[str] | None = None,
) -> None:
    heads = sorted(
        ident for ident, entity in graph.entities.items() if entity.entity_type == "dataset"
    )
    if dirty is None:
        connection.execute("DELETE FROM entity_status")
    else:
        placeholders = ",".join("?" for _ in heads)
        if placeholders:
            connection.execute(
                f"DELETE FROM entity_status WHERE head_id NOT IN ({placeholders})", heads
            )
    for head in heads:
        existing = connection.execute(
            "SELECT 1 FROM entity_status WHERE head_id=? LIMIT 1", (head,)
        ).fetchone()
        if dirty is None or existing is None:
            statuses = compute_statuses(graph, head)
        else:
            base = {
                row["entity_id"]: StatusRecord(
                    entity_id=row["entity_id"],
                    status=row["status"],
                    reason=row["reason"],
                    predecessor_id=row["predecessor_id"],
                )
                for row in connection.execute(
                    "SELECT * FROM entity_status WHERE head_id=?", (head,)
                )
            }
            statuses = compute_statuses_for_entities(graph, head, dirty, base=base)
        for ident in sorted(statuses):
            record = statuses[ident]
            connection.execute(
                """INSERT OR REPLACE INTO entity_status(
                       head_id,entity_id,status,reason,predecessor_id
                   ) VALUES(?,?,?,?,?)""",
                (
                    head,
                    ident,
                    record.status.value,
                    record.reason,
                    record.predecessor_id,
                ),
            )


def _checkpoint_files(data_root: Path, configs_root: Path) -> list[tuple[str, Path]]:
    candidates = [
        *(configs_root.rglob("*.jsonl") if configs_root.is_dir() else []),
        *((data_root / "measure").rglob("*.jsonl") if (data_root / "measure").is_dir() else []),
        *((data_root / "runs").glob("*/history.jsonl") if (data_root / "runs").is_dir() else []),
        *((data_root / "runs").glob("*/train.log.jsonl") if (data_root / "runs").is_dir() else []),
        *(
            (data_root / "artifacts").glob("*/supersession.jsonl")
            if (data_root / "artifacts").is_dir()
            else []
        ),
        *((data_root / "logs").glob("*.jsonl") if (data_root / "logs").is_dir() else []),
    ]
    unique: dict[str, Path] = {}
    for path in candidates:
        resolved = path.resolve()
        try:
            key = f"data/{resolved.relative_to(data_root).as_posix()}"
        except ValueError:
            key = f"configs/{resolved.relative_to(configs_root).as_posix()}"
        unique[key] = resolved
    return sorted(unique.items())


def _prefix_hash(path: Path, length: int) -> str:
    digest = hashlib.sha256()
    remaining = length
    with path.open("rb") as source:
        while remaining:
            chunk = source.read(min(1 << 20, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    if remaining:
        raise IntegrityError(f"prefix_drift: {path} became shorter than {length} bytes")
    return digest.hexdigest()


def _last_event_id(path: Path) -> str | None:
    lines = [line for line in path.read_bytes().splitlines() if line.strip()]
    return sha256_text(lines[-1].decode("utf-8", errors="replace")) if lines else None


def _write_checkpoints(
    connection: sqlite3.Connection,
    data_root: Path,
    configs_root: Path,
    *,
    snapshot: dict[str, tuple[int, str]] | None = None,
) -> None:
    connection.execute("DELETE FROM ingest_checkpoints")
    for key, path in _checkpoint_files(data_root, configs_root):
        size, digest = (
            snapshot[key]
            if snapshot is not None and key in snapshot
            else (path.stat().st_size, sha256_file(path))
        )
        connection.execute(
            "INSERT INTO ingest_checkpoints VALUES(?,?,?,?)",
            (key, size, digest, _last_event_id(path) if size == path.stat().st_size else None),
        )


def _path_for_checkpoint(key: str, data_root: Path, configs_root: Path) -> Path:
    prefix, _, relative = key.partition("/")
    return (data_root if prefix == "data" else configs_root) / relative


def _verify_checkpoints(
    connection: sqlite3.Connection, data_root: Path, configs_root: Path
) -> None:
    for row in connection.execute("SELECT * FROM ingest_checkpoints ORDER BY source_path"):
        path = _path_for_checkpoint(row["source_path"], data_root, configs_root)
        if not path.is_file():
            raise IntegrityError(f"prefix_drift: checkpoint source is missing: {path}")
        actual = _prefix_hash(path, row["consumed_bytes"])
        if actual != row["prefix_sha256"]:
            raise IntegrityError(
                f"prefix_drift: {path} changed inside the consumed prefix",
                location=str(path),
            )


def _verify_ingested_artifacts(connection: sqlite3.Connection, data_root: Path) -> None:
    for row in connection.execute(
        "SELECT artifact_id,manifest_sha256 FROM ingested_artifacts ORDER BY artifact_id"
    ):
        path = store.manifest_path(data_root, DIFF_KIND, row["artifact_id"])
        if not path.is_file():
            raise IntegrityError(
                f"canonical_drift: ingested dataset diff is missing: {row['artifact_id']}"
            )
        if sha256_file(path) != row["manifest_sha256"]:
            raise IntegrityError(
                f"canonical_drift: dataset diff manifest changed: {row['artifact_id']}"
            )
        verified = store.verify(data_root, DIFF_KIND, row["artifact_id"])
        if verified.failed:
            raise IntegrityError(
                f"canonical_drift: dataset diff payload changed: {row['artifact_id']}"
            )


def _record_diff_artifacts(
    connection: sqlite3.Connection, graph: ProvenanceGraph, data_root: Path
) -> None:
    for edge in graph.edges.values():
        if edge.edge_type != "DERIVED_FROM" or "artifact" not in edge.attributes:
            continue
        artifact_id = str(edge.attributes["artifact"])
        digest = sha256_file(store.manifest_path(data_root, DIFF_KIND, artifact_id))
        connection.execute(
            "INSERT OR REPLACE INTO ingested_artifacts VALUES(?,?)", (artifact_id, digest)
        )


class ProvenanceIndex:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()

    def _open(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise ValidationFailed(
                f"not_found: provenance index {self.path}; run `vcp provenance rebuild`"
            )
        connection = _connection(self.path)
        row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if row is None or int(row["value"]) != SCHEMA_VERSION:
            connection.close()
            raise IntegrityError("mismatch: provenance index schema version")
        return connection

    def rebuild(self, data_root: Path, configs_root: Path) -> RebuildResult:
        data_root = Path(data_root).resolve()
        configs_root = Path(configs_root).resolve()
        before = _canonical_snapshot(data_root, configs_root)
        graph = build_graph(data_root, configs_root)
        if _canonical_snapshot(data_root, configs_root) != before:
            raise IntegrityError("canonical_drift: inputs changed during provenance rebuild")
        digest = graph_hash(graph)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.urandom(4).hex()}.tmp")
        connection: sqlite3.Connection | None = None
        try:
            connection = _connection(temporary, wal=False)
            with connection:
                _schema(connection)
                _write_graph(connection, graph)
                _write_statuses(connection, graph)
                _write_checkpoints(connection, data_root, configs_root, snapshot=before)
                _record_diff_artifacts(connection, graph, data_root)
                stored_digest = _store_fingerprint(connection, graph)
                if stored_digest != digest:
                    raise IntegrityError("mismatch: provenance fingerprint construction")
                connection.execute(
                    "INSERT OR REPLACE INTO metadata VALUES('built_at',?)", (stamp(),)
                )
            connection.close()
            connection = None
            check = _connection(temporary, wal=False)
            loaded = _load_graph(check)
            for head in dataset_heads(graph):
                expected = compute_statuses(graph, head)
                rows = check.execute(
                    "SELECT * FROM entity_status WHERE head_id=? ORDER BY entity_id", (head,)
                )
                actual = {
                    row["entity_id"]: StatusRecord(
                        entity_id=row["entity_id"],
                        status=row["status"],
                        reason=row["reason"],
                        predecessor_id=row["predecessor_id"],
                    )
                    for row in rows
                }
                if actual != expected:
                    raise IntegrityError(f"mismatch: rebuilt provenance statuses for head {head}")
            check.close()
            if graph_hash(loaded) != digest:
                raise IntegrityError("mismatch: rebuilt provenance index graph hash")
            if _canonical_snapshot(data_root, configs_root) != before:
                raise IntegrityError(
                    "canonical_drift: inputs changed before provenance index publication"
                )
            configured = _connection(temporary)
            configured.close()
            os.replace(temporary, self.path)
        finally:
            if connection is not None:
                connection.close()
            temporary.unlink(missing_ok=True)
            temporary.with_name(temporary.name + "-wal").unlink(missing_ok=True)
            temporary.with_name(temporary.name + "-shm").unlink(missing_ok=True)
        return RebuildResult(
            entities=len(graph.entities),
            edges=len(graph.edges),
            changes=len(graph.changes),
            heads=len(dataset_heads(graph)),
            graph_hash=digest,
        )

    def load_graph(self) -> ProvenanceGraph:
        connection = self._open()
        try:
            return _load_graph(connection)
        finally:
            connection.close()

    def _merge_graph(
        self,
        connection: sqlite3.Connection,
        canonical: ProvenanceGraph,
        *,
        exact: bool = False,
    ) -> set[str]:
        current = _load_graph(connection)
        dirty: set[str] = set()
        if exact:
            for label, present, wanted in (
                ("entity", set(current.entities), set(canonical.entities)),
                ("edge", set(current.edges), set(canonical.edges)),
                ("change", set(current.changes), set(canonical.changes)),
                ("transition", set(current.transitions), set(canonical.transitions)),
            ):
                removed = sorted(present - wanted)
                if removed:
                    raise IntegrityError(
                        f"canonical_drift: indexed {label} disappeared: {removed[0]}"
                    )
        for ident, entity in sorted(canonical.entities.items()):
            previous = current.entities.get(ident)
            if previous is None:
                _insert_entity(connection, entity)
                dirty.add(ident)
            elif previous != entity:
                raise IntegrityError(f"canonical_drift: entity {ident} changed; rebuild required")
        for ident, edge in sorted(canonical.edges.items()):
            previous = current.edges.get(ident)
            if previous is None:
                _insert_edge(connection, edge)
                dirty.update({edge.source_id, edge.target_id})
            elif previous != edge:
                raise IntegrityError(f"canonical_drift: edge {ident} changed; rebuild required")
        for ident, change in sorted(canonical.changes.items()):
            previous = current.changes.get(ident)
            if previous is None:
                _insert_change(connection, change)
            elif previous != change:
                raise IntegrityError(f"canonical_drift: change {ident} changed; rebuild required")
        for transition, change_ids in sorted(canonical.transitions.items()):
            previous = current.transitions.get(transition)
            if previous is None:
                connection.execute(
                    "INSERT INTO dataset_edges VALUES(?,?,?)",
                    (transition[0], transition[1], _json(sorted(change_ids))),
                )
                dirty.update(transition)
            elif sorted(previous) != sorted(change_ids):
                raise IntegrityError(
                    f"canonical_drift: transition {transition} changed; rebuild required"
                )
        return dirty

    def sync(self, data_root: Path, configs_root: Path) -> RebuildResult:
        """Append newly arrived canonical records while rejecting deletion or mutation."""
        data_root = Path(data_root).resolve()
        configs_root = Path(configs_root).resolve()
        before = _canonical_snapshot(data_root, configs_root)
        canonical = build_graph(data_root, configs_root)
        if _canonical_snapshot(data_root, configs_root) != before:
            raise IntegrityError("canonical_drift: inputs changed during provenance sync")
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _verify_checkpoints(connection, data_root, configs_root)
            _verify_ingested_artifacts(connection, data_root)
            self._merge_graph(connection, canonical, exact=True)
            _write_statuses(connection, canonical)
            if _canonical_snapshot(data_root, configs_root) != before:
                raise IntegrityError("canonical_drift: inputs changed during provenance sync")
            _write_checkpoints(connection, data_root, configs_root, snapshot=before)
            _record_diff_artifacts(connection, canonical, data_root)
            digest = _store_fingerprint(connection, canonical)
            connection.commit()
            return RebuildResult(
                entities=len(canonical.entities),
                edges=len(canonical.edges),
                changes=len(canonical.changes),
                heads=len(dataset_heads(canonical)),
                graph_hash=digest,
            )
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ingest_diff(
        self,
        artifact_id: str,
        data_root: Path,
        configs_root: Path,
        *,
        _fail_after: Literal["changes"] | None = None,
    ) -> IngestResult:
        data_root = Path(data_root).resolve()
        configs_root = Path(configs_root).resolve()
        manifest_path = store.manifest_path(data_root, DIFF_KIND, artifact_id)
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _verify_checkpoints(connection, data_root, configs_root)
            _verify_ingested_artifacts(connection, data_root)
            # Re-hash this artifact and every pinned endpoint inside the same write
            # transaction before any derived row is inserted.
            loaded = load_dataset_diff(data_root, artifact_id, verify_inputs=True)
            manifest_hash = sha256_file(manifest_path)
            old = dataset_version_id(loaded.summary.from_dataset, loaded.summary.from_samples_hash)
            new = dataset_version_id(loaded.summary.to_dataset, loaded.summary.to_samples_hash)
            previous = connection.execute(
                "SELECT manifest_sha256 FROM ingested_artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if previous is not None:
                if previous["manifest_sha256"] != manifest_hash:
                    raise IntegrityError(
                        f"canonical_drift: dataset diff {artifact_id!r} manifest changed"
                    )
                connection.commit()
                digest = connection.execute(
                    "SELECT value FROM metadata WHERE key='graph_hash'"
                ).fetchone()["value"]
                return IngestResult(artifact_id, False, 0, digest)

            # The operational graph is small relative to sample-change history. Load only
            # entities, edges and transition topology; never deserialize old change events.
            canonical = _load_graph(
                connection,
                include_changes=False,
                include_change_ids=False,
            )
            before_entities = set(canonical.entities)
            before_edges = set(canonical.edges)
            before_transitions = set(canonical.transitions)
            manifest = store.load_manifest(data_root, DIFF_KIND, artifact_id)
            add_artifact(canonical, data_root, manifest)
            add_dataset_diff_transition(canonical, data_root, artifact_id)

            delta = ProvenanceGraph()
            for ident in sorted(set(canonical.entities) - before_entities):
                entity = canonical.entities[ident]
                _insert_entity(connection, entity)
                delta.add_entity(entity)
            for ident in sorted(set(canonical.edges) - before_edges):
                edge = canonical.edges[ident]
                _insert_edge(connection, edge)
                delta.add_existing_edge(edge)
            for change in loaded.changes:
                row = connection.execute(
                    "SELECT event_json FROM sample_changes WHERE change_id=?",
                    (change.change_id,),
                ).fetchone()
                if row is None:
                    _insert_change(connection, change)
                    delta.changes[change.change_id] = change
                elif SampleChange.model_validate_json(row["event_json"]) != change:
                    raise IntegrityError(f"change_conflict: {change.change_id}")
            transition = (old, new)
            if transition in before_transitions:
                raise IntegrityError(f"transition_conflict: {old} -> {new}")
            change_ids = [change.change_id for change in loaded.changes]
            connection.execute(
                "INSERT INTO dataset_edges VALUES(?,?,?)",
                (old, new, _json(sorted(change_ids))),
            )
            delta.transitions[transition] = change_ids
            if _fail_after == "changes":
                raise RuntimeError("injected interruption after changes")

            # Reuse the fully materialized source-head status and apply only this transition's
            # events to its ancestor closure. This remains exact because status severity is
            # monotonic across an evolution path.
            dataset_sources = {old}
            queue = [old]
            while queue:
                target = queue.pop()
                for source, transition_target in canonical.transitions:
                    if transition_target == target and source not in dataset_sources:
                        dataset_sources.add(source)
                        queue.append(source)
            status_graph = ProvenanceGraph(entities=dict(canonical.entities))
            for edge in canonical.edges.values():
                status_graph.add_existing_edge(edge)
            status_graph.changes = {change.change_id: change for change in loaded.changes}
            status_graph.transitions = {
                (source, new): list(change_ids) for source in dataset_sources
            }
            dirty: set[str] = set()
            for source in dataset_sources:
                dirty.update(status_graph.descendants(source))
            base = {
                row["entity_id"]: StatusRecord(
                    entity_id=row["entity_id"],
                    status=row["status"],
                    reason=row["reason"],
                    predecessor_id=row["predecessor_id"],
                )
                for row in connection.execute("SELECT * FROM entity_status WHERE head_id=?", (old,))
            }
            if not base:
                raise IntegrityError(
                    f"missing source status materialization for {old}; rebuild required"
                )
            connection.execute(
                """INSERT OR REPLACE INTO entity_status(
                       head_id,entity_id,status,reason,predecessor_id
                   ) SELECT ?,entity_id,status,reason,predecessor_id
                     FROM entity_status WHERE head_id=?""",
                (new, old),
            )
            statuses = compute_statuses_for_entities(
                status_graph,
                new,
                dirty,
                base=base,
                preserve_selected_base=True,
            )
            for ident, record in statuses.items():
                connection.execute(
                    "INSERT OR REPLACE INTO entity_status VALUES(?,?,?,?,?)",
                    (
                        new,
                        ident,
                        record.status.value,
                        record.reason,
                        record.predecessor_id,
                    ),
                )
            # Dataset versions may have been synced before their connecting diff arrived.
            # Materialize the new artifact/sample entities for those independent heads too;
            # this is O(delta * version-heads), never O(historical change events).
            delta_entities = set(delta.entities)
            delta_dirty = set(delta_entities)
            for ident in delta_entities:
                delta_dirty.update(status_graph.descendants(ident))
            neutral_graph = ProvenanceGraph(entities=dict(canonical.entities))
            for edge in canonical.edges.values():
                neutral_graph.add_existing_edge(edge)
            other_heads = [
                row["head_id"]
                for row in connection.execute(
                    "SELECT DISTINCT head_id FROM entity_status WHERE head_id<>? ORDER BY head_id",
                    (new,),
                )
            ]
            for head in other_heads:
                head_base = {
                    row["entity_id"]: StatusRecord(
                        entity_id=row["entity_id"],
                        status=row["status"],
                        reason=row["reason"],
                        predecessor_id=row["predecessor_id"],
                    )
                    for row in connection.execute(
                        "SELECT * FROM entity_status WHERE head_id=?", (head,)
                    )
                }
                head_statuses = compute_statuses_for_entities(
                    neutral_graph,
                    head,
                    delta_dirty,
                    base=head_base,
                    preserve_selected_base=True,
                )
                for ident, record in head_statuses.items():
                    connection.execute(
                        "INSERT OR REPLACE INTO entity_status VALUES(?,?,?,?,?)",
                        (
                            head,
                            ident,
                            record.status.value,
                            record.reason,
                            record.predecessor_id,
                        ),
                    )
            committed_inputs = load_dataset_diff(data_root, artifact_id, verify_inputs=True)
            if committed_inputs != loaded or sha256_file(manifest_path) != manifest_hash:
                raise IntegrityError(
                    f"canonical_drift: dataset diff {artifact_id!r} or its inputs changed "
                    "during ingest"
                )
            connection.execute(
                "INSERT INTO ingested_artifacts VALUES(?,?)", (artifact_id, manifest_hash)
            )
            digest = _advance_fingerprint(connection, delta)
            connection.commit()
            return IngestResult(artifact_id, True, len(dirty), digest)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def statuses(self, head_id: str) -> dict[str, StatusRecord]:
        connection = self._open()
        try:
            rows = connection.execute(
                "SELECT * FROM entity_status WHERE head_id=? ORDER BY entity_id", (head_id,)
            )
            return {
                row["entity_id"]: StatusRecord(
                    entity_id=row["entity_id"],
                    status=row["status"],
                    reason=row["reason"],
                    predecessor_id=row["predecessor_id"],
                )
                for row in rows
            }
        finally:
            connection.close()

    def normalized(self) -> dict[str, Any]:
        connection = self._open()
        try:
            graph = _load_graph(connection).normalized()
            statuses = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM entity_status ORDER BY head_id,entity_id"
                )
            ]
            return {"graph": graph, "statuses": statuses}
        finally:
            connection.close()

    def stats(self) -> dict[str, Any]:
        connection = self._open()
        try:
            counts = {
                table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                for table in (
                    "entities",
                    "provenance_edges",
                    "sample_changes",
                    "dataset_edges",
                    "entity_status",
                    "ingest_checkpoints",
                    "ingested_artifacts",
                )
            }
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key,value FROM metadata ORDER BY key")
            }
            return {**counts, **metadata, "bytes": self.path.stat().st_size}
        finally:
            connection.close()

    def verify(self, data_root: Path, configs_root: Path) -> VerifyIndexResult:
        data_root = Path(data_root).resolve()
        configs_root = Path(configs_root).resolve()
        connection = self._open()
        try:
            _verify_checkpoints(connection, data_root, configs_root)
            indexed = _load_graph(connection)
            canonical = build_graph(data_root, configs_root)
            issues: list[str] = []
            if indexed.normalized() != canonical.normalized():
                issues.append("graph differs from canonical replay")
            digest = graph_hash(indexed)
            record_count, record_sum = _fingerprint(indexed)
            recorded = connection.execute(
                "SELECT value FROM metadata WHERE key='graph_hash'"
            ).fetchone()
            if recorded is None or recorded["value"] != digest:
                issues.append("recorded graph hash differs")
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key,value FROM metadata WHERE key IN "
                    "('graph_record_count','graph_record_sum')"
                )
            }
            if metadata.get("graph_record_count") != str(record_count):
                issues.append("recorded graph record count differs")
            if metadata.get("graph_record_sum") != f"{record_sum:064x}":
                issues.append("recorded graph record sum differs")
            for head in dataset_heads(canonical):
                wanted = compute_statuses(canonical, head)
                present = self.statuses(head)
                if present != wanted:
                    mismatch = next(
                        ident
                        for ident in sorted(set(wanted) | set(present))
                        if wanted.get(ident) != present.get(ident)
                    )
                    indexed_record = present.get(mismatch)
                    canonical_record = wanted.get(mismatch)
                    issues.append(
                        f"status differs for head {head} at {mismatch}: "
                        f"indexed={indexed_record.status.value if indexed_record else None}/"
                        f"{indexed_record.reason if indexed_record else None}; "
                        f"canonical={canonical_record.status.value if canonical_record else None}/"
                        f"{canonical_record.reason if canonical_record else None}"
                    )
            return VerifyIndexResult(not issues, issues, digest)
        finally:
            connection.close()
