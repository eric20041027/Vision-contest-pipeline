# PostgreSQL Adaptive Provenance：課程簡報素材 v3（E5 與 10/20 之後的更新，2026-11-02）

> 給 Data_Base 專案裡負責做簡報的 agent 與組員。這份接在 v1 brief（`postgres-provenance-course-brief-v1.md`）與
> v2 brief（`postgres-provenance-course-brief-v2.md`）之後，只放兩份 brief 沒有的東西：E5 打平點實驗的設計與完整數字、
> 量測後找到並修好的效能缺陷、10/20 之後 VCP 與發表有關的更新、組內 14 頁大綱逐頁要改什麼，以及第 11、12、14 頁與
> 備用頁的英文投影片文字。E5 的數字都從 `postgres-provenance-break-even-v1.json` 回讀；標「推算」的是從量到的數字
> 算出來的，不是量測；標「profile」的是單次 profile，不是證據。v1、v2 brief 的內容仍然有效，這裡不重複。

課程：CS 5151/6051 Database Theory 小組專案（Group 1）。時程：slides 2026-11-15、15 分鐘報告（加 2–3 分鐘問答）
11/18–25、final report 2026-12-06。專案母體：`vcp`（開源，MIT，GitHub `eric20041027/Vision-contest-pipeline`）。
框架在 11/13–12/6 凍結，發表期間不改程式；最後一個發版是 0.17.0（2026-11-02）。

---

## 0. 三十秒版本

- **E5 問的事**：合成資料和真實資料從來沒有越過成本模型的打平點（約「每兩條邊一個變更」），所以 v2 在 held-out 全部選
  INCREMENTAL，最多只能和固定 INCREMENTAL 打平。E5 刻意做出越過打平點的資料，問三件事：FULL 真的會比較快嗎？v2 會改選
  FULL 嗎？選得對嗎？
- **事先登記**：28 個場景（1K、10K × 7 個密度級 × 2 seeds）、三個假設、v1 與 v2 的每一個選擇，都在量測前 commit。
- **結果**（`VERDICT status=WARN h5a=holds h5b=fails h5c=holds`）：
  - 112 列與重放逐項一致；v1、v2 的 28 個選擇和預測檔完全相同。
  - **H5a 成立**：實測交會點在密度 0.40 與 0.50 之間，模型預測 0.504；越過以後 FULL 快 9–23%。
  - **H5b 不成立**：v2 從 0.30 就改選 FULL，太早了；10K 在 0.30、0.40 慢 1.66–3.11 倍，p95 比值 1.628 > 1.10。
  - **H5c 成立**：交會點以上，v2 比固定 INCREMENTAL 快（pooled p50 4,979 對 5,856 ms，0.85 倍）。
- **量測後的發現**：10K 的 FULL 為什麼隨讀數變少而變快？是 VCP 的效能缺陷（每筆讀數掃整張圖）。已修（PR #56，隨 0.16.0
  發出），狀態完全相同；修好後 10K L1 的 FULL 從 33.1 秒降到 8.3 秒（profile）。E5 的數字與判定不改。
- **一個結構上的發現**：只有修改的 diff 密度一定小於 0.5（每筆修改加 2 條邊），所以凍結的模型對它永遠不會預估 FULL 比較
  便宜；要越過打平點只能靠新增或刪除。這也解釋了 RSNA r3→r4 為什麼卡在 0.5 附近、勝負會翻轉。

---

## 1. E5 的設計

### 1.1 問題與成本模型

- 成本模型（policy v1、v2 共用，截距 0；`E` = total_edges，`H` = historical_changes，`C` = changed_samples）：
  - incremental：`1.1624·C + 0.1182·E`
  - full：`0.7037·E + 0.0968·H`
  - 打平點：`C ≈ 0.5037·E + 0.0833·H`。E5 讓 `H = 0`，所以打平點是密度 `d* = C / E ≈ 0.504`。
- v2 的選擇規則：`預估 incremental + 帶寬_inc < 預估 full − 帶寬_full` 才選 INCREMENTAL，否則 FULL。帶寬依 `E` 的十進位層
  查表：1K 層（decade 3）556.2 ms，10K 層（decade 4）5,586.3 ms。
