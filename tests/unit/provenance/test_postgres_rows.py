from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from vcp.cli import app
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.provenance.backend import BackendConfig, BackendName
from vcp.provenance.graph import ProvenanceGraph, dataset_version_id
from vcp.provenance.index import _canonical_snapshot
from vcp.provenance.postgres import (
    PostgresProvenanceBackend,
    _checkpoint_rows,
    deserialize_graph,
    serialize_graph,
)
from vcp.provenance.schema import (
    ChangeDomain,
    ChangeType,
    ProvenanceEntity,
    SampleChange,
    SemanticEffect,
    make_change_id,
)

GENERATION = "00000000-0000-0000-0000-000000000004"
OLD_HASH = "1" * 64
NEW_HASH = "2" * 64
ROW_HASH = "3" * 64


def _change(sample_id: str = "sample-1") -> SampleChange:
    change_id = make_change_id(
        OLD_HASH,
        NEW_HASH,
        sample_id,
        ChangeType.ADDED,
        None,
        ROW_HASH,
    )
    return SampleChange(
        change_id=change_id,
        from_dataset="old",
        from_samples_hash=OLD_HASH,
        to_dataset="new",
        to_samples_hash=NEW_HASH,
        sample_id=sample_id,
        change_type=ChangeType.ADDED,
        changed_domains=[ChangeDomain.META],
        changed_fields=["meta.site"],
        semantic_effects=[SemanticEffect.UNKNOWN],
        after_row_hash=ROW_HASH,
    )


@pytest.fixture
def tiny_graph() -> ProvenanceGraph:
    old = dataset_version_id("old", OLD_HASH)
    new = dataset_version_id("new", NEW_HASH)
    graph = ProvenanceGraph(gaps=["broken-but-preserved"])
    graph.add_entity(
        ProvenanceEntity(
            entity_id=old,
            entity_type="dataset",
            key=f"old@{OLD_HASH}",
            dataset_version_id=old,
            attributes={"nested": {"b": 2, "a": 1}},
        )
    )
    graph.add_entity(
        ProvenanceEntity(
            entity_id=new,
            entity_type="dataset",
            key=f"new@{NEW_HASH}",
            dataset_version_id=new,
        )
    )
    graph.add_edge(
        old,
        new,
        "DERIVED_FROM",
        {"artifact": "tiny-diff", "total_changes": 1},
    )
    change = _change()
    graph.changes[change.change_id] = change
    graph.transitions[(old, new)] = [change.change_id]
    return graph


def test_graph_rows_round_trip_exactly(tiny_graph):
    rows = serialize_graph(tiny_graph, generation_id=GENERATION)

    assert deserialize_graph(rows).normalized() == tiny_graph.normalized()


def test_graph_rows_normalize_dataset_edge_change_ids(tiny_graph):
    second = _change("sample-2")
    tiny_graph.changes[second.change_id] = second
    transition = next(iter(tiny_graph.transitions))
    tiny_graph.transitions[transition] = [second.change_id, *tiny_graph.transitions[transition]]

    rows = serialize_graph(tiny_graph, generation_id=GENERATION)

    assert [row[-1] for row in rows.dataset_edge_changes] == sorted(tiny_graph.changes)
    assert deserialize_graph(rows).transitions[transition] == sorted(tiny_graph.changes)


def test_graph_rows_accept_equivalent_artifacts_for_one_transition(tiny_graph):
    source, target = next(iter(tiny_graph.transitions))
    tiny_graph.add_edge(
        source,
        target,
        "DERIVED_FROM",
        {"artifact": "another-diff", "total_changes": 1},
    )

    rows = serialize_graph(tiny_graph, generation_id=GENERATION)

    assert rows.dataset_edges[0][-1] == "another-diff"
    assert deserialize_graph(rows).normalized() == tiny_graph.normalized()


