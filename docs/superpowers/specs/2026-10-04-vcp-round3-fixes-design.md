# vcp 第三輪回報修正（VCP-044–047）與文件同步設計

- 日期：2026-10-04
- 來源：
  - 一個比賽工作區在 2026-09-26 升到 0.10.0 時回報的四個缺陷（VCP-044～047，接續 VCP-043）。0.11.0 與 0.12.0 都沒處理，稽核文件也沒收錄。
  - 2026-10-04 對 repo 文件做的一致性檢查（稽核狀態、交接文件、1.0 門檻的寫法）。
- 四個缺陷都在 0.12.0（main `d4e49e5`）上用 probe 重現過。
- 使用者的決定（2026-10-04）：
  1. 1.0 的門檻照現有規則：稽核 Wave 1 全部落地（含 1c：程式碼快照與產物授權）之後才發 1.0。
  2. 先做這一份：VCP-044～047 加文件同步，發 0.13.0。
  3. 核准設計表（本文 §3–§6）。
- 版本：0.13.0（MINOR）。

## 1. 問題

- **VCP-044：provenance 索引不認 configs root。**
  - SQLite 索引一個 data root 只有一份（`<data_root>/indexes/provenance.sqlite3`）。
  - 台帳檢查點的鍵只有 `configs/<相對路徑>`，驗證時用「當下」的 configs root 解析。
  - 兩個 checkout（兩個 configs root）共用一個 data root 時，從沒建索引的那邊驗證，分岔的台帳會被報成 `prefix_drift: … became shorter than N bytes` 或 `changed inside the consumed prefix`，看起來像只增台帳被截短或竄改。任一邊 rebuild 之後，失敗就換到另一邊。
  - PostgreSQL 一個 database 一份索引，另一個 root 的 `sync`（v1 是整份重建）會無聲取代前一份。
- **VCP-045：舊備份清單缺檔看不出來。**
  - 0.10.0 以前，同檔名、不同路徑的權重（例如多折的 `fold-N/model.pt`）在清單裡只留最後一個。
  - 0.10.0 只改了建清單的方式。舊清單照樣通過 `verify`、tier 3 的 push 與 verify，`status` 也說 verified，缺的權重從沒到過目的地。
- **VCP-046：本機的 `remote_copy` 被當成異機備份。**
  - `train upload --dest <本機目錄>` 驗過的權重，在清單裡記成 `remote_copy`。
  - tier 3 push 只送 `file`、不送它；verify 到它自己的本機目錄就地驗；`status` 說 verified；`--forget-remote` 照樣刪掉憑證。權重其實從沒離開這台機器。
- **VCP-047：Kaggle CLI 失敗時不回讀。**
  - CLI 送出後在等回應時斷線（非 0 退出），vcp 直接 FAIL、不寫列，但平台其實已經收下。
  - 0.12.0 的上傳前同步，會在下一次 upload 時把它綁進台帳並擋下重傳。但第一個命令仍然報錯了事實；下一次同步前，台帳、`status` 與配額都少算；`--no-sync`、`--force`，或平台晚一點才列出時，仍會多扣一格。

## 2. 範圍

做：

- VCP-044：每個 configs root 一份 SQLite 索引；兩種後端都記錄並檢查索引的 root（§3）。
- VCP-045：清單的完整性檢查（§4）。
- VCP-046：本機的 `remote_copy` 跟著目的地走（§5）。
- VCP-047：CLI 失敗時回讀；Kaggle 的列表呼叫加逾時（§6）。
- 文件同步（§10）。

不做：

- 索引路徑的覆寫（環境變數或選項）。之後需要再加（MINOR）。
- `train status` 的 `backed=` 把本機上傳算成已備份（同一個問題的下一層）。記進後記的待辦。
- `backup pull` 還原後的完整性 WARN。
- 1.0 的發版，以及「1.0 承諾什麼」的說明：留給 1.0 的發版計畫。

## 3. VCP-044：索引的 root 身分

### 3.1 root id

- `vcp.core.paths.path_id(path)` = `sha256(os.path.normcase(str(Path(path).resolve())))` 的前 16 碼。
- 跟 0.12.0 鎖檔名的規則相同；`vcp.core.lock.lock_path` 改用這個函式。
- 同一個目錄的不同寫法（磁碟代號大小寫、8.3 短名、junction／symlink）得到同一個 id。
- 不在 configs root 裡放 id 檔：
  - commit 進 git，每個 worktree 會共用同一個 id，正好分不出要分的兩邊；
  - 不 commit，又會被 `git clean -fdx` 刪掉，或被 `cp -r` 複製。
