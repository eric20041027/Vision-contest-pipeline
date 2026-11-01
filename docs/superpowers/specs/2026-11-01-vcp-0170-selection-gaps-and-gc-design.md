# vcp 0.17.0：選取的兩個缺口、失敗時的 CLI 警告、磁碟盤點 `vcp gc scan` 設計

- 日期：2026-11-01
- 來源：0.16.0 後記 `docs/superpowers/plans/2026-10-30-vcp-0160-selection-and-disk-followups.md` §2.2 的第 6、7、12 條（第 7 條連同 §1 的 `foreign:<ref>` 誤報），與 RSNA 收尾後記 `docs/superpowers/plans/2026-10-23-rsna-wrapup-followups.md` §4（`gc`；經過見賽後報告 `docs/postmortems/2026-10-rsna-knee-abnormality-detection.md` §4.2）。
- 決定：使用者 2026-11-01 要這四項一起做成 0.17.0，spec 寫完直接實作，發版時再覆核，不另外停下來問。所以本 spec 的每個選擇都是 spec 作者的裁決：理由隨文寫，使用者可能想改的集中在 §13。
- 時程：框架 2026-11-13 起為課程凍結，範圍以「約一週、由計畫交給 subagent 實作」為準（§10.6 的順序）。
- 版本：下一個 MINOR（0.17.0），理由見 §9。

## 1. 問題

- **不能記「什麼都沒勾」。** 空的選取被拒（0.16.0 spec §12）。刻意讓平台自己挑的人，從截止前 48 小時起 `status` 永遠 `selection=missing`。
- **選取以 id 為單位。** 同一個 id 傳過兩次（`--force` 重傳、`final` 的 re-send）時，台帳說不出勾的是哪一發；`report` 把兩發都標 `selected`。另外，記成 `foreign:<ref>` 的一發後來被台帳認成自己的上傳（`_ours`），標籤不會換成 id：`differs` 誤報，`report` 也不標。
- **失敗的列表丟掉警告。** 0.16.0 只在成功的 `sync` / `upload` 與上傳本身失敗時帶 `cli_warnings=`。`sync_failed:`、CLI 非 0、`platform_response:` 的 FAIL 都沒有；某一頁內容壞掉時（`parse_submissions` 丟 `ValidationFailed`），這一頁與前面頁的警告行一起丟掉。RSNA 那次 `not JSON` 正是警告行引起的。
- **清磁碟沒有工具。** RSNA 磁碟只剩 4 GB 時，是逐一確認設定、run 卡、台帳與文件都沒有引用之後，才由人親手刪掉 8.9 GB 的重複上傳包與快取。vcp 沒有命令回答「這個大檔有沒有被誰引用」。

## 2. 範圍

目標：

- `select --none`、`--record <id>@<ref>`，以及讀取時把已被認成自己上傳的 `foreign:<ref>` 解析成 id（§3）。
- 列表的失敗與上傳前同步的失敗，VERDICT 也帶 `cli_warnings=`；剖析失敗的那一頁也帶它的警告行（§4）。
- 新命令 `vcp gc scan`：列出大項目、各自的類別與引用者，寫一份不可變的稽核紀錄（§5）。

非目標：

- 自動讀回平台的勾選：kaggle CLI 仍沒有對應旗標（0.16.0 spec §2 不變）。
- `--none` 時模擬平台會自動挑哪幾發。
- FAIL 時印出警告行本身或升級提示：只帶個數，行在 log（§4.2）。
- gc 建議刪什麼、比較兩次掃描、估算刪除之後的效果。

反目標（原則上不做）：

- **vcp 刪除任何東西。** gc 只列：沒有 `--apply`、`--delete`，以後也不在這個命令加。既有的 `vcp artifact clean --apply` 範圍不變。
- **以衍生索引判斷「沒有被引用」。** provenance 索引可能過期或不存在；gc 只讀正本（§5.3）。
- **呼叫平台、讀憑證。** `select` 照舊從不呼叫平台。
- **把操作面的事實寫進治理台帳**（0.16.0 的 R1 照舊：警告不寫台帳）。

## 3. 選取（後記 §2.2 第 6、7 條）

### 3.1 條目的寫法與解析

`selected` 列的 `chosen` 每一項是一個「條目」，三種寫法：

- `<id>`：這個 id 的某一發（0.16.0 的寫法，意思不變）。
- `<id>@<ref>`（新）：這個 id 在平台上的那一發 `<ref>`。以第一個 `@` 切開（id 經 `validate_name`，不含 `@`）；兩側去空白之後寫入。
- `foreign:<ref>`：同 0.16.0。先判斷前綴，所以 `foreign:` 的 ref 可以含 `@`。

台帳新增兩個讀法（`vcp.submit.ledger.SubmissionLedger`）：

- `refs_of(id) -> set[str]`：這個 id 的 `uploaded`、`scored`、`errored` 列帶的 `platform_ref`。`sync` 對上一發沒有 ref 的 vcp 上傳時不寫綁定列，ref 只在 `scored` / `errored` 列上，所以三種列都要看。
- `owners() -> dict[str, str]`：`_ours` 改回傳 `{ref: id}`（被認成自己上傳的 foreign ref → 那一發的 id），`arrivals()` 改用它的鍵，行為不變。

解析 `vcp.submit.select.resolve(ledger, entry) -> Resolved(label, submission_id, ref)`：

- `<id>` → 標籤 `<id>`、ref 無；`<id>@<ref>` → 標籤 `<id>`、ref `<ref>`；
- `foreign:<ref>`：ref 在 `owners()` → 標籤是那個 id、ref `<ref>`；否則標籤 `foreign:<ref>`。
- **標籤是比對的單位**，跟 `final` 的 `chosen`、`arrivals()` 的標籤同一種。`@ref` 只決定「哪一發」（`report` 標哪一列、檢查同一發有沒有記兩次），不改變跟 `final` 比的單位。
- 解析在讀取時做、不在寫入時改寫：ref 的歸屬會在之後的 `sync` 改變（這正是誤報的來源），台帳記的是人當時說的。

### 3.2 `select --record` 的檢查與寫入

```
uv run vcp submit select --dataset T --record S5@123,S3 [--message "…"]
uv run vcp submit select --dataset T --none [--message "…"]
```

- `--record` 改成選填，新旗標 `--none`；兩者恰好一個。
- 跟 0.16.0 一樣，所有檢查都在台帳交易裡、拿到鎖讀完台帳之後（0.16.0 後記 §2.1 第 7 條：鎖被佔時連壞輸入也是 ABORT `locked:`）。任一項不過就 FAIL、一列都不寫：
  1. `invalid:`：`--record` 與 `--none` 都給或都沒給；`--record` 去空白後沒有任何項；同一項兩次；`@` 的左邊或右邊是空的；同一個 id 同時以 `<id>` 與 `<id>@…` 出現（「這個 id 的某一發」與「這一發」放在一起說不清）；同一個 ref 被兩項指到（`S3@123,foreign:123`）。
  2. 依輸入順序逐項：
     - `foreign:<ref>`：ref 不在 `foreign` 列 → `not_found:`（同 0.16.0）；ref 在某個 id 的 `refs_of` 裡時，訊息加一句「記成 `<id>@<ref>`」。
     - `<id>`、`<id>@<ref>`：沒有 `staged` 列 → `not_staged:`；沒有 `uploaded` 列 → `not_uploaded:`（同 0.16.0）。
     - `<id>@<ref>`：ref 不在 `refs_of(id)` → `not_found:`（`id=` 是那一項）；ref 是別的 id 的 → 訊息說是誰的。