- E5 之前，held-out v2 的 92 個非零場景密度最多約 1/6（1K：`C` 326、`E` 1,982），v2 全部選 INCREMENTAL。唯一靠近打平點
  的是真實 RSNA r3→r4（`C` 4,407、`E` 約 8,873–9,437，密度約 0.47–0.50），而且兩次量測勝負翻轉：9 月 pg_full 5,625 ms
  比 pg_incr 6,056 ms 快，10 月 pg_incr 6,788 ms 比 pg_full 7,221 ms 快。

### 1.2 密度的結構上限

每一筆 MODIFIED 在圖裡加 2 條 `CONTAINS_CHANGE` 邊（變更前、變更後各一），ADDED 或 REMOVED 只加 1 條。所以只有修改的
diff `E ≥ 2·C`，密度一定小於 0.5；代進凍結的係數，`full − incremental ≥ 0.0086·C + 0.5855·(其他邊) + 0.0968·H > 0`。
**凍結的模型對只有修改的 diff 永遠不會預估 FULL 比較便宜**。要越過 0.5 只能靠 ADDED 或 REMOVED。這是 E5 的第一個發現，
在量測前就成立；held-out v2 的 432 列都符合 `E = 2(C + H) + 26`，216 個 adaptive 列從特徵重算的預估值誤差為 0。

### 1.3 Workload 與矩陣

| 項目 | 值 |
|---|---|
| family | `dense`：source / target 兩個 dataset 由 vcp 的 `create_dataset_diff` 產生 diff；`H = 0`，密度是唯一變動的特徵 |
| 填充 | 另一個無關的 dataset 掛 `F` 筆讀數（每筆 1 個 entity、1 條邊），補足邊數又不落進 incremental 的 dirty 範圍 |
| 規模 | 1K（`E ≈ 2,000`）、10K（`E ≈ 20,000`）；100K 沒有量（§6） |
| 密度級 | L1 0.17、L2 0.30、L3 0.40、L4 結構上限（1K 0.4985、10K 0.4999）：只有修改（A 段）；L5 0.60、L6 0.75、L7 約 1.0（1K 0.9970、10K 0.9997）：加入新增（B 段，`F = 0`） |
| seeds | 20261201、20261202（從沒用過；`overlap_count` 0）。兩個 seed 的圖形狀相同，是重複計時，不是獨立重現 |
| 方法 | `postgres_full`、`postgres_incremental`、`postgres_adaptive`（v2）、`postgres_adaptive_v1`（凍結的 v1），同一次執行 |
| 重複 | 每個場景 7 次，每次全新的資料庫；毫秒都是 p50 |
| 量測 | 2026-10-27 23:48 → 10-28 03:07 UTC，commit `86919f3`（`vcp 0.15.0+g86919f38…`），看管程式沒有暫停過 |

### 1.4 事先登記（spec §6，量測前 commit）

- **正確性 gate**：每列 parity true；每個 adaptive 決策用它的 policy 重算一致；`overlap_count` 0。
- **預測**：v1、v2 在 28 個場景的選擇，從凍結的 policy 算出並 commit（`postgres-provenance-break-even-v1-predictions.json`）。
- **H5a（交會點存在）**：至少一層、一個密度級，該級每個場景 `pg_full ≤ 0.95 × pg_incr`。實測交會點 = 每層「該級以上每一級
  都符合」的最低一級。
- **H5b（門檻）**：28 個場景的原始樣本 pool 起來，`v2 p50 / min(兩個固定方法的 p50) ≤ 1.05`、`p95 比值 ≤ 1.10`（與 E3 同一門檻與算法）。
- **H5c（自適應的價值）**：每層交會點（含）以上的場景 pool 起來，`v2 pooled p50 < pg_incr pooled p50`。
- **禁止事項**：第一個量測開始後，spec §6、預測檔、分析腳本都不能改；失敗照實報告。

---

## 2. E5 結果

### 2.1 判定

| 項目 | 判定 | 數字 |
|---|---|---|
| 正確性 gate | 通過 | 112 列（28 × 4）parity 全 true；adaptive 決策重算全一致；`overlap_count` 0 |
| 預測 | 全中 | v1、v2 的 28 個選擇都和預測檔相同（`prediction_agreement` 28/28） |
| H5a 交會點存在 | **成立** | 1K、10K 的實測交會點都是 L4；L4–L7 每個場景 `pg_full ≤ 0.95 × pg_incr` |
| H5b 門檻（v2） | **不成立** | p50 2,551 ÷ min(4,939, 2,659) = **0.959**（≤ 1.05）；p95 22,068 ÷ min(27,875, 13,554) = **1.628**（> 1.10） |
| H5b，v1 對照 | 不成立 | p50 0.955；p95 1.633 |
| H5c 自適應的價值 | **成立** | 交會點以上 16 個場景：v2 4,979 ms < pg_incr 5,856 ms |

