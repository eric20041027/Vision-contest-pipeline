"""E5 rerun (spec 2026-11-15): the frozen analysis on clearly synthetic rows, and the runner's
checks against the real frozen policy v2 and the committed predictions. Nothing here is
measured or saved as benchmark output."""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from performance.provenance import adaptive_benchmark as benchmark
from performance.provenance import break_even_rerun_analysis as analysis
from performance.provenance import break_even_rerun_evaluation as runner
from performance.provenance import break_even_rerun_predictions as predictions
from performance.provenance.workloads import (
    CALIBRATION_SEEDS,
    DENSE_LEVELS,
    DENSE_SCALES,
    DENSE_SEEDS,
    HELDOUT_SEEDS,
    dense_matrix,
)
from unit.provenance.test_break_even_evaluation import _features, _row
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.provenance.strategy import HELDOUT_V2_SEEDS

BENCHMARKS = Path(__file__).resolve().parents[3] / "docs" / "benchmarks"
POLICY = BENCHMARKS / "postgres-provenance-policy-v2.json"
PREDICTIONS = BENCHMARKS / "postgres-provenance-break-even-v2-predictions.json"
LEVELS = DENSE_LEVELS
SEEDS = predictions.RERUN_SEEDS


# --- the frozen analysis, on synthetic rows -------------------------------------------------


def scenario_rows(
    entities, level, seed, *, full, incremental, v2=None, v2_selected=None, estimates=(100.0, 200.0)
):
    """The three method rows of one scenario; every sample of a row takes its p50."""
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
        "status": "ok",
        "parity_rate": 1.0,
        **dict.fromkeys(analysis.PARITY_FIELDS, True),
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
    ]


def matrix(full_wins=frozenset(), win_layers=DENSE_SCALES, **overrides):
    """Every layer x level x seed; FULL wins (0.8 x incremental) exactly at ``full_wins`` in
    ``win_layers``."""
    rows = []
    for entities in DENSE_SCALES:
        for level in LEVELS:
            for seed in SEEDS:
                wins = level in full_wins and entities in win_layers
                rows += scenario_rows(
                    entities,
                    level,
                    seed,
                    full=80.0 if wins else 120.0,
                    incremental=100.0,
                    **overrides,
                )
    return rows


def analyze(rows, *, overlap_count=0, expected_hashes=None):
    if expected_hashes is None:
        expected_hashes = {row["scenario_hash"] for row in rows}
    return analysis.analyze(rows, expected_hashes=expected_hashes, overlap_count=overlap_count)


def test_crossover_is_the_lowest_level_from_which_full_always_wins():
    result = analyze(matrix(full_wins={0.60, 0.75, 1.0}))
    assert result["h5a"]["verdict"] == "holds"
    for layer in result["layers"].values():
        assert layer["crossover_density"] == 0.60
        assert layer["level_below_crossover"] == 0.50
        assert layer["full_vs_incremental_above_crossover"] == {
            "min": 0.8,
            "max": 0.8,
            "scenarios": 6,
        }
        assert layer["v2_vs_incremental_below_crossover"] == {
            "min": 1.0,
            "max": 1.0,
            "scenarios": 8,
        }
    assert result["h5c"]["verdict"] == "holds"
    assert result["h5c"]["scenarios"] == 12
    assert result["h5c"]["ratio"] == pytest.approx(0.8)


def test_without_any_full_win_h5a_fails_and_h5c_does_not_apply():
    result = analyze(matrix())
    assert result["h5a"]["verdict"] == "fails"
    assert result["h5c"] == {"verdict": "not_applicable", "scenarios": 0}
    for layer in result["layers"].values():
        assert layer["crossover_density"] is None
        assert layer["level_below_crossover"] is None
        assert layer["full_vs_incremental_above_crossover"] is None
        assert layer["v2_vs_incremental_below_crossover"]["scenarios"] == 14


def test_a_layer_whose_top_level_loses_has_no_crossover():
    result = analyze(matrix(full_wins={0.60}))
    assert result["h5a"]["verdict"] == "holds"
    assert all(layer["crossover_density"] is None for layer in result["layers"].values())
    assert result["h5c"]["verdict"] == "not_applicable"


