# 第三輪回報（VCP-044–047）與文件同步後記（v0.13.0）

- 計畫：`2026-10-04-vcp-round3-fixes.md`；spec：`../specs/2026-10-04-vcp-round3-fixes-design.md`（§12 是執行期補充決定）。
- 分支 `feat/vcp-044-047-round3`，base `main` d4e49e5（v0.12.0，2026-10 改寫後的歷史）。
- 審查經過：
  - 8 個任務各經一次任務審查。Task 5 在審查前補了一個修正（逾時結束整棵程序樹），Task 6 走了一輪修正（`root_mismatch:` 依後端說明）。
  - 最終整支審查（opus）沒有 Critical，給出 3 個 Important 與 12 個 Minor。一輪修正收掉 3 個 Important 與 7 個 Minor；再審確認全部修好，只多記 1 個 Minor（見 §3）。
- 全套 2044 passed / 76 skipped，覆蓋率 95.43%。
- Task 9（發版 0.13.0）等 PR 合併、使用者同意後另做。

## 1. 執行期裁決

1. **計畫的偏離（預檢時接受）：**
   - CLI 的接線（`root=`、`index_root=`、讀取命令解析 configs root）併進 Task 1，因為函式簽名一改，CLI 不跟著改就紅。
   - PostgreSQL 的 `sync` 先在交易外建 canonical 圖，再在寫入交易裡比對 root；它沒有前綴檢查，不會出現誤導的訊息（spec §12）。
   - HANDOVER 與 CODEX_PROMPT 的日期、0.13.0 的數字留給發版。
2. **commit 對照表保留標題**，包括含比賽名稱的：那些標題本來就公開在 git 歷史裡，表裡沒有資料或 id。
3. **Task 5：逾時要結束整棵程序樹。** 計畫的 `subprocess.run(timeout=)` 只殺得掉直接的子程序；Windows 上 `uv tool` 裝的 kaggle 是一個 trampoline 加 python 孫程序，孫程序佔著管道，120 秒的逾時切不斷。改成逾時就結束整棵樹。
4. **最終審查 I1：Windows 不開新的程序群組。** 第 3 條原本在 Windows 上也開新群組，結果 Ctrl+C 傳不到子程序、等待也中斷不了（實測 10 秒以上）。`taskkill /T /F` 不需要新群組；POSIX 照舊開新 session、`killpg`。結束樹之後若子程序還在，再 `proc.kill()`。
5. **Task 6：`root_mismatch:` 依後端說明。** SQLite 的檔名綁著 configs root，`--configs-root` 指錯或 checkout 搬過是 `not_found:`；SQLite 的 `root_mismatch:` 只在 data root 換了路徑或檔案被手動複製時出現。PostgreSQL 的 `root_mismatch:` 是 database 裝著別的 checkout（或 0.13.0 以前）的索引。七處文件一起改。
6. **Task 8：** gate 列寫成「別的 root 的索引放在這個 root 的路徑上讀時，是 `root_mismatch:`」，跟測試做的一致。
7. **最終審查 I2：`all` 清單跳過的 run 不留半份。** 走訪失敗時，把那次走訪加進去的項目全部收回；引用它的 judgement 與提交也一起跳過，跟「run 被刪掉」的處理一致，都列在 `skipped=`。
8. **最終審查的其他修正：**
   - `status` 對 rclone 上的副本、與 0.12 驗過的清單的背書規則補上測試（I3）。
   - 一份讀不了的 run 紀錄只讓那份清單變 `unchecked` 並說明原因，不再讓整個 `backup status` FAIL。
   - 程序樹測試的時限放寬到 3 秒。
   - `_root()` 解析不了 configs root 時，命令照樣以 VERDICT 收尾。
   - spec §12 補記執行期的決定；1.0 那句寫成「Wave 1（1a、1b、1c）」與「§11 的 Wave 1 清單第 4、5 項」，意思不變。
   - 測試註解不用內部流程用語。

## 2. 已知限制

