"""Explicit six-method benchmark; requires a disposable PostgreSQL test service.

All methods replay identical canonical metadata on fresh state per repetition.
No connection data, raw exception, credentials, or SQL query text is serialized.
Raw PostgreSQL EXPLAIN JSON is retained alongside a redacted allowlisted view.
The service must allow CREATE DATABASE; only newly created databases are dropped.
"""

from __future__ import annotations

import argparse
import copy
import gc
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache, partial
from importlib import import_module
from itertools import zip_longest
from pathlib import Path
from time import perf_counter_ns
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from vcp.core.atomic import write_once_text
from vcp.core.hashing import sha256_file, sha256_text
from vcp.core.time import stamp
from vcp.provenance.backend import BackendConfig, BackendName, SQLiteBackend
from vcp.provenance.graph import ProvenanceGraph, build_graph
from vcp.provenance.index import ProvenanceIndex, dataset_heads, graph_hash
from vcp.provenance.strategy import BENCHMARK_SCHEMA_VERSION
from vcp.provenance.views import compute_statuses, explain, impact

if __package__:
    from .workloads import RATIOS, SCALES, Scenario, Workload, build_scenario, scenario_matrix
else:
    from workloads import RATIOS, SCALES, Scenario, Workload, build_scenario, scenario_matrix

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
_SENSITIVE_TEXT = (
    "password",
    "secret",
    "token",
    "conninfo",
    "credential",
    "pgpass",
    "authorization",
)
_SENSITIVE_KEY_PARTS = frozenset(
    {"password", "passwd", "secret", "token", "credential", "authorization"}
)
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "access_key",
        "private_key",
        "query_text",
        "connection",
        "connection_string",
        "connection_uri",
        "conninfo",
        "dsn",
        "host",
        "hostaddr",
        "port",
        "dbname",
        "user",
        "passfile",
        "service",
        "sslcert",
        "sslkey",
        "sslrootcert",
    }
)
_CONNECTION_URI = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_LIBPQ_KEYWORD = re.compile(
    r"(?:^|\s)(?:host|hostaddr|port|dbname|user|password|passfile|service|"
    r"sslmode|sslcert|sslkey|sslrootcert)\s*=\s*(?:'[^']*'|\"[^\"]*\"|\S+)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?:^|\s)bearer\s+\S+", re.IGNORECASE)
_SECRET_KEY_VALUE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:sk|rk|pk)[_-][A-Za-z0-9_-]{8,}", re.IGNORECASE
)
CHECKPOINT_SCHEMA_VERSION = 1
EXECUTION_IDENTITY_SCHEMA_VERSION = 1
SOURCE_TREE_SCHEMA_VERSION = 1
_SOURCE_TREE_DIRECTORIES = (
    "src/vcp",
    "tests/performance/provenance",
)
_SOURCE_TREE_FILES = (
    "pyproject.toml",
    "uv.lock",
)
_RUNTIME_IDENTITY_FIELDS = (
    "system",
    "system_release",
    "system_version",
    "machine",
    "python_version",
    "python_implementation",
    "cpu_model",
    "physical_cpu_cores",
    "logical_cpu_count",
    "ram_bytes",
    "disk_path",
    "disk_total_bytes",
    "sqlite_version",
    "vcp_commit",
)


def _unsafe_explain_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
    parts = frozenset(normalized.split("_"))
    return normalized in _SENSITIVE_KEYS or bool(parts & _SENSITIVE_KEY_PARTS)


