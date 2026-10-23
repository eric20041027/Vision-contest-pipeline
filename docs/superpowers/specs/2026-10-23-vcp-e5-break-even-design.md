# E5 打平點實驗：越過成本模型的打平點，policy v2 會不會改選 FULL

- 日期：2026-10-23
- 來源：Data_Base（CS 5151/6051 課程專題）session 的 E5 設計（2026-10-23）。課程進度報告（10/25）已把 E5 寫成接下來的實驗，承諾 11/12 前完成，11/15 投影片使用。
- 使用者的決定（2026-10-23）：
  - E5 排在所有 vcp 後續之前（`docs/superpowers/plans/2026-10-23-rsna-wrapup-followups.md` §0）；
  - 密度用兩段做：A 段只有修改（MODIFIED），B 段用新增（ADDED）的比例超過 0.5（§3）；
  - 事先登記加上「v2 預測在哪個密度改選 FULL」（§6.2）。
- 版本：不發版。E5 只動 `tests/performance/provenance/`，`src/vcp` 不變（§9）。
- 凍結、不動的東西：policy v2 `postgres-adaptive-v2-9f4e58346529`（`policy.json` SHA-256 `6fe654a90468edb57aa84fdec7ac2e53cf18b20106f7a77d65df8ac7cd27c000`）與 policy v1 `postgres-adaptive-v1-9f4e58346529`（`a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78`）；既有的所有證據與既有 workload family 的產生結果。不重新擬合、不改信心帶。

## 0. 給第一次讀的人

- policy v2 的兩個成本模型（截距都是 0；`E` = total_edges，`H` = historical_changes，`C` = changed_samples）：
  - incremental：`1.1624·C + 0.1182·E`
  - full：`0.7037·E + 0.0968·H`
  - 相減：`full − incremental = 0.5855·E + 0.0968·H − 1.1624·C`。打平點在 `C ≈ 0.5037·E + 0.0833·H`。
- v2 的選擇規則：`預估 incremental + 帶寬_inc < 預估 full − 帶寬_full` 才選 INCREMENTAL，否則 FULL。帶寬依 `E` 的十進位層（decade）查表：decade 3 為 90.9 + 465.3 = 556.2 ms，decade 4 為 1,168.0 + 4,418.3 = 5,586.3 ms。
- 「密度」在本文指 `C / E`。

## 1. 問題（量到的事實）

- held-out v2 的 92 個非零場景，v2 全部選 INCREMENTAL；它最多只能和「固定用 INCREMENTAL」打平。
- 原因是合成資料從來沒有越過打平點：現有 family 即使 100% 變更，密度也只有約 1/6（1K：`C` 326、`E` 1,982、`H` 652）。
- 唯一靠近打平點的是真實 RSNA r3→r4（4,407 個變更，`E` 約 8,873–9,437，密度約 0.47–0.50），而且勝負會翻轉：9 月 pg_full 5,625 ms 比 pg_incr 6,056 ms 快，10 月 pg_incr 6,788 ms 比 pg_full 7,221 ms 快。
- E5 要回答：越過打平點以後，FULL 是不是真的比較快？v2 會不會跟著改選 FULL？選得對不對？

## 2. 密度的結構上限（可行性結論）