- 代價：搬移或改名 checkout 後要 rebuild 一次。索引本來就可刪可重建。

### 3.2 SQLite

- 檔名：`<data_root>/indexes/provenance-<configs root id>.sqlite3`；`provenance_index_path(data_root, configs_root)`。
- metadata 新增 `configs_root_id`、`configs_root`、`data_root_id`、`data_root`（路徑只供顯示）。`SCHEMA_VERSION` 從 2 變 3。
- 舊檔 `indexes/provenance.sqlite3` 不再讀：
  - 新檔不存在時 FAIL `not_found:`；
  - 訊息指出舊檔，並說「每個 checkout 跑一次 `vcp provenance rebuild`；沒有 0.12 使用者之後可以刪掉舊檔」。
- 直接打開 schema 2 的檔 → FAIL `mismatch:`（既有字）。

### 3.3 PostgreSQL

- 每個 generation 的 metadata 新增同樣四個鍵。不改 DDL，`POSTGRES_SCHEMA_VERSION` 維持 1。
- 規則：一個 database（service）只服務一組（data root, configs root）。
- `sync`（v1 是整份重建）遇到別的 root 的 generation → FAIL `root_mismatch:`，不取代。
- `rebuild` 是既有的修復路徑：可以取代別的 root 的 generation，但 WARN `replaced_root=<id>`。
- 0.13.0 以前建的 generation 沒有記 root：
  - 除了 `rebuild`，所有命令都 FAIL `root_mismatch:`（`index_root=none`，訊息是「這份索引沒有記 root；請 rebuild」）；
  - `rebuild` 會取代它，並在人類訊息裡說明。

### 3.4 檢查順序與訊息

- 每個會打開索引的路徑，第一件事是比對 root（configs root id 與 data root id），在任何 replay 或前綴檢查之前。這些命令是 `verify-index`、`status`、`sync`、`ingest`、`impact`、`stale`、`explain`、`graph`。
- 不符 → `ValidationFailed` `root_mismatch:`，訊息寫出兩邊的路徑，不會出現「became shorter」或「consumed prefix」。
- 同一個 root 內真的截短或改寫台帳 → 仍然是 `prefix_drift:`。
- `impact`、`stale`、`explain`、`graph` 從此也要解析 configs root：沿用既有的 `--configs-root`，預設是 repo 的 `configs/` 或 `VCP_CONFIGS_ROOT`。
- 共用台帳（0.12.0 的 `ledger: shared`）與 `logs/*.jsonl` 不用特別處理：它們在 data root、只在鎖下增長，每個 root 的索引各自記它們的檢查點。

## 4. VCP-045：清單的完整性

### 4.1 規則

- 新模組 `vcp.backup.completeness`，函式 `manifest_gaps(manifest, paths)`：
  - 清單裡的每個 `train_record`，不論結論種類（`run:` / `judgement:` / `submission:` / `all`），都讀出它的 `TrainRecord`。
  - 必須列出的權重：每個登記路徑，取 `registered_at` 不晚於清單 `created_at` 的最新一筆。`registered_at` 解析不了的也算必須。
  - 「列出」= 清單裡有那個鍵，任何角色、任何種類都算。`runs/<id>/train/` 底下的權重會以 tier 2 的 `train_dir` 列出，也算數。
  - 證據與標籤集同一條規則：`run.yaml` / `train.yaml` 的 `evidence` 裡，`attached_at` 不晚於 `created_at` 的每一筆，它的 `artifacts/<kind>/<id>/manifest.json` 必須列出。
  - 鍵的算法跟建清單時用同一個函式（`entry_key`，從現在的 `Collector.locate` 抽出）。「每個路徑取最新一筆」也抽成共用函式；現在 evidence、verify、train upload 各有一份。
- `train.yaml` 只會追加權重。所以清單建立之後才登記的權重不算缺：那是過期（stale），drift 已經會報。

### 4.2 誰檢查