- 冪等照舊比「條目的集合」，以字面比：`S3` 改記成 `S3@123` 是一筆更精確的紀錄，要寫。
- `foreign:<ref>` 而 ref 已在 `owners()`：照記，人看的行多一句 `note: foreign:<ref> is <id>'s upload; it is read as <id>`；VERDICT 不變。
- 不受封槍、截止擋；從不呼叫平台；個數或內容看起來不對照寫、只 WARN（0.16.0 §3.2 的原則不變）。

### 3.3 `--none`

- 寫一列 `selected`，`chosen: []`、`source=manual`、選填 `message`。
- schema：`selected` 的 `chosen` 仍必填（欄位不能缺），但可以是空 list；重複照舊拒絕。舊 vcp 讀到空的 `chosen` 會 FAIL（§8）。
- 意思：平台上一發都沒勾，平台照自己的規則決定（後記 §2.2 第 6 條說 Kaggle 這時自動用 public 最好的兩發；vcp 不模擬、也沒有驗證）。
- 冪等：最新一列也是空的 → 不寫。之後再 `--record` 就以新列為準。

### 3.4 選取狀態（改 0.16.0 §3.4）

`selection_state` 的輸入輸出不變，多一個欄位、多一個字：

- `resolved`：`chosen` 每一項的標籤（順序同 `chosen`）；沒有 `selected` 列時 `None`。`--json` 的 `selection` 物件多這個鍵。
- `expected` 不變（0.16.0 的 C4：`min(名額, arrivals() 的不同標籤數)`，至少 1）。
- `words`（只有 `board_rule: best`；`last` 一律 `none`，同 C1）：
  - 沒有 `selected` 列：`missing` 或 `none`（不變）；
  - 最新一列是空的：`unticked`；有 `final` 列時 `unticked,differs`。不判 `slots`：沒勾是決定，不是數錯；
  - 其他：
    - `slots`：**不同標籤數** `len(set(resolved))` 不等於 `expected`（把兩格都勾在同一個 id 的兩發上，只算一個 submission）；
    - `differs`：有 `final` 而 `set(resolved) != set(final.chosen)`。
- `NO_WARNING` 加 `unticked`：單獨的 `unticked` 不 WARN；有 `final` 時由 `differs` WARN，因為平台會計哪幾發 vcp 不知道，無法說它等於 `final`。
- 人看的行（`selection_lines`）：
  - `slots`：`selection: <n> recorded (<k> distinct), <expected> expected (slots=<s>)`；`(<k> distinct)` 只在 `k != n` 時出現，所以 0.16.0 的行一個位元組都不變；
  - `differs`：兩邊的差集以標籤算，`recorded` 照台帳的寫法印；
  - `unticked,differs`：一行 `selection: final chose <a,b>; nothing ticked (recorded <ts>): the platform picks by its own rule`。

### 3.5 讀取端

- **`submit status`**：`selected=<n>`，`n` 是條目數（`--none` 之後是 `0`；沒有列仍是 `none`）；`selection=` 多 `unticked`。WARN 規則不變（`selection_state.warns`）。
- **`submit report`**：`ReportRow.selected` 的判斷改成依條目：
  - 我們的一發（`uploaded` 列，id `S`）：有 `<S>` 條目 → 標（0.16.0 行為）；有標籤為 `S`、ref 等於這一發的 ref 的條目 → 標。這一發的 ref 是列上的 `platform_ref`，沒有就取 `assign_scores` 歸給它的結果列的 `platform_ref`，都沒有就只會被 `<S>` 標到。
  - 別人的一發（`foreign` 列）：有 `foreign:<這一發的 ref>` 條目 → 標（同 0.16.0；`arrivals()` 裡的 foreign 列本來就沒有被認成自己的）。
  - `--none` 之後一列都不標。
- **`submit final`**：`_differing_selection` 改用 §3.1 的標籤比集合（否則 `S3@123` 永遠跟 `S3` 不同）。已記的是空選取 → `selection=differs`，人看的行用 §3.4 的 `unticked` 句子。排名、`final` 列與 `next:` 不變。

## 4. 失敗的列表也帶 `cli_warnings=`（後記 §2.2 第 12 條）

### 4.1 例外帶著警告行

- `cli_warnings: tuple[str, ...] = ()` 從 `PlatformError` 搬到 `VcpError`（關鍵字參數；`PlatformError` 的建構方式不變）。任何 vcp 例外都能帶警告行，不必為了帶它而改例外類別：改類別會改 `reason=` 開頭的類別名。
- `kaggle.parse_submissions`：`_past_warnings` 之後丟出的 `ValidationFailed`（`not JSON`、缺鍵、分數或日期解析不了）帶著這一頁的警告行。
- `KagglePlatform.list_submissions`：合併前面頁警告的 `except` 從 `PlatformError` 放寬到 `VcpError`，所以後一頁內容壞掉時，前面頁的行也跟著例外走。
- `_read_back` 不用改：它已用 `getattr(e, "cli_warnings", ())`，現在剖析失敗的那次查看也帶回來。
- 新 helper `vcp.submit.platforms.base.with_cli_warnings(e, *groups)`：把各組行併進 `e.cli_warnings`（`merge_warnings`），並 `e.fields.setdefault("cli_warnings", <行數>)`，不蓋過失敗本身的欄位；回傳 `e` 讓呼叫端 `raise`。

### 4.2 哪些失敗帶欄位

- **`submit sync`**：從 `list_submissions` 呼叫開始、到 `reconcile` 結束為止丟出的任何 `VcpError`，VERDICT 都帶 `cli_warnings=<n>`。`n` 是例外帶的行，加上列表已經回來時 `listed.warnings` 的行（去重）。
  - `PlatformTimeout` 照舊轉成 `sync_failed:`，新的 `ValidationFailed` 帶上原例外的行；
  - CLI 非 0、翻頁超過上限（`PlatformError`）、`platform_response:`（剖析或 `reconcile` 的 `_row`）、`reconcile` 裡讀 `stage.json` 失敗，都帶；
  - `kaggle_not_found:` 在任何 CLI 呼叫之前發生，但在這個範圍裡：`cli_warnings=0`；
  - 這個範圍之前的失敗（`manual_platform:`、`locked:`、`not_adopted:`、profile 或台帳讀不動）不帶，比照 0.16.0「上傳前同步之前的失敗不帶 `sync=`」。
- **`submit upload`**：上傳前同步的 `sync_failed:` 帶 `cli_warnings=`（同步讀到的行加上例外帶的行）。`sync=`、`bound=` 照舊不帶：同步沒有完成。之後各步的失敗照 0.16.0。
- 只帶個數。`run_command` 在失敗時不印人看的行，警告行本身在 log（`_past_warnings` 自 0.15.1 起每行一筆 WARNING `kaggle CLI: …`）；FAIL 也不印升級提示（§13 第 12 條）。
- `OSError`（ABORT）不帶：它沒有 `fields`，失敗的是本機，不是平台。

