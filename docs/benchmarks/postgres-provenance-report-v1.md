# PostgreSQL Adaptive Provenance：十項證據總結（v1，2026-09-20）

給資料庫課程專案（CS 5151/6051）與外部讀者的一頁式回報。每一個數字都從
`docs/benchmarks/` 的 machine-readable 結果回讀，來源檔與 SHA-256 列在每一節；方法、逐格表與裁決見
`postgres-provenance-v1.md`（evidence record）與 `../superpowers/plans/2026-09-13-vcp-postgresql-adaptive-provenance-followups.md`
（Plan 12 後記）。這份文件不含任何估計值；沒量到的事寫「未量」。

| 證據 | 檔案 | SHA-256（前 8 碼） | 量測 commit |
|---|---|---|---|
| Live integration | `postgres-provenance-integration-v3.json` / `.xml` | `b55f34fd` / `b50ada0f` | `d8cc334` |
| Calibration（policy 來源） | `postgres-provenance-calibration-v2.json` + `-artifacts/` | `43d213e4` / policy `a22d7067` | `b1512ae` |
| Six-method（calibration split） | `postgres-provenance-six-method-v1.json` | `47bc10ab` | `6ab2b7a` |
| Held-out（normative gate） | `postgres-provenance-heldout-v1.json` | `6d7efe24` | `2644e83` |
| Real RSNA six-method | `postgres-provenance-real-rsna-v1.json` | `78487fc5` | `32f0f65` |

## 1. PostgreSQL 實際版本與硬體環境

- PostgreSQL **17.11**（`server_version_num` 170011；`SELECT version()` = `PostgreSQL 17.11 on x86_64-windows,
  compiled by msvc-19.44.35228, 64-bit`），原生 Windows 服務（`native_portable`，不是 Docker），使用者排程工作
  `VCP-PostgreSQL-17.11`，只聽 `127.0.0.1:55432`，libpq service `vcp-pg17-evidence`；VCP 不接受連線 URI 或憑證欄位，
  憑證檔只以路徑指給 `PGSERVICEFILE` / `PGPASSFILE`。backend schema v1；environment fingerprint `f29fc1bb…`（五份證據相同）。
- 主機：AMD Ryzen 7 9700X（8C/16T）、31.2 GiB RAM、NVMe 999 GB；Windows 11 10.0.26200；Python 3.12.14；
  SQLite 3.53.1（對照組）。所有量測都在這一台機器、單一 writer、桌面程式在背景的狀態下完成；六方法期間主機另有
  核心 nonpaged pool 洩漏（7.6 GiB，重開機後清除），held-out 與 real RSNA 在重開機後跑，絕對毫秒因此比六方法低
  5–20%（比值不受影響；evidence record §Held-out 正式結果觀察 3）。

## 2. Live integration tests 通過／失敗數

`uv run pytest tests/integration/provenance`（opt-in，PostgreSQL service 必須存在；未配置時這些案例是 `skipped`，
不是 pass）：**51 passed / 0 failed / 0 errors / 0 skipped**，58.4 s，commit `d8cc334`，2026-09-14。前兩次嘗試保留：
v1 FAIL（39 passed、4 behavior failures，checkpoint 測試與 schema 修正前）、v2 ERROR（51 setup errors，PostgreSQL
程序在跑前被回收）。GitHub CI 的 `postgres` job（ubuntu、Docker Compose、PostgreSQL 17.11）在每個 PR 都跑並通過
（例如 PR #12、#13），但沒有像 v3 那樣保存成 write-once evidence record；Linux host 的 live record 仍列為尚缺。

## 3. Transaction rollback、concurrency、MVCC 與 parity

integration v3 的四個類別（`categories`，皆 `executed_behavior: true`）：

| 類別 | 測試 | 結果 |
|---|---|---|
| transaction rollback（測試名含 `rollback`） | 28 | 28 passed |
| concurrency（`test_advisory_lock_serializes_concurrent_duplicate_writers`：兩個 writer 同時 ingest 同一 diff，advisory lock 序列化，第二個看到 no-op） | 1 | passed |
| MVCC（`test_reader_sees_old_generation_until_rebuild_commit`：full rebuild 提交前讀者仍看舊 generation） | 1 | passed |
| canonical / SQLite / PostgreSQL parity（`test_every_strategy_matches_canonical_and_sqlite`：incremental / full / auto 三種策略） | 3 | passed |