- 決策時的 `E` 是套用這次 diff **之後**的圖的邊數；`H` 不含這次的 diff。held-out v2 每一列的 `estimated_*_ms` 都和這個算法逐位吻合（`postgres.py` 的 `decide()`）。
- 每一筆 MODIFIED 變更在圖裡加 2 條 `CONTAINS_CHANGE` 邊（變更前、變更後各一）；ADDED 或 REMOVED 只加 1 條（另一側的 row hash 是空的，`graph.py` 的 `add_dataset_diff_transition`）。`C` 數的是變更列數；`create_dataset_diff` 每個樣本只產生一列。每個 diff 另外固定多 1 條 `DERIVED_FROM` 與 2 條 dataset 的 `PRODUCED_BY`（有 source audit 再加 2 條）。
- 獨立審查（2026-10-23）用三種方式驗證了上面兩點：讀程式的執行順序；held-out v2 的 432 列都符合 `E = 2(C + H) + 26`，216 個 adaptive 列從特徵重算的預估值誤差為 0；在暫存目錄建圖數邊（MODIFIED 2 列加 4 條、ADDED 60 列加 60 條、REMOVED 2 列加 2 條，重放結果與 `build_graph` 的正本一致）。
- 所以只有修改的 diff，`E ≥ 2·C`，密度一定小於 0.5；代進凍結的係數，`full − incremental ≥ 0.0086·C + 0.5855·(其他邊) + 0.0968·H > 0`。**凍結的模型對只有修改的 diff 永遠不會預估 FULL 比較便宜**，最多在密度逼近 0.5 時差距逼近 0。
- 要超過 0.5 只能靠 ADDED（或 REMOVED）：它們每筆只加 1 條邊，密度可以推到接近 1。
- 這是 E5 的第一個發現，不論量測結果如何都成立；它也解釋了 RSNA r3→r4 為什麼卡在 0.5 附近。

## 3. Workload：`dense` family

### 3.1 結構

- 兩個 dataset：`dense-source`（`S` 個樣本）與 `dense-target`，由 vcp 自己的 `create_dataset_diff` 從兩份 samples 產生 pending diff。**沒有歷史 diff**（`H = 0`），讓密度成為唯一變動的特徵。
- 這次的 diff 有 `m` 筆 MODIFIED 與 `a` 筆 ADDED，`C = m + a`。
- 填充（filler）沿用既有 family 的尾巴拓樸：一個 split plan、一個消費 `dense-source` 的 run，以及 `F` 筆讀數（每筆 1 個 entity、1 條邊）。
- 每層固定一個邊數目標，讓各密度級的 `E` 幾乎相同、只有 `C` 改變：
  - decade 3 層：`E ≈ 2,000`；
  - decade 4 層：`E ≈ 20,000`。
- 密度 `d = C / E`，其中 `E = 2m + a + F + s`（`s` 是 dataset、plan、run、diff 等結構邊，數量由產生器實際算出）。

### 3.2 密度級

| 級 | 目標密度 | 段 | 做法 |
|---|---|---|---|
| L1 | 0.17 | A | 只有修改；`F` 補足邊數。與現有 family 的上限同密度，當錨點（結構不同：`H = 0`，見 §10） |
| L2 | 0.30 | A | 只有修改 |
| L3 | 0.40 | A | 只有修改 |
| L4 | 結構上限（約 0.498） | A | 只有修改，`F = 0` |
| L5 | 0.60 | B | `a / C = 1/3`，`F = 0` |
| L6 | 0.75 | B | `a / C = 2/3`，`F = 0` |
| L7 | 1.00（約 0.997） | B | 全部新增，`F = 0` |

- 實際的 `C`、`E`、`d` 由產生器建出 workload 後回報，連同 §6.2 的預測一起在量測前 commit。
- 新 family 不改動既有 family：既有場景的 `scenario_hash`、產生結果與 `TOPOLOGIES` 預設矩陣都不變（測試鎖住）。

### 3.3 規模與 seeds

- 必做：decade 3 與 decade 4 兩層（對應 v2 的第 3、4 層帶寬）。
- 100K（decade 5）：只有 1K、10K 量完、時間與 C 槽空間都夠時才加；不加就在結果寫明。
- Seeds：`20261201`、`20261202`，從未用過。overlap 檢查：E5 的 seeds 與 calibration（20260913、20260914）、held-out v1（20261001、20261002）、held-out v2（20261101、20261102）不相交，場景 hash 也不相交；`overlap_count` 必須為 0。
- 場景數：2 層 × 7 級 × 2 seeds = 28 個，全部非零。

## 4. 方法與量測

