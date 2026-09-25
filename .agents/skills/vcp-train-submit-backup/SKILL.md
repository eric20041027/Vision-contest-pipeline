---
name: vcp-train-submit-backup
description: Use when wrapping a training command with vcp train run, registering or uploading checkpoints, reading vcp train status, staging / verifying / uploading / recording / scoring / finalising a submission with vcp submit, or building, pushing, verifying and pulling backup manifests with vcp backup — including file vs kernel submissions, submission kinds, backup tiers, and what must not be done without authorisation.
---

# 訓練、提交、備份

## 核心原則
- vcp 不定義模型：`vcp train run -- <任何命令>` 只負責記命令、環境、config、console、checkpoint sha 與副本。訓練程式在專案 venv 跑，`--venv` 指專案 `.venv`，Windows 用絕對 interpreter。
- 候選 = (eval 側 run, test 側 run) 一對，身分靠 `weights_hash`（`eval ingest --weights`）或融合配方；`candidate` 必須有新鮮的 `verdict=PASS`。
- 每一步都是「先可審查、再不可逆」：`stage` → `verify` → `upload/record` → `sync/score` → `final --dry-run` → `final`。
- vcp 不碰平台憑證；`kaggle` CLI 自己取。不要要求使用者貼 token，也不寫進命令、log、台帳。
- 選項以 `--help` 為準；表格與恢復順序見 [reference.md](reference.md)。

## 訓練
```bash
uv run vcp train run --run R --dataset D --plan fixed-v1 --export <export dir> --venv projects/<c>/.venv --seed 42 \
  --framework "…" --cwd projects/<c> --checkpoints "work/R/*.pt" --final "work/R/best.pt" [--upload DEST] -- <訓練命令>
uv run vcp train status --run R --verify
```
- 訓練迴圈用 `MaterializedReader(...)` 或 `Session.current().access(subsets={"train"})` 讀資料才有 access receipt；`Session.current().register_checkpoint(path, final=True)`、`s.note(k, v)` 在 `train run` 底下才可用。
- `train status`：`backed=0` 才是有副本；`unbacked=` 是目前 bytes 沒副本（WARN，不是壞）；`superseded=` 同路徑被後來的登記取代且從沒上傳；`drift=` 檔案與登記 sha 不符（壞）。`attempts[-1]` 是最新一次執行。
- `--resume` 加 attempt；`train upload --run R --dest …` 冪等，`--only final` 只傳最終權重。
- 開訓前先 commit 專案程式：`train/env.N.json` 記 `git.commit` 與 `dirty`，髒樹會如實寫 `dirty: true`。

## 提交
```bash
uv run vcp submit init --dataset D-test --eval-dataset D --plan fixed-v1 --sealed holdout --platform kaggle|manual --metric M --deadline 2026-10-22T23:59:00Z
uv run vcp eval ingest --run good.test --dataset D-test --plan all-v1 --subset test --format scores_csv --src preds.csv --weights <best.pt>
uv run vcp submit stage --dataset D-test --id SUB34 --eval-run good --test-run good.test      # 四道門：封槍/截止、eval-test 配對、準入判決、產檔
uv run vcp submit verify --dataset D-test --id SUB34                                          # 重 hash + 從 test run 重產，位元級
uv run vcp submit upload --dataset D-test --id SUB34            # 手動平台：record --at "YYYY-MM-DD HH:MM"
uv run vcp submit sync --dataset D-test                          # 手動平台：score --public …
uv run vcp eval measure --run good --subsets holdout --unseal --reason "final pick"
uv run vcp submit final --dataset D-test --dry-run && uv run vcp submit final --dataset D-test  # 自動封槍
```
kind 只有 `candidate | baseline | probe`：baseline / probe 要 `--reason`，admission 記 waived；probe 永不進 final。不要發明 legacy / emergency。任何一道門不過，`stage` 就 FAIL 且不寫任何檔或台帳列。使用者說「判決 FAIL 沒關係，先丟上去看分數」時：不能當 candidate；可以提議 `--kind probe --reason "…"`（留紀錄、佔配額、不進 final）或 `--kind baseline`（可進 final 但不是準入候選），由使用者選；上傳本身仍要授權。kernel 提交用 `--kernel user/notebook --version N --output file --weights RUN[:sha]`，要先在授權內拿到真正成功的 notebook 版本。

## 備份
```bash
uv run vcp backup manifest --dataset D-test --conclusion submission:SUB34 --id sub34   # 從結論反向走證據圖，寫進 git
uv run vcp backup push --dataset D-test --manifest sub34 --dest <rclone remote:path|dir> --tier 1   # 先決策層
uv run vcp backup push … --tier 2 ; … --tier 3                                                    # 重現層、權重層
uv run vcp backup verify --dataset D-test --manifest sub34 --dest … --tier 2
uv run vcp backup status --dataset D-test
```
里程碑就備份：訓練完 `train upload` 或 `run:<id>` 清單；重要判決 `judgement:<prereg>`；真實提交 `submission:<id>`。`cache/`、`raw/` 永不進清單；C 槽副本不是異機備份；`--forget-remote` 要整份清單在目的地驗過。

## 停在決定點的事
外部上傳、正式提交、sealed 解封、`unlock`、`--overwrite`、`--forget-remote`、超出算力／費用上限——只在既有授權涵蓋時做；否則先做完 stage / verify / dry-run，再提出一個具體決定點。已授權的事不反覆確認。

## 常見錯誤
- 把 RUNBOOK 的命令當已完成；staged ≠ uploaded；local `remote_copy` ≠ 異機備份；`rclone_conf=unknown` ≠ absent。
- `submit upload` 的 WARN `confirmed=false`：列已經寫了，但不代表上了。重傳之前先到平台看這個 id 在 `at` 前後有沒有一發——有就是上了，重傳會再吃一發配額；`submit sync` 之後會配對（`unconfirmed=` 以 id 為單位，只對第一次上傳的 id 才代表沒上）。CLI 回 0 卻說 `Could not submit to competition` 會是 FAIL `upload_failed:`、不寫列，可以直接重傳。
- 直接上傳既有 CSV 再補紀錄：先釐清來源，無法證明身分的檔只能如實標示。
- 在 main checkout 的 venv 啟動訓練：用比賽釘版 worktree 的 `vcp.exe`（見 `vcp-release-and-environments`）。
