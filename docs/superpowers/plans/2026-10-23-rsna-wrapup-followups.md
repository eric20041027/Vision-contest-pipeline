# RSNA Knee 收尾後的 vcp 後續計畫（2026-10-23）

- 來源：RSNA Knee 比賽線收尾時的回報（比賽 2026-10-22 截止；比賽用的 vcp 是釘在 v0.14.0 的 worktree）。本檔只列 vcp 要做的事，不含比賽本身的紀錄。
- 排序考慮課程時程：進度報告 10/25、投影片 11/15、發表 11/18–25、期末報告 12/6。
  - E5 打平點實驗是課程承諾（11/12 交），優先；它的設計與事先登記另有 spec。
  - 11/3–11/10 是 E5 的正式量測期，機器保持安靜：不跑測試、不訓練，只寫文件。
  - 11/13–12/6 框架凍結，只做課程。
- 發版一律先經使用者同意。

## 0. 時程

| 時間 | 事項 | 版本 |
|---|---|---|
| 10/28 前 | E5 的密度可行性、設計與事先登記 | 不發版 |
| 11/2 前 | E5 的 workload；§1 的 1a | 0.15.1（PATCH） |
| 11/3–11/10 | E5 量測（實際提前在 10/27–10/28 完成，見 E5 spec §12）；§6 的 6b（只寫文件） | — |
| 11/12 前 | E5 交付（結果檔 `docs/benchmarks/postgres-provenance-break-even-v1.md`） | — |
| 12/6 之後 | §1 的 1b、§2、§3（0.16.0）；§4；Wave 1c；1.0 | MINOR |

- **2026-10-30 調整（使用者的決定）**：E5 提早在 10/28 量完，課程凍結前多出約一週。§6 的 6b 照舊先做（11/8 前），之後三件都做：
  - 0.16.0（§1 的 1b、§2、§3）提前到凍結前；11/11 前沒有全綠就只合併、發版延到 12/6 之後。發版仍要使用者同意。
  - Wave 1c 只寫 spec、不寫程式。
  - 查 E5 看到的 FULL 讀數成本：已查明是 `src/vcp` 的效能缺陷（E5 後記待辦 2），修正併進 0.16.0。
- 0.16.0 的設計決定（同日）：
  - §2 只做手動記錄加提醒，不自動讀平台：Kaggle SDK 有「只列已勾選」的查詢選項，但 CLI 2.2.5 沒有開放。
  - 截止前 48 小時開始提醒，可設定。
  - §3 `train run` 啟動前可用空間低於 10 GiB 就 ABORT，可覆寫。

## 1. 【Bug，P1】Kaggle CLI 的版本警告讓 `submit sync` 失敗

- 症狀：`VERDICT cmd=submit.sync status=FAIL reason="ValidationFailed: platform_response: not JSON"`。
- 原因：kaggle CLI 2.2.4 在伺服器版本較新時，把 `Warning: Looks like you're using an outdated kaggle version…` 印在 stdout、JSON 的前面；`submit/platforms/kaggle.py` 的 `parse_submissions` 對整段輸出做 `json.loads`。`submit.yaml` 的 `kaggle_command` 是 exe 的絕對路徑，無法用 PATH 墊一層繞過。比賽端把 CLI 升到 2.2.5 之後才同步成功。
- 1a（PATCH 0.15.1）：解析前略過資料開始之前的警告行。哪些行可以略過（只限 `Warning:` 開頭，或資料前的任何非資料行）在實作前決定，原則是不要放寬到把真正的錯誤訊息吞掉。回歸測試：假 CLI 先印警告、再印 JSON。
- 1b（併入 0.16.0）：略過的警告以 WARN 浮出，`submit status` 也提示 CLI 過舊。新的 VERDICT 欄位屬於 CLI 契約，是 MINOR。
- 一併確認 CLI 2.2.5 的 JSON 形狀和 2.2.4 相同：`is_errored` 依 2.2.4 的形狀判斷出錯，形狀變了就更新假 CLI 的 fixture。
- 1a 的處置（2026-10-23，分支 `fix/kaggle-warning-lines`）：
  - 裁決：只略過答案前面以 `Warning:` 開頭的行與空白行，每一行經 redact 後寫進 vcp logger 的 WARNING。其他任何行、只有警告沒有答案、答案之後才出現的警告，都還是 `not JSON`。回歸測試包含 RSNA 失敗的那條路徑：`list_submissions` 讀到先印警告的列表。
  - log 行不是 CLI 契約，VERDICT 欄位不變，所以仍是 PATCH。VERDICT 的 WARN 留給 1b。
  - CLI 2.2.5 的 JSON 形狀沒有確認：確認要用使用者的 Kaggle 憑證呼叫一次真的 CLI，這由使用者決定。RSNA 端升到 2.2.5 之後 `submit sync` 成功，代表 2.2.5 的列表能被解析；`is_errored` 依賴的出錯形狀沒有再驗證。
  - 發版（0.15.1）要使用者同意，修正先合併進 `main`。
