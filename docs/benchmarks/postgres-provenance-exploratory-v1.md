# PostgreSQL Provenance 探路矩陣 v1（2026-09-15，非正式）

給 Data_Base 專題 agent 的回報。這是**探路版**：1K / 10K / 100K 三個規模、每場景 1 次重複、五個固定方法，
不含 1M、不含 `postgres_adaptive`。它回答「形狀」與「量尺」，**不是** calibration / held-out 證據，
也不能拿來裁決 adaptive gate。正式驗收欄位的狀態仍以 `postgres-provenance-v1.md` 的 pending 表為準。

- 機器可讀結果：`postgres-provenance-exploratory-v1.json`（540 列、每列 p50/p95、吞吐、儲存、parity）
- EXPLAIN：`postgres-provenance-exploratory-v1-explain.json`（216 個 PostgreSQL 列、每列約 13 個計畫）
- 程式碼：vcp `f7fe955`（v0.8.0 + 串流 replay）；runner 為 `tests/performance/provenance/adaptive_benchmark.py`
  的 `run_matrix_isolated`（每場景獨立子程序、逐列 checkpoint），由 scratch 腳本以 `repetitions=1` 驅動

## 1. PostgreSQL 實際版本與硬體

| 項目 | 值 |
|---|---|
| PostgreSQL | 17.11（`server_version_num` 170011），原生 Windows 服務（`deployment_kind=native_portable`，非 Docker），只聽 `127.0.0.1:55432` |
| CPU | AMD Ryzen 7 9700X，8 核 / 16 執行緒 |
| RAM | 31.2 GiB；跑的期間可用 RAM 5.5 → 最低 2.8 GiB（護欄 2 GiB / 30 秒，未觸發） |
| 磁碟 | C:（NVMe，1 TB），剩約 143 GB |
| OS / Python / SQLite | Windows 11 10.0.26200 / CPython 3.12.14 / SQLite 3.53.1 |
| 環境指紋 | `f29fc1bb…7217f`（`environment.environment_fingerprint`） |
| 總時間 | 8 小時 34 分（30,827 秒），2026-09-15 14:16 UTC 起 |

## 2. Live integration tests

來源 `postgres-provenance-integration-v3.json`（commit `d8cc334`）：**51 passed / 0 failed / 0 error / 0 skipped**。
前兩版：v1 FAIL（39 passed、4 behavior failures，修正前）、v2 ERROR（51 setup errors，服務被回收）。

## 3. transaction rollback、concurrency、MVCC、parity

| 類別 | 測試數 | 結果 |
|---|---:|---|
| transaction rollback | 28 | PASS |
| concurrency（advisory lock 序列化重複 writer） | 1 | PASS |
| MVCC（reader 在 rebuild commit 前仍看舊 generation） | 1 | PASS |
| canonical / SQLite / PostgreSQL parity（每種 strategy） | 3 | PASS |

## 4. 五方法 benchmark（1K / 10K / 100K；1M 未跑）

Maintenance p50（ms，18 個 ratio × topology × seed 組合的中位數；1 次重複，p95 = p50）：

| entities | canonical_full | sqlite_full | sqlite_incremental | postgres_full | postgres_incremental |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 115 | 286 | 85 | 734 | 236 |
| 10,000 | 936 | 2,246 | 761 | 7,187 | 1,417 |
| 100,000 | 11,335 | 26,399 | 9,036 | 100,211 | 18,848 |

Graph 大小：1K = 1,001 entities / 1,330 edges；10K = 10,001 / 13,330；100K = 100,001 / 133,330。

PostgreSQL incremental vs full 依變更比例（p50 ms，topology × seed 中位數）：

| ratio | 1K inc | 1K full | 10K inc | 10K full | 100K inc | 100K full | inc/full @100K |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 (NO_OP) | 167 | 159 | 706 | 777 | 7,291 | 7,296 | 1.00 |
| 0.001 | 164 | 166 | 1,033 | 6,970 | 12,271 | 96,411 | 0.13 |
| 0.01 | 183 | 702 | 1,057 | 6,653 | 13,217 | 93,118 | 0.14 |
| 0.05 | 197 | 692 | 1,224 | 7,364 | 15,234 | 96,112 | 0.16 |
| 0.10 | 213 | 734 | 1,365 | 6,948 | 18,848 | 95,291 | 0.20 |
| 0.25 | 266 | 746 | 1,884 | 6,841 | 27,080 | 110,147 | 0.25 |
| 0.50 | 327 | 790 | 2,716 | 8,067 | 38,557 | 115,525 | 0.33 |
| 0.90 | 446 | 898 | 4,018 | 9,193 | 56,519 | 130,972 | 0.43 |
| 1.00 | 476 | 906 | 4,187 | 9,046 | 57,485 | 128,714 | 0.45 |

1K ratio 0.001 的變更數四捨五入為 0 列，因此與 ratio 0 一樣是 NO_OP。

