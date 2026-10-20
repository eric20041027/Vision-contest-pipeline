# PostgreSQL Adaptive Provenance：課程簡報素材完整版（v2，2026-10-20）

> 給 Data_Base 專案裡負責做簡報的 agent。這份文件接在 v1 brief（`postgres-provenance-course-brief-v1.md`）之後，
> 集中 policy v2 這條線（2026-10-09 → 2026-10-20）的全部資訊：v1 的問題、v2 的設計與預先登記的比較、三段量測的
> 完整數字、v1 與 v2 的對照、發現與解讀、執行紀錄與教訓、限制、建議的簡報大綱、術語、檔案清單。所有數字都從
> `docs/benchmarks/` 的 machine-readable 結果回讀，來源檔在 §10；沒量到的事寫「未量」。v1 brief 的背景、系統設計
> （schema、交易、發布）與 v1 的五份證據仍然有效，這裡不重複。

課程：CS 5151/6051 Database Theory 小組專案（Group 1）。時程：progress report 2026-10-25（數字 10/22 凍結）、
slides 2026-11-15、15 分鐘報告 11/18–25、final report 2026-12-06。專案母體：`vcp`（開源，MIT，GitHub
`eric20041027/Vision-contest-pipeline`）；policy v2 隨 0.15.0 發布（2026-10-10）。

---

## 0. 三十秒版本

- **v1 的問題**：adaptive selector 只有在「預估 full − 預估 incremental」大於固定的 7.3 秒時才選 incremental。7.3 秒由
  100K 規模的誤差決定，跟這次的圖多大無關，所以 1K 的圖（預估差距約 0.84 秒）全部選 full，慢 1.8–4.3 倍；真實資料
  r4→r5 也選錯（慢 1.79 倍）。v1 仍然通過正式 gate，因為 pooled 分位數看不到 1K。
- **v2 的做法**：信心帶按圖的規模分層（⌊log10 total_edges⌋ 每層各自的 RMSE）。1K 層的帶寬是 0.56 秒，100K 層是
  11.0 秒。成本模型完全沿用 v1，只換帶。用哪一種帶，是由一個**事先 commit** 的交叉驗證腳本只用 calibration 資料選的
  （分層帶 1.011 勝過相對帶 1.584）。
- **正式驗收（held-out v2，從沒用過的 seeds，v1 與 v2 同場）**：v2 通過 gate（p50 1.016、p95 0.958）；
  超過 1.05 倍的場景 v2 18/92、v1 37/92（**H1 成立**）；只看 1K，v2 13/28、v1 28/28（**H1a 成立**）。v2 在 1K
  全部選 incremental，剩下的「超標」是同一策略兩次量測之間的雜訊（最多 1.25 倍）。
- **真實資料**：r4→r5 v2 改選 incremental（1.007 倍），v1 仍選 full（1.72 倍）；r3→r4 兩版都選 full（1.066 倍）。
- **預覽（six-method v2）**：648 列全 ok；超標 18/92（同一算法下 v1 為 31/92）。所有 parity 全部一致。

---

## 1. 背景：v1 的問題（量到的事實）

| 情況 | 預估差距（full − incremental） | v1 的選擇 | 實測 |
|---|---|---|---|
| 合成 1K，28 個非零場景 | 約 0.84 秒 | 全部 FULL | held-out v1 上比 incremental 慢 1.82–4.26 倍 |
| 合成 10K／100K，64 個非零場景 | 8.4 秒以上 | 全部 INCREMENTAL | 正確 |
| 真實 RSNA r4→r5（8.8K entities、98.7% 變更） | 5.7 秒 | FULL | 慢 1.79 倍 |

v1 的規則：`預估 incremental + 2,286 ms < 預估 full − 5,057 ms` 才選 incremental。2,286 與 5,057 是兩個成本模型在整份
calibration 上的 RMSE，加起來 7,343.7 ms。這是效能問題、不是正確性問題：FULL 與 INCREMENTAL 的結果逐位元相同，
所有證據的 parity 都是 true。問題的本質是「預估差距相對於這個規模的誤差」，不是「圖小」：r4→r5 有 8.8K entities
也中（followups 裁決 7-1）。

---

## 2. v2 的設計

### 2.1 不動的東西

