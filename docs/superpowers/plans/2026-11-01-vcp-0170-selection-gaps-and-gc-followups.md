# vcp 0.17.0 後記：選取的兩個缺口、失敗時的 CLI 警告、磁碟盤點 `vcp gc scan`（待 0.17.0）

- spec：`../specs/2026-11-01-vcp-0170-selection-gaps-and-gc-design.md`（f1e7c45，builder check 之後 c609e28）。計畫：`2026-11-01-vcp-0170-selection-gaps-and-gc.md`（6f57812），十一個任務，大致一個任務一個 commit（Task 6 先一個鎖 golden 的 commit，Task 7 多一個 `checkpoint_dirs` 的重構 commit，Task 7 與 Task 9 各多一個審查修正，Task 11 的測試與文件分兩個）。
- 分支 `feat/v0170-selection-gc`，base `main` 7e43fe1：兩個 PATCH 先合併（#63 `fix/append-whole-line`，#64 `refactor/v0160-review-leftovers`），本分支再把 `main` 合進來。
- commit：
  - `0852932` feat(submit): failed lists and syncs still count the platform CLI's warnings
  - `0dcb988` feat(submit): ledger refs_of and owners, and selection entries resolved to labels
  - `7b5da30` feat(submit): an empty selection reads as unticked and labels decide slots and differs
  - `ef4a7cc` feat(submit): select --none and --record <id>@<ref>
  - `dfeaea0` feat(submit): status, report and final read selection entries by label and shot
  - `e562ef5` test(backup): lock the manifest files of all, run, and submission before the walker is refactored
  - `0e1ecf3` refactor(backup): a replaceable recording end for the evidence walk and split conclusions
  - `2e42221` refactor(train): checkpoint_dirs out of tmp_files
  - `1833a95` feat(gc): collect every reference to a path from the canonical records
  - `c9b00aa` fix(gc): a broken dataset card no longer drops the references its runs, preregs and submissions made（Task 7 審查的修正）
  - `e1f7245` feat(gc): classes and units of the disk inventory
  - `0eacd33` feat(gc): scan the data root, checkpoint directories and --root
  - `5c64465` fix(gc): an unreadable or skipped child keeps its parent from becoming one item, and duplicates are verified against the original（Task 9 審查的修正）
  - `a810627` feat(gc): vcp gc scan writes a gc_scan artifact and never deletes
  - `c2d754d` test(gate): guard the 0.17.0 selection, CLI-warning and gc scan behaviour
  - 文件一個 commit（本檔、`cli.md`、CLAUDE.md / AGENTS.md、三個 skill 與鏡射、`AGENT_SKILLS.md`、治理 spec 第 34 條、備份審計 spec 的補充決定、0.16.0 後記與 RSNA 收尾後記的處置）。
- 每個行為變更都先寫測試、看它失敗再實作。例外是只鎖現狀的測試：`backup manifest` 的位元組 golden（`e562ef5`，在 Collector 重構之前就是綠的，重構後仍綠）；0.16.0 留下的 `final` 列 golden 也照舊綠（有沒有 `selected` 列，`final` 列除 `ts` 外逐位元組相同）。
- 同版的兩項 PATCH（#63、#64）當時為了避開衝突沒有帶文件，它們的處置寫在 `2026-10-30-vcp-0160-selection-and-disk-followups.md` §2.2 的第 10、11 條。
- 版本、`CHANGELOG.md`、`.claude/.claude-plugin/plugin.json`、`docs/handover/` 在發版 commit 改（§2.2 第 1 條）。

## 1. 已知限制

完整的列表見 spec §12；這裡只記使用時會踩到的，與實作中發現的。

- **讀不到平台的勾選**（沒變）：記的是人說的；`--none` 時平台會計哪幾發 vcp 不知道，有 `final` 就 WARN `unticked,differs`，要消掉就勾 `final` 選的並記下來。
- **`<id>@<ref>` 的邊界**：沒有 ref 的一發（手動平台沒給 `--platform-ref`、還沒 `sync` 的未確認上傳）只能用裸 id 記；ref 含逗號的條目寫不出來（`--record` 以逗號分隔；內建平台的 ref 不含逗號）；`slots` 以 submission 計，兩格勾同一個 id 的兩發在只有一個 id 可選時是 `ok`。
- **舊 vcp 讀不動空的選取。** 0.16.0 讀到 `chosen: []` 會 FAIL `bad ledger row`（範圍同 `selected`：每個 submit 寫入命令、`status`、`report`、`backup manifest`、`backup verify`），所以第一次 `select --none` 之前，讀寫那份台帳的每個 checkout 都先升到 0.17.0。`<id>@<ref>` 條目 0.16.0 照常載入，只是讀錯：可能多報 `differs` / `slots`，不會少報、不會 FAIL。
- **失敗的 `sync` 只帶 `cli_warnings=<n>` 的個數。** 行在 log（每行一筆 WARNING `kaggle CLI: …`），VERDICT 不印行也不印升級提示。本機 `OSError` 的 ABORT 不帶。
- **gc 只知道 vcp 自己的紀錄。** `unreferenced` 不等於可以刪：筆記、notebook、別的工具、共用 data root 的別的專案可能需要它。別的 configs root 的 backup 清單與預登記讀不到（它們的 run 目錄有走到）。
- **gc 看不到只以 sha 記的權重。** `eval ingest --weights` 的 `weights_hash`、kernel 提交的 `WeightRef` 沒有路徑，不算引用也不參與重複檔比對：不經 `vcp train run` 登記的權重會出現在 `unreferenced`。
- **重複檔只認位元組完全相同**、而且被引用的那一份有記錄 sha 的；壓縮或重新打包過的認不出來。硬連結會被算兩次；不跟隨符號連結與 junction。
- **gc 的快照不上鎖。** 正在訓練時新寫的 checkpoint 還沒登記，會出現在 `unreferenced`（所以 `running > 0` 時 WARN）。很大的 `--root` 會掃很久，沒有進度輸出。每次掃描留一個小產物，它們自己在之後的掃描裡算 `record`。
- **`--no-record` 也會寫每日 log**（所有命令共用的 `run_command`）；真正唯讀的 data root 上，log 寫不了就跟其他命令一樣。

