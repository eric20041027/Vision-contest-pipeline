# vcp 0.16.0：最終選取的記錄與核對、Kaggle CLI 警告、訓練的磁碟空間 設計

- 日期：2026-10-30
- 來源：RSNA 收尾後記 `docs/superpowers/plans/2026-10-23-rsna-wrapup-followups.md` 的 §1（只有 1b；1a 已在 0.15.1）、§2、§3。§4（`gc`）不在本版。
- 決定：使用者 2026-10-30 的五項決定（§3–§5 逐項落實），以及 controller 同日對三項的裁決（R1：`status` 不提示 CLI 過舊；R2：失敗後的空間比啟動門檻；R3：yaml 改成原子替換）與 builder 檢查後的五項（C1–C5）。其餘選擇是 spec 作者的裁決，集中在 §13；實作時的裁決記在後記 `docs/superpowers/plans/2026-10-30-vcp-0160-selection-and-disk-followups.md`。
- 版本：下一個 MINOR（0.16.0），理由見 §9。

## 1. 問題

- **最終選取沒有紀錄。** RSNA 的最終選取是在 Kaggle 網頁上手動勾的，勾錯了兩發（勾成九月的舊提交），vcp 沒有察覺。`submit final` 記的是 vcp 依 sealed 讀數的建議，不是平台上實際的勾選；台帳沒有地方存後者，也沒有任何命令拿兩者比。
- **CLI 過舊只留在 log。** 0.15.1 讓解析略過 Kaggle CLI 印在答案前面的 `Warning:` 行，但只寫進 vcp 的 log；VERDICT 看不到。
- **訓練中磁碟滿。** `torch.save` 存 resume 檔時失敗（`ios_base::badbit`），留下 266 MB 的 `.tmp`。`train run` 事前不看空間；child 結束後 VERDICT 只有 `status=FAIL exit_code=1`，看不出是磁碟；`train status` 也不提殘檔。vcp 自己的 `train.yaml`、`run.yaml` 是就地重寫，磁碟滿時可能被截成半個檔。

## 2. 範圍

做：

- 台帳新事件 `selected` 與命令 `vcp submit select --record`（§3.1、§3.2）。
- `submit.yaml` 新鍵 `selection_warn_hours`（§3.3）。
- 選取狀態的共用判斷，由 `status`、`select`、`final` 讀，`report` 標出選取的列（§3.4、§3.5）；核對只在平台有決選勾選的 `board_rule: best` 做。
- 平台列表帶回 CLI 警告，`sync` / `upload` 的 VERDICT 浮出（§4）。
- `train run` 啟動前檢查可用空間、記進環境快照（§5.1、§5.2）。
- child 失敗之後的 `reason=disk_full` 與 `reason=low_disk`（§5.3、§5.4）。
- `train status` 列出殘留的 `*.tmp`（§5.5）。
- `dump_yaml_model` 改成原子替換（§5.6）。

不做：

- **自動讀回平台的勾選。** kagglesdk 0.1.38 的 `ApiListSubmissionsRequest.group` 有 `SubmissionGroup.SUBMISSION_GROUP_SELECTED`，但 kaggle CLI 2.2.5 的 `competitions submissions` 沒有對應的旗標，它的 JSON 也沒有 selected 欄位。vcp 只呼叫 CLI，不用 Python API、不碰憑證，所以讀不到。日後 CLI 有了旗標，`sync` 可以寫 `source=platform` 的 `selected` 列（§3.1 已留位置）。
- **`submit status` 的 CLI 過舊提示**（偏離後記 §1 的 1b，controller 裁決 R1）：
  - `status` 從不呼叫平台，要提示就得把「上次讀列表時 CLI 印了什麼」存下來；
  - 唯一讀得到的地方是治理台帳（`configs` 模式下在 git 裡），把操作面的事實寫進去，代價大於這個缺口；
  - `upload` 一律先同步，所以每次讀平台都會 WARN（§4.2）。
- **在平台上替人勾選**：vcp 不操作平台的選取介面。
- **強制選取等於 `final`**：只 WARN。選別的是人的決定，vcp 只確保它被看見。
- **刪除 `.tmp`**：只列。刪除仍由人做；清理工具是後記 §4（`gc`），不在本版。
- **訓練中途監看空間**：只在啟動前量一次、結束後失敗時再量一次。
- **改 `train.yaml` 的 schema**：`Attempt` 不加欄位（舊 vcp 嚴格驗 `train.yaml`）。磁碟的紀錄放 `train.log.jsonl` 的 `note`。
- **`submit init` 的新選項**：`selection_warn_hours` 要改就在 git 裡改 `submit.yaml`。
- **`gc`（後記 §4）**。

## 3. 最終選取（後記 §2；決定 1、2）

### 3.1 台帳事件 `selected`

- `Event` 加 `selected`。
- 必填：`chosen`（list，沿用 `final` 的欄位）、`source`。選填：`message`。
- `chosen` 的每一項是一個「標籤」，格式同 `status` 的 `current`：
  - staged 的 submission id；
  - 或 `foreign:<platform_ref>`：別人或網頁上傳、台帳記成 `foreign` 的一發。
- schema 檢查：`chosen` 不得為空（`selected needs at least one entry`）、不得重複（`selected has duplicate entries`）。
- `source`：目前只有 `manual`（`select --record` 寫）。`platform` 保留給日後的自動讀回。
- 跟其他列一樣只 append、不改。記錯了就再記一次，最新的一列為準（台帳順序）。

### 3.2 `vcp submit select`

```
uv run vcp submit select --dataset T --record S5,S3 [--message "勾選時的備註"]
```

- `--record`：必填，逗號分隔（同 `--trained-on`）。submission id 只能有字母、數字與 `. _ -`，不會含逗號或冒號。
- 整段在台帳交易裡（拿鎖後才讀台帳，§7）。依序檢查，任一項不過就 FAIL、一列都不寫：
  - 去掉空白後沒有任何項 → `invalid:`；有重複 → `invalid:`；
  - id 沒有 `staged` 列 → `not_staged:`（`id=`）；
  - id 沒有任何 `uploaded` 列 → `not_uploaded:`（`id=`）；平台上沒有的東西勾不到；
  - `foreign:<ref>` 的 ref 不在台帳的 `foreign` 列 → `not_found:`（`id=`）。
