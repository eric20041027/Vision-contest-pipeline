from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from helpers import dataset_with_perfect_run, det_samples, det_with_runs, make_card
from vcp.core.errors import IntegrityError
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.source_audit import write_source_audit
from vcp.provenance import postgres
from vcp.provenance.backend import BackendConfig, BackendName
from vcp.provenance.diff import DatasetDiffSpec, create_dataset_diff
from vcp.provenance.graph import build_graph
from vcp.provenance.index import ProvenanceIndex, graph_hash
from vcp.provenance.views import compute_statuses


def _dataset(roots, name, samples):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    dataset = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    dataset.save(paths)
    write_source_audit(paths, dataset.card, data_root=roots.data)
    return dataset


def _versions(roots):
    samples = det_samples(4, seed=21)
    _dataset(roots, "idx-old", samples)
    updated = [sample.model_copy(deep=True) for sample in samples]
    updated[0] = updated[0].model_copy(update={"group": "new"})
    _dataset(roots, "idx-new", updated)


def _diff(roots):
    return create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="idx-old",
            to_dataset="idx-new",
            artifact_id="idx-diff",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )


class FakePostgres:
    """Transactional relational double; SQLite executes the recursive SQL itself.

    PostgreSQL DDL/driver semantics are covered separately. Only parameter syntax,
    JSON casts and the array seed are adapted here; no ingest behavior is mocked.
    """

    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.execute("ATTACH DATABASE ':memory:' AS vcp_provenance")
        self.events = []
        self.statements = []
        self.history_reads = []
        self.fail_at = None
        self.tables = {
            "generations": (
                "generation_id state schema_version built_at graph_hash "
                "graph_record_count graph_record_sum canonical_snapshot_hash",
                1,
            ),
            "active_generation": ("singleton generation_id", 1),
            "metadata": ("generation_id key value", 2),
            "entities": (
                "generation_id entity_id entity_type key_value dataset_version_id "
                "attributes broken_reason",
                2,
            ),
            "provenance_edges": (
                "generation_id edge_id source_id target_id edge_type attributes",
                2,
            ),
            "sample_changes": (
                "generation_id change_id schema_version source_id target_id "
                "from_dataset from_samples_hash to_dataset to_samples_hash "
                "sample_id change_type changed_domains changed_fields "
                "semantic_effects before_row_hash after_row_hash",
                2,
            ),
            "dataset_edges": ("generation_id source_id target_id artifact_id", 3),
            "dataset_edge_changes": ("generation_id source_id target_id change_id", 4),
            "entity_status": ("generation_id head_id entity_id status reason predecessor_id", 3),
            "ingest_checkpoints": (
                "generation_id source_path consumed_bytes prefix_sha256 last_event_id",
                2,
            ),
            "ingested_artifacts": ("generation_id artifact_id manifest_sha256", 2),
            "maintenance_decisions": (
                "generation_id decision_id artifact_id requested_strategy selected_strategy "
                "reason_code changed_samples dirty_entities total_entities dirty_ratio "
                "total_edges historical_changes head_count estimated_incremental_ms "
                "estimated_full_ms policy_version elapsed_ms decided_at",
                2,
            ),
        }
        for table, (fields, keys) in self.tables.items():
            columns = fields.split()
            self.db.execute(
                f"CREATE TABLE vcp_provenance.{table} ("
                + ",".join(columns)
                + ",PRIMARY KEY ("
                + ",".join(columns[:keys])
                + "))"
            )

    @contextmanager
    def transaction(self):
        # Schema validation may use a nested driver transaction (savepoint).
        if self.db.in_transaction:
            raise AssertionError("writer opened a nested transaction")
        self.events.append("begin")
        self.db.execute("BEGIN")
        try:
            yield
            if self.fail_at == "commit":
                raise RuntimeError("injected interruption before commit")
            self.db.commit()
            self.events.append("commit")
        except BaseException:
            self.db.rollback()
            self.events.append("rollback")
            raise

    def close(self):
        pass

    @contextmanager
    def cursor(self):
        yield self

    def executemany(self, query, rows):
        for row in rows:
            self.execute(query, row)

    def execute(self, query, params=()):
        compact = " ".join(query.split())
        self.statements.append((compact, params))
        if "pg_advisory_xact_lock" in compact:
            self.events.append("lock")
            return self.db.execute("SELECT NULL")
        if "obj_description" in compact:
            return self.db.execute("SELECT 'vcp_provenance_schema_version=1'")
        if compact.startswith("SET TRANSACTION"):
            return self.db.execute("SELECT NULL")
        if compact.startswith("SELECT pg_database_size"):
            return self.db.execute("SELECT 4096")
        if self.fail_at == "after_status" and compact.startswith(
            "INSERT INTO vcp_provenance.ingested_artifacts"
        ):
            raise RuntimeError("injected interruption after status")
        translated = query.replace("%s", "?").replace("::jsonb", "")
        translated = translated.replace("::text[]", "").replace("::text", "")
        translated = translated.replace("SELECT unnest(?)", "SELECT value FROM json_each(?)")
        parameters = tuple(
            json.dumps(item)
            if isinstance(item, (list, dict))
            else item
            if item is None or isinstance(item, (str, int, float))
            else str(item)
            for item in params
        )
        cursor = self.db.execute(translated, parameters)
        # Restore PostgreSQL native array values in sample-change reads.
        if compact.startswith(("SELECT", "WITH RECURSIVE")) and "semantic_effects" in compact:
            rows = [
                tuple(json.loads(v) if i in (11, 12, 13) else v for i, v in enumerate(row))
                for row in cursor.fetchall()
            ]
            if compact.startswith("WITH RECURSIVE"):
                self.history_reads.append(rows)
            return SimpleNamespace(
                fetchone=lambda: rows[0] if rows else None, fetchall=lambda: rows
            )
        return cursor

    def snapshot(self):
        return {
            table: self.db.execute(f"SELECT * FROM vcp_provenance.{table}").fetchall()
            for table in self.tables
        }