def test_a_level_wins_only_when_every_scenario_is_at_most_095():
    rows = matrix(full_wins={1.0})
    for row in rows:
        if (row["entities"], row["change_ratio"], row["seed"]) == (1_000, 1.0, SEEDS[1]):
            if row["method"] == "postgres_full":
                row["maintenance_p50_ms"] = 96.0
                row["samples"] = [{"maintenance_ms": 96.0}] * 7
    result = analyze(rows)
    assert result["layers"]["1000"]["crossover_density"] is None
    assert result["layers"]["10000"]["crossover_density"] == 1.0


def test_a_crossover_ignores_a_lone_win_below_a_losing_level():
    result = analyze(matrix(full_wins={0.40, 0.75, 1.0}))
    assert result["layers"]["1000"]["crossover_density"] == 0.75
    assert result["layers"]["1000"]["level_below_crossover"] == 0.60
    assert result["layers"]["1000"]["full_win_levels"] == [0.40, 0.75, 1.0]
    assert result["h5c"]["scenarios"] == 8


@pytest.mark.parametrize(("slower", "verdict"), [(1.04, "holds"), (1.06, "fails")])
def test_h5b_pools_raw_samples_against_the_better_fixed_method(slower, verdict):
    rows = matrix()
    for row in rows:
        if row["method"] == "postgres_adaptive":
            row["maintenance_p50_ms"] = row["maintenance_p95_ms"] = 100.0 * slower
            row["samples"] = [{"maintenance_ms": 100.0 * slower}] * 7
    result = analyze(rows)
    assert result["h5b"]["verdict"] == verdict
    assert result["h5b"]["median_ratio"] == pytest.approx(slower)


def test_h5b_pools_raw_samples_not_scenario_medians():
    rows = matrix()
    adaptive = [row for row in rows if row["method"] == "postgres_adaptive"]
    for index, row in enumerate(adaptive):
        values = [50.0] * 4 + [500.0] * 3 if index % 2 else [150.0] * 7
        row["samples"] = [{"maintenance_ms": value} for value in values]
        row["maintenance_p50_ms"] = sorted(values)[3]
        row["maintenance_p95_ms"] = sorted(values)[6]
    result = analyze(rows)
    assert result["aggregate_maintenance_ms"]["postgres_adaptive"] == {"p50": 150.0, "p95": 500.0}
    assert result["h5b"]["median_ratio"] == pytest.approx(1.5)
    assert result["h5b"]["verdict"] == "fails"


def test_h5c_fails_when_v2_is_no_faster_than_incremental_above_the_crossover():
    result = analyze(matrix(full_wins={0.75, 1.0}, v2=100.0, v2_selected="INCREMENTAL"))
    assert result["h5c"]["verdict"] == "fails"
    assert result["h5c"]["adaptive_p50"] == result["h5c"]["incremental_p50"] == 100.0


def test_a_cell_reports_the_mean_the_choice_and_the_residuals():
    rows = scenario_rows(1_000, 0.6, SEEDS[0], full=80.0, incremental=100.0, estimates=(90.0, 70.0))
    rows += scenario_rows(
        1_000, 0.6, SEEDS[1], full=90.0, incremental=110.0, v2=95.0, estimates=(90.0, 70.0)
    )
    cell = analyze(rows)["levels"][0]
    assert cell["mean_p50_ms"] == {"pg_full": 85.0, "pg_incr": 105.0, "v2": 87.5}
    assert cell["v2_selected"] == ["FULL"]
    assert cell["incremental_residual_ms"] == [10.0, 20.0]
    assert cell["full_residual_ms"] == [10.0, 20.0]
    assert (cell["clear_scenarios"], cell["v2_correct"]) == (2, 2)
    assert cell["v2_full_reasons"] == {"model_predicts_full": 2}
    assert cell["scenarios"][1]["v2_vs_incremental"] == pytest.approx(95.0 / 110.0)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda rows: rows.pop(), "coverage:methods"),
        (lambda rows: rows[0].update(status="failed", maintenance_p50_ms=None), "failed_row"),
        (lambda rows: rows[0].update(head_parity=False), "parity"),
        (lambda rows: rows[0].update(parity_rate=0.5), "parity"),
        (lambda rows: rows.append(dict(rows[0])), "unexpected_row"),
        (lambda rows: rows[0].update(method="postgres_adaptive_v1"), "unexpected_row"),
    ],
)
def test_invalid_evidence_returns_only_the_gate(mutate, reason):
    rows = matrix()
    expected = {row["scenario_hash"] for row in rows}
    mutate(rows)
    result = analyze(rows, expected_hashes=expected)
    assert set(result) == {"correctness_gate"}
    assert any(r.startswith(reason) for r in result["correctness_gate"]["reasons"])