- 不檢查封槍、不檢查截止：這是記錄平台上的狀態，跟 `sync`、`score` 一樣不受 `lock` 擋。截止之後補記，記的是截止當下凍結的選取。
- 勾到 probe 或最新一發出錯的 id 照記，由 §3.4 的比對讓人看見。
- 冪等：最新的 `selected` 列跟這次的集合相同（不管順序）→ 不寫，`written=false`。
- 否則 append 一列 `selected`（`source=manual`、`chosen` 照輸入順序），`written=true`。
- 寫完（或沒寫）都跑 §3.4 的判斷（`missing` 不會出現：已經有一列），`slots` 或 `differs` → WARN。照記不擋：台帳要說平台上實際是什麼，即使它是錯的。`board_rule: last` 照記，判斷一律 `none`（§3.4）。
- Manual 平台完全相同：這個命令從不呼叫平台。
- VERDICT：`dataset=` `selected=<n>` `written=` `selection=` `ledger=`。`--json` 的 result 是最新的那列 `selected` 加上 §3.4 的狀態物件。

### 3.3 `submit.yaml` 的 `selection_warn_hours`

- `PlatformProfile.selection_warn_hours: float | None = 48`。
- 驗證：有限、`>= 0`；否則載入 `submit.yaml` 就 FAIL（pydantic 驗證錯誤，同其他鍵）。
- 意思：`board_rule: best` 時，截止前幾小時起，還沒有任何 `selected` 列時 `status` 會 WARN（`last` 不用這個鍵）。`0` 表示從截止那一刻起；`null` 關掉這一項（只關 `missing`，§3.4 的另外兩項照常）。
- 等於預設值時不寫出，比照 `ledger: configs`：0.16.0 的 `submit init` 寫的檔，0.15.x 照樣讀得動。手動寫了這個鍵，舊 vcp 載入時 FAIL `extra_forbidden`，這正是要的：舊 vcp 不會照它做。

### 3.4 選取的狀態（共用判斷）

`vcp.submit.select.selection_state(ledger, profile, now) -> SelectionState`。輸入是台帳、profile 與 `utc_now()`；輸出：

- **只有 `board_rule: best` 才核對**（controller 裁決 C1）。`board_rule: last` 的平台算最後一發、沒有勾選這一步：`words` 一律 `none`，其餘欄位照算（`chosen` 照讀，給 `report` 與 `--json`）。下面各條只講 `best`。
- `chosen`：最新 `selected` 列的 `chosen`，沒有則 `None`；`recorded_at`：那列的 `ts`。
- `due_at`：
  - 有 `deadline`、鍵不是 `null` → `deadline − selection_warn_hours`；
  - 沒有 `deadline`、鍵不是 `null` → 有任何 `final` 列時視為已到（`due_at` 是那列的 `ts`），否則未到；
  - 鍵是 `null` → 永遠未到。
- `expected`：`min(名額, 可選的條目數)`，至少 1（controller 裁決 C4）。
  - 名額：有 `final` 列時是最新那列的 `slots`（`final --slots` 覆寫過的值），否則是 profile 的 `final_slots`；
  - 可選的條目數 = `arrivals()` 裡不同標籤的數目（我們的 id 與他人的 `foreign:<ref>`）。
- `final`：最新 `final` 列的 `chosen`，沒有則 `None`。
- `words`（VERDICT 的 `selection=`）：
  - 沒有 `selected` 列：已到 → `missing`；未到 → `none`；
  - 有 `selected` 列：
    - 個數不等於 `expected` → `slots`；
    - 有 `final` 而集合不同 → `differs`；沒有 `final` 不比；
    - 都沒有 → `ok`；
  - 多項時依 `missing,slots,differs` 的順序以逗號串起來。

### 3.5 讀取端

- **`submit status`**：
  - VERDICT 一律帶 `selected=<n>|none` 與 `selection=`；`missing`、`slots`、`differs` 任一項 → WARN。`board_rule: last` 永遠是 `selection=none`，不因選取 WARN。
  - 人看的行：
    - `selection: missing (deadline <stamp>, warning since <due_at>)`；
    - `selection: <n> recorded, <expected> expected (slots=<k>)`；
    - `selection: final chose <a,b>; recorded <c,d> (not selected: <…>; selected, not final: <…>)`。
  - `--json` 的 `selection`：`{chosen, recorded_at, words, expected, final, due_at}`。
  - 既有的 `deadline_in < 24` WARN 不變。
- **`submit report`**：
  - `ReportRow` 加 `selected: bool = False`（放最後、有預設）：這一列的標籤在最新 `selected` 列的 `chosen` 裡；同一個 id 的每一發都標。
  - 人看的行尾加 ` selected`（在 ` errored` 之後），`--json` 的 rows 帶 `selected`；VERDICT 不變。`board_rule` 不影響。
- **`submit final`**：
  - 排名與 `final` 列（含 `table`）一個位元組都不變：決選只看 sealed 讀數。
  - 以下兩項只在 `board_rule: best`；`last` 都沒有（它照舊靠 `needs_reupload=`）：
    - 已有 `selected` 列、而集合跟這次的 `chosen` 不同 → WARN，VERDICT 帶 `selection=differs`，人看的行印兩邊。沒有 `selected` 列或相同時不帶這個欄位；
    - 多印一行提示：`next: select <chosen> on the platform, then: vcp submit select --dataset T --record <chosen>`。`--dry-run` 也印。

## 4. Kaggle CLI 的警告（後記 §1 的 1b；決定 5、裁決 R1）

### 4.1 平台契約

- 新的 frozen dataclass `PlatformList(submissions: list[PlatformSubmission], warnings: tuple[str, ...] = ())`。`Platform.list_submissions` 改回傳它。
- 用 `register_platform` 登記、仍回傳 list 的平台：`sync` 與 `upload` 把它包成 `PlatformList(list, ())`，照常運作。
- Kaggle：
  - `_past_warnings` 除了寫 log（照舊），也回傳略過的行（已 redact）；
  - `parse_submissions` 回傳 `(submissions, token, warnings)`；
  - 多頁時同一行只留一次（依出現順序）。
- `UploadResult` 加 `cli_warnings: tuple[str, ...] = ()`：回讀的那幾次列表看到的警告。
- 略過哪些行不變（0.15.1 的規則：答案前面以 `Warning:` 開頭的行與空白行）。

### 4.2 `sync` 與 `upload` 的 VERDICT

