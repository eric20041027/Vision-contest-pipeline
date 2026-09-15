"""Pure full-recompute impact, stale and explanation views over a provenance graph."""

from __future__ import annotations

from collections import deque

from vcp.core.errors import ValidationFailed
from vcp.provenance.graph import ProvenanceGraph
from vcp.provenance.schema import (
    EntityStatus,
    ExplainResult,
    ImpactResult,
    SampleChange,
    SemanticEffect,
    StatusRecord,
)

_PRIORITY = {
    EntityStatus.VALID: 0,
    EntityStatus.REVIEW: 1,
    EntityStatus.STALE: 2,
    EntityStatus.BROKEN: 3,
}


def _set_status(
    statuses: dict[str, StatusRecord],
    entity_id: str,
    status: EntityStatus,
    reason: str,
    predecessor_id: str | None = None,
) -> None:
    previous = statuses[entity_id]
    candidate_key = (reason, predecessor_id or "")
    previous_key = (previous.reason, previous.predecessor_id or "")
    if _PRIORITY[status] > _PRIORITY[previous.status] or (
        status == previous.status and candidate_key < previous_key
    ):
        statuses[entity_id] = StatusRecord(
            entity_id=entity_id,
            status=status,
            reason=reason,
            predecessor_id=predecessor_id,
        )


def _changes_to_head(graph: ProvenanceGraph, head_id: str) -> dict[str, list[SampleChange]]:
    """For each dataset ancestor, union the events on every path that reaches ``head_id``."""
    if head_id not in graph.entities:
        raise ValidationFailed(f"not_found: provenance entity {head_id!r}")
    changes: dict[str, dict[str, SampleChange]] = {head_id: {}}
    queue = deque([head_id])
    while queue:
        target = queue.popleft()
        tail = changes[target]
        predecessors = sorted(old for old, new in graph.transitions if new == target)
        for source in predecessors:
            merged = dict(changes.get(source, {}))
            merged.update(tail)
            for change_id in graph.transitions[(source, target)]:
                merged[change_id] = graph.changes[change_id]
            previous = changes.get(source)
            if previous is None or set(previous) != set(merged):
                changes[source] = merged
                queue.append(source)
    return {
        dataset_id: [items[key] for key in sorted(items)] for dataset_id, items in changes.items()
    }


def _plan_for(graph: ProvenanceGraph, dataset_id: str | None, plan_id: str | None):
    matches = [
        entity
        for entity in graph.entities.values()
        if entity.entity_type == "split"
        and entity.dataset_version_id == dataset_id
        and entity.attributes.get("plan_id") == plan_id
    ]
    return sorted(matches, key=lambda entity: entity.entity_id)[0] if matches else None


def _subset(plan, sample_id: str) -> str | None:
    if plan is None:
        return None
    assignment = plan.attributes.get("assignment", {})
    return assignment.get(sample_id) if isinstance(assignment, dict) else None


def _run_status(
    graph: ProvenanceGraph,
    statuses: dict[str, StatusRecord],
    run_id: str,
    changes: list[SampleChange],
) -> None:
    run = graph.entities[run_id]
    plan = _plan_for(graph, run.dataset_version_id, str(run.attributes.get("plan_id")))
    trained = set(run.attributes.get("trained_on", []))
    predicted = set(run.attributes.get("predicted_subsets", []))
    for change in changes:
        effects = set(change.semantic_effects)
        subset = _subset(plan, change.sample_id)
        if SemanticEffect.SPLIT_AFFECTING in effects:
            _set_status(
                statuses,
                run_id,
                EntityStatus.STALE,
                f"split-affecting change for sample {change.sample_id}",
                run.dataset_version_id,
            )
            continue
        if (
            effects
            & {
                SemanticEffect.INPUT_AFFECTING,
                SemanticEffect.TRAINING_AFFECTING,
            }
            and subset in trained
        ):
            _set_status(
                statuses,
                run_id,
                EntityStatus.STALE,
                f"training-affecting change in subset {subset} for sample {change.sample_id}",
                run.dataset_version_id,
            )
        if SemanticEffect.UNKNOWN in effects and (
            subset is None or subset in trained or subset in predicted
        ):
            _set_status(
                statuses,
                run_id,
                EntityStatus.REVIEW,
                f"unclassified change for sample {change.sample_id}",
                run.dataset_version_id,
            )


