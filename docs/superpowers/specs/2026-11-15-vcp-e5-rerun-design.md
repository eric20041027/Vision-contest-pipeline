# E5 重測：在修正後的程式上，用新種子再量一次打平點實驗

- 日期：2026-11-15
- 來源：Data_Base（CS 5151/6051 課程專題）session 的重測要求（2026-11-15）。
- 原本的實驗：`docs/superpowers/specs/2026-10-23-vcp-e5-break-even-design.md`（以下稱 E5 spec），結果
  `docs/benchmarks/postgres-provenance-break-even-v1.md`。那份證據寫一次不改，這次不動它。
- 這是一個**新的、事先登記的實驗**：新的 id（`postgres-provenance-break-even-v2`）、新的種子、新的預測檔、新的分析腳本。
- 版本：不發版。`src/vcp` 不變。在 `tests/performance/provenance/` 與 `tests/unit/provenance/` 加檔案；既有檔案只改一處：
  `adaptive_benchmark.py` 的 checkpoint 合約檢查（§3.1）。E5 的 runner、分析腳本、預測檔與 workload 產生器一行不改。

## 1. 為什麼重測

- E5 在 2026-10-27/28 量完（commit `86919f3`）：H5a 成立、H5b 不成立、H5c 成立。
- 量完之後查到狀態計算的效能缺陷（每筆讀數掃整張圖），10K 前幾級的 FULL 因此變慢；PR #56（`6b72dc4`）修掉，
  隨 0.16.0 發出，算出的狀態完全相同。
- H5b 失敗的幅度（v2 在交會點以下是 INCREMENTAL 的 1.66–3.11 倍、pooled p95 比值 1.628）有一部分來自那個缺陷。
  修正後剩多少，目前只有單次 profile，不是證據。
- 這次回答：在修正後的程式上，同一個設計、同一個凍結的 policy v2，三個假設各是什麼結果。

## 2. 不變的三件事

1. **policy v2 維持凍結**：`postgres-adaptive-v2-9f4e58346529`（`policy.json` SHA-256
   `6fe654a90468edb57aa84fdec7ac2e53cf18b20106f7a77d65df8ac7cd27c000`）。不重新擬合成本模型，不改信心帶。結果不好就照實寫。
2. **全新的種子**：`20261115`、`20261116`（§3）。
3. **先登記、再量測**：本檔、預測檔、分析腳本、runner 與測試在同一個 commit，量測從那個 commit 的 detached worktree 開始。
   回報時附這個 commit 的 hash 與時間，以及量測開始的時間。

## 3. 設計：和 E5 相同，只改這幾項

| 項目 | E5 | 這次 |
|---|---|---|
| 程式 | `86919f3`（0.15.0 之後） | 本檔所在的 commit（main `7be1fd9`，tag `v0.18.0`，加上本次登記的檔案） |
| 種子 | 20261201、20261202 | 20261115、20261116 |
| 方法 | pg_full、pg_incr、v2、v1 | pg_full、pg_incr、v2。**不跑 policy v1**（範圍決定：省時間；v1 已凍結，只留給重現舊證據） |
| 看管 | `supervise.py`（機器空閒才啟動，別的程式用 CPU／GPU 就暫停並丟棄進行中的場景） | 只用 `bench_launch.py` 的 RAM 護欄（可用 < 2 GiB 持續 30 秒中止）。**不用 `supervise.py`**（範圍決定，見 §7） |

其餘相同：

- workload：`dense` family，兩層（`E ≈ 2,000` 與 `E ≈ 20,000`）× 7 個密度級（0.17、0.30、0.40、約 0.50、0.60、0.75、約 1.00）× 2 個種子 = 28 個場景，`H = 0`。
  產生器（`workloads.py` 的 `dense_matrix`、`build_scenario`）一行不改，只換種子。
- 每個方法 7 次重複，沒有計時的 warmup，每次重複用全新的資料庫，量完就 drop。
- 標準答案是 canonical replay 的 expected graph；每一列做 exact parity（graph、graph hash、head、status）。
- 每個 adaptive 列的決策，用 policy v2 從記錄的特徵重算，必須完全一致。
- overlap 檢查：這次的種子、場景 id、場景 hash、workload hash，和 calibration（20260913、20260914）、held-out v1（20261001、20261002）、
  held-out v2（20261101、20261102）、E5（20261201、20261202，含它預測檔裡的 workload hash）都不相交；`overlap_count` 必須為 0。
