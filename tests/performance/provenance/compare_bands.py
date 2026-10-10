"""Pre-registered comparison of policy v2's candidate bands (spec 2026-10-09 §5).

Only calibration evidence goes in. The two seed-crossvalidated directions decide; the
leave-one-decade-out cells and the in-sample decisions are reported, never used to decide.
The script refuses to run while it, the band module or strategy.py has uncommitted changes,
so the commit that holds them predates every result (§5.5).

Run (after the commit that adds this file):
    INPUTS=docs/benchmarks/postgres-provenance-calibration-v2-artifacts/inputs
    uv run python tests/performance/provenance/compare_bands.py \
        --calibration $INPUTS/calibration.json \
        --output docs/benchmarks/postgres-provenance-band-comparison-v1.json
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

from vcp.core.atomic import write_once_text
from vcp.core.hashing import sha256_file
from vcp.provenance.policy_bands import band_allows_incremental, edges_decade, fit_band, width
from vcp.provenance.strategy import (
    CALIBRATION_SEEDS,
    CalibrationEvidence,
    CalibrationObservation,
    fit_cost_models,
)

KIND = "postgres-provenance-band-comparison-v1"
TIE = 0.1
CANDIDATES = ("relative", "stratified_edges")
_SIDES = (("incremental", "incremental_p50_ms"), ("full", "full_p50_ms"))
REPOSITORY = Path(__file__).resolve().parents[3]
PINNED = (
    "tests/performance/provenance/compare_bands.py",
    "src/vcp/provenance/policy_bands.py",
    "src/vcp/provenance/strategy.py",
)


def load_observations(path: Path) -> tuple[CalibrationObservation, ...]:
    evidence = CalibrationEvidence.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if not evidence.observations:
        raise ValueError("calibration evidence without observations")
    return evidence.observations


def distance(s: float) -> float:
    """``|ln s|``: how far the band's width is from the error it is meant to cover."""
    if s == 0 or math.isinf(s) or math.isnan(s):
        return math.inf
    return abs(math.log(s))


def choose(score_relative: float, score_stratified: float) -> str | None:
    """§5.3 step 6: within ``TIE`` the simpler relative band; else the lower score; None when
    neither band ever had a width."""
    if math.isinf(score_relative) and math.isinf(score_stratified):
        return None
    if abs(score_relative - score_stratified) < TIE:
        return "relative"
    return "relative" if score_relative < score_stratified else "stratified_edges"


def _fit(train, kind):
    incremental, full = fit_cost_models(train)
    band = fit_band(
        kind,
        total_edges=[row.total_edges for row in train],
        incremental_predicted=incremental.predicted,
        incremental_actual=incremental.actual,
        full_predicted=full.predicted,
        full_actual=full.actual,
    )
    return {"incremental": incremental.model, "full": full.model}, band


def _cells(train, test, kind, split):
    """Standardized residuals of ``test`` under models and band fitted on ``train`` only."""
    models, band = _fit(train, kind)
    cells = []
    for side, target in _SIDES:
        by_decade: dict[int, list[float]] = {}
        for row in test:
            predicted = models[side].predict(row)
            half_width = width(band, side, predicted, row.total_edges)
            z = math.inf if half_width <= 0 else (getattr(row, target) - predicted) / half_width
            by_decade.setdefault(edges_decade(row.total_edges), []).append(z)
        for decade, values in sorted(by_decade.items()):
            s = math.sqrt(sum(z * z for z in values) / len(values))
            cells.append(
                {"split": split, "model": side, "decade": decade, "n": len(values), "s": s}
            )
    return cells


def seed_crossvalidation(observations, kind):
    first, second = CALIBRATION_SEEDS
    cells = []
    for train_seed, test_seed in ((first, second), (second, first)):
        train = [row for row in observations if row.seed == train_seed]
        test = [row for row in observations if row.seed == test_seed]
        cells += _cells(train, test, kind, f"train-{train_seed}-test-{test_seed}")
    return cells


def leave_one_decade_out(observations, kind):
    cells = []
    for decade in sorted({edges_decade(row.total_edges) for row in observations}):
        train = [row for row in observations if edges_decade(row.total_edges) != decade]
        test = [row for row in observations if edges_decade(row.total_edges) == decade]
        cells += _cells(train, test, kind, f"without-decade-{decade}")
    return cells


def _score(cells) -> float:
    return max(distance(cell["s"]) for cell in cells)


def _decisions(observations, kind):
    """In-sample: what the band fitted on every observation would choose for each of them."""
    models, band = _fit(observations, kind)
    summary: dict[str, dict[str, float]] = {}
    for row in observations:
        incremental = models["incremental"].predict(row)
        full = models["full"].predict(row)
        chose_incremental = band_allows_incremental(band, incremental, full, row.total_edges)
        chosen = row.incremental_p50_ms if chose_incremental else row.full_p50_ms
        best = min(row.incremental_p50_ms, row.full_p50_ms)
        cell = summary.setdefault(
            str(edges_decade(row.total_edges)),
            {"n": 0, "incremental": 0, "full": 0, "slower_choice": 0, "extra_ms": 0.0},
        )
        cell["n"] += 1
        cell["incremental" if chose_incremental else "full"] += 1
        cell["slower_choice"] += int(chosen > best)
        cell["extra_ms"] += chosen - best
    return summary, band


def _jsonable(value):
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def compare(observations, *, calibration_sha256: str, commit: str) -> dict:
    import sklearn  # imported here as the fitter does; its version is part of the evidence

    observations = list(observations)
    candidates = {kind: seed_crossvalidation(observations, kind) for kind in CANDIDATES}
    scores = {kind: _score(cells) for kind, cells in candidates.items()}
    decisions, bands = {}, {}
    for kind in CANDIDATES:
        decisions[kind], band = _decisions(observations, kind)
        bands[kind] = band.model_dump(mode="json")
    incremental, full = fit_cost_models(observations)
    return _jsonable(
        {
            "kind": KIND,
            "calibration_sha256": calibration_sha256,
            "script_commit": commit,
            "tie": TIE,
            "candidates": candidates,
            "scores": scores,
            "winner": choose(scores["relative"], scores["stratified_edges"]),
            "leave_one_decade_out": {
                kind: leave_one_decade_out(observations, kind) for kind in CANDIDATES
            },
            "calibration_decisions": decisions,
            "full_data_bands": bands,
            "v1_rmse_ms": {"incremental": incremental.rmse_ms, "full": full.rmse_ms},
            "scikit_learn_version": sklearn.__version__,
        }
    )


def pinned_commit() -> str:
    """HEAD, after checking that the files this comparison depends on are committed there."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=REPOSITORY, capture_output=True, text=True, check=True
        ).stdout.strip()

    if git("status", "--porcelain", "--", *PINNED):
        raise RuntimeError("commit compare_bands.py, policy_bands.py and strategy.py first")
    return git("rev-parse", "HEAD")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValueError("band comparison output is write-once")
        commit = pinned_commit()
        document = compare(
            load_observations(args.calibration),
            calibration_sha256=sha256_file(args.calibration),
            commit=commit,
        )
        text = json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
        write_once_text(args.output, text)
    except Exception as error:  # a script: say what failed, then a VERDICT
        print(f"band comparison failed: {type(error).__name__}: {error}", file=sys.stderr)
        print("VERDICT cmd=provenance.compare_bands status=FAIL", file=sys.stderr)
        return 1
    winner = document["winner"] or "none"
    status = "OK" if document["winner"] else "FAIL"
    print(f"VERDICT cmd=provenance.compare_bands status={status} winner={winner}", file=sys.stderr)
    return 0 if document["winner"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
