# VCP PostgreSQL Adaptive Provenance Backend 設計

- 日期：2026-09-13
- 狀態：implementation-ready design；本輪只規劃，不實作
- 研究題目：**Adaptive Incremental Provenance Maintenance in PostgreSQL for Evolving ML Datasets**
- 起點：`codex/dataset-evolution-provenance@a625331`
- 既有功能 commit：`6ba42b8`
- 對應計畫：`docs/superpowers/plans/2026-09-13-vcp-postgresql-adaptive-provenance.md`

## 1. Research question

在 canonical VCP evidence、graph semantics 與 exact parity 不變的前提下，PostgreSQL provenance
backend 能否根據 dataset change size、dirty closure size 與 graph size，對每次 verified
`dataset_diff` deterministic 地選擇 `NO_OP`、`INCREMENTAL` 或 `FULL`，使 held-out workloads 的
maintenance latency 優於固定 full 與固定 incremental 策略？

研究比較三種 PostgreSQL maintenance methods：

1. PostgreSQL full recomputation。
2. PostgreSQL incremental maintenance。
3. PostgreSQL adaptive maintenance。

SQLite 保留為 embedded baseline。研究不重新發明 dataset diff、canonical graph 或 status semantics，
也不以效能結果放寬 correctness requirements。

## 2. Current VCP baseline

已完成的 Dataset Evolution and Incremental Provenance 提供：

- verified immutable `dataset_diff` artifact 與 deterministic `SampleChange`。
- `build_graph()` canonical full replay。
- `compute_statuses()` canonical status oracle。
- SQLite `ProvenanceIndex` 的 rebuild、sync、transactional incremental ingest、query 與 verify。
- duplicate ingest idempotency、rollback、checkpoint prefix drift、input re-hash、commit-boundary
  drift detection 與 incremental fingerprint。
- CLI `data diff` 與 `provenance rebuild|sync|ingest|impact|stale|explain|status|verify-index`。

已驗證 baseline：

| Historical events | SQLite incremental + query p95 | Full load + fingerprint p95 | Speedup |
|---:|---:|---:|---:|
| 1,000 | 55.218 ms | 21.384 ms | 0.39× |
| 10,000 | 61.275 ms | 176.223 ms | 2.88× |
| 100,000 | 204.521 ms | 2,114.040 ms | 10.34× |
| 1,000,000 | 2,427.051 ms | 23,731.252 ms | 9.78× |

這些數字只證明 SQLite workload 存在 crossover，是研究動機，不是 PostgreSQL policy 結論。

## 3. Architecture

```text
Canonical files / immutable artifacts / append-only ledgers
                         |
                         v
        build_graph() + compute_statuses() oracle
                         |
             +-----------+-----------+
             |                       |
             v                       v
   SQLiteBackend adapter     PostgresProvenanceBackend
   wrapping ProvenanceIndex    full / incremental / auto
             |                       |
             +-----------+-----------+
                         |
          shared ProvenanceBackend protocol
                         |
                         v
                  provenance CLI factory
```

核心裁決：

- 保留公開類別 `vcp.provenance.index.ProvenanceIndex` 與既有 method signatures。
- 新增薄 `SQLiteBackend` adapter；不重新實作或大搬動 SQLite SQL。
- `ProvenanceBackend` protocol 固定 CLI 需要的 operations 與 domain return models。
- PostgreSQL 寫入與查詢集中在 `postgres.py`；DDL 與 SQL constants 集中在
  `postgres_schema.py`。
- `strategy.py` 只做 deterministic feature、policy、cost estimate 與 decision，不開 database。
- canonical graph/status code維持 backend-agnostic；PostgreSQL full replay直接重用它們。
- 不使用 database triggers；maintenance order 必須在 Python transaction orchestration 中可見、可測。

### 3.1 Planned file responsibilities

