from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from performance.provenance import adaptive_benchmark as bench
from performance.provenance import production_benchmark, real_validation
from performance.provenance.workloads import Scenario, build_scenario, scenario_matrix
from vcp.provenance.graph import build_graph


def test_scenario_matrix_is_complete_deterministic_and_disjoint():
    calibration = scenario_matrix(seeds=(20260913, 20260914))
    heldout = scenario_matrix(seeds=(20261001, 20261002))
    assert len(calibration) == 144
    assert {s.entities for s in calibration} == {1_000, 10_000, 100_000, 1_000_000}
    assert {s.change_ratio for s in calibration} == {
        0,
        0.001,
        0.01,
        0.05,
        0.10,
        0.25,
        0.50,
        0.90,
        1.0,
    }
    assert {s.topology for s in calibration} == {"chain", "branched"}
    assert calibration == scenario_matrix(seeds=(20260913, 20260914))
    assert not {s.scenario_hash for s in calibration} & {s.scenario_hash for s in heldout}
    assert len({s.scenario_id for s in calibration}) == len(calibration)
    assert all(s.repetitions == (7 if s.entities <= 10_000 else 3) for s in calibration)


def test_matrix_releases_each_scenario_fixture_before_building_the_next(tmp_path, monkeypatch):
    from types import SimpleNamespace

    scenarios = [Scenario(40, ratio, "chain", 20260913) for ratio in (0, 0.5)]
    fixture_roots = []
    calls = []
    runtime = object()

    def build(root, scenario):
        assert all(not previous.exists() for previous in fixture_roots)
        root.mkdir(parents=True)
        (root / "fixture.json").write_text("{}\n", encoding="utf-8", newline="\n")
        fixture_roots.append(root)
        return SimpleNamespace(root=root, scenario=scenario)

    def run(workload, method, *, repetitions, pg_runtime, policy_id=None, policy_sha256=None):
        assert (workload.root / "fixture.json").is_file()
        assert repetitions == 2 and pg_runtime is runtime
        row = {"scenario_id": workload.scenario.scenario_id, "method": method}
        calls.append(row)
        return SimpleNamespace(to_dict=lambda: row)

    monkeypatch.setattr(bench, "build_scenario", build)
    monkeypatch.setattr(bench, "run_method", run)
    monkeypatch.setattr(bench, "prepare_policy_workload", lambda workload, *args: workload)
    rows = bench.run_matrix(
        tmp_path,
        scenarios,
        repetitions=2,
        pg_runtime=runtime,
        policy=SimpleNamespace(id="unit-policy"),
        evidence=object(),
        policy_sha256="a" * 64,
    )

    assert (
        rows
        == calls
        == [
            {"scenario_id": scenario.scenario_id, "method": method}
            for scenario in scenarios
            for method in bench.METHODS
        ]
    )
    assert len(fixture_roots) == 2
    assert all(not root.exists() for root in fixture_roots)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("topology", ["chain", "branched"])
def test_workload_is_canonical_exact_size_and_seeded(tmp_path, topology):
    spec = Scenario(entities=40, change_ratio=0.5, topology=topology, seed=20260913)
    first = build_scenario(tmp_path / "first", spec)
    second = build_scenario(tmp_path / "second", spec)
    other = build_scenario(tmp_path / "other", replace(spec, seed=20260914))
    before = build_graph(first.data, first.configs)
    assert len(before.entities) == spec.entities
    assert not before.gaps
    assert not any(e.broken_reason for e in before.entities.values())
    assert first.workload_hash == second.workload_hash
    assert first.workload_hash != other.workload_hash
    assert first.changed_samples == round(first.sample_entities * spec.change_ratio)
    assert first.sample_entities == len(
        {
            e.attributes["sample_id"]
            for e in before.entities.values()
            if e.entity_type == "sample" and e.dataset_version_id == first.source_id
        }
    )
    assert first.expected.normalized() == second.expected.normalized()
    assert len(first.expected.changes) > len(before.changes)


def test_six_exact_methods():
    assert bench.METHODS == (
        "canonical_full",
        "sqlite_full",
        "sqlite_incremental",
        "postgres_full",
        "postgres_incremental",
        "postgres_adaptive",
    )


