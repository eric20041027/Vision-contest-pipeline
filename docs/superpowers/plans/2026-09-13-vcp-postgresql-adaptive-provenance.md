# VCP PostgreSQL Adaptive Provenance Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional PostgreSQL provenance backend and a calibrated, deterministic adaptive maintenance selector while preserving SQLite as the default embedded baseline and canonical VCP files as the source of truth.

**Architecture:** Keep `ProvenanceIndex` intact behind a thin `SQLiteBackend` adapter, define one shared backend protocol/factory, and implement PostgreSQL with generation-based atomic publication. Reuse `build_graph()`, `compute_statuses()` and verified `dataset_diff` artifacts; adaptive policy selects only the maintenance algorithm and can never change provenance semantics.

**Tech Stack:** Python 3.12, pydantic v2, typer, SQLite, PostgreSQL 17.x, optional Psycopg 3, pytest, Docker Compose, numpy/scikit-learn, ruff.

**Spec:** `docs/superpowers/specs/2026-09-13-vcp-postgresql-adaptive-provenance-design.md`

## Global Constraints

- Begin execution from `a625331` or a reviewed descendant in a fresh `codex/postgresql-adaptive-provenance` worktree; never reset or overwrite the dirty `codex/vcp-visual-guide` checkout.
- This plan is documentation only. Python, SQL, CLI, dependency, benchmark and Compose changes begin only after separate implementation authorization.
- JSONL/YAML, dataset manifests, source audits, dataset diffs, append-only ledgers and immutable artifacts remain canonical; both databases remain disposable indexes.
- `ProvenanceIndex` remains the public SQLite implementation and default behavior; no-backend-option CLI calls must remain compatible.
- PostgreSQL is optional and lazy-imported. Base installs without Psycopg must import and run SQLite normally.
- No VCP CLI accepts a password, full DSN or credential-bearing URI; no output/log/exception/artifact may contain connection secrets or conninfo.
- Writer operations use transaction-scoped PostgreSQL advisory lock and rollback completely on every failure.
- PostgreSQL full publication, incremental update, checkpoints, statuses, artifact ingest record and decision record commit atomically.
- Canonical full replay is the correctness oracle; graph, graph hash, statuses and dataset heads require exact parity.
- All timestamps use `vcp.core.time.utc_now()` or `stamp()`; elapsed durations use `perf_counter_ns()`.
- Every behavior change starts with a failing test. Every task gets a scoped review before the next dependent task.
- Never run large 100K/1M benchmarks in ordinary CI. Never include RSNA-specific code in `src/vcp`.
- Every CLI path ends in a VCP VERDICT; string option parsing occurs inside `run_command()`, not Typer callbacks.

## Planned File Map

| File | Responsibility |
|---|---|
| `src/vcp/provenance/backend.py` | backend contract, config, SQLite adapter, lazy factory, shared operation report |
| `src/vcp/provenance/postgres_schema.py` | PostgreSQL schema v1 DDL, indexes, constraints and validation |
| `src/vcp/provenance/postgres.py` | secure connection, generation publication, full/incremental/query/verify |
| `src/vcp/provenance/strategy.py` | feature/decision/policy models, selector, immutable policy artifact |
| `src/vcp/provenance/index.py` | existing SQLite code; only compatibility exports if required |
| `src/vcp/cli_provenance.py` | shared backend construction and PostgreSQL strategy flags/output |
| `tests/unit/provenance/test_backend.py` | contract/default/lazy-import compatibility |
| `tests/unit/provenance/test_postgres_connection.py` | dependency and credential-safe connection failures |
| `tests/unit/provenance/test_postgres_schema.py` | complete DDL and invariant assertions |
| `tests/unit/provenance/test_postgres_rows.py` | domain row serialization and read-back |
| `tests/unit/provenance/test_strategy.py` | deterministic selector and policy artifact |
| `tests/integration/provenance/test_postgres_backend.py` | real PostgreSQL correctness, rollback and concurrency |
| `tests/integration/postgres/compose.yaml` | localhost-only PostgreSQL 17.11 test service |
| `tests/integration/postgres/run.ps1` | secret-safe local lifecycle and pytest runner |
| `tests/performance/provenance/workloads.py` | shared scaled topology/change generator |
| `tests/performance/provenance/adaptive_benchmark.py` | six-method matrix and rollback-safe EXPLAIN |
| `tests/performance/provenance/calibrate_adaptive.py` | calibration-only cost-model fitting and policy publication |
| `tests/performance/provenance/evaluate_adaptive.py` | frozen-policy held-out evaluation |

---

### Task 1: Backend Contract and SQLite Compatibility Adapter

**Files:**
- Create: `src/vcp/provenance/backend.py`
- Create: `tests/unit/provenance/test_backend.py`
- Modify only if import compatibility requires it: `src/vcp/provenance/index.py:29-55`

**Interfaces:**
- Consumes: `ProvenanceIndex`, `IngestResult`, `RebuildResult`, `VerifyIndexResult`, `ProvenanceGraph`, `StatusRecord`.
- Produces: `BackendName`, `BackendConfig`, `MaintenanceResult`, `ProvenanceBackend`, `SQLiteBackend`, `parse_backend()`, `make_backend()`.

- [ ] **Step 1: Write the failing contract/default tests**

