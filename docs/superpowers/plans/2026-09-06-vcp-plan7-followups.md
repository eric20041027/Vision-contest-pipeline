# Plan 7 後記（子專案 6 備份審計）

- 日期：2026-09-06
- 計畫：`2026-09-06-vcp-plan7-backup-audit.md`（8 個任務）；spec：`../specs/2026-09-06-vcp-backup-audit-design.md`（§14 補充決定同日寫入）
- 執行：Subagent-Driven Development，worktree `plan7-backup`；實作者 haiku（T1、T2）/ sonnet（T3–T8）、任務審查 sonnet、最終審查與修正波 opus
- 結果：8 個任務全數通過任務審查；最終審查 14 項 → 一次修正波（3 個 commit）→ 再審 8 項殘餘 → 1 項由控制者微修；全套 829 passed / 4 skipped（既有的真資料 skip），覆蓋率 96.3%，ruff 乾淨

## 1. 執行期裁決 R0–R7

| 裁決 | 內容 | 依據 / 代價 |
|---|---|---|
| R0（預檢） | Task 7 的 pull 測試第三例用空 store 的假 remote 期待 `mismatch`，實際是 `missing`；改為「還原 valB → push 到 hashsum 誠實、copyto 給錯位元組的假 remote → 刪 valB → pull」 | pull 語意：目的地 sha 先比對，拉回不符才是 mismatch |
| R1 | `Collector.add` 把缺 sha 的檔丟進 `unlisted` 後沒記住 key，第二次走到會重複計數 → Task 4 順手去重 | spec §6 `unlisted=` 是個數 |
| R2 | R1 沒有測試 → Task 8 補「刪 plan 檔後走兩個 run，`unlisted` 只出現一次」 | 審查旨在有人驗過 |
| R3 | Task 6 測試用 `write_text` 重寫 `readings.jsonl` 沒給 `newline="\n"`，Windows 寫成 CRLF → 前綴 sha 自然不同；`verify.py` 判 drift 是對的。計畫全部改台帳 / 卡的 `write_text` 加 `newline="\n"` | 既有量測層測試改台帳檔都帶 `newline="\n"` |
| R4 | `push --tier 2` 後 `verify --dest` 同一目的地被 tier 3 的 `last.pt` 打成 `missing=1`：程式對、計畫測試錯，而且 spec §11 的端到端「push --tier 2 → verify OK」與 §6.2 自相矛盾。verify 加 `--tier N`（只限副本層，預設 3 = 全查），verify 列記 `tier` | spec §2「先小後大」——只推前兩層不該因權重層 FAIL；預設值保住 §6.2 |
| R5 | CLI pull 測試的 `pull --tier 3` 期待 `missing`，但 `last.pt` 本機還在且 sha 相同 → `skipped`。先刪本機 `last.pt` 再 pull | pull 冪等是 spec §6 明寫 |
| R6 | `FileEntry` external 的 `source` 用 `Path(...).is_absolute()` 在 Linux 會拒絕 `D:/weights/best.pt`。改成 Windows / posix 兩種絕對路徑都認 | 清單跨機器旅行 |
| R7（再審後微修） | push 在複製或讀回時遇到 `PlatformError`：原本 `verified` 仍算「已複製的個數」、讀回失敗時 `failed=[]`，`status` 會把該 tier 當已備份（append-only 台帳，永久）。改為失敗一律 `verified=0`、`failed` = 全部 chosen；`--forget-remote` 的 help 改成「整份清單」 | 台帳寫錯就改不掉 |

三次 NEEDS_CONTEXT（R3、R4、R5）都是「程式對、計畫的測試錯」，實作者正確地停下來而不是改測試。

## 2. 各任務審查發現