寫入路徑：每個 writer 以 `pg_advisory_xact_lock` 序列化；full rebuild 寫新 generation 後以 `active_generation` 指標
原子切換，舊 generation 的列 `DELETE`（cascade），讀者在切換前始終看到一致的舊 generation。

## 4. 1K / 10K / 100K（1M）六方法 benchmark

- **1M 已於 2026-09-15 以裁決移出正式矩陣**（Plan 12 後記 §1）：31 GiB 主機跑不完（三次 memory gate 皆在
  `baseline_graph_build` 被 RAM 護欄中止，需 ≥128 GB 記憶體），且 `expected_scenarios` 含 1M 時 calibration /
  held-out 永遠產不出 policy。結論的適用範圍因此是 **1K–100K**；1M 仍可用 `--entities 1000000` 臨時跑。
- 正式矩陣：1K / 10K / 100K × 變更比例 0、0.1%、1%、5%、10%、25%、50%、90%、100% × chain / branched × 2 seeds
  = 108 場景；1K、10K 各 7 次重複，100K 3 次；每次重複 fresh database；只計 maintenance 時間。
- **Six-method v1**（calibration split，seeds 20260913/20260914）：108 × 6 方法 = 648 列全 ok，3672 次量測，
  parity 648/648。maintenance p50（每格四個場景的中位數，ms）：

| scale | ratio | canonical_full | sqlite_full | sqlite_incr | pg_full | pg_incr | pg_adaptive |
|---|---|---|---|---|---|---|---|
| 1K | 0.01 | 115 | 272 | 70 | 663 | 181 | 696（FULL） |
| 1K | 0.5 | 130 | 321 | 114 | 766 | 322 | 784（FULL） |
| 10K | 0.01 | 810 | 2,043 | 650 | 6,433 | 1,040 | 1,070 |
| 10K | 0.5 | 1,051 | 2,602 | 1,193 | 7,738 | 2,924 | 2,920 |
| 100K | 0.01 | 9,921 | 23,966 | 7,874 | 85,416 | 12,666 | 12,485 |
| 100K | 0.5 | 11,867 | 29,050 | 14,189 | 112,101 | 35,872 | 35,198 |
| 100K | 1.0 | 14,679 | 34,471 | 19,497 | 120,891 | 50,511 | 55,681 |

  （完整 9 個 ratio × 3 個規模的表在 evidence record §六方法正式結果。）排除 NO_OP 後 PostgreSQL incremental 的
  p50 是 full 的 1K 27–56%、10K 16–50%、100K 13–52%；SQLite incremental 是每個規模最快的維護路徑，PostgreSQL
  incremental 是它的 1.5–3.5 倍；PostgreSQL full 在 100K 要 75–140 s，是 canonical replay 的 7.4–10.1 倍。

## 5. Calibration 與 held-out evaluation 的分離證據

- Calibration v2（2026-09-16，14.5 h，1224 次量測，seeds 20260913/20260914）只跑 postgres_full / postgres_incremental，
  92 個可 fit 觀測 → policy artifact；held-out v1（2026-09-20，seeds **20261001/20261002**，另一個 process、另一個
  worktree）只載入已驗的 immutable policy，**不呼叫 fit**。
- Evaluator 以 scenario_id、scenario_hash 與 workload_hash 三種身分比對兩個 split：**`overlap_count` 0**。
  離線契約測試另外釘死 disjoint ID / hash / workload 集合與 no-refit。
- 兩個 split 的 108 場景一一對應（同規模、同比例、同拓樸、不同 seed）；held-out 的 policy 決策模式與 calibration
  split 完全相同（1K FULL ×28、10K/100K INCREMENTAL ×64、NO_OP ×16）。

## 6. Adaptive policy ID、hash 與 crossover point