def test_checkpoint_rows_hash_log_outside_canonical_snapshot(roots):
    log = roots.data / "logs" / "provenance.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"event":"cli"}\n', encoding="utf-8", newline="\n")
    snapshot = _canonical_snapshot(roots.data, roots.configs)

    rows = _checkpoint_rows(GENERATION, roots.data, roots.configs, snapshot)

    checkpoint = next(row for row in rows if row[1] == "data/logs/provenance.jsonl")
    assert checkpoint[2] == log.stat().st_size
    assert checkpoint[3] == sha256_file(log)
    assert checkpoint[4] is not None


class _Cursor:
    def __init__(self, rows=(), connection=None):
        self._rows = list(rows)
        self._connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def executemany(self, query, rows):
        self._connection.executemany(query, rows)


class _FakeConnection:
    def __init__(self):
        self.events = []
        self.active = None
        self.generations = {}
        self.tables = {
            name: []
            for name in (
                "metadata",
                "entities",
                "provenance_edges",
                "sample_changes",
                "dataset_edges",
                "dataset_edge_changes",
                "entity_status",
                "ingest_checkpoints",
                "ingested_artifacts",
            )
        }
        self.fail_on_publish = False

    @contextmanager
    def transaction(self):
        snapshot = copy.deepcopy((self.active, self.generations, self.tables))
        self.events.append("begin")
        try:
            yield
        except BaseException:
            self.active, self.generations, self.tables = snapshot
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    def close(self):
        pass

    def cursor(self):
        return _Cursor(connection=self)

    def executemany(self, query, rows):
        table = query.split("INSERT INTO vcp_provenance.", 1)[1].split("(", 1)[0].split()[0]
        self.tables[table].extend(tuple(row) for row in rows)

    def execute(self, query, params=()):
        compact = " ".join(query.split())
        if "pg_advisory_xact_lock" in compact:
            self.events.append("lock")
            return _Cursor([(None,)])
        if compact == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY":
            self.events.append("repeatable_read")
            return _Cursor()
        if compact.startswith("SELECT generation_id FROM vcp_provenance.active_generation"):
            self.events.append("read_active")
            return _Cursor([(self.active,)] if self.active is not None else [])
        if compact.startswith("INSERT INTO vcp_provenance.generations"):
            generation = params[0]
            self.generations[generation] = tuple(params)
            self.events.append("insert_generation")
            return _Cursor()
        if compact.startswith("UPDATE vcp_provenance.generations"):
            state, generation = params
            row = list(self.generations[generation])
            row[1] = state
            self.generations[generation] = tuple(row)
            self.events.append("mark_ready")
            return _Cursor()
        if compact.startswith("INSERT INTO vcp_provenance.active_generation"):
            if self.fail_on_publish:
                raise RuntimeError("injected failure before publish")
            self.active = params[0]
            self.events.append("publish_active")
            return _Cursor()
        if compact.startswith("DELETE FROM vcp_provenance.generations"):
            old = params[0]
            self.generations.pop(old, None)
            for table in self.tables:
                self.tables[table] = [row for row in self.tables[table] if row[0] != old]
            self.events.append("delete_old")
            return _Cursor()
        if compact.startswith("SELECT generation_id, entity_id"):
            return _Cursor(self._for_generation("entities", params[0]))
        if compact.startswith("SELECT generation_id, edge_id"):
            return _Cursor(self._for_generation("provenance_edges", params[0]))
        if compact.startswith("SELECT generation_id, change_id"):
            return _Cursor(self._for_generation("sample_changes", params[0]))
        if compact.startswith("SELECT generation_id, source_id, target_id, artifact_id"):
            return _Cursor(self._for_generation("dataset_edges", params[0]))
        if compact.startswith("SELECT generation_id, source_id, target_id, change_id"):
            return _Cursor(self._for_generation("dataset_edge_changes", params[0]))
        if compact.startswith("SELECT value FROM vcp_provenance.metadata"):
            generation, key = params
            rows = [row for row in self.tables["metadata"] if row[:2] == (generation, key)]
            return _Cursor([(row[2],) for row in rows])
        if compact.startswith("SELECT head_id, entity_id, status"):
            generation = params[0]
            rows = self._for_generation("entity_status", generation)
            if len(params) == 2:
                rows = [row for row in rows if row[1] == params[1]]
            return _Cursor([row[1:] for row in rows])
        if compact.startswith("SELECT DISTINCT head_id"):
            self.events.append("verify_status_parity")
            rows = self._for_generation("entity_status", params[0])
            return _Cursor((head,) for head in sorted({row[1] for row in rows}))
        if compact.startswith("SELECT state, schema_version"):
            generation = params[0]
            row = self.generations[generation]
            return _Cursor([(row[1], *row[2:])])
        if compact.startswith("SELECT COUNT(*) FROM vcp_provenance."):
            table = compact.split("SELECT COUNT(*) FROM vcp_provenance.", 1)[1].split()[0]
            return _Cursor([(len(self._for_generation(table, params[0])),)])
        if compact.startswith("SELECT key, value FROM vcp_provenance.metadata"):
            return _Cursor(row[1:] for row in self._for_generation("metadata", params[0]))
        if compact.startswith("SELECT pg_database_size"):
            return _Cursor([(4096,)])
        raise AssertionError((compact, params))

    def _for_generation(self, table, generation):
        return [row for row in self.tables[table] if row[0] == generation]


