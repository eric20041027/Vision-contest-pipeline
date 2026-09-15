"""PostgreSQL v1 DDL for the disposable provenance index."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from vcp.core.errors import IntegrityError

POSTGRES_SCHEMA_VERSION = 1
SCHEMA_NAME = "vcp_provenance"
WRITER_LOCK_KEY = 0x56435050524F5631
_SCHEMA_VERSION_COMMENT = f"vcp_provenance_schema_version={POSTGRES_SCHEMA_VERSION}"
_HASH_CHECK = "VALUE ~ '^[0-9a-f]{64}$'"


def _hash_check(column: str) -> str:
    return _HASH_CHECK.replace("VALUE", column)


POSTGRES_DDL = (
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.generations (
        generation_id uuid PRIMARY KEY,
        state text NOT NULL CHECK (state IN ('building', 'ready')),
        schema_version smallint NOT NULL CHECK (schema_version = {POSTGRES_SCHEMA_VERSION}),
        built_at timestamptz NOT NULL,
        graph_hash char(64) NOT NULL CHECK ({_hash_check("graph_hash")}),
        graph_record_count bigint NOT NULL CHECK (graph_record_count >= 0),
        graph_record_sum char(64) NOT NULL CHECK ({_hash_check("graph_record_sum")}),
        canonical_snapshot_hash char(64) NOT NULL
            CHECK ({_hash_check("canonical_snapshot_hash")})
    )""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.active_generation (
        singleton boolean PRIMARY KEY CHECK (singleton IS TRUE),
        generation_id uuid UNIQUE NOT NULL
            REFERENCES {SCHEMA_NAME}.generations(generation_id)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.metadata (
        generation_id uuid NOT NULL
            REFERENCES {SCHEMA_NAME}.generations(generation_id) ON DELETE CASCADE,
        key text NOT NULL,
        value text NOT NULL,
        PRIMARY KEY (generation_id, key)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.entities (
        generation_id uuid NOT NULL
            REFERENCES {SCHEMA_NAME}.generations(generation_id) ON DELETE CASCADE,
        entity_id text NOT NULL,
        entity_type text NOT NULL,
        key_value text NOT NULL,
        dataset_version_id text,
        attributes JSONB NOT NULL DEFAULT '{{}}'::JSONB,
        broken_reason text,
        PRIMARY KEY (generation_id, entity_id)
    )""",
    f"""CREATE INDEX IF NOT EXISTS entities_type
        ON {SCHEMA_NAME}.entities(generation_id, entity_type, entity_id)""",
    f"""CREATE INDEX IF NOT EXISTS entities_dataset
        ON {SCHEMA_NAME}.entities(generation_id, dataset_version_id, entity_type, entity_id)""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.provenance_edges (
        generation_id uuid NOT NULL
            REFERENCES {SCHEMA_NAME}.generations(generation_id) ON DELETE CASCADE,
        edge_id text NOT NULL,
        source_id text NOT NULL,
        target_id text NOT NULL,
        edge_type text NOT NULL CHECK (edge_type IN (
            'DERIVED_FROM', 'CONTAINS_CHANGE', 'USES_SPLIT', 'CONSUMED_BY', 'PRODUCED_BY',
            'EVALUATED_BY', 'COMBINED_INTO', 'SUPERSEDES', 'BACKED_UP_AS', 'SUBMITTED_AS'
        )),
        attributes JSONB NOT NULL DEFAULT '{{}}'::JSONB,
        PRIMARY KEY (generation_id, edge_id),
        FOREIGN KEY (generation_id, source_id)
            REFERENCES {SCHEMA_NAME}.entities(generation_id, entity_id)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (generation_id, target_id)
            REFERENCES {SCHEMA_NAME}.entities(generation_id, entity_id)
            DEFERRABLE INITIALLY DEFERRED
    )""",
    f"""CREATE INDEX IF NOT EXISTS edges_source
        ON {SCHEMA_NAME}.provenance_edges(generation_id, source_id, edge_type, target_id)""",
    f"""CREATE INDEX IF NOT EXISTS edges_target
        ON {SCHEMA_NAME}.provenance_edges(generation_id, target_id, edge_type, source_id)""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.dataset_edges (
        generation_id uuid NOT NULL
            REFERENCES {SCHEMA_NAME}.generations(generation_id) ON DELETE CASCADE,
        source_id text NOT NULL,
        target_id text NOT NULL,
        artifact_id text NOT NULL,
        PRIMARY KEY (generation_id, source_id, target_id),
        FOREIGN KEY (generation_id, source_id)
            REFERENCES {SCHEMA_NAME}.entities(generation_id, entity_id)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (generation_id, target_id)
            REFERENCES {SCHEMA_NAME}.entities(generation_id, entity_id)
            DEFERRABLE INITIALLY DEFERRED
    )""",
    f"""CREATE INDEX IF NOT EXISTS dataset_edges_target
        ON {SCHEMA_NAME}.dataset_edges(generation_id, target_id, source_id)""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.sample_changes (
        generation_id uuid NOT NULL
            REFERENCES {SCHEMA_NAME}.generations(generation_id) ON DELETE CASCADE,
        change_id char(64) NOT NULL CHECK ({_hash_check("change_id")}),
        schema_version smallint NOT NULL CHECK (schema_version = {POSTGRES_SCHEMA_VERSION}),
        source_id text NOT NULL,
        target_id text NOT NULL,
        from_dataset text NOT NULL,
        from_samples_hash char(64) NOT NULL CHECK ({_hash_check("from_samples_hash")}),
        to_dataset text NOT NULL,
        to_samples_hash char(64) NOT NULL CHECK ({_hash_check("to_samples_hash")}),
        sample_id text NOT NULL,
        change_type text NOT NULL CHECK (change_type IN ('ADDED', 'REMOVED', 'MODIFIED')),
        changed_domains text[] NOT NULL CHECK (changed_domains <@ ARRAY[
            'VIEWS', 'LABELS', 'LABEL_SOURCE', 'GROUP', 'META', 'UNKNOWN'
        ]::text[]),
        changed_fields text[] NOT NULL,
        semantic_effects text[] NOT NULL CHECK (semantic_effects <@ ARRAY[
            'INPUT_AFFECTING', 'TRAINING_AFFECTING', 'SPLIT_AFFECTING',
            'EVALUATION_AFFECTING', 'DISPLAY_ONLY', 'UNKNOWN'
        ]::text[]),
        before_row_hash char(64) CHECK ({_hash_check("before_row_hash")}),
        after_row_hash char(64) CHECK ({_hash_check("after_row_hash")}),
        PRIMARY KEY (generation_id, change_id),
        CHECK (
            (change_type = 'ADDED' AND before_row_hash IS NULL AND after_row_hash IS NOT NULL)
            OR (change_type = 'REMOVED' AND before_row_hash IS NOT NULL AND after_row_hash IS NULL)
            OR (
                change_type = 'MODIFIED' AND before_row_hash IS NOT NULL
                AND after_row_hash IS NOT NULL
            )
        ),
        FOREIGN KEY (generation_id, source_id)
            REFERENCES {SCHEMA_NAME}.entities(generation_id, entity_id)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (generation_id, target_id)
            REFERENCES {SCHEMA_NAME}.entities(generation_id, entity_id)
            DEFERRABLE INITIALLY DEFERRED
    )""",
    f"""CREATE INDEX IF NOT EXISTS changes_sample
        ON {SCHEMA_NAME}.sample_changes(generation_id, sample_id, change_id)""",
    f"""CREATE INDEX IF NOT EXISTS changes_transition
        ON {SCHEMA_NAME}.sample_changes(generation_id, source_id, target_id)""",
    f"""CREATE INDEX IF NOT EXISTS changes_target
        ON {SCHEMA_NAME}.sample_changes(generation_id, target_id)""",
    f"""CREATE INDEX IF NOT EXISTS changes_type
        ON {SCHEMA_NAME}.sample_changes(generation_id, change_type, change_id)""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.dataset_edge_changes (
        generation_id uuid NOT NULL
            REFERENCES {SCHEMA_NAME}.generations(generation_id) ON DELETE CASCADE,
        source_id text NOT NULL,
        target_id text NOT NULL,
        change_id char(64) NOT NULL CHECK ({_hash_check("change_id")}),
        PRIMARY KEY (generation_id, source_id, target_id, change_id),
        FOREIGN KEY (generation_id, source_id, target_id)
            REFERENCES {SCHEMA_NAME}.dataset_edges(generation_id, source_id, target_id)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (generation_id, change_id)
            REFERENCES {SCHEMA_NAME}.sample_changes(generation_id, change_id)
            DEFERRABLE INITIALLY DEFERRED
    )""",
    f"""CREATE INDEX IF NOT EXISTS dataset_edge_changes_change
        ON {SCHEMA_NAME}.dataset_edge_changes(generation_id, change_id)""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.entity_status (
        generation_id uuid NOT NULL
            REFERENCES {SCHEMA_NAME}.generations(generation_id) ON DELETE CASCADE,
        head_id text NOT NULL,
        entity_id text NOT NULL,
        status text NOT NULL CHECK (status IN ('VALID', 'STALE', 'REVIEW', 'BROKEN')),
        reason text NOT NULL,
        predecessor_id text,
        PRIMARY KEY (generation_id, head_id, entity_id),
        FOREIGN KEY (generation_id, head_id)
            REFERENCES {SCHEMA_NAME}.entities(generation_id, entity_id)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (generation_id, entity_id)
            REFERENCES {SCHEMA_NAME}.entities(generation_id, entity_id)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (generation_id, predecessor_id)
            REFERENCES {SCHEMA_NAME}.entities(generation_id, entity_id)
            DEFERRABLE INITIALLY DEFERRED
    )""",
    f"""CREATE INDEX IF NOT EXISTS status_head_state
        ON {SCHEMA_NAME}.entity_status(generation_id, head_id, status, entity_id)""",
    f"""CREATE INDEX IF NOT EXISTS status_entity_head
        ON {SCHEMA_NAME}.entity_status(generation_id, entity_id, head_id)""",
    f"""CREATE INDEX IF NOT EXISTS status_predecessor
        ON {SCHEMA_NAME}.entity_status(generation_id, predecessor_id)
        WHERE predecessor_id IS NOT NULL""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.ingest_checkpoints (
        generation_id uuid NOT NULL
            REFERENCES {SCHEMA_NAME}.generations(generation_id) ON DELETE CASCADE,
        source_path text NOT NULL,
        consumed_bytes bigint NOT NULL CHECK (consumed_bytes >= 0),
        prefix_sha256 char(64) NOT NULL CHECK ({_hash_check("prefix_sha256")}),
        last_event_id char(64) CHECK ({_hash_check("last_event_id")}),
        PRIMARY KEY (generation_id, source_path)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.ingested_artifacts (
        generation_id uuid NOT NULL
            REFERENCES {SCHEMA_NAME}.generations(generation_id) ON DELETE CASCADE,
        artifact_id text NOT NULL,
        manifest_sha256 char(64) NOT NULL CHECK ({_hash_check("manifest_sha256")}),
        PRIMARY KEY (generation_id, artifact_id)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.maintenance_decisions (
        generation_id uuid NOT NULL
            REFERENCES {SCHEMA_NAME}.generations(generation_id) ON DELETE CASCADE,
        decision_id uuid NOT NULL,
        artifact_id text NOT NULL,
        requested_strategy text NOT NULL
            CHECK (requested_strategy IN ('incremental', 'full', 'auto')),
        selected_strategy text NOT NULL
            CHECK (selected_strategy IN ('NO_OP', 'INCREMENTAL', 'FULL')),
        reason_code text NOT NULL CHECK (reason_code IN (
            'verified_zero_semantic_changes', 'requested_incremental', 'requested_full',
            'calibrated_incremental_lower_confident_cost',
            'calibrated_full_lower_or_uncertain_cost', 'fallback_policy_absent_full',
            'duplicate_artifact_no_op'
        )),
        changed_samples bigint NOT NULL CHECK (changed_samples >= 0),
        dirty_entities bigint NOT NULL CHECK (dirty_entities >= 0),
        total_entities bigint NOT NULL CHECK (total_entities >= 0),
        dirty_ratio double precision NOT NULL CHECK (dirty_ratio >= 0 AND dirty_ratio <= 1),
        total_edges bigint NOT NULL CHECK (total_edges >= 0),
        historical_changes bigint NOT NULL CHECK (historical_changes >= 0),
        head_count bigint NOT NULL CHECK (head_count >= 0),
        estimated_incremental_ms double precision
            CHECK (estimated_incremental_ms IS NULL OR estimated_incremental_ms >= 0),
        estimated_full_ms double precision
            CHECK (estimated_full_ms IS NULL OR estimated_full_ms >= 0),
        policy_version text NOT NULL,
        elapsed_ms double precision NOT NULL CHECK (elapsed_ms >= 0),
        decided_at timestamptz NOT NULL,
        PRIMARY KEY (generation_id, decision_id)
    )""",
    f"COMMENT ON SCHEMA {SCHEMA_NAME} IS '{_SCHEMA_VERSION_COMMENT}'",
)

