"""Real SQL, constraints, locks and MVCC: never substituted by a database double."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import pytest

from helpers import det_samples, det_with_runs, make_card
from vcp.core.errors import IntegrityError
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.source_audit import write_source_audit
from vcp.provenance import postgres
from vcp.provenance.diff import DatasetDiffSpec, create_dataset_diff
from vcp.provenance.graph import build_graph
from vcp.provenance.index import ProvenanceIndex, dataset_heads, graph_hash
from vcp.provenance.strategy import (
    FULL_FEATURE_ORDER,
    INCREMENTAL_FEATURE_ORDER,
    AdaptivePolicy,
    CostModel,
    write_policy_artifact,
)
from vcp.provenance.views import compute_statuses

pytestmark = pytest.mark.postgres


def dataset(roots, name, samples):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    result = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    result.save(paths)
    write_source_audit(paths, result.card, data_root=roots.data)
    return result


def diff(roots, ident="change", source="old", target="new"):
    return create_dataset_diff(
        DatasetDiffSpec(
            from_dataset=source,
            to_dataset=target,
            artifact_id=ident,
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )


def versions(roots, *, zero=False):
    samples = det_samples(4, seed=21)
    dataset(roots, "old", samples)
    changed = [sample.model_copy(deep=True) for sample in samples]
    if not zero:
        changed[0] = changed[0].model_copy(update={"group": "changed"})
    dataset(roots, "new", changed)


@pytest.fixture
def checkpoint_log(roots):
    path = roots.data / "logs" / "provenance.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"event_id": "a" * 64}) + "\n", encoding="utf-8", newline="\n")
    return path


def assert_parity(backend, roots):
    canonical = build_graph(roots.data, roots.configs)
    indexed = backend.load_graph()
    assert indexed.normalized() == canonical.normalized()
    assert dataset_heads(indexed) == dataset_heads(canonical)
    assert backend.stats()["graph_hash"] == graph_hash(canonical)
    heads = {key for key, entity in canonical.entities.items() if entity.entity_type == "dataset"}
    assert {row["head_id"] for row in backend.normalized()["statuses"]} == heads
    for head in sorted(heads):
        assert backend.statuses(head) == compute_statuses(canonical, head)
    verified = backend.verify(roots.data, roots.configs)
    assert verified.ok, verified.issues


@pytest.mark.parametrize("selected", ["INCREMENTAL", "FULL"])
def test_auto_executes_both_policy_selected_paths(postgres_harness, roots, selected):
    """Synthetic selector inputs only: no fitted policy or benchmark claim."""
    versions(roots)
    evidence = roots.data / "synthetic-selector-input.json"
    evidence.write_text(
        json.dumps({"scenario_ids": ["synthetic-selector"], "scenario_hashes": ["a" * 64]}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    with postgres_harness.connect() as connection:
        assert connection.info.server_version == 170011
        major, fingerprint = postgres.maintenance_environment(connection)
    policy = AdaptivePolicy(
        backend_schema_version=1,
        postgresql_major=major,
        benchmark_schema_version=1,
        environment_fingerprint=fingerprint,
        calibration_sha256=sha256_file(evidence),
        incremental_model=CostModel(
            feature_order=INCREMENTAL_FEATURE_ORDER,
            coefficients={name: 0.0 for name in INCREMENTAL_FEATURE_ORDER},
            intercept_ms=1.0 if selected == "INCREMENTAL" else 100.0,
        ),
        full_model=CostModel(
            feature_order=FULL_FEATURE_ORDER,
            coefficients={name: 0.0 for name in FULL_FEATURE_ORDER},
            intercept_ms=100.0 if selected == "INCREMENTAL" else 1.0,
        ),
        incremental_rmse_ms=1.0,
        full_rmse_ms=1.0,
        training_row_count=1,
    )
    write_policy_artifact(roots.data, policy, evidence)
    backend = postgres_harness.backend
    backend.rebuild(roots.data, roots.configs)
    old_generation = postgres_harness.reader_generation()
    diff(roots)
    result = backend.ingest_diff(
        "change", roots.data, roots.configs, requested_strategy="auto", policy_id=policy.id
    )
    assert result.selected_strategy == selected
    assert (postgres_harness.reader_generation() != old_generation) == (selected == "FULL")
    with postgres_harness.connect() as connection:
        decision = connection.execute(
            "SELECT selected_strategy, reason_code, policy_version "
            "FROM vcp_provenance.maintenance_decisions"
        ).fetchone()
    assert decision == (selected, result.strategy_reason, result.policy_version)
    assert_parity(backend, roots)


@pytest.mark.parametrize("requested", ["incremental", "full", "auto"])
def test_every_strategy_matches_canonical_and_sqlite(postgres_harness, roots, tmp_path, requested):
    first, _, _ = det_with_runs(roots, tmp_path, n=24)
    samples = [sample.model_copy(deep=True) for sample in first.samples]
    for sample in samples:
        sample.meta["opaque"] = 1
    dataset(roots, "second", samples)
    changed = [sample.model_copy(deep=True) for sample in samples]
    changed[0] = changed[0].model_copy(update={"group": "changed"})
    dataset(roots, "third", changed)
    dataset(roots, "same-third", changed)
    dataset(roots, "branch", first.samples)
    clone = postgres_harness.fresh_clone()
    backend = clone.backend
    backend.rebuild(roots.data, roots.configs)
    sqlite = ProvenanceIndex(tmp_path / "reference.sqlite3")
    sqlite.rebuild(roots.data, roots.configs)
    for ident, source, target in (
        ("hop-1", "tiny", "second"),
        ("hop-2", "second", "third"),
        ("zero-tail", "third", "same-third"),
        ("fork", "tiny", "branch"),
    ):
        diff(roots, ident, source, target)
        result = backend.ingest_diff(ident, roots.data, roots.configs, requested_strategy=requested)
        assert result.inserted
        if ident in {"hop-1", "hop-2"}:
            assert result.selected_strategy == (
                "INCREMENTAL" if requested == "incremental" else "FULL"
            )
        sqlite.ingest_diff(ident, roots.data, roots.configs)
        assert backend.normalized() == sqlite.normalized()
        assert_parity(backend, roots)
        if ident == "hop-1":
            head = next(
                key for key in backend.load_graph().entities if key.startswith("dataset:second@")
            )
            assert any(
                record.status.value == "REVIEW" for record in backend.statuses(head).values()
            )
    before = backend.normalized()
    backend.rebuild(roots.data, roots.configs)
    assert backend.normalized() == before
    assert_parity(backend, roots)


@pytest.mark.parametrize("requested", ["incremental", "full", "auto"])
def test_late_zero_event_edge_propagates_existing_semantics(
    postgres_harness, roots, tmp_path, requested
):
    first, _, _ = det_with_runs(roots, tmp_path, n=24)
    dataset(roots, "middle", first.samples)
    changed = [sample.model_copy(update={"group": "changed"}) for sample in first.samples]
    dataset(roots, "tail", changed)
    diff(roots, "downstream", "middle", "tail")
    backend = postgres_harness.backend
    backend.rebuild(roots.data, roots.configs)
    diff(roots, "late", "tiny", "middle")
    result = backend.ingest_diff("late", roots.data, roots.configs, requested_strategy=requested)
    assert result.changed_samples == 0 and result.dirty_entities > 0
    assert result.selected_strategy == ("INCREMENTAL" if requested == "incremental" else "FULL")
    assert_parity(backend, roots)
    tail = next(key for key in backend.load_graph().entities if key.startswith("dataset:tail@"))
    assert any(record.status.value == "STALE" for record in backend.statuses(tail).values())


@pytest.mark.parametrize("requested", ["incremental", "full", "auto"])
def test_zero_event_is_topology_only_and_duplicate_is_no_op(postgres_harness, roots, requested):
    versions(roots, zero=True)
    backend = postgres_harness.backend
    backend.rebuild(roots.data, roots.configs)
    before_graph = backend.load_graph().normalized()
    diff(roots)
    result = backend.ingest_diff("change", roots.data, roots.configs, requested_strategy=requested)
    assert result.inserted and result.selected_strategy == "NO_OP"
    assert result.changed_samples == result.dirty_entities == 0
    assert result.strategy_reason == "verified_zero_semantic_changes"
    assert backend.load_graph().normalized() != before_graph
    assert_parity(backend, roots)
    before = postgres_harness.snapshot()
    again = backend.ingest_diff("change", roots.data, roots.configs, requested_strategy=requested)
    assert not again.inserted and again.selected_strategy == "NO_OP"
    assert again.strategy_reason == "duplicate_artifact_no_op"
    after = postgres_harness.snapshot()
    assert len(after.pop("maintenance_decisions")) == len(before.pop("maintenance_decisions")) + 1
    assert after == before


@pytest.mark.parametrize("requested", ["incremental", "full", "auto"])
@pytest.mark.parametrize(
    "table", ["sample_changes", "entity_status", "maintenance_decisions", "ingest_checkpoints"]
)
def test_injected_rollback_preserves_every_table(
    postgres_harness, roots, checkpoint_log, requested, table
):
    versions(roots)
    backend = postgres_harness.backend
    backend.rebuild(roots.data, roots.configs)
    diff(roots)
    before = postgres_harness.snapshot()
    assert before["ingest_checkpoints"], "rollback test requires a persisted checkpoint row"
    checkpoint = json.loads(before["ingest_checkpoints"][0][0])
    assert checkpoint["consumed_bytes"] == checkpoint_log.stat().st_size > 0
    with checkpoint_log.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps({"event_id": "b" * 64}) + "\n")
    with postgres_harness.fail_after(table) as fired:
        with pytest.raises(RuntimeError, match="injected rollback"):
            backend.ingest_diff("change", roots.data, roots.configs, requested_strategy=requested)
    assert fired.is_set()
    assert postgres_harness.snapshot() == before
    backend.ingest_diff("change", roots.data, roots.configs, requested_strategy=requested)
    checkpoint = json.loads(postgres_harness.snapshot()["ingest_checkpoints"][0][0])
    assert checkpoint["consumed_bytes"] == checkpoint_log.stat().st_size
    assert checkpoint["prefix_sha256"] == sha256_file(checkpoint_log)
    assert_parity(backend, roots)


@pytest.mark.parametrize("damage", ["missing", "manifest", "payload", "input"])
def test_missing_mutated_evidence_fails_and_restored_evidence_rebuilds(
    postgres_harness, roots, damage
):
    versions(roots)
    backend = postgres_harness.backend
    backend.rebuild(roots.data, roots.configs)
    diff(roots)
    backend.ingest_diff("change", roots.data, roots.configs)
    before = postgres_harness.snapshot()
    if damage == "input":
        path = roots.data / "datasets/old/samples.jsonl"
    else:
        filename = "changes.jsonl" if damage == "payload" else "manifest.json"
        path = roots.data / "artifacts/dataset_diff/change" / filename
    original = path.read_bytes()
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(original + b"\n")
    with pytest.raises(IntegrityError):
        backend.ingest_diff("change", roots.data, roots.configs)
    assert postgres_harness.snapshot() == before
    path.write_bytes(original)
    backend.rebuild(roots.data, roots.configs)
    assert_parity(backend, roots)


def test_conflicting_immutable_row_fails_without_partial_writes(postgres_harness, roots):
    versions(roots)
    backend = postgres_harness.backend
    backend.rebuild(roots.data, roots.configs)
    diff(roots)
    backend.ingest_diff("change", roots.data, roots.configs)
    generation = postgres_harness.reader_generation()
    before = postgres_harness.snapshot()
    with postgres_harness.connect() as connection:
        row = list(
            connection.execute("SELECT * FROM vcp_provenance.sample_changes LIMIT 1").fetchone()
        )
        row[9] = "conflicting-sample"
        with pytest.raises(IntegrityError, match="conflict"):
            with connection.transaction():
                assert row[0] == generation
                postgres._insert_immutable(connection, "sample_changes", row)
    assert postgres_harness.snapshot() == before


def test_cycle_rejection_preserves_generation(postgres_harness, roots):
    versions(roots)
    backend = postgres_harness.backend
    backend.rebuild(roots.data, roots.configs)
    diff(roots)
    backend.ingest_diff("change", roots.data, roots.configs)
    diff(roots, "reverse", "new", "old")
    before = postgres_harness.snapshot()
    with pytest.raises(IntegrityError, match="dataset_cycle"):
        backend.ingest_diff("reverse", roots.data, roots.configs)
    assert postgres_harness.snapshot() == before


@pytest.mark.parametrize("damage", ["delete", "index_drift", "ledger_drift"])
def test_full_rebuild_recovers_disposable_index(postgres_harness, roots, damage):
    versions(roots)
    diff(roots)
    ledger = roots.configs / "datasets/old/events.jsonl"
    ledger.write_text('{"event":"old"}\n', encoding="utf-8", newline="\n")
    backend = postgres_harness.backend
    backend.rebuild(roots.data, roots.configs)
    expected = backend.normalized()
    with postgres_harness.connect() as connection:
        if damage == "delete":
            connection.execute("DROP SCHEMA vcp_provenance CASCADE")
        elif damage == "index_drift":
            connection.execute("UPDATE vcp_provenance.entities SET key_value='drift'")
            assert not backend.verify(roots.data, roots.configs).ok
        else:
            ledger.write_text('{"event":"new"}\n', encoding="utf-8", newline="\n")
            assert not backend.verify(roots.data, roots.configs).ok
            with pytest.raises(IntegrityError, match="prefix_drift"):
                backend.ingest_diff("change", roots.data, roots.configs)
    backend.rebuild(roots.data, roots.configs)
    assert backend.normalized() == expected
    assert_parity(backend, roots)


def test_reader_sees_old_generation_until_rebuild_commit(postgres_harness, roots):
    versions(roots)
    backend = postgres_harness.backend
    backend.rebuild(roots.data, roots.configs)
    old = postgres_harness.reader_generation()
    old_graph = backend.normalized()
    with postgres_harness.connect() as reader, reader.transaction():
        reader.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        assert postgres_harness.reader_generation(reader) == old
        rows = reader.execute("SELECT * FROM vcp_provenance.entities ORDER BY entity_id").fetchall()
        diff(roots)
        with postgres_harness.paused_before_publish(roots):
            assert postgres_harness.reader_generation() == old
            assert backend.normalized() == old_graph
        assert postgres_harness.reader_generation() != old
        assert postgres_harness.reader_generation(reader) == old
        assert (
            reader.execute("SELECT * FROM vcp_provenance.entities ORDER BY entity_id").fetchall()
            == rows
        )
    assert_parity(backend, roots)


def test_advisory_lock_serializes_concurrent_duplicate_writers(postgres_harness, roots):
    versions(roots)
    backend = postgres_harness.backend
    backend.rebuild(roots.data, roots.configs)
    diff(roots)
    attempted = Event()
    pids = set()

    def observe(query, stage, connection):
        if "pg_advisory_xact_lock" in query and stage == "before":
            pids.add(connection.info.backend_pid)
            if len(pids) == 2:
                attempted.set()

    postgres_harness.hook = observe
    with ThreadPoolExecutor(max_workers=2) as executor:
        with postgres_harness.connect() as blocker, blocker.transaction():
            blocker.execute("SELECT pg_advisory_xact_lock(%s)", (postgres._ADVISORY_LOCK_KEY,))
            jobs = [
                executor.submit(backend.ingest_diff, "change", roots.data, roots.configs)
                for _ in range(2)
            ]
            assert attempted.wait(10)
            with postgres_harness.connect() as monitor:
                deadline = monotonic() + 10
                while True:
                    waiting = monitor.execute(
                        "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                        "AND NOT granted AND pid=ANY(%s)",
                        (list(pids),),
                    ).fetchone()[0]
                    if waiting == 2 or monotonic() >= deadline:
                        break
                    Event().wait(0.02)
                assert waiting == 2
            assert not any(job.done() for job in jobs)
        results = [job.result(timeout=20) for job in jobs]
    postgres_harness.hook = None
    assert sorted(result.inserted for result in results) == [False, True]
    assert_parity(backend, roots)


@pytest.mark.parametrize(
    "statement,sqlstate",
    [
        ("UPDATE vcp_provenance.generations SET schema_version=2", "23514"),
        ("UPDATE vcp_provenance.generations SET graph_hash='invalid'", "23514"),
        ("UPDATE vcp_provenance.entity_status SET status='UNKNOWN'", "23514"),
        ("UPDATE vcp_provenance.sample_changes SET semantic_effects=ARRAY['INVALID']", "23514"),
        ("UPDATE vcp_provenance.sample_changes SET before_row_hash=NULL", "23514"),
        ("UPDATE vcp_provenance.ingest_checkpoints SET consumed_bytes=-1", "23514"),
        ("UPDATE vcp_provenance.provenance_edges SET target_id='absent'", "23503"),
        ("INSERT INTO vcp_provenance.entities SELECT * FROM vcp_provenance.entities", "23505"),
    ],
)
@pytest.mark.parametrize("schema_state", ["fresh", "existing"])
def test_real_schema_constraints_rollback(
    postgres_harness, roots, checkpoint_log, statement, sqlstate, schema_state
):
    versions(roots)
    diff(roots)
    postgres_harness.backend.rebuild(roots.data, roots.configs)
    before = postgres_harness.snapshot()
    assert before["ingest_checkpoints"], "constraint test must update an existing checkpoint row"
    checkpoint = json.loads(before["ingest_checkpoints"][0][0])
    assert checkpoint["consumed_bytes"] == checkpoint_log.stat().st_size > 0
    with postgres_harness.connect() as connection:
        if schema_state == "existing":
            postgres.install_schema(connection)
        with pytest.raises(postgres_harness.factory.driver.Error) as caught:
            with connection.transaction():
                connection.execute(statement)
        assert caught.value.sqlstate == sqlstate
    assert postgres_harness.snapshot() == before


def test_real_backend_fixture(postgres_backend, roots):
    versions(roots)
    postgres_backend.rebuild(roots.data, roots.configs)
    assert isinstance(postgres_backend, postgres.PostgresProvenanceBackend)
    assert_parity(postgres_backend, roots)