## 5. `vcp gc scan`（RSNA 收尾後記 §4）

### 5.1 命令

```
uv run vcp gc scan [--root DIR]… [--min-mib M] [--no-record] [--json]
```

- 新命令群組 `vcp gc`，目前只有 `scan`：名字本身說明它只看。後記與賽後報告寫的 `vcp gc --dry-run` 不採用：一個永遠必填的旗標暗示有一天會有不帶它的刪除模式，反目標（§2）說不會有。
- 不刪、不改任何既有檔。寫入只有兩處：每個命令都寫的每日 log（`run_command` 在命令本體之前就建 `logs/`、寫 VERDICT，gc 不例外）與 §5.7 的稽核產物（`--no-record` 時只剩 log）。不上鎖：台帳以唯讀方式讀（`read_only`，略過寫到一半的最後一列）。
- `--root DIR`（可重複）：另外遞迴掃的目錄。不存在或不是目錄 → FAIL `not_found:`。
- `--min-mib M`：列出的項目門檻，預設 `100`（MiB = 1024² bytes）。不是有限數或 `< 0` → FAIL `invalid:`；`0` 列出每個項目。門檻只影響列出哪些項目，各類別的總量一律算全部。
- data root 不另外檢查是否存在：`run_command` 已經先建了 `logs/`，跟其他命令一樣。打錯的 `--data-root` 掃出來只有 `logs/` 的一項 `record`，人一眼看得出來。找不到 configs root → 既有的錯誤（`resolve_configs_root`）。
- 永遠讀整個 data root 與這個 configs root 的所有引用，沒有 `--dataset`：只讀一部分的引用，會把別的 dataset 用的檔說成沒被引用。

### 5.2 掃哪裡

1. **data root**，遞迴。
2. **檢查點目錄**，不遞迴：每個有 `train.yaml` 的 run，其已登記 checkpoint 所在的目錄，加上 `started` 事件裡 `--checkpoints` / `--final` glob 所在的目錄。規則與 `train status` 找 `.tmp` 的相同（0.16.0 後記 §2.1 第 19 條）：從 `vcp.train.status` 抽出公開的 `checkpoint_dirs(data_root, record) -> set[Path]`，`tmp_files` 與 gc 共用。未登記的中間 checkpoint、半途的 `.tmp` 通常就在這裡。
3. **`--root`**，遞迴。

- 根目錄先 `resolve()`。**規則跟著路徑走，不跟著根走**：data root 底下的檔一律套 data root 的分類與單位（§5.4、§5.5），即使是從 `--root ~` 走進來的；檢查點目錄直接底下的檔一律逐檔為單位；其餘 `--root` 底下的檔才以「根的直接子項」為單位。走 `--root` 時遇到 data root 就不往下走（它自己是一個根）；每個檔只算一次（以 `resolve()` 後的路徑去重）。`scan.json` 的 `roots` 照列每個根，被別的根包住的也列。
- 不跟隨符號連結與 junction（不往下走、也不計大小）：避免繞圈與把根以外的東西算進來。
- 讀不了的目錄（`PermissionError` 等 `OSError`）記進 `unreadable`、跳過、不 ABORT（跟 0.17.0 起的 `train status` 一樣，見 §6 的第 10 條）。
- 依目錄彙總，不把每個檔留在記憶體：`raw/` 可能有幾十萬個檔。
- 預設不掃各 run 的 `--cwd`：那常是整個 repo 加上 venv，要看就用 `--root`。

### 5.3 讀哪些引用（只讀正本）

| 來源 | 被引用的路徑 | role | 引用者 |
|---|---|---|---|
| data root 每個 `runs/<id>/run.yaml`（不論屬於哪個 dataset） | backup 的 `Collector.walk_run` 列的每個檔：run 卡、預測、`history.jsonl`、`fuse.json` 與配方、`train.yaml`、`train.log.jsonl`、`train/`、每個 checkpoint（含 `train upload` 驗過的副本）、收據、source audit、證據與標籤集產物 | 沿用 backup 的 role | `run:<id>` |
| 同一個 run 的 `train.yaml` | `exports[].dir`（目錄）；`kind: local` 的 `uploads`（`<dest>/<run>/<name>`）；`venv` 與每個 attempt 的 `venv`（目錄）；`config.path` | `export`、`upload_copy`、`venv`、`config` | `run:<id>` |
| 這個 configs root 的每個 dataset | Collector 的 dataset 走法：`dataset.yaml`、plan、unseal 紀錄、量測台帳、`samples.jsonl`、`raw_manifest.txt`；每個預登記；每個 staged submission 的 `stage.json`、上傳檔與台帳，以及它們走到的 run；`dataset.yaml` 的 `image_root`（目錄） | 同上；`image_root` | `dataset:<name>`、`judgement:<name>/<prereg>`、`submission:<name>/<id>` |
| 每份 backup 清單 | 每個 `FileEntry` 的本機路徑（`local_path`） | 清單記的 role | `backup:<name>/<manifest>` |
| `backup.log.jsonl` 的 `push` 列 | `dest_kind == "local"` 的目的地整棵 | `backup_dest` | `backup:<name>/<manifest>` |
| data root 每個有 `manifest.json` 的產物 | `spec.inputs[].path` | `artifact_input` | `artifact:<kind>/<id>` |

- **不複製走法。** `vcp.backup.evidence.Collector` 的 `add` 抽成可替換的記錄端：backup 用原本的（雜湊、建 `FileEntry`），gc 用不雜湊的（只記路徑、role、結論、記錄裡的 sha 與大小、`remote`）。`walk_all` 加參數 `split: bool = False`：`True` 時每個 run、預登記、submission 各用自己的結論字串（上表的引用者），dataset 本身的檔用 `dataset:<name>`，不列 `logs/`（gc 以版面把它歸 `record`）。backup 不傳，清單的位元組不變。
- configs root 沒有 card 的 run（別的 checkout 或別的專案共用這個 data root）仍以 `walk_run` 走過，所以它的 checkpoint 不會被說成沒有引用。
- 引用者字串是 `<結論>#<role>`，例如 `run:r7#checkpoint_final`、`submission:t/S3#artifact`。目錄引用（export、venv、`image_root`、`backup_dest`）涵蓋底下每個檔。路徑一律 `resolve()`、在 Windows 上 `normcase` 之後比對。
- **不讀 provenance 索引**：它是衍生、可刪、可能過期或不存在的索引，而且記的是 entity，沒有 checkpoint 或上傳包的檔案路徑。依賴它，錯的方向正好是把有用的檔說成沒被引用。索引檔本身歸 `rebuildable`（§5.4）。
- 走法失敗記進 `skipped`、跳過，WARN（§5.8）。gc 的容忍比 backup 寬：每個走法單位（一個 run、一個 dataset、一個預登記、一個 submission、一份 backup 清單、一個產物）各自包一層，接 `VcpError`、`OSError`、`ValueError`（含 `UnicodeDecodeError` 與 pydantic 的驗證錯誤）；其他例外是程式錯誤，照舊 ABORT。backup 的 `_try` 不變。
- gc 的記錄端以走法單位為交易：一個單位的引用先暫存，走完才併入；單位失敗就整批丟掉，不會留下「已被 skip 的 submission 仍掛在別的檔上當引用者」（Collector 的 `add` 對已存在的路徑是附加 `for_`，backup 的回滾不撤這一筆）。
- 只記 sha、沒有路徑的權重（`eval ingest --weights` 的 `source.weights_hash`、kernel 提交的 `WeightRef`）不算引用：沒有路徑可對（§12）。
- 記錄說有、磁碟上沒有的路徑記進 `missing`（只計數，不 WARN：刪掉本機副本、只留遠端是正常的）。

