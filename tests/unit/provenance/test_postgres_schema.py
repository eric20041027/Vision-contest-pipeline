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
    def __init__(self, *, exists=False, version=None):
        self.statements = []
        self.exists = exists
        self.version = version
        self.transactions = 0

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        if "to_regnamespace" in statement and "obj_description" not in statement:
            return _Result((SCHEMA_NAME,) if self.exists else (None,))
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
    assert "dataset_edge_changes_change" in sql
    assert "status_predecessor" in sql
    assert "WHERE predecessor_id IS NOT NULL" in sql


def test_schema_declares_every_vocabulary_member():
    sql = "\n".join(POSTGRES_DDL)
    for value in (
        "DERIVED_FROM",
        "CONTAINS_CHANGE",
        "USES_SPLIT",
        "CONSUMED_BY",
        "PRODUCED_BY",
        "EVALUATED_BY",
        "COMBINED_INTO",
        "SUPERSEDES",
        "BACKED_UP_AS",
        "SUBMITTED_AS",
        "ADDED",
        "REMOVED",
        "MODIFIED",
        "VIEWS",
        "LABELS",
        "LABEL_SOURCE",
        "GROUP",
        "META",
        "UNKNOWN",
        "INPUT_AFFECTING",
        "TRAINING_AFFECTING",
        "SPLIT_AFFECTING",
        "EVALUATION_AFFECTING",
        "DISPLAY_ONLY",
        "VALID",
        "STALE",
        "REVIEW",
        "BROKEN",
        "incremental",
        "full",
        "auto",
        "NO_OP",
        "INCREMENTAL",
        "FULL",
        "verified_zero_semantic_changes",
        "requested_incremental",
        "requested_full",
        "calibrated_incremental_lower_confident_cost",
        "calibrated_full_lower_or_uncertain_cost",
        "fallback_policy_absent_full",
        "duplicate_artifact_no_op",
    ):
        assert value in sql


def test_schema_declares_every_hash_and_numeric_constraint():
    sql = "\n".join(POSTGRES_DDL)
    for column in (
        "graph_hash",
        "graph_record_sum",
        "canonical_snapshot_hash",
        "change_id",
        "from_samples_hash",
        "to_samples_hash",
        "before_row_hash",
        "after_row_hash",
        "prefix_sha256",
        "last_event_id",
        "manifest_sha256",
    ):
        assert f"{column} char(64)" in sql
        assert f"CHECK ({column} ~ '^[0-9a-f]{{64}}$')" in sql
    for constraint in (
        "graph_record_count >= 0",
        "consumed_bytes >= 0",
        "changed_samples >= 0",
        "dirty_entities >= 0",
        "total_entities >= 0",
        "total_edges >= 0",
        "historical_changes >= 0",
        "head_count >= 0",
        "estimated_incremental_ms IS NULL OR estimated_incremental_ms >= 0",
        "estimated_full_ms IS NULL OR estimated_full_ms >= 0",
        "elapsed_ms >= 0",
        "dirty_ratio >= 0 AND dirty_ratio <= 1",
    ):
        assert constraint in sql


def test_schema_declares_generation_and_normalized_join_foreign_keys():
    sql = "\n".join(POSTGRES_DDL)
    assert "singleton IS TRUE" in sql
    assert "REFERENCES vcp_provenance.generations(generation_id) ON DELETE CASCADE" in sql
    assert "REFERENCES vcp_provenance.dataset_edges(generation_id, source_id, target_id)" in sql
    assert "REFERENCES vcp_provenance.sample_changes(generation_id, change_id)" in sql


def test_install_creates_and_marks_a_new_schema_in_one_transaction():
    connection = _Connection()

    install_schema(connection)

    assert connection.transactions == 1
    statements = [statement for statement, _params in connection.statements]
    assert statements[1:] == list(POSTGRES_DDL)
    assert "to_regnamespace" in statements[0]
    assert statements[-1].startswith("COMMENT ON SCHEMA")
    assert POSTGRES_SCHEMA_VERSION == 1
    assert SCHEMA_NAME == "vcp_provenance"


@pytest.mark.parametrize("version", [None, ("vcp_provenance_schema_version=2",)])
def test_install_rejects_existing_schema_without_matching_marker(version):
    connection = _Connection(exists=True, version=version)

    with pytest.raises(IntegrityError, match="mismatch: PostgreSQL provenance schema version"):
        install_schema(connection)

    assert [statement for statement, _params in connection.statements] == [
        "SELECT to_regnamespace(%s)",
        "SELECT obj_description(to_regnamespace(%s), 'pg_namespace')",
    ]


def test_install_is_idempotent_for_existing_matching_schema():
    connection = _Connection(exists=True, version=("vcp_provenance_schema_version=1",))

    install_schema(connection)

    statements = [statement for statement, _params in connection.statements]
    assert statements[:2] == [
        "SELECT to_regnamespace(%s)",
        "SELECT obj_description(to_regnamespace(%s), 'pg_namespace')",
    ]
    assert statements[2:] == list(POSTGRES_DDL[1:-1])


@pytest.mark.parametrize("value", [None, ("wrong",)])
def test_validate_schema_rejects_absent_or_wrong_version(value):
    with pytest.raises(IntegrityError, match="mismatch: PostgreSQL provenance schema version"):
        validate_schema(_Connection(version=value))


def test_validate_schema_accepts_installed_version_marker():
    connection = _Connection(version=("vcp_provenance_schema_version=1",))

    validate_schema(connection)

    assert connection.transactions == 1
    assert connection.statements[0][1] == (SCHEMA_NAME,)


def test_validate_schema_is_absence_safe_and_uses_to_regnamespace():
    connection = _Connection()

    with pytest.raises(IntegrityError, match="mismatch: PostgreSQL provenance schema version"):
        validate_schema(connection)

    assert connection.statements == [
        ("SELECT obj_description(to_regnamespace(%s), 'pg_namespace')", (SCHEMA_NAME,))
    ]