```python
def test_default_factory_wraps_existing_sqlite_without_importing_psycopg(roots, monkeypatch):
    imported = []
    monkeypatch.setattr("vcp.provenance.backend.import_module", lambda name: imported.append(name))
    backend = make_backend(BackendConfig(), roots.data)
    assert backend.name is BackendName.SQLITE
    assert backend.location_label == str(provenance_index_path(roots.data))
    assert imported == []


def test_sqlite_adapter_preserves_normalized_results(roots):
    direct = ProvenanceIndex(provenance_index_path(roots.data))
    adapter = SQLiteBackend(direct)
    direct.rebuild(roots.data, roots.configs)
    assert adapter.normalized() == direct.normalized()
```

- [ ] **Step 2: Run the tests and confirm the red state**

Run: `uv run pytest tests/unit/provenance/test_backend.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'vcp.provenance.backend'`.

- [ ] **Step 3: Add the minimal contract and adapter**

```python
class BackendName(StrEnum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


@dataclass(frozen=True)
class BackendConfig:
    name: BackendName = BackendName.SQLITE
    pg_service: str | None = None


@dataclass(frozen=True)
class MaintenanceResult:
    artifact_id: str
    inserted: bool
    backend: str
    requested_strategy: str
    selected_strategy: str
    strategy_reason: str
    changed_samples: int
    dirty_entities: int
    total_entities: int
    dirty_ratio: float
    estimated_incremental_ms: float | None
    estimated_full_ms: float | None
    policy_version: str
    elapsed_ms: float
    graph_hash: str
```

Implement every protocol method as an exact delegate in `SQLiteBackend`. Its `ingest_diff()` accepts only
`requested_strategy="incremental"`, loads the verified diff for `changed_samples`, measures the existing
`ProvenanceIndex.ingest_diff()`, and reports `NO_OP` only for a duplicate or zero-change diff. Keep the original
`ProvenanceIndex` class and result dataclasses importable from `index.py`.

- [ ] **Step 4: Verify target and legacy behavior**

Run: `uv run pytest tests/unit/provenance/test_backend.py tests/unit/provenance/test_index.py tests/unit/provenance/test_cli_provenance.py -q`

Expected: all tests pass; no test imports Psycopg; direct `ProvenanceIndex` behavior is unchanged.

- [ ] **Step 5: Lint, review and commit**

Run: `uv run ruff check src/vcp/provenance/backend.py tests/unit/provenance/test_backend.py && uv run ruff format --check src/vcp/provenance/backend.py tests/unit/provenance/test_backend.py`

Commit: `feat(provenance): add backend contract`

---

### Task 2: Optional Psycopg Dependency and Secret-Safe Connection Boundary

**Files:**
- Create: `src/vcp/provenance/postgres.py`
- Create: `tests/unit/provenance/test_postgres_connection.py`
- Modify: `src/vcp/provenance/backend.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/unit/test_package.py`

**Interfaces:**
- Consumes: `BackendConfig`, `BackendName`, `make_backend()`.
- Produces: `validate_pg_service(value) -> str | None`, `_load_psycopg()`, `_connect(config)`, `PostgresProvenanceBackend` constructor.

- [ ] **Step 1: Write missing-dependency, service allowlist and redaction tests**

```python
def test_postgres_import_is_lazy_and_missing_extra_is_actionable(monkeypatch, roots):
    def missing_driver(name):
        raise ImportError("no psycopg")

    monkeypatch.setattr("vcp.provenance.backend.import_module", missing_driver)
    with pytest.raises(ValidationFailed, match="uv sync --extra postgres"):
        make_backend(BackendConfig(BackendName.POSTGRESQL), roots.data)


@pytest.mark.parametrize("value", ["postgresql://u:secret@host/db", "password=secret", "bad service"])
def test_service_rejects_dsn_or_whitespace(value):
    with pytest.raises(ValidationFailed, match="invalid_postgres_service"):
        validate_pg_service(value)


def test_driver_error_never_exposes_secret(monkeypatch):
    error = fake_psycopg_error("password=TOPSECRET host=private", sqlstate="08001")
    with pytest.raises(ValidationFailed) as caught:
        raise_redacted_database_error(error)
    assert "TOPSECRET" not in str(caught.value)
    assert caught.value.fields == {"backend": "postgresql", "sqlstate": "08001"}
```

- [ ] **Step 2: Confirm failures**

Run: `uv run pytest tests/unit/provenance/test_postgres_connection.py tests/unit/test_package.py -q`

Expected: missing module/functions and missing optional-extra pin failures.

- [ ] **Step 3: Add the optional extra and lazy connection code**

Add exactly `postgres = ["psycopg[binary]>=3.2,<4"]` under optional dependencies and the identical pin to
the dev group, then run `uv lock`. `_connect()` calls `psycopg.connect(service=name, autocommit=True)` when a
validated service is provided and `psycopg.connect(autocommit=True)` otherwise. Import Psycopg only inside
the PostgreSQL factory path. Convert driver errors to a fixed message plus SQLSTATE; never include `str(error)`.

- [ ] **Step 4: Verify optional behavior**

Run: `uv run pytest tests/unit/provenance/test_postgres_connection.py tests/unit/test_package.py tests/unit/provenance/test_backend.py -q`

Expected: all pass; a subprocess with the base wheel and no postgres extra can `import vcp` and instantiate SQLite.

- [ ] **Step 5: Lint, review and commit**

Run: `uv run ruff check src/vcp/provenance/postgres.py src/vcp/provenance/backend.py tests/unit/provenance/test_postgres_connection.py && uv run ruff format --check src/vcp/provenance/postgres.py src/vcp/provenance/backend.py tests/unit/provenance/test_postgres_connection.py`

Commit: `feat(provenance): add optional postgres connection`

---

### Task 3: PostgreSQL Schema v1