def validate_raw_explain(value) -> None:
    """Fail closed before raw PostgreSQL plan evidence enters a result artifact."""
    if isinstance(value, list):
        for item in value:
            validate_raw_explain(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if _unsafe_explain_key(key):
                raise ValueError("unsafe EXPLAIN evidence")
            validate_raw_explain(item)
        return
    if isinstance(value, str):
        text = value.casefold()
        if (
            any(marker in text for marker in _SENSITIVE_TEXT)
            or _CONNECTION_URI.search(value)
            or _LIBPQ_KEYWORD.search(value)
            or _BEARER.search(value)
            or _SECRET_KEY_VALUE.search(value)
        ):
            raise ValueError("unsafe EXPLAIN evidence")
        return
    if not isinstance(value, (int, float, bool, type(None))):
        raise ValueError("unsafe EXPLAIN evidence")


def validate_publication_explain(value) -> None:
    """Validate every raw EXPLAIN field at a formal publication boundary."""
    if isinstance(value, list):
        for item in value:
            validate_publication_explain(item)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key == "explain_analyze":
            validate_raw_explain(item)
        validate_publication_explain(item)


def _validate_checkpoint_value(value) -> None:
    """Apply a generic secret-safe boundary before durable resume evidence."""
    if isinstance(value, list):
        for item in value:
            _validate_checkpoint_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if _unsafe_explain_key(key):
                raise ValueError("unsafe checkpoint evidence")
            _validate_checkpoint_value(item)
        return
    if isinstance(value, str):
        text = value.casefold()
        if (
            any(marker in text for marker in _SENSITIVE_TEXT)
            or _CONNECTION_URI.search(value)
            or _LIBPQ_KEYWORD.search(value)
            or _BEARER.search(value)
            or _SECRET_KEY_VALUE.search(value)
        ):
            raise ValueError("unsafe checkpoint evidence")
        return
    if not isinstance(value, (int, float, bool, type(None))):
        raise ValueError("unsafe checkpoint evidence")


def _json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate checkpoint key")
        result[key] = value
    return result


@dataclass(frozen=True)
class SpoolKey:
    scenario_hash: str
    method: str
    kind: str
    repetition_index: int

    def __post_init__(self):
        if (
            not re.fullmatch(r"[0-9a-f]{64}", self.scenario_hash)
            or self.method not in METHODS
            or self.kind not in {"sample", "explain"}
            or self.repetition_index < 0
        ):
            raise ValueError("invalid spool key")

    @property
    def name(self) -> str:
        return f"{self.kind}-{self.repetition_index:03d}"


class CheckpointStore:
    """Write-once hashed records tied to one exact benchmark execution contract."""

    def __init__(self, root: Path, contract: dict[str, Any]):
        self.root = Path(root).resolve()
        self.contract = contract
        _validate_checkpoint_value(contract)
        self.contract_text = _json_text(contract)
        self.contract_sha256 = sha256_text(self.contract_text)
        identity = contract.get("execution_identity")
        identity_sha256 = contract.get("execution_identity_sha256")
        if identity is not None and (
            not isinstance(identity, dict) or identity_sha256 != sha256_text(_json_text(identity))
        ):
            raise ValueError("invalid checkpoint execution identity")

    def _validate_record_environment(self, key: SpoolKey, payload: dict[str, Any]) -> None:
        identity = self.contract.get("execution_identity")
        if identity is None:
            return
        try:
            environment = payload["environment"]
            if not isinstance(environment, dict):
                raise KeyError
            if _stable_runtime_identity(environment) != identity["runtime"]:
                raise KeyError
            if key.method.startswith("postgres_"):
                expected_postgres = identity["postgresql"]
                actual_postgres = {field: environment[field] for field in expected_postgres}
                if actual_postgres != expected_postgres:
                    raise KeyError
        except (KeyError, TypeError):
            raise ValueError("checkpoint record environment mismatch") from None

    @property
    def contract_path(self) -> Path:
        return self.root / "checkpoint.json"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.contract_path.exists():
            try:
                existing = self.contract_path.read_text(encoding="utf-8")
            except OSError:
                raise ValueError("checkpoint contract mismatch") from None
            if existing != self.contract_text:
                raise ValueError("checkpoint contract mismatch")
            return
        write_once_text(self.contract_path, self.contract_text)

    def record_path(self, key: SpoolKey) -> Path:
        return self.root / "records" / key.scenario_hash / key.method / key.name

    def write(self, key: SpoolKey, payload: dict[str, Any]) -> None:
        _validate_checkpoint_value(payload)
        validate_publication_explain(payload)
        self._validate_record_environment(key, payload)
        target = self.record_path(key)
        if target.exists():
            raise ValueError("checkpoint record is write-once")
        target.parent.mkdir(parents=True, exist_ok=True)
        pending_root = self.root / ".pending"
        pending_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="record-", dir=pending_root))
        try:
            envelope = {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "contract_sha256": self.contract_sha256,
                "key": asdict(key),
                "payload": payload,
            }
            payload_path = temporary / "payload.json"
            write_once_text(payload_path, _json_text(envelope))
            manifest = {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "payload_sha256": sha256_file(payload_path),
            }
            # Publication marker is deliberately written last.
            write_once_text(temporary / "manifest.json", _json_text(manifest))
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def read(self, key: SpoolKey) -> dict[str, Any] | None:
        target = self.record_path(key)
        if not target.exists():
            return None
        if {path.name for path in target.iterdir()} != {"payload.json", "manifest.json"}:
            raise ValueError("invalid checkpoint record")
        try:
            manifest = json.loads(
                (target / "manifest.json").read_text(encoding="utf-8"),
                object_pairs_hook=_no_duplicate_keys,
            )
            envelope = json.loads(
                (target / "payload.json").read_text(encoding="utf-8"),
                object_pairs_hook=_no_duplicate_keys,
            )
        except (OSError, json.JSONDecodeError, ValueError):
            raise ValueError("invalid checkpoint record") from None
        if (
            manifest
            != {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "payload_sha256": sha256_file(target / "payload.json"),
            }
            or envelope.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
            or envelope.get("contract_sha256") != self.contract_sha256
            or envelope.get("key") != asdict(key)
            or set(envelope) != {"schema_version", "contract_sha256", "key", "payload"}
            or not isinstance(envelope.get("payload"), dict)
        ):
            raise ValueError("invalid checkpoint record")
        _validate_checkpoint_value(envelope["payload"])
        validate_publication_explain(envelope["payload"])
        self._validate_record_environment(key, envelope["payload"])
        return envelope["payload"]


