---
name: vcp-provenance
description: Use when a dataset gets a new version and existing runs must be re-checked (vcp data diff, vcp provenance rebuild / sync / ingest / impact / stale / explain / status / verify-index), when choosing SQLite or the optional PostgreSQL backend, when picking --strategy incremental / full / auto or a policy, when an index reports STALE / REVIEW / BROKEN or fails closed, or when running, resuming or publishing the provenance benchmarks.
---

# Dataset evolution 與 provenance 索引

## 邊界（先記住這個）
- **真相在檔案**：台帳、card、`dataset_diff` artifact（`changes.jsonl` + `summary.json`，write-once）。
- **索引是衍生品**：SQLite（`indexes/provenance.sqlite3`，預設，可刪可重建，不進 git）與 PostgreSQL（optional，`uv sync --extra postgres`）都只是同一張圖的副本。未指定 `--backend` 永遠是 SQLite。
- 既有 evidence 消失、改寫或 ledger prefix drift → 命令 **fail closed**，不做 silent repair；人工查明後 `rebuild`。
- PostgreSQL 只透過 libpq **service** 連線（`--pg-service`，`PGSERVICEFILE` / `PGPASSFILE` 以路徑指），沒有 host / port / URI / 憑證 flag，vcp 不讀憑證檔；一個 database 一個 index；schema 不相容就換乾淨 database 重建。
- 選項以 `--help` 為準；status 語意、strategy reason、證據檔見 [reference.md](reference.md)。
- 要把索引畫成圖（全比賽、單一 run、相對某個 head）→ `vcp-provenance-graph`。
- 0.9.1 起 SQLite 索引另存 canonical graph 的 gaps；0.9.0 以前建的索引讀取時 FAIL `mismatch: graph_gaps metadata; rebuild required`——不是資料壞了，`rebuild` 一次（先講時間）。

## 資料改版的標準流程
```bash
uv run vcp data validate --name D-v1 && uv run vcp data validate --name D-v2   # 兩版都要 source_audit
uv run vcp data diff --from D-v1 --to D-v2 [--id diff-v1-v2] [--plugin projects.<c>.impact --policy P]
uv run vcp provenance ingest --artifact diff-v1-v2          # SQLite；重複 ingest 是 no-op
uv run vcp provenance impact --dataset D-v2 [--sample S]    # 下游閉包
uv run vcp provenance stale --head D-v2                     # 每個 run 相對這個 head 的 VALID/STALE/REVIEW/BROKEN
uv run vcp provenance explain --entity run:<id>             # 追 canonical 前身證據
uv run vcp provenance verify-index                          # 重算 canonical graph 與索引逐項比對
```
命名：新版是**另一個 dataset**（`D-v2`、`knee-r6-fixed`…，card 與 samples 各自獨立），舊名的 raw / card / samples 不動；`stale --head` 與 `impact --dataset` 的 head 就是新版的名字。新的 canonical row（新 run、讀數）用 `sync`；`sync` 只接受新增，看到刪改就拒絕並建議 `rebuild`。未知的 `meta.*` 變更預設 REVIEW（要人判），不是 STALE。

## PostgreSQL 與 strategy
`--backend postgresql --pg-service S`；`ingest` 另接受 `--strategy incremental|full|auto [--policy ID]`。
- 預設 `incremental`；`full` 是損毀修復、索引漂移、schema 換代的重建路徑，**不是效能選項**（100K entities 一次 75–140 s，是 canonical replay 的 7–10 倍，重建後 dead tuples 讓 storage 約 2 倍）。
- `auto` 要 frozen policy（`--policy postgres-adaptive-v1-9f4e58346529`）；沒 policy 就 WARN 並安全選 FULL。
- **已知限制**：policy v1 的信心帶是絕對毫秒，預估差距 < 7.3 s 時一律 FULL——小圖（~1K）與接近全量變更的 transition 會選錯、慢 2–4 倍。這兩種情況直接 `--strategy incremental`。
- 決策是 derived telemetry（`maintenance_decisions` 表 + VERDICT 的 requested / selected / reason），不改 status 語意。

## 基準量測（研究線）
runner 在 `tests/performance/provenance/`：`adaptive_benchmark.py`（六方法）、`calibrate_adaptive.py`、`evaluate_adaptive.py`（held-out）、`real_validation.py --six-method`；都要 `VCP_TEST_PG_SERVICE` + `PGSERVICEFILE` + `PGPASSFILE` 的 opt-in preflight，否則 fail closed。
- 有 per-repetition checkpoint 與續跑；run 合約釘住 168 個原始檔雜湊——**量測期間不能改 worktree 的任何原始檔**，否則拒絕續跑。
- 長跑用 repo 外的 `C:/vcp-data/bench/{bench_launch.py, supervise.py}`：RAM 護欄（可用 < 2 GiB 持續 30 s 中止）、機器空閒才啟動、遊戲或別的 python ≥ 4 GiB 就停並丟棄進行中場景。
- 結果只從 machine-readable 輸出回讀後才填 `docs/benchmarks/postgres-provenance-v1.md`；探路數字不填正式表。五份證據（integration v3、calibration v2、six-method v1、held-out v1、real RSNA v1）已齊全，1M 依裁決不在正式矩陣。

## 常見錯誤
- 手改或刪 `indexes/`、`artifacts/dataset_diff/` 以「修」索引：刪整個索引再 `rebuild` 才是正途。
- 把 integration 測試的 `skipped` 當 PostgreSQL 通過；把 SQLite 的歷史 crossover 當 PostgreSQL 結論。
- 在 `auto` 上對小圖抱怨慢：那是信心帶問題，不是資料問題。
