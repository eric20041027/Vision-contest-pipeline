"""E5 predictions from the frozen policies (spec 2026-10-23 §6.2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from performance.provenance import break_even_predictions as predictions
from performance.provenance.evaluate_adaptive import prepare_workload
from performance.provenance.workloads import DENSE_LEVELS, Scenario, build_scenario
from vcp.core.errors import ValidationFailed

BENCHMARKS = Path(__file__).resolve().parents[3] / "docs" / "benchmarks"
V2_DOCUMENT = BENCHMARKS / "postgres-provenance-policy-v2.json"
V1_DOCUMENT = BENCHMARKS / "postgres-provenance-calibration-v2.json"
V2_SHA256 = "6fe654a90468edb57aa84fdec7ac2e53cf18b20106f7a77d65df8ac7cd27c000"
V1_SHA256 = "a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78"


@pytest.fixture(scope="module")
def frozen():
    return predictions.load_policies(V2_DOCUMENT, V1_DOCUMENT)


def test_loads_the_frozen_pair(frozen):
    assert frozen.policies["postgres_adaptive"].id == "postgres-adaptive-v2-9f4e58346529"
    assert frozen.policies["postgres_adaptive_v1"].id == "postgres-adaptive-v1-9f4e58346529"
    assert frozen.sha256 == {"postgres_adaptive": V2_SHA256, "postgres_adaptive_v1": V1_SHA256}


def test_a_published_heldout_workload_still_rebuilds_bit_for_bit(tmp_path, frozen):
    """Spec §3.2: adding the dense family left the scaled one untouched. Held-out v2's 1K chain
    scenario at ratio 0.5 still rebuilds, with both policies installed, to its published hash."""
    scenario = Scenario(1_000, 0.5, "chain", 20261101)
    assert scenario.scenario_id == "scaled-20261101-1000-38ff0577eba57ab7"
    workload = build_scenario(tmp_path / "w", scenario)
    prepared = prepare_workload(
        workload,
        frozen.policies["postgres_adaptive"],
        frozen.evidence,
        (frozen.policies["postgres_adaptive_v1"],),
    )
    assert prepared.workload_hash == (
        "a37ac0eca6eb86d20e9b70a7337d91e0cc976b37f7385a9fbfb8b581a5e505ce"
    )


def test_a_policy_that_sees_dirty_entities_is_refused(frozen):
    v2 = frozen.policies["postgres_adaptive"]
    model = v2.incremental_model
    seeing = model.model_copy(
        update={"coefficients": {**model.coefficients, "dirty_entities": 1.0}}
    )
    with pytest.raises(ValueError):
        predictions.require_blind_to_dirty(v2.model_copy(update={"incremental_model": seeing}))


@pytest.mark.parametrize("density", DENSE_LEVELS)
def test_decade_three_predictions(tmp_path, frozen, density):
    workload = build_scenario(tmp_path / "w", Scenario(1_000, density, "flat", 20261201, "dense"))
    row = predictions.scenario_prediction(workload, frozen)
    assert (row["total_edges"], row["historical_changes"], row["decade"]) == (2_000, 0, 3)
    assert row["density"] == row["changed_samples"] / row["total_edges"]
    v2, v1 = row["postgres_adaptive"], row["postgres_adaptive_v1"]
    # Spec §6.2: the model's break-even is near 0.504, v2 switches to FULL near 0.264 and v1
    # never leaves FULL in decade 3. The structural ceiling level (0.50) stays below 0.504.
    assert v2["model_cheaper"] == ("INCREMENTAL" if row["density"] < 0.5037 else "FULL")
    assert v2["selected"] == ("INCREMENTAL" if row["density"] < 0.2645 else "FULL")
    assert v2["band_ms"] == pytest.approx(556.1966, abs=1e-3)
    assert v1["selected"] == "FULL"
    assert v1["band_ms"] == pytest.approx(7343.7, abs=0.2)
    for side in (v1, v2):
        gap = side["estimated_full_ms"] - side["estimated_incremental_ms"]
        assert side["gap_ms"] == pytest.approx(gap)


def test_document_is_write_once_and_names_its_inputs(tmp_path, frozen):
    scenarios = [Scenario(100, level, "flat", 20261201, "dense") for level in (0.17, 1.0)]
    document = predictions.build_document(scenarios, frozen, tmp_path / "work")
    assert document["kind"] == "postgres-provenance-break-even-predictions-v1"
    assert document["policies"]["postgres_adaptive"]["policy_sha256"] == V2_SHA256
    assert [row["scenario_id"] for row in document["scenarios"]] == [
        s.scenario_id for s in scenarios
    ]
    output = tmp_path / "predictions.json"
    predictions.write_document(document, output)
    assert json.loads(output.read_text(encoding="utf-8")) == document
    with pytest.raises(ValidationFailed):
        predictions.write_document(document, output)
