"""Synthetic unit observations only; these are never benchmark result artifacts."""

import ast
import copy
import inspect
import json
import subprocess
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest

from performance.provenance import calibrate_adaptive as calibration
from performance.provenance import evaluate_adaptive as evaluation
from performance.provenance import publish_policy_v2 as publish_v2
from performance.provenance.workloads import Scenario, build_scenario, scenario_matrix
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir
from vcp.provenance import strategy
from vcp.provenance.graph import build_graph
from vcp.provenance.index import graph_hash
from vcp.provenance.policy_bands import edges_decade


def observations(seeds=(20260913, 20260914)):
    return [
        dict(
            scenario_id=f"unit-{seed}-{i}",
            scenario_hash=f"{seed * 100 + i:064x}",
            workload_hash=f"{seed * 100 + i + 30:064x}",
            seed=seed,
            changed_samples=i,
            dirty_entities=i * 2,
            total_entities=100 + i,
            dirty_ratio=i * 2 / (100 + i),
            total_edges=200 + i,
            historical_changes=50 + i,
            head_count=1,
            incremental_p50_ms=2 + i * 0.5,
            full_p50_ms=20 + i,
            environment_fingerprint="a" * 64,
            backend_schema_version=1,
            postgresql_major=17,
            benchmark_schema_version=1,
        )
        for seed in seeds
        for i in range(1, 9)
    ]


def test_fit_deterministic_positive_and_calibration_pinned():
    rows = observations()
    first = strategy.fit_policy(rows)
    assert first == strategy.fit_policy(list(reversed(rows)))
    assert first.incremental_model.feature_order == strategy.INCREMENTAL_FEATURE_ORDER
    assert first.full_model.feature_order == strategy.FULL_FEATURE_ORDER
    assert all(value >= 0 for value in first.incremental_model.coefficients.values())
    assert first.incremental_rmse_ms < 1e-9
    assert first.training_row_count == len(rows)
    changed = copy.deepcopy(rows)
    changed[0]["incremental_p50_ms"] += 1
    assert strategy.fit_policy(changed).id != first.id


@pytest.mark.parametrize("mutation", ["holdout", "duplicate", "missing_seed", "extra", "env"])
def test_fit_rejects_invalid_observations(mutation):
    rows = observations()
    if mutation == "holdout":
        rows[0]["seed"] = 20261001
    elif mutation == "duplicate":
        rows.append(rows[0])
    elif mutation == "missing_seed":
        rows = rows[:8]
    elif mutation == "extra":
        rows[0]["PGPASSWORD"] = "PRIVATE_MARKER"
    else:
        rows[0]["environment_fingerprint"] = "b" * 64
    with pytest.raises(ValidationFailed) as error:
        strategy.fit_policy(rows)
    assert "PRIVATE_MARKER" not in "".join(traceback.format_exception(error.value))


def test_bundle_verifies_exact_embedded_policy_and_immutable_output(tmp_path):
    path = tmp_path / "result.json"
    policy = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    loaded, evidence = evaluation.load_calibration(path)
    assert loaded == policy
    assert len(evidence.observations) == policy.training_row_count
    with pytest.raises(ValidationFailed):
        calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    payload = json.loads(path.read_text())
    payload["policy"]["full_rmse_ms"] += 1
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    with pytest.raises(ValidationFailed, match="invalid_calibration_artifact"):
        evaluation.load_calibration(path)


def test_calibration_publishes_multivariate_empirical_crossover_schema(tmp_path):
    path = tmp_path / "result.json"
    calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    crossover = json.loads(path.read_text())["empirical_crossover"]
    assert crossover["schema_version"] == 1
    assert crossover["global_threshold"] is None
    assert crossover["fixed_feature_dimensions"] == ["topology", "entities", "seed"]
    assert crossover["varied_feature"] == "change_ratio"
    assert "no interpolation" in crossover["algorithm"]
    assert crossover["slices"]
    assert set(crossover["slices"][0]) == {
        "topology",
        "entities",
        "seed",
        "observations",
        "crossover",
    }


def test_calibration_and_evaluation_outputs_are_write_once(tmp_path, monkeypatch):
    output = tmp_path / "heldout.json"
    output.write_text("owner-data\n", encoding="utf-8")
    policy_path = tmp_path / "policy.json"
    policy = calibration.publish_calibration(
        benchmark_rows(strategy.CALIBRATION_SEEDS), policy_path
    )
    monkeypatch.setattr(evaluation.benchmark, "postgres_preflight", lambda: object())
    monkeypatch.setattr(
        evaluation,
        "collect_rows",
        lambda *args, **kwargs: benchmark_rows(strategy.HELDOUT_SEEDS, policy),
    )
    assert evaluation.main(["--policy-from", str(policy_path), "--output", str(output)]) == 1
    assert output.read_text(encoding="utf-8") == "owner-data\n"


