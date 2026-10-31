# 訓練、提交、備份參考

## `vcp train`
| 命令 | 必填 | 其餘 |
|---|---|---|
| `train run` | `--run --dataset --plan` + `-- COMMAND…` | `--export`（可重複；推 `trained_on`）或 `--trained-on`、`--venv`、`--config`（hash + 複製）、`--seed`、`--framework`、`--cwd`、`--checkpoints <glob>`（可重複，相對 `--cwd`）、`--final <glob>`（恰一個檔）、`--upload remote:path|dir`、`--resume`、`--notes`、`--min-free-gib G`（預設 10；低於 → ABORT `low_disk:`，`0` 只記錄；失敗後 console 有磁碟滿字樣 → FAIL `disk_full:`，空間低於門檻 → FAIL `low_disk:`；VERDICT `free_gib=`） |
| `train upload` | `--run --dest` | `--only final` |
| `train status` | `--run` | `--verify`（重算 sha）；唯讀；VERDICT 一律帶 `tmp=`（run 目錄遞迴、加上已登記 checkpoint 所在目錄不遞迴，名稱以 `.tmp` 結尾的檔；> 0 WARN，人看的行 `tmp: <path> (<bytes> bytes)`，`--json` 的 `tmp` 是 `[{path, bytes}]`；只列不刪，從沒登記過 checkpoint 的目錄看不到） |

磁碟（`train run`，0.16.0）：量 run 目錄、`--cwd`、每個 checkpoint / final glob 在第一個 `*?[` 之前的前綴所在磁碟區，每區量一次、取最小；最小可用空間 `< --min-free-gib`（GiB = 1024³ bytes，預設 10；非有限數或負數 → FAIL `invalid:`）→ ABORT `low_disk:`（`free_gib=` `min_free_gib=` `disk_path=`），在所有輸入檢查之後、第一次寫入之前，`--resume` 也一樣，一個檔都不寫。量不到 → WARN `disk_check=unavailable`。child 失敗（exit ≠ 0、不是中斷）後再量並讀 console 最後 64 KiB：字樣 `No space left on device`、`[Errno 28]`、`[WinError 112]`、`[WinError 39]`、`There is not enough space on the disk`、`Disk quota exceeded`（不分大小寫）→ FAIL `reason=disk_full:`、`disk_evidence=console`；否則空間低於門檻 → FAIL `reason=low_disk: … the disk may have filled`（只說可能，沒有 `disk_evidence=`）；`train.log.jsonl` 記 `note`（`key=disk_full|low_disk`、`marker`、`free_bytes`）。vcp 自己的紀錄第一次寫入之後寫不下（ENOSPC / EDQUOT / WinError 112、39）→ 停掉 child，ABORT `disk_full:`、`disk_evidence=vcp`，清出空間後 `--resume`；例外是 `--upload` 步驟中途寫不下：attempt 已經結束，VERDICT 的訊息改說 `free space, then: vcp train upload --run R --dest DEST`（冪等），不是 `--resume`；這個轉換只有 `train run` 做，其他寫 yaml 的命令的 `OSError` 不變。`free` 是 `shutil.disk_usage` 的磁碟區可用空間，不反映 Windows 配額。

留下的檔：`runs/<run>/run.yaml`（開始就寫）、`train.yaml`（每事件整份重寫；`attempts[]`、`checkpoints[]`；`run.yaml` 與 `train.yaml` 的重寫都是暫存檔 → fsync → replace，被殺時舊檔完整、可能多一個 `.<名>.<nonce>.tmp`，Windows 目標被別的程序開著時最多重試約 1.55 秒）、`train.log.jsonl`（只增）、`train/config.N.json`、`train/env.N.json`（套件版本、`vcp_version` build string、`disk`：啟動前量到的門檻、時戳與各磁碟區的 `path` `roles` `free_bytes` `total_bytes`、`git`：`commit`、`dirty`、追蹤檔改動與未追蹤的數量和路徑、status 與 diff 的 sha256）、`train/git.N.patch`（追蹤檔的未提交差異，只在有改動時，10 MiB 以內）、`train/console.N.log`。checkpoint 不搬動，只記路徑與 sha；`--upload` 的副本另記 sha 與驗證結果（rclone `copyto --checksum`）。