- `submit sync`：一律帶 `cli_warnings=<n>`（不同行數）；`n > 0` → WARN。
  - 人看的行：每行一個 `cli_warning: <line>`；
  - `--json` 的 `cli_warnings` 是那些行。
- `submit upload`：一律帶 `cli_warnings=`，算的是上傳前同步加回讀看到的不同行；`n > 0` → WARN。
  - 上傳前同步之後才 FAIL 的（配額、重傳護欄、平台）也帶它，跟 `sync=`、`bound=` 一樣。
- 任一行含 `outdated`（不分大小寫）時多印一行：`the platform CLI says it is outdated; upgrade the CLI that kaggle_command runs`。
- 警告只出現在這次命令的 VERDICT、人看的行與 log，不寫進台帳。每次讀列表各自報：CLI 沒升級，每一次 `sync` / `upload` 都 WARN。

## 5. 訓練的磁碟空間（後記 §3；決定 3、4，裁決 R2、R3）

### 5.1 啟動前檢查

- 新選項 `vcp train run --min-free-gib G`（`RunSpec.min_free_gib`，預設 `10.0`，GiB = 1024³ bytes）。`G` 不是有限數或 `< 0` → FAIL `invalid:`。`G = 0` 照量照記、永不 ABORT，也關掉 §5.3 結束後的空間檢查。
- 量哪些目錄（「run 的輸出」，各取最近一個已存在的上層目錄）：
  - `run`：`<data_root>/runs/<run_id>/`（vcp 自己的 console、`train.yaml`、env 快照）；
  - `cwd`：`--cwd`；
  - `checkpoints`：每個 `--checkpoints` 與 `--final` glob 在第一個 `*`、`?`、`[` 之前的固定前綴，以 `--cwd` 為基準（child 寫權重的地方）。
- 依 `os.stat(path).st_dev` 分到磁碟區，每區 `shutil.disk_usage` 一次。`free` 就是 `shutil.disk_usage().free`：Windows 上是整個磁碟區的可用空間，不反映使用者配額；不用 ctypes 另查（controller 裁決 C5）。
- 位置：所有輸入檢查之後（含 `check_names`）、第一次寫入之前（`_close_running` 之前；它會寫 `train.log.jsonl`）。每個 attempt 都檢查，含 `--resume`。
- 最小的可用空間 `< G` → ABORT `low_disk:`（`VcpError`），一個檔都不寫：
  - VERDICT 帶 `free_gib=`（一位小數）、`min_free_gib=`、`disk_path=`（那一區的代表路徑）；
  - 訊息說清空或換到別的磁碟，或用 `--min-free-gib` 調低門檻。
- 量不到（`disk_usage` 丟 `OSError`）：那一區記 `free_bytes: null`；`train run` WARN `disk_check=unavailable`，不 ABORT（同 `git_changed=unavailable`）。全部量不到時 `free_gib=unknown`。
- 成功的 VERDICT（OK / WARN / FAIL）一律帶 `free_gib=`：啟動前量到的最小值。

### 5.2 環境快照

- `EnvSnapshot` 加 `disk: DiskCheck | None = None`：
  - `DiskCheck`：`min_free_gib`、`checked_at`、`volumes`；
  - `volumes` 的每一項：`path`（`store_path`）、`roles`（`run` / `cwd` / `checkpoints`）、`free_bytes`、`total_bytes`。
- 記的是 §5.1 做決定時量到的值，不另外重量。
- 舊的 `env.<n>.json` 沒有 `disk` 照樣載入；vcp 本來就不讀回 `env.json`。

### 5.3 child 失敗之後：磁碟滿與空間不足

- 只在 attempt 的 `status == "failed"`（exit ≠ 0、不是 interrupted）時判斷。成功或中斷的一律不判。失敗的 attempt 都重量一次 §5.1 的同一組磁碟區。
- 依序：
  1. **console 訊號** → `reason="disk_full: the command exited <n> and its console says \"<字樣>\""`、`disk_evidence=console`。`console.<n>.log` 最後 64 KiB（`DISK_FULL_TAIL`）不分大小寫含下列任一字樣：
     - `No space left on device`、`[Errno 28]`（POSIX 的 ENOSPC；Windows 上 Python 的 CRT 寫入錯誤也是這個）；
     - `[WinError 112]`、`[WinError 39]`（Windows 的 ERROR_DISK_FULL / ERROR_HANDLE_DISK_FULL）；
     - `There is not enough space on the disk`、`Disk quota exceeded`。
  2. 否則，`G > 0` 而重量的最小可用空間 `< G` → `reason="low_disk: the command exited <n> with <x> GiB free (below --min-free-gib <G>); the disk may have filled"`。只說「可能」：空間不足是事實，是不是失敗的原因 vcp 不知道。沒有 `disk_evidence=`。
  3. 都不是 → 跟 0.15 相同，沒有 `reason=`。
- 狀態照舊 FAIL（exit 1）。有 `reason=` 時：
  - `train.log.jsonl` append `note`：`key=disk_full|low_disk`（用了哪個字）、`marker=<對到的字樣或 null>`、`free_bytes=<重量的最小值>`；
  - 人看的行：`<字>: …; free space, then --resume`。
- **vcp 自己寫不下**（controller 裁決 C3）：`train run` 第一次寫入之後，vcp 自己寫檔（console、`train.yaml`、`run.yaml`、事件、快照，含 §5.6 的替換）遇到磁碟滿（`errno` 是 ENOSPC 或 EDQUOT，或 `winerror` 是 112、39）→ 轉成 ABORT `disk_full:`，`disk_evidence=vcp`。
  - child 還在跑時寫不下 console：`execute()` 照現行行為先停掉 child（5-1），再 ABORT。vcp 記不下的 run 不算一個有紀錄的 run：紀錄的完整勝過訓練的延續。
  - 這時 attempt 可能還停在 `running`；清出空間後 `--resume`，它會被標成 `interrupted`（既有的 5-3）。
  - 其他 `OSError` 照舊原樣往外丟。

### 5.4 能偵測與不能偵測的

- 能：
  - Python 程式的 ENOSPC 例外（Windows 與 POSIX 都會印 `[Errno 28] No space left on device`）；
  - Win32 呼叫的 `[WinError 112]`：訊息本文會在地化，代碼不會；
  - POSIX 工具與 shell 的 `No space left on device`；
  - 結束後可用空間低於啟動門檻：報 `low_disk:`，只說可能滿過。RSNA 這次失敗後約剩 2.2 GB，console 只有 `ios_base::badbit`；門檻 10 GiB 時靠這一步報出。固定 1 GiB 的下限會漏掉它。
