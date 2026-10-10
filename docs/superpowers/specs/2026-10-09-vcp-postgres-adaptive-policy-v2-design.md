# PostgreSQL adaptive provenance：policy v2 設計

- 日期：2026-10-09
- 來源：Data_Base（CS 5151/6051 課程專題）session 的交接（2026-10-09），以及 `docs/superpowers/plans/2026-09-13-vcp-postgresql-adaptive-provenance-followups.md` §3-6、§3-7、§5–§7。
- 使用者的決定（2026-10-09）：
  - policy v2 在課程期間推進，排在稽核 Wave 1c 之前；
  - full rebuild 收尾**不加** `VACUUM`，寫進操作指南（§2）；
  - v2 **沿用** calibration v2 的量測擬合，不重量（§4.3、§12）；
  - 信心帶的候選是 A（相對帶）與 B（按規模分層的絕對 RMSE），用 §5 預先登記的程序只靠 calibration 資料選出一個；
  - 不改 DDL、不加 reason code，v1 與 v2 用 `policy_version` 分辨（§4.6）。
- 版本：0.15.0（MINOR），理由見 §9。
- 凍結、不動的東西：policy v1 `postgres-adaptive-v1-9f4e58346529`（`policy.json` SHA-256 `a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78`），以及五份證據：integration v3、calibration v2、six-method v1、held-out v1、real RSNA v1。不改、不重新發布、不重新擬合。

## 0. 給第一次讀的人

這一節不需要先懂 VCP。

- **provenance 索引**：VCP 把「哪個訓練、哪個讀數用了哪一版資料的哪些樣本」記成一張圖（entity 是節點，edge 是「誰用了誰」）。資料改版時，要把這張圖更新到新版。圖的權威來源是檔案；索引只是可以刪掉重建的副本，可以放在 SQLite 或 PostgreSQL。
- **兩種更新方式**：
  - **incremental**：只重算這次改版碰到的那部分圖。改得少時很快。
  - **full**：整張圖從頭重建，再一次換上去。永遠正確，但成本跟整張圖的大小成正比。
- **adaptive（`--strategy auto`）**：每次更新前先預估兩種方式各要多久，挑便宜的那個。挑錯不會算錯（兩種結果逐位元相同），只會比較慢。
- **policy**：做這個預估與選擇的「規則包」。它由兩個成本模型（預估 incremental 與 full 各要幾毫秒）加一條「多有把握才選 incremental」的規則組成。成本模型從 calibration（事先在這台機器上量的 1,224 次量測，整理成 92 個觀測）擬合出來。policy 一旦寫下就不改（write-once）；要改就發新版。
- **v1 的規則**：只有在 `預估 incremental + 2,286 ms < 預估 full − 5,057 ms` 時才選 incremental，否則選 full。2,286 與 5,057 是兩個成本模型在整份 calibration 上的誤差（RMSE），加起來 7,343 ms，也就是 7.3 秒。
- **v1 的問題**：7.3 秒這個「把握」是固定的，跟這次要更新的圖多大無關。它的量級由最大的 100K 規模決定。所以只要兩個預估相差不到 7.3 秒，不管 incremental 其實便宜多少，v1 都選 full。§1 是實際量到的後果。
- **v2 要做的**：讓「把握」跟這次的預估值成比例（或跟這次的規模層相稱），小圖、差距小的情況也能正確選 incremental。

## 1. 問題（量到的事實）

| 情況 | 預估差距（full − incremental） | v1 的選擇 | 實測 |
|---|---|---|---|
| 合成 1K，28 個非零場景 | 約 0.84 秒 | 全部 FULL（`calibrated_full_lower_or_uncertain_cost`） | held-out v1 上比 incremental 慢 1.82–4.26 倍 |
| 合成 10K／100K，64 個非零場景 | 8.4 秒以上 | 全部 INCREMENTAL | 正確（差距在重複雜訊內） |
| 真實 RSNA r4→r5（8.8K entities、98.7% 變更） | 5.7 秒（預估 7,127 對 12,788 ms） | FULL | FULL 10,502 ms，incremental 5,865 ms，慢 1.79 倍 |