def _deployment_evidence() -> dict[str, str | None]:
    kind = os.environ.get("VCP_TEST_PG_DEPLOYMENT_KIND", "native_portable")
    declared = os.environ.get("VCP_TEST_PG_IMAGE_DIGEST")
    if kind not in {"native_portable", "container"}:
        raise ValueError("invalid PostgreSQL deployment evidence")
    if declared is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", declared):
        raise ValueError("invalid PostgreSQL deployment evidence")
    if kind == "native_portable":
        if declared is not None:
            raise ValueError("invalid PostgreSQL deployment evidence")
        return {
            "deployment_kind": kind,
            "image_digest": None,
            "image_digest_source": "not_applicable",
            "operator_declared_image_digest": None,
            "operator_declared_image_digest_source": None,
        }
    return {
        "deployment_kind": kind,
        "image_digest": None,
        "image_digest_source": "unavailable",
        "operator_declared_image_digest": declared,
        "operator_declared_image_digest_source": (
            "operator_declared_unverified" if declared is not None else None
        ),
    }


@lru_cache(maxsize=1)
def _cpu_details() -> tuple[str, int, int]:
    """Return model, physical cores and logical CPUs without an optional dependency."""
    logical = os.cpu_count() or 1
    model = platform.processor().strip() or os.environ.get("PROCESSOR_IDENTIFIER", "unknown")
    physical = logical
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "$c=Get-CimInstance Win32_Processor; "
                        "[pscustomobject]@{name=($c.Name -join '; '); "
                        "physical=($c|Measure-Object NumberOfCores -Sum).Sum; "
                        "logical=($c|Measure-Object NumberOfLogicalProcessors -Sum).Sum}"
                        "|ConvertTo-Json -Compress"
                    ),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            payload = json.loads(result.stdout)
            model = str(payload["name"]).strip() or model
            physical, logical = int(payload["physical"]), int(payload["logical"])
        elif platform.system() == "Linux":
            pairs = set()
            current: dict[str, str] = {}
            linux_model = ""
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    if "physical id" in current and "core id" in current:
                        pairs.add((current["physical id"], current["core id"]))
                    current = {}
                    continue
                if ":" in line:
                    key, value = line.split(":", 1)
                    current[key.strip()] = value.strip()
                    if key.strip() == "model name" and not linux_model:
                        linux_model = value.strip()
            if "physical id" in current and "core id" in current:
                pairs.add((current["physical id"], current["core id"]))
            physical = len(pairs) or logical
            model = linux_model or model
        elif platform.system() == "Darwin":
            physical = int(
                subprocess.run(
                    ["sysctl", "-n", "hw.physicalcpu"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                ).stdout
            )
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    return model, max(1, physical), max(1, logical)


@lru_cache(maxsize=1)
def _ram_bytes() -> int:
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            return int(result.stdout.strip())
        if platform.system() == "Darwin":
            return int(
                subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                ).stdout
            )
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 1