def _reading_status(
    graph: ProvenanceGraph,
    statuses: dict[str, StatusRecord],
    reading_id: str,
    changes: list[SampleChange],
) -> None:
    reading = graph.entities[reading_id]
    run_id = f"run:{reading.attributes.get('run_id')}"
    if run_id in statuses and statuses[run_id].status != EntityStatus.VALID:
        inherited = statuses[run_id]
        _set_status(
            statuses,
            reading_id,
            inherited.status,
            f"upstream {run_id} is {inherited.status.value}",
            run_id,
        )
        return
    plan = _plan_for(graph, reading.dataset_version_id, str(reading.attributes.get("plan_id")))
    wanted = reading.attributes.get("subset")
    for change in changes:
        subset = _subset(plan, change.sample_id)
        if subset != wanted:
            continue
        effects = set(change.semantic_effects)
        if effects & {
            SemanticEffect.INPUT_AFFECTING,
            SemanticEffect.EVALUATION_AFFECTING,
            SemanticEffect.SPLIT_AFFECTING,
        }:
            _set_status(
                statuses,
                reading_id,
                EntityStatus.STALE,
                f"evaluation-affecting change in subset {wanted} for sample {change.sample_id}",
                reading.dataset_version_id,
            )
        elif SemanticEffect.UNKNOWN in effects:
            _set_status(
                statuses,
                reading_id,
                EntityStatus.REVIEW,
                f"unclassified change in subset {wanted} for sample {change.sample_id}",
                reading.dataset_version_id,
            )


def compute_statuses_for_entities(
    graph: ProvenanceGraph,
    head_id: str,
    entity_ids: set[str],
    *,
    base: dict[str, StatusRecord] | None = None,
    preserve_selected_base: bool = False,
) -> dict[str, StatusRecord]:
    """Recompute a dependency-closed dirty set, reusing clean predecessor statuses."""
    by_dataset = _changes_to_head(graph, head_id)
    selected = set(entity_ids) & set(graph.entities)
    predecessors = {
        edge.source_id
        for ident in selected
        for edge in graph.incoming(ident)
        if edge.source_id not in selected
    }
    statuses = dict(base or {})
    for entity_id, entity in graph.entities.items():
        needed = entity_id in selected or entity_id in predecessors
        if not needed:
            continue
        if preserve_selected_base and entity_id in statuses and entity.broken_reason is None:
            continue
        if entity_id in predecessors and entity_id in statuses:
            continue
        statuses[entity_id] = StatusRecord(
            entity_id=entity_id,
            status=EntityStatus.BROKEN if entity.broken_reason else EntityStatus.VALID,
            reason=entity.broken_reason or "canonical evidence is current",
        )
    for entity_id in sorted(selected):
        entity = graph.entities[entity_id]
        changes = by_dataset.get(entity.dataset_version_id or "", [])
        if not changes or statuses[entity_id].status == EntityStatus.BROKEN:
            continue
        if entity.entity_type == "split":
            effects = {effect for change in changes for effect in change.semantic_effects}
            if SemanticEffect.SPLIT_AFFECTING in effects:
                _set_status(
                    statuses,
                    entity_id,
                    EntityStatus.STALE,
                    "split-affecting dataset evolution",
                    entity.dataset_version_id,
                )
            elif SemanticEffect.UNKNOWN in effects:
                _set_status(
                    statuses,
                    entity_id,
                    EntityStatus.REVIEW,
                    "unclassified dataset evolution may affect split",
                    entity.dataset_version_id,
                )
        elif entity.entity_type in {"run", "fusion_run"}:
            _run_status(graph, statuses, entity_id, changes)
        elif entity.entity_type in {"artifact", "export", "materialized_cache"}:
            effects = {effect for change in changes for effect in change.semantic_effects}
            if effects - {SemanticEffect.DISPLAY_ONLY, SemanticEffect.UNKNOWN}:
                _set_status(
                    statuses,
                    entity_id,
                    EntityStatus.STALE,
                    "dataset evolution affects derived data",
                    entity.dataset_version_id,
                )
            elif SemanticEffect.UNKNOWN in effects:
                _set_status(
                    statuses,
                    entity_id,
                    EntityStatus.REVIEW,
                    "unclassified dataset evolution may affect derived data",
                    entity.dataset_version_id,
                )
    for entity_id in sorted(selected):
        entity = graph.entities[entity_id]
        if entity.entity_type == "reading" and statuses[entity_id].status != EntityStatus.BROKEN:
            _reading_status(
                graph,
                statuses,
                entity_id,
                by_dataset.get(entity.dataset_version_id or "", []),
            )
    inheriting = {"judgement", "fusion_run", "artifact", "submission", "backup"}
    changed = True
    while changed:
        changed = False
        for target_id in sorted(selected):
            target = graph.entities[target_id]
            before = statuses[target_id]
            for edge in graph.incoming(target_id):
                source_status = statuses.get(edge.source_id)
                if source_status is None:
                    continue
                # Missing/corrupt required evidence always poisons every dependent. Semantic
                # STALE/REVIEW propagation remains deliberately restricted because runs/readings
                # are handled with split-aware rules above.
                if source_status.status == EntityStatus.BROKEN:
                    _set_status(
                        statuses,
                        target_id,
                        EntityStatus.BROKEN,
                        f"upstream {edge.source_id} is BROKEN",
                        edge.source_id,
                    )
                elif (
                    target.entity_type in inheriting and source_status.status != EntityStatus.VALID
                ):
                    _set_status(
                        statuses,
                        target_id,
                        source_status.status,
                        f"upstream {edge.source_id} is {source_status.status.value}",
                        edge.source_id,
                    )
            changed = changed or statuses[target_id] != before
    return {ident: statuses[ident] for ident in sorted(selected)}