- held-out v1 的正式（aggregate）gate 仍然通過（p50 1.018、p95 1.017），因為 1K 的樣本拉不動 pooled 的分位數；every-scenario diagnostic 是 36/92 個場景超過 1.05 倍，1K 的 28 個全部在內。
- 裁決 7-1（followups §7）：問題的本質是「預估差距相對於預估值」，不是「圖小」。r4→r5 有 8.8K entities 也中。
- 這是效能問題，不是正確性問題：所有證據的 parity 全部為 true。

## 2. 範圍

做：

- policy v2 的資料模型、擬合、載入與選擇（§4）。
- 候選帶的預先登記比較程序與它的證據（§5）。
- 三段量測：six-method v2、held-out v2（v1 與 v2 同場）、real RSNA v2（§6），以及預先登記的驗收條件（§7）。
- 文件、0.15.0 發版、交給課程的 course brief v2（§8、§9、§11）。

不做：

- **full rebuild 收尾的 `VACUUM`**（followups §3-7）。使用者 2026-10-09 決定不加進程式，理由：
  - 一般 `VACUUM` 讓 dead tuples 可重用、更新 visibility map，`status` 會變快，但幾乎不縮小檔案，所以 storage 約 2 倍的現象不會消失；
  - `VACUUM FULL` 才會縮小，但它對整張表上 ACCESS EXCLUSIVE 鎖，擋住所有讀者，抵銷 PostgreSQL 後端「讀者不被擋、原子發布」的價值；
  - 兩者都不能在交易內執行；加進程式會改變 FULL 的實測成本，calibration 就不能沿用，v1 與 v2 也不再在同一個 FULL 成本上比較。
  - 改成寫進操作指南（§8）。量測口徑（`pg_total_relation_size`）不變。
- **DDL 變更與新的 reason code**（§4.6）。
- **`BENCHMARK_SCHEMA_VERSION` 升版**（§6.3）。
- **重新量 calibration**：沿用 calibration v2（§4.3、§12 的限制）。
- 1M 規模（followups §1 的裁決照舊）、Linux CI 的 integration evidence（§3-3）、EXPLAIN 覆蓋面（§3-4）：不在這一版。
- 方案 C（帶寬 = `a + b·預估值`）：參數多一組、最難解釋，不列為候選。

## 3. 命名對照

證據與 policy 的版本號各自獨立計數。為了不再出現「calibration v2 產生 policy v1」那樣的混淆，一律照這張表稱呼：

| 步驟 | 名稱 | 狀態 |
|---|---|---|
| 量測，擬合用 | calibration v2（`docs/benchmarks/postgres-provenance-calibration-v2.json`，量測 commit `b1512ae`） | 已有，沿用、不改 |
| 擬合 | policy v1 `postgres-adaptive-v1-9f4e58346529` | 已有，凍結 |
| 候選帶比較 | band comparison v1（`docs/benchmarks/postgres-provenance-band-comparison-v1.json`） | 新，§5 |
| 擬合 | policy v2 `postgres-adaptive-v2-9f4e58346529` | 新，§4 |
| 量測，預覽 | six-method v2（calibration seeds） | 新，§6.2 |
| 量測，正式驗收 | held-out v2（新 seeds，v1 與 v2 同場） | 新，§6.3 |
| 量測，真實資料 | real RSNA v2（sanity check） | 新，§6.4 |
| 交給課程 | course brief v2（`docs/benchmarks/postgres-provenance-course-brief-v2.md`） | 新，§8 |

policy v2 的 id 尾段跟 v1 相同（`9f4e58346529`），因為 id 由 calibration 的 SHA-256 前 12 碼衍生，而兩者用同一份 calibration。版本前綴不同，所以是兩個不同的 artifact。

## 4. Policy v2 的資料模型與選擇

### 4.1 資料模型