@pytest.fixture
def fake_postgres(monkeypatch):
    connection = FakePostgres()
    monkeypatch.setattr(postgres, "_load_psycopg", lambda: SimpleNamespace())
    monkeypatch.setattr(postgres, "_connect", lambda _config: connection)
    monkeypatch.setattr(postgres, "install_schema", lambda _connection: None)
    monkeypatch.setattr(postgres, "validate_schema", lambda _connection: None)
    connection.backend = postgres.PostgresProvenanceBackend(BackendConfig(BackendName.POSTGRESQL))
    return connection


def _prepare(connection, roots):
    _versions(roots)
    connection.backend.rebuild(roots.data, roots.configs)
    _diff(roots)
    connection.statements.clear()
    connection.events.clear()


def _assert_parity(connection, roots):
    canonical = build_graph(roots.data, roots.configs)
    assert connection.backend.load_graph().normalized() == canonical.normalized()
    assert connection.backend.stats()["graph_hash"] == graph_hash(canonical)
    heads = {
        ident for ident, entity in canonical.entities.items() if entity.entity_type == "dataset"
    }
    for head in heads:
        assert connection.backend.statuses(head) == compute_statuses(canonical, head)
    assert {row["head_id"] for row in connection.backend.normalized()["statuses"]} == heads
    verified = connection.backend.verify(roots.data, roots.configs)
    assert verified.ok, verified.issues


def test_dirty_closure_uses_recursive_forward_and_reverse_indexes():
    sql = postgres.DIRTY_CLOSURE_SQL
    assert "WITH RECURSIVE" in sql
    assert "dataset_edges" in sql
    assert "provenance_edges" in sql
    assert "UNION" in sql
    assert "UNION ALL" not in sql


def test_incremental_exact_parity_and_duplicate_no_op(fake_postgres, roots):
    _prepare(fake_postgres, roots)
    first = fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs)
    assert first.inserted
    assert first.dirty_entities > 0
    assert fake_postgres.events[:2] == ["begin", "lock"]
    ingest_queries = list(fake_postgres.statements)
    before = fake_postgres.snapshot()
    second = fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs)
    assert not second.inserted
    assert second.graph_hash == first.graph_hash
    after = fake_postgres.snapshot()
    assert len(after.pop("maintenance_decisions")) == 2
    before.pop("maintenance_decisions")
    assert after == before
    assert second.selected_strategy == "NO_OP"
    assert second.strategy_reason == "duplicate_artifact_no_op"
    assert not any(
        "FROM vcp_provenance.sample_changes" in q and "change_id=" not in q and "COUNT(*)" not in q
        for q, _ in ingest_queries
    )
    _assert_parity(fake_postgres, roots)