def test_a_missing_scenario_or_an_overlap_fails_the_gate():
    rows = matrix()
    expected = {row["scenario_hash"] for row in rows} | {"10000-1.0-missing"}
    assert analyze(rows, expected_hashes=expected)["correctness_gate"]["reasons"] == [
        "coverage:scenarios"
    ]
    assert analyze(matrix(), overlap_count=2)["correctness_gate"]["reasons"] == ["overlap_count:2"]


# --- the runner, against the real frozen policy and the committed predictions ---------------


@pytest.fixture(scope="module")
def frozen():
    return runner.load_frozen(POLICY, PREDICTIONS)


def rerun_rows(frozen):
    """Every rerun scenario x the three methods, shaped like measured rows."""
    shim = SimpleNamespace(
        policies={"postgres_adaptive": frozen.policy}, sha256={"postgres_adaptive": frozen.sha256}
    )
    return [
        _row(
            scenario,
            method,
            _features(scenario),
            frozen.predictions[scenario.scenario_hash]["workload_hash"],
            shim,
        )
        for scenario in predictions.rerun_matrix()
        for method in runner.METHODS
    ]


def test_the_rerun_is_e5s_design_under_new_seeds_and_without_policy_v1():
    scenarios = predictions.rerun_matrix()
    assert len(scenarios) == 28
    assert {(s.entities, s.change_ratio) for s in scenarios} == {
        (s.entities, s.change_ratio) for s in dense_matrix()
    }
    assert {s.repetitions for s in scenarios} == {7}
    assert runner.METHODS == ("postgres_full", "postgres_incremental", "postgres_adaptive")
    used = {*CALIBRATION_SEEDS, *HELDOUT_SEEDS, *HELDOUT_V2_SEEDS, *DENSE_SEEDS}
    assert not used & set(SEEDS)
    assert not {s.scenario_hash for s in scenarios} & {s.scenario_hash for s in dense_matrix()}


def test_valid_rerun_rows_reach_the_frozen_analysis(frozen):
    result = runner.evaluate(rerun_rows(frozen), frozen)
    assert result["overlap_count"] == 0
    assert result["seeds"] == list(SEEDS)
    assert result["analysis"]["correctness_gate"] == {"verdict": "holds", "reasons": []}
    assert result["analysis"]["scenario_count"] == 28
    assert result["predictions"] == {"file": PREDICTIONS.name, "sha256": runner.PREDICTIONS_SHA256}
    assert result["analysis_script"] == {
        "file": "break_even_rerun_analysis.py",
        "sha256": sha256_file(Path(analysis.__file__)),
    }
    assert result["policy"] == {
        "policy_id": "postgres-adaptive-v2-9f4e58346529",
        "policy_sha256": "6fe654a90468edb57aa84fdec7ac2e53cf18b20106f7a77d65df8ac7cd27c000",
    }
    assert result["prediction_agreement"] == {"agree": 28, "disagree": []}
    assert runner._verdict_line(result).startswith("VERDICT cmd=provenance.break_even_rerun ")


def test_the_predictions_cover_the_matrix_and_are_the_committed_bytes(frozen):
    assert sha256_file(PREDICTIONS) == runner.PREDICTIONS_SHA256
    assert list(frozen.predictions) == [s.scenario_hash for s in predictions.rerun_matrix()]
    assert not {row["workload_hash"] for row in frozen.predictions.values()} & (
        runner._first_workload_hashes()
    )