_SCHEMA_EXISTS_QUERY = "SELECT to_regnamespace(%s)"
_VERSION_QUERY = "SELECT obj_description(to_regnamespace(%s), 'pg_namespace')"


@contextmanager
def _transaction(connection: Any) -> Iterator[None]:
    """Use the driver's explicit transaction context when it provides one."""
    transaction = getattr(connection, "transaction", None)
    if transaction is None:
        yield
        return
    with transaction():
        yield


def _scalar(row: Any) -> Any:
    if isinstance(row, Mapping):
        return next(iter(row.values()), None)
    return row[0] if row is not None else None


def _schema_marker(connection: Any) -> Any:
    row = connection.execute(_VERSION_QUERY, (SCHEMA_NAME,)).fetchone()
    return _scalar(row)


def _require_matching_schema_marker(connection: Any) -> None:
    if _schema_marker(connection) != _SCHEMA_VERSION_COMMENT:
        raise IntegrityError("mismatch: PostgreSQL provenance schema version")


def install_schema(connection: Any) -> None:
    """Install the fixed v1 layout atomically without applying a migration."""
    with _transaction(connection):
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (WRITER_LOCK_KEY,))
        install_schema_in_transaction(connection)


def install_schema_in_transaction(connection: Any) -> None:
    """Install within the caller's transaction, which must already hold the writer lock."""
    existing_schema = _scalar(connection.execute(_SCHEMA_EXISTS_QUERY, (SCHEMA_NAME,)).fetchone())
    if existing_schema is not None:
        _require_matching_schema_marker(connection)
        statements = POSTGRES_DDL[1:-1]
    else:
        statements = POSTGRES_DDL
    for statement in statements:
        connection.execute(statement)


def validate_schema(connection: Any) -> None:
    """Reject an absent or incompatible PostgreSQL provenance schema."""
    with _transaction(connection):
        _require_matching_schema_marker(connection)


def validate_schema_in_transaction(connection: Any) -> None:
    """Validate under an existing writer transaction without opening a savepoint."""
    _require_matching_schema_marker(connection)


__all__ = [
    "POSTGRES_DDL",
    "POSTGRES_SCHEMA_VERSION",
    "SCHEMA_NAME",
    "WRITER_LOCK_KEY",
    "install_schema",
    "install_schema_in_transaction",
    "validate_schema",
    "validate_schema_in_transaction",
]
