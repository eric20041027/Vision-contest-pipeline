# E5 打平點實驗 後記

- spec：`../specs/2026-10-23-vcp-e5-break-even-design.md`。沒有另寫計畫：spec §11 的時程就是工作清單，照「spec → 產生器 → 預測 → 分析腳本 → runner 與 PostgreSQL 正確性測試 → 量測 → 交付」的順序做。
- 分支 `bench/e5-break-even`，base `main` 4cfd1ee（v0.15.0 之後，#48 合併後）。
- commit（事先登記的部分）：
  - `2d853a0` docs(spec): E5 break-even experiment design
  - `894a156` feat(bench): dense workload family for the E5 break-even experiment
  - `d24c80b` feat(bench): pre-register E5 predictions from the frozen policies
  - `9411182` docs(bench): E5 pre-registered predictions（`postgres-provenance-break-even-v1-predictions.json`，SHA-256 `e2f61c8d3185158e8cb84548128cc4f3699dd5d5fdb9a904e1d6b1007fe2164d`，由 `d24c80b` 產生）
  - `3c4229d` feat(bench): pre-registered E5 analysis
  - 以上與 code review 的修正一起由 PR #50 合併（`3695dc9`）。
- commit（runner 與 PostgreSQL 正確性測試，分支 `bench/e5-runner`，base `main` `3695dc9`）：
  - `401eaba` test(provenance): PostgreSQL parity for ADDED and REMOVED diffs
  - `0c5a843` feat(bench): E5 break-even runner（本後記裁決 8–12）
  - `a1d68d1` test(bench): pin every dense count and the registered workloads（code review 的處置）
- 還沒有任何 E5 量測。spec §6 從第一個 E5 量測開始凍結。

## 1. 裁決（spec 沒寫死、實作時決定的事）

1. **密度怎麼做出來**（使用者 2026-10-23 同意的方向）：A 段只有 MODIFIED，用讀數補足邊數；B 段用 ADDED 換掉 MODIFIED，沒有讀數。理由：MODIFIED 每筆加 2 條邊，只靠它密度到不了 0.5（spec §2）。只用 ADDED、不用 REMOVED：一個旋鈕比較好解釋。
2. **規模的定義**：dense 場景的 `entities` 是名目值，套用 delta 後的圖剛好有 `2 × entities` 條邊（1K → 2,000、10K → 20,000，分別落在 v2 的 decade 3、4）。重複次數沿用既有規則（`entities ≤ 10,000` 為 7 次）。
3. **填充放在獨立的 dataset**（量測前更正 spec §3.1）：spec 初稿寫「一個消費 `dense-source` 的 run」，產生器卻把讀數掛在第三個 dataset `dense-filler`。code review 抓到這個不一致；裁決改 spec、留程式。理由：掛在 source 下游的讀數會落進 incremental 的 dirty 範圍（`DIRTY_CLOSURE_SQL` 從 delta 的 source 往下走），L1–L3 每次 ingest 要多修 394–13,194 筆讀數；成本模型看不到 dirty，密度就不再是唯一的變數。實際的 dirty 範圍由量測的 `dirty_entities` 回報（spec §6.4）。
4. **來源永遠留一個沒變的樣本**，任何版本都不會是空的 dataset。它沒有 sample entity，不影響邊數。
5. **L4 的目標寫成 0.50**，實際是結構上限：decade 3 為 0.4985、decade 4 為 0.4999，都在模型打平點 0.5037 之下。
6. **H5c 的不適用條件**（spec §6.3，量測前補上）：某一層的最高密度級 FULL 沒有贏，該層就沒有實測交會點；兩層都沒有時 H5c 不適用。原文只說「H5a 不成立時不適用」，勝負不單調時會沒有定義。
7. **預測的 dirty 欄位寫 0**：預測時還不知道 dirty 範圍，但兩個 policy 的成本模型都不看它（係數為 0，腳本檢查，看了就拒絕），所以不影響任何預測。
8. **runner 是獨立入口 `break_even_evaluation.py`**（量測前更正 spec §9）：spec 原寫「`evaluate_adaptive.py` 的 E5 模式」，但那個檔已有 989 行。E5 的評估放進新檔，和預測腳本對稱；共用的列驗證留在 `evaluate_adaptive.py`，泛化成 `RowFamily` 與 `validate_family_rows`，scaled family 的行為不變（既有 108 個測試照過）。dense 列多驗 `total_edges = 2 × entities`、`historical_changes = 0`，以及產生器算得出的 `sample_entities`、`changed_samples`。launcher 接受任意命令，所以不需要改看管程式。
9. **無效證據不發布**：任何列失敗、缺列、計數不符、決策重算不一致、overlap 非零，都在寫檔前 FAIL（exit 1），和 held-out 的 runner 相同；checkpoint 留在 work dir，照 §6.5 如實回報。分析腳本的關卡是第二道檢查，正常情況下一定通過；萬一不通過也是 FAIL、不發布。
10. **量測綁定事先登記**：runner 只接受 SHA-256 為 `e2f61c8d…164d` 的預測檔，預測檔裡的兩個 policy 必須就是載入的凍結 policy，每個場景量到的 `workload_hash` 必須等於預測檔記的值（不等就 `unregistered_workload`）：量的就是事先登記的那份 workload。實際選擇與預測選擇的對照（`prediction_agreement`）只報告、不判定。
11. **VERDICT**：證據有效就一定發布；三個假設都成立為 `OK`，否則 `WARN`，兩者都 exit 0；`FAIL`（exit 1）只留給無效證據與執行失敗。理由：E5 是假設檢定，沒有任何驗收依賴它（不發版、不改 policy）。held-out v2 的 `FAIL` 代表 policy 沒有通過驗收，意義不同。
12. **overlap 的定義**：E5 的列和 calibration、held-out v1、held-out v2 的矩陣共用 seed，或寫著它們的 scenario id／scenario hash，或寫著 calibration 的 workload hash，就算重疊，以不同的場景計數。held-out 的 workload hash 不從已發表的 JSON 讀：workload hash 由 scenario hash 推出，scenario hash 不同，workload 就不同。

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

