"""The E5 ``dense`` workload family (spec 2026-10-23 §3)."""

from __future__ import annotations

import tempfile
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from performance.provenance.workloads import (
    CALIBRATION_SEEDS,
    DENSE_LEVELS,
    DENSE_SCALES,
    DENSE_SEEDS,
    DENSE_STRUCTURAL_EDGES,
    HELDOUT_SEEDS,
    Scenario,
    build_scenario,
    dense_counts,
    dense_matrix,
    scenario_matrix,
)
from vcp.core.hashing import sha256_text
from vcp.provenance.graph import build_graph
from vcp.provenance.schema import ChangeType
from vcp.provenance.strategy import HELDOUT_V2_SEEDS

# Ordered scenario ids of every published matrix, pinned before the dense family was added.
PINNED_MATRICES = {
    CALIBRATION_SEEDS: "c3c87c5da83b285e8c6930cf97b7f310401d53188d39645902da9b23a70d6d8f",
    HELDOUT_SEEDS: "ddb8fbe673b163f1562ceb01e5b67d0780fb342f009a34fccde5940661f18526",
    HELDOUT_V2_SEEDS: "a3d0bfe2c1f91955dca06ba7cba9b33a2dec3b1e22c4ca94d9a34fef16e1bb98",
}


@pytest.mark.parametrize("seeds", list(PINNED_MATRICES))
def test_published_matrices_are_unchanged(seeds):
    matrix = scenario_matrix(seeds=seeds)
    assert sha256_text("\n".join(s.scenario_id for s in matrix)) == PINNED_MATRICES[seeds]


def test_dense_matrix_is_complete_deterministic_and_disjoint():
    matrix = dense_matrix()
    assert len(matrix) == 28
    assert {s.seed for s in matrix} == set(DENSE_SEEDS) == {20261201, 20261202}
    assert {s.entities for s in matrix} == set(DENSE_SCALES) == {1_000, 10_000}
    assert {s.change_ratio for s in matrix} == set(DENSE_LEVELS)
    assert DENSE_LEVELS == (0.17, 0.30, 0.40, 0.50, 0.60, 0.75, 1.0)
    assert {(s.track, s.topology, s.variant) for s in matrix} == {("dense", "flat", "v1")}
    assert all(s.repetitions == 7 for s in matrix)
    assert matrix == dense_matrix()
    assert not set(DENSE_SEEDS) & {*CALIBRATION_SEEDS, *HELDOUT_SEEDS, *HELDOUT_V2_SEEDS}
    published = {s.scenario_hash for seeds in PINNED_MATRICES for s in scenario_matrix(seeds=seeds)}
    assert not {s.scenario_hash for s in matrix} & published


@pytest.mark.parametrize(
    "scenario",
    [
        {"topology": "chain", "track": "dense"},
        {"topology": "flat", "track": "scaled"},
        {"topology": "flat", "track": "dense", "change_ratio": 0.0},
    ],
)
def test_dense_track_takes_only_flat_nonzero_scenarios(scenario):
    values = {"entities": 1_000, "change_ratio": 0.5, "seed": 20261201, **scenario}
    with pytest.raises(ValueError):
        Scenario(**values)


@pytest.mark.parametrize(
    ("density", "expected"),
    [
        (0.17, (340, 0, 1314)),
        (0.30, (600, 0, 794)),
        (0.40, (800, 0, 394)),
        (0.50, (997, 0, 0)),
        (0.60, (794, 406, 0)),
        (0.75, (494, 1006, 0)),
        (1.0, (0, 1994, 0)),
    ],
)
def test_dense_counts_fill_the_edge_target_exactly(density, expected):
    modified, added, readings = dense_counts(2_000, density)
    assert (modified, added, readings) == expected
    assert 2 * modified + added + readings + DENSE_STRUCTURAL_EDGES == 2_000


@pytest.mark.parametrize(
    ("edge_target", "density"), [(DENSE_STRUCTURAL_EDGES + 1, 0.5), (2_000, 0.0), (2_000, 1.5)]
)
def test_dense_counts_reject_infeasible_targets(edge_target, density):
    with pytest.raises(ValueError):
        dense_counts(edge_target, density)


@pytest.mark.parametrize("density", DENSE_LEVELS)
def test_dense_workload_hits_its_edge_target_and_change_mix(tmp_path, density):
    workload = build_scenario(tmp_path / "w", Scenario(100, density, "flat", 20261201, "dense"))
    modified, added, readings = dense_counts(200, density)
    before = build_graph(workload.data, workload.configs)
    assert not before.gaps
    assert not any(e.broken_reason for e in before.entities.values())
    assert len(workload.expected.edges) == 200
    assert workload.changed_samples == modified + added
    assert workload.historical_changes == 0
    assert workload.sample_entities == modified + 1 + added
    kinds = Counter(change.change_type for change in workload.expected.changes.values())
    assert kinds == +Counter({ChangeType.MODIFIED: modified, ChangeType.ADDED: added})
    evaluated = [e for e in workload.expected.edges.values() if e.edge_type == "EVALUATED_BY"]
    assert len(evaluated) == readings


def test_dense_workload_publishes_to_its_expected_graph(tmp_path):
    workload = build_scenario(tmp_path / "w", Scenario(100, 0.6, "flat", 20261201, "dense"))
    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        data, configs = workload.clone(Path(temporary))
        workload.publish(data)
        assert build_graph(data, configs).normalized() == workload.expected.normalized()


def test_dense_workload_is_deterministic_and_seeded(tmp_path):
    spec = Scenario(100, 0.6, "flat", 20261201, "dense")
    first = build_scenario(tmp_path / "first", spec)
    second = build_scenario(tmp_path / "second", spec)
    other = build_scenario(tmp_path / "other", replace(spec, seed=20261202))
    assert first.workload_hash == second.workload_hash
    assert first.workload_hash != other.workload_hash