- 成本模型（非負線性迴歸、同一份 calibration v2、同樣的 feature）：`incremental_ms ≈ 1.162·changed_samples + 0.118·total_edges`、
  `full_ms ≈ 0.704·total_edges + 0.097·historical_changes`。v2 的係數與 v1 逐項相同（相對誤差 ≤ 1e-9）。
- PostgreSQL DDL、reason code、benchmark schema 版本都不變；v1 與 v2 以 `policy_version`（`postgres-adaptive-v1` /
  `postgres-adaptive-v2`）分辨。policy v1 凍結，五份 v1 證據一個位元都沒動；三份已發布證據裡 v1 的全部決策用凍結的
  v1 重算後完全相同（回歸測試）。
- CLI 不加選項：`vcp provenance ingest --backend postgresql --strategy auto --policy postgres-adaptive-v2-9f4e58346529`。

### 2.2 兩個候選帶

- **A：相對帶**：帶寬 = 預估值 × 相對 RMSE；`est_inc·(1 + r_inc) < est_full·(1 − r_full)` 才選 INCREMENTAL。
  全資料 `r_inc` 0.227、`r_full` 0.307。
- **B：按規模分層的絕對 RMSE（`stratified_edges`）**：層 = ⌊log10 total_edges⌋；每層各自的 incremental 與 full RMSE；
  `est_inc + rmse_inc[層] < est_full − rmse_full[層]` 才選 INCREMENTAL；不在已知層就用最近層（距離相同取較大的層）。

| 層 | 觀測 | incremental RMSE | full RMSE | 帶寬合計 |
|---|---|---|---|---|
| decade 3（1K） | 28 | 90.9 ms | 465.3 ms | 556.2 ms |
| decade 4（10K） | 32 | 1,168.0 ms | 4,418.3 ms | 5,586.3 ms |
| decade 5（100K） | 32 | 3,695.5 ms | 7,336.4 ms | 11,031.9 ms |
| v1（不分層） | 92 | 2,286.3 ms | 5,057.4 ms | 7,343.7 ms |

兩者都沒有寫死的門檻：帶寬全部由 calibration 推導（延續 v1「沒有 hard-coded dirty-ratio 門檻」的原則）。

### 2.3 怎麼選帶（預先登記，spec §5）

- **不能比「誰選錯比較少」**：calibration 的 92 個觀測裡 full 從來沒有比較快，「選錯最少」的就是最窄的帶，等於拿掉安全邊際。
  也不能在擬合用的資料上比，因為 B 的帶寬本來就是那一層的 RMSE。
- **判準**：以 seed 交叉驗證。用一個 seed 擬合成本模型與帶，在另一個 seed 上算標準化殘差 `z = (實際 − 預估) / 帶寬`，
  每個（方向、模型、層）組合算 `s = √mean(z²)`（帶寬剛好等於實際誤差時 `s = 1`）；分數 = 最大的 `|ln s|`，越小越好；
  相差小於 0.1 選較簡單的 A。
- **程序**：比較腳本與單元測試先 commit（`1823807`），之後才對資料執行；commit 的先後就是預先登記的紀錄。
- **結果**：A 1.584、B **1.011**，勝者 B。決定分數的格子：A 在 100K 的 full 太寬（`s` 0.205，帶寬約為實際誤差的 5 倍）；
  B 最差的是 100K 的 incremental（`s` 2.749，偏窄）。
- **只報告、不決定**：在 92 個 calibration 觀測上，A 與 B 都全部選 INCREMENTAL；留一層外推時 B 不穩（往大規模外推
  `s` 83.3 / 141.8 偏窄，往小規模 0.077 / 0.105 偏寬），A 較穩（7.6 / 12.8 與 0.68 / 1.42）。這是 B 的已知限制（§7）。

### 2.4 v1 與 v2 在 held-out 的實際判斷

| 規模 | total_edges | 預估差距 | v1 帶寬 → 選擇 | v2 層帶寬 → 選擇 |
|---|---|---|---|---|
| 1K | 1,336–1,982 | 842–845 ms | 7,343.7 ms → FULL | 556.2 ms → INCREMENTAL |
| 10K | 13,336–19,982 | 8,448–8,477 ms | 7,343.7 ms → INCREMENTAL | 5,586.3 ms → INCREMENTAL |
| 100K | 133,396–199,982 | 84,515–84,800 ms | 7,343.7 ms → INCREMENTAL | 11,031.9 ms → INCREMENTAL |

---

## 3. 實驗方法

### 3.1 三段量測（spec §6）

