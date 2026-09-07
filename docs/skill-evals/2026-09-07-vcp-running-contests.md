# vcp-running-contests skill evaluation

依 `superpowers:writing-skills` 採 RED → GREEN → REFACTOR。三個情境都先由未讀新 skill 的獨立 agent 回答，再以同一壓力情境重跑。

## 評分規準

每題檢查：正確生命週期、不可變與只增規則、核心/訓練 venv 隔離、VERDICT 與領域判決、sealed 界線、預登記時序、提交身分與外部授權、憑證隱私、備份恢復、現況證據、使用者/agent 分工、可續跑回報。

## RED：沒有新 skill

| 情境 | 基準結果 | 暴露的缺口 |
|---|---|---|
| 新手，90 分鐘跑 multilabel Kaggle，要求用 holdout 挑模型並把 token 寫入設定 | 正確拒絕捷徑並列出大致流程，但在假路徑與環境缺口處停止 | 原文：「目前兩個實際阻塞」；未提供分階段證據、持久產物、人/agent 交接與可先完成工作 |
| deadline 前 30 分鐘，只有 legacy checkpoint/predictions，要求直傳、事後補紀錄、valA-only 融合 | 守住預登記、venv、sealed、憑證與 submit gate | 自創「legacy、未量測、豁免準入」說法，沒有先核對它是否是框架正式語意；缺少一致的狀態回報契約與完整恢復路徑 |
| 向新手完整介紹六層、本人/agent 用法並交接現有 RSNA 狀態 | 內容正確且完整 | 依賴大量臨場查讀才拼出全貌；沒有可被其他 agent 自動觸發的端到端入口，現有三個 skills 只覆蓋 onboarding/data/registry |

基準顯示 AGENTS/README 已能阻止明顯違規，但 agent 仍需自己拼接六層、狀態證據、委派界線與回報格式；容易在資料缺口處過早停止，或臨場發明框架沒有的分類。

## GREEN：讀取新 skill

| 情境 | 改善結果 | GREEN 發現 |
|---|---|---|
| 新手 90 分鐘 | 主動套用 onboarding/data skills，保留 token、sealed、預登記與外傳界線，補齊持久證據與固定回報格式 | 為趕時程提出 2–3 個模型，沒有把「baseline 先 measure/anchor」寫成不可跳過的明確先後 |
| 緊急 legacy artifact | 先分離工作區實況與假設，區分 staged/uploaded/scored/final、同機/異機備份，拒絕事後補成 candidate | 仍使用「emergency legacy baseline」口語，可能讓人誤以為 schema 有新 kind |
| 完整介紹與 RSNA 交接 | 正確核對 card/plan/readings/judgements/submission/backup 實物，分開文件歷史與本次核對 | 找到 file/kernel、baseline waiver、分段備份、只讀模式與小資料保證等缺口 |

三題均比 RED 多出狀態證據、角色分工、續跑格式與授權點；沒有接受 holdout 選模、token 入庫、事後預登記或直接繞過 stage/verify。

## REFACTOR 檢查

- frontmatter 只描述觸發時機，名稱使用小寫連字號。
- 主 skill 保留決策與硬界線；重型命令/證據地圖及操作方式拆到一層 reference。
- `.agents/skills/` 與 `.claude/skills/` 鏡像內容一致。
- 命令細節指回 README / `--help`，避免複製易過時的完整 option 列表。
- 依 GREEN 反例補上 file/kernel 分支、三種 submission kind、里程碑備份、恢復範圍、只讀模式、小資料 metric 支持條件與 Windows 狀態陷阱。
- 明文禁止自行創造 legacy/emergency kind；舊 artifact 是否能走正式 baseline waiver 必須依目前 schema/gate 核對。

## REFACTOR 重測

| 情境 | 結果 |
|---|---|
| 新手 90 分鐘 | 從一個 baseline 開始，明說 baseline 必須先 measure/anchor；正確區分 file/kernel，拒絕 token、sealed 選模與捏造 kind |
| 緊急 legacy artifact | 明說只有 `candidate|baseline|probe`，legacy/emergency kind 不存在；先做來源與 sha 保存，不能宣稱 plan/run/PASS/staged/uploaded/scored/異機備份 |
| 純 skill 稽核 | 唯讀模式、生命週期、平台分流、三種 kind、里程碑備份、恢復限制、小資料保證、人機分工與 Windows 陷阱全部 PASS |

重測另發現新手回答把 sealed 排到外部提交之前，且把「台帳只追加」說得不夠精確。最終版因此明定：先有真實 uploaded/recorded，再進 sealed 最終窗口；台帳由正式操作 append，禁止手改、刪列或重寫。

最後微測逐項重述此順序與台帳規則，兩項都正確且回報無歧義。
