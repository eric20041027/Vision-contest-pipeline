# vcp 0.16.0 後記：最終選取的記錄與核對、平台 CLI 警告、訓練的磁碟空間（待 0.16.0）

- spec：`../specs/2026-10-30-vcp-0160-selection-and-disk-design.md`（3253192）。計畫：`2026-10-30-vcp-0160-selection-and-disk.md`（545e50d），九個任務，一個任務一個 commit（Task 7 多一個修正 commit，Task 9 兩個）。
- 分支 `feat/v0160-selection-disk`，base `main` 4a7af73。
- commit：
  - `a321793` fix(core): rewrite yaml files through an atomic replace
  - `bbad622` feat(submit): selected ledger event and the shared selection state
  - `9201bf7` feat(submit): submit select records the platform's final selection
  - `bdc6c2c` feat(submit): status, report and final read the recorded selection
  - `6ec025c` feat(submit): surface the platform CLI's warnings in sync and upload
  - `a2ce47d` feat(train): check free disk space before the first write
  - `88bf5b9` feat(train): name disk_full and low_disk when an attempt fails
  - `470c2ce` fix(train): treat upload bookkeeping writes as vcp's own（Task 7 審查的修正）
  - `810e027` feat(train): train status lists leftover tmp files
  - `5e32222` test(gate): guard the 0.16.0 selection, CLI-warning and disk fixes
  - 文件一個 commit（本檔、`cli.md`、CLAUDE.md / AGENTS.md、三個 skill 與鏡射、治理 spec 第 33 條、訓練層 spec 第 20 條、RSNA 收尾後記的處置）。
- 每個任務各自審查（審查檔不進 repo）：沒有 Critical；只有一個 Important（Task 7 的 `_upload_all`，見 §2.1 第 3 條），修正在 `470c2ce`。其餘 Minor 的處置見 §1、§2.2。
- 每個行為變更都先寫測試、看它失敗再實作。例外是只鎖現狀的測試：yaml 位元組的 golden（Task 1 的 `TrainRecord`、`RunCard`、`PlatformProfile`）與 `ledger adopt` 合併 `selected` 列的測試（原本就合併，先就是綠的）。
- 版本、`CHANGELOG.md`、`.claude/.claude-plugin/plugin.json` 都沒動，發版時再做（§2.2 第 1 條）。

## 1. 已知限制

完整的列表見 spec §5.4 與 §12；這裡只記使用時會踩到的，與實作中發現的。

- **讀不到平台的勾選。** kagglesdk 0.1.38 有 `SUBMISSION_GROUP_SELECTED`，kaggle CLI 2.2.5 沒有對應旗標，vcp 只呼叫 CLI，所以選取只能手動記（`select --record`），記的是人說的，vcp 無從核對平台。`status` 的 `selection=` 比的是「你記的」對「`final` 選的」與名額。
- **選取只核對 `board_rule: best`。** `last` 的平台自己算最後一發：`selection=none`、`status` 不因選取 WARN，`select --record` 照記、`report` 照標。平台其實有勾選、profile 卻寫 `last` 的比賽完全不被核對，`board_rule` 要照平台設定。
- **選取以 id 為單位。** 同一個 id 傳過兩次時，vcp 不知道勾的是哪一發；不能記「什麼都沒勾」（空的被拒）。
- **`foreign:<ref>` 之後被台帳認成自己的上傳時，選取裡的標籤不會自動換成 id**，`differs` 可能誤報（spec §12）：這種上傳請改用 id 記。
- **`select --message` 只在寫列時用。** 再記同一個集合什麼都不寫，舊列的 `message` 不變。
- **`status` 看不到 CLI 過舊**（裁決 R1）：要等下一次 `sync` 或 `upload` 才 WARN。`upload --json` 只有 VERDICT 的 `cli_warnings=<n>` 與 WARN，行本身在人看的輸出與 log（`sync --json` 的 `cli_warnings` 才有那些行）。
- **磁碟的偵測有邊界**（spec §5.4）：
  - exit code 不帶原因；C++ 的 `ios_base::badbit`、PyTorch 的 `file write failed` 不含磁碟字樣，只能落到 `low_disk:`，而且空間若已回到門檻以上就什麼都不報。
  - 不比對在地化訊息；console 只讀最後 64 KiB；量測範圍以外的磁碟（`%TEMP%`、模型快取）看不到。
  - `free` 是 `shutil.disk_usage().free`：Windows 的使用者配額不反映（C5）。
  - 失敗後的空間比啟動門檻，訓練本身吃掉大量空間而因別的原因失敗時也會報 `low_disk:`，所以措辭只說 may have filled；`train.log.jsonl` 的 `note` 記了當時的可用空間。
