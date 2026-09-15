# Plan 12 後記：PostgreSQL Adaptive Provenance（v0.8.0）

計畫：`2026-09-13-vcp-postgresql-adaptive-provenance.md`（不可修改）；spec：`../specs/2026-09-13-vcp-postgresql-adaptive-provenance-design.md`。Codex 於 2026-09-13 完成 Task 1–12（每任務一次審查，最終 database / security / whole-branch 三次終審皆 clean），2026-09-14 追加 live PostgreSQL 整合證據與記憶體護欄工作（commits `af03188`…`fd92de2`，無 ledger）。分支於 2026-09-15 以 PR 合併並 tag `v0.8.0`；實作期裁決見 SDD ledger（`.claude/worktrees/postgresql-adaptive-provenance/.superpowers/sdd/2026-09-13-vcp-postgresql-adaptive-provenance/progress.md`）。本檔記合併之後的裁決與開放待辦。

## 1. 裁決：1M 移出正式（normative）adaptive 矩陣

**決定**：正式矩陣的 entity scales 由 1K/10K/100K/1M 縮為 **1K/10K/100K**。`workloads.SCALES`、`scenario_matrix` 的預設、`adaptive_benchmark.py --entities` 的預設與 `evaluate_adaptive.expected_scenarios` 的釘死清單四處同步。每個 split 因此從 144 場景 / 720 次重複 / 124 個可 fit 觀測 / 20 個 NO_OP，變成 **108 / 612 / 92 / 16**；重複次數規則不變（1K、10K 各 7 次，100K 3 次）。`production_benchmark.py` 另用 `workloads.PRODUCTION_SCALES`（仍含 1M），因為它的 1M 是 canonical/SQLite 路徑且已有完成的結果（`docs/benchmarks/provenance-production-2026-09-13.json`）。1M 仍可用 `--entities 1000000` 臨時跑，只是不再是驗收條件。

**依據**：1M 在這台 31 GiB 的機器上跑不完。Codex 的三次 memory gate（`postgres-provenance-1m-memory-gate-v2/v3/v4.json`）都在 `baseline_graph_build` 被 RAM 護欄中止，peak private 5.67 GiB、最低可用 RAM 0.556 GiB；calibration runner 更觀測到 16.3 GiB。串流 replay（`open_dataset_diff`）已把 fixture 與 diff 重放的常駐量壓下來，但 canonical graph 本身在 1M 仍是數 GiB 等級，需要 ≥128 GB 記憶體的機器。而 `expected_scenarios` 是 calibration 與 held-out 的完整性閘門：只要 1M 留在裡面，兩者永遠產不出 policy，`postgres_adaptive`、six-method 與 real RSNA 三條線全部卡死。2026-09-15 的探路矩陣（`docs/benchmarks/postgres-provenance-exploratory-v1.md`）也顯示三個規模已足以看出趨勢——1K→100K 的 incremental / full 比值單調變化且方向一致。

**代價**：若交會點只在 1M 以上出現，這個矩陣看不到它，所有結論的適用範圍只能寫成「1K–100K」。日後要補 1M 必須換機器，並重跑 calibration 與 held-out（policy 綁定完整場景集合，不能把新舊 split 混用）。

**處置**：`tests/unit/provenance/test_adaptive_benchmark.py::test_normative_manifest_and_runner_ladder_cannot_drift_apart` 保證釘死清單與 runner 階梯不會單方面漂移；spec §20-1 記同一條；`docs/guides/POSTGRESQL_PROVENANCE.md`、`docs/benchmarks/postgres-provenance-v1.md` 的命令與數字已同步。

## 2. 合併後已完成

1. **串流 replay**（`open_dataset_diff`，PR #7）：canonical graph replay 先整檔驗證、再逐筆重放，兩遍 SHA-256 互驗；`load_dataset_diff` 的 API、錯誤型別與訊息不變。
2. **探路矩陣**（PR #8，非正式）：1K/10K/100K × 9 ratios × 2 topologies × 2 seeds、五個固定方法各 1 次，8 小時 34 分，540/540 parity 全過。結論：**ratio > 0 的每一格 incremental 都快於 full**（100K 時為 full 的 13–45%，連 100% 變更也是），12 個切片沒有任何一個出現交會點。
3. **live 證據**：整合測試 51/51（`postgres-provenance-integration-v3.json`，原生 PostgreSQL 17.11）。

## 3. 開放的待辦

1. **正式 calibration → six-method → held-out → real RSNA**（依序，後三者都要 frozen policy）。本機 1K–100K 的正式重複次數估約 30 小時；跑之前先清出記憶體，`bench_launch` 類的護欄要保留（可用 RAM < 2 GiB 持續 30 秒即中止，runner 的逐列 checkpoint 可續跑）。
2. **adaptive 退化的處置**：若正式 calibration 重現探路結果（full 從不最快），policy 會退化成「NO_OP 或 INCREMENTAL」二選一。這是量測結論不是缺陷，但報告要明說，並解釋 full 的存在理由是 diff 損毀、index 漂移與 schema 不相容時的重建路徑，而不是效能選項。
3. **Linux CI / Docker Compose host** 的 integration evidence 仍缺（本機是原生 Windows 服務）。
4. **EXPLAIN 覆蓋面**：目前只涵蓋每種 DML SQL 形狀的第一列，不含 `status` / `impact` / `explain` 查詢自身的計畫（runner 既有限制）。
5. **`docs/benchmarks/postgres-provenance-v1.md` 的 pending 表**：只有拿到 machine-readable 結果回讀後才准填；探路數字不得填入。
