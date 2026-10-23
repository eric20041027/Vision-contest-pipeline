"""E5 pre-registered analysis (spec 2026-10-23 §6.1-§6.4).

Reads only the measured rows plus the runner's overlap count, and returns every verdict, table
and diagnostic of the experiment. The runner rechecks each adaptive decision against its policy
and fails closed before calling this. Committed before any E5 measurement; never edited after
the first one.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Set
from typing import Any

FIXED = ("postgres_full", "postgres_incremental")
ADAPTIVE = {"v2": "postgres_adaptive", "v1": "postgres_adaptive_v1"}
METHODS = (*FIXED, *ADAPTIVE.values())
PARITY_FIELDS = ("graph_parity", "graph_hash_parity", "head_parity", "status_parity")
FULL_WIN = 0.95  # §6.3 H5a: pg_full p50 <= 0.95 x pg_incr p50
CLEAR_GAP = 1.05  # §6.4: the fixed methods differ by more than 5%
MEDIAN_GATE, P95_GATE = 1.05, 1.10  # §6.3 H5b, the E3 gate

Row = dict[str, Any]


def correctness_gate(
    rows: list[Row], *, expected_hashes: Set[str], overlap_count: int
) -> list[str]:
    """§6.1: the reasons the evidence is invalid; empty when it passes. Touches no timing, so
    a failed row (no percentiles) is a reason, never a crash."""
    reasons = []
    seen: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        methods = seen[row["scenario_hash"]]
        if row["method"] in methods or row["method"] not in METHODS:
            reasons.append(f"unexpected_row:{row['scenario_hash']}:{row['method']}")
        methods.add(row["method"])
        if row.get("status") != "ok":
            reasons.append(f"failed_row:{row['scenario_hash']}:{row['method']}")
        elif row.get("parity_rate") != 1.0 or not all(row.get(f) is True for f in PARITY_FIELDS):
            reasons.append(f"parity:{row['scenario_hash']}:{row['method']}")
    if set(seen) != set(expected_hashes):
        reasons.append("coverage:scenarios")
    if any(methods != set(METHODS) for methods in seen.values()):
        reasons.append("coverage:methods")
    if overlap_count != 0:
        reasons.append(f"overlap_count:{overlap_count}")
    return reasons


def _groups(rows: list[Row]) -> dict[str, dict[str, Row]]:
    groups: dict[str, dict[str, Row]] = defaultdict(dict)
    for row in rows:
        groups[row["scenario_hash"]][row["method"]] = row
    return dict(groups)


def _p50(row: Row) -> float:
    return row["maintenance_p50_ms"]


def _full_wins(group: dict[str, Row]) -> bool:
    return _p50(group["postgres_full"]) <= FULL_WIN * _p50(group["postgres_incremental"])


def _pooled(groups: list[dict[str, Row]]) -> dict[str, dict[str, float]]:
    """E3's aggregate: each method's raw samples pooled, then the p50 and nearest-rank p95."""
    result = {}
    for method in METHODS:
        values = sorted(s["maintenance_ms"] for g in groups for s in g[method]["samples"])
        result[method] = {
            "p50": statistics.median(values),
            "p95": values[round(0.95 * (len(values) - 1))],
        }
    return result


def _gate(pooled: dict[str, dict[str, float]], method: str) -> dict[str, Any]:
    median = pooled[method]["p50"] / min(pooled[m]["p50"] for m in FIXED)
    p95 = pooled[method]["p95"] / min(pooled[m]["p95"] for m in FIXED)
    passed = median <= MEDIAN_GATE and p95 <= P95_GATE
    return {"verdict": "holds" if passed else "fails", "median_ratio": median, "p95_ratio": p95}


def _crossover(cells: dict[float, list[dict[str, Row]]]) -> float | None:
    """The lowest level from which every level up to the top has FULL winning everywhere."""
    crossover = None
    for level in sorted(cells, reverse=True):
        if not all(_full_wins(group) for group in cells[level]):
            break
        crossover = level
    return crossover


def _full_reason(row: Row) -> str:
    """v2's two FULL cases share one strategy_reason; only the estimates tell them apart."""
    if row["estimated_full_ms"] < row["estimated_incremental_ms"]:
        return "model_predicts_full"
    return "uncertain"


def _faster_fixed(group: dict[str, Row]) -> str:
    full, incremental = (_p50(group[m]) for m in FIXED)
    return "FULL" if full < incremental else "INCREMENTAL"


def _is_clear(group: dict[str, Row]) -> bool:
    timings = [_p50(group[m]) for m in FIXED]
    return max(timings) > CLEAR_GAP * min(timings)