def runtime_environment(storage_path: Path | None = None) -> dict[str, Any]:
    repository = Path(__file__).resolve().parents[3]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unavailable"
    model, physical, logical = _cpu_details()
    disk_path = Path(storage_path or Path.cwd()).resolve()
    disk = shutil.disk_usage(disk_path.anchor or disk_path)
    return {
        "system": platform.system(),
        "system_release": platform.release(),
        "system_version": platform.version(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "cpu_count": logical,
        "cpu_model": model,
        "physical_cpu_cores": physical,
        "logical_cpu_count": logical,
        "ram_bytes": _ram_bytes(),
        "disk_path": disk_path.anchor or str(disk_path),
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
        "sqlite_version": sqlite3.sqlite_version,
        "vcp_commit": commit,
    }


def _stable_runtime_identity(environment: dict[str, Any]) -> dict[str, Any]:
    """Select exact, stable runtime/hardware fields and exclude volatile free space."""
    try:
        return {field: environment[field] for field in _RUNTIME_IDENTITY_FIELDS}
    except KeyError:
        raise ValueError("incomplete benchmark runtime identity") from None


def _source_tree_snapshot(repository: Path | None = None) -> dict[str, Any]:
    """Hash allowlisted relevant paths using their current tracked or dirty bytes."""
    repository = Path(repository or Path(__file__).resolve().parents[3]).resolve()
    paths: set[Path] = set()
    for relative in _SOURCE_TREE_DIRECTORIES:
        root = repository / relative
        if root.is_dir():
            paths.update(path for path in root.rglob("*.py") if path.is_file())
    paths.update(
        path for relative in _SOURCE_TREE_FILES if (path := repository / relative).is_file()
    )
    files = [
        {
            "path": path.relative_to(repository).as_posix(),
            "sha256": sha256_file(path),
        }
        for path in sorted(paths, key=lambda item: item.relative_to(repository).as_posix())
    ]
    return {
        "schema_version": SOURCE_TREE_SCHEMA_VERSION,
        "files": files,
        "sha256": sha256_text(_json_text(files)),
    }


def _execution_identity(environment: dict[str, Any], storage_path: Path) -> dict[str, Any]:
    return {
        "schema_version": EXECUTION_IDENTITY_SCHEMA_VERSION,
        "runtime": _stable_runtime_identity(runtime_environment(storage_path)),
        "postgresql": dict(environment),
        "source_tree": _source_tree_snapshot(),
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
def fresh_backend(method: str, data: Path, *, pg_runtime=None, owned_database=None):
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
    database = owned_database or ("vcp_bench_" + uuid4().hex)
    if not re.fullmatch(r"vcp_bench_[0-9a-f]{32}", database):
        raise ValueError("invalid owned benchmark database")

    @contextmanager
    def connect(*, admin=False):
        kwargs = {"service": service, "autocommit": True, "connect_timeout": 5}
        if not admin:
            kwargs["dbname"] = database
        with postgres.connection_lifecycle(lambda: driver.connect(**kwargs), driver) as connection:
            yield connection

    owned = False

    def cleanup():
        if owned:
            with connect(admin=True) as connection:
                connection.execute(
                    driver.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        driver.sql.Identifier(database)
                    )
                )

    with postgres._database_operation(driver, cleanup):
        with connect(admin=True) as connection:
            connection.execute(
                driver.sql.SQL("CREATE DATABASE {}").format(driver.sql.Identifier(database))
            )
            owned = True
        backend = postgres.PostgresProvenanceBackend(BackendConfig(BackendName.POSTGRESQL, service))
        original = postgres._connection
        with connect() as connection:
            major, fingerprint = postgres.maintenance_environment(connection)
            version = connection.info.server_version
            server_version = connection.execute("SHOW server_version").fetchone()[0]

        deployment = _deployment_evidence()

        # This test-only scope routes only this backend's identity to its owned database.
        def backend_connection(config, driver):
            return connect() if config is backend.config else original(config, driver)

        with patch.object(postgres, "_connection", backend_connection):
            yield BackendState(
                backend,
                connect,
                {
                    "postgresql_major": major,
                    "postgresql_version": version,
                    "postgresql_server_version": server_version,
                    **deployment,
                    "environment_fingerprint": fingerprint,
                    "backend_schema_version": postgres.POSTGRES_SCHEMA_VERSION,
                },
            )


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
            validate_raw_explain(raw)
            self.plans.extend(raw)

    def execute(self, query, params=()):
        self.probe(query, params)
        return self.raw.execute(query, params)

    @contextmanager
    def cursor(self, *args, **kwargs):
        with self.raw.cursor(*args, **kwargs) as cursor:
            owner = self

            class Cursor:
                def __getattr__(self, name):
                    return getattr(cursor, name)

                def execute(self, query, params=()):
                    owner.probe(query, params)
                    return cursor.execute(query, params)

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
    from vcp.provenance import postgres

    observed = _ExplainConnection(connection)
    with postgres._database_operation(postgres._load_psycopg(), connection.rollback):
        connection.execute("BEGIN")
        action(observed)
        if not observed.plans:
            raise RuntimeError("PostgreSQL maintenance produced no instrumentation plans")
        return observed.plans


def _timed(action):
    started = perf_counter_ns()
    result = action()
    return result, (perf_counter_ns() - started) / 1_000_000


def _maintain(state, method, workload, data, configs, policy_id):
    if method == "canonical_full":
        graph = build_graph(data, configs)
        # Status computation remains part of the canonical timed maintenance path,
        # but individual maps are released instead of retaining an O(heads*entities)
        # cache until parity validation.
        for head in _all_heads(graph):
            compute_statuses(graph, head)
        return graph, graph_hash(graph)
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


def _ordered_graph_records(graph):
    """Yield every normalized field in canonical order without constructing a copy."""
    for name, records in (
        ("entities", graph.entities),
        ("edges", graph.edges),
        ("changes", graph.changes),
    ):
        for key in sorted(records):
            yield name, key, records[key].model_dump(mode="json")
    for (old, new), change_ids in sorted(graph.transitions.items()):
        yield "transitions", (old, new), tuple(sorted(change_ids))
    for gap in sorted(graph.gaps):
        yield "gaps", gap, None


def graphs_equal_exact(left, right) -> bool:
    """Compare exact records one at a time; hashes never substitute for parity."""
    missing = object()
    return all(
        left_record == right_record
        for left_record, right_record in zip_longest(
            _ordered_graph_records(left), _ordered_graph_records(right), fillvalue=missing
        )
    )


def _materialized_status_heads(state, method, actual_status_heads) -> set[str]:
    if method == "canonical_full":
        return set(actual_status_heads)
    if getattr(state.backend, "index", None) is not None:
        connection = state.backend.index._open()
        try:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT head_id FROM entity_status ORDER BY head_id"
                )
            }
        finally:
            connection.close()
    with state.connect() as connection:
        rows = connection.execute(
            "SELECT DISTINCT s.head_id FROM vcp_provenance.entity_status s "
            "JOIN vcp_provenance.active_generation a ON a.singleton=TRUE "
            "AND a.generation_id=s.generation_id ORDER BY s.head_id"
        )
        return {str(row[0]) for row in rows}


