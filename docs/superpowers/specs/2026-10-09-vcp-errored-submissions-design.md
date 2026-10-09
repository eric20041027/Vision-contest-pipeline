# vcp 平台出錯、沒有分數的提交（VCP-048）設計

- 日期：2026-10-09
- 來源：比賽工作區 2026-10-09 的回報（不在本 repo，以下只留通用的缺陷）。
  - 兩發 Kaggle code submission 在隱藏重跑時出錯。
  - Kaggle 的列表顯示 `status` 為 `SubmissionStatus.COMPLETE`，`publicScore` 與 `privateScore` 都是空字串。
  - Python API 另有 `error_description`，但 CLI 2.2.4 的 `--format json` 沒有這個欄位。
  - `vcp submit sync` 回報 `scored=1`，兩發出錯的都沒寫列。
  - 從此 `vcp submit status` 一直算 `unscored=2`，VERDICT 永遠是 WARN。
  - `vcp submit final` 也看不出那兩個 id 在平台上沒有結果。
- 決定：使用者 2026-10-09 要求處理這個缺口。本 spec 沒寫到的設計選擇是實作者的裁決，記在後記 `docs/superpowers/plans/2026-10-09-vcp-048-errored-submissions-followups.md`。
- 版本：下一個 MINOR（0.14.0），理由見 §6。

## 1. 問題

- 平台把一發提交「做完但沒有分數」時，sync 只看分數，於是那一發在台帳裡跟「還在跑」一樣。
- 後果：
  - `status` 永遠把它算成 unscored 並 WARN；
  - `report` 分不出「出錯」與「還沒出分」；
  - `final` 可能選中一個在平台上根本沒有結果的 id。
- 平台的狀態本來就讀進來了（`PlatformSubmission.status`），只是對上 id 的那一發沒有地方存。foreign 列早就存 `platform_status`，不受影響。

## 2. 範圍

做：

- 平台契約：每一發多一個「出錯、沒有分數」的判斷（§3.1）。
- 台帳新事件 `errored`（§3.2），由 sync 寫入（§4.1）。
- 讀取端：
  - 每一發的結果歸屬（§4.2）；
  - `status`（§4.3）；
  - `report`（§4.4）；
  - `final`（§4.5）；
  - arrivals 的 ref 歸屬（§4.6）。

不做：

- **錯誤訊息的文字**：CLI 的 JSON 沒有，vcp 不碰憑證，也不改用 Python API。
- **foreign 列**：它本來就存 `platform_status`。
- **Manual 平台**：沒有列表可讀；需要時用既有的 `note` 事件手記。
- **`upload` 的 VERDICT**：上傳前同步照樣會寫 `errored` 列，但 VERDICT 不加欄位，跟 `scored` 一樣。
- **公開排行榜不存在的比賽**：這種比賽的 COMPLETE 本來就沒有 public 分數，會被判成出錯。目前沒有這種比賽，後記記下限制。

## 3. 資料模型

### 3.1 平台契約

- `PlatformSubmission` 加 `errored: bool = False`。放在最後一個欄位，給了預設值，既有的建構呼叫不必改。
- Kaggle 的判斷：
  - 先取 `status` 的最後一段：以 `.` 分割取最後一段，再轉小寫。所以 `SubmissionStatus.COMPLETE` 與 `complete` 都是 `complete`。
  - `errored = True` 的情況：
    - 最後一段是 `error`；
    - 最後一段是 `complete`，而且 `public` 與 `private` 都是 `None`。
  - 其他一律 `False`，包括 `pending`、有分數的、空狀態，以及不認得的狀態。
- `errored = True` 的一發一定沒有分數：狀態是 `error` 但帶了分數時，`errored = False`，照有分數處理。

### 3.2 台帳列

- `Event` 加 `errored`。
- 必填欄位：`submission_id`、`source`（sync 一律寫 `platform`）、`at`（平台時間）、`platform_ref`、`platform_status`。
- 不得帶 `public` 或 `private`：帶了就是 schema 錯誤，`errored needs no score`。
- 跟其他列一樣只 append、不改。

## 4. 行為

### 4.1 sync

- 對上 id 之後，綁定（2026-09-28 §4.4）照舊先做。
- 接著：
  - 這一發 `errored` → 走 errored 的路徑，不走 `scored` 的路徑：
    - 只在兩種情況寫一列 `errored`：
      - 這個 `platform_ref` 還沒有 `errored` 列；
      - 同一個 ref 最新的那列 `errored` 的 `platform_status` 跟這次不同。
    - 規則同 2026-09-28 §4.6，所以重跑 sync 不會重寫。
  - 有分數 → 照舊寫 `scored`。
  - 都不是（pending）→ 什麼都不寫，照舊。
- 同一個 ref 後來被平台重新計分時，照舊寫 `scored`。這列比較新，結果以它為準（§4.2）。
- `SyncResult` 加 `errored: int`，是這次寫入的 `errored` 列數。
- VERDICT：
  - `sync` 一律帶 `errored=`；
  - `errored > 0` 時 WARN：新的出錯只在寫入的那一次提醒；
  - JSON payload 帶 `errored`，是這次寫入列的 id 清單。
- `upload` 的上傳前同步用同一個 `reconcile`，所以一樣會寫 `errored` 列。

### 4.2 每一發的結果歸屬

- 一個 id 每一發 `uploaded` 的「結果」，用既有的 `assign_scores` 規則，套在這個 id 的 `scored` 列加 `errored` 列的聯集上：
  - 帶平台時間的列，歸給平台時間最近的那一發；
  - 同一發被歸到多列時，`ts` 最新的那列為準。