def _cell(entities: int, level: float, groups: list[dict[str, Row]]) -> dict[str, Any]:
    clear = [g for g in groups if _is_clear(g)]

    def correct(name: str) -> int:
        return sum(g[ADAPTIVE[name]]["selected_strategy"] == _faster_fixed(g) for g in clear)

    v2_rows = [g[ADAPTIVE["v2"]] for g in groups]
    return {
        "entities": entities,
        "level": level,
        "scenarios": [
            {
                "seed": g["postgres_full"]["seed"],
                "changed_samples": g["postgres_full"]["changed_samples"],
                "total_edges": g["postgres_full"]["total_edges"],
                "density": g["postgres_full"]["changed_samples"]
                / g["postgres_full"]["total_edges"],
                "pg_full_p50_ms": _p50(g["postgres_full"]),
                "pg_incr_p50_ms": _p50(g["postgres_incremental"]),
                **{
                    f"{name}_{key}": value
                    for name, method in ADAPTIVE.items()
                    for key, value in (
                        ("p50_ms", _p50(g[method])),
                        ("selected", g[method]["selected_strategy"]),
                        ("vs_best", _p50(g[method]) / min(_p50(g[m]) for m in FIXED)),
                        ("vs_incremental", _p50(g[method]) / _p50(g["postgres_incremental"])),
                    )
                },
                "dirty_entities": g["postgres_incremental"]["dirty_entities"],
            }
            for g in sorted(groups, key=lambda g: g["postgres_full"]["seed"])
        ],
        "full_wins": all(_full_wins(g) for g in groups),
        "clear_scenarios": len(clear),
        "v2_correct": correct("v2"),
        "v1_correct": correct("v1"),
        "v2_full_reasons": dict(
            Counter(_full_reason(row) for row in v2_rows if row["selected_strategy"] == "FULL")
        ),
        "incremental_residual_ms": [
            _p50(g["postgres_incremental"]) - g[ADAPTIVE["v2"]]["estimated_incremental_ms"]
            for g in groups
        ],
        "full_residual_ms": [
            _p50(g["postgres_full"]) - g[ADAPTIVE["v2"]]["estimated_full_ms"] for g in groups
        ],
    }


def analyze(rows: list[Row], *, expected_hashes: Set[str], overlap_count: int) -> dict[str, Any]:
    """The whole §6 result; only the §6.1 gate when the evidence is invalid."""
    reasons = correctness_gate(rows, expected_hashes=expected_hashes, overlap_count=overlap_count)
    if reasons:
        return {"correctness_gate": {"verdict": "fails", "reasons": reasons}}
    groups = _groups(rows)
    cells: dict[int, dict[float, list[dict[str, Row]]]] = defaultdict(lambda: defaultdict(list))
    for group in groups.values():
        first = group["postgres_full"]
        cells[first["entities"]][first["change_ratio"]].append(group)
    layers = {
        str(entities): {
            "crossover_density": _crossover(levels),
            "full_win_levels": sorted(
                level for level, gs in levels.items() if all(map(_full_wins, gs))
            ),
        }
        for entities, levels in sorted(cells.items())
    }
    h5a = any(layer["full_win_levels"] for layer in layers.values())
    region = [
        group
        for entities, levels in cells.items()
        for level, gs in levels.items()
        if (crossover := layers[str(entities)]["crossover_density"]) is not None
        and level >= crossover
        for group in gs
    ]
    if h5a and region:
        pooled_region = _pooled(region)
        adaptive, incremental = (
            pooled_region[method]["p50"] for method in (ADAPTIVE["v2"], "postgres_incremental")
        )
        h5c = {
            "verdict": "holds" if adaptive < incremental else "fails",
            "adaptive_p50": adaptive,
            "incremental_p50": incremental,
            "scenarios": len(region),
        }
    else:
        h5c = {"verdict": "not_applicable", "scenarios": len(region)}
    pooled = _pooled(list(groups.values()))
    return {
        "correctness_gate": {"verdict": "holds", "reasons": []},
        "scenario_count": len(groups),
        "h5a": {"verdict": "holds" if h5a else "fails"},
        "layers": layers,
        "h5b": {**_gate(pooled, ADAPTIVE["v2"]), "v1": _gate(pooled, ADAPTIVE["v1"])},
        "h5c": h5c,
        "aggregate_maintenance_ms": pooled,
        "levels": [
            _cell(entities, level, gs)
            for entities, levels in sorted(cells.items())
            for level, gs in sorted(levels.items())
        ],
    }