- v1 的 `AdaptivePolicy` 一字不改，包括 `_derived_policy_id`（`postgres-adaptive-v1-<calibration sha 前 12 碼>`）。
- 新增 `AdaptivePolicyV2`（嚴格模型，`extra="forbid"`）：
  - `policy_version: Literal["postgres-adaptive-v2"]`；
  - 跟 v1 相同的欄位：`backend`、`backend_schema_version`、`postgresql_major`、`benchmark_schema_version`、`environment_fingerprint`、`calibration_sha256`、`incremental_model`、`full_model`、`training_row_count`；
  - 沒有 v1 的 `incremental_rmse_ms` / `full_rmse_ms`；
  - `band`：依 `kind` 分辨的聯集，只存 §5 選出的那一種：
    - `RelativeBand`：`kind: Literal["relative"]`、`incremental_relative_rmse: float ≥ 0`、`full_relative_rmse: float ≥ 0`；
    - `StratifiedEdgesBand`：`kind: Literal["stratified_edges"]`、`strata`：依 `decade` 遞增、不重複的清單，每一層是 `decade: int ≥ 0`、`incremental_rmse_ms: float ≥ 0`、`full_rmse_ms: float ≥ 0`、`observations: int ≥ 1`。
  - `id`：`postgres-adaptive-v2-<calibration sha 前 12 碼>`。
- 兩版的 `policy.json` 各自只能用自己的模型驗過：v2 的 payload 帶 v1 的 RMSE 欄位、或 v1 的 payload 帶 `band`，都是 schema 錯誤。

### 4.2 兩種帶的定義

符號：`est_inc`、`est_full` 是兩個成本模型對這次 features 的預估（毫秒）。

- **A：相對帶（`relative`）**
  - 擬合：對每個 calibration 觀測 i，相對殘差 `q_i = (實際_i − 預估_i) / 預估_i`；`r = √(mean(q_i²))`，incremental 與 full 各算一個。
  - 選擇：`est_inc · (1 + r_inc) < est_full · (1 − r_full)` 時選 INCREMENTAL，否則 FULL。
  - 退化：`r_full ≥ 1` 時右邊不大於 0，永遠選 FULL。這是有意的保守結果，不另外處理。
  - 擬合時任何 `預估_i ≤ 0` → `ValidationFailed`（成本模型係數非負、截距 0，calibration 的觀測都有變更，正常不會發生）。
- **B：按規模分層的絕對 RMSE（`stratified_edges`）**
  - 分層規則：`decade = ⌊log10(total_edges)⌋`；`total_edges < 1` 視為 decade 0。calibration v2 的三個規模層正好是 decade 3、4、5。
  - 擬合：每個 calibration 裡出現的 decade 各算一個 incremental RMSE 與一個 full RMSE（毫秒）。
  - 選擇：這次 features 的 decade 若不在 `strata` 裡，用距離最近的那一層；距離相同時取較大的 decade（較保守，因為大規模層的 RMSE 較大）。然後 `est_inc + rmse_inc[層] < est_full − rmse_full[層]` 時選 INCREMENTAL，否則 FULL。
- 兩種帶都不用任何寫死的門檻：帶寬全部由 calibration 資料推導（延續 v1「沒有 hard-coded dirty-ratio 門檻」的原則）。B 的分層規則（以 10 為底的數量級）是一條固定的分組規則，不是調出來的門檻；它的限制寫在 §12。

### 4.3 擬合

- 新函式 `fit_policy_v2(calibration_rows, *, band: Literal["relative", "stratified_edges"]) -> AdaptivePolicyV2`。
- 兩個成本模型的擬合跟 v1 完全相同（同一個非負線性迴歸、同樣的 feature 順序、截距 0）。所以對 calibration v2，v2 的係數必須與 v1 相同：逐項相對誤差 ≤ 1e-9。不同就停下來查（例如 scikit-learn 版本改變），不得繼續發布。
- 只有帶不同。`band` 由 §5 的比較結果決定，記在 §13；§13 填入之前，不得產生正式的 v2 policy artifact。
- 寫入：照 v1 的 artifact 程序（`write_policy_artifact`），write-once，manifest 釘住 calibration 的 SHA-256，`policy.json` 與 `calibration.json` 兩個檔。

### 4.4 載入與分派

