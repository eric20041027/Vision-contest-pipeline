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

1. **正式 calibration（完成，§4）→ six-method（完成，§5）→ held-out（完成，§6）→ real RSNA**（依序，都要 frozen policy）。real RSNA：`real_validation.py --data-root … --configs-root … --six-method --policy-from docs/benchmarks/postgres-provenance-calibration-v2.json`，來源唯讀複製；跑之前先清出記憶體（§5 的執行紀錄：桌面程式加驅動洩漏可把 31 GiB 主機壓到 2 GiB 以下），`bench_launch` 的護欄要保留（可用 RAM < 2 GiB 持續 30 秒即中止，runner 的逐列 checkpoint 可續跑），看管腳本 `C:/vcp-data/bench/supervise.py` 只在機器空閒時跑、偵測到遊戲或低記憶體就停並丟棄進行中場景。
2. **adaptive 退化的處置**：正式結果是「部分退化」——10K/100K 永遠 INCREMENTAL（full 從不最快），1K 卻永遠 FULL（§5 的信心帶問題）。報告要兩件都明說：full 的存在理由是 diff 損毀、index 漂移與 schema 不相容時的重建路徑，不是效能選項；而 1K 的 FULL 是 selector 的保守分支在小規模失效，不是 full 在 1K 較快。
3. **Linux CI / Docker Compose host** 的 integration evidence 仍缺（本機是原生 Windows 服務）。
4. **EXPLAIN 覆蓋面**：目前只涵蓋每種 DML SQL 形狀的第一列，不含 `status` / `impact` / `explain` 查詢自身的計畫（runner 既有限制）。
5. **`docs/benchmarks/postgres-provenance-v1.md` 的 acceptance 表**：只有拿到 machine-readable 結果回讀後才准填；探路數字不得填入。six-method（2026-09-18）、held-out 與 gate 裁決（2026-09-20）已填；只剩 RSNA。
6. **policy v2：規模相對的信心帶**（spec 變更，§5）。`select_strategy` 的絕對 RMSE 帶要改成相對於預估值的帶（或按 `total_edges` 分層的 RMSE），並重跑 calibration → six-method → held-out；v1 的 policy 與三份證據不改。
7. **全量重建後的 dead tuples**（§5 觀察 4）：`_publish_generation` 刪除舊 generation 後不 VACUUM，storage 約 2 倍、`status` 變慢直到 autovacuum 追上。是否在 full rebuild 收尾加 `VACUUM`（不能在交易內）或記錄為操作指南事項，待裁決；量測口徑（`pg_total_relation_size`）不改。

## 4. 裁決：calibration v2 從 checkpoint 發布（2026-09-16）

**發生的事**：正式 calibration 跑完 1224 次量測（14.5 小時，RAM 護欄未觸發）後，發布階段 FAIL。原因是生產者／消費者 schema 漂移：`3bde664` 讓 `runtime_environment()` 多輸出 `system_release` / `system_version`，同一個 commit 同步進了 `test_adaptive_benchmark.py` 的合成環境，卻沒進 `evaluate_adaptive.py` 的 strict `_Environment`，也沒進 `test_adaptive_evaluation.py` 的合成環境。兩邊的測試各自用自己的手寫字典，所以離線全綠，live 第一次就炸。

**決定**：補上兩個欄位；新增契約測試 `test_every_runtime_environment_key_is_accepted_by_the_row_model`，直接拿真的 `runtime_environment()` 輸出去驗 `_Environment`（對舊模型正好報出那兩個欄位）；量測**不重跑**，以外部腳本從 checkpoint store 發布，前提是逐檔證明生產端程式碼與 run 合約釘住的快照完全一致。

**依據**：量測資料本身正確且完整（216 列全 ok、同一 commit `b1512ae`、同一環境指紋）；改的是消費端模型，不影響任何量測值；`.provenance.json` 記下 168 個原始檔中只有 `evaluate_adaptive.py` 不同。重跑要再佔用機器 14.5 小時，換到的是同一組數字加雜訊。

**代價**：runner 的 resume 守門（原始樹必須逐位元一致）被一次性繞過。若有人日後主張量測程式在跑的期間被改過，`.provenance.json` 的逐檔比對是反證；若連這份比對都不信，就只能重跑。

**結果**：policy `postgres-adaptive-v1-9f4e58346529`，`policy.json` SHA-256 `a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78`，92 個 fit 觀測。fit 出的成本模型（非負線性、截距 0）：`incremental_ms ≈ 1.16·changed_samples + 0.118·total_edges`，`full_ms ≈ 0.704·total_edges + 0.097·historical_changes`（`dirty_entities`、`dirty_ratio`、`head_count`、`total_entities` 係數被非負約束壓到 0）。RMSE：incremental 2286 ms、full 5057 ms，量級由 100K 主導。**12 個切片全部沒有交會點**：因為 changed_samples 最多約 0.25·edges，而 full 比 incremental 多出 0.586·edges 的固定成本，所以在這個 workload 產生器下 `auto` 對任何非零工作永遠選 INCREMENTAL——§3-2 預告的退化成立，且現在是正式證據而不是探路推測。（2026-09-18 更正：這個推論漏了 selector 的絕對 RMSE 信心帶，six-method 實測 1K 全部落入 FULL，只有 10K/100K 永遠 INCREMENTAL；見 §5。）

