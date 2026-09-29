# vcp 提交台帳的共用正本、加鎖與上傳前同步（VCP-038 第 2–4 段 + VCP-014）設計

- 日期：2026-09-28
- 來源：
  - RSNA 第二輪回報的 VCP-038。第 1 段已隨 0.10.0 修好（#28）。
  - VCP-014：同一個 id 重傳沒有護欄。
  - 0.10.0 審查記下的問題：`sync` 不冪等。
  - 處置表在稽核文件第 16 節。
- 使用者的決定（2026-09-28）：
  1. **拓樸**：同一台機器共用一份台帳正本，寫入時加鎖。跨機器不在這次範圍。
  2. **位置**：`submit.yaml` 加 `ledger: configs | shared`，預設不變。`shared` 會把正本放到 data root。另給一個明確的遷移命令。
  3. **上傳前同步**：先把平台列表同步進台帳，再算配額。讀不到平台就 FAIL，明寫 `--no-sync` 才略過。
  4. **一併處理**：VCP-014 的重傳護欄，以及 PENDING 綁定。
  5. **方案**：每個寫入命令整段是一個交易，用作業系統的檔案鎖保護。
- 版本：0.12.0（MINOR）。
- 執行期修訂（最終審查，2026-09-28）：§3.1、§4.1、§4.2、§4.4、§5、§6、§7、§8。共用正本只由 adopt 建立，adopt 一定收本 checkout 的台帳；另補描述配對的順序、同一個 ref 只算一次、保留的 id、鎖不可重入，以及上傳 FAIL 時的同步欄位。

## 1. 問題

- **台帳各自為政**：每個 checkout 各有一份 `configs/datasets/<test>/submissions.jsonl`，跟著 git 走。同一台機器上的多個 worktree 或 agent 輪流提交時，彼此看不到對方的上傳，於是配額會少算，同一個 id 也可能被重傳。RSNA 目前靠人工約定「只准一個 worktree 寫」。
- **寫入沒有鎖**：「讀台帳 → 檢查配額或 lock → 上傳 → 寫一列」之間有競態。即使在同一個 worktree，兩個程序也可能同時通過配額檢查。
- **上傳前不看平台**：平台上有、台帳卻沒有的提交（網頁上傳、別的 worktree、別台機器），要等下一次 `sync` 才會算進配額。
- **PENDING 綁不上**：`sync` 用 ref、描述或「檔名＋時間」把平台上的一發對上某個 id 時，如果台帳沒有那一發的上傳，就什麼都不寫，不管它有沒有分數。那一發從此不算配額，也擋不住重傳。
- **VCP-014**：同一個 id 可以無聲地重傳。
- **`sync` 不冪等**：同一個 id 有幾發分數不同時，每次 `sync` 都重寫一輪 `scored`。

## 2. 範圍

做：

- `submit.yaml` 的 `ledger` 欄位，以及台帳位置的解析（§3.1）。
- 遷移命令 `vcp submit ledger adopt`（§4.1）。
- 台帳鎖，以及寫入命令的交易（§4.2）。
- `upload` 的上傳前同步（§4.3）、綁定列（§4.4）、重傳護欄（§4.5）。
- `sync` 依 ref 冪等，「最新分數」改依平台時間（§4.6）。

不做：

- **跨機器**：各機一份台帳，再用 git 的 `merge=union` 合併。讀取時的去重與排序、`backup verify` 的單調檢查都不改。
- **提交檔**：`submit/<test>/<id>/` 本來就在 data root，不動。
- **其他台帳**：例如 `unseal.jsonl`、`prereg.log.jsonl`。
- **Manual 平台的列表讀取**：它沒有 API 可讀。

## 3. 資料模型

### 3.1 台帳位置

- `submit.yaml` 新增欄位 `ledger: configs | shared`，預設 `configs`。
  - `configs`：`<configs_root>/datasets/<test>/submissions.jsonl`。這是現況，台帳跟著 git 走。
  - `shared`：`<data_root>/submit/<test>/submissions.jsonl`，跟這場比賽的提交檔放在同一個目錄。用同一個 data root 的所有 worktree 都共用這一份。