- 兩個種子的圖幾乎一樣（形狀由 `m`、`a`、`F` 決定），是同一種 workload 的重複計時，不是獨立重現（E5 spec §3.3）。

### 3.1 量測前修掉的一個擋路問題（不影響計時）

- 量測框架把原始檔清單（路徑與雜湊）寫進 checkpoint 合約，再用一個通用的「機密字串」過濾檢查整份合約；
  0.18.0 新增的 `src/vcp/artifact/authorization.py` 檔名含 `authorization`，合約因此一律被判為不安全，
  在現行 main 上任何場景都啟動不了（`unsafe checkpoint evidence`）。E5 當時的原始碼沒有這個檔案。
- 修法：原始檔路徑改用形狀檢查（repo 內的相對路徑，只含英數、底線、連字號，副檔名 `.py`／`.toml`／`.lock`），
  合約的其他內容照舊過濾。只動 `CheckpointStore` 建立時的檢查，不動任何計時、parity 或決策的程式。
- 這是用一個**不在任何登記範圍內的丟棄種子**（987654，1K、密度 0.6，一個場景）做端到端冒煙測試時發現的；
  那次的輸出只用來確認 runner 跑得完，量到的時間不保留、不當證據，工作目錄已刪除。這次登記的兩個種子在本 commit 之前沒有量過。

## 4. 預測（量測前從凍結的 policy 算出）

- 檔案：`docs/benchmarks/postgres-provenance-break-even-v2-predictions.json`，寫一次不改；SHA-256 釘在 runner 的 `PREDICTIONS_SHA256`。
- 內容：28 個場景各自的 `C`、`E`、`H`、密度、workload hash，以及 v2 的 `estimated_incremental_ms`、`estimated_full_ms`、帶寬、模型的勝方與選擇。
- 這次只裝 policy v2（E5 同時裝了 v1），所以 workload hash 和 E5 的不同；圖的特徵與 v2 的選擇不受影響。
- 預測檔的 `vcp` 欄是 `0.18.0+g7be1fd9….dirty`：產生時本次登記的檔案還沒 commit；它只依賴凍結的 policy 與沒改過的產生器。
- **v2 的預測選擇**（兩層、兩個種子都相同）：L1（0.17）選 INCREMENTAL；L2–L7（0.30 起）選 FULL，共 4 個 INCREMENTAL、24 個 FULL。
  L2–L4 是「差距在帶寬內」（模型預估 INCREMENTAL 較便宜，1K 的 L4 是 1,395 對 1,407 ms，10K 是 13,985 對 14,074 ms），
  L5–L7 是「模型預估 FULL 較便宜」。圖的特徵（`C`、`E`、`H`）和 E5 相同，所以這些預測也和 E5 的 v2 預測相同。
- 量完後回報 28 個選擇裡幾個吻合（診斷，不是 gate）。

## 5. 正確性 gate 與假設

正確性 gate（必須通過，否則這份證據無效、不發表）：每一列 parity 為 true；每個 adaptive 決策重算一致；`overlap_count` 為 0；
列剛好涵蓋 28 個場景 × 三個方法。

假設的定義和 E5 spec §6.3 相同：

- **H5a（交會點存在）**：至少有一層、一個密度級，該級每個場景都是 `pg_full p50 ≤ 0.95 × pg_incr p50`。
  實測交會點是每層「該級以上每一級都符合」的最低一級；回報它落在哪兩級之間。最高一級不符合時，該層沒有實測交會點。
- **H5b（門檻）**：28 個場景的原始樣本 pool 起來，`v2 p50 ÷ min(兩個固定方法的 pooled p50) ≤ 1.05`、`p95 比值 ≤ 1.10`。
- **H5c（自適應的價值）**：每層位於實測交會點（含）以上的場景 pool 起來，`v2 pooled p50 < pg_incr pooled p50`。
  H5a 不成立，或兩層都沒有實測交會點時，標為「不適用」。
- 判定只寫「成立」「不成立」「不適用」，附兩邊數字。

