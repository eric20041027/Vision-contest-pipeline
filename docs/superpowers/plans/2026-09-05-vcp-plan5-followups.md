# Plan 5 後記：裁決、審查發現與待辦（訓練層）

- 日期：2026-09-05
- 計畫：`docs/superpowers/plans/2026-09-05-vcp-plan5-training-layer.md`（8 任務）
- spec：`docs/superpowers/specs/2026-09-05-vcp-training-layer-design.md`（v1 + §14 補充決定）
- 分支：`worktree-plan5-training-layer`（自 main 的 `5948855` 分出；`.claude/worktrees/plan5-training-layer`）
- 執行方式：Subagent-Driven Development。預檢由控制者人工掃描（16 列接縫表）；每個任務一位實作者（轉錄型 haiku、整合型 sonnet）、一位審查者（sonnet）；四個任務各一輪修正（Task 1、4、5、6）；最終全分支審查一位（opus）、唯一修正輪、範圍限定再審後收尾。

## 1. 結果摘要

| 項目 | 值 |
|---|---|
| commit | 15 個（8 任務 + 4 任務修正 + 最終修正輪 2 個 + Task 8 的 README 格式）+ 收尾文件 |
| 測試 | 647 passed、4 skipped（海廢資料集未匯入 ×3、ensemble-boxes 未安裝 ×1） |
| 覆蓋率 | 96.61%（門檻 80%） |
| ruff | `check` 與 `format --check` 皆乾淨（README 的 Python 示範也經 ruff format） |
| 警告 | 本層測試在 Deprecation / Runtime / ResourceWarning 皆為錯誤下無警告；整套仍剩 Plan 2c 遺留的 PIL 未關檔 1 個 |
| 真資料 | `tests/integration/test_rsna_knee_train.py` 1 passed（RSNA 三個 study materialize 後以 `MaterializedReader(verify=True)` 讀回，shape 與 manifest 一致） |
| 端到端 | export yolo → `train run`（WARN venv=inherited，checkpoints=2、uploaded=2、verified=2）→ `status` unbacked=0 → `eval ingest`（不給 `--trained-on`）→ `eval measure` readings=2 → `--resume` attempt 2 → 第二個目的地 `upload` 兩次（第二次全 skipped）→ exit 1 的 run FAIL 且不上傳 |

交付：`src/vcp/train/`（schema、records、env、checkpoints、upload、run、status、reader、session）；`vcp train run|upload|status`（`cli_train.py`）；`vcp.data.split.assert_plan_matches`；README「訓練層命令」一節；CLAUDE.md 路徑與常用命令各一條。

## 2. 預檢裁決（執行前）

接縫表 16 列全部乾淨；計畫層決定 1–7（`--` 無法偵測改為「命令非空且不以 `-` 開頭」、`assert_plan_matches` 新增於資料層、`execute()` 的 `on_line` 接縫、rclone 經 runner 注入、探針在測試用當前直譯器、已在目的地的檔也回 `verified=True` 紀錄、命令可執行性預檢）。

## 3. 執行期裁決

- **R1（Task 1）** `ConfigRef.copy` 遮蔽 `BaseModel.copy` 產生 pydantic 警告 → 改名 `copied_to`（spec §4.2 的 yaml 鍵同步）。
- **R2（Task 2）** 探針字串裡一行超過 100 字元、ruff format 不能換行 → 等價改寫成 `if` 守衛。
- **R3（Task 5）** CLI 測試把「命令改了」的第三次呼叫當 `--resume`，但命令改了就是 config hash 改了、必須是新 run → 測試改用新 run id；行為不變。
- **R4（Task 5）** 命令可執行性預檢把相對路徑對「程序的 cwd」而非 `--cwd` 解析 → 抽出 `command_found(token, cwd, path)`。
- **R5（Task 6）** 冪等重跑 `upload_run` 每次都記 `uploaded` 事件 → 只對本次新出現的 `(dest, name, sha256)` 記事件（`run._upload_all` 同）。
- **R6（Task 8）** ruff 0.16 會格式化 README 內嵌的 Python → 示範改成 ruff format 穩定的寫法。
- **R7–R10（最終審查）** 見 §5。