def test_a_selection_unlike_the_prediction_is_reported_not_hidden(frozen):
    rows = rerun_rows(frozen)
    changed = copy.deepcopy(frozen.predictions)
    flipped = rows[0]["scenario_hash"]
    predicted = changed[flipped]["postgres_adaptive"]
    predicted["selected"] = "FULL" if predicted["selected"] == "INCREMENTAL" else "INCREMENTAL"
    result = runner.evaluate(rows, frozen._replace(predictions=changed))
    assert result["prediction_agreement"]["agree"] == 27
    assert result["prediction_agreement"]["disagree"][0]["scenario_hash"] == flipped


def _drop_one(rows):
    rows.pop()


def _fail_one(rows):
    rows[0].update(status="failed")


def _unregistered(rows):
    for row in rows[:3]:
        row["workload_hash"] = "0" * 64


def _flip_adaptive(rows):
    row = next(r for r in rows if r["method"] == "postgres_adaptive")
    flipped = "FULL" if row["selected_strategy"] == "INCREMENTAL" else "INCREMENTAL"
    row["selected_strategy"] = flipped
    for sample in row["samples"]:
        sample["selected_strategy"] = flipped


def _e5_seed(rows):
    rows[0]["seed"] = DENSE_SEEDS[0]


def _e5_scenario(rows):
    rows[0]["scenario_id"] = dense_matrix()[0].scenario_id


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (_drop_one, "invalid_benchmark_rows"),
        (_fail_one, "invalid_benchmark_rows"),
        (_unregistered, "unregistered_workload"),
        (_flip_adaptive, "policy_decision_mismatch"),
        (_e5_seed, "workload_leakage"),
        (_e5_scenario, "workload_leakage"),
    ],
)
def test_invalid_evidence_fails_before_any_analysis(frozen, mutate, reason):
    rows = rerun_rows(frozen)
    mutate(rows)
    with pytest.raises(ValidationFailed, match=reason):
        runner.evaluate(rows, frozen)


def test_a_row_naming_an_e5_workload_is_leakage(frozen):
    rows = rerun_rows(frozen)
    rows[0]["workload_hash"] = sorted(runner._first_workload_hashes())[0]
    assert runner.overlap_count(frozen.evidence, rows) == 1


def test_the_predictions_must_be_the_committed_file(tmp_path):
    tampered = tmp_path / "predictions.json"
    tampered.write_bytes(PREDICTIONS.read_bytes().replace(b'"seeds"', b'"seeds" ', 1))
    with pytest.raises(ValidationFailed, match="invalid_predictions"):
        runner.load_frozen(POLICY, tampered)


def _contract(*paths, **extra):
    identity = {"source_tree": {"files": [{"path": p, "sha256": "0" * 64} for p in paths]}}
    return {
        "execution_identity": identity,
        "execution_identity_sha256": benchmark.sha256_text(benchmark._json_text(identity)),
        **extra,
    }


def test_a_module_named_like_a_secret_does_not_make_the_contract_unsafe(tmp_path):
    """0.18.0 added ``src/vcp/artifact/authorization.py``; the contract's text filter refused
    every run for it. The real source snapshot must pass."""
    benchmark.CheckpointStore(tmp_path, _contract("src/vcp/artifact/authorization.py", "uv.lock"))
    snapshot = benchmark._source_tree_snapshot()
    assert "src/vcp/artifact/authorization.py" in {entry["path"] for entry in snapshot["files"]}
    identity = {"source_tree": snapshot}
    benchmark.CheckpointStore(
        tmp_path,
        {
            "execution_identity": identity,
            "execution_identity_sha256": benchmark.sha256_text(benchmark._json_text(identity)),
        },
    )


@pytest.mark.parametrize(
    "contract",
    [
        _contract("src/vcp/a b.py"),
        _contract("../pgpass.conf"),
        _contract("postgresql://example/x.py"),
        _contract("src/vcp/ok.py", note="password"),
        {"execution_identity": {"source_tree": {"files": [{"sha256": "0" * 64}]}}},
    ],
)
def test_the_contract_still_refuses_anything_that_is_not_a_plain_source_path(tmp_path, contract):
    with pytest.raises(ValueError, match="unsafe checkpoint evidence"):
        benchmark.CheckpointStore(tmp_path, contract)


def test_only_the_frozen_policy_v2_is_accepted():
    with pytest.raises(ValidationFailed, match="invalid_policy"):
        predictions.load_policy(BENCHMARKS / "postgres-provenance-calibration-v2.json")
