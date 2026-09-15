"""Explicit, deterministic Real-plus-Scaled provenance benchmark (not normal CI).

Run with::

    uv run python tests/performance/provenance/benchmark.py --output result.json

The generated records use the production SampleChange fields and valid opaque entity/edge
references. No medical payload or live VCP file is copied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from vcp.core.time import stamp
from vcp.provenance.schema import ChangeDomain, ChangeType, SampleChange, SemanticEffect

SEED = 20260913
SUBSETS = tuple(f"subset-{value}" for value in range(8))
EFFECTS = (
    SemanticEffect.INPUT_AFFECTING,
    SemanticEffect.TRAINING_AFFECTING,
    SemanticEffect.SPLIT_AFFECTING,
    SemanticEffect.EVALUATION_AFFECTING,
    SemanticEffect.DISPLAY_ONLY,
    SemanticEffect.UNKNOWN,
)
FROM_HASH = "a" * 64
TO_HASH = "b" * 64
BEFORE_HASH = "c" * 64
AFTER_HASH = "d" * 64


def _event_id(index: int, change_type: ChangeType) -> str:
    text = "|".join(
        [
            FROM_HASH,
            TO_HASH,
            f"sample-{index:08d}",
            change_type.value,
            "-" if change_type == ChangeType.ADDED else BEFORE_HASH,
            "-" if change_type == ChangeType.REMOVED else AFTER_HASH,
        ]
    )
    return hashlib.sha256(text.encode()).hexdigest()


def _event(index: int) -> tuple[str, ...]:
    change_type = (ChangeType.MODIFIED, ChangeType.MODIFIED, ChangeType.ADDED, ChangeType.REMOVED)[
        index % 4
    ]
    effect = EFFECTS[(index * 5 + SEED) % len(EFFECTS)]
    domain = {
        SemanticEffect.INPUT_AFFECTING: ChangeDomain.VIEWS,
        SemanticEffect.TRAINING_AFFECTING: ChangeDomain.LABEL_SOURCE,
        SemanticEffect.SPLIT_AFFECTING: ChangeDomain.GROUP,
        SemanticEffect.EVALUATION_AFFECTING: ChangeDomain.LABELS,
        SemanticEffect.DISPLAY_ONLY: ChangeDomain.META,
        SemanticEffect.UNKNOWN: ChangeDomain.META,
    }[effect]
    before = None if change_type == ChangeType.ADDED else BEFORE_HASH
    after = None if change_type == ChangeType.REMOVED else AFTER_HASH
    return (
        _event_id(index, change_type),
        f"sample-{index:08d}",
        FROM_HASH,
        TO_HASH,
        change_type.value,
        json.dumps([domain.value]),
        json.dumps([domain.value.lower()]),
        json.dumps([effect.value]),
        before,
        after,
        SUBSETS[(index * 3 + SEED) % len(SUBSETS)],
    )


def _validate_schema(row: tuple[str, ...]) -> None:
    SampleChange(
        change_id=row[0],
        from_dataset="synthetic-before",
        from_samples_hash=row[2],
        to_dataset="synthetic-after",
        to_samples_hash=row[3],
        sample_id=row[1],
        change_type=row[4],
        changed_domains=json.loads(row[5]),
        changed_fields=json.loads(row[6]),
        semantic_effects=json.loads(row[7]),
        before_row_hash=row[8],
        after_row_hash=row[9],
    )


def _chunks(count: int, size: int = 10_000) -> Iterable[list[tuple[str, ...]]]:
    for start in range(0, count, size):
        yield [_event(index) for index in range(start, min(count, start + size))]


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE changes(
          change_id TEXT PRIMARY KEY, sample_id TEXT, from_hash TEXT, to_hash TEXT,
          change_type TEXT, changed_domains TEXT, changed_fields TEXT,
          semantic_effects TEXT, before_row_hash TEXT, after_row_hash TEXT, subset TEXT
        );
        CREATE INDEX changes_sample ON changes(sample_id);
        CREATE INDEX changes_transition ON changes(from_hash,to_hash);
        CREATE TABLE entities(entity_id TEXT PRIMARY KEY, entity_type TEXT, subset TEXT, mode TEXT);
        CREATE TABLE edges(source_id TEXT, target_id TEXT, edge_type TEXT,
                           PRIMARY KEY(source_id,target_id,edge_type));
        CREATE INDEX edges_source ON edges(source_id,edge_type);
        CREATE INDEX edges_target ON edges(target_id,edge_type);
        CREATE TABLE heads(dataset_id TEXT PRIMARY KEY);
        """
    )