## 4. 各任務審查發現與處置

| 任務 | 發現 | 處置 |
|---|---|---|
| 1 | `ConfigRef.copy` 遮蔽父類方法（Important，計畫規定）；`append_event` 的 payload 可用 `ts=` 蓋掉時鐘 | 改名（R1）；後者留待辦 |
| 2 | 探針行長；`gpus()` 缺 driver 欄位的列會被丟、driver 取最後一列 | R2；後者留待辦 |
| 3 | `resolve_final` 的「命中但未登記」分支、`mark_final` 未知身分清空所有 final、`expand([])`、TOCTOU 皆無測試 | 留待辦 |
| 4 | 兩個檔案被手工重排超出 ruff format、掉了一句註解（Important） | 修正輪：從計畫原文重生 + 一次 ruff format |
| 5 | CLI 測試把改命令當 resume（計畫缺陷）；相對命令對錯誤的 cwd 解析（Important，計畫規定） | R3、R4 |
| 6 | 冪等上傳灌事件（Important，計畫規定） | R5 |
| 7 | `Session` 重複「最後 attempt 或 1」邏輯、hash 兩次；reader `_key` 對無 view 無 seq_id 的列退回 `""` | 留待辦 |
| 8 | README 的 Python 示範被 ruff 格式化 | R6 |

## 5. 最終全分支審查（opus）

三輪通讀（spec + 台帳、10 個原始檔、測試與文件），三個探針腳本對真實套件重現發現。

**Critical（唯一修正輪處理，再審通過）**

1. `train run` 在命令結束後用**命令前的記憶體快照**改寫 `train.yaml`，把子程序透過 `Session.register_checkpoint` 寫的東西全部蓋掉——session 標的 final 永遠到不了 `weights_hash`（spec §6.1 第 8 步的 session 路徑是死碼）。修正：命令結束後先從磁碟重讀再套 attempt 更新；新增「子程序在 `train run` 底下用 `Session` 登記 final」的測試。
2. `train status` 以**路徑**判斷有無副本：`--resume` 換了權重後，舊 bytes 的驗證副本讓新 bytes 也算 backed，`unbacked=0` 卻沒有任何副本——正是本層要防的事。修正：以 sha256 判斷（全部紀錄），新 bytes 無副本即列為 unbacked。

**Important（同一修正輪處理）**：`upload._targets` 把同路徑不同 sha 的兩筆紀錄當撞名，resume 換權重後 `train upload` 一律 FAIL → 改為每個檔名取最新一筆（舊紀錄是歷史）；`execute()` 的 `Popen` 沒關 → `with` 區塊（本層測試在 `-W error::ResourceWarning` 下歸零）；`--resume` 改寫 `weights_hash` 沒留痕 → 覆寫前在 `history.jsonl` 記 `{"event": "replace", "field": "weights_hash", "old_sha256", "via": "train.run"}`。另補「同一命令、第二個 attempt 寫出不同權重」的 resume 測試（兩筆 `best.pt` 紀錄、只有最新是 final、history 一列、status 與 upload 的語意）。

裁決：**R7** 五項合成一個修正輪；**R8** minors 留待辦；**R9** spec §14 記錄：R1 改名、計畫層決定 1、包裝器在命令後重讀 `train.yaml`（§8.2 的推理修正）、有 predictions 的 run 被 resume 時 `weights_hash` 換寫留痕、上傳 / 備份的標的是每個檔名最新的一筆、`running` 的殘留 attempt 只揭露不調和；**R10（斷路器）** 再審在修正 diff 裡發現的新 Important——`with Popen` 下若讀取迴圈拋出非 `KeyboardInterrupt` 的例外（實際上是 stdout 消費者提早關閉時 `typer.echo` 的 `BrokenPipeError`），`Popen.__exit__` 會關管線後無時限 `wait()`，子程序若不再寫入就一直等——依規則不開第二輪，判為「真但不承重」（不影響任何持久狀態；修正前的程式碼是漏管線而非等待），列為待辦第 1 項。