| 量測 | 目的 | seeds | 方法 | 規模 |
|---|---|---|---|---|
| held-out v2 | 正式驗收 | 20261101、20261102（從未用過） | `postgres_full`、`postgres_incremental`、`postgres_adaptive`（v2）、`postgres_adaptive_v1`（凍結的 v1），同一次執行 | 108 場景 × 4 = 432 列 |
| real RSNA v2 | sanity check | 真實資料 r3→r4→r5→r6 | 六方法 + `postgres_adaptive_v1` | 3 transition × 7 = 21 列 |
| six-method v2 | 預覽 | calibration 的 20260913、20260914 | 六方法（adaptive 用 v2） | 108 × 6 = 648 列 |

矩陣與 v1 相同：1K / 10K / 100K entities × 9 個變更比例 × chain / branched × 2 seeds = 108 場景；1K、10K 各 7 次重複、
100K 3 次；每次重複用全新的資料庫；每一列都對 canonical replay 做 exact parity。

### 3.2 預先登記的驗收條件（spec §7，在任何 v2 量測之前 commit）

- **正確性 gate**：每列 parity true；每個 adaptive 列用它的 policy 重算決策完全一致；`overlap_count` 0。
- **正式效能 gate（對 v2）**：每種方法把所有場景的原始樣本 pool 起來；adaptive p50 / 較佳固定方法 p50 ≤ 1.05、
  p95 / p95 ≤ 1.10。同一組數字也對 v1 計算並列，但不影響 v2 的 gate。
- **診斷（照報，不是 gate）**：每個場景的 adaptive p50 / 較佳固定方法 p50；超過 1.05 倍的場景數與最差倍率；依層拆開；決策模式。
- **H1**：非 NO_OP 場景中，v2 超過 1.05 倍的場景數少於 v1。**H1a**：只看 decade 3（1K）。
- **禁止事項**：看過 held-out v2 的任何結果後，不得調整 policy、帶、判準或條件後重跑；gate 失敗就照實報告。

### 3.3 執行環境

AMD Ryzen 7 9700X（8C/16T）、31.2 GiB RAM、NVMe；Windows 11 10.0.26200；Python 3.12.14；SQLite 3.53.1；PostgreSQL 17.11
（原生、127.0.0.1:55432）。held-out 與 six-method 從 v0.15.0（`660d015`）的 detached worktree 量；real 從 `cafeea2`
（只改了 real runner 的複製範圍，`src/vcp` 與 v0.15.0 相同）。repo 外的看管程式：可用 RAM、外來大程式、GPU 與 CPU
佔用、每日 Windows 維護時段都會讓它停下並丟棄進行中的場景，空出來再從 checkpoint 自動續跑；重開機後由排程工作接回。

---

## 4. 結果（完整數字）

### 4.1 Held-out v2（2026-10-17 07:53 → 10-18 19:31 UTC，commit `660d015`）

432 列全 ok、2,448 次計時量測、5,608 個 EXPLAIN 計畫；parity 432/432；216 個 adaptive 決策重算一致；`overlap_count` 0；
12 個切片都沒有交會點；`VERDICT status=OK h1=true h1a=true`。

**正式 gate（pool 全部 108 個場景的原始樣本；ms）**

| | pg_full | pg_incr | adaptive v2 | adaptive v1 |
|---|---|---|---|---|
| pooled p50 | 5,969 | 1,001 | 1,016 | 1,012 |
| pooled p95 | 115,148 | 36,140 | 34,609 | 36,027 |
| p50 比值（≤ 1.05） | | | **1.0157 PASS** | 1.0115（並列） |
| p95 比值（≤ 1.10） | | | **0.9576 PASS** | 0.9969（並列） |

**診斷與假設**

| 層 | v2 超過 1.05 | v2 最差 | v1 超過 1.05 | v1 最差 | v2 選擇 | v1 選擇 |
|---|---|---|---|---|---|---|
| 1K | 13/28 | 1.245 | **28/28** | **4.219** | INCREMENTAL 28 | FULL 28 |
| 10K | 5/32 | 1.086 | 8/32 | 1.119 | INCREMENTAL 32 | INCREMENTAL 32 |
| 100K | 0/32 | 1.032 | 1/32 | 1.489 | INCREMENTAL 32 | INCREMENTAL 32 |
| 合計 | **18/92** | | **37/92** | | | |

H1：18 < 37，**成立**。H1a：13 < 28，**成立**。各層另有 NO_OP 8 / 4 / 4 個（兩版相同）。

