# VCP-038 第 2–4 段 + VCP-014 後記：共用提交台帳、加鎖與上傳前同步（v0.12.0）

- 計畫：`2026-09-28-vcp-shared-ledger.md`；spec：`../specs/2026-09-28-vcp-shared-ledger-design.md`。
  - 最終審查後改過 spec 的 §3.1、§4.1、§4.2、§4.4、§5、§6、§7、§8（spec 表頭有一行「執行期修訂」）。
  - 計畫裡「兩邊都沒有列就直接開始」與「`--from` 取代預設來源」是改之前的版本，以程式與 spec 為準。計畫 Task 9 的 CHANGELOG 草稿也還是舊規則，發版時要改寫。
- 分支 `feat/vcp-038-shared-ledger`，base `main` 341d94a（v0.11.0）。
- 審查經過：
  - 8 個任務各經一次任務審查；只有 Task 6 走了一輪修正。
  - 最終整支審查（opus）沒有 Critical，給出 4 個 Important 與 8 個 Minor。
  - 一輪修正收掉 4 個 Important、5 個 Minor 與 3 個缺的測試；再審確認全部修好。
  - 再審抓到那輪修正自己引入的 1 個測試問題（見第 1 節第 9 條），經使用者同意在開 PR 前修掉。
- 全套 1923 passed / 76 skipped，覆蓋率 95.35%（`lock.py` 86%，`adopt` / `guards` / `ledger` / `location` / `sync` 100%，`actions` 97%）。
- Task 9（發版 0.12.0）等 PR 合併、使用者同意後另做。

## 1. 執行期裁決

1. **Task 6 修正：adopt 讀每個來源時，也拿那份台帳自己的鎖。**
   - 原因：還停在 `ledger: configs` 的 worktree 可能在 adopt 讀到一半時寫入來源。結果不是讀到半列而 FAIL，就是正本漏掉那一列。
   - 鎖的順序：先拿正本的鎖，再一次拿一個來源的。不會死結。
2. **切換程序寫進文件**（skill、`cli.md`、CLAUDE.md / AGENTS.md）。最終審查後的版本見第 3、4 條。
3. **最終審查 I1：正本只能由 adopt 建立（改了 spec §4.1）。**
   - 問題：原本「兩邊都沒有列 → 直接開始」。本身台帳是空的 checkout，第一次寫入就會建出正本；別的 checkout 之後 adopt 只會得到 `exists:`，它們的歷史從此不算配額、擋不住重傳、`final` 也看不到。
   - 修法：`ledger: shared` 而正本不存在時，所有 submit 命令（含 `status`、`report`）一律 `not_adopted:`。adopt 沒有任何來源列時寫出空的正本。
   - 代價：新比賽用 `shared` 要先跑一次 adopt。
4. **最終審查 I2：adopt 一定收本 checkout 的 configs 台帳。**
   - 問題：原本給了 `--from` 就取代預設來源。照 skill 的範例只列別的 worktree，自己的歷史就漏了，而 adopt 只有一次機會。
   - 修法：本 checkout 的台帳是檔案就一定收，`--from` 再往上加。
   - 來源依正規化後的絕對路徑去重。`--from` 指到正本自己直接 `invalid:`；原本會自己跟自己搶鎖，等 60 秒才 ABORT。
5. **最終審查 I3：同步比對描述時，先看開頭的 id。**
   - 問題：「描述提到哪個 id」的規則會把 `S2 same as S1` 對到 S1。這個分支開始寫綁定列，S2 自己那列沒有 ref，於是平台 2 發在台帳算成 3 發，而且只增不改的台帳收不回來。
   - 修法：先試 `leads`（vcp 自己寫描述的格式），都不中才退回 `mentions`。綁定規則不變。