@pytest.mark.parametrize("failure", ["after_status", "commit"])
def test_incremental_failure_rolls_back_all_derived_records(fake_postgres, roots, failure):
    _prepare(fake_postgres, roots)
    before = fake_postgres.snapshot()
    fake_postgres.fail_at = failure
    with pytest.raises(RuntimeError, match="injected interruption"):
        fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs)
    assert fake_postgres.snapshot() == before
    assert fake_postgres.events[-1] == "rollback"


def test_incremental_rechecks_inputs_before_commit(fake_postgres, roots, monkeypatch):
    _prepare(fake_postgres, roots)
    before = fake_postgres.snapshot()
    original = postgres.compute_statuses_for_entities
    changed = False

    def mutate(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            path = roots.data / "datasets" / "idx-old" / "samples.jsonl"
            path.write_bytes(path.read_bytes() + b"\n")
            changed = True
        return result

    monkeypatch.setattr(postgres, "compute_statuses_for_entities", mutate)
    with pytest.raises(IntegrityError, match="changed|drift"):
        fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs)
    assert fake_postgres.snapshot() == before


def test_zero_change_publishes_topology_and_exact_statuses(fake_postgres, roots):
    samples = det_samples(4, seed=21)
    _dataset(roots, "idx-old", samples)
    _dataset(roots, "idx-new", samples)
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    _diff(roots)
    result = fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs)
    assert result.inserted
    assert result.dirty_entities == 0
    assert result.selected_strategy == "NO_OP"
    assert result.strategy_reason == "verified_zero_semantic_changes"
    _assert_parity(fake_postgres, roots)


@pytest.mark.parametrize("file", ["manifest.json", "changes.jsonl"])
def test_conflicting_duplicate_rolls_back(fake_postgres, roots, file):
    _prepare(fake_postgres, roots)
    fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs)
    before = fake_postgres.snapshot()
    path = roots.data / "artifacts" / "dataset_diff" / "idx-diff" / file
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(IntegrityError):
        fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs)
    assert fake_postgres.snapshot() == before


def test_cycle_rejected_without_writes(fake_postgres, roots):
    _prepare(fake_postgres, roots)
    fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs)
    create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="idx-new",
            to_dataset="idx-old",
            artifact_id="reverse",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    before = fake_postgres.snapshot()
    with pytest.raises(IntegrityError, match="dataset_cycle"):
        fake_postgres.backend.ingest_diff("reverse", roots.data, roots.configs)
    assert fake_postgres.snapshot() == before


def test_history_unknown_changes_and_branching_match_sqlite_and_full(
    fake_postgres, roots, tmp_path, monkeypatch
):
    first, _, _ = det_with_runs(roots, tmp_path, n=24)
    samples = [sample.model_copy(deep=True) for sample in first.samples]
    for sample in samples:
        sample.meta["opaque"] = 1
    _dataset(roots, "second", samples)
    samples[0] = samples[0].model_copy(update={"group": "changed"})
    _dataset(roots, "third", samples)
    _dataset(roots, "same-third", samples)
    _dataset(roots, "branch", first.samples)
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    sqlite = ProvenanceIndex(tmp_path / "reference.sqlite3")
    sqlite.rebuild(roots.data, roots.configs)
    for artifact_id, source, target in (
        ("hop-1", "tiny", "second"),
        ("hop-2", "second", "third"),
        ("zero-tail", "third", "same-third"),
        ("fork", "tiny", "branch"),
    ):
        create_dataset_diff(
            DatasetDiffSpec(
                from_dataset=source,
                to_dataset=target,
                artifact_id=artifact_id,
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )
        with monkeypatch.context() as scoped:
            scoped.setattr(postgres, "graph_hash", lambda *_: pytest.fail("full graph hash"))
            scoped.setattr(postgres, "build_graph", lambda *_: pytest.fail("full graph replay"))
            original = postgres._fingerprint

            def only_delta(graph, original=original):
                assert all(entity.key != "perfect" for entity in graph.entities.values())
                return original(graph)

            scoped.setattr(postgres, "_fingerprint", only_delta)
            fake_postgres.backend.ingest_diff(artifact_id, roots.data, roots.configs)
        sqlite.ingest_diff(artifact_id, roots.data, roots.configs)
        assert fake_postgres.backend.normalized() == sqlite.normalized()
        _assert_parity(fake_postgres, roots)
        if artifact_id == "hop-1":
            assert any(
                record.status.value == "REVIEW"
                for record in fake_postgres.backend.statuses(
                    next(
                        ident
                        for ident in fake_postgres.backend.load_graph().entities
                        if ident.startswith("dataset:second@")
                    )
                ).values()
            )
    # Full rebuild must retain exactly the same historical-head materializations.
    before = fake_postgres.backend.normalized()
    fresh = FakePostgres()
    monkeypatch.setattr(postgres, "_connect", lambda _config: fresh)
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    assert fake_postgres.backend.normalized() == before


@pytest.mark.parametrize("pin", ["from_samples", "from_source_audit"])
def test_changed_input_pin_rejected_before_first_write(fake_postgres, roots, pin):
    _prepare(fake_postgres, roots)
    manifest = postgres.store.load_manifest(roots.data, "dataset_diff", "idx-diff")
    reference = next(ref for ref in manifest.spec.inputs if ref.name == pin)
    path = roots.data / reference.path
    path.write_bytes(path.read_bytes() + b"\n")
    before = fake_postgres.snapshot()
    with pytest.raises(IntegrityError, match="input .*changed"):
        fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs)
    assert fake_postgres.snapshot() == before
    assert not any(
        q.startswith(("INSERT", "UPDATE", "DELETE")) for q, _ in fake_postgres.statements
    )


