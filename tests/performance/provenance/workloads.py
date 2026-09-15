"""Canonical-file workloads shared by explicit provenance performance runners.

Entity targets describe the graph *before* ingest. Ratios use distinct source
sample entities, with integer rounding reported separately from the requested ratio.
Generation, artifact creation and copying are never timed as maintenance.
"""

from __future__ import annotations

import gc
import json
import math
import random
import shutil
from collections.abc import Set
from dataclasses import asdict, dataclass
from pathlib import Path

from vcp.core.config import dump_yaml_model
from vcp.core.hashing import sha256_file, sha256_text
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.schema import DatasetCard, Sample, SourceInfo, View, sample_json_line
from vcp.data.split import SplitPlan, SubsetSpec, save_plan
from vcp.measure.schema import Reading, RunCard, RunSource
from vcp.provenance.diff import DatasetDiffSpec, create_dataset_diff
from vcp.provenance.graph import ProvenanceGraph, build_graph, dataset_version_id
from vcp.provenance.index import graph_hash
from vcp.provenance.schema import (
    ChangeDomain,
    ChangeType,
    SampleChange,
    SemanticEffect,
    make_change_id,
)

SEED = 20260913
SCALES = (1_000, 10_000, 100_000, 1_000_000)
RATIOS = (0, 0.001, 0.01, 0.05, 0.10, 0.25, 0.50, 0.90, 1.0)
TOPOLOGIES = ("chain", "branched")
CALIBRATION_SEEDS = (20260913, 20260914)
HELDOUT_SEEDS = (20261001, 20261002)


@dataclass(frozen=True)
class Scenario:
    entities: int
    change_ratio: float
    topology: str
    seed: int
    track: str = "scaled"
    variant: str = "v1"

    def __post_init__(self):
        minimum = 32 if self.track == "scaled" else 1
        if self.entities < minimum or not math.isfinite(self.change_ratio):
            raise ValueError("scenario has too few entities or a non-finite ratio")
        if not 0 <= self.change_ratio <= 1 or self.topology not in TOPOLOGIES:
            raise ValueError("invalid scenario ratio or topology")

    @property
    def scenario_hash(self) -> str:
        values = {**asdict(self), "change_ratio": float(self.change_ratio)}
        return sha256_text(json.dumps(values, sort_keys=True, separators=(",", ":")))

    @property
    def scenario_id(self) -> str:
        return f"{self.track}-{self.seed}-{self.entities}-{self.scenario_hash[:16]}"

    @property
    def repetitions(self) -> int:
        return 7 if self.entities <= 10_000 else 3


def scenario_matrix(
    *, seeds=CALIBRATION_SEEDS, entities=SCALES, ratios=RATIOS, topologies=TOPOLOGIES
) -> list[Scenario]:
    result = []
    for seed in seeds:
        batch = [
            Scenario(size, ratio, topology, seed)
            for size in entities
            for ratio in ratios
            for topology in topologies
        ]
        random.Random(seed).shuffle(batch)
        result.extend(batch)
    if len({s.scenario_id for s in result}) != len(result):
        raise ValueError("duplicate scenario in matrix")
    return result


@dataclass(frozen=True)
class Workload:
    scenario: Scenario
    data: Path
    configs: Path
    pending: Path
    artifact_id: str
    source_id: str
    target_id: str
    sample_entities: int
    changed_samples: int
    historical_changes: int
    expected: ProvenanceGraph
    workload_hash: str

    def clone(self, root: Path) -> tuple[Path, Path]:
        data, configs = root / "data", root / "configs"
        shutil.copytree(self.data, data)
        shutil.copytree(self.configs, configs)
        return data, configs

    def publish(self, data: Path) -> None:
        shutil.copytree(self.pending, data / "artifacts" / "dataset_diff" / self.artifact_id)


def _change(
    index: int,
    before: str,
    after: str,
    *,
    prefix: str,
    effect: SemanticEffect = SemanticEffect.DISPLAY_ONLY,
) -> SampleChange:
    """Original production benchmark event; kept byte-for-byte compatible."""
    sample_id = f"{prefix}-{index:08d}"
    row_before, row_after = sha256_text(f"before:{sample_id}"), sha256_text(f"after:{sample_id}")
    training = effect == SemanticEffect.TRAINING_AFFECTING
    return SampleChange(
        change_id=make_change_id(
            before, after, sample_id, ChangeType.MODIFIED, row_before, row_after
        ),
        from_dataset="synthetic-source",
        from_samples_hash=before,
        to_dataset="synthetic-target",
        to_samples_hash=after,
        sample_id=sample_id,
        change_type=ChangeType.MODIFIED,
        changed_domains=[ChangeDomain.LABEL_SOURCE if training else ChangeDomain.META],
        changed_fields=["label_source" if training else "meta.display"],
        semantic_effects=[effect],
        before_row_hash=row_before,
        after_row_hash=row_after,
    )


def _diff(data: Path, configs: Path, before: str, after: str, ident: str):
    return create_dataset_diff(
        DatasetDiffSpec(
            from_dataset=before,
            to_dataset=after,
            artifact_id=ident,
            data_root=data,
            configs_root=configs,
        )
    )