@pytest.fixture
def fake_postgres(monkeypatch):
    connection = _FakeConnection()
    driver = SimpleNamespace()
    monkeypatch.setattr("vcp.provenance.postgres._load_psycopg", lambda: driver)
    monkeypatch.setattr("vcp.provenance.postgres._connect", lambda _config: connection)
    monkeypatch.setattr(
        "vcp.provenance.postgres.install_schema_in_transaction", lambda _connection: None
    )
    monkeypatch.setattr("vcp.provenance.postgres.validate_schema", lambda _connection: None)
    return connection


def _patch_canonical(monkeypatch, graph):
    monkeypatch.setattr("vcp.provenance.postgres.build_graph", lambda *_args: graph)
    monkeypatch.setattr(
        "vcp.provenance.postgres._canonical_snapshot",
        lambda *_args: {"data/canonical.json": (7, "a" * 64)},
    )
    monkeypatch.setattr("vcp.provenance.postgres._checkpoint_rows", lambda *_args: ())
    monkeypatch.setattr("vcp.provenance.postgres._ingested_artifact_rows", lambda *_args: ())


def test_rebuild_publishes_active_generation_last(fake_postgres, roots, tiny_graph, monkeypatch):
    _patch_canonical(monkeypatch, tiny_graph)
    backend = PostgresProvenanceBackend(BackendConfig(BackendName.POSTGRESQL))

    result = backend.rebuild(roots.data, roots.configs)

    assert result.entities == len(tiny_graph.entities)
    assert fake_postgres.events.index("lock") < fake_postgres.events.index("insert_generation")
    assert fake_postgres.events.index("verify_status_parity") < fake_postgres.events.index(
        "publish_active"
    )
    assert fake_postgres.events.index("publish_active") < fake_postgres.events.index("commit")
    assert fake_postgres.events[-1] == "commit"