def test_checkpoint_prefix_drift_is_rejected(fake_postgres, roots):
    _versions(roots)
    ledger = roots.configs / "datasets" / "idx-old" / "events.jsonl"
    ledger.write_text('{"event":"a"}\n', encoding="utf-8", newline="\n")
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    _diff(roots)
    ledger.write_text('{"event":"b"}\n', encoding="utf-8", newline="\n")
    before = fake_postgres.snapshot()
    with pytest.raises(IntegrityError, match="prefix_drift"):
        fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs)
    assert fake_postgres.snapshot() == before


def test_recursive_closure_deduplicates_diamond_and_cycle(fake_postgres):
    connection = fake_postgres
    for source, target in (("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")):
        connection.execute(
            "INSERT INTO vcp_provenance.dataset_edges VALUES (%s,%s,%s,%s)",
            ("g", source, target, "artifact"),
        )
    for index, (source, target) in enumerate((("a", "r"), ("r", "x"), ("x", "r"))):
        connection.execute(
            "INSERT INTO vcp_provenance.provenance_edges VALUES (%s,%s,%s,%s,%s,%s)",
            ("g", str(index), source, target, "CONSUMED_BY", "{}"),
        )
    assert postgres._dataset_ancestors(connection, "g", "d") == {"a", "b", "c", "d"}
    assert postgres._dirty_closure(connection, "g", "d", {"delta"}) == {
        "a",
        "b",
        "c",
        "d",
        "r",
        "x",
        "delta",
    }


def test_immutable_insert_exact_duplicate_and_conflict(fake_postgres):
    row = ("g", "run:r", "run", "r", None, '{"x":1}', None)
    assert postgres._insert_immutable(fake_postgres, "entities", row)
    assert not postgres._insert_immutable(fake_postgres, "entities", row)
    with pytest.raises(IntegrityError, match="entities_conflict"):
        postgres._insert_immutable(fake_postgres, "entities", (*row[:-1], "broken"))


def test_fixed_result_and_decision_contract(fake_postgres, roots):
    _prepare(fake_postgres, roots)
    result = fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs)
    assert result.backend == "postgresql"
    assert result.requested_strategy == "incremental"
    assert result.selected_strategy == "INCREMENTAL"
    assert result.strategy_reason == "requested_incremental"
    assert result.changed_samples == 1
    assert result.dirty_ratio == result.dirty_entities / result.total_entities
    assert result.elapsed_ms >= 0
    assert result.estimated_incremental_ms is None
    assert result.estimated_full_ms is None
    decision = fake_postgres.snapshot()["maintenance_decisions"][0]
    assert decision[2:10] == (
        result.artifact_id,
        result.requested_strategy,
        result.selected_strategy,
        result.strategy_reason,
        result.changed_samples,
        result.dirty_entities,
        result.total_entities,
        result.dirty_ratio,
    )