6. **最終審查 I4：`final` 之後重傳選中的 id，仍要 `--force`。**
   - 問題：`board_rule=last` 時 `final` 會說 `needs_reupload`，但重傳護欄一定擋，訊息也沒說要加 `--force`。
   - 裁決：spec §4.5 不改，護欄對所有 id 一致。`already_uploaded:` 的訊息說明怎麼加 `--force`；`final` 需要重傳時，直接印出完整命令（`--force "final re-send"`）。
   - Manual 平台不能用 `upload`（`manual_platform:`），`final` 改為提示手動重傳後跑 `vcp submit record`。
7. **最終審查的其他修正：**
   - 上傳前同步之後才 FAIL 時，VERDICT 帶 `sync=` / `bound=`：同步寫的列會留在台帳。
   - `record --platform-ref R` 若 R 已經有 `uploaded` 列 → `exists:`，不寫。`arrivals()` 同一個 ref 只算一次，因為 adopt 可能合進兩列。
   - `stage` 保留 `submissions.jsonl` 這個 id；`locate` 改用 `is_file()`。
   - `file_lock` 在同一個程序重入同一把鎖時立刻 `locked:`，不再等 60 秒。
   - 補三個測試：
     - 非有限分數 FAIL 後台帳不變；
     - configs 建的索引在 adopt 後 sync，會加上正本的檢查點；
     - `submission:` 結論在未 adopt 時 `not_adopted:`。
   - 文件：
     - `ledger=` 只在成功與 `not_adopted:` / `not_shared:` 時出現；
     - 寫到一半的最後一列怎麼修；
     - 別的 checkout 正在寫時，provenance 可能要重跑；
     - `upload` 持有鎖的範圍。
8. **保留不改：**
   - `SubmissionLedger.append` 直接改 `self.rows`：同一個交易裡，`reconcile`、配額與護欄要看到剛寫的列，這是刻意的。
   - 第一次上傳就給 `--force`，照樣記 `reason` 並帶 `forced=true`。
9. **再審抓到的殘留（使用者選「開 PR 前修掉」，由 controller 直接改）：**
   - 問題：`test_the_wait_is_a_minute_in_half_second_steps` 換掉的是 `time` 模組本身的 `sleep`，而且一直開著。POSIX 上 `Popen.wait(timeout=...)` 也用它輪詢，ubuntu 那一腿必紅；Windows 用 `WaitForSingleObject`，本機看不出來。
   - 修法：`monkeypatch.context()` 只包住取鎖那一段。在 Windows 上模擬 POSIX 的 wait：舊寫法紅、新寫法綠。
   - 同時修掉再審的兩個小項：`test_adopt` 一個測試的說明與它驗的東西不符；治理 spec §6.4 第 6 條的重傳補上 `--force` 與指向第 30 條。

## 2. 已知限制

- **跨機器不在範圍。** `shared` 台帳在 data root、不進 git，別台機器看不到。多台機器仍是各用 `configs` 台帳，再靠 git 的 `merge=union` 合併。
- **`shared` 台帳的保護靠備份。** 它不在 git 裡，data root 壞掉而沒有 `vcp backup push` 過，就沒了。
- **鎖只擋 vcp 自己。** 鎖在另一個檔上，不擋其他程式直接改台帳。`configs` 模式下，`git pull` / `checkout` 仍可能在交易進行中改寫台帳。
- **0.11 以前的 vcp 不加鎖。**
  - `ledger: configs`（預設、不寫出）時，新舊 vcp 可以同時寫同一份台帳。
  - 設了 `ledger: shared` 的 `submit.yaml` 舊 vcp 讀不動（`extra_forbidden`），所以切換本身擋得住沒升級的 worktree。