def test_concurrent_empty_bootstrap_holds_writer_lock_through_publication(
    roots, tiny_graph, monkeypatch
):
    """Model READ COMMITTED catalogs and transaction-scoped advisory lock ownership."""
    from vcp.provenance import postgres_schema

    writer_lock = Lock()
    first_in_ddl = Event()
    second_at_lock = Event()
    catalog = {"exists": False}
    connections = []

    class BootstrapConnection:
        def __init__(self):
            self.events = []
            self.locked = False
            self.pending_schema = False
            self.in_transaction = False

        @contextmanager
        def transaction(self):
            assert not self.in_transaction, "bootstrap must not use a separate transaction"
            self.in_transaction = True
            self.events.append("begin")
            try:
                yield
                if self.pending_schema:
                    catalog["exists"] = True
                self.events.append("commit")
            finally:
                self.in_transaction = False
                if self.locked:
                    self.locked = False
                    writer_lock.release()

        def execute(self, query, params=()):
            if "pg_advisory_xact_lock" in query:
                assert self.in_transaction
                assert params == (0x56435050524F5631,)
                if first_in_ddl.is_set():
                    second_at_lock.set()
                writer_lock.acquire()
                self.locked = True
                self.events.append("lock")
            elif query == "SELECT to_regnamespace(%s)":
                assert self.locked, "catalog existence checked before writer lock"
                self.events.append(("exists", catalog["exists"]))
                return _Cursor([("vcp_provenance" if catalog["exists"] else None,)])
            elif "obj_description" in query:
                return _Cursor([("vcp_provenance_schema_version=1",)])
            elif query.startswith("CREATE SCHEMA"):
                assert self.locked
                self.pending_schema = True
                first_in_ddl.set()
                assert second_at_lock.wait(5), "second bootstrap never attempted writer lock"
                self.events.append("ddl")
            elif query.startswith("SELECT generation_id"):
                return _Cursor()
            else:
                assert self.locked
            return _Cursor()

        def close(self):
            self.events.append("close")

    def connect(_config):
        connection = BootstrapConnection()
        connections.append(connection)
        return connection

    def publish(_backend, connection, *_args):
        assert connection.locked
        assert "commit" not in connection.events, "DDL committed separately before publication"
        connection.events.append("publish")

    _patch_canonical(monkeypatch, tiny_graph)
    monkeypatch.setattr("vcp.provenance.postgres._load_psycopg", lambda: SimpleNamespace())
    monkeypatch.setattr("vcp.provenance.postgres._connect", connect)
    monkeypatch.setattr("vcp.provenance.postgres.install_schema", postgres_schema.install_schema)
    monkeypatch.setattr(PostgresProvenanceBackend, "_publish_generation", publish)
    backend = PostgresProvenanceBackend(BackendConfig(BackendName.POSTGRESQL))
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(backend.rebuild, roots.data, roots.configs)
        # A missing lock fails immediately; do not obscure that RED with a timeout.
        if first_in_ddl.wait(1):
            second = executor.submit(backend.rebuild, roots.data, roots.configs)
            second.result(timeout=10)
        first.result(timeout=10)
    assert len(connections) == 2
    assert catalog["exists"] is True
    assert [event for item in connections for event in item.events if isinstance(event, tuple)] == [
        ("exists", False),
        ("exists", True),
    ]
    for connection in connections:
        assert connection.events[:2] == ["begin", "lock"]
        assert connection.events[-3:] == ["publish", "commit", "close"]
        assert connection.events.count("begin") == 1


def test_failure_before_publish_rolls_back_to_previous_generation(
    fake_postgres, roots, tiny_graph, monkeypatch
):
    _patch_canonical(monkeypatch, tiny_graph)
    backend = PostgresProvenanceBackend(BackendConfig(BackendName.POSTGRESQL))
    backend.rebuild(roots.data, roots.configs)
    old_generation = fake_postgres.active
    before = backend.normalized()
    fake_postgres.fail_on_publish = True

    with pytest.raises(RuntimeError, match="injected failure"):
        backend.rebuild(roots.data, roots.configs)

    assert fake_postgres.active == old_generation
    assert backend.normalized() == before
    assert "rollback" in fake_postgres.events


