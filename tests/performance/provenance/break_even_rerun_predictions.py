"""E5 rerun pre-registration (spec 2026-11-15 §4): what the frozen policy v2 predicts for
every dense scenario of the rerun seeds, written once before any rerun measurement.

Building a workload times nothing. Each prediction reads the same canonical graph the
PostgreSQL decision sees -- after the delta, with policy v2 installed. Policy v1 is not
installed (the rerun does not measure it), so these workload hashes differ from E5's.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

from vcp.core.atomic import write_once_text
from vcp.core.build import build_string
from vcp.core.errors import ValidationFailed
from vcp.provenance.backend import RequestedStrategy
from vcp.provenance.index import dataset_heads
from vcp.provenance.policy_bands import edges_decade
from vcp.provenance.strategy import MaintenanceFeatures, ProvenancePolicy, select_strategy

if __package__:
    from . import adaptive_benchmark as benchmark
    from .break_even_predictions import _band_ms, require_blind_to_dirty
    from .evaluate_adaptive import SafeArgumentParser, prepare_workload
    from .workloads import Scenario, Workload, build_scenario, dense_matrix
else:
    import adaptive_benchmark as benchmark
    from break_even_predictions import _band_ms, require_blind_to_dirty
    from evaluate_adaptive import SafeArgumentParser, prepare_workload
    from workloads import Scenario, Workload, build_scenario, dense_matrix

KIND = "postgres-provenance-break-even-predictions-v2"
SPEC = "docs/superpowers/specs/2026-11-15-vcp-e5-rerun-design.md"
# Never used before: not E5's (20261201/02), a held-out partition's or a calibration seed.
RERUN_SEEDS = (20261115, 20261116)
METHOD = "postgres_adaptive"
# The frozen policy v2; the rerun neither refits it nor changes its band.
POLICY_ID = "postgres-adaptive-v2-9f4e58346529"
POLICY_SHA256 = "6fe654a90468edb57aa84fdec7ac2e53cf18b20106f7a77d65df8ac7cd27c000"


class FrozenPolicy(NamedTuple):
    policy: ProvenancePolicy
    sha256: str
    evidence: Any


def rerun_matrix() -> list[Scenario]:
    """E5's dense family -- the same two layers and seven levels -- under the rerun seeds."""
    return dense_matrix(seeds=RERUN_SEEDS)


def load_policy(policy_from: Path) -> FrozenPolicy:
    policy, evidence, policy_sha256 = benchmark.load_frozen_policy(Path(policy_from))
    if (policy.id, policy_sha256) != (POLICY_ID, POLICY_SHA256):
        raise ValidationFailed("invalid_policy")
    require_blind_to_dirty(policy)
    return FrozenPolicy(policy, policy_sha256, evidence)


def scenario_prediction(workload: Workload, frozen: FrozenPolicy) -> dict[str, Any]:
    prepared = prepare_workload(workload, frozen.policy, frozen.evidence)
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
    decision = select_strategy(RequestedStrategy.AUTO, features, frozen.policy)
    incremental, full = decision.estimated_incremental_ms, decision.estimated_full_ms
    return {
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
        METHOD: {
            "estimated_incremental_ms": incremental,
            "estimated_full_ms": full,
            "gap_ms": full - incremental,
            "band_ms": _band_ms(frozen.policy, incremental, full, edges),
            "model_cheaper": "INCREMENTAL" if incremental < full else "FULL",
            "selected": decision.selected_strategy.value,
            "reason": decision.reason,
        },
    }


def build_document(scenarios: list[Scenario], frozen: FrozenPolicy, work_dir: Path) -> dict:
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
        "seeds": sorted({scenario.seed for scenario in scenarios}),
        "policy": {"policy_id": frozen.policy.id, "policy_sha256": frozen.sha256},
        "scenarios": rows,
    }


def main(argv=None) -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--policy-from", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    try:
        args = parser.parse_args(argv)
        frozen = load_policy(args.policy_from)
        work_dir = args.work_dir or args.output.with_name(args.output.stem + "-work")
        document = build_document(rerun_matrix(), frozen, work_dir)
        write_once_text(Path(args.output), json.dumps(document, indent=2) + "\n")
        print("VERDICT cmd=provenance.break_even_rerun_predictions status=OK", file=sys.stderr)
        return 0
    except Exception as error:
        print(
            "Break-even rerun predictions failed: " + benchmark.error_summary(error),
            file=sys.stderr,
        )
        print("VERDICT cmd=provenance.break_even_rerun_predictions status=FAIL", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