| Path | Responsibility |
|---|---|
| `src/vcp/provenance/backend.py` | backend name/config、protocol、SQLite adapter、lazy factory、shared operation report |
| `src/vcp/provenance/postgres_schema.py` | PostgreSQL schema version、DDL、indexes、constraints、schema install/validate |
| `src/vcp/provenance/postgres.py` | lazy Psycopg connection、generation publication、full/incremental/query/verify |
| `src/vcp/provenance/strategy.py` | policy models、cost estimates、NO_OP/INCREMENTAL/FULL selector、policy artifact loader/writer |
| `src/vcp/provenance/index.py` | existing SQLite implementation；只接受必要 compatibility extraction |
| `src/vcp/cli_provenance.py` | shared backend options/factory 與 strategy-aware ingest output |
| `tests/performance/provenance/adaptive_benchmark.py` | shared six-method workload matrix與 result schema |
| `tests/performance/provenance/calibrate_adaptive.py` | calibration-only model fit與 immutable policy artifact publication |
| `tests/performance/provenance/evaluate_adaptive.py` | frozen-policy held-out evaluation，不重新 fit |

## 4. Backend contract

`backend.py` 定義下列 public types：

```python
class BackendName(StrEnum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


@dataclass(frozen=True)
class BackendConfig:
    name: BackendName = BackendName.SQLITE
    pg_service: str | None = None


class ProvenanceBackend(Protocol):
    name: BackendName
    location_label: str

    def rebuild(self, data_root: Path, configs_root: Path) -> RebuildResult: ...
    def sync(self, data_root: Path, configs_root: Path) -> RebuildResult: ...
    def ingest_diff(
        self,
        artifact_id: str,
        data_root: Path,
        configs_root: Path,
        *,
        requested_strategy: RequestedStrategy = RequestedStrategy.INCREMENTAL,
        policy_id: str | None = None,
    ) -> MaintenanceResult: ...
    def load_graph(self) -> ProvenanceGraph: ...
    def statuses(self, head_id: str) -> dict[str, StatusRecord]: ...
    def normalized(self) -> dict[str, Any]: ...
    def stats(self) -> dict[str, Any]: ...
    def verify(self, data_root: Path, configs_root: Path) -> VerifyIndexResult: ...
```

上列 `...` 表示 Python Protocol method body；implementation plan 會以可執行 method definitions 落地，
不留 implementation placeholder。

`SQLiteBackend` 委派給現有 `ProvenanceIndex`。它只接受預設 `incremental`；對明示 `full` 或 `auto`
回 `ValidationFailed("unsupported_strategy: ...")`。未指定任何新 option 的 CLI 仍建立 SQLite adapter，
路徑仍為 `<data_root>/indexes/provenance.sqlite3`，原 command meaning、exit code 與既有 result keys 不變。

`make_backend(config, data_root)` 是唯一 factory。只有 `name=postgresql` 時才 lazy import
`vcp.provenance.postgres`，確保 base install 沒有 Psycopg 仍能 import `vcp` 與使用 SQLite。

## 5. Canonical versus derived boundary

下列仍是 canonical source of truth：

- JSONL / YAML records。
- DatasetCard、`samples.jsonl` 與 dataset manifests。
- source audit artifacts。
- verified `dataset_diff` artifacts。
- append-only ledgers。
- immutable artifact manifests、payload hashes與 supersession ledger。

SQLite 與 PostgreSQL 都是 derived、disposable、rebuildable indexes。PostgreSQL 不接受任意 graph
mutation API；所有寫入只能來自 canonical replay、canonical sync 或 verified diff ingest。

Policy artifact 是 immutable VCP artifact，用來證明 selector 版本與 calibration inputs；它不改變
dataset/run evidence，也不使 PostgreSQL 成為 canonical store。Maintenance decision row 是 derived
research telemetry；正式可攜證據是已去除 credentials 的 benchmark JSON 與 policy artifact。

## 6. PostgreSQL connection and compatibility

- 支援 PostgreSQL 17.x；reproducible local harness 固定 `postgres:17.11-bookworm`。
- Python adapter 使用 optional `psycopg[binary]>=3.2,<4`，由 `uv.lock` 固定實際 resolution。
- CLI 只接受 `--pg-service <name>`；service name 限 `[A-Za-z0-9_.-]{1,128}`。
- 沒有 `--dsn`、`--password`、`--host`、`--user` 等 VCP connection flags。
- service 未指定時呼叫 `psycopg.connect()`，由 libpq standard environment/service/password-file
  resolution 處理；VCP 不自行讀取 `PGPASSWORD` 或 password file。
- PostgreSQL exception 只轉成固定訊息、backend 與 SQLSTATE；不得串接原 exception、conninfo、
  `ConnectionInfo`、service file內容或完整 connection string。
