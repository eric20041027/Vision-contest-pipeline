"""Synthetic unit observations only; these are never benchmark result artifacts."""

import ast
import copy
import inspect
import json
import subprocess
import sys
import traceback

import pytest

from performance.provenance import calibrate_adaptive as calibration
from performance.provenance import evaluate_adaptive as evaluation
from performance.provenance.workloads import Scenario, build_scenario, scenario_matrix
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir
from vcp.provenance import strategy
from vcp.provenance.graph import build_graph
from vcp.provenance.index import graph_hash


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


def benchmark_rows(seeds, policy=None):
    """Clearly synthetic Task 10-shaped rows, never saved as research output."""
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
            total_edges=2000,
            historical_changes=600,
            head_count=1,
        )
        for method in evaluation.EVALUATION_METHODS if policy else evaluation.FIXED_METHODS:
            requested = {
                "postgres_full": "full",
                "postgres_incremental": "incremental",
                "postgres_adaptive": "auto",
            }[method]
            decision = strategy.select_strategy(
                requested, features, policy if requested == "auto" else None
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
                policy_id=policy.id if method == "postgres_adaptive" else None,
                policy_sha256=(
                    strategy._policy_sha256(policy) if method == "postgres_adaptive" else None
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
    monkeypatch.setattr(evaluation.benchmark, "postgres_preflight", lambda: object())

    def collect(root, scenarios, **kwargs):
        assert {s.seed for s in scenarios} == set(strategy.HELDOUT_SEEDS)
        assert kwargs["policy"] == policy
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
    assert len(evidence.scenario_ids) == 144
    assert len(evidence.scenario_repetitions) == 144
    assert sum(evidence.scenario_repetitions) == 720
    assert first.training_row_count == 124
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
    assert len(scenarios) == 144 and len(eligible) == 124
    assert evaluation.scenario_counts(Scenario(1000, 0.001, "chain", 20260913)) == (326, 0)
    assert evaluation.scenario_counts(Scenario(1000000, 1.0, "chain", 20260913)) == (333326, 333326)


def test_heldout_cannot_reuse_excluded_calibration_noop_workload(tmp_path):
    path = tmp_path / "complete.json"
    policy = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), path)
    _, evidence = evaluation.load_calibration(path)
    fitted = {row.workload_hash for row in evidence.observations}
    excluded = set(evidence.scenario_workload_hashes) - fitted
    assert len(excluded) == 20
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
        == 144
    )
    with pytest.raises(ValidationFailed, match="workload_leakage") as error:
        evaluation.evaluate_policy(path, heldout)
    assert error.value.fields["overlap_count"] == 1