def compute_statuses(graph: ProvenanceGraph, head_id: str) -> dict[str, StatusRecord]:
    """Correctness oracle for all entity statuses relative to one dataset head."""
    return compute_statuses_for_entities(graph, head_id, set(graph.entities))


def impact(
    graph: ProvenanceGraph,
    dataset_id: str,
    sample_id: str | None,
    head_id: str | None = None,
) -> ImpactResult:
    if dataset_id not in graph.entities:
        raise ValidationFailed(f"not_found: provenance entity {dataset_id!r}")
    entity_ids = graph.descendants(dataset_id)
    statuses = compute_statuses(graph, head_id) if head_id is not None else {}
    if sample_id is not None:
        if head_id is None:
            raise ValidationFailed("sample impact needs an unambiguous dataset head")
        filtered = ProvenanceGraph(
            entities=dict(graph.entities),
            gaps=list(graph.gaps),
        )
        for edge in graph.edges.values():
            filtered.add_existing_edge(edge)
        for transition, change_ids in graph.transitions.items():
            kept = [
                change_id
                for change_id in change_ids
                if graph.changes[change_id].sample_id == sample_id
            ]
            if kept:
                filtered.transitions[transition] = kept
                for change_id in kept:
                    filtered.changes[change_id] = graph.changes[change_id]
        reachable = set(entity_ids)
        to_head = _changes_to_head(filtered, head_id)
        matching = {
            change.change_id
            for change in to_head.get(dataset_id, [])
            if change.sample_id == sample_id
        }
        if not matching:
            return ImpactResult(
                dataset_version_id=dataset_id,
                sample_id=sample_id,
                head_id=head_id,
                entity_ids=[dataset_id],
                statuses={},
            )
        statuses = compute_statuses(filtered, head_id)
        affected = {ident for ident in reachable if statuses[ident].status != EntityStatus.VALID}
        selected = {dataset_id, *affected}
        selected.update(
            ident
            for ident in reachable
            if filtered.entities[ident].entity_type == "sample"
            and filtered.entities[ident].attributes.get("sample_id") == sample_id
        )
        for source, target in filtered.transitions:
            if source in reachable and source in to_head:
                selected.update({source, target})
        queue = deque(affected)
        while queue:
            target = queue.popleft()
            for edge in filtered.incoming(target):
                if edge.source_id in reachable and edge.source_id not in selected:
                    selected.add(edge.source_id)
                    queue.append(edge.source_id)
        entity_ids = sorted(selected)
    return ImpactResult(
        dataset_version_id=dataset_id,
        sample_id=sample_id,
        head_id=head_id,
        entity_ids=entity_ids,
        statuses={key: statuses[key] for key in entity_ids if key in statuses},
    )


def explain(graph: ProvenanceGraph, entity_id: str) -> ExplainResult:
    if entity_id not in graph.entities:
        raise ValidationFailed(f"not_found: provenance entity {entity_id!r}")
    seen = {entity_id}
    queue = deque([entity_id])
    edges = []
    while queue:
        target = queue.popleft()
        for edge in graph.incoming(target):
            edges.append(edge)
            if edge.source_id not in seen:
                seen.add(edge.source_id)
                queue.append(edge.source_id)
    return ExplainResult(
        entity_id=entity_id,
        ancestor_ids=sorted(seen - {entity_id}),
        edges=sorted(edges, key=lambda edge: edge.edge_id),
    )