**開放**：held-out 若重現同樣結果，adaptive 的 p50/p95 gate 會 trivially 通過（它就是 incremental）；報告要寫清楚這代表「這個 workload 結構下 full 沒有效能上的存在理由」，而不是 selector 有多聰明。（2026-09-18：six-method 顯示 1K 不是 trivially 通過，見 §5 的 gate 預覽。）

## 5. 六方法正式結果與 1K 的信心帶問題（2026-09-18）

**發生的事**：正式 six-method（`docs/benchmarks/postgres-provenance-six-method-v1.json`，commit `6ab2b7a`，frozen policy `postgres-adaptive-v1-9f4e58346529`）跑完 108 場景 × 6 方法 × 7/7/3 次 = 648 列、3672 次量測，全部 ok、parity 648/648、crossover 12/12 `not_observed`。但 `postgres_adaptive` 的 `auto` 在 1K 的 28 個非 NO_OP 場景全部選 FULL（`calibrated_full_lower_or_uncertain_cost`），在 10K/100K 的 64 個全部選 INCREMENTAL。§4 預告的「非零工作永遠 INCREMENTAL」在 1K 不成立。

**原因**：`select_strategy` 只在 `incremental_ms + incremental_rmse < full_ms − full_rmse` 時選 INCREMENTAL，兩個 RMSE 是整個 calibration split 的絕對值（2286 + 5057 = 7343 ms），量級由 100K 殘差決定。1K 的預估差距（`full − incremental`）只有 842–845 ms，10K 是 8448–8477 ms；帶寬與規模無關，所以 1K 永遠落入「不確定 → FULL」的保守分支。量到的代價：1K 的 adaptive p50 是 postgres_incremental 的 1.82–3.97 倍（0.68–0.90 s 對 0.18–0.49 s）；10K/100K 差在重複雜訊內（−10%…+14%）。

**決定**：policy 與 selector 維持凍結，held-out 照這個 policy 跑並如實報告 1K 的 FULL；six-method v1 的三份證據不重跑。修正（相對信心帶或按規模分層的 RMSE）是 policy v2 的 spec 變更（§3-6），需要重跑 calibration → six-method → held-out 三段。

**依據**：spec 禁止用 held-out 或 six-method 的結果回頭調 selector（那就是 leakage）；1K 的絕對代價是 0.5 s 一次、方向錯但不影響正確性（FULL 永遠正確，parity 648/648）；aggregate gate 的預覽（calibration split：p50 1.010、p95 0.932）顯示 pooled 口徑不會因此翻盤，every-scenario diagnostic（31/92 > 1.05）本來就是要暴露這種事的。

**代價**：v1 的 adaptive 在小圖上比 `--strategy incremental` 慢 2–4 倍；操作指南應建議 1K 級別的 index 直接指定 `incremental`，`auto` 留給 10K 以上。若 held-out 的 aggregate gate 因 1K 的樣本數（7 次 × 28 場景 = 196 個 sample，佔 pool 的 38%）而失敗，那是 v1 的真實結果，不得以「已知問題」為由排除。（2026-09-20 結果：held-out aggregate gate PASS 1.018 / 1.017，1K 的 196 個 sample 沒有拉動 pooled p50/p95；every-scenario diagnostic 在 1K 全部 FAIL，1.82–4.26 倍，比 calibration split 更差——見 §6。）

**其他量到的事（細節在 evidence record §六方法正式結果）**：PostgreSQL 全量重建在 100K 要 75–140 s，是 canonical replay 的 7.4–10.1 倍、SQLite full 的 3.0–4.1 倍；重建後舊 generation 的 dead tuples 讓 storage 約 2 倍、`status` p50 2.1 s（incremental 後 0.73 s）（§3-7）。SQLite incremental 是每個規模最快的維護路徑（100K 7.5–19.8 s），postgres_incremental 是它的 1.5–3.5 倍——PostgreSQL 在單 writer 主機上的價值是併發、MVCC 與原子發布，不是速度。

**執行紀錄**：由 `C:/vcp-data/bench/supervise.py`（不進 repo）看管，啟動 5 次、停 4 次（NVIDIA Overlay 誤判一次、遊戲一次、RAM 護欄兩次），每次停止刪除進行中場景的 checkpoint 並 drop 用完即棄的 database；主機核心 nonpaged pool 洩漏到 7.6 GiB 是低記憶體的根因，重開機後 2 小時 16 分跑完最後 5 個場景。runner 每次續跑都驗證 168 個原始檔雜湊；原始樹整段未變，輸出由 runner 直接發布。