- 不能：
  - **exit code 不帶原因。** Python 任何未捕捉的例外都是 1；Windows 的 112 是 API 的錯誤碼，不是程序的 exit code。
  - **只說串流壞了的錯誤。** C++ iostream 的 `ios_base::badbit`、PyTorch 的 `file write failed` 不含磁碟的字樣，只能落到 `low_disk:`；vcp 重量時空間若已回到門檻以上（框架刪掉半途檔、別的程序清出空間），就什麼都不報。
  - 非 Python 程式印的在地化訊息（例如中文 Windows 的「磁碟空間不足。」）：不比對。
  - child 只寫進自己 log 檔、不經 stdout / stderr 的訊息。
  - 量測範圍以外的磁碟滿了（例如 `%TEMP%`、`/tmp`、模型快取）：console 訊號可能抓到，空間檢查抓不到。
  - 64 KiB 之前的訊息。
  - Windows 的使用者配額：`free` 是整個磁碟區的可用空間（C5），配額用完、磁碟區還有空時，啟動前檢查與結束後的空間檢查都看不出來；console 的 `Disk quota exceeded` 或 `[Errno 28]` 仍抓得到。
- 已知的後果（C3）：vcp 自己的寫入優先。data root 所在的磁碟滿了，即使 checkpoint 寫在另一顆磁碟、child 本來還能跑，`train run` 也會停掉它並 ABORT `disk_full:`；清出空間後 `--resume`。
- 誤判：
  - 程式攔下一次 ENOSPC、繼續跑、最後因別的原因失敗，仍標 `disk_full`；
  - 訓練本身吃掉大量空間（checkpoint 一路累積）而因別的原因失敗，也會報 `low_disk:`，所以措辭只說 may；
  - `disk_evidence=` 區分 `disk_full:` 的兩個來源。

### 5.5 `train status` 的殘留 `.tmp`

- 掃的範圍：
  - run 目錄 `runs/<run_id>/`，遞迴；
  - 這個 run 每個已登記 checkpoint 所在的目錄，不遞迴；不存在的目錄略過。
- 名稱以 `.tmp` 結尾（不分大小寫）的檔都列，含 vcp 自己 `write_once` 與 §5.6 的替換被中斷時留下的 `.<name>.<nonce>.tmp`。
- VERDICT 一律帶 `tmp=<n>`；`n > 0` → WARN（attempt 還在 `running` 時也照列：那時本來就 WARN）。
  - 人看的行：`tmp: <path> (<bytes> bytes)`；
  - `--json` 的 `tmp`：`[{path, bytes}]`，`path` 是 `store_path`。
- 唯讀、不刪。`--checkpoints` 的 glob 沒有存進 `train.yaml`，所以從沒登記過 checkpoint 的目錄看不到（§12）。
  - 2026-10-31 註：現在會掃 glob 所在的目錄——`train run` 把 `--checkpoints` / `--final` 的 glob 寫進 `started` 事件，`train status` 據此多看那些目錄；見後記 `docs/superpowers/plans/2026-10-30-vcp-0160-selection-and-disk-followups.md` §2.1 第 19 條。

### 5.6 vcp 自己的 yaml 改成原子替換（裁決 R3）

- `vcp.core.config.dump_yaml_model` 目前用 `open("w")` 就地重寫：先截斷、再寫。磁碟滿或程序中途被殺時，檔案可能只剩一半。
- 改用 `vcp.core.atomic` 的做法（同 `write_once_stream` 與 `measure/anchors.py`），但允許覆寫：
  - 新函式 `vcp.core.atomic.replace_text(path, text)`，`dump_yaml_model` 改呼叫它；
  - 同目錄的暫存檔 `.<name>.<nonce>.tmp`（`_tmp_path`，`is_tmp_name` 認得）→ UTF-8、`\n` 寫入 → flush、`fsync` → `os.replace` 到目標 → `_fsync_dir`；
  - 任何例外：刪掉暫存檔、原樣往外丟，舊檔完整。`OSError` 不轉換（controller 裁決 C2）。
- 寫出的位元組不變（同一個 `dump_yaml_text`）。
- Windows：別的程序開著目標檔時（例如訓練中 `train status` 正在讀 `train.yaml`、防毒或索引服務），`os.replace` 會丟 `PermissionError`。
  - 只在 Windows（`os.name == "nt"`）重試：最多 5 次，每次之前 `time.sleep` 0.05、0.1、0.2、0.4、0.8 秒（`REPLACE_RETRY_DELAYS`，共約 1.55 秒；不用 `time.time`，ruff TID251）；
  - 仍失敗就刪暫存檔、原樣丟出；
  - POSIX 的 `PermissionError` 是真的權限問題，不重試。
- 涵蓋所有呼叫者：`train.yaml`、`run.yaml`、`dataset.yaml`、`submit init` 寫的 `submit.yaml`。讀者在任何時刻讀到的都是完整的舊檔或新檔。
- 錯誤的對應（C2）：
  - 只有 `train run` 在第一次寫入之後，把磁碟滿的 `OSError`（ENOSPC、EDQUOT、`WinError` 112、39）轉成 ABORT `disk_full:`、`disk_evidence=vcp`（§5.3）；
  - 其他呼叫 `dump_yaml_model` 的命令（`eval ingest` 寫 `run.yaml`、`data import` 寫 `dataset.yaml`、`submit init` 寫 `submit.yaml` 等）照今天的錯誤行為：`OSError` 照樣成為 ABORT，`reason=` 是原本的例外文字。它們的契約不變。

## 6. VERDICT 與字彙