**Files:**
- Create: `src/vcp/provenance/postgres_schema.py`
- Create: `tests/unit/provenance/test_postgres_schema.py`
- Modify: `src/vcp/provenance/postgres.py`

**Interfaces:**
- Produces: `POSTGRES_SCHEMA_VERSION = 1`, `SCHEMA_NAME = "vcp_provenance"`, `install_schema(connection)`, `validate_schema(connection)`.
- Tables: `generations`, `active_generation`, `metadata`, `entities`, `provenance_edges`, `dataset_edges`, `sample_changes`, `dataset_edge_changes`, `entity_status`, `ingest_checkpoints`, `ingested_artifacts`, `maintenance_decisions`.

- [ ] **Step 1: Write complete DDL inventory tests**

```python
def test_schema_declares_every_normalized_table_and_no_trigger():
    sql = "\n".join(POSTGRES_DDL)
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS vcp_provenance.{table}" in sql
    assert "JSONB" in sql
    assert "change_ids_json" not in sql
    assert "CREATE TRIGGER" not in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql


def test_schema_has_forward_reverse_status_and_artifact_indexes():
    sql = "\n".join(POSTGRES_DDL)
    for name in ("edges_source", "edges_target", "status_head_state", "changes_sample"):
        assert f"CREATE INDEX IF NOT EXISTS {name}" in sql
```

Also assert every status/change/edge vocabulary CHECK, all hash checks, nonnegative counts, active singleton,
generation foreign keys and both dataset-edge/change join foreign keys.

- [ ] **Step 2: Confirm failures**

Run: `uv run pytest tests/unit/provenance/test_postgres_schema.py -q`

Expected: `ModuleNotFoundError: vcp.provenance.postgres_schema`.

- [ ] **Step 3: Implement the exact schema from design spec section 7**

Use fixed schema-qualified identifiers. `install_schema()` runs the DDL in one transaction and inserts only
the schema version marker. `validate_schema()` reads the installed version and raises
`IntegrityError("mismatch: PostgreSQL provenance schema version")` on absence/mismatch. Do not add triggers,
server-generated timestamps, credential columns or generic event JSON.

- [ ] **Step 4: Verify schema contract**

Run: `uv run pytest tests/unit/provenance/test_postgres_schema.py tests/unit/provenance/test_postgres_connection.py -q`

Expected: all schema inventory, constraint and install-order assertions pass.

- [ ] **Step 5: Lint, review and commit**

Run: `uv run ruff check src/vcp/provenance/postgres_schema.py tests/unit/provenance/test_postgres_schema.py && uv run ruff format --check src/vcp/provenance/postgres_schema.py tests/unit/provenance/test_postgres_schema.py`

Commit: `feat(provenance): define postgres schema`

---

### Task 4: PostgreSQL Full Rebuild and Read/Query Parity

**Files:**
- Create: `tests/unit/provenance/test_postgres_rows.py`
- Modify: `src/vcp/provenance/postgres.py`
- Modify: `src/vcp/provenance/backend.py`

**Interfaces:**
- Consumes: `build_graph()`, `compute_statuses()`, `graph_hash()`, `dataset_heads()`.
- Produces: PostgreSQL `rebuild()`, `sync()`, `load_graph()`, `statuses()`, `normalized()`, `stats()`, `verify()`.

- [ ] **Step 1: Write row round-trip and atomic-publication order tests**

```python
def test_graph_rows_round_trip_exactly(tiny_graph):
    rows = serialize_graph(tiny_graph, generation_id=GENERATION)
    assert deserialize_graph(rows).normalized() == tiny_graph.normalized()


def test_rebuild_publishes_active_generation_last(fake_postgres, roots):
    backend = PostgresProvenanceBackend(fake_postgres.config)
    backend.rebuild(roots.data, roots.configs)
    assert fake_postgres.events.index("verify_status_parity") < fake_postgres.events.index("publish_active")
    assert fake_postgres.events[-1] == "commit"
```

Add a failure injection assertion: error before `publish_active` rolls back and leaves the prior generation id
and normalized result unchanged.

- [ ] **Step 2: Confirm failures**

Run: `uv run pytest tests/unit/provenance/test_postgres_rows.py -q`

Expected: imports or missing `rebuild()`/serialization helpers fail.

- [ ] **Step 3: Implement generation-based full publication**

Build canonical graph before writes, capture/recheck `_canonical_snapshot`, then in one explicit transaction:
acquire `pg_advisory_xact_lock`, insert a `building` generation, bulk-insert normalized rows with parameters,
write every head status, read back and compare normalized graph/hash/status/heads, mark `ready`, update active
pointer, delete the prior generation, and commit. `sync()` deliberately delegates to the same full publication
in PostgreSQL v1. Every query captures active generation once per read transaction.

- [ ] **Step 4: Verify full behavior and SQLite isolation**

Run: `uv run pytest tests/unit/provenance/test_postgres_rows.py tests/unit/provenance/test_backend.py tests/unit/provenance/test_index.py -q`

Expected: row round-trip and publication-order tests pass; SQLite tests remain unchanged.

- [ ] **Step 5: Lint, review and commit**

Run: `uv run ruff check src/vcp/provenance/postgres.py tests/unit/provenance/test_postgres_rows.py && uv run ruff format --check src/vcp/provenance/postgres.py tests/unit/provenance/test_postgres_rows.py`

Commit: `feat(provenance): add postgres full rebuild`

---

### Task 5: PostgreSQL Incremental Ingest, Recursive Dirty Closure and Rollback

**Files:**
- Modify: `src/vcp/provenance/postgres.py`
- Modify: `src/vcp/provenance/postgres_schema.py`
- Create: `tests/unit/provenance/test_postgres_incremental.py`