def test_production_runner_reuses_generator_and_preserves_legacy_result_fields(tmp_path):
    from performance.provenance import workloads

    assert production_benchmark._change is workloads._change
    result = production_benchmark.run_scale(tmp_path, 10)
    assert set(result) == {
        "events",
        "delta_events",
        "repetitions",
        "generation_ms",
        "database_bytes",
        "canonical_metadata_bytes",
        "index_to_canonical_ratio",
        "fixture_counts",
        "full_load_fingerprint_p50_ms",
        "full_load_fingerprint_p95_ms",
        "production_ingest_p50_ms",
        "production_ingest_p95_ms",
        "status_query_p95_ms",
        "incremental_plus_query_p95_ms",
        "speedup",
        "parity_rate",
        "status_counts",
    }


def test_real_scenarios_copy_metadata_and_leave_source_bytes_unchanged(tmp_path, monkeypatch):
    source = build_scenario(tmp_path / "source", Scenario(40, 0.5, "chain", 20260913))
    names = ("synthetic-a", "synthetic-b", "synthetic-source", "synthetic-target")
    monkeypatch.setattr(real_validation, "DATASETS", names)
    monkeypatch.setattr(
        real_validation, "TRANSITIONS", tuple(zip(names[:-1], names[1:], strict=True))
    )
    before = {
        p.relative_to(source.data): p.read_bytes() for p in source.data.rglob("*") if p.is_file()
    }
    fixture = real_validation.build_real_scenario(tmp_path / "real", source.data, source.configs, 2)
    assert fixture.scenario.track == "real"
    assert fixture.changed_samples == source.changed_samples
    assert fixture.sample_entities == source.sample_entities
    assert (
        bench.run_method(fixture, "sqlite_incremental", repetitions=1).to_dict()["parity_rate"] == 1
    )
    after = {
        p.relative_to(source.data): p.read_bytes() for p in source.data.rglob("*") if p.is_file()
    }
    assert before == after
    assert not (fixture.data / "raw").exists()


def test_real_validation_retains_legacy_fields_when_six_methods_not_requested(
    tmp_path, monkeypatch
):
    source = build_scenario(tmp_path / "source", Scenario(40, 0.5, "chain", 20260913))
    names = ("synthetic-a", "synthetic-b", "synthetic-source", "synthetic-target")
    monkeypatch.setattr(real_validation, "DATASETS", names)
    monkeypatch.setattr(
        real_validation, "TRANSITIONS", tuple(zip(names[:-1], names[1:], strict=True))
    )
    result = real_validation.validate(source.data, source.configs)
    assert set(result) == {
        "created_at",
        "mode",
        "source_data_root",
        "source_configs_root",
        "diff_summaries",
        "selected_runs",
        "selected_run_count",
        "graph",
        "graph_parity",
        "status_parity",
        "status_differences",
        "verify_index",
    }
    assert result["graph_parity"] and result["status_parity"]


@pytest.mark.parametrize("ratio", [0, 0.5, 1.0])
@pytest.mark.parametrize("method", ["canonical_full", "sqlite_full", "sqlite_incremental"])
def test_offline_methods_have_exact_parity_metrics_and_fresh_repetitions(tmp_path, ratio, method):
    fixture = build_scenario(tmp_path / "fixture", Scenario(40, ratio, "branched", 20260913))
    roots = []
    original = bench.fresh_backend

    @contextmanager
    def recording(method, data, **kwargs):
        roots.append(data)
        with original(method, data, **kwargs) as backend:
            yield backend

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(bench, "fresh_backend", recording)
        result = bench.run_method(fixture, method, repetitions=2)
    doc = result.to_dict()
    assert doc["status"] == "ok", doc
    assert all(
        doc[key] is True
        for key in (
            "graph_parity",
            "graph_hash_parity",
            "status_parity",
            "head_parity",
        )
    )
    assert doc["parity_rate"] == 1.0
    assert len(roots) == len(set(roots)) == 2
    assert len(doc["samples"]) == 2
    for operation in ("maintenance", "status", "impact", "explain"):
        assert doc[f"{operation}_p50_ms"] >= 0
        assert doc[f"{operation}_p95_ms"] >= doc[f"{operation}_p50_ms"]
    for key in (
        "throughput_samples_per_second",
        "database_bytes",
        "storage_measurement",
        "changed_samples",
        "dirty_entities",
        "dirty_ratio",
        "total_entities",
        "total_edges",
        "historical_changes",
        "head_count",
        "environment",
        "requested_strategy",
        "selected_strategy",
        "strategy_reason",
        "policy_version",
        "estimated_incremental_ms",
        "estimated_full_ms",
        "workload_hash",
    ):
        assert key in doc
    assert doc["changed_samples"] == fixture.changed_samples
    assert doc["total_entities"] == len(fixture.expected.entities)
    assert not (fixture.data / "artifacts/dataset_diff/adaptive-delta").exists()