- Policy ID **`postgres-adaptive-v1-9f4e58346529`**；`policy.json` SHA-256
  `a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78`；`calibration_sha256` `9f4e5834…`；
  policy version `postgres-adaptive-v1`；artifact 目錄 `postgres-provenance-calibration-v2-artifacts/artifacts/provenance_policy/…`。
- 成本模型（非負線性迴歸、截距 0，92 個觀測）：`incremental_ms ≈ 1.16·changed_samples + 0.118·total_edges`，
  `full_ms ≈ 0.704·total_edges + 0.097·historical_changes`；RMSE incremental 2,286 ms、full 5,057 ms。
- **Crossover point：沒有。** calibration、six-method、held-out 各 12 個切片（拓樸 × 規模 × seed）全部
  `status: not_observed`、`last_incremental_preferred_ratio: 1.0`、`global_threshold: null`——在這個 workload 產生器下
  incremental 在每個非零變更比例（含 100%）都比 full 快，因為 changed_samples 最多約 0.25·edges，而 full 多付
  0.586·edges 的固定成本。真實 RSNA 資料裡唯一 full 不比 incremental 慢的量測是 r3→r4（前圖 38 個 entity、100% 變更）。
- **Selector 的已知缺陷（誠實報告）**：`auto` 只在 `incremental + 2,286 < full − 5,057`（絕對毫秒）時選 INCREMENTAL，
  所以預估差距小於 7.3 s 的 workload 永遠落入保守的 FULL：合成 1K 的 28 個非 NO_OP 場景全部（慢 1.82–4.26 倍）、
  真實 RSNA r4→r5（8.8K entities、98.7% 變更，慢 1.79 倍）。10K/100K 全部選 INCREMENTAL（與 incremental 差在雜訊內）。
  Policy v1 凍結不改；規模相對的信心帶是 policy v2 的 spec 變更（Plan 12 後記 §3-6、§5、§7）。

**Held-out normative gate（pool 92 個非 NO_OP 場景的 raw samples）：**

| 指標 | pg_full | pg_incr | pg_adaptive | adaptive / 較佳 fixed | 門檻 | 結果 |
|---|---|---|---|---|---|---|
| aggregate p50 ms | 6,029 | 998 | 1,017 | **1.018** | ≤ 1.05 | **PASS** |
| aggregate p95 ms | 106,290 | 34,070 | 34,664 | **1.017** | ≤ 1.10 | **PASS** |

`performance_pass: true`、`VERDICT cmd=provenance.evaluate status=OK`。Every-scenario diagnostic **FAIL**：36/92 場景
> 1.05（1K 全部 28 個、10K 2 個 ≤ 1.06、100K 6 個 ≤ 1.28）。兩句話必須並列：v1 policy 通過正式 aggregate gate；
v1 policy 在 1K 每個場景都選錯。

## 7. Real RSNA 實驗結果

`rsna-knee-sixslot` r3 → r4 → r5 → r6 四個真實資料集（各約 4,400 個 sample、7 個 run card）唯讀複製到暫存目錄
（metadata-only；`raw/` 與 live data 不動），canonical 圖 13,196 entities / 17,569 edges / 8,752 changes / 0 gaps，
SQLite 對 canonical `graph_parity` / `status_parity` true、`verify_index ok`。三個 transition × 6 方法 = 18 列全 ok、
parity 18/18。真實變更比例 100%（4,407：split 群組整批換掉）/ 98.7%（4,345：label-agreement 中繼資料，effect UNKNOWN
→ REVIEW）/ 0%。

| transition | 前圖 entities → changed | canonical | sqlite_full | sqlite_incr | pg_full | pg_incr | pg_adaptive（選擇） |
|---|---|---|---|---|---|---|---|
| r3→r4 | 38 → 4,407 | 782 | 2,250 | 1,688 | 5,625 | 6,056 | 5,768（FULL） |
| r4→r5 | 8,849 → 4,345 | 1,337 | 3,683 | 2,107 | 10,320 | 5,865 | 10,502（FULL，選錯 1.79×） |
| r5→r6 | 13,195 → 0 | 1,353 | 3,365 | 913（NO_OP） | 1,318（NO_OP） | 1,421（NO_OP） | 1,363（NO_OP） |