**Interfaces:**
- Consumes: verified `load_dataset_diff()`, `add_artifact()`, `add_dataset_diff_transition()`, `compute_statuses_for_entities()`.
- Produces: `_dataset_ancestors()`, `_dirty_closure()`, `_insert_delta()`, fixed-strategy PostgreSQL `ingest_diff()`.

- [ ] **Step 1: Write failing SQL-shape, idempotency and rollback-order tests**

```python
def test_dirty_closure_uses_recursive_forward_and_reverse_indexes():
    assert "WITH RECURSIVE" in DIRTY_CLOSURE_SQL
    assert "dataset_edges" in DIRTY_CLOSURE_SQL
    assert "provenance_edges" in DIRTY_CLOSURE_SQL
    assert "UNION" in DIRTY_CLOSURE_SQL


def test_incremental_failure_rolls_back_all_derived_records(fake_postgres, prepared_diff):
    before = fake_postgres.snapshot()
    fake_postgres.fail_at = "after_status"
    with pytest.raises(RuntimeError, match="injected interruption"):
        fake_postgres.backend.ingest_diff(prepared_diff.id, DATA_ROOT, CONFIGS_ROOT)
    assert fake_postgres.snapshot() == before
```

Also assert identical duplicate returns `inserted=False` and unchanged hash; conflicting payload fails; current
diff inputs are re-hashed at start and immediately before commit; historical change payload rows are not loaded.

- [ ] **Step 2: Confirm failures**

Run: `uv run pytest tests/unit/provenance/test_postgres_incremental.py -q`

Expected: missing recursive SQL and ingest helpers fail.

- [ ] **Step 3: Implement fixed incremental maintenance**

Inside one locked transaction: verify checkpoints/artifacts, load and re-hash the diff, reject cycles/conflicts,
compute ancestors and downstream dirty ids using recursive CTE, insert immutable rows with `ON CONFLICT DO
NOTHING RETURNING` plus exact existing-row comparison, copy source-head statuses to target, recompute only
dirty records with the shared view function, add statuses for delta entities to other heads, re-hash inputs,
advance fingerprint, write checkpoints and ingested-artifact row, then commit. Use no `DO UPDATE` for immutable
entity/edge/change identities.

- [ ] **Step 4: Verify incremental semantics**

Run: `uv run pytest tests/unit/provenance/test_postgres_incremental.py tests/unit/provenance/test_postgres_rows.py tests/unit/provenance/test_index.py -q`

Expected: all incremental/rollback/idempotency tests pass and the SQLite reference tests still pass.

- [ ] **Step 5: Lint, review and commit**

Run: `uv run ruff check src/vcp/provenance/postgres.py tests/unit/provenance/test_postgres_incremental.py && uv run ruff format --check src/vcp/provenance/postgres.py tests/unit/provenance/test_postgres_incremental.py`

Commit: `feat(provenance): add postgres incremental ingest`

---

### Task 6: Deterministic Adaptive Strategy and Immutable Policy Artifact

**Files:**
- Create: `src/vcp/provenance/strategy.py`
- Create: `tests/unit/provenance/test_strategy.py`
- Modify: `src/vcp/provenance/backend.py`

**Interfaces:**
- Produces: `RequestedStrategy`, `SelectedStrategy`, `MaintenanceFeatures`, `CostModel`, `AdaptivePolicy`, `StrategyDecision`, `select_strategy()`, `write_policy_artifact()`, `load_policy_artifact()`.

- [ ] **Step 1: Write selector and policy failure tests**

```python
def test_zero_change_is_topology_only_no_op(features):
    decision = select_strategy("auto", features.model_copy(update={"changed_samples": 0}), None)
    assert decision.selected_strategy == "NO_OP"
    assert decision.reason == "verified_zero_semantic_changes"


def test_auto_uses_calibrated_noise_band(features, compatible_policy):
    decision = select_strategy("auto", features, compatible_policy)
    expected = "INCREMENTAL" if (
        decision.estimated_incremental_ms + compatible_policy.incremental_rmse_ms
        < decision.estimated_full_ms - compatible_policy.full_rmse_ms
    ) else "FULL"
    assert decision.selected_strategy == expected


def test_missing_explicit_policy_fails_before_database_write(roots):
    with pytest.raises(ValidationFailed, match="not_found: provenance policy"):
        load_policy_artifact(roots.data, "missing")
```

Also test requested full/incremental reasons, absent-policy FULL fallback, deterministic repeated decisions,
backend/schema/PostgreSQL-major/environment incompatibility, manifest mutation and calibration-input pinning.

- [ ] **Step 2: Confirm failures**

Run: `uv run pytest tests/unit/provenance/test_strategy.py -q`

Expected: `ModuleNotFoundError: vcp.provenance.strategy`.

- [ ] **Step 3: Implement strict models, selector and artifact IO**

Use pydantic `extra="forbid"`. Cost is `max(0.0, intercept + sum(coefficient[name] * feature[name]))` in
the policy's fixed feature order. Apply the exact noise-band rule from design section 10. Write
`artifacts/provenance_policy/{derived_id}/{policy.json,calibration.json,manifest.json}` with `ArtifactWriter`;
derive it in code with `derived_id = f"postgres-adaptive-v1-{calibration_sha256[:12]}"`. Explicit invalid
policy fails; absent implicit policy selects FULL with `fallback_policy_absent_full`.

- [ ] **Step 4: Verify pure policy behavior**

Run: `uv run pytest tests/unit/provenance/test_strategy.py tests/unit/provenance/test_backend.py -q`