| 位置 | 行為 |
|---|---|
| `backup verify`（一致性層） | 每個缺口一個問題：`manifest_incomplete:<run>/train.yaml:checkpoints.<path>`。`reason=manifest_incomplete`，排第一順位，因為補救方式是換新清單。VERDICT `incomplete=N`，跟 `drift=` 一樣一律印出。FAIL。 |
| `backup status` | 每份清單都重算，舊的 verify 列不能背書：缺檔 → `verified=false`、WARN，VERDICT `incomplete=<缺檔的清單數>`。本機沒有 train record 時，看 verify 列的 `incomplete`；再不行就看清單的 `vcp_version`：早於 0.10.0 → `unchecked`，不算 verified。 |
| `backup push --tier 3` | 動任何檔案之前 FAIL `manifest_incomplete:`，帶 `incomplete=N`，不寫 push 列。tier 1、2 不受影響。 |
| `backup push --forget-remote` | `forget_refused:`，帶 `incomplete=N`。 |
| `backup manifest` | 建完自檢。有缺口代表建清單的程式有 bug → `InvariantError`（ABORT）。 |

### 4.3 寫入

- `backup.log.jsonl` 的 verify 列多一個 `incomplete`，大於 0 才寫。
- `status` 的 `local_ok()` / `passed()` 也要求 `incomplete` 為空。
- 清單本身的格式不變。

## 5. VCP-046：本機的 `remote_copy` 跟著目的地走

### 5.1 規則

- `vcp.backup.dest.covers(dest, remote_copy)`：`remote_copy` 只有在兩種情況下就地驗、不推：
  1. 它在 rclone remote 上（`dest_kind(rc.dest) == "rclone"`），本來就不在這台機器；
  2. 目的地是本機，而且副本就在目的地裡面。
- 其他的 `remote_copy`（本機副本、不在目的地裡），在這個目的地就當成 `file`：
  - 推到 `<dest>/<root>/<path>`（標準佈局，`pull` 會還原到原路徑）；
  - 在那裡驗、從那裡拉，`--forget-remote` 也把它算進去；
  - 不管清單的 `present` 是什麼，它都必須在。
- 所有目的地一律適用，包括本機目的地（例如外接硬碟）。「在 D 驗過」對每種目的地都是同一個意思：D 有每一項，已經在 rclone remote 上的副本除外。

### 5.2 各命令

- **push**：
  - 送出的項目 = tier ≤ N 的 `file`，加上沒被 cover 的 `remote_copy`。
  - 來源：原 checkpoint（sha 與清單相符）；否則用 `train upload` 留下的那份本機副本（路徑由上傳時的命名規則算出，sha 相符）。兩者都不符 → 動任何檔案之前 FAIL（`not_found:` / `drift:`）。
  - VERDICT 帶 `local_copies=N`；push 列也帶，大於 0 才寫。
- **verify**：
  - 沒被 cover 的 `remote_copy` 在目的地驗，計入 ok / missing / mismatch。
  - 它不在目的地 → `missing`，不是 `absent`。
  - verify 列帶 `local_copies`。
- **status**：清單 M 在目的地 D 有沒被 cover 的副本時，只有帶 `local_copies` 的 verify 列（0.13.0 以後寫的）才算數，0.13.0 以前的列不再背書。tier 3 的 push 列也一樣。
- **`--forget-remote`**：未驗證的項目包括沒被 cover 的副本。
- **pull**：沒被 cover 的副本從目的地拉；目的地沒有時，退回它自己的本機副本，同機還原照舊能用。
- **manifest**：
  - 同一個路徑有多個驗過的上傳時，優先選 rclone 的，同一種裡取最新的。
  - VERDICT `local_copies=N`：本機副本的數目，只供參考。

### 5.3 判決字

- 不新增。失敗沿用 `not_found:`、`drift:`、`missing`、`mismatch`、`forget_refused:`。

## 6. VCP-047：Kaggle CLI 失敗時回讀

### 6.1 流程

- CLI 非 0 退出時，在同一個上傳交易裡（持有台帳鎖）做一次 VCP-037 的回讀，時間窗與比對規則相同。exit 0 的行為不變。
- 回讀排除台帳已經記下的 ref。`--force` 重傳時，舊的那一發還在平台列表上，排除之後才對得到新的那一發。exit 0 的回讀也照這樣做。
- `UploadResult` 新增 `exit_code`，預設 0。

### 6.2 結果

