"""E5 rerun pre-registered analysis (spec 2026-11-15 §5-§6).

The E5 analysis (``break_even_analysis.py``, frozen with its evidence) without policy v1,
which this rerun does not measure, plus the two ranges the rerun reports. Reads only the
measured rows and the runner's overlap count. Committed before any rerun measurement; never
edited after the first one.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Set
from typing import Any

FIXED = ("postgres_full", "postgres_incremental")
ADAPTIVE = "postgres_adaptive"
METHODS = (*FIXED, ADAPTIVE)
PARITY_FIELDS = ("graph_parity", "graph_hash_parity", "head_parity", "status_parity")
FULL_WIN = 0.95  # H5a: pg_full p50 <= 0.95 x pg_incr p50
CLEAR_GAP = 1.05  # diagnostics: the fixed methods differ by more than 5%
MEDIAN_GATE, P95_GATE = 1.05, 1.10  # H5b, the E3 gate

Row = dict[str, Any]
Group = dict[str, Row]


def correctness_gate(
    rows: list[Row], *, expected_hashes: Set[str], overlap_count: int
) -> list[str]:
    """The reasons the evidence is invalid; empty when it passes. Touches no timing, so a
    failed row (no percentiles) is a reason, never a crash."""
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


def _groups(rows: list[Row]) -> dict[str, Group]:
    groups: dict[str, Group] = defaultdict(dict)
    for row in rows:
        groups[row["scenario_hash"]][row["method"]] = row
    return dict(groups)


def _p50(row: Row) -> float:
    return row["maintenance_p50_ms"]


def _full_vs_incremental(group: Group) -> float:
    return _p50(group["postgres_full"]) / _p50(group["postgres_incremental"])


def _adaptive_vs_incremental(group: Group) -> float:
    return _p50(group[ADAPTIVE]) / _p50(group["postgres_incremental"])


def _full_wins(group: Group) -> bool:
    return _p50(group["postgres_full"]) <= FULL_WIN * _p50(group["postgres_incremental"])


def _pooled(groups: list[Group]) -> dict[str, dict[str, float]]:
    """E3's aggregate: each method's raw samples pooled, then the p50 and nearest-rank p95."""
    result = {}
    for method in METHODS:
        values = sorted(s["maintenance_ms"] for g in groups for s in g[method]["samples"])
        result[method] = {
            "p50": statistics.median(values),
            "p95": values[round(0.95 * (len(values) - 1))],
        }
    return result


def _gate(pooled: dict[str, dict[str, float]]) -> dict[str, Any]:
    median = pooled[ADAPTIVE]["p50"] / min(pooled[m]["p50"] for m in FIXED)
    p95 = pooled[ADAPTIVE]["p95"] / min(pooled[m]["p95"] for m in FIXED)
    passed = median <= MEDIAN_GATE and p95 <= P95_GATE
    return {"verdict": "holds" if passed else "fails", "median_ratio": median, "p95_ratio": p95}


def _crossover(cells: dict[float, list[Group]]) -> float | None:
    """The lowest level from which every level up to the top has FULL winning everywhere."""
    crossover = None
    for level in sorted(cells, reverse=True):
        if not all(_full_wins(group) for group in cells[level]):
            break
        crossover = level
    return crossover


def _range(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    return {"min": min(values), "max": max(values), "scenarios": len(values)}


def _layer(levels: dict[float, list[Group]]) -> dict[str, Any]:
    """One layer's crossover, the level just below it, and the two reported ranges. A layer
    without a crossover has every level below it."""
    crossover = _crossover(levels)
    below = sorted(level for level in levels if crossover is None or level < crossover)
    above = sorted(level for level in levels if crossover is not None and level >= crossover)
    return {
        "crossover_density": crossover,
        "level_below_crossover": below[-1] if crossover is not None and below else None,
        "full_win_levels": sorted(
            level for level, groups in levels.items() if all(map(_full_wins, groups))
        ),
        "full_vs_incremental_above_crossover": _range(
            [_full_vs_incremental(g) for level in above for g in levels[level]]
        ),
        "v2_vs_incremental_below_crossover": _range(
            [_adaptive_vs_incremental(g) for level in below for g in levels[level]]
        ),
    }


def _full_reason(row: Row) -> str:
    """v2's two FULL cases share one strategy_reason; only the estimates tell them apart."""
    if row["estimated_full_ms"] < row["estimated_incremental_ms"]:
        return "model_predicts_full"
    return "uncertain"