- 缺 Psycopg 時只有 PostgreSQL backend 回
  `ValidationFailed("missing_dependency: install with `uv sync --extra postgres`")`；SQLite 不受影響。

## 7. PostgreSQL normalized schema

固定 schema 名稱：`vcp_provenance`。一個 PostgreSQL database只承載一個 active VCP provenance
index；多個 VCP data roots 使用不同 database/service，v1 不新增 namespace option。

PostgreSQL schema version為 `1`，與 SQLite `SCHEMA_VERSION = 2` 各自獨立。

### 7.1 Generation publication

`generations`

- `generation_id uuid PRIMARY KEY`，由 Python產生。
- `state text NOT NULL CHECK (state IN ('building','ready'))`。
- `schema_version smallint NOT NULL`。
- `built_at timestamptz NOT NULL`，值只能由 `vcp.core.time.utc_now()` 提供。
- `graph_hash char(64) NOT NULL`、`graph_record_count bigint NOT NULL`、
  `graph_record_sum char(64) NOT NULL`。
- `canonical_snapshot_hash char(64) NOT NULL`。

`active_generation`

- singleton boolean primary key，固定 `TRUE`。
- `generation_id uuid UNIQUE NOT NULL REFERENCES generations(generation_id)`。

Full rebuild先寫全新 generation；在同一 transaction完成 row count、graph hash、status parity與
canonical snapshot recheck後，把 state改成 `ready`，最後更新 singleton active pointer。Commit前舊
generation仍是讀者的 active snapshot。舊 generation 在 pointer更新後於同一 transaction刪除；MVCC
讀者仍能完成舊 snapshot，失敗則整筆 transaction rollback。

### 7.2 Core tables

每張 derived data table都有 `generation_id uuid NOT NULL REFERENCES generations ON DELETE CASCADE`。

`metadata`

- `(generation_id, key)` primary key，`value text NOT NULL`。
- 保存 build string、backend schema version與非核心 extensible metadata；graph hash等核心欄位以
  `generations` typed columns為準，避免兩份權威。

`entities`

- `(generation_id, entity_id)` primary key。
- `entity_type text NOT NULL`、`key_value text NOT NULL`、`dataset_version_id text NULL`。
- `attributes jsonb NOT NULL DEFAULT '{}'::jsonb`、`broken_reason text NULL`。
- indexes：`(generation_id, entity_type, entity_id)`、
  `(generation_id, dataset_version_id, entity_type, entity_id)`。

`provenance_edges`

- `(generation_id, edge_id)` primary key。
- `source_id text NOT NULL`、`target_id text NOT NULL`、`edge_type text NOT NULL`。
- `attributes jsonb NOT NULL DEFAULT '{}'::jsonb`。
- source與target使用 `(generation_id, entity_id)` deferrable foreign keys。
- edge type CHECK只接受現有十種：`DERIVED_FROM`、`CONTAINS_CHANGE`、`USES_SPLIT`、
  `CONSUMED_BY`、`PRODUCED_BY`、`EVALUATED_BY`、`COMBINED_INTO`、`SUPERSEDES`、
  `BACKED_UP_AS`、`SUBMITTED_AS`。
- indexes：`(generation_id, source_id, edge_type, target_id)` 與
  `(generation_id, target_id, edge_type, source_id)`。

`dataset_edges`

- `(generation_id, source_id, target_id)` primary key。
- `artifact_id text NOT NULL`；source/target為 dataset entity foreign keys。
- indexes：`(generation_id, target_id, source_id)` 支援 reverse ancestor traversal。

`sample_changes`

- `(generation_id, change_id)` primary key。
- typed columns：`schema_version smallint`、`source_id`、`target_id`、`from_dataset`、
  `from_samples_hash char(64)`、`to_dataset`、`to_samples_hash char(64)`、`sample_id`、
  `change_type`、`changed_domains text[]`、`changed_fields text[]`、
  `semantic_effects text[]`、`before_row_hash char(64) NULL`、`after_row_hash char(64) NULL`。
- CHECK change type為 `ADDED|REMOVED|MODIFIED`，hash為64 lowercase hex，array items只接受
  現有 domain/effect vocabularies。
- indexes：`(generation_id, sample_id, change_id)`、
  `(generation_id, source_id, target_id)`、`(generation_id, change_type, change_id)`。

`dataset_edge_changes`