Expected: all strategy and artifact verification tests pass without PostgreSQL.

- [ ] **Step 5: Lint, review and commit**

Run: `uv run ruff check src/vcp/provenance/strategy.py tests/unit/provenance/test_strategy.py && uv run ruff format --check src/vcp/provenance/strategy.py tests/unit/provenance/test_strategy.py`

Commit: `feat(provenance): add adaptive strategy policy`

---

### Task 7: Adaptive PostgreSQL Maintenance Orchestration

**Files:**
- Modify: `src/vcp/provenance/postgres.py`
- Modify: `src/vcp/provenance/backend.py`
- Create: `tests/unit/provenance/test_postgres_adaptive.py`

**Interfaces:**
- Consumes: `select_strategy()`, fixed full/incremental paths, `maintenance_decisions` schema.
- Produces: strategy-aware `PostgresProvenanceBackend.ingest_diff()` returning complete `MaintenanceResult`.

- [ ] **Step 1: Write failing branch and decision-record tests**

```python
@pytest.mark.parametrize("selected", ["NO_OP", "INCREMENTAL", "FULL"])
def test_selected_path_and_decision_commit_together(fake_postgres, selected, policy):
    fake_postgres.costs_select(selected)
    result = fake_postgres.backend.ingest_diff("diff", DATA_ROOT, CONFIGS_ROOT,
                                               requested_strategy="auto", policy_id=policy.id)
    assert result.selected_strategy == selected
    assert fake_postgres.last_decision()["graph_hash"] == result.graph_hash


def test_no_op_publishes_transition_but_skips_semantic_recompute(fake_postgres, zero_diff):
    result = fake_postgres.backend.ingest_diff(zero_diff.id, DATA_ROOT, CONFIGS_ROOT,
                                               requested_strategy="auto")
    assert result.selected_strategy == "NO_OP"
    assert fake_postgres.has_transition(zero_diff.id)
    assert fake_postgres.semantic_recompute_calls == 0
```

Add assertions for dirty/total ratio, elapsed time, policy version, duplicate reason, and failure after decision
write rolling back decision/status/checkpoint/artifact rows together.

- [ ] **Step 2: Confirm failures**

Run: `uv run pytest tests/unit/provenance/test_postgres_adaptive.py -q`

Expected: strategy arguments or decision persistence are missing.

- [ ] **Step 3: Integrate selection without duplicating maintenance semantics**

Compute verified change count and recursive dirty features before persistent mutation. Select strategy once.
NO_OP calls the topology/status-copy path, INCREMENTAL calls Task 5, FULL calls generation rebuild. Write one
typed decision row and return one `MaintenanceResult` in the same transaction. Do not catch and retry a failed
strategy as another strategy; correctness failures must surface and rollback.

- [ ] **Step 4: Verify all branches**

Run: `uv run pytest tests/unit/provenance/test_postgres_adaptive.py tests/unit/provenance/test_strategy.py tests/unit/provenance/test_postgres_incremental.py -q`

Expected: all three selected paths, metrics and rollback tests pass.

- [ ] **Step 5: Lint, review and commit**

Run: `uv run ruff check src/vcp/provenance/postgres.py tests/unit/provenance/test_postgres_adaptive.py && uv run ruff format --check src/vcp/provenance/postgres.py tests/unit/provenance/test_postgres_adaptive.py`

Commit: `feat(provenance): select adaptive maintenance`

---

### Task 8: CLI Backend and Strategy Selection

**Files:**
- Modify: `src/vcp/cli_provenance.py`
- Modify: `tests/unit/provenance/test_cli_provenance.py`
- Create: `tests/unit/provenance/test_cli_postgres.py`

**Interfaces:**
- Consumes: `parse_backend()`, `make_backend()`, `RequestedStrategy`, `MaintenanceResult`.
- Produces: `_backend(data_root, backend, pg_service)`, shared backend options on eight commands, ingest strategy/policy options.

- [ ] **Step 1: Write failing default, JSON parity and credential-redaction tests**

```python
def test_no_backend_option_still_uses_sqlite(runner, roots):
    result = runner.invoke(app, ["provenance", "status", "--json", *_roots_args(roots)])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["fields"]["backend"] == "sqlite"


def test_postgres_ingest_reports_complete_decision(runner, fake_backend):
    result = runner.invoke(app, ["provenance", "ingest", "--backend", "postgresql",
                                 "--strategy", "auto", "--policy", "policy-1",
                                 "--artifact", "diff-1", "--json"])
    doc = json.loads(result.stdout)
    for key in ("backend", "selected_strategy", "strategy_reason", "changed_samples",
                "dirty_entities", "total_entities", "dirty_ratio", "elapsed_ms", "graph_hash"):
        assert key in doc["result"]
```

Also assert unknown backend/strategy and illegal SQLite+auto end with VCP VERDICT, `--pg-service` rejects DSN,
and a fake error containing `TOPSECRET` never appears in stdout/stderr/log capture.

- [ ] **Step 2: Confirm failures**

Run: `uv run pytest tests/unit/provenance/test_cli_postgres.py tests/unit/provenance/test_cli_provenance.py -q`

Expected: new options are unknown or expected backend fields are absent.

- [ ] **Step 3: Route every command through one backend helper**

Add plain-string `--backend` and `--pg-service` to all provenance commands. Add plain-string `--strategy`
defaulting to `incremental` and optional `--policy` only to ingest. Parse inside each `fn()` after
`run_command()` begins. Preserve existing result keys and add backend fields; PostgreSQL ingest emits the full
decision record. Human output and VERDICT contain no connection location.

