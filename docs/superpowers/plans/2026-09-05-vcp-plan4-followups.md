# Plan 4 後記：裁決、審查發現與待辦（融合層）

- 日期：2026-09-05
- 計畫：`docs/superpowers/plans/2026-09-05-vcp-plan4-fusion-layer.md`（8 任務）
- spec：`docs/superpowers/specs/2026-09-05-vcp-fusion-layer-design.md`（v1 + §14 補充決定）
- 分支：`worktree-plan4-fusion-layer`（自 main 的 `604ec70` 分出；`.claude/worktrees/plan4-fusion-layer`）
- 執行方式：Subagent-Driven Development。預檢由控制者人工掃描（16 列接縫表，3 條裁決）；每個任務一位實作者（轉錄型任務 haiku、整合型任務 sonnet）、一位審查者（sonnet）；最終全分支審查一位（opus）、唯一修正輪、範圍限定再審後收尾。

## 1. 結果摘要

| 項目 | 值 |
|---|---|
| commit | 10 個（8 任務 + 最終修正輪 2 個）+ 收尾文件 |
| 測試 | 585 passed、4 skipped（海廢資料集未匯入 ×3、ensemble-boxes 未安裝 ×1） |
| 覆蓋率 | 96.64%（門檻 80%） |
| ruff | `check` 與 `format --check` 皆乾淨 |
| 警告 | 整套測試在 Deprecation / Resource / RuntimeWarning 皆為錯誤下只剩 1 個：Plan 2c 遺留的 `test_png_resize_then_skip_then_force` 未關閉 PIL 檔案（Plan 3 後記待辦 14 已列），非本計畫範圍 |
| 真資料 | `tests/integration/test_rsna_knee_fuse.py` 2 passed（RSNA 兩個完美 run 以 `rank_mean` / `mean` 融合後 macro AUC = 1.0） |
| 端到端 | det 三成員準入：`good` PASS、`noise` FAIL、`half` FAIL；multilabel `mean` / `rank_mean` 各兩筆 macro_auc 讀數；`--plugin` 自訂融合器可用 |

交付：`src/vcp/fuse/`（schema、recipes、members、build、ablate、fusers/{base,wbf,scores}）；`vcp fuse recipe|build|ablate`（`cli_fuse.py`）；`cli_common.parse_csv`（eval / fuse 共用）；`measure/predictions.predictions_text`（`write_predictions` 與 `build.content_sha` 共用）；`measure/prereg.measured_subsets` 公開；README「融合層命令」一節；CLAUDE.md 十個變異軸、台帳路徑、常用命令。

## 2. 預檢裁決（執行前）

接縫表 16 列（任務對、每任務自洽）全部乾淨，三條裁決：

- **R1** `cli_fuse.py` 保留自己的 3 行 `_csv`，不跨模組 import 私有名——最終審查建議改放 `cli_common`，修正輪照做（`parse_csv`），R1 作廢。
- **R2** `check_plan` 住在 `vcp/fuse/members.py`，`build` / `ablate` 從那裡 import；計畫檔案表的 `build.check_plan` 是筆誤，介面段為準。
- **R3** `build.content_sha` 刻意重寫 `write_predictions` 的序列化並以測試釘住——最終審查建議抽出共用的 `predictions_text`，修正輪照做，R3 作廢。

## 3. 執行期裁決

- **R4（Task 3）** 計畫的 `test_wbf.py` 夾具 `_sample()` 預設 view 為 8×8，四個測試卻融合 10–14 px 的框；spec §7.2 第 5 步的裁邊把幾何縮小，手算期望值以未裁邊為準。裁決：夾具預設改 `(64, 64)`，演算法與 spec 不動；計畫檔一併修正。
- **R5（Task 5）** 計畫的 `test_cli_fuse.py` import 了沒用到的 `DatasetPaths`；實作者以 `ruff check --fix` 移除視為 lint，不算偏離。
- **R6–R8（最終審查）** 見 §5。

## 4. 各任務審查發現與處置

| 任務 | 發現 | 處置 |
|---|---|---|
| 1 | `load_recipe` 的「檔案自稱 X 非 Y」分支無測試（計畫自己的測試檔） | 留待辦 |
| 2 | 實作者報告的行數錯（31/150/143） | 報告衛生，程式碼經獨立核對 |
| 3 | Σq=0 後備、view 索引越界守門、IoU 等於門檻三個分支無測試；`members` 為空 / Σweight=0 未守門（不可達：Recipe 要求 ≥1 成員且 weight>0）；`_fuse_sample` 約 57 行（計畫規定） | IoU 邊界測試在修正輪補上；其餘留待辦 |
| 4 | `RankMean` 跨 sample 鍵不齊分支無測試；該錯誤的 `member=` 指向 `members[0]`（其實是 sample 對 sample 的不齊） | 留待辦 |
| 5 | 沒有 CLI 層測試驅動 `PlanMismatchError`（exit 2）走過 `run_command` | 留待辦 |
| 6 | `check_existing_run` 的 `assert_run_matches` 錯誤沒帶 `run=`（全庫慣例）；trained_on 漂移分支缺 `run_bound_elsewhere` 前綴且無測試；「run 存在但無 fuse.json」與 `--subsets` 去重無測試；`check_predictions` 的 `--opt allow_unknown` 提示會從 `fuse build` 冒出；`vcp_version` 凍結在首次 build | 凍結問題升級為最終審查 Important 2 並修正；其餘留待辦 |
| 7 | `ablate_recipe` 約 64 行（線性檢查順序刻意保留） | 不改 |
| 8 | 無 | — |

## 5. 最終全分支審查（opus）

