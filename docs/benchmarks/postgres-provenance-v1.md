# PostgreSQL Adaptive Provenance Benchmark v1

## 結論與證據邊界

此文件目前是可重跑方法與 pending acceptance record，不是 PostgreSQL 效能報告。2026-09-14 起本機有
原生 PostgreSQL 17.11（使用者排程工作 `VCP-PostgreSQL-17.11`，只聽 `127.0.0.1:55432`，service
`vcp-pg17-evidence`；佈建紀錄在 `C:/vcp-data/services/postgresql-17.11-vcp/`，不進 git），因此有
**live integration** 證據；仍沒有 **large-scale**、**calibration** 或 **held-out** outcomes，也沒有
policy artifact ID/hash、latency、crossover、adaptive gate pass/fail 或 RSNA six-method 數字。這些欄位
不可從 offline doubles 或 SQLite 結果推導。

## Live evidence（2026-09-14，本機原生 PostgreSQL 17.11）

每個檔案寫一次不改；後續嘗試以新的 `-vN` 檔案接續並在 `predecessor` 欄位指回前一版。

| 檔案 | 結果 | SHA-256（前 8 碼） |
|---|---|---|
| `postgres-provenance-integration-v1.json` / `.xml` | FAIL：39 passed、4 behavior failures（checkpoint 測試與 schema 修正前） | `b9d289dd` / `24726f40` |
| `postgres-provenance-integration-v2.json` / `.xml` | ERROR：51 setup errors（PostgreSQL 程序在跑前被回收） | `04be858b` / `367e0460` |
| `postgres-provenance-integration-v3.json` / `.xml` | PASS 51/51 at `d8cc334`：rollback 28、concurrency 1、MVCC 1、canonical/SQLite/PostgreSQL parity 3；server 17.11（170011） | `b55f34fd` / `b50ada0f` |
| `postgres-provenance-calibration-v1.run.json` / `.log.txt` | ABORT `resource_guard_available_ram`：可用 RAM 低於 2 GiB 持續 30 秒；runner private bytes 峰值 16.3 GB；沒有正式 calibration JSON，沒有 policy | `5c60b75c` / `dc71b39e` |
| `postgres-provenance-1m-memory-gate-v2.json`、`-v3.json`、`-v4.json` | 非正式單場景資源護欄，皆 ABORT `system_available_ram_lt_2_gib_sustained_30s`；v4 的 historical diff streaming 已完成（該階段 private ≈0.52 GiB），止於 `baseline_graph_build`（peak private 5.67 GiB、最低可用 RAM 0.556 GiB）；v1 未保存 | `724634e8` / `4672be87` / `a2c295df` |

| `postgres-provenance-exploratory-v1.json` / `-explain.json` / `.md` | 探路矩陣（非正式）：1K/10K/100K × 9 ratios × 2 topologies × 2 seeds = 108 場景、每場景 1 次、五個固定方法；540/540 parity；ratio > 0 每格 incremental 都快於 full（100K 時為 full 的 13–45%）；1M 與 adaptive 未跑 | `98247c28` / `451550ee` |

完整 SHA-256 以 `sha256sum docs/benchmarks/postgres-provenance-*` 為準。1M 場景的瓶頸不在 fixture
writer，而在 canonical graph replay 讀取約 41 萬筆 diff 事件時同時常駐文字、lines、pydantic change list
與 graph；graph replay 已改為串流（`open_dataset_diff`：先整檔驗證、再逐筆重放，`load_dataset_diff`
API 與 exact parity 不變），下一步是重跑 gate。主機 31 GB RAM 常態只剩約 7 GB 可用，跑 1M 前先清出記憶體。

目前可引用的 Task 9–11 evidence：

- opt-in PostgreSQL integration cases 在 service 未配置時全部 `skipped`；這不是 database pass。Live pass
  只有上表 v3 那一份。
- Task 10 的 offline 1K smoke 只跑 canonical full、SQLite full、SQLite incremental：12 個
  method/scenario rows、24 個 fresh timed samples，graph/hash/status/head exact parity。PostgreSQL 未跑。
- unit tests 使用明示 synthetic fixtures 驗證 schema/query orchestration、六方法 result contract、
  exact matrix/sample coverage、NO_OP exclusion、policy byte hash、calibration/held-out leakage 與 no-refit；
  synthetic timings 不是 empirical research result。
- live runner、benchmark、calibrator、evaluator都在固定 preflight fail closed，沒有留下 result JSON 或
  policy artifact。

歷史 SQLite production benchmark 曾在同一專案顯示 1K 較 full 慢、10K 到 100K 間有 crossover；它只
是 PostgreSQL 研究動機，不是這個 backend 的 crossover 或 adaptive policy 結論。

## 固定方法與 matrix

每個 scaled scenario 比較六種方法：

1. `canonical_full`
2. `sqlite_full`
3. `sqlite_incremental`
4. `postgres_full`
5. `postgres_incremental`
6. `postgres_adaptive`