- [ ] **Step 4: Verify CLI and legacy contract**

Run: `uv run pytest tests/unit/provenance/test_cli_postgres.py tests/unit/provenance/test_cli_provenance.py tests/unit/test_cli.py -q`

Expected: all pass; default SQLite command outputs remain valid and every invalid new option ends in VERDICT.

- [ ] **Step 5: Lint, review and commit**

Run: `uv run ruff check src/vcp/cli_provenance.py tests/unit/provenance/test_cli_postgres.py && uv run ruff format --check src/vcp/cli_provenance.py tests/unit/provenance/test_cli_postgres.py`

Commit: `feat(cli): select provenance backend strategy`

---

### Task 9: Reproducible PostgreSQL Integration Harness and Exact Parity Suite

**Files:**
- Create: `tests/integration/postgres/compose.yaml`
- Create: `tests/integration/postgres/run.ps1`
- Create: `tests/integration/provenance/conftest.py`
- Create: `tests/integration/provenance/test_postgres_backend.py`
- Create: `tests/unit/provenance/test_postgres_harness.py`
- Create: `.github/workflows/postgres-provenance.yml`
- Modify: `pyproject.toml` pytest markers

**Interfaces:**
- Consumes: real `PostgresProvenanceBackend`, canonical graph/status oracle.
- Produces: env-gated `postgres_backend` fixture, `PostgresHarness` test controller, and separate local/CI
  execution paths. `PostgresHarness.fresh_clone()`, `paused_before_publish()` and `reader_generation()` are
  test-only controls implemented in `tests/integration/provenance/conftest.py`, never production APIs.

- [ ] **Step 1: Write failing harness contract and env-gated integration assertions before adding Compose**

```python
def test_compose_is_localhost_only_and_requires_an_explicit_password():
    text = (ROOT / "tests/integration/postgres/compose.yaml").read_text(encoding="utf-8")
    assert "postgres:17.11-bookworm" in text
    assert "127.0.0.1:${VCP_TEST_PG_PORT:-55432}:5432" in text
    assert "${VCP_TEST_PG_PASSWORD:?set VCP_TEST_PG_PASSWORD}" in text


@pytest.mark.postgres
def test_every_strategy_matches_canonical(postgres_harness, roots, three_diffs):
    for requested in ("incremental", "full", "auto"):
        result = postgres_harness.fresh_clone().ingest_history(three_diffs, requested)
        assert result.normalized() == build_graph(roots.data, roots.configs).normalized()
        assert result.verify(roots.data, roots.configs).ok is True


@pytest.mark.postgres
def test_reader_sees_old_generation_until_rebuild_commit(postgres_harness, roots):
    old = postgres_harness.reader_generation()
    with postgres_harness.paused_before_publish(roots):
        assert postgres_harness.reader_generation() == old
    assert postgres_harness.reader_generation() != old
```

Cover duplicate ingest, injected rollback at changes/status/decision/checkpoint, UNKNOWN→REVIEW, zero-change
NO_OP, missing/mutated artifacts, cycle rejection, advisory-lock writer serialization, schema constraints and
full rebuild after delete/drift.

- [ ] **Step 2: Confirm the harness contract fails and unconfigured integration skips explicitly**

Run red contract: `uv run pytest tests/unit/provenance/test_postgres_harness.py -q`

Expected: `FileNotFoundError` for `tests/integration/postgres/compose.yaml`.

Run unconfigured integration: `uv run pytest -m postgres tests/integration/provenance -q`

Expected: all PostgreSQL tests skip with exactly `PostgreSQL integration service is not configured` rather
than erroring or touching SQLite tests.

- [ ] **Step 3: Add localhost-only Compose and secret-safe runners**

Use `postgres:17.11-bookworm`, healthcheck, ephemeral volume and
`127.0.0.1:${VCP_TEST_PG_PORT:-55432}:5432`. Require
`${VCP_TEST_PG_PASSWORD:?set VCP_TEST_PG_PASSWORD}` with no default. `run.ps1` creates temporary service/pass
files, never echoes the password, waits for health, runs marked tests, then removes files/container/volume in
`finally`. CI generates a random password, masks it, and uses the same Compose plus test command. Run
`uv run pytest tests/unit/provenance/test_postgres_harness.py -q`; the previously failing contract must pass.

- [ ] **Step 4: Run actual PostgreSQL integration**

Run: `tests/integration/postgres/run.ps1`

Expected: all marked tests pass against PostgreSQL 17.11; unmarked test invocation does not start Docker.

- [ ] **Step 5: Run cross-backend regression and commit**

Run: `uv run pytest tests/unit/provenance tests/integration/provenance -q && uv run ruff check src tests/integration/provenance`

Expected: unit tests pass and integration either passes when configured or reports explicit skips.

Commit: `test(provenance): add postgres integration harness`

---

### Task 10: Unified Real and Scaled Six-Method Benchmark

**Files:**
- Create: `tests/performance/provenance/workloads.py`
- Create: `tests/performance/provenance/adaptive_benchmark.py`
- Create: `tests/unit/provenance/test_adaptive_benchmark.py`
- Modify: `tests/performance/provenance/production_benchmark.py`
- Modify: `tests/performance/provenance/real_validation.py`

**Interfaces:**
- Produces: `Scenario`, `ScenarioResult`, `METHODS`, `build_scenario()`, `run_method()`, `capture_explain_rollback()`.
- Reuses: existing deterministic topology, metadata-copy RSNA track and result conventions.

- [ ] **Step 1: Write matrix/result/instrumentation tests**