（maintenance p50，ms。）真實資料只覆蓋變更比例的兩端；中段（0.1%–25%）由合成矩陣負責，兩者互補。

## 8. p50 / p95 latency、throughput、index size

六方法 v1（非零 ratio 場景的中位數；`impact` / `explain` 含 graph load，`status` 為 public status API；PostgreSQL
storage = `vcp_provenance` 全部 relation 的 `pg_total_relation_size`，SQLite = database bytes，物理口徑不同）：

| scale | method | maintenance p50 範圍（ratio 0.01–1.0，ms） | status p50 / p95 ms | impact p50 / p95 ms | index MiB | storage MiB | throughput samples/s |
|---|---|---|---|---|---|---|---|
| 1K | pg_incr | 181–448 | 27.8 / 47.9 | 66.2 / 88.3 | 8 | 12 | 227 |
| 1K | pg_full | 663–876 | 27.5 / 37.1 | 64.6 / 82.7 | 18 | 25 | 80 |
| 10K | pg_incr | 1,040–4,246 | 82.0 / 153 | 545 / 623 | 81 | 119 | 329 |
| 10K | pg_full | 6,433–9,343 | 99.3 / 166 | 538 / 621 | 181 | 247 | 81 |
| 100K | pg_incr | 12,666–50,511 | 731 / 784 | 7,249 / 7,610 | 812 | 1,188 | 232 |
| 100K | pg_full | 85,416–120,891 | 2,117 / 2,360 | 6,523 / 6,764 | 1,826 | 2,467 | 61 |
| 100K | sqlite_incr | 7,874–19,497 | 263 / 602 | 4,591 / 4,645 | — | 596 | 546 |
| 100K | canonical | 9,921–14,679 | 6,127 / 6,692 | 6,134 / 6,276 | — | — | 537 |

- Held-out aggregate（§6 表）：pg_incr p50 998 / p95 34,070 ms；pg_full 6,029 / 106,290；pg_adaptive 1,017 / 34,664。
- **全量重建後 storage 約 2 倍**（100K：relation 2.05–2.90 GiB 對 incremental 1.07–1.57 GiB；index 1.44–2.16 對
  0.73–1.08 GiB）：舊 generation 的列已 `DELETE` 但在 VACUUM 前仍是 dead tuples，`status` 也因此 2.1 s 對 0.73 s。
  這是原子 generation 切換的 MVCC 成本（Plan 12 後記 §3-7）。
- 13 個索引（FK / CHECK / 複合 B-tree）全在 `src/vcp/provenance/postgres_schema.py`；index size 隨規模線性
  （1K 8 MiB → 100K 812 MiB，incremental 維護後）。

## 9. `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`

- 每個 PostgreSQL 列都帶「每種 SQL 形狀第一個執行的 DML 列」的計畫，在額外的 fresh state、rollback-only 交易中觀測，
  不計入 timing、不改後續 fixture；原始 JSON 與 allowlisted / sanitized 兩份都保存（欄位：`Node Type`、`Operation`、
  `Actual Rows/Loops/Startup/Total Time`、`Shared/Local Hit/Read/Dirtied/Written Blocks`、`Temp Read/Written Blocks`、
  `Planning Time`、`Execution Time`…；不含任何 conninfo）。
- 覆蓋量：six-method 324 列 / 4,236 個計畫；held-out 324 列 / 4,236 個；real RSNA 9 列 / 115 個。頂層節點：
  `ModifyTable Insert` 3,384、`Update` 528、`Delete` 324（six-method）。
- 尚未涵蓋：`status` / `impact` / `explain` 查詢自身的計畫（`WITH RECURSIVE` 的 dataset ancestors / dirty closure），
  只有它們的延遲（§8）；列為 Plan 12 後記 §3-4。

## 10. SQLite、PostgreSQL 與 canonical replay 的完全一致性

- 每個 scenario 的每次重複都做 exact parity：graph（normalized）、graph hash、每個 materialized status、leaf heads
  與 stored heads，對 canonical replay（從 JSONL / YAML / dataset_diff artifact 重放的 canonical graph）逐項比對：
  six-method **648/648**、held-out **324/324**（含 `status_parity`）、real RSNA **18/18**（另有整體 `status_differences`
  = 空、`verify_index ok`）、calibration 1224/1224、integration parity 3/3。
