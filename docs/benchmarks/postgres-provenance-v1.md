# PostgreSQL Adaptive Provenance Benchmark v1

## 結論與證據邊界

此文件是可重跑方法與 acceptance record：每一格只在 machine-readable 結果回讀後才填。2026-09-14 起本機有
原生 PostgreSQL 17.11（使用者排程工作 `VCP-PostgreSQL-17.11`，只聽 `127.0.0.1:55432`，service
`vcp-pg17-evidence`；佈建紀錄在 `C:/vcp-data/services/postgresql-17.11-vcp/`，不進 git）。目前已有
**live integration**（v3）、**正式 calibration**（v2，policy `postgres-adaptive-v1-9f4e58346529`）與
**正式 six-method / large-scale**（1K–100K，2026-09-18，§六方法正式結果）三份證據；仍沒有 **held-out**
outcome、adaptive gate 的正式 pass/fail 裁決與 RSNA six-method 數字。這些欄位不可從 offline doubles、
SQLite 結果或 calibration split 推導。

## Live evidence（2026-09-14，本機原生 PostgreSQL 17.11）

每個檔案寫一次不改；後續嘗試以新的 `-vN` 檔案接續並在 `predecessor` 欄位指回前一版。

| 檔案 | 結果 | SHA-256（前 8 碼） |
|---|---|---|
| `postgres-provenance-integration-v1.json` / `.xml` | FAIL：39 passed、4 behavior failures（checkpoint 測試與 schema 修正前） | `b9d289dd` / `24726f40` |
| `postgres-provenance-integration-v2.json` / `.xml` | ERROR：51 setup errors（PostgreSQL 程序在跑前被回收） | `04be858b` / `367e0460` |
| `postgres-provenance-integration-v3.json` / `.xml` | PASS 51/51 at `d8cc334`：rollback 28、concurrency 1、MVCC 1、canonical/SQLite/PostgreSQL parity 3；server 17.11（170011） | `b55f34fd` / `b50ada0f` |
| `postgres-provenance-calibration-v1.run.json` / `.log.txt` | ABORT `resource_guard_available_ram`：可用 RAM 低於 2 GiB 持續 30 秒；runner private bytes 峰值 16.3 GB；沒有正式 calibration JSON，沒有 policy | `5c60b75c` / `dc71b39e` |
| `postgres-provenance-1m-memory-gate-v2.json`、`-v3.json`、`-v4.json` | 非正式單場景資源護欄，皆 ABORT `system_available_ram_lt_2_gib_sustained_30s`；v4 的 historical diff streaming 已完成（該階段 private ≈0.52 GiB），止於 `baseline_graph_build`（peak private 5.67 GiB、最低可用 RAM 0.556 GiB）；v1 未保存 | `724634e8` / `4672be87` / `a2c295df` |

| `postgres-provenance-calibration-v2.json` / `-artifacts/` / `.provenance.json` | **正式 calibration**（2026-09-16，14.5 小時，護欄未觸發，最低可用 RAM 2.63 GiB）：108 場景 × postgres_full / postgres_incremental × 7/7/3 次 = 1224 次量測，全部 ok；92 個 fit 觀測 → policy `postgres-adaptive-v1-9f4e58346529`，`policy.json` SHA-256 `a22d7067…03a2d78`；**12 個切片全部 `not_observed`、`global_threshold: null`**（incremental 在每個非零 ratio 都快，100K 時 p50 為 full 的 13–50%）。量測在 `b1512ae`；發布時只有消費端 `evaluate_adaptive.py` 改了（見 §發布註記），`.provenance.json` 記 168 個生產端檔案逐一比對相同 | `43d213e4` / `a22d7067` |
| `postgres-provenance-exploratory-v1.json` / `-explain.json` / `.md` | 探路矩陣（非正式）：1K/10K/100K × 9 ratios × 2 topologies × 2 seeds = 108 場景、每場景 1 次、五個固定方法；540/540 parity；ratio > 0 每格 incremental 都快於 full（100K 時為 full 的 13–45%）；1M 與 adaptive 未跑 | `98247c28` / `451550ee` |
| `postgres-provenance-six-method-v1.json` | **正式 six-method**（2026-09-17/18，calibration split，frozen policy `postgres-adaptive-v1-9f4e58346529`）：108 場景 × 6 方法 × 7/7/3 次 = 648 列、3672 次量測，全部 `ok`、0 failure；graph / hash / head parity 648/648；12 個切片 crossover 仍 `not_observed`；adaptive 在 10K/100K 選 INCREMENTAL（64 列）、在 1K 全部選 FULL（28 列，信心帶問題，見 §六方法正式結果）、NO_OP 16 列。量測在 `6ab2b7a`，runner 直接發布（未經 checkpoint 外部發布）。EXPLAIN 內嵌於 324 個 PostgreSQL 列 | `47bc10ab` |

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
- 在 opt-in preflight 未配置的環境，live runner、benchmark、calibrator、evaluator 都 fail closed，不留 result
  JSON 或 policy artifact；本機已配置，benchmark（six-method v1）與 calibrator（calibration v2）的結果見上表，
  evaluator（held-out）尚未執行。

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

