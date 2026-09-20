# PostgreSQL Adaptive Provenance 操作指南

## 權威與支援邊界

PostgreSQL backend 是 **optional**、**noncanonical**、可丟棄再建的 provenance index。canonical
authority 仍是 VCP 的 YAML/JSONL、不可變 artifact、source audit 與 append-only ledger。PostgreSQL
只提供與 SQLite 相同 graph/status/query 語意及額外的 full、incremental、adaptive maintenance；它不是
證據來源，也沒有任意 graph mutation API。

未指定 `--backend` 時，一律維持 SQLite default：

```powershell
uv run vcp provenance rebuild
uv run vcp provenance ingest --artifact DIFF_ID
```

SQLite index 仍在 `<data_root>/indexes/provenance.sqlite3`。base install 不需要 PostgreSQL driver；只有
明確選 `--backend postgresql` 才 lazy 載入 optional dependency。

## 安裝與 libpq service

```powershell
uv sync --extra postgres
```

支援 PostgreSQL 17.x；repository 的可重現 integration image 固定為 `postgres:17.11-bookworm`。VCP
只接受 libpq service 名稱；名稱必須符合 `[A-Za-z0-9_.-]{1,128}`。它沒有 host、port、database、role、
secret 或完整 connection URI 的 CLI flags。

在 operator 管理的 libpq service file 建立不含 secret 的 section，並讓 libpq 找到它：

```text
[<SERVICE>]
host=127.0.0.1
port=<PORT>
dbname=<DATABASE>
user=<ROLE>
```

若檔案不在 libpq 平台預設位置，只設定 `PGSERVICEFILE` 指到它。認證資料放在 operator 管理、權限受限
的 libpq passfile 或外部認證機制，必要時用 `PGPASSFILE` 指向 passfile；不要把內容放進 repo、命令、
artifact、benchmark output 或 VCP log。VCP 不讀這些檔案。命令可以指定：

```powershell
uv run vcp provenance status --backend postgresql --pg-service <SERVICE>
```

省略 `--pg-service` 時，Psycopg 交由 libpq standard defaults 解決連線。一個 database 在 v1 只承載
一個 active VCP provenance index；不同 data roots 應使用不同 database/service。正式 role 只需要自己
的 `vcp_provenance` schema/tables 權限；benchmark/integration harness 另需建立 disposable database 的
權限。

## 命令與 flags

八個 `vcp provenance` 命令都接受下列共用 flags：

- `--backend sqlite|postgresql`，預設 `sqlite`。
- `--pg-service <SERVICE>`，只可與 PostgreSQL 一起使用。
- `--data-root <PATH>`、`--configs-root <PATH>`。
- `--json`：result JSON 到 stdout，最後一行 `VERDICT` 到 stderr。

完整命令面如下；實際 help 是參數權威：

```powershell
uv run vcp provenance rebuild --backend postgresql --pg-service <SERVICE>
uv run vcp provenance sync --backend postgresql --pg-service <SERVICE>
uv run vcp provenance ingest --artifact DIFF_ID --backend postgresql --pg-service <SERVICE> --strategy incremental
uv run vcp provenance impact --dataset DATASET_OR_ENTITY --sample SAMPLE_ID --backend postgresql --pg-service <SERVICE>
uv run vcp provenance stale --head DATASET_OR_ENTITY --backend postgresql --pg-service <SERVICE>
uv run vcp provenance explain --entity type:id --backend postgresql --pg-service <SERVICE>
uv run vcp provenance status --backend postgresql --pg-service <SERVICE>
uv run vcp provenance verify-index --backend postgresql --pg-service <SERVICE>
```

`impact --sample` 可省略。只有 `ingest` 多兩個 flags：

- `--strategy incremental|full|auto`，預設 `incremental`。
- `--policy <ID>`，只允許 `--backend postgresql --strategy auto`。

SQLite 只支援預設 incremental request，不接受 full、auto 或 policy。PostgreSQL 的 selected strategy
可能是 `NO_OP`、`INCREMENTAL` 或 `FULL`：

