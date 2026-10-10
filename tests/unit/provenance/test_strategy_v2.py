"""Adaptive policy v2 in strategy.py (spec 2026-10-09 §4)."""

import dataclasses
import json

import pytest
from pydantic import ValidationError

from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.provenance import strategy
from vcp.provenance.policy_bands import EdgesStratum, RelativeBand, StratifiedEdgesBand
from vcp.provenance.strategy import (
    FULL_FEATURE_ORDER,
    HELDOUT_SEEDS,
    HELDOUT_V2_SEEDS,
    INCREMENTAL_FEATURE_ORDER,
    POLICY_VERSION_V2,
    AdaptivePolicy,
    AdaptivePolicyV2,
    CalibrationObservation,
    CostModel,
    MaintenanceFeatures,
    _policy_spec,
    calibration_evidence,
    fit_cost_models,
    fit_policy,
    fit_policy_v2,
    load_policy_artifact,
    policy_from_payload,
    select_strategy,
    write_policy_artifact,
)


def _features(total_edges=1500):
    return MaintenanceFeatures(
        changed_samples=12,
        dirty_entities=40,
        total_entities=200,
        dirty_ratio=0.2,
        total_edges=total_edges,
        historical_changes=80,
        head_count=3,
    )


def _models(incremental_ms, full_ms):
    return (
        CostModel(
            feature_order=INCREMENTAL_FEATURE_ORDER,
            coefficients={name: 0.0 for name in INCREMENTAL_FEATURE_ORDER},
            intercept_ms=incremental_ms,
        ),
        CostModel(
            feature_order=FULL_FEATURE_ORDER,
            coefficients={name: 0.0 for name in FULL_FEATURE_ORDER},
            intercept_ms=full_ms,
        ),
    )


def _v2(band, calibration_sha256="c" * 64, incremental_ms=100.0, full_ms=150.0):
    incremental, full = _models(incremental_ms, full_ms)
    return AdaptivePolicyV2(
        backend_schema_version=1,
        postgresql_major=17,
        benchmark_schema_version=1,
        environment_fingerprint="env-fixture-v1",
        calibration_sha256=calibration_sha256,
        incremental_model=incremental,
        full_model=full,
        band=band,
        training_row_count=18,
    )