固定 entity scales 為 1K、10K、100K；change ratios 為 0%、0.1%、1%、5%、10%、25%、50%、
90%、100%；topology 為 chain/branched。Calibration seeds 是 `20260913`、`20260914`，held-out seeds
是 `20261001`、`20261002`。每個 split 共 108 scenarios；1K/10K 各 7 repetitions，100K 各 3，
每 method 共 612 measured samples。1M 已於 2026-09-15 以裁決移出正式矩陣（後記 §1），仍可用
`--entities 1000000` 臨時跑；`production_benchmark.py` 的 1M 階梯不受影響。

Real track 的來源必須唯讀複製到 temporary metadata root；它不修改 live data。Task 10 runner也可讓
既有 production/real entrypoint增加 `--six-method`，但本環境沒有執行 live RSNA track。

## 發布註記：calibration v2 是從 checkpoint 發布的

正式 calibration 在 2026-09-16 跑完全部 1224 次量測後，`calibrate_adaptive.py` 在發布階段回 FAIL：
`runtime_environment()` 自 `3bde664` 起輸出 `system_release` / `system_version`，但 `evaluate_adaptive.py`
的 strict `_Environment` 模型沒宣告這兩個欄位，`BenchmarkRow.model_validate` 對 216 列全部 `extra_forbidden`。
離線測試用手寫的合成環境，所以從未撞到；`test_every_runtime_environment_key_is_accepted_by_the_row_model`
現在把這條生產者／消費者契約釘死。

處置：補上模型欄位後，**不重跑**量測，改以 `publish_from_checkpoints.py`（保存在 `C:/vcp-data/bench/`，不進
repo）從同一個 checkpoint store 發布。發布前它逐檔比對 run 合約釘住的 168 個原始檔雜湊與當下的樹：只有
`tests/performance/provenance/evaluate_adaptive.py` 不同，`src/vcp/**`、`adaptive_benchmark.py`、
`workloads.py`、`calibrate_adaptive.py`、`pyproject.toml`、`uv.lock` 全部逐位元相同，216 列都在 `b1512ae`
量到，才呼叫未改動的 `publish_calibration`。比對結果與 launcher 摘要記在 `.provenance.json`。

代價：runner 的「resume 時原始樹必須完全一致」守門被一次性繞過；若日後懷疑量測程式在跑的期間變過，
`.provenance.json` 的逐檔比對就是反證，否則只能重跑 14.5 小時。

## 六方法正式結果（2026-09-18，1K–100K，calibration split）

### 執行事實

- 命令：`uv run --frozen python tests/performance/provenance/adaptive_benchmark.py --policy-from
  docs/benchmarks/postgres-provenance-calibration-v2.json --output
  docs/benchmarks/postgres-provenance-six-method-v1.json --work-dir C:/vcp-data/bench/six-method-v1-work`，
  經 `bench_launch.py`（PostgreSQL env 只以路徑設定；RAM 護欄可用 < 2 GiB 持續 30 秒即中止）。
