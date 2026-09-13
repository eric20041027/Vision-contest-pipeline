import json

import pytest
from pydantic import ValidationError

from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir
from vcp.provenance.backend import RequestedStrategy, SelectedStrategy
from vcp.provenance.strategy import (
    FULL_FEATURE_ORDER,
    INCREMENTAL_FEATURE_ORDER,
    AdaptivePolicy,
    CostModel,
    MaintenanceFeatures,
    load_policy_artifact,
    select_strategy,
    write_policy_artifact,
)


@pytest.fixture
def features():
    return MaintenanceFeatures(
        changed_samples=12,
        dirty_entities=40,
        total_entities=200,
        dirty_ratio=0.2,
        total_edges=500,
        historical_changes=80,
        head_count=3,
    )


def _policy(calibration_sha256: str, **updates):
    policy = AdaptivePolicy(
        backend_schema_version=1,
        postgresql_major=17,
        benchmark_schema_version=1,
        environment_fingerprint="env-fixture-v1",
        calibration_sha256=calibration_sha256,
        incremental_model=CostModel(
            feature_order=INCREMENTAL_FEATURE_ORDER,
            coefficients={name: 0.1 for name in INCREMENTAL_FEATURE_ORDER},
            intercept_ms=1.0,
        ),
        full_model=CostModel(
            feature_order=FULL_FEATURE_ORDER,
            coefficients={name: 0.5 for name in FULL_FEATURE_ORDER},
            intercept_ms=10.0,
        ),
        incremental_rmse_ms=2.0,
        full_rmse_ms=3.0,
        training_row_count=18,
    )
    return policy.model_copy(update=updates)