### 5.4 分類

每個檔依序套用，第一條成立的就是它的類別：

| 順序 | 類別 | 條件 |
|---|---|---|
| 1 | `protected` | data root 的 `raw/`、`locks/` 底下；任何根裡的 `.git` 目錄底下 |
| 2 | `debris` | 檔名以 `.tmp` 結尾（不分大小寫）；有 `spec.json` 沒有 `manifest.json` 的半途產物目錄裡的檔 |
| 3 | `referenced` | 這個檔或它的上層目錄被 §5.3 的任一來源引用 |
| 4 | `rebuildable` | `datasets/<name>/cache/materialize/` 底下（`vcp data materialize` 重建）；`datasets/<name>/cache/audit/` 與 `cache/dhash.jsonl`（`vcp data audit` 重建）；`indexes/` 底下（`vcp provenance rebuild` 重建） |
| 5 | `record` | vcp 自己的版面、但沒有任何引用走到：走法失敗（`skipped`）的 run 的 `runs/<id>/`；`runs/<id>/inputs/`（`eval ingest --keep-input` 留的來源副本，run 卡沒有欄位記它）；有 `manifest.json` 的產物目錄與 `artifacts/<kind>/supersession.jsonl`；`measure/`、`submit/`、`logs/`；`datasets/<name>/` 的 `samples.jsonl`、`raw_manifest.txt`，與 `cache/` 底下其他的檔（例如 `import_skipped.jsonl`，只有重新匯入才會重寫）；`--root` 涵蓋到的 configs root |
| 6 | `unreferenced` | 其他（含 `artifacts/<kind>/` 底下兩個檔都沒有的外來目錄） |

- `protected` 排第一：`raw/` 照鐵則永不動，即使裡面有 `.tmp`。
- 走得到的 run 目錄裡、沒有任何紀錄提到的檔（例如有人丟進去的大檔）是 `unreferenced`，不是 `record`：vcp 自己寫的 run 檔，除了上表點名的 `inputs/`，都在走法裡。沒有 `run.yaml` 的 `runs/<x>/` 也是 `unreferenced`。
- `record` 不是垃圾：那是 vcp 的紀錄，只是沒有結論再依賴它（例如別的 checkout 的 dataset、被放棄的 run）。要不要整批放棄是人的決定，vcp 不說它可以刪。
- `unreferenced` 只表示「vcp 的紀錄沒有提到」，不表示沒有別的東西需要它（筆記、notebook、別的工具）。RSNA 那次人工也看了文件；這句寫進每次輸出的最後一行。

### 5.5 項目、門檻與複本

- 項目是一個檔，或一個目錄：目錄底下每個檔的（類別、引用者、`copy`）都相同時，往上合併成一項，但不超過「單位」：

| 位置 | 單位 |
|---|---|
| `raw/<name>/…` | `raw/<name>` |
| `datasets/<name>/cache/materialize/<mode>[-r<長邊>]/…` | 那一個模式目錄 |
| `datasets/<name>/cache/` 底下的其他項目（`audit/`、`dhash.jsonl`、`import_skipped.jsonl`…） | `cache/` 的直接子項 |
| `datasets/<name>/` 的其他 | `datasets/<name>` |
| `runs/<id>/…`、`measure/<name>/…`、`submit/<test>/<id>/…` | 那一層 |
| `artifacts/<kind>/<id>/…` | `artifacts/<kind>/<id>` |
| `artifacts/<kind>/`、`submit/<test>/` 直接底下的檔（`supersession.jsonl`、共用台帳） | 檔本身 |
| `indexes/`、`logs/`、`locks/`、data root 的其他頂層項目 | 那個頂層項目 |
| `--root` 底下 | 根的每個直接子項 |
| 檢查點目錄（不遞迴） | 每個檔 |

- 位元組 `>= --min-mib` 的項目才列出，依位元組由大到小、同大小依路徑排序。
- `copy`：被引用的 checkpoint 有 `train upload` 驗過的副本時（Collector 的 `remote`），記目的地。backup 推過的副本不逐檔判斷（§12）。
- **重複檔**：類別是 `record` 或 `unreferenced`、大小 `>= max(1 MiB, 門檻)` 的檔，大小等於某個「有記錄 sha」的被引用檔（checkpoint、產物檔、stage 的上傳檔、預測檔；大小取記錄的，沒有就 `stat`）時才雜湊；sha 相同 → `duplicate_of` 記那個被引用檔的路徑。只雜湊大小相符的候選：逐位元相同的權重複本（RSNA 的重複上傳包若是這種）會被認出來，雜湊的量以候選為界。打包成 zip 的認不出來（§12）。目錄項目記 `duplicate_bytes`。

### 5.6 輸出

- 人看的行，每項一行：`<類別> <大小> <路徑>[/ (files=<n>)] [by <引用者>; …; +<k> more] [copy <dest>] [duplicate of <path>]`。大小 `>= 1 GiB` 印 GiB、否則 MiB，都無條件捨去到一位小數（沿用 `vcp.train.disk._gib` 的做法）；引用者最多印三個。
- 接著：每類一行總量、`skipped:` / `unreadable:` / `running:` 各行，最後一行：`gc scan <id>: listed only; vcp deletes nothing. unreferenced means no vcp record names it, not that nothing else needs it`。
- `--json` 的 result 是 §5.7 的 `scan.json` 加上 `items`（全部列出的項目）。

### 5.7 稽核紀錄：產物 `gc_scan`

- 掃完之後才用 `ArtifactWriter` 開一個產物 `artifacts/gc_scan/<id>/`；掃描中途失敗不留半途目錄。id 是 `gc-<YYYYMMDDTHHMMSSZ>-<8 位十六進位 nonce>`。
- 這是 CLAUDE.md 裡最合適的不可變等級：寫一次、逐檔 sha、`manifest.json` 是 commit 點；之後人刪了某個項目，這份紀錄就是「當時 vcp 找不到任何引用」的證據。比照 access receipt 與 source audit 的做法。
- `spec`：`kind=gc_scan`、`params={"min_bytes": …, "root_options": <--root 的個數>}`，沒有 `inputs`（為了 input 雜湊整個 data root 不划算）。實際掃了哪些根在 `scan.json` 的 `roots`。
- 檔案：
  - `scan.json`：`scanned_at`、`finished_at`、`data_root`、`configs_root`、`roots`（每項 `path`、`recursive`、`source=data_root|checkpoint_dir|option`）、`min_bytes`、`totals`（每類 `bytes`、`files`）、`duplicates`（`bytes`、`files`）、`items`（列出的項目數）、`references`（讀了幾個 run、dataset、backup 清單、產物）、`missing`、`skipped`、`unreadable`、`running`；
  - `items.jsonl`：每個列出的項目一行：`path`、`kind=file|dir`、`bytes`、`files`、`class`、`referrers`、`copy`、`duplicate_of`、`duplicate_bytes`、`mtime`（最新的 mtime，`stamp` 格式）。
  - 路徑一律 `store_path`：data root 底下是相對路徑，其他是絕對路徑。產物在 data root，不進 git。
