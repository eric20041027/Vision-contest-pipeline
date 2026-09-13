from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from helpers import det_samples
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.provenance import postgres
from vcp.provenance.backend import RequestedStrategy
from vcp.provenance.diff import DatasetDiffSpec, create_dataset_diff
from vcp.provenance.graph import ProvenanceGraph
from vcp.provenance.schema import ProvenanceEntity
from vcp.provenance.strategy import (
    FULL_FEATURE_ORDER,
    INCREMENTAL_FEATURE_ORDER,
    AdaptivePolicy,
    CostModel,
    MaintenanceFeatures,
    select_strategy,
    write_policy_artifact,
)

from . import test_postgres_incremental as incremental_tests
from .test_postgres_incremental import (
    _assert_parity,
    _dataset,
    _diff,
    _versions,
)

fake_postgres = incremental_tests.fake_postgres


def _policy(roots, connection, selected):
    calibration = roots.data / "calibration.json"
    calibration.write_text(
        json.dumps({"scenario_ids": ["adaptive-test"], "scenario_hashes": ["a" * 64]}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    major, fingerprint = postgres.maintenance_environment(connection)
    policy = AdaptivePolicy(
        backend_schema_version=1,
        postgresql_major=major,
        benchmark_schema_version=1,
        environment_fingerprint=fingerprint,
        calibration_sha256=sha256_file(calibration),
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
        training_row_count=10,
    )
    write_policy_artifact(roots.data, policy, calibration)
    return policy


def _decision(connection):
    rows = connection.snapshot()["maintenance_decisions"]
    assert len(rows) == 1
    return dict(zip(connection.tables["maintenance_decisions"][0].split(), rows[0], strict=True))


@pytest.mark.parametrize("selected", ["NO_OP", "INCREMENTAL", "FULL"])
def test_selected_path_and_complete_decision_commit_together(fake_postgres, roots, selected):
    _versions(roots)
    if selected == "NO_OP":
        _dataset(roots, "idx-new", det_samples(4, seed=21))
    policy = _policy(roots, fake_postgres, selected)
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    old_generation = fake_postgres.snapshot()["active_generation"][0][1]
    _diff(roots)
    result = fake_postgres.backend.ingest_diff(
        "idx-diff",
        roots.data,
        roots.configs,
        requested_strategy="auto",
        policy_id=policy.id,
    )
    assert result.selected_strategy == selected
    assert result.inserted and result.backend == "postgresql"
    assert result.elapsed_ms >= 0
    row = _decision(fake_postgres)
    features = MaintenanceFeatures(**{key: row[key] for key in MaintenanceFeatures.model_fields})
    recomputed = select_strategy("auto", features, policy)
    for key, value in recomputed.model_dump(mode="json").items():
        assert row["reason_code" if key == "reason" else key] == value
    for key, value in asdict(result).items():
        if key not in {"backend", "inserted", "graph_hash", "strategy_reason"}:
            assert row[key] == value
    assert row["reason_code"] == result.strategy_reason
    generation = next(
        g for g in fake_postgres.snapshot()["generations"] if g[0] == row["generation_id"]
    )
    assert generation[4] == result.graph_hash
    assert (row["generation_id"] != old_generation) == (selected == "FULL")
    assert result.dirty_ratio == result.dirty_entities / result.total_entities
    indexed = fake_postgres.backend.load_graph()
    assert row["total_entities"] == len(indexed.entities)
    assert row["total_edges"] == len(indexed.edges)
    assert row["head_count"] == len(postgres.dataset_heads(indexed))
    assert row["historical_changes"] == len(indexed.changes) - result.changed_samples
    _assert_parity(fake_postgres, roots)


@pytest.mark.parametrize("requested", ["incremental", "full", "auto"])
def test_fixed_requests_and_absent_policy(fake_postgres, roots, requested):
    _versions(roots)
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    _diff(roots)
    result = fake_postgres.backend.ingest_diff(
        "idx-diff",
        roots.data,
        roots.configs,
        requested_strategy=requested,
    )
    assert result.selected_strategy == ("INCREMENTAL" if requested == "incremental" else "FULL")
    assert result.strategy_reason == (
        "fallback_policy_absent_full" if requested == "auto" else f"requested_{requested}"
    )
    _assert_parity(fake_postgres, roots)


@pytest.mark.parametrize("requested", list(RequestedStrategy))
def test_duplicate_returns_complete_no_op(fake_postgres, roots, requested):
    _versions(roots)
    _diff(roots)
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    before = fake_postgres.snapshot()
    result = fake_postgres.backend.ingest_diff(
        "idx-diff",
        roots.data,
        roots.configs,
        requested_strategy=requested,
    )
    assert not result.inserted and result.selected_strategy == "NO_OP"
    assert result.strategy_reason == "duplicate_artifact_no_op"
    assert result.dirty_entities == 0
    after = fake_postgres.snapshot()
    after.pop("maintenance_decisions")
    before.pop("maintenance_decisions")
    assert after == before


@pytest.mark.parametrize("failure", ["missing", "mutated", "incompatible", "request"])
def test_invalid_request_or_policy_fails_before_first_write(fake_postgres, roots, failure):
    _versions(roots)
    policy = _policy(roots, fake_postgres, "INCREMENTAL")
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    _diff(roots)
    policy_id = policy.id
    if failure == "missing":
        policy_id = "missing"
    elif failure == "mutated":
        path = roots.data / "artifacts" / "provenance_policy" / policy.id / "policy.json"
        path.write_text("{}\n", encoding="utf-8", newline="\n")
    elif failure == "incompatible":
        fake_postgres.info.server_version = 160000
    fake_postgres.statements.clear()
    before = fake_postgres.snapshot()
    with pytest.raises((ValidationFailed, IntegrityError)):
        fake_postgres.backend.ingest_diff(
            "idx-diff",
            roots.data,
            roots.configs,
            requested_strategy="invalid" if failure == "request" else "auto",
            policy_id=policy_id,
        )
    assert fake_postgres.snapshot() == before
    assert not any(
        q.startswith(("INSERT", "UPDATE", "DELETE")) for q, _ in fake_postgres.statements
    )


@pytest.mark.parametrize("requested", ["incremental", "full", "auto"])
@pytest.mark.parametrize("zero", [False, True])
def test_failure_after_decision_rolls_back_every_table(
    fake_postgres, roots, monkeypatch, requested, zero
):
    _versions(roots)
    if zero:
        _dataset(roots, "idx-new", det_samples(4, seed=21))
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    _diff(roots)
    before = fake_postgres.snapshot()
    original = fake_postgres.execute

    def fail_after_decision(query, params=()):
        result = original(query, params)
        if "INSERT INTO vcp_provenance.maintenance_decisions" in query:
            raise RuntimeError("after decision write")
        return result

    monkeypatch.setattr(fake_postgres, "execute", fail_after_decision)
    with pytest.raises(RuntimeError, match="after decision write"):
        fake_postgres.backend.ingest_diff(
            "idx-diff",
            roots.data,
            roots.configs,
            requested_strategy=requested,
        )
    assert fake_postgres.snapshot() == before
    assert fake_postgres.events[-1] == "rollback"


@pytest.mark.parametrize("selected", ["INCREMENTAL", "FULL"])
def test_features_and_selection_precede_first_write_and_execute_one_path(
    fake_postgres,
    roots,
    monkeypatch,
    selected,
):
    _versions(roots)
    policy = _policy(roots, fake_postgres, selected)
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    _diff(roots)
    fake_postgres.statements.clear()
    original_selector = postgres.select_strategy
    selector_calls = []

    def select(requested, features, policy):
        assert not any(
            q.startswith(("INSERT", "UPDATE", "DELETE")) for q, _ in fake_postgres.statements
        )
        assert features.total_entities > 0 and features.dirty_entities > 0
        assert features.historical_changes == 0
        selector_calls.append(features)
        return original_selector(requested, features, policy)

    def forbidden(*args, **kwargs):
        pytest.fail("executed an unselected maintenance path")

    monkeypatch.setattr(postgres, "select_strategy", select)
    if selected == "FULL":
        monkeypatch.setattr(postgres, "_insert_delta", forbidden)
        monkeypatch.setattr(postgres, "compute_statuses_for_entities", forbidden)
    else:
        monkeypatch.setattr(postgres, "build_graph", forbidden)
        monkeypatch.setattr(postgres, "compute_statuses", forbidden)
    result = fake_postgres.backend.ingest_diff(
        "idx-diff",
        roots.data,
        roots.configs,
        requested_strategy="auto",
        policy_id=policy.id,
    )
    assert result.selected_strategy == selected
    assert len(selector_calls) == 1
    assert len(fake_postgres.snapshot()["maintenance_decisions"]) == 1


def test_environment_fingerprint_uses_only_allowlisted_non_secret_information(monkeypatch):
    class Info:
        server_version = 170011

        def __getattr__(self, name):
            pytest.fail(f"forbidden ConnectionInfo access: {name}")

    def forbidden(*args, **kwargs):
        pytest.fail("forbidden host lookup")

    monkeypatch.setattr(postgres.platform, "node", forbidden)
    connection = SimpleNamespace(info=Info())
    first = postgres.maintenance_environment(connection)
    for key in ("PGHOST", "PGUSER", "PGDATABASE", "PGPASSWORD", "PGSERVICE"):
        monkeypatch.setenv(key, f"{key}_SECRET_MARKER")
    second = postgres.maintenance_environment(connection)
    assert first == second
    assert first[0] == 17 and len(first[1]) == 64
    assert "SECRET_MARKER" not in repr(first)
    assert (
        postgres.maintenance_environment(
            SimpleNamespace(info=SimpleNamespace(server_version=160001))
        )
        != first
    )


@pytest.mark.parametrize("version", [None, "170011", 90600, True])
def test_invalid_live_version_fails_closed(version):
    with pytest.raises(ValidationFailed, match="incompatible_policy"):
        postgres.maintenance_environment(
            SimpleNamespace(info=SimpleNamespace(server_version=version))
        )


@pytest.mark.parametrize("requested", ["incremental", "full", "auto"])
def test_verified_zero_changes_skip_semantic_recompute(
    fake_postgres, roots, monkeypatch, requested
):
    samples = det_samples(4, seed=21)
    _dataset(roots, "idx-old", samples)
    _dataset(roots, "idx-new", samples)
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    _diff(roots)
    compute = postgres.compute_statuses_for_entities

    def topology_only(graph, *args, **kwargs):
        assert not graph.changes
        assert not graph.transitions
        return compute(graph, *args, **kwargs)

    monkeypatch.setattr(postgres, "compute_statuses_for_entities", topology_only)
    result = fake_postgres.backend.ingest_diff(
        "idx-diff",
        roots.data,
        roots.configs,
        requested_strategy=requested,
    )
    assert result.selected_strategy == "NO_OP"
    assert result.changed_samples == result.dirty_entities == 0
    _assert_parity(fake_postgres, roots)


@pytest.mark.parametrize("requested", ["incremental", "full"])
def test_policy_mutation_during_maintenance_rolls_back(
    fake_postgres, roots, monkeypatch, requested
):
    _versions(roots)
    policy = _policy(roots, fake_postgres, "INCREMENTAL")
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    _diff(roots)
    before = fake_postgres.snapshot()
    original = postgres._record_decision

    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        path = roots.data / "artifacts" / "provenance_policy" / policy.id / "policy.json"
        path.write_text("{}\n", encoding="utf-8", newline="\n")
        return result

    monkeypatch.setattr(postgres, "_record_decision", mutate)
    with pytest.raises(IntegrityError):
        fake_postgres.backend.ingest_diff(
            "idx-diff",
            roots.data,
            roots.configs,
            requested_strategy=requested,
            policy_id=policy.id,
        )
    assert fake_postgres.snapshot() == before


@pytest.mark.parametrize("selected", ["INCREMENTAL", "FULL"])
def test_auto_late_zero_event_link_counts_history_and_preserves_bounded_reads(
    fake_postgres,
    roots,
    selected,
):
    samples = det_samples(4, seed=21)
    changed = [sample.model_copy(update={"group": "changed"}) for sample in samples]
    unrelated = det_samples(3, seed=91)
    for name, rows in (
        ("source", samples),
        ("middle", samples),
        ("tail", changed),
        ("outside-old", unrelated),
        ("outside-new", [sample.model_copy(update={"group": "outside"}) for sample in unrelated]),
    ):
        _dataset(roots, name, rows)
    policy = _policy(roots, fake_postgres, selected)

    def diff(source, target, ident):
        return create_dataset_diff(
            DatasetDiffSpec(
                from_dataset=source,
                to_dataset=target,
                artifact_id=ident,
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )

    first = diff("middle", "tail", "downstream")
    outside = diff("outside-old", "outside-new", "outside")
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    diff("source", "middle", "late")
    result = fake_postgres.backend.ingest_diff(
        "late",
        roots.data,
        roots.configs,
        requested_strategy="auto",
        policy_id=policy.id,
    )
    assert result.selected_strategy == selected
    assert result.changed_samples == 0 and result.dirty_entities > 0
    decision = _decision(fake_postgres)
    assert decision["historical_changes"] == (
        first.summary.total_changes + outside.summary.total_changes
    )
    if selected == "INCREMENTAL":
        assert len(fake_postgres.history_reads) == 1
        assert {(row[5], row[7]) for row in fake_postgres.history_reads[0]} == {("middle", "tail")}
    else:
        assert not fake_postgres.history_reads
    _assert_parity(fake_postgres, roots)


@pytest.mark.parametrize("semantic", [True, False])
def test_dirty_feature_includes_reading_run_fusion_predecessors_and_descendants(
    monkeypatch,
    semantic,
):
    graph = ProvenanceGraph()
    for ident, kind, attributes in (
        ("reading:a", "reading", {"run_id": "fusion"}),
        ("run:fusion", "fusion_run", {}),
        ("run:member", "run", {}),
        ("artifact:downstream", "artifact", {}),
        ("artifact:unrelated", "artifact", {}),
    ):
        graph.add_entity(
            ProvenanceEntity(
                entity_id=ident,
                entity_type=kind,
                key=ident,
                attributes=attributes,
            )
        )
    graph.add_edge("run:member", "run:fusion", "MEMBER_OF")
    graph.add_edge("run:fusion", "reading:a", "MEASURED_BY")
    graph.add_edge("run:member", "artifact:downstream", "USED_BY")
    monkeypatch.setattr(postgres, "_dirty_closure", lambda *args, **kwargs: {"reading:a"})
    planned = postgres._planned_dirty(
        None,
        "g",
        graph,
        "source",
        set(),
        semantic=semantic,
        join_readings=set() if semantic else {"reading:a"},
    )
    assert planned == {"reading:a", "run:fusion", "run:member", "artifact:downstream"}