def _storage(state, method) -> dict[str, int | str | None]:
    if method == "canonical_full":
        return {
            "database_bytes": 0,
            "provenance_total_relation_bytes": None,
            "index_bytes": None,
            "storage_measurement": "no persistent database; Python heap not measured",
        }
    if method.startswith("sqlite_"):
        path = state.backend.index.path
        paths = [path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")]
        return {
            "database_bytes": sum(p.stat().st_size for p in paths if p.exists()),
            "provenance_total_relation_bytes": None,
            "index_bytes": None,
            "storage_measurement": "SQLite file bytes including WAL/SHM",
        }
    with state.connect() as connection:
        database_bytes = connection.execute(
            "SELECT pg_database_size(current_database())"
        ).fetchone()[0]
        relation_bytes = connection.execute(
            "SELECT coalesce(sum(pg_total_relation_size(c.oid)),0)::bigint "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='vcp_provenance' AND c.relkind='r'"
        ).fetchone()[0]
        index_bytes = connection.execute(
            "SELECT coalesce(sum(pg_indexes_size(c.oid)),0)::bigint "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='vcp_provenance' AND c.relkind='r'"
        ).fetchone()[0]
    return {
        "database_bytes": int(database_bytes),
        "provenance_total_relation_bytes": int(relation_bytes),
        "index_bytes": int(index_bytes),
        "storage_measurement": (
            "PostgreSQL pg_database_size plus vcp_provenance total relation and index bytes"
        ),
    }


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
    policy_id: str | None = None
    policy_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        validate_raw_explain(self.plans)
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
            "explain_analyze_sanitized": sanitize_explain(self.plans),
            "explain_scope": "first executed DML row for each SQL statement shape",
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
            "provenance_total_relation_bytes",
            "index_bytes",
        ):
            result[key] = first.get(key)
        result["policy_id"] = self.policy_id
        result["policy_sha256"] = self.policy_sha256
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
    workload: Workload,
    method: str,
    *,
    repetitions=None,
    pg_runtime=None,
    policy_id=None,
    policy_sha256=None,
    capture_explain=True,
    owned_database=None,
) -> ScenarioResult:
    if method not in METHODS:
        raise ValueError("unknown benchmark method")
    repetitions = workload.scenario.repetitions if repetitions is None else repetitions
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    if method == "postgres_adaptive" and not policy_id:
        raise ValueError("postgres_adaptive requires a frozen policy")
    result = ScenarioResult(
        method,
        workload,
        runtime_environment(workload.data),
        policy_id=policy_id if method == "postgres_adaptive" else None,
        policy_sha256=policy_sha256 if method == "postgres_adaptive" else None,
    )
    expected, expected_hash = workload.expected, graph_hash(workload.expected)
    expected_heads = _all_heads(expected)
    dirty = _closure(workload)
    phase = "benchmark_operation_failed"
    try:
        for _ in range(repetitions):
            with tempfile.TemporaryDirectory(prefix="vcp-adaptive-sample-") as temporary:
                data, configs = workload.clone(Path(temporary))
                with fresh_backend(
                    method, data, pg_runtime=pg_runtime, owned_database=owned_database
                ) as state:
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
                    recorded_hash = (
                        outcome[1] if state.backend is None else state.backend.stats()["graph_hash"]
                    )
                    status_parity = True
                    actual_status_heads = []
                    for head in expected_heads:
                        expected_status = compute_statuses(expected, head)
                        actual_status = status(head)
                        actual_status_heads.append(head)
                        if actual_status != expected_status:
                            status_parity = False
                        del actual_status, expected_status
                    materialized_heads = _materialized_status_heads(
                        state, method, actual_status_heads
                    )
                    storage = _storage(state, method)
                    if hasattr(state.backend, "assert_graph_parity"):
                        state.backend.assert_graph_parity(expected)
                        graph_parity = True
                        graph_hash_parity = recorded_hash == expected_hash
                        head_parity = materialized_heads == set(expected_heads)
                    else:
                        actual = outcome[0] if state.backend is None else load()
                        graph_parity = graphs_equal_exact(actual, expected)
                        graph_hash_parity = recorded_hash == graph_hash(actual) == expected_hash
                        head_parity = dataset_heads(actual) == dataset_heads(
                            expected
                        ) and materialized_heads == set(expected_heads)
                    row = {
                        "maintenance_ms": maintenance_ms,
                        "status_ms": status_ms,
                        "impact_ms": impact_ms,
                        "explain_ms": explain_ms,
                        "graph_parity": graph_parity,
                        "graph_hash_parity": graph_hash_parity,
                        "status_parity": status_parity,
                        "head_parity": head_parity,
                        "graph_hash": recorded_hash,
                        **storage,
                        **_decision(outcome, method, workload, dirty),
                    }
                    result.samples.append(row)
                    if not all(row[key] for key in PARITY_FIELDS):
                        raise RuntimeError("benchmark parity failed")
        if method.startswith("postgres_") and capture_explain:
            phase = "benchmark_instrumentation_failed"
            with tempfile.TemporaryDirectory(prefix="vcp-adaptive-explain-") as temporary:
                data, configs = workload.clone(Path(temporary))
                with fresh_backend(
                    method, data, pg_runtime=pg_runtime, owned_database=owned_database
                ) as state:
                    state.backend.rebuild(data, configs)
                    workload.publish(data)
                    from vcp.provenance import postgres

                    def instrument(observed):
                        with patch.object(
                            postgres,
                            "_connection",
                            lambda config, driver: postgres.connection_lifecycle(
                                lambda: observed, driver
                            ),
                        ):
                            _maintain(state, method, workload, data, configs, policy_id)

                    with state.connect() as connection:
                        result.plans = capture_explain_rollback(connection, method, instrument)
    except Exception:
        # Never stringify third-party errors, including errors during teardown.
        result.failure = phase
    return result