- `(generation_id, source_id, target_id, change_id)` primary key。
- foreign keys連到 `dataset_edges` 與 `sample_changes`。
- 將 SQLite 的 `change_ids_json` 正規化，重建時依 `change_id`排序還原 exact graph representation。

`entity_status`

- `(generation_id, head_id, entity_id)` primary key。
- `status text NOT NULL CHECK (status IN ('VALID','STALE','REVIEW','BROKEN'))`。
- `reason text NOT NULL`、`predecessor_id text NULL`。
- head/entity/predecessor連到同 generation entities，predecessor foreign key可為NULL。
- indexes：`(generation_id, head_id, status, entity_id)`、
  `(generation_id, entity_id, head_id)`。

`ingest_checkpoints`

- `(generation_id, source_path)` primary key。
- `consumed_bytes bigint NOT NULL CHECK (consumed_bytes >= 0)`、
  `prefix_sha256 char(64) NOT NULL`、`last_event_id char(64) NULL`。

`ingested_artifacts`

- `(generation_id, artifact_id)` primary key。
- `manifest_sha256 char(64) NOT NULL`。

`maintenance_decisions`

- `(generation_id, decision_id uuid)` primary key。
- typed fields：artifact id、requested strategy、selected strategy、reason code、changed samples、
  dirty entities、total entities、dirty ratio、estimated incremental/full milliseconds、policy version、
  elapsed milliseconds、`decided_at timestamptz`。
- requested CHECK為 `incremental|full|auto`；selected CHECK為 `NO_OP|INCREMENTAL|FULL`；counts非負、
  ratio介於0與1。`decided_at`由 `utc_now()`傳入。

## 8. Transactions, locking and publication

所有 PostgreSQL writer operation：

1. 以 Psycopg explicit transaction context開始。
2. 第一個 statement執行固定 key的 `pg_advisory_xact_lock`，序列化此 database的 provenance writer。
3. 讀 active generation並 verify schema/checkpoints/已 ingest artifacts。
4. 驗證 canonical snapshot或本次 diff manifest與所有 pinned inputs。
5. 執行 full、incremental或 topology-only no-op maintenance。
6. 再驗 canonical snapshot/diff inputs。
7. 寫完整 graph fingerprint、checkpoint、ingested artifact與 decision row。
8. Commit；任何 exception由 transaction context rollback。

讀者先在同一 read transaction讀 active generation id，所有後續 query都帶該 generation id，避免
rebuild publication中途混用兩代資料。

Incremental dirty closure使用 `WITH RECURSIVE ... UNION`：

- reverse traverse `dataset_edges` 找 source dataset的所有 ancestors。
- 以 ancestors與本次 delta entities為 roots，沿 `provenance_edges.source_id -> target_id` 找 downstream。
- `UNION`去重使意外 cycle不會無限展開；canonical dataset cycle guard仍保留。
- dirty count與total entity count在同一 transaction snapshot中取得。

Idempotent insert使用 `INSERT ... ON CONFLICT DO NOTHING RETURNING`，然後逐欄比較既有 row；相同
identity、不同 payload回 `IntegrityError`，不能以 `DO UPDATE`悄悄覆寫 immutable derived identity。

## 9. Full, incremental and NO_OP semantics

### 9.1 FULL

- 重新執行 `build_graph()`與所有 head的 `compute_statuses()`。
- 寫新 generation並 exact read-back；graph/status/head parity全通過才更新 active pointer。
- `sync()`在 PostgreSQL v1也走此 atomic full publication；研究優化只針對 diff ingest。

### 9.2 INCREMENTAL

- verify diff與inputs後建立與 SQLite同義的 delta entities、edges、changes與dataset edge。
- 從 source-head materialized statuses開始，使用 SQL recursive dirty closure與現有
  `compute_statuses_for_entities()`重算 dirty set。
- old head status複製到new head後只upsert dirty rows；其他head只補本次新增entities。
- 不載入歷史 `sample_changes` payload，也不重新hash完整歷史 graph。

### 9.3 NO_OP

`NO_OP`由 verified diff的 `total_changes == 0`觸發。它不是忽略artifact：仍插入dataset transition、
artifact/entity/edge、checkpoint、fingerprint與ingested-artifact record，並從source head複製status；只
省略semantic dirty propagation。這樣 normalized graph仍與canonical full replay完全相同。

