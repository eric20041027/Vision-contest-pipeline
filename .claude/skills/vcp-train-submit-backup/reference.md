# 訓練、提交、備份參考

## `vcp train`
| 命令 | 必填 | 其餘 |
|---|---|---|
| `train run` | `--run --dataset --plan` + `-- COMMAND…` | `--export`（可重複；推 `trained_on`）或 `--trained-on`、`--venv`、`--config`（hash + 複製）、`--seed`、`--framework`、`--cwd`、`--checkpoints <glob>`（可重複，相對 `--cwd`）、`--final <glob>`（恰一個檔）、`--upload remote:path|dir`、`--resume`、`--notes`、`--min-free-gib G`（預設 10；低於 → ABORT `low_disk:`，`0` 只記錄；失敗後 console 有磁碟滿字樣 → FAIL `disk_full:`，空間低於門檻 → FAIL `low_disk:`；VERDICT `free_gib=`）、`--code-root DIR`（可重複，0.18.0）、`--authorization ID`（0.18.0；VERDICT 一律帶 `code=` `code_snapshot=` `code_trace=`，見下一節） |
| `train upload` | `--run --dest` | `--only final` |
| `train status` | `--run` | `--verify`（重算 sha）；唯讀；VERDICT 一律帶 `code=`（0.18.0：run 最弱的 attempt 的程式碼等級，永不 WARN）與 `tmp=`（run 目錄遞迴、加上已登記 checkpoint 所在目錄與 `--checkpoints` / `--final` 每個 glob 所在的目錄不遞迴〔glob 是 `train run` 寫進 `started` 事件的；0.16.0 以前的 run 沒有〕，名稱以 `.tmp` 結尾的檔；> 0 WARN，人看的行 `tmp: <path> (<bytes> bytes)`，`--json` 的 `tmp` 是 `[{path, bytes}]`；只列不刪） |

磁碟（`train run`，0.16.0）：量 run 目錄、`--cwd`、每個 checkpoint / final glob 在第一個 `*?[` 之前的前綴所在磁碟區，每區量一次、取最小；最小可用空間 `< --min-free-gib`（GiB = 1024³ bytes，預設 10；非有限數或負數 → FAIL `invalid:`）→ ABORT `low_disk:`（`free_gib=` `min_free_gib=` `disk_path=`），在所有輸入檢查之後、第一次寫入之前，`--resume` 也一樣，一個檔都不寫。量不到 → WARN `disk_check=unavailable`。child 失敗（exit ≠ 0、不是中斷）後再量並讀 console 最後 64 KiB：字樣 `No space left on device`、`[Errno 28]`、`[WinError 112]`、`[WinError 39]`、`There is not enough space on the disk`、`Disk quota exceeded`（不分大小寫）→ FAIL `reason=disk_full:`、`disk_evidence=console`；否則空間低於門檻 → FAIL `reason=low_disk: … the disk may have filled`（只說可能，沒有 `disk_evidence=`）；`train.log.jsonl` 記 `note`（`key=disk_full|low_disk`、`marker`、`free_bytes`）。vcp 自己的紀錄第一次寫入之後寫不下（ENOSPC / EDQUOT / WinError 112、39）→ 停掉 child，ABORT `disk_full:`、`disk_evidence=vcp`，清出空間後 `--resume`；例外是 `--upload` 步驟中途寫不下：attempt 已經結束，VERDICT 的訊息改說 `free space, then: vcp train upload --run R --dest DEST`（冪等），不是 `--resume`；這個轉換只有 `train run` 做，其他寫 yaml 的命令的 `OSError` 不變。`free` 是 `shutil.disk_usage` 的磁碟區可用空間，不反映 Windows 配額。

