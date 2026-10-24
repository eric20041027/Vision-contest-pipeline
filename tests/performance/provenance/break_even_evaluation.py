"""E5 break-even measurement (spec 2026-10-23 §4, §6.1, §7, §8): the four PostgreSQL methods
on every dense scenario, checked against the pre-registration, then the frozen analysis.

Invalid evidence is never published. A failed or missing row, a count the generator does not
pin, a workload the predictions did not describe, a decision the frozen policy would not
make or any overlap with the calibration and held-out partitions fails before the output is
claimed; the checkpoints stay in the work directory. Valid evidence is always published,
whatever the hypotheses say.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Literal, NamedTuple

from pydantic import Field

from vcp.core.atomic import write_once_text
from vcp.core.build import build_string
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.provenance.strategy import (
    CALIBRATION_SEEDS,
    HELDOUT_SEEDS,
    HELDOUT_V2_SEEDS,
    CalibrationEvidence,
    ProvenancePolicy,
    _object_without_duplicate_keys,
)

if __package__:
    from . import adaptive_benchmark as benchmark
    from . import break_even_analysis as analysis
    from . import break_even_predictions as predictions
    from . import evaluate_adaptive as evaluation
    from .workloads import Scenario, dense_counts, dense_matrix
else:
    import adaptive_benchmark as benchmark
    import break_even_analysis as analysis
    import break_even_predictions as predictions
    import evaluate_adaptive as evaluation
    from workloads import Scenario, dense_counts, dense_matrix

KIND = "postgres-provenance-break-even-v1"
SPEC = predictions.SPEC
METHODS = evaluation.HELDOUT_V2_METHODS
ADAPTIVE = (predictions.PRIMARY, predictions.COMPARISON)
# The pre-registration committed before any E5 measurement (spec §6.2); write-once.
PREDICTIONS_SHA256 = "e2f61c8d3185158e8cb84548128cc4f3699dd5d5fdb9a904e1d6b1007fe2164d"
HYPOTHESES = ("h5a", "h5b", "h5c")


class DenseRow(evaluation.BenchmarkRow):
    """A measured dense row: ``entities`` is nominal (the graph has twice as many edges)."""

    scenario_id: str = Field(pattern=r"^dense-\d+-\d+-[0-9a-f]{16}$")
    track: Literal["dense"]
    topology: Literal["flat"]
    entities: int = Field(gt=0)


def _dense_fields(scenario: Scenario) -> dict[str, int]:
    """The generator's arithmetic: one source sample never changes, and there is no history."""
    modified, added, _readings = dense_counts(2 * scenario.entities, scenario.change_ratio)
    return {
        "sample_entities": modified + 1 + added,
        "changed_samples": modified + added,
        "total_edges": 2 * scenario.entities,
        "historical_changes": 0,
    }


DENSE = evaluation.RowFamily(
    DenseRow,
    lambda row: Scenario(row.entities, row.change_ratio, row.topology, row.seed, track="dense"),
    _dense_fields,
)


class Frozen(NamedTuple):
    policies: dict[str, ProvenancePolicy]
    sha256: dict[str, str]
    evidence: CalibrationEvidence
    predictions: dict[str, dict[str, Any]]
    predictions_file: str


def expected_scenarios() -> dict[str, Scenario]:
    return {s.scenario_hash: s for s in sorted(dense_matrix(), key=lambda s: s.scenario_hash)}


def load_frozen(policy_from: Path, comparison_from: Path, predictions_from: Path) -> Frozen:
    """The frozen v2 and v1 and the committed predictions about exactly these two policies
    and every dense scenario; anything else fails before a service is touched."""
    pair = predictions.load_policies(policy_from, comparison_from)
    path = Path(predictions_from)
    try:
        if sha256_file(path) != PREDICTIONS_SHA256:
            raise ValueError
        document = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_object_without_duplicate_keys
        )
        policies = {
            method: {"policy_id": policy.id, "policy_sha256": pair.sha256[method]}
            for method, policy in pair.policies.items()
        }
        rows = document["scenarios"]
        if (
            document["kind"] != predictions.KIND
            or document["policies"] != policies
            or [row["scenario_hash"] for row in rows] != [s.scenario_hash for s in dense_matrix()]
        ):
            raise ValueError
    except (OSError, TypeError, ValueError, KeyError):
        raise ValidationFailed("invalid_predictions") from None
    return Frozen(
        pair.policies,
        pair.sha256,
        pair.evidence,
        {row["scenario_hash"]: row for row in rows},
        path.name,
    )


