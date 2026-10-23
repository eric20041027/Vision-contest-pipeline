# E5 打平點實驗 後記

- spec：`../specs/2026-10-23-vcp-e5-break-even-design.md`。沒有另寫計畫：spec §11 的時程就是工作清單，照「spec → 產生器 → 預測 → 分析腳本 → runner 與 PostgreSQL 正確性測試 → 量測 → 交付」的順序做。
- 分支 `bench/e5-break-even`，base `main` 4cfd1ee（v0.15.0 之後，#48 合併後）。
- commit（事先登記的部分）：
  - `2d853a0` docs(spec): E5 break-even experiment design
  - `894a156` feat(bench): dense workload family for the E5 break-even experiment
  - `d24c80b` feat(bench): pre-register E5 predictions from the frozen policies
  - `9411182` docs(bench): E5 pre-registered predictions（`postgres-provenance-break-even-v1-predictions.json`，SHA-256 `e2f61c8d3185158e8cb84548128cc4f3699dd5d5fdb9a904e1d6b1007fe2164d`，由 `d24c80b` 產生）
  - `3c4229d` feat(bench): pre-registered E5 analysis
- 還沒有任何 E5 量測。spec §6 從第一個 E5 量測開始凍結。

## 1. 裁決（spec 沒寫死、實作時決定的事）

1. **密度怎麼做出來**（使用者 2026-10-23 同意的方向）：A 段只有 MODIFIED，用讀數補足邊數；B 段用 ADDED 換掉 MODIFIED，沒有讀數。理由：MODIFIED 每筆加 2 條邊，只靠它密度到不了 0.5（spec §2）。只用 ADDED、不用 REMOVED：一個旋鈕比較好解釋。
2. **規模的定義**：dense 場景的 `entities` 是名目值，套用 delta 後的圖剛好有 `2 × entities` 條邊（1K → 2,000、10K → 20,000，分別落在 v2 的 decade 3、4）。重複次數沿用既有規則（`entities ≤ 10,000` 為 7 次）。
3. **填充放在獨立的 dataset**（量測前更正 spec §3.1）：spec 初稿寫「一個消費 `dense-source` 的 run」，產生器卻把讀數掛在第三個 dataset `dense-filler`。code review 抓到這個不一致；裁決改 spec、留程式。理由：掛在 source 下游的讀數會落進 incremental 的 dirty 範圍（`DIRTY_CLOSURE_SQL` 從 delta 的 source 往下走），L1–L3 每次 ingest 要多修 394–13,194 筆讀數；成本模型看不到 dirty，密度就不再是唯一的變數。實際的 dirty 範圍由量測的 `dirty_entities` 回報（spec §6.4）。
4. **來源永遠留一個沒變的樣本**，任何版本都不會是空的 dataset。它沒有 sample entity，不影響邊數。
5. **L4 的目標寫成 0.50**，實際是結構上限：decade 3 為 0.4985、decade 4 為 0.4999，都在模型打平點 0.5037 之下。
6. **H5c 的不適用條件**（spec §6.3，量測前補上）：某一層的最高密度級 FULL 沒有贏，該層就沒有實測交會點；兩層都沒有時 H5c 不適用。原文只說「H5a 不成立時不適用」，勝負不單調時會沒有定義。
7. **預測的 dirty 欄位寫 0**：預測時還不知道 dirty 範圍，但兩個 policy 的成本模型都不看它（係數為 0，腳本檢查，看了就拒絕），所以不影響任何預測。

## 2. 預測結果（量測前）

兩個 seed 的選擇完全相同。

| 層 | 級 | 實際密度 | 模型較便宜 | v2 選擇 | v1 選擇 |
|---|---|---|---|---|---|
| decade 3 | L1 0.17 | 0.1700 | INCREMENTAL | INCREMENTAL | FULL |
| decade 3 | L2–L4 | 0.30–0.4985 | INCREMENTAL | FULL | FULL |
| decade 3 | L5–L7 | 0.60–0.997 | FULL | FULL | FULL |
| decade 4 | L1 0.17 | 0.1700 | INCREMENTAL | INCREMENTAL | INCREMENTAL |
| decade 4 | L2–L4 | 0.30–0.4999 | INCREMENTAL | FULL | FULL |
| decade 4 | L5–L7 | 0.60–0.9997 | FULL | FULL | FULL |

- L2–L4 是 v2 的「不確定」區：模型認為 INCREMENTAL 比較便宜，但差距小於帶寬（decade 3 556.2 ms、decade 4 5,586.3 ms），規則選 FULL。

## 3. 過程紀錄

- 2026-10-23 獨立審查（全新脈絡的 agent）驗證 spec §2 的前提，結論是成立；它提的三個注意事項已寫進 spec §2、§6.4、§10。
- TDD：產生器與分析腳本的測試都先跑紅再實作；預測腳本的實作和測試在同一步寫好，測試沒有先跑紅。預測腳本的測試用的是凍結的真實 policy，數值斷言不依賴實作。
- 2026-10-23 code review（全新脈絡的 agent）：沒有 CRITICAL。它逐位元組比對了舊產生器（scaled family 的檔案、`workload_hash` 與三個已發表的 held-out v2 1K 場景都不變），也確認預測的特徵和 `decide()` 一致。處置：
  - HIGH（spec §3.1 與產生器的拓樸不一致）：改 spec，見裁決 3。
  - MEDIUM（分析腳本沒有 §6.1 的關卡與覆蓋檢查）：`analyze` 現在先跑 `correctness_gate`，檢查場景與方法的覆蓋、每列 status 與五個 parity 欄位、`overlap_count`；不通過就只輸出關卡，不輸出任何假設判定。決策重算仍由 runner 在呼叫前做（spec §7）。
  - MEDIUM（測試抓不到的錯誤）：補了不連續勝出的交會點、只有一層有交會點的 H5c、能分出「原始樣本 pool」與「各場景 p50 的中位數」的 H5b，以及關卡的各種失敗。
  - LOW（舊產生結果沒有被測試鎖住）：加測試，鎖住 held-out v2 場景 `scaled-20261101-1000-38ff0577eba57ab7` 的 `workload_hash`。
  - LOW（兩個 seed 幾乎是同一個 workload）：寫進 spec §3.3：兩個 seed 是重複計時，不是獨立重現。

## 4. 待辦

1. 11/2 前：ADDED 型 diff 的 PostgreSQL 正確性測試（spec §10）；`evaluate_adaptive.py` 的 E5 模式（dense 列的驗證、overlap 檢查、四個方法、輸出接 `break_even_analysis.analyze`）。
2. 量測前：檢查 C 槽空間；從合併後的 commit 開 detached worktree。
3. 11/3–11/10 量測；11/12 前交付（spec §8）。