| 命令 | 變更 |
|---|---|
| `submit select`（新） | `selected=` `written=` `selection=ok\|slots\|differs`（`board_rule: last` 一律 `none`）；`slots` / `differs` → WARN；FAIL 用既有字彙 `invalid:`、`not_staged:`、`not_uploaded:`、`not_found:` |
| `submit status` | `selected=<n>\|none`、`selection=none\|ok\|missing\|slots\|differs`（一律印；後三項 WARN；`board_rule: last` 一律 `none`） |
| `submit report` | 每列的 `selected`（`--json`），人看的行尾 ` selected`（不分 `board_rule`） |
| `submit final` | 只在 `board_rule: best`：`selection=differs`（有才印，WARN）；多一行 `next:` 提示 |
| `submit sync` | `cli_warnings=`（一律印；> 0 WARN） |
| `submit upload` | `cli_warnings=`（一律印，FAIL 時也帶；> 0 WARN） |
| `train run` | 新選項 `--min-free-gib`；`free_gib=`（一律印）；啟動前 ABORT `low_disk:`（`free_gib=` `min_free_gib=` `disk_path=`）；attempt 失敗後 FAIL `reason=disk_full:`（`disk_evidence=console`）或 `reason=low_disk:`；第一次寫入之後 vcp 自己寫不下 ABORT `disk_full:`（`disk_evidence=vcp`；只有 `train run` 做這個轉換，其他寫 yaml 的命令的錯誤不變）；WARN `disk_check=unavailable` |
| `train status` | `tmp=`（一律印；> 0 WARN） |

新的 `reason=` 字彙，都只在 `train run`：`low_disk:`（啟動前 ABORT；attempt 失敗後 FAIL）、`disk_full:`（console 有字樣時 FAIL；vcp 自己寫不下時 ABORT）。

## 7. 冪等與鎖

- `submit select` 是寫入命令：
  - 整段持有 `<data_root>/locks/submissions-<hash16>.lock`，拿到鎖才讀台帳；等 60 秒 → ABORT `locked:`；
  - `ledger: shared` 還沒 adopt → `not_adopted:`；
  - 集合相同不重寫（§3.2）。
- CLI 警告不寫台帳，`sync` / `upload` 的交易內容不變。
- `status`、`report`：唯讀、不上鎖，略過寫到一半的最後一列（既有規則）。
- `final`：既有交易內讀最新的 `selected` 列，不寫它。
- `adopt`：`selected` 列跟其他列一樣合併、依 `ts` 排序。兩個 checkout 各記了不同的選取時，兩列都留，`ts` 較新的為準。
- 訓練端沒有台帳鎖：
  - 磁碟檢查在第一次寫入前；
  - `note` 事件照舊 append；
  - `train.yaml`、`run.yaml` 的重寫是原子替換（§5.6），訓練中 `train status` 讀到的一定是完整的檔；
  - `train status` 唯讀。

## 8. 相容性與升級

- **`selected` 列**：0.15.1 以前的 vcp 讀到會 FAIL `bad ledger row`（每個讀者都嚴格驗每一列，同 0.14.0 的 `errored`）。會 FAIL 的有每個 submit 寫入命令（`stage`、`upload`、`record`、`score`、`sync`、`final`、`lock`、`unlock`、`ledger adopt`）、唯讀的 `status`、`report`，以及讀台帳的 `backup manifest`、`backup verify`。
- 升級：一份台帳的每個讀者與寫者都先升到 0.16.0，才第一次跑 `select --record`：
  - `configs` 模式的台帳跟著 git 走：共用它的每個 checkout；
  - `shared` 模式的正本：同一個 data root 的每個 worktree；
  - 跟 `errored` 不同，`selected` 只由人下的命令寫，所以在那之前 0.15 與 0.16 可以共用台帳；誰先記選取，誰負責先讓大家升級。
- **`submit.yaml` 的 `selection_warn_hours`**：等於預設值時不寫出；寫了之後，所有讀這份 profile 的 vcp 都要 0.16.0（舊版 `extra_forbidden`）。
- **訓練端**：`env.<n>.json` 多 `disk`（舊 vcp 不讀它）；`train.log.jsonl` 的 `note` 多幾個 key（讀事件的程式碼不驗欄位）；`train.yaml`、`run.yaml` 的內容不變。
- **原子替換**：四種 yaml 的位元組不變，舊 vcp 照讀。被硬殺時目錄裡可能多一個 `.<name>.<nonce>.tmp`，目標檔仍是完整的舊版。
- **CI 與既有測試**：預設門檻 10 GiB 會讓空間不足的 runner 上每個 `train run` 測試 ABORT。測試的共用 conftest 預設把磁碟探針換成大數值，只有磁碟測試自己覆寫（§10.5）。
- 不需要遷移：沒有 `selected` 列的台帳，`status` 依 §3.4 給 `none` 或 `missing`。

## 9. 版本

0.16.0（MINOR），依 CHANGELOG「版本規則」：

- 台帳與產物的新內容：事件 `selected`；`env.<n>.json` 的 `disk`；`train.log.jsonl` 的 `disk_full` / `low_disk` note。
- CLI 契約：新命令 `submit select`、新選項 `--min-free-gib`；新 VERDICT 欄位（§6）；新 `reason=` 字彙 `low_disk:`、`disk_full:`；`status`、`final`、`sync`、`upload`、`train run`、`train status` 可能從 OK 變成 WARN。
- 設定與 API：`submit.yaml` 新鍵；`Platform.list_submissions` 的回傳型別。
- `dump_yaml_model` 的原子替換本身不改位元組、不改契約（PATCH 等級的修正），隨本版發出。
- 發版時 `.claude/.claude-plugin/plugin.json` 的版本跟著改成 0.16.0。

## 10. 測試（先寫、先紅）

### 10.1 `selected` 事件與 `select`

- schema：`selected` 在 `EVENTS`；缺 `chosen` 或 `source`、空的 `chosen`、重複項 → FAIL。
- `select`：
  - 記兩個已上傳的 id → 一列 `source=manual`，VERDICT `selected=2 written=true selection=ok`；
  - 同集合換順序再記 → 不寫，`written=false`、OK；
  - 未 stage → `not_staged:`；未上傳 → `not_uploaded:`；未知的 `foreign:<ref>` → `not_found:`（已知的照記）；空、重複 → `invalid:`；每種 FAIL 都不寫列；
  - `final` 封槍之後照記；manual 平台照記；
  - 個數 ≠ `final_slots` → 照寫、WARN `selection=slots`；跟 `final` 不同 → 照寫、WARN `selection=differs`；
  - 另一個程序持有台帳鎖 → ABORT `locked:`（沿用 `test_transactions.py` 的做法）。

### 10.2 狀態與讀取端