- 1b 的處置（2026-10-30，0.16.0，分支 `feat/v0160-selection-disk`）：
  - `submit sync` 與 `submit upload` 的 VERDICT 一律帶 `cli_warnings=`（不同行數；> 0 WARN），人看的行每行一個 `cli_warning: <行>`，任一行含 `outdated`（不分大小寫）時多一行升級提示；警告只出現在這次命令的 VERDICT、輸出與 log，不寫台帳，每次讀列表各自報。
  - **偏離本節的 1b：`submit status` 不提示 CLI 過舊**（controller 裁決 R1）。`status` 從不呼叫平台，要提示就得把「上次讀列表時 CLI 印了什麼」存下來；唯一讀得到的地方是治理台帳（`configs` 模式下在 git 裡），把操作面的事實寫進去，代價大於這個缺口；`upload` 一律先同步，所以每次讀平台都會 WARN。
  - CLI 2.2.5 的 JSON 形狀仍沒有確認（沒有動）。
  - 設計見 `../specs/2026-10-30-vcp-0160-selection-and-disk-design.md` §4，實作裁決與待辦見 `2026-10-30-vcp-0160-selection-and-disk-followups.md`。

## 2. 【缺口，P2】平台上實際勾選的最終提交沒有記錄、也沒有核對

- 經過：這場比賽的最終選取是在 Kaggle 網頁上手動勾的。這次勾錯了兩發（勾成九月的舊提交），vcp 沒有察覺：台帳的 current 和實際勾選不同。
- 現況：`submit final` 依 sealed 讀數挑選並封槍，記的是 vcp 的建議，不是平台上實際的選取。暫時的做法是用 `note` 事件手記實際勾選。
- 做法（0.16.0，MINOR，先寫 spec）：
  - 新的台帳事件記錄實際勾選的 id，指令例如 `submit select --record <ids>`。舊 vcp 讀到新事件會 FAIL，讀寫同一份台帳的都要先升級（同 0.14.0 的 `errored`）。
  - `submit status`：截止前 N 小時（N 可設定）還沒記錄選取、選取數和名額不同、或選取和 `final` 不同時給 WARN。
  - 調查 Kaggle API 能不能讀到 selected 旗標。CLI 的 CSV 只有 ref、fileName、date、description、status、publicScore、privateScore。讀得到就由 `sync` 自動核對。
- 處置（2026-10-30，0.16.0，分支 `feat/v0160-selection-disk`）：照「手動記錄加提醒」做，不自動讀平台。
  - 調查結果：kagglesdk 0.1.38 的 `ApiListSubmissionsRequest.group` 有 `SubmissionGroup.SUBMISSION_GROUP_SELECTED`，但 kaggle CLI 2.2.5 的 `competitions submissions` 沒有對應旗標，它的 JSON 也沒有 selected 欄位；vcp 只呼叫 CLI、不用 Python API、不碰憑證，所以讀不到。日後 CLI 有了旗標，`sync` 可以寫 `source=platform` 的 `selected` 列（schema 已留位置）。
  - 做了：台帳新事件 `selected`、`vcp submit select --record`、`submit.yaml` 的 `selection_warn_hours`（預設 48）、`status` 的 `selected=` 與 `selection=missing|slots|differs`、`report` 的 `selected`、`final` 的 `selection=differs` 與 `next:` 提示。核對只在 `board_rule: best`（`last` 的平台自己算最後一發）。舊 vcp 讀到 `selected` 列會 FAIL，升級順序同 0.14.0 的 `errored`。
  - 設計見 `../specs/2026-10-30-vcp-0160-selection-and-disk-design.md` §3，實作裁決與待辦見 `2026-10-30-vcp-0160-selection-and-disk-followups.md`。

## 3. 【韌性，P2】訓練中磁碟滿