def test_unconfigured_smoke_fails_before_creating_output(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("VCP_TEST_PG_SERVICE", raising=False)
    output = tmp_path / "result.json"
    assert (
        bench.main(
            [
                "--entities",
                "1000",
                "--ratios",
                "0",
                "0.01",
                "--repetitions",
                "2",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()
    captured = capsys.readouterr()
    assert "PostgreSQL benchmark service is not configured" in captured.err
    assert "Traceback" not in captured.err


def test_six_method_matrix_fails_closed_without_frozen_policy(tmp_path):
    scenario = Scenario(40, 0.5, "chain", 20260913)
    with pytest.raises(ValueError, match="frozen policy"):
        bench.run_matrix(tmp_path, [scenario], repetitions=1, pg_runtime=object())


def test_adaptive_row_reports_exact_policy_identity_and_is_not_forced_full(tmp_path):
    from types import SimpleNamespace

    fixture = build_scenario(tmp_path / "fixture", Scenario(40, 0.5, "chain", 20260913))
    decision = SimpleNamespace(
        requested_strategy="auto",
        selected_strategy="INCREMENTAL",
        strategy_reason="calibrated_incremental_lower_confident_cost",
        policy_version="postgres-adaptive-v1",
        estimated_incremental_ms=1.0,
        estimated_full_ms=10.0,
        dirty_entities=2,
    )
    result = bench.ScenarioResult(
        "postgres_adaptive",
        fixture,
        {},
        samples=[
            {
                "maintenance_ms": 1.0,
                "status_ms": 1.0,
                "impact_ms": 1.0,
                "explain_ms": 1.0,
                "graph_parity": True,
                "graph_hash_parity": True,
                "status_parity": True,
                "head_parity": True,
                "graph_hash": "f" * 64,
                "database_bytes": 1,
                "provenance_total_relation_bytes": 1,
                "index_bytes": 1,
                "storage_measurement": "unit",
                **decision.__dict__,
            }
        ],
        policy_id="postgres-adaptive-v1-deadbeefcafe",
        policy_sha256="a" * 64,
    ).to_dict()
    assert result["policy_id"] == "postgres-adaptive-v1-deadbeefcafe"
    assert result["policy_sha256"] == "a" * 64
    assert result["selected_strategy"] == "INCREMENTAL"


def test_adaptive_maintenance_delegates_auto_to_the_frozen_policy():
    calls = []

    class Backend:
        def ingest_diff(self, *args, **kwargs):
            calls.append((args, kwargs))
            return "adaptive-result"

    state = bench.BackendState(backend=Backend())
    workload = SimpleNamespace(artifact_id="delta")
    assert (
        bench._maintain(
            state,
            "postgres_adaptive",
            workload,
            "data",
            "configs",
            "frozen-policy",
        )
        == "adaptive-result"
    )
    assert calls == [
        (
            ("delta", "data", "configs"),
            {"requested_strategy": "auto", "policy_id": "frozen-policy"},
        )
    ]


def test_formal_output_is_write_once(tmp_path, monkeypatch):
    output = tmp_path / "result.json"
    output.write_text("owner-data\n", encoding="utf-8")
    monkeypatch.setattr(bench, "postgres_preflight", lambda: object())
    monkeypatch.setattr(bench, "load_frozen_policy", lambda path: (object(), object(), "a" * 64))
    assert (
        bench.main(
            [
                "--entities",
                "1000",
                "--ratios",
                "0",
                "--policy-from",
                str(tmp_path / "policy.json"),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert output.read_text(encoding="utf-8") == "owner-data\n"


@pytest.mark.parametrize("runner", [production_benchmark, real_validation])
def test_legacy_formal_outputs_are_write_once_before_work(tmp_path, monkeypatch, runner):
    output = tmp_path / "owned.json"
    output.write_text("owner-data\n", encoding="utf-8")
    arguments = [runner.__file__, "--output", str(output)]
    if runner is production_benchmark:
        arguments += ["--scales", "10"]
    else:
        arguments += [
            "--data-root",
            str(tmp_path),
            "--configs-root",
            str(tmp_path),
        ]
    monkeypatch.setattr("sys.argv", arguments)
    with pytest.raises(ValueError, match="write-once"):
        runner.main()
    assert output.read_text(encoding="utf-8") == "owner-data\n"


def test_real_six_method_validation_fails_closed_without_policy(tmp_path):
    with pytest.raises(ValueError, match="frozen policy"):
        real_validation.validate(tmp_path, tmp_path, six_method=True)


def test_runtime_and_postgres_storage_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(bench, "_cpu_details", lambda: ("Unit CPU", 4, 8))
    monkeypatch.setattr(bench, "_ram_bytes", lambda: 16 * 1024**3)
    environment = bench.runtime_environment(tmp_path)
    assert environment["cpu_model"] == "Unit CPU"
    assert environment["physical_cpu_cores"] == 4
    assert environment["logical_cpu_count"] == 8
    assert environment["ram_bytes"] == 16 * 1024**3
    assert environment["disk_total_bytes"] >= environment["disk_free_bytes"] > 0

    class Connection:
        def execute(self, query):
            if "pg_database_size" in query:
                return SimpleResult(1000)
            if "pg_indexes_size" in query:
                return SimpleResult(300)
            return SimpleResult(700)

    class SimpleResult:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

    @contextmanager
    def connect():
        yield Connection()

    storage = bench._storage(SimpleNamespace(connect=connect), "postgres_full")
    assert storage == {
        "database_bytes": 1000,
        "provenance_total_relation_bytes": 700,
        "index_bytes": 300,
        "storage_measurement": (
            "PostgreSQL pg_database_size plus vcp_provenance total relation and index bytes"
        ),
    }


def test_raw_explain_is_retained_with_explicit_dml_shape_scope(tmp_path):
    fixture = build_scenario(tmp_path / "fixture", Scenario(40, 0.5, "chain", 20260913))
    raw = [{"Plan": {"Node Type": "ModifyTable", "Relation Name": "entities"}}]
    row = bench.ScenarioResult("postgres_full", fixture, {}, plans=raw).to_dict()
    assert row["explain_analyze"] == raw
    assert "Relation Name" not in json.dumps(row["explain_analyze_sanitized"])
    assert row["explain_scope"] == "first executed DML row for each SQL statement shape"


def test_failed_instrumentation_excludes_samples_and_redacts_error(tmp_path, monkeypatch):
    fixture = build_scenario(tmp_path / "fixture", Scenario(40, 0.5, "chain", 20260913))

    @contextmanager
    def broken(*args, **kwargs):
        raise RuntimeError("password=TOPSECRET host=private service=private")
        yield

    monkeypatch.setattr(bench, "fresh_backend", broken)
    doc = bench.run_method(fixture, "postgres_incremental", repetitions=1).to_dict()
    assert doc["status"] == "failed"
    assert doc["samples"] == []
    assert doc["maintenance_p50_ms"] is None
    assert doc["parity_rate"] == 0
    assert "TOPSECRET" not in json.dumps(doc)
    assert "private" not in json.dumps(doc)


def test_extra_materialized_head_fails_parity_and_excludes_performance(tmp_path, monkeypatch):
    fixture = build_scenario(tmp_path / "fixture", Scenario(40, 0.5, "chain", 20260913))
    original = bench.fresh_backend

    @contextmanager
    def extra_head(method, data, **kwargs):
        with original(method, data, **kwargs) as state:
            normalize = state.backend.normalized

            def corrupted():
                value = normalize()
                value["statuses"].append({"head_id": "extra-materialized-head"})
                return value

            state.backend.normalized = corrupted
            yield state

    monkeypatch.setattr(bench, "fresh_backend", extra_head)
    result = bench.run_method(fixture, "sqlite_full", repetitions=1).to_dict()
    assert result["head_parity"] is False
    assert result["status"] == "failed"
    assert result["samples"] == []
    assert result["maintenance_p50_ms"] is None


def test_instrumentation_failure_after_valid_measurements_invalidates_result(tmp_path, monkeypatch):
    fixture = build_scenario(tmp_path / "fixture", Scenario(40, 0, "chain", 20260913))
    original = bench.fresh_backend
    clones = []

    @contextmanager
    def simulated(method, data, **kwargs):
        # Exercise orchestration only: this is deliberately an offline SQLite double,
        # never a PostgreSQL integration or performance result.
        clones.append(data)
        with original("sqlite_incremental", data) as state:
            if len(clones) == 2:
                raise RuntimeError("TOPSECRET instrumentation failure")
            ingest = state.backend.ingest_diff
            state.backend.ingest_diff = lambda *args, **kwargs: ingest(*args)
            yield state

    monkeypatch.setattr(bench, "fresh_backend", simulated)
    monkeypatch.setattr(
        bench,
        "_storage",
        lambda *args: {
            "database_bytes": 1,
            "provenance_total_relation_bytes": 1,
            "index_bytes": 1,
            "storage_measurement": "offline test double",
        },
    )
    result = bench.run_method(
        fixture,
        "postgres_adaptive",
        repetitions=1,
        policy_id="unit-policy",
        policy_sha256="a" * 64,
    ).to_dict()
    assert len(set(clones)) == 2
    assert result["failure"] == "benchmark_instrumentation_failed"
    assert result["samples"] == []
    assert result["maintenance_p50_ms"] is None
    assert result["parity_rate"] == 0
    assert "TOPSECRET" not in json.dumps(result)


@pytest.mark.parametrize("failed_action", [False, True])
def test_postgres_fresh_database_ownership_and_cleanup(monkeypatch, tmp_path, failed_action):
    from types import SimpleNamespace

    from vcp.provenance import postgres

    events = []

    class SQL(str):
        def format(self, value):
            return str(self).format(value)

    class Connection:
        info = SimpleNamespace(server_version=170011)

        def close(self):
            pass

        def execute(self, query):
            events.append(query)
            if query == "SHOW server_version":
                return SimpleNamespace(fetchone=lambda: ("17.11",))

    driver = SimpleNamespace(
        sql=SimpleNamespace(SQL=SQL, Identifier=lambda value: value),
        connect=lambda **kwargs: Connection(),
    )
    monkeypatch.setattr(postgres, "_load_psycopg", lambda: driver)
    database_names = []
    for _ in range(2):
        try:
            with bench.fresh_backend(
                "postgres_full", tmp_path, pg_runtime=(driver, "private_service")
            ) as state:
                database_names.append(
                    next(
                        event.split()[-1]
                        for event in reversed(events)
                        if event.startswith("CREATE DATABASE")
                    )
                )
                assert "private_service" not in json.dumps(state.environment)
                assert state.environment["postgresql_major"] == 17
                if failed_action:
                    raise RuntimeError("operation interrupted")
        except RuntimeError:
            assert failed_action
    assert len(set(database_names)) == 2
    for name in database_names:
        assert events.count(f"CREATE DATABASE {name}") == 1
        assert events.count(f"DROP DATABASE {name} WITH (FORCE)") == 1


def test_legacy_entrypoints_save_failed_six_method_results_and_exit_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "postgres_preflight", lambda: (None, None))
    monkeypatch.setattr(bench, "run_matrix", lambda *args, **kwargs: [{"status": "failed"}])
    monkeypatch.setattr(
        bench,
        "load_frozen_policy",
        lambda path: (SimpleNamespace(id="unit-policy"), object(), "a" * 64),
    )
    monkeypatch.setattr(bench, "empirical_crossover", lambda evidence: {"unit": True})
    monkeypatch.setattr(production_benchmark, "run_scale", lambda *args: {"legacy": True})
    production_output = tmp_path / "production.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "production_benchmark.py",
            "--scales",
            "1000",
            "--six-method",
            "--policy-from",
            str(tmp_path / "policy.json"),
            "--output",
            str(production_output),
        ],
    )
    assert production_benchmark.main() == 1
    doc = json.loads(production_output.read_text())
    assert doc["scales"] == [{"legacy": True}]
    assert doc["six_method_benchmark"] == [{"status": "failed"}]
    monkeypatch.setattr(real_validation, "validate", lambda *args, **kwargs: doc)
    real_output = tmp_path / "real.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "real_validation.py",
            "--data-root",
            str(tmp_path),
            "--configs-root",
            str(tmp_path),
            "--six-method",
            "--policy-from",
            str(tmp_path / "policy.json"),
            "--output",
            str(real_output),
        ],
    )
    assert real_validation.main() == 1
    assert json.loads(real_output.read_text()) == doc


def test_explain_output_keeps_measurements_but_drops_connection_and_expression_data():
    raw = [
        {
            "Plan": {
                "Node Type": "ModifyTable",
                "Actual Total Time": 2.5,
                "Filter": "password=TOPSECRET",
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "entities",
                        "Shared Hit Blocks": 3,
                        "Output": ["secret"],
                        "service": "private",
                    }
                ],
            },
            "Execution Time": 3.0,
            "Query Text": "secret",
            "conninfo": "private",
        }
    ]
    clean = bench.sanitize_explain(raw)
    assert clean[0]["Plan"]["Actual Total Time"] == 2.5
    assert clean[0]["Plan"]["Plans"][0]["Shared Hit Blocks"] == 3
    assert not any(word in json.dumps(clean) for word in ("TOPSECRET", "private", "secret"))