- 結果是 `scored` 列 → 已出分；是 `errored` 列 → 出錯；沒有 → 還沒出分。
- 只看分數的地方（`latest_score`、`report` 的 public／private）照舊只看 `scored` 列。

### 4.3 status

- 每個有上傳的 id，看最新那一發的結果：
  - 還沒出分 → `unscored`，照舊；
  - 出錯 → 新清單 `errored`，不算 unscored。
- VERDICT 一律帶 `errored=<n>`。人看的行加 `errored: <id>`，`--json` 帶 `errored` 清單。
- `errored` 不讓 `status` WARN：它是已結束的狀態，每次都 WARN 就是這次要修的問題。

### 4.4 report

- `ReportRow` 加 `errored: bool`：我們的上傳，歸屬結果是 `errored` 列時為 `True`；foreign 一律 `False`。
- 這一列的 `public`、`private` 是 `None`，last-vs-last 的 `delta` 跟還沒出分的一發處理相同。
- 人看的行，出錯時在行尾加 ` errored`。`--json` 的 rows 帶 `errored`。

### 4.5 final

- candidate 與 baseline 若有上傳、而且最新一發的結果是出錯 → `why="errored"`，不排名。
- 判斷順序是 `probe`、`not_uploaded`、`errored`，然後才是既有的 sealed 讀數與來源檢查。
- `errored` 不在 `NOT_RANKED`，所以會出現在「沒排名」的清單裡，讓人看到。

### 4.6 arrivals 的 ref 歸屬

- `SubmissionLedger.arrivals()` 判斷「foreign ref 是不是我們自己的上傳」時（`_ours`，VCP-038），`errored` 列跟 `scored` 列一樣，能把 ref 綁到 id。

## 5. VERDICT 與字彙

| 命令 | 變更 |
|---|---|
| `submit sync` | 新欄位 `errored=`（一律印）；`errored > 0` 時 WARN；JSON 加 `errored`（id 清單） |
| `submit status` | 新欄位 `errored=`（一律印）；人看的行 `errored: <id>`；JSON 加 `errored`（id 清單）；不因出錯 WARN |
| `submit report` | 每列多 `errored`（JSON），人看的行尾 ` errored` |
| `submit final` | `why` 的新值 `errored`，寫進 `final` 列的 `table` |

## 6. 相容性與版本

- 0.14.0（MINOR）：新台帳事件、新 VERDICT 欄位、`final` 表的新 `why` 值，以及平台契約的新欄位。
- 舊 vcp（≤ 0.13.0）讀到 `errored` 列會 FAIL `bad ledger row`：每個讀者都嚴格驗每一列。所以：
  - 一份台帳的所有讀者與寫者，要先升到 0.14.0，才能讓 0.14.0 對它跑 `sync` 或 `upload`；
  - `configs` 模式的台帳跟著 git 走，所有 checkout 都要先升級；
  - `shared` 模式的正本，同一個 data root 的所有 worktree 都要先升級。
- 既有台帳不必遷移：升級後第一次 sync，會替平台上已經出錯的那幾發補寫 `errored` 列。

## 7. 測試

- **平台**：
  - Kaggle 解析的每種狀態：`SubmissionStatus.COMPLETE` 有分數、COMPLETE 沒分數、`SubmissionStatus.ERROR`、ERROR 帶分數、PENDING、小寫舊格式、空狀態；
  - `errored` 只在 §3.1 的兩種情況為真。
- **schema**：
  - `errored` 缺任一必填欄位 → FAIL；
  - 帶 `public` 或 `private` → FAIL；
  - `errored` 在 `EVENTS` 裡。
- **sync**（用既有的假 runner 或假平台）：
  - 出錯的一發寫一列 `errored`、不寫 `scored`，VERDICT `errored=1` 並 WARN；
  - 第二次 sync 不寫任何列，`errored=0` 且 OK；
  - 狀態改變（COMPLETE → ERROR）再寫一列；
  - 之後被重新計分 → 寫 `scored`；
  - 台帳沒有上傳的出錯一發 → 先綁定，再寫 `errored`；
  - pending 照舊什麼都不寫；
  - `upload` 的上傳前同步也會寫。
- **status**：
  - 最新一發出錯 → 不在 unscored，在 errored，VERDICT OK（沒有其他 WARN 條件時）；
  - 同一個 id 先出錯、後來重傳而且還沒出分 → unscored；
  - 先出錯、重傳後出分 → 兩邊都不在。
- **report**：出錯的那一列 `errored=True`、`public=None`；其他列 `False`。
- **final**：
  - 最新一發出錯的 candidate → `why="errored"`，不被選中，列在沒排名的清單裡；
  - 出錯後重傳並出分的 → 照常排名。
- **arrivals**：foreign 列的 ref 只靠一列 `errored` 綁到我們不帶 ref 的上傳時，被吸收，不算兩次到達。
- **相容性**：用 0.13.0 寫的台帳（沒有 `errored` 列）照常讀寫，既有測試全過。

## 8. 文件

- `docs/reference/cli.md`：`submit sync`、`status`、`report`、`final` 的列。
- `CLAUDE.md` 與 `AGENTS.md`（逐位元相同）「提交治理」一段的事件清單加 `errored`。
- skill `vcp-train-submit-backup` 的提交部分，並鏡射到 `.agents/skills/`。
- 提交治理 spec（`2026-09-05-vcp-submission-governance-design.md`）加一條修訂，指回本 spec。
- 稽核文件新增第 18 節「補遺：VCP-048」，處置寫「已實作，待 0.14.0 發出」。
- 後記 `docs/superpowers/plans/2026-10-09-vcp-048-errored-submissions-followups.md`：裁決、已知限制（§2 的「不做」），以及待辦。
- `CHANGELOG.md` 不在這個分支改，發版時才加 0.14.0 條目。