- `load_policy_artifact` 先讀 `policy.json` 的 `policy_version`，再用對應的模型做嚴格驗證：
  - `postgres-adaptive-v1` → `AdaptivePolicy`；
  - `postgres-adaptive-v2` → `AdaptivePolicyV2`；
  - 其他 → `ValidationFailed("incompatible_policy: provenance policy policy version")`，跟現在相同的字彙。
- manifest、calibration pin、calibration 副本、環境相容性（backend、schema 版本、PostgreSQL 主版號、環境指紋）的檢查兩版共用，行為不變。
- 回傳型別是兩者的聯集。

### 4.5 選擇

- `select_strategy(requested, features, policy)` 的簽名不變。
- NO_OP、`--strategy incremental`、`--strategy full`、沒有 policy 的分支在 v1 與 v2 之間都不變。
- `auto` 有 policy 時依型別分派：v1 分支的程式碼不動；v2 分支用 §4.2 的規則。
- `StrategyDecision.policy_version` 是 policy 的 `policy_version`（v2 為 `postgres-adaptive-v2`）。
- `estimated_incremental_ms` / `estimated_full_ms` 照舊是成本模型的預估（不含帶）。

### 4.6 telemetry：不改 DDL

- PostgreSQL 的 `maintenance_decisions` 表把 `reason_code` 限定在 CHECK 列舉的字彙。加新的 reason code 就要改 DDL、升 `POSTGRES_SCHEMA_VERSION`；而 v1 policy 釘著 `backend_schema_version = 1`，升版會讓 v1 載入失敗，違反「v1 照舊載入、照舊決策」。
- 所以 v2 沿用同兩個 reason：`calibrated_incremental_lower_confident_cost` 與 `calibrated_full_lower_or_uncertain_cost`。
- 一個決策是哪一版的帶，由同一列的 `policy_version` 欄位分辨（`postgres-adaptive-v1` 或 `postgres-adaptive-v2`）。這個欄位本來就存在、是自由文字，DDL 不變。
- 這偏離交接文件「reason code 能分辨 v1／v2」的字面要求，但分辨的需求由 `(policy_version, reason_code)` 這一對滿足。

### 4.7 CLI

- `vcp provenance ingest --backend postgresql --strategy auto --policy <id>` 不加選項。傳 v2 的 id 就走 v2。
- VERDICT 與 `--json` 的欄位不變；`policy_version` 多一個可能的值。

## 5. 候選帶比較（預先登記）

這一節是 v2 選哪一種帶的唯一依據。它只用 calibration 資料，在任何 v2 量測之前完成。分析可以由課程組員 En Shuo Zhang 執行：輸入只有一個 JSON 檔，不需要懂 VCP 的其他部分。

### 5.1 為什麼不能直接比「誰選錯比較少」

- calibration 的 92 個觀測裡，full 從來沒有比 incremental 快（12 個切片的 crossover 全部 `not_observed`）。在這份資料上「選錯最少」的帶，就是最窄的帶，等於把安全邊際拿掉。
- 也不能拿「帶寬跟同一份資料的誤差合不合」比：B 的每一層帶寬本來就是那一層的 RMSE，在擬合它的資料上必定完全吻合。
- 所以比較一律用「沒參與擬合的資料」，看帶寬是否貼合實際誤差。

### 5.2 資料

- `docs/benchmarks/postgres-provenance-calibration-v2-artifacts/inputs/calibration.json` 的 92 個觀測（SHA-256 前 12 碼 `9f4e58346529`）。
- 兩個 seed（20260913、20260914）各 46 個；三個規模層（decade 3、4、5）。

### 5.3 主判準：以 seed 交叉驗證

1. 兩個方向：用 seed 20260913 的觀測擬合、拿 20260914 的檢驗；反過來再做一次。
2. 每個方向，只用訓練資料：擬合兩個成本模型（§4.3 的同一個擬合器），再擬合 A 與 B 的帶（§4.2）。
3. 在檢驗資料的每個觀測 i 上，對兩個模型（incremental、full）各算標準化殘差 `z_i = (實際_i − 預估_i) / w_i`：
   - A：`w_i = r × 預估_i`；
   - B：`w_i = 該觀測所在層（§4.2 的最近層規則）的 RMSE`。