- 方法：`postgres_full`、`postgres_incremental`、`postgres_adaptive`（policy v2）、`postgres_adaptive_v1`（policy v1，對照）。SQLite 不量。
- 標準答案：canonical replay 算出的 expected graph。每一列做 exact parity：graph、graph hash、head、status。
- 重複次數、warmup、每個場景用全新資料庫：都和 held-out v2 相同（decade 3、4 每個方法 7 次）。
- 每個 adaptive 列的決策，用它的 policy 從記錄的特徵重算，必須完全一致。
- 看管：沿用 `supervise.py`（RAM 護欄、靜默時段、GPU 與 CPU 偵測、別的大程式出現就暫停）；重量只依時段或失敗狀態挑，不看數值（裁決 8–12 的做法）。
- 每一列記錄 `estimated_incremental_ms`、`estimated_full_ms`、`total_edges`、`changed_samples`、`dirty_entities` 與兩種實測值。

## 5. 量測前的準備

- 量測前檢查 C 槽可用空間；不足就先清，不在量測中途清。
- 從 E5 runner 合併後的 commit 開一個 detached worktree 量測；量測期間該 worktree 的原始檔不得改動（runner 釘住原始檔雜湊）。
- 量測期間（11/3–11/10）機器保持安靜：不跑測試、不訓練。

## 6. 事先登記

本節、§3 的密度級與 seeds、§6.2 的預測檔與分析腳本，全部在任何 E5 量測之前 commit。第一個 E5 量測開始之後不得修改；要改就換新的 seeds 與新的實驗 id。

### 6.1 正確性 gate（必須通過，否則該份證據無效）

- 每一列 parity 為 true；
- 每個 adaptive 決策重算一致；
- `overlap_count` 為 0。

### 6.2 預測（量測前從凍結的 policy 算出並 commit）

- 對每個場景，用產生器實際算出的 `C`、`E`、`H` 計算 `estimated_*_ms`、模型的勝方，以及 v1、v2 的選擇。
- 依名目值（`H = 0`）事先可以算出：
  - **模型的打平點**：`d* = 0.5855 / 1.1624 ≈ 0.504`，兩層相同。L1–L4 模型預估 INCREMENTAL 較快，L5–L7 預估 FULL 較快。
  - **v2 改選 FULL 的密度**：decade 3 約 0.264（`E = 2,000`、帶寬 556.2 ms），decade 4 約 0.263（`E = 20,000`、帶寬 5,586.3 ms）。也就是說 v2 預計從 L2（0.30）起就選 FULL，比模型的打平點早得多。
  - **v1**：decade 3 一律 FULL（帶寬 7,343.7 ms 大於最大差距約 1,171 ms）；decade 4 只有 L1 選 INCREMENTAL（改選點約 0.188）。
- 預測檔：`docs/benchmarks/postgres-provenance-break-even-v1-predictions.json`，寫一次不改。

### 6.3 假設

- **H5a（交會點存在）**：至少有一層、一個密度級，該級每個場景都是 `pg_full p50 ≤ 0.95 × pg_incr p50`。
  - **實測交會點**定義為每層「該級以上每一級都符合上式」的最低一級；回報它落在哪個密度，並和 `d* ≈ 0.504` 比較。
  - 某一層的最高密度級沒有符合上式時，該層沒有實測交會點（勝負不單調也算在內）。
- **H5b（門檻）**：把 E5 全部 28 個場景的原始樣本 pool 起來，`v2 adaptive p50 / min(兩個固定方法的 pooled p50) ≤ 1.05`、`p95 比值 ≤ 1.10`（與 E3 同一門檻與算法）。
- **H5c（自適應的價值）**：把每一層位於該層實測交會點（含）以上的場景一起 pool 起來，`v2 adaptive pooled p50 < pg_incr pooled p50`。H5a 不成立，或兩層都沒有實測交會點時，標為「不適用」。
- 判定只寫「成立」「不成立」或「不適用」，附兩邊數字。

### 6.4 診斷（只報告，不判定）