- 經過：存 resume 檔時 `torch.save` 失敗（`ios_base::badbit`），留下一個 266 MB 的 `.tmp` 殘檔；接續成第 2 次 attempt 才完成。
- 做法（0.16.0）：
  - `train run` 啟動前檢查輸出目錄所在磁碟的可用空間（門檻可設定），不足就 ABORT；可用空間記進環境快照。
  - child 因磁碟滿（ENOSPC 一類）結束時，VERDICT 的 `reason=` 寫明原因。新字彙，屬 MINOR。
  - `train status` 列出 run 輸出目錄裡殘留的 `*.tmp`。
- 處置（2026-10-30，0.16.0，分支 `feat/v0160-selection-disk`）：三項都做，另加兩項。
  - `train run --min-free-gib`（預設 10 GiB，`0` 只記錄）：第一次寫入前量 run 目錄、`--cwd`、checkpoint glob 前綴所在的磁碟區，不足 → ABORT `low_disk:`；量測記進 `env.<n>.json` 的 `disk`。
  - child 失敗後的 `reason=`：console 最後 64 KiB 有磁碟滿字樣 → `disk_full:`；否則重量的空間低於門檻 → `low_disk:`（只說可能滿過）。這次的失敗後約剩 2.2 GB、console 只有 `ios_base::badbit`，靠後者報出（controller 裁決 R2：固定 1 GiB 的下限會漏掉它）。
  - `train status` 的 `tmp=`：run 目錄（遞迴）與已登記 checkpoint 所在目錄（不遞迴）的 `*.tmp`，只列不刪。
  - 加做：vcp 自己的 yaml 重寫改成原子替換（裁決 R3）；`train run` 第一次寫入之後 vcp 自己寫不下 → 停掉 child、ABORT `disk_full:`（`disk_evidence=vcp`，裁決 C3）。
  - 設計見 `../specs/2026-10-30-vcp-0160-selection-and-disk-design.md` §5，實作裁決與待辦見 `2026-10-30-vcp-0160-selection-and-disk-followups.md`。

## 4. 【功能，P3】安全的清理工具 `vcp gc --dry-run`

- 經過：清磁碟時，是手動確認設定、`run.yaml`、台帳與文件都沒有引用之後，才由人親手刪掉 8.9 GB 的重複 bundle。
- 做法（12/6 之後；新命令，MINOR；先設計）：列出大型檔案與目錄，以及每一個被誰引用（run 紀錄、台帳、產物 manifest、backup 清單、provenance 索引），並寫一份稽核紀錄。只列不刪，刪除仍由人執行。引用關係可以從 provenance 圖取得。

## 5. 【量測】real RSNA 重量：建議不單獨做（待使用者決定）

- real RSNA v2 已在 2026-10-18–19 量過（`cafeea2`，當時的唯讀 metadata 複本：104 個 run、13,421 entities）。結果在 PR #44 合併，寫進 `docs/benchmarks/postgres-provenance-v2.md`。
- 比賽收尾後，root 已凍結（112 個 run）。
  - 重量的好處：一組最終圖的數字；r3→r4 多量一次。它是唯一接近打平點的真實轉換，9 月與 10 月的勝負相反。
  - 壞處：新數字可能因雜訊和 10/25 報告已用的數字對不上，要解釋以哪組為準；證據寫了就不能改。
  - 圖只有約 1.3 萬個 entity，磁碟不是問題。
- 建議：不單獨重量。要做的話，改成 E5 的「真實資料參考點」附錄：只描述、不判定，量測前登記，排在 E5 量完之後，不改 10/25 已交的數字。

## 6. 【文件】

- 6a（可選，10/24 前，不進 repo）：一頁「真實世界使用」素材給進度報告。數字先用唯讀命令查核。
- 6b（11/8 前；2026-10-30 寫成 `docs/postmortems/2026-10-rsna-knee-abnormality-detection.md`）：RSNA 賽後報告，放 `docs/postmortems/`。內容包括：
  - 「最終選取是手動的」這條路徑；
  - §1–§3 的實戰教訓與規模數字。
  - 它也是 1.0 路線圖的一步，以及投影片「真實世界使用」的素材。依慣例不放本機路徑與比賽資料。

## 7. 不碰的東西

- 比賽的 raw 資料；已鎖定的提交台帳與既有紀錄（歷史一律不改寫）。
- 任何憑證檔：只用路徑或 service name，不讀內容。