28 個場景的 pooled 維護時間（ms）：

| 方法 | p50 | p95 |
|---|---|---|
| pg_full | 4,939 | 27,875 |
| pg_incr | 2,659 | 13,554 |
| v2 adaptive | 2,551 | 22,068 |
| v1 adaptive | 2,539 | 22,134 |

### 2.2 交會點與模型

| | 模型（事先算出） | 實測 |
|---|---|---|
| FULL 開始比較快的密度 | `d* ≈ 0.504`，兩層相同 | 兩層都在 L3 與 L4 之間（1K (0.40, 0.4985]；10K (0.40, 0.4999]）。A 段到不了更高，B 段第一級就是 0.60，解析度只到這裡 |
| v2 改選 FULL 的密度 | 約 0.26（L2 起） | L2 起，和預測相同 |
| v1 改選 FULL 的密度 | 1K 一律 FULL；10K 約 0.19（L2 起） | 和預測相同 |

- L4 以上每個場景 FULL 都快 9–23%（`pg_full ÷ pg_incr` 0.773–0.908）。
- v2 選 FULL 的原因：L2–L4 是「差距在帶寬內，不確定」，L5–L7 是「模型預估 FULL 較便宜」。L4 的模型其實預估 INCREMENTAL
  稍便宜（1K：1,395 對 1,407 ms），是帶寬讓 v2 選了 FULL，而 FULL 確實比較快。
- v1 和 v2 只有一處不同：1K 的 L1。v2 選 INCREMENTAL（1.00–1.06 倍），v1 選 FULL（2.08–2.15 倍）。這和 held-out v2 的
  1K 結論一致。
- 診斷（只報告）：兩個固定方法差距超過 5% 的 27 個場景，v2 選對 21 個、v1 選對 19 個；v2 選錯的 6 個全在 L2、L3。

### 2.3 F7 的資料（兩個 seed 的平均，推算自結果檔的逐場景 p50）

逐場景的值在 `postgres-provenance-break-even-v1.md`〈層 × 密度級〉；下表把同一級的兩個 seed 平均，給畫圖用。

| 層 | 級 | 密度 | pg_full | pg_incr | v2 | v1 | v2 選擇 | v1 選擇 |
|---|---|---|---|---|---|---|---|---|
| 1K | L1 | 0.17 | 872 | 428 | 440 | 905 | INCREMENTAL | FULL |
| 1K | L2 | 0.30 | 862 | 646 | 879 | 901 | FULL | FULL |
| 1K | L3 | 0.40 | 916 | 924 | 920 | 884 | FULL | FULL |
| 1K | L4 | 0.4985 | 845 | 977 | 866 | 860 | FULL | FULL |
| 1K | L5 | 0.60 | 854 | 1,098 | 895 | 934 | FULL | FULL |
| 1K | L6 | 0.75 | 904 | 1,127 | 934 | 917 | FULL | FULL |
| 1K | L7 | 0.9970 | 1,023 | 1,301 | 1,057 | 1,060 | FULL | FULL |
| 10K | L1 | 0.17 | 27,938 | 4,063 | 4,131 | 4,120 | INCREMENTAL | INCREMENTAL |
| 10K | L2 | 0.30 | 22,040 | 7,708 | 22,893 | 22,989 | FULL | FULL |
| 10K | L3 | 0.40 | 17,122 | 10,440 | 17,809 | 17,864 | FULL | FULL |
| 10K | L4 | 0.4999 | 9,014 | 10,538 | 9,025 | 9,274 | FULL | FULL |
| 10K | L5 | 0.60 | 9,755 | 11,081 | 9,762 | 9,837 | FULL | FULL |
| 10K | L6 | 0.75 | 10,578 | 12,099 | 10,590 | 10,709 | FULL | FULL |
| 10K | L7 | 0.9997 | 12,773 | 14,534 | 12,465 | 12,859 | FULL | FULL |

