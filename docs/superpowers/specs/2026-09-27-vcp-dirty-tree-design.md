# vcp train run 的 dirty 工作樹（VCP-041）設計

- 日期：2026-09-27
- 來源：RSNA 第二輪回報的 VCP-041；稽核文件第 16 節；屬於 Wave 1c 的一部分。
- 使用者的決定（2026-09-27）：
  1. **只看追蹤中的檔**：WARN 與 `--require-clean` 都只看追蹤檔的改動。未追蹤檔只記路徑與數量。
  2. **記到最細**：記截斷的路徑清單、status 與 diff 的 sha256，並把 diff 存成 patch。
  3. **開始記一次、結束再比一次**：有變就 WARN。
  4. **資料放在既有的環境快照裡**：`train/env.<n>.json` 的 `git`（方案 1）。
- 版本：與 VCP-040 + 042 一起隨 0.11.0 發出（MINOR）。

## 1. 問題

`train run` 的每個 attempt 會在 `train/env.<n>.json` 記下 `git: {commit, dirty}`。只要 `git status --porcelain` 有任何輸出，`dirty` 就是 true。這有三個缺口：

- **不 WARN**：用改過、沒 commit 的程式訓練，VERDICT 照樣 OK。
- **擋不住**：沒有 `--require-clean`。比賽想規定「只能用 commit 過的程式」時，沒有辦法擋。
- **不記內容**：
  - 未追蹤的輸出檔也會讓 `dirty: true`，事後無法證明那只是無關的檔；
  - 追蹤檔的改動無從還原。

## 2. 範圍

做：

- 記錄 `--cwd` 所在 repo 的狀態（§3）：
  - 改動與未追蹤的數量和路徑；
  - status 與 diff 的 sha256；
  - 追蹤檔改動的 patch。
- `--require-clean` 預檢（§4.1）。
- 追蹤檔有改動 → WARN（§4.2）；訓練中途追蹤檔或 HEAD 變了 → WARN（§4.3）。
- `train run` 的 VERDICT 新欄位（§5）。

不做：

- **別的 repo 裡的訓練程式**：程式若不在 `--cwd` 所在的 repo（例如 `python /other/repo/train.py`），vcp 跟現在一樣只看 `--cwd` 那個。
- **未追蹤檔與 .gitignore 檔的內容**：只記路徑，vcp 不讀內容。
- **其他紀錄的格式**：`train status`、`run.yaml`、`train.yaml` 都不改。
- **submodule 的內容**：submodule 有改動算一筆追蹤改動，patch 裡只有它的 commit 那一行。

## 3. 資料模型

### 3.1 `train/env.<n>.json` 的 `git`

| 欄位 | 意思 |
|---|---|
| `commit` | HEAD 的 40 位 sha（不變）。 |
| `dirty` | porcelain 有任何輸出，含未追蹤檔（意思不變，與舊檔可比）。 |
| `modified` | porcelain 裡 `??` 以外的列數：暫存、未暫存、刪除、改名、submodule 都算。 |
| `untracked` | `??` 的列數。用 `--untracked-files=normal`，整個未追蹤的目錄只算一列。 |
| `modified_paths` | 那些改動列的路徑（改名取新路徑），照 git 的順序，最多 50 筆。 |
| `untracked_paths` | 未追蹤列的路徑，規則同上，最多 50 筆。 |
| `status_sha256` | §3.2 status 原始輸出的 sha256。 |
| `diff_sha256` | §3.2 diff 原始輸出的 sha256；`modified == 0` 時為 null。 |
| `patch_bytes` | diff 原始輸出的位元組數；`modified == 0` 時為 null。 |
| `patch` | `train/git.<n>.patch`（相對於 run 目錄）。沒有改動、diff 為空或超過上限時為 null。 |

- 數量欄位永遠是精確值，只有路徑清單會截斷。
- 新欄位都有預設值（0、空清單、null），所以舊的 `env.json` 照樣解析得動。vcp 本身不會讀回 `env.json`。