```python
def test_scenario_matrix_is_complete_and_disjoint():
    scenarios = scenario_matrix(seeds=(20260913, 20260914))
    assert {s.entities for s in scenarios} == {1_000, 10_000, 100_000, 1_000_000}
    assert {s.change_ratio for s in scenarios} == {0, .001, .01, .05, .10, .25, .50, .90, 1.0}
    assert {s.topology for s in scenarios} == {"chain", "branched"}


def test_mutating_explain_always_rolls_back(fake_postgres):
    before = fake_postgres.snapshot()
    plan = capture_explain_rollback(fake_postgres, "postgres_incremental", SCENARIO)
    assert plan[0]["Plan"]
    assert fake_postgres.snapshot() == before
```

Assert the six exact methods, required p50/p95/throughput/storage/parity/decision fields, deterministic scenario
hashes, fresh backend per method, and no connection/service fields in JSON.

- [ ] **Step 2: Confirm failures**

Run: `uv run pytest tests/unit/provenance/test_adaptive_benchmark.py -q`

Expected: missing `workloads` and `adaptive_benchmark` modules.

- [ ] **Step 3: Implement compatible workload and runner layers**

Refactor fixture generation without changing old JSON fields. Generate target entity counts 1K/10K/100K/1M,
nine change ratios and chain/branched topologies. Run canonical oracle, SQLite full/incremental, PostgreSQL
full/incremental/adaptive on independent states. Capture status/impact/explain p50/p95, maintenance p50/p95,
throughput, closure, graph size, database size, decision, exact parity and environment. Run mutating EXPLAIN in
an explicit transaction followed by rollback; exclude it from latency samples.

- [ ] **Step 4: Run deterministic smoke benchmarks**

Run: `uv run python tests/performance/provenance/adaptive_benchmark.py --entities 1000 --ratios 0 0.01 --repetitions 2 --output .tmp/postgres-adaptive-smoke.json`

Expected: every method has graph/status/head parity true; zero ratio adaptive selects NO_OP; result contains no
secret or connection string. Remove `.tmp/postgres-adaptive-smoke.json` after inspection.

- [ ] **Step 5: Lint, review and commit**

Run: `uv run pytest tests/unit/provenance/test_adaptive_benchmark.py -q && uv run ruff check tests/performance/provenance tests/unit/provenance/test_adaptive_benchmark.py`

Commit: `perf(provenance): compare adaptive postgres maintenance`

---

### Task 11: Calibration Artifact and Held-Out Evaluation

**Files:**
- Create: `tests/performance/provenance/calibrate_adaptive.py`
- Create: `tests/performance/provenance/evaluate_adaptive.py`
- Create: `tests/unit/provenance/test_adaptive_evaluation.py`
- Modify: `src/vcp/provenance/strategy.py`

**Interfaces:**
- Consumes: benchmark scenario/result schema and immutable policy APIs.
- Produces: `fit_policy(calibration_rows)`, calibration manifest, frozen policy, and
  `evaluate_adaptive.py --policy-from PATH`, which reads and verifies the exact policy embedded in that artifact.

- [ ] **Step 1: Write leakage, deterministic-fit and no-refit tests**

```python
def test_calibration_and_holdout_hashes_cannot_overlap(calibration, holdout):
    with pytest.raises(ValidationFailed, match="workload_leakage"):
        evaluate_policy(calibration.policy, holdout + [calibration.rows[0]])


def test_fit_is_deterministic(calibration_rows):
    first = fit_policy(calibration_rows)
    second = fit_policy(list(reversed(calibration_rows)))
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_heldout_runner_never_calls_fit(monkeypatch, frozen_policy, heldout_rows):
    monkeypatch.setattr("vcp.provenance.strategy.fit_policy", fail_if_called)
    assert evaluate_policy(frozen_policy, heldout_rows).parity_rate == 1.0
```

- [ ] **Step 2: Confirm failures**

Run: `uv run pytest tests/unit/provenance/test_adaptive_evaluation.py -q`

Expected: missing fit/evaluation modules or functions.

- [ ] **Step 3: Implement calibration-only fit and frozen evaluation**

Sort rows by scenario hash, fit positive linear models with calibration seeds `20260913/20260914`, compute RMSE,
record feature order/environment/backend schema/PostgreSQL major, and publish the immutable policy. Held-out
uses only seeds `20261001/20261002`, verifies zero hash overlap and policy compatibility, and never imports or
calls fit logic. Write results to `docs/benchmarks/postgres-provenance-calibration-v1.json` and
`docs/benchmarks/postgres-provenance-heldout-v1.json`.

- [ ] **Step 4: Run calibration then held-out evaluation**

Run calibration: `uv run python tests/performance/provenance/calibrate_adaptive.py --output docs/benchmarks/postgres-provenance-calibration-v1.json`

Run held-out: `uv run python tests/performance/provenance/evaluate_adaptive.py --policy-from docs/benchmarks/postgres-provenance-calibration-v1.json --output docs/benchmarks/postgres-provenance-heldout-v1.json`

Expected: policy artifact verifies; overlap count is zero; every correctness parity is 100%; performance gates
are reported pass or fail without refitting. The evaluator derives the immutable `policy_id` from the verified
calibration artifact, so the execution command contains no manually substituted identifier.

- [ ] **Step 5: Test, review and commit immutable results**

Run: `uv run pytest tests/unit/provenance/test_adaptive_evaluation.py tests/unit/provenance/test_strategy.py -q`

Expected: deterministic fit, no leakage, no refit and artifact verification all pass.

Commit: `perf(provenance): calibrate and evaluate adaptive policy`

---