def finish_workload(
    root: Path,
    scenario: Scenario,
    before: str,
    after: str,
    *,
    sample_entities: int,
) -> Workload:
    """Freeze a baseline and stage its delta outside the canonical scan root."""
    data, configs = root / "data", root / "configs"
    baseline = build_graph(data, configs)
    if len(baseline.entities) != scenario.entities:
        raise ValueError("canonical fixture does not match the target entity count")
    baseline_hash = graph_hash(baseline)
    historical_changes = len(baseline.changes)
    del baseline
    gc.collect()
    delta = _diff(data, configs, before, after, "adaptive-delta")
    expected = build_graph(data, configs)
    if expected.gaps or any(e.broken_reason for e in expected.entities.values()):
        raise ValueError("canonical fixture contains broken evidence")
    pending = root / "pending"
    shutil.move(str(delta.artifact_dir), pending)
    source_id = dataset_version_id(before, delta.summary.from_samples_hash)
    target_id = dataset_version_id(after, delta.summary.to_samples_hash)
    workload_hash = sha256_text(scenario.scenario_hash + baseline_hash + graph_hash(expected))
    return Workload(
        scenario,
        data,
        configs,
        pending,
        delta.artifact_id,
        source_id,
        target_id,
        sample_entities,
        delta.summary.total_changes,
        historical_changes,
        expected,
        workload_hash,
    )


def _write_synthetic_dataset(
    paths: DatasetPaths,
    card: DatasetCard,
    *,
    count: int,
    revision: int,
    seed: int,
    changed: Set[int],
) -> DatasetCard:
    """Write the already-sorted synthetic samples without retaining them in memory."""
    if paths.name != card.name:
        raise ValueError(f"paths name {paths.name!r} != card name {card.name!r}")
    paths.samples_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with paths.samples_jsonl.open("w", encoding="utf-8", newline="\n") as stream:
        for index in range(count):
            sample = Sample(
                sample_id=f"delta-{index:08d}",
                views=[View(path=f"opaque/{index}.png")],
                label_source="none",
                group=f"changed-{index}" if index in changed else f"g-{index % 8}",
                meta={"display": revision, "seed": seed},
            )
            stream.write(sample_json_line(sample))
            stream.write("\n")
    saved = card.model_copy(
        update={"sample_count": count, "samples_hash": sha256_file(paths.samples_jsonl)}
    )
    dump_yaml_model(saved, paths.card_yaml)
    return saved


def build_scenario(root: Path, scenario: Scenario) -> Workload:
    """Build exact-size graphs using real DatasetCards, plans, runs, readings and diffs.

    Two historical diffs materialize three versions of every sample. Chain uses
    a->b->source; branched uses a->source and b->source. A small operational tail
    reuses the existing run->reading benchmark topology and fills the exact count.
    """
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    data, configs = root / "data", root / "configs"
    count = (scenario.entities - 20) // 3
    rng = random.Random(scenario.seed)
    changed = set(rng.sample(range(count), round(count * scenario.change_ratio)))
    source_card = None
    for name, revision in (
        ("synthetic-a", 0),
        ("synthetic-b", 1),
        ("synthetic-source", 2),
        ("synthetic-target", 2),
    ):
        card = DatasetCard(
            name=name,
            task="cls",
            image_root=f"raw/{name}",
            source=SourceInfo(
                importer="synthetic",
                importer_version="1",
                raw_path="opaque",
                raw_hash="0" * 64,
                license="synthetic",
                url="",
                downloaded_at=stamp(),
            ),
            created_at=stamp(),
            sample_count=count,
            samples_hash="0" * 64,
        )
        card = _write_synthetic_dataset(
            DatasetPaths.resolve(name, data_root=data, configs_root=configs),
            card,
            count=count,
            revision=revision,
            seed=scenario.seed,
            changed=changed if name == "synthetic-target" else frozenset(),
        )
        if name == "synthetic-source":
            source_card = card
    transitions = (("synthetic-a", "synthetic-b"), ("synthetic-b", "synthetic-source"))
    if scenario.topology == "branched":
        transitions = (("synthetic-a", "synthetic-source"), ("synthetic-b", "synthetic-source"))
    for index, (before, after) in enumerate(transitions):
        _diff(data, configs, before, after, f"history-{index}")
    if source_card is None:  # pragma: no cover - fixed fixture definition above
        raise RuntimeError("synthetic source dataset was not built")
    paths = DatasetPaths.resolve(source_card.name, data_root=data, configs_root=configs)
    plan = SplitPlan(
        plan_id="fixed",
        dataset=source_card.name,
        dataset_hash=source_card.samples_hash,
        strategy="fixed",
        params={},
        subsets=[SubsetSpec(name="train", role="train", ratio=1)],
        assignment={f"delta-{index:08d}": "train" for index in range(count)},
        created_at=stamp(),
    )
    save_plan(plan, paths)
    run = RunCard(
        run_id="r-000000",
        dataset=source_card.name,
        samples_hash=source_card.samples_hash,
        plan_id=plan.plan_id,
        trained_on=["train"],
        source=RunSource(),
        created_at=stamp(),
    )
    dump_yaml_model(run, data / "runs" / run.run_id / "run.yaml")
    # Four datasets, two diff artifacts, three sample versions, one split, one run.
    reading_count = scenario.entities - (3 * count + 8)
    ledger = data / "measure" / source_card.name / "readings.jsonl"
    ledger.parent.mkdir(parents=True)
    with ledger.open("w", encoding="utf-8", newline="\n") as stream:
        for index in range(reading_count):
            reading = Reading(
                reading_id=f"r-{index:06d}",
                ts=stamp(),
                run_id=run.run_id,
                dataset=run.dataset,
                samples_hash=run.samples_hash,
                plan_id=run.plan_id,
                subset="train",
                metric="accuracy",
                metric_version="1",
                params={},
                value=0.5,
                per_class=None,
                n_samples=count,
                prediction_sha="0" * 64,
            )
            stream.write(reading.model_dump_json() + "\n")
    return finish_workload(
        root, scenario, "synthetic-source", "synthetic-target", sample_entities=count
    )