## 6. Held-out 正式結果與執行事故（2026-09-20）

**結果**：`docs/benchmarks/postgres-provenance-heldout-v1.json`（commit `2644e83`，SHA-256 `6d7efe24…`）。seeds 20261001/20261002 的 108 場景 × 3 個 PostgreSQL 方法 = 324 列全 ok、parity 324/324、`overlap_count` 0。**Normative aggregate gate PASS**：adaptive p50 / 較佳 fixed p50 = 1.018（≤ 1.05）、p95 = 1.017（≤ 1.10），`performance_pass: true`、`VERDICT status=OK`。every-scenario diagnostic FAIL：36/92 個非 NO_OP 場景 > 1.05，其中 1K 的 28 個全部（1.82–4.26 倍）、10K 2 個（≤ 1.06）、100K 6 個（≤ 1.28，3 次重複的雜訊）。policy 的決策模式在未見過的 seeds 上與 calibration split 完全相同（1K FULL ×28、10K/100K INCREMENTAL ×64、NO_OP ×16），crossover 12/12 `not_observed`。絕對時間比 six-method 略快（非 NO_OP 中位數比 1K 0.82–0.95、10K 0.91–1.00、100K 0.91–1.10）：主機在重開機後、驅動洩漏清除的狀態下跑，不是 seeds 效應。

**裁決 6-1：gate 的正式讀法**。spec 的 normative gate 是 aggregate（pool raw samples），held-out 通過；every-scenario 是 diagnostic，同時報、不取代。報告必須把兩件事並列：「v1 policy 通過 held-out 的 aggregate gate」與「v1 policy 在 1K 每個場景都選錯，慢 2–4 倍」。只講前者是誤導，只講後者是漏報。§3-6（policy v2 的相對信心帶）維持為下一版的 spec 變更；v1 的 policy、selector 與四份證據都不改。

**事故**：09-19 20:39 UTC 開跑，09-20 14:56 被 launcher 的 RAM 護欄中止（最低可用 1.46 GiB）。原因是使用者誤啟動了另一個專案的訓練（一個 16 GB 的 python 程序）；看管程式 `supervise.py` 當時把所有 `python` / `uv` 都視為自己人，沒能在 30 秒內先停 benchmark，直到記憶體耗盡才由 RAM 護欄擋下。中止後沒有 record 被寫入（最後一筆 14:47:17）；進行中的 100K 場景依規則丟棄。

**裁決 6-2：丟棄並重量一個已完成場景**。中止前最後完成的場景 `42e973f9…`（10K、ratio 0.01、chain、seed 20261002，14:42–14:46 量到）帶汙染跡象：同場景 adaptive / incremental = 1.15、與另一 seed 的對照 = 1.21，兩者都超出六方法在 10K 觀察到的最大值（1.06、1.15）。其餘 100 個已完成場景與對照的比值都在正常變異內（0.83–1.20；六方法的 sibling max/min 在 10K 為 1.16、100K 為 1.41）。處置：停下續跑、刪除該場景的 checkpoint、重新啟動讓 runner 以同一合約重量它（連同其餘 7 個）。依據：訓練程序的啟動時間無法回推，但汙染會先出現在最後完成的場景；用六方法的實測變異當門檻是唯一不依賴主觀判斷的準則。代價：多花約 30 分鐘（重播 100 個場景 + 重量一個 10K 場景）；若訓練其實更早開始，前一個場景（1K，14:41–14:42，比值 1.02–1.05）也可能受影響，但它在門檻內，保留。

**裁決 6-3：看管程式的自己人判定**。`supervise.py` 改成：`python` / `uv` 只有命令列含 `supervise.py` / `bench_launch` / `adaptive_benchmark` / `evaluate_adaptive` / `calibrate_adaptive` / `vcp-data` 之一才算自己人；其他 python ≥ `--big`（4 GiB）持續 30 秒即停 benchmark 並丟棄進行中場景。這樣訓練程序會在吃掉主機前先觸發停機，保住 timing 的乾淨。runner 本身的常駐量每 5 分鐘取樣（`heldout-v1.memory-samples.log`），峰值 2.04 GiB，沒有洩漏跡象；六方法期間的多次 RAM 中止應歸因於主機的 nonpaged pool 洩漏與桌面程式，不是 runner。

**執行紀錄**：三次啟動（20:39 / 17:46 / 18:12 UTC），DONE 19:49 UTC；最後一次最低可用 RAM 13.79 GiB；每次續跑 runner 驗證 168 個原始檔雜湊，原始樹整段未變；輸出由 runner 直接發布（含 evaluator 的 `validate_rows` 與 108 個 decision 的重算比對）。