def overlap_count(evidence: CalibrationEvidence, rows) -> int:
    """Distinct E5 scenarios that share a seed with, or name a scenario id, scenario hash or
    workload of, the calibration or either held-out partition (spec §3.3)."""
    earlier = [
        evaluation.expected_scenarios(seeds)
        for seeds in (CALIBRATION_SEEDS, HELDOUT_SEEDS, HELDOUT_V2_SEEDS)
    ]
    seeds = {scenario.seed for scenarios in earlier for scenario in scenarios.values()}
    names = {
        value
        for scenarios in earlier
        for scenario in scenarios.values()
        for value in (scenario.scenario_id, scenario.scenario_hash)
    }
    names.update(evidence.scenario_workload_hashes or ())
    overlapping = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        fields = (row.get(key) for key in ("scenario_id", "scenario_hash", "workload_hash"))
        if row.get("seed") in seeds or any(value in names for value in fields):
            overlapping.add(row.get("scenario_hash"))
    return len(overlapping)


def _agreement(groups, frozen: Frozen) -> dict[str, dict[str, Any]]:
    """Measured selections beside the pre-registered ones; a diagnostic, never a gate."""
    result = {method: {"agree": 0, "disagree": []} for method in ADAPTIVE}
    for scenario_hash, group in groups.items():
        for method in ADAPTIVE:
            predicted = frozen.predictions[scenario_hash][method]["selected"]
            measured = group[method].selected_strategy
            if measured == predicted:
                result[method]["agree"] += 1
            else:
                result[method]["disagree"].append(
                    {"scenario_hash": scenario_hash, "predicted": predicted, "measured": measured}
                )
    return result


def evaluate(rows, frozen: Frozen) -> dict[str, Any]:
    """Validate the measured rows against the pre-registration, then run the frozen analysis
    (spec §7: decisions are re-derived here, before the analysis sees any row)."""
    overlaps = overlap_count(frozen.evidence, rows)
    if overlaps:
        raise ValidationFailed("workload_leakage", fields={"overlap_count": overlaps})
    expected = expected_scenarios()
    groups = evaluation.validate_family_rows(rows, expected, methods=METHODS, family=DENSE)
    for scenario_hash, group in groups.items():
        if any(
            row.workload_hash != frozen.predictions[scenario_hash]["workload_hash"]
            for row in group.values()
        ):
            raise ValidationFailed("unregistered_workload")
        for method in ADAPTIVE:
            evaluation._check_adaptive_row(
                group[method], frozen.policies[method], frozen.sha256[method]
            )
    result = analysis.analyze(rows, expected_hashes=set(expected), overlap_count=overlaps)
    if result["correctness_gate"]["verdict"] != "holds":
        raise ValidationFailed("correctness_gate_failed")
    return {
        "policies": {
            method: {
                "policy_id": frozen.policies[method].id,
                "policy_sha256": frozen.sha256[method],
            }
            for method in ADAPTIVE
        },
        "predictions": {"file": frozen.predictions_file, "sha256": PREDICTIONS_SHA256},
        "overlap_count": overlaps,
        "prediction_agreement": _agreement(groups, frozen),
        "analysis": result,
    }


def _verdict_line(result: dict[str, Any]) -> str:
    verdicts = {name: result["analysis"][name]["verdict"] for name in HYPOTHESES}
    status = "OK" if all(value == "holds" for value in verdicts.values()) else "WARN"
    fields = " ".join(f"{name}={value}" for name, value in verdicts.items())
    return f"VERDICT cmd=provenance.break_even status={status} {fields}"


def _run(args) -> int:
    if args.output.exists():
        raise ValidationFailed("break_even_output_exists")
    frozen = load_frozen(args.policy_from, args.comparison_policy_from, args.predictions_from)
    policy, comparison = (frozen.policies[method] for method in ADAPTIVE)
    pg_runtime = benchmark.postgres_preflight()
    benchmark.require_policy_environment(pg_runtime, policy, comparison)
    rows = evaluation.collect_rows(
        args.work_dir or args.output.with_name(args.output.stem + "-work"),
        dense_matrix(),
        methods=METHODS,
        pg_runtime=pg_runtime,
        policy=policy,
        evidence=frozen.evidence,
        policy_from=args.policy_from,
        comparison=comparison,
        comparison_from=args.comparison_policy_from,
        isolated=True,
    )
    result = evaluate(rows, frozen)
    document = {
        "kind": KIND,
        "spec": SPEC,
        "vcp": build_string(),
        "evaluation": result,
        "results": rows,
    }
    benchmark.validate_publication_explain(document)
    write_once_text(args.output, json.dumps(document, indent=2) + "\n")
    print(_verdict_line(result), file=sys.stderr)
    return 0


def main(argv=None) -> int:
    parser = evaluation.SafeArgumentParser(description=__doc__)
    parser.add_argument("--policy-from", type=Path, required=True)
    parser.add_argument("--comparison-policy-from", type=Path, required=True)
    parser.add_argument("--predictions-from", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    try:
        return _run(parser.parse_args(argv))
    except Exception as error:
        print(
            "Break-even evaluation failed (live service or verified evidence required): "
            + benchmark.error_summary(error),
            file=sys.stderr,
        )
        print("VERDICT cmd=provenance.break_even status=FAIL", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