- **data root 所在的磁碟滿了會停掉訓練**（C3），即使 checkpoint 寫在別顆磁碟：紀錄的完整優先於訓練的延續，清出空間後 `--resume`。`--upload` 的步驟中途 `disk_full:` 時 attempt 已經結束，用冪等的 `vcp train upload --run R --dest DEST` 補，不是 `--resume`。
- **`train.log.jsonl` 的 append 不是原子的，`read_events` 逐行解析。** 一次 append 寫到一半遇到 ENOSPC，可能留下半列，之後讀事件（含 `--resume`）會壞。C3 的「清出空間後 `--resume`」仰賴這件事不發生；0.16.0 沒有改它（既有行為，見 §2.2 第 5 項）。
- **`.tmp` 的掃描範圍**：從沒登記過 checkpoint 的目錄看不到。
- **`selection_warn_hours` 大得離譜**（約 1.75e7 小時以上）通過載入驗證（有限、`>= 0`），但算 `due_at` 時日期溢位，唯讀的 `status` 會 ABORT `OverflowError: date value out of range`（§2.2 第 6 項）。
- **`assert_room` 的訊息只印一位小數，比較用精確的位元組**：9.96 GiB 對門檻 10 會印成「10.0 GiB free … below --min-free-gib 10」（§2.2 第 7 項）。
- **Windows 的替換重試只等約 1.55 秒**：更久的佔用（例如防毒掃描大檔）仍會讓那次寫入原樣丟出，舊檔完整。
- **舊 vcp 讀不動新台帳。** 0.15.1 以前的 vcp 讀到 `selected` 列會 FAIL `bad ledger row`。`selected` 只由人下的命令寫，所以在第一次 `select --record` 之前 0.15 與 0.16 可以共用台帳；誰先記，誰負責先讓每個讀寫那份台帳的 checkout 升級（`configs` 台帳是所有 checkout，`shared` 正本是同一個 data root 的所有 worktree）。`submit.yaml` 寫了非預設的 `selection_warn_hours`，舊 vcp 載入時 FAIL `extra_forbidden`。
- **平台協定改了回傳型別。** 外部登記、回傳 list 的平台由包裝相容；直接呼叫 `KagglePlatform.list_submissions` 或 `parse_submissions` 的程式要改。

## 2. 執行期裁決與開放待辦

### 2.1 執行期裁決

1. **本檔的結構。** 同 VCP-048 後記：已知限制放前面，裁決與開放待辦合在最後一節。
2. **`PlatformError(cli_warnings=)`。** 上傳之後的回讀看到警告、而上傳又以平台 FAIL 收場（`upload_failed:`、`upload_unconfirmed:`）時，警告要帶回 VERDICT。`PlatformError` 多一個關鍵字參數 `cli_warnings`（預設空），`upload` 的交易把它跟上傳前同步的警告合併去重，把個數放進失敗 VERDICT 的 `cli_warnings=`（`setdefault`，不蓋過失敗本身的欄位）。
3. **`_upload_all` 只有 `upload(...)` 呼叫在 `own_writes` 之外**（取代計畫的「`_upload_all` 不在 C3 轉換範圍」，Task 7 審查的 Important）。上傳目的地寫滿不是 vcp 的紀錄，仍是原本的 `OSError`；但 `_upload_all` 自己的簿記寫入——`save_record` 與 `uploaded` 事件——是 vcp 的紀錄，在 `own_writes` 裡，所以這時的 ENOSPC 跟 `train run` 的其他紀錄寫入一樣是 ABORT `disk_full:`、`disk_evidence=vcp`。只有 `train_run` 呼叫 `_upload_all`，沒有別的命令受影響。
4. **`own_writes` 也轉換「把 child 的輸出回印到 stdout」失敗。** `on_line` 回印時 stdout 被導向的檔滿了，一樣是 ABORT `disk_full:`、`disk_evidence=vcp`；人看的訊息仍說 vcp 寫不下自己的紀錄。
5. **測試 patch `atomic.IS_WINDOWS`，不 patch `os.name`。** 翻 `os.name` 會讓 `Path()` 建出錯的類別。`replace_text` 的 Windows 重試判斷用模組屬性 `IS_WINDOWS`、延遲用 `REPLACE_RETRY_DELAYS`，兩者都可以被測試換掉；`time.sleep` 換成記錄呼叫的假函式。
6. **`final` 列的 golden 是「有沒有選取，除了 `ts` 以外都相同」。** `final` 對已記的選取只說、不改：同一份台帳在有 / 沒有 `selected` 列時，`final` 列的 JSON 排除 `ts` 之後逐位元組相同。
7. **`select` 的 `invalid:` 也在交易內檢查。** 空的、重複的項目跟 `not_staged:` 等一樣，是拿到台帳鎖、讀完台帳之後才檢查（先 `invalid:`，再依輸入順序找第一個過不了的項目），所以另一個程序持有鎖時，連壞輸入也是 ABORT `locked:`。
8. **`select --json` 的 result 是最新那一列 `selected` 加一個 `selection` 物件**（`{chosen, recorded_at, words, expected, final, due_at}`）。沒寫（`written=false`）時，那一列是已經在台帳裡的最新一列。
9. **沒有截止時的 `missing` 行。** spec §3.5 只給有截止的文字。沒有截止時寫成 `selection: missing (no deadline, warning since <final 列的時戳>)`（Task 2）。
10. **`selected` 放在 `Event` literal 的 `final` 之後**（spec 沒給順序）。
11. **計畫的一個測試草稿是錯的。** 「`select S1,S2`、`final_row(['S1'], 1)` → words `slots`」：個數與集合都不同，依 spec §3.4 是 `slots,differs`。測試斷言 `slots,differs`，另有一個只差個數的 `slots` 案例（Task 3）。
12. **`free_gib=` 在每個由 `RunResult` 建出來的 `train run` VERDICT 上。** 量測之後才以例外 FAIL 的（例如 `--final` 對到多個檔）沒有（Task 6）。啟動前 ABORT 的 `low_disk:` 自己帶 `free_gib=` `min_free_gib=` `disk_path=`。
13. **`unavailable(check)`：任何一個磁碟區量不到就成立**（→ WARN `disk_check=unavailable`）；`free_gib=unknown` 只在全部量不到時。
14. **`disk_path` 是 `store_path` 的形式**（data root 底下的是相對路徑，例如 `runs` 或 `.`，其他是絕對路徑），跟 `env.<n>.json` 的 `volumes[].path` 一樣。
15. **文件。** `submit init` 沒有 `selection_warn_hours` 的選項（spec §2 不做），`cli.md` 把這個鍵寫在 `init` 那一列；CLAUDE.md / AGENTS.md 另加半句 `sync` / `upload` 的 `cli_warnings=`（spec §11 只列選取與磁碟）。`vcp-orientation/map.md` 的版本沿革補上 0.15.0、0.15.1，讓 0.16.0 接得上。