def test_runners_fail_without_pg_and_do_not_emit_json(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("VCP_TEST_PG_SERVICE", raising=False)
    output = tmp_path / "result.json"
    assert calibration.main(["--output", str(output)]) == 1
    assert evaluation.main(["--policy-from", str(output), "--output", str(output)]) == 1
    assert not output.exists()
    captured = capsys.readouterr()
    assert "VERDICT" in captured.err
    assert "Traceback" not in captured.err


def benchmark_rows(seeds, policy=None, comparison=None, total_edges=2000):
    """Clearly synthetic Task 10-shaped rows, never saved as research output. ``total_edges``
    is one number for every scenario, or a function of the scenario."""
    rows = []
    for scenario in scenario_matrix(seeds=seeds):
        seed = scenario.seed
        sample_entities = (scenario.entities - 20) // 3
        changed = round(sample_entities * scenario.change_ratio)
        features = strategy.MaintenanceFeatures(
            changed_samples=changed,
            dirty_entities=20 if changed else 0,
            total_entities=1100,
            dirty_ratio=20 / 1100 if changed else 0,
            total_edges=total_edges(scenario) if callable(total_edges) else total_edges,
            historical_changes=600,
            head_count=1,
        )
        methods = (
            evaluation.HELDOUT_V2_METHODS
            if comparison
            else evaluation.EVALUATION_METHODS
            if policy
            else evaluation.FIXED_METHODS
        )
        for method in methods:
            requested = {
                "postgres_full": "full",
                "postgres_incremental": "incremental",
                "postgres_adaptive": "auto",
                "postgres_adaptive_v1": "auto",
            }[method]
            method_policy = comparison if method == "postgres_adaptive_v1" else policy
            decision = strategy.select_strategy(
                requested, features, method_policy if requested == "auto" else None
            )
            latency = 20.0 if requested == "full" else 10.0
            sample = dict(
                maintenance_ms=latency,
                status_ms=1.0,
                impact_ms=1.0,
                explain_ms=1.0,
                graph_parity=True,
                graph_hash_parity=True,
                status_parity=True,
                head_parity=True,
                graph_hash="f" * 64,
                database_bytes=1024,
                provenance_total_relation_bytes=768,
                index_bytes=256,
                storage_measurement=evaluation._STORAGE,
                requested_strategy=requested,
                selected_strategy=decision.selected_strategy.value,
                strategy_reason=decision.reason,
                policy_version=decision.policy_version,
                estimated_incremental_ms=decision.estimated_incremental_ms,
                estimated_full_ms=decision.estimated_full_ms,
                dirty_entities=features.dirty_entities,
            )
            row = {
                key: value
                for key, value in sample.items()
                if key
                not in {
                    operation + "_ms"
                    for operation in ("maintenance", "status", "impact", "explain")
                }
            }
            row.update(
                schema_version=1,
                method=method,
                policy_id=method_policy.id if requested == "auto" else None,
                policy_sha256=(
                    strategy._policy_sha256(method_policy) if requested == "auto" else None
                ),
                scenario_id=scenario.scenario_id,
                scenario_hash=scenario.scenario_hash,
                workload_hash=scenario.scenario_hash,
                track="scaled",
                seed=seed,
                topology=scenario.topology,
                entities=scenario.entities,
                change_ratio=float(scenario.change_ratio),
                realized_change_ratio=changed / sample_entities,
                sample_entities=sample_entities,
                **features.model_dump(),
                total_changes=600 + changed,
                environment=dict(
                    system="Windows",
                    system_release="11",
                    system_version="10.0.26200",
                    machine="AMD64",
                    python_version="3.12.10",
                    python_implementation="CPython",
                    cpu_count=8,
                    cpu_model="Unit Test CPU",
                    physical_cpu_cores=4,
                    logical_cpu_count=8,
                    ram_bytes=16 * 1024**3,
                    disk_path="C:\\",
                    disk_total_bytes=100 * 1024**3,
                    disk_free_bytes=50 * 1024**3,
                    sqlite_version="3.50.0",
                    vcp_commit="a" * 40,
                    postgresql_major=17,
                    postgresql_version=170011,
                    postgresql_server_version="17.11",
                    deployment_kind="native_portable",
                    image_digest=None,
                    image_digest_source="not_applicable",
                    operator_declared_image_digest=None,
                    operator_declared_image_digest_source=None,
                    environment_fingerprint="a" * 64,
                    backend_schema_version=1,
                ),
                status="ok",
                failure=None,
                samples=[copy.deepcopy(sample) for _ in range(scenario.repetitions)],
                repetitions=scenario.repetitions,
                parity_rate=1.0,
                explain_analyze=[{"Plan": {"Node Type": "ModifyTable"}}],
                explain_analyze_sanitized=[{"Plan": {"Node Type": "ModifyTable"}}],
                explain_scope="first executed DML row for each SQL statement shape",
                instrumentation=(
                    "separate fresh state; first DML row per SQL shape; "
                    "rollback; excluded from timings"
                ),
                query_measurement="public status API; impact/explain include graph load as in CLI",
                warmup=(
                    "baseline rebuilt before each sample; no timed warmup; OS caches may be warm"
                ),
                throughput_samples_per_second=changed / (latency / 1000),
            )
            for operation in ("maintenance", "status", "impact", "explain"):
                row[operation + "_p50_ms"] = sample[operation + "_ms"]
                row[operation + "_p95_ms"] = sample[operation + "_ms"]
            rows.append(row)
    return rows


def test_task10_rows_fit_and_heldout_never_refits(tmp_path, monkeypatch):
    path = tmp_path / "result.json"
    rows = benchmark_rows(strategy.CALIBRATION_SEEDS)
    policy = calibration.publish_calibration(rows, path)

    def forbidden(*args, **kwargs):
        pytest.fail("held-out called fitting")

    monkeypatch.setattr(strategy, "fit_policy", forbidden)
    monkeypatch.setattr(strategy, "fit_cost_models", forbidden)
    monkeypatch.setattr(strategy, "fit_policy_v2", forbidden)
    monkeypatch.setattr(calibration, "fit_policy", forbidden)
    heldout = benchmark_rows(strategy.HELDOUT_SEEDS, policy)
    result = evaluation.evaluate_policy(path, heldout)
    assert result.parity_rate == 1 and result.overlap_count == 0
    assert result.policy_id == policy.id and result.performance_pass
    assert evaluation.evaluate_policy(path, list(reversed(heldout))) == result
    with pytest.raises(ValidationFailed, match="workload_leakage"):
        evaluation.evaluate_policy(path, heldout + rows[:1])
    tree = ast.parse(inspect.getsource(evaluation))
    imports = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    assert not any(
        "calibrat" in name
        and name not in {"CalibrationEvidence", "calibration_text"}
        or "sklearn" in name
        or "fit_policy" in name
        or "fit_cost_models" in name
        for name in imports
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "failed",
        "sqlite",
        "parity",
        "missing",
        "duplicate",
        "samples",
        "percentile",
        "features",
        "environment",
        "secret",
        "nested_secret",
        "plan_secret",
        "identity",
        "seed",
        "nan",
        "wrong_strategy",
    ],
)
def test_task10_input_validation_is_complete_and_secret_safe(mutation):
    rows = benchmark_rows(strategy.CALIBRATION_SEEDS)
    row = rows[0]
    if mutation == "failed":
        row["status"] = "failed"
    elif mutation == "sqlite":
        row["method"] = "sqlite_full"
    elif mutation == "parity":
        row["samples"][0]["head_parity"] = False
    elif mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(row))
    elif mutation == "samples":
        row["samples"] = []
    elif mutation == "percentile":
        row["maintenance_p50_ms"] += 1
    elif mutation == "features":
        row["dirty_entities"] += 1
    elif mutation == "environment":
        row["environment"]["environment_fingerprint"] = "b" * 64
    elif mutation == "secret":
        row["PGPASSWORD"] = "PRIVATE_MARKER"
    elif mutation == "nested_secret":
        row["environment"]["PGHOST"] = "PRIVATE_MARKER"
    elif mutation == "plan_secret":
        row["explain_analyze"][0]["Secret"] = "PRIVATE_MARKER"
    elif mutation == "identity":
        row["scenario_hash"] = "0" * 64
    elif mutation == "seed":
        row["seed"] = 20261001
    elif mutation == "nan":
        row["maintenance_p50_ms"] = float("nan")
    elif mutation == "wrong_strategy":
        row["selected_strategy"] = "INCREMENTAL"
    with pytest.raises(ValidationFailed, match="invalid_benchmark_rows") as error:
        evaluation.paired_observations(rows)
    assert "PRIVATE_MARKER" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("mutation", ["decision", "estimate", "environment", "slow"])
def test_heldout_compatibility_identity_and_honest_performance(tmp_path, mutation):
    path = tmp_path / "result.json"
    policy = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    rows = benchmark_rows(strategy.HELDOUT_SEEDS, policy)
    for row in rows:
        if mutation == "environment":
            row["environment"]["environment_fingerprint"] = "b" * 64
        if row["method"] != "postgres_adaptive":
            continue
        if mutation in {"decision", "estimate"}:
            key = "strategy_reason" if mutation == "decision" else "estimated_full_ms"
            value = "fallback_policy_absent_full" if mutation == "decision" else 999.0
            row[key] = value
            for sample in row["samples"]:
                sample[key] = value
        if mutation == "slow":
            for sample in row["samples"]:
                sample["maintenance_ms"] = 100.0
            row["maintenance_p50_ms"] = row["maintenance_p95_ms"] = 100.0
            row["throughput_samples_per_second"] = 100.0
    if mutation == "slow":
        result = evaluation.evaluate_policy(path, rows)
        assert not result.performance_pass and result.parity_rate == 1
    else:
        with pytest.raises(ValidationFailed):
            evaluation.evaluate_policy(path, rows)


def test_prepare_policy_recomputes_oracle_before_measured_baseline(tmp_path):
    path = tmp_path / "result.json"
    policy = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    _, evidence = evaluation.load_calibration(path)
    workload = build_scenario(tmp_path / "fixture", Scenario(40, 0.5, "chain", 20261001))
    prepared = evaluation.prepare_workload(workload, policy, evidence)
    assert prepared.workload_hash != workload.workload_hash
    assert graph_hash(prepared.expected) != graph_hash(workload.expected)
    baseline = build_graph(prepared.data, prepared.configs)
    assert any(entity.entity_type == "artifact" for entity in baseline.entities.values())
    data, configs = prepared.clone(tmp_path / "clone")
    prepared.publish(data)
    assert build_graph(data, configs).normalized() == prepared.expected.normalized()
    assert strategy.load_policy_artifact(data, policy.id) == policy


