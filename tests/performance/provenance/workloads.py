"""Canonical-file workloads shared by explicit provenance performance runners.

Entity targets describe the graph *before* ingest. Ratios use distinct source
sample entities, with integer rounding reported separately from the requested ratio.
Generation, artifact creation and copying are never timed as maintenance.
"""

from __future__ import annotations

import gc
import hashlib
import json
import math
import random
import shutil
import tempfile
from collections import Counter
from collections.abc import Set
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import ValidationError

from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_text
from vcp.core.paths import DatasetPaths, validate_name
from vcp.core.time import stamp
from vcp.data.schema import DatasetCard, Sample, SourceInfo, View, dump_sample, sample_json_line
from vcp.data.split import SplitPlan, SubsetSpec, save_plan
from vcp.measure.schema import Reading, RunCard, RunSource
from vcp.provenance.diff import (
    CHANGES_FILE,
    KIND,
    SUMMARY_FILE,
    DatasetDiffSpec,
    create_dataset_diff,
)
from vcp.provenance.graph import ProvenanceGraph, build_graph, dataset_version_id
from vcp.provenance.index import graph_hash
from vcp.provenance.policy import classify_rows
from vcp.provenance.schema import (
    ChangeDomain,
    ChangeType,
    DatasetDiffSummary,
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
            sample = _synthetic_sample(index, revision, seed, changed)
            stream.write(sample_json_line(sample))
            stream.write("\n")
    saved = card.model_copy(
        update={"sample_count": count, "samples_hash": sha256_file(paths.samples_jsonl)}
    )
    dump_yaml_model(saved, paths.card_yaml)
    return saved


def _synthetic_sample(
    index: int, revision: int, seed: int, changed: Set[int] = frozenset()
) -> Sample:
    return Sample(
        sample_id=f"delta-{index:08d}",
        views=[View(path=f"opaque/{index}.png")],
        label_source="none",
        group=f"changed-{index}" if index in changed else f"g-{index % 8}",
        meta={"display": revision, "seed": seed},
    )


def _write_synthetic_history_diff(
    data: Path,
    configs: Path,
    before: str,
    after: str,
    ident: str,
    *,
    count: int,
) -> Path:
    """Publish a known synthetic history transition with bounded benchmark-fixture memory."""
    validate_name(ident)
    before_paths = DatasetPaths.resolve(before, data_root=data, configs_root=configs)
    after_paths = DatasetPaths.resolve(after, data_root=data, configs_root=configs)
    before_card = load_yaml_model(before_paths.card_yaml, DatasetCard)
    after_card = load_yaml_model(after_paths.card_yaml, DatasetCard)
    if before_card.name != before or after_card.name != after:
        raise ValueError("synthetic history card name does not match its path")
    if before_card.sample_count != count or after_card.sample_count != count:
        raise ValueError("synthetic history card count does not match the scenario")
    change_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    effect_counts: Counter[str] = Counter()
    before_digest = hashlib.sha256()
    after_digest = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix=f"vcp-{ident}-", dir=data) as temp:
        spool = Path(temp) / CHANGES_FILE
        with (
            before_paths.samples_jsonl.open("rb") as before_stream,
            after_paths.samples_jsonl.open("rb") as after_stream,
            spool.open("wb") as stream,
        ):
            seen = 0
            while True:
                before_row = before_stream.readline()
                after_row = after_stream.readline()
                if not before_row and not after_row:
                    break
                if not before_row or not after_row:
                    raise ValidationFailed("synthetic history versions have different row counts")
                if (
                    not before_row.endswith(b"\n")
                    or before_row.endswith(b"\r\n")
                    or not after_row.endswith(b"\n")
                    or after_row.endswith(b"\r\n")
                ):
                    raise ValidationFailed("synthetic history rows must use LF line endings")
                before_digest.update(before_row)
                after_digest.update(after_row)
                try:
                    before_sample = Sample.model_validate_json(before_row)
                    after_sample = Sample.model_validate_json(after_row)
                except ValidationError as error:
                    raise ValidationFailed(
                        f"invalid synthetic history row {seen + 1}: {error}"
                    ) from error
                expected_id = f"delta-{seen:08d}"
                if before_sample.sample_id != expected_id or after_sample.sample_id != expected_id:
                    raise ValidationFailed(
                        f"synthetic history row {seen + 1} must align on {expected_id!r}"
                    )
                before_payload = dump_sample(before_sample)
                after_payload = dump_sample(after_sample)
                if before_payload == after_payload:
                    raise ValidationFailed(
                        f"synthetic history row {seen + 1} is not a modified sample"
                    )
                fields, domains, effects = classify_rows(before_payload, after_payload)
                before_hash = hashlib.sha256(before_row).hexdigest()
                after_hash = hashlib.sha256(after_row).hexdigest()
                change = SampleChange(
                    change_id=make_change_id(
                        before_card.samples_hash,
                        after_card.samples_hash,
                        expected_id,
                        ChangeType.MODIFIED,
                        before_hash,
                        after_hash,
                    ),
                    from_dataset=before,
                    from_samples_hash=before_card.samples_hash,
                    to_dataset=after,
                    to_samples_hash=after_card.samples_hash,
                    sample_id=expected_id,
                    change_type=ChangeType.MODIFIED,
                    changed_domains=domains,
                    changed_fields=fields,
                    semantic_effects=effects,
                    before_row_hash=before_hash,
                    after_row_hash=after_hash,
                )
                stream.write(change.model_dump_json().encode())
                stream.write(b"\n")
                change_counts[change.change_type.value] += 1
                domain_counts.update(domain.value for domain in domains)
                effect_counts.update(effect.value for effect in effects)
                seen += 1
        if seen != count:
            raise ValidationFailed(f"synthetic history has {seen} rows, expected {count}")
        if before_digest.hexdigest() != before_card.samples_hash:
            raise IntegrityError("synthetic history before samples_hash mismatch")
        if after_digest.hexdigest() != after_card.samples_hash:
            raise IntegrityError("synthetic history after samples_hash mismatch")
        spec = ArtifactSpec(
            kind=KIND,
            id=ident,
            dataset=after,
            params={
                "from_dataset": before,
                "from_samples_hash": before_card.samples_hash,
                "to_dataset": after,
                "to_samples_hash": after_card.samples_hash,
                "policies": "",
            },
            inputs=[
                InputRef(
                    name="from_samples",
                    path=str(before_paths.samples_jsonl),
                    sha256=before_card.samples_hash,
                ),
                InputRef(
                    name="to_samples",
                    path=str(after_paths.samples_jsonl),
                    sha256=after_card.samples_hash,
                ),
            ],
        )
        summary = DatasetDiffSummary(
            artifact_id=ident,
            from_dataset=before,
            from_samples_hash=before_card.samples_hash,
            to_dataset=after,
            to_samples_hash=after_card.samples_hash,
            grade="fallback",
            policies=[],
            total_changes=seen,
            counts=dict(sorted(change_counts.items())),
            domain_counts=dict(sorted(domain_counts.items())),
            effect_counts=dict(sorted(effect_counts.items())),
        )
        with ArtifactWriter.create(spec, data_root=data) as writer:
            writer.add_file(CHANGES_FILE, spool)
            writer.write_json(SUMMARY_FILE, summary.model_dump(mode="json"))
            writer.commit()
            return writer.dir


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
        _write_synthetic_history_diff(
            data,
            configs,
            before,
            after,
            f"history-{index}",
            count=count,
        )
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