留下的檔：`runs/<run>/run.yaml`（開始就寫）、`train.yaml`（每事件整份重寫；`attempts[]`、`checkpoints[]`；`run.yaml` 與 `train.yaml` 的重寫都是暫存檔 → fsync → replace，被殺時舊檔完整、可能多一個 `.<名>.<nonce>.tmp`，Windows 目標被別的程序開著時最多重試約 1.55 秒）、`train.log.jsonl`（只增）、`train/config.N.json`、`train/env.N.json`（套件版本、`vcp_version` build string、`disk`：啟動前量到的門檻、時戳與各磁碟區的 `path` `roles` `free_bytes` `total_bytes`、`git`：`commit`、`dirty`、追蹤檔改動與未追蹤的數量和路徑、status 與 diff 的 sha256）、`train/git.N.patch`（追蹤檔的未提交差異，只在有改動時，10 MiB 以內）、`train/console.N.log`。checkpoint 不搬動，只記路徑與 sha；`--upload` 的副本另記 sha 與驗證結果（rclone `copyto --checksum`）。

## `vcp submit`
| 命令 | 必填 | 其餘 |
|---|---|---|
| `submit init` | `--dataset`（test）`--eval-dataset --plan --sealed --platform manual|kaggle --metric` | `--competition`、`--kind file|kernel`、`--board-rule last|best`、`--slots`、`--quota`、`--day-tz`、`--day-start`、`--display-tz`、`--deadline <UTC>`、`--params`、`--writer`、`--writer-opt`、`--kaggle-command`、`--test-plan`（`all-v1`）、`--test-subset`（`test`）；沒有 `selection_warn_hours` 的選項：那是 `submit.yaml` 的鍵（數字 `>= 0` 或 `null`，預設 48，等於預設時不寫出），要改就在 git 裡改，寫了之後舊 vcp 載入 FAIL `extra_forbidden` |
| `submit stage` | `--dataset --id`（< 32 字）`--eval-run` | `--test-run`（file）、`--kind candidate|baseline|probe`、`--reason`、`--kernel --version --output --weights RUN[:sha]`（kernel）、`--code ID`（kernel 的 notebook bundle 快照，0.18.0；file 提交給了 → `invalid:`；VERDICT `code=`）、`--writer-opt`、`--plugin` |
| `submit verify` | `--dataset --id` | |
| `submit upload` | `--dataset --id` | `--message`、`--force "<理由>"`（同 id 再傳才需要，否則 `already_uploaded:`；`final` 之後的重傳也要）、`--no-sync`（略過上傳前同步，WARN）；VERDICT `ledger=` `sync=` `bound=` `cli_warnings=`（一律印：上傳前同步加回讀看到的平台 CLI 警告行，算不同的行數，> 0 WARN；`--json` 只有數字）`forced=` `confirmed=` `platform_ref=` `detail=`（CLI 沒確認、或 CLI 非 0 時都回讀列表：描述以 id 開頭 + 上傳前後 2 分鐘，台帳已有的 ref 不算；`readback=` 記結果。CLI 非 0 而回讀對上 → 寫列、WARN `exit_code=`；沒列出 → FAIL `upload_failed:`；判斷不了 → FAIL `upload_unconfirmed:`；列表呼叫各 120 秒逾時，逾時結束整個程序樹，`uv tool` 之類的包裝程式也一併切斷）；同步之後才 FAIL 也帶 `sync=` `bound=` `cli_warnings=`；上傳前同步本身 FAIL（`sync_failed:`）也帶 `cli_warnings=`（0.17.0；同步讀到的行加例外帶的行），但沒有 `sync=` `bound=`；FAIL 只有個數，行在 log |
| `submit record` | `--dataset --id --at "YYYY-MM-DD HH:MM"` | `--tz platform|utc`、`--platform-ref`（已經在某個 `uploaded` 列 → `exists:`） |
| `submit score` / `sync` | `--dataset` (+ `--id --public/--private`) | sync 把別人的發記成 `foreign`，照數配額；對上 id、平台做完卻沒有分數的一發（狀態最後一段 `error`，或 `complete` 而兩個分數都空）記成 `errored`：先綁定；同一個 ref 最新的結果列（`scored` 或 `errored`）跟這次一樣就不再寫，分數與沒分數之間來回時每一步都寫；VERDICT 一律帶 `errored=`（這次寫入的列數），大於 0 時 WARN，`--json` 的 `errored` 是 id 清單；`sync` 另一律帶 `cli_warnings=`（CLI 印在列表前的警告行，算不同的行數，> 0 WARN；人看的行 `cli_warning: <行>`，含 `outdated` 時多一行升級提示，`--json` 的 `cli_warnings` 是那些行；不寫台帳、每次各自報，CLI 不升級就每次 WARN）；失敗的 `sync` 也帶（0.17.0：`sync_failed:`、CLI 非 0〔`exit_code=` 不被蓋掉〕、`platform_response:`；壞掉的那一頁與前面頁的警告行都算；`kaggle_not_found:` 是 `cli_warnings=0`；`manual_platform:`、`locked:` 與本機 `OSError` 沒有這個欄位；FAIL 只有個數，行在 log） |
| `submit final` | `--dataset` | `--slots`、`--dry-run`；最新一發出錯的候選與基準不排名（`why=errored`，列在沒排名的清單裡）；`board_rule=best` 時已記的選取跟這次的 `chosen` 不同 → WARN `selection=differs`（沒有 `selected` 列或相同時不帶；0.17.0 起比標籤，`S3@123` 就是 `S3`，已記空選取〔`--none`〕也是 `differs`，人看的行是 `selection: final chose …; nothing ticked …`），並一律印 `next: select <chosen> on the platform, then: vcp submit select --dataset T --record <chosen>`（`--dry-run` 也印）；`last` 沒有這兩項；排名與 `final` 列不受選取影響 |
| `submit select` | `--dataset` + 恰好一個 `--record ENTRY[,ENTRY…]` 或 `--none` | `--message`（只在寫列時用）；記平台上實際勾選的，從不呼叫平台，不受封槍與截止擋；條目是 submission id、`<id>@<ref>`（0.17.0；同一個 id 的某一發）或 `foreign:<ref>`；`--none`（0.17.0）寫 `chosen: []`、`selected=0`；檢查都在交易內、全過才寫：`invalid:`（兩個旗標都給或都沒給、空、重複、`@` 一側是空的、同一個 id 既裸記又 `@ref`、同一個 ref 被兩項指到）→ `not_staged:` / `not_uploaded:` / `not_found:`（`foreign:` 的 ref 不在台帳，或 `@ref` 的 ref 不是那個 id 的上傳；皆 `id=`）都不寫列；同條目集合（字面，不管順序）再記 → `written=false`，`S3` 改記 `S3@123` 會寫；`foreign:<ref>` 已被認成自己的上傳 → 照記、多印一行 `note:`、讀取時解析成那個 id；VERDICT `selected=`（條目數）`written=` `selection=ok\|slots\|differs\|unticked`（`slots` / `differs` / `unticked,differs` WARN，單獨 `unticked` OK；`board_rule=last` 一律 `none`）`ledger=`；`--json` 的 result 是那一列加 `selection`（含 `resolved`，每個條目的標籤） |
| `submit lock` / `unlock` | `--dataset --reason` | |
| `submit status` / `report` | `--dataset` | 唯讀（不上鎖）；`status` 看每個 id 最新一發的結果：還沒出分 → `unscored=`，出錯 → `errored=`（人看的行 `errored: <id>`，不 WARN）；`report` 每列帶 `errored`，出錯那一發沒有 public／private，人看的行尾加 ` errored`；0.16.0 起 `status` 另一律帶 `selected=<n>\|none`（`--none` 之後是 `0`）與 `selection=none\|ok\|missing\|unticked\|slots\|differs`（`missing` `slots` `differs` WARN，單獨 `unticked` 不 WARN；核對只在 `board_rule=best`；`missing` = 截止前 `selection_warn_hours` 小時起還沒記，沒有截止時是 `final` 之後；`slots` = 記的不同標籤數不是 `min(名額, 可選條目數)`，名額取最新 `final` 列的 `slots`；`differs` = 跟 `final` 的標籤集合不同；`unticked` = 最新一列是空的，有 `final` 時 `unticked,differs`；人看的行 `selection: …`，`--json` 的 `selection`，0.17.0 起含 `resolved`）；`report` 每列帶 `selected`（0.17.0 起依條目：裸 `<id>` 標該 id 的每一發，`<id>@<ref>` 只標 ref 相同的那一發，`foreign:<ref>` 標那一發，`--none` 之後都不標；人看的行尾 ` selected`） |
| `submit ledger adopt` | `--dataset` | `--from PATH`（可重複；本 checkout 的 configs 台帳一定收）；只在 `ledger: shared` 時用；建共用正本的唯一方法，沒有舊台帳就建空的 |