（毫秒。）圖的建議：兩個並排的面板（1K、10K，各自的 y 軸），x = 密度，三條線 pg_full、pg_incr、v2；在 0.504 畫一條虛線
（模型的打平點），把 L2–L4 塗成「不確定區」（v2 因帶寬選 FULL），v2 選 FULL 的點用實心標記。10K 面板的重點是 L2、L3：
v2 跟著 pg_full 走，比 pg_incr 慢 1.66–3.11 倍。

---

## 3. 量測後的發現與修正（2026-10-30）

結果檔與上面的判定都沒有改。這一節是事後的觀察；數字來自單次 profile，不是證據。

- **觀察**：10K 的 A 段 `E` 固定，但 FULL 從 L1 的 27.9 秒一路降到 L4 的 9.0 秒，跟著填充讀數（13,194 → 7,994 → 3,994 → 0 筆）
  變少；1K 同一段只差 27 毫秒。成本模型只看 `E`，看不到這件事（L1 低估 13.9 秒）。
- **原因**：`src/vcp/provenance/views.py` 計算狀態時，每一筆讀數都要掃過整張圖找它屬於哪個切分（`_plan_for`），成本是
  「讀數 × 圖的大小」。PostgreSQL 的 FULL 每次重建算 6 輪狀態，SQLite 的重建算 5 輪。
- **修法**：PR #56（合併 commit `6b72dc4`，隨 0.16.0 在 2026-11-01 發出）：每次狀態計算先建一次切分的查表，讀數與 run 直接
  查。兩個版本算出的狀態完全相同（parity 測試），輸出不變。

| 填充讀數（E5 的 10K） | 狀態計算（原程式） | 狀態計算（修好後） | FULL 總時間（原程式） | FULL 總時間（修好後） |
|---|---|---|---|---|
| 13,194（L1） | 25.4 s | 0.85 s | 33.1 s | 8.3 s |
| 3,994（L3） | 9.2 s | 0.87 s | 19.7 s | 10.5 s |
| 0（L4） | 1.15 s | 1.16 s | 10.1 s | 10.5 s |

（profile：seed 20261201、同一台機器、各一次。）

- **calibration 為什麼沒抓到**：既有 workload 每個場景只有約 14 筆讀數，這個成本看不出來。INCREMENTAL 不受影響，因為填充
  讀數不在變更的下游。
- **對 E5 的意義**：E5 量的是當時的程式，數字與判定都有效。10K 前幾級 FULL 的慢，有一部分來自這個缺陷；修好之後 FULL 很可能
  在 L3（0.40）附近就開始贏，「FULL 只看邊數」的模型假設也會更貼近。這是推測；要量化得重新事先登記、重新量測（§7 未來工作 1）。
- **policy 不受影響**：選擇只看成本模型的預估，修正不改預估，所以 v1、v2 在同一份資料上的選擇不變；改變的只有 FULL 的實際時間。

---

## 4. 發現與解讀（每條附英文講法）

### 4-1 打平點存在，而且就在模型說的地方

實測交會點在 0.40 與 0.50 之間，模型的 `d*` 是 0.504。成本模型雖然各項都有殘差（incremental 一律高估），兩條線的交會位置
是對的。英文：*"The break-even is real, and it is where the cost model said: between 0.40 and 0.50 changes per edge,
against a predicted 0.504."*

### 4-2 只改不加的 diff 永遠到不了打平點

每筆修改加 2 條邊，所以只有修改的 diff 密度 < 0.5（§1.2）。這說明了兩件事：合成資料（最多約 1/6）與 held-out 為什麼從來
沒有交會；真實的 r3→r4（約 0.47–0.50）為什麼剛好卡在打平點上、兩次量測勝負翻轉。英文：*"A diff that only modifies rows
can never pass one change per two edges — each modification adds two edges. That is why none of our earlier workloads
crossed, and why the real r3→r4 sits right on the line."*

### 4-3 v2 會改選 FULL，但改得太早

v2 從 0.30 起選 FULL：在 L2–L4，預估差距落在帶寬內，「不確定時選 FULL」。L4 選對了（FULL 快 13–16%），L2、L3 選錯
（10K 慢 1.66–3.11 倍），p95 門檻因此失敗。v1 也一樣（p95 1.633）。英文：*"v2 does switch to FULL past the break-even —
but it switches early. Inside its uncertainty band it falls back to FULL, which costs up to 3.1x at 10K, so the p95
threshold fails. We report that as measured."*

### 4-4 越過交會點以後，自適應比固定 INCREMENTAL 快