- 台帳的名字不能當提交 id：`submissions.jsonl`（不分大小寫），以及以 `submissions.jsonl.` 開頭的名字，`stage` 都 FAIL `invalid:`。
- 由一個解析函式決定台帳位置，所有讀寫台帳的地方都經過它：
  - 寫入命令：`stage`、`upload`、`record`、`score`、`sync`、`final`、`lock` / `unlock`；
  - 唯讀命令：`status`、`report`；
  - 其他：備份的走訪與驗證、provenance 的掃描。
- 備份的 `submissions_log` 角色跟著解析結果走。`shared` 時台帳落在 data 根，備份到 `<dest>/data/...`。

### 3.2 台帳列

- 不新增事件種類。
- `uploaded` 的 `source` 多一個值 `platform`，代表同步時對上了、但台帳裡沒有對應上傳的那一發（§4.4）。這一列帶：
  - 平台的 `at` 與 `platform_ref`；
  - `confirmed=true`；
  - `profile_sha256`，取同步當下的 profile。
- `uploaded` 可以帶 `reason`，記錄 `--force` 重傳的理由（§4.5）。

### 3.3 鎖檔

- 路徑：`<data_root>/locks/submissions-<sha256(台帳的絕對路徑) 前 16 位>.lock`。
- 放在 data root、不放在台帳旁邊：`configs` 模式的台帳在 git 工作樹裡，鎖檔不該出現在 `git status`。
- 內容是持有者的 pid、主機名、命令與取得時間（UTC stamp），只給診斷用。
- 不進備份：備份的走訪只收已知角色。

## 4. 行為

### 4.1 `vcp submit ledger adopt --dataset T [--from PATH]...`

- 只在 `ledger: shared` 時可用，否則 FAIL `not_shared:`。
- 共用正本只由 adopt 建立。正本已經存在 → FAIL `exists:`。
- 來源：本 checkout 的 `configs` 台帳一定算（是檔案時），再加上每個 `--from`，依給的順序。
  - 同一個檔只讀一次，路徑寫法不同也一樣（比 `normcase(resolve())`）；
  - `--from` 指到正本本身 → 還沒拿鎖就 FAIL `invalid:`；
  - `--from` 不是檔案 → FAIL `not_found:`；本 checkout 沒有 `configs` 台帳，就只是少一個來源；
  - 每個來源都必須是解析得動的台帳檔。
- 合併規則：
  - 所有來源的列一起依 `ts` 排序；`ts` 相同時照來源順序（本 checkout 的在前），再照來源裡的順序；
  - 內容完全相同的列只留一份，這些是不同 worktree 共有的同一段歷史；
  - 同一個 id 若有內容不同的 `staged` 列 → FAIL `ledger_conflict:`，列出衝突的 id，一列都不寫。
- 正本在鎖內一次寫成：先寫 `.tmp` 再改名。沒有來源、或來源都沒有列，就寫一份空的正本。合併結果的 `ts` 單調遞增，所以 `backup verify` 照常通過。
- 來源檔不動。vcp 之後不再讀 `configs` 那一份，要不要從 git 刪掉由人決定。
- VERDICT：`rows=`（正本的列數）、`sources=`（讀了幾份來源）、`duplicates=`（去掉的重複列數）。沒有來源時是 `rows=0 sources=0 duplicates=0`。
- `ledger: shared` 時，正本還不存在 → 所有 submit 命令 FAIL `not_adopted:`（`status`、`report` 與備份的 `submission:` 結論也是），並提示先跑 adopt；不管本 checkout 的 `configs` 台帳有沒有列，也不建正本。一個 checkout 看不到別的 worktree 的舊台帳：誰先寫就先建正本的話，adopt 只剩 `exists:`，別人的歷史再也收不進來。
- 切換的順序：
  1. `ledger: shared` commit 進 git，每個 worktree 都拉下來。從這時起，每個 submit 命令都停在 `not_adopted:`。
  2. 在任一個 checkout 跑一次 `vcp submit ledger adopt`，用 `--from` 指其他 worktree 的 `configs` 台帳；本 checkout 自己的一定會收。
  3. 沒有舊台帳的新比賽也要跑一次 adopt：它建一份空的正本。

