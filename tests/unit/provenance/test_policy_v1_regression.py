"""Policy v1 stays frozen (spec 2026-10-09 §10): the committed v1 artifact reproduces every
adaptive decision of the three published v1 evidence files, field for field."""

import json
from pathlib import Path

import pytest

from vcp.core.hashing import sha256_file
from vcp.provenance.strategy import (
    POLICY_VERSION,
    MaintenanceFeatures,
    load_policy_artifact,
    select_strategy,
)

BENCHMARKS = Path(__file__).resolve().parents[3] / "docs" / "benchmarks"
ARTIFACTS = BENCHMARKS / "postgres-provenance-calibration-v2-artifacts"
V1_ID = "postgres-adaptive-v1-9f4e58346529"
V1_SHA256 = "a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78"


@pytest.fixture(scope="module")
def v1_policy():
    policy_file = ARTIFACTS / "artifacts" / "provenance_policy" / V1_ID / "policy.json"
    assert sha256_file(policy_file) == V1_SHA256
    policy = load_policy_artifact(ARTIFACTS, V1_ID)
    assert policy.policy_version == POLICY_VERSION
    return policy


@pytest.mark.parametrize(
    ("name", "key", "count"),
    [
        ("postgres-provenance-six-method-v1.json", "results", 108),
        ("postgres-provenance-heldout-v1.json", "results", 108),
        ("postgres-provenance-real-rsna-v1.json", "six_method_benchmark", 3),
    ],
)
def test_v1_policy_reproduces_every_published_decision(v1_policy, name, key, count):
    document = json.loads((BENCHMARKS / name).read_text(encoding="utf-8"))
    rows = [row for row in document[key] if row["method"] == "postgres_adaptive"]
    assert len(rows) == count
    for row in rows:
        assert (row["policy_id"], row["policy_sha256"]) == (V1_ID, V1_SHA256)
        features = MaintenanceFeatures.model_validate(
            {field: row[field] for field in MaintenanceFeatures.model_fields}
        )
        decision = select_strategy("auto", features, v1_policy)
        assert (
            decision.selected_strategy.value,
            decision.reason,
            decision.policy_version,
            decision.estimated_incremental_ms,
            decision.estimated_full_ms,
        ) == (
            row["selected_strategy"],
            row["strategy_reason"],
            row["policy_version"],
            row["estimated_incremental_ms"],
            row["estimated_full_ms"],
        ), row["scenario_hash"]