def run_explain_only(
    workload: Workload,
    method: str,
    *,
    pg_runtime=None,
    policy_id=None,
    owned_database=None,
) -> dict[str, Any]:
    """Capture PostgreSQL DML-shape plans on fresh state without a timed repetition."""
    if not method.startswith("postgres_"):
        raise ValueError("EXPLAIN instrumentation requires a PostgreSQL method")
    plans: list[dict[str, Any]] = []
    failure = None
    environment = runtime_environment(workload.data)
    try:
        with tempfile.TemporaryDirectory(prefix="vcp-adaptive-explain-") as temporary:
            data, configs = workload.clone(Path(temporary))
            with fresh_backend(
                method, data, pg_runtime=pg_runtime, owned_database=owned_database
            ) as state:
                environment.update(state.environment)
                state.backend.rebuild(data, configs)
                workload.publish(data)
                from vcp.provenance import postgres

                def instrument(observed):
                    with patch.object(
                        postgres,
                        "_connection",
                        lambda config, driver: postgres.connection_lifecycle(
                            lambda: observed, driver
                        ),
                    ):
                        _maintain(state, method, workload, data, configs, policy_id)

                with state.connect() as connection:
                    plans = capture_explain_rollback(connection, method, instrument)
    except Exception:
        failure = "benchmark_instrumentation_failed"
    validate_raw_explain(plans)
    return {
        "status": "ok" if failure is None else "failed",
        "failure": failure,
        "plans": plans,
        "environment": environment,
    }


def _owned_database(contract_sha256: str, key: SpoolKey) -> str:
    digest = sha256_text(contract_sha256 + _json_text(asdict(key)))[:32]
    return "vcp_bench_" + digest


def _merge_repetition_rows(rows, explain_record):
    """Reconstruct the historical aggregate schema in deterministic repetition order."""
    if not rows:
        raise ValueError("missing benchmark repetitions")
    merged = copy.deepcopy(rows[0])
    samples = [sample for row in rows for sample in row.get("samples", [])]
    failed = any(row.get("status") != "ok" for row in rows)
    if explain_record is not None and explain_record.get("status") != "ok":
        failed = True
        merged["failure"] = "benchmark_instrumentation_failed"
    if failed:
        samples = []
        merged["status"] = "failed"
        merged["failure"] = merged.get("failure") or "benchmark_operation_failed"
    else:
        merged["status"] = "ok"
        merged["failure"] = None
    merged["samples"] = samples
    merged["repetitions"] = len(rows)
    plans = [] if explain_record is None else explain_record.get("plans", [])
    validate_raw_explain(plans)
    merged["explain_analyze"] = plans
    merged["explain_analyze_sanitized"] = sanitize_explain(plans)
    for key in PARITY_FIELDS:
        merged[key] = bool(samples) and all(sample[key] for sample in samples)
    merged["parity_rate"] = float(bool(samples) and all(merged[key] for key in PARITY_FIELDS))
    for operation in OPERATIONS:
        values = sorted(sample[f"{operation}_ms"] for sample in samples)
        merged[f"{operation}_p50_ms"] = statistics.median(values) if values else None
        merged[f"{operation}_p95_ms"] = values[round(0.95 * (len(values) - 1))] if values else None
    first = samples[0] if samples else {}
    for key in (
        *DECISION_FIELDS,
        "dirty_entities",
        "database_bytes",
        "storage_measurement",
        "graph_hash",
        "provenance_total_relation_bytes",
        "index_bytes",
    ):
        merged[key] = first.get(key)
    merged["dirty_ratio"] = (
        first.get("dirty_entities", 0) / merged["total_entities"] if samples else None
    )
    maintenance = merged["maintenance_p50_ms"]
    merged["throughput_samples_per_second"] = (
        merged["changed_samples"] / (maintenance / 1000)
        if maintenance
        else (0 if merged["status"] == "ok" else None)
    )
    return merged


def _checkpoint_contract(
    work_dir: Path,
    scenarios,
    methods,
    repetitions,
    policy_sha256,
    environment,
):
    matrix = []
    for scenario in scenarios:
        matrix.append(
            {
                **asdict(scenario),
                "change_ratio": float(scenario.change_ratio),
                "scenario_id": scenario.scenario_id,
                "scenario_hash": scenario.scenario_hash,
                "repetitions": scenario.repetitions if repetitions is None else repetitions,
            }
        )
    execution_identity = _execution_identity(environment, Path(work_dir))
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "benchmark_schema_version": BENCHMARK_SCHEMA_VERSION,
        "execution_id": sha256_text(str(Path(work_dir).resolve()))[:32],
        "matrix": matrix,
        "methods": list(methods),
        "policy_sha256": policy_sha256,
        "environment_fingerprint": environment["environment_fingerprint"],
        "postgresql_major": environment["postgresql_major"],
        "backend_schema_version": environment["backend_schema_version"],
        "vcp_commit": execution_identity["runtime"]["vcp_commit"],
        "execution_identity": execution_identity,
        "execution_identity_sha256": sha256_text(_json_text(execution_identity)),
    }