- **索引跟著 checkout 走。** 搬移或改名 checkout 後要 rebuild 一次；刪掉 worktree 不會刪它在 `indexes/` 的檔。升級後每個 checkout 都要 rebuild 一次，舊的 `provenance.sqlite3` 不再讀。
- **PostgreSQL 一個 checkout 一個 database。** 0.13.0 以前建的 generation 要 rebuild 才能用。
- **0.12 讀不動新的備份紀錄。** 帶 `incomplete` / `local_copies` 的 `backup.log.jsonl` 列，0.12 讀到會 `extra_forbidden`；同一個 configs root 的寫入者要一起升級。
- **逾時是每一頁 120 秒。** Kaggle 列表分頁時，總時間可能是頁數乘以 120 秒，期間持有台帳鎖。啟動器先結束、留下孤兒佔著管道時，Windows 上的收尾讀取沒有時限。
- **回讀持有鎖。** CLI 失敗而回讀什麼都沒找到時，至少持有鎖約 17 秒；別的寫入者等 60 秒才 ABORT `locked:`。
- **外部權重的鍵跟機器有關。** `external/` 的鍵是寫清單那台機器的絕對路徑，換作業系統還原後會被當成缺口。
- **已刪原檔的 rclone 副本。** 副本自己也不見時，verify 讀成 `absent` 而不是 `missing`（0.12 就是如此）。
- **相對路徑的 `train upload --dest`。** 存的是打進去的相對路徑，0.13.0 判斷「副本在不在目的地裡」時以當下的工作目錄解析。
- **commit 對照表不收分支 commit。** 稽核文件提到的 `d193113`、`d881a1d` 從沒進 main，不在表裡。

## 3. 開放待辦（依優先順序）

1. **發版 0.13.0（計畫 Task 9）。** CHANGELOG 除了計畫列的，還要寫：
   - `--json` 的新內容（`replaced_roots`、四個 root 鍵、`status` 的 `index`）；
   - `Platform.upload(..., known_refs=)`、`make_backend` 與 `provenance_index_path` 的新簽名；
   - exit 0 只對上已知 ref 時會等滿約 17 秒；
   - `backup status` 現在會讀 run 紀錄；
   - 刪掉 worktree 後留下的索引檔。
   - 另外修掉 `CHANGELOG.md` 發版步驟第 4 步（main 受保護，要走 PR），以及 `HANDOVER.md` 第 25 行的「產物授權」。
2. **RSNA 的升級時機。** 0.13.0 要求每個 checkout rebuild 一次索引；RSNA 的 Claude 線用自己的 PostgreSQL service。比賽 10/22 截止前不建議換版，除非要用到這次的修正。
3. **`all` 清單的提交走訪。** `_walk_submissions` 在每個 id 的 `_try` 外面呼叫 `stage_json`，手改台帳留下一個不合法的 id，會讓前面已走訪的提交一起收回。把那個檢查搬進每個 id 的 `_try`。
4. **`load_yaml_model` 讓 `UnicodeDecodeError` 漏出來。** 截在多位元組字元中間的 `train.yaml` 仍會讓 `backup status` 與 `all` 清單 ABORT。在讀取器裡轉成 `ValidationFailed`。
5. **逾時的總時限與收尾。** 列表分頁加總時限；結束程序樹後的收尾讀取加時限。
6. **補測試：**
   - root 比對排在 replay 之前（目前刪掉提早的檢查，測試照樣綠）；
   - gate 列加上 VCP-047 的遮蔽測試；
   - `_inside` 的路徑分隔邊界（`usb-train` 對 `usb`）；
   - `_known_refs` 的範圍；
   - 上傳本身不加逾時；
   - `test_failure_message_falls_back_to_stdout` 現在是碰巧通過；
   - VCP-046 端到端測試加隱私掃描。
7. **文件：**
   - PostgreSQL 指南裡 `root=` 沒定義，`sync --json` 不該放在「接手時」；
   - 「其他寫入者等 60 秒」放在 17 秒旁邊容易誤讀；
   - 治理 spec §12 那列（回讀逾時是 `failed`，不一律 FAIL）；
   - PostgreSQL spec 第 139、141 行與交接文件第 84 行的舊索引路徑；
   - `vcp-provenance` skill 少了 SQLite `root_mismatch:` 的補救；
   - README 引用稽核 §11 沒附連結；
   - 稽核文件「建議處置」的舊措辭；
   - 對照表外的分支 hash 標註；
   - CLAUDE.md 與 AGENTS.md 逐位元相同沒有測試把關；
   - `local_copies` 的註解寫「sent」，實際是「在目的地處理過」。
8. **程式碼的小整理：**
   - `_publish_full` 太長；
   - `ROOT_KEYS` 的字面重複；
   - `check_roots` 的名稱與 docstring；
   - 測試輔助函式的第五份複本；
   - `_fake_rclone` 與既有故事測試的重複；
   - `completeness` 重寫 `store.manifest_path`；
   - `write_old_manifest` 重做建清單的分派；
   - `train status` 自己的「每個路徑取最新」；
   - `ManifestStatus.incomplete` 的預設值；
   - `_copy_source` 先比大小；
   - `verify` 的死分支；
   - `pull` 重寫 `travels()`。
9. **較舊的測試檔還有內部流程用語**（「Ruling N」「Task N」「Plan N」）。
10. **依某種順序列出 pytest 的檔案參數時出現「fixture 'pair' not found」。** 跟這個分支無關；全套的標準跑法正常。