| 回讀結果 | 寫列 | 狀態 | 判決 |
|---|---|---|---|
| `matched`，而且台帳還沒有這個 ref | `uploaded`（`source=vcp`、`confirmed=true`、`platform_ref`），跟 VCP-037 對上時同一個樣子 | WARN | VERDICT `confirmed=true platform_ref=<ref> readback=matched exit_code=N detail=<CLI 錯誤>`。人類訊息：CLI 失敗，但平台列出了這一發，已經記下，不要再傳。 |
| `not_listed` | 無 | FAIL | `upload_failed:`：約 17 秒內平台沒有列出以這個 id 開頭的提交，可以重傳。下一次 upload 會先讀列表，它若晚一點才出現，會被 `already_uploaded:` 擋下；用 `--no-sync` 重傳前先看平台。VERDICT `exit_code=`、`readback=not_listed`。 |
| `ambiguous`、`failed`、`interrupted`，或只對上已知的 ref | 無 | FAIL | `upload_unconfirmed:`：vcp 判斷不出平台有沒有收下。重傳前先到平台找這個 id、時間在送出時間附近的提交；有的話，`vcp submit sync` 會把它記下。VERDICT `exit_code=`、`readback=<結果>`。 |

- 訊息保留 `kaggle CLI failed (exit N)`，以及 redact 後的 CLI 最後一行。
- 台帳格式不變。不加新欄位，免得 0.12 讀不動 `ledger: shared` 的共用正本。

### 6.3 列表呼叫的逾時

- Kaggle 的列表呼叫（上傳前同步、`sync`、回讀）加 120 秒逾時。逾時時，上傳前同步與 `sync` → `sync_failed:`；回讀 → 結果 `failed`。
- 上傳本身不加逾時，大檔可能要很久。
- 原因：這些呼叫都在台帳鎖裡跑，卡住就會擋住所有 checkout 的寫入。

## 7. VERDICT 與判決字彙

| 命令 | 新增 |
|---|---|
| 所有 `vcp provenance` 命令 | `root=<configs root id>`，失敗時也帶 |
| provenance，root 不符 | FAIL `root_mismatch:`；`index_root=<id\|none>` |
| `provenance rebuild`（PostgreSQL 取代別的 root） | WARN `replaced_root=<id>` |
| `backup verify` | FAIL `manifest_incomplete`（reason 第一順位）；`incomplete=`、`local_copies=` |
| `backup status` | WARN；`incomplete=` |
| `backup push` | tier 3 FAIL `manifest_incomplete:`；`local_copies=` |
| `backup push --forget-remote` | `forget_refused:` 多了兩種情況：清單缺檔、副本不在目的地 |
| `backup manifest` | `local_copies=`；有缺口 ABORT（`InvariantError`） |
| `submit upload` | CLI 失敗但回讀對上 → WARN，帶 `exit_code=`；FAIL `upload_failed:` / `upload_unconfirmed:`，帶 `exit_code=`、`readback=` |

- 新判決字：`root_mismatch:`、`manifest_incomplete:`、`upload_unconfirmed:`。
- 既有字用在新情境：
  - `not_found:`：舊的 SQLite 索引；
  - `mismatch:`：schema 2 的索引；
  - `upload_failed:`：CLI 失敗，而且平台沒有列出；
  - `forget_refused:`：上表的兩種新情況；
  - `sync_failed:`：列表逾時。

## 8. 相容性與升級

- 0.13.0（MINOR）的理由：
  - 新判決字與 VERDICT 欄位。
  - 寫入內容：每個 root 一份、schema 3 的 SQLite 索引；PostgreSQL generation 的 root metadata；`backup.log.jsonl` 的 `incomplete` / `local_copies`；清單優先選 rclone 的上傳。
  - 行為：tier 3 push 會送本機副本、會拒絕缺檔的清單；`--forget-remote` 多拒絕兩種情況；upload 在 CLI 失敗時回讀。
- 升級步驟（寫進 CHANGELOG）：
  1. 每個 checkout 跑一次 `vcp provenance rebuild`，SQLite 會建新檔。用 PostgreSQL 的，先讓每個 checkout 各用自己的 service／database，再 rebuild。
  2. 不再有 0.12 的使用者之後，刪掉 `indexes/provenance.sqlite3`。
  3. 0.10.0 以前、為「同檔名不同路徑」的多折 run 建的清單，現在會報 `manifest_incomplete:`。用新 id 重建清單，重推、重驗。
  4. 清單裡有本機副本，又要推到別的目的地時：用 0.13.0 跑一次 `backup push --tier 3` 與 `backup verify`。在那之前，`status` 不會說 verified。
