---
name: vcp-contest-onboarding
description: Use on day 1 of a new image contest (Kaggle, AIdea, any organiser download) when setting the contest up in this repo — deciding where raw data, contest scripts and dataset cards go, recording provenance, naming datasets and split plans, and what to commit for a teammate.
---

# 新比賽 Day 1 建置

## 核心原則
框架以資料形態為軸；一場比賽只在三個地方留下東西：`VCP_DATA_ROOT/raw/<name>/`（原始下載，永不修改，不進 git）、`configs/datasets/<name>/`（card 與 plan，進 git）、`projects/<contest>/`（比賽專屬腳本與 README，進 git）。`src/vcp` 不因比賽而改；格式沒人吃就寫轉換腳本走 `jsonl` 匯入器，真的缺一種形態才用 vcp-extend-registry。
命令與判決的細節在 vcp-data-pipeline，這裡只定順序與決定。

## 檢查清單（照順序）
1. **讀組織方文件**：授權、來源 URL、格式、評分方式、test 有無標籤。記下你「實際下載的 UTC 日期」（`--downloaded-at` 用這個，不是文件上的日期）。
2. **環境**：`uv sync`（DICOM 加 `--extra dicom`）；Windows 沒有 symlink 權限是常態，匯出會自動複製。
3. **放資料**：組織方下載複製到 `<VCP_DATA_ROOT>/raw/<name>/`，之後不再動它（card 會存相對路徑，跨機器可用）。太大搬不動時 `--src` 直接指向原位置也合法（card 存絕對路徑），Kaggle notebook 端就是這樣用。
4. **命名**：資料集名 = 比賽 slug 的 kebab-case（`beach-trash`）；無標註 test 集 `<name>-test`；plan id 用 `fixed-v1`，改切分就 `fixed-v2`（plan 寫入後不可改）。
5. **建 `projects/<contest>/`**：`README.md`（重現命令、資料品質發現、決定與理由）+ 轉換腳本（例如 `classes.txt` → categories JSON、比賽格式 → `samples.jsonl`）。腳本要有 docstring 與可重跑的命令列。
6. **匯入訓練集**：帶 `--license`、`--url`、`--downloaded-at`；壞列先 `on_bad_row=skip` 看清楚再決定；`--notes` 記下壞列數與處置。真實比賽的標註錯誤要回報主辦方，回報紀錄寫進 README。
7. **匯入 test 集**（無標註）：jsonl 食譜、`label_source: none`，不要用空 CSV 假裝成 gold 負樣本。
8. **稽核**：`audit --against <name>-test`；overlap 與近重複數字寫進 README；壞列數用 `--max-bad-boxes` 明確承認。
9. **切分**：`split --plan-id fixed-v1 --group-from-audit`。預設四子集（train / valA / valB / holdout sealed）需要每個子集都撐得起評估；樣本少（每類不到幾十張）就 `--subsets train:train:0.8,val:eval:0.2` 並在 README 註明理由。
10. **驗證**：`validate` 與 `lineage --trained-on train`，確認 git 裡的 card + plan 自洽。
11. **commit**：`configs/datasets/<name>/dataset.yaml`、`configs/datasets/<name>/splits/fixed-v1.json`、`configs/datasets/<name>-test/dataset.yaml`（test 集的 card 也進 git）、`projects/<contest>/`。訊息 `feat(<contest>): 資料集卡、fixed-v1 切分與匯入筆記`。不 commit 任何 `datasets/`、`raw/`、匯出目錄。
    正常作業不要設 `VCP_CONFIGS_ROOT`：它預設就是 repo 的 `configs/`，card 與 plan 會直接落在 git 追蹤的位置。若為了隔離把它指到別處（試跑、測試），commit 前要把 `configs/datasets/<name>/` 整個目錄複製回 repo。

## 判斷準則
- **要不要複製 raw 進資料根目錄**：能放就放（可攜 card、`raw_manifest.txt` 完整）；超過磁碟或已在雲端掛載就 `--src` 原位置。
- **要不要寫腳本**：會重跑第二次的轉換就寫成腳本；只有一次的（4 個類別）inline JSON 即可，但把命令記進 README。
- **holdout 開封**：只在最終決策前開一次，`--unseal --reason`，理由寫清楚；平時所有量測用 valA / valB。
- **Kaggle**：先接受規則；下載用 `kaggle competitions download -f <檔>`；大量小檔會撞 API 配額，超過幾千個檔案改在 Kaggle notebook 端打包或 materialize 後整包下載。

## 常見錯誤
- 把 `--downloaded-at` 填成組織方文件上的日期；它是你這份 raw 的取得時間。
- 手改 plan 檔或 card；要改就重匯入 / 新 plan id。
- 把 test 集留在框架外面（之後 audit 就查不到與 train 的重疊）。
- 把比賽專屬欄名或流程塞進 `src/vcp`。