固定 entity scales 為 1K、10K、100K、1M；change ratios 為 0%、0.1%、1%、5%、10%、25%、50%、
90%、100%；topology 為 chain/branched。Calibration seeds 是 `20260913`、`20260914`，held-out seeds
是 `20261001`、`20261002`。每個 split 共 144 scenarios；1K/10K 各 7 repetitions，100K/1M 各 3，
每 method 共 720 measured samples。

Real track 的來源必須唯讀複製到 temporary metadata root；它不修改 live data。Task 10 runner也可讓
既有 production/real entrypoint增加 `--six-method`，但本環境沒有執行 live RSNA track。

## 可重跑命令

先依 `docs/guides/POSTGRESQL_PROVENANCE.md` 完成 benchmark/calibration/held-out 的 separate opt-in
preflight：全部設定 `VCP_TEST_PG_SERVICE`、`PGSERVICEFILE`、`PGPASSFILE`，後兩者使用權限受限的
absolute temporary file paths。這不是 ordinary VCP/libpq defaults；disposable service 必須有
`CREATE DATABASE` privilege。命令本身不得包含 connection material 或任何檔案內容。

```powershell
uv run python tests/performance/provenance/adaptive_benchmark.py `
  --entities 1000 10000 100000 1000000 `
  --ratios 0 0.001 0.01 0.05 0.10 0.25 0.50 0.90 1 `
  --seeds 20260913 20260914 `
  --output <NEW_SIX_METHOD_RESULT>

uv run python tests/performance/provenance/production_benchmark.py `
  --six-method --output <NEW_PRODUCTION_RESULT>

uv run python tests/performance/provenance/real_validation.py `
  --data-root <READ_ONLY_SOURCE_ROOT> --configs-root <READ_ONLY_CONFIG_ROOT> `
  --six-method --output <NEW_REAL_RESULT>

uv run python tests/performance/provenance/calibrate_adaptive.py `
  --output docs/benchmarks/postgres-provenance-calibration-v1.json

uv run python tests/performance/provenance/evaluate_adaptive.py `
  --policy-from docs/benchmarks/postgres-provenance-calibration-v1.json `
  --output docs/benchmarks/postgres-provenance-heldout-v1.json
```

每個 output 路徑必須尚不存在。Calibration output 和 sibling `-artifacts` 目錄要一起保存；held-out
只載入已驗 immutable policy，不呼叫 fit。若 preflight、row validation、parity、instrumentation 或
performance gate 失敗，命令 exit 1；不得把失敗/缺列排除後再宣稱成功。

## 量測與驗收口徑

- Maintenance、status、impact、explain 都存 raw samples 與 p50/p95；throughput、changed/dirty/total
  counts、dirty ratio、edges/changes/heads、storage、requested/selected strategy、reason、policy version
  與 estimated costs 同列。
- 每個 repetition 使用 fresh backend state；fixture generation、initial publication、parity、storage
  與 instrumentation 不進 maintenance latency。
- PostgreSQL EXPLAIN 只保存 allowlisted plan enum/numeric timing/buffer fields；mutating statements在額外
  fresh state 的 rollback-only savepoint 觀測，不改後續 fixture。
- PostgreSQL storage 是 provenance tables 的 `pg_total_relation_size` 總和；SQLite 是 database/WAL/SHM
  bytes。物理口徑不同，不直接互相比率。
- 每個 scenario 必須 graph、hash、每個 materialized status、leaf heads、stored heads exact parity。
- Normative held-out gate：pool 完整 matched valid scenarios 的 raw samples。Adaptive aggregate p50 <=
  較佳 fixed p50 的 1.05；adaptive aggregate p95 <= 較佳 fixed p95 的 1.10。另報每-scenario ratios 與
  stricter every-scenario diagnostic；不得用它們替換 normative aggregate。

## Pending acceptance record

| Field | Current state |
|---|---|
| PostgreSQL server version | 17.11（`server_version_num` 170011）live readback；`integration-v3.json` |
| Live integration | PASS 51/51 at `d8cc334`（`integration-v3`）；Linux CI / Docker Compose host 仍缺 |
| Six-method scaled result | Absent（探路版五方法 1K–100K 見 `postgres-provenance-exploratory-v1.md`，非正式） |
| Large-scale 10K/100K/1M execution | Absent；1M memory gate v2–v4 ABORT 於 `baseline_graph_build`（非正式） |
| Real/RSNA six-method result | Absent |
| Calibration result JSON | Absent；v1 嘗試 ABORT（RAM 護欄，`calibration-v1.run.json`） |
| Policy artifact ID | Absent |
| Exact policy file SHA-256 | Absent |
| Held-out result JSON | Absent |
| Calibration/held-out overlap | Pending live evidence；offline contract要求 0 |
| Every-scenario correctness parity | Pending live evidence |
| Adaptive aggregate p50/p95 gates | Pending live evidence；未裁決 pass/fail |
| PostgreSQL latency/crossover/storage | Pending；不得填入估計值 |

填寫時必須記 command、commit、environment fingerprint、PostgreSQL numeric server version、result file
SHA、policy ID/精確 `policy.json` SHA、scenario/sample counts、parity、gate outcomes與任何 failed rows。
沒有 machine-readable result 回讀前不要更新這張表。