### 3.2 git 命令

所有命令都在 `--cwd` 所在 repo 的最上層目錄執行，也就是 `git -C <cwd> rev-parse --show-toplevel` 的結果。這樣可以避開 `diff.relative` 設定和子目錄的相對路徑。

- 狀態：`git status --porcelain=v1 -z --untracked-files=normal`
- diff：`git -c core.quotepath=false diff HEAD --binary --no-color --no-ext-diff --no-textconv --src-prefix=a/ --dst-prefix=b/`

解析規則：

- sha256 對 stdout 的原始位元組計算。
- 路徑以 UTF-8 解碼。無法解碼的位元組換成替代字元，這不影響 sha。
- `-z` 格式裡，改名與複製的列後面多一個原路徑欄位，解析時略過它。
- 明寫 `--untracked-files=normal`，讓使用者的 `status.showUntrackedFiles` 設定不會把未追蹤檔藏起來。
- 明寫 `a/` / `b/` 前綴：使用者設了 `diff.noprefix` 或 `diff.mnemonicPrefix` 時，patch 仍能直接 `git apply`。`core.quotepath=false` 讓非 ASCII 檔名在不同機器上得到同一個 `diff_sha256`。

### 3.3 patch 檔

- 只在 `modified > 0` 時產生。diff 以串流方式讀取：
  - 全部內容都算 sha256 與位元組數；
  - 位元組數在上限（10 MiB）以內，才寫成 `train/git.<n>.patch`，先寫 `.tmp` 再改名；
  - 超過上限就刪掉半成品，`patch` 記為 null。
- 內容是 git 的原始輸出。在乾淨的 `commit` 上用 `git apply` 就能還原當時的追蹤檔。
- 檔案放在 `runs/<id>/train/`，隨 `train_dir` 角色（tier 2）進備份。
- patch 只含追蹤檔的差異。如果追蹤檔本身含機密，機密會跟著 patch 進 data root 與備份，風險和 console log 相同。文件要寫明這一點。

## 4. 行為

### 4.1 `--require-clean`（預檢）

只在給了 `--require-clean` 時做。和其他預檢一起，在第一次寫入之前：

- 找不到 git、`--cwd` 不在 repo 裡、或 git 命令失敗 → FAIL `not_found: git repository for --cwd <path>`。
- `modified > 0` → FAIL `dirty_tree: <n> tracked path(s) changed: <前 5 個路徑>`，帶欄位 `modified=<n>`。
- 只有未追蹤檔 → 通過。

沒給 `--require-clean` 時，git 的問題都不 FAIL，`git` 照現在一樣記成 null。

### 4.2 開始時（第 6 步，環境快照）

- 寫 `env.<n>.json` 時，`git` 帶上 §3.1 的全部欄位；有改動就先寫好 patch。
- `modified > 0` → WARN，警告行寫：
  - `modified=<n> tracked path(s); patch train/git.<n>.patch`；
  - 超過上限時改寫 `patch too large (<bytes> bytes), sha256 only`。

### 4.3 結束時

位置在子程序結束、`finished` 事件之後，與 `evidence_changed` 同一處。

1. 重算 `commit` 與 `diff_sha256`，規則同 §3.2，這次不寫 patch。
2. 只比追蹤檔這一側：未追蹤檔不比，因為訓練常把輸出寫進 repo 裡的新目錄，這和決定 1 一致。
3. 任一項和開始時不同 → `train.log.jsonl` 寫一筆 `note` 事件，並 WARN `git_changed=`：
   - `key="git_changed"`；
   - `value` 是變了的項目（`commit`、`diff`），逗號分隔；
   - 另帶結束時的 `commit` 與 `diff_sha256`。
4. 結束時 git 命令失敗 → `value="unavailable"`，同樣 WARN。
5. 開始時 `git` 是 null（不在 repo 裡）→ 不比。

### 4.4 `--resume`

