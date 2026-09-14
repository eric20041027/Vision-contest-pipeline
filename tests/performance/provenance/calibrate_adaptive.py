"""Collect calibration-only live PostgreSQL timings and publish a frozen policy."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from vcp.core.atomic import write_once_text
from vcp.core.errors import ValidationFailed, VcpError
from vcp.provenance.strategy import (
    CALIBRATION_SEEDS,
    CalibrationEvidence,
    calibration_evidence,
    calibration_text,
    fit_policy,
    write_policy_artifact,
)

if __package__:
    from . import adaptive_benchmark as benchmark
    from .evaluate_adaptive import (
        FIXED_METHODS,
        SafeArgumentParser,
        collect_rows,
        paired_observations,
        validate_calibration_manifest,
        validate_rows,
    )
    from .workloads import scenario_matrix
else:
    import adaptive_benchmark as benchmark
    from evaluate_adaptive import (
        FIXED_METHODS,
        SafeArgumentParser,
        collect_rows,
        paired_observations,
        validate_calibration_manifest,
        validate_rows,
    )
    from workloads import scenario_matrix


def publish_calibration(rows, output):
    """Publish only validated measurements; callers own whether inputs are live or tests.

    The output embeds the exact policy and manifest. Its sibling artifact root
    retains the verified immutable VCP artifact and original calibration pin.
    No held-out datum is accepted by the fitting boundary.
    """
    try:
        output = Path(output).resolve()
        if output.exists():
            raise ValueError
        groups = validate_rows(rows, seeds=CALIBRATION_SEEDS, methods=FIXED_METHODS)
        selected = calibration_evidence(paired_observations(rows))
        coverage = [group["postgres_full"] for group in groups.values()]
        evidence = CalibrationEvidence(
            scenario_ids=tuple(row.scenario_id for row in coverage),
            scenario_hashes=tuple(row.scenario_hash for row in coverage),
            scenario_repetitions=tuple(row.repetitions for row in coverage),
            scenario_workload_hashes=tuple(row.workload_hash for row in coverage),
            observations=selected.observations,
        )
        validate_calibration_manifest(evidence)
        policy = fit_policy(evidence)
        root = output.parent / (output.stem + "-artifacts")
        source = root / "inputs" / "calibration.json"
        write_once_text(source, calibration_text(evidence))
        write_policy_artifact(root, policy, source)
        write_once_text(
            output,
            json.dumps(
                {
                    "kind": "postgres-provenance-calibration-v1",
                    "policy": policy.model_dump(mode="json"),
                    "calibration": evidence.model_dump(mode="json"),
                },
                indent=2,
            )
            + "\n",
        )
        return policy
    except (OSError, TypeError, ValueError, VcpError):
        raise ValidationFailed("calibration_publication_failed") from None


def main(argv=None) -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        pg_runtime = benchmark.postgres_preflight()
        with tempfile.TemporaryDirectory(prefix="vcp-calibration-") as temporary:
            rows = collect_rows(
                Path(temporary),
                scenario_matrix(seeds=CALIBRATION_SEEDS),
                methods=FIXED_METHODS,
                pg_runtime=pg_runtime,
            )
        publish_calibration(rows, args.output)
        print("VERDICT cmd=provenance.calibrate status=OK", file=sys.stderr)
        return 0
    except Exception:
        print("Calibration failed (live PostgreSQL benchmark evidence required)", file=sys.stderr)
        print("VERDICT cmd=provenance.calibrate status=FAIL", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
