"""E5 rerun measurement (spec 2026-11-15): the two fixed PostgreSQL methods and policy v2 on
every dense scenario of the rerun seeds, checked against the pre-registration, then the
frozen rerun analysis.

The E5 runner (``break_even_evaluation.py``) without policy v1, on the code after the status
fix (PR #56). Invalid evidence is never published: a failed or missing row, a count the
generator does not pin, a workload the predictions did not describe, a decision the frozen
policy would not make, or any overlap with the calibration, either held-out partition or E5
itself fails before the output is claimed; the checkpoints stay in the work directory. Valid
evidence is always published, whatever the hypotheses say.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, NamedTuple

from vcp.core.atomic import write_once_text
from vcp.core.build import build_string
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.time import stamp
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
    from . import break_even_evaluation as first
    from . import break_even_rerun_analysis as analysis
    from . import break_even_rerun_predictions as predictions
    from . import evaluate_adaptive as evaluation
    from .workloads import Scenario, dense_matrix
else:
    import adaptive_benchmark as benchmark
    import break_even_evaluation as first
    import break_even_rerun_analysis as analysis
    import break_even_rerun_predictions as predictions
    import evaluate_adaptive as evaluation
    from workloads import Scenario, dense_matrix

KIND = "postgres-provenance-break-even-v2"
SPEC = predictions.SPEC
METHODS = evaluation.EVALUATION_METHODS
METHOD = predictions.METHOD
# The pre-registration committed before any rerun measurement (spec §4); write-once.
PREDICTIONS_SHA256 = "19c00f9daf5493cb5b0486ca425bb432f9aa8b3ed269a03962269a7f86657913"
# E5's own pre-registration: its workloads count as earlier ones in the overlap check.
FIRST_PREDICTIONS = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "benchmarks"
    / "postgres-provenance-break-even-v1-predictions.json"
)
HYPOTHESES = ("h5a", "h5b", "h5c")


class Frozen(NamedTuple):
    policy: ProvenancePolicy
    sha256: str
    evidence: CalibrationEvidence
    predictions: dict[str, dict[str, Any]]
    predictions_file: str


def expected_scenarios() -> dict[str, Scenario]:
    matrix = predictions.rerun_matrix()
    return {s.scenario_hash: s for s in sorted(matrix, key=lambda s: s.scenario_hash)}


def load_frozen(policy_from: Path, predictions_from: Path) -> Frozen:
    """The frozen v2 and the committed predictions about exactly that policy and every rerun
    scenario; anything else fails before a service is touched."""
    frozen = predictions.load_policy(policy_from)
    path = Path(predictions_from)
    try:
        if sha256_file(path) != PREDICTIONS_SHA256:
            raise ValueError
        document = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_object_without_duplicate_keys
        )
        rows = document["scenarios"]
        matrix = predictions.rerun_matrix()
        if (
            document["kind"] != predictions.KIND
            or document["policy"] != {"policy_id": frozen.policy.id, "policy_sha256": frozen.sha256}
            or [row["scenario_hash"] for row in rows] != [s.scenario_hash for s in matrix]
        ):
            raise ValueError
    except (OSError, TypeError, ValueError, KeyError):
        raise ValidationFailed("invalid_predictions") from None
    return Frozen(
        frozen.policy,
        frozen.sha256,
        frozen.evidence,
        {row["scenario_hash"]: row for row in rows},
        path.name,
    )


def _first_workload_hashes() -> set[str]:
    """The workloads E5 measured, from its write-once pre-registration."""
    try:
        if sha256_file(FIRST_PREDICTIONS) != first.PREDICTIONS_SHA256:
            raise ValueError
        document = json.loads(FIRST_PREDICTIONS.read_text(encoding="utf-8"))
        return {row["workload_hash"] for row in document["scenarios"]}
    except (OSError, TypeError, ValueError, KeyError):
        raise ValidationFailed("invalid_first_predictions") from None


def overlap_count(evidence: CalibrationEvidence, rows) -> int:
    """Distinct rerun scenarios that share a seed with, or name a scenario id, scenario hash
    or workload of, the calibration, either held-out partition or E5 (spec §3)."""
    earlier = [
        evaluation.expected_scenarios(seeds)
        for seeds in (CALIBRATION_SEEDS, HELDOUT_SEEDS, HELDOUT_V2_SEEDS)
    ]
    earlier.append({scenario.scenario_hash: scenario for scenario in dense_matrix()})
    seeds = {scenario.seed for scenarios in earlier for scenario in scenarios.values()}
    names = {
        value
        for scenarios in earlier
        for scenario in scenarios.values()
        for value in (scenario.scenario_id, scenario.scenario_hash)
    }
    names.update(evidence.scenario_workload_hashes or ())
    names.update(_first_workload_hashes())
    overlapping = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        fields = (row.get(key) for key in ("scenario_id", "scenario_hash", "workload_hash"))
        if row.get("seed") in seeds or any(value in names for value in fields):
            overlapping.add(row.get("scenario_hash"))
    return len(overlapping)


def _agreement(groups, frozen: Frozen) -> dict[str, Any]:
    """Measured selections beside the pre-registered ones; a diagnostic, never a gate."""
    result: dict[str, Any] = {"agree": 0, "disagree": []}
    for scenario_hash, group in groups.items():
        predicted = frozen.predictions[scenario_hash][METHOD]["selected"]
        measured = group[METHOD].selected_strategy
        if measured == predicted:
            result["agree"] += 1
        else:
            result["disagree"].append(
                {"scenario_hash": scenario_hash, "predicted": predicted, "measured": measured}
            )
    return result


def evaluate(rows, frozen: Frozen) -> dict[str, Any]:
    """Validate the measured rows against the pre-registration, then run the frozen analysis
    (the decision of every adaptive row is re-derived here, before the analysis sees it)."""
    overlaps = overlap_count(frozen.evidence, rows)
    if overlaps:
        raise ValidationFailed("workload_leakage", fields={"overlap_count": overlaps})
    expected = expected_scenarios()
    groups = evaluation.validate_family_rows(rows, expected, methods=METHODS, family=first.DENSE)
    for scenario_hash, group in groups.items():
        if any(
            row.workload_hash != frozen.predictions[scenario_hash]["workload_hash"]
            for row in group.values()
        ):
            raise ValidationFailed("unregistered_workload")
        evaluation._check_adaptive_row(group[METHOD], frozen.policy, frozen.sha256)
    result = analysis.analyze(rows, expected_hashes=set(expected), overlap_count=overlaps)
    if result["correctness_gate"]["verdict"] != "holds":
        raise ValidationFailed("correctness_gate_failed")
    return {
        "policy": {"policy_id": frozen.policy.id, "policy_sha256": frozen.sha256},
        "predictions": {"file": frozen.predictions_file, "sha256": PREDICTIONS_SHA256},
        # The pre-registration forbids editing it after the first measurement; the bytes that ran.
        "analysis_script": {
            "file": Path(analysis.__file__).name,
            "sha256": sha256_file(Path(analysis.__file__)),
        },
        "seeds": list(predictions.RERUN_SEEDS),
        "overlap_count": overlaps,
        "prediction_agreement": _agreement(groups, frozen),
        "analysis": result,
    }


def _verdict_line(result: dict[str, Any]) -> str:
    verdicts = {name: result["analysis"][name]["verdict"] for name in HYPOTHESES}
    status = "OK" if all(value == "holds" for value in verdicts.values()) else "WARN"
    fields = " ".join(f"{name}={value}" for name, value in verdicts.items())
    return f"VERDICT cmd=provenance.break_even_rerun status={status} {fields}"


def _run(args) -> int:
    if args.output.exists():
        raise ValidationFailed("break_even_output_exists")
    frozen = load_frozen(args.policy_from, args.predictions_from)
    pg_runtime = benchmark.postgres_preflight()
    benchmark.require_policy_environment(pg_runtime, frozen.policy)
    started = stamp()
    rows = evaluation.collect_rows(
        args.work_dir or args.output.with_name(args.output.stem + "-work"),
        predictions.rerun_matrix(),
        methods=METHODS,
        pg_runtime=pg_runtime,
        policy=frozen.policy,
        evidence=frozen.evidence,
        policy_from=args.policy_from,
        isolated=True,
    )
    result = evaluate(rows, frozen)
    document = {
        "kind": KIND,
        "spec": SPEC,
        "vcp": build_string(),
        # This process only: a resumed run starts later than the first measurement.
        "runner": {"started_utc": started, "finished_utc": stamp()},
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
    parser.add_argument("--predictions-from", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    try:
        return _run(parser.parse_args(argv))
    except Exception as error:
        print(
            "Break-even rerun failed (live service or verified evidence required): "
            + benchmark.error_summary(error),
            file=sys.stderr,
        )
        print("VERDICT cmd=provenance.break_even_rerun status=FAIL", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
