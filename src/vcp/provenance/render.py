"""A drawable view of the provenance graph: pick a scope, fold what a reader does not need, and
keep every node's status honest.

Pure functions over an already loaded ``ProvenanceGraph`` and its status records -- nothing here
reads or writes files. ``vcp.provenance.render_mermaid`` turns a ``GraphView`` into text.

Overview folds (``detail="overview"``):

- readings disappear into run -> judgement arrows labelled with the subsets they read;
- exports, decode caches, backups, access receipts, source audits and dataset diffs disappear;
  what they prove shows up as a run's provenance grade, a thick border (backed up) or the
  label of a dataset-evolution arrow;
- artifacts of any other kind (a contest's own) merge into one node per kind, with counts;
- samples are never drawn, in either detail level (``vcp provenance impact --sample`` is the
  sample-level question).

Entity types this module does not know are drawn as their own node, never silently dropped.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from vcp.core.errors import ValidationFailed
from vcp.provenance.graph import ProvenanceGraph
from vcp.provenance.schema import EntityStatus, ProvenanceEdge, ProvenanceEntity, StatusRecord
from vcp.provenance.views import _PRIORITY

DETAILS = ("overview", "full")
FOLDED_TYPES = frozenset({"reading", "export", "materialized_cache", "backup", "sample"})
FOLDED_ARTIFACT_KINDS = frozenset({"access_receipt", "source_audit", "dataset_diff"})
ARTIFACT_KIND = "artifact_kind"

_VALID_REASON = "canonical evidence is current"
_LINE_LIMIT = 60
_STATUS_ORDER = (EntityStatus.VALID, EntityStatus.REVIEW, EntityStatus.STALE, EntityStatus.BROKEN)
_KIND_ORDER = {
    "dataset": 0,
    "split": 1,
    "run": 2,
    "fusion_run": 3,
    "judgement": 4,
    "submission": 5,
    ARTIFACT_KIND: 6,
}


@dataclass(frozen=True)
class ViewNode:
    node_id: str
    kind: str  # the entity type, or ARTIFACT_KIND for one merged contest artifact kind
    label: tuple[str, ...]
    group: str | None  # dataset name whose frame the node is drawn in
    status: EntityStatus
    reason: str
    members: tuple[str, ...]  # the entity ids this node stands for
    backed_up: bool


@dataclass(frozen=True)
class ViewEdge:
    source: str
    target: str
    label: str
    edge_types: tuple[str, ...]
    count: int  # distinct (source entity, target entity) pairs behind the arrow
    thick: bool  # dataset evolution


@dataclass(frozen=True)
class GraphView:
    scope: str
    detail: str
    nodes: tuple[ViewNode, ...]
    edges: tuple[ViewEdge, ...]
    entities: int  # entities in scope
    folded: int  # entities in scope that are not drawn as their own node
    counts: Mapping[str, int]  # status -> entities in scope
    hidden_alerts: tuple[StatusRecord, ...]  # non-VALID entities in scope with no node at all


@dataclass
class _EdgeParts:
    types: set[str] = field(default_factory=set)
    subsets: set[str] = field(default_factory=set)
    diffs: list[str] = field(default_factory=list)
    pairs: set[tuple[str, str]] = field(default_factory=set)  # (source entity, target entity)


def node_id(key: str) -> str:
    """A Mermaid-safe id that stays the same for one entity across scopes and re-renders."""
    return "n" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def validate_detail(detail: str) -> str:
    if detail not in DETAILS:
        raise ValidationFailed(f"unsupported_detail: {detail}; use overview or full")
    return detail


def worst_statuses(maps: Iterable[Mapping[str, StatusRecord]]) -> dict[str, StatusRecord]:
    """Per entity, the most severe record over several heads; ties keep the earlier map."""
    merged: dict[str, StatusRecord] = {}
    for statuses in maps:
        for ident, record in statuses.items():
            current = merged.get(ident)
            if current is None or _PRIORITY[record.status] > _PRIORITY[current.status]:
                merged[ident] = record
    return merged


def _reachable(graph: ProvenanceGraph, start: str, *, forward: bool) -> set[str]:
    seen = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        edges = graph.outgoing(current) if forward else graph.incoming(current)
        for edge in edges:
            following = edge.target_id if forward else edge.source_id
            if following not in seen:
                seen.add(following)
                queue.append(following)
    return seen


def _dataset_versions(graph: ProvenanceGraph, value: str) -> list[str]:
    exact = graph.entities.get(value)
    if exact is not None and exact.entity_type == "dataset":
        return [value]
    versions = sorted(
        ident
        for ident, entity in graph.entities.items()
        if entity.entity_type == "dataset" and entity.attributes.get("dataset") == value
    )
    if not versions:
        raise ValidationFailed(f"not_found: dataset {value!r} in provenance index")
    return versions


def scope_entities(
    graph: ProvenanceGraph, *, dataset: str | None = None, entity: str | None = None
) -> tuple[str, set[str]]:
    """``(label, entity ids)``: everything, or the anchors plus all their ancestors and
    descendants. A dataset name anchors every indexed version carrying that name."""
    if dataset is not None and entity is not None:
        raise ValidationFailed("scope_conflict: pass --dataset or --entity, not both")
    if dataset is None and entity is None:
        return "all", set(graph.entities)
    if entity is not None:
        if entity not in graph.entities:
            raise ValidationFailed(f"not_found: provenance entity {entity!r}")
        anchors, label = [entity], f"entity:{entity}"
    else:
        assert dataset is not None
        anchors, label = _dataset_versions(graph, dataset), f"dataset:{dataset}"
    selected: set[str] = set()
    for anchor in anchors:
        selected |= _reachable(graph, anchor, forward=True)
        selected |= _reachable(graph, anchor, forward=False)
    return label, selected


def _intrinsic(graph: ProvenanceGraph, ident: str) -> StatusRecord:
    broken = graph.entities[ident].broken_reason
    return StatusRecord(
        entity_id=ident,
        status=EntityStatus.BROKEN if broken else EntityStatus.VALID,
        reason=broken or _VALID_REASON,
    )


def fallback_statuses(graph: ProvenanceGraph) -> dict[str, StatusRecord]:
    """Statuses when the index has no dataset head to judge change against (a wrong
    ``--configs-root``, or nothing imported yet). Change cannot be judged, but broken evidence
    still poisons everything downstream of it, as in ``views.compute_statuses``."""
    records = {ident: _intrinsic(graph, ident) for ident in graph.entities}
    queue = deque(sorted(i for i, r in records.items() if r.status == EntityStatus.BROKEN))
    while queue:
        source = queue.popleft()
        for edge in graph.outgoing(source):
            target = edge.target_id
            if target in records and records[target].status != EntityStatus.BROKEN:
                records[target] = StatusRecord(
                    entity_id=target,
                    status=EntityStatus.BROKEN,
                    reason=f"upstream {source} is BROKEN",
                    predecessor_id=source,
                )
                queue.append(target)
    return records


def _status_of(
    graph: ProvenanceGraph, statuses: Mapping[str, StatusRecord], ident: str
) -> StatusRecord:
    record = statuses.get(ident)
    return record if record is not None else _intrinsic(graph, ident)


def _artifact_kind(entity: ProvenanceEntity) -> str:
    # The key is "<kind>/<id>" for every artifact, including one whose manifest did not parse.
    return entity.key.split("/", 1)[0]


def _representative(entity: ProvenanceEntity, detail: str) -> str | None:
    """The node key an entity is drawn as, or None when it is folded away."""
    if entity.entity_type == "sample":
        return None
    if detail == "full":
        return entity.entity_id
    if entity.entity_type in FOLDED_TYPES:
        return None
    if entity.entity_type == "artifact":
        kind = _artifact_kind(entity)
        return None if kind in FOLDED_ARTIFACT_KINDS else f"kind:{kind}"
    return entity.entity_id


def _lines(*parts: Any) -> tuple[str, ...]:
    # The text vcp adds stays ASCII, so the usual label prints cleanly on any code page; a name a
    # user gave keeps its own characters and the CLI escapes what a console cannot show.
    out = []
    for part in parts:
        text = " ".join(str(part).split()) if part is not None else ""
        if text:
            out.append(text if len(text) <= _LINE_LIMIT else text[: _LINE_LIMIT - 3] + "...")
    return tuple(out)


def _label(entity: ProvenanceEntity) -> tuple[str, ...]:
    attrs, key, kind = entity.attributes, entity.key, entity.entity_type
    if kind == "dataset":
        name, _, digest = key.partition("@")
        short = str(attrs.get("samples_hash") or digest)[:8]
        count = attrs.get("sample_count")
        samples = f"{count:,} samples" if isinstance(count, int) else ""
        detail = " - ".join(part for part in (f"@{short}" if short else "", samples) if part)
        return _lines(attrs.get("dataset") or name, detail)
    if kind == "split":
        roles = attrs.get("roles")
        subsets = " / ".join(roles) if isinstance(roles, dict) else ""
        return _lines(attrs.get("plan_id") or key, subsets)
    if kind == "run":
        grade = attrs.get("provenance_grade")
        return _lines(key, f"grade {grade}" if grade else "")
    if kind == "fusion_run":
        recipe = attrs.get("recipe_id")
        return _lines(key, f"fusion {recipe}" if recipe else "fusion")
    if kind == "judgement":
        return _lines(attrs.get("prereg_id") or key, attrs.get("verdict"))
    if kind == "submission":
        return _lines(key.split("/", 1)[-1], attrs.get("kind"))  # framed by its test set
    if kind == "reading":
        return _lines(f"{attrs.get('run_id')} / {attrs.get('subset')}", attrs.get("metric"))
    if kind == "materialized_cache":
        return _lines(f"cache {attrs.get('mode')}", f"{attrs.get('rows')} rows")
    if kind == "backup":
        return _lines(f"backup {key}", attrs.get("conclusion"))
    if kind == "artifact":
        return _lines(key)
    return _lines(kind, key)


def _group(graph: ProvenanceGraph, entity: ProvenanceEntity) -> str | None:
    name = entity.attributes.get("dataset")
    if isinstance(name, str) and name:
        return name
    version = graph.entities.get(entity.dataset_version_id or "")
    if version is not None and version.entity_id != entity.entity_id:
        return _group(graph, version)
    if entity.entity_type == "dataset":
        return entity.key.split("@", 1)[0]
    if entity.entity_type in {"judgement", "backup"}:  # keyed "<dataset>/<id>..."
        return entity.key.split("/", 1)[0]
    return None


def _worst(records: list[StatusRecord]) -> StatusRecord:
    return max(records, key=lambda record: _PRIORITY[record.status])  # first wins a tie


def _node(
    graph: ProvenanceGraph,
    key: str,
    members: list[str],
    records: Mapping[str, StatusRecord],
    backed: set[str],
) -> ViewNode:
    ordered = sorted(members)
    worst = _worst([records[ident] for ident in ordered])
    if key.startswith("kind:"):
        tally = [
            f"{sum(records[ident].status == status for ident in ordered)} {status.value}"
            for status in reversed(_STATUS_ORDER[1:])
            if any(records[ident].status == status for ident in ordered)
        ]
        return ViewNode(
            node_id=node_id(key),
            kind=ARTIFACT_KIND,
            label=_lines(f"{key.removeprefix('kind:')} x{len(ordered)}", " - ".join(tally)),
            group=None,
            status=worst.status,
            reason=worst.reason,
            members=tuple(ordered),
            backed_up=all(ident in backed for ident in ordered),
        )
    entity = graph.entities[key]
    return ViewNode(
        node_id=node_id(key),
        kind=entity.entity_type,
        label=_label(entity),
        group=_group(graph, entity),
        status=worst.status,
        reason=worst.reason,
        members=(key,),
        backed_up=key in backed,
    )


def _walk(
    graph: ProvenanceGraph,
    first: ProvenanceEdge,
    selected: set[str],
    representative: Mapping[str, str | None],
) -> Iterator[tuple[str, list[ProvenanceEdge]]]:
    """Follow one edge through folded entities to every drawn node it reaches."""
    stack: list[list[ProvenanceEdge]] = [[first]]
    seen: set[str] = set()
    while stack:
        path = stack.pop()
        target = path[-1].target_id
        key = representative.get(target)
        if key is not None:
            yield key, path
            continue
        if target in seen:
            continue
        seen.add(target)
        stack.extend([*path, edge] for edge in graph.outgoing(target) if edge.target_id in selected)


def _edge_label(parts: _EdgeParts, source_kind: str, target_kind: str) -> str:
    if parts.diffs:
        return "; ".join(sorted(parts.diffs))
    if parts.subsets:
        return ", ".join(sorted(parts.subsets))
    if len(parts.pairs) > 1 and ARTIFACT_KIND in (source_kind, target_kind):
        return f"x{len(parts.pairs)}"
    return ""


def _edges(
    graph: ProvenanceGraph,
    selected: set[str],
    representative: Mapping[str, str | None],
    kinds: Mapping[str, str],
) -> list[ViewEdge]:
    merged: dict[tuple[str, str], _EdgeParts] = {}
    for source in sorted(selected):
        source_key = representative.get(source)
        if source_key is None:
            continue
        for first in graph.outgoing(source):
            if first.target_id not in selected:
                continue
            for target_key, path in _walk(graph, first, selected, representative):
                if target_key == source_key:
                    continue
                parts = merged.setdefault((node_id(source_key), node_id(target_key)), _EdgeParts())
                parts.pairs.add((source, path[-1].target_id))
                parts.types.update(edge.edge_type for edge in path)
                parts.subsets.update(
                    str(edge.attributes["subset"]) for edge in path if "subset" in edge.attributes
                )
                if len(path) == 1 and first.edge_type == "DERIVED_FROM":
                    attrs = first.attributes
                    parts.diffs.append(
                        f"diff {attrs.get('artifact')}: {attrs.get('total_changes')} changes"
                    )
    return [
        ViewEdge(
            source=source,
            target=target,
            label=_edge_label(parts, kinds[source], kinds[target]),
            edge_types=tuple(sorted(parts.types)),
            count=len(parts.pairs),
            thick=bool(parts.diffs),
        )
        for (source, target), parts in sorted(merged.items())
    ]


def _drop_split_shortcuts(edges: list[ViewEdge], kinds: Mapping[str, str]) -> list[ViewEdge]:
    """Overview only: a dataset -> run arrow says nothing once dataset -> split -> run is drawn."""
    pairs = {(edge.source, edge.target) for edge in edges}
    splits_into: dict[str, set[str]] = {}
    for source, target in pairs:
        if kinds[source] == "split":
            splits_into.setdefault(target, set()).add(source)
    return [
        edge
        for edge in edges
        if not (
            kinds[edge.source] == "dataset"
            and not edge.label
            and any((edge.source, split) in pairs for split in splits_into.get(edge.target, ()))
        )
    ]


def _sort_key(node: ViewNode) -> tuple[Any, ...]:
    return (node.group is None, node.group or "", _KIND_ORDER.get(node.kind, 99), node.node_id)


def build_view(
    graph: ProvenanceGraph,
    statuses: Mapping[str, StatusRecord],
    *,
    dataset: str | None = None,
    entity: str | None = None,
    detail: str = "overview",
) -> GraphView:
    validate_detail(detail)
    scope, selected = scope_entities(graph, dataset=dataset, entity=entity)
    records = {ident: _status_of(graph, statuses, ident) for ident in selected}
    representative = {
        ident: _representative(graph.entities[ident], detail) for ident in sorted(selected)
    }
    members: dict[str, list[str]] = {}
    for ident, key in representative.items():
        if key is not None:
            members.setdefault(key, []).append(ident)
    backed = {edge.source_id for edge in graph.edges.values() if edge.edge_type == "BACKED_UP_AS"}
    nodes = sorted(
        (_node(graph, key, idents, records, backed) for key, idents in members.items()),
        key=_sort_key,
    )
    kinds = {node.node_id: node.kind for node in nodes}
    edges = _edges(graph, selected, representative, kinds)
    if detail == "overview":
        edges = _drop_split_shortcuts(edges, kinds)
    drawn = {ident for node in nodes if node.kind != ARTIFACT_KIND for ident in node.members}
    grouped = {ident for node in nodes if node.kind == ARTIFACT_KIND for ident in node.members}
    hidden = tuple(
        records[ident]
        for ident in sorted(selected - drawn - grouped)
        if records[ident].status != EntityStatus.VALID
    )
    return GraphView(
        scope=scope,
        detail=detail,
        nodes=tuple(nodes),
        edges=tuple(edges),
        entities=len(selected),
        folded=len(selected) - len(drawn),
        counts={
            status.value: sum(record.status == status for record in records.values())
            for status in _STATUS_ORDER
        },
        hidden_alerts=hidden,
    )


def view_payload(view: GraphView) -> dict[str, Any]:
    """The ``--json`` form of a view: enough for an agent to draw it some other way."""
    return {
        "scope": view.scope,
        "detail": view.detail,
        "entities": view.entities,
        "folded": view.folded,
        "counts": dict(view.counts),
        "nodes": [
            {
                "id": node.node_id,
                "kind": node.kind,
                "label": list(node.label),
                "group": node.group,
                "status": node.status.value,
                "reason": node.reason,
                "members": list(node.members),
                "backed_up": node.backed_up,
            }
            for node in view.nodes
        ],
        "edges": [
            {
                "source": edge.source,
                "target": edge.target,
                "label": edge.label,
                "edge_types": list(edge.edge_types),
                "count": edge.count,
                "thick": edge.thick,
            }
            for edge in view.edges
        ],
        "hidden_alerts": [record.model_dump(mode="json") for record in view.hidden_alerts],
    }