- 0.12 讀不動帶 `incomplete` / `local_copies` 的 `backup.log.jsonl` 列（`extra_forbidden`），同一個 configs root 的寫入者要一起升級。
- 提交台帳（`submissions.jsonl`）的格式不變。

## 9. 測試

- **VCP-044：**
  - 同一個 data root、兩個 configs root：各自 rebuild 之後，兩邊的 `verify-index` 都 OK；`graph` 各自反映自己的台帳與備份清單。
  - 拿 A 的索引在 B 執行 `verify-index` / `status` / `sync` / `ingest` / `impact` / `stale` / `explain` / `graph` → `root_mismatch:`，不是 `prefix_drift:`，訊息裡沒有「became shorter」與「consumed prefix」。
  - 同一個 root 內真的截短或改寫台帳 → 仍是 `prefix_drift:`。
  - 另一個 checkout 經 `ledger: shared` 追加共用台帳 → `sync` OK。
  - 舊檔名不讀，訊息指出它；schema 2 → `mismatch:`；搬移 configs root 後要 rebuild。
  - PostgreSQL（用 `FakePostgres`）：
    - generation 記下 root；
    - 別的 root 執行 verify / ingest → `root_mismatch:`，在前綴檢查之前；
    - `sync` 不取代；`rebuild` 取代並 WARN；
    - 沒記 root 的舊 generation，rebuild 之前一律 FAIL；
    - data root 不符也 FAIL。
  - root id 跟鎖檔同一個規則；同一個目錄的不同寫法得到同一個 id。
- **VCP-045：**
  - 規則：
    - 用 0.9.1 的方式建的五折清單只列一折 → 缺 4 個；目前的程式建的清單完整；
    - 清單建立之後才登記的權重不算缺；resume 重複登記的路徑只要求一次；
    - 以 `train_dir` 列出的算數；外部路徑用建清單時的鍵；
    - 四種結論都會檢查。
  - verify：`reason=manifest_incomplete`、`incomplete=4`、FAIL；完整清單的 verify 列跟 0.12 逐位元相同。
  - status：舊的通過列不能背書（`verified=false`、`incomplete=1`）；沒有 train record 時的退路；0.10.0 以前的清單是 `unchecked`。
  - push：tier 3 在動任何檔案之前 FAIL；tier 1、2 照常；`--forget-remote` 拒絕。
  - manifest：自檢抓到建清單的 bug → ABORT。
  - 端到端：重現（舊清單報缺檔）→ 用新 id 重建 → push、verify OK。
- **VCP-046：**
  - `covers` 的四種組合。
  - tier 3 push 到 rclone：
    - 從原檔送出本機副本，`local_copies=1`，權重在 remote 上；
    - 原檔刪了，就從本機副本送；
    - 兩者都不符 → 動任何檔案之前 FAIL；
    - tier 2 不送本機副本；
    - rclone 上的副本照舊就地驗、不推。
  - verify：push 之前 `missing`，push 之後 ok；`present=false` 的本機副本不在目的地 → `missing`。
  - status：0.13 以前的列不算數；0.13 push 加 verify 之後 verified。
  - `--forget-remote`：權重到目的地之前拒絕。
  - pull：先從目的地拉，沒有再退回本機副本。
  - manifest：優先選 rclone 的上傳。
  - 本機目的地：副本在目的地裡 → 就地驗；不在 → 送進去。
  - 端到端：`train upload --dest <本機>` → 建清單 → tier 3 push 到 fake rclone，權重到位 → verify，`status` 是 verified → forget 成功。
- **VCP-047：**
  - CLI exit 1、列表有以這個 id 開頭的提交 → 寫列（帶 ref）、WARN、配額只算一次。
  - 列表沒有 → FAIL `upload_failed:`，不寫列，台帳位元組不變。
  - ambiguous / 列表失敗 / 中斷 / 只對上已知的 ref → FAIL `upload_unconfirmed:`，不寫列。
  - `--no-sync` 也回讀；`--force` 重傳時，排除已知的 ref 後對上新的那一發。
  - 列表逾時 → 上傳前同步 `sync_failed:`；回讀的結果是 `failed`。
  - CLI 的錯誤文字在台帳、`logs/`、VERDICT 裡都經過 redact。
- 回歸 gate：新增一列，釘住以上的驗收測試。

## 10. 文件同步

