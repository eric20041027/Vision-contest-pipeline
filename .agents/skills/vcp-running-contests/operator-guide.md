# 第一次上手、親自操作與 agent 委派

## 用一句話介紹

vcp 是影像比賽的流程與證據管理框架：它不取代 PyTorch、Ultralytics 或 Kaggle，而是把資料、訓練、量測、融合、提交和備份綁成可追蹤、可判決、可恢復的一條鏈。

對新使用者介紹時依序說明：

1. 它降低 public→private 掉分、單一驗證集過擬合、模型/提交身分混淆與機器回收後無法復原的風險，並保留可稽核證據。
2. 六層各自的輸入、輸出和閘門；特別說明 `judge status=OK` 與 `verdict=PASS` 的差異。
3. 模型與比賽膠水由 `projects/<contest>/` 提供，vcp 負責治理和證據。
4. 一次比賽的固定順序、第一次跑通的最小路徑、sealed holdout 和外部提交的界線。
5. 展示該比賽 RUNBOOK 的「已完成 / 待完成」，不要把範例命令當成真實完成紀錄。

## 第一次使用的最短路徑

1. 安裝 uv 與核心環境：`uv sync`；DICOM 用 `uv sync --extra dicom`。確認 `uv run vcp --help`。
2. 設定 `VCP_DATA_ROOT`（預設 Windows `C:/vcp-data`、Linux `~/vcp-data`）；通常不設定 `VCP_CONFIGS_ROOT`，讓 configs 直接進 repo。
3. 選一個小型、可在當天完成的 baseline；先跑 `vcp-contest-onboarding` 的 Day 1 流程。
4. 跑資料層到固定 plan 與 materialize/export；確認 train、兩個 eval、sealed 的 group 不洩漏。
5. 在 `projects/<contest>/.venv` 建訓練環境並 editable 安裝 vcp；以 `vcp train run -- ...` 包住專案訓練命令。
6. 先把 baseline 的 valA/valB 預測 ingest、measure、anchor。要驗候選時先 preregister 再 measure/judge；只跑通最小流程時可把 baseline 以 `--kind baseline --reason` 明確豁免，不能稱為 PASS candidate。
7. file submission 為同一權重產生並 ingest test run；kernel submission 先在授權範圍內取得真正成功的 notebook version/output，再綁 notebook/version/weights。完成 submit init、stage、verify 後才 upload/record，再 sync/score。probe 要寫理由且不進 final。
8. 最後候選固定後才解封 sealed、final；以實際 submission 或 judgement 建 manifest，推送並 verify。

第一次的成功標準是整條證據鏈跑通，不是追求最高分。先用小資料、小 epoch、單一 baseline；流程穩定後才擴大訓練與融合。

## 本人親自使用

- 每次先讀 RUNBOOK 的最後狀態，再用各層 status/report 核對；純介紹/審查任務則保持唯讀並標示哪些只是歷史紀錄。
- 用 `uv run vcp <group> <command> --help` 組命令；先在小資料執行，讀最後 VERDICT 與產物。
- 人負責比賽規則、資料權利、評分目標、GPU/費用預算、sealed 最終窗口、平台提交與備份目的地。
- 把每個新 id、實際命令、VERDICT、讀數、判決及平台結果寫回 RUNBOOK；失敗也保留。
- 平台憑證只交給官方 CLI 的安全設定。不要貼進聊天、repo 或 vcp 選項。
- Windows 的訓練/推論用專案 venv 的絕對 interpreter；`train.yaml` 的最新執行看 `attempts[-1]`。`rclone_conf=unknown` 不是 absent，本機 `remote_copy` 也不是異機備份。

## 使用 agent 幫忙

一次給足以下內容：比賽規則網址、raw 位置、任務/metric、運算與時間上限、資料與設定根、備份目的地、哪些外部上傳/正式提交已授權、sealed 是否仍封存。缺的欄位可標「待確認」，agent 應先完成不依賴它們的工作。

可直接委派：

```text
請在此 vcp repo 接手 <contest>，使用 vcp-running-contests skill。

規則與來源：<URL / license / downloaded-at>
raw 與 train/test：<paths>
任務、官方 metric、submission：<known details>
同源分組單位：<patient/study/video/source>
機器、GPU、時間、費用上限：<budget>
data/config roots：<paths or defaults>
備份目的地：<destination or pending>
已授權外部操作：<exact destinations/actions; otherwise pending>
sealed：保持封存，除非我已明確開啟最後評估窗口。

先讀 AGENTS.md、HANDOVER、README 與現有 RUNBOOK，核對實際台帳和檔案後回報目前階段。依固定生命週期完成所有已授權工作；比賽專屬程式只放 projects/<contest>/。每步保留 VERDICT、產物與決定，更新 RUNBOOK。不要事後預登記、改不可變紀錄、用 sealed 挑模型、記錄憑證或把待執行命令說成已完成。外部提交、解封、超預算或未授權目的地前，先做完可審查的 stage/verify/dry-run，再提出一個具體決定點。
```

## agent 應如何回報與交接

進行中回報只說新資訊：目前階段、剛驗證的證據、下一步會解除哪個不確定性。完成回報必須包含：

- 已完成到哪一層，以及每層的主要 VERDICT / `verdict=`。
- 新增的 dataset/plan/run/prereg/recipe/submission/manifest id。
- 測試、ruff、真資料 smoke test 的結果（若修改程式）。
- Git branch、commit、是否已合併與 push。
- 未完成的真實外部里程碑；需要使用者提供的值或授權。
- RUNBOOK 與重要產物的路徑，以及一個確切的續跑命令。

## 何時讓 agent 停在決定點

只有下一步真的依賴以下事項時才停：規則或資料權利不明、必要資料缺失、會超出既定資源、未授權的外傳/正式提交、sealed 最終解封、解鎖/覆寫/忘記遠端等不可逆變更。已授權的步驟持續完成，不反覆確認；某層受阻時繼續其他獨立且有價值的工作。