三輪通讀：spec + 台帳、11 個原始檔、9 個測試檔與被消費的量測層函式；對照 ensemble-boxes 上游原始碼逐行核對 WBF；以探針腳本在拋棄式資料根目錄重現兩個 Important。無 Critical。

**Important（唯一修正輪全部處理，再審通過）**

1. `ablate --preregister` 的 `--bases` 只靠 `create_prereg` 在寫入之後才驗（非空、無重複），`--bases valA,valA` 會留下兩份寫了不改的變體配方與三個 run；`--no-build --bases typo` 會寫下永遠無法判決的預登記。修正：`_check_claims` 無條件（含 `--no-build`）先驗非空、無重複、每個都是 plan 子集，再驗「建構時 bases ⊆ 建的子集」，全部在 `save_recipe` 之前；三個測試各斷言一個檔都沒寫。
2. `fuse.json` 的 `method_version` / `vcp_version` / `members[].trained_on` 凍結在首次 build，重建（fuser 升版、vcp 升版）後仍寫舊值——spec §4.2 說整檔重寫。修正：每次 build 重算 `_new_record`，只把既有的 `subsets` 帶過去；測試把磁碟上的 record 改成過時值再 `--replace` 重建。

**Minor（修正輪一併處理）**：`load_record` 驗 `run_id`（F3）；IoU 等於門檻不合併的邊界測試（F4）；`_csv` → `cli_common.parse_csv`（F5）；`predictions_text` 抽出（F6）；README 補 ablate 無 `--replace` 的復原流程（F7）；oracle 測試上方記下 2026-09-05 對上游的人工核對（F8）。

裁決：**R6** 上述八項合併為一個修正輪；**R7** Minor 4（`--opt` 提示文字住在量測層、服務 ingest）、Minor 5（plan hash 檢查在資料 / 量測 / 融合三處各一份）、Minor 10（`eval ingest` 可對融合 run 加子集而 `fuse.json` 不記——子專案 5 的來源規則）留待辦；**R8** spec / 計畫的文字修正由控制者在收尾 commit 做（計畫 Global Constraints 的 `fields` 清單補 `sample=`；計畫 Task 3 夾具預設改 64×64；spec 加 §14）。

再審的兩個理論觀察（皆不可達或無讀者）：`write_predictions` 現在先開檔再序列化（呼叫者傳入的都是已驗證、已序列化過一次的清單）；`BuildResult.record` 在全部快取命中時是記憶體重算的，若兩次快取建構之間 fuser / vcp 升版，會與磁碟上未動的 `fuse.json` 不同（沒有任何程式讀 `BuildResult.record`）。

## 6. 待辦（依優先序）

1. **`eval ingest` 不應對 `source.framework == "vcp.fuse"` 的 run 加子集**（或由子專案 5 的來源規則處理）：融合 run 會混入 `fuse.json` 沒記錄的子集。
2. **plan hash 檢查三處各一份**（`dataset.py:150-155`、`ingest.py:148-153`、`fuse/members.py:39-45`）：抽成共用的 `assert_plan_matches(dataset, plan)`。
3. **`check_predictions` 的 `--opt allow_unknown=true` 提示**會從 `fuse build`（沒有 `--opt`）冒出：訊息改成不綁命令的說法。
4. **`BuildResult.record` 在快取命中時的新鮮度**：若日後有讀者，改為回傳磁碟上的 record。
5. **測試覆蓋**：`load_recipe` id 不符分支；`wbf` 的 Σq=0 後備與 view 索引越界；`RankMean` 跨 sample 鍵不齊；CLI 層的 `PlanMismatchError` exit 2；`build` 的 trained_on 漂移、「run 存在但無 fuse.json」、`--subsets` 去重。
6. **`RankMean` 跨 sample 鍵不齊的錯誤欄位**：`member=` 指向 `members[0]` 不精確，`sample=` 已足以定位；可改為不帶 `member=`。
7. **`check_existing_run` 的 `assert_run_matches` 錯誤不帶 `run=`**：與全庫慣例一致，但同函式其他分支都帶；若要一致就包一層 `setdefault`。
8. **ensemble-boxes oracle 從未在 CI 跑**（spec 不允許進依賴）：目前靠測試檔上方的人工核對紀錄；日後若 numba 相容問題解決，可考慮進 dev 群組。
9. **ablate 沒有 `--replace`**：成員重新 ingest 後要逐 run `fuse build --replace` 再重跑 ablate（README 已寫）；若常用可加旗標透傳。
10. **Plan 2c 遺留**：`test_png_resize_then_skip_then_force` 在 `-W error::ResourceWarning` 下失敗（未關閉 PIL 檔案）。→ Hygiene A（2026-09-07）已修：漏在測試本身，改用 `with` 開圖。

## 7. 方法上的紀錄

- 這一輪沒有 Workflow（ultracode 關閉）：預檢由控制者人工做接縫表，8 個任務只有 Task 3 卡住（計畫夾具缺陷，一條裁決解決），其餘一次過。轉錄型任務用 haiku（Task 1、2、4 各約 2–3 分鐘）、整合型用 sonnet；審查者 sonnet 每次都獨立重算手算案例並讀被消費的相鄰程式碼，8 次審查零 Important。
- 最終審查抓到的兩個 Important 都是跨任務接縫（寫入順序 vs 被呼叫層的寫入時驗證；首次建構 vs 重建的生命週期），單任務審查看不到——下次預檢清單加一條：「被呼叫層在寫入時做的每一項驗證，呼叫端在任何寫入前都有對應的預檢嗎？」
- 實作者報告若沒有整套測試的證據行，控制者自己補跑（Task 4）；`SendMessage` 不可用時修正輪改派新實作者並以報告檔為記憶（Task 3）。