- 量測 commit `6ab2b7a34dd8fcd3027ff22febffdbbe210d9fd9`（648 列同一 commit）；policy
  `postgres-adaptive-v1-9f4e58346529`，`policy.json` SHA-256 `a22d7067…03a2d78`；environment fingerprint
  `f29fc1bb…`；PostgreSQL 17.11（170011，native_portable）；Python 3.12.14；AMD Ryzen 7 9700X、31 GiB。
- 輸出 `postgres-provenance-six-method-v1.json` SHA-256
  `47bc10ab19f20882e1afbb128290329d75bbb1565ec7ebef6dff03689be7395c`（26.6 MB，含 324 個 PostgreSQL 列的
  `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 原始與 sanitized 計畫）。
- 108 場景 × 6 方法 = 648 列，全部 `status=ok`、`failure=null`；每方法 612 個 timed samples（1K/10K 各 7
  次、100K 3 次），共 3672 次。`graph_parity`、`graph_hash_parity`、`head_parity` 648/648 為 true，
  `parity_rate` 全為 1.0。16 列 NO_OP（ratio 0 全部 + 1K ratio 0.001）在 sqlite_incremental、postgres_full、
  postgres_incremental、postgres_adaptive 四個方法都以 `verified_zero_semantic_changes` 略過；canonical_full
  與 sqlite_full 一律 FULL。
- 消費端回讀（發布後另做，不在 run 內）：324 個 PostgreSQL 列通過 `evaluate_adaptive.validate_rows`（held-out
  evaluator 的 strict 模型、場景計數、p50/p95 重算、EXPLAIN sanitize 一致性、fixed-method decision 一致性）；
  108 個 `postgres_adaptive` 的 selected strategy / reason / estimated costs 全部可由凍結的
  `policy.json` 以 `select_strategy("auto", …)` 重算得到相同值，`policy_sha256` 與檔案一致。
- 執行過程：由 `C:/vcp-data/bench/supervise.py`（不進 repo）看管，共啟動 5 次、被停 4 次：09-17 17:50 UTC
  看管程式把 NVIDIA Overlay 誤判為大型程式（之後加入忽略清單）；09-18 01:09 偵測到遊戲（5.1 GiB）；09-18
  12:26 launcher 護欄（可用 RAM 最低 1.27 GiB）；09-18 13:41 看管程式護欄（1.9 GiB）。每次停止都刪除進行中
  場景的 checkpoint（`188d188c…`、`591ae9c9…`、`9b19ff86…` 兩次）並 drop 用完即棄的 `vcp_bench_*`
  database，已完成場景不重量。續跑前 runner 逐一驗證 run 合約釘住的 168 個原始檔雜湊；整個期間原始樹未變。
  低記憶體的根因是主機核心 nonpaged pool 洩漏到 7.6 GiB（8.6 天未重開機），16:27 UTC 重開機後最後一次
  16:31–18:47 跑完剩餘 5 個場景，該段最低可用 RAM 13.5 GiB。五次合計約 29 小時（含每次續跑的 fixture 重播）。
  背景一直有一般桌面程式（瀏覽器、通訊軟體）；沒有任何場景在偵測到遊戲或低記憶體的狀態下完成。

### Maintenance latency（p50，ms；每格為 chain/branched × 2 seeds 四個場景的中位數）

**1K**

| ratio | canonical_full | sqlite_full | sqlite_incr | pg_full | pg_incr | pg_adaptive | adaptive 選擇 |
|---|---|---|---|---|---|---|---|
| 0 | 111 | 265 | 65.5 | 150 | 148 | 163 | NO_OP ×4 |
| 0.001 | 113 | 272 | 66.2 | 155 | 158 | 166 | NO_OP ×4 |
| 0.01 | 115 | 272 | 69.9 | 663 | 181 | 696 | FULL ×4 |
| 0.05 | 117 | 284 | 78.4 | 673 | 198 | 692 | FULL ×4 |
| 0.1 | 121 | 288 | 86.3 | 701 | 215 | 686 | FULL ×4 |
| 0.25 | 128 | 301 | 98.0 | 725 | 265 | 751 | FULL ×4 |
| 0.5 | 130 | 321 | 114 | 766 | 322 | 784 | FULL ×4 |
| 0.9 | 136 | 340 | 134 | 856 | 417 | 887 | FULL ×4 |
| 1 | 140 | 347 | 154 | 876 | 448 | 896 | FULL ×4 |

**10K**

| ratio | canonical_full | sqlite_full | sqlite_incr | pg_full | pg_incr | pg_adaptive | adaptive 選擇 |
|---|---|---|---|---|---|---|---|
| 0 | 792 | 2,015 | 606 | 702 | 710 | 721 | NO_OP ×4 |
| 0.001 | 781 | 1,995 | 634 | 6,368 | 1,055 | 1,070 | INCREMENTAL ×4 |
| 0.01 | 810 | 2,043 | 650 | 6,433 | 1,040 | 1,070 | INCREMENTAL ×4 |
| 0.05 | 854 | 2,141 | 726 | 6,801 | 1,258 | 1,258 | INCREMENTAL ×4 |
| 0.1 | 854 | 2,175 | 763 | 6,787 | 1,419 | 1,427 | INCREMENTAL ×4 |
| 0.25 | 918 | 2,245 | 890 | 7,118 | 1,899 | 1,961 | INCREMENTAL ×4 |
| 0.5 | 1,051 | 2,602 | 1,193 | 7,738 | 2,924 | 2,920 | INCREMENTAL ×4 |
| 0.9 | 1,253 | 2,955 | 1,545 | 9,169 | 4,131 | 4,145 | INCREMENTAL ×4 |
| 1 | 1,278 | 3,077 | 1,648 | 9,343 | 4,246 | 4,253 | INCREMENTAL ×4 |

**100K**

| ratio | canonical_full | sqlite_full | sqlite_incr | pg_full | pg_incr | pg_adaptive | adaptive 選擇 |
|---|---|---|---|---|---|---|---|
| 0 | 9,631 | 23,768 | 7,630 | 7,171 | 7,010 | 6,671 | NO_OP ×4 |
| 0.001 | 9,855 | 22,853 | 7,760 | 89,170 | 12,050 | 11,776 | INCREMENTAL ×4 |
| 0.01 | 9,921 | 23,966 | 7,874 | 85,416 | 12,666 | 12,485 | INCREMENTAL ×4 |
| 0.05 | 10,073 | 24,096 | 8,652 | 87,697 | 14,481 | 14,799 | INCREMENTAL ×4 |
| 0.1 | 10,394 | 25,006 | 9,154 | 88,469 | 17,603 | 17,912 | INCREMENTAL ×4 |
| 0.25 | 10,493 | 26,474 | 10,937 | 96,426 | 25,523 | 24,700 | INCREMENTAL ×4 |
| 0.5 | 11,867 | 29,050 | 14,189 | 112,101 | 35,872 | 35,198 | INCREMENTAL ×4 |
| 0.9 | 13,994 | 34,351 | 18,729 | 118,614 | 54,344 | 51,148 | INCREMENTAL ×4 |
| 1 | 14,679 | 34,471 | 19,497 | 120,891 | 50,511 | 55,681 | INCREMENTAL ×4 |

ratio 0 那一列的 postgres_full / postgres_incremental / postgres_adaptive 是 NO_OP（只驗證零變更，不重建），
不是 full rebuild 的成本；100K 的 pg_full ratio 0（7.2 s）與 ratio 0.001（89 s）之差就是一次全量重建。
逐場景的 p50/p95 與 raw samples 在 JSON。

### 查詢延遲、儲存與吞吐（非零 ratio 場景的中位數）

`impact` 與 `explain` 延遲和 CLI 一樣包含 graph load；`status` 是 public status API；storage 的
PostgreSQL 欄是 `vcp_provenance` 全部 relation 的 `pg_total_relation_size` 總和，SQLite 欄是 database
bytes，兩者物理口徑不同。

| scale | method | impact p50/p95 ms | status p50/p95 ms | EXPLAIN p50/p95 ms | index MiB | storage MiB | throughput samples/s |
|---|---|---|---|---|---|---|---|
| 1K | canonical_full | 56.0 / 74.1 | 55.9 / 71.6 | 51.6 / 67.1 | — | — | 418 |
| 1K | sqlite_full | 22.5 / 35.2 | 4.6 / 5.7 | 17.0 / 31.1 | — | 6 | 190 |
| 1K | sqlite_incr | 22.5 / 36.7 | 3.6 / 4.9 | 17.2 / 28.6 | — | 6 | 569 |
| 1K | pg_full | 64.6 / 82.7 | 27.5 / 37.1 | 60.8 / 94.0 | 18 | 25 | 80 |
| 1K | pg_incr | 66.2 / 88.3 | 27.8 / 47.9 | 59.7 / 78.5 | 8 | 12 | 227 |
| 1K | pg_adaptive | 65.8 / 87.0 | 27.7 / 39.5 | 60.9 / 89.2 | 18 | 25 | 77 |
| 10K | canonical_full | 521 / 573 | 530 / 587 | 404 / 524 | — | — | 609 |
| 10K | sqlite_full | 374 / 414 | 27.9 / 69.1 | 273 / 299 | — | 59 | 246 |
| 10K | sqlite_incr | 373 / 403 | 25.4 / 66.7 | 268 / 307 | — | 60 | 657 |
| 10K | pg_full | 538 / 621 | 99.3 / 166 | 441 / 512 | 181 | 247 | 81 |
| 10K | pg_incr | 545 / 623 | 82.0 / 153 | 454 / 543 | 81 | 119 | 329 |
| 10K | pg_adaptive | 557 / 621 | 69.3 / 128 | 442 / 535 | 81 | 119 | 331 |
| 100K | canonical_full | 6,134 / 6,276 | 6,127 / 6,692 | 4,750 / 5,229 | — | — | 537 |
| 100K | sqlite_full | 4,560 / 4,833 | 257 / 579 | 3,211 / 3,383 | — | 590 | 221 |
| 100K | sqlite_incr | 4,591 / 4,645 | 263 / 602 | 3,179 / 3,389 | — | 596 | 546 |
| 100K | pg_full | 6,523 / 6,764 | 2,117 / 2,360 | 4,720 / 5,052 | 1,826 | 2,467 | 61 |
| 100K | pg_incr | 7,249 / 7,610 | 731 / 784 | 4,780 / 5,047 | 812 | 1,188 | 232 |
| 100K | pg_adaptive | 7,175 / 7,423 | 718 / 805 | 4,818 / 5,207 | 812 | 1,188 | 247 |

### 觀察（只列量到的事）

1. **沒有交會點。** `empirical_crossover` 12/12 切片 `status: not_observed`、`last_incremental_preferred_ratio:
   1.0`、`global_threshold: null`，與 calibration v2 一致。排除 NO_OP 後，postgres_incremental 的 p50 是
   postgres_full 的 1K 27–56%（中位 37%）、10K 16–50%（24%）、100K 13–52%（23%）；連 100% 變更也是
   incremental 快，因為 changed_samples 最多約 0.25·edges，而 full 多付 0.586·edges 的固定成本。
2. **adaptive 在 1K 全部選 FULL（28 列），在 10K/100K 全部選 INCREMENTAL（64 列）。** 這不是 §發布註記所預告的
   「非零工作永遠 INCREMENTAL」。`select_strategy` 只在
   `incremental_ms + incremental_rmse < full_ms − full_rmse` 時才選 INCREMENTAL，兩個 RMSE 是絕對值
   （2286 + 5057 = 7343 ms，由 100K 殘差主導）；1K 的預估差距只有 842–845 ms，10K 是 8448–8477 ms，所以 1K
   永遠落入「不確定 → FULL」分支。代價：1K 的 adaptive p50 是 postgres_incremental 的 1.82–3.97 倍（0.68–0.90 s
   對 0.18–0.49 s，絕對值小但方向錯）；10K/100K 的 adaptive 與 incremental 差在 −10%…+14% 的重複雜訊內
   （中位 100–101%）。處置見 Plan 12 後記 §5：policy 維持凍結、held-out 照跑，信心帶改成規模相對是 policy
   v2 的 spec 變更。
3. **Gate 預覽（calibration split，非正式）：** 以 92 個非 NO_OP 場景的 raw samples pool，adaptive aggregate
   p50 = 較佳 fixed（postgres_incremental）p50 的 1.010（門檻 1.05）、p95 = 0.932（門檻 1.10）——aggregate
   會過。every-scenario diagnostic 有 31/92 場景 > 1.05：1K 的 28 個（1.82–3.97）加 10K/100K 各 1–2 個
   （1.06–1.14，雜訊）。正式 gate 只認 held-out；這裡的數字是在同一個 split 上量的，不能拿來裁決。
4. **PostgreSQL 全量重建是每個規模最慢的維護路徑：** 100K 一次 75–140 s，是 canonical replay（9.3–14.9 s）的
   7.4–10.1 倍、SQLite full（22.6–35.1 s）的 3.0–4.1 倍。它要把整張圖以 `INSERT … ON CONFLICT` 寫進正規化表並
   維護 13 個索引，再切換 generation pointer。全量重建後 storage 約為 incremental 的 2 倍（100K：relation 2.05–2.90 GiB 對
   1.07–1.57 GiB，index 1.44–2.16 GiB 對 0.73–1.08 GiB）：舊 generation 的列已 `DELETE` 但在 VACUUM 前仍是
   dead tuples 佔空間，同一原因讓重建後的 `status` p50 變成 2.1 s（incremental 後 0.73 s）。這是原子 generation
   切換的 MVCC 成本，屬於這個 backend 的量測結論，不是 SQLite 對照組的缺陷。
5. **SQLite incremental 是每個規模最快的維護路徑**（100K：7.5–19.8 s；postgres_incremental 是它的 1.5–3.5 倍，
   中位 2.1 倍）。在這台單 writer 主機上 PostgreSQL 的價值不是原始速度，而是 advisory lock 序列化、MVCC
   讀者、原子 generation 發布與整合測試驗過的 rollback / concurrency 行為。查詢面（incremental 對
   incremental）：`impact` / `explain`（含 graph load）在 10K/100K 時 PostgreSQL 為 SQLite 的 1.3–2.3 倍，1K
   時 1.9–4.1 倍（固定開銷）；`status` 在 1K 慢 6–10 倍（27 ms 對 4 ms，連線與 round-trip），10K/100K 為
   0.6–6.4 倍，重建後的 dead tuples 使 pg_full 的 100K `status` 最多達 SQLite 的 10 倍。
6. **EXPLAIN 覆蓋面：** 324 個 PostgreSQL 列各帶「每種 SQL 形狀第一個執行的 DML 列」的
   `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`（rollback-only、獨立 fresh state、不計入 timing；原始與
   allowlisted/sanitized 兩份）；`explain_p50/p95_ms` 是 CLI `explain` 查詢本身的延遲，不是計畫。`status` /
   `impact` 查詢自身的計畫仍未涵蓋（後記 §3-4）。

## 可重跑命令

先依 `docs/guides/POSTGRESQL_PROVENANCE.md` 完成 benchmark/calibration/held-out 的 separate opt-in
preflight：全部設定 `VCP_TEST_PG_SERVICE`、`PGSERVICEFILE`、`PGPASSFILE`，後兩者使用權限受限的
absolute temporary file paths。這不是 ordinary VCP/libpq defaults；disposable service 必須有
`CREATE DATABASE` privilege。命令本身不得包含 connection material 或任何檔案內容。

```powershell
uv run python tests/performance/provenance/adaptive_benchmark.py `
  --entities 1000 10000 100000 `
  --ratios 0 0.001 0.01 0.05 0.10 0.25 0.50 0.90 1 `
  --seeds 20260913 20260914 `
  --policy-from docs/benchmarks/postgres-provenance-calibration-v2.json `
  --output <NEW_SIX_METHOD_RESULT> [--work-dir <CHECKPOINT_DIR>]

uv run python tests/performance/provenance/production_benchmark.py `
  --six-method --output <NEW_PRODUCTION_RESULT>

