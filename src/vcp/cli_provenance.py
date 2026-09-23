"""CLI for the disposable dataset-evolution provenance index."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer

from vcp.cli_common import CmdResult, ConfigsRootOpt, DataRootOpt, JsonOpt, run_command
from vcp.core.build import build_string
from vcp.core.errors import ValidationFailed
from vcp.core.log import FieldValue, Status, format_value
from vcp.core.paths import resolve_configs_root, resolve_data_root
from vcp.core.time import stamp
from vcp.provenance.backend import (
    BackendConfig,
    BackendName,
    MaintenanceResult,
    ProvenanceBackend,
    ProvenanceReader,
    RequestedStrategy,
    make_backend,
    parse_backend,
)
from vcp.provenance.graph import ProvenanceGraph
from vcp.provenance.index import dataset_heads, graph_hash
from vcp.provenance.render import (
    GraphView,
    build_view,
    fallback_statuses,
    validate_detail,
    view_payload,
    worst_statuses,
)
from vcp.provenance.render_mermaid import (
    DocumentMeta,
    prepare_output,
    render_document,
    write_document,
)
from vcp.provenance.schema import EntityStatus, StatusRecord
from vcp.provenance.views import explain, impact

provenance_app = typer.Typer(no_args_is_help=True, help="derived impact provenance index")

BackendOpt = Annotated[
    str, typer.Option("--backend", help="provenance backend: sqlite or postgresql")
]
PgServiceOpt = Annotated[
    str | None, typer.Option("--pg-service", help="libpq service name for PostgreSQL")
]
StrategyOpt = Annotated[
    str, typer.Option("--strategy", help="maintenance strategy: incremental, full, or auto")
]
PolicyOpt = Annotated[
    str | None, typer.Option("--policy", help="immutable provenance policy artifact id")
]


def _backend_name(value: BackendName | str) -> BackendName:
    try:
        return parse_backend(value)
    except ValidationFailed:
        raise ValidationFailed("unsupported_backend") from None


def _backend(
    data_root: Path | None,
    backend: BackendName | str,
    pg_service: str | None,
) -> tuple[Path, ProvenanceBackend]:
    root = resolve_data_root(data_root)
    name = _backend_name(backend)
    if pg_service is not None and name is not BackendName.POSTGRESQL:
        raise ValidationFailed("pg_service_requires_postgresql")
    return root, make_backend(BackendConfig(name=name, pg_service=pg_service), root)


def _strategy(value: str) -> RequestedStrategy:
    try:
        return RequestedStrategy(value.lower())
    except (AttributeError, ValueError):
        raise ValidationFailed("unsupported_strategy") from None


def _payload(backend: ProvenanceBackend, payload: dict[str, Any]) -> dict[str, Any]:
    return {"backend": backend.name.value, **payload}


def _maintenance_fields(result: MaintenanceResult) -> dict[str, FieldValue]:
    values = asdict(result)
    return {key: "none" if value is None else value for key, value in values.items()}


def _maintenance_human(result: MaintenanceResult) -> list[str]:
    return [f"{key}={format_value(value)}" for key, value in _maintenance_fields(result).items()]


def _dataset_id(graph: ProvenanceGraph, value: str) -> str:
    exact = graph.entities.get(value)
    if exact is not None and exact.entity_type == "dataset":
        return value
    matches = sorted(
        ident
        for ident, entity in graph.entities.items()
        if entity.entity_type == "dataset" and entity.attributes.get("dataset") == value
    )
    if not matches:
        raise ValidationFailed(f"not_found: dataset version {value!r} in provenance index")
    if len(matches) > 1:
        raise ValidationFailed(
            f"ambiguous: dataset {value!r} has {len(matches)} indexed versions; pass the full "
            "dataset:<name>@<samples_hash> entity id"
        )
    return matches[0]


def _head_for(graph: ProvenanceGraph, source_id: str) -> str | None:
    reachable = sorted(set(dataset_heads(graph)) & set(graph.descendants(source_id)))
    if len(reachable) > 1:
        raise ValidationFailed(
            f"ambiguous: dataset evolution from {source_id!r} has heads {reachable}"
        )
    return reachable[0] if reachable else None


@provenance_app.command("rebuild")
def rebuild_cmd(
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
    backend: BackendOpt = "sqlite",
    pg_service: PgServiceOpt = None,
) -> None:
    """Atomically rebuild the disposable index from canonical VCP records."""

    def fn() -> CmdResult:
        root, index = _backend(data_root, backend, pg_service)
        result = index.rebuild(root, resolve_configs_root(configs_root))
        fields: dict[str, FieldValue] = {
            "backend": index.name.value,
            "entities": result.entities,
            "edges": result.edges,
            "changes": result.changes,
            "heads": result.heads,
            "hash": result.graph_hash[:12],
        }
        human = [f"rebuilt {index.location_label}", f"graph={result.graph_hash}"]
        return "OK", fields, _payload(index, result.__dict__), human

    run_command("provenance.rebuild", json_mode, data_root, fn)


@provenance_app.command("ingest")
def ingest_cmd(
    artifact_id: Annotated[str, typer.Option("--artifact", help="dataset_diff artifact id")],
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
    backend: BackendOpt = "sqlite",
    pg_service: PgServiceOpt = None,
    strategy: StrategyOpt = "incremental",
    policy: PolicyOpt = None,
) -> None:
    """Transactionally ingest one verified dataset diff; duplicate ingest is a no-op."""

    def fn() -> CmdResult:
        requested = _strategy(strategy)
        backend_name = _backend_name(backend)
        if policy is not None and (
            backend_name is not BackendName.POSTGRESQL or requested is not RequestedStrategy.AUTO
        ):
            raise ValidationFailed("policy_requires_postgresql_auto")
        root, index = _backend(data_root, backend_name, pg_service)
        result = index.ingest_diff(
            artifact_id,
            root,
            resolve_configs_root(configs_root),
            requested_strategy=requested,
            policy_id=policy,
        )
        maintenance = _maintenance_fields(result)
        fields: dict[str, FieldValue] = {
            "artifact": artifact_id,
            "inserted": result.inserted,
            "dirty": result.dirty_entities,
            "hash": result.graph_hash[:12],
            **maintenance,
        }
        word = "ingested" if result.inserted else "already ingested"
        status: Status = "WARN" if requested is RequestedStrategy.AUTO and policy is None else "OK"
        human = [f"{word}: {artifact_id}", *_maintenance_human(result)]
        return status, fields, asdict(result), human

    run_command(
        "provenance.ingest",
        json_mode,
        data_root,
        fn,
        context={"artifact": artifact_id},
    )


@provenance_app.command("sync")
def sync_cmd(
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
    backend: BackendOpt = "sqlite",
    pg_service: PgServiceOpt = None,
) -> None:
    """Append new canonical records; reject mutation/deletion and suggest rebuild."""

    def fn() -> CmdResult:
        root, index = _backend(data_root, backend, pg_service)
        result = index.sync(root, resolve_configs_root(configs_root))
        fields: dict[str, FieldValue] = {
            "backend": index.name.value,
            "entities": result.entities,
            "edges": result.edges,
            "changes": result.changes,
            "hash": result.graph_hash[:12],
        }
        return "OK", fields, _payload(index, result.__dict__), ["canonical records synchronized"]

    run_command("provenance.sync", json_mode, data_root, fn)


@provenance_app.command("impact")
def impact_cmd(
    dataset: Annotated[str, typer.Option("--dataset", help="dataset name or version entity id")],
    sample: Annotated[
        str | None, typer.Option("--sample", help="optional changed sample id")
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
    backend: BackendOpt = "sqlite",
    pg_service: PgServiceOpt = None,
) -> None:
    """List the downstream closure of a dataset version or one changed sample."""

    def fn() -> CmdResult:
        _root, index = _backend(data_root, backend, pg_service)
        graph = index.load_graph()
        source = _dataset_id(graph, dataset)
        head = _head_for(graph, source)
        result = impact(graph, source, sample, head)
        fields: dict[str, FieldValue] = {
            "backend": index.name.value,
            "dataset": dataset,
            "entities": len(result.entity_ids),
        }
        if sample is not None:
            fields["sample"] = sample
        if head is not None:
            fields["head"] = head
        human = [source]
        for ident in result.entity_ids:
            status = result.statuses.get(ident)
            suffix = f"  {status.status.value}  {status.reason}" if status else ""
            human.append(f"- {ident}{suffix}")
        return "OK", fields, _payload(index, result.model_dump(mode="json")), human

    context: dict[str, FieldValue] = {"dataset": dataset}
    if sample is not None:
        context["sample"] = sample
    run_command("provenance.impact", json_mode, data_root, fn, context=context)


@provenance_app.command("stale")
def stale_cmd(
    head: Annotated[str, typer.Option("--head", help="head dataset name or entity id")],
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
    backend: BackendOpt = "sqlite",
    pg_service: PgServiceOpt = None,
) -> None:
    """Show run validity relative to a dataset-version head."""

    def fn() -> CmdResult:
        _root, index = _backend(data_root, backend, pg_service)
        with index.read_snapshot() as reader:
            graph = reader.load_graph()
            head_id = _dataset_id(graph, head)
            if head_id not in dataset_heads(graph):
                raise ValidationFailed(
                    f"not_a_head: {head_id}; choose one of {dataset_heads(graph)}"
                )
            statuses = reader.statuses(head_id)
        rows = [
            statuses[ident]
            for ident, entity in sorted(graph.entities.items())
            if entity.entity_type in {"run", "fusion_run"} and ident in statuses
        ]
        counts = {
            status.value: sum(row.status == status for row in rows) for status in EntityStatus
        }
        fields: dict[str, FieldValue] = {
            "backend": index.name.value,
            "head": head_id,
            "runs": len(rows),
            "stale": counts["STALE"],
            "review": counts["REVIEW"],
            "broken": counts["BROKEN"],
        }
        human = [f"{row.entity_id}  {row.status.value}  {row.reason}" for row in rows]
        payload: dict[str, Any] = {
            "backend": index.name.value,
            "head_id": head_id,
            "counts": counts,
            "runs": [row.model_dump(mode="json") for row in rows],
        }
        status: Status = "WARN" if counts["REVIEW"] or counts["BROKEN"] else "OK"
        return status, fields, payload, human

    run_command("provenance.stale", json_mode, data_root, fn, context={"head": head})


@provenance_app.command("explain")
def explain_cmd(
    entity: Annotated[str, typer.Option("--entity", help="entity id, for example run:model-a")],
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
    backend: BackendOpt = "sqlite",
    pg_service: PgServiceOpt = None,
) -> None:
    """Trace canonical predecessor evidence for one entity."""

    def fn() -> CmdResult:
        _root, index = _backend(data_root, backend, pg_service)
        result = explain(index.load_graph(), entity)
        fields: dict[str, FieldValue] = {
            "backend": index.name.value,
            "entity": entity,
            "ancestors": len(result.ancestor_ids),
            "edges": len(result.edges),
        }
        human = [entity, *(f"<- {item}" for item in result.ancestor_ids)]
        return "OK", fields, _payload(index, result.model_dump(mode="json")), human

    run_command("provenance.explain", json_mode, data_root, fn, context={"entity": entity})


_GRAPH_ROWS = 10
_REASON_WIDTH = 120


def _graph_statuses(
    reader: ProvenanceReader, graph: ProvenanceGraph, head: str | None
) -> tuple[str | None, dict[str, StatusRecord]]:
    """Statuses relative to ``head``, or the worst over every current head when none is given;
    with no dataset at all, broken evidence alone (still spread to its dependents)."""
    heads = dataset_heads(graph)
    if head is None:
        if not heads:
            return None, fallback_statuses(graph)
        return None, worst_statuses(reader.statuses(item) for item in heads)
    head_id = _dataset_id(graph, head)
    if head_id not in heads:
        raise ValidationFailed(f"not_a_head: {head_id}; choose one of {heads}")
    return head_id, reader.statuses(head_id)


def _graph_human(view: GraphView, oversize: bool) -> list[str]:
    lines = [
        f"{len(view.nodes)} nodes, {len(view.edges)} edges "
        f"({view.folded} of {view.entities} entities not drawn on their own)"
    ]
    rows = [
        f"{node.status.value}  {node.members[0] if len(node.members) == 1 else node.label[0]}  "
        f"{node.reason[:_REASON_WIDTH]}"
        for node in view.nodes
        if node.status != EntityStatus.VALID
    ]
    rows += [
        f"{record.status.value}  {record.entity_id} (not drawn)  {record.reason[:_REASON_WIDTH]}"
        for record in view.hidden_alerts
    ]
    lines += rows[:_GRAPH_ROWS]
    if len(rows) > _GRAPH_ROWS:
        lines.append(f"... {len(rows) - _GRAPH_ROWS} more in --json")
    if oversize:
        lines.append(
            "the diagram is past Mermaid's default limits (500 edges / 50,000 characters): "
            "write .html, or narrow it with --dataset / --entity"
        )
    return lines


@provenance_app.command("graph")
def graph_cmd(
    out: Annotated[
        Path, typer.Option("--out", help="file to write: .html, .md or .mmd, outside the data root")
    ],
    dataset: Annotated[
        str | None,
        typer.Option("--dataset", help="only this dataset's versions and what they connect to"),
    ] = None,
    entity: Annotated[
        str | None,
        typer.Option("--entity", help="only this entity's ancestors and descendants (run:X)"),
    ] = None,
    head: Annotated[
        str | None,
        typer.Option("--head", help="statuses relative to this head; default: worst of all heads"),
    ] = None,
    detail: Annotated[
        str, typer.Option("--detail", help="overview (folds readings, receipts...) or full")
    ] = "overview",
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
    backend: BackendOpt = "sqlite",
    pg_service: PgServiceOpt = None,
) -> None:
    """Draw the indexed provenance graph as Mermaid; reads the index, writes only --out."""

    def fn() -> CmdResult:
        validate_detail(detail)
        root, index = _backend(data_root, backend, pg_service)
        target, fmt = prepare_output(out, root)
        with index.read_snapshot() as reader:
            graph = reader.load_graph()
            head_id, statuses = _graph_statuses(reader, graph, head)
        view = build_view(graph, statuses, dataset=dataset, entity=entity, detail=detail)
        digest = graph_hash(graph)[:12]
        meta = DocumentMeta(
            build=build_string(),
            backend=index.name.value,
            index_hash=digest,
            head=head_id,
            generated=stamp(),
        )
        document = render_document(view, fmt, meta)
        write_document(target, document.text)
        counts = view.counts
        fields: dict[str, FieldValue] = {
            "backend": index.name.value,
            "out": str(target),
            "format": fmt,
            "detail": view.detail,
            "scope": view.scope,
            "nodes": len(view.nodes),
            "edges": len(view.edges),
            "folded": view.folded,
            "broken": counts["BROKEN"],
            "stale": counts["STALE"],
            "review": counts["REVIEW"],
            "hash": digest,
        }
        if head_id is not None:
            fields["head"] = head_id
        if document.oversize:
            fields["oversize"] = True
        payload = {
            "backend": index.name.value,
            "hash": digest,
            "format": fmt,
            "out": str(target),
            "head": head_id,
            "oversize": document.oversize,
            **view_payload(view),
        }
        warn = counts["BROKEN"] or counts["REVIEW"] or document.oversize
        status: Status = "WARN" if warn else "OK"
        human = [f"wrote {target}", *_graph_human(view, document.oversize)]
        return status, fields, payload, human

    run_command("provenance.graph", json_mode, data_root, fn, context={"out": str(out)})


@provenance_app.command("status")
def status_cmd(
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
    backend: BackendOpt = "sqlite",
    pg_service: PgServiceOpt = None,
) -> None:
    """Summarize index size and synchronization with canonical records."""

    def fn() -> CmdResult:
        root, index = _backend(data_root, backend, pg_service)
        with index.read_snapshot() as reader:
            stats = reader.stats()
            verified = reader.verify(root, resolve_configs_root(configs_root))
        fields: dict[str, FieldValue] = {
            "backend": index.name.value,
            "entities": stats["entities"],
            "edges": stats["provenance_edges"],
            "changes": stats["sample_changes"],
            "synchronized": verified.ok,
            "issues": len(verified.issues),
        }
        payload = {
            "backend": index.name.value,
            **stats,
            "synchronized": verified.ok,
            "issues": verified.issues,
        }
        status: Status = "OK" if verified.ok else "WARN"
        human = [
            f"index={index.location_label}",
            f"entities={stats['entities']} edges={stats['provenance_edges']} "
            f"changes={stats['sample_changes']}",
            *(f"issue: {issue}" for issue in verified.issues),
        ]
        return status, fields, payload, human

    run_command("provenance.status", json_mode, data_root, fn)


@provenance_app.command("verify-index")
def verify_index_cmd(
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
    backend: BackendOpt = "sqlite",
    pg_service: PgServiceOpt = None,
) -> None:
    """Recompute canonical graph and statuses and compare them exactly to the index."""

    def fn() -> CmdResult:
        root, index = _backend(data_root, backend, pg_service)
        result = index.verify(root, resolve_configs_root(configs_root))
        fields: dict[str, FieldValue] = {
            "backend": index.name.value,
            "ok": result.ok,
            "issues": len(result.issues),
            "hash": result.graph_hash[:12],
        }
        status: Status = "OK" if result.ok else "FAIL"
        human = ["index matches canonical replay"] if result.ok else result.issues
        return status, fields, _payload(index, result.__dict__), human

    run_command("provenance.verify-index", json_mode, data_root, fn)
