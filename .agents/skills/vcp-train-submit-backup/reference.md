# 訓練、提交、備份參考

## `vcp train`
| 命令 | 必填 | 其餘 |
|---|---|---|
| `train run` | `--run --dataset --plan` + `-- COMMAND…` | `--export`（可重複；推 `trained_on`）或 `--trained-on`、`--venv`、`--config`（hash + 複製）、`--seed`、`--framework`、`--cwd`、`--checkpoints <glob>`（可重複，相對 `--cwd`）、`--final <glob>`（恰一個檔）、`--upload remote:path|dir`、`--resume`、`--notes` |
| `train upload` | `--run --dest` | `--only final` |
| `train status` | `--run` | `--verify`（重算 sha）；唯讀 |

留下的檔：`runs/<run>/run.yaml`（開始就寫）、`train.yaml`（每事件整份重寫；`attempts[]`、`checkpoints[]`）、`train.log.jsonl`（只增）、`train/config.N.json`、`train/env.N.json`（套件版本、`vcp_version` build string、`git.commit`、`git.dirty`）、`train/console.N.log`。checkpoint 不搬動，只記路徑與 sha；`--upload` 的副本另記 sha 與驗證結果（rclone `copyto --checksum`）。

## `vcp submit`
| 命令 | 必填 | 其餘 |
|---|---|---|
| `submit init` | `--dataset`（test）`--eval-dataset --plan --sealed --platform manual|kaggle --metric` | `--competition`、`--kind file|kernel`、`--board-rule last|best`、`--slots`、`--quota`、`--day-tz`、`--day-start`、`--display-tz`、`--deadline <UTC>`、`--params`、`--writer`、`--writer-opt`、`--kaggle-command`、`--test-plan`（`all-v1`）、`--test-subset`（`test`） |
| `submit stage` | `--dataset --id`（< 32 字）`--eval-run` | `--test-run`（file）、`--kind candidate|baseline|probe`、`--reason`、`--kernel --version --output --weights RUN[:sha]`（kernel）、`--writer-opt`、`--plugin` |
| `submit verify` | `--dataset --id` | |
| `submit upload` | `--dataset --id` | `--message`；VERDICT `confirmed=` `platform_ref=` `detail=`（CLI 沒確認就回讀列表：描述以 id 開頭 + 上傳前後 2 分鐘；`readback=` 記結果） |
| `submit record` | `--dataset --id --at "YYYY-MM-DD HH:MM"` | `--tz platform|utc`、`--platform-ref` |
| `submit score` / `sync` | `--dataset` (+ `--id --public/--private`) | sync 把別人的發記成 `foreign`，照數配額 |
| `submit final` | `--dataset` | `--slots`、`--dry-run` |
| `submit lock` / `unlock` | `--dataset --reason` | |
| `submit status` / `report` | `--dataset` | 唯讀 |

四道門（stage）：封槍 / 截止 → eval-test 配對（單模比 `weights_hash`，融合比 method / params / 成員遞迴）→ 準入判決（candidate 要 PASS；融合每位成員 PASS）→ 產檔。`Staged.provenance` 記候選等級，低於 `submit.yaml` 的 `require_provenance` → `provenance_required:`。輸出在 `submit/<test>/<id>/`（寫一次不改）；台帳 `submissions.jsonl` 列：staged / uploaded / scored / foreign / final / lock / unlock。`final` 依 sealed 讀數選 `final_slots` 個（同分看 public、再看 staged 時間），`board_rule=last` 時照 `needs_reupload` 重傳。

## `vcp backup`
| 命令 | 必填 | 其餘 |
|---|---|---|
| `backup manifest` | `--dataset --conclusion submission:<id>|judgement:<prereg>|run:<id>|all` | `--id`（預設 `<conclusion>-<UTC>`） |
| `backup push` | `--dataset --manifest --dest` | `--tier 1|2|3`（累進）、`--forget-remote` |
| `backup verify` | `--dataset --manifest` | `--dest`、`--tier`（只限副本層） |
| `backup pull` | `--dataset --manifest --dest` | `--tier`、`--overwrite`（舊檔留 `.bak-<時戳>`） |
| `backup status` | `--dataset` | 唯讀；`rclone_conf=present|absent|unknown` |

Tier 1 決策層（台帳、卡、判決、預登記、配方、快照、候選檔；KB）、tier 2 重現層（預測檔、樣本、`train/`、logs；MB）、tier 3 權重層（GB，只在要求時）。目的地佈局 `<dest>/data|configs|external/<相對路徑>`；`train upload` 驗過的權重記成 `remote_copy`，verify 到原地驗、不重推。verify 三層：副本（要 `--dest`；`absent=` 是清單時已缺）、本機一致性（卡 ↔ 預測、`train.yaml` ↔ checkpoint、`fuse.json` ↔ 成員、`stage.json` ↔ 候選檔）、時戳。`verified` 要有一次連 tier 3 都過的 verify。

## 機器回收前與賽後重建
```bash
uv run vcp backup manifest --dataset D-test --conclusion submission:SUB34 --id sub34
uv run vcp backup push … --tier 1 ; … --tier 2 ; （有空）manifest --conclusion all → push --tier 3 --forget-remote
# 重建：git pull → backup pull --tier 2 → backup verify → submit verify --id SUB34
```
另外保存：原始資料重新取得方法、Git commit / source bundle、專案程式、環境鎖定檔、notebook bundle。