4. 每個（方向、模型、decade）組合算 `s = √(mean(z²))`。帶寬跟實際誤差一樣大時 `s = 1`。
5. 一個候選的分數 = 所有組合中最大的 `|ln s|`。分數越小越好。任一個 `w_i = 0`（帶寬為 0）時，該候選的分數為 +∞。
6. 選擇：
   - 兩個分數相差小於 0.1（`|score_A − score_B| < 0.1`，約 10%）時選 A：較簡單，也是裁決 7-1 的方向；
   - 否則選分數較小的；
   - 兩者都是 +∞ 時，不發布 v2，回報使用者。

### 5.4 只報告、不拿來決定的數字

- **留一層外推**：用兩個 decade 擬合、檢驗第三個（三種留法），算同樣的 `s`。看外推到沒見過的規模時誰較穩。
- **在 calibration 上的決策**：用全部 92 個觀測擬合的 A 與 B，各自在這 92 個觀測上的選擇；1K 層選 INCREMENTAL 的比例；選了較慢那一方的次數與多花的毫秒數。
- 兩個帶在全資料上的數值（A 的 `r_inc`、`r_full`；B 每層的兩個 RMSE），跟 v1 的 2,286 / 5,057 ms 並列。

### 5.5 程序

1. 比較腳本（`tests/performance/provenance/compare_bands.py`）與它的單元測試先 commit；之後才對 calibration v2 執行。commit 的先後就是預先登記的紀錄。
2. 輸出 `docs/benchmarks/postgres-provenance-band-comparison-v1.json`，write-once：輸入檔的 SHA-256、腳本的 commit、每個組合的 `s`、兩個分數、勝者、§5.4 的數字。
3. 勝者寫進 §13（spec 的執行期修訂），然後才擬合正式的 v2 policy。
4. 執行失敗的嘗試也保存（`docs/benchmarks/` 下另存，檔名加 `.failed-<n>`），不覆寫。

## 6. 量測

### 6.1 環境與看管

- 程式合併、發 0.15.0 之後才開始量測。所有量測都從 `v0.15.0` tag 的 detached worktree 執行；runner 釘住原始檔雜湊，量測期間這個 worktree 不得有任何改動，開發照常在別的 worktree 做。
- 長跑由 `C:/vcp-data/bench/supervise.py`（不進 repo）看管：
  - RAM 護欄照舊：可用 RAM < 2 GiB 持續 30 秒即中止，runner 從逐列 checkpoint 續跑；
  - 裁決 6-3 的「自己人」判定照舊：其他 python 程序 ≥ 4 GiB 持續 30 秒就停 benchmark，丟棄進行中的場景；
  - 裁決 6-2 的汙染處置照舊：中止前最後完成的場景，若與對照的比值超出 six-method 觀察到的變異，就刪掉它的 checkpoint 重量。
- 排程避開 RSNA 的訓練：10/26 之後才開始（RSNA 截止日 10/22）。
- PostgreSQL 只用 libpq service name；不讀、不複製 `pg_service.conf` / `pgpass.conf`。

### 6.2 six-method v2（預覽）

- calibration seeds（20260913、20260914），108 個場景 × 6 種方法，重複次數照 v1（1K 7 次、10K 7 次、100K 3 次）。
- `postgres_adaptive` 用 policy v2。其他五種固定方法也在同一次執行裡重量，比較的才是同一時間、同一台機器的數字。
- 這是預覽：它的 seeds 就是 calibration 的 seeds，不是 held-out 證據。

### 6.3 held-out v2（正式驗收）