逐場景比值（最小–最大，括號中位數）：adaptive v2 / 較佳固定 1K 0.97–1.25（1.04）、10K 0.96–1.09（1.01）、100K 0.86–1.03（1.00）；
adaptive v1 1K 1.82–4.22（2.96）、10K 0.96–1.12（1.01）、100K 0.85–1.49（0.99）；pg_incr / pg_full 1K 24%–57%（35%）、
10K 16%–50%（24%）、100K 12%–43%（22%）。

**1K 的 maintenance p50（ms；每格 = chain/branched × 2 seeds 四個場景的中位數）**——v1 與 v2 差別全在這一層

| ratio | pg_full | pg_incr | adaptive v2 | adaptive v1 | v2 選擇 | v1 選擇 |
|---|---|---|---|---|---|---|
| 0 | 117 | 123 | 130 | 132 | NO_OP | NO_OP |
| 0.001 | 135 | 132 | 153 | 149 | NO_OP | NO_OP |
| 0.01 | 597 | 155 | 162 | 635 | INCREMENTAL | FULL |
| 0.05 | 605 | 175 | 178 | 628 | INCREMENTAL | FULL |
| 0.1 | 652 | 188 | 195 | 641 | INCREMENTAL | FULL |
| 0.25 | 676 | 239 | 253 | 705 | INCREMENTAL | FULL |
| 0.5 | 687 | 278 | 292 | 713 | INCREMENTAL | FULL |
| 0.9 | 774 | 388 | 402 | 806 | INCREMENTAL | FULL |
| 1 | 842 | 428 | 455 | 809 | INCREMENTAL | FULL |

10K 與 100K 的完整表格在 `postgres-provenance-v2.md`：兩版在那兩層每格都選 INCREMENTAL，adaptive 與 pg_incr 的差在重複雜訊內
（例如 100K ratio 1：pg_full 143,381、pg_incr 54,272、v2 52,197、v1 53,984 ms）。

### 4.2 Real RSNA v2（2026-10-18 19:37 → 20:10 UTC，commit `cafeea2`，33 分鐘）

canonical 圖 13,421 entities / 18,133 edges / 8,752 changes / 0 gaps；104 個 run（RSNA 9/20 之後又加了約 90 個 run，所以
不是 real v1 那張 7 個 run、13,196 entities 的圖）；SQLite 對 canonical `graph_parity` / `status_parity` true、`verify_index` ok；
21 列全 ok、parity 21/21；153 個 EXPLAIN 計畫。

| transition | 變更 | 預估 inc / full（差） | 帶寬 v1 / v2 | v1 | v2 | pg_full | pg_incr | v1 實測 | v2 實測 |
|---|---|---|---|---|---|---|---|---|---|
| r3→r4 | 4,407（100%），edges 9,437 | 6,238 / 6,641（403） | 7,343.7 / 556.2 | FULL | FULL | 7,221 | 6,788 | 7,110（1.05×） | 7,234（1.066×） |
| r4→r5 | 4,345（98.7%），edges 18,130 | 7,194 / 13,185（5,991） | 7,343.7 / 5,586.3 | FULL | **INCREMENTAL** | 12,109 | 6,888 | 11,824（**1.72×**） | 6,936（**1.007×**） |
| r5→r6 | 0 | — | — | NO_OP | NO_OP | 1,943 | 2,042 | 1,884 | 1,943 |

（毫秒；實測欄的括號 = 對 pg_incr 的倍率。）各方法的 p95、status / impact、storage 與吞吐見 `postgres-provenance-v2.md`。
r4→r5 是 v2 的設計動機，所以只能說明「v2 在這個已知案例上的選擇」，不是 held-out 證據。

### 4.3 Six-method v2（2026-10-18 20:13 → 10-20 17:17 UTC，commit `660d015`）

648 列全 ok、3,672 次計時量測、4,208 個 EXPLAIN 計畫、parity 648/648；PostgreSQL 列通過 held-out 的列驗證、每個 adaptive
決策以 policy v2 重算一致；12 個切片沒有交會點。seeds 與 calibration 相同，所以是預覽。

