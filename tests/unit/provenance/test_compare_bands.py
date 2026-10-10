"""The pre-registered band comparison (spec 2026-10-09 §5), on synthetic rows only."""

import json
import math

import pytest

from performance.provenance import compare_bands
from vcp.provenance.strategy import CalibrationObservation


def _rows(noise):
    """Two seeds, three size decades; ``noise(i, seed, predicted)`` gives the measurement."""
    rows = []
    for seed in (20260913, 20260914):
        for decade, edges in ((3, 1500), (4, 15_000), (5, 150_000)):
            for i in range(1, 9):
                total = edges + 7 * i
                # The changed count scales with the size decade, so the changed-samples term
                # stays identifiable from every fold; at a fixed count it is lost in the noise of
                # the edges term and the cost model, not the band, would drive the score.
                changed = 10 * i * 10 ** (decade - 3)
                incremental = 1.2 * changed + 0.12 * total
                full = 0.7 * total + 0.1 * (total // 2)
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
                        incremental_p50_ms=noise(i, seed, incremental),
                        full_p50_ms=noise(i, seed, full),
                        environment_fingerprint="a" * 64,
                        backend_schema_version=1,
                        postgresql_major=17,
                        benchmark_schema_version=1,
                    )
                )
    return rows


def _multiplicative(i, seed, value):
    return value * (1.1 if (i + seed) % 2 else 0.9)


def _additive(i, seed, value):
    return value + (50.0 if (i + seed) % 2 else -50.0)


def test_distance_and_choose():
    assert compare_bands.distance(1.0) == 0.0
    assert compare_bands.distance(math.e) == pytest.approx(1.0)
    assert compare_bands.distance(0.0) == math.inf
    assert compare_bands.distance(math.inf) == math.inf
    assert compare_bands.choose(0.30, 0.25) == "relative"  # within 0.1: the simpler A
    assert compare_bands.choose(0.50, 0.30) == "stratified_edges"
    assert compare_bands.choose(0.20, 0.50) == "relative"
    assert compare_bands.choose(math.inf, 0.4) == "stratified_edges"
    assert compare_bands.choose(math.inf, math.inf) is None


def test_seed_crossvalidation_covers_both_directions_models_and_decades():
    cells = compare_bands.seed_crossvalidation(_rows(_multiplicative), "relative")
    assert {cell["split"] for cell in cells} == {
        "train-20260913-test-20260914",
        "train-20260914-test-20260913",
    }
    assert {cell["model"] for cell in cells} == {"incremental", "full"}
    assert {cell["decade"] for cell in cells} == {3, 4, 5}
    assert len(cells) == 2 * 2 * 3
    assert all(cell["n"] == 8 for cell in cells)


def test_multiplicative_noise_chooses_the_relative_band():
    document = compare_bands.compare(
        _rows(_multiplicative), calibration_sha256="c" * 64, commit="d" * 40
    )
    assert document["winner"] == "relative"


def test_constant_noise_favours_the_stratified_band():
    document = compare_bands.compare(_rows(_additive), calibration_sha256="c" * 64, commit="d" * 40)
    assert document["scores"]["relative"] - document["scores"]["stratified_edges"] >= 0.1
    assert document["winner"] == "stratified_edges"


def test_document_shape_and_determinism():
    rows = _rows(_multiplicative)
    first = compare_bands.compare(rows, calibration_sha256="c" * 64, commit="d" * 40)
    second = compare_bands.compare(rows, calibration_sha256="c" * 64, commit="d" * 40)
    assert first == second
    assert first["kind"] == compare_bands.KIND
    assert (first["calibration_sha256"], first["script_commit"], first["tie"]) == (
        "c" * 64,
        "d" * 40,
        0.1,
    )
    assert set(first) == {
        "kind",
        "calibration_sha256",
        "script_commit",
        "tie",
        "candidates",
        "scores",
        "winner",
        "leave_one_decade_out",
        "calibration_decisions",
        "full_data_bands",
        "v1_rmse_ms",
    }
    assert set(first["calibration_decisions"]) == {"relative", "stratified_edges"}
    assert {"3", "4", "5"} <= set(first["calibration_decisions"]["relative"])
    json.dumps(first, allow_nan=False)  # every infinity is spelled out as a string


def test_main_refuses_an_existing_output_and_an_uncommitted_script(tmp_path, monkeypatch, capsys):
    calibration = tmp_path / "calibration.json"
    calibration.write_text("{}", encoding="utf-8")
    existing = tmp_path / "exists.json"
    existing.write_text("{}", encoding="utf-8")
    assert compare_bands.main(["--calibration", str(calibration), "--output", str(existing)]) == 1

    def dirty():
        raise RuntimeError("uncommitted")

    monkeypatch.setattr(compare_bands, "pinned_commit", dirty)
    output = tmp_path / "out.json"
    assert compare_bands.main(["--calibration", str(calibration), "--output", str(output)]) == 1
    assert not output.exists()
    assert "status=FAIL" in capsys.readouterr().err
