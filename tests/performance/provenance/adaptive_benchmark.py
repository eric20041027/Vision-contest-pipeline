"""Explicit six-method benchmark; requires a disposable PostgreSQL test service.

All methods replay identical canonical metadata on fresh state per repetition.
No connection data, raw exception, SQL text or SQL expression is serialized.
The service must allow CREATE DATABASE; only newly created databases are dropped.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from importlib import import_module
from pathlib import Path
from time import perf_counter_ns
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from vcp.core.time import stamp
from vcp.provenance.backend import BackendConfig, BackendName, SQLiteBackend
from vcp.provenance.graph import build_graph
from vcp.provenance.index import ProvenanceIndex, dataset_heads, graph_hash
from vcp.provenance.strategy import BENCHMARK_SCHEMA_VERSION
from vcp.provenance.views import compute_statuses, explain, impact

if __package__:
    from .workloads import RATIOS, SCALES, Workload, build_scenario, scenario_matrix
else:
    from workloads import RATIOS, SCALES, Workload, build_scenario, scenario_matrix

METHODS = (
    "canonical_full",
    "sqlite_full",
    "sqlite_incremental",
    "postgres_full",
    "postgres_incremental",
    "postgres_adaptive",
)
OPERATIONS = ("maintenance", "status", "impact", "explain")
PARITY_FIELDS = ("graph_parity", "graph_hash_parity", "status_parity", "head_parity")
DECISION_FIELDS = (
    "requested_strategy",
    "selected_strategy",
    "strategy_reason",
    "policy_version",
    "estimated_incremental_ms",
    "estimated_full_ms",
)
NOT_CONFIGURED = "PostgreSQL benchmark service is not configured"


def runtime_environment() -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unavailable"
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "cpu_count": os.cpu_count(),
        "sqlite_version": sqlite3.sqlite_version,
        "vcp_commit": commit,
    }


def postgres_preflight():
    """Explicit opt-in only. Never read credential files or libpq secret variables."""
    service = os.environ.get("VCP_TEST_PG_SERVICE")
    if not service or any(
        not os.environ.get(key) or not Path(os.environ[key]).is_file()
        for key in ("PGSERVICEFILE", "PGPASSFILE")
    ):
        raise RuntimeError(NOT_CONFIGURED)
    from vcp.provenance.postgres import validate_pg_service

    try:
        validate_pg_service(service)
        driver = import_module("psycopg")
    except Exception:
        raise RuntimeError(
            "PostgreSQL benchmark driver or service configuration is invalid"
        ) from None
    return driver, service


@dataclass
class BackendState:
    backend: Any = None
    connect: Callable | None = None
    environment: dict[str, Any] = field(default_factory=dict)


@contextmanager
def fresh_backend(method: str, data: Path, *, pg_runtime=None):
    if method == "canonical_full":
        yield BackendState()
        return
    if method.startswith("sqlite_"):
        yield BackendState(SQLiteBackend(ProvenanceIndex(data / "indexes" / "benchmark.sqlite3")))
        return
    if method not in METHODS:
        raise ValueError("unknown benchmark method")
    from vcp.provenance import postgres

    driver, service = pg_runtime if pg_runtime is not None else postgres_preflight()
    database = "vcp_bench_" + uuid4().hex

    def connect(*, admin=False):
        kwargs = {"service": service, "autocommit": True, "connect_timeout": 5}
        if not admin:
            kwargs["dbname"] = database
        try:
            return driver.connect(**kwargs)
        except Exception:
            raise RuntimeError("PostgreSQL benchmark connection failed") from None

    owned = False
    try:
        with connect(admin=True) as connection:
            connection.execute(
                driver.sql.SQL("CREATE DATABASE {}").format(driver.sql.Identifier(database))
            )
            owned = True
        backend = postgres.PostgresProvenanceBackend(BackendConfig(BackendName.POSTGRESQL, service))
        original = postgres._connect
        with connect() as connection:
            major, fingerprint = postgres.maintenance_environment(connection)
            version = connection.info.server_version
        # This test-only scope routes only this backend's identity to its owned database.
        with patch.object(
            postgres,
            "_connect",
            lambda config: connect() if config is backend.config else original(config),
        ):
            yield BackendState(
                backend,
                connect,
                {
                    "postgresql_major": major,
                    "postgresql_version": version,
                    "environment_fingerprint": fingerprint,
                    "backend_schema_version": postgres.POSTGRES_SCHEMA_VERSION,
                },
            )
    finally:
        if owned:
            try:
                with connect(admin=True) as connection:
                    connection.execute(
                        driver.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            driver.sql.Identifier(database)
                        )
                    )
            except Exception:
                raise RuntimeError("PostgreSQL benchmark database cleanup failed") from None


# Retain plan measurements, but never expressions, literals, object aliases or query text.
_PLAN_TEXT = {
    "Node Type": {
        "ModifyTable",
        "Insert",
        "Update",
        "Delete",
        "Seq Scan",
        "Index Scan",
        "Index Only Scan",
        "Bitmap Heap Scan",
        "Bitmap Index Scan",
        "Result",
        "Nested Loop",
        "Hash Join",
        "Merge Join",
        "Hash",
        "Aggregate",
        "Sort",
        "Limit",
        "Append",
        "Recursive Union",
        "WorkTable Scan",
        "CTE Scan",
        "Function Scan",
        "Values Scan",
        "Materialize",
        "Memoize",
        "Gather",
        "Gather Merge",
        "Subquery Scan",
        "Unique",
    },
    "Operation": {"Insert", "Update", "Delete", "Merge"},
    "Join Type": {"Inner", "Left", "Right", "Full", "Semi", "Anti"},
}
_PLAN_NUMERIC = {
    "Startup Cost",
    "Total Cost",
    "Plan Rows",
    "Plan Width",
    "Actual Startup Time",
    "Actual Total Time",
    "Actual Rows",
    "Actual Loops",
    "Shared Hit Blocks",
    "Shared Read Blocks",
    "Shared Dirtied Blocks",
    "Shared Written Blocks",
    "Local Hit Blocks",
    "Local Read Blocks",
    "Local Dirtied Blocks",
    "Local Written Blocks",
    "Temp Read Blocks",
    "Temp Written Blocks",
    "I/O Read Time",
    "I/O Write Time",
    "Planning Time",
    "Execution Time",
    "Rows Removed by Filter",
    "Rows Removed by Join Filter",
    "Tuples Inserted",
    "Conflicting Tuples",
}


def sanitize_explain(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def clean(value):
        result = {}
        for key, item in value.items():
            if key in _PLAN_NUMERIC and isinstance(item, (int, float)) and math.isfinite(item):
                result[key] = item
            elif key in _PLAN_TEXT and item in _PLAN_TEXT[key]:
                result[key] = item
            elif key in ("Plan", "Planning") and isinstance(item, dict):
                result[key] = clean(item)
            elif key == "Plans" and isinstance(item, list):
                result[key] = [clean(child) for child in item]
        return result

    return [clean(plan) for plan in plans]


class _ExplainConnection:
    def __init__(self, raw):
        self.raw = raw
        self.plans = []
        self.seen = set()

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def close(self):
        # The outer instrumentation context owns and rolls back this connection.
        pass

    def probe(self, query, params):
        operation = str(query).lstrip().split(None, 1)[0].upper()
        if operation not in {"INSERT", "UPDATE", "DELETE"} or query in self.seen:
            return
        self.seen.add(query)
        # EXPLAIN ANALYZE executes DML. Undo the probe before executing the actual
        # statement, which may need RETURNING rows to continue the production path.
        with self.raw.transaction(force_rollback=True):
            raw = self.raw.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params
            ).fetchone()[0]
            self.plans.extend(sanitize_explain(raw))

    def execute(self, query, params=()):
        self.probe(query, params)
        return self.raw.execute(query, params)

    @contextmanager
    def cursor(self):
        with self.raw.cursor() as cursor:
            owner = self

            class Cursor:
                def executemany(self, query, rows):
                    # Production writers supply finite row sequences. Probe one row
                    # per SQL shape, then run the complete batch normally.
                    if rows:
                        owner.probe(query, rows[0])
                    return cursor.executemany(query, rows)

            yield Cursor()


def capture_explain_rollback(connection, method: str, action: Callable) -> list[dict[str, Any]]:
    """Instrument a complete production maintenance action on an owned connection.

    Capture the first DML row for each statement shape, not an aggregate execution
    plan for the Python operation. All probes and the entire action are rolled back.
    The caller supplies the action so it can bind the backend to this connection.
    """
    if method not in METHODS or not method.startswith("postgres_"):
        raise ValueError("EXPLAIN instrumentation requires a PostgreSQL method")
    observed = _ExplainConnection(connection)
    connection.execute("BEGIN")
    try:
        action(observed)
        if not observed.plans:
            raise RuntimeError("PostgreSQL maintenance produced no instrumentation plans")
        return observed.plans
    finally:
        connection.rollback()


def _timed(action):
    started = perf_counter_ns()
    result = action()
    return result, (perf_counter_ns() - started) / 1_000_000


def _maintain(state, method, workload, data, configs, policy_id):
    if method == "canonical_full":
        graph = build_graph(data, configs)
        return (
            graph,
            {head: compute_statuses(graph, head) for head in _all_heads(graph)},
            graph_hash(graph),
        )
    if method == "sqlite_full":
        return state.backend.rebuild(data, configs)
    kwargs = {}
    if method.startswith("postgres_"):
        kwargs = {
            "requested_strategy": {
                "postgres_full": "full",
                "postgres_incremental": "incremental",
                "postgres_adaptive": "auto",
            }[method],
            "policy_id": policy_id,
        }
    return state.backend.ingest_diff(workload.artifact_id, data, configs, **kwargs)


def _all_heads(graph):
    return sorted(key for key, entity in graph.entities.items() if entity.entity_type == "dataset")


def _storage(state, method) -> tuple[int, str]:
    if method == "canonical_full":
        return 0, "no persistent database; Python heap not measured"
    if method.startswith("sqlite_"):
        path = state.backend.index.path
        paths = [path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")]
        return sum(
            p.stat().st_size for p in paths if p.exists()
        ), "SQLite file bytes including WAL/SHM"
    with state.connect() as connection:
        size = connection.execute(
            "SELECT coalesce(sum(pg_total_relation_size(c.oid)),0)::bigint "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='vcp_provenance' AND c.relkind='r'"
        ).fetchone()[0]
    return int(size), "sum pg_total_relation_size of vcp_provenance tables (includes indexes/TOAST)"


def _closure(workload):
    if workload.changed_samples == 0:
        return 0
    graph = workload.expected
    ancestors = {workload.source_id}
    pending = [workload.source_id]
    while pending:
        current = pending.pop()
        for source, target in graph.transitions:
            if target == current and source not in ancestors:
                ancestors.add(source)
                pending.append(source)
    return len(set().union(*(set(graph.descendants(source)) for source in ancestors)))


def _decision(outcome, method, workload, dirty):
    if method in ("canonical_full", "sqlite_full"):
        return {
            "requested_strategy": "full",
            "selected_strategy": "FULL",
            "strategy_reason": "canonical_full_replay"
            if method == "canonical_full"
            else "requested_full",
            "policy_version": "not_applicable",
            "estimated_incremental_ms": None,
            "estimated_full_ms": None,
            "dirty_entities": dirty,
        }
    return {
        **{key: getattr(outcome, key) for key in DECISION_FIELDS},
        "dirty_entities": outcome.dirty_entities,
    }


@dataclass
class ScenarioResult:
    method: str
    workload: Workload
    environment: dict[str, Any]
    samples: list[dict[str, Any]] = field(default_factory=list)
    plans: list[dict[str, Any]] = field(default_factory=list)
    failure: str | None = None

    def to_dict(self) -> dict[str, Any]:
        spec, graph = self.workload.scenario, self.workload.expected
        good = (
            self.failure is None
            and bool(self.samples)
            and all(all(sample[key] for key in PARITY_FIELDS) for sample in self.samples)
        )
        samples = self.samples if good else []
        result = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "method": self.method,
            "scenario_id": spec.scenario_id,
            "scenario_hash": spec.scenario_hash,
            "workload_hash": self.workload.workload_hash,
            "track": spec.track,
            "seed": spec.seed,
            "topology": spec.topology,
            "entities": spec.entities,
            "change_ratio": spec.change_ratio,
            "realized_change_ratio": self.workload.changed_samples / self.workload.sample_entities,
            "sample_entities": self.workload.sample_entities,
            "changed_samples": self.workload.changed_samples,
            "total_entities": len(graph.entities),
            "total_edges": len(graph.edges),
            "total_changes": len(graph.changes),
            "historical_changes": self.workload.historical_changes,
            "head_count": len(dataset_heads(graph)),
            "environment": self.environment,
            "status": "ok" if good else "failed",
            "failure": self.failure,
            "samples": samples,
            "repetitions": len(self.samples),
            "parity_rate": float(good),
            "explain_analyze": self.plans,
            "instrumentation": (
                "separate fresh state; first DML row per SQL shape; rollback; excluded from timings"
            ),
            "query_measurement": "public status API; impact/explain include graph load as in CLI",
            "warmup": "baseline rebuilt before each sample; no timed warmup; OS caches may be warm",
        }
        for key in PARITY_FIELDS:
            result[key] = bool(self.samples) and all(row[key] for row in self.samples)
        for operation in OPERATIONS:
            values = sorted(row[f"{operation}_ms"] for row in samples)
            result[f"{operation}_p50_ms"] = statistics.median(values) if values else None
            result[f"{operation}_p95_ms"] = (
                values[round(0.95 * (len(values) - 1))] if values else None
            )
        first = samples[0] if samples else {}
        for key in (
            *DECISION_FIELDS,
            "dirty_entities",
            "database_bytes",
            "storage_measurement",
            "graph_hash",
        ):
            result[key] = first.get(key)
        result["dirty_ratio"] = (
            first.get("dirty_entities", 0) / len(graph.entities) if samples else None
        )
        maintenance = result["maintenance_p50_ms"]
        result["throughput_samples_per_second"] = (
            self.workload.changed_samples / (maintenance / 1000)
            if maintenance
            else (0 if good else None)
        )
        return result


def run_method(
    workload: Workload, method: str, *, repetitions=None, pg_runtime=None, policy_id=None
) -> ScenarioResult:
    if method not in METHODS:
        raise ValueError("unknown benchmark method")
    repetitions = workload.scenario.repetitions if repetitions is None else repetitions
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    result = ScenarioResult(method, workload, runtime_environment())
    expected, expected_hash = workload.expected, graph_hash(workload.expected)
    expected_statuses = {head: compute_statuses(expected, head) for head in _all_heads(expected)}
    dirty = _closure(workload)
    phase = "benchmark_operation_failed"
    try:
        for _ in range(repetitions):
            with tempfile.TemporaryDirectory(prefix="vcp-adaptive-sample-") as temporary:
                data, configs = workload.clone(Path(temporary))
                with fresh_backend(method, data, pg_runtime=pg_runtime) as state:
                    result.environment.update(state.environment)
                    if state.backend is not None:
                        state.backend.rebuild(data, configs)
                    workload.publish(data)
                    outcome, maintenance_ms = _timed(
                        partial(_maintain, state, method, workload, data, configs, policy_id)
                    )
                    load = (
                        partial(build_graph, data, configs)
                        if state.backend is None
                        else state.backend.load_graph
                    )
                    status = (
                        (lambda head, load=load: compute_statuses(load(), head))
                        if state.backend is None
                        else state.backend.statuses
                    )
                    _, status_ms = _timed(partial(status, workload.target_id))
                    _, impact_ms = _timed(
                        lambda load=load: impact(
                            load(), workload.source_id, None, head_id=workload.target_id
                        )
                    )
                    entity = sorted(expected.entities)[-1]
                    _, explain_ms = _timed(lambda load=load, entity=entity: explain(load(), entity))
                    actual = outcome[0] if state.backend is None else load()
                    recorded_hash = (
                        outcome[2] if state.backend is None else state.backend.stats()["graph_hash"]
                    )
                    actual_statuses = (
                        outcome[1]
                        if state.backend is None
                        else {head: status(head) for head in _all_heads(expected)}
                    )
                    materialized_heads = (
                        set(actual_statuses)
                        if state.backend is None
                        else {row["head_id"] for row in state.backend.normalized()["statuses"]}
                    )
                    database_bytes, storage_measurement = _storage(state, method)
                    row = {
                        "maintenance_ms": maintenance_ms,
                        "status_ms": status_ms,
                        "impact_ms": impact_ms,
                        "explain_ms": explain_ms,
                        "graph_parity": actual.normalized() == expected.normalized(),
                        "graph_hash_parity": recorded_hash == graph_hash(actual) == expected_hash,
                        "status_parity": actual_statuses == expected_statuses,
                        "head_parity": (
                            dataset_heads(actual) == dataset_heads(expected)
                            and materialized_heads == set(_all_heads(expected))
                        ),
                        "graph_hash": recorded_hash,
                        "database_bytes": database_bytes,
                        "storage_measurement": storage_measurement,
                        **_decision(outcome, method, workload, dirty),
                    }
                    result.samples.append(row)
                    if not all(row[key] for key in PARITY_FIELDS):
                        raise RuntimeError("benchmark parity failed")
        if method.startswith("postgres_"):
            phase = "benchmark_instrumentation_failed"
            with tempfile.TemporaryDirectory(prefix="vcp-adaptive-explain-") as temporary:
                data, configs = workload.clone(Path(temporary))
                with fresh_backend(method, data, pg_runtime=pg_runtime) as state:
                    state.backend.rebuild(data, configs)
                    workload.publish(data)
                    from vcp.provenance import postgres

                    def instrument(observed):
                        with patch.object(postgres, "_connect", lambda config: observed):
                            _maintain(state, method, workload, data, configs, policy_id)

                    with state.connect() as connection:
                        result.plans = capture_explain_rollback(connection, method, instrument)
    except Exception:
        # Never stringify third-party errors, including errors during teardown.
        result.failure = phase
    return result


def run_matrix(root: Path, scenarios, *, repetitions=None, pg_runtime=None):
    rows = []
    for scenario in scenarios:
        workload = build_scenario(root / scenario.scenario_id, scenario)
        for method in METHODS:
            rows.append(
                run_method(
                    workload, method, repetitions=repetitions, pg_runtime=pg_runtime
                ).to_dict()
            )
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entities", type=int, nargs="+", default=list(SCALES))
    parser.add_argument("--ratios", type=float, nargs="+", default=list(RATIOS))
    parser.add_argument("--seeds", type=int, nargs="+", default=[20260913, 20260914])
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        pg_runtime = postgres_preflight()
        scenarios = scenario_matrix(seeds=args.seeds, entities=args.entities, ratios=args.ratios)
        with tempfile.TemporaryDirectory(prefix="vcp-adaptive-benchmark-") as temporary:
            rows = run_matrix(
                Path(temporary), scenarios, repetitions=args.repetitions, pg_runtime=pg_runtime
            )
        document = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "created_at": stamp(),
            "kind": "adaptive-provenance-six-method-benchmark",
            "methods": list(METHODS),
            "results": rows,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        return 0 if all(row["status"] == "ok" for row in rows) else 1
    except Exception as error:
        message = (
            NOT_CONFIGURED
            if error.args == (NOT_CONFIGURED,)
            else "Provenance benchmark failed (details redacted)"
        )
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