四道門（stage）：封槍 / 截止 → eval-test 配對（單模比 `weights_hash`，融合比 method / params / 成員遞迴）→ 準入判決（candidate 要 PASS；融合每位成員 PASS）→ 產檔。`Staged.provenance` 記候選等級，低於 `submit.yaml` 的 `require_provenance` → `provenance_required:`。id 不能是台帳的名字 `submissions.jsonl`（`invalid:`）。輸出在 `submit/<test>/<id>/`（寫一次不改）；台帳 `submissions.jsonl` 列：staged / uploaded / scored / errored / foreign / final / selected / lock / unlock（`errored` 自 0.14.0 起、`selected` 自 0.16.0 起；舊版讀到會 FAIL `bad ledger row`，讀寫同一份台帳的 vcp 先一起升級；`selected` 只由人下 `select --record` 才寫，所以記第一筆之前 0.15 與 0.16 可以共用台帳；`selected` 的 `chosen` 自 0.17.0 起可以是空的〔`select --none`〕，0.16.0 讀到會 FAIL，第一次 `--none` 之前讀寫的都先升到 0.17.0）；`ledger: configs` 在 `configs/datasets/<test>/`，`ledger: shared` 在 `<data_root>/submit/<test>/`；寫入命令持有 `<data_root>/locks/submissions-<hash16>.lock`。`final` 依 sealed 讀數選 `final_slots` 個（同分看 public、再看 staged 時間），`board_rule=last` 而最後一發不是選中的就 WARN `needs_reupload=`，並印出重傳命令：Kaggle 是 `vcp submit upload --dataset T --id <選中> --force "final re-send"`（選中的 id 不受封槍擋，但它上傳過，要 `--force`），手動平台是網頁重傳後 `record`。