## `vcp submit`
| 命令 | 必填 | 其餘 |
|---|---|---|
| `submit init` | `--dataset`（test）`--eval-dataset --plan --sealed --platform manual|kaggle --metric` | `--competition`、`--kind file|kernel`、`--board-rule last|best`、`--slots`、`--quota`、`--day-tz`、`--day-start`、`--display-tz`、`--deadline <UTC>`、`--params`、`--writer`、`--writer-opt`、`--kaggle-command`、`--test-plan`（`all-v1`）、`--test-subset`（`test`）；沒有 `selection_warn_hours` 的選項：那是 `submit.yaml` 的鍵（數字 `>= 0` 或 `null`，預設 48，等於預設時不寫出），要改就在 git 裡改，寫了之後舊 vcp 載入 FAIL `extra_forbidden` |
| `submit stage` | `--dataset --id`（< 32 字）`--eval-run` | `--test-run`（file）、`--kind candidate|baseline|probe`、`--reason`、`--kernel --version --output --weights RUN[:sha]`（kernel）、`--writer-opt`、`--plugin` |
| `submit verify` | `--dataset --id` | |
| `submit upload` | `--dataset --id` | `--message`、`--force "<理由>"`（同 id 再傳才需要，否則 `already_uploaded:`；`final` 之後的重傳也要）、`--no-sync`（略過上傳前同步，WARN）；VERDICT `ledger=` `sync=` `bound=` `cli_warnings=`（一律印：上傳前同步加回讀看到的平台 CLI 警告行，算不同的行數，> 0 WARN；`--json` 只有數字）`forced=` `confirmed=` `platform_ref=` `detail=`（CLI 沒確認、或 CLI 非 0 時都回讀列表：描述以 id 開頭 + 上傳前後 2 分鐘，台帳已有的 ref 不算；`readback=` 記結果。CLI 非 0 而回讀對上 → 寫列、WARN `exit_code=`；沒列出 → FAIL `upload_failed:`；判斷不了 → FAIL `upload_unconfirmed:`；列表呼叫各 120 秒逾時，逾時結束整個程序樹，`uv tool` 之類的包裝程式也一併切斷）；同步之後才 FAIL 也帶 `sync=` `bound=` `cli_warnings=` |
| `submit record` | `--dataset --id --at "YYYY-MM-DD HH:MM"` | `--tz platform|utc`、`--platform-ref`（已經在某個 `uploaded` 列 → `exists:`） |
| `submit score` / `sync` | `--dataset` (+ `--id --public/--private`) | sync 把別人的發記成 `foreign`，照數配額；對上 id、平台做完卻沒有分數的一發（狀態最後一段 `error`，或 `complete` 而兩個分數都空）記成 `errored`：先綁定；同一個 ref 最新的結果列（`scored` 或 `errored`）跟這次一樣就不再寫，分數與沒分數之間來回時每一步都寫；VERDICT 一律帶 `errored=`（這次寫入的列數），大於 0 時 WARN，`--json` 的 `errored` 是 id 清單；`sync` 另一律帶 `cli_warnings=`（CLI 印在列表前的警告行，算不同的行數，> 0 WARN；人看的行 `cli_warning: <行>`，含 `outdated` 時多一行升級提示，`--json` 的 `cli_warnings` 是那些行；不寫台帳、每次各自報，CLI 不升級就每次 WARN） |
| `submit final` | `--dataset` | `--slots`、`--dry-run`；最新一發出錯的候選與基準不排名（`why=errored`，列在沒排名的清單裡）；`board_rule=best` 時已記的選取跟這次的 `chosen` 不同 → WARN `selection=differs`（沒有 `selected` 列或相同時不帶），並一律印 `next: select <chosen> on the platform, then: vcp submit select --dataset T --record <chosen>`（`--dry-run` 也印）；`last` 沒有這兩項；排名與 `final` 列不受選取影響 |
| `submit select` | `--dataset --record ID[,ID…]` | `--message`（只在寫列時用）；記平台上實際勾選的，從不呼叫平台，不受封槍與截止擋；項目是 submission id 或 `foreign:<ref>`；`not_staged:` / `not_uploaded:` / `not_found:`（皆 `id=`）/ `invalid:`（空或重複）都不寫列；同集合（不管順序）再記 → `written=false`；VERDICT `selected=` `written=` `selection=ok\|slots\|differs`（後兩者 WARN；`board_rule=last` 一律 `none`）`ledger=`；`--json` 的 result 是那一列加 `selection` |
| `submit lock` / `unlock` | `--dataset --reason` | |
| `submit status` / `report` | `--dataset` | 唯讀（不上鎖）；`status` 看每個 id 最新一發的結果：還沒出分 → `unscored=`，出錯 → `errored=`（人看的行 `errored: <id>`，不 WARN）；`report` 每列帶 `errored`，出錯那一發沒有 public／private，人看的行尾加 ` errored`；0.16.0 起 `status` 另一律帶 `selected=<n>\|none` 與 `selection=none\|ok\|missing\|slots\|differs`（後三者 WARN；核對只在 `board_rule=best`；`missing` = 截止前 `selection_warn_hours` 小時起還沒記，沒有截止時是 `final` 之後；`slots` = 記的個數不是 `min(名額, 可選條目數)`，名額取最新 `final` 列的 `slots`；`differs` = 跟 `final` 的集合不同；人看的行 `selection: …`，`--json` 的 `selection`）；`report` 每列帶 `selected`（標籤在最新 `selected` 列裡，同一個 id 的每一發都標，人看的行尾 ` selected`） |
| `submit ledger adopt` | `--dataset` | `--from PATH`（可重複；本 checkout 的 configs 台帳一定收）；只在 `ledger: shared` 時用；建共用正本的唯一方法，沒有舊台帳就建空的 |