@pytest.mark.parametrize(
    "payload",
    [
        [{"Plan": {"Node Type": "Seq Scan", "Filter": "password=TOPSECRET"}}],
        [{"Plan": {"Node Type": "Seq Scan", "Alias": "postgresql://user@private/db"}}],
        [{"Plan": {"Node Type": "Seq Scan", "Output": ["api_token=PRIVATE"]}}],
        [{"Plan": {"Node Type": "Seq Scan", "Authorization": "Bearer deadbeef"}}],
        [
            {
                "Plan": {
                    "Node Type": "Seq Scan",
                    "Filter": "host=db.internal user=bench dbname=vcp",
                }
            }
        ],
        [{"Plan": {"Node Type": "Seq Scan", "Output": ["Bearer deadbeef"]}}],
        [{"Plan": {"Node Type": "Seq Scan", "Output": ["sk-privatecredential"]}}],
        [{"Plan": {"Node Type": "Seq Scan", "Secret Key": "deadbeef"}}],
    ],
)
def test_raw_explain_rejects_sensitive_keys_and_ordinary_string_values(payload):
    with pytest.raises(ValueError, match="unsafe EXPLAIN"):
        bench.validate_raw_explain(payload)


def test_raw_explain_allows_normal_postgres_sort_and_hash_key_fields():
    payload = [
        {
            "Plan": {
                "Node Type": "Sort",
                "Sort Key": ["entity_id"],
                "Plans": [{"Node Type": "Hash", "Hash Key": "edge_id"}],
            }
        }
    ]
    bench.validate_raw_explain(payload)


