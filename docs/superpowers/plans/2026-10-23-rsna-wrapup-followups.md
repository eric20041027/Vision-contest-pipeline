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
| 11/3–11/10 | E5 量測；§6 的 6b（只寫文件） | — |
| 11/12 前 | E5 交付 | — |
| 12/6 之後 | §1 的 1b、§2、§3（0.16.0）；§4；Wave 1c；1.0 | MINOR |

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

## 2. 【缺口，P2】平台上實際勾選的最終提交沒有記錄、也沒有核對

- 經過：這場比賽的最終選取是在 Kaggle 網頁上手動勾的。這次勾錯了兩發（勾成九月的舊提交），vcp 沒有察覺：台帳的 current 和實際勾選不同。
- 現況：`submit final` 依 sealed 讀數挑選並封槍，記的是 vcp 的建議，不是平台上實際的選取。暫時的做法是用 `note` 事件手記實際勾選。
- 做法（0.16.0，MINOR，先寫 spec）：
  - 新的台帳事件記錄實際勾選的 id，指令例如 `submit select --record <ids>`。舊 vcp 讀到新事件會 FAIL，讀寫同一份台帳的都要先升級（同 0.14.0 的 `errored`）。
  - `submit status`：截止前 N 小時（N 可設定）還沒記錄選取、選取數和名額不同、或選取和 `final` 不同時給 WARN。
  - 調查 Kaggle API 能不能讀到 selected 旗標。CLI 的 CSV 只有 ref、fileName、date、description、status、publicScore、privateScore。讀得到就由 `sync` 自動核對。

## 3. 【韌性，P2】訓練中磁碟滿

- 經過：存 resume 檔時 `torch.save` 失敗（`ios_base::badbit`），留下一個 266 MB 的 `.tmp` 殘檔；接續成第 2 次 attempt 才完成。
- 做法（0.16.0）：
  - `train run` 啟動前檢查輸出目錄所在磁碟的可用空間（門檻可設定），不足就 ABORT；可用空間記進環境快照。
  - child 因磁碟滿（ENOSPC 一類）結束時，VERDICT 的 `reason=` 寫明原因。新字彙，屬 MINOR。
  - `train status` 列出 run 輸出目錄裡殘留的 `*.tmp`。

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
- 6b（11/8 前）：RSNA 賽後報告，放 `docs/postmortems/`。內容包括：
  - 「最終選取是手動的」這條路徑；
  - §1–§3 的實戰教訓與規模數字。
  - 它也是 1.0 路線圖的一步，以及投影片「真實世界使用」的素材。依慣例不放本機路徑與比賽資料。

## 7. 不碰的東西

- 比賽的 raw 資料；已鎖定的提交台帳與既有紀錄（歷史一律不改寫）。
- 任何憑證檔：只用路徑或 service name，不讀內容。