### 4.2 鎖與交易

- **鎖的實作**：新模組 `vcp.core.lock`，只用標準函式庫。Windows 用 `msvcrt.locking`，其他平台用 `fcntl.flock`。程序一結束，作業系統就會釋放鎖。
- **等待**：最多 60 秒，每 0.5 秒重試一次。等不到 → ABORT `locked: <台帳> held by <command> (pid <n> on <host> since <stamp>)`。
- **不可重入**：同一個程序再要一把它已經拿著的鎖，等下去也只是等自己，所以立即 ABORT `locked: <台帳> is already held by this process (<command>)`，不等。
- **交易範圍**：寫入命令整段都在鎖內，拿到鎖之後先重讀台帳。這些命令是 `stage`、`upload`、`record`、`score`、`sync`、`final`、`lock` / `unlock`、`ledger adopt`。其中 `upload` 的交易包含上傳前同步、配額、重傳護欄、平台上傳，以及寫入 `uploaded` 列。
- **唯讀命令不上鎖**：`status`、`report`、備份、provenance。讀到最後一列沒有換行（代表正在寫），就當作那一列還沒寫入。
- `configs` 與 `shared` 兩種位置都加鎖。

### 4.3 上傳前同步

- `upload` 在交易內、檢查配額之前，先做一次跟 `vcp submit sync` 一樣的同步。寫入的列和規則都相同，包括 §4.4 與 §4.6。
- 平台列表讀不到 → FAIL `sync_failed: <原因>`，不上傳。
- 加 `--no-sync` 就略過同步，改為 WARN（VERDICT `sync=skipped`）。
- Manual 平台本來就不能用 `upload`。`record` 不做同步，因為 Manual 平台沒有列表可讀。
- VERDICT：`sync=ok|skipped`，並帶這次同步寫入的 `bound=`（§4.4）。

### 4.4 綁定

- 同步用 ref、描述或「檔名＋時間」把平台上的一發對上某個 id 時，如果台帳沒有對應的上傳，就寫一列 `uploaded`（`source=platform`）。不管有沒有分數都寫，PENDING 的也寫。
- 「沒有對應的上傳」的意思是同時滿足：
  - 沒有帶同一個 `platform_ref` 的 `uploaded` 列；
  - 這個 id 在平台時間前後 10 分鐘內，也沒有不帶 ref 的 `uploaded` 列。
- 這一列會算進配額，也讓 §4.5 的護欄看得到。原本寫 `scored` 的規則不變：有分數時照樣寫。
- 描述的配對先找以 id 開頭的描述（vcp 寫的描述一律是 `<id> <message>`），沒有才找描述裡提到的 id。ref 仍最先，「檔名＋時間」仍最後。所以 `S2 same as S1` 是 S2 的那一發，不會綁成 S1 多一次上傳。
- 同一個 `platform_ref` 的 `uploaded` 列只算一次到達，算台帳順序的第一列：adopt 可能把一邊的 `record --platform-ref` 和另一邊的綁定列合在一起。`record --platform-ref` 遇到某個 `uploaded` 列已經帶著的 ref → FAIL `exists:`，一列都不寫。
- `vcp submit sync` 的 VERDICT 多一個欄位 `bound=`。

### 4.5 重傳護欄（VCP-014）

- `upload` 時，這個 id 已經有任何 `uploaded` 列 → FAIL `already_uploaded: <id> was uploaded <n> time(s), last at <at>`。
  - `vcp`、`manual`、`platform` 三種來源都算。
  - 上傳前同步剛綁上的那一列也算。