- 一致性的機制：PostgreSQL 與 SQLite 都是 noncanonical 的衍生索引，canonical evidence 永遠在檔案（append-only ledger、
  write-once artifact）；full rebuild 後以 canonical snapshot hash 驗證（`canonical_drift` 即 fail closed），incremental
  以 dataset_diff artifact 的 immutable explicit edge 推進；任何 evidence 消失 / 改寫或 ledger prefix drift 都 fail closed，
  不做 silent repair。

## 限制與尚缺

1. 1M 未量（§4 裁決）；結論適用 1K–100K。
2. Selector 的絕對信心帶（§6）：小圖與接近全量變更的 transition 會選 FULL；操作指南建議這兩種情況直接指定
   `--strategy incremental`。
3. 單一主機、單一 writer；concurrency / MVCC 只有整合測試的行為證據（§3），沒有多 writer 的吞吐量測。
4. Linux CI（Docker Compose host）只有 CI 容器內的 integration 證據，沒有本機式的 live record。
5. 真實資料的變更比例只在 100% / 98.7% / 0%（§7）。

## 機器可讀摘要

```json
{
  "postgresql": {"server_version": "17.11", "server_version_num": 170011, "deployment": "native_portable", "environment_fingerprint": "f29fc1bbf397de87030c90ead9614d5093572bbf8ff5208bf1ffb25b19f7217f"},
  "host": {"cpu": "AMD Ryzen 7 9700X", "cores": 8, "threads": 16, "ram_gib": 31.2, "os": "Windows 11 10.0.26200", "python": "3.12.14", "sqlite": "3.53.1"},
  "integration_v3": {"tests": 51, "passed": 51, "failed": 0, "rollback": 28, "concurrency": 1, "mvcc": 1, "parity": 3, "commit": "d8cc334"},
  "matrix": {"scales": [1000, 10000, 100000], "one_million": "excluded_by_ruling_2026-09-15", "ratios": [0, 0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 0.9, 1.0], "topologies": ["chain", "branched"], "repetitions": {"1000": 7, "10000": 7, "100000": 3}, "scenarios_per_split": 108},
  "calibration_v2": {"seeds": [20260913, 20260914], "rows": 216, "samples": 1224, "fit_observations": 92, "policy_id": "postgres-adaptive-v1-9f4e58346529", "policy_sha256": "a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78", "incremental_rmse_ms": 2286, "full_rmse_ms": 5057},
  "six_method_v1": {"rows": 648, "samples": 3672, "parity": "648/648", "crossover_slices_not_observed": "12/12", "adaptive_decisions": {"FULL_1K": 28, "INCREMENTAL_10K_100K": 64, "NO_OP": 16}, "sha256": "47bc10ab19f20882e1afbb128290329d75bbb1565ec7ebef6dff03689be7395c", "commit": "6ab2b7a"},
  "heldout_v1": {"seeds": [20261001, 20261002], "rows": 324, "samples": 1836, "parity": "324/324", "overlap_count": 0, "aggregate_p50_ratio": 1.018, "aggregate_p95_ratio": 1.017, "gate": "PASS", "every_scenario_pass": false, "every_scenario_over_1_05": "36/92", "sha256": "6d7efe24a0e30ba2bdafa710e2ee5c8728aaa795d61e033c5166e46589597bbb", "commit": "2644e83"},
  "real_rsna_v1": {"transitions": 3, "rows": 18, "parity": "18/18", "graph": {"entities": 13196, "edges": 17569, "changes": 8752}, "change_ratios": [1.0, 0.9868, 0.0], "adaptive_decisions": ["FULL", "FULL", "NO_OP"], "sha256": "78487fc51db2847dd4afe8d79f4cd61ff7ab99d5de8b3f5b2971238f02830560", "commit": "32f0f65"},
  "explain_plans": {"six_method": 4236, "heldout": 4236, "real_rsna": 115}
}
```