| 用 held-out 的算法 | six-method v2（policy v2） | six-method v1（policy v1，2026-09-18） |
|---|---|---|
| pooled p50 ms（pg_full / pg_incr / adaptive） | 5,989 / 1,008 / 1,036 | 6,286 / 1,053 / 1,072 |
| p50 比值 | 1.0277 | 1.0180 |
| p95 比值 | 1.0452 | 0.9581 |
| 超過 1.05 / 最差 | **18/92**，1.163 | **31/92**，3.973 |
| 1K | 13/28，INCREMENTAL 28 | 28/28，FULL 28 |
| 10K | 2/32，最差 1.065 | 1/32，最差 1.065 |
| 100K | 3/32，最差 1.118 | 2/32，最差 1.140 |

（v1 brief 寫的預覽數字 1.010 / 0.932 是只 pool 非 NO_OP 場景、用另一種 p95 算法，不能和這張表直接比。）100K 兩次的
選擇完全相同，p95 比值的差來自相隔一個月的兩次量測，不是 policy。六種方法逐格的完整表格在 `postgres-provenance-v2.md`。

---

## 5. 發現與解讀（每條都附「簡報上可以怎麼講」）

### 5-1 v2 修好了 1K：預估差距要跟「這個規模的誤差」比

v1 拿 0.84 秒的預估差距去比 7.3 秒的全域誤差，必然選 full；v2 拿它去比 1K 層自己的 0.56 秒，選 incremental。held-out 上
1K 的超標場景從 28/28 降到 13/28，最差倍率從 4.22 降到 1.25；剩下的 13 個與較佳固定方法用的是同一個策略，是雜訊。
簡報講法：「信心帶要跟著規模走：同一個 0.8 秒的差距，對 100K 是雜訊，對 1K 是 3 倍的速度。」

### 5-2 正式 gate 兩版都過：aggregate 分不出 v1 和 v2，診斷才分得出

v2 1.016 / 0.958、v1 1.012 / 0.997，都通過。pooled 的 p50 落在 10K、p95 落在 100K，1K 的樣本拉不動。v1 與 v2 的差別只在
逐場景診斷與 H1 / H1a 看得到——這也是 spec 預先要求兩種口徑都報的原因。簡報講法：「只看 gate 會以為 v1 沒問題；
所以我們事先登記了第二個問題（H1）。」

### 5-3 真實資料：r4→r5 修好，r3→r4 仍選 full

r4→r5 的預估差距 5,991 ms > 10K 層帶寬 5,586 ms → INCREMENTAL（1.007 倍；v1 1.72 倍）。r3→r4 差距 403 ms < 1K 層帶寬
556 ms → FULL（1.066 倍）：兩個預估只差 6%，而且 full 的預估低估了實測（6,641 對 7,221），保守選 full 是規則的設計結果。
簡報講法：用這兩個 transition 的長條圖，誠實地放一個修好、一個沒修好的案例。

### 5-4 預先登記讓「選帶」與「驗收」都不能事後調

帶的選擇、新 seeds、驗收條件、H1 / H1a 都在任何 v2 量測之前 commit；比較腳本先 commit 再執行。量測期間遇到的問題
（§6）只用事先寫好的規則或「與數值無關」的條件（失敗狀態、時段）處理，沒有任何調參後重跑。簡報講法：用一張時間軸
（commit → 執行 → 量測 → 結果）講 pre-registration。

### 5-5 B 勝出，但它的外推有限制

交叉驗證裡 A 在 100K 的 full 太寬（s 0.205），所以 B 勝；但留一層外推時 B 不穩：比 calibration 大的圖（約 200K edges 以上）
用 100K 層的帶，可能偏向 INCREMENTAL；比 1K 小的圖（約 1.3K edges 以下）用 1K 層的帶，偏寬約 10 倍、可能偏向 FULL。
只影響速度，不影響正確性。簡報講法：「分層帶在量過的範圍內最準，範圍外要再量。」

### 5-6 仍然沒有交會點

held-out v2、six-method v2 各 12/12 切片 `not_observed`，與 v1 的三份證據相同：incremental 在每個非零變更比例都比 full 快
（100K 為 12%–43%）。adaptive 的實際答案仍是「幾乎永遠 incremental」；full 的理由是修復與換代，不是效能。

---

## 6. 執行紀錄與教訓（可做「lessons learned」一頁）