- 兩種固定方法差距超過 5% 的場景裡，v2（與 v1）選對的比例，依層 × 密度級列出。
- 每個場景的 `adaptive / 較佳固定方法` 與 `adaptive / pg_incr`。
- 模型殘差：每一級的 `實測 − 預估`（兩種方法分開），特別是 B 段：成本模型只用修改型的資料擬合過，B 段是外推。
- `dirty_entities` 依密度的變化。
- v2 選 FULL 的兩種原因要分開報：「模型預估 FULL 較便宜」與「差距在帶寬內、不確定」。兩者的 `strategy_reason` 相同（`calibrated_full_lower_or_uncertain_cost`），只能用 `estimated_*_ms` 欄位分辨。

### 6.5 禁止事項

- 看過任何 E5 結果之後，不得調整 policy、帶寬、密度級、§6.3 的條件或分析腳本後重跑。
- 不得以「已知問題」為由排除任何場景；H5b 失敗就照實報告為失敗。

## 7. 分析腳本

- `tests/performance/provenance/break_even_analysis.py`：只吃量測輸出的列，算出 §6.1–§6.4 的全部判定與表格，不讀其他來源。
- 單元測試用捏造的列覆蓋每個判定分支（成立、不成立、不適用、交會點的定義）。
- 與 §6.2 的預測檔一起，在量測前 commit。

## 8. 交付

1. `docs/benchmarks/postgres-provenance-break-even-v1.json`：寫一次不改，附 SHA-256、commit、兩個 policy 的 id 與 `policy.json` SHA-256。
2. `docs/benchmarks/postgres-provenance-break-even-v1.md`：可達到的最高密度；層 × 密度級的表（pg_full、pg_incr、v2 p50、v2 選擇、adaptive ÷ 較佳固定方法、adaptive ÷ pg_incr）；實測交會點與模型預測的比較；H5a/H5b/H5c 判定；高密度的模型殘差；所有偏離。
3. 給 Data_Base 的結果摘要（比照 `VCP_POLICY_V2_RESULTS.md`），數字能直接對回 JSON，放在使用者指定的位置，不進 repo。

## 9. 相容性與版本

- `src/vcp` 不變，不發版。新程式只在 `tests/performance/provenance/`：`workloads.py` 的 `dense` family、`evaluate_adaptive.py` 的 E5 模式、`break_even_analysis.py`。
- 既有 workload 與既有證據的重現性由測試鎖住（§3.2）。

## 10. 風險與已知限制

- **`H = 0`**：真實的圖都有歷史；`H` 讓 FULL 更貴、打平點更高。E5 刻意固定 `H = 0` 隔離密度，結論只對這個結構成立。L1 與現有 family 同密度但結構不同，不能逐值比較。
- **B 段是外推**：成本模型沒有見過 ADDED 型的變更（calibration 與 held-out 全是 MODIFIED，密度最高 0.1666）。ADDED 一律帶四個 semantic effect，dirty 範圍比較大，但 `dirty_entities` 的係數是 0，模型看不到這部分成本。殘差就是要回答模型在那裡準不準。
- **PostgreSQL 的 incremental 路徑沒有測過 ADDED**：既有測試只改既有樣本。量測前先加一個 ADDED 型 diff 的正確性測試（incremental 與 full 的結果都要和 canonical 一致），在 CI 的 `postgres` job 跑。測出 `src/vcp` 的 bug 就停下來，修正與發版另行決定，記為偏離。
- **只有兩層**：100K 可能沒做。
- **同一台機器、同一個產生器**：和 E3 相同的限制。

## 11. 時程

| 時間 | 事項 |
|---|---|
| 10/28 前 | 本 spec、`dense` 產生器（只建圖、不計時）、實際密度、§6.2 預測檔、分析腳本與測試，合併 |
| 11/2 前 | ADDED 型 diff 的 PostgreSQL 正確性測試；E5 runner 模式與測試，合併 |
| 11/3–11/10 | 正式量測 |
| 11/12 前 | §8 的三項交付 |

## 12. 執行期修訂

（量測開始後的任何偏離，記在這裡與後記，附日期與理由。）
