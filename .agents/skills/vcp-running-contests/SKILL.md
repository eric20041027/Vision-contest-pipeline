---
name: vcp-running-contests
description: Use when starting, resuming, operating, auditing, handing off, or explaining an end-to-end vision contest in the vcp repository, including first-time setup, deciding the next lifecycle step, and coordinating work between a user and an agent.
---

# 用 vcp 跑完整場影像比賽

## 先建立正確心智模型

`vcp` 管的是流程、身分、證據與治理；模型、訓練迴圈、推論程式和比賽格式放在 `projects/<contest>/`。目標不是只產出一個 submission，而是能證明它來自哪份資料、哪個模型、哪次判決，並能在另一台機器恢復。

操作前依序讀 `AGENTS.md`、`docs/handover/HANDOVER.md`、`README.md`、該比賽的 `projects/<contest>/RUNBOOK.md`（若存在）。修改框架時再讀該層 spec 與 followups。README 與 `uv run vcp <group> <command> --help` 是參數權威，不憑記憶猜選項。

新比賽必須再用 **vcp-contest-onboarding**；資料匯入、稽核、切分、匯出或 WARN/FAIL/ABORT 排查必須再用 **vcp-data-pipeline**；只有新增通用形態才用 **vcp-extend-registry**。

## 每次接手先判定狀態

若使用者只要求介紹、審查或規劃，保持唯讀：讀檔案與台帳，清楚標示「文件記載」「本次核對」「未驗證」，不執行命令或改 RUNBOOK。操作任務則：

1. 檢查 Git、資料根與設定根；讀現有 runbook、card、plan、預登記、配方、提交與備份紀錄。
2. 跑適用的唯讀狀態命令：`eval status/report`、`train status`、`submit status/report`、`backup status`；第三方工具本身不會產生 vcp VERDICT。
3. 將「文件說已做」與實際檔案、台帳、VERDICT 分開；待執行命令不是完成證據。
4. 回報目前階段、已驗證證據、阻塞、下一個具體動作，再繼續所有已授權且不受阻的工作。

完整階段、命令地圖與產物見 [workflow.md](workflow.md)。第一次上手、親自操作、委派 agent 與介紹專案的格式見 [operator-guide.md](operator-guide.md)。

## 固定生命週期

依序走：**規則與來源 → 匯入/驗證/稽核 → 固定切分 → materialize/export → baseline 訓練 → ingest/measure/anchor → 候選預登記/measure/judge → 融合消融準入 → file test 推論或 kernel 實跑 → stage/verify → upload/record + sync/score → sealed final → submission manifest/verify**。訓練、重要判決與提交後都要建立當下適用的備份或已驗證 checkpoint 副本，不把備份全拖到最後。

不能倒置的關係：audit 先於使用 audit group 的 split；baseline 先 measure/anchor，再開始候選；候選與融合完整配方量測前先 preregister；candidate 只有 `verdict=PASS` 才準入；file/kernel 證據先備妥，stage/verify 先於外部提交；至少一個候選或 baseline 真實 uploaded/recorded 後，才進 sealed 最終窗口；真實結論存在後才以它建立備份清單。

## 一步一證據的操作迴圈

每個命令都做同一個閉環：

1. 說明此步要驗證的假設及預期產物。
2. 執行命令並讀最後的 `VERDICT`；需要機器處理時加 `--json`，stdout 只解析 JSON，stderr 讀 VERDICT。
3. `OK/WARN` exit 0、`FAIL` exit 1、`ABORT` exit 2。WARN 要讀欄位後裁決；FAIL 修資料或不合格行為；ABORT 修環境、計畫或程式。
4. `eval judge status=OK` 只代表判決完成，準入看 `verdict=PASS|FAIL|INVALID`；自動閘門用 `--strict`。
5. 核對產物及其 sha/台帳列，將真實結果、決定、下一步與待續條件寫進比賽 RUNBOOK。

## 絕不跨越的界線

- `raw/` 不修改；台帳只能由正式操作追加，不手改、刪列或重寫；plan、預登記、融合配方與 backup manifest 寫後不改，改設計就換 id。
- 不事後預登記、不用 sealed holdout 挑模型、不把 FAIL/INVALID 說成通過、不把尚未執行的外部步驟說成完成。
- 核心 venv 只跑 vcp；訓練框架用獨立 venv 並 editable 安裝 vcp；量測環境凍結後不 install。
- 不讓 `src/vcp` 出現比賽名；比賽專屬輸入轉換、metric、writer、模型與 notebook 都放 `projects/<contest>/`。
- vcp 不讀寫平台憑證。不要要求使用者貼 token，也不要將 token 放入設定、命令、log 或台帳；平台 CLI 自己取憑證。
- 外部上傳、正式提交、sealed 解封、超出既定算力/費用、`--forget-remote` 或其他不可逆動作，僅在既有授權涵蓋時執行。若未涵蓋，先完成 stage、verify、dry-run 等可審查結果，再提出一個具體決定點。

## 常見失敗

- 一看到缺資料就停止整個任務：先完成文件、環境、規則、現況與命令模板盤點，只把依賴缺口的步驟列為阻塞。
- 只報命令成功：同時報 VERDICT、領域判決、持久產物和下一步。
- 直接上傳既有 CSV 再補紀錄：先釐清來源；無法證明的 legacy artifact 只能如實標示，不能冒充 candidate。只使用目前 schema/`--help` 接受的 `candidate|baseline|probe`，不可自行發明 legacy/emergency 類別或豁免。
- 把 RUNBOOK 當執行紀錄：逐項以台帳、檔案、sha、平台回讀與 VERDICT 驗證。
- 反覆向使用者確認已授權事項：授權會延續；只在新的外部承諾、sealed 最終窗口或資源上限改變時提出決定點。