- **adopt 之後才切換的 worktree（落隊者）。** adopt 只有一次。之後才拉到 `ledger: shared` 的 worktree，它在那之前寫進自己 configs 台帳的列不會進正本，vcp 也不會提醒。所以切換時要先讓每個 worktree 都拉到 `ledger: shared`，再 adopt。
- **綁定的 10 分鐘窗。** 一列沒有 ref 的上傳，會吃掉同一個 id 在前後 10 分鐘內的所有平台提交（spec §4.4 的規則）。10 分鐘內用網頁再傳一次同一個 id，那一發不會算進配額。
- **少了換行、但本身完整的最後一列。** 寫入時斷電，剛好只差最後的換行：
  - 唯讀命令把它當成還沒寫完而略過；
  - 下一個寫入者卻把它當成一列讀進來，再把新列接在同一行後面；
  - 兩列從此都解析不了，要手動拆開。
- **鎖檔內容只供診斷。** 等到逾時的程序若剛好碰上新持有者已拿到鎖、還沒寫入持有者資訊，訊息會指名前一個持有者。
- **macOS 的大小寫。** 鎖的身分用 `normcase` 後的絕對路徑。macOS 上 `normcase` 不改大小寫，data root 的寫法要一致；Windows 與 Linux 沒有這個問題。
- **Manual 平台沒有上傳前同步。** 它沒有列表可讀；`record` 補記已上傳過的 id 也只 WARN。

## 3. 開放待辦（依優先順序）

1. **發版 0.12.0（計畫 Task 9）**：版號三處、CHANGELOG、HANDOVER.md / CODEX_PROMPT.md 的狀態行、README。CHANGELOG 與 RSNA 升級指示要寫清楚切換程序：
   1. 所有寫入者先升到 0.12.0。
   2. 每個 worktree 都拉到 `ledger: shared`。
   3. 在任一個 checkout 跑一次 adopt；沒有舊台帳也要跑。
2. **落隊者提醒**：`shared` 模式下，本 checkout 的 configs 台帳有正本沒有的列時，`status` 與寫入命令 WARN（用 adopt 的內容鍵比對）。要不要順便給一個把落隊列補進正本的路徑，得先想清楚 `ts` 單調與備份清單的前綴。
3. **不完整的最後一行**：寫入者拿到鎖後，台帳最後一個位元組不是換行就 FAIL，並指向修復步驟，不把新列接在那一行後面。
4. **Kaggle CLI 沒有逾時**：現在它在鎖裡跑，卡住就會擋住所有 checkout 的寫入（`locked:` 訊息會指名 pid）。列表呼叫給一個寬鬆的逾時，逾時算 `sync_failed:`。
5. **綁定的 10 分鐘窗**：改成「每列沒有 ref 的上傳最多吃一發、先吃最近的」。要改 spec §4.4。
6. **`backup pull --overwrite`**：換掉正本時沒有拿鎖。改成拿 `ledger_lock`，或拒絕覆寫 `shared` 台帳。
7. **adopt 的來源檢查**：檢查 `--from` 屬於這個 dataset（例如上層目錄名），並給一個明確的覆寫選項。
8. **adopt 之後的封槍狀態**：各 checkout 的 lock / unlock 列依 `ts` 交錯，`lock_state()` 變成「跨 checkout 最新的那列」。adopt 的輸出可以說明，合併後的封槍狀態跟各來源是否不同。
9. **小項：**
   - 正本位置是目錄時，`locate` 說 `not_adopted:`，adopt 卻說 `exists:`。應該 `invalid:`；只有 0.12.0 以前 stage 過叫 `submissions.jsonl` 的 id，或手動建目錄，才碰得到。
   - `_write_holder` 沒檢查 `os.write` 的短寫。
   - `final --dry-run` 沒有併發測試。
   - 缺同一次 sync 既綁定又寫分數的單一測試。
   - `ledger.uploads(id)` 在 `assert_not_uploaded` 算兩次。
   - 沒有 `--force` 用在沒上傳過的 id 的測試。
   - `_walk_submissions` 每個 staged id 讀一次 `submit.yaml`；`ledger_mode` 每個命令結束後重讀一次 `submit.yaml`。
10. **CLAUDE.md 與 AGENTS.md**：在這個分支之前就有幾行不同步，找時間對齊。