def _observations():
    """Synthetic paired rows over three size decades, both calibration seeds; multiplicative
    noise so both candidate bands fit."""
    rows = []
    for seed in (20260913, 20260914):
        for decade, edges in ((3, 1500), (4, 15_000), (5, 150_000)):
            for i in range(1, 7):
                total = edges + 7 * i
                changed = 10 * i
                noise = 1.1 if (i + seed) % 2 else 0.9
                rows.append(
                    CalibrationObservation(
                        scenario_id=f"unit-{seed}-{decade}-{i}",
                        scenario_hash=f"{seed}{decade}{i:02d}".rjust(64, "0"),
                        workload_hash=f"{seed}{decade}{i:02d}".rjust(64, "1"),
                        seed=seed,
                        changed_samples=changed,
                        dirty_entities=changed,
                        total_entities=total,
                        dirty_ratio=changed / total,
                        total_edges=total,
                        historical_changes=total // 2,
                        head_count=1,
                        incremental_p50_ms=(1.2 * changed + 0.12 * total) * noise,
                        full_p50_ms=(0.7 * total + 0.1 * (total // 2)) * noise,
                        environment_fingerprint="a" * 64,
                        backend_schema_version=1,
                        postgresql_major=17,
                        benchmark_schema_version=1,
                    )
                )
    return rows


def test_heldout_v2_seeds_are_new():
    assert HELDOUT_V2_SEEDS == (20261101, 20261102)
    assert not set(HELDOUT_V2_SEEDS) & {20260908, 20260909, 20260913, 20260914, *HELDOUT_SEEDS}


def test_v2_relative_band_decides_and_keeps_the_v1_reasons():
    policy = _v2(RelativeBand(incremental_relative_rmse=0.2, full_relative_rmse=0.1))
    decision = select_strategy("auto", _features(), policy)
    assert decision.selected_strategy.value == "INCREMENTAL"  # 120 < 135
    assert decision.reason == "calibrated_incremental_lower_confident_cost"
    assert decision.policy_version == POLICY_VERSION_V2
    assert (decision.estimated_incremental_ms, decision.estimated_full_ms) == (100.0, 150.0)
    wide = _v2(RelativeBand(incremental_relative_rmse=0.5, full_relative_rmse=0.0))
    decision = select_strategy("auto", _features(), wide)
    assert decision.selected_strategy.value == "FULL"  # 150 < 150 is false: a tie is FULL
    assert decision.reason == "calibrated_full_lower_or_uncertain_cost"


def test_v2_stratified_band_uses_the_features_size_decade():
    band = StratifiedEdgesBand(
        strata=(
            EdgesStratum(decade=3, incremental_rmse_ms=10.0, full_rmse_ms=20.0, observations=6),
            EdgesStratum(decade=5, incremental_rmse_ms=1000.0, full_rmse_ms=2000.0, observations=6),
        )
    )
    policy = _v2(band)
    assert select_strategy("auto", _features(1500), policy).selected_strategy.value == "INCREMENTAL"
    assert select_strategy("auto", _features(150_000), policy).selected_strategy.value == "FULL"


def test_fixed_requests_no_op_and_fallback_are_the_same_for_v2():
    policy = _v2(RelativeBand(incremental_relative_rmse=0.0, full_relative_rmse=0.0))
    assert select_strategy("full", _features(), policy).reason == "requested_full"
    assert select_strategy("incremental", _features(), policy).reason == "requested_incremental"
    zero = _features().model_copy(update={"changed_samples": 0, "dirty_entities": 0})
    assert select_strategy("auto", zero, policy).reason == "verified_zero_semantic_changes"


def test_v1_and_v2_payloads_do_not_mix():
    v2 = _v2(RelativeBand(incremental_relative_rmse=0.1, full_relative_rmse=0.1))
    payload = v2.model_dump(mode="json")
    with pytest.raises(ValidationError, match="extra"):
        AdaptivePolicyV2.model_validate({**payload, "incremental_rmse_ms": 1.0})
    v1_payload = {key: value for key, value in payload.items() if key != "band"}
    v1_payload.update(
        policy_version="postgres-adaptive-v1", incremental_rmse_ms=1.0, full_rmse_ms=1.0
    )
    with pytest.raises(ValidationError, match="extra"):
        AdaptivePolicy.model_validate({**v1_payload, "band": payload["band"]})
    assert isinstance(policy_from_payload(payload), AdaptivePolicyV2)
    assert isinstance(policy_from_payload(v1_payload), AdaptivePolicy)
    assert v2.id == "postgres-adaptive-v2-" + "c" * 12


@pytest.mark.parametrize(
    "calibration_sha256",
    ["", "c" * 63, "c" * 65, "C" * 64, "g" * 64, "c" * 63 + " ", "c" * 64 + "\n"],
    ids=["empty", "short", "long", "uppercase", "not-hex", "space", "newline"],
)
def test_v2_calibration_sha256_must_be_64_lowercase_hex_characters(calibration_sha256):
    band = RelativeBand(incremental_relative_rmse=0.1, full_relative_rmse=0.1)
    with pytest.raises(ValidationError, match="calibration_sha256 must be 64 lowercase hex"):
        _v2(band, calibration_sha256=calibration_sha256)
    payload = _v2(band).model_dump(mode="json")
    with pytest.raises(ValidationError, match="calibration_sha256 must be 64 lowercase hex"):
        policy_from_payload({**payload, "calibration_sha256": calibration_sha256})


def test_v2_refuses_swapped_cost_model_feature_orders():
    band = RelativeBand(incremental_relative_rmse=0.1, full_relative_rmse=0.1)
    incremental, full = _models(100.0, 150.0)
    good = _v2(band).model_dump(mode="python")
    assert good["incremental_model"] == incremental.model_dump(mode="python")
    # The incremental model carries the full model's feature order ...
    with pytest.raises(ValidationError, match="incremental model feature_order is incompatible"):
        AdaptivePolicyV2.model_validate({**good, "incremental_model": full})
    # ... and the full model carries the incremental model's.
    with pytest.raises(ValidationError, match="full model feature_order is incompatible"):
        AdaptivePolicyV2.model_validate({**good, "full_model": incremental})
    with pytest.raises(ValidationError, match="feature_order is incompatible"):
        AdaptivePolicyV2.model_validate(
            {**good, "incremental_model": full, "full_model": incremental}
        )


def test_fit_cost_models_and_fit_policy_v2_keep_the_v1_cost_models():
    rows = _observations()
    v1 = fit_policy(rows)
    # fit_policy fits the evidence's sorted rows; float sums depend on order, so fit the same
    incremental, full = fit_cost_models(calibration_evidence(rows).observations)
    assert (incremental.model, full.model) == (v1.incremental_model, v1.full_model)
    assert (incremental.rmse_ms, full.rmse_ms) == (v1.incremental_rmse_ms, v1.full_rmse_ms)
    for band in ("relative", "stratified_edges"):
        v2 = fit_policy_v2(rows, band=band)
        assert v2.band.kind == band
        assert (v2.incremental_model, v2.full_model) == (v1.incremental_model, v1.full_model)
        assert v2.calibration_sha256 == v1.calibration_sha256
        assert v2.training_row_count == len(rows)
    stratified = fit_policy_v2(rows, band="stratified_edges")
    assert [s.decade for s in stratified.band.strata] == [3, 4, 5]


def test_fit_policy_v2_rejects_an_unknown_band():
    with pytest.raises(ValidationFailed, match="unsupported_band"):
        fit_policy_v2(_observations(), band="absolute")


def test_fit_policy_v2_rejects_a_non_positive_estimate(monkeypatch):
    rows = _observations()
    incremental, full = fit_cost_models(calibration_evidence(rows).observations)
    zeroed = dataclasses.replace(incremental, predicted=(0.0, *incremental.predicted[1:]))
    monkeypatch.setattr(strategy, "fit_cost_models", lambda _rows: (zeroed, full))
    with pytest.raises(ValidationFailed, match="invalid_calibration_rows"):
        fit_policy_v2(rows, band="relative")


def _calibration(roots):
    path = roots.data / "inputs" / "calibration-result.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"scenario_ids": ["cal-1"], "scenario_hashes": ["a" * 64]}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _compatibility():
    return dict(
        backend_schema_version=1,
        postgresql_major=17,
        benchmark_schema_version=1,
        environment_fingerprint="env-fixture-v1",
    )


def test_v2_policy_artifact_round_trips(roots):
    calibration = _calibration(roots)
    policy = _v2(
        RelativeBand(incremental_relative_rmse=0.1, full_relative_rmse=0.2),
        calibration_sha256=sha256_file(calibration),
    )
    policy_id = write_policy_artifact(roots.data, policy, calibration)
    assert policy_id.startswith("postgres-adaptive-v2-")
    assert load_policy_artifact(roots.data, policy_id, **_compatibility()) == policy
    assert write_policy_artifact(roots.data, policy, calibration) == policy_id


def test_an_unknown_policy_version_is_incompatible_on_load(roots):
    calibration = _calibration(roots)
    incremental, full = _models(1.0, 2.0)
    unknown = AdaptivePolicy(
        policy_version="postgres-adaptive-v9",
        backend_schema_version=1,
        postgresql_major=17,
        benchmark_schema_version=1,
        environment_fingerprint="env-fixture-v1",
        calibration_sha256=sha256_file(calibration),
        incremental_model=incremental,
        full_model=full,
        incremental_rmse_ms=1.0,
        full_rmse_ms=1.0,
        training_row_count=18,
    )
    with pytest.raises(ValidationFailed, match="incompatible_policy"):
        write_policy_artifact(roots.data, unknown, calibration)
    # Publish it by hand, as only a foreign writer could, to reach the loader's own check.
    with ArtifactWriter.create(_policy_spec(unknown, calibration), data_root=roots.data) as writer:
        writer.write_json("policy.json", unknown.model_dump(mode="json"))
        writer.add_file("calibration.json", calibration)
        writer.commit()
    with pytest.raises(
        ValidationFailed, match="incompatible_policy: provenance policy policy version"
    ):
        load_policy_artifact(roots.data, unknown.id, **_compatibility())


def test_a_v2_shaped_payload_with_an_unknown_version_is_incompatible_on_load(roots):
    calibration = _calibration(roots)
    policy = _v2(
        RelativeBand(incremental_relative_rmse=0.1, full_relative_rmse=0.2),
        calibration_sha256=sha256_file(calibration),
    )
    payload = {**policy.model_dump(mode="json"), "policy_version": "postgres-adaptive-v3"}
    # Only a foreign writer could publish this: v2's fields under a version v2 does not name.
    with ArtifactWriter.create(_policy_spec(policy, calibration), data_root=roots.data) as writer:
        writer.write_json("policy.json", payload)
        writer.add_file("calibration.json", calibration)
        writer.commit()
    with pytest.raises(
        ValidationFailed, match="incompatible_policy: provenance policy policy version"
    ):
        load_policy_artifact(roots.data, policy.id, **_compatibility())