| 日期（UTC） | 事件 | 處置／教訓 |
|---|---|---|
| 10-09 | 交接；spec 定案（沿用 calibration v2、不加 VACUUM、A/B 兩個候選、新 seeds） | 決定都寫進 spec |
| 10-10 | 比較腳本先 commit（`1823807`）再執行；B 勝；policy v2 發布；0.15.0 | 預先登記 |
| 10-15 | 為了趕 10/22 的進度報告，量測提前開跑；主機可用記憶體只剩 0.56 GiB（核心 nonpaged pool 洩漏） | 重開機後才開跑（裁決 8） |
| 10-17 | real RSNA 冒煙測試在量測前失敗：RSNA 新增約 90 個 run 帶存取收據，real runner 沒複製這些 artifact | 先跑 held-out；修 runner 後 real 從新 commit 量（裁決 9） |
| 10-17 | 加自動復原：排程工作每 15 分鐘檢查，重開機或當機後從 checkpoint 接回；實際砍掉一次驗證 | 長量測不能靠人盯 |
| 10-18 07:15 | Windows 自動維護與 Defender 更新讓 WMI 變慢；每個場景的子程序查一次 CPU 資訊，逾時就靜默改用備用值，那個場景的每筆紀錄都被環境檢查拒寫（fail closed） | 不寫錯資料，但 runner 把錯誤細節藏起來，要另外重跑才看得到原因；維護時段完成的 4 個場景依「時段」重量；每日 06:55–08:45 不量（裁決 10） |
| 10-18 | 外部程式（遊戲）在記憶體門檻以下就會搶 CPU / GPU | 看管程式加 GPU 3D 佔用條件 |
| 10-18 19:31 | held-out v2 發布：gate PASS、H1 / H1a 成立；20:10 real v2 發布 | |
| 10-20 00:03 | notebook kernel 用掉約 14 核，PostgreSQL 新連線要 55 秒，連續逾時 | 看管程式加 CPU 佔用條件；當時沒有寫入任何紀錄 |
| 10-20 16:43 | six-method v2 有 2 列各一次重複失敗；依「失敗」狀態重量這兩個場景，17:17 發布 648/648（裁決 12） | 第一版也保存 |

通用教訓：(1) 環境檢查不能「查不到就改用預設值」：這次幸好被下游的 fail closed 擋住；(2) 「機器忙」不只看記憶體，
CPU、GPU、作業系統的維護時段都會干擾計時；(3) 真實資料會長大，real runner 的複製範圍要跟著資料模型走；
(4) 重量的選擇要與數值無關（時段、失敗狀態），並把原檔與重量後的檔都留下；(5) 錯誤訊息去敏時至少要留下例外類別。

---

## 7. 限制與誠實聲明

1. **單一主機、單一 workload 產生器、一組新 seeds**：結論的外推範圍是這台機器與這個合成矩陣；1M 仍未量。
2. **真實資料只有變更比例的兩端**（100% / 98.7% / 0%），而且只是 sanity check；r4→r5 是 v2 的設計動機。
3. **B 的外推**（§5-5）：calibration 範圍外的圖只能用最近層。
4. **成本模型沿用 calibration v2**（量於 commit `b1512ae`）：v1 與 v2 共享同一個成本模型偏差；held-out v2 量的是 0.15.0 的程式碼。
5. **pooled gate 不敏感**：1K 的改善在 gate 上看不到，要靠診斷與 H1 / H1a。
6. **跨次比較的絕對毫秒**受主機狀態影響（six-method v2 與 v1 相隔一個月）；同一次執行內的比值才可直接比。
7. **量測中的重量**：held-out 4 個場景（維護時段）、six-method 2 個場景（單次重複失敗），選擇都與數值無關，原檔保存。
8. **runner 的兩個缺陷已在量測後修正**（只改 `tests/performance/provenance/`，不影響已發布的數字）：CPU 資訊查詢逾時改成重試、仍失敗就明確報錯，不再靜默改值；錯誤訊息會留下例外類別與安全的原因字彙。

未來工作：外推範圍外的規模（1M、極小圖）重量；多 writer 併發吞吐；查詢計畫的 EXPLAIN。

---

## 8. 建議的簡報大綱（policy v2 部分，約 10 頁）