- `selection_state`：
  - 截止前 72 h、N=48、沒選取 → `none`；截止前 47 h、截止後 → `missing`；N=72 時截止前 71 h → `missing`；`null` → 永遠 `none`；
  - 沒有截止：沒有 `final` → `none`；有 `final` → `missing`；
  - 跟 `final` 相同、個數對 → `ok`；只有一個條目、slots=2、選一個 → `ok`（`expected` 取最小值）；
  - 兩項同時 → `slots,differs`；沒有 `final` 不比 `differs`；最新的一列為準；
  - 名額取最新 `final` 列的 `slots`（C4）：`final_slots: 2` 的 profile 跑 `final --slots 1`、再記 1 個 id → `ok`；
  - `board_rule: last`（C1）：過了 `due_at`、沒選取 → `none`；記了跟 `final` 不同的選取 → 仍是 `none`；`select --record` 照寫、`report` 照標。
- profile：預設值不寫出；明寫的值能來回；負數、`nan` → FAIL；沒有這個鍵的舊 profile 照常載入。
- `status`：VERDICT 一律帶 `selected=`、`selection=`；`missing` / `slots` / `differs` 讓它 WARN；`--json` 有 `selection`。
- `report`：選取的 id 的每一發 `selected=True`、`foreign:<ref>` 也標；沒有選取時全部 `False`。
- `final`（`board_rule: best`）：已記的選取跟新的 `chosen` 不同 → WARN `selection=differs`，相同或沒有 → 沒有這個欄位；一律印 `next:`。`board_rule: last`：沒有 `next:` 行，也沒有 `selection=`。兩者的 `final` 列位元組都跟 0.15.1 相同（golden）。

### 10.3 CLI 警告

- 解析：先警告後 JSON → 回傳那一行（已 redact）與正確的列表；沒警告 → 空；兩頁各印同一行 → 一行。
- `sync`（假 CLI）：
  - 有警告 → `cli_warnings=1`、WARN，`--json` 帶那一行；含 `outdated` 時多一行升級提示；
  - 同樣的警告再 sync → 照樣 WARN；
  - 台帳在 sync 前後只差平台資料本來就會寫的列（`foreign`、綁定、`scored`、`errored`），沒有 `note`。
- `upload`：上傳前同步印警告 → `cli_warnings=1`、WARN；之後配額 FAIL 的 VERDICT 也帶它；回讀印的警告也算。
- 回傳 list 的登記平台照常 sync，`cli_warnings=0`。
- `status` 的 VERDICT 沒有 CLI 警告的欄位（鎖住裁決 R1）。

### 10.4 磁碟檢查

用可替換的探針（`vcp.train.disk.usage`），不真的塞滿磁碟。

- 門檻：
  - 低於門檻 → ABORT `low_disk:`，帶 `free_gib=` `min_free_gib=` `disk_path=`，`run.yaml`、`train.yaml`、`train.log.jsonl` 都不存在；`--resume` 同樣 ABORT，既有紀錄的位元組不變；
  - `--min-free-gib 0` → 不 ABORT、照記；`-1`、`nan` → FAIL `invalid:`；高於門檻 → `free_gib=`。
- 快照：`env.<n>.json` 的 `disk` 有門檻、時戳與各區。
- 量測範圍：`cwd` 與 run 目錄同一區 → 一項、兩個 role；glob 前綴在另一區（假的 `st_dev`）→ 兩項，取最小；探針丟 `OSError` → WARN `disk_check=unavailable`、不 ABORT。
- 順序：`run_exists`、`not_found` 之類的輸入錯誤先於 `low_disk:` 報出。

### 10.5 失敗之後與 `.tmp`

- `tests/conftest.py`（或各層 conftest）的 autouse fixture 把探針換成大數值；磁碟測試自己覆寫。
- 失敗之後：
  - 假 child 印 `OSError: [Errno 28] No space left on device` 後 exit 1 → FAIL、`reason` 以 `disk_full:` 開頭、`disk_evidence=console`，`train.log.jsonl` 有 `key=disk_full` 的 `note`；`[WinError 112]` 的版本同樣；
  - 印 `ios_base::badbit` 後 exit 1、門檻 10、重量時探針給 2 GiB → FAIL、`reason` 以 `low_disk:` 開頭、含 `may have filled`、沒有 `disk_evidence=`，`note` 的 `key=low_disk`；探針給 50 GiB → 沒有 `reason=`；
  - 同一個 badbit child、探針 2 GiB、`--min-free-gib 0` → 沒有 `reason=`；
  - console 有字樣、空間也不足 → `disk_full:`（console 優先）；
  - 無關的錯誤、空間充足 → 沒有 `reason=`（跟 0.15 相同）；exit 0 但印了 `No space left`、或中斷 → 不判；
  - 字樣在最後 64 KiB 之前 → 不判（鎖住這個限制）；
  - child 還在跑時 vcp 寫 console 丟 `OSError(ENOSPC)` → child 被停掉（C3）、ABORT `disk_full:`、`disk_evidence=vcp`；其他 `OSError` 照原樣。
- `train status`：run 目錄巢狀的 `.tmp` 與 checkpoint 旁的 `.tmp` → `tmp=2`、WARN、`--json` 有路徑與大小；無關目錄的 `.tmp` 不列；沒有 → `tmp=0`；checkpoint 目錄不存在 → 略過。

### 10.6 原子替換

- 輸出跟 0.15.1 的 `dump_yaml_model` 逐位元組相同（golden：`TrainRecord`、`RunCard`、`PlatformProfile` 各一）。
- 寫入中途失敗（替換 write 或 `fsync` 丟例外）→ 舊內容完整、目錄裡沒有暫存檔。
- 模擬 Windows：第一次 `os.replace` 丟 `PermissionError` → 重試後成功；每次都丟 → 重試完原樣丟出、舊內容完整、沒有暫存檔；`time.sleep` 換成記錄呼叫的假函式。
- 模擬 POSIX：`PermissionError` 不重試。
- 暫存檔的名字 `is_tmp_name` 回傳 `True`。
- `replace_text` 遇到 `OSError(ENOSPC)` 原樣丟出（不是 `VcpError`）（C2）。
- `train run` 中 `train.yaml` 的替換丟 `OSError(ENOSPC)` → ABORT `disk_full:`、`disk_evidence=vcp`，`train.yaml` 仍是替換前的完整內容。
- `eval ingest` 寫 `run.yaml` 時丟 `OSError(ENOSPC)` → ABORT，`reason=` 是原本的 `OSError` 文字、沒有 `disk_evidence=`（C2：契約不變）。