- 每次掃描一個新產物，同 id 不重開；不 supersede 舊的掃描。它會被 `artifact status` 列出、被 `provenance sync` 當成一般產物索引；半途目錄由 `artifact clean` 處理（它們都不認 kind）。
- `--no-record`：不寫產物，VERDICT `scan=none`，狀態不受影響。用在不能寫的 data root（唯讀快照、別人的機器）。

### 5.8 不完整與並行

- WARN 的條件（其他情形 OK；找到 `debris` 或 `unreferenced` 是這個命令的本分，不 WARN）：
  - `skipped > 0`：有引用沒讀到，它們引用的檔可能被說成 `unreferenced`。人看的行說是哪一個與原因。
  - `unreadable > 0`：有目錄沒看到。
  - `running > 0`：有 run 的最新 attempt 還是 `running`（讀 `train.yaml`），它正在寫的檔還沒登記。久未接續的 `running` 也算，`train status` 本來就 WARN 它。
- 不上鎖、不暫停訓練：結果是掃描當下的快照。

## 6. 同版的 PATCH 項（不在本 spec 設計）

0.17.0 另外帶兩項 PATCH 等級的修正，正由 0.16.0 後記平行實作，這裡只列出來，讓 release notes 完整：

- 後記 §2.2 第 11 條：其他 append-only jsonl（量測台帳、`prereg.log.jsonl`、`supersession.jsonl`、`history.jsonl`、`anchors.log.jsonl`、`unseal.jsonl`、`backup.log.jsonl`、每日 log、`submissions.jsonl`）改用整列 append，寫到一半失敗不留半列。
- 後記 §2.2 第 10 條：審查留下的小項，與 `status_cmd`、`_final_locked`、`train_run` 等長函式的拆分。

本 spec 會碰到 `cli_submit.status_cmd`、`final._final_locked`、`train.status.tmp_files`、`append_ledger_row` 的呼叫者，所以**實作從這兩項都合併進 `main` 之後開始**。第 10 條已讓 `train status` 略過讀不了的目錄、不跟隨符號連結，`tmp_files` 改回傳帶略過紀錄的結果；`checkpoint_dirs`（§5.2）在那之上抽出。

## 7. VERDICT 與字彙

| 命令 | 變更 |
|---|---|
| `submit select` | 新旗標 `--none`；`--record` 收 `<id>@<ref>`；`selected=0`（`--none`）；`selection=` 多 `unticked`（單獨 OK）與 `unticked,differs`（WARN）；FAIL 用既有字彙 `invalid:` `not_staged:` `not_uploaded:` `not_found:` |
| `submit status` | `selected=` 可以是 `0`；`selection=none\|ok\|missing\|unticked\|slots\|differs`（可逗號相連）；`--json` 的 `selection.resolved` |
| `submit report` | `selected` 依條目判斷（§3.5）；欄位不變 |
| `submit final` | `selection=differs` 也在已記空選取時出現；比對以標籤為準 |
| `submit sync` | 失敗（FAIL，與 `kaggle_not_found:` 的 ABORT）也帶 `cli_warnings=`（§4.2 的範圍） |
| `submit upload` | `sync_failed:` 帶 `cli_warnings=` |
| `gc scan`（新） | `scan=<id>\|none` `roots=`（實際掃的根數：data root、檢查點目錄、`--root`，`resolve()` 後去重） `root_options=`（`--root` 的個數） `items=` `min_mib=` `referenced_gib=` `record_gib=` `rebuildable_gib=` `debris_gib=` `unreferenced_gib=` `protected_gib=` `duplicate_gib=` `missing=` `skipped=` `unreadable=` `running=`；後三項任一 `> 0` → WARN；FAIL `invalid:`、`not_found:`；寫產物失敗 → ABORT（原例外） |

- exit code 照鐵則 2：OK / WARN 0、FAIL 1、ABORT 2。`--json` 時結果到 stdout、VERDICT 到 stderr。
- `gc scan` 的 `run_command(context=)` 帶 `min_mib=`（有給時）與 `root_options=`（有給 `--root` 時）；`roots=` 只在掃描完成後出現，同一個鍵在成功與失敗時意思不變。
- **沒有新的 `reason=` 字彙。** 新的 VERDICT 欄位與字：`selection=unticked`、`resolved`（JSON）、`gc scan` 的各欄位、FAIL 時的 `cli_warnings=`。

## 8. 相容性與升級

- **空的 `chosen`（`select --none`）**：0.16.0 讀到會 FAIL `bad ledger row`（schema 拒絕空選取；每個讀者都嚴格驗每一列）。會 FAIL 的同 0.16.0 spec §8：每個 submit 寫入命令、唯讀的 `status`、`report`，與讀台帳的 `backup manifest`、`backup verify`。
- 升級順序：一份台帳的每個讀者與寫者都先升到 0.17.0，才第一次跑 `select --none`（`configs` 台帳：共用它的每個 checkout；`shared` 正本：同一個 data root 的每個 worktree）。跟 0.16.0 的 `selected` 一樣，這一列只由人下的命令寫，在那之前 0.16 與 0.17 可以共用台帳；誰先記 `--none`，誰負責先讓大家升級。
- **`<id>@<ref>` 條目**：0.16.0 照常載入（`chosen` 仍是非空、不重複的字串 list），但讀錯：`status` / `select` / `final` 可能誤報 `differs`，條目數與標籤數不同時誤報 `slots`，`report` 不標這些列。只會多 WARN、不會少 WARN、不會 FAIL：建議升級，但不擋。
- **`foreign:<ref>` 的解析**：只改讀法，台帳不變；同一份台帳 0.17.0 的 `differs` 比 0.16.0 少（少的都是誤報）。
- **CLI 警告**：只在 VERDICT，台帳與產物不變。`VcpError` 多一個屬性；`PlatformError(cli_warnings=)` 照舊可用。
- **`gc_scan` 產物**：新 kind。舊 vcp 的 `artifact status` / `verify` / `clean` 與 provenance 索引對 kind 一視同仁，照常處理。Collector 的重構不改任何 backup 清單的位元組（§10.4 的 golden）。
- **`_ours` 改回傳 dict**：內部函式，沒有外部呼叫者。
- 不需要遷移：0.16.0 寫的每一列在 0.17.0 意思不變。

## 9. 版本