1. 回顧：adaptive selector 與 v1 的兩句話結論（通過 gate、1K 全選錯）（§1）
2. 問題的本質：固定 7.3 秒的信心帶 vs 0.84 秒的預估差距（§1，可畫數線）
3. 兩個候選帶：相對帶 vs 分層帶（§2.2，帶寬表）
4. 怎麼公平地選：為什麼不能比「選錯幾次」；seed 交叉驗證；`|ln s|` 分數（§2.3）
5. 預先登記的時間軸：腳本 commit → 執行 → policy → 驗收條件 → 量測（§5-4）
6. held-out v2：gate 表 + 診斷表並列（§4.1、§5-2）
7. 1K 的 v1 / v2 對照：maintenance p50 折線（§4.1 的 1K 表）
8. 真實資料：r3→r4、r4→r5 的長條圖（§4.2、§5-3）
9. 預覽與一致性：six-method v2 vs v1（§4.3）、仍無交會點（§5-6）
10. 限制、教訓、未來工作（§6、§7）

圖表建議：1K 表畫折線（x = ratio，y = p50，四條線：pg_full、pg_incr、v2、v1）；診斷表畫分層長條（v1 vs v2 的超標數）；
real 用 r4→r5 四根長條（pg_full、pg_incr、v1、v2）；帶寬用數線（1K 的 0.84 秒差距落在 v1 的 7.3 秒帶內、落在 v2 的 0.56 秒帶外）。

---

## 9. 術語表（v1 brief 之外新增的）

| 詞 | 意思 |
|---|---|
| 信心帶（band） | selector 要求「incremental 便宜得夠有把握」的邊際；v1 是固定的 7.3 秒，v2 依規模分層 |
| decade / 層 | ⌊log10 total_edges⌋；1K、10K、100K 的圖分別落在 decade 3、4、5 |
| relative / stratified_edges | 兩個候選帶：A 相對帶、B 分層的絕對 RMSE（勝者） |
| 標準化殘差均方根 `s` | 檢驗資料上 `(實際 − 預估) / 帶寬` 的均方根；`s = 1` 代表帶寬剛好等於實際誤差 |
| 預先登記（pre-registration） | 判準、seeds、驗收條件、假設在看到資料之前 commit；之後不得修改 |
| held-out v2 | 用從未用過的 seeds（20261101、20261102）、v1 與 v2 同場的正式驗收 |
| H1 / H1a | v2 超過 1.05 倍的場景數少於 v1（全部 / 只看 1K） |
| fail closed（環境檢查） | 紀錄的環境身分與契約不符就拒寫，寧可失敗也不留下可疑的數字 |

---

## 10. 檔案清單（repo `docs/benchmarks/`，完整 SHA-256）

| 檔案 | 內容 | SHA-256 | 量測 commit |
|---|---|---|---|
| `postgres-provenance-band-comparison-v1.json` | 候選帶比較：分數、每格 `s`、只報告的數字 | `ade06945cc2411f950d9a4f25346b934fd1dd86af151ed934f4b0ce3a610b16c` | 腳本 `1823807` |
| `postgres-provenance-policy-v2.json` + `-artifacts/` | policy v2 artifact | `81ec2eba938765209ae1cbf461f0ddad8a07e4debee34db627ee036327f0f770`；`policy.json` `6fe654a90468edb57aa84fdec7ac2e53cf18b20106f7a77d65df8ac7cd27c000` | — |
| `postgres-provenance-calibration-v2.json` + `-artifacts/` | calibration 與凍結的 policy v1 | `43d213e4ddc57cb554ccc55bf153b3f7ec7ca7e34364568300ef1cfdf88f9239`；v1 `policy.json` `a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78` | `b1512ae` |
| `postgres-provenance-heldout-v2.json` | 432 列、evaluation（v1 與 v2） | `e4cca33d04ea2b981a031afbe4b6a8edf64acfbe12e837f4a621594e2cc85a48` | `660d015` |
| `postgres-provenance-real-rsna-v2.json` | 21 列（repo 版，兩個來源路徑改成相對路徑） | `bb29448f60db052c1d51e180437c356b09824b7b923a3eb3ea01a97e8e1ad75f`（原檔 `e9eac27f34c667f37596e99feefb8bc3ac8fc99d9f57ff79893e37508d529d4d`） | `cafeea2` |
| `postgres-provenance-six-method-v2.json` | 648 列（預覽） | `5bbee9fad2d20b609ccd7e67a01951096cbedd37ef1e202f00d8d73d01ac5a63`（第一次發布 `d5adce06877e27f2dd9336867f5cb58b02320c461927462668707a4b5b151682`） | `660d015` |
| `postgres-provenance-v2.md` | v2 證據紀錄：全部表格與 acceptance record | — | — |
| `../superpowers/specs/2026-10-09-vcp-postgres-adaptive-policy-v2-design.md` | 設計 spec（§13 執行期修訂） | — | — |
| `../superpowers/plans/2026-10-09-vcp-postgres-adaptive-policy-v2-followups.md` | 裁決 1–12 與開放待辦 | — | — |