- 明示 incremental/full 時，非零 semantic work 走指定演算法。
- auto 加 verified policy 時，使用 frozen cost models 與 RMSE noise band 決定。noise band 是絕對毫秒
  （policy `postgres-adaptive-v1-9f4e58346529`：2286 + 5057 ms），所以小圖（約 1K entities、預估差距不到
  7.3 s）永遠落入保守的 FULL，實測比 incremental 慢 2–4 倍；接近全量變更的 transition 也會（真實 RSNA 資料
  8.8K entities、98.7% 變更時預估差距 5.7 s → FULL，慢 1.79 倍）。這兩種情況請直接用 `--strategy incremental`，
  `auto` 留給 10K 以上、變更比例不極端的 index（`docs/benchmarks/postgres-provenance-v1.md` §六方法正式結果、
  §Real RSNA six-method，Plan 12 後記 §5、§7）。
- auto 未加 policy 時，非零 semantic work 安全選 FULL，命令回 WARN。
- full rebuild 以新 generation 原子發布後刪除舊 generation，但 PostgreSQL 在 VACUUM 前不回收那些 dead
  tuples：重建後 relation/index 約為 incremental 維護的 2 倍，`status` 也變慢，直到 autovacuum 追上。
- zero-event 不等於一定無工作；只有 verified dirty closure 也為零才是 semantic `NO_OP`。duplicate
  artifact 也以 no-op decision 留下 derived telemetry。

PostgreSQL `ingest` 的 human、JSON 與 `VERDICT` 會報 backend、requested/selected strategy、reason、
changed samples、dirty/total entities、dirty ratio、estimated costs、elapsed milliseconds、graph hash 與
policy version。它們不報 service、host、port、database、role、conninfo 或 driver exception text。

## 正常流程與 repair

1. 第一次連到乾淨 database 時執行 `rebuild`。它安裝固定 schema v1，從 canonical records 建新
   generation，核對 graph/status/head/canonical snapshot 後才原子切換 active pointer。
2. canonical run、reading、artifact 或 ledger 只有 append 時執行 `sync`。PostgreSQL v1 的 sync 是
   atomic full publication。
3. 新 `dataset_diff` artifact 用 `ingest --artifact ...`。writer transaction 先取得 advisory lock，
   commit 前重驗 diff、inputs、checkpoints 與 policy。
4. 查詢前後可用 `status`；重要決策前跑 `verify-index`，它與 canonical full replay 做 exact parity。

不要手改任何 backend。若 `status` WARN 或 `verify-index` FAIL：

- 先查 canonical evidence 是否遭刪改、ledger prefix 是否 drift、diff artifact 是否仍完整；不可用
  rebuild 掩蓋真正的 canonical corruption。
- canonical evidence 確認正確後，可對現有相容 schema 執行 `rebuild`，用全新 generation 修復衍生
  rows。
- schema marker/version 不相容時 VCP 會 fail closed，不做 silent migration。保留必要調查資料後，
  由 operator 配一個乾淨 database，再執行 `rebuild`；VCP 沒有 destructive drop/migrate command。
- 連線或 transaction 失敗不會留下 partial active generation、checkpoint、status 或 artifact rows。

## Failure 與 redaction 語意

- optional Psycopg 缺席：只有 PostgreSQL 命令 FAIL，訊息指向 `uv sync --extra postgres`；SQLite
  import/commands 不受影響。
- 不支援的 backend/strategy、錯誤的 service name 或 policy 組合：固定 ValidationFailed，exit 1，
  final `VERDICT ... status=FAIL`。
- connection/schema/timeout/driver error：transaction rollback；輸出只保留固定 reason、backend 與經
  allowlist 的 SQLSTATE。raw exception 不會串進輸出。
- explicit policy missing、mutated 或不相容：在 maintenance 前 FAIL。auto 沒給 policy 才使用安全
  fallback，且命令為 WARN。