台帳的操作細節：
- 寫入命令與 `status` 成功（OK / WARN）時 VERDICT 帶 `ledger=`；失敗時只有 `not_adopted:`、`not_shared:` 帶。
- `upload` 從上傳前同步、平台上傳到回讀都拿著鎖：回讀找不到新的一筆（平台沒列出，或只看到台帳已有的 ref；CLI 回 0 的也一樣）時要等滿整個回讀約 17 秒才寫列或失敗，每次列表的 CLI 呼叫最多 120 秒；其他 checkout 的寫入者最多等 60 秒，之後 ABORT `locked:`，重跑即可。
- 寫到一半就當掉的寫入者會留下沒有換行的最後一列：唯讀命令略過它；寫入者在鎖內嚴格讀，殘列解析不了就 FAIL `bad ledger row`。處理：先確定沒有 vcp 寫入者在跑，刪掉那個殘列（它從沒被當成一列讀過）；有 provenance 索引就再跑 `vcp provenance rebuild`（檢查點可能記了殘列的位元組，截掉之後 `sync` 會 `prefix_drift:`）。
- `ledger: shared` 時，一個 checkout 跑 `vcp provenance rebuild` / `sync` 碰上別的 checkout 正在寫正本，可能 FAIL `canonical_drift: inputs changed …`；重跑即可。

## `vcp gc`
| 命令 | 必填 | 其餘 |
|---|---|---|
| `gc scan` | （無） | `--root DIR`（可重複；遞迴掃，不存在 → FAIL `not_found:`）、`--min-mib M`（預設 100；非有限數或負數 → FAIL `invalid:`；`0` 列出每個項目，類別總量不受門檻影響）、`--no-record`（不寫產物，`scan=none`）、`--json`；唯讀、不上鎖、不刪、不讀 provenance 索引；沒有 `--dataset`（永遠讀全部引用） |