交會點以上 16 個場景，v2 pooled p50 4,979 ms、pg_incr 5,856 ms（0.85 倍）。這是第一份「v2 比固定 INCREMENTAL 快」的證據，
但範圍只在交會點以上；把 28 個場景全部算進去，v2 的 p95 是較佳固定方法的 1.63 倍。英文：*"Above the crossover, the
adaptive selector beats fixed INCREMENTAL by 15 percent. Across all 28 scenarios it does not: its p95 is 1.63x."*

### 4-5 事先登記讓一個失敗的假設也有價值

28 個選擇全部和事先 commit 的預測相同，H5b 失敗照實報告，沒有任何事後調整。失敗的位置（不確定區）直接指出下一步要改什麼
（§7 未來工作 2）。英文：*"Every one of the 28 choices matched the prediction we committed before measuring. The failed
hypothesis tells us exactly which rule to change next."*

### 4-6 量測找到一個真的缺陷

FULL 的時間跟著讀數數量變，不是成本模型假設的只看邊數。查下去是狀態計算每筆讀數掃整張圖；修好後 10K L1 的 FULL 從 33.1 秒
降到 8.3 秒（profile），狀態完全相同。calibration 的 workload 讀數太少，看不出來。英文：*"The experiment also found a real
bug: status computation scanned the whole graph for every reading. The fix shipped in 0.16.0; FULL at 10K went from
33 to 8 seconds in a profile, with identical results."*

---

## 5. 10/20 之後 VCP 的更新（與發表有關的）

| 版本／PR | 日期 | 內容 | 和發表的關係 |
|---|---|---|---|
| 0.15.1 | 10-24 | Kaggle CLI 在 JSON 前印警告行時，`submit sync` 不再整個失敗 | 只在被問到框架的穩定性時提 |
| PR #55 | 10-28 | E5 結果與證據進 repo | 第 11 頁 |
| PR #56 | 10-30 | 狀態計算先建切分查表（§3） | 第 11 頁的備用頁、第 14 頁的優點 |
| 0.16.0 | 11-01 | `vcp submit select --record`：記下平台上實際勾選的最終提交；和 `vcp submit final` 選出的不一致、或截止前還沒記錄時 WARN；訓練前檢查磁碟空間 | 備用頁「實戰教訓」：RSNA 最後兩發的選取沒有被記錄或核對，這件事已經修好 |
| 0.17.0 | 11-02 | `select --none`（記錄什麼都沒勾）、`<id>@<ref>`（重傳過的 id 指定某一發）；`vcp gc scan`：列出大檔案與哪一份紀錄引用它，只列不刪，每次掃描留一份稽核產物 | 備用頁「實戰教訓」；框架完整度（加分項） |

- 測試：0.16.0 2,549 passed / 85 skipped、覆蓋率 95.69%；0.17.0 2,958 passed / 90 skipped、覆蓋率 95.92%。
- **code drop 的建議**：用 tag `v0.17.0`（合併 commit `6f867fe`）。它是凍結前最後一個發版，CI 四個 check 全綠；
  這份 brief 等文件之後會在 `main` 上，連結 tag 時程式與證據都在。要不要在 11/14 另打 tag 由使用者決定（發版要使用者同意）。

---

## 6. 對照組內 14 頁大綱：哪些要改

組內的大綱（10/23 版）大部分仍然正確。下表只列有變動或需要注意的頁；沒列的頁照原樣。

| 頁 | 大綱寫的 | 現在 | 建議 |
|---|---|---|---|
| 一句話主軸第 3 點 | 「E5 則檢驗越過打平點後 v2 會不會改選 FULL」 | 有答案了 | 改成「v2 會改選 FULL，交會點以上選對、還比固定 INCREMENTAL 快 15%；但在不確定區改得太早，p95 門檻失敗」 |
| 4 | RSNA 112 個 run、38 次提交 | 數字對（RSNA 賽後報告，10/30 的總數） | 第 12 頁的圖是 10/18 量測時的 104 個 run；兩頁並列時註明日期，免得看起來矛盾 |
| 9 | 合成資料沒有交會點，最多每六條邊一個變更 | 正確 | 可以加一句「只有修改的 diff 永遠到不了 0.5」（§4-2），接到第 11 頁 |
| 11 | 11/12 拿到結果後填入 | 結果 10/28 就有了 | 用 §8.1 的文字與 §2.3 的 F7 資料 |
| 12 | r3→r6 三次改版 | 數字在 v2 brief §4.2，不變 | 加一句 r3→r4 的密度約 0.47–0.50，剛好在打平點上，兩次量測勝負翻轉（§1.1） |
| 14 | 限制與未來工作 | 有新的優點、限制與具體的下一步 | 用 §8.3 |
| 不要講的事第 3 點 | 不說 v2 比固定 INCREMENTAL 快，除非 E5 證明 | E5 只證明了交會點以上 | 可以說「交會點以上 v2 快 15%（H5c）」；不能說「v2 整體比固定 INCREMENTAL 快」（E5 全部場景的 p95 1.63 倍，held-out 是打平） |
| 備用頁「實戰教訓」 | RSNA 最後兩發的選取沒有被記錄或核對，已經排進工作 | 已經在 0.16.0、0.17.0 做完 | 改成過去式，見 §8.4 |