- `--force "<理由>"`：照樣上傳，理由寫進新 `uploaded` 列的 `reason`，VERDICT 帶 `forced=true`。理由不能是空字串。
- `record` 補記的是已經發生的上傳，所以不擋，只 WARN（`already_uploaded=<n>`）。

### 4.6 sync 冪等與最新分數

- 對上 id 的平台提交，只在兩種情況寫 `scored`：
  - 這個 `platform_ref` 還沒有 `scored` 列；
  - 它的分數或狀態，跟「同一個 ref 最新的那列 `scored`」不同。

  這樣同一個 id 即使有幾發分數不同，重跑 `sync` 也不會重寫。
- 「最新分數」（`latest_score`）改成依平台時間 `at` 排序，取最後一發；沒有 `at` 的列排在最前面。不再依檔案順序。

## 5. VERDICT

| 命令 | 新欄位 |
|---|---|
| 寫入命令與 `status` | 成功（OK / WARN）時帶 `ledger=configs\|shared`；失敗時只有 `not_adopted:`、`not_shared:` 帶 `ledger=` |
| `upload` | `sync=ok\|skipped`、`bound=`；用 `--force` 時 `forced=true`；FAIL `already_uploaded:` 時帶 `uploads=<n>`；上傳前同步之後才 FAIL（配額、重傳護欄、平台錯誤）也帶 `sync=`、`bound=`，因為同步寫進台帳的列留著 |
| `sync` | `bound=` |
| `record` | id 已上傳過時 WARN，帶 `already_uploaded=<n>` |
| `ledger adopt` | `rows=`、`sources=`、`duplicates=` |

## 6. 錯誤與判決字彙

| 情況 | 例外 | 狀態 |
|---|---|---|
| 60 秒內等不到台帳鎖 | `VcpError` `locked:` | ABORT |
| 同一個程序再要它已經拿著的台帳鎖 | `VcpError` `locked:`（立即，不等） | ABORT |
| 上傳前同步讀不到平台 | `ValidationFailed` `sync_failed:` | FAIL |
| id 已上傳過，又沒給 `--force` | `ValidationFailed` `already_uploaded:`（訊息說怎麼加 `--force`） | FAIL |
| `--force` 的理由是空字串 | `ValidationFailed` `invalid:`（既有） | FAIL |
| `record --platform-ref` 的 ref 已經在某個 `uploaded` 列 | `ValidationFailed` `exists:`（既有） | FAIL |
| `stage` 的 id 是台帳的名字 | `ValidationFailed` `invalid:`（既有） | FAIL |
| 設了 `shared`，但正本還不存在（沒跑過 adopt） | `ValidationFailed` `not_adopted:` | FAIL |
| 跑 `ledger adopt`，但 `ledger` 是 `configs` | `ValidationFailed` `not_shared:` | FAIL |
| adopt 的 `--from` 是正本本身 | `ValidationFailed` `invalid:`（既有） | FAIL |
| adopt 時正本已經存在 | `ValidationFailed` `exists:`（既有） | FAIL |
| adopt 時同一個 id 的 `staged` 列不同 | `ValidationFailed` `ledger_conflict:` | FAIL |
| 加了 `--no-sync` | — | WARN |
| `record` 補記已上傳過的 id | — | WARN |

## 7. 相容性與版本

- 0.12.0（MINOR）。這次的變動有：
  - `submit.yaml` 新欄位、新命令；
  - 新選項 `--no-sync`、`--force`；
  - 新 VERDICT 欄位與 `reason=` 字；
  - `uploaded` 的新來源 `platform`，以及 `reason` 欄位。
- 舊 vcp 讀得動新台帳，因為用到的欄位都是既有的。但舊 vcp 不會加鎖。
- 舊 vcp 讀到帶 `ledger:` 的 `submit.yaml` 會 FAIL（`extra_forbidden`）。所以切到 `shared` 之前，所有寫入者都要先升到 0.12.0；這個 FAIL 也正好擋住還沒升級的 worktree。
- 切到 `shared` 一律要跑一次 adopt，沒有舊台帳的新比賽也一樣（§4.1 的切換順序）；在那之前每個 submit 命令都停在 `not_adopted:`。
- `configs` 模式除了新增的加鎖、上傳前同步、綁定與護欄，其他行為不變，既有台帳不必遷移。

