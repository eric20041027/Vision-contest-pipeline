"""Read-only real-data validation using a metadata-only temporary copy."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from vcp.core.atomic import write_once_text
from vcp.core.errors import ValidationFailed
from vcp.core.paths import provenance_index_path
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.measure.runs import load_run
from vcp.provenance.diff import DatasetDiffSpec, create_dataset_diff
from vcp.provenance.graph import build_graph
from vcp.provenance.index import ProvenanceIndex, dataset_heads
from vcp.provenance.views import compute_statuses

if __package__:
    from .workloads import Scenario, Workload, finish_workload
else:
    from workloads import Scenario, Workload, finish_workload

DATASETS = (
    "rsna-knee-sixslot-r3-20260908",
    "rsna-knee-sixslot-r4-20260908",
    "rsna-knee-sixslot-labelagree-r5-20260909",
    "rsna-knee-sixslot-labelagree-r6-20260909",
)
TRANSITIONS = tuple(zip(DATASETS[:-1], DATASETS[1:], strict=True))


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _copy_snapshot(
    source_data: Path, source_configs: Path, target_data: Path, target_configs: Path
) -> list[str]:
    for dataset in DATASETS:
        source = source_configs / "datasets" / dataset
        if not source.is_dir():
            raise FileNotFoundError(source)
        shutil.copytree(source, target_configs / "datasets" / dataset)
        _copy_file(
            source_data / "datasets" / dataset / "samples.jsonl",
            target_data / "datasets" / dataset / "samples.jsonl",
        )
    selected_runs: list[str] = []
    runs_root = source_data / "runs"
    if runs_root.is_dir():
        for directory in sorted(path for path in runs_root.iterdir() if path.is_dir()):
            try:
                card = load_run(source_data, directory.name)
            except (ValidationFailed, OSError):
                continue
            if card.dataset not in DATASETS:
                continue
            selected_runs.append(card.run_id)
            shutil.copytree(directory, target_data / "runs" / directory.name)
    for dataset in DATASETS:
        source = source_data / "measure" / dataset
        if source.is_dir():
            shutil.copytree(source, target_data / "measure" / dataset)
    return selected_runs


def build_real_scenario(
    root: Path,
    source_data: Path,
    source_configs: Path,
    transition: int,
) -> Workload:
    """Each real transition starts from a fresh copy and its own canonical prefix."""
    before, after = TRANSITIONS[transition]
    root.mkdir(parents=True, exist_ok=False)
    data, configs = root / "data", root / "configs"
    _copy_snapshot(source_data, source_configs, data, configs)
    for index, (old, new) in enumerate(TRANSITIONS[:transition]):
        create_dataset_diff(
            DatasetDiffSpec(
                from_dataset=old,
                to_dataset=new,
                artifact_id=f"real-history-{index}",
                data_root=data,
                configs_root=configs,
            )
        )
    from vcp.provenance.diff import compare_dataset_versions

    comparison = compare_dataset_versions(
        DatasetDiffSpec(
            from_dataset=before,
            to_dataset=after,
            artifact_id="adaptive-delta",
            data_root=data,
            configs_root=configs,
        )
    )
    samples = Dataset.load_card(before, data_root=data, configs_root=configs).sample_count
    if samples == 0:
        raise ValueError("real benchmark requires a nonempty source dataset")
    scenario = Scenario(
        entities=len(build_graph(data, configs).entities),
        change_ratio=comparison.summary.total_changes / samples,
        topology="chain",
        seed=20260913,
        track="real",
        variant=f"transition-{transition}",
    )
    return finish_workload(root, scenario, before, after, sample_entities=samples)


def _six_method_rows(
    root: Path,
    source_data: Path,
    source_configs: Path,
    *,
    runtime,
    policy,
    evidence,
    policy_sha256,
    comparison=None,
    comparison_sha256=None,
) -> list[dict]:
    """Every transition under the six methods, plus the frozen v1 policy when given (spec
    2026-10-09 §6.4: a sanity check of the case that motivated v2, never held-out evidence)."""
    if __package__:
        from . import adaptive_benchmark as benchmark
    else:
        import adaptive_benchmark as benchmark
    methods = benchmark.METHODS
    comparisons = ()
    if comparison is not None:
        methods = (*methods, benchmark.COMPARISON_METHOD)
        comparisons = (comparison,)
    rows = []
    for transition in range(len(TRANSITIONS)):
        workload = build_real_scenario(
            root / f"real-{transition}", source_data, source_configs, transition
        )
        workload = benchmark.prepare_policy_workload(workload, policy, evidence, *comparisons)
        for method in methods:
            method_policy, method_sha256 = benchmark.policy_for(
                method, policy, policy_sha256, comparison, comparison_sha256
            )
            rows.append(
                benchmark.run_method(
                    workload,
                    method,
                    pg_runtime=runtime,
                    policy_id=method_policy,
                    policy_sha256=method_sha256,
                ).to_dict()
            )
    return rows


def validate(
    source_data: Path,
    source_configs: Path,
    *,
    six_method=False,
    policy_from: Path | None = None,
    comparison_from: Path | None = None,
) -> dict[str, object]:
    if comparison_from is not None and not six_method:
        raise ValueError("a comparison policy needs the six-method validation")
    if six_method:
        if __package__:
            from .adaptive_benchmark import (
                empirical_crossover,
                load_frozen_policy,
                postgres_preflight,
                require_policy_environment,
                require_policy_pair,
            )
        else:
            from adaptive_benchmark import (
                empirical_crossover,
                load_frozen_policy,
                postgres_preflight,
                require_policy_environment,
                require_policy_pair,
            )
        if policy_from is None:
            raise ValueError("six-method validation requires a frozen policy")
        runtime = postgres_preflight()
        policy, evidence, policy_sha256 = load_frozen_policy(policy_from)
        comparison = comparison_sha256 = None
        if comparison_from is not None:
            comparison, comparison_evidence, comparison_sha256 = load_frozen_policy(comparison_from)
            require_policy_pair(policy, evidence, comparison, comparison_evidence)
        require_policy_environment(
            runtime, *((policy,) if comparison is None else (policy, comparison))
        )
    with tempfile.TemporaryDirectory(prefix="vcp-real-provenance-") as directory:
        root = Path(directory)
        data = root / "data"
        configs = root / "configs"
        selected_runs = _copy_snapshot(source_data, source_configs, data, configs)
        index = ProvenanceIndex(provenance_index_path(data, configs))
        index.rebuild(data, configs)
        summaries = []
        for position, (before, after) in enumerate(TRANSITIONS, start=1):
            artifact_id = f"real-readonly-{position}"
            result = create_dataset_diff(
                DatasetDiffSpec(
                    from_dataset=before,
                    to_dataset=after,
                    artifact_id=artifact_id,
                    data_root=data,
                    configs_root=configs,
                )
            )
            summaries.append(result.summary.model_dump(mode="json"))
            index.ingest_diff(artifact_id, data, configs)
        canonical = build_graph(data, configs)
        indexed = index.load_graph()
        graph_parity = canonical.normalized() == indexed.normalized()
        status_differences: dict[str, list[dict[str, object]]] = {}
        for head in dataset_heads(canonical):
            expected = compute_statuses(canonical, head)
            actual = index.statuses(head)
            differences = []
            for ident in sorted(set(expected) | set(actual)):
                if expected.get(ident) != actual.get(ident):
                    differences.append(
                        {
                            "entity_id": ident,
                            "full": (
                                expected[ident].model_dump(mode="json")
                                if ident in expected
                                else None
                            ),
                            "incremental": (
                                actual[ident].model_dump(mode="json") if ident in actual else None
                            ),
                        }
                    )
            if differences:
                status_differences[head] = differences
        status_parity = not status_differences
        verification = index.verify(data, configs)
        document = {
            "created_at": stamp(),
            "mode": "metadata-only temporary copy; source roots opened read-only",
            "source_data_root": str(source_data),
            "source_configs_root": str(source_configs),
            "diff_summaries": summaries,
            "selected_runs": selected_runs,
            "selected_run_count": len(selected_runs),
            "graph": {
                "entities": len(canonical.entities),
                "edges": len(canonical.edges),
                "changes": len(canonical.changes),
                "gaps": canonical.gaps,
            },
            "graph_parity": graph_parity,
            "status_parity": status_parity,
            "status_differences": status_differences,
            "verify_index": verification.__dict__,
        }
        if six_method:
            rows = _six_method_rows(
                root,
                source_data,
                source_configs,
                runtime=runtime,
                policy=policy,
                evidence=evidence,
                policy_sha256=policy_sha256,
                comparison=comparison,
                comparison_sha256=comparison_sha256,
            )
            document["six_method_benchmark"] = rows
            document["policy_id"] = policy.id
            document["policy_sha256"] = policy_sha256
            if comparison is not None:
                document["comparison_policy_id"] = comparison.id
                document["comparison_policy_sha256"] = comparison_sha256
            document["empirical_crossover"] = empirical_crossover(evidence)
            document["postgresql_environment"] = next(
                (
                    row["environment"]
                    for row in rows
                    if row.get("method", "").startswith("postgres_") and row["status"] == "ok"
                ),
                None,
            )
        return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--configs-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--six-method", action="store_true")
    parser.add_argument("--policy-from", type=Path)
    parser.add_argument("--comparison-policy-from", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("real validation output is write-once")
    result = validate(
        args.data_root.resolve(),
        args.configs_root.resolve(),
        six_method=args.six_method,
        policy_from=args.policy_from,
        comparison_from=args.comparison_policy_from,
    )
    if __package__:
        from .adaptive_benchmark import validate_publication_explain
    else:
        from adaptive_benchmark import validate_publication_explain
    validate_publication_explain(result)
    write_once_text(
        args.output,
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
    )
    return int(any(row["status"] != "ok" for row in result.get("six_method_benchmark", [])))


if __name__ == "__main__":
    raise SystemExit(main())