**大綱備用頁裡在 repo 文件找不到逐字出處的數字**（做投影片前請換成有出處的值，或在頁底附出處）：

| 大綱寫的 | repo 文件寫的 | 出處 |
|---|---|---|
| FULL 重建 75–160 s | 100K 一次 75–140 s（v1 證據）；held-out v2 100K ratio 1 的格子中位數 143 s | v1 brief §5-3；`postgres-provenance-v2.md` 的 100K 表 |
| 留下 1.6–2.2 倍的儲存空間 | storage 約 2 倍：pg_full relation 2.05–2.90 GiB 對 pg_incr 1.07–1.57 GiB（100K） | v1 brief §4.3 下方、§5-3 |
| PostgreSQL 增量維護比一次重放慢 1.2–4.4 倍 | 沒有找到這個比值；有的是 pg_full ÷ canonical 4.9–10.1 倍、pg_incr ÷ pg_full 13–57% | v1 brief §4.3 比值摘要 |
| v2 的 1K 額外開銷中位數多 12 ms | 沒有找到；held-out v2 1K 表逐格（v2 − pg_incr）是 3–27 ms | v2 brief §4.1 的 1K 表 |

「比 SQLite 慢 1.5–3.5 倍」有出處（v1 brief §4.3：pg_incr ÷ sqlite_incr 1.52–3.54 倍）。

---

## 7. 限制與未來工作（E5 之後）

**限制**

1. E5 只量了 1K、10K；100K 與 `H > 0`（有歷史 diff）的打平點沒有量。`H` 會讓 FULL 更貴、打平點更高。
2. E5 量的是修正前的程式（§3）；FULL 在 10K 前幾級的時間包含已修好的缺陷。
3. 兩個 seed 是同一種圖形狀的重複計時，不是獨立重現。
4. 單一主機、單一 PostgreSQL（17.11）、單一產生器，和 E3 相同；1M 仍未量。
5. 真實資料的變更比例只有兩端（0%、98.7%、100%），r3→r4 是唯一靠近打平點的真實案例。

**未來工作（具體）**

1. **用修好的程式重量 E5**：新的事先登記（新 id、新預測檔）。問：交會點會不會前移到 0.40 附近？L2、L3 的代價會不會變小？
   成本模型對 10K FULL 的殘差會不會縮小？
2. **事先登記一條「不確定區」的規則**：現在 v2 在帶寬內一律選 FULL。推算（不是量測）：如果 E5 的 L2–L4 改選
   INCREMENTAL，用同一場景量到的 pg_incr p50 代入，逐場景最差倍率從 3.11 倍降到 1.18 倍（L4 會慢 15–18%）。
   修好缺陷之後 FULL 變快，這條規則要和第 1 項一起重量才算數。
3. **100K 與 `H > 0` 的打平點**：新 seeds、新預測檔。
4. **impact／explain 改寫成 SQL（`WITH RECURSIVE`）**：目前在 Python 裡算，100K 的 impact p50 約 7.2 s，重放約 6.1 s，
   PostgreSQL 沒有讓它變快。
5. **多 writer 併發與 1M 規模**。

---

## 8. 英文投影片文字

每頁最多五點；講稿是建議，時間照大綱。頁底的出處用檔名就好。

### 8.1 第 11 頁：Result 3 — past the break-even (E5)

**Title:** Past the break-even: does the selector switch to FULL?

- Pre-registered: 28 scenarios (1K and 10K graphs, 7 densities, 2 seeds); every selector choice was predicted before
  measuring — 28/28 matched