## 5. calibration 與 held-out 分離證據

**未產生。** `calibrate_adaptive.py` / `evaluate_adaptive.py` 的 `expected_scenarios` 釘死完整 1K–1M 矩陣
（每 split 144 場景，1K/10K 各 7 次、100K/1M 各 3 次）；1M 在這台機器仍會被 RAM 護欄擋下（Codex 09-14
的 memory gate v2–v4）。要嘛換機器跑完整規格，要嘛以裁決縮小正式矩陣（去掉 1M）再跑；兩者都尚未做。
離線契約測試仍保證 calibration seeds（20260913/20260914）與 held-out seeds（20261001/20261002）零重疊。

## 6. adaptive policy ID、hash 與 crossover

- policy ID / SHA：**無**（沒有 calibration 就沒有 policy，`postgres_adaptive` 因此未跑）。
- 探路矩陣的 crossover：**在 ratio > 0 的每一格，incremental 都比 full 快**（12 個 topology × entities × seed
  切片全部如此；`crossover[*].first_full_preferred_ratio` 只在 ratio 0 或 NO_OP 格出現）。也就是說，在這個
  workload 產生器（每個變更列都帶下游 run / reading / fusion / submission 拓樸）下，100% 變更的 incremental
  仍只需 full 的 45%，因為 full 要重掃 canonical 檔案、重建整個 generation、重算所有狀態並原子換 pointer。
- 對 adaptive 的意涵：policy 在這個資料上會退化成「NO_OP 或 incremental」；full 只有在 diff 事件本身損毀、
  index 漂移或 schema 不相容時才有理由被選。正式 calibration 若結果相同，這就是要寫進報告的結論，而不是
  硬找一個 threshold。

## 7. Real RSNA 實驗

**PostgreSQL 六方法未跑**（`real_validation.py --six-method` 需要 frozen policy）。既有的 canonical + SQLite
真資料驗證見 Codex 的 `provenance-real-2026-09-13.json`（唯讀 metadata 複本，13,196 entities / 17,569 edges /
8,752 changes，graph 與 status parity 皆 true）。

## 8. p50 / p95 latency、throughput、index size

- Latency：見 §4；1 次重複所以 p95 = p50，正式版才會有真正的 p95。
- Throughput（樣本列 / 秒，中位數）：

| entities | sqlite_incremental | postgres_incremental | postgres_full |
|---:|---:|---:|---:|
| 1,000 | 879 | 309 | 110 |
| 10,000 | 636 | 336 | 82 |
| 100,000 | 526 | 231 | 48 |

- 查詢（100K，p50 ms）：status 269 / 721 / 2,118（sqlite_inc / pg_inc / pg_full；canonical 6,097）；impact 與
  explain 各方法都在 4.7–6.9 秒，主要是 canonical 讀取成本。
- 儲存（中位數 MB；PostgreSQL 為 provenance tables 的 `pg_total_relation_size` 總和，`index_bytes` 為
  `pg_indexes_size`；SQLite 為 db + WAL + SHM；口徑不同不可直接相除）：

| entities | sqlite（db） | postgres_full（db / index） | postgres_incremental（db / index） |
|---:|---:|---:|---:|
| 1,000 | 6.0 | 34.5 / 19.3 | 20.9 / 8.5 |
| 10,000 | 60.0 | 266 / 183 | 129 / 82 |
| 100,000 | 602 | 2,582 / 1,897 | 1,209 / 818 |

postgres_full 的量測包含新舊兩個 generation（rebuild 先建新 generation 再切 pointer），所以約是 incremental
的兩倍。

## 9. EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)

`postgres-provenance-exploratory-v1-explain.json`：216 個 PostgreSQL 列（108 場景 × full / incremental），
每列約 13 個計畫，對應「每種 DML SQL 形狀的第一列」，在額外的 rollback-only transaction 內取得，不進
latency 樣本；只保留 allowlisted 的 plan / timing / buffer 欄位，沒有 SQL 文字、連線資訊或憑證。它不涵蓋
status / impact / explain 查詢本身的計畫（runner 的既有限制）。

## 10. SQLite、PostgreSQL 與 canonical replay 的一致性

540 / 540 列 `graph_parity`、`graph_hash_parity`、`status_parity`、`head_parity` 全部 true（每列都與同一場景
的 canonical full replay 逐 record 比對 graph、graph hash、每個 materialized status 與 heads）。

## 尚缺與下一步

1. 1M：需要 ≥128 GB 記憶體的機器，或先量測 100K 之後的記憶體斜率再決定；本機不再嘗試。
2. 正式矩陣（每場景 7/7/3 次）：本機 1K–100K 約 30 小時；需先裁決是否把 1M 拿出 `expected_scenarios`。
3. calibration → six-method（含 adaptive）→ held-out → real RSNA six-method，順序不能變（後三者都要 policy）。