- 2026-10-23 PostgreSQL 正確性測試（spec §10）：`test_postgres_backend.py` 加了兩組。一組是帶 run 的 det dataset 先「新增 3、移除 1、修改 1」，再移除其中兩筆新增；incremental、full、auto 每一步都要和 canonical replay 與 SQLite 一致。另一組是 E5 的三種 dense 形狀（32 條邊：只有修改、混合、只有新增），incremental 與 full 都要等於產生器的 expected graph。本機 PostgreSQL 17.11 上整個 integration 檔 64 個全過，`src/vcp` 沒有測出 bug。這些是既有行為的特徵測試，沒有「先紅」的階段；測試裡斷言了變更類型的計數與 STALE 的出現，不會空過。postgres workflow 的路徑篩選加上 `workloads.py`。
- 2026-10-23 本機 vcp PostgreSQL（port 55432）沒在跑：排程工作 `VCP-PostgreSQL-17.11` 是 Ready，手動 `schtasks /Run` 啟動。port 5432 上的是另一套 PostgreSQL，與 vcp 無關，沒有動。
- 2026-10-23 E5 runner：測試先跑紅（模組不存在）再實作，17 個測試用凍結的真實 policy 與已 commit 的預測檔。另跑一次探路：兩個 32 條邊的 dense 場景（0.60、1.0）經隔離 runner、真實 PostgreSQL 與四個方法產生 8 列，全部通過 `DenseRow` 驗證與 adaptive 決策重算。這次的毫秒數不是證據，不引用。
- 2026-10-23 runner 的 code review（全新脈絡的 agent）：沒有 CRITICAL、HIGH。它重建了三個 1K dense 場景，`workload_hash` 都等於預測檔；也確認 `DenseRow` 與真實列的欄位相同，`validate_rows` 重構後對 scaled 列的接受與拒絕不變。處置：
  - MEDIUM（三個 dense 計數沒有測試）：`total_edges`、`historical_changes`、`changed_samples` 各漂移 1，決策從漂移後的特徵重算，必須被擋下；同一份列拿掉那個 pin 就會通過，所以擋住它的確實是那個 pin。
  - MEDIUM（沒有測試確認 dense workload 重建後仍是登記的 hash）：14 個 1K 場景裝上兩個 policy 後重建，`workload_hash` 都要等於預測檔的值。量測從合併後的 commit 跑，那個 commit 的 CI 綠了，量的就是登記的 workload；runner 執行時不另做這個檢查。
  - LOW（leakage 只測兩條路徑）：改成五條路徑各一個案例，另測同一個場景的四列只算一次。
  - LOW（輸出沒有記分析腳本）：評估區塊加 `analysis_script`（檔名與 SHA-256）。

## 4. 待辦

1. 量測前：確認 `VCP-PostgreSQL-17.11` 在跑（port 55432）；檢查 C 槽空間；從 runner 合併後的 commit 開 detached worktree，`uv sync --frozen --extra postgres`。
2. 11/3–11/10 量測，在 detached worktree 裡經 `bench_launch.py`、在 `supervise.py` 看管下執行：
   `uv run --frozen python tests/performance/provenance/break_even_evaluation.py --policy-from docs/benchmarks/postgres-provenance-policy-v2.json --comparison-policy-from docs/benchmarks/postgres-provenance-calibration-v2.json --predictions-from docs/benchmarks/postgres-provenance-break-even-v1-predictions.json --output docs/benchmarks/postgres-provenance-break-even-v1.json --work-dir C:/vcp-data/bench/break-even-v1-work`
3. 11/12 前交付（spec §8）。