@pytest.mark.parametrize("options", [{"requested_strategy": "full"}, {"policy_id": "p"}])
def test_fixed_ingest_rejects_policy_and_other_strategies(fake_postgres, roots, options):
    from vcp.core.errors import ValidationFailed

    with pytest.raises(ValidationFailed, match="unsupported"):
        fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs, **options)
    assert fake_postgres.events == []


@pytest.mark.parametrize("link_changes", [False, True])
def test_late_zero_change_link_propagates_prior_descendant_changes(
    fake_postgres, roots, tmp_path, link_changes
):
    first, _, _ = det_with_runs(roots, tmp_path, n=24)
    middle = [
        sample.model_copy(update={"meta": {"link": 1}}) if link_changes else sample
        for sample in first.samples
    ]
    _dataset(roots, "middle", middle)
    changed = [sample.model_copy(update={"group": "changed"}) for sample in middle]
    _dataset(roots, "tail", changed)
    create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="middle",
            to_dataset="tail",
            artifact_id="prior-history",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    unrelated = det_samples(8, seed=71)
    _dataset(roots, "unrelated-old", unrelated)
    _dataset(
        roots,
        "unrelated-new",
        [sample.model_copy(update={"meta": {"unrelated": 1}}) for sample in unrelated],
    )
    create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="unrelated-old",
            to_dataset="unrelated-new",
            artifact_id="unrelated-history",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    graph = fake_postgres.backend.load_graph()
    tail = next(ident for ident in graph.entities if ident.startswith("dataset:tail@"))
    source = next(ident for ident in graph.entities if ident.startswith("dataset:tiny@"))
    assert fake_postgres.backend.statuses(source)["run:perfect"].status.value == "VALID"
    assert fake_postgres.backend.statuses(tail)["run:perfect"].status.value == "VALID"
    diff = create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="tiny",
            to_dataset="middle",
            artifact_id="late-link",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    assert diff.summary.total_changes == (24 if link_changes else 0)
    canonical = build_graph(roots.data, roots.configs)
    assert compute_statuses(canonical, tail)["run:perfect"].status.value == "STALE"
    fake_postgres.statements.clear()
    result = fake_postgres.backend.ingest_diff("late-link", roots.data, roots.configs)
    assert result.selected_strategy == "INCREMENTAL"
    assert len(fake_postgres.history_reads) == 1
    assert len(fake_postgres.history_reads[0]) == 24
    assert {(row[5], row[7]) for row in fake_postgres.history_reads[0]} == {("middle", "tail")}
    assert not any(
        query.startswith("SELECT generation_id, change_id")
        for query, _params in fake_postgres.statements
    )
    _assert_parity(fake_postgres, roots)


def test_unrelated_new_evidence_requires_sync_without_writes(fake_postgres, roots):
    _prepare(fake_postgres, roots)
    _dataset(roots, "unindexed", det_samples(2, seed=5))
    before = fake_postgres.snapshot()
    with pytest.raises(IntegrityError, match="unindexed evidence"):
        fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs)
    assert fake_postgres.snapshot() == before
    assert not any(
        query.startswith(("INSERT", "UPDATE", "DELETE"))
        for query, _params in fake_postgres.statements
    )


def test_schema_marker_read_reuses_writer_transaction(fake_postgres, roots):
    _prepare(fake_postgres, roots)
    fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs)
    assert fake_postgres.events == ["begin", "lock", "commit"]
    assert "pg_advisory_xact_lock" in fake_postgres.statements[0][0]
    assert "obj_description" in fake_postgres.statements[1][0]


def test_join_preserves_existing_target_lineage_statuses(fake_postgres, roots, tmp_path):
    first, _, _ = det_with_runs(roots, tmp_path, n=24)
    dataset_with_perfect_run(
        roots,
        tmp_path,
        name="other",
        task="det",
        samples=[sample.model_copy(update={"meta": {"other": 1}}) for sample in first.samples],
        run_id="other-run",
    )
    changed = [sample.model_copy(update={"group": "changed"}) for sample in first.samples]
    _dataset(roots, "target", changed)
    create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="other",
            to_dataset="target",
            artifact_id="other-path",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="tiny",
            to_dataset="target",
            artifact_id="joining",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    fake_postgres.backend.ingest_diff("joining", roots.data, roots.configs)
    _assert_parity(fake_postgres, roots)