def _faster_fixed(group: Group) -> str:
    full, incremental = (_p50(group[m]) for m in FIXED)
    return "FULL" if full < incremental else "INCREMENTAL"


def _is_clear(group: Group) -> bool:
    timings = [_p50(group[m]) for m in FIXED]
    return max(timings) > CLEAR_GAP * min(timings)


def _cell(entities: int, level: float, groups: list[Group]) -> dict[str, Any]:
    clear = [g for g in groups if _is_clear(g)]
    v2_rows = [g[ADAPTIVE] for g in groups]
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
                "full_vs_incremental": _full_vs_incremental(g),
                "v2_p50_ms": _p50(g[ADAPTIVE]),
                "v2_selected": g[ADAPTIVE]["selected_strategy"],
                "v2_vs_best": _p50(g[ADAPTIVE]) / min(_p50(g[m]) for m in FIXED),
                "v2_vs_incremental": _adaptive_vs_incremental(g),
                "estimated_incremental_ms": g[ADAPTIVE]["estimated_incremental_ms"],
                "estimated_full_ms": g[ADAPTIVE]["estimated_full_ms"],
                "dirty_entities": g["postgres_incremental"]["dirty_entities"],
            }
            for g in sorted(groups, key=lambda g: g["postgres_full"]["seed"])
        ],
        # The per-level table of the report: the mean of the scenarios' p50s.
        "mean_p50_ms": {
            "pg_full": statistics.fmean(_p50(g["postgres_full"]) for g in groups),
            "pg_incr": statistics.fmean(_p50(g["postgres_incremental"]) for g in groups),
            "v2": statistics.fmean(_p50(g[ADAPTIVE]) for g in groups),
        },
        "v2_selected": sorted({row["selected_strategy"] for row in v2_rows}),
        "full_wins": all(_full_wins(g) for g in groups),
        "clear_scenarios": len(clear),
        "v2_correct": sum(g[ADAPTIVE]["selected_strategy"] == _faster_fixed(g) for g in clear),
        "v2_full_reasons": dict(
            Counter(_full_reason(row) for row in v2_rows if row["selected_strategy"] == "FULL")
        ),
        "incremental_residual_ms": [
            _p50(g["postgres_incremental"]) - g[ADAPTIVE]["estimated_incremental_ms"]
            for g in groups
        ],
        "full_residual_ms": [
            _p50(g["postgres_full"]) - g[ADAPTIVE]["estimated_full_ms"] for g in groups
        ],
    }


def _h5c(region: list[Group], h5a: bool) -> dict[str, Any]:
    if not h5a or not region:
        return {"verdict": "not_applicable", "scenarios": len(region)}
    pooled = _pooled(region)
    adaptive, incremental = pooled[ADAPTIVE]["p50"], pooled["postgres_incremental"]["p50"]
    return {
        "verdict": "holds" if adaptive < incremental else "fails",
        "adaptive_p50": adaptive,
        "incremental_p50": incremental,
        "ratio": adaptive / incremental,
        "scenarios": len(region),
    }


def analyze(rows: list[Row], *, expected_hashes: Set[str], overlap_count: int) -> dict[str, Any]:
    """The whole result; only the correctness gate when the evidence is invalid."""
    reasons = correctness_gate(rows, expected_hashes=expected_hashes, overlap_count=overlap_count)
    if reasons:
        return {"correctness_gate": {"verdict": "fails", "reasons": reasons}}
    groups = _groups(rows)
    cells: dict[int, dict[float, list[Group]]] = defaultdict(lambda: defaultdict(list))
    for group in groups.values():
        first = group["postgres_full"]
        cells[first["entities"]][first["change_ratio"]].append(group)
    layers = {str(entities): _layer(levels) for entities, levels in sorted(cells.items())}
    h5a = any(layer["full_win_levels"] for layer in layers.values())
    region = [
        group
        for entities, levels in cells.items()
        for level, level_groups in levels.items()
        if (crossover := layers[str(entities)]["crossover_density"]) is not None
        and level >= crossover
        for group in level_groups
    ]
    pooled = _pooled(list(groups.values()))
    return {
        "correctness_gate": {"verdict": "holds", "reasons": []},
        "scenario_count": len(groups),
        "h5a": {"verdict": "holds" if h5a else "fails"},
        "layers": layers,
        "h5b": _gate(pooled),
        "h5c": _h5c(region, h5a),
        "aggregate_maintenance_ms": pooled,
        "levels": [
            _cell(entities, level, level_groups)
            for entities, levels in sorted(cells.items())
            for level, level_groups in sorted(levels.items())
        ],
    }