- **seeds：20261101、20261102**。這兩個 seed 從未被任何量測或探路使用（已用過的是探路的 20260908、20260909，calibration 的 20260913、20260914，held-out v1 的 20261001、20261002；2026-10-09 以 `git grep` 核對過，整個 repo 與 `docs/benchmarks/` 的證據都沒有出現 20261101、20261102），在此登記，並寫成程式常數 `HELDOUT_V2_SEEDS`。v1 的 held-out seeds（20261001、20261002）的結果已經被拿來設計 v2，不得再當 held-out。
- 每個場景四種方法，同一次執行：`postgres_full`、`postgres_incremental`、`postgres_adaptive`（policy v2）、`postgres_adaptive_v1`（凍結的 policy v1）。108 個場景 × 4 = 432 列。
- 既有的守門照舊：workload 與 calibration 的重疊檢查（`overlap_count` 必須為 0）、parity、每個 adaptive 列都用它的 policy 重算決策並逐欄比對（policy id、policy SHA-256、選擇、reason、`policy_version`、兩個預估值）。
- **`BENCHMARK_SCHEMA_VERSION` 維持 1**：v1 與 v2 policy 都釘著它，升版會讓兩者都載入失敗。兩個 policy 的同場評估只在評估輸出那一層加方法名稱，不改 calibration 觀測的列格式。

### 6.4 real RSNA v2（sanity check）

- `rsna-knee-sixslot` r3→r4→r5→r6 的唯讀複製（metadata-only），六種方法加上 `postgres_adaptive_v1`。
- r4→r5 是 v2 的設計動機，所以它只能證明「v2 在這個已知案例上的選擇」，不能當 held-out 證據。報告裡要這樣寫。

### 6.5 證據規則

- 每份證據 write-once；失敗的嘗試也保存。
- 數字只從 machine-readable 輸出回讀後才填進文件；探路數字不填正式表。
- 每份新證據記下：commit、SHA-256、policy id 與 `policy.json` 的 SHA-256。

## 7. 預先登記的驗收條件

本節與 §6.3 的 seeds 隨本 spec 在任何 v2 量測之前 commit。之後不得修改；要改就是 policy v3，並換新的 seeds。

### 7.1 正確性 gate（必須通過，否則該份證據無效）

- 每一列 parity 為 true。
- 每個 adaptive 列的決策用它的 policy 重算後完全一致（§6.3）。
- held-out v2 的 `overlap_count` 為 0。

### 7.2 正式效能 gate（對 policy v2，在 held-out v2 上）

跟 v1 的定義相同：每種方法把所有場景的原始樣本 pool 起來取 p50 與 p95，

- `postgres_adaptive 的 pooled p50 / min(postgres_full, postgres_incremental 的 pooled p50) ≤ 1.05`；
- `postgres_adaptive 的 pooled p95 / min(兩個固定方法的 pooled p95) ≤ 1.10`。

同一組數字也對 `postgres_adaptive_v1` 計算並並列報告，但 v1 的結果不影響 v2 的 gate。

### 7.3 診斷（照報，不是 gate）

兩個 policy 都報，以 held-out v2 的非 NO_OP 場景為母體：

- 每個場景的 `adaptive p50 / 較佳固定方法 p50`；超過 1.05 倍的場景數；最差倍率；
- 依 decade（3、4、5）拆開的同樣數字；
- 決策模式：每一層選 INCREMENTAL / FULL / NO_OP 的次數。

### 7.4 改善假設

- **H1**：在 held-out v2 的非 NO_OP 場景中，v2 超過 1.05 倍的場景數少於 v1。
- **H1a**：只看 decade 3（1K）層，同樣的比較。
- 結果寫「成立」或「不成立」，附上兩邊的數字。這是要驗證的主張，不是 gate：不成立就是負面發現，照實報告。

### 7.5 禁止事項

- 看過 held-out v2 的任何結果之後，不得調整 policy v2、帶的種類、§5 的判準或本節的條件後重跑。
- §7.2 的 gate 失敗就照實報告為失敗；不得以「已知問題」為由排除任何場景。

## 8. 文件

- `README.md` 與 `README.zh-TW.md` 的 Status and roadmap：policy v2 從「1.0 之後」移到「進行中，排在稽核 Wave 1c 之前」。
- `docs/guides/POSTGRESQL_PROVENANCE.md`：
  - full rebuild 之後的 `VACUUM`：可以手動跑 `VACUUM (ANALYZE)` 讓 `status` 變快；storage 不會因此縮小；不建議 `VACUUM FULL`（擋住所有讀者）；
  - `auto` 的建議依 policy 版本分開寫：v1 的「小圖或接近全量變更直接 `--strategy incremental`」保留；v2 在 held-out v2 的結果出來後補上。