@pytest.mark.parametrize("effect", ["group", "unknown"])
def test_reading_reason_matches_replay_after_upstream_run_becomes_stale(
    fake_postgres, roots, tmp_path, effect
):
    from vcp.measure.measure import MeasureSpec, measure_run

    first, plan, _ = det_with_runs(roots, tmp_path, n=24)
    measure_run(
        MeasureSpec(
            run_id="perfect",
            subsets=["valA"],
            metrics=["coco_map"],
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    middle = [sample.model_copy(deep=True) for sample in first.samples]
    for sample in middle:
        if plan.assignment[sample.sample_id] == "valA":
            sample.labels.boxes[0].x += 1
    _dataset(roots, "eval-changed", middle)
    tail = [
        sample.model_copy(
            update={"group": "changed"} if effect == "group" else {"meta": {"opaque": 1}}
        )
        for sample in middle
    ]
    _dataset(roots, "train-changed", tail)
    unrelated = det_samples(8, seed=91)
    _dataset(roots, "unrelated-old", unrelated)
    _dataset(
        roots,
        "unrelated-new",
        [sample.model_copy(update={"group": "other"}) for sample in unrelated],
    )
    create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="unrelated-old",
            to_dataset="unrelated-new",
            artifact_id="unrelated",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    expected_history_rows = 0
    for artifact_id, source, target in (
        ("eval-hop", "tiny", "eval-changed"),
        ("train-hop", "eval-changed", "train-changed"),
    ):
        diff = create_dataset_diff(
            DatasetDiffSpec(
                from_dataset=source,
                to_dataset=target,
                artifact_id=artifact_id,
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )
        expected_history_rows += diff.summary.total_changes
        fake_postgres.backend.ingest_diff(artifact_id, roots.data, roots.configs)
        _assert_parity(fake_postgres, roots)
    assert len(fake_postgres.history_reads) == 1
    assert len(fake_postgres.history_reads[0]) == expected_history_rows
    assert {(row[5], row[7]) for row in fake_postgres.history_reads[0]} == {
        ("tiny", "eval-changed"),
        ("eval-changed", "train-changed"),
    }


def _unrelated_history(roots):
    samples = det_samples(5, seed=77)
    _dataset(roots, "outside-old", samples)
    _dataset(
        roots, "outside-new", [sample.model_copy(update={"group": "outside"}) for sample in samples]
    )
    create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="outside-old",
            to_dataset="outside-new",
            artifact_id="outside-history",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )


def test_fusion_reading_uses_pre_propagation_run_status(fake_postgres, roots, tmp_path):
    from vcp.fuse.build import BuildSpec, build_run
    from vcp.fuse.recipes import save_recipe
    from vcp.fuse.schema import Member, Recipe
    from vcp.measure.measure import MeasureSpec, measure_run

    first, plan, paths = det_with_runs(roots, tmp_path, n=24)
    save_recipe(
        paths,
        Recipe(
            recipe_id="fusion",
            dataset="tiny",
            plan_id="fixed-v1",
            method="wbf",
            params={"iou": "0.5"},
            members=[Member(run="perfect")],
            created_at="2026-09-13T00:00:00.000Z",
        ),
    )
    build_run(
        BuildSpec(
            dataset="tiny",
            recipe_id="fusion",
            subsets=["valA"],
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    reading = measure_run(
        MeasureSpec(
            run_id="fuse-fusion",
            subsets=["valA"],
            metrics=["coco_map"],
            data_root=roots.data,
            configs_root=roots.configs,
        )
    ).readings[0]
    changed = [
        sample.model_copy(update={"meta": {"unknown": 1}})
        if plan.assignment[sample.sample_id] == "valB"
        else sample
        for sample in first.samples
    ]
    _dataset(roots, "fusion-target", changed)
    _unrelated_history(roots)
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    diff = create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="tiny",
            to_dataset="fusion-target",
            artifact_id="member-change",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    canonical = build_graph(roots.data, roots.configs)
    target = next(
        ident for ident in canonical.entities if ident.startswith("dataset:fusion-target@")
    )
    expected = compute_statuses(canonical, target)
    reading_id = f"reading:{reading.reading_id}"
    assert expected["run:perfect"].status.value == "REVIEW"
    assert expected["run:fuse-fusion"].status.value == "REVIEW"
    assert expected[reading_id].status.value == "VALID"
    fake_postgres.backend.ingest_diff("member-change", roots.data, roots.configs)
    assert fake_postgres.backend.statuses(target)[reading_id] == expected[reading_id]
    _assert_parity(fake_postgres, roots)
    assert len(fake_postgres.history_reads) == 1
    assert len(fake_postgres.history_reads[0]) == diff.summary.total_changes
    assert {(row[5], row[7]) for row in fake_postgres.history_reads[0]} == {
        ("tiny", "fusion-target")
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_zero_change_join_recomputes_incompatible_reading_bases(
    fake_postgres, roots, tmp_path, reverse
):
    from vcp.measure.measure import MeasureSpec, measure_run

    first, plan, _ = det_with_runs(roots, tmp_path, n=24)
    reading = measure_run(
        MeasureSpec(
            run_id="perfect",
            subsets=["valA"],
            metrics=["coco_map"],
            data_root=roots.data,
            configs_root=roots.configs,
        )
    ).readings[0]
    label_samples = [sample.model_copy(deep=True) for sample in first.samples]
    for sample in label_samples:
        if plan.assignment[sample.sample_id] == "valA":
            sample.labels.boxes[0].x += 1
    unknown_samples = [
        sample.model_copy(update={"meta": {"unknown": 1}})
        if plan.assignment[sample.sample_id] == "train"
        else sample
        for sample in first.samples
    ]
    for name, samples in (
        ("label-middle", label_samples),
        ("unknown-middle", unknown_samples),
        ("source-final", first.samples),
        ("target-final", first.samples),
    ):
        _dataset(roots, name, samples)
    expected_rows = 0
    for index, (source, target) in enumerate(
        (
            ("tiny", "label-middle"),
            ("label-middle", "source-final"),
            ("tiny", "unknown-middle"),
            ("unknown-middle", "target-final"),
        )
    ):
        diff = create_dataset_diff(
            DatasetDiffSpec(
                from_dataset=source,
                to_dataset=target,
                artifact_id=f"history-{index}",
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )
        expected_rows += diff.summary.total_changes
    _unrelated_history(roots)
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    graph = fake_postgres.backend.load_graph()
    source = next(ident for ident in graph.entities if ident.startswith("dataset:source-final@"))
    target = next(ident for ident in graph.entities if ident.startswith("dataset:target-final@"))
    reading_id = f"reading:{reading.reading_id}"
    assert fake_postgres.backend.statuses(source)[reading_id].status.value == "STALE"
    assert fake_postgres.backend.statuses(target)[reading_id].status.value == "REVIEW"
    source_name, target_name = "source-final", "target-final"
    if reverse:
        source, target = target, source
        source_name, target_name = target_name, source_name
    diff = create_dataset_diff(
        DatasetDiffSpec(
            from_dataset=source_name,
            to_dataset=target_name,
            artifact_id="zero-join",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    assert diff.summary.total_changes == 0
    expected = compute_statuses(build_graph(roots.data, roots.configs), target)[reading_id]
    assert expected.status.value == "REVIEW"
    result = fake_postgres.backend.ingest_diff("zero-join", roots.data, roots.configs)
    assert result.selected_strategy == "NO_OP"
    assert fake_postgres.backend.statuses(target)[reading_id] == expected
    _assert_parity(fake_postgres, roots)
    assert len(fake_postgres.history_reads) == 1
    assert len(fake_postgres.history_reads[0]) == expected_rows
    assert all(not row[5].startswith("outside") for row in fake_postgres.history_reads[0])


@pytest.mark.parametrize("mutation_point", ["status", "checkpoint"])
def test_original_log_checkpoint_cannot_be_replaced_by_rewritten_prefix(
    fake_postgres, roots, monkeypatch, mutation_point
):
    _versions(roots)
    path = roots.data / "logs" / "provenance.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"event":"old"}\n', encoding="utf-8", newline="\n")
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    _diff(roots)
    assert "data/logs/provenance.jsonl" not in postgres._canonical_snapshot(
        roots.data, roots.configs
    )
    before = fake_postgres.snapshot()
    helper = "compute_statuses_for_entities" if mutation_point == "status" else "_checkpoint_rows"
    original = getattr(postgres, helper)

    def mutate(*args, **kwargs):
        path.write_text('{"event":"new"}\n', encoding="utf-8", newline="\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(postgres, helper, mutate)
    with pytest.raises(IntegrityError, match="prefix_drift"):
        fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs)
    assert fake_postgres.snapshot() == before
    assert fake_postgres.events[-1] == "rollback"