### 10.7 回歸門檻

- `tests/unit/test_regression_gate.py` 加一列 `0.16.0`，列出保護下列行為的測試：選取的寫入與冪等、`missing` / `slots` / `differs`；CLI 警告的 VERDICT；`low_disk:`（啟動前與失敗後）、`disk_full:`（console 與 vcp）、`tmp=`；原子替換的舊檔完整與 Windows 重試。

## 11. 文件

- `CLAUDE.md` 與 `AGENTS.md`（改一份、整份複製到另一份，`diff` 無輸出）：
  - 「提交治理」：事件清單加 `selected`（0.16.0 起才有，舊 vcp 讀到會 FAIL，讀寫同一份台帳的都先升級）、`select --record` 一句（核對只在 `board_rule: best`）、`selection_warn_hours`；
  - 「路徑」的 `runs/<id>/` 一段：`train.yaml`、`run.yaml` 的整份重寫是「暫存檔 → fsync → replace」；`env.<n>.json` 的 `disk`；`train status` 列 `.tmp`；
  - 「常用命令」：加 `vcp submit select`；`train run` 加 `--min-free-gib`。
- `docs/reference/cli.md`：新列 `vcp submit select`；改 `status` / `report`、`final`（選取的核對與 `next:` 只在 `board_rule: best`；名額取最新 `final` 列的 `slots`）、`score` / `sync`（`cli_warnings=`）、`upload`、`init`（profile 新鍵）、`train run`（`low_disk:` 的兩個時點、`disk_full:` 只由 `train run` 轉換、`free` 不反映 Windows 配額）、`train status`；「一次提交」的範例在 `final` 之後加 `select --record`。
- skills（`.claude/skills/` 改完 `cp -r` 到 `.agents/skills/`）：
  - `vcp-train-submit-backup`：`SKILL.md` 的流程（`final` → 平台勾選 → `select --record` → `status` 沒有 `selection=` 的 WARN）、常見錯誤加「勾錯」、「磁碟滿」與「`cli_warnings=` 一直 WARN 就是 CLI 該升級了」；`reference.md` 的命令表；
  - `vcp-running-contests`：`workflow.md` 第 10 步「決選」加選取的記錄與核對；`operator-guide.md` 對應段落；
  - `vcp-orientation`：`SKILL.md` 的命令表加 `select`；`map.md` 的版本沿革加 0.16.0。
- 設計 spec 修訂：提交治理 spec（`2026-09-05-vcp-submission-governance-design.md`）加第 33 條、訓練層 spec（`2026-09-05-vcp-training-layer-design.md`）加第 20 條，都指回本 spec。
- 後記：
  - 新增 `docs/superpowers/plans/2026-10-30-vcp-0160-selection-and-disk-followups.md`（實作裁決、已知限制、待辦）；
  - 在 `2026-10-23-rsna-wrapup-followups.md` 的 §1（1b）、§2、§3 各補一條處置；1b 的處置寫明 `status` 不提示，以及理由（裁決 R1）。
- `CHANGELOG.md`：不在功能分支改；發版 commit 加 `## [0.16.0]`，照 0.14.0 的格式寫 MINOR 的理由（§9）、Added / Changed / Fixed（原子替換）、相容性與升級（§8）、已知限制（§5.4、§12）。

## 12. 風險與待決

- **失敗後的空間比啟動門檻。** 門檻設得高、訓練本來就會吃掉大量空間時，無關的失敗也會報 `low_disk:`；措辭只說 may，`train.log.jsonl` 記了當時的可用空間。
- **Windows 的重試只等約 1.55 秒。** 更久的佔用（例如防毒掃描大檔）仍會讓替換失敗：那次寫入原樣丟出、舊檔完整，`train run` 照既有規則 ABORT。
- **`status` 看不到 CLI 過舊**（裁決 R1）：要等下一次 `sync` 或 `upload` 才 WARN。
- **`.tmp` 的掃描範圍**：從沒登記過 checkpoint 的目錄看不到。補法之一是把 `--checkpoints` / `--final` 的 glob 寫進 `started` 事件（`train.log.jsonl` 不驗欄位，不破舊版），`train status` 再掃它們的前綴。本版不做，待使用者決定。
  - 2026-10-31 註：已做——`started` 事件帶著 glob，`train status` 掃它們所在的目錄；見後記 `docs/superpowers/plans/2026-10-30-vcp-0160-selection-and-disk-followups.md` §2.1 第 19 條。
- **選取以 id 為單位。** 同一個 id 傳過兩次時，vcp 不知道勾的是哪一發。要分到每一發，得讓 `--record` 收 `<id>@<ref>`；目前沒有需要。
- **不能記「什麼都沒勾」。** 空的選取被拒（Kaggle 這時會自動用 public 最好的兩發）。要支援的話另加 `--none`。
- **`foreign:<ref>` 的 ref 若後來被 `_ours` 認成我們自己的一發**，標籤不會自動換成 id，`differs` 可能誤報。
- **`status` 的 WARN 變多**（只在 `board_rule: best`）。截止前 48 h 起沒記選取就 WARN；`final` 之後本來就因封槍 WARN。這是刻意的。平台其實有勾選、profile 卻寫 `last` 的比賽，選取完全不被核對：`board_rule` 要照平台設定。
- **data root 磁碟滿會停掉訓練**（C3）：即使 checkpoint 寫在另一顆磁碟。紀錄的完整優先；`--resume` 接續。
- **平台協定改型別。** 外部登記、回傳 list 的平台由包裝相容；直接呼叫 `KagglePlatform.list_submissions` 或 `parse_submissions` 的程式要改。

## 13. spec 作者的裁決

使用者決定與後記都沒定、由本 spec 決定的選擇（每條一行理由；標 R1–R3、C1–C5 的是 controller 2026-10-30 的裁決）：