- Correct: 112/112 rows identical to replay
- FULL does win past the break-even: measured crossover between 0.40 and 0.50 changes per edge; the model predicted 0.504
- v2 switches to FULL — but too early, inside its uncertainty band: up to 3.1x slower at 10K; p95 gate fails (1.63 > 1.10)
- Above the crossover, adaptive beats fixed INCREMENTAL: 4,979 vs 5,856 ms pooled p50 (0.85x)

**Figure:** F7 (§2.3).
**Source:** `postgres-provenance-break-even-v1.json`

**Speaker notes (about 60 s):** "Until now, no workload ever crossed the break-even, so v2 always chose INCREMENTAL. One
reason is structural: a diff that only modifies rows adds two edges per change, so it can never reach one change per two
edges. In E5 we added rows to push past it. FULL does become faster, almost exactly where the model said. v2 does switch
— but already at 0.30, where the estimates are within its error band, and there it pays up to three times. So one
hypothesis failed, and we report it. Above the crossover, the selector is 15 percent faster than always using
INCREMENTAL."

### 8.2 第 12 頁：Real data（在原本的內容上加一點）

- RSNA Knee r3→r6: three real revisions; every index row identical to replay (21/21)
- r4→r5: v2 chooses INCREMENTAL (1.007x of the best); v1 chose FULL (1.72x)
- r3→r4: about 0.47–0.50 changes per edge — right on the break-even; the faster method flipped between two measurement
  runs (Sep: FULL faster; Oct: INCREMENTAL faster)
- r5→r6: no change, both select NO_OP

**Source:** `postgres-provenance-real-rsna-v2.json`; graph at measurement time: 13,421 entities, 18,133 edges, 104 runs.

### 8.3 第 14 頁：Strengths, limits, future work

**Strengths**
- Exact: every published row matched replay (e.g. 432/432 held-out, 112/112 E5)
- Pre-registered: hypotheses and predictions committed before data; failures reported as measured
- Real data, and the measurements found and fixed a real performance bug

**Limits**
- One machine; E5 only at 1K and 10K with no history; 1M never measured
- Inside its uncertainty band, v2 falls back to FULL too early
- Real revisions changed either almost nothing or almost everything

**Future work**
1. Re-run E5 on the fixed code (new pre-registration)
2. Pre-register a rule for the uncertainty band — from E5's own numbers, worst case 3.1x → 1.2x (estimate, not measured)
3. Break-even at 100K and with history
4. Move impact / explain into SQL (`WITH RECURSIVE`)
5. Multiple writers; 1M scale

### 8.4 備用頁（問答用，不計時）

**B1. Why did FULL get faster as readings dropped?** 用 §3 的表。一句話：*"Status computation scanned the whole graph for
every reading; we fixed it in 0.16.0 with identical results. E5's numbers stay as measured."*

**B2. Why can't a modification reach 0.5?** 用 §1.2。畫一筆 MODIFIED（兩條邊）對一筆 ADDED（一條邊）。

**B3. Lesson from real use (only if asked).** *"In the competition, the two final picks ticked on the platform were not
recorded in VCP, so nothing compared them with VCP's recommendation. Since 0.16.0, `vcp submit select --record` records
them and warns when they differ; 0.17.0 adds recording an empty pick."* 不講名次與分數。

**B4. Code drop.** Repository `eric20041027/Vision-contest-pipeline`, tag `v0.17.0`; 2,958 tests passed, coverage 95.92%;
framework frozen during the presentations.

---

## 9. 術語（v1、v2 brief 之外新增的）

| 詞 | 意思 |
|---|---|
| 密度（density） | `C / E`：這次的變更列數除以套用 diff 之後的總邊數；打平點約 0.504 |
| 打平點（break-even，`d*`） | 兩個成本模型預估相等的密度 |
| 實測交會點（measured crossover） | 每層「該級以上每一級 FULL 都至少快 5%」的最低一級 |
| 不確定區（uncertainty band） | 預估差距小於帶寬的範圍；v2 在這裡選 FULL |
| A 段／B 段 | 只有修改（到不了 0.5）／加入新增（可以到接近 1） |
| 填充讀數（filler） | 補足邊數用的讀數，放在和變更無關的 dataset，不進 incremental 的 dirty 範圍 |
| H5a／H5b／H5c | E5 的三個事先登記假設：交會點存在／門檻／交會點以上自適應比固定 INCREMENTAL 快 |