## 10. Adaptive strategy

### 10.1 Request and decision records

Requested strategies：`incremental|full|auto`。Selected strategies：`NO_OP|INCREMENTAL|FULL`。

每個 `StrategyDecision`至少保存：

- requested與selected strategy。
- changed samples。
- dirty entities與total entities。
- dirty ratio。
- total edges、historical changes與dataset head count。
- estimated incremental與full cost，單位milliseconds。
- policy version。
- machine-readable reason code。

Reason codes固定為：

- `verified_zero_semantic_changes`
- `requested_incremental`
- `requested_full`
- `calibrated_incremental_lower_confident_cost`
- `calibrated_full_lower_or_uncertain_cost`
- `fallback_policy_absent_full`
- `duplicate_artifact_no_op`

### 10.2 Cost models

Policy包含兩個非負linear cost models：

- incremental features：`changed_samples`、`dirty_entities`、`dirty_ratio`、`total_edges`、`head_count`。
- full features：`total_entities`、`total_edges`、`historical_changes`、`head_count`。

Calibration以既有 scikit-learn `LinearRegression(positive=True)` deterministic fit p50 latency；intercept
與prediction最低截為0。Policy記錄feature order、coefficients、intercept、RMSE、training row count、
PostgreSQL major、backend schema version、benchmark schema version與environment fingerprint。

Decision rule：

1. verified change count為0，一律 `NO_OP`。
2. requested為full或incremental，照request執行並記reason。
3. requested為auto且policy相容時，若
   `estimated_incremental_ms + incremental_rmse_ms < estimated_full_ms - full_rmse_ms`，選
   `INCREMENTAL`；否則選`FULL`。

RMSE形成由calibration資料推導的noise band；沒有任意hard-coded dirty-ratio crossover。

### 10.3 Policy artifact and fail-safe behavior

Artifact layout：

```text
artifacts/provenance_policy/<policy_id>/
  spec.json
  policy.json
  calibration.json
  manifest.json
```

`spec.json` inputs pin calibration result JSON；`policy.json`是strict `AdaptivePolicy`；
`calibration.json`保存選入fit的scenario IDs與hash，不含held-out rows。`manifest.json`最後發布。

- 明示 `--policy ID`但artifact missing、mutated或不相容：`ValidationFailed`，不執行maintenance。
- `--strategy auto`未給policy：使用built-in safe fallback；zero change為NO_OP，其餘FULL，CLI為WARN。
- policy environment/backend schema/PostgreSQL major不相容：明示ID時FAIL；未明示時FULL fallback。
- policy不能改變status semantics，只選擇maintenance algorithm。

## 11. CLI design

所有`vcp provenance` commands增加string options：

- `--backend sqlite|postgresql`，預設`sqlite`。
- `--pg-service NAME`，只對postgresql有效；缺席時交給libpq defaults。

只有`provenance ingest`增加：

- `--strategy incremental|full|auto`，預設`incremental`。
- `--policy ID`，只允許postgresql + auto。

Option以plain string接收，在`run_command()`內呼叫parser並以`ValidationFailed`回報；不使用Typer enum
或callback提前退出，確保錯誤仍有final VERDICT。

Backend建立只發生在`_backend()` helper，不讓八個commands複製connection邏輯。現有未指定backend
命令仍使用SQLite。PostgreSQL output不包含service name、host、port、database、user、password、DSN、
conninfo或raw exception。

PostgreSQL ingest human/JSON/VERDICT共同欄位：

- backend。
- requested strategy與selected strategy。
- strategy reason。
- changed sample count。
- dirty entity count。
- total entity count。
- dirty ratio。
- estimated incremental/full cost。
- elapsed milliseconds。
- graph hash。
- policy version。

## 12. Reproducible PostgreSQL integration environment

Local Compose使用`postgres:17.11-bookworm`、container healthcheck與ephemeral named volume。Port只綁
`127.0.0.1:${VCP_TEST_PG_PORT:-55432}:5432`。`POSTGRES_PASSWORD`必須由執行環境提供，Compose用
`${VCP_TEST_PG_PASSWORD:?set VCP_TEST_PG_PASSWORD}`拒絕缺值；repo不提供default或`.env`。