## 6. 待辦（依優先序）

1. **`execute()` 的非 KeyboardInterrupt 例外**（R10）：在 `with Popen` 內把迴圈包成 `try/except BaseException: proc.terminate(); raise`（保留 KeyboardInterrupt 分支）或用 `finally` 做有時限的等待；補一個 `on_line` 拋 `RuntimeError`、子程序 sleep 的測試，斷言在 1 秒內返回。
2. **`--resume` 丟掉新一輪的 metadata**：`command` / `exports` / `notes` / `framework` / `seed` / `venv` 只在建立時寫，`--config` 相同但命令不同時 `train.yaml` 的 `command` 會描述錯誤的 attempt；可改為每個 attempt 各記 `command` / `seed` / `venv`。
3. **殘留的 `running` attempt 永不調和**：vcp 崩潰後 `status` 永遠 WARN `running=1`；`--resume` 可把上一個 `running` 標成 `interrupted`（或加 `train status --close-stale`）。
4. **`execute()` 之外的 Ctrl+C 沒有 VERDICT**：checkpoint hash 與上傳（GB 級權重要幾分鐘）期間的 `KeyboardInterrupt` 是 `BaseException`，逃過 `run_command` 的 `except Exception`——鐵則 2 的漏洞，屬 `cli_common` 基礎設施（各層皆受影響）。
5. **`Session.register_checkpoint` 對同一檔 hash 兩次**；`Session` 與 `register_checkpoint` 重複「最後 attempt 或 1」的邏輯。
6. **`append_event` 讓 payload 的 `ts` / `event` / `attempt` 蓋掉模組自己的值**（目前無呼叫者這麼做）：明確拒絕保留鍵。
7. **plan hash 檢查已有四份**（`dataset.py`、`ingest.py`、`fuse/members.py`、`split.assert_plan_matches`）：把前三份改用 `assert_plan_matches`（Plan 4 後記待辦 2 的延伸）。
8. **文件與 CLI 小項**：README 的 Python 示範沒 import `torch` 也沒定義 `NAMES`（示意用，可加註）；`train upload` / `status` 宣告 `--configs-root` 但不用；「final=skipped (command failed)」在 `interrupted` 時用詞不準；`--cwd` 不存在時是裸 OSError → ABORT 且留下 `running` attempt（加一行 `is_dir()` 預檢）。
9. **測試覆蓋**：`resolve_final` 命中但未登記、`mark_final` 未知身分、`expand([])`、TOCTOU、探針回非 dict JSON、`--only garbage`、`gpus()` 缺 driver 欄位。
10. **`train status` 對已被取代的舊紀錄**：舊 bytes 若從未上傳且已被覆寫，`unbacked` 會永遠列出它（誠實但吵）；可考慮 `superseded=` 另計或 `--current` 只看每路徑最新一筆。

## 7. 方法上的紀錄

- 八次任務審查抓到 4 個 Important，都是計畫層的缺陷（欄位名遮蔽、測試把改命令當 resume、預檢對錯 cwd、冪等重跑灌事件）——轉錄型任務的價值在審查者獨立跑程式碼與探針，而不是在實作者。
- 最終審查的兩個 Critical 都是**跨任務的生命週期接縫**（包裝器與 Session 對同一個檔的先後寫入；checkpoint 身分是 (path, sha) 但兩個下游只看 path）。下次預檢清單加兩條：「同一個檔有幾個寫者、各在什麼時間點、後寫者有沒有先重讀？」、「複合身分有沒有在某個消費者被壓成單鍵？」
- 修正輪本身也可能帶進新問題（`with Popen` 的 `__exit__` 語意）：範圍限定再審抓到了，斷路器規則讓它成為有記錄的待辦而不是第二輪。