- skill `vcp-provenance` 與 `.agents/skills/` 的鏡射：v2 的 id、與 v1 的差別、VACUUM 的建議。
- `docs/reference/cli.md`：`--policy` 接受 v1 與 v2。
- 新的證據紀錄 `docs/benchmarks/postgres-provenance-v2.md`：§3 的命名對照、§5 的比較結果、§6 的三段量測、§7 的驗收結果。`postgres-provenance-v1.md` 不動。
- `docs/benchmarks/postgres-provenance-course-brief-v2.md`（交給 Data_Base）：格式同 v1；設計、方法、完整表格、v1 與 v2 的對照、負面發現、限制、每個檔案的 SHA-256、policy id 與 `policy.json` 的 SHA-256。
- 後記 `docs/superpowers/plans/2026-10-09-vcp-postgres-adaptive-policy-v2-followups.md`：裁決、已知限制、待辦。
- 交接文件（HANDOVER、CODEX_PROMPT）的 policy v2 那一項；舊後記 followups §3-6、§3-7 指向本 spec。

## 9. 相容性與版本

- **0.15.0（MINOR）**：
  - policy artifact 的新內容（`policy_version: postgres-adaptive-v2` 與 `band`）；
  - telemetry 與 VERDICT 的 `policy_version` 多一個值；
  - benchmark runner 的新 seeds 常數與新方法名稱。
- 不變：PostgreSQL DDL 與 `POSTGRES_SCHEMA_VERSION`（1）、`BENCHMARK_SCHEMA_VERSION`（1）、SQLite 索引、提交台帳與其他產物的格式。
- 0.14.0 讀不了 v2 的 `policy.json`（模型嚴格），會報 `incompatible_policy`；v1 policy 在 0.15.0 照舊可用。
- 不需要遷移：已建的 PostgreSQL 索引不必重建。

## 10. 測試

- **v1 回歸**：讀 six-method v1、held-out v1、real RSNA v1 三份已發布證據裡的每一個 adaptive 列，用凍結的 v1 policy 重算決策，選擇、reason、`policy_version` 與兩個預估值必須完全相同。
- **v2 單元測試**：
  - A 的邊界：剛好等號時選 FULL；`r_full ≥ 1` 永遠 FULL；
  - B 的分層：decade 在層內、低於最低層、高於最高層、距離相同時取較大的 decade；`total_edges = 0`；
  - 擬合：任何預估值 ≤ 0 → `ValidationFailed`；對 calibration v2，v2 的成本模型係數與 v1 相同（§4.3）；
  - schema：v1 與 v2 的欄位混用都失敗；未知的 `policy_version` 報 `incompatible_policy`；
  - 載入：v1 與 v2 artifact 都能照舊驗證，manifest 或 calibration pin 被改都失敗。
- **比較腳本**：用小型合成資料測 §5.3 的每一步（兩個方向、標準化殘差、分數、0.1 的平手規則、+∞ 的處理）。
- **runner**：held-out v2 的四方法列驗證、兩個 policy 的決策重算、seeds 常數不與任何舊 seeds 重疊。
- 既有測試全部照過；覆蓋率 ≥ 80%。

## 11. 時程

| 日期 | 事項 |
|---|---|
| 10/09–10/12 | 本 spec，使用者審查後 commit（含 §6.3 的 seeds 與 §7） |
| 10/18 前 | §5 的比較腳本 commit 後執行，結果寫進 §13 |
| 10/22 | 課程進度報告凍結數字（報告描述 v2 的設計與排程） |
| 10/25 前 | 實作、審查、合併，發 0.15.0 |
| 10/26–11/10 | six-method v2（約 2 天）、held-out v2（約 1–1.5 天）、real RSNA v2（約 20 分鐘），其餘是中止的緩衝 |
| 11/12 前 | course brief v2 與證據清單交回 Data_Base（11/15 交投影片） |

## 12. 風險與已知限制