uv run python tests/performance/provenance/real_validation.py `
  --data-root <READ_ONLY_SOURCE_ROOT> --configs-root <READ_ONLY_CONFIG_ROOT> `
  --six-method --output <NEW_REAL_RESULT>

uv run python tests/performance/provenance/calibrate_adaptive.py `
  --output <NEW_CALIBRATION_RESULT>

uv run python tests/performance/provenance/evaluate_adaptive.py `
  --policy-from docs/benchmarks/postgres-provenance-calibration-v2.json `
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

## Acceptance record

| Field | Current state |
|---|---|
| PostgreSQL server version | 17.11（`server_version_num` 170011）live readback；`integration-v3.json`、`six-method-v1.json` 一致 |
| Live integration | PASS 51/51 at `d8cc334`（`integration-v3`）；Linux CI / Docker Compose host 仍缺 |
| Six-method scaled result | `postgres-provenance-six-method-v1.json`（SHA-256 `47bc10ab…e7395c`，commit `6ab2b7a`，2026-09-18）：108 場景 × 6 方法，648 列全 ok、3672 次量測，parity 648/648 |
| Large-scale 10K/100K execution | 在 six-method v1 內完成（10K 各 7 次、100K 各 3 次；1M 已移出正式矩陣，其 memory gate v2–v4 ABORT 於 `baseline_graph_build`，非正式） |
| Real/RSNA six-method result | Absent |
| Calibration result JSON | `postgres-provenance-calibration-v2.json`（SHA-256 `43d213e4…88f9239`）；v1 嘗試曾 ABORT（RAM 護欄） |
| Policy artifact ID | `postgres-adaptive-v1-9f4e58346529`（`calibration_sha256` = `9f4e5834…`，`environment_fingerprint` = `f29fc1bb…`） |
| Exact policy file SHA-256 | `a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78`（`-artifacts/artifacts/provenance_policy/…/policy.json`） |
| Held-out result JSON | Absent |
| Calibration/held-out overlap | Pending（held-out 尚未跑）；offline contract要求 0 |
| Every-scenario correctness parity | six-method v1 648/648、calibration v2 1224/1224（graph / hash / head / status exact parity）；held-out pending |
| Adaptive aggregate p50/p95 gates | 正式裁決 pending held-out。Calibration split 預覽：aggregate p50 1.010（≤ 1.05）、p95 0.932（≤ 1.10），every-scenario diagnostic 31/92 > 1.05（1K 全部 28 個因信心帶落入 FULL） |
| PostgreSQL latency/crossover/storage | Crossover：calibration 12/12、six-method 12/12 切片 `not_observed`。100K maintenance p50：pg_incr 11.3–65.5 s、pg_full 75–140 s、sqlite_incr 7.5–19.8 s、canonical 9.3–14.9 s；`status` p50 pg_incr 0.73 s / pg_full 2.1 s；storage 100K pg_incr 1.07–1.57 GiB、pg_full 2.05–2.90 GiB（含 dead tuples）。逐格見 §六方法正式結果 |

填寫時必須記 command、commit、environment fingerprint、PostgreSQL numeric server version、result file
SHA、policy ID/精確 `policy.json` SHA、scenario/sample counts、parity、gate outcomes與任何 failed rows。
沒有 machine-readable result 回讀前不要更新這張表。