| 任務 | 發現與處置 |
|---|---|
| T1 core/proc.py | LOW：`PlatformError` docstring 提到 rclone 但訓練層仍拋 `VcpError`——計畫刻意（spec §13）。不修。 |
| T2 schema / 台帳 / 清單 | LOW：`manifest_ids()` truthy 過濾、`local_path` external 分支皆計畫原文。不修。 |
| T3 證據圖 run 走法 | MEDIUM：`unlisted` 重複計數 → R1；LOW：`for_` 就地 append（私有累加器）。 |
| T4 其餘走法、manifest 命令 | MEDIUM：去重沒測試 → R2；LOW：run 歸屬錯誤訊息呼叫 `load_run` 兩次。 |
| T5 dest / push | 3 LOW：`failure.fields.update` 就地改；中途失敗的計數（後由 R7 修正）；`proc: Any` 型別。 |
| T6 verify | 2 LOW：`dests.setdefault(..., open_dest(...))` eager 評估；`_run_card` / `_stage` 未用的 `paths` 參數（統一簽章）。 |
| T7 pull / status（含 verify `--tier`） | 2 LOW：`pull()` 約 100 行；同上的 setdefault。 |
| T8 端到端 / 真資料 / 文件 | LOW：驗收 4「改預測檔 → drift」在端到端以台帳 ts 篡改觸發（單元測試已直接改預測檔）。 |

## 3. 最終審查（全分支，opus）14 項與處置

| # | 嚴重度 | 發現 | 處置 |
|---|---|---|---|
| F1 | CRITICAL | `logs/vcp-*.jsonl` 在 `manifest` 命令自己跑完就長大（VERDICT 落地），`all` 清單 `push --tier ≥2` 必 `drift:`；README 的撤離流程走不通 | 修：清單是快照——push 對台帳角色推清單那一刻的前 N 位元組（前綴 sha 相同才算長大）、pull 視長大為已有；`sha256_prefix` 搬到 `core/hashing.py` |
| F2 | HIGH | 清單 `path` 未驗證（`..`、絕對路徑、磁碟）pull 可寫到根外；external 的 `source` 是原機器絕對路徑，新機器會自己建目錄 | 修：schema 只收根下的相對 posix 路徑、external 的 `source` 必須絕對；pull 做容器檢查（`unsafe_path:`）；external 只還原到既有目錄，否則 `external_skipped=`（WARN） |
| F3 | HIGH | `--forget-remote` 在 chosen 為空或只推 tier 1 時仍刪憑證 | 修：整份清單每個 `kind=file` 條目（含更高 tier 與 `present=false`）都在目的地驗過且至少一個 verified 才刪，否則 `forget_refused` 帶 `unverified=` |
| F4 | MEDIUM | `present=false` 條目讓 `verify --dest` 永遠 `missing` | 修：`absent` 桶（清單時已缺、目的地也沒有）不算失敗 |
| F5 | MEDIUM | `--overwrite` 拉回失敗只剩 `.bak` | 修：讀回不符或 `PlatformError` 都把 `.bak` 還原 |
| F6 | MEDIUM | `hashsum` 非 0 一律當空目錄（過期憑證會顯示成「整個備份不見了」）；不支援 sha256 的後端 | 修：exit 3 / 4 才是空，其餘 `PlatformError`（redact）；非 64 hex 的雜湊列拒絕 |
| F7 | MEDIUM | `status` 把沒查副本（或只查 tier 1）的 verify 當通過 | 修：`verified` 要 tier 3 副本層也過；另報 `local_ok` |
| F8 | MEDIUM | `all` 走法遇到失聯的 run 引用整個炸 | 修：`walk_all` 容忍，記 `Collector.skipped`，manifest WARN `skipped=` |
| F9 | LOW | pull 列借用 `missing` / `failed` | 修：`dest_missing` / `mismatch`（加 `external_skipped`） |
| F10 | LOW | `Manifest.data_root` 沒人讀 | 不修：留作人讀的來源標記（spec §14） |
| F11 | LOW | 失敗 VERDICT 掉 `dataset=` / `manifest=`（`cli_common` 跨層既有） | 待辦 |
| F12 | LOW | 台帳在傳輸後才開 | 修：`load_manifest` 之後立刻建 `BackupLedger` |
| F13 | LOW | 測試會跑到真 rclone（`status` 無 runner 注入） | 修：測試 monkeypatch `shutil.which` |
| F14 | LOW | `dest_kind("C:backup")`；push 讀檔多次；`first_bad` 標籤用 path；未用參數 | 待辦 |

