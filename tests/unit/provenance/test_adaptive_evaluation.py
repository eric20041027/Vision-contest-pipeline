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
    policy = calibration.publish_calibration(observations(), path)
    loaded, evidence = evaluation.load_calibration(path)
    assert loaded == policy
    assert len(evidence.observations) == policy.training_row_count
    with pytest.raises(ValidationFailed):
        calibration.publish_calibration(observations(), path)
    payload = json.loads(path.read_text())
    payload["policy"]["full_rmse_ms"] += 1
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    with pytest.raises(ValidationFailed, match="invalid_calibration_artifact"):
        evaluation.load_calibration(path)


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
    for seed in seeds:
        scenario = Scenario(1000, 0.1, "chain", seed)
        features = strategy.MaintenanceFeatures(
            changed_samples=10,
            dirty_entities=20,
            total_entities=1100,
            dirty_ratio=20 / 1100,
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
                storage_measurement=evaluation._STORAGE,
                requested_strategy=requested,
                selected_strategy=decision.selected_strategy.value,
                strategy_reason=decision.reason,
                policy_version=decision.policy_version,
                estimated_incremental_ms=decision.estimated_incremental_ms,
                estimated_full_ms=decision.estimated_full_ms,
                dirty_entities=20,
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
                scenario_id=scenario.scenario_id,
                scenario_hash=scenario.scenario_hash,
                workload_hash=f"{seed:064x}",
                track="scaled",
                seed=seed,
                topology="chain",
                entities=1000,
                change_ratio=0.1,
                realized_change_ratio=0.1,
                sample_entities=100,
                **features.model_dump(),
                total_changes=610,
                environment=dict(
                    system="Windows",
                    machine="AMD64",
                    python_version="3.12.10",
                    python_implementation="CPython",
                    cpu_count=8,
                    sqlite_version="3.50.0",
                    vcp_commit="a" * 40,
                    postgresql_major=17,
                    postgresql_version=170011,
                    environment_fingerprint="a" * 64,
                    backend_schema_version=1,
                ),
                status="ok",
                failure=None,
                samples=[sample],
                repetitions=1,
                parity_rate=1.0,
                explain_analyze=[{"Plan": {"Node Type": "ModifyTable"}}],
                instrumentation=(
                    "separate fresh state; first DML row per SQL shape; "
                    "rollback; excluded from timings"
                ),
                query_measurement="public status API; impact/explain include graph load as in CLI",
                warmup=(
                    "baseline rebuilt before each sample; no timed warmup; OS caches may be warm"
                ),
                throughput_samples_per_second=10 / (latency / 1000),
            )
            for operation in ("maintenance", "status", "impact", "explain"):
                row[operation + "_p50_ms"] = sample[operation + "_ms"]
                row[operation + "_p95_ms"] = sample[operation + "_ms"]
            rows.append(row)
    return rows


def test_task10_rows_fit_and_heldout_never_refits(tmp_path, monkeypatch):
    path = tmp_path / "result.json"
    rows = benchmark_rows(strategy.CALIBRATION_SEEDS)
    policy = calibration.publish_calibration(evaluation.paired_observations(rows), path)

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
    policy = calibration.publish_calibration(observations(), path)
    rows = benchmark_rows(strategy.HELDOUT_SEEDS, policy)
    for row in rows:
        if mutation == "environment":
            row["environment"]["environment_fingerprint"] = "b" * 64
        if row["method"] != "postgres_adaptive":
            continue
        if mutation in {"decision", "estimate"}:
            key = "strategy_reason" if mutation == "decision" else "estimated_full_ms"
            value = "fallback_policy_absent_full" if mutation == "decision" else 999.0
            row[key] = row["samples"][0][key] = value
        if mutation == "slow":
            row["samples"][0]["maintenance_ms"] = 100.0
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
    policy = calibration.publish_calibration(observations(), path)
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
    calibration.publish_calibration(observations(), path)
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
    calibration.publish_calibration(observations(), "relative.json")
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
    policy = calibration.publish_calibration(observations(), path)
    rows = benchmark_rows(strategy.HELDOUT_SEEDS, policy)
    for row in rows:
        latency = (
            100.0
            if row["seed"] == 20261002
            else (12.0 if row["method"] == "postgres_adaptive" else 10.0)
        )
        row["samples"][0]["maintenance_ms"] = latency
        row["maintenance_p50_ms"] = row["maintenance_p95_ms"] = latency
        row["throughput_samples_per_second"] = 10 / (latency / 1000)
    result = evaluation.evaluate_policy(path, rows)
    assert result.median_ratio == pytest.approx(56 / 55)
    assert result.p95_ratio == 1.0 and result.performance_pass
    assert not result.every_scenario_pass


def test_policy_json_must_match_exactly_before_model_normalization(tmp_path):
    path = tmp_path / "result.json"
    policy = calibration.publish_calibration(
        evaluation.paired_observations(benchmark_rows(strategy.CALIBRATION_SEEDS)), path
    )
    assert all(value == 0 for value in policy.full_model.coefficients.values())
    document = json.loads(path.read_text())
    document["policy"]["full_model"]["coefficients"]["total_entities"] = -1
    path.write_text(json.dumps(document), encoding="utf-8", newline="\n")
    with pytest.raises(ValidationFailed, match="invalid_calibration_artifact"):
        evaluation.load_calibration(path)


def test_heldout_executes_in_separate_process_without_fitting_import(tmp_path):
    path = tmp_path / "unit-policy.json"
    policy = calibration.publish_calibration(observations(), path)
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
    policy = calibration.publish_calibration(observations(), path)
    rows = benchmark_rows(strategy.HELDOUT_SEEDS, policy)
    if slow:
        for row in rows:
            if row["method"] == "postgres_adaptive":
                row["maintenance_p50_ms"] = row["maintenance_p95_ms"] = 100.0
                row["samples"][0]["maintenance_ms"] = 100.0
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


def test_real_task10_serialization_is_consumed_without_interface_changes(tmp_path):
    rows = []
    for seed in strategy.CALIBRATION_SEEDS:
        workload = build_scenario(tmp_path / str(seed), Scenario(40, 0.5, "chain", seed))
        for template in benchmark_rows((seed,)):
            sample = copy.deepcopy(template["samples"][0])
            sample["dirty_entities"] = 2
            result = evaluation.benchmark.ScenarioResult(
                template["method"],
                workload,
                template["environment"],
                samples=[sample],
                plans=template["explain_analyze"],
            )
            rows.append(result.to_dict())
    pairs = evaluation.paired_observations(rows)
    assert len(pairs) == 2
    assert {row["seed"] for row in pairs} == set(strategy.CALIBRATION_SEEDS)