- missing/mutated canonical evidence、snapshot drift、checkpoint prefix drift：fail closed；不會部分發布。
- `--json` 不改 exit code 或狀態語意，stdout 只放結構化結果；所有敏感第三方文字在落地前 redact。

## Integration harness

普通 test suite 不會啟動 Docker。明確 opt-in 的 shared runner 從 repository 任意目錄執行：

```powershell
& tests/integration/postgres/run.ps1
```

它要求 Docker Compose v2 與 `uv`，使用 localhost-only PostgreSQL 17.11 container、ephemeral volume、
隨機 runtime credential、temporary protected service/pass files，執行 `pytest -m postgres`，並在
`finally` 清除 container、volume、files 和 process environment。它不顯示 Compose diagnostics。沒有
runtime 時固定 exit 1；直接跑未配置的 marked tests會明確 skip，skip 不是 PostgreSQL pass。

## Benchmark、calibration 與 held-out reproduction

這三個 experiment runners 有自己的 **separate opt-in preflight**，不是一般 VCP command 或 libpq
defaults 的替代。執行 benchmark、calibration 或 held-out 前，operator 必須設定全部三個：
`VCP_TEST_PG_SERVICE`（disposable service name）、`PGSERVICEFILE` 與 `PGPASSFILE`；後兩者必須是
operator 建立、權限受限的 **absolute temporary file paths**。不要在命令列、repo、result JSON、log 或
本文件寫入其內容。這個 disposable service 必須允許 benchmark runner 建立 fresh databases，也就是有
`CREATE DATABASE` privilege；普通 VCP/libpq CLI 仍可依自己的 service/default resolution 運作，不能假定
它已滿足此 experiment preflight。

六方法 scaled matrix 比較 canonical full、SQLite full/incremental、PostgreSQL full/incremental/adaptive：

```powershell
uv run python tests/performance/provenance/adaptive_benchmark.py `
  --entities 1000 10000 100000 `
  --ratios 0 0.001 0.01 0.05 0.10 0.25 0.50 0.90 1 `
  --seeds 20260913 20260914 `
  --output <NEW_RESULT_PATH>
```

1K/10K 每 scenario 固定 7 repetitions，100K 固定 3；不要用 `--repetitions` override 產生 normative
calibration/held-out evidence。每個 method/repetition 使用 fresh state；mutating EXPLAIN 在額外 state 的
explicit rollback transaction 內執行，不進 latency samples。

```powershell
uv run python tests/performance/provenance/calibrate_adaptive.py `
  --output docs/benchmarks/postgres-provenance-calibration-v1.json

uv run python tests/performance/provenance/evaluate_adaptive.py `
  --policy-from docs/benchmarks/postgres-provenance-calibration-v1.json `
  --output docs/benchmarks/postgres-provenance-heldout-v1.json
```

Calibration 只用 seeds `20260913/20260914` 的 fixed PostgreSQL methods，完整驗證 108 scenarios 後從
92 個 positive-change pairs fit nonnegative models；16 個 NO_OP scenarios仍屬 coverage/leakage evidence，
不進 fit。Held-out 只用 seeds `20261001/20261002`，在另一 process 驗 immutable policy，禁止 fit，並以
scenario ID/hash/workload hash 拒絕 leakage。兩個 output 都是 write-once；policy artifact 在 calibration
output 的 sibling `-artifacts` data root。

效能 gate 使用完整 matched held-out rows 的 raw maintenance samples：adaptive aggregate p50 不得比同
percentile 較佳 fixed method 慢超過 5%，p95 不得慢超過 10%；每個 scenario 的 canonical graph/hash、
status、heads 必須 100% parity。失敗結果仍保存並 exit 1，不可用 held-out refit。

本 repository 目前只有 offline contract/synthetic-fixture evidence。live PostgreSQL、完整 large-scale
matrix、calibration、held-out、policy identity 與 live RSNA six-method結果都尚未產生；證據表與待填欄位
見 `docs/benchmarks/postgres-provenance-v1.md`。