def test_explain_capture_rejects_sensitive_value_before_retaining_raw_plan():
    class Connection:
        @contextmanager
        def transaction(self, **kwargs):
            yield

        def execute(self, query, params=()):
            return SimpleNamespace(
                fetchone=lambda: (
                    [
                        {
                            "Plan": {
                                "Node Type": "Seq Scan",
                                "Filter": "password=TOPSECRET",
                            }
                        }
                    ],
                )
            )

    observed = bench._ExplainConnection(Connection())
    with pytest.raises(ValueError, match="unsafe EXPLAIN"):
        observed.probe("INSERT INTO vcp_provenance.entities VALUES (%s)", (1,))
    assert observed.plans == []


@pytest.mark.parametrize(
    "runner_name", ["adaptive", "calibration", "production", "real", "heldout"]
)
def test_every_formal_runner_rejects_sensitive_explain_before_publication(
    tmp_path, monkeypatch, runner_name
):
    from performance.provenance import calibrate_adaptive, evaluate_adaptive

    output = tmp_path / f"{runner_name}.json"
    unsafe = {
        "status": "failed",
        "method": "postgres_full",
        "explain_analyze": [{"Plan": {"Node Type": "Seq Scan", "Filter": "password=TOPSECRET"}}],
    }
    if runner_name == "adaptive":
        monkeypatch.setattr(bench, "postgres_preflight", lambda: object())
        monkeypatch.setattr(
            bench,
            "load_frozen_policy",
            lambda path: (SimpleNamespace(id="unit"), object(), "a" * 64),
        )
        monkeypatch.setattr(bench, "empirical_crossover", lambda evidence: {})
        monkeypatch.setattr(bench, "run_matrix", lambda *args, **kwargs: [unsafe])
        assert (
            bench.main(
                [
                    "--policy-from",
                    str(tmp_path / "policy.json"),
                    "--output",
                    str(output),
                ]
            )
            == 1
        )
    elif runner_name == "calibration":
        monkeypatch.setattr(calibrate_adaptive.benchmark, "postgres_preflight", lambda: object())
        monkeypatch.setattr(calibrate_adaptive, "collect_rows", lambda *args, **kwargs: [unsafe])
        assert calibrate_adaptive.main(["--output", str(output)]) == 1
    elif runner_name == "production":
        monkeypatch.setattr(production_benchmark, "run_scale", lambda *args: unsafe)
        monkeypatch.setattr(
            "sys.argv",
            ["production_benchmark.py", "--scales", "10", "--output", str(output)],
        )
        with pytest.raises(ValueError, match="unsafe EXPLAIN"):
            production_benchmark.main()
    elif runner_name == "real":
        monkeypatch.setattr(real_validation, "validate", lambda *args, **kwargs: unsafe)
        monkeypatch.setattr(
            "sys.argv",
            [
                "real_validation.py",
                "--data-root",
                str(tmp_path),
                "--configs-root",
                str(tmp_path),
                "--output",
                str(output),
            ],
        )
        with pytest.raises(ValueError, match="unsafe EXPLAIN"):
            real_validation.main()
    else:
        monkeypatch.setattr(
            evaluate_adaptive, "load_calibration", lambda path: (object(), object())
        )
        monkeypatch.setattr(evaluate_adaptive.benchmark, "postgres_preflight", lambda: object())
        monkeypatch.setattr(evaluate_adaptive, "collect_rows", lambda *args, **kwargs: [unsafe])
        monkeypatch.setattr(
            evaluate_adaptive,
            "evaluate_policy",
            lambda *args: SimpleNamespace(model_dump=lambda: {"ok": True}, performance_pass=True),
        )
        assert (
            evaluate_adaptive.main(
                [
                    "--policy-from",
                    str(tmp_path / "policy.json"),
                    "--output",
                    str(output),
                ]
            )
            == 1
        )
    assert not output.exists()


