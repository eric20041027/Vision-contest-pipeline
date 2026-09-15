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


def validate(
    source_data: Path,
    source_configs: Path,
    *,
    six_method=False,
    policy_from: Path | None = None,
) -> dict[str, object]:
    if six_method:
        if __package__:
            from .adaptive_benchmark import (
                METHODS,
                empirical_crossover,
                load_frozen_policy,
                postgres_preflight,
                run_method,
            )
        else:
            from adaptive_benchmark import (
                METHODS,
                empirical_crossover,
                load_frozen_policy,
                postgres_preflight,
                run_method,
            )
        if policy_from is None:
            raise ValueError("six-method validation requires a frozen policy")
        runtime = postgres_preflight()
        policy, evidence, policy_sha256 = load_frozen_policy(policy_from)
    with tempfile.TemporaryDirectory(prefix="vcp-real-provenance-") as directory:
        root = Path(directory)
        data = root / "data"
        configs = root / "configs"
        selected_runs = _copy_snapshot(source_data, source_configs, data, configs)
        index = ProvenanceIndex(provenance_index_path(data))
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
            rows = []
            for transition in range(len(TRANSITIONS)):
                workload = build_real_scenario(
                    root / f"real-{transition}", source_data, source_configs, transition
                )
                if __package__:
                    from .adaptive_benchmark import prepare_policy_workload
                else:
                    from adaptive_benchmark import prepare_policy_workload
                workload = prepare_policy_workload(workload, policy, evidence)
                for method in METHODS:
                    rows.append(
                        run_method(
                            workload,
                            method,
                            pg_runtime=runtime,
                            policy_id=policy.id if method == "postgres_adaptive" else None,
                            policy_sha256=(
                                policy_sha256 if method == "postgres_adaptive" else None
                            ),
                        ).to_dict()
                    )
            document["six_method_benchmark"] = rows
            document["policy_id"] = policy.id
            document["policy_sha256"] = policy_sha256
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
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("real validation output is write-once")
    result = validate(
        args.data_root.resolve(),
        args.configs_root.resolve(),
        six_method=args.six_method,
        policy_from=args.policy_from,
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
