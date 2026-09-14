"""Evaluate an exact immutable calibration policy on disjoint live PostgreSQL seeds.

This module deliberately has no dependency on calibration or numerical fitting.
The public row validator is also used by the calibration entrypoint.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.atomic import write_once_text
from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.hashing import sha256_text
from vcp.provenance.graph import build_graph
from vcp.provenance.index import graph_hash
from vcp.provenance.strategy import (
    CALIBRATION_SEEDS,
    HELDOUT_SEEDS,
    POLICY_VERSION,
    AdaptivePolicy,
    CalibrationEvidence,
    MaintenanceFeatures,
    _object_without_duplicate_keys,
    calibration_text,
    load_policy_artifact,
    select_strategy,
    write_policy_artifact,
)

if __package__:
    from . import adaptive_benchmark as benchmark
    from .workloads import Scenario, build_scenario, scenario_matrix
else:
    import adaptive_benchmark as benchmark
    from workloads import Scenario, build_scenario, scenario_matrix

FIXED_METHODS = ("postgres_full", "postgres_incremental")
EVALUATION_METHODS = (*FIXED_METHODS, "postgres_adaptive")
_STORAGE = "sum pg_total_relation_size of vcp_provenance tables (includes indexes/TOAST)"


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValidationFailed("invalid_arguments") from None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class _Environment(_Strict):
    system: Literal["Windows", "Linux", "Darwin"]
    machine: Literal["AMD64", "x86_64", "arm64", "aarch64", "ARM64"]
    python_version: str = Field(pattern=r"^3\.12\.\d+$")
    python_implementation: Literal["CPython"]
    cpu_count: int = Field(gt=0)
    sqlite_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    vcp_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|unavailable)$")
    postgresql_major: Literal[17]
    postgresql_version: int = Field(ge=170000, lt=180000)
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    backend_schema_version: Literal[1]


class _Sample(_Strict):
    maintenance_ms: float = Field(gt=0)
    status_ms: float = Field(ge=0)
    impact_ms: float = Field(ge=0)
    explain_ms: float = Field(ge=0)
    graph_parity: Literal[True]
    graph_hash_parity: Literal[True]
    status_parity: Literal[True]
    head_parity: Literal[True]
    graph_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_bytes: int = Field(ge=0)
    storage_measurement: Literal[_STORAGE]
    requested_strategy: Literal["full", "incremental", "auto"]
    selected_strategy: Literal["FULL", "INCREMENTAL", "NO_OP"]
    strategy_reason: Literal[
        "requested_full",
        "requested_incremental",
        "verified_zero_semantic_changes",
        "calibrated_incremental_lower_confident_cost",
        "calibrated_full_lower_or_uncertain_cost",
        "fallback_policy_absent_full",
    ]
    policy_version: Literal["safe-fallback-v1", "postgres-adaptive-v1"]
    estimated_incremental_ms: float | None = Field(ge=0)
    estimated_full_ms: float | None = Field(ge=0)
    dirty_entities: int = Field(ge=0)


class BenchmarkRow(_Sample):
    """Complete allowlist for Task 10 PostgreSQL rows, including nested samples."""

    # Aggregate rows have percentiles in place of the four individual timings.
    maintenance_ms: float = Field(default=1.0, exclude=True)
    status_ms: float = Field(default=0.0, exclude=True)
    impact_ms: float = Field(default=0.0, exclude=True)
    explain_ms: float = Field(default=0.0, exclude=True)
    schema_version: Literal[1]
    method: Literal["postgres_full", "postgres_incremental", "postgres_adaptive"]
    scenario_id: str = Field(pattern=r"^scaled-\d+-\d+-[0-9a-f]{16}$")
    scenario_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    workload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    track: Literal["scaled"]
    seed: int
    topology: Literal["chain", "branched"]
    entities: int = Field(ge=32)
    change_ratio: float = Field(ge=0, le=1)
    realized_change_ratio: float = Field(ge=0, le=1)
    sample_entities: int = Field(gt=0)
    changed_samples: int = Field(ge=0)
    total_entities: int = Field(gt=0)
    total_edges: int = Field(ge=0)
    total_changes: int = Field(ge=0)
    historical_changes: int = Field(ge=0)
    head_count: int = Field(ge=0)
    environment: _Environment
    status: Literal["ok"]
    failure: None
    samples: list[_Sample] = Field(min_length=1)
    repetitions: int = Field(gt=0)
    parity_rate: Literal[1.0]
    explain_analyze: list[dict] = Field(min_length=1)
    instrumentation: Literal[
        "separate fresh state; first DML row per SQL shape; rollback; excluded from timings"
    ]
    query_measurement: Literal["public status API; impact/explain include graph load as in CLI"]
    warmup: Literal["baseline rebuilt before each sample; no timed warmup; OS caches may be warm"]
    maintenance_p50_ms: float = Field(gt=0)
    maintenance_p95_ms: float = Field(gt=0)
    status_p50_ms: float = Field(ge=0)
    status_p95_ms: float = Field(ge=0)
    impact_p50_ms: float = Field(ge=0)
    impact_p95_ms: float = Field(ge=0)
    explain_p50_ms: float = Field(ge=0)
    explain_p95_ms: float = Field(ge=0)
    dirty_ratio: float = Field(ge=0, le=1)
    throughput_samples_per_second: float = Field(ge=0)

    def features(self):
        return MaintenanceFeatures.model_validate(
            {key: getattr(self, key) for key in MaintenanceFeatures.model_fields}
        )


def validate_rows(rows, *, seeds, methods):
    """Reject incomplete/mixed/failed inputs before any fit, publication or aggregate."""
    try:
        validated = [BenchmarkRow.model_validate(row) for row in rows]
        if not validated or {r.seed for r in validated} != set(seeds):
            raise ValueError
        groups = {}
        environments = set()
        ids = set()
        workloads = set()
        for row in validated:
            scenario = Scenario(row.entities, row.change_ratio, row.topology, row.seed)
            if (row.scenario_id, row.scenario_hash) != (
                scenario.scenario_id,
                scenario.scenario_hash,
            ):
                raise ValueError
            if row.method not in methods:
                raise ValueError
            group = groups.setdefault(row.scenario_hash, {})
            if row.method in group:
                raise ValueError
            group[row.method] = row
            row.features()
            if (
                row.repetitions != len(row.samples)
                or row.dirty_ratio != row.dirty_entities / row.total_entities
                or row.realized_change_ratio != row.changed_samples / row.sample_entities
                or row.total_changes != row.historical_changes + row.changed_samples
                or benchmark.sanitize_explain(row.explain_analyze) != row.explain_analyze
                or not any(plan for plan in row.explain_analyze)
            ):
                raise ValueError
            for operation in benchmark.OPERATIONS:
                values = sorted(getattr(s, operation + "_ms") for s in row.samples)
                if (
                    getattr(row, operation + "_p50_ms") != statistics.median(values)
                    or getattr(row, operation + "_p95_ms")
                    != values[round(0.95 * (len(values) - 1))]
                ):
                    raise ValueError
            for sample in row.samples:
                for key in (*benchmark.DECISION_FIELDS, "graph_hash", "dirty_entities"):
                    if getattr(sample, key) != getattr(row, key):
                        raise ValueError
            expected_requested = {
                "postgres_full": "full",
                "postgres_incremental": "incremental",
                "postgres_adaptive": "auto",
            }[row.method]
            if row.requested_strategy != expected_requested:
                raise ValueError
            if row.method in FIXED_METHODS:
                decision = select_strategy(expected_requested, row.features(), None)
                if (row.selected_strategy, row.strategy_reason) != (
                    decision.selected_strategy.value,
                    decision.reason,
                ):
                    raise ValueError
            environments.add(
                (
                    row.environment.environment_fingerprint,
                    row.environment.postgresql_major,
                    row.environment.backend_schema_version,
                    row.schema_version,
                )
            )
        for group in groups.values():
            if set(group) != set(methods):
                raise ValueError
            first = next(iter(group.values()))
            if first.scenario_id in ids or first.workload_hash in workloads:
                raise ValueError
            ids.add(first.scenario_id)
            workloads.add(first.workload_hash)
            for row in group.values():
                if (
                    row.features() != first.features()
                    or row.workload_hash != first.workload_hash
                    or row.graph_hash != first.graph_hash
                    or row.repetitions != first.repetitions
                ):
                    raise ValueError
        if len(environments) != 1:
            raise ValueError
        return {key: groups[key] for key in sorted(groups)}
    except (TypeError, ValueError, KeyError, AttributeError):
        raise ValidationFailed("invalid_benchmark_rows") from None


def paired_observations(rows):
    groups = validate_rows(rows, seeds=CALIBRATION_SEEDS, methods=FIXED_METHODS)
    result = []
    for group in groups.values():
        full, incremental = (group[name] for name in FIXED_METHODS)
        result.append(
            {
                **full.features().model_dump(),
                "scenario_id": full.scenario_id,
                "scenario_hash": full.scenario_hash,
                "workload_hash": full.workload_hash,
                "seed": full.seed,
                "incremental_p50_ms": incremental.maintenance_p50_ms,
                "full_p50_ms": full.maintenance_p50_ms,
                "environment_fingerprint": full.environment.environment_fingerprint,
                "backend_schema_version": full.environment.backend_schema_version,
                "postgresql_major": full.environment.postgresql_major,
                "benchmark_schema_version": full.schema_version,
            }
        )
    return result


def load_calibration(path: Path):
    """Read embedded policy, then verify it against the immutable on-disk artifact."""
    try:
        path = Path(path)
        document = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_object_without_duplicate_keys
        )
        if set(document) != {"kind", "policy", "calibration"}:
            raise ValueError
        if document["kind"] != "postgres-provenance-calibration-v1":
            raise ValueError
        policy = AdaptivePolicy.model_validate(document["policy"])
        evidence = CalibrationEvidence.model_validate(document["calibration"])
        if not evidence.observations:
            raise ValueError
        first = evidence.observations[0]
        verified = load_policy_artifact(
            path.parent / (path.stem + "-artifacts"),
            policy.id,
            environment_fingerprint=first.environment_fingerprint,
            postgresql_major=first.postgresql_major,
            backend_schema_version=first.backend_schema_version,
            benchmark_schema_version=first.benchmark_schema_version,
        )
        if (
            verified != policy
            or document["policy"] != verified.model_dump(mode="json")
            or sha256_text(calibration_text(evidence)) != policy.calibration_sha256
            or len(evidence.observations) != policy.training_row_count
        ):
            raise ValueError
        return verified, evidence
    except (OSError, TypeError, ValueError, VcpError):
        raise ValidationFailed("invalid_calibration_artifact") from None


def prepare_workload(workload, policy, evidence):
    """Install policy before baseline; rebuild candidate oracle outside measured runs."""
    evidence_path = workload.data / "policy-inputs" / "calibration.json"
    write_once_text(evidence_path, calibration_text(evidence))
    write_policy_artifact(workload.data, policy, evidence_path)
    baseline = build_graph(workload.data, workload.configs)
    with tempfile.TemporaryDirectory(prefix="vcp-policy-oracle-") as temporary:
        data, configs = workload.clone(Path(temporary))
        workload.publish(data)
        expected = build_graph(data, configs)
    if (
        baseline.gaps
        or expected.gaps
        or any(e.broken_reason for graph in (baseline, expected) for e in graph.entities.values())
    ):
        raise ValidationFailed("invalid_policy_workload_oracle")
    return replace(
        workload,
        expected=expected,
        workload_hash=sha256_text(
            workload.scenario.scenario_hash + graph_hash(baseline) + graph_hash(expected)
        ),
    )


def collect_rows(root, scenarios, *, methods, pg_runtime, policy=None, evidence=None):
    rows = []
    for scenario in scenarios:
        with tempfile.TemporaryDirectory(prefix="scenario-", dir=root) as temporary:
            workload = build_scenario(Path(temporary) / "fixture", scenario)
            if policy is not None:
                workload = prepare_workload(workload, policy, evidence)
            for method in methods:
                rows.append(
                    benchmark.run_method(
                        workload,
                        method,
                        pg_runtime=pg_runtime,
                        policy_id=policy.id if policy and method == "postgres_adaptive" else None,
                    ).to_dict()
                )
    return rows


class EvaluationResult(_Strict):
    policy_id: str
    policy_sha256: str
    parity_rate: float
    overlap_count: int
    scenario_count: int
    median_ratio: float
    p95_ratio: float
    median_gate_pass: bool
    p95_gate_pass: bool
    performance_pass: bool
    every_scenario_pass: bool
    aggregate_maintenance_ms: dict[str, dict[str, float]]
    scenarios: list[dict]


def evaluate_policy(policy_from, heldout_rows) -> EvaluationResult:
    policy, evidence = load_calibration(policy_from)
    # Check both identities before seed validation, so leakage has a precise failure.
    ids, hashes = set(evidence.scenario_ids), set(evidence.scenario_hashes)
    workloads = {row.workload_hash for row in evidence.observations}
    if any(
        isinstance(row, dict)
        and (
            row.get("scenario_id") in ids
            or row.get("scenario_hash") in hashes
            or row.get("workload_hash") in workloads
        )
        for row in heldout_rows
    ):
        raise ValidationFailed("workload_leakage")
    groups = validate_rows(heldout_rows, seeds=HELDOUT_SEEDS, methods=EVALUATION_METHODS)
    ratios50, ratios95, reports = [], [], []
    pooled = {method: [] for method in EVALUATION_METHODS}
    for scenario_hash, group in groups.items():
        adaptive = group["postgres_adaptive"]
        env = adaptive.environment
        if (
            env.environment_fingerprint != policy.environment_fingerprint
            or env.postgresql_major != policy.postgresql_major
            or env.backend_schema_version != policy.backend_schema_version
            or adaptive.schema_version != policy.benchmark_schema_version
        ):
            raise ValidationFailed("incompatible_policy")
        decision = select_strategy("auto", adaptive.features(), policy)
        if (
            adaptive.selected_strategy != decision.selected_strategy.value
            or adaptive.strategy_reason != decision.reason
            or adaptive.policy_version != POLICY_VERSION
            or adaptive.estimated_incremental_ms != decision.estimated_incremental_ms
            or adaptive.estimated_full_ms != decision.estimated_full_ms
        ):
            raise ValidationFailed("policy_decision_mismatch")
        fixed = [group[method] for method in FIXED_METHODS]
        ratio50 = adaptive.maintenance_p50_ms / min(r.maintenance_p50_ms for r in fixed)
        ratio95 = adaptive.maintenance_p95_ms / min(r.maintenance_p95_ms for r in fixed)
        ratios50.append(ratio50)
        ratios95.append(ratio95)
        for method, row in group.items():
            pooled[method].extend(sample.maintenance_ms for sample in row.samples)
        reports.append(
            {
                "scenario_hash": scenario_hash,
                "median_ratio": ratio50,
                "p95_ratio": ratio95,
                "selected_strategy": adaptive.selected_strategy,
            }
        )
    # Normative gates use each method's pooled held-out latency distribution.
    # Retain per-scenario regressions as a separate conservative diagnostic.
    aggregates = {
        method: {
            "p50": statistics.median(values),
            "p95": sorted(values)[round(0.95 * (len(values) - 1))],
        }
        for method, values in pooled.items()
    }
    adaptive_costs = aggregates["postgres_adaptive"]
    median_ratio = adaptive_costs["p50"] / min(aggregates[m]["p50"] for m in FIXED_METHODS)
    p95_ratio = adaptive_costs["p95"] / min(aggregates[m]["p95"] for m in FIXED_METHODS)
    if not all(math.isfinite(value) for value in (median_ratio, p95_ratio)):
        raise ValidationFailed("invalid_evaluation_aggregate")
    return EvaluationResult(
        policy_id=policy.id,
        policy_sha256=sha256_text(json.dumps(policy.model_dump(mode="json"), sort_keys=True)),
        parity_rate=1.0,
        overlap_count=0,
        scenario_count=len(groups),
        median_ratio=median_ratio,
        p95_ratio=p95_ratio,
        median_gate_pass=median_ratio <= 1.05,
        p95_gate_pass=p95_ratio <= 1.10,
        performance_pass=median_ratio <= 1.05 and p95_ratio <= 1.10,
        every_scenario_pass=all(ratio <= 1.05 for ratio in ratios50)
        and all(ratio <= 1.10 for ratio in ratios95),
        aggregate_maintenance_ms=aggregates,
        scenarios=reports,
    )


def main(argv=None) -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--policy-from", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        policy, evidence = load_calibration(args.policy_from)
        pg_runtime = benchmark.postgres_preflight()
        with tempfile.TemporaryDirectory(prefix="vcp-heldout-") as temporary:
            rows = collect_rows(
                Path(temporary),
                scenario_matrix(seeds=HELDOUT_SEEDS),
                methods=EVALUATION_METHODS,
                pg_runtime=pg_runtime,
                policy=policy,
                evidence=evidence,
            )
        result = evaluate_policy(args.policy_from, rows)
        write_once_text(
            args.output,
            json.dumps(
                {
                    "kind": "postgres-provenance-heldout-v1",
                    "evaluation": result.model_dump(),
                    "results": rows,
                },
                indent=2,
            )
            + "\n",
        )
        status = "OK" if result.performance_pass else "FAIL"
        print(f"VERDICT cmd=provenance.evaluate status={status}", file=sys.stderr)
        return 0 if result.performance_pass else 1
    except Exception:
        print(
            "Held-out evaluation failed (live service or verified evidence required)",
            file=sys.stderr,
        )
        print("VERDICT cmd=provenance.evaluate status=FAIL", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