def test_native_and_operator_declared_image_evidence_are_not_conflated(monkeypatch):
    monkeypatch.delenv("VCP_TEST_PG_DEPLOYMENT_KIND", raising=False)
    monkeypatch.delenv("VCP_TEST_PG_IMAGE_DIGEST", raising=False)
    assert bench._deployment_evidence() == {
        "deployment_kind": "native_portable",
        "image_digest": None,
        "image_digest_source": "not_applicable",
        "operator_declared_image_digest": None,
        "operator_declared_image_digest_source": None,
    }
    monkeypatch.setenv("VCP_TEST_PG_DEPLOYMENT_KIND", "container")
    monkeypatch.setenv("VCP_TEST_PG_IMAGE_DIGEST", "sha256:" + "a" * 64)
    evidence = bench._deployment_evidence()
    assert evidence["image_digest"] is None
    assert evidence["image_digest_source"] == "unavailable"
    assert evidence["operator_declared_image_digest"] == "sha256:" + "a" * 64
    assert evidence["operator_declared_image_digest_source"] == "operator_declared_unverified"


def test_invalid_operator_declared_image_digest_fails_closed(monkeypatch):
    monkeypatch.setenv("VCP_TEST_PG_DEPLOYMENT_KIND", "container")
    monkeypatch.setenv("VCP_TEST_PG_IMAGE_DIGEST", "not-a-digest")
    with pytest.raises(ValueError, match="deployment evidence"):
        bench._deployment_evidence()


@pytest.mark.parametrize("fail", [False, True])
def test_mutating_explain_always_rolls_back(fail):
    class Connection:
        def __init__(self):
            self.value = 0
            self.events = []

        def execute(self, query, params=()):
            self.events.append(query)
            if query.startswith("EXPLAIN"):
                self.value += 1
                if fail:
                    raise RuntimeError("private database error")
            return self

        def fetchone(self):
            return ([{"Plan": {"Node Type": "ModifyTable"}}],)

        @contextmanager
        def transaction(self, **kwargs):
            previous = self.value
            try:
                yield
            finally:
                if kwargs.get("force_rollback"):
                    self.value = previous

        def rollback(self):
            self.events.append("ROLLBACK")
            self.value = 0

    connection = Connection()

    def action(observed):
        observed.execute("INSERT INTO vcp_provenance.entities VALUES (%s)", (1,))

    if fail:
        with pytest.raises(RuntimeError):
            bench.capture_explain_rollback(connection, "postgres_incremental", action)
    else:
        assert bench.capture_explain_rollback(connection, "postgres_incremental", action)[0]["Plan"]
    assert connection.value == 0
    assert connection.events[0] == "BEGIN"
    assert connection.events[-1] == "ROLLBACK"
