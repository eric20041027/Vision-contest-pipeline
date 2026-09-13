from contextlib import contextmanager

import pytest

from vcp.core.errors import IntegrityError
from vcp.provenance.postgres_schema import (
    POSTGRES_DDL,
    POSTGRES_SCHEMA_VERSION,
    SCHEMA_NAME,
    install_schema,
    validate_schema,
)

EXPECTED_TABLES = (
    "generations",
    "active_generation",
    "metadata",
    "entities",
    "provenance_edges",
    "dataset_edges",
    "sample_changes",
    "dataset_edge_changes",
    "entity_status",
    "ingest_checkpoints",
    "ingested_artifacts",
    "maintenance_decisions",
)


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, version=None):
        self.statements = []
        self.version = version
        self.transactions = 0

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        if "obj_description" in statement:
            return _Result(self.version)
        return _Result(None)


def test_schema_declares_every_normalized_table_and_no_trigger():
    sql = "\n".join(POSTGRES_DDL)
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS vcp_provenance.{table}" in sql
    assert "JSONB" in sql
    assert "change_ids_json" not in sql
    assert "CREATE TRIGGER" not in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql


def test_schema_has_forward_reverse_status_and_artifact_indexes():
    sql = "\n".join(POSTGRES_DDL)
    for name in ("edges_source", "edges_target", "status_head_state", "changes_sample"):
        assert f"CREATE INDEX IF NOT EXISTS {name}" in sql
    assert "dataset_edges_target" in sql
    assert "status_entity_head" in sql


def test_schema_declares_vocabulary_hash_and_generation_constraints():
    sql = "\n".join(POSTGRES_DDL)
    for value in (
        "'VALID', 'STALE', 'REVIEW', 'BROKEN'",
        "'ADDED', 'REMOVED', 'MODIFIED'",
        "'DERIVED_FROM', 'CONTAINS_CHANGE'",
        "'verified_zero_semantic_changes', 'requested_incremental'",
        "'INPUT_AFFECTING', 'TRAINING_AFFECTING'",
        "'VIEWS', 'LABELS', 'LABEL_SOURCE'",
        "^[0-9a-f]{64}$",
        "graph_record_count >= 0",
        "consumed_bytes >= 0",
        "singleton IS TRUE",
        "REFERENCES vcp_provenance.generations(generation_id) ON DELETE CASCADE",
    ):
        assert value in sql
    assert "REFERENCES vcp_provenance.dataset_edges(generation_id, source_id, target_id)" in sql
    assert "REFERENCES vcp_provenance.sample_changes(generation_id, change_id)" in sql


def test_install_runs_every_statement_once_in_one_transaction():
    connection = _Connection()

    install_schema(connection)

    assert connection.transactions == 1
    assert [statement for statement, _params in connection.statements] == list(POSTGRES_DDL)
    assert POSTGRES_SCHEMA_VERSION == 1
    assert SCHEMA_NAME == "vcp_provenance"


@pytest.mark.parametrize("value", [None, ("wrong",)])
def test_validate_schema_rejects_absent_or_wrong_version(value):
    with pytest.raises(IntegrityError, match="mismatch: PostgreSQL provenance schema version"):
        validate_schema(_Connection(value))


def test_validate_schema_accepts_installed_version_marker():
    connection = _Connection(("vcp_provenance_schema_version=1",))

    validate_schema(connection)

    assert connection.transactions == 1
    assert connection.statements[0][1] == (SCHEMA_NAME,)