測試client透過temporary `pg_service.conf`與`pgpass.conf`或操作者既有`PGSERVICE`連線。產生temporary
files的test helper可從測試環境取得secret，但不得print；VCP package本身不讀該environment variable。
cleanup刪除temporary files與Compose volume。

`pytest` marker為`postgres`。Psycopg缺席、Docker/service不可用或未設定integration environment時明確
skip；CI job與local instructions分開。普通SQLite test suite不啟動Docker。

## 13. Experimental methodology

### 13.1 Workloads

Scaled graph sizes以entity count為準：1K、10K、100K、1M。每個size使用change ratios：0%、0.1%、
1%、5%、10%、25%、50%、90%、100%；ratio定義為changed samples / sample entities，dirty ratio另外
由實際closure量測。每個組合包含chain與branched topology。

- calibration seeds：`20260913`、`20260914`。
- held-out seeds：`20261001`、`20261002`。
- 1K/10K各7 repetitions；100K/1M各3 repetitions。
- scenario順序以seed deterministic shuffle；每次method使用獨立fresh backend state。

Real track沿用metadata-only temporary copy：13,196 entities、17,569 edges、8,752 changes、7 runs；
r3→r4為4,407 changes、r4→r5為4,345 UNKNOWN metadata changes、r5→r6為0 changes。RSNA名稱只
存在`tests/performance` runner/fixture，不進`src/vcp`。

### 13.2 Compared methods

每個scenario比較：

1. Canonical full replay oracle。
2. SQLite full recomputation。
3. SQLite incremental maintenance。
4. PostgreSQL full recomputation。
5. PostgreSQL incremental maintenance。
6. PostgreSQL adaptive maintenance。

### 13.3 Measurements

- ingest/maintenance latency p50與p95。
- status、impact、explain query latency p50與p95。
- throughput、changed sample count、dirty closure size與dirty ratio。
- entity/edge/change/head counts與storage size。
- requested/selected strategy、reason、policy version與estimated costs。
- exact normalized graph、graph hash、status與dataset-head parity。
- PostgreSQL `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`。

Mutating EXPLAIN一律在explicit transaction執行並rollback；該instrumented run不列入latency samples、
不改變後續fixture。PostgreSQL storage用`pg_total_relation_size`，SQLite用file bytes；兩者都明列量測
口徑，不把數字當成相同physical representation。

### 13.4 Calibration versus held-out evaluation

Calibration runner只讀calibration scenario manifest，fit兩個cost models並發布immutable policy artifact。
Policy manifest hash固定後，held-out runner在另一個process載入不同seed manifest；它禁止呼叫fit API，
若scenario ID/hash與calibration集合重疊立即FAIL。研究報告分開列calibration fit與held-out結果。

Adaptive success不只看平均latency。Acceptance同時要求：

- 每個scenario correctness parity 100%。
- held-out adaptive median maintenance latency不劣於較佳fixed strategy超過5%。
- held-out adaptive p95不劣於較佳fixed strategy超過10%。
- strategy decision與reason可由policy和features重算得到。
- 若performance門檻未達，結果仍如實報告，不能以調held-out policy修飾。

## 14. Security constraints

- VCP不接受、讀取、保存、驗證、輸出或log database password。
- 禁止full DSN/URI CLI option；service名稱以allowlist regex驗證。
- 所有SQL values用Psycopg parameters；dynamic identifiers只允許fixed schema constants並以
  `psycopg.sql.Identifier`組合。
- 不在exception、JSON、VERDICT、artifact、benchmark result或EXPLAIN output保存conninfo。
- PostgreSQL role只授權`vcp_provenance` schema與tables；不要求production superuser。
- Compose只綁localhost，沒有committed credential/default password。
- 所有writer使用transaction advisory lock；statement/lock timeout由service config管理，不在log回顯。
- 全部時間欄位由`utc_now()`或`stamp()`產生；duration使用monotonic `perf_counter_ns()`。

## 15. Failure model

- missing optional dependency：PostgreSQL command FAIL並提供安裝方式；SQLite繼續可用。
- connection/schema error：固定redacted FAIL，僅附backend與SQLSTATE。
- schema version mismatch：FAIL並要求使用已驗證rebuild流程；不silent migration。
- lock/statement timeout：整個transaction rollback，沒有checkpoint/status/artifact partial rows。
- duplicate identical artifact：selected NO_OP、`inserted=false`、graph hash不變。
- duplicate id不同manifest/payload：IntegrityError。
- missing/mutated/unverifiable canonical artifact：fail closed。
- canonical snapshot或diff input在operation中漂移：commit前recheck後rollback。
- explicit policy missing/incompatible：FAIL；implicit absence為safe FULL fallback + WARN。
- benchmark instrumentation failure：scenario標failed且不納入performance aggregate；correctness gate失敗。