---

## 10. 檔案清單（repo，完整 SHA-256）

| 檔案 | 內容 | SHA-256 | 量測 commit |
|---|---|---|---|
| `docs/benchmarks/postgres-provenance-break-even-v1.json` | E5 的 112 列與評估 | `c1e41cb8d6b49056dd214624e4b5c153c123dc3ead771f59f57b518eeef5d260` | `86919f3` |
| `docs/benchmarks/postgres-provenance-break-even-v1-predictions.json` | 事先登記的預測 | `e2f61c8d3185158e8cb84548128cc4f3699dd5d5fdb9a904e1d6b1007fe2164d` | — |
| `tests/performance/provenance/break_even_analysis.py` | 分析腳本 | `2d525286a54ac3573343cad0615ae3e1c8cdce7a04bbe23ffc8674a3f677af5f` | — |
| `docs/benchmarks/postgres-provenance-break-even-v1.md` | E5 的完整表格、診斷、殘差、量測後的發現 | — | — |
| `docs/superpowers/specs/2026-10-23-vcp-e5-break-even-design.md` | E5 設計 spec（§6 事先登記） | — | — |
| `docs/superpowers/plans/2026-10-23-vcp-e5-break-even-followups.md` | 裁決、過程、待辦 | — | — |
| `docs/postmortems/2026-10-rsna-knee-abnormality-detection.md` | RSNA 賽後報告（第 4 頁的規模數字） | — | — |

policy v2 `postgres-adaptive-v2-9f4e58346529`（`policy.json` `6fe654a90468edb57aa84fdec7ac2e53cf18b20106f7a77d65df8ac7cd27c000`）；
policy v1 `postgres-adaptive-v1-9f4e58346529`（`a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78`）。

PR：#50 E5 事先登記、#51 runner、#55 結果、#56 效能修正、#58／#61 0.16.0、#63／#64／#65 0.17.0。
tag：`v0.16.0`（`14bb0f0`）、`v0.17.0`（`6f867fe`）。

---

## 11. 機器可讀摘要

```json
{
  "e5": {"id": "postgres-provenance-break-even-v1", "commit": "86919f3", "sha256": "c1e41cb8d6b49056dd214624e4b5c153c123dc3ead771f59f57b518eeef5d260", "predictions_sha256": "e2f61c8d3185158e8cb84548128cc4f3699dd5d5fdb9a904e1d6b1007fe2164d", "scenarios": 28, "rows": 112, "parity": "112/112", "prediction_agreement": "28/28", "overlap_count": 0, "seeds": [20261201, 20261202], "decades": [3, 4], "model_break_even_density": 0.504, "measured_crossover": {"1K": "(0.40, 0.4985]", "10K": "(0.40, 0.4999]"}, "v2_switches_to_full_from_density": 0.30, "pooled_ms": {"postgres_full": {"p50": 4939, "p95": 27875}, "postgres_incremental": {"p50": 2659, "p95": 13554}, "postgres_adaptive_v2": {"p50": 2551, "p95": 22068}, "postgres_adaptive_v1": {"p50": 2539, "p95": 22134}}, "h5a": "holds", "h5b": {"v2": "fails", "p50_ratio": 0.959, "p95_ratio": 1.628, "v1_p50_ratio": 0.955, "v1_p95_ratio": 1.633}, "h5c": {"result": "holds", "scenarios": 16, "v2_p50_ms": 4979, "pg_incr_p50_ms": 5856}, "diagnostic_right_choices": {"v2": "21/27", "v1": "19/27"}, "verdict": "WARN"},
  "post_measurement_fix": {"pr": 56, "merge_commit": "6b72dc4", "release": "0.16.0", "status_identical": true, "profile_not_evidence": {"10K_L1_full_s": [33.1, 8.3], "10K_L3_full_s": [19.7, 10.5], "10K_L4_full_s": [10.1, 10.5]}},
  "releases_since_brief_v2": {"0.15.1": "2026-10-24", "0.16.0": "2026-11-01", "0.17.0": "2026-11-02"},
  "code_drop_suggestion": {"tag": "v0.17.0", "commit": "6f867fe", "tests_passed": 2958, "tests_skipped": 90, "coverage": 0.9592},
  "open_items": ["e5_rerun_on_fixed_code", "uncertainty_band_rule", "break_even_100k_and_history", "impact_explain_in_sql", "multi_writer_and_1m"]
}
```
