# VCP-041 後記：train run 的 dirty 工作樹（v0.11.0）

- 計畫：`2026-09-27-vcp-dirty-tree.md`；spec：`../specs/2026-09-27-vcp-dirty-tree-design.md`。
  - 執行期改過 spec 的 §3.1、§3.2、§4.1、§4.3、§5、§7。
  - 計畫裡的 `DIFF_ARGS` 是改之前的版本，以程式與 spec 為準。
- 分支 `feat/vcp-041-dirty-tree`，base `main` ed228ef（#30 已合併）。
- 審查經過：
  - 4 個任務各經一次任務審查；Task 1、Task 4 各走一輪修正。
  - 最終整支審查（opus）給出 4 個 Important，都是 git 邊角情況。一輪修正後，再審抓到那輪修正自己引入的 1 個退步，經使用者同意在開 PR 前修掉。
- 全套 1828 passed / 76 skipped，覆蓋率 95.27%（`gitstate.py` 92%）。
- Task 5（發版 0.11.0，與 VCP-040 + 042 一起）等 PR 合併後另做。

## 1. 執行期裁決

1. **Task 1 修正：patch 固定 `a/` `b/` 前綴與 `core.quotepath=false`。**
   - 原因：使用者的 `diff.noprefix` / `diff.mnemonicPrefix` 會讓 patch 無法 `git apply`，也就破壞了 spec §3.3 的承諾。
   - 另外，非 ASCII 檔名在不同機器上原本會得到不同的 sha。
2. **寫 patch 失敗時不降成 `git=null`，讓 run ABORT。**
   - 原因：patch 寫在 `train/`，跟緊接著寫的 `env.<n>.json` 是同一個目錄，而那個寫入本來就沒有保護。`--resume` 時 `_close_running` 會修好該 attempt。
   - 最終修正補了一項：失敗時清掉 `.tmp`。
3. **Task 4 修正：** skill 的 `reference.md` 在「`train run` 留下哪些檔」補上 `train/git.N.patch` 與 `git` 的欄位；SKILL.md 過時的那行一起改寫。
4. **最終審查修正（一輪）：**
   - submodule 裡的未追蹤檔不算改動：status 與 diff 都加 `--ignore-submodules=untracked`，空 diff 視為沒有 diff。
   - `--submodule=short`：`diff.submodule=log|diff` 不會讓 patch 變成無法套用的摘要。
   - 結尾的 `--`：頂層有叫 `HEAD` 的檔（NTFS 上叫 `head` 也算）時，命令不會失敗。
   - git 子程序不繼承決定 repo 的變數，並設 `GIT_OPTIONAL_LOCKS=0`。
   - `not_found:` 附上 git 的第一行錯誤；預檢 FAIL 也帶 `commit=`。
   - 文件補上機密提醒；orientation 地圖加上 `git.N.patch`。
   - 測試加 `isolated_git` fixture，不受開發機 git 設定影響。
5. **殘留退步（使用者選「開 PR 前修掉」，由 controller 直接改）：**
   - 問題：第 4 條把 `GIT_CONFIG_PARAMETERS` / `GIT_CONFIG_COUNT` 也清掉了。容器或 CI 用環境變數設的 `safe.directory` 因此失效，run 會悄悄記成 `git=null`。
   - 修法：跟 git 自己切換到別的 repo 時一樣，保留這些環境設定，只清掉決定 repo 的變數。另加測試，並把 `isolated_git` 的語系固定成 `LC_ALL=C`。
6. **延後：** 見 §3 第 1、2 條。

## 2. 已知限制

- vcp 只看 `--cwd` 所在的 repo。訓練程式放在別的 repo 時，那邊的改動看不到。
- patch 只含追蹤檔的差異。追蹤檔若含機密，會跟著 patch 進 data root 與備份（`train_dir`，tier 2）。
- `git diff` 仍可能更新 index 的 stat 快取。`GIT_OPTIONAL_LOCKS=0` 只保證 `git status` 不寫 index。
- `diff.context`、`diff.suppressBlankEmpty`、`GIT_DIFF_OPTS` 會改變 `diff_sha256`。vcp 只在同一台機器、同一個 run 裡比較這個 sha，patch 也照樣能套用。
- `dirty` 跟舊定義只差一點：submodule 裡的未追蹤檔不再算。
- 預檢與快照之間隔幾秒。工作樹若在這段時間才變髒，給了 `--require-clean` 也只會得到一般的 `modified=` WARN。

## 3. 開放待辦（依優先順序）

1. 發版 0.11.0 時一起更新 HANDOVER.md §6 第 9 條與 CODEX_PROMPT.md 的狀態行，VCP-040 + 042 與 VCP-041 都要改。
2. 如果需要的話，給預檢到快照之間變髒的情況一個獨立的 WARN（前面第 6 條延後的 M7）。
3. `vcp.core.build`（vcp 自己的 build string）的 git 呼叫也改用 `gitstate` 的子程序環境。
   - 目前在 git hook 裡，或 shell 設了 `GIT_DIR` 時，產物記的 `vcp_version` 可能是別的 repo 的 commit。
   - 它的 `git status --porcelain` 也還會順手改寫 index。
4. `not_found:` 取第一個 `fatal:` 行，而不是第一行 stderr。前面若有 `error:` 或 `warning:`，就會蓋過真正的原因。
