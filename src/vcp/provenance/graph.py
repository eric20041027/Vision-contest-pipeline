"""Deterministic full-recompute graph built only from canonical VCP records."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vcp.artifact import store
from vcp.artifact.schema import ArtifactManifest
from vcp.backup.manifest import load_manifest as load_backup_manifest
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_text
from vcp.core.paths import DatasetPaths, artifacts_root, check_relative_path, runs_root
from vcp.data.dataset import Dataset
from vcp.data.materialize.manifest import ManifestRow
from vcp.data.source_audit import load_source_audit
from vcp.data.split import SplitPlan
from vcp.fuse.schema import FuseRecord
from vcp.measure.ledger import JUDGEMENTS_LEDGER, READINGS_LEDGER, read_rows
from vcp.measure.provenance import provenance as run_provenance
from vcp.measure.runs import load_run, verify_prediction
from vcp.measure.schema import Judgement, Reading, RunCard
from vcp.provenance.diff import KIND as DIFF_KIND
from vcp.provenance.diff import open_dataset_diff
from vcp.provenance.schema import ProvenanceEdge, ProvenanceEntity, SampleChange
from vcp.submit.schema import Staged


def entity_id(entity_type: str, key: str) -> str:
    return f"{entity_type}:{key}"


def dataset_version_id(dataset: str, samples_hash: str) -> str:
    return entity_id("dataset", f"{dataset}@{samples_hash}")


def sample_version_id(dataset_id: str, sample_id: str, row_hash: str) -> str:
    return entity_id("sample", f"{dataset_id.removeprefix('dataset:')}/{sample_id}@{row_hash}")


def split_plan_id(dataset: str, plan_id: str, dataset_hash: str) -> str:
    return entity_id("split", f"{dataset}/{plan_id}@{dataset_hash}")


def edge_id(source_id: str, target_id: str, edge_type: str, attributes: dict[str, Any]) -> str:
    payload = json.dumps(attributes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text("|".join([source_id, target_id, edge_type, payload]))


@dataclass
class ProvenanceGraph:
    entities: dict[str, ProvenanceEntity] = field(default_factory=dict)
    edges: dict[str, ProvenanceEdge] = field(default_factory=dict)
    changes: dict[str, SampleChange] = field(default_factory=dict)
    transitions: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    _outgoing_ids: dict[str, set[str]] = field(default_factory=dict, repr=False)
    _incoming_ids: dict[str, set[str]] = field(default_factory=dict, repr=False)

    def add_entity(self, entity: ProvenanceEntity) -> None:
        previous = self.entities.get(entity.entity_id)
        if previous is not None and previous != entity:
            raise IntegrityError(f"entity_conflict: {entity.entity_id}")
        self.entities[entity.entity_id] = entity

    def replace_entity(self, entity: ProvenanceEntity) -> None:
        self.entities[entity.entity_id] = entity

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        attributes: dict[str, Any] | None = None,
    ) -> ProvenanceEdge:
        attrs = attributes or {}
        ident = edge_id(source_id, target_id, edge_type, attrs)
        edge = ProvenanceEdge(
            edge_id=ident,
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            attributes=attrs,
        )
        previous = self.edges.get(ident)
        if previous is not None and previous != edge:
            raise IntegrityError(f"edge_conflict: {ident}")
        self.edges[ident] = edge
        self._outgoing_ids.setdefault(source_id, set()).add(ident)
        self._incoming_ids.setdefault(target_id, set()).add(ident)
        return edge

    def add_existing_edge(self, edge: ProvenanceEdge) -> None:
        previous = self.edges.get(edge.edge_id)
        if previous is not None and previous != edge:
            raise IntegrityError(f"edge_conflict: {edge.edge_id}")
        self.edges[edge.edge_id] = edge
        self._outgoing_ids.setdefault(edge.source_id, set()).add(edge.edge_id)
        self._incoming_ids.setdefault(edge.target_id, set()).add(edge.edge_id)

    def outgoing(self, source_id: str) -> list[ProvenanceEdge]:
        return sorted(
            (self.edges[ident] for ident in self._outgoing_ids.get(source_id, set())),
            key=lambda edge: edge.edge_id,
        )

    def incoming(self, target_id: str) -> list[ProvenanceEdge]:
        return sorted(
            (self.edges[ident] for ident in self._incoming_ids.get(target_id, set())),
            key=lambda edge: edge.edge_id,
        )

    def descendants(self, start_id: str) -> list[str]:
        seen = {start_id}
        queue = [start_id]
        while queue:
            current = queue.pop(0)
            for edge in self.outgoing(current):
                if edge.target_id not in seen:
                    seen.add(edge.target_id)
                    queue.append(edge.target_id)
        return sorted(seen)

    def normalized(self) -> dict[str, Any]:
        return {
            "entities": [
                self.entities[key].model_dump(mode="json") for key in sorted(self.entities)
            ],
            "edges": [self.edges[key].model_dump(mode="json") for key in sorted(self.edges)],
            "changes": [self.changes[key].model_dump(mode="json") for key in sorted(self.changes)],
            "transitions": [
                {"from": old, "to": new, "changes": sorted(change_ids)}
                for (old, new), change_ids in sorted(self.transitions.items())
            ],
            "gaps": sorted(self.gaps),
        }


def _broken(entity_type: str, key: str, reason: str) -> ProvenanceEntity:
    return ProvenanceEntity(
        entity_id=entity_id(entity_type, key),
        entity_type=entity_type,
        key=key,
        broken_reason=reason,
    )


def _scan_datasets(graph: ProvenanceGraph, data_root: Path, configs_root: Path) -> None:
    root = configs_root / "datasets"
    if not root.is_dir():
        return
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        name = directory.name
        try:
            card = Dataset.load_card(name, data_root=data_root, configs_root=configs_root)
        except (ValidationFailed, OSError) as error:
            graph.add_entity(_broken("dataset", f"{name}@unknown", str(error)))
            continue
        ident = dataset_version_id(name, card.samples_hash)
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        broken_reason: str | None = None
        audit_id: str | None = None
        try:
            if not paths.samples_jsonl.is_file():
                raise ValidationFailed(f"not_found: {paths.samples_jsonl}")
            actual = sha256_file(paths.samples_jsonl)
            if actual != card.samples_hash:
                raise IntegrityError(
                    f"mismatch: samples.jsonl {actual[:12]} != card {card.samples_hash[:12]}"
                )
            audit = load_source_audit(paths, card, data_root=data_root)
            audit_id = audit.artifact_id if audit is not None else None
        except (ValidationFailed, IntegrityError, OSError) as error:
            broken_reason = str(error)
        graph.add_entity(
            ProvenanceEntity(
                entity_id=ident,
                entity_type="dataset",
                key=f"{name}@{card.samples_hash}",
                dataset_version_id=ident,
                attributes={
                    "dataset": name,
                    "samples_hash": card.samples_hash,
                    "sample_count": card.sample_count,
                    "source_audit": audit_id,
                },
                broken_reason=broken_reason,
            )
        )


def _scan_plans(graph: ProvenanceGraph, configs_root: Path) -> None:
    root = configs_root / "datasets"
    if not root.is_dir():
        return
    for path in sorted(root.glob("*/splits/*.json")):
        try:
            plan = SplitPlan.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            key = path.relative_to(root).as_posix()
            graph.add_entity(_broken("split", key, str(error)))
            continue
        dataset_id = dataset_version_id(plan.dataset, plan.dataset_hash)
        ident = split_plan_id(plan.dataset, plan.plan_id, plan.dataset_hash)
        graph.add_entity(
            ProvenanceEntity(
                entity_id=ident,
                entity_type="split",
                key=f"{plan.dataset}/{plan.plan_id}@{plan.dataset_hash}",
                dataset_version_id=dataset_id,
                attributes={
                    "dataset": plan.dataset,
                    "plan_id": plan.plan_id,
                    "assignment": plan.assignment,
                    "roles": {subset.name: subset.role for subset in plan.subsets},
                },
                broken_reason=None if dataset_id in graph.entities else "dataset version missing",
            )
        )
        if dataset_id in graph.entities:
            graph.add_edge(dataset_id, ident, "USES_SPLIT")
        else:
            graph.gaps.append(f"{ident}: missing {dataset_id}")


def _scan_materialized_caches(graph: ProvenanceGraph, data_root: Path) -> None:
    """Index current portable decode-cache manifests; cache bytes remain replaceable."""
    root = data_root / "datasets"
    if not root.is_dir():
        return
    datasets = {
        entity.attributes.get("dataset"): ident
        for ident, entity in graph.entities.items()
        if entity.entity_type == "dataset"
    }
    for path in sorted(root.glob("*/cache/materialize/*/manifest.jsonl")):
        dataset_name = path.parents[3].name
        dataset_id = datasets.get(dataset_name)
        mode = path.parent.name
        manifest_sha = sha256_file(path)
        key = f"{dataset_name}/{mode}@{manifest_sha}"
        ident = entity_id("materialized_cache", key)
        broken: str | None = None
        rows: list[ManifestRow] = []
        seen: set[tuple[str, int | None, str | None]] = set()
        try:
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                row = ManifestRow.model_validate_json(line)
                row_key = (row.sample_id, row.view, row.seq_id)
                if row_key in seen:
                    raise IntegrityError(f"duplicate materialize row at {path}:{lineno}")
                seen.add(row_key)
                check_relative_path(row.out)
                output = (path.parent / row.out).resolve()
                output.relative_to(path.parent.resolve())
                if not output.is_file() or sha256_file(output) != row.sha256:
                    raise IntegrityError(f"materialized output mismatch: {output}")
                rows.append(row)
        except (OSError, ValueError, ValidationError, ValidationFailed, IntegrityError) as error:
            broken = str(error)
        if dataset_id is None:
            broken = f"dataset version missing for {dataset_name}"
        graph.add_entity(
            ProvenanceEntity(
                entity_id=ident,
                entity_type="materialized_cache",
                key=key,
                dataset_version_id=dataset_id,
                attributes={
                    "dataset": dataset_name,
                    "mode": mode,
                    "manifest_sha256": manifest_sha,
                    "rows": len(rows),
                },
                broken_reason=broken,
            )
        )
        if dataset_id is not None:
            graph.add_edge(dataset_id, ident, "PRODUCED_BY")


def _run_entity(
    card: RunCard,
    *,
    broken_reason: str | None = None,
    provenance_grade: str | None = None,
    observed: list[str] | None = None,
) -> ProvenanceEntity:
    dataset_id = dataset_version_id(card.dataset, card.samples_hash)
    return ProvenanceEntity(
        entity_id=entity_id("run", card.run_id),
        entity_type="run",
        key=card.run_id,
        dataset_version_id=dataset_id,
        attributes={
            "dataset": card.dataset,
            "samples_hash": card.samples_hash,
            "plan_id": card.plan_id,
            "trained_on": sorted(card.trained_on),
            "predicted_subsets": sorted(card.predictions),
            "access": [ref.model_dump(mode="json") for ref in card.access],
            "access_receipts": [ref.artifact_id for ref in card.access],
            "provenance_grade": provenance_grade,
            "observed_subsets": observed or [],
        },
        broken_reason=broken_reason,
    )


def _scan_runs(graph: ProvenanceGraph, data_root: Path, configs_root: Path) -> None:
    root = runs_root(data_root)
    if not root.is_dir():
        return
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        try:
            card = load_run(data_root, directory.name)
        except (ValidationFailed, OSError) as error:
            graph.add_entity(_broken("run", directory.name, str(error)))
            continue
        dataset_id = dataset_version_id(card.dataset, card.samples_hash)
        plan_id = split_plan_id(card.dataset, card.plan_id, card.samples_hash)
        missing = []
        if dataset_id not in graph.entities:
            missing.append(dataset_id)
        if plan_id not in graph.entities:
            missing.append(plan_id)
        for subset in sorted(card.predictions):
            try:
                verify_prediction(data_root, card, subset)
            except (ValidationFailed, IntegrityError, OSError) as error:
                missing.append(f"prediction/{subset}: {error}")
        info = run_provenance(card, data_root=data_root, configs_root=configs_root)
        if info.invalid:
            missing.extend(f"invalid access_receipt/{value}" for value in info.invalid)
        ident = entity_id("run", card.run_id)
        graph.add_entity(
            _run_entity(
                card,
                broken_reason=f"missing or invalid {missing}" if missing else None,
                provenance_grade=info.grade,
                observed=info.observed,
            )
        )
        export_hashes = {
            value
            for value in [
                card.source.export_manifest_sha,
                *(entry.export_manifest_sha for entry in card.predictions.values()),
            ]
            if value is not None
        }
        for export_hash in sorted(export_hashes):
            export = entity_id("export", export_hash)
            graph.add_entity(
                ProvenanceEntity(
                    entity_id=export,
                    entity_type="export",
                    key=export_hash,
                    dataset_version_id=dataset_id,
                    attributes={
                        "manifest_sha256": export_hash,
                        "evidence": "run_card_pin",
                    },
                )
            )
            if dataset_id in graph.entities:
                graph.add_edge(dataset_id, export, "PRODUCED_BY")
            graph.add_edge(export, ident, "CONSUMED_BY")
        if dataset_id in graph.entities:
            graph.add_edge(dataset_id, ident, "CONSUMED_BY")
        if plan_id in graph.entities:
            graph.add_edge(plan_id, ident, "USES_SPLIT")
        for receipt_ref in info.receipts:
            receipt = entity_id("artifact", f"access_receipt/{receipt_ref.artifact_id}")
            if receipt in graph.entities:
                graph.add_edge(receipt, ident, "CONSUMED_BY")


def _scan_measurements(graph: ProvenanceGraph, data_root: Path) -> None:
    root = data_root / "measure"
    if not root.is_dir():
        return
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        for reading in read_rows(directory / READINGS_LEDGER, Reading):
            ident = entity_id("reading", reading.reading_id)
            run = entity_id("run", reading.run_id)
            run_entity = graph.entities.get(run)
            identity_matches = (
                run_entity is not None
                and run_entity.attributes.get("dataset") == reading.dataset
                and run_entity.attributes.get("samples_hash") == reading.samples_hash
                and run_entity.attributes.get("plan_id") == reading.plan_id
            )
            graph.add_entity(
                ProvenanceEntity(
                    entity_id=ident,
                    entity_type="reading",
                    key=reading.reading_id,
                    dataset_version_id=dataset_version_id(reading.dataset, reading.samples_hash),
                    attributes={
                        "run_id": reading.run_id,
                        "plan_id": reading.plan_id,
                        "subset": reading.subset,
                        "metric": reading.metric,
                    },
                    broken_reason=(
                        None if identity_matches else f"run identity mismatch or missing {run}"
                    ),
                )
            )
            if identity_matches:
                graph.add_edge(run, ident, "EVALUATED_BY", {"subset": reading.subset})
        for judgement in read_rows(directory / JUDGEMENTS_LEDGER, Judgement):
            event_hash = sha256_text(
                json.dumps(
                    judgement.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            key = f"{directory.name}/{judgement.prereg_id}@{event_hash}"
            ident = entity_id("judgement", key)
            graph.add_entity(
                ProvenanceEntity(
                    entity_id=ident,
                    entity_type="judgement",
                    key=key,
                    attributes={
                        "prereg_id": judgement.prereg_id,
                        "ts": judgement.ts,
                        "verdict": judgement.verdict,
                    },
                )
            )
            sources = [entity_id("reading", value) for value in judgement.reading_ids]
            missing = [source for source in sources if source not in graph.entities]
            if missing:
                graph.replace_entity(
                    graph.entities[ident].model_copy(update={"broken_reason": f"missing {missing}"})
                )
            for source in sources:
                if source in graph.entities:
                    graph.add_edge(source, ident, "EVALUATED_BY")


def _scan_fusions(graph: ProvenanceGraph, data_root: Path) -> None:
    root = runs_root(data_root)
    if not root.is_dir():
        return
    for path in sorted(root.glob("*/fuse.json")):
        try:
            record = FuseRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            ident = entity_id("run", path.parent.name)
            if ident in graph.entities:
                graph.replace_entity(
                    graph.entities[ident].model_copy(update={"broken_reason": str(error)})
                )
            continue
        target = entity_id("run", record.run_id)
        if target in graph.entities:
            graph.replace_entity(
                graph.entities[target].model_copy(
                    update={
                        "entity_type": "fusion_run",
                        "attributes": {
                            **graph.entities[target].attributes,
                            "recipe_id": record.recipe_id,
                            "members": [member.run for member in record.members],
                        },
                    }
                )
            )
        missing_members: list[str] = []
        for member in record.members:
            source = entity_id("run", member.run)
            if source in graph.entities and target in graph.entities:
                graph.add_edge(source, target, "COMBINED_INTO")
            else:
                graph.gaps.append(f"{target}: missing fusion member {source}")
                missing_members.append(source)
        if missing_members and target in graph.entities:
            prior = graph.entities[target].broken_reason
            reason = f"missing fusion member {', '.join(missing_members)}"
            graph.replace_entity(
                graph.entities[target].model_copy(
                    update={"broken_reason": f"{prior}; {reason}" if prior else reason}
                )
            )


def _artifact_source(path: str, data_root: Path) -> str | None:
    candidate = Path(path)
    try:
        relative = candidate.relative_to(data_root).as_posix() if candidate.is_absolute() else path
    except ValueError:
        return None
    parts = relative.replace("\\", "/").split("/")
    if len(parts) >= 2 and parts[0] == "runs":
        return entity_id("run", parts[1])
    if len(parts) >= 3 and parts[0] == "artifacts":
        return entity_id("artifact", f"{parts[1]}/{parts[2]}")
    return None


def _artifact_entity(manifest: ArtifactManifest, broken_reason: str | None) -> ProvenanceEntity:
    key = f"{manifest.spec.kind}/{manifest.spec.id}"
    return ProvenanceEntity(
        entity_id=entity_id("artifact", key),
        entity_type="artifact",
        key=key,
        attributes={
            "kind": manifest.spec.kind,
            "id": manifest.spec.id,
            "verified": broken_reason is None,
            "dataset": manifest.spec.dataset,
            "params": manifest.spec.params,
            "inputs": [ref.model_dump(mode="json") for ref in manifest.spec.inputs],
            "supersedes": manifest.spec.supersedes,
            "supersedes_reason": manifest.spec.supersedes_reason,
        },
        broken_reason=broken_reason,
    )


def _artifact_dataset_sources(graph: ProvenanceGraph, manifest: ArtifactManifest) -> list[str]:
    """Resolve dataset inputs by canonical dataset name and exact samples hash."""
    hashes = {
        value
        for key, value in manifest.spec.params.items()
        if key == "samples_hash" or key.endswith("_samples_hash")
    }
    hashes.update(
        ref.sha256
        for ref in manifest.spec.inputs
        if ref.sha256 is not None and ref.name in {"samples", "from_samples", "to_samples"}
    )
    names = {
        value
        for key, value in manifest.spec.params.items()
        if key == "dataset" or key.endswith("_dataset")
    }
    if manifest.spec.dataset is not None:
        names.add(manifest.spec.dataset)
    return sorted(
        ident
        for ident, entity in graph.entities.items()
        if entity.entity_type == "dataset"
        and entity.attributes.get("dataset") in names
        and entity.attributes.get("samples_hash") in hashes
    )


def add_artifact(
    graph: ProvenanceGraph,
    data_root: Path,
    manifest: ArtifactManifest,
) -> ProvenanceEntity:
    """Add one already parsed artifact and every resolvable canonical input edge."""
    result = store.verify(data_root, manifest.spec.kind, manifest.spec.id)
    verification_failed = result.failed or result.unlinked
    broken = (
        f"artifact verification failed: mismatch={len(result.mismatch)} "
        f"missing={len(result.missing)} extra={len(result.extra)} "
        f"unlinked={result.unlinked}"
        if verification_failed
        else None
    )
    artifact = _artifact_entity(manifest, broken)
    graph.add_entity(artifact)
    dataset_sources = _artifact_dataset_sources(graph, manifest)
    if len(dataset_sources) == 1:
        artifact = artifact.model_copy(update={"dataset_version_id": dataset_sources[0]})
        graph.replace_entity(artifact)
    for source in dataset_sources:
        graph.add_edge(source, artifact.entity_id, "PRODUCED_BY", {"input": "dataset"})
    for ref in manifest.spec.inputs:
        if ref.path is None:
            continue
        source = _artifact_source(ref.path, data_root)
        if source in graph.entities:
            graph.add_edge(source, artifact.entity_id, "PRODUCED_BY", {"input": ref.name})
    run_name = manifest.spec.params.get("run")
    run = entity_id("run", run_name) if run_name else None
    # Access receipts are linked only after RunCard identity/hash validation in _scan_runs.
    if manifest.spec.kind != "access_receipt" and run in graph.entities:
        graph.add_edge(artifact.entity_id, run, "CONSUMED_BY")
    return artifact


def add_dataset_diff_transition(graph: ProvenanceGraph, data_root: Path, artifact_id: str) -> None:
    """Replay one verified diff transition into a graph whose dataset entities already exist.

    The diff is validated end to end before the first mutation; its events are then streamed
    one at a time, so replaying a large history never materializes every event twice.
    """
    stream = open_dataset_diff(data_root, artifact_id)
    old = dataset_version_id(stream.summary.from_dataset, stream.summary.from_samples_hash)
    new = dataset_version_id(stream.summary.to_dataset, stream.summary.to_samples_hash)
    if old not in graph.entities or new not in graph.entities:
        raise ValidationFailed(
            f"missing dataset version for dataset_diff/{artifact_id}; sync or rebuild first"
        )
    reachable = {new}
    queue = [new]
    while queue:
        source = queue.pop()
        for transition_source, transition_target in graph.transitions:
            if transition_source == source and transition_target not in reachable:
                reachable.add(transition_target)
                queue.append(transition_target)
    if old in reachable:
        raise IntegrityError(f"dataset_cycle: {old} -> {new} closes an evolution cycle")
    attrs = {"artifact": artifact_id, "total_changes": stream.summary.total_changes}
    graph.add_edge(old, new, "DERIVED_FROM", attrs)
    previous_transition = graph.transitions.get((old, new))
    if previous_transition is not None and previous_transition != stream.change_ids:
        raise IntegrityError(f"transition_conflict: {old} -> {new}")
    graph.transitions[(old, new)] = stream.change_ids
    artifact = entity_id("artifact", f"{DIFF_KIND}/{artifact_id}")
    for change in stream.events:
        previous = graph.changes.get(change.change_id)
        if previous is not None and previous != change:
            raise IntegrityError(f"change_conflict: {change.change_id}")
        graph.changes[change.change_id] = change
        for side, dataset_id, row_hash in (
            ("before", old, change.before_row_hash),
            ("after", new, change.after_row_hash),
        ):
            if row_hash is None:
                continue
            sample = sample_version_id(dataset_id, change.sample_id, row_hash)
            graph.add_entity(
                ProvenanceEntity(
                    entity_id=sample,
                    entity_type="sample",
                    key=sample.removeprefix("sample:"),
                    dataset_version_id=dataset_id,
                    attributes={"sample_id": change.sample_id, "row_sha256": row_hash},
                )
            )
            if artifact in graph.entities:
                graph.add_edge(
                    artifact,
                    sample,
                    "CONTAINS_CHANGE",
                    {"change_id": change.change_id, "side": side},
                )


def _scan_artifacts(graph: ProvenanceGraph, data_root: Path) -> None:
    root = artifacts_root(data_root)
    if not root.is_dir():
        return
    diff_ids: list[str] = []
    for kind_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        for directory in sorted(path for path in kind_dir.iterdir() if path.is_dir()):
            if not (directory / "manifest.json").is_file():
                continue
            try:
                manifest = store.load_manifest(data_root, kind_dir.name, directory.name)
            except (ValidationFailed, IntegrityError, OSError) as error:
                key = f"{kind_dir.name}/{directory.name}"
                graph.add_entity(_broken("artifact", key, str(error)))
                graph.gaps.append(f"{key}: invalid manifest")
                continue
            add_artifact(graph, data_root, manifest)
            if kind_dir.name == DIFF_KIND:
                diff_ids.append(directory.name)
    for artifact_id in sorted(diff_ids):
        try:
            add_dataset_diff_transition(graph, data_root, artifact_id)
        except (ValidationFailed, IntegrityError) as error:
            ident = entity_id("artifact", f"{DIFF_KIND}/{artifact_id}")
            graph.replace_entity(
                graph.entities[ident].model_copy(update={"broken_reason": str(error)})
            )
            graph.gaps.append(f"dataset_diff/{artifact_id}: {error}")


def _link_artifact_runs(graph: ProvenanceGraph, data_root: Path) -> None:
    """Resolve run outputs after run entities exist, without rehashing artifact payloads."""
    del data_root  # artifacts were verified and normalized during the first pass
    for artifact_entity in sorted(graph.entities.values(), key=lambda item: item.entity_id):
        if artifact_entity.entity_type != "artifact":
            continue
        if artifact_entity.attributes.get("kind") == "access_receipt":
            continue
        params = artifact_entity.attributes.get("params", {})
        run_name = params.get("run") if isinstance(params, dict) else None
        run = entity_id("run", run_name) if run_name else None
        if run in graph.entities:
            graph.add_edge(artifact_entity.entity_id, run, "CONSUMED_BY")


def _link_artifact_inputs(graph: ProvenanceGraph, data_root: Path) -> None:
    """Resolve artifact-to-artifact inputs after every artifact entity has been scanned."""
    for artifact_entity in sorted(graph.entities.values(), key=lambda item: item.entity_id):
        if artifact_entity.entity_type != "artifact":
            continue
        inputs = artifact_entity.attributes.get("inputs", [])
        for ref in inputs if isinstance(inputs, list) else []:
            path = ref.get("path") if isinstance(ref, dict) else None
            name = ref.get("name") if isinstance(ref, dict) else None
            source = _artifact_source(path, data_root) if isinstance(path, str) else None
            if source in graph.entities and isinstance(name, str):
                graph.add_edge(source, artifact_entity.entity_id, "PRODUCED_BY", {"input": name})


def _link_artifact_supersessions(graph: ProvenanceGraph) -> None:
    """Link verified immutable artifacts after directory-order-independent discovery."""
    for successor in sorted(graph.entities.values(), key=lambda item: item.entity_id):
        if successor.entity_type != "artifact":
            continue
        if successor.attributes.get("verified") is not True:
            continue
        old = successor.attributes.get("supersedes")
        kind = successor.attributes.get("kind")
        if not isinstance(old, str) or not isinstance(kind, str):
            continue
        predecessor = entity_id("artifact", f"{kind}/{old}")
        if predecessor in graph.entities:
            graph.add_edge(
                predecessor,
                successor.entity_id,
                "SUPERSEDES",
                {"reason": successor.attributes.get("supersedes_reason")},
            )


def _scan_submissions(graph: ProvenanceGraph, data_root: Path) -> None:
    root = data_root / "submit"
    if not root.is_dir():
        return
    for path in sorted(root.glob("*/*/stage.json")):
        key = f"{path.parent.parent.name}/{path.parent.name}"
        ident = entity_id("submission", key)
        try:
            staged = Staged.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            graph.add_entity(_broken("submission", key, str(error)))
            continue
        graph.add_entity(
            ProvenanceEntity(
                entity_id=ident,
                entity_type="submission",
                key=key,
                attributes={"dataset": staged.dataset, "kind": staged.kind},
            )
        )
        for run_id in [staged.eval_run, staged.test_run]:
            source = entity_id("run", run_id) if run_id else None
            if source in graph.entities:
                graph.add_edge(source, ident, "SUBMITTED_AS")
            elif source is not None:
                graph.replace_entity(
                    graph.entities[ident].model_copy(update={"broken_reason": f"missing {source}"})
                )


def _source_for_backup(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "runs":
        return entity_id("run", parts[1])
    if len(parts) >= 3 and parts[0] == "artifacts":
        return entity_id("artifact", f"{parts[1]}/{parts[2]}")
    if len(parts) >= 3 and parts[0] == "submit":
        return entity_id("submission", f"{parts[1]}/{parts[2]}")
    return None


def _scan_backups(graph: ProvenanceGraph, data_root: Path, configs_root: Path) -> None:
    root = configs_root / "datasets"
    if not root.is_dir():
        return
    for path in sorted(root.glob("*/backup/*.json")):
        dataset = path.parents[1].name
        manifest_id = path.stem
        key = f"{dataset}/{manifest_id}"
        ident = entity_id("backup", key)
        try:
            paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
            manifest = load_backup_manifest(paths, manifest_id)
        except (ValidationFailed, OSError) as error:
            graph.add_entity(_broken("backup", key, str(error)))
            continue
        graph.add_entity(
            ProvenanceEntity(
                entity_id=ident,
                entity_type="backup",
                key=key,
                attributes={"conclusion": manifest.conclusion},
            )
        )
        for entry in manifest.files:
            source = _source_for_backup(entry.path)
            if source in graph.entities:
                graph.add_edge(source, ident, "BACKED_UP_AS")


def build_graph(data_root: Path, configs_root: Path) -> ProvenanceGraph:
    """Full correctness oracle: rebuild a normalized graph from canonical files."""
    data_root = Path(data_root).resolve()
    configs_root = Path(configs_root).resolve()
    graph = ProvenanceGraph()
    _scan_datasets(graph, data_root, configs_root)
    _scan_plans(graph, configs_root)
    _scan_materialized_caches(graph, data_root)
    # Artifact entities must exist before run access receipts can be linked and validated.
    _scan_artifacts(graph, data_root)
    _link_artifact_inputs(graph, data_root)
    _link_artifact_supersessions(graph)
    _scan_runs(graph, data_root, configs_root)
    _scan_measurements(graph, data_root)
    _scan_fusions(graph, data_root)
    _link_artifact_runs(graph, data_root)
    _scan_submissions(graph, data_root)
    _scan_backups(graph, data_root, configs_root)
    return graph