掃描範圍：data root（遞迴）、檢查點目錄（每個有 `train.yaml` 的 run 的已登記 checkpoint 所在目錄，加 `started` 事件裡 glob 所在的目錄；不遞迴）、每個 `--root`（遞迴）；規則跟著路徑走（data root 底下一律套 data root 的規則）；不跟隨符號連結與 junction；各 run 的 `--cwd` 預設不掃。引用只讀正本：run 卡與 `train.yaml`（checkpoint、export 與 venv 目錄、本機上傳副本、config）、dataset 與預登記、staged submission、backup 清單與本機目的地、產物 manifest 的 input。

類別（依序，第一條成立的）：`protected`（`raw/`、`locks/`、`.git` 底下）→ `debris`（`.tmp` 結尾、半途產物）→ `referenced` → `rebuildable`（materialize 與 audit 快取、`indexes/`）→ `record`（vcp 自己的紀錄、沒有引用走到：走法失敗的 run、`runs/<id>/inputs/`、已 commit 的產物、`measure/` `submit/` `logs/`、`samples.jsonl`、`cache/` 其他檔）→ `unreferenced`。項目合併到「單位」為止（`raw/<name>`、每個 materialize 模式目錄、`runs/<id>`、`artifacts/<kind>/<id>`、`--root` 的每個直接子項、檢查點目錄的每個檔）；重複檔只在大小等於某個有記錄 sha 的被引用檔時才雜湊，相同 → `duplicate of <路徑>`。

VERDICT：`scan=<id>|none` `roots=`（實際掃的根數）`root_options=`（`--root` 的個數）`items=` `min_mib=` `referenced_gib=` `record_gib=` `rebuildable_gib=` `debris_gib=` `unreferenced_gib=` `protected_gib=` `duplicate_gib=` `missing=` `skipped=` `unreadable=` `running=`；後三項任一 `> 0` → WARN（`skipped` = 有引用沒讀到、`unreadable` = 有目錄沒看到、`running` = 有 run 還在跑）。留下的檔：`artifacts/gc_scan/<id>/`（`spec.json`、`scan.json`、`items.jsonl`、`manifest.json`），id 是 `gc-<時間>-<nonce>`，每次一個新產物，`vcp artifact verify --kind gc_scan --id <id>` 驗；寫產物失敗 → ABORT。限制：`unreferenced` 只是 vcp 的紀錄沒提到；只以 sha 記的權重（`eval ingest --weights`、kernel 的 `WeightRef`）沒有路徑，不算引用；硬連結算兩次；沒有進度輸出。

## 程式碼快照與授權（0.18.0）
| 命令 | 必填 | 其餘 |
|---|---|---|
| `code snapshot` | `--root DIR`（可重複） | 寫或重用快照，**全部保留位元組**；重用 `train run` 先寫的有上限快照時 WARN `not_kept=`；FAIL `too_large:` `drift:` `not_found:`（不是目錄）`invalid:`（在 data / configs root 裡面）；ABORT `code_unavailable:`（`code_root=` `cause=locked\|partial\|unwritable\|unreadable`）；VERDICT `roots=` `ids=` `files=` `kept=` `not_kept=` `reused=` |
| `code verify` | `--id ID` 或 `--dir DIR` | `--id`：產物完好、index 與保留的檔一致、身分重算相同，**不看工作樹**；`--dir`：驗 `export` 的目錄，不需要 git，VERDICT `id=` 是目錄是哪份快照（同時給 `--id` 必須是它）；在場的檔要對 sha（不符 → FAIL `mismatch:`），`kept` 的檔不在 → FAIL（`missing=`），沒保留的不在 → WARN `not_kept=`，多出來的符合規則的檔 → WARN `extra=` |
| `code diff` | `--id ID --root DIR` 或 `--run R` | `--attempt N`（預設最後一個）；`changed=` `added=` `removed=`，任一 > 0 WARN；唯讀；這是「現在的檔動了沒」，不是「快照還成立嗎」 |
| `code export` | `--id ID --out DIR` | 保留的檔寫到 `DIR/<path>`，index 與 `snapshot.json` 在 `DIR/.vcp-code/`；`DIR` 非空 → `exists:`，在 data root 裡 → `invalid:`；`not_kept=` > 0 WARN |
| `artifact authorize` | `--id --basis artifact:<kind>/<id>\|run:<id> --for artifact\|train --reason` | `--target-kind K`（`--for artifact` 必填）、`--free NAME`（可重複）、`--set NAME=VALUE\|NAME=@PATH`、`--supersedes OLD`；FAIL `unbindable:`（名稱不在消費者的字彙裡）`superseded:` `mismatch:`（依據壞了）`not_found:` `exists:` `invalid:`；VERDICT `id=` `basis=` `for=` `claims=` `freed=` |