1. **稽核文件**：
   - 新增第三輪回報一節（VCP-044～047），沿用 §16 的格式、泛化描述，標記隨 0.13.0 處理。
   - 更新過時的狀態：VCP-001、002（只限 `samples.jsonl`）、003、005、007、008、009 的 `狀態:` 行；§1 表的「目前狀態」欄；VCP-034（視覺指南已在 main）；檔頭說明（Wave 0、1a、1b 已完成）。
2. **1.0 門檻統一成一種說法**（CHANGELOG 第 3、9 行、兩份 README、CLAUDE.md／AGENTS.md、`vcp-release-and-environments` skill、CODEX_PROMPT）：「`1.0.0` 留給稽核 Wave 1 全部落地之後，包括 1c（程式碼快照與產物授權，VCP-004／006）。稽核 §11 Wave 1 的第 4 項（單一大陣列的選取列存取器）與第 5 項（合成插件端到端、比賽原型遷移）排在 1.0 之後。」
3. **兩份 README**：拿掉「格式已穩定到可以在上面蓋東西」，改成：1.0 之前，MINOR 版可能改寫入格式或 CLI 契約，每次改了什麼寫在 CHANGELOG；用到新功能之後，舊版 vcp 可能讀不動新紀錄。
4. **交接文件**：
   - HANDOVER：標題日期；§6 拿掉已完成的項目；§6-5、§6-7 寫明說的是哪個工作區；`vcp submit` 的命令數；程式碼地圖；真實資料測試的數字。
   - CODEX_PROMPT：版本與測試數字；優先順序改成 0.13.0 → Wave 1c → 凍結前的契約項 → 1.0。
   - `DATASET_EVOLUTION_PROVENANCE_HANDOFF.md`：貼上用的 prompt 說「尚未合併」，標成歷史。
5. **對齊 CLAUDE.md 與 AGENTS.md**：目前有 6 處不同。
6. **orientation 地圖**：版本歷史補到 0.13.0，更新 build string 的範例。
7. **2026-10 的歷史改寫**（使用者 2026-10-04 決定加對照表）：
   - GitHub 上的歷史在 0.12.0 之後改寫過：拿掉了 307 個 `Co-Authored-By` trailer，481 個 commit 的檔案樹、作者與時間都沒變，但 hash 全部改了。改寫前寫下的 build string（`<version>+g<舊 hash>`）因此在 GitHub 上找不到。
   - 新增 `docs/reference/commit-map-2026-10.tsv`：每一列是舊 hash、新 hash、日期、標題。以（檔案樹、作者時間、作者 email、標題）一對一配對，481 對全部配上。
   - CHANGELOG 的版本規則補一句：改寫前的 build string 用這份對照表解析；`vcp version` 與產物格式都不變。
   - HANDOVER 的陷阱一節補一句：本機的 `refs/archive/pre-rewrite-2026-10/` 保留了舊歷史，釘在舊 commit 上的 worktree 照常能用。

## 11. 隨各缺陷更新的文件與 skill

- **spec**：
  - dataset-evolution provenance §6，加一條補充決定；
  - PostgreSQL provenance §7、§15、§20；
  - 備份 §2、§3、§5、§6、§6.1、§6.2、§8、§14（新的補充決定取代 2026-09-07 本機副本的註記）；
  - 提交治理 §10.1、§10.2、§12，§17 新增第 31 條；
  - 共用台帳 §4.2、§4.3、§8 的交叉引用。
- **指南**：`POSTGRESQL_PROVENANCE.md`（一個 checkout 一個 database）、`DATASET_EVOLUTION_PROVENANCE.md`（索引路徑）。
- **`docs/reference/cli.md`**：provenance 一節、備份的命令表、`submit upload` 那一列。
- **skill**（改完鏡射到 `.agents/skills/`）：
  - `vcp-provenance`；
  - `vcp-provenance-graph`：讀索引路徑的 snippet 改成從 `vcp provenance status --json` 讀；
  - `vcp-orientation/map.md`；
  - `vcp-release-and-environments`：一個 worktree 一份索引、一個 PostgreSQL service；
  - `vcp-train-submit-backup`；
  - `vcp-running-contests` 的 `workflow.md`、`operator-guide.md`。
- **CLAUDE.md 與 AGENTS.md**：
  - 備份那一行：本機副本跟著 tier 3 走、清單的完整性；
  - provenance 與 PostgreSQL 那幾行：每個 root 一份索引、一個 database 只服務一個 checkout；
  - 提交治理那一行：CLI 失敗時回讀。