四道門（stage）：封槍 / 截止 → eval-test 配對（單模比 `weights_hash`，融合比 method / params / 成員遞迴）→ 準入判決（candidate 要 PASS；融合每位成員 PASS）→ 產檔。`Staged.provenance` 記候選等級，低於 `submit.yaml` 的 `require_provenance` → `provenance_required:`。id 不能是台帳的名字 `submissions.jsonl`（`invalid:`）。輸出在 `submit/<test>/<id>/`（寫一次不改）；台帳 `submissions.jsonl` 列：staged / uploaded / scored / errored / foreign / final / selected / lock / unlock（`errored` 自 0.14.0 起、`selected` 自 0.16.0 起；舊版讀到會 FAIL `bad ledger row`，讀寫同一份台帳的 vcp 先一起升級；`selected` 只由人下 `select --record` 才寫，所以記第一筆之前 0.15 與 0.16 可以共用台帳）；`ledger: configs` 在 `configs/datasets/<test>/`，`ledger: shared` 在 `<data_root>/submit/<test>/`；寫入命令持有 `<data_root>/locks/submissions-<hash16>.lock`。`final` 依 sealed 讀數選 `final_slots` 個（同分看 public、再看 staged 時間），`board_rule=last` 而最後一發不是選中的就 WARN `needs_reupload=`，並印出重傳命令：Kaggle 是 `vcp submit upload --dataset T --id <選中> --force "final re-send"`（選中的 id 不受封槍擋，但它上傳過，要 `--force`），手動平台是網頁重傳後 `record`。