0.17.0（MINOR），依 CHANGELOG「版本規則」：

- 台帳內容：`selected` 的 `chosen` 可以是空的；條目多一種寫法 `<id>@<ref>`。
- 產物：新 kind `gc_scan`（`scan.json`、`items.jsonl`）。
- CLI 契約：新命令 `vcp gc scan`；`submit select` 的 `--none` 與 `--record` 的新寫法；新字 `selection=unticked`；`--json` 的 `selection.resolved`；FAIL 的 `sync` 與 `sync_failed:` 的 `upload` 多 `cli_warnings=`；`final`、`report` 的比對改以標籤為準。
- API：`VcpError.cli_warnings`；`SubmissionLedger.refs_of`、`owners`；`vcp.train.status.checkpoint_dirs`；`Collector` 的記錄端與 `walk_all(split=)`。
- §6 的兩項 PATCH 隨本版發出。`.claude/.claude-plugin/plugin.json` 跟著改成 0.17.0。發版仍要使用者同意。

## 10. 測試（先寫、先紅；只用 `tmp_path` 下的 data root，從不碰真的 data root）

### 10.1 選取

- schema：`selected` 的 `chosen: []` 可以載入；缺 `chosen`、重複項照舊 FAIL。
- `select --none`：寫一列 `chosen: []`、`selected=0`；再跑 → `written=false`；`--none` 加 `--record`、兩者都沒有 → `invalid:`；鎖被佔 → `locked:`（壞輸入也是）；封槍之後照記。
- `--record` 的 `@`：
  - ref 在 `uploaded` 列；ref 只在 `scored` / `errored` 列（未確認的 vcp 上傳被 `sync` 對上）→ 照記；
  - `S3@`、`@123`、`S3,S3@123`、`S3@123,foreign:123` → `invalid:`；ref 不屬於任何 id、屬於別的 id → `not_found:`（訊息說是誰的）；未 stage、未上傳照舊；
  - 字面不同的同一發（`S3` → `S3@123`）→ 寫新列。
- `selection_state`：
  - `--none`、沒有 `final` → `unticked`、不 WARN；有 `final` → `unticked,differs`、WARN；不再 `missing`；`board_rule: last` → `none`；
  - `S3@1,S5@2` 對 `final` `S3,S5` → `ok`；`S3@1,S3@2` 對 `S3,S5`、slots 2 → `slots,differs`，行裡有 `(1 distinct)`；
  - `foreign:k9` 之後被綁成 S1 的上傳、`final` 選 S1 → `ok`（0.16.0 是 `differs`）；`resolved` 是 `["S1"]`；
  - 0.16.0 的 `selection_lines` 測試全部照過（golden：`k == n` 時行不變）。
- `report`：`S3@2` 只標 ref 2 那一發、裸 `S3` 標兩發；被綁的 `foreign:k9` 標 S1 帶 k9 的那一發；`--none` 之後全不標。
- `final`：已記 `S3@1`、選出 S3 → 沒有 `selection=`；已記空選取 → `selection=differs` 與 `unticked` 句子；`final` 列的位元組除 `ts` 外不變（0.16.0 後記 §2.1 第 6 條的 golden）。
- `select` 對已被綁的 `foreign:<ref>` 印 `note:` 行、VERDICT 不變。

### 10.2 CLI 警告（假 CLI）

- `parse_submissions`：先警告、後非 JSON → `ValidationFailed` 帶那一行；缺鍵、分數解析不了同樣。
- `list_submissions`：第 1 頁有警告、第 2 頁非 JSON → 例外帶第 1、2 頁的行（去重）；第 2 頁 CLI 非 0 照 0.16.0。
- `sync`：逾時 → `sync_failed:`、`cli_warnings=1`；CLI 非 0 → `PlatformError`、`cli_warnings=`、`exit_code=` 不被蓋掉；非 JSON → `cli_warnings=1`；列表好、`reconcile` 的 `platform_response:` → `cli_warnings=`；`kaggle_not_found:` → `cli_warnings=0`；`manual_platform:`、`locked:` → 沒有這個欄位。
- `upload`：上傳前同步非 JSON → `sync_failed:`、`cli_warnings=1`、沒有 `sync=`；回讀時某次查看剖析失敗 → 它前面看到的行仍計入。
- 台帳在每種失敗前後位元組相同。

### 10.3 gc 掃描（合成的 data root 與 `--root`）

- 分類：`raw/` 的檔 → `protected`（含裡面的 `.tmp`）；`.tmp` 與半途產物 → `debris`；`cache/materialize/`、`cache/audit/`、`cache/dhash.jsonl`、`indexes/` → `rebuildable`；`cache/import_skipped.jsonl`、`runs/<id>/inputs/`、沒有引用的 run 目錄、產物、`samples.jsonl` → `record`；data root 頂層的不明大檔與 `--root` 裡的 → `unreferenced`；`.git` → `protected`。
- 單位：兩個 materialize 模式（`png`、`npy-r256`）是兩項，不併成 `cache/materialize`。
- 根互相包含：`--root` 是 data root 的上層時，data root 底下的檔仍套 data root 的類別與單位（`raw/<name>` 仍是一項），每個檔只算一次。
- 走法容忍：run 卡不是 UTF-8（`UnicodeDecodeError`）、讀 run 卡時 `PermissionError` → `skipped=1`、不 ABORT；一個 submission 的走法在中途失敗時，它已經走到的檔不帶 `submission:` 引用者（記錄端的交易）。
- 引用：被 submission 用的 checkpoint 同時有 `run:` 與 `submission:` 引用者；`train upload` 驗過的 → `copy`；export 目錄、venv、本機上傳副本、外部 `image_root`、本機 backup 目的地、產物 input → `referenced`；backup 清單列的舊 checkpoint 版本 → `backup:` 引用者。
- 別的 dataset（configs 沒有 card）的 run 的外部 checkpoint → `referenced`，不是 `unreferenced`。
- 走得到的 run 目錄裡多放的大檔、沒有 `run.yaml` 的 `runs/<x>/` → `unreferenced`；走法失敗的 run 目錄 → `record`。
- 合併與門檻：單位不被跨過（兩個 `raw/<name>` 是兩項）；一個 run 目錄裡混類別時拆開；門檻以下不列、但計入總量；排序。
- 重複檔：`--root` 裡一個 checkpoint 的複本 → `duplicate_of`；同大小不同內容 → 沒有；大小不符的檔不雜湊（把 `sha256_file` 換成記錄呼叫的假函式）。
- 不讀索引：刪掉或弄壞 `indexes/` 的檔，結果除了那些檔本身以外不變。
- WARN：run 卡壞掉 → `skipped=1`、它的 run 目錄是 `record`；`os.scandir` 丟 `PermissionError`（monkeypatch）→ `unreadable=1`、不 ABORT；最新 attempt 是 `running` → `running=1`。
- 符號連結不跟隨（沒有建立權限的 Windows 跳過）。
- 輸入：`--min-mib -1`、`nan` → `invalid:`；不存在的 `--root` → `not_found:`；不存在的 data root → 照常掃（只有 `run_command` 建的 `logs/`），OK。

### 10.4 稽核紀錄與不刪

