"""E5 pre-registration (spec 2026-10-23 §6.2): what the frozen policies predict for every
dense scenario, written once before any E5 measurement.

Building a workload times nothing. Each prediction reads the same canonical graph the
PostgreSQL decision sees -- after the delta, with both policies installed. Dirty entities are
only known once a backend plans the ingest; neither cost model sees them (checked), so they
are 0 here and cannot change a prediction.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

from vcp.core.atomic import write_once_text
from vcp.core.build import build_string
from vcp.provenance.backend import RequestedStrategy
from vcp.provenance.index import dataset_heads
from vcp.provenance.policy_bands import edges_decade, width
from vcp.provenance.strategy import MaintenanceFeatures, ProvenancePolicy, select_strategy

if __package__:
    from . import adaptive_benchmark as benchmark
    from .evaluate_adaptive import SafeArgumentParser, prepare_workload
    from .workloads import DENSE_SEEDS, Scenario, Workload, build_scenario, dense_matrix
else:
    import adaptive_benchmark as benchmark
    from evaluate_adaptive import SafeArgumentParser, prepare_workload
    from workloads import DENSE_SEEDS, Scenario, Workload, build_scenario, dense_matrix

KIND = "postgres-provenance-break-even-predictions-v1"
SPEC = "docs/superpowers/specs/2026-10-23-vcp-e5-break-even-design.md"
PRIMARY, COMPARISON = "postgres_adaptive", "postgres_adaptive_v1"
UNSEEN_FEATURES = ("dirty_entities", "dirty_ratio")


class FrozenPair(NamedTuple):
    policies: dict[str, ProvenancePolicy]
    sha256: dict[str, str]
    evidence: Any


def require_blind_to_dirty(policy: ProvenancePolicy) -> None:
    for model in (policy.incremental_model, policy.full_model):
        if any(model.coefficients.get(name, 0.0) != 0.0 for name in UNSEEN_FEATURES):
            raise ValueError("policy weighs dirty entities; predictions would need planning")


def load_policies(policy_from: Path, comparison_from: Path) -> FrozenPair:
    """The frozen v2 and the v1 it was derived from (the held-out v2 guard)."""
    policy, evidence, policy_sha256 = benchmark.load_frozen_policy(Path(policy_from))
    comparison, comparison_evidence, comparison_sha256 = benchmark.load_frozen_policy(
        Path(comparison_from)
    )
    benchmark.require_policy_pair(policy, evidence, comparison, comparison_evidence)
    for frozen in (policy, comparison):
        require_blind_to_dirty(frozen)
    return FrozenPair(
        {PRIMARY: policy, COMPARISON: comparison},
        {PRIMARY: policy_sha256, COMPARISON: comparison_sha256},
        evidence,
    )


def _band_ms(policy: ProvenancePolicy, incremental: float, full: float, edges: int) -> float:
    band = getattr(policy, "band", None)
    if band is None:
        return policy.incremental_rmse_ms + policy.full_rmse_ms
    return width(band, "incremental", incremental, edges) + width(band, "full", full, edges)


def scenario_prediction(workload: Workload, frozen: FrozenPair) -> dict[str, Any]:
    prepared = prepare_workload(
        workload, frozen.policies[PRIMARY], frozen.evidence, (frozen.policies[COMPARISON],)
    )
    graph = prepared.expected
    scenario = workload.scenario
    edges = len(graph.edges)
    features = MaintenanceFeatures(
        changed_samples=workload.changed_samples,
        dirty_entities=0,
        total_entities=len(graph.entities),
        dirty_ratio=0.0,
        total_edges=edges,
        historical_changes=workload.historical_changes,
        head_count=len(dataset_heads(graph)),
    )
    kinds = {kind: 0 for kind in ("MODIFIED", "ADDED")}
    for change in graph.changes.values():
        kinds[change.change_type.value] += 1
    row: dict[str, Any] = {
        "scenario_id": scenario.scenario_id,
        "scenario_hash": scenario.scenario_hash,
        "seed": scenario.seed,
        "entities": scenario.entities,
        "target_density": scenario.change_ratio,
        "modified": kinds["MODIFIED"],
        "added": kinds["ADDED"],
        "changed_samples": workload.changed_samples,
        "total_edges": edges,
        "historical_changes": workload.historical_changes,
        "density": workload.changed_samples / edges,
        "decade": edges_decade(edges),
        "workload_hash": prepared.workload_hash,
    }
    for method, policy in frozen.policies.items():
        decision = select_strategy(RequestedStrategy.AUTO, features, policy)
        incremental, full = decision.estimated_incremental_ms, decision.estimated_full_ms
        row[method] = {
            "estimated_incremental_ms": incremental,
            "estimated_full_ms": full,
            "gap_ms": full - incremental,
            "band_ms": _band_ms(policy, incremental, full, edges),
            "model_cheaper": "INCREMENTAL" if incremental < full else "FULL",
            "selected": decision.selected_strategy.value,
            "reason": decision.reason,
        }
    return row


def build_document(scenarios: list[Scenario], frozen: FrozenPair, work_dir: Path) -> dict:
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    rows = []
    for scenario in scenarios:
        with tempfile.TemporaryDirectory(prefix="dense-", dir=work_dir) as temporary:
            workload = build_scenario(Path(temporary) / "fixture", scenario)
            rows.append(scenario_prediction(workload, frozen))
    return {
        "kind": KIND,
        "spec": SPEC,
        "vcp": build_string(),
        "seeds": list(DENSE_SEEDS),
        "policies": {
            method: {"policy_id": policy.id, "policy_sha256": frozen.sha256[method]}
            for method, policy in frozen.policies.items()
        },
        "scenarios": rows,
    }


def write_document(document: dict, output: Path) -> None:
    write_once_text(Path(output), json.dumps(document, indent=2) + "\n")


def main(argv=None) -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--policy-from", type=Path, required=True)
    parser.add_argument("--comparison-policy-from", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    try:
        args = parser.parse_args(argv)
        frozen = load_policies(args.policy_from, args.comparison_policy_from)
        work_dir = args.work_dir or args.output.with_name(args.output.stem + "-work")
        write_document(build_document(dense_matrix(), frozen, work_dir), args.output)
        print("VERDICT cmd=provenance.break_even_predictions status=OK", file=sys.stderr)
        return 0
    except Exception as error:
        print("Break-even predictions failed: " + benchmark.error_summary(error), file=sys.stderr)
        print("VERDICT cmd=provenance.break_even_predictions status=FAIL", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