def _checkpoint_environment(root: Path, pg_runtime):
    root.mkdir(parents=True, exist_ok=True)
    with fresh_backend("postgres_full", root, pg_runtime=pg_runtime) as state:
        return state.environment


def _drop_owned_database(pg_runtime, database: str) -> None:
    driver, service = pg_runtime
    if not re.fullmatch(r"vcp_bench_[0-9a-f]{32}", database):
        raise ValueError("invalid owned benchmark database")
    from vcp.provenance import postgres

    with postgres.connection_lifecycle(
        lambda: driver.connect(service=service, autocommit=True, connect_timeout=5), driver
    ) as connection:
        connection.execute(
            driver.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                driver.sql.Identifier(database)
            )
        )


def _run_scenario_child(
    store: CheckpointStore,
    scenario_hash: str,
    *,
    policy_from: Path | None,
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_child-work-dir",
        str(store.root),
        "--_child-scenario-hash",
        scenario_hash,
    ]
    if policy_from is not None:
        command.extend(["--_child-policy-from", str(Path(policy_from).resolve())])
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _scenario_from_contract(row):
    return Scenario(
        entities=row["entities"],
        change_ratio=row["change_ratio"],
        topology=row["topology"],
        seed=row["seed"],
        track=row["track"],
        variant=row["variant"],
    )


def _load_contract(path: Path):
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys
        )
    except (OSError, json.JSONDecodeError, ValueError):
        raise ValueError("invalid checkpoint contract") from None
    if not isinstance(value, dict):
        raise ValueError("invalid checkpoint contract")
    return value


def _child_run_scenario(work_dir: Path, scenario_hash: str, policy_from: Path | None) -> int:
    contract = _load_contract(Path(work_dir) / "checkpoint.json")
    store = CheckpointStore(work_dir, contract)
    store.initialize()
    matches = [row for row in contract["matrix"] if row["scenario_hash"] == scenario_hash]
    if len(matches) != 1:
        raise ValueError("invalid child scenario")
    scenario = _scenario_from_contract(matches[0])
    policy = evidence = None
    policy_sha256 = None
    if policy_from is not None:
        policy, evidence, policy_sha256 = load_frozen_policy(policy_from)
    if policy_sha256 != contract["policy_sha256"]:
        raise ValueError("checkpoint policy mismatch")
    pg_runtime = postgres_preflight()
    current_environment = _checkpoint_environment(Path(work_dir) / ".child-preflight", pg_runtime)
    current_identity = _execution_identity(current_environment, Path(work_dir))
    if current_identity != contract.get("execution_identity") or sha256_text(
        _json_text(current_identity)
    ) != contract.get("execution_identity_sha256"):
        raise ValueError("checkpoint execution identity mismatch")
    runtime_root = store.root / ".runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="scenario-", dir=runtime_root) as temporary:
        workload = build_scenario(Path(temporary) / scenario.scenario_id, scenario)
        if policy is not None:
            workload = prepare_policy_workload(workload, policy, evidence)
        repetitions = matches[0]["repetitions"]
        for method in contract["methods"]:
            for repetition_index in range(repetitions):
                key = SpoolKey(scenario_hash, method, "sample", repetition_index)
                if store.read(key) is not None:
                    continue
                database = _owned_database(store.contract_sha256, key)
                row = run_method(
                    workload,
                    method,
                    repetitions=1,
                    pg_runtime=pg_runtime,
                    policy_id=policy.id if policy and method == "postgres_adaptive" else None,
                    policy_sha256=(
                        policy_sha256 if policy and method == "postgres_adaptive" else None
                    ),
                    capture_explain=False,
                    owned_database=database,
                ).to_dict()
                store.write(key, row)
        explain_workload = replace(workload, expected=ProvenanceGraph())
        del workload
        gc.collect()
        for method in contract["methods"]:
            if method.startswith("postgres_"):
                key = SpoolKey(scenario_hash, method, "explain", 0)
                if store.read(key) is None:
                    database = _owned_database(store.contract_sha256, key)
                    explained = run_explain_only(
                        explain_workload,
                        method,
                        pg_runtime=pg_runtime,
                        policy_id=(policy.id if policy and method == "postgres_adaptive" else None),
                        owned_database=database,
                    )
                    store.write(key, explained)
    return 0