- 產物：`artifacts/gc_scan/<id>/` 有 `manifest.json`、`scan.json`、`items.jsonl`，`vcp artifact verify` 通過；`items.jsonl` 等於 `--json` 的 `items`。
- 不刪不改：掃描前後 data root 與每個 `--root` 的檔案清單、大小、mtime 相同，只多了那個產物目錄與 `logs/` 的當日 log；`--no-record` 時只多 `logs/`、`scan=none`。
- Collector：backup 的既有測試全過；`backup manifest` 的位元組 golden（`all`、`run:`、`submission:` 各一）在重構前後相同；gc 模式下 `sha256_file` 從未被呼叫（除了 §5.5 的候選）。
- `checkpoint_dirs` 抽出之後，`train status` 的 `tmp=` 測試全過。

### 10.5 端到端與回歸門檻

- `tests/unit/test_e2e_*.py` 風格的 CLI 端到端：一個比賽從 stage、upload（假 CLI）、`final`、`select --record S1@<ref>` 到 `gc scan`，VERDICT 逐欄比對。
- 真資料的整合測試（`VCP_REALDATA_ROOT`）不跑 `gc scan`：它會寫產物，而 `raw/` 與真的 data root 不能多任何東西。
- `tests/unit/test_regression_gate.py` 加一列 `0.17.0`：`--none` 與 `unticked`、`@ref` 的驗證與 `report` 標記、`foreign:` 解析、失敗時的 `cli_warnings=`、gc 的分類、不刪、不讀索引。

### 10.6 建議的實作順序（給計畫）

1. `VcpError.cli_warnings`、`with_cli_warnings`、`sync` 與上傳前同步的失敗路徑（§4）。
2. `refs_of`、`owners`、條目解析（§3.1）。
3. `select --none` / `@ref`、狀態與行（§3.2–§3.4）。
4. `status`、`report`、`final`（§3.5）。
5. Collector 的記錄端與 `walk_all(split=)`，先鎖 backup 的 golden（§5.3）。
6. `checkpoint_dirs` 抽出（§5.2）。
7. gc 的引用收集、掃描、分類、合併、重複檔（§5.3–§5.5）。
8. `gc_scan` 產物與 CLI（§5.6–§5.8）。
9. 回歸門檻與文件（§10.5、§11）。

## 11. 文件

- `CLAUDE.md` 與 `AGENTS.md`（改一份、整份複製到另一份，`diff` 無輸出）：
  - 「提交治理」：`selected` 的 `chosen` 可以是空的（`select --none`，0.17.0 起，舊 vcp 讀到會 FAIL，第一次 `--none` 之前讀寫同一份台帳的都先升級）；條目 `<id>@<ref>`；`foreign:<ref>` 讀取時解析；失敗的 `sync` 與 `sync_failed:` 也帶 `cli_warnings=`；
  - 「路徑」：`artifacts/gc_scan/<id>/`（`scan.json` + `items.jsonl`）是 `vcp gc scan` 的稽核紀錄；gc 只列、不刪，不讀 provenance 索引；
  - 「常用命令」：加 `uv run vcp gc scan [--root DIR] [--min-mib M] [--no-record]`。
- `docs/reference/cli.md`：`submit select`（`--none`、`@ref`、檢查順序）、`status` / `report` / `final`（`unticked`、標籤比對）、`sync` / `upload`（FAIL 的 `cli_warnings=`）；新節「磁碟盤點 `vcp gc`」（命令列、掃描範圍、類別表、單位表、產物、限制），並在「一次提交」的範例加一行 `select --none` 的註解。
- skills（`.claude/skills/` 改完 `cp -r` 到 `.agents/skills/`；`docs/guides/AGENT_SKILLS.md` 的路由表跟著改）：
  - `vcp-train-submit-backup`：`SKILL.md` 的選取步驟（`@ref` 用在重傳過的 id、`--none`）、新段「清磁碟」：`gc scan` → 讀 `unreferenced` / `debris` / `duplicate` → 人自己刪（`record` 先問自己是否放棄那個 dataset 或 run）→ 掃描的 id 就是紀錄；`reference.md` 的命令表；
  - `vcp-running-contests`：`workflow.md` 第 10 步「決選」與收尾的清磁碟；`operator-guide.md` 對應段落；
  - `vcp-orientation`：`SKILL.md` 的命令表加 `gc scan`、路由（gc → `vcp-train-submit-backup`）；`map.md` 的版本沿革加 0.17.0。
- 設計 spec 修訂：提交治理 spec（`2026-09-05-vcp-submission-governance-design.md`）加第 34 條；backup 審計 spec（`2026-09-06-vcp-backup-audit-design.md`）的補充決定加一條 Collector 的記錄端；都指回本 spec。
- 後記：新增 `docs/superpowers/plans/2026-11-01-vcp-0170-selection-gaps-and-gc-followups.md`；在 0.16.0 後記 §2.2 第 6、7、12 條與 RSNA 收尾後記 §4 各補一條處置（§4 的處置寫明不用 provenance 圖的理由）。
- `docs/handover/HANDOVER.md` 與 `CODEX_PROMPT.md`：發版 commit 裡更新現況、待辦、升級順序。
- `CHANGELOG.md`：不在功能分支改；發版 commit 加 `## [0.17.0]`，寫 MINOR 的理由（§9）、Added / Changed / Fixed（§6 的 PATCH 項）、相容性與升級（§8）、已知限制（§12）。

## 12. 已知限制

- **選取：**
  - 平台的勾選仍讀不到；記的是人說的。
  - `--none` 時平台會計哪幾發，vcp 不知道；有 `final` 就 WARN `differs`，要消掉就勾 `final` 選的並記下來。
  - ref 含逗號的條目寫不出來（`--record` 以逗號分隔）。內建平台的 ref 不含逗號（Kaggle 是數字，退回的是 16 位十六進位）；只有 `record --platform-ref` 手打的可能。
  - 沒有 ref 的一發（手動平台沒給 `--platform-ref`、還沒 `sync` 的未確認上傳）只能用裸 id 記。
  - `slots` 以 submission 計：兩格勾同一個 id 的兩發，在只有一個 id 可選時是 `ok`，有更多 id 時是 `slots`。
- **CLI 警告：** FAIL 時只有個數，行要去 log 看；本機 `OSError` 的 ABORT 不帶。
- **gc：**
  - 只知道 vcp 自己的紀錄。`unreferenced` 不等於可以刪：筆記、notebook、別的工具、別的 configs root（共用 data root 的別的專案）可能需要它。別的 configs root 的 backup 清單與預登記不會讀到；它們的 run 目錄有走到。
  - 不知道平台上的副本（例如 Kaggle dataset）；backup 推過的副本不逐檔判斷，只有 `train upload` 驗過的 checkpoint 有 `copy`。
  - 不跟隨符號連結與 junction；硬連結會被算兩次；經由連結路徑寫下的引用可能對不到掃到的檔。
  - 重複檔只認位元組完全相同、而且被引用的那一份有記錄 sha 的；壓縮或重新打包過的認不出來。
  - 只以 sha 記的權重（`eval ingest --weights`、kernel 提交的 `WeightRef`）沒有路徑，不算引用、也不參與重複檔比對：不經 `vcp train run` 登記的權重會出現在 `unreferenced`。
  - `--no-record` 也會寫每日 log（所有命令共用的 `run_command`）；真正唯讀的 data root 上，log 寫不了就跟其他命令一樣。
  - 快照不上鎖：正在訓練時新寫的 checkpoint 還沒登記，會出現在 `unreferenced`（所以 `running > 0` 時 WARN）。
  - 很大的 `--root` 會掃很久；沒有進度輸出。
  - 每次掃描留一個小產物，它們本身會在之後的掃描裡算成 `record`。