def test_recursive_duplicate_bundle_keys_fail_secret_safe(tmp_path):
    path = tmp_path / "result.json"
    calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    raw = path.read_text(encoding="utf-8")
    raw = raw.replace('"seed": 20260913', '"seed": "PRIVATE_MARKER", "seed": 20260913', 1)
    path.write_text(raw, encoding="utf-8", newline="\n")
    with pytest.raises(ValidationFailed) as error:
        evaluation.load_calibration(path)
    assert "PRIVATE_MARKER" not in "".join(traceback.format_exception(error.value))


def test_calibration_and_holdout_matrix_hashes_are_disjoint():
    first = scenario_matrix(seeds=strategy.CALIBRATION_SEEDS)
    second = scenario_matrix(seeds=strategy.HELDOUT_SEEDS)
    assert not {r.scenario_hash for r in first} & {r.scenario_hash for r in second}
    assert not {r.scenario_id for r in first} & {r.scenario_id for r in second}


def test_relative_output_path_is_loadable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), "relative.json")
    assert evaluation.load_calibration("relative.json")[0]


@pytest.mark.parametrize("runner", [calibration, evaluation])
def test_invalid_cli_arguments_do_not_leak_or_escape_verdict(runner, capsys):
    assert runner.main(["--PRIVATE_MARKER"]) == 1
    captured = capsys.readouterr()
    assert "PRIVATE_MARKER" not in captured.err + captured.out
    assert "VERDICT" in captured.err


def test_extended_evidence_rejects_conflicting_policy_before_claim(tmp_path):
    evidence = strategy.calibration_evidence(observations())
    path = tmp_path / "evidence.json"
    path.write_text(strategy.calibration_text(evidence), encoding="utf-8", newline="\n")
    policy = strategy.fit_policy(observations()).model_copy(update={"training_row_count": 99})
    with pytest.raises(ValidationFailed):
        strategy.write_policy_artifact(tmp_path / "data", policy, path)
    assert not (tmp_path / "data" / "artifacts").exists()


def test_extended_evidence_rejects_nested_secret_and_duplicate_before_claim(tmp_path):
    policy = strategy.fit_policy(observations())
    raw = strategy.calibration_text(strategy.calibration_evidence(observations()))
    raw = raw.replace('"seed":20260913', '"seed":"PRIVATE_MARKER","seed":20260913', 1)
    path = tmp_path / "evidence.json"
    path.write_text(raw, encoding="utf-8", newline="\n")
    with pytest.raises(ValidationFailed) as error:
        strategy.write_policy_artifact(tmp_path / "data", policy, path)
    assert "PRIVATE_MARKER" not in "".join(traceback.format_exception(error.value))
    assert not (tmp_path / "data" / "artifacts").exists()