@pytest.fixture
def compatible_policy(tmp_path):
    calibration = tmp_path / "calibration-source.json"
    calibration.write_text(
        json.dumps({"scenario_ids": ["cal-1"], "scenario_hashes": ["a" * 64]}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return _policy(sha256_file(calibration))


def test_zero_change_is_topology_only_no_op(features):
    decision = select_strategy("auto", features.model_copy(update={"changed_samples": 0}), None)
    assert decision.selected_strategy == "NO_OP"
    assert decision.reason == "verified_zero_semantic_changes"


@pytest.mark.parametrize(
    ("requested", "selected", "reason"),
    [
        ("incremental", SelectedStrategy.INCREMENTAL, "requested_incremental"),
        ("full", SelectedStrategy.FULL, "requested_full"),
    ],
)
def test_requested_fixed_strategy_is_selected(features, requested, selected, reason):
    decision = select_strategy(requested, features, None)
    assert decision.requested_strategy == requested
    assert decision.selected_strategy is selected
    assert decision.reason == reason


def test_auto_uses_calibrated_noise_band(features, compatible_policy):
    decision = select_strategy("auto", features, compatible_policy)
    expected = (
        "INCREMENTAL"
        if (
            decision.estimated_incremental_ms + compatible_policy.incremental_rmse_ms
            < decision.estimated_full_ms - compatible_policy.full_rmse_ms
        )
        else "FULL"
    )
    assert decision.selected_strategy == expected


def test_auto_without_policy_falls_back_to_full(features):
    decision = select_strategy(RequestedStrategy.AUTO, features, None)
    assert decision.selected_strategy is SelectedStrategy.FULL
    assert decision.reason == "fallback_policy_absent_full"
    assert decision.estimated_incremental_ms is None
    assert decision.estimated_full_ms is None


def test_requested_values_are_exact(features):
    with pytest.raises(ValidationFailed, match="unsupported_strategy: AUTO"):
        select_strategy("AUTO", features, None)


def test_equal_noise_band_boundary_selects_full(features, compatible_policy):
    incremental = CostModel(
        feature_order=INCREMENTAL_FEATURE_ORDER,
        coefficients={name: 0.0 for name in reversed(INCREMENTAL_FEATURE_ORDER)},
        intercept_ms=7.0,
    )
    full = CostModel(
        feature_order=FULL_FEATURE_ORDER,
        coefficients={name: 0.0 for name in reversed(FULL_FEATURE_ORDER)},
        intercept_ms=12.0,
    )
    policy = compatible_policy.model_copy(
        update={
            "incremental_model": incremental,
            "full_model": full,
            "incremental_rmse_ms": 2.0,
            "full_rmse_ms": 3.0,
        }
    )
    decision = select_strategy("auto", features, policy)
    assert decision.estimated_incremental_ms + policy.incremental_rmse_ms == 9.0
    assert decision.estimated_full_ms - policy.full_rmse_ms == 9.0
    assert decision.selected_strategy is SelectedStrategy.FULL
    assert decision.reason == "calibrated_full_lower_or_uncertain_cost"
    assert tuple(incremental.coefficients) == INCREMENTAL_FEATURE_ORDER


def test_repeated_decisions_are_identical(features, compatible_policy):
    first = select_strategy("auto", features, compatible_policy)
    second = select_strategy("auto", features, compatible_policy)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_cost_model_clamps_negative_coefficients_intercept_and_result(features):
    model = CostModel(
        feature_order=("changed_samples",),
        coefficients={"changed_samples": -5.0},
        intercept_ms=-10.0,
    )
    assert model.coefficients == {"changed_samples": 0.0}
    assert model.intercept_ms == 0.0
    assert model.predict(features) == 0.0


def test_models_forbid_extra_fields(features, compatible_policy):
    with pytest.raises(ValidationError, match="extra"):
        MaintenanceFeatures(**features.model_dump(), surprise=1)
    with pytest.raises(ValidationError, match="extra"):
        AdaptivePolicy(**compatible_policy.model_dump(), surprise=1)


def _write_calibration(path):
    path.write_text(
        json.dumps(
            {
                "scenario_ids": ["cal-1", "cal-2"],
                "scenario_hashes": ["a" * 64, "b" * 64],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _publish(roots):
    calibration = roots.data / "inputs" / "calibration-result.json"
    calibration.parent.mkdir(parents=True)
    _write_calibration(calibration)
    policy = _policy(sha256_file(calibration))
    policy_id = write_policy_artifact(roots.data, policy, calibration)
    return calibration, policy, policy_id


def _load(roots, policy_id, **updates):
    compatibility = {
        "backend_schema_version": 1,
        "postgresql_major": 17,
        "benchmark_schema_version": 1,
        "environment_fingerprint": "env-fixture-v1",
    }
    compatibility.update(updates)
    return load_policy_artifact(roots.data, policy_id, **compatibility)


def test_policy_artifact_is_manifest_last_pinned_and_idempotent(roots):
    calibration, policy, policy_id = _publish(roots)
    expected = f"postgres-adaptive-v1-{sha256_file(calibration)[:12]}"
    assert policy_id == expected
    directory = artifact_dir(roots.data, "provenance_policy", policy_id)
    assert {path.name for path in directory.iterdir()} == {
        "spec.json",
        "policy.json",
        "calibration.json",
        "manifest.json",
    }
    assert _load(roots, policy_id) == policy
    assert write_policy_artifact(roots.data, policy, calibration) == policy_id


def test_conflicting_policy_for_same_calibration_fails_closed(roots):
    calibration, policy, _ = _publish(roots)
    conflicting = policy.model_copy(update={"full_rmse_ms": 99.0})
    with pytest.raises(IntegrityError, match="mismatch: provenance policy payload"):
        write_policy_artifact(roots.data, conflicting, calibration)


def test_missing_explicit_policy_fails_before_database_write(roots):
    with pytest.raises(ValidationFailed, match="not_found: provenance policy"):
        load_policy_artifact(roots.data, "missing")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend", "sqlite"),
        ("backend_schema_version", 2),
        ("postgresql_major", 16),
        ("benchmark_schema_version", 2),
        ("environment_fingerprint", "another-environment"),
    ],
)
def test_loader_rejects_incompatible_policy(roots, field, value):
    _, _, policy_id = _publish(roots)
    with pytest.raises(ValidationFailed, match="incompatible_policy"):
        _load(roots, policy_id, **{field: value})


def test_loader_rejects_manifest_mutation(roots):
    _, _, policy_id = _publish(roots)
    manifest_path = artifact_dir(roots.data, "provenance_policy", policy_id) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["spec"]["params"]["postgresql_major"] = "16"
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(IntegrityError, match="mismatch: provenance policy manifest"):
        _load(roots, policy_id)


def test_loader_rechecks_pinned_calibration_input(roots):
    calibration, _, policy_id = _publish(roots)
    calibration.write_text("{}\n", encoding="utf-8", newline="\n")
    with pytest.raises(IntegrityError, match="mismatch: provenance policy calibration input"):
        _load(roots, policy_id)