## 13. 使用者可在發版前覆核的裁決

依重要性排列；每條是「裁決 — 另一個做法 — 那個做法的代價」。

1. **gc 不讀 provenance 索引，只讀正本**（偏離 RSNA 收尾後記 §4「引用關係可以從 provenance 圖取得」）。— 從索引取引用。— 索引是衍生的、可能過期或不存在，記的是 entity 不是檔案路徑；過期時錯的方向是把有用的檔說成沒被引用。代價是掃描要自己走一遍紀錄（不雜湊，秒級）。
2. **命令是 `vcp gc scan`，不是 `vcp gc --dry-run`**。— 照後記的名字，`--dry-run` 必填。— 必填的旗標暗示有不帶它的刪除模式；反目標說 vcp 不刪。代價是跟後記與賽後報告的寫法不同，文件要說明。
3. **每次掃描預設寫一個 `gc_scan` 產物，`--no-record` 才不寫**。— 只寫到 `--out`，或只印。— 沒有預設的稽核紀錄，人刪了之後沒有「當時沒有引用」的證據。代價是 data root 每次多一個小產物，也會進 provenance 索引。
4. **預設掃 data root 加檢查點目錄（不遞迴），其他用 `--root`**。— 也遞迴掃每個 run 的 `--cwd`。— `--cwd` 常是整個 repo 加 venv，慢而且大多是 `unreferenced` 雜訊。代價是 RSNA 那種放在專案目錄的上傳包，要人記得加 `--root` 才看得到。
5. **分類有 `record` 一類，vcp 自己的紀錄從不歸 `unreferenced`**。— 沒有引用就一律 `unreferenced`。— 別的 checkout 的 dataset 或共用 data root 的別的專案，整批會被說成垃圾。代價是被放棄的 run 要人自己判斷。
6. **`--none` 寫 `chosen: []`，不另開事件**。— 新事件 `unselected`。— 舊 vcp 一樣 FAIL，多一個事件只多一個字彙。代價是 0.16.0 讀者遇到這一列就 FAIL，升級順序同 0.16.0。
7. **`unticked` 單獨不 WARN，有 `final` 時以 `differs` WARN**。— 一律 WARN（當成 `slots`），或一律不 WARN。— 一律 WARN 讓刻意的決定到截止都在 WARN；一律不 WARN 會漏掉「vcp 選了、平台卻自己挑」的情況。代價是沒有 `final` 的比賽記了 `--none` 就不再提醒。
8. **`@ref` 條目讓 0.16.0 照常讀（只會多報）**。— 讓舊版讀到就 FAIL（例如另開欄位）。— 強制升級；多報的 WARN 對舊版讀者是安全方向。代價是混用版本時 `status` 可能誤報 `differs`。
9. **比對單位仍是 submission 標籤，`slots` 數不同標籤數**。— 以平台的一發為單位（`expected` 改數 arrivals，`slots` 數條目）。— 那會改 0.16.0 的 C4，只上傳過一個 id 兩次的人會多一個 WARN。代價是兩格勾同一個 id 只算一格。
10. **`foreign:<ref>` 在讀取時解析，寫入時照字面**。— 寫入時改寫成 `<id>@<ref>`。— 歸屬會在之後的 `sync` 才確定，寫入時改不完；台帳要記人說的話。代價是每個讀者都要做解析。
11. **gc 的門檻預設 100 MiB，類別總量不受門檻影響**。— 10 MiB 或 1 GiB。— 太低時輸出被小檔淹沒，太高時漏掉中型的 checkpoint 與 266 MB 那種 `.tmp`。代價是要看小東西得 `--min-mib 0`。
12. **FAIL 時只帶 `cli_warnings=<n>`，不印行、不印升級提示**。— 讓 `run_command` 在失敗時也印人看的行。— 改所有命令共用的失敗路徑，超出這一版。代價是要去 log 找那一行。
13. **重複檔只在大小相符時才雜湊，只比有記錄 sha 的被引用檔**。— 雜湊每個大檔、或完全不比。— 雜湊全部在大的 `--root` 上可能幾十分鐘；完全不比就認不出逐位元相同的權重複本。代價是改過格式的複本認不出來。
14. **引用來源包含 backup 清單、本機 backup 目的地與產物的 input**。— 只看 run 與台帳。— 掃到備份目的地時會把整份備份說成沒被引用。代價是 backup 清單裡的舊版本檔也算被引用，人得看引用者才知道只剩 backup 在記它。
15. **gc 沒有 `--dataset`，永遠讀全部引用**。— 允許只看一個 dataset。— 只讀一部分引用會把別的 dataset 用的檔說成沒被引用。代價是大 data root 每次都走全部紀錄。

### 13.1 builder check 之後的裁決（2026-11-01，controller）

審查者讀完 spec、對過程式碼後，有幾處 spec 的說法與現有程式不符，或留下要猜的地方；處置如下，正文已照改。

16. **每日 log 照寫**（§5.1）：`run_command` 在命令本體之前就建 `logs/`、寫 VERDICT，`--no-record` 也一樣；data root 不另外判斷「不存在」。— 讓 gc 不寫 log。— 要動所有命令共用的失敗與 log 路徑，超出這一版。
17. **快取的實際版面是 `cache/materialize/<mode>[-r<長邊>]/`**（§5.4、§5.5）：每個模式目錄一項；`cache/audit/`、`dhash.jsonl` 由 `vcp data audit` 重建，歸 `rebuildable`；`import_skipped.jsonl` 只有重新匯入才重寫，歸 `record`。
18. **`runs/<id>/inputs/` 歸 `record`**（§5.4）：`eval ingest --keep-input` 的來源副本是 vcp 寫的，但 run 卡沒有欄位記它；只在 gc 這邊認，backup 清單不變。— 加進 backup 的走法。— 會改 backup 清單的位元組，是另一件事。
19. **規則跟著路徑走**（§5.2）：被 `--root` 包住的 data root 仍套 data root 的規則。— 依根套規則。— `--root ~` 會讓 `raw/` 失去單位與保護的語意。
20. **gc 的走法容忍較寬、以單位為交易**（§5.3）：接 `VcpError`、`OSError`、`ValueError`，其他例外 ABORT；backup 不變。
21. **只記 sha 的權重不算引用**（§5.3、§12）。— 對每個大檔算 sha 去比。— 大的 `--root` 上會花幾十分鐘。
22. **`roots=` 是實際掃的根數，`--root` 的個數另叫 `root_options=`**（§7）。
