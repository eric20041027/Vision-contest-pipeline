"""E5 pre-registered analysis (spec 2026-10-23 §6.3-§6.4) on clearly synthetic rows."""

from __future__ import annotations

import pytest

from performance.provenance import break_even_analysis as analysis

LEVELS = (0.17, 0.30, 0.40, 0.50, 0.60, 0.75, 1.0)
SEEDS = (20261201, 20261202)


def scenario_rows(
    entities,
    level,
    seed,
    *,
    full,
    incremental,
    v2=None,
    v2_selected=None,
    v1=None,
    v1_selected="FULL",
    estimates=(100.0, 200.0),
):
    """The four method rows of one scenario; every sample of a row takes its p50."""
    common = {
        "entities": entities,
        "change_ratio": level,
        "seed": seed,
        "scenario_hash": f"{entities}-{level}-{seed}",
        "total_edges": 2 * entities,
        "changed_samples": round(level * 2 * entities),
        "estimated_incremental_ms": estimates[0],
        "estimated_full_ms": estimates[1],
        "dirty_entities": 10,
    }
    faster = "FULL" if full < incremental else "INCREMENTAL"

    def row(method, p50, selected):
        return {
            **common,
            "method": method,
            "maintenance_p50_ms": p50,
            "maintenance_p95_ms": p50,
            "samples": [{"maintenance_ms": p50}] * 7,
            "selected_strategy": selected,
        }

    return [
        row("postgres_full", full, "FULL"),
        row("postgres_incremental", incremental, "INCREMENTAL"),
        row(
            "postgres_adaptive", min(full, incremental) if v2 is None else v2, v2_selected or faster
        ),
        row("postgres_adaptive_v1", full if v1 is None else v1, v1_selected),
    ]


def matrix(full_wins=frozenset(), **overrides):
    """Every layer x level x seed; FULL wins (0.8 x incremental) exactly at ``full_wins``."""
    rows = []
    for entities in (1_000, 10_000):
        for level in LEVELS:
            for seed in SEEDS:
                full = 80.0 if level in full_wins else 120.0
                rows += scenario_rows(
                    entities, level, seed, full=full, incremental=100.0, **overrides
                )
    return rows


def test_crossover_is_the_lowest_level_from_which_full_always_wins():
    result = analysis.analyze(matrix(full_wins={0.60, 0.75, 1.0}))
    assert result["h5a"]["verdict"] == "holds"
    assert {layer: cell["crossover_density"] for layer, cell in result["layers"].items()} == {
        "1000": 0.60,
        "10000": 0.60,
    }
    assert result["h5c"]["verdict"] == "holds"
    assert result["h5c"]["scenarios"] == 12


def test_without_any_full_win_h5a_fails_and_h5c_does_not_apply():
    result = analysis.analyze(matrix())
    assert result["h5a"]["verdict"] == "fails"
    assert all(cell["crossover_density"] is None for cell in result["layers"].values())
    assert result["h5c"]["verdict"] == "not_applicable"


def test_a_layer_whose_top_level_loses_has_no_crossover():
    result = analysis.analyze(matrix(full_wins={0.60}))
    assert result["h5a"]["verdict"] == "holds"
    assert all(cell["crossover_density"] is None for cell in result["layers"].values())
    assert result["h5c"]["verdict"] == "not_applicable"


def test_a_level_wins_only_when_every_scenario_is_at_most_095():
    rows = matrix(full_wins={1.0})
    for row in rows:
        if (row["entities"], row["change_ratio"], row["seed"]) == (1_000, 1.0, SEEDS[1]):
            if row["method"] == "postgres_full":
                row["maintenance_p50_ms"] = 96.0
                row["samples"] = [{"maintenance_ms": 96.0}] * 7
    result = analysis.analyze(rows)
    assert result["layers"]["1000"]["crossover_density"] is None
    assert result["layers"]["10000"]["crossover_density"] == 1.0


@pytest.mark.parametrize(("slower", "verdict"), [(1.04, "holds"), (1.06, "fails")])
def test_h5b_pools_raw_samples_against_the_better_fixed_method(slower, verdict):
    rows = matrix()
    for row in rows:
        if row["method"] == "postgres_adaptive":
            row["maintenance_p50_ms"] = row["maintenance_p95_ms"] = 100.0 * slower
            row["samples"] = [{"maintenance_ms": 100.0 * slower}] * 7
    result = analysis.analyze(rows)
    assert result["h5b"]["verdict"] == verdict
    assert result["h5b"]["median_ratio"] == pytest.approx(slower)


def test_h5c_fails_when_v2_is_no_faster_than_incremental_above_the_crossover():
    result = analysis.analyze(matrix(full_wins={0.75, 1.0}, v2=100.0, v2_selected="INCREMENTAL"))
    assert result["h5c"]["verdict"] == "fails"
    assert result["h5c"]["adaptive_p50"] == result["h5c"]["incremental_p50"] == 100.0


def test_correct_choice_counts_only_scenarios_whose_fixed_methods_differ_by_over_5_percent():
    rows = scenario_rows(
        1_000, 0.6, SEEDS[0], full=80.0, incremental=100.0, v1_selected="INCREMENTAL"
    )
    rows += scenario_rows(1_000, 0.6, SEEDS[1], full=97.0, incremental=100.0)
    cell = analysis.analyze(rows)["levels"][0]
    assert (cell["clear_scenarios"], cell["v2_correct"], cell["v1_correct"]) == (1, 1, 0)


@pytest.mark.parametrize(
    ("estimates", "reason"),
    [((300.0, 200.0), "model_predicts_full"), ((100.0, 200.0), "uncertain")],
)
def test_v2_full_choices_are_split_by_their_estimates(estimates, reason):
    rows = scenario_rows(
        1_000,
        0.3,
        SEEDS[0],
        full=120.0,
        incremental=100.0,
        v2=120.0,
        v2_selected="FULL",
        estimates=estimates,
    )
    cell = analysis.analyze(rows)["levels"][0]
    assert cell["v2_full_reasons"] == {reason: 1}


def test_residuals_are_measured_minus_estimated():
    rows = scenario_rows(1_000, 0.6, SEEDS[0], full=80.0, incremental=100.0, estimates=(90.0, 70.0))
    cell = analysis.analyze(rows)["levels"][0]
    assert cell["incremental_residual_ms"] == [10.0]
    assert cell["full_residual_ms"] == [10.0]