## 4. 修正波再審（opus）殘餘與處置

1. MEDIUM（load-bearing）讀回 `hashsum` 失敗時 push 列 `verified=N`、`failed=[]` → R7 修。
2. 中途 `put` 失敗時 `verified` 算了只複製未讀回的檔 → 同 R7。
3. `--forget-remote` help 過時 → 同 R7。
4. 帶 `present=false` 條目的清單永遠不能 `--forget-remote` → 刻意（憑證刪除是唯一不可逆動作）；寫進 spec §14；操作上先從別的目的地 pull 回來或手動 `rclone config delete`。
5. `LocalDest.get` 的 `OSError` 不在 `.bak` 還原的 except 裡 → 待辦。
6. verify 的副本層拋 `PlatformError` 時不寫 verify 列 → 待辦（spec 4.2 沒有錯誤欄位，要先決定）。
7. `external_skipped` 的 CLI 呈現沒測試；UNSUPPORTED 雜湊列的 redact 斷言沒真的把秘密放在該行 → 待辦。
8. spec §14 未寫 → 本後記同日補。

## 5. 待辦

1. `pull._fetch`：`OSError`（本機目的地 `copy2` 失敗）也要把 `.bak` 還原並寫列；docstring「`.bak` 只在新檔驗過才留」才成立。
2. `verify`：副本層拋 `PlatformError` 時要不要寫一列（`copies=null` + 錯誤註記）——spec 4.2 加欄位再做。
3. 測試缺口：`vcp backup pull` 的 `external_skipped=` WARN 呈現；`FakeRemote(unsupported=True)` 把秘密放進 stdout 的雜湊列，驗 `redact` 真的跑到。
4. `cli_common.run_command`：失敗 VERDICT 併入命令層的識別欄位（`dataset=`、`manifest=`、`dest=`）——跨層改動，一次做。
5. `dest_kind("C:backup")`（磁碟相對路徑）會被當成 remote `C`——訓練層既有；改時兩層同步。
6. push 對每個檔最多讀四次（預檢 sha、本機目的地的 before hash、複製、讀回）；tier 3 的 GB 級檔可考慮快取本機 sha。
7. `_check_stamps` 的標籤用 `e.path`，`data` 與 `configs` 同相對路徑時 `first_bad` 會撞——改用 `e.key`（端到端斷言要跟著改）。
8. spec §11 端到端敘述「push --tier 2 → verify OK」早於 `--tier`；已在 §14 說明。
9. `train/upload.py` 的 `_hashsum` 仍把任何非 0 當空、copyto 失敗拋 `VcpError`（ABORT）——下次動訓練層時對齊 `backup/dest.py`（spec §13 刻意不在本層範圍）。
10. Plan 6 後記的 10 項待辦仍開著。

## 6. 方法筆記

- 全分支的最終審查（opus）抓到的是任務審查看不到的跨層語意：台帳長大 vs push、`--forget-remote` 的前提、清單路徑的安全——值得那個成本。
- 429 用量上限切斷實作者兩次：先 `git status` / `git log` 看留下什麼，再派 fresh implementer 以 report 檔為記憶收尾。
- Windows：測試重寫台帳 / 卡一律 `newline="\n"`；`Path.is_absolute()` 對另一平台的絕對路徑回 False——跨機器的路徑用 `PureWindowsPath` / `PurePosixPath` 兩邊都問。
- worktree 的 Bash 守衛拒絕 heredoc 與串接命令：改檔用 Write / Edit，git 一次一條、用 `--` 指定路徑。
- 計畫的 markdown 不跑 `ruff format`（Plan 6 R1）；計畫修正用小 Python 腳本做精確替換，每個替換 `assert count == 1`。