### Task 12: Documentation, Version, Full Regression and Final Handoff

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `CHANGELOG.md`
- Modify: `src/vcp/__init__.py`
- Modify: `docs/handover/HANDOVER.md`
- Create: `docs/guides/POSTGRESQL_PROVENANCE.md`
- Create: `docs/benchmarks/postgres-provenance-v1.md`
- Create: `docs/handover/POSTGRESQL_ADAPTIVE_PROVENANCE_HANDOFF.md`
- Create: `tests/unit/provenance/test_postgres_docs.py`
- Modify: this plan's completion/evidence section only after commands finish

**Interfaces:**
- Consumes: completed backend, policy, integration and benchmark evidence.
- Produces: `0.8.0` candidate documentation and auditable final handoff.

- [ ] **Step 1: Write documentation contract tests**

```python
def test_postgres_docs_state_optional_and_noncanonical():
    text = (ROOT / "docs/guides/POSTGRESQL_PROVENANCE.md").read_text(encoding="utf-8")
    assert "optional" in text.lower()
    assert "canonical" in text.lower()
    assert "--password" not in text
    assert "postgresql://" not in text
```

Extend package/version tests to require the new optional extra in dev and CHANGELOG head `0.8.0`.

- [ ] **Step 2: Confirm documentation/version failures**

Run: `uv run pytest tests/unit/test_package.py tests/unit/provenance/test_postgres_docs.py -q`

Expected: missing guide and version/CHANGELOG mismatch until documentation and version edits are complete.

- [ ] **Step 3: Document exact operations and evidence boundaries**

Document install (`uv sync --extra postgres`), libpq service setup without secrets, default SQLite behavior,
all backend/strategy flags, rebuild/repair, policy calibration, integration harness, benchmark reproduction,
failure semantics and credential redaction. Bump the candidate version to `0.8.0`; do not tag or call it released.
Report negative results and small-scale crossover honestly.

- [ ] **Step 4: Run security and repository gates**

Run credential scan: `rg -n -i "postgres(ql)?://|password=|PGPASSWORD|TOPSECRET" src tests docs .github pyproject.toml`

Expected: matches are only explicit negative tests/security documentation; no credential value or DSN appears.

Run: `uv run ruff check . && uv run ruff format --check . && git diff --check`

Expected: all pass.

Run PostgreSQL integration: `tests/integration/postgres/run.ps1`

Expected: all PostgreSQL integration tests pass on the pinned local service.

Run full suite: `uv run pytest --cov=vcp`

Expected: all legacy and new tests pass, total coverage remains at least 80%, and PostgreSQL modules have at
least 80% line coverage in the postgres-enabled run.

- [ ] **Step 5: Perform scoped specialist reviews and final whole-branch review**

Required review order: database schema/query review, security/credential review, task-scoped code review,
narrow fix/re-review for every finding, then final whole-branch review. Re-run the smallest affected tests after
each fix and the complete gates after the final fix. Record exact commands/counts; do not claim unseen evidence.

- [ ] **Step 6: Commit and hand off without external publication**

Stage explicit files only; do not use `git add -A`.

Commit: `docs(provenance): document postgres adaptive backend`

Final handoff must report branch, base, commit list, test counts, coverage, PostgreSQL version, policy artifact
id/hash, calibration/held-out separation, benchmark outcomes, review verdict and remaining limitations. Do not
push, open PR, merge, tag or release without separate authorization.

---

## Plan Self-Review

### Spec coverage

| Spec requirement | Implementing task(s) |
|---|---|
| Backend contract and SQLite default compatibility | 1, 8 |
| Optional Psycopg and lazy import | 2 |
| Normalized PostgreSQL schema, indexes and constraints | 3 |
| Atomic full generation publication and read/query parity | 4, 9 |
| Incremental ingest, recursive closure, idempotency and rollback | 5, 9 |
| Deterministic adaptive selector and fail-safe policy | 6, 7 |
| CLI backend/strategy/output/VERDICT contract | 8 |
| Localhost-only reproducible PostgreSQL and CI separation | 9 |
| Six-method real/scaled benchmark and rollback-safe EXPLAIN | 10 |
| Calibration versus frozen held-out evaluation | 11 |
| Documentation, version, security and final handoff | 12 |
| Canonical graph/hash/status/head exact parity | 4, 5, 7, 9, 10, 11 |

### Placeholder scan

The plan contains no deferred implementation section, manually substituted identifier, or unspecified
error-handling step. Task 11 passes the calibration artifact path to the held-out evaluator, which verifies the
artifact and derives its immutable policy identity internally.

### Type and name consistency

- Backend identity is always `BackendName.SQLITE|POSTGRESQL` with wire values `sqlite|postgresql`.
- Requested strategy values are lowercase `incremental|full|auto`; selected values are uppercase
  `NO_OP|INCREMENTAL|FULL` in models, database CHECKs and outputs.
- All backend write methods return existing `RebuildResult`/`VerifyIndexResult` or shared `MaintenanceResult`.
- `policy_id` names the artifact; `policy_version` names the verified policy/environment/schema identity.
- Graph/status types remain `ProvenanceGraph` and `StatusRecord`; no backend-specific domain model is introduced.

### Recommended execution mode

Use **Subagent-Driven Development**. The 12 tasks cross backend contracts, database DDL, security, algorithms,
CLI and experimental methodology; a fresh task-scoped implementer plus database/security/code review gates gives
clearer isolation than one long inline batch. Use `executing-plans` only when one agent must retain the same local
PostgreSQL service and benchmark environment across several consecutive tasks; still stop for review after each
task and never run Tasks 10–11 in parallel against the same database.