## 8. 測試

- **鎖**：
  - 子程序持有鎖時，命令會等待，逾時後 ABORT `locked:`（測試時把等待上限調小）；
  - 持有鎖的子程序被結束後，鎖拿得到；
  - 鎖檔在 data root，不在台帳旁邊；
  - 同一個程序再要同一把鎖（路徑寫法不同也一樣）→ 立即 `locked:`，不等。
- **交易**：拿到鎖之後，看得到別的程序剛寫入的列。
- **上傳前同步**：用假的 Kaggle runner 測。
  - 平台上多出來的提交先寫進台帳，再算配額；
  - 讀不到平台 → FAIL `sync_failed:`，不上傳；
  - `--no-sync` → WARN；
  - 同步之後才 FAIL（配額、重傳護欄、平台錯誤）→ VERDICT 帶 `sync=`、`bound=`。
- **綁定**：
  - PENDING 或已計分、但台帳沒有上傳的那一發 → 寫 `uploaded(source=platform)`；
  - 10 分鐘內已有不帶 ref 的上傳 → 不重複寫；
  - 配額只算一次；
  - `S2 same as S1` 配給 S2，不綁成 S1 的上傳；
  - 同一個 ref 的兩列 `uploaded` 只算一次到達；綁上之後 `record --platform-ref` 同一個 ref → `exists:`，台帳不變。
- **重傳護欄**：
  - 同一個 id 再上傳 → FAIL `already_uploaded:`，訊息說怎麼加 `--force`；
  - `--force "理由"` → `reason` 有寫入；空理由 → FAIL；
  - `record` 只 WARN；
  - `final` 要求重傳時印出命令；沒 `--force` → `already_uploaded:`，`--force "final re-send"` 過得了封槍與護欄。
- **sync 冪等**：同一個 id 有兩發分數不同時，第二次 `sync` 不寫任何列；`latest_score` 依平台時間。
- **adopt**：
  - 合併兩份台帳並去重；
  - `ledger_conflict:`、`exists:`、`not_shared:`；
  - 沒 adopt 時，不管 `configs` 台帳有沒有列，每個 submit 命令都 `not_adopted:`，也不建正本；
  - 沒有來源 → 空的正本（`rows=0 sources=0`），之後 `stage` 照常；
  - 本 checkout 的台帳一定收；同一個檔只讀一次、不等鎖；`--from` 是正本本身 → `invalid:`；
  - 合併後 `ts` 單調，`backup verify` 通過。
- **位置**：`shared` 路徑的解析；備份的走訪與驗證、provenance 都跟著解析結果；`stage` 不收台帳的名字當 id。
- **升級**：索引在 `configs` 時建好，切到 `shared` 並 adopt 之後，`provenance sync` 加上正本的檢查點、不報漂移；備份的 `submission:` 結論在 adopt 之前 `not_adopted:`。
- **端到端**：兩個 configs 根共用一個 data root，設了 `ledger: shared`。adopt 之前另一邊的 `status` 是 `not_adopted:`；一邊 adopt、`stage` + `upload`，另一邊的 `status` 看得到；另一邊對同一個 id 再 `upload` → `already_uploaded:`。

## 9. 文件與 skill

- 提交治理 spec（`2026-09-05-vcp-submission-governance-design.md`）加一條修訂，指回本 spec。
- `docs/reference/cli.md`：
  - `submit upload`、`record`、`sync`、`status` 的列；
  - 新命令 `submit ledger adopt`。
- `CLAUDE.md` 與 `AGENTS.md` 的「提交治理」一段：
  - `shared` 模式的位置；
  - 鎖；
  - adopt。
- skill `vcp-train-submit-backup` 的提交部分，並鏡射到 `.agents/skills/`。
- 稽核文件第 16 節：VCP-038 與 VCP-014 標為已實作（隨 0.12.0）。