### 2.2 開放待辦（依優先順序）

1. **發版 0.16.0**（使用者同意之後；發版 commit 不在功能分支）。`CHANGELOG.md` 要寫：
   - MINOR 的理由（spec §9）：台帳新事件 `selected`、`env.<n>.json` 的 `disk`、`train.log.jsonl` 的 `disk_full` / `low_disk` note；新命令 `submit select`、新選項 `--min-free-gib`、新 VERDICT 欄位、新 `reason=` 字彙 `low_disk:` `disk_full:`；`submit.yaml` 新鍵 `selection_warn_hours`；`Platform.list_submissions` 的回傳型別；
   - Added / Changed / Fixed（`dump_yaml_model` 的原子替換，位元組不變）；
   - 相容性與升級（spec §8）、已知限制（spec §5.4、§12 與本檔 §1）；
   - `src/vcp/__init__.py` 的 `__version__` 與 `.claude/.claude-plugin/plugin.json` 同步改成 0.16.0。
2. **比賽工作區升級。** 讀寫同一份台帳的每個 checkout 一起升到 0.16.0，再讓第一個人 `select --record`；共用同一個 `submit.yaml` 的 vcp 都要先升級才能寫非預設的 `selection_warn_hours`。
3. **交接文件。** `HANDOVER.md` 與 `CODEX_PROMPT.md` 在發版時提到新事件、升級順序與磁碟檢查。
4. **把 glob 寫進 `started` 事件**（spec §12，待使用者決定）：`--checkpoints` / `--final` 的 glob 寫進 `train.log.jsonl` 的 `started`（不驗欄位，不破舊版），`train status` 再掃它們的前綴，從沒登記過 checkpoint 的目錄的 `.tmp` 才看得到。
5. **`train.log.jsonl` 的 append 與讀取。** `append_event` 改成單次寫入並在失敗時回復，或 `read_events` 容忍最後一列半列（只略過最後一列、不吞中間的）；之後 C3 的 `--resume` 才有把握。
6. **`selection_warn_hours` 的上限。** 驗證加一個上限，或 `select._due` 接住 `OverflowError`、當成視窗已經打開；現在是唯讀 `status` 的 ABORT。
7. **`assert_room` 的訊息。** ABORT 路徑改成無條件捨去（或多印一位），不要把低於門檻的值印成等於門檻。
8. **`select --none`**（spec §12）：要支援「什麼都沒勾」（Kaggle 這時自動用 public 最好的兩發）就另加選項，現在空的選取被拒。
9. **`--record <id>@<ref>`**（spec §12）：要分到同一個 id 的每一發時，讓 `--record` 收 `<id>@<ref>`；目前沒有需要。
10. **`vcp gc --dry-run`**（RSNA 收尾後記 §4，不在本版）。
11. **自動讀回平台的勾選。** 日後 kaggle CLI 開出「只列已勾選」的旗標，`sync` 可以寫 `source=platform` 的 `selected` 列（schema 已留位置），`differs` 對 `foreign:<ref>` 的誤報也一併處理。
12. **審查留下的小項**（沒有急迫性）：
    - `train status` 掃到讀不了的目錄項目時會 ABORT（`PermissionError`），符號連結的 `.tmp` 以目標路徑列出；
    - `console_marker` 讀不到 console 時靜默回傳 `None`；
    - Kaggle 多頁列表的後一頁丟例外時，前一頁的警告行跟著丟掉；
    - `_fsync_dir` 判斷 `os.name`、`replace_text` 判斷 `IS_WINDOWS`，兩處的 Windows 判斷可以收成一個；
    - `final` 的 `FinalResult` 用十個位置參數建構；`status_cmd`、`_final_locked`、`train_run` 都超過 50 行，可以抽函式。
