"""Publish policy v2 from the frozen calibration v2 and the band comparison's winner.

No measurement here (spec 2026-10-09 §4.3): v2's cost models are refitted from the very
calibration v1 was fitted from, and must equal v1's; only the band is new.

    uv run python tests/performance/provenance/publish_policy_v2.py \
        --calibration-from docs/benchmarks/postgres-provenance-calibration-v2.json \
        --band-comparison docs/benchmarks/postgres-provenance-band-comparison-v1.json \
        --output docs/benchmarks/postgres-provenance-policy-v2.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from vcp.core.atomic import write_once_text
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file, sha256_text
from vcp.provenance.policy_bands import BAND_KINDS
from vcp.provenance.strategy import (
    AdaptivePolicyV2,
    CostModel,
    calibration_text,
    fit_policy_v2,
    write_policy_artifact,
)

if __package__:
    from .evaluate_adaptive import empirical_crossover, load_calibration
else:
    from evaluate_adaptive import empirical_crossover, load_calibration

DRIFT = 1e-9  # spec §4.3: v2's coefficients equal v1's within this relative error


def _band_record(band_comparison: Path, output: Path, calibration_sha256: str) -> dict:
    band_comparison = Path(band_comparison)
    if band_comparison.resolve().parent != Path(output).resolve().parent:
        raise ValidationFailed("invalid_band_comparison: keep it beside the policy document")
    try:
        comparison = json.loads(band_comparison.read_text(encoding="utf-8"))
        valid = (
            comparison["kind"] == "postgres-provenance-band-comparison-v1"
            and comparison["winner"] in BAND_KINDS
            and comparison["calibration_sha256"] == calibration_sha256
        )
    except (OSError, ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise ValidationFailed("invalid_band_comparison")
    return {
        "path": band_comparison.name,
        "sha256": sha256_file(band_comparison),
        "winner": comparison["winner"],
    }


def _same_model(first: CostModel, second: CostModel) -> bool:
    if first.feature_order != second.feature_order:
        return False
    pairs = [(first.intercept_ms, second.intercept_ms)]
    pairs += [(first.coefficients[n], second.coefficients[n]) for n in first.feature_order]
    return all(math.isclose(a, b, rel_tol=DRIFT, abs_tol=0.0) for a, b in pairs)


def publish_policy_v2(
    calibration_from: Path, band_comparison: Path, output: Path
) -> AdaptivePolicyV2:
    # The artifact store resolves a relative input path below its own root, so be absolute.
    output = Path(output).resolve()
    v1, evidence = load_calibration(calibration_from)
    record = _band_record(band_comparison, output, sha256_text(calibration_text(evidence)))
    policy = fit_policy_v2(evidence, band=record["winner"])
    if not (
        _same_model(policy.incremental_model, v1.incremental_model)
        and _same_model(policy.full_model, v1.full_model)
    ):
        raise ValidationFailed("cost_model_drift: v2 refit differs from v1; stop and investigate")
    root = output.parent / (output.stem + "-artifacts")
    source = root / "inputs" / "calibration.json"
    write_once_text(source, calibration_text(evidence))
    write_policy_artifact(root, policy, source)
    document = {
        "kind": "postgres-provenance-policy-v2",
        "policy": policy.model_dump(mode="json"),
        "calibration": evidence.model_dump(mode="json"),
        "empirical_crossover": empirical_crossover(evidence),
        "band_comparison": record,
    }
    write_once_text(output, json.dumps(document, indent=2) + "\n")
    return policy


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-from", type=Path, required=True)
    parser.add_argument("--band-comparison", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValidationFailed("policy_output_exists")
        policy = publish_policy_v2(args.calibration_from, args.band_comparison, args.output)
    except Exception as error:  # a script: say what failed, then a VERDICT
        print(f"policy v2 publication failed: {type(error).__name__}: {error}", file=sys.stderr)
        print("VERDICT cmd=provenance.publish_policy_v2 status=FAIL", file=sys.stderr)
        return 1
    print(
        f"VERDICT cmd=provenance.publish_policy_v2 status=OK policy={policy.id} "
        f"band={policy.band.kind}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
