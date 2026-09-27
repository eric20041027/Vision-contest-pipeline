# VCP-040 + 042 後記：run 的證據檔與標籤集（v0.11.0）

- 計畫：`2026-09-26-vcp-run-evidence.md`；spec：`../specs/2026-09-26-vcp-run-evidence-design.md`。
- 分支 `feat/vcp-040-run-evidence`，base `main` 69918fc（v0.10.0），PR #30。
- 審查：
  - 11 個任務各經一次任務審查，Task 6、Task 11 各走一輪修正。
  - 最終整支審查（opus）給 4 個 Important，一輪修正後乾淨。
- 測試：全套 1805 passed / 76 skipped，覆蓋率 95.30%。
- skill 做了前後對照：
  - 沒有新文件時，agent 只能用 `s.note` 記 sha，偽標籤落到驗證集也只留下 `denied` 計數。
  - 有新文件後，agent 正確使用 `vcp data labels` / `--labels` / `--evidence`。
  - 驗證抓到 agent 把 `--id-field` 與 `--id-col` 搞混，已補進 skill。
- Task 12（發版 0.11.0）等 PR 合併後另做。

## 1. 執行期裁決

1. **Task 6**：`train run` 預檢補上 spec §5.2「名稱不衝突」。
   - 問題：計畫的預檢只擋重複的 `--evidence` 名稱。`--evidence X --labels X`，或續跑時同名換角色，原本要到寫下 `run.yaml` / `train.yaml` 之後才 FAIL。
   - 處置：`add_ref` 的規則抽成 `evidence_ref.check_name`。`check_names` 在 `_existing` / `_new` 之後、`_close_running` 與第一次寫入之前，檢查兩份清單。
2. **Task 7**：`check_names` 搬到 `vcp.data.evidence`，因為 `vcp.measure` 不能 import `vcp.train`。`eval ingest` 在複製任何證據前先查，避免留下沒被參照的副本。
3. **Task 11**：計畫漏了三處列出命令或產物種類的文件，已一起更新並鏡射：
   - `vcp-orientation` 的 SKILL.md 與 map.md；
   - `vcp-running-contests` 的 workflow.md。

   另外，`cli.md` 的 `train run` 列寫上 `evidence_conflict:`。
4. **Task 11 修正**：
   - 稽核 §16 的 VCP-040+042 段落改寫成已發出的內容。原文還在提議沒有發出的 `inputs[]` / `--input`。
   - skill 驗證時，agent 把欄名當成 `--id-field`。skill 已補上兩者的差別：`--id-field` 是樣本身分，`--id-col` 是欄名。
5. **最終審查修正（一輪）**：
   - 證據檔名在第一次寫入前驗證（`reserved_name:` / `unsafe_path:`，不分大小寫）。
   - `Session.attach_*` 附上前，對 `run.yaml` + `train.yaml` 查名稱。
   - 無表頭 CSV 的錯誤訊息只報欄數。
   - `duplicate_id:` 的欄位改成 `duplicate=`，不再蓋掉 `id=`。
   - 失敗欄位改成 `evidence_name=` 與單一的 `outside=`，不和成功欄位撞名。
   - 格式錯誤與空 id 用既有的 `invalid:`。
   - 一列都沒對到樣本時 WARN。
   - 沒被參照的證據副本不畫成 consumed：`_link_artifact_runs` 比照 `access_receipt` 跳過 `evidence`。
   - 標籤檔在讀取前後比對 sha（`drift:`）。
   - spec §4.2 / §6.2 / §7 跟著改。
6. **延後**：見 §3 第 1、2 條。

## 2. 已知限制

- `eval ingest` 只對 `run.yaml` 查名稱，因為 `vcp.measure` 讀不到 `train.yaml`。
  - 情境：訓練進行中，或 run 在結束合併前就掛了，這時跑 ingest。
  - 後果：下次合併後 `run.yaml` 可能出現同一名稱兩種 kind，`labels=` 就會報錯。
- 結束合併後，`current()` 依清單位置決定現行列，不依 `attached_at`。
  - 後果：ingest 附上的列可能在 `run.yaml` 裡蓋過之後 `--evidence` 附上的新 bytes。
  - 目前看不出錯：數量不變，label_set 也不可變。
- 相容性（發版說明要寫）：
  - 舊版 vcp 讀到帶 `evidence` 的 `run.yaml` / `train.yaml` 會 FAIL（`extra_forbidden`）。
  - 舊版 vcp 讀不了帶新角色的備份清單。
  - 訓練 venv 若裝的是舊版、非 editable 的 vcp，wrapper 附上證據之後，`Session.register_checkpoint` 會失敗。
- 一個 id 一列：偵測框這類一張圖多列的標籤，要先併成一列（JSONL 放 list）。

## 3. 開放待辦（依優先順序）

1. 證據產物 id `<run>-<name>-<sha12>` 可能撞號：`r1-fold0` + `teacher` 與 `r1` + `fold0-teacher` 會得到同一個 id。
   - 現況：會安全地 FAIL（`spec_mismatch:`），但發生在第一次寫入之後。
   - 做法：`train run` 預檢時，先對算出的 spec 做唯讀的 `store.reuse`。
2. `current()` 改依 `attached_at` 決定，或合併時依時間排序（spec 層級的改動）。
3. `eval ingest` 的名稱檢查要涵蓋 `train.yaml`。做法二選一：`train run` 結束合併時檢查並 WARN，或抽出一個不破壞分層的讀取器。
4. `meta.<key>` 空值去空白後，都會共用 `''` 這個鍵，於是 FAIL `duplicate_id:`。做法：比照 `None` 跳過空鍵。
5. `add_artifact` 對 `params.run` 的排除，改成和 `_link_artifact_runs` 同一組 `(access_receipt, evidence)`。目前證據產物走不到這裡。
6. spec §5.1 / §5.2 / §7 補上三件事（`cli.md` 已寫了 WARN）：
   - 證據檔名的 `reserved_name:` / `unsafe_path:`；
   - 標籤檔的 `drift:`；
   - 全外部時的 WARN。
7. 0.11.0 的 CHANGELOG 寫上 §2 的相容性事實。