## 2. 執行期裁決與開放待辦

### 2.1 執行期裁決

1. **本檔的結構。** 同 0.16.0 後記：已知限制放前面，裁決與開放待辦合在最後一節。
2. **kaggle 只合併 `cli_warnings`，個數只由 `sync` / `actions` 寫。** `list_submissions` 與 `parse_submissions` 只把已讀頁面的警告行併進例外的 `cli_warnings`（`merge_warnings` 去重），不碰 `fields`；`fields["cli_warnings"]` 的個數由 `sync` 與 `upload` 的上傳前同步在知道所有頁面的行之後，用 `with_cli_warnings(e, *groups)` 以 `setdefault` 補上。平台若在讀第一頁時就設個數，後面頁的行進來之後個數會停在第一頁的。
3. **`_pre_sync` 把 `OSError` 也轉成 `sync_failed:` 並帶個數。** 0.16.0 的上傳前同步本來就把 `VcpError` 與 `OSError` 都轉成 `sync_failed:`；spec §4.2 說「`OSError` 不帶」指的是 `sync` 命令沒接住、直接成為 ABORT 的那種。兩者不矛盾：只多了上傳前同步的 FAIL 上的一個欄位。
4. **`Collector._try` 改名為公開的 `attempt`；沒有記錄端時行為逐位元不變。** `backup manifest` 的 golden（`all`、`run:`、`submission:` 與 test dataset 各一）先在舊程式碼上鎖住（`e562ef5`），重構（`0e1ecf3`）後仍綠。`Recorder` 的 `tolerates` 決定一個走法單位失敗時接哪些例外：backup 沒有記錄端，只接 `ValidationFailed`；gc 接 `VcpError`、`OSError`、`ValueError`。
5. **gc 的走法單位互相獨立，`skipped` 依 label 去重。** dataset 自有的檔（`dataset:<name>`）、`image_root:<name>`、`backup:<name>`（push 列）各是自己的單位。最初把整個 `walk_all(split=True)` 包成一個單位，`dataset.yaml` 壞掉時會把已走完的 run、預登記、submission 引用一起收回，錯的方向是把有用的檔說成沒被引用；現在壞掉的 card 只丟它自己的檔與 `image_root`（`c9b00aa`）。`skipped` 同一個 label 只留第一個原因。
6. **「有記錄 sha 的被引用檔」也包含 backup 清單與產物 input。** 重複檔比對的原件除了 checkpoint、產物檔、stage 的上傳檔與預測檔，還有 backup 清單的條目與產物 `spec.inputs`：它們的 sha 本來就在記錄裡。
7. **規則跟著路徑走的幾個推論。** 檢查點目錄落在 data root 底下時，套 data root 的分類與單位（不是逐檔為單位）；被別的根包住的 `--root` 仍列在 `scan.json` 的 `roots`，但由外層走，每個檔只算一次；已不存在的檢查點目錄與磁碟根（`C:\`、`/`；絕對的 glob 往上找最近既存目錄時可能走到那裡）不掃，讀不了的才記進 `unreadable`。
8. **含有被略過部分的目錄不合併成項目**（`5c64465`）。符號連結、junction、讀不了或 stat 失敗的子項，會讓它所在的目錄不往上合併：否則一個項目會蓋住沒看到的檔，讓 `referenced` 的檔被併進 `unreferenced` 的項目。任何 `OSError` 都算（包含 `FileNotFoundError`）；記進 `unreadable` 的是非 `FileNotFoundError`，與 Windows 上路徑長度 `>= 260` 的 `FileNotFoundError`。
9. **重複檔先確認原件。** `duplicate_of` 之前，原件也雜湊一次（快取），且要是不同的檔（`samestat`）才成立：不邀人刪掉最後一份。只雜湊大小相符的候選，量以候選為界。
10. **`--json` 的 `items` 是清單，`scan.json` 的 `items` 是個數**；兩者其餘的鍵逐字相同，`items.jsonl` 等於 `--json` 的 `items`。成功的 VERDICT 以自己的順序列欄位（`scan=` 開頭）：`fn` 在成功返回之前清掉失敗用的 `context`（`min_mib=`、`root_options=`），因為 `run_command` 把 context 排在最前面。失敗的 VERDICT 仍帶已知的 `min_mib=` 與 `root_options=`，不帶 `roots=`。
11. **`FinalResult` 多 `resolved`、`recorded_at`**（有預設值，既有呼叫不受影響）：`final` 在印 `differs` / `unticked` 行時需要已記條目的標籤與記下的時間。
12. **`checkpoint_dirs(data_root, record, on_error=None)`**：從 `train.status.tmp_files` 抽出的公開函式，`train status` 與 gc 共用；#64 在計畫寫完之後給 `_glob_dir` / `_glob_dirs` 加了 `on_error`，所以 `checkpoint_dirs` 也收它。`train status` 傳它的略過記錄器，gc 傳記進 `unreadable` 的函式。
13. **`note:` 只在人看的行。** `select` 對已被綁成自己上傳的 `foreign:<ref>` 印 `note:`，`--json` 沒有對應欄位。
14. **測試目錄不叫 `gc`。** 測試放在 `tests/unit/test_gc_*.py`、`tests/unit/test_cli_gc.py`：`tests/unit/gc/` 會遮住標準函式庫的 `gc` 模組。模組內的 `os.scandir` 包成 `scan._scandir`，測試 patch 它，不 patch 全域的。
15. **文件。** `docs/reference/cli.md` 新節「磁碟盤點命令 `vcp gc`」（命令列、掃描範圍、引用來源、類別、單位、輸出、產物、限制，並說明為什麼是 `gc scan`）；CLAUDE.md / AGENTS.md 另加半句 `sync` 失敗路徑的 `cli_warnings=`；`vcp-orientation/map.md` 的版本沿革補上 0.17.0。

### 2.2 開放待辦（依優先順序）

1. **發版 0.17.0**（使用者同意之後；發版 commit 不在功能分支）。`CHANGELOG.md` 加 `## [0.17.0]`：MINOR 的理由（spec §9）；Added / Changed / Fixed（#63、#64 兩項 PATCH：整列 append 與每日 log 在 Windows 改成 LF、審查留下的小項）；相容性與升級（spec §8；空的 `chosen` 的升級順序）；已知限制（spec §12 與本檔 §1）。`src/vcp/__init__.py` 的 `__version__` 與 `.claude/.claude-plugin/plugin.json` 同步改成 0.17.0；`docs/handover/HANDOVER.md` 與 `CODEX_PROMPT.md` 更新現況、待辦與升級順序。
2. **使用者覆核 spec §13 與 §13.1 的裁決**（二十二條，集中在發版前一次看）。其中兩條偏離既有文字：gc 不讀 provenance 索引（RSNA 收尾後記 §4 原本寫從圖取得），命令叫 `gc scan` 不叫 `gc --dry-run`。
3. **自動讀回平台的勾選**（0.16.0 後記第 9 條照舊）：kaggle CLI 目前最新是 2.2.5（2026-11-01 查），沒有「只列已勾選」的旗標。有了之後 `sync` 可以寫 `source=platform` 的 `selected` 列（schema 已留位置）。
4. **`gc` 的進度輸出**：很大的 `--root` 沒有進度，要等到掃完。
5. **硬連結重複計算**：同一個檔案的兩個硬連結會被算兩次，總量偏大。要修就用檔案身分（`st_dev` + `st_ino`）去重。
6. **拆 `train/run.py`**：現在 923 行，超過 800 行的上限（#64 抽了函式，檔案還是長）。把 `train_run` 的流程搬到 `vcp/train/attempt.py` 之類，測試裡 monkeypatch 的目標也要跟著搬，所以沒在 #64 做。
7. **其他整份重寫不是原子的**：`import_skipped.jsonl`、materialize 的 `failed.jsonl`、audit 輸出用 `"w"` 開檔重寫，被殺時會留下半份。跟 append 的半列是不同的問題（要用「暫存檔 → fsync → replace」），可能是未來一項。
8. **整列 append 的已知限制**（#63，`vcp.core.atomic.append_whole_line`）：不加鎖，檢查與截斷之間有一個短窗口；硬殺（`kill -9`、斷電）不涵蓋；別人的列接在我們寫了一半的位元組之後時，檔案刻意留著壞掉（大聲、位元組保留）。
9. **審查留下的小項**（沒有急迫性）：各任務審查時記下、判斷不阻擋合併的 Minor（例如 `Path.resolve()` 在符號連結迴圈時丟 `RuntimeError`，目前不被 gc 的走法容忍；相對的上傳目的地以 gc 的 cwd 解析；保護的名稱 `raw` / `locks` / `.git` 在大小寫不分的 Windows 上以大小寫相符比對）。發版前的最終審查可以一併處理。