PR：#39 policy v2、#40 0.15.0、#41 提前開跑、#42 real runner 修正、#43 WMI 事件、#44 held-out 與 real 證據、#45 six-method 證據。

---

## 11. 機器可讀摘要

```json
{
  "policy_v2": {"id": "postgres-adaptive-v2-9f4e58346529", "policy_sha256": "6fe654a90468edb57aa84fdec7ac2e53cf18b20106f7a77d65df8ac7cd27c000", "band": "stratified_edges", "strata_ms": {"3": {"incremental": 90.9, "full": 465.3}, "4": {"incremental": 1168.0, "full": 4418.3}, "5": {"incremental": 3695.5, "full": 7336.4}}, "cost_models": "identical_to_v1", "release": "0.15.0"},
  "policy_v1": {"id": "postgres-adaptive-v1-9f4e58346529", "policy_sha256": "a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78", "band_ms": {"incremental": 2286.3, "full": 5057.4}, "status": "frozen"},
  "band_comparison": {"script_commit": "1823807", "scores": {"relative": 1.584, "stratified_edges": 1.011}, "tie": 0.1, "winner": "stratified_edges", "sha256": "ade06945cc2411f950d9a4f25346b934fd1dd86af151ed934f4b0ce3a610b16c"},
  "heldout_v2": {"seeds": [20261101, 20261102], "rows": 432, "timed_samples": 2448, "parity": "432/432", "decisions_rechecked": "216/216", "overlap_count": 0, "aggregate_ms": {"postgres_full": {"p50": 5969.0, "p95": 115148.1}, "postgres_incremental": {"p50": 1000.8, "p95": 36139.8}, "postgres_adaptive": {"p50": 1016.5, "p95": 34608.9}, "postgres_adaptive_v1": {"p50": 1012.2, "p95": 36027.2}}, "v2": {"p50_ratio": 1.0157, "p95_ratio": 0.9576, "gate": "PASS", "over_1_05": "18/92", "by_decade_over": {"3": "13/28", "4": "5/32", "5": "0/32"}, "worst": 1.245}, "v1": {"p50_ratio": 1.0115, "p95_ratio": 0.9969, "over_1_05": "37/92", "by_decade_over": {"3": "28/28", "4": "8/32", "5": "1/32"}, "worst": 4.219}, "h1": true, "h1a": true, "crossover_slices_not_observed": "12/12", "sha256": "e4cca33d04ea2b981a031afbe4b6a8edf64acfbe12e837f4a621594e2cc85a48", "commit": "660d015"},
  "real_rsna_v2": {"rows": 21, "parity": "21/21", "graph": {"entities": 13421, "edges": 18133, "changes": 8752}, "runs": 104, "transitions": {"r3->r4": {"v1": "FULL", "v2": "FULL", "estimates_ms": [6238, 6641], "v2_over_incremental": 1.066}, "r4->r5": {"v1": "FULL", "v2": "INCREMENTAL", "estimates_ms": [7194, 13185], "v1_over_incremental": 1.72, "v2_over_incremental": 1.007}, "r5->r6": {"v1": "NO_OP", "v2": "NO_OP"}}, "sha256_repo": "bb29448f60db052c1d51e180437c356b09824b7b923a3eb3ea01a97e8e1ad75f", "sha256_original": "e9eac27f34c667f37596e99feefb8bc3ac8fc99d9f57ff79893e37508d529d4d", "commit": "cafeea2"},
  "six_method_v2": {"seeds": [20260913, 20260914], "rows": 648, "timed_samples": 3672, "parity": "648/648", "p50_ratio": 1.0277, "p95_ratio": 1.0452, "over_1_05": "18/92", "six_method_v1_same_math": {"p50_ratio": 1.018, "p95_ratio": 0.9581, "over_1_05": "31/92"}, "crossover_slices_not_observed": "12/12", "sha256": "5bbee9fad2d20b609ccd7e67a01951096cbedd37ef1e202f00d8d73d01ac5a63", "commit": "660d015"},
  "open_items": ["extrapolation_beyond_calibration_range", "one_million_scale", "multi_writer_throughput", "explain_for_query_plans"]
}
```