台帳的操作細節：
- 寫入命令與 `status` 成功（OK / WARN）時 VERDICT 帶 `ledger=`；失敗時只有 `not_adopted:`、`not_shared:` 帶。
- `upload` 從上傳前同步、平台上傳到回讀都拿著鎖：回讀找不到新的一筆（平台沒列出，或只看到台帳已有的 ref；CLI 回 0 的也一樣）時要等滿整個回讀約 17 秒才寫列或失敗，每次列表的 CLI 呼叫最多 120 秒；其他 checkout 的寫入者最多等 60 秒，之後 ABORT `locked:`，重跑即可。
- 寫到一半就當掉的寫入者會留下沒有換行的最後一列：唯讀命令略過它；寫入者在鎖內嚴格讀，殘列解析不了就 FAIL `bad ledger row`。處理：先確定沒有 vcp 寫入者在跑，刪掉那個殘列（它從沒被當成一列讀過）；有 provenance 索引就再跑 `vcp provenance rebuild`（檢查點可能記了殘列的位元組，截掉之後 `sync` 會 `prefix_drift:`）。
- `ledger: shared` 時，一個 checkout 跑 `vcp provenance rebuild` / `sync` 碰上別的 checkout 正在寫正本，可能 FAIL `canonical_drift: inputs changed …`；重跑即可。

## `vcp backup`
| 命令 | 必填 | 其餘 |
|---|---|---|
| `backup manifest` | `--dataset --conclusion submission:<id>|judgement:<prereg>|run:<id>|all` | `--id`（預設 `<conclusion>-<UTC>`） |
| `backup push` | `--dataset --manifest --dest` | `--tier 1|2|3`（累進）、`--forget-remote` |
| `backup verify` | `--dataset --manifest` | `--dest`、`--tier`（只限副本層） |
| `backup pull` | `--dataset --manifest --dest` | `--tier`、`--overwrite`（舊檔留 `.bak-<時戳>`） |
| `backup status` | `--dataset` | 唯讀；`rclone_conf=present|absent|unknown` |

Tier 1 決策層（台帳、卡、判決、預登記、配方、快照、候選檔；KB）、tier 2 重現層（預測檔、樣本、`train/`、logs；MB）、tier 3 權重層（GB，只在要求時）。目的地佈局 `<dest>/data|configs|external/<相對路徑>`；`train upload` 驗過的權重記成 `remote_copy`（同一路徑有多個時優先 rclone 的）：在 rclone 遠端上、或就在目的地裡的就地驗、不重推；其他的（只在這台機器上的）在目的地當一般檔——`--tier 3` 推過去、在那裡驗、從那裡拉（拉不到再退回本機副本），`--forget-remote` 也算它（VERDICT `local_copies=`）。verify 三層：副本（要 `--dest`；`absent=` 是清單時已缺）、本機一致性（卡 ↔ 預測、`train.yaml` ↔ checkpoint、`fuse.json` ↔ 成員、`stage.json` ↔ 候選檔；另查完整性：清單的 `run.yaml` / `train.yaml` 在清單建立前登記的每個 checkpoint 與掛上的證據都要在清單裡，缺的是 `manifest_incomplete`，`reason=` 第一順位、`incomplete=`）、時戳。`status` 每次重算完整性，舊的通過列不能背書；tier 3 push 先擋缺檔的清單；補救是用新 id 重建清單。`verified` 要有一次連 tier 3 都過的 verify（清單完整；有本機副本時要是 0.13.0 起帶 `local_copies` 的列）。

## 機器回收前與賽後重建
```bash
uv run vcp backup manifest --dataset D-test --conclusion submission:SUB34 --id sub34
uv run vcp backup push … --tier 1 ; … --tier 2 ; （有空）manifest --conclusion all → push --tier 3 --forget-remote
# 重建：git pull → backup pull --tier 2 → backup verify → submit verify --id SUB34
```
另外保存：原始資料重新取得方法、Git commit / source bundle、專案程式、環境鎖定檔、notebook bundle。