## 16. Non-goals

- 不取代、刪除或改變SQLite default backend。
- 不把PostgreSQL當canonical evidence store。
- 不重寫dataset diff、graph scanner或status semantics。
- 不加入RSNA或其他競賽邏輯到`src/vcp`。
- 不做remote hosted service、API server、dashboard、multi-tenant namespace、connection pool或RLS。
- 不使用database triggers、logical replication、CDC或background daemon。
- 不在本計畫加入automatic production migration或credential manager。
- 不以benchmark speedup取代correctness parity。

## 17. Acceptance criteria

1. Base install沒有Psycopg時，`import vcp`與全部SQLite commands/tests正常。
2. 未指定`--backend`的CLI行為、path、VERDICT與exit code維持SQLite baseline。
3. PostgreSQL schema具備本spec的typed columns、JSONB boundary、FK/unique/check/index constraints。
4. Full rebuild以generation pointer原子發布；失敗時active generation與所有queries不變。
5. Incremental ingest使用transaction advisory lock、`WITH RECURSIVE` closure與idempotent conflict checks。
6. NO_OP仍發布必要topology/evidence，且與canonical full replay exact parity。
7. 每個backend/strategy的normalized graph、graph hash、statuses與dataset heads完全等於oracle。
8. Duplicate ingest no-op；injected interruption完全rollback；rebuild後`verify-index`成功。
9. UNKNOWN metadata保持REVIEW；missing/mutated evidence fail closed。
10. Auto decision deterministic、可由record重算；policy由calibration-only immutable artifact提供。
11. Explicit bad policy FAIL；無policy的auto非零change安全選FULL。
12. PostgreSQL output含策略與規模metrics，不含credential、DSN、conninfo或raw driver error。
13. Local Compose只綁localhost、無committed secret；PostgreSQL不可用時integration tests明確skip。
14. 六方法real/scaled benchmark保存machine-readable results與rollback-safe EXPLAIN JSON。
15. Calibration與held-out scenario hashes不重疊；held-out不執行fit。
16. 現有完整test suite與ruff通過，新增PostgreSQL code coverage達80%以上。
17. Security review、database review與whole-branch review均無未處理HIGH/MEDIUM findings。

## 18. Impact assessment

對現有研究題目沒有方向性改變。Dataset evolution與incremental provenance仍提供研究問題的
correctness substrate；本次新增的是database backend、maintenance strategies、adaptive selector與
實驗方法。最主要的compatibility風險是過度抽象既有SQLite、CLI提前參數驗證破壞VERDICT，以及
PostgreSQL generation/status row volume；implementation plan分別以薄adapter、run_command內parser、
generation pointer與scaled measurement處理。

## 19. Primary references checked for this design

- PostgreSQL supported-version policy：<https://www.postgresql.org/support/versioning/>
- libpq environment variables：<https://www.postgresql.org/docs/current/libpq-envars.html>
- libpq service connection parameter：<https://www.postgresql.org/docs/current/libpq-connect.html>
- PostgreSQL recursive queries：<https://www.postgresql.org/docs/17/queries-with.html>
- PostgreSQL advisory locks：<https://www.postgresql.org/docs/17/functions-admin.html>
- PostgreSQL `INSERT ... ON CONFLICT`：<https://www.postgresql.org/docs/17/sql-insert.html>
- PostgreSQL `EXPLAIN`：<https://www.postgresql.org/docs/17/sql-explain.html>
- Psycopg transaction management：<https://www.psycopg.org/psycopg3/docs/basic/transactions.html>
- Psycopg parameter binding：<https://www.psycopg.org/psycopg3/docs/basic/params.html>
- Psycopg binary installation：<https://www.psycopg.org/psycopg3/docs/api/pq.html>
- Docker Compose networking：<https://docs.docker.com/compose/how-tos/networking/>
- Docker Official PostgreSQL image：<https://hub.docker.com/_/postgres>