def run_matrix_isolated(
    work_dir: Path,
    scenarios,
    *,
    methods=METHODS,
    repetitions=None,
    pg_runtime=None,
    policy_from=None,
    policy_sha256=None,
):
    """Run one child per scenario and resume only verified, pinned repetitions."""
    scenarios = list(scenarios)
    pg_runtime = pg_runtime or postgres_preflight()
    environment = _checkpoint_environment(Path(work_dir) / ".preflight", pg_runtime)
    contract = _checkpoint_contract(
        work_dir, scenarios, methods, repetitions, policy_sha256, environment
    )
    store = CheckpointStore(work_dir, contract)
    store.initialize()
    for scenario in scenarios:
        completed = False
        for _attempt in range(2):
            process = _run_scenario_child(store, scenario.scenario_hash, policy_from=policy_from)
            if process.returncode == 0:
                completed = True
                break
            for method in methods:
                count = scenario.repetitions if repetitions is None else repetitions
                keys = [SpoolKey(scenario.scenario_hash, method, "sample", i) for i in range(count)]
                if method.startswith("postgres_"):
                    keys.append(SpoolKey(scenario.scenario_hash, method, "explain", 0))
                for key in keys:
                    if store.read(key) is None:
                        database = _owned_database(store.contract_sha256, key)
                        _drop_owned_database(pg_runtime, database)
        if not completed:
            raise RuntimeError("benchmark scenario child failed")
    rows = []
    for scenario in scenarios:
        count = scenario.repetitions if repetitions is None else repetitions
        for method in methods:
            repetitions_rows = [
                store.read(SpoolKey(scenario.scenario_hash, method, "sample", index))
                for index in range(count)
            ]
            if any(row is None for row in repetitions_rows):
                raise ValueError("missing benchmark checkpoint")
            explain = (
                store.read(SpoolKey(scenario.scenario_hash, method, "explain", 0))
                if method.startswith("postgres_")
                else None
            )
            rows.append(_merge_repetition_rows(repetitions_rows, explain))
    return rows


def run_matrix(
    root: Path,
    scenarios,
    *,
    repetitions=None,
    pg_runtime=None,
    policy=None,
    evidence=None,
    policy_sha256=None,
):
    if policy is None or evidence is None or policy_sha256 is None:
        raise ValueError("six-method benchmark requires a frozen policy")
    rows = []
    for scenario in scenarios:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="vcp-scenario-", dir=root) as temporary:
            workload = build_scenario(Path(temporary) / scenario.scenario_id, scenario)
            workload = prepare_policy_workload(workload, policy, evidence)
            for method in METHODS:
                rows.append(
                    run_method(
                        workload,
                        method,
                        repetitions=repetitions,
                        pg_runtime=pg_runtime,
                        policy_id=policy.id if method == "postgres_adaptive" else None,
                        policy_sha256=(policy_sha256 if method == "postgres_adaptive" else None),
                    ).to_dict()
                )
    return rows


def prepare_policy_workload(workload, policy, evidence):
    if __package__:
        from .evaluate_adaptive import prepare_workload
    else:
        from evaluate_adaptive import prepare_workload
    return prepare_workload(workload, policy, evidence)


def load_frozen_policy(path: Path):
    if __package__:
        from .evaluate_adaptive import load_calibration, policy_file_sha256
    else:
        from evaluate_adaptive import load_calibration, policy_file_sha256
    policy, evidence = load_calibration(path)
    return policy, evidence, policy_file_sha256(path, policy)


def empirical_crossover(evidence):
    if __package__:
        from .evaluate_adaptive import empirical_crossover as build_crossover
    else:
        from evaluate_adaptive import empirical_crossover as build_crossover
    return build_crossover(evidence)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entities", type=int, nargs="+", default=list(SCALES))
    parser.add_argument("--ratios", type=float, nargs="+", default=list(RATIOS))
    parser.add_argument("--seeds", type=int, nargs="+", default=[20260913, 20260914])
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--policy-from", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--_child-work-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_child-scenario-hash", help=argparse.SUPPRESS)
    parser.add_argument("--_child-policy-from", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args._child_work_dir is not None:
            if args._child_scenario_hash is None or args.output is not None:
                raise ValueError("invalid benchmark child invocation")
            return _child_run_scenario(
                args._child_work_dir,
                args._child_scenario_hash,
                args._child_policy_from,
            )
        if args.output is None:
            raise ValueError("benchmark output is required")
        if args.output.exists():
            raise ValueError("benchmark output is write-once")
        pg_runtime = postgres_preflight()
        if args.policy_from is None:
            raise ValueError("six-method benchmark requires a frozen policy")
        policy, evidence, policy_sha256 = load_frozen_policy(args.policy_from)
        scenarios = scenario_matrix(seeds=args.seeds, entities=args.entities, ratios=args.ratios)
        work_dir = args.work_dir or args.output.with_name(args.output.stem + "-work")
        rows = run_matrix_isolated(
            work_dir,
            scenarios,
            repetitions=args.repetitions,
            pg_runtime=pg_runtime,
            policy_from=args.policy_from,
            policy_sha256=policy_sha256,
        )
        document = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "created_at": stamp(),
            "kind": "adaptive-provenance-six-method-benchmark",
            "methods": list(METHODS),
            "policy_id": policy.id,
            "policy_sha256": policy_sha256,
            "empirical_crossover": empirical_crossover(evidence),
            "environment": next(
                (
                    row["environment"]
                    for row in rows
                    if row["method"].startswith("postgres_") and row["status"] == "ok"
                ),
                None,
            ),
            "results": rows,
        }
        validate_publication_explain(document)
        write_once_text(
            args.output,
            json.dumps(document, indent=2) + "\n",
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