def generate(path: Path, count: int) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    _schema(connection)
    digest = hashlib.sha256()
    canonical_bytes = 0
    with connection:
        connection.execute("INSERT INTO heads VALUES(?)", (f"dataset:after@{TO_HASH}",))
        connection.executemany(
            "INSERT INTO entities VALUES(?,?,?,?)",
            [
                (f"dataset:before@{FROM_HASH}", "dataset", None, None),
                (f"dataset:after@{TO_HASH}", "dataset", None, None),
            ],
        )
        connection.execute(
            "INSERT INTO edges VALUES(?,?,?)",
            (f"dataset:before@{FROM_HASH}", f"dataset:after@{TO_HASH}", "DERIVED_FROM"),
        )
        for rows in _chunks(count):
            _validate_schema(rows[0])
            _validate_schema(rows[-1])
            connection.executemany("INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
            for row in rows:
                digest.update(row[0].encode())
                canonical_bytes += sum(len(value) for value in row if value is not None) + 10
        run_count = max(15, round(count * 15 / 4407))
        entities = []
        edges = []
        for index in range(run_count):
            subset = SUBSETS[index % len(SUBSETS)]
            mode = "train" if index % 3 else "eval"
            run = f"run:r-{index:06d}"
            reading = f"reading:r-{index:06d}"
            entities.extend([(run, "run", subset, mode), (reading, "reading", subset, mode)])
            edges.extend(
                [
                    (f"dataset:before@{FROM_HASH}", run, "CONSUMED_BY"),
                    (run, reading, "EVALUATED_BY"),
                ]
            )
            if index % 10 == 0:
                artifact = f"artifact:a-{index:06d}"
                entities.append((artifact, "artifact", subset, mode))
                edges.append((run, artifact, "PRODUCED_BY"))
            if index % 25 == 0:
                fusion = f"run:fusion-{index:06d}"
                submission = f"submission:s-{index:06d}"
                entities.extend(
                    [(fusion, "fusion_run", subset, mode), (submission, "submission", subset, mode)]
                )
                edges.extend([(run, fusion, "COMBINED_INTO"), (fusion, submission, "SUBMITTED_AS")])
        connection.executemany("INSERT INTO entities VALUES(?,?,?,?)", entities)
        connection.executemany("INSERT INTO edges VALUES(?,?,?)", edges)
        canonical_bytes += sum(
            sum(len(value) for value in row if value is not None) for row in [*entities, *edges]
        )
    connection.close()
    return {
        "seed": SEED,
        "events": count,
        "runs": run_count,
        "subsets": len(SUBSETS),
        "event_id_stream_sha256": digest.hexdigest(),
        "database_bytes": path.stat().st_size,
        "canonical_metadata_bytes": canonical_bytes,
        "index_to_canonical_ratio": path.stat().st_size / canonical_bytes,
        "no_op_transitions": 1,
    }


def _severity(effect: str, mode: str) -> int:
    if effect == SemanticEffect.UNKNOWN.value:
        return 1
    if effect == SemanticEffect.SPLIT_AFFECTING.value:
        return 2
    if mode == "train" and effect in {
        SemanticEffect.INPUT_AFFECTING.value,
        SemanticEffect.TRAINING_AFFECTING.value,
    }:
        return 2
    if mode == "eval" and effect in {
        SemanticEffect.INPUT_AFFECTING.value,
        SemanticEffect.EVALUATION_AFFECTING.value,
    }:
        return 2
    return 0


def _full_python(connection: sqlite3.Connection, limit: int) -> dict[str, int]:
    effects: dict[str, set[str]] = {subset: set() for subset in SUBSETS}
    rows = connection.execute(
        "SELECT subset,semantic_effects FROM changes ORDER BY rowid LIMIT ?", (limit,)
    )
    for subset, values in rows:
        effects[subset].update(json.loads(values))
    return {
        entity_id: max((_severity(effect, mode) for effect in effects[subset]), default=0)
        for entity_id, subset, mode in connection.execute(
            "SELECT entity_id,subset,mode FROM entities WHERE entity_type='run'"
        )
    }


def _full_sqlite(connection: sqlite3.Connection, limit: int) -> dict[str, int]:
    grouped = {
        subset: set(json.loads(values))
        for subset, values in connection.execute(
            """SELECT subset,json_group_array(DISTINCT value)
               FROM (SELECT subset,value FROM changes,json_each(semantic_effects)
                     WHERE changes.rowid<=?) GROUP BY subset""",
            (limit,),
        )
    }
    return {
        entity_id: max(
            (_severity(effect, mode) for effect in grouped.get(subset, set())), default=0
        )
        for entity_id, subset, mode in connection.execute(
            "SELECT entity_id,subset,mode FROM entities WHERE entity_type='run'"
        )
    }


def _incremental(
    connection: sqlite3.Connection, start: int, limit: int, base: dict[str, int]
) -> dict[str, int]:
    result = dict(base)
    changed: dict[str, set[str]] = {}
    for subset, values in connection.execute(
        "SELECT subset,semantic_effects FROM changes WHERE rowid>? AND rowid<=?",
        (start, limit),
    ):
        changed.setdefault(subset, set()).update(json.loads(values))
    if not changed:
        return result
    placeholders = ",".join("?" for _ in changed)
    for entity_id, subset, mode in connection.execute(
        f"SELECT entity_id,subset,mode FROM entities WHERE entity_type='run' "
        f"AND subset IN ({placeholders})",
        tuple(sorted(changed)),
    ):
        result[entity_id] = max(
            result[entity_id],
            max((_severity(effect, mode) for effect in changed[subset]), default=0),
        )
    return result


def _measure(action: Callable[[], Any], repetitions: int) -> tuple[Any, dict[str, float]]:
    values = []
    result: Any = None
    for _ in range(repetitions):
        started = perf_counter_ns()
        result = action()
        values.append((perf_counter_ns() - started) / 1_000_000)
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return result, {"p50_ms": statistics.median(values), "p95_ms": p95}


def _queries(connection: sqlite3.Connection, count: int, repetitions: int) -> dict[str, Any]:
    sample = f"sample-{count // 2:08d}"
    run = "run:r-000000"
    statements = {
        "Q1_latest_head": ("SELECT dataset_id FROM heads", ()),
        "Q2_diff_summary": ("SELECT change_type,count(*) FROM changes GROUP BY change_type", ()),
        "Q3_sample_impact": ("SELECT * FROM changes WHERE sample_id=?", (sample,)),
        "Q4_dataset_impact": (
            "SELECT count(*) FROM edges WHERE source_id=?",
            (f"dataset:before@{FROM_HASH}",),
        ),
        "Q5_stale_runs": (
            "SELECT count(*) FROM entities WHERE entity_type='run' AND mode='train'",
            (),
        ),
        "Q6_explain": (
            """WITH RECURSIVE ancestors(id) AS (
                 SELECT ? UNION SELECT e.source_id FROM edges e JOIN ancestors a
                 ON e.target_id=a.id) SELECT count(*) FROM ancestors""",
            (run,),
        ),
        "Q7_sync_status": ("SELECT count(*),max(rowid) FROM changes", ()),
    }
    results: dict[str, Any] = {}
    for name, (sql, params) in statements.items():
        _, timing = _measure(
            lambda s=sql, p=params: connection.execute(s, p).fetchall(), repetitions
        )
        results[name] = timing
    return results


def _transaction_probe(connection: sqlite3.Connection, kind: str) -> bool:
    connection.execute("BEGIN")
    try:
        if kind == "run":
            connection.execute(
                "INSERT OR REPLACE INTO entities VALUES(?,?,?,?)",
                ("run:probe", "run", SUBSETS[0], "train"),
            )
            connection.execute(
                "INSERT OR REPLACE INTO edges VALUES(?,?,?)",
                (f"dataset:after@{TO_HASH}", "run:probe", "CONSUMED_BY"),
            )
        elif kind == "supersession":
            connection.execute(
                "INSERT OR REPLACE INTO edges VALUES(?,?,?)",
                ("artifact:old", "artifact:new", "SUPERSEDES"),
            )
        elif kind == "interrupted":
            connection.execute(
                "INSERT OR REPLACE INTO entities VALUES(?,?,?,?)",
                ("run:interrupted", "run", SUBSETS[0], "train"),
            )
            raise RuntimeError("injected interruption")
        return True
    except RuntimeError:
        return kind == "interrupted"
    finally:
        connection.rollback()


def benchmark_scale(path: Path, count: int) -> dict[str, Any]:
    generated_started = perf_counter_ns()
    generated = generate(path, count)
    rebuild_ms = (perf_counter_ns() - generated_started) / 1_000_000
    connection = sqlite3.connect(path)
    try:
        repetitions = 7 if count <= 10_000 else 3
        batch = min(max(100, count // 100), 100_000)
        prefix = count - batch
        base = _full_python(connection, prefix)
        full_result, full = _measure(lambda: _full_python(connection, count), repetitions)
        sqlite_result, sqlite_full = _measure(lambda: _full_sqlite(connection, count), repetitions)
        incremental_result, incremental = _measure(
            lambda: _incremental(connection, prefix, count, base), repetitions
        )
        parity = full_result == sqlite_result == incremental_result
        queries = _queries(connection, count, repetitions)
        _, small_update = _measure(
            lambda: _incremental(connection, max(0, count - min(100, count)), count, base),
            repetitions,
        )
        _, no_op_update = _measure(
            lambda: _incremental(connection, count, count, full_result), repetitions
        )
        _, run_update = _measure(lambda: _transaction_probe(connection, "run"), repetitions)
        _, supersession_update = _measure(
            lambda: _transaction_probe(connection, "supersession"), repetitions
        )
        recovery, interrupted_update = _measure(
            lambda: _transaction_probe(connection, "interrupted"), repetitions
        )
    finally:
        connection.close()
    speedup = full["p95_ms"] / max(incremental["p95_ms"], 0.000_001)
    return {
        "generator": generated,
        "repetitions": repetitions,
        "full_rebuild_ms": rebuild_ms,
        "baselines": {
            "full_replay": full,
            "full_sqlite_recompute": sqlite_full,
            "dependency_incremental": incremental,
        },
        "queries": queries,
        "updates": {
            "U1_small_diff": {"events": min(100, count), **small_update},
            "U2_large_diff": {"events": batch, **incremental},
            "U3_no_op_transition": no_op_update,
            "U4_new_run_dependencies": run_update,
            "U5_artifact_supersession_edge": supersession_update,
            "U6_interrupted_transaction": {
                **interrupted_update,
                "recovery_success": bool(recovery),
            },
        },
        "parity_rate": 1.0 if parity else 0.0,
        "incremental_p95_speedup": speedup,
        "events_per_second_full": count / max(full["p50_ms"] / 1000, 0.000_001),
        "affected_rows_per_input_event": generated["runs"] / batch,
        "peak_memory": None,
        "peak_memory_note": "not measured; tracemalloc would omit SQLite native allocations",
        "crash_recovery_success_rate": 1.0 if recovery else 0.0,
    }


def _git_revision() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--scales", type=int, nargs="+", default=[1_000, 10_000, 100_000, 1_000_000]
    )
    args = parser.parse_args()
    document: dict[str, Any] = {
        "schema_version": 1,
        "created_at": stamp(),
        "environment": {
            "hardware": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "unknown"),
            "operating_system": platform.platform(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "vcp_commit": _git_revision(),
            "seed": SEED,
            "warmup": "one prefix materialization before timed repetitions",
        },
        "scales": {},
    }
    with tempfile.TemporaryDirectory(prefix="vcp-provenance-benchmark-") as directory:
        for count in args.scales:
            document["scales"][str(count)] = benchmark_scale(
                Path(directory) / f"{count}.sqlite3", count
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    main()