每個 attempt 各自做 §4.1–§4.3，各自寫 `env.<n>.json` 與 `git.<n>.patch`。`--require-clean` 只對當次呼叫有效。

## 5. VERDICT（`vcp train run`）

- `commit=<前 12 位>`。不在 repo 裡，或 git 不可用時，寫 `commit=none`。
- `modified=<n>`、`untracked=<n>`：只在 repo 裡出現。
- `modified > 0` → WARN。
- `git_changed=<commit,diff 的子集，或 unavailable>`：訓練中途有變時才出現，並 WARN。
- `dirty_tree:` FAIL 時帶 `modified=`。

## 6. 錯誤與判決字彙

| 情況 | 例外 | 狀態 |
|---|---|---|
| `--require-clean`，git 不可用或不在 repo 裡 | `ValidationFailed` `not_found:` | FAIL |
| `--require-clean`，追蹤檔有改動 | `ValidationFailed` `dirty_tree:`（新字） | FAIL |
| 追蹤檔有改動 | — | WARN（`modified=`） |
| 訓練中途 HEAD 或追蹤檔變了，或結束時取不到狀態 | — | WARN（`git_changed=`） |

## 7. 相容性與版本

- 與 VCP-040 + 042 一起隨 0.11.0 發出（MINOR）。這次的變動有：
  - 新選項 `--require-clean`；
  - VERDICT 新欄位；
  - 新 `reason=` 字 `dirty_tree:`；
  - `env.<n>.json` 與 `train.log.jsonl` 的新內容；
  - 新檔 `git.<n>.patch`。
- `GitInfo.dirty` 的意思不變，舊 `env.json` 照樣解析得動。
- `train.yaml`、`run.yaml`、provenance 索引的 schema 都不變。

## 8. 測試

測試用的 git repo 建在 `tmp_path`。git 設定只用 `-c user.name=… -c user.email=…` 傳入，不碰全域設定；找不到 git 就 skip。

- **乾淨 repo** → OK，有 `commit=`、`modified=0`、`untracked=0`，沒有 patch。
- **改了追蹤檔** → WARN，並且：
  - patch 檔存在；
  - `diff_sha256` 等於 patch 檔的 sha256；
  - 在乾淨 clone 上 `git apply --check` 通過。
- **只有未追蹤檔** → OK，`untracked=1`，路徑有記下。
- **解析**：暫存的改動、刪除、改名（`-z` 的第二欄位）、檔名含空白與非 ASCII。
- **`--require-clean`**：
  - 有改動 → FAIL `dirty_tree:`；
  - 不在 repo 裡 → FAIL `not_found:`；
  - 這兩種 FAIL 都不留任何檔；
  - 只有未追蹤檔 → 通過。
- **上限**：
  - 把 patch 上限調小 → 不存檔，但仍有 sha 與大小；
  - 把路徑上限調小 → 清單被截斷，數量仍是精確值。
- **結束比對**：
  - 子程序在訓練中改了追蹤檔 → 有 `note` 事件 `git_changed=diff`，並 WARN；
  - 子程序做了 commit → `git_changed` 含 `commit`；
  - 子程序只寫了未追蹤的輸出檔 → 沒有 `git_changed`。
- **`--resume`**：每個 attempt 各有一份 `env.<n>.json` 與 patch。

## 9. 文件與 skill

- `docs/reference/cli.md`：`train run` 那一列加上 `--require-clean`，以及 `commit=` / `modified=` / `untracked=` / `git_changed=`。
- 訓練層 spec：加修訂第 19 條，指回本 spec。
- `CLAUDE.md` 與 `AGENTS.md`：`train/` 放的東西加上 git patch，並寫明 patch 只含追蹤檔的差異。
- skill `vcp-train-submit-backup`：說明 `--require-clean` 和 `modified=` WARN 的意思；鏡射到 `.agents/skills/`。
- 稽核文件第 16 節：VCP-041 標為已實作（隨 0.11.0）。
