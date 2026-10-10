"""Policy v2 is pinned (spec 2026-10-09 §4, §5, §13): the committed v2 artifact is the one the
band comparison chose, built on the frozen v1 cost models and loadable by the held-out runner."""

import json
from pathlib import Path

import pytest

from performance.provenance import evaluate_adaptive as evaluation
from vcp.core.hashing import sha256_file
from vcp.provenance.policy_bands import StratifiedEdgesBand
from vcp.provenance.strategy import POLICY_VERSION, POLICY_VERSION_V2, load_policy_artifact

BENCHMARKS = Path(__file__).resolve().parents[3] / "docs" / "benchmarks"
V2_DOCUMENT = BENCHMARKS / "postgres-provenance-policy-v2.json"
V1_DOCUMENT = BENCHMARKS / "postgres-provenance-calibration-v2.json"
BAND_COMPARISON = BENCHMARKS / "postgres-provenance-band-comparison-v1.json"
ARTIFACTS = BENCHMARKS / "postgres-provenance-policy-v2-artifacts"
V2_ID = "postgres-adaptive-v2-9f4e58346529"
V2_SHA256 = "6fe654a90468edb57aa84fdec7ac2e53cf18b20106f7a77d65df8ac7cd27c000"
V1_ID = "postgres-adaptive-v1-9f4e58346529"


@pytest.fixture(scope="module")
def v2_loaded():
    policy_file = ARTIFACTS / "artifacts" / "provenance_policy" / V2_ID / "policy.json"
    assert sha256_file(policy_file) == V2_SHA256
    return evaluation.load_calibration(V2_DOCUMENT)


@pytest.fixture(scope="module")
def v2_policy(v2_loaded):
    return v2_loaded[0]


@pytest.fixture(scope="module")
def v1_loaded():
    return evaluation.load_calibration(V1_DOCUMENT)


def test_the_committed_v2_document_loads_as_the_stratified_policy(v2_policy):
    assert v2_policy.id == V2_ID
    assert v2_policy.policy_version == POLICY_VERSION_V2
    assert v2_policy.band.kind == "stratified_edges"
    assert isinstance(v2_policy.band, StratifiedEdgesBand)
    assert evaluation.policy_file_sha256(V2_DOCUMENT, v2_policy) == V2_SHA256


def test_the_committed_artifact_verifies_on_its_own(v2_policy):
    assert load_policy_artifact(ARTIFACTS, V2_ID) == v2_policy


def test_the_band_is_the_one_the_comparison_fitted_on_all_data(v2_policy):
    comparison = json.loads(BAND_COMPARISON.read_text(encoding="utf-8"))
    assert comparison["winner"] == "stratified_edges"
    assert (
        v2_policy.band.model_dump(mode="json") == comparison["full_data_bands"]["stratified_edges"]
    )
    assert comparison["calibration_sha256"] == v2_policy.calibration_sha256


def test_the_cost_models_are_the_frozen_v1_ones(v2_policy, v1_loaded):
    v1_policy = v1_loaded[0]
    assert v1_policy.id == V1_ID and v1_policy.policy_version == POLICY_VERSION
    assert v2_policy.incremental_model == v1_policy.incremental_model
    assert v2_policy.full_model == v1_policy.full_model
    assert v2_policy.calibration_sha256 == v1_policy.calibration_sha256


def test_v2_and_the_frozen_v1_pass_the_held_out_pair_check(v2_loaded, v1_loaded):
    evaluation._require_policy_pair(*v2_loaded, *v1_loaded)