def test_cli_failed_rows_never_publish(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(calibration.benchmark, "postgres_preflight", lambda: object())
    rows = benchmark_rows(strategy.CALIBRATION_SEEDS)
    rows[0]["status"] = "failed"
    monkeypatch.setattr(calibration, "collect_rows", lambda *args, **kwargs: rows)
    path = tmp_path / "result.json"
    assert calibration.main(["--output", str(path)]) == 1
    assert not path.exists() and not (tmp_path / "result-artifacts").exists()
    assert "status=FAIL" in capsys.readouterr().err


def test_spec_gates_pool_samples_and_keep_stricter_scenario_diagnostic(tmp_path):
    path = tmp_path / "result.json"
    policy = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    rows = benchmark_rows(strategy.HELDOUT_SEEDS, policy)
    for row in rows:
        latency = (
            100.0
            if row["seed"] == 20261002
            else (12.0 if row["method"] == "postgres_adaptive" else 10.0)
        )
        for sample in row["samples"]:
            sample["maintenance_ms"] = latency
        row["maintenance_p50_ms"] = row["maintenance_p95_ms"] = latency
        row["throughput_samples_per_second"] = 10 / (latency / 1000)
    result = evaluation.evaluate_policy(path, rows)
    assert result.median_ratio == pytest.approx(56 / 55)
    assert result.p95_ratio == 1.0 and result.performance_pass
    assert not result.every_scenario_pass


def test_policy_json_must_match_exactly_before_model_normalization(tmp_path):
    path = tmp_path / "result.json"
    policy = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    assert all(value == 0 for value in policy.full_model.coefficients.values())
    document = json.loads(path.read_text())
    document["policy"]["full_model"]["coefficients"]["total_entities"] = -1
    path.write_text(json.dumps(document), encoding="utf-8", newline="\n")
    with pytest.raises(ValidationFailed, match="invalid_calibration_artifact"):
        evaluation.load_calibration(path)


def test_heldout_executes_in_separate_process_without_fitting_import(tmp_path):
    path = tmp_path / "unit-policy.json"
    policy = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    rows = tmp_path / "unit-rows.json"
    rows.write_text(
        json.dumps(benchmark_rows(strategy.HELDOUT_SEEDS, policy)), encoding="utf-8", newline="\n"
    )
    script = """
import builtins, json, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if 'calibrate_adaptive' in name or name.startswith('sklearn'):
        raise AssertionError('forbidden calibration import')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
sys.path.insert(0, 'tests')
from performance.provenance.evaluate_adaptive import evaluate_policy
from pathlib import Path
result = evaluate_policy(Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text()))
assert result.parity_rate == 1.0
assert not any('calibrate_adaptive' in name for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), str(rows)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_clipped_intercept_rmse_uses_published_predictions():
    rows = observations()
    for row in rows:
        row["incremental_p50_ms"] = row["changed_samples"] - 0.5
    policy = strategy.fit_policy(rows)
    assert policy.incremental_model.intercept_ms == 0
    expected = (
        sum(
            (
                policy.incremental_model.predict(strategy.CalibrationObservation(**row))
                - row["incremental_p50_ms"]
            )
            ** 2
            for row in rows
        )
        / len(rows)
    ) ** 0.5
    assert policy.incremental_rmse_ms == pytest.approx(expected)


@pytest.mark.parametrize("slow", [False, True])
def test_heldout_cli_publishes_honest_gates_on_unit_data(tmp_path, monkeypatch, capsys, slow):
    path = tmp_path / "unit-policy.json"
    policy = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    rows = benchmark_rows(strategy.HELDOUT_SEEDS, policy)
    if slow:
        for row in rows:
            if row["method"] == "postgres_adaptive":
                row["maintenance_p50_ms"] = row["maintenance_p95_ms"] = 100.0
                for sample in row["samples"]:
                    sample["maintenance_ms"] = 100.0
                row["throughput_samples_per_second"] = 100.0
    runtime = object()
    monkeypatch.setattr(evaluation.benchmark, "postgres_preflight", lambda: runtime)
    checked = _accepting_environment(monkeypatch)

    def collect(root, scenarios, **kwargs):
        assert {s.seed for s in scenarios} == set(strategy.HELDOUT_SEEDS)
        assert kwargs["policy"] == policy
        assert checked == [(runtime, (policy,))]  # the environment is checked first
        return rows

    monkeypatch.setattr(evaluation, "collect_rows", collect)
    output = tmp_path / "unit-evaluation.json"
    assert evaluation.main(["--policy-from", str(path), "--output", str(output)]) == int(slow)
    document = json.loads(output.read_text())
    assert document["evaluation"]["performance_pass"] == (not slow)
    assert document["evaluation"]["policy_id"] == policy.id
    assert "status=" + ("FAIL" if slow else "OK") in capsys.readouterr().err


def test_collect_rows_can_delegate_to_process_isolated_checkpoint_runner(tmp_path, monkeypatch):
    policy = object()
    policy_path = tmp_path / "policy.json"
    observed = {}
    monkeypatch.setattr(evaluation, "policy_file_sha256", lambda path, value: "a" * 64)

    def isolated(root, scenarios, **kwargs):
        observed.update(root=root, scenarios=list(scenarios), kwargs=kwargs)
        return [{"status": "ok"}]

    monkeypatch.setattr(evaluation.benchmark, "run_matrix_isolated", isolated)
    scenarios = [Scenario(40, 0.5, "chain", strategy.HELDOUT_SEEDS[0])]
    assert evaluation.collect_rows(
        tmp_path,
        scenarios,
        methods=evaluation.EVALUATION_METHODS,
        pg_runtime="pg",
        policy=policy,
        evidence="evidence",
        policy_from=policy_path,
        isolated=True,
    ) == [{"status": "ok"}]
    assert observed["root"] == tmp_path
    assert observed["kwargs"]["policy_from"] == policy_path
    assert observed["kwargs"]["policy_sha256"] == "a" * 64


def test_task10_rows_parse_but_small_smoke_cannot_be_normative(tmp_path):
    rows = []
    for seed in strategy.CALIBRATION_SEEDS:
        workload = build_scenario(tmp_path / str(seed), Scenario(40, 0.5, "chain", seed))
        for template in benchmark_rows((seed,))[:2]:
            sample = copy.deepcopy(template["samples"][0])
            sample["dirty_entities"] = 2
            result = evaluation.benchmark.ScenarioResult(
                template["method"],
                workload,
                template["environment"],
                samples=[sample],
                plans=template["explain_analyze"],
            )
            row = result.to_dict()
            assert evaluation.BenchmarkRow.model_validate(row)
            rows.append(row)
    with pytest.raises(ValidationFailed, match="invalid_benchmark_rows"):
        evaluation.paired_observations(rows)


@pytest.mark.parametrize("mutation", ["omission", "extra", "short", "long"])
@pytest.mark.parametrize(
    "seeds,methods",
    [
        (strategy.CALIBRATION_SEEDS, evaluation.FIXED_METHODS),
        (strategy.HELDOUT_SEEDS, evaluation.EVALUATION_METHODS),
    ],
)
def test_exact_manifest_and_sample_counts_cannot_be_reduced(mutation, seeds, methods):
    policy = strategy.fit_policy(observations()) if "postgres_adaptive" in methods else None
    rows = benchmark_rows(seeds, policy)
    ident = rows[0]["scenario_hash"]
    if mutation == "omission":
        rows = [r for r in rows if r["scenario_hash"] != ident]
    elif mutation == "extra":
        extras = [copy.deepcopy(r) for r in rows if r["scenario_hash"] == ident]
        for row in extras:
            scenario = Scenario(2000, row["change_ratio"], row["topology"], row["seed"])
            row.update(
                entities=2000,
                scenario_id=scenario.scenario_id,
                scenario_hash=scenario.scenario_hash,
                workload_hash=scenario.scenario_hash,
            )
        rows.extend(extras)
    else:
        for row in rows:
            if row["scenario_hash"] != ident:
                continue
            if mutation == "short":
                row["samples"].pop()
            else:
                row["samples"].append(copy.deepcopy(row["samples"][0]))
            row["repetitions"] = len(row["samples"])
    with pytest.raises(ValidationFailed, match="invalid_benchmark_rows"):
        evaluation.validate_rows(rows, seeds=seeds, methods=methods)


def test_no_op_latency_cannot_change_fitted_policy_or_identity(tmp_path):
    rows = benchmark_rows(strategy.CALIBRATION_SEEDS)
    first = calibration.publish_calibration(rows, tmp_path / "first.json")
    changed = copy.deepcopy(rows)
    for row in changed:
        if row["selected_strategy"] == "NO_OP":
            row["maintenance_p50_ms"] = row["maintenance_p95_ms"] = 123456.0
            for sample in row["samples"]:
                sample["maintenance_ms"] = 123456.0
    second = calibration.publish_calibration(changed, tmp_path / "second.json")
    assert first.model_dump() == second.model_dump()
    _, evidence = evaluation.load_calibration(tmp_path / "first.json")
    assert len(evidence.scenario_ids) == 108
    assert len(evidence.scenario_repetitions) == 108
    assert sum(evidence.scenario_repetitions) == 612
    assert first.training_row_count == 92
    assert first.training_row_count == sum(
        r["changed_samples"] > 0 and r["method"] == "postgres_full" for r in rows
    )
    assert all(row.changed_samples > 0 for row in evidence.observations)


def test_publication_cannot_bypass_manifest_with_compact_or_missing_noop_rows(tmp_path):
    with pytest.raises(ValidationFailed, match="calibration_publication_failed"):
        calibration.publish_calibration(observations(), tmp_path / "compact.json")
    rows = benchmark_rows(strategy.CALIBRATION_SEEDS)
    rows = [row for row in rows if row["selected_strategy"] != "NO_OP"]
    with pytest.raises(ValidationFailed, match="calibration_publication_failed"):
        calibration.publish_calibration(rows, tmp_path / "missing-noop.json")
    assert list(tmp_path.iterdir()) == []


def test_normative_evaluation_rejects_omitted_whole_scenario(tmp_path):
    path = tmp_path / "result.json"
    policy = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    rows = benchmark_rows(strategy.HELDOUT_SEEDS, policy)
    missing = rows[0]["scenario_hash"]
    with pytest.raises(ValidationFailed, match="invalid_benchmark_rows"):
        evaluation.evaluate_policy(path, [r for r in rows if r["scenario_hash"] != missing])


def test_fit_rejects_noop_and_insufficient_algorithm_observations():
    rows = observations()
    rows[0]["changed_samples"] = rows[0]["dirty_entities"] = 0
    rows[0]["dirty_ratio"] = 0
    with pytest.raises(ValidationFailed, match="invalid_calibration_rows"):
        strategy.fit_policy(rows)
    with pytest.raises(ValidationFailed, match="invalid_calibration_rows"):
        strategy.fit_policy([observations()[0], observations()[8]])


def test_reported_policy_hash_is_exact_verified_file_hash(tmp_path):
    path = tmp_path / "result.json"
    policy = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    result = evaluation.evaluate_policy(path, benchmark_rows(strategy.HELDOUT_SEEDS, policy))
    directory = artifact_dir(tmp_path / "result-artifacts", "provenance_policy", policy.id)
    manifest = json.loads((directory / "manifest.json").read_text())
    entry = next(entry for entry in manifest["files"] if entry["name"] == "policy.json")
    assert result.policy_sha256 == entry["sha256"] == sha256_file(directory / "policy.json")


@pytest.mark.parametrize("field", ["policy_id", "policy_sha256"])
def test_heldout_rejects_mismatched_adaptive_policy_identity(tmp_path, field):
    path = tmp_path / "result.json"
    policy = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    rows = benchmark_rows(strategy.HELDOUT_SEEDS, policy)
    for row in rows:
        if row["method"] == "postgres_adaptive":
            row[field] = "wrong-policy" if field == "policy_id" else "b" * 64
    with pytest.raises(ValidationFailed, match="policy_decision_mismatch"):
        evaluation.evaluate_policy(path, rows)


def publish_generic_unit_evidence(path, evidence):
    """Use the generic artifact API to demonstrate a valid hash is not a normative study."""
    policy = strategy.fit_policy(evidence)
    root = path.parent / (path.stem + "-artifacts")
    source = root / "inputs" / "calibration.json"
    source.parent.mkdir(parents=True)
    source.write_text(strategy.calibration_text(evidence), encoding="utf-8", newline="\n")
    strategy.write_policy_artifact(root, policy, source)
    path.write_text(
        json.dumps(
            {
                "kind": "postgres-provenance-calibration-v1",
                "policy": policy.model_dump(mode="json"),
                "calibration": evidence.model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    assert strategy.load_policy_artifact(root, policy.id) == policy
    return policy


def test_normative_load_rejects_trimmed_but_valid_generic_policy(tmp_path):
    path = tmp_path / "complete.json"
    calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    _, evidence = evaluation.load_calibration(path)
    retained = tuple(
        sorted(
            [
                row
                for seed in strategy.CALIBRATION_SEEDS
                for row in [row for row in evidence.observations if row.seed == seed][:3]
            ],
            key=lambda row: row.scenario_hash,
        )
    )
    trimmed = evidence.model_copy(update={"observations": retained})
    path = tmp_path / "trimmed.json"
    policy = publish_generic_unit_evidence(path, trimmed)
    assert policy.training_row_count == 6
    with pytest.raises(ValidationFailed, match="invalid_calibration_artifact"):
        evaluation.load_calibration(path)
    with pytest.raises(ValidationFailed, match="invalid_calibration_artifact"):
        evaluation.evaluate_policy(path, benchmark_rows(strategy.HELDOUT_SEEDS, policy))


def test_publication_rejects_nonzero_scenario_falsified_as_noop(tmp_path):
    rows = benchmark_rows(strategy.CALIBRATION_SEEDS)
    identity = next(row["scenario_hash"] for row in rows if row["changed_samples"] > 0)
    for row in rows:
        if row["scenario_hash"] != identity:
            continue
        row.update(
            changed_samples=0,
            dirty_entities=0,
            dirty_ratio=0.0,
            realized_change_ratio=0.0,
            total_changes=row["historical_changes"],
            selected_strategy="NO_OP",
            strategy_reason="verified_zero_semantic_changes",
            throughput_samples_per_second=0.0,
        )
        for sample in row["samples"]:
            sample.update(
                dirty_entities=0,
                selected_strategy="NO_OP",
                strategy_reason="verified_zero_semantic_changes",
            )
    with pytest.raises(ValidationFailed, match="calibration_publication_failed"):
        calibration.publish_calibration(rows, tmp_path / "falsified.json")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field", ["sample_entities", "changed_samples"])
@pytest.mark.parametrize("seeds", [strategy.CALIBRATION_SEEDS, strategy.HELDOUT_SEEDS])
def test_scenario_counts_cannot_be_self_consistently_falsified(field, seeds):
    policy = strategy.fit_policy(observations()) if seeds == strategy.HELDOUT_SEEDS else None
    rows = benchmark_rows(seeds, policy)
    identity = next(row["scenario_hash"] for row in rows if row["changed_samples"] > 0)
    for row in rows:
        if row["scenario_hash"] == identity:
            row[field] += 1
            row["total_changes"] = row["historical_changes"] + row["changed_samples"]
            row["realized_change_ratio"] = row["changed_samples"] / row["sample_entities"]
    with pytest.raises(ValidationFailed, match="invalid_benchmark_rows"):
        evaluation.validate_rows(
            rows,
            seeds=seeds,
            methods=evaluation.EVALUATION_METHODS if policy else evaluation.FIXED_METHODS,
        )


@pytest.mark.parametrize("field", ["workload_hash", "seed", "changed_samples"])
def test_normative_loader_rejects_republished_mismatched_fit_associations(tmp_path, field):
    path = tmp_path / "complete.json"
    calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    _, evidence = evaluation.load_calibration(path)
    changed = list(evidence.observations)
    row = changed[0]
    value = {
        "workload_hash": "c" * 64,
        "seed": next(seed for seed in strategy.CALIBRATION_SEEDS if seed != row.seed),
        "changed_samples": row.changed_samples + 1,
    }[field]
    changed[0] = row.model_copy(update={field: value})
    evidence = evidence.model_copy(update={"observations": tuple(changed)})
    path = tmp_path / "reassociated.json"
    publish_generic_unit_evidence(path, evidence)
    with pytest.raises(ValidationFailed, match="invalid_calibration_artifact"):
        evaluation.load_calibration(path)


def test_publication_checks_exact_fit_identity_set_before_claim(tmp_path, monkeypatch):
    rows = benchmark_rows(strategy.CALIBRATION_SEEDS)
    projected = evaluation.paired_observations(rows)
    trimmed = [
        row
        for seed in strategy.CALIBRATION_SEEDS
        for row in [row for row in projected if row["seed"] == seed][:3]
    ]
    monkeypatch.setattr(calibration, "paired_observations", lambda rows: trimmed)
    with pytest.raises(ValidationFailed, match="calibration_publication_failed"):
        calibration.publish_calibration(rows, tmp_path / "trimmed.json")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("mutation", ["missing", "swapped"])
def test_normative_load_requires_pinned_workload_associations(tmp_path, mutation):
    path = tmp_path / "complete.json"
    calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    _, evidence = evaluation.load_calibration(path)
    hashes = list(evidence.scenario_workload_hashes)
    positions = [
        evidence.scenario_hashes.index(row.scenario_hash) for row in evidence.observations[:2]
    ]
    hashes[positions[0]], hashes[positions[1]] = hashes[positions[1]], hashes[positions[0]]
    evidence = evidence.model_copy(
        update={"scenario_workload_hashes": None if mutation == "missing" else tuple(hashes)}
    )
    path = tmp_path / "generic.json"
    publish_generic_unit_evidence(path, evidence)
    with pytest.raises(ValidationFailed, match="invalid_calibration_artifact"):
        evaluation.load_calibration(path)


@pytest.mark.parametrize("mutation", ["secret", "nested", "duplicate", "short"])
def test_new_workload_hash_evidence_field_is_strict_and_secret_safe(tmp_path, mutation):
    evidence = strategy.calibration_evidence(observations())
    payload = evidence.model_dump(mode="json")
    hashes = [row["workload_hash"] for row in payload["observations"]]
    if mutation == "secret":
        hashes[0] = "postgresql://PRIVATE_MARKER"
    elif mutation == "nested":
        hashes[0] = {"password": "PRIVATE_MARKER"}
    elif mutation == "duplicate":
        hashes[0] = hashes[1]
    else:
        hashes.pop()
    payload["scenario_workload_hashes"] = hashes
    source = tmp_path / "untrusted.json"
    source.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    policy = strategy.fit_policy(observations()).model_copy(
        update={"calibration_sha256": sha256_file(source)}
    )
    with pytest.raises(ValidationFailed) as error:
        strategy.write_policy_artifact(tmp_path / "data", policy, source)
    assert "PRIVATE_MARKER" not in "".join(traceback.format_exception(error.value))
    assert not (tmp_path / "data" / "artifacts").exists()


def test_exact_eligible_set_is_derived_without_building_fixtures(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("deriving a manifest must not build any fixture")

    monkeypatch.setattr(evaluation, "build_scenario", forbidden)
    scenarios = evaluation.expected_scenarios(strategy.CALIBRATION_SEEDS)
    eligible = [
        scenario for scenario in scenarios.values() if evaluation.scenario_counts(scenario)[1]
    ]
    assert len(scenarios) == 108 and len(eligible) == 92
    assert evaluation.scenario_counts(Scenario(1000, 0.001, "chain", 20260913)) == (326, 0)
    assert evaluation.scenario_counts(Scenario(1000000, 1.0, "chain", 20260913)) == (333326, 333326)


def test_heldout_cannot_reuse_excluded_calibration_noop_workload(tmp_path):
    path = tmp_path / "complete.json"
    policy = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    _, evidence = evaluation.load_calibration(path)
    fitted = {row.workload_hash for row in evidence.observations}
    excluded = set(evidence.scenario_workload_hashes) - fitted
    assert len(excluded) == 16
    leaked_hash = sorted(excluded)[0]
    heldout = benchmark_rows(strategy.HELDOUT_SEEDS, policy)
    identity = heldout[0]["scenario_hash"]
    group = [row for row in heldout if row["scenario_hash"] == identity]
    assert len(group) == 3
    assert identity not in evidence.scenario_hashes
    assert group[0]["scenario_id"] not in evidence.scenario_ids
    for row in group:
        row["workload_hash"] = leaked_hash
    # This remains a complete, internally matched held-out matrix. Only cross-split
    # workload leakage invalidates it; it must never report overlap_count=0/pass.
    assert (
        len(
            evaluation.validate_rows(
                heldout, seeds=strategy.HELDOUT_SEEDS, methods=evaluation.EVALUATION_METHODS
            )
        )
        == 108
    )
    with pytest.raises(ValidationFailed, match="workload_leakage") as error:
        evaluation.evaluate_policy(path, heldout)
    assert error.value.fields["overlap_count"] == 1


def test_every_runtime_environment_key_is_accepted_by_the_row_model(tmp_path):
    """Producer/consumer contract: whatever runtime_environment() emits must validate.

    The live calibration of 2026-09-16 collected all 1,224 measurements and then failed at
    publication because runtime_environment() had grown ``system_release`` and
    ``system_version`` while the strict ``_Environment`` model had not. Synthetic fixtures
    never exercised the real producer, so nothing offline could catch it. This does.
    """
    from performance.provenance import adaptive_benchmark as bench

    emitted = bench.runtime_environment(tmp_path)
    postgres_keys = {
        "postgresql_major": 17,
        "postgresql_version": 170011,
        "postgresql_server_version": "17.11",
        "deployment_kind": "native_portable",
        "image_digest": None,
        "image_digest_source": "not_applicable",
        "operator_declared_image_digest": None,
        "operator_declared_image_digest_source": None,
        "environment_fingerprint": "a" * 64,
        "backend_schema_version": 1,
    }
    declared = set(evaluation._Environment.model_fields)
    assert set(emitted) | set(postgres_keys) == declared, {
        "emitted_but_undeclared": sorted((set(emitted) | set(postgres_keys)) - declared),
        "declared_but_never_emitted": sorted(declared - set(emitted) - set(postgres_keys)),
    }
    evaluation._Environment.model_validate({**emitted, **postgres_keys})


def publish_v1_and_v2(tmp_path, band="relative"):
    """A unit v1 calibration document, a unit band comparison naming ``band``, and the v2
    policy document published from them; all synthetic."""
    v1_path = tmp_path / "unit-calibration.json"
    v1 = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), v1_path)
    comparison = tmp_path / "unit-band-comparison.json"
    comparison.write_text(
        json.dumps(
            {
                "kind": "postgres-provenance-band-comparison-v1",
                "winner": band,
                "calibration_sha256": v1.calibration_sha256,
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    v2_path = tmp_path / "unit-policy-v2.json"
    v2 = publish_v2.publish_policy_v2(v1_path, comparison, v2_path)
    return v1_path, v1, v2_path, v2


@pytest.mark.parametrize("band", ["relative", "stratified_edges"])
def test_policy_v2_is_published_from_the_v1_calibration_and_loads(tmp_path, band):
    v1_path, v1, v2_path, v2 = publish_v1_and_v2(tmp_path, band)
    assert v2.policy_version == strategy.POLICY_VERSION_V2
    assert v2.band.kind == band
    assert (v2.incremental_model, v2.full_model) == (v1.incremental_model, v1.full_model)
    assert v2.id == "postgres-adaptive-v2-" + v1.calibration_sha256[:12]
    loaded, evidence = evaluation.load_calibration(v2_path)
    assert loaded == v2
    assert strategy.calibration_text(evidence) == strategy.calibration_text(
        evaluation.load_calibration(v1_path)[1]
    )
    document = json.loads(v2_path.read_text(encoding="utf-8"))
    assert document["kind"] == "postgres-provenance-policy-v2"
    assert document["band_comparison"]["path"] == "unit-band-comparison.json"
    assert evaluation.policy_file_sha256(v2_path, v2) == strategy._policy_sha256(v2)


def test_policy_v2_document_must_match_its_band_comparison(tmp_path):
    _, _, v2_path, _ = publish_v1_and_v2(tmp_path)
    comparison = tmp_path / "unit-band-comparison.json"
    comparison.write_text(comparison.read_text().replace("relative", "stratified_edges"))
    with pytest.raises(ValidationFailed, match="invalid_calibration_artifact"):
        evaluation.load_calibration(v2_path)


def test_publish_policy_v2_refuses_a_comparison_of_another_calibration(tmp_path):
    v1_path = tmp_path / "unit-calibration.json"
    calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), v1_path)
    comparison = tmp_path / "unit-band-comparison.json"
    comparison.write_text(
        json.dumps(
            {
                "kind": "postgres-provenance-band-comparison-v1",
                "winner": "relative",
                "calibration_sha256": "e" * 64,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="invalid_band_comparison"):
        publish_v2.publish_policy_v2(v1_path, comparison, tmp_path / "unit-policy-v2.json")


def test_a_v1_calibration_document_cannot_carry_a_v2_policy(tmp_path):
    v1_path, _, _, v2 = publish_v1_and_v2(tmp_path)
    document = json.loads(v1_path.read_text(encoding="utf-8"))
    document["policy"] = v2.model_dump(mode="json")
    v1_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValidationFailed, match="invalid_calibration_artifact"):
        evaluation.load_calibration(v1_path)


def test_publish_policy_v2_accepts_relative_paths(tmp_path, monkeypatch):
    v1_path, _, _, _ = publish_v1_and_v2(tmp_path)
    monkeypatch.chdir(tmp_path)
    policy = publish_v2.publish_policy_v2(
        Path(v1_path.name), Path("unit-band-comparison.json"), Path("unit-policy-v2-rel.json")
    )
    assert evaluation.load_calibration("unit-policy-v2-rel.json")[0] == policy


def test_prepare_workload_installs_the_comparison_policy_beside_the_primary(tmp_path):
    _, v1, _, v2 = publish_v1_and_v2(tmp_path)
    evidence = evaluation.load_calibration(tmp_path / "unit-policy-v2.json")[1]
    workload = build_scenario(tmp_path / "fixture", Scenario(40, 0.5, "chain", 20261101))
    prepared = evaluation.prepare_workload(workload, v2, evidence, (v1,))
    assert strategy.load_policy_artifact(prepared.data, v2.id) == v2
    assert strategy.load_policy_artifact(prepared.data, v1.id) == v1
    data, configs = prepared.clone(tmp_path / "clone")
    prepared.publish(data)
    assert build_graph(data, configs).normalized() == prepared.expected.normalized()


def test_prepare_workload_refuses_a_comparison_of_another_calibration(tmp_path):
    _, v1, _, v2 = publish_v1_and_v2(tmp_path)
    evidence = evaluation.load_calibration(tmp_path / "unit-policy-v2.json")[1]
    other = v1.model_copy(update={"calibration_sha256": "e" * 64})
    workload = build_scenario(tmp_path / "fixture", Scenario(40, 0.5, "chain", 20261101))
    with pytest.raises(ValidationFailed, match="invalid_policy_workload_oracle"):
        evaluation.prepare_workload(workload, v2, evidence, (other,))


def test_collect_rows_passes_the_comparison_to_the_isolated_runner(tmp_path, monkeypatch):
    observed = {}
    monkeypatch.setattr(evaluation, "policy_file_sha256", lambda path, value: value.sha)

    def isolated(root, scenarios, **kwargs):
        observed.update(kwargs)
        return []

    monkeypatch.setattr(evaluation.benchmark, "run_matrix_isolated", isolated)
    primary = SimpleNamespace(sha="a" * 64)
    comparison = SimpleNamespace(sha="b" * 64)
    evaluation.collect_rows(
        tmp_path,
        [],
        methods=evaluation.HELDOUT_V2_METHODS,
        pg_runtime="pg",
        policy=primary,
        policy_from=tmp_path / "v2.json",
        comparison=comparison,
        comparison_from=tmp_path / "v1.json",
        isolated=True,
    )
    assert observed["comparison_from"] == tmp_path / "v1.json"
    assert observed["comparison_sha256"] == "b" * 64
    assert observed["policy_sha256"] == "a" * 64


def _slow_v1(rows, decade=None):
    """Make every non-NO_OP v1 adaptive row three times slower than the fixed methods; only
    those of one size decade when ``decade`` is given."""
    for row in rows:
        if (
            row["method"] == "postgres_adaptive_v1"
            and row["selected_strategy"] != "NO_OP"
            and (decade is None or edges_decade(row["total_edges"]) == decade)
        ):
            row["maintenance_p50_ms"] = row["maintenance_p95_ms"] = 30.0
            for sample in row["samples"]:
                sample["maintenance_ms"] = 30.0
            row["throughput_samples_per_second"] = row["changed_samples"] / 0.03
    return rows


def test_heldout_v2_rows_validate_with_four_methods_on_the_new_seeds(tmp_path):
    _, v1, _, v2 = publish_v1_and_v2(tmp_path)
    rows = benchmark_rows(strategy.HELDOUT_V2_SEEDS, v2, v1)
    groups = evaluation.validate_rows(
        rows, seeds=strategy.HELDOUT_V2_SEEDS, methods=evaluation.HELDOUT_V2_METHODS
    )
    assert len(groups) == 108
    assert all(set(group) == set(evaluation.HELDOUT_V2_METHODS) for group in groups.values())


def test_evaluate_policies_reports_both_and_tests_h1(tmp_path):
    v1_path, v1, v2_path, v2 = publish_v1_and_v2(tmp_path)
    rows = _slow_v1(benchmark_rows(strategy.HELDOUT_V2_SEEDS, v2, v1))
    result = evaluation.evaluate_policies(v2_path, v1_path, rows)
    assert (result.v2.policy_id, result.v1.policy_id) == (v2.id, v1.id)
    assert result.v2.performance_pass is True
    assert result.v1.performance_pass is False
    assert result.v2.scenarios_over == 0
    assert result.v1.scenarios_over == result.v1.non_no_op_scenarios > 0
    assert result.h1 is True and result.h1a is True
    assert set(result.v2.by_decade) == {"3"}
    assert result.seeds == list(strategy.HELDOUT_V2_SEEDS)


def test_evaluate_policies_splits_both_policies_by_size_decade(tmp_path):
    """v1 is slow only on graphs of 10^4 edges: H1 holds overall, H1a (decade 3) does not."""
    v1_path, v1, v2_path, v2 = publish_v1_and_v2(tmp_path)
    by_scale = benchmark_rows(
        strategy.HELDOUT_V2_SEEDS, v2, v1, total_edges=lambda scenario: 2 * scenario.entities
    )
    rows = _slow_v1(by_scale, decade=4)
    result = evaluation.evaluate_policies(v2_path, v1_path, list(reversed(rows)))
    no_op = {
        "3": 8,
        "4": 4,
        "5": 4,
    }  # change ratio 0 (and 0.001 of 1,000), two topologies, two seeds
    for evaluated, method in (
        (result.v2, "postgres_adaptive"),
        (result.v1, "postgres_adaptive_v1"),
    ):
        assert list(evaluated.by_decade) == ["3", "4", "5"]
        for decade, cell in evaluated.by_decade.items():
            decade_rows = [
                row
                for row in rows
                if row["method"] == method and str(edges_decade(row["total_edges"])) == decade
            ]
            selected = {}
            for row in decade_rows:
                selected[row["selected_strategy"]] = selected.get(row["selected_strategy"], 0) + 1
            assert cell["selected"] == selected
            assert cell["selected"]["NO_OP"] == no_op[decade]
            assert sum(cell["selected"].values()) == 36
            assert cell["scenarios"] == 36 - no_op[decade]
    assert result.v2.non_no_op_scenarios == result.v1.non_no_op_scenarios == 92
    assert result.v2.scenarios_over == 0 and result.v2.worst_ratio == 1.0
    assert result.v1.scenarios_over == 32 and result.v1.worst_ratio == 3.0
    for decade in ("3", "4", "5"):
        assert result.v2.by_decade[decade]["over"] == 0
        assert result.v2.by_decade[decade]["worst_ratio"] == 1.0
    assert [result.v1.by_decade[d]["over"] for d in ("3", "4", "5")] == [0, 32, 0]
    assert [result.v1.by_decade[d]["worst_ratio"] for d in ("3", "4", "5")] == [1.0, 3.0, 1.0]
    assert result.h1 is True
    assert result.h1a is False


def test_evaluate_policies_with_equal_policies_does_not_claim_h1(tmp_path):
    v1_path, v1, v2_path, v2 = publish_v1_and_v2(tmp_path)
    result = evaluation.evaluate_policies(
        v2_path, v1_path, benchmark_rows(strategy.HELDOUT_V2_SEEDS, v2, v1)
    )
    assert result.v2.scenarios_over == result.v1.scenarios_over == 0
    assert result.h1 is False and result.h1a is False


def test_evaluate_policies_refuses_a_swapped_pair(tmp_path):
    v1_path, v1, v2_path, v2 = publish_v1_and_v2(tmp_path)
    rows = benchmark_rows(strategy.HELDOUT_V2_SEEDS, v2, v1)
    with pytest.raises(ValidationFailed, match="invalid_policy_pair"):
        evaluation.evaluate_policies(v1_path, v2_path, rows)


def test_evaluate_policies_rejects_a_v1_row_decided_by_v2(tmp_path):
    v1_path, v1, v2_path, v2 = publish_v1_and_v2(tmp_path)
    rows = benchmark_rows(strategy.HELDOUT_V2_SEEDS, v2, v1)
    for row in rows:
        if row["method"] == "postgres_adaptive_v1":
            row["policy_id"] = v2.id
    with pytest.raises(ValidationFailed, match="policy_decision_mismatch"):
        evaluation.evaluate_policies(v2_path, v1_path, rows)


def test_heldout_v2_cli_publishes_both_policies(tmp_path, monkeypatch, capsys):
    v1_path, v1, v2_path, v2 = publish_v1_and_v2(tmp_path)
    rows = _slow_v1(benchmark_rows(strategy.HELDOUT_V2_SEEDS, v2, v1))
    runtime = object()
    monkeypatch.setattr(evaluation.benchmark, "postgres_preflight", lambda: runtime)
    checked = _accepting_environment(monkeypatch)

    def collect(root, scenarios, **kwargs):
        assert {s.seed for s in scenarios} == set(strategy.HELDOUT_V2_SEEDS)
        assert kwargs["methods"] == evaluation.HELDOUT_V2_METHODS
        assert (kwargs["policy"], kwargs["comparison"]) == (v2, v1)
        assert (kwargs["policy_from"], kwargs["comparison_from"]) == (v2_path, v1_path)
        assert kwargs["isolated"] is True
        assert checked == [(runtime, (v2, v1))]  # both environments are checked first
        return rows

    monkeypatch.setattr(evaluation, "collect_rows", collect)
    output = tmp_path / "unit-heldout-v2.json"
    argv = ["--policy-from", str(v2_path), "--comparison-policy-from", str(v1_path)]
    assert evaluation.main([*argv, "--output", str(output)]) == 0
    document = json.loads(output.read_text())
    assert document["kind"] == "postgres-provenance-heldout-v2"
    assert document["evaluation"]["h1"] is True
    assert "status=OK h1=true h1a=true" in capsys.readouterr().err


def _other_calibration(tmp_path):
    """A v1 calibration fitted from different synthetic latencies, so its evidence differs."""
    rows = benchmark_rows(strategy.CALIBRATION_SEEDS)
    for row in rows:
        if row["method"] == "postgres_full":
            row["maintenance_p50_ms"] = row["maintenance_p95_ms"] = 25.0
            for sample in row["samples"]:
                sample["maintenance_ms"] = 25.0
    path = tmp_path / "other" / "unit-calibration.json"
    path.parent.mkdir()
    calibration.publish_calibration(rows, path)
    return path


@pytest.mark.parametrize("case", ["v2_alone", "swapped", "both_v2", "other_calibration"])
def test_heldout_cli_refuses_a_wrong_policy_pair_before_measuring(
    tmp_path, monkeypatch, capsys, case
):
    v1_path, _, v2_path, _ = publish_v1_and_v2(tmp_path)
    argv = {
        "v2_alone": ["--policy-from", str(v2_path)],
        "swapped": ["--policy-from", str(v1_path), "--comparison-policy-from", str(v2_path)],
        "both_v2": ["--policy-from", str(v2_path), "--comparison-policy-from", str(v2_path)],
        "other_calibration": [
            "--policy-from",
            str(v2_path),
            "--comparison-policy-from",
            str(_other_calibration(tmp_path)),
        ],
    }[case]
    calls = []
    monkeypatch.setattr(
        evaluation.benchmark, "postgres_preflight", lambda: calls.append("preflight")
    )
    monkeypatch.setattr(evaluation, "collect_rows", lambda *a, **k: calls.append("collect"))
    output = tmp_path / "unit-heldout.json"
    assert evaluation.main([*argv, "--output", str(output)]) == 1
    assert calls == []
    assert not output.exists()
    assert "status=FAIL" in capsys.readouterr().err


def _stub_scenario_run(monkeypatch):
    """Replace the live pieces of collect_rows; return what prepare_workload and the policy
    hasher were called with, and which policy id each method ran under."""
    prepared, hashed, ran = [], [], {}
    monkeypatch.setattr(evaluation, "build_scenario", lambda path, scenario: "workload")
    monkeypatch.setattr(
        evaluation,
        "prepare_workload",
        lambda *args, **kwargs: prepared.append((args, kwargs)) or args[0],
    )
    monkeypatch.setattr(
        evaluation, "_policy_sha256", lambda value: hashed.append(value) or "a" * 64
    )

    def run_method(workload, method, **kwargs):
        ran[method] = kwargs["policy_id"]
        return SimpleNamespace(to_dict=lambda: {"method": method})

    monkeypatch.setattr(evaluation.benchmark, "run_method", run_method)
    return prepared, hashed, ran


def test_collect_rows_without_a_comparison_keeps_the_three_argument_call_shape(
    tmp_path, monkeypatch
):
    prepared, hashed, _ = _stub_scenario_run(monkeypatch)
    policy = SimpleNamespace(id="policy")
    scenarios = [Scenario(40, 0.5, "chain", seed) for seed in strategy.HELDOUT_SEEDS]
    rows = evaluation.collect_rows(
        tmp_path,
        scenarios,
        methods=evaluation.EVALUATION_METHODS,
        pg_runtime="pg",
        policy=policy,
        evidence="evidence",
    )
    assert len(rows) == len(scenarios) * len(evaluation.EVALUATION_METHODS)
    assert prepared == [(("workload", policy, "evidence"), {})] * len(scenarios)
    assert hashed == [policy]


def test_collect_rows_with_a_comparison_hashes_each_policy_once(tmp_path, monkeypatch):
    prepared, hashed, ran = _stub_scenario_run(monkeypatch)
    policy, comparison = SimpleNamespace(id="v2"), SimpleNamespace(id="v1")
    scenarios = [Scenario(40, 0.5, "chain", seed) for seed in strategy.HELDOUT_V2_SEEDS]
    evaluation.collect_rows(
        tmp_path,
        scenarios,
        methods=evaluation.HELDOUT_V2_METHODS,
        pg_runtime="pg",
        policy=policy,
        evidence="evidence",
        comparison=comparison,
    )
    assert prepared == [(("workload", policy, "evidence", (comparison,)), {})] * len(scenarios)
    assert hashed == [policy, comparison]
    assert ran == {
        "postgres_full": None,
        "postgres_incremental": None,
        "postgres_adaptive": "v2",
        "postgres_adaptive_v1": "v1",
    }


def _accepting_environment(monkeypatch):
    """A policy-environment check that always accepts; returns the calls it received."""
    calls = []
    monkeypatch.setattr(
        evaluation.benchmark,
        "require_policy_environment",
        lambda pg_runtime, *policies: calls.append((pg_runtime, policies)),
    )
    return calls


def _refusing_environment(monkeypatch):
    """A policy-environment check that always refuses; returns the calls it received."""
    calls = []

    def refuse(pg_runtime, *policies):
        calls.append((pg_runtime, policies))
        raise ValidationFailed("incompatible_policy: environment fingerprint")

    monkeypatch.setattr(evaluation.benchmark, "require_policy_environment", refuse)
    return calls


def test_heldout_v1_cli_checks_the_policy_environment_before_measuring(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "unit-policy.json"
    policy = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    runtime = object()
    monkeypatch.setattr(evaluation.benchmark, "postgres_preflight", lambda: runtime)
    calls = _refusing_environment(monkeypatch)
    monkeypatch.setattr(
        evaluation, "collect_rows", lambda *a, **k: pytest.fail("measured despite the gate")
    )
    output = tmp_path / "unit-heldout.json"
    assert evaluation.main(["--policy-from", str(path), "--output", str(output)]) == 1
    assert calls == [(runtime, (policy,))]
    assert not output.exists() and not (tmp_path / "unit-heldout-work").exists()
    assert "status=FAIL" in capsys.readouterr().err


def test_heldout_v2_cli_checks_both_policy_environments_before_measuring(
    tmp_path, monkeypatch, capsys
):
    v1_path, v1, v2_path, v2 = publish_v1_and_v2(tmp_path)
    runtime = object()
    monkeypatch.setattr(evaluation.benchmark, "postgres_preflight", lambda: runtime)
    calls = _refusing_environment(monkeypatch)
    monkeypatch.setattr(
        evaluation, "collect_rows", lambda *a, **k: pytest.fail("measured despite the gate")
    )
    output = tmp_path / "unit-heldout-v2.json"
    argv = ["--policy-from", str(v2_path), "--comparison-policy-from", str(v1_path)]
    assert evaluation.main([*argv, "--output", str(output)]) == 1
    assert calls == [(runtime, (v2, v1))]
    assert not output.exists() and not (tmp_path / "unit-heldout-v2-work").exists()
    assert "status=FAIL" in capsys.readouterr().err