def test_successful_rebuild_deletes_previous_generation_after_pointer_update(
    fake_postgres, roots, tiny_graph, monkeypatch
):
    _patch_canonical(monkeypatch, tiny_graph)
    backend = PostgresProvenanceBackend(BackendConfig(BackendName.POSTGRESQL))
    backend.rebuild(roots.data, roots.configs)
    old_generation = fake_postgres.active

    backend.rebuild(roots.data, roots.configs)

    assert fake_postgres.active != old_generation
    assert old_generation not in fake_postgres.generations
    publish = max(
        index for index, event in enumerate(fake_postgres.events) if event == "publish_active"
    )
    assert fake_postgres.events[publish + 1 : publish + 3] == ["delete_old", "commit"]


def test_read_query_and_verify_match_canonical(fake_postgres, roots, tiny_graph, monkeypatch):
    _patch_canonical(monkeypatch, tiny_graph)
    backend = PostgresProvenanceBackend(BackendConfig(BackendName.POSTGRESQL))
    rebuilt = backend.rebuild(roots.data, roots.configs)
    head = dataset_version_id("new", NEW_HASH)

    assert backend.load_graph().normalized() == tiny_graph.normalized()
    assert backend.normalized()["graph"] == tiny_graph.normalized()
    assert backend.statuses(head)
    assert backend.stats()["graph_hash"] == rebuilt.graph_hash
    assert backend.stats()["state"] == "ready"
    assert backend.verify(roots.data, roots.configs).ok is True
    last_repeatable_read = max(
        index for index, event in enumerate(fake_postgres.events) if event == "repeatable_read"
    )
    assert fake_postgres.events[last_repeatable_read + 1] == "read_active"