## 7. Plan 7c：§5 待辦的處置（2026-09-06，分支 `worktree-plan7c-hygiene`，3 個 commit）

| §5 項 | 處置 |
|---|---|
| 1 | 做了：`pull._fetch` 對 `OSError` 也還原 `.bak`；`pull()` 把它包成 `VcpError("copy_failed: …")`（ABORT），列照寫、計數併入 fields。 |
| 2 | 做了：`BackupRow.error`（可選）；verify 的副本層拋 `PlatformError` 時仍算本機兩層、寫列（`copies` 空、`error` 記訊息）再拋。 |
| 3 | 做了：`vcp backup pull` 的 `external_skipped=` WARN 有 CLI 測試；`FakeRemote(unsupported=True)` 的雜湊列帶秘密，驗到 redact。 |
| 4 | 做了：`run_command(..., context=)`——失敗時 `reason=` 之後保留命令層的識別欄位，成功時命令自己的欄位優先；五個 backup 命令都傳；其他層未動（可比照採用）。 |
| 5 | 做了：`dest_kind` 與兩個正則搬到 `backup/dest.py`（`train/upload.py` 再匯出）；Windows 上 `^[A-Za-z]:` 一律是磁碟。 |
| 6 | 不做：來源本來就要讀兩次（預檢 sha、複製），目的地讀回是驗證本體；tier 3 的成本可接受。 |
| 7 | 做了：時戳標籤改 `e.key`（`data/measure/beach/readings.jsonl:12`）；`backup.log.jsonl` 仍用檔名。 |
| 8 | 做了：spec §11 端到端句子配 `--tier 2` 並指向 §14。 |
| 9 | 做了：`train upload` 的 rclone 路徑改用 `RcloneDest`（`_hashsum` 刪除）：rclone 失敗 `PlatformError`（FAIL，原本 ABORT）、不在 `rclone_not_found`（ABORT）、`hashsum` exit 3 / 4 才是空；README 訓練層段落更新。 |
| 10 | 未動（Plan 6 後記的 10 項仍開著）。 |

審查（sonnet）：SPEC ✅、APPROVED，3 LOW：`open_dest` docstring 的「as in the training layer」過時（本次一併改）；spec §14「不寫 verify 列」過時（本次改）；`test_rclone_upload_reports_unverified_and_failures` 仍以 `VcpError` 接（`PlatformError` 是子類，不追）。全套 837 passed / 4 skipped、覆蓋率 96.36%。

## 8. 跨層識別欄位與第一階段處置（2026-09-07）

| 項 | 處置 |
|---|---|
| §5-4 / §7-4 | eval 8、fuse 3、train 3、submit 12 個命令全部採用既有 context；各層補一個早期失敗 CLI 測試。backup 的五個命令原已完成，不改 runner 或欄位 schema。 |
| §7-10 | Plan 6 Hygiene C 兩個 commit 完成，詳該後記 §9；其他小项結案／延期理由見各層最新節。 |
| §5-6 | 依指定範圍留到效能回合，目的地讀回為驗證本體，不能省去。 |
| Manifest.data_root | 留作人讀來源標記（spec §14），非未完成程式；移除會減少搬移後的可追溯性。 |

裁決：只傳命令已知的識別欄位，不為錯誤額外讀取 run / dataset — 早期驗證失敗也要可識別且不產副作用 — 可選值 None 省略，深層錯誤欄位仍覆蓋 context，保留真正失敗葉節點。沒有新增憑證或路徑選項。新測試先 4 failed；四層 CLI gate 56 passed。

第一階段最終驗證：933 passed / 4 skipped，覆蓋率 96.69%；真資料 8 passed / 3 skipped；ruff check / format --check 與 git diff --check 乾淨。自審：26 個命令逐一對照旗標與欄位型別；初版傳 None 的問題由既有 report / sigma 測試攔下並修正，既有斷言未弱化；未改台帳或 schema，未增加外部操作。
