"""Production-schema/API incremental-ingest benchmark (explicit, never normal CI).

This complements ``benchmark.py``'s algorithm microbenchmark. It seeds the real SQLite
schema with a deterministic historical ``SampleChange`` population, publishes a real verified
``dataset_diff`` artifact, and times ``ProvenanceIndex.ingest_diff`` against the production full
graph load plus fingerprint path.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from time import perf_counter_ns

from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.core.hashing import sha256_file, sha256_text
from vcp.core.time import stamp
from vcp.provenance.diff import CHANGES_FILE, KIND, SUMMARY_FILE
from vcp.provenance.graph import ProvenanceGraph, dataset_version_id, entity_id, split_plan_id
from vcp.provenance.index import (
    ProvenanceIndex,
    _connection,
    _fingerprint_hash,
    _graph_records,
    _insert_change,
    _insert_entity,
    _json,
    _load_graph,
    _schema,
    graph_hash,
)
from vcp.provenance.schema import (
    ChangeDomain,
    ChangeType,
    DatasetDiffSummary,
    EntityStatus,
    ProvenanceEntity,
    SampleChange,
    SemanticEffect,
    make_change_id,
)
from vcp.provenance.views import compute_statuses

SEED = 20260913
SCALES = (1_000, 10_000, 100_000, 1_000_000)


def _change(
    index: int,
    before: str,
    after: str,
    *,
    prefix: str,
    effect: SemanticEffect = SemanticEffect.DISPLAY_ONLY,
) -> SampleChange:
    sample_id = f"{prefix}-{index:08d}"
    row_before = sha256_text(f"before:{sample_id}")
    row_after = sha256_text(f"after:{sample_id}")
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
        changed_domains=[
            ChangeDomain.LABEL_SOURCE
            if effect == SemanticEffect.TRAINING_AFFECTING
            else ChangeDomain.META
        ],
        changed_fields=[
            "label_source" if effect == SemanticEffect.TRAINING_AFFECTING else "meta.display"
        ],
        semantic_effects=[effect],
        before_row_hash=row_before,
        after_row_hash=row_after,
    )


def _record_value(record: dict[str, object]) -> int:
    return int(sha256_text(_json(record)), 16)


def _seed_index(path: Path, count: int, source_hash: str, target_hash: str) -> None:
    connection = _connection(path)
    ancestor = dataset_version_id("synthetic-ancestor", "c" * 64)
    source = dataset_version_id("synthetic-source", source_hash)
    target = dataset_version_id("synthetic-target", target_hash)
    graph = ProvenanceGraph()
    for ident, name, digest in (
        (ancestor, "synthetic-ancestor", "c" * 64),
        (source, "synthetic-source", source_hash),
        (target, "synthetic-target", target_hash),
    ):
        graph.add_entity(
            ProvenanceEntity(
                entity_id=ident,
                entity_type="dataset",
                key=ident.removeprefix("dataset:"),
                dataset_version_id=ident,
                attributes={"dataset": name, "samples_hash": digest},
            )
        )
    split = split_plan_id("synthetic-source", "fixed", source_hash)
    graph.add_entity(
        ProvenanceEntity(
            entity_id=split,
            entity_type="split",
            key=split.removeprefix("split:"),
            dataset_version_id=source,
            attributes={
                "plan_id": "fixed",
                "assignment": {f"delta-{index:08d}": "train" for index in range(100)},
                "roles": {"train": "train", "val": "eval"},
            },
        )
    )
    graph.add_edge(ancestor, source, "DERIVED_FROM", {"events": count})
    graph.add_edge(source, split, "USES_SPLIT")
    run_count = max(15, round(count * 15 / 4407))
    for index in range(run_count):
        run = entity_id("run", f"r-{index:06d}")
        reading = entity_id("reading", f"r-{index:06d}")
        graph.add_entity(
            ProvenanceEntity(
                entity_id=run,
                entity_type="run",
                key=run.removeprefix("run:"),
                dataset_version_id=source,
                attributes={
                    "plan_id": "fixed",
                    "trained_on": ["train"],
                    "predicted_subsets": ["val"],
                },
            )
        )
        graph.add_entity(
            ProvenanceEntity(
                entity_id=reading,
                entity_type="reading",
                key=reading.removeprefix("reading:"),
                dataset_version_id=source,
                attributes={"run_id": f"r-{index:06d}", "plan_id": "fixed", "subset": "val"},
            )
        )
        graph.add_edge(split, run, "USES_SPLIT")
        graph.add_edge(run, reading, "EVALUATED_BY")
        if index % 10 == 0:
            artifact = entity_id("artifact", f"derived/a-{index:06d}")
            graph.add_entity(
                ProvenanceEntity(
                    entity_id=artifact,
                    entity_type="artifact",
                    key=artifact.removeprefix("artifact:"),
                    dataset_version_id=source,
                )
            )
            graph.add_edge(run, artifact, "PRODUCED_BY")
        if index % 25 == 0:
            fusion = entity_id("run", f"fusion-{index:06d}")
            submission = entity_id("submission", f"s-{index:06d}")
            graph.add_entity(
                ProvenanceEntity(
                    entity_id=fusion,
                    entity_type="fusion_run",
                    key=fusion.removeprefix("run:"),
                    dataset_version_id=source,
                    attributes={
                        "plan_id": "fixed",
                        "trained_on": ["train"],
                        "predicted_subsets": ["val"],
                    },
                )
            )
            graph.add_entity(
                ProvenanceEntity(
                    entity_id=submission,
                    entity_type="submission",
                    key=submission.removeprefix("submission:"),
                )
            )
            graph.add_edge(run, fusion, "COMBINED_INTO")
            graph.add_edge(fusion, submission, "SUBMITTED_AS")
    record_count = 0
    record_sum = 0
    history_ids: list[str] = []
    with connection:
        _schema(connection)
        for entity in graph.entities.values():
            _insert_entity(connection, entity)
        for edge in graph.edges.values():
            connection.execute(
                "INSERT INTO provenance_edges VALUES(?,?,?,?,?)",
                (
                    edge.edge_id,
                    edge.source_id,
                    edge.target_id,
                    edge.edge_type,
                    _json(edge.attributes),
                ),
            )
        for record in _graph_records(graph):
            record_count += 1
            record_sum = (record_sum + _record_value(record)) % (1 << 256)
        for start in range(0, count, 10_000):
            changes = [
                _change(index, "c" * 64, "d" * 64, prefix="history")
                for index in range(start, min(count, start + 10_000))
            ]
            for change in changes:
                _insert_change(connection, change)
                history_ids.append(change.change_id)
                record_count += 1
                record_sum = (
                    record_sum
                    + _record_value({"kind": "change", "value": change.model_dump(mode="json")})
                ) % (1 << 256)
        connection.execute(
            "INSERT INTO dataset_edges VALUES(?,?,?)",
            (ancestor, source, _json(sorted(history_ids))),
        )
        transition = {
            "kind": "transition",
            "value": {"from": ancestor, "to": source, "changes": sorted(history_ids)},
        }
        record_count += 1
        record_sum = (record_sum + _record_value(transition)) % (1 << 256)
        for head in (ancestor, source, target):
            for ident in graph.entities:
                connection.execute(
                    "INSERT INTO entity_status VALUES(?,?,?,?,?)",
                    (head, ident, EntityStatus.VALID.value, "canonical evidence is current", None),
                )
        digest = _fingerprint_hash(record_count, record_sum)
        connection.execute(
            "INSERT OR REPLACE INTO metadata VALUES('graph_record_count',?)",
            (str(record_count),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata VALUES('graph_record_sum',?)",
            (f"{record_sum:064x}",),
        )
        connection.execute("INSERT OR REPLACE INTO metadata VALUES('graph_hash',?)", (digest,))
    connection.close()


def _publish_delta(data_root: Path, source: Path, target: Path, count: int = 100) -> str:
    source_hash = sha256_file(source)
    target_hash = sha256_file(target)
    changes = [
        _change(
            index,
            source_hash,
            target_hash,
            prefix="delta",
            effect=SemanticEffect.TRAINING_AFFECTING,
        )
        for index in range(count)
    ]
    artifact_id = "production-delta"
    summary = DatasetDiffSummary(
        artifact_id=artifact_id,
        from_dataset="synthetic-source",
        from_samples_hash=source_hash,
        to_dataset="synthetic-target",
        to_samples_hash=target_hash,
        grade="fallback",
        policies=["builtin@1"],
        total_changes=count,
        counts={ChangeType.MODIFIED.value: count},
        domain_counts={ChangeDomain.LABEL_SOURCE.value: count},
        effect_counts={SemanticEffect.TRAINING_AFFECTING.value: count},
    )
    spec = ArtifactSpec(
        kind=KIND,
        id=artifact_id,
        dataset="synthetic-target",
        params={
            "from_dataset": "synthetic-source",
            "from_samples_hash": source_hash,
            "to_dataset": "synthetic-target",
            "to_samples_hash": target_hash,
            "policies": "builtin@1",
        },
        inputs=[
            InputRef(name="from_samples", path=str(source), sha256=source_hash),
            InputRef(name="to_samples", path=str(target), sha256=target_hash),
        ],
    )
    with ArtifactWriter.create(spec, data_root=data_root) as writer:
        writer.write_text(
            CHANGES_FILE, "".join(change.model_dump_json() + "\n" for change in changes)
        )
        writer.write_json(SUMMARY_FILE, summary.model_dump(mode="json"))
        writer.commit()
    return artifact_id


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


def _commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or "unknown"


def run_scale(root: Path, count: int) -> dict[str, object]:
    scale = root / str(count)
    data_root = scale / "data"
    configs_root = scale / "configs"
    source = data_root / "datasets" / "synthetic-source" / "samples.jsonl"
    target = data_root / "datasets" / "synthetic-target" / "samples.jsonl"
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    configs_root.mkdir(parents=True)
    source.write_text('{"version":"source"}\n', encoding="utf-8", newline="\n")
    target.write_text('{"version":"target"}\n', encoding="utf-8", newline="\n")
    artifact_id = _publish_delta(data_root, source, target)
    baseline = scale / "baseline.sqlite3"
    started = perf_counter_ns()
    _seed_index(baseline, count, sha256_file(source), sha256_file(target))
    generation_ms = (perf_counter_ns() - started) / 1_000_000
    sizing = sqlite3.connect(baseline)
    canonical_metadata_bytes = sum(
        sizing.execute(statement).fetchone()[0]
        for statement in (
            "SELECT coalesce(sum(length(event_json)),0) FROM sample_changes",
            "SELECT coalesce(sum(length(change_ids_json)),0) FROM dataset_edges",
            "SELECT coalesce(sum(length(attributes_json)),0) FROM entities",
            "SELECT coalesce(sum(length(attributes_json)),0) FROM provenance_edges",
        )
    )
    fixture_counts = {
        table: sizing.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("entities", "provenance_edges", "dataset_edges")
    }
    sizing.close()

    repetitions = 7 if count <= 10_000 else 3
    full_ms: list[float] = []
    for _ in range(repetitions):
        connection = sqlite3.connect(baseline)
        connection.row_factory = sqlite3.Row
        started = perf_counter_ns()
        graph_hash(_load_graph(connection))
        full_ms.append((perf_counter_ns() - started) / 1_000_000)
        connection.close()

    ingest_ms: list[float] = []
    query_ms: list[float] = []
    exact_parity: bool | None = None
    status_counts: dict[str, int] | None = None
    target_id = dataset_version_id("synthetic-target", sha256_file(target))
    for repetition in range(repetitions):
        candidate = scale / f"candidate-{repetition}.sqlite3"
        shutil.copy2(baseline, candidate)
        index = ProvenanceIndex(candidate)
        started = perf_counter_ns()
        index.ingest_diff(artifact_id, data_root, configs_root)
        ingest_ms.append((perf_counter_ns() - started) / 1_000_000)
        started = perf_counter_ns()
        statuses = index.statuses(target_id)
        query_ms.append((perf_counter_ns() - started) / 1_000_000)
        if exact_parity is None:
            exact_parity = statuses == compute_statuses(index.load_graph(), target_id)
            status_counts = dict(
                sorted(Counter(status.status.value for status in statuses.values()).items())
            )
        candidate.unlink()
        candidate.with_name(candidate.name + "-wal").unlink(missing_ok=True)
        candidate.with_name(candidate.name + "-shm").unlink(missing_ok=True)
    combined_p95 = _p95([left + right for left, right in zip(ingest_ms, query_ms, strict=True)])
    full_p95 = _p95(full_ms)
    return {
        "events": count,
        "delta_events": 100,
        "repetitions": repetitions,
        "generation_ms": generation_ms,
        "database_bytes": baseline.stat().st_size,
        "canonical_metadata_bytes": canonical_metadata_bytes,
        "index_to_canonical_ratio": baseline.stat().st_size / canonical_metadata_bytes,
        "fixture_counts": fixture_counts,
        "full_load_fingerprint_p50_ms": statistics.median(full_ms),
        "full_load_fingerprint_p95_ms": full_p95,
        "production_ingest_p50_ms": statistics.median(ingest_ms),
        "production_ingest_p95_ms": _p95(ingest_ms),
        "status_query_p95_ms": _p95(query_ms),
        "incremental_plus_query_p95_ms": combined_p95,
        "speedup": full_p95 / combined_p95,
        "parity_rate": 1.0 if exact_parity else 0.0,
        "status_counts": status_counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scales", type=int, nargs="+", default=list(SCALES))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="vcp-production-provenance-") as temporary:
        root = Path(temporary)
        scales = [run_scale(root, count) for count in args.scales]
    result = {
        "kind": "production-provenance-index-benchmark",
        "generated_at": stamp(),
        "seed": SEED,
        "commit": _commit(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "warmup": "OS cache naturally warm after deterministic fixture generation; no timed warmup",
        "scales": scales,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