`train run` 的 root 與規則：label 是 `cwd`、`script`、`dist-<名稱>`（editable）、`root-<n>`（`--code-root`）；每個 root 一份快照，路徑、commit 與 label 不進產物。規則 v1 固定：git root 讀追蹤檔（`CODE` + `CONFIG`）加上未追蹤但未被忽略的 `CODE` 檔（`.py .pyi .pyx .pxd .c .cc .cpp .cu .cuh .h .hpp`）；一般目錄只讀 `CODE`；跳過 `.` 開頭的路徑、`__pycache__`、venv、被忽略的檔、機密檔名（`*.pem *.key id_rsa* kaggle.json *secret*`…，只記路徑）、data / configs root、checkpoint glob 的基底目錄、symlink。保留：`train run` 單檔 ≤ 1 MiB、每個 root ≤ 64 MiB；納入 ≤ 20,000 檔、雜湊 ≤ 512 MiB。寫死在程式碼裡的機密不掃，會進 data root 與備份。

`train run` 留下的新紀錄：`train/env.<n>.json` 多 `code`（root 清單、`excluded_dirs`）、`distributions`（套件名、版本、`RECORD` 的 sha、PEP 610 的 `direct_url`，去掉 userinfo／query 再過 redact）、`identity`（python、`packages_sha256`、gpus）；`evidence` 清單多 `code.<label>`（kind `code_snapshot`，binding `cli`）、`code_trace.a<n>.<seq>`（kind `code_trace`，binding `session`）與 `authorization`（kind `authorization`）；這三種 kind 與這些名稱保留，`--evidence` 用了 → `role_reserved:`。0.17 以前的 vcp 讀到會 FAIL。

等級的 note：`train.log.jsonl` 的 `note key=code_changed`（`value=<n>`、`paths` ≤ 5、`roots`）把 `snapshot` 往下拉；`key=git_changed` 把 `commit` 往下拉。`eval ingest --code LABEL=ID`（可重複）替沒有 `train.yaml` 的 run 掛快照（binding `manual`；快照不存在 → `not_found:`、壞了 → `mismatch:`）：完好就是 `snapshot`；已有 `train.yaml` 的 run 照記 ref、但等級仍由 `train.yaml` 算。`eval status` 每個 run 列 `code=`。`submit stage` 的 VERDICT `code=` 是等級（kernel 候選沒給 `--code` 時是 `none`，`--code` 壞了時是那個快照 id）；`Staged.code`、`Artifact.code_snapshot` 在有值時才寫進 `stage.json`。

備份 role：`code_snapshot`（tier 2）、`code_trace`（tier 1）、`authorization`（tier 1）、`authorization_basis`（tier 2）；`walk_run` 收快照、trace、授權與它的依據（run 依據的 `run.yaml` 不見 → `not_found:`，整個 `run:<id>` 清單建不成）；`walk_submission` 收 kernel 的快照。`gc scan` 把它們算 `referenced`（`locks/` 照舊 `protected`）。provenance 索引每個 checkout `vcp provenance rebuild` 一次；run entity 的 `code_grade` 在 `declared` 時不寫。

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