1. 事件名 `selected`，必填 `chosen` + `source`，沿用 `final` 的 `chosen` 欄位：跟 `final` 比對最直接，不加新欄位。
2. `--record` 收逗號清單：同 `--trained-on`；id 不會含逗號。
3. 選取以 id 為單位：`final` 也是 id；同一個 id 的每一發是同一個檔。
4. 收 `foreign:<ref>`：隊友或網頁上傳的一發也勾得到；標籤格式沿用 `status` 的 `current`。
5. 必須已上傳；probe 與出錯的照記：平台只能勾上傳過的；vcp 記錄、不審查。
6. 拒絕空的選取：讓事件的意思固定是「勾了這些」；待決（§12）。
7. 不受封槍與截止擋：它記錄平台狀態，比照 `sync`、`score`。
8. 以集合判斷冪等、不寫重複列：平台的選取沒有順序。
9. 個數不對或跟 `final` 不同照寫、只 WARN：台帳要說平台上實際是什麼。
10. 鍵名 `selection_warn_hours`，`float`、`>= 0`，`null` 只關 `missing`，預設值不寫出：比照 `ledger` 的省略，0.16 寫的 profile 讓 0.15 照樣讀得動。
11. 沒有截止時，有 `final` 列就算「已到」：那是 vcp 唯一知道「接下來該勾選」的時刻。
12. （C4）`expected = min(名額, 可選條目數)`，名額是最新 `final` 列的 `slots`、沒有 `final` 才用 `final_slots`：`final --slots` 覆寫過的名額才是實際的決定；條目少於名額時不永遠 WARN。
13. `differs` 是集合不相等：照決定的字面；刻意避險的選法也會 WARN，讓人看見。
14. `status` 用 `selected=` + 一個 `selection=` 字彙欄位：好 grep，比照 `git_changed=` 的逗號清單。
15. `report` 依標籤把該 id 的每一發標 `selected`：最小的讀取端，比照 ` errored`。
16. `final` 不改排名與列，只在已有選取且不同時 WARN，並一律印 `next:`：決選只看 sealed 讀數。
17. 平台協定改回傳 `PlatformList`，list 照收：登記的外掛平台不壞。
18. （R1）`status` 不提示 CLI 過舊、警告不寫進台帳：`status` 從不呼叫平台；把操作面的事實寫進治理台帳（`configs` 模式進 git）代價大於缺口；`upload` 一律先同步，每次讀平台都會 WARN。偏離後記 §1 的 1b。
19. `cli_warnings=` 在 `sync` / `upload` 一律印、> 0 WARN：比照 `errored=` 的計數欄位。
20. 警告不跨命令累積，每次讀列表各自報：沒有地方存「上一次」（第 18 條），CLI 沒升級就每次 WARN。
21. 「outdated」提示以子字串判斷：CLI 的原文就是這個字，不另建字彙。
22. 磁碟量 run 目錄、`--cwd`、glob 前綴，以 `st_dev` 去重、取最小：「run 的輸出」同時是 vcp 的 run 目錄與 child 寫權重的地方。
23. `--min-free-gib`，`0` 不 ABORT、也關掉結束後的空間檢查，負數 FAIL：決定要的覆寫手段；`0` 讓空間小的機器仍能照記。
24. 檢查放在所有輸入檢查之後、`_close_running` 之前：先報輸入錯誤，ABORT 時一個檔都沒寫。
25. ABORT 用 `VcpError("low_disk: …")`：使用者指定 ABORT；比照 `kaggle_not_found`。
26. 量不到 → WARN `disk_check=unavailable`、不 ABORT：比照 `git_changed=unavailable`；探針壞了不擋訓練。
27. `free_gib=` 一律印：不打開 `env.json` 就看得到。
28. 快照記的是啟動前那次量測（`DiskCheck`），不重量：紀錄要顯示當時根據什麼做的決定。
29. （R2）失敗後先看 console 字樣（`disk_full:`），否則拿重量的空間比啟動門檻（`low_disk:`，措辭 may have filled），只判 `failed`：exit code 沒有可攜的意思；RSNA 失敗後約剩 2.2 GB，固定 1 GiB 的下限會漏掉促成這項的那一次。
30. `disk_evidence=console|vcp` 只跟 `disk_full:`：區分它的兩個來源；`low_disk:` 的措辭本身就說明不確定。
31. vcp 自己寫不下 → ABORT `disk_full:`、`disk_evidence=vcp`：同一個原因用同一個字。
32. 不加 `Attempt` 欄位，改寫 `train.log.jsonl` 的 `note`（`key` 記用了哪個字）：舊 vcp 嚴格驗 `train.yaml`。
33. 不比對在地化訊息：列不完，還可能誤配；Python 的代碼字樣已經涵蓋主要情況。
34. `.tmp` 掃 run 目錄（遞迴）加 checkpoint 所在目錄（不遞迴）：glob 沒存下來；遞迴掃 `--cwd` 可能是整個 repo 加資料。
35. `tmp=` 一律印、> 0 WARN，`running` 時也列：比照 `unbacked=`；那時本來就 WARN。
36. 測試的 conftest 預設換掉磁碟探針：CI runner 的空間不能決定既有測試的成敗。
37. 失敗後的空間不足沿用 `low_disk:`、狀態是 FAIL（啟動前是 ABORT）：同一個事實用同一個字，狀態說明它發生在哪個時點。
38. （R3）原子替換的 helper `replace_text` 放在 `vcp.core.atomic`，暫存檔名沿用 `_tmp_path`：同一套 fsync 與命名，`is_tmp_name` 認得殘檔。
39. （R3）`PermissionError` 只在 Windows 重試 5 次（共約 1.55 秒），POSIX 不重試：Windows 的檔案共用鎖是暫時的，POSIX 上它是真的權限錯誤。
40. （C1）選取的核對只在 `board_rule: best`；`last` 的 `selection_state` 一律 `none`、`status` 不因選取 WARN、`final` 不印 `next:`，`select --record` 照寫、`report` 照標：manual 與 `last` 的比賽沒有勾選這一步，否則會永遠 WARN，而且只能用 0.15 讀不動的鍵關掉。
41. （C2）`replace_text` 原樣丟出 `OSError`，磁碟滿轉成 `disk_full:` 只在 `train run` 第一次寫入之後做：其他呼叫 `dump_yaml_model` 的命令契約不變。
42. （C3）vcp 自己的 console 或紀錄寫不下時，照現行行為停掉 child（5-1）再 ABORT `disk_full:`：vcp 記不下的 run 不算有紀錄的 run，紀錄的完整勝過訓練的延續；清出空間後 `--resume`。
43. （C5）`free` 就是 `shutil.disk_usage().free`，Windows 上不反映使用者配額，不用 ctypes：標準函式庫的值可攜；配額由 console 的字樣補一部分（§5.4）。