## 6. 另外回報（只報告，不判定）

- 每個場景的 `estimated_incremental_ms`、`estimated_full_ms` 與實測 p50 的差。成本模型是用修正前的程式擬合的，這裡看它在修正後準不準。
- 每層交會點以下，`v2 ÷ pg_incr` 的範圍（對應 E5 的 1.66–3.11）；沒有交會點的層，報全部七級。
- 每層交會點以上，`pg_full ÷ pg_incr` 的範圍。
- 每一級兩個種子的 p50 平均（pg_full、pg_incr、v2）與 v2 的選擇。
- 兩種固定方法差距超過 5% 的場景裡 v2 選對幾個；v2 選 FULL 的兩種原因（模型預估 FULL 較便宜／差距在帶寬內）分開數。

分析腳本：`tests/performance/provenance/break_even_rerun_analysis.py`。它是 E5 分析腳本去掉 v1、加上本節兩個範圍與每級平均的版本；
只吃量測輸出的列與 runner 給的 `overlap_count`。它的 SHA-256 由 runner 記進證據檔。

## 7. 執行條件與已知限制（量測前就知道的）

- **停損**：投影片在 2026-11-15 23:59 截止（這台機器的當地時間，UTC−5）。當地 21:30（2026-11-16 02:30 UTC）還沒發表證據檔就停止量測。
  量到一半的 checkpoint 不是證據，不整理成結論；這兩個種子就此作廢，之後要量得換新的種子重新登記。
- **機器不安靜**：同一台電腦有另一個 session 在改投影片（PowerPoint 開著，偶爾跑幾秒的 `node build.js`）。
  `supervise.py` 遇到這種負載會暫停並丟棄進行中的場景，在固定的停損時間下等於量不完，所以這次不用它。
  代價是計時可能帶雜訊；判定用每個場景 7 次的 p50 與 pooled 的 p50／p95，短暫的干擾主要影響 p95。這一點寫進結果的偏離。
- **`H = 0`、只有兩層、同一台機器、同一個產生器、同一個 PostgreSQL（17.11）**：和 E5 相同的限制（E5 spec §10）。
- 修正後 10K 的 FULL 不再隨填充讀數變慢，所以這次的交會點可能比 E5 低；模型「FULL 只看邊數」的假設會不會更貼近，由 §6 的殘差回答。

## 8. 禁止事項

- 看過任何這次的結果之後，不得調整 policy、帶寬、密度級、§5 的條件或分析腳本後重跑。
- 不得以「已知問題」為由排除任何場景；假設不成立就照實報告為不成立。
- 量測期間 worktree 的原始檔不得改動（runner 釘住原始檔雜湊）。

## 9. 交付

1. `docs/benchmarks/postgres-provenance-break-even-v2.json`：寫一次不改，附 SHA-256。
2. 給 Data_Base 的結果摘要（放在使用者指定的位置，不進 repo），每個數字都能對回 JSON，並附一小段和 10 月那次的對照。

## 10. 執行期修訂

（量測開始後的任何偏離，記在這裡，附時間與理由。）

- 2026-11-15 19:43（當地，量測結束後記錄）：量測從 `1da729f` 的 detached worktree 在 18:02:05 開始、19:43:20 結束，
  returncode 0，一次跑完，沒有中斷或續跑；量測期間 worktree 的原始檔沒有改動。停損沒有觸發。
- 證據檔 `docs/benchmarks/postgres-provenance-break-even-v2.json`，
  SHA-256 `9f9c624873982351734ab4d6099eadbbf181141bc0b2138174172993b1f6f63d`。
  看過結果之後沒有重跑、沒有改分析腳本、沒有排除場景。
- 量測中沒有發生登記以外的偏離。§7 寫的「機器不安靜」照舊成立：另一個 session 整段時間都開著 PowerPoint，影響多大沒有量。
- 結果裡有一件事原因未查：10K 交會點以上，兩種固定方法的 p50 都比 E5 慢（FULL 在 L4–L6 慢 7%–20%，
  INCREMENTAL 在 L4–L7 慢 9%–25%）。判定用的是同一次量測內的比值，不受影響；跨兩次的毫秒數不能直接相減。