- **calibration 量測時的程式碼較舊**：calibration v2 量於 commit `b1512ae`；之後 0.9.0–0.14.0 改過 provenance（例如 0.13.0 的 VCP-044 在每次 replay 前比對 root）。成本模型可能因此略有偏差。six-method v2 與 held-out v2 量的都是 0.15.0 的程式碼，所以偏差若影響選擇，會在 §7 的數字裡如實出現。環境指紋已核對與 policy v1 相同（`f29fc1bb…`：PostgreSQL 17、Python 3.12.14、16 核）。
- **A 在 1K 可能仍然偏寬**：如果 1K 的相對雜訊本來就大，`r` 也大，v2 在 1K 仍可能偏向 FULL；那時 H1a 不成立，照實報告。
- **B 的分層規則是固定的**：以 10 為底的數量級是一條分組規則；介於兩層邊界附近的圖，可能被分到雜訊特性不同的層；超出 calibration 範圍的規模只能用最近層外推。
- **同一份 calibration 擬合兩個 policy**：v1 與 v2 的成本模型相同，只差帶；這讓比較乾淨，但兩者共享同一個成本模型偏差。
- **held-out v2 只有一組新 seeds**：結論的外推範圍是同一個 workload 產生器、同一台機器。
- **真實資料只覆蓋變更比例的兩端**（裁決 7-2 照舊）。

## 13. 執行期修訂

- 候選帶比較（2026-10-10，腳本 commit `1823807`，scikit-learn 1.9.0）：`docs/benchmarks/postgres-provenance-band-comparison-v1.json`，SHA-256 `ade06945cc2411f950d9a4f25346b934fd1dd86af151ed934f4b0ce3a610b16c`。
- 分數（§5.3，越小越好）：A `relative` = 1.584、B `stratified_edges` = 1.011；相差 0.573，不在 0.1 的平手範圍內。勝者 **`stratified_edges`**，`fit_policy_v2(..., band="stratified_edges")`。
- 決定分數的那一格：A 最差的是 100K 層的 full（s = 0.205，帶寬約為實際誤差的 5 倍，太寬）；B 最差的是 100K 層的 incremental（s = 2.749，帶寬只有實際誤差的約 0.36 倍，太窄）。
- 全資料的帶：
  - A：`r_inc` = 0.227、`r_full` = 0.307；
  - B（incremental / full，毫秒）：decade 3 = 90.9 / 465.3（28 個觀測）、decade 4 = 1,168.0 / 4,418.3（32 個）、decade 5 = 3,695.5 / 7,336.4（32 個）；
  - v1（不分層）：2,286.3 / 5,057.4。
- §5.4 只報告的數字：
  - 在 calibration 的 92 個觀測上，A 與 B 都全部選 INCREMENTAL（1K 層 28/28）；選了較慢一方 0 次、多花 0 ms。
  - 留一層外推（只報告）：A 在兩個方向都比 B 穩。往較大規模外推（用 decade 3、4 擬合，檢驗 decade 5），B 嚴重偏窄（incremental s = 83.3、full s = 141.8，|ln s| = 4.42 / 4.95），A 也偏窄但較輕（7.6 / 12.8，|ln s| = 2.03 / 2.55）；往較小規模外推（不含 decade 3），B 偏寬約 10 倍（s = 0.077 / 0.105，|ln s| = 2.56 / 2.25），A 接近 1（0.68 / 1.42，|ln s| = 0.39 / 0.35）。超過 calibration 範圍（約 200K edges 以上）的圖，B 用 decade 5 的帶，可能偏向 INCREMENTAL；小於 calibration 範圍的圖，B 用 decade 3 的帶，偏向 FULL。兩者都只影響速度，不影響正確性（FULL 與 INCREMENTAL 的結果逐位元相同）。
- 更正 §4.3 的措辭：「截距 0」描述的是 v1 擬合出的結果。擬合器（v1 與 v2 共用）是非負截距的線性迴歸，這一版不改；v2 的成本模型仍與 v1 相同。
- 更正 §9：0.14.0 讀到 v2 的 `policy.json` 會報 `mismatch: provenance policy payload`（它把每個 `policy.json` 都當 v1 嚴格驗證），不是 `incompatible_policy`。