@pytest.mark.parametrize("command", ["stale", "status"])
def test_compound_cli_keeps_snapshot_when_generation_publishes_between_subcalls(
    command, fake_postgres, roots, tiny_graph, monkeypatch
):
    from vcp import cli_provenance

    head = dataset_version_id("new", NEW_HASH)
    tiny_graph.add_entity(
        ProvenanceEntity(entity_id="run:old", entity_type="run", key="old", dataset_version_id=head)
    )
    _patch_canonical(monkeypatch, tiny_graph)
    backend = PostgresProvenanceBackend(BackendConfig(BackendName.POSTGRESQL))
    old_result = backend.rebuild(roots.data, roots.configs)
    old = copy.deepcopy((fake_postgres.active, fake_postgres.generations, fake_postgres.tables))
    newer = copy.deepcopy(tiny_graph)
    newer.entities.pop("run:old")
    for ident in ("run:new", "run:extra"):
        newer.add_entity(
            ProvenanceEntity(entity_id=ident, entity_type="run", key=ident, dataset_version_id=head)
        )
    _patch_canonical(monkeypatch, newer)
    backend.rebuild(roots.data, roots.configs)
    published = copy.deepcopy(
        (fake_postgres.active, fake_postgres.generations, fake_postgres.tables)
    )
    fake_postgres.active, fake_postgres.generations, fake_postgres.tables = old
    publications = []
    readers = []

    def publish():
        publications.append(True)
        fake_postgres.active, fake_postgres.generations, fake_postgres.tables = published

    class SnapshotConnection(_FakeConnection):
        def execute(self, query, params=()):
            if query == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY":
                self.active, self.generations, self.tables = copy.deepcopy(
                    (fake_postgres.active, fake_postgres.generations, fake_postgres.tables)
                )
            result = super().execute(query, params)
            if command == "status" and "pg_database_size" in query:
                publish()
            return result

    def connect(_config):
        connection = SnapshotConnection()
        readers.append(connection)
        return connection

    if command == "stale":
        resolve = cli_provenance._dataset_id

        def resolve_then_publish(graph, value):
            resolved = resolve(graph, value)
            publish()
            return resolved

        monkeypatch.setattr(cli_provenance, "_dataset_id", resolve_then_publish)
    monkeypatch.setattr("vcp.provenance.postgres._connect", connect)
    monkeypatch.setattr(cli_provenance, "make_backend", lambda *_args: backend)
    result = CliRunner().invoke(
        app,
        [
            "provenance",
            command,
            "--backend",
            "postgresql",
            "--json",
            "--data-root",
            str(roots.data),
            "--configs-root",
            str(roots.configs),
            *(["--head", head] if command == "stale" else []),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert publications == [True]
    if command == "stale":
        assert [row["entity_id"] for row in payload["runs"]] == ["run:old"]
    else:
        assert payload["entities"] == len(tiny_graph.entities)
        assert payload["graph_hash"] == old_result.graph_hash
        assert payload["synchronized"] is False
        assert "graph differs from canonical replay" in payload["issues"]
    assert sum(reader.events.count("read_active") for reader in readers) == 1


def test_verify_reports_recorded_generation_mismatch(fake_postgres, roots, tiny_graph, monkeypatch):
    _patch_canonical(monkeypatch, tiny_graph)
    backend = PostgresProvenanceBackend(BackendConfig(BackendName.POSTGRESQL))
    backend.rebuild(roots.data, roots.configs)
    generation = fake_postgres.active
    row = list(fake_postgres.generations[generation])
    row[1] = "building"
    row[4] = "f" * 64
    fake_postgres.generations[generation] = tuple(row)

    result = backend.verify(roots.data, roots.configs)

    assert result.ok is False
    assert "active generation is not ready" in result.issues
    assert "recorded graph hash differs" in result.issues


def test_sync_uses_atomic_full_rebuild(fake_postgres, roots, tiny_graph, monkeypatch):
    _patch_canonical(monkeypatch, tiny_graph)
    backend = PostgresProvenanceBackend(BackendConfig(BackendName.POSTGRESQL))

    result = backend.sync(roots.data, roots.configs)

    assert result.entities == len(tiny_graph.entities)
    assert fake_postgres.events.count("lock") == 1


def test_rebuild_rejects_canonical_drift_before_publication(
    fake_postgres, roots, tiny_graph, monkeypatch
):
    snapshots = iter(
        [
            {"data/a": (1, "a" * 64)},
            {"data/a": (1, "a" * 64)},
            {"data/a": (2, "b" * 64)},
        ]
    )
    monkeypatch.setattr("vcp.provenance.postgres.build_graph", lambda *_args: tiny_graph)
    monkeypatch.setattr(
        "vcp.provenance.postgres._canonical_snapshot", lambda *_args: next(snapshots)
    )
    monkeypatch.setattr("vcp.provenance.postgres._checkpoint_rows", lambda *_args: ())
    monkeypatch.setattr("vcp.provenance.postgres._ingested_artifact_rows", lambda *_args: ())
    backend = PostgresProvenanceBackend(BackendConfig(BackendName.POSTGRESQL))

    with pytest.raises(IntegrityError, match="canonical_drift"):
        backend.rebuild(roots.data, roots.configs)

    assert fake_postgres.active is None
    assert fake_postgres.events[-1] == "rollback"


def test_query_driver_error_is_redacted(monkeypatch):
    class FakeDatabaseError(Exception):
        sqlstate = "XX001"

    class BrokenConnection(_FakeConnection):
        def execute(self, query, params=()):
            compact = " ".join(query.split())
            if compact.startswith("SELECT generation_id FROM vcp_provenance.active_generation"):
                raise FakeDatabaseError("password=TOPSECRET")
            return super().execute(query, params)

    driver = SimpleNamespace(Error=FakeDatabaseError)
    monkeypatch.setattr("vcp.provenance.postgres._load_psycopg", lambda: driver)
    monkeypatch.setattr("vcp.provenance.postgres._connect", lambda _config: BrokenConnection())
    monkeypatch.setattr("vcp.provenance.postgres.validate_schema", lambda _connection: None)
    backend = PostgresProvenanceBackend(BackendConfig(BackendName.POSTGRESQL))

    with pytest.raises(ValidationFailed) as caught:
        backend.load_graph()

    assert "TOPSECRET" not in str(caught.value)
    assert caught.value.fields == {"backend": "postgresql", "sqlstate": "XX001"}
