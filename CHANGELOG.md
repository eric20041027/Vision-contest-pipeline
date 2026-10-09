# Changelog

vcp 的每個 release 一條，最新在最上面。格式依 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，版本號依 [SemVer](https://semver.org/lang/zh-TW/)，停在 `0.x` 直到稽核的 Wave 1 全部落地（見「版本規則」）。

## 版本規則

- **MINOR（`0.N.0`）**：任何會改變**寫進產物或台帳的內容**（run.yaml、train.yaml、fuse.json、stage.json、各 `*.jsonl` 台帳、backup manifest 的欄位或語意），或改變 **CLI 契約**（命令與選項、VERDICT 欄位、exit code、`reason=` 字彙、登記表項目）的變更。讀舊產物的人靠這個數字判斷「欄位的意思有沒有變」。
- **PATCH（`0.N.P`）**：不改寫入位元組、不改契約的修正——bug、訊息措辭、效能、測試、文件、內部重構。
- **`1.0.0`**：留給 2026-09-11 稽核的 Wave 1（1a、1b、1c）全部落地之後，包括 1c（程式碼快照與產物授權，VCP-004／006）——屆時產物契約才是可以對外承諾的契約。稽核 §11 的 Wave 1 清單裡，第 4 項（單一大陣列的選取列存取器）與第 5 項（合成插件端到端、比賽原型遷移）排在 1.0 之後。
- **產物記的 `vcp_version` 是 build string**，不只是版本號：`<version>`（已發版的 wheel，或 git 未追蹤的副本）、`<version>+g<40 hex commit>`（從 checkout 執行）、`<version>+g<commit>.dirty`（該 checkout 的 repo 有未提交變更——刻意過度回報，因為 commit 已不能完整描述跑過的程式碼）。`vcp version` 印同一字串；`vcp.core.build.parse_build_string` 解析它。
- **2026-10 的歷史改寫**：GitHub 上的歷史在 0.12.0 之後改寫過一次，拿掉了 307 個 `Co-Authored-By` trailer；481 個 commit 的檔案樹、作者與時間都沒變，但 hash 全部改了。改寫前寫下的 build string（`<version>+g<舊 hash>`），以及本檔與其他文件引用的舊 hash，用 `docs/reference/commit-map-2026-10.tsv`（舊 hash、新 hash、日期、標題）對到新 hash；表只含 main 的歷史，從沒進 main 的分支 commit 不在裡面。`vcp version` 與產物格式都不變。
- **發版步驟**（一個 commit 一個 tag）：
  1. 改 `src/vcp/__init__.py` 的 `__version__`——它是唯一來源，`pyproject.toml` 以 `[tool.hatch.version]` 動態讀它，`uv.lock` 不記版本字面值。同一個 commit 把 Claude Code plugin 的 `.claude/.claude-plugin/plugin.json` 的 `version` 改成同一個號碼（`tests/unit/test_skills_plugin.py` 會擋住不一致），別的專案才會收到新版 skill。
  2. 在本檔最上方加一條 `## [x.y.z] - YYYY-MM-DD`，列 Added / Changed / Fixed / Removed 與影響的層。
  3. `uv sync --reinstall-package vcp`（editable 安裝的 metadata 不會因 `__init__.py` 改動自動重建），再 `uv run pytest --cov=vcp`（`tests/unit/test_package.py` 會擋住 `__version__`、安裝 metadata 與本檔最新條目三者不一致）與 `uv run ruff check . && uv run ruff format --check .`。
  4. commit（`chore(release): vx.y.z`）、開 PR、CI 綠了合併（`main` 有 branch protection 而且是 strict，不能直接推）、在**合併 commit** 上 `git tag -a vx.y.z <合併 commit> -m "vcp x.y.z"`、`git push origin vx.y.z`。
- 產物不可改寫（專案鐵則）：舊版本寫下的 `vcp_version` 永遠留著，本檔是它們的解析路徑。

## [0.14.0] - 2026-10-09

VCP-048：平台做完卻沒有分數的提交（#37）；tag `v0.14.0` 打在發版 PR 的合併 commit 上。
MINOR 的理由：
- 台帳新事件 `errored`：必填 `submission_id`、`source`、`at`、`platform_ref`、`platform_status`，
  帶 `public` 或 `private` 是 schema 錯誤（`errored needs no score`）。由 `sync` 與 `upload` 的上傳前
  同步寫入，一律 `source=platform`。0.13.0 以前的 vcp 讀不動它（見「相容性與升級」）。
- VERDICT 新欄位：
  - `submit sync` 的 `errored=`（一律印）：這次寫入的 `errored` 列數；大於 0 時 WARN，所以新的出錯
    只在寫入的那一次提醒。
  - `submit status` 的 `errored=`（一律印）：最新一發出錯的 id 數。出錯是已結束的狀態，從不因此 WARN。
- `submit final` 的決選表（`final` 列的 `table`）多一個 `why` 值 `errored`。它不在 `NOT_RANKED`，
  所以算進 `unranked=`、列在沒排名的清單裡（WARN）。
- `--json` 的新內容：`sync` 的 `result.errored`（這次寫入的列的 id，照寫入順序）、`status` 的
  `result.errored`（id 清單）、`report` 每列的 `errored`（foreign 一律 `false`）。
- 人看的行：`sync` 每寫一列印 `errored: <id> (the platform finished it without a score)`；
  `status` 印 `errored: <id>`；`report` 出錯那一發的行尾加 ` errored`。
- 行為：`sync` 的冪等改跟同一個 ref 最新的結果列比（見 Changed）。

### Fixed
- **VCP-048：平台做完卻沒有分數的提交。** Kaggle code submission 的隱藏重跑出錯時，CLI 2.2.4 把它
  列成 `SubmissionStatus.COMPLETE`、兩個分數都是空字串，JSON 沒有錯誤訊息。`sync` 只看分數，那一發
  在台帳裡跟「還在跑」一樣：`status` 永遠算它 unscored 而 WARN，`report` 分不出出錯與還沒出分，
  `final` 可能選中一個在平台上沒有結果的 id。現在：
  - 平台契約多一個判斷 `PlatformSubmission.errored`。Kaggle 的規則
    （`vcp.submit.platforms.kaggle.is_errored`）：狀態以 `.` 分割的最後一段轉小寫是 `error`，或是
    `complete` 而 public 與 private 都空；帶分數的一律不算。
  - `sync`（含上傳前同步）對上 id 之後照舊先綁定，出錯的一發寫一列 `errored`、不寫 `scored`。之後
    平台替同一發補上分數，照樣寫 `scored`，以較新的那列為準。
  - 每一發的結果由這個 id 的 `scored` 與 `errored` 列一起、照 `assign_scores` 的規則歸屬：`scored`
    是已出分，`errored` 是出錯，沒有是還沒出分。
  - `status` 把最新一發出錯的 id 列在 `errored=`，不算 unscored。
  - `report` 出錯那一發沒有 public／private，下一發的 delta 跟接在還沒出分的一發後面一樣是空的。
  - `final` 依序判斷 `probe`、`not_uploaded`、`errored`，然後才是 sealed 讀數與來源檢查；出錯的
    不讀 provenance 與 sealed 讀數。重傳之後出分就照常排名。
  - `arrivals()` 判斷 foreign ref 是不是自己的上傳時，`errored` 列跟 `scored` 列一樣能把 ref 綁到
    id。
- Release 回歸門檻多一列 `VCP-048 (0.14.0)`（Kaggle 的判斷、schema、sync 的寫入與來回、arrivals、
  status、final），刪掉或改名任一個測試即紅。

### Changed
- `submit sync` 的冪等改跟同一個 (id, ref) 最新的結果列（`scored` 或 `errored`，台帳順序）比：是
  同一種、分數與狀態也相同才不寫。狀態沒變時重跑照樣不寫；在有分數與沒分數之間來回時（分數 → 空 →
  同一個分數，或空 → 分數 → 空）每一步都寫，結果跟平台最後的狀態一致。沒有 `errored` 列的台帳，
  最新的結果列就是最新的 `scored` 列，行為跟 0.13.0 相同。
- `final` 什麼都排不了時，`no_sealed_readings:` 的訊息在有出錯的候選時說出幾發出錯（列出 id）：
  最新一發沒有分數，到平台看、再傳一次；還有其他沒排上的條目時才接著保留解封 sealed 的提示。字彙
  照舊，VERDICT 不加欄位。
- 文件：`docs/reference/cli.md` 的 `submit upload`、`sync`、`final`、`status` / `report` 與台帳
  位置一段；`CLAUDE.md` / `AGENTS.md` 的事件清單；skill `vcp-train-submit-backup` 與鏡射；提交治理
  spec 第 32 條；稽核文件第 18 節；spec `2026-10-09-vcp-errored-submissions-design.md` 與後記
  `2026-10-09-vcp-048-errored-submissions-followups.md`。
- Python API（自己登記平台、或在程式裡呼叫這些函式的人要看）：
  - `PlatformSubmission` 多一個欄位 `errored: bool = False`（放在最後、有預設值，既有的建構呼叫
    不必改）。用 `register_platform` 登記的平台要自己判斷哪一發算出錯，不設就永遠不寫 `errored` 列。
  - `SyncResult.errored_ids`（這次寫入的 `errored` 列的 id，照寫入順序）與唯讀 property `errored`
    （它的長度）。
  - `StatusView.errored`（必填，只有 `status()` 建它）；`ReportRow.errored`（預設 `False`，放在
    最後，位置參數的建構照舊）。
  - `SubmissionLedger.outcomes`、`assigned_outcomes`、`latest_outcome`、`outcome_for_ref`。
    `score_for_ref` 保留，但 vcp 自己不再用它。
  - `assign_scores` 搬到 `vcp.submit.ledger`（`final` 也要用，放在 `report` 會循環 import）；
    `vcp.submit.report` 照樣匯出它，舊的 import 不用改。它的 `scores` 也可以帶 `errored` 列。

### 相容性與升級
- 0.13.0 以前的 vcp 讀到 `errored` 列會 FAIL `bad ledger row`：每個讀者都嚴格驗每一列。所以一份
  台帳的每個讀者與寫者都先升到 0.14.0，才讓 0.14.0 對它跑 `sync` 或 `upload`：
  - `configs` 模式的台帳跟著 git 走：共用它的每個 checkout；
  - `shared` 模式的正本：同一個 data root 的每個 worktree。
- 不用遷移：升級後第一次 `sync` 替平台上已經出錯的那幾發各補寫一列 `errored`，WARN 一次
  （`errored=`），之後 `status` 不再因為它們 WARN。先跑的若是 `upload`，上傳前同步照樣補寫，但它的
  VERDICT 不報。
- 沒有 `errored` 列的台帳，讀寫行為跟 0.13.0 相同；`submit.yaml` 與其他產物的格式不變。
- `final` 可能從 OK 變成 WARN：候選或 baseline 的最新一發出錯時，它算進 `unranked=`、列在沒排名的
  清單裡。同一份台帳在 0.13.0 看不出出錯，可能是 OK。
- 已知限制（後記 §1；開放待辦在後記 §2.2）：
  - 沒有錯誤訊息的文字：CLI 2.2.4 的 JSON 沒有，vcp 不碰憑證、也不改用 Python API；錯在哪要到
    平台的提交頁看。
  - 沒有公開排行榜的比賽，每一發 COMPLETE 都沒有分數，會被判成出錯；`final` 因此一發都排不了，只會
    FAIL `no_sealed_readings:`（0.13.0 照 sealed 讀數排名）。目前沒有這種比賽。
  - foreign 列不變：它本來就存 `platform_status`，出錯的 foreign 一發只多一筆快照，不寫 `errored`
    列。
  - Manual 平台沒有列表：出錯的一發在 `status` 裡一直是 `unscored`，需要時用 `note` 事件手記。
  - 平台若把出錯的一發排回 PENDING，pending 什麼都不寫，結果仍是出錯，直到它出分或再出錯（Kaggle
    目前不會這樣）。
  - `upload` 的 VERDICT 不報上傳前同步寫的 `errored` 列，之後的 `status` / `report` 才看得到。
  - `status` 只看每個 id 最新的一發：較早的一發出錯、最新的已出分時不列它（`report` 那一行仍標
    ` errored`）。
  - `final` 也只看最新的一發（刻意偏保守）：較早的一發有分數、最新重傳的那一發出錯時，這個 id 是
    `why=errored`、不排名；再傳一次、出分之後恢復排名。
  - 狀態字在解析時就經過 redact：32 個以上連續英數字的狀態會變成 `<redacted>`，認不出出錯。實際的
    狀態字遠短於此。

## [0.13.0] - 2026-10-05

第三輪回報的 VCP-044～047 與文件同步（#35）；tag `v0.13.0` 打在發版 PR 的合併 commit 上。
MINOR 的理由：
- `reason=` 新字：`root_mismatch:`、`manifest_incomplete:`、`upload_unconfirmed:`。既有字用在新情境：
  `not_found:`（0.12 的 SQLite 索引）、`mismatch:`（schema 2 的索引）、`upload_failed:`（CLI 失敗，
  而且平台沒有列出）、`forget_refused:`（清單缺檔、本機副本不在目的地）、`sync_failed:`（列表逾時）。
- VERDICT 新欄位：
  - 每個 `vcp provenance` 命令的 `root=`（失敗時也帶）；root 不符時的 `index_root=`；PostgreSQL
    `rebuild` 取代別的 root 時的 `replaced_root=`。
  - `backup verify` 的 `incomplete=`（一律印）與 `local_copies=`（給了 `--dest` 時）；
    `backup status` 的 `incomplete=`；`backup push` 與 `backup manifest` 的 `local_copies=`。
  - `submit upload` 的 `exit_code=`（FAIL 時加 `readback=`）。
- `--json` 的新內容：
  - `provenance rebuild` 與 `sync` 的 `result.replaced_roots`：只有 PostgreSQL `rebuild` 取代了別的
    checkout 的 generation 時有值（那個 generation 記的 root；它沒記 root 時是 `{}`），其他情況是 `null`。
  - `provenance status` 的 `result.index`（這個 checkout 的索引在哪裡），以及索引記的四個 root 鍵
    `configs_root_id`、`configs_root`、`data_root_id`、`data_root`。
- 寫入內容：
  - SQLite 索引每個 configs root 一份：`indexes/provenance-<configs root id>.sqlite3`，schema 3，
    記著它服務的 configs root 與 data root。
  - PostgreSQL generation 的 metadata 多四個 root 鍵（DDL 不變，`POSTGRES_SCHEMA_VERSION` 仍是 1）。
  - `backup.log.jsonl` 的 `incomplete` / `local_copies`（大於 0 才寫）。
  - 同一個路徑有多個驗過的上傳時，清單優先記 rclone 的那份。
- 行為：tier 3 push 會送本機副本、會拒絕缺檔的清單；`--forget-remote` 多拒絕兩種情況；
  `submit upload` 在 kaggle CLI 失敗時回讀。

### Fixed
- **VCP-044：provenance 索引的 root 身分。** 每個 configs root 一份 SQLite 索引；兩種後端都記下索引的
  configs root 與 data root，root 不符時在任何 replay 或前綴檢查之前 FAIL `root_mismatch:`
  （`index_root=`），不再把另一個 checkout 分岔的台帳報成 `prefix_drift:`。root id 跟鎖檔同一個規則
  （`vcp.core.paths.path_id`）。PostgreSQL 一個 database 只服務一個 checkout：`sync` 不取代別的
  root 的 generation，`rebuild` 取代時 WARN `replaced_root=`；沒記 root 的舊 generation 在 rebuild
  之前一律 FAIL。`impact`、`stale`、`explain`、`graph` 也解析 configs root；解析不了時，每個
  `vcp provenance` 命令照樣以 VERDICT 收尾。
- **VCP-045：備份清單的完整性。** run 紀錄在清單建立前登記的 checkpoint 與證據都要在清單裡（任何角色
  都算）。缺了 → `backup verify` FAIL `manifest_incomplete`（`incomplete=`）；`backup status` 每份
  清單都重算，WARN 且不算 verified；tier 3 push 在動任何檔案之前 FAIL；`--forget-remote` 拒絕；
  `backup manifest` 寫出前自檢，有缺口 ABORT。
  - `backup status` 因此會讀清單點名的 run 紀錄。讀不了的那一份只讓它所在的清單變 `unchecked`
    （備註寫出那個檔與原因），其他清單照常列出，`backup verify` 照樣 FAIL。
  - `backup manifest --conclusion all` 跳過的 run 不留半份：走訪失敗時，那次走訪加進清單的項目全部
    收回；點名它的判決、提交與融合 run 也一起跳過（跟 run 被刪掉時一樣），都列在 `skipped=`。
- **VCP-046：本機的 `remote_copy` 跟著目的地走。** 不在 rclone、也不在目的地裡的副本，跟著
  `--tier 3` 推到目的地（從原檔送，原檔不在或不符就從本機副本送）、在目的地驗；0.13 以前的推送與
  驗證列不替它背書；`--forget-remote` 在它到目的地之前拒絕；`backup pull` 先從目的地拉，沒有再退回
  本機副本。
- **VCP-047：Kaggle CLI 失敗時回讀。** CLI 非 0 退出時在同一個上傳交易裡回讀（排除台帳已知的 ref，
  exit 0 的回讀也排除）：平台列出了 → 照寫一列、WARN `exit_code=`；沒列出 → FAIL `upload_failed:`；
  判斷不了 → FAIL `upload_unconfirmed:`。兩種 FAIL 都不寫列。Kaggle 的列表呼叫（上傳前同步、`sync`、
  回讀）各加 120 秒逾時；上傳本身不加。
  - exit 0、CLI 沒印出 ref 時，回讀若只看到台帳已知的 ref，會等滿整個回讀窗（約 17 秒），照舊記成
    未確認（`confirmed: false`）的上傳。
  - 逾時結束整棵程序樹（`uv tool` 裝的 kaggle 是啟動器加一個 python 孫程序）：Windows 用
    `taskkill /T /F`，不開新的程序群組，Ctrl+C 照樣傳得到 CLI；POSIX 讓 CLI 在新的 session 裡跑、
    用 `killpg` 結束；結束樹之後 CLI 還在就 `proc.kill()`。

### Changed
- 文件同步：稽核文件的狀態與第 17 節；1.0 門檻統一成一種說法；README 的穩定度說法；交接文件；
  `CLAUDE.md` 與 `AGENTS.md` 逐位元相同；orientation 地圖的版本歷史。新增
  `docs/reference/commit-map-2026-10.tsv`（2026-10 歷史改寫前後的 commit 對照，見「版本規則」）。
- 「版本規則」的發版步驟第 4 步照實際做法改寫：`main` 受保護，發版 commit 走 PR，tag 打在合併
  commit 上。
- Python API（自己登記平台、或在程式裡呼叫這些函式的人要跟著改）：
  - `vcp.provenance.backend.make_backend(config, data_root, configs_root)` 與
    `vcp.core.paths.provenance_index_path(data_root, configs_root)` 多了 `configs_root`；
    新增 `vcp.core.paths.path_id`。
  - `Platform.upload(...)` 多了關鍵字參數 `known_refs=`（台帳已有的 ref，回讀時略過）；用
    `register_platform` 登記的平台要接受它。

### 相容性與升級
- 升級步驟：
  1. 每個 checkout 跑一次 `vcp provenance rebuild`，SQLite 會建新檔。用 PostgreSQL 的，先讓每個
     checkout 各用自己的 service／database，再 rebuild。
  2. 不再有 0.12 的使用者之後，刪掉 `indexes/provenance.sqlite3`。
  3. 0.10.0 以前、為「同檔名不同路徑」的多折 run 建的清單，現在會報 `manifest_incomplete:`。用新 id
     重建清單，重推、重驗。
  4. 清單裡有本機副本，又要推到別的目的地時：用 0.13.0 跑一次 `backup push --tier 3` 與
     `backup verify`。在那之前，`status` 不會說 verified。
- 0.12 讀不動帶 `incomplete` / `local_copies` 的 `backup.log.jsonl` 列（`extra_forbidden`），同一個
  configs root 的寫入者要一起升級。
- 索引跟著 checkout 走：搬移或改名 checkout 之後要再 rebuild 一次。刪掉 worktree 不會刪它在
  `indexes/` 的 `provenance-<configs root id>.sqlite3`；索引是衍生品，不用的可以直接刪。
- 提交台帳（`submissions.jsonl`）的格式不變。

## [0.12.0] - 2026-09-29

RSNA Knee 第二輪回報的 VCP-038 第 2–4 段與 VCP-014（#33）；tag `v0.12.0` 打在發版 PR 的合併
commit 上。MINOR 的理由：
- `submit.yaml` 新欄位 `ledger: configs | shared`（`configs` 不寫出）。
- 新命令 `vcp submit ledger adopt`。
- 新選項：`submit upload --no-sync / --force "<理由>"`。
- VERDICT 新欄位：`ledger=`（寫入命令與 `status`）、`sync=`、`bound=`、`forced=`、`uploads=`、
  `already_uploaded=`、`rows=`、`sources=`、`duplicates=`。
- `reason=` 新字：`sync_failed:`、`already_uploaded:`、`not_adopted:`、`not_shared:`、
  `ledger_conflict:`。`locked:` 也用在等不到台帳鎖、以及同一個程序重入同一把鎖的 ABORT 上；既有的
  `invalid:`、`exists:`、`not_found:` 用在新情境。
- 台帳寫入內容的改變：
  - `uploaded` 的新來源 `source=platform`（同步綁上的平台發）與 `reason` 欄位（`--force` 的理由）；
  - `scored` 依 ref 冪等，`sync` 不再每次重寫。

### Added
- `ledger: shared`：台帳正本放在 `<data_root>/submit/<test>/submissions.jsonl`，同一個 data root 的
  worktree 共用一份。
  - 正本只由 `vcp submit ledger adopt [--from PATH]…` 建立。本 checkout 的 configs 台帳一定收，
    `--from` 再加；依 `ts` 合併一次，相同的列只留一份，同 id 的 `staged` 不同 → `ledger_conflict:`。
  - 沒有舊台帳也要跑一次，它建立空的正本。還沒 adopt 前，每個 submit 命令（含 `status`、`report`）
    都 FAIL `not_adopted:`。
  - `--from` 指到正本自己 → `invalid:`；同一個來源換個寫法再給一次，只讀一次。
- 台帳鎖 `vcp.core.lock`：`<data_root>/locks/submissions-<hash16>.lock`，Windows 用
  `msvcrt.locking`、其他平台用 `fcntl.flock`。
  - 寫入命令整段持有，拿到鎖才重讀台帳。
  - 等 60 秒拿不到 → ABORT `locked:`，訊息寫著持有者的命令、pid、主機與時間。
  - 同一個程序重入同一把鎖 → 立刻 `locked:`，不等。
- `submit upload` 先把平台列表同步進台帳再算配額（讀不到 → `sync_failed:`；`--no-sync` 略過、
  WARN）。同步之後才 FAIL 時，VERDICT 帶 `sync=` / `bound=`，同步寫的列留在台帳。
- 重傳護欄（VCP-014）：同一個 id 已上傳過 → FAIL `already_uploaded:`，訊息說明要加
  `--force "<理由>"` 才照傳。`final` 需要重傳時印出完整命令。
- `submit sync` 把對上 id 卻沒有對應上傳的平台發（PENDING 也算）寫成 `uploaded(source=platform)`，
  算配額也擋重傳；VERDICT `bound=`。

### Changed
- `submit sync`：
  - 只在 ref 還沒有 `scored` 列、或分數或狀態變了才寫；「最新分數」依平台時間 `at`。
  - 用描述配對時，先找以 id 開頭的描述，沒有才找提到 id 的（`S2 same as S1` 是 S2 的）。
  - 檔名＋時間的配對只看 vcp 與手動的上傳。
- `submit record`：
  - 補記已上傳過的 id → WARN `already_uploaded=<n>`（仍寫列）。
  - `--platform-ref` 已經有 `uploaded` 列 → FAIL `exists:`，不寫。配額裡同一個 ref 的上傳只算一次。
- `submit stage` 不收 `submissions.jsonl` 這個 id（`invalid:`）。
- `status`、`report`、備份不上鎖，略過還沒寫完的最後一列；備份的走訪與驗證、provenance 的檢查點
  跟著台帳位置走。

### 相容性
- 新 vcp 照讀舊台帳與舊 `submit.yaml`。`configs` 模式除了加鎖、上傳前同步、綁定與護欄，其他行為
  不變，既有台帳不必遷移。
- 舊 vcp 讀得動新台帳（用到的欄位都是既有的），但不會加鎖；讀到帶 `ledger:` 的 `submit.yaml` 會
  FAIL `extra_forbidden`。
- 切到 `shared` 的程序：
  1. 每個寫入者都先升到 0.12.0。
  2. 每個 worktree 都 commit 並拉到 `ledger: shared`；之後所有 submit 命令都停在 `not_adopted:`。
  3. 在任一個 checkout 跑一次 `vcp submit ledger adopt --dataset T --from <其他 worktree 的 configs
     台帳>…`；本 checkout 的一定會收進來。

## [0.11.0] - 2026-09-28

RSNA Knee 第二輪回報的 VCP-040 + 042（#30）與 VCP-041（#31）；tag `v0.11.0` 打在發版 PR 的合併
commit 上。MINOR 的理由：
- 新命令 `vcp data labels`。
- 新選項：`train run --evidence / --labels / --require-clean`、`eval ingest --evidence / --labels`。
- VERDICT 新欄位：`evidence=`、`labels=`、`evidence_changed=`、`commit=`、`modified=`、`untracked=`、
  `git_changed=`。
- `reason=` 新字：`labels_outside_subsets:`、`labels_on_sealed:`、`labels_mismatch:`、
  `evidence_conflict:`、`role_reserved:`、`dirty_tree:`。既有的 `invalid:` 也用在標籤列格式與
  `--evidence` 參數上。
- 新產物種類：`label_set`、`evidence`。
- 寫入內容的改變：
  - `run.yaml` / `train.yaml` 的 `evidence` 清單；
  - `train.log.jsonl` 的 `evidence` 事件，以及 `note` 的 `evidence_changed` / `git_changed`；
  - `env.<n>.json` 的 `git` 新欄位與 `train/git.<n>.patch`；
  - 備份新角色 `label_set` / `evidence`。

### Added
- `vcp data labels`（VCP-042，#30）：訓練標籤檔（`.csv` / `.jsonl`，一個 id 一列）對切分 plan 驗過後，
  存成不可變的 `label_set/<id>`。
  - 落在允許子集以外的 dataset 樣本 → FAIL `labels_outside_subsets:`。只報計數與前 5 個 sample id，
    不報標籤內容。
  - sealed 子集不能標（`labels_on_sealed:`）。
  - 不在 dataset 裡的列記 `external=`。
  - 一列都沒對到 → WARN。
- run 讀的證據檔（VCP-040，#30）：
  - 證據檔複製成不可變的 `evidence/<run>-<name>-<sha12>`。
  - `run.yaml` / `train.yaml` 多一張 `evidence` 參照清單，空的時候不寫出。
  - 附上的入口有三個：
    - `train run --evidence NAME=PATH --labels ID`：預檢（含名稱衝突與保留檔名）都在第一次寫入前；
      結束時原檔變了 → WARN `evidence_changed=`。
    - `Session.attach_evidence / attach_labels`。
    - `eval ingest --evidence / --labels`。
- 下游跟著認得證據與標籤集：
  - `train status`：`evidence=` / `labels=`，`--verify` 多出 `evidence:<名稱>` drift。
  - `eval status`：每個 run 的 `labels`。
  - 備份：新角色 `label_set`（tier 1）與 `evidence`（tier 2，含歷史列）。`backup verify` 比對 run
    釘住的 `manifest_sha256`。
  - provenance 圖：「產物 → run」的 `CONSUMED_BY` 邊；參照壞掉時 run 就是 broken。
- `train run --require-clean`（VCP-041，#31）：
  - `--cwd` 所在 repo 的追蹤檔有改動時，在第一次寫入前 FAIL `dirty_tree:`，帶 `modified=` 與 `commit=`。
  - 不在 repo 裡或 git 不能用 → `not_found:`，附 git 的原因。
- `vcp.train.gitstate`：vcp 對訓練 repo 的 git 呼叫都在這裡。
  - 固定前綴、`core.quotepath`、submodule 格式與結尾的 `--`。
  - 不繼承 `GIT_DIR` 等決定 repo 的環境變數；環境變數給的設定照樣有效。
  - 設 `GIT_OPTIONAL_LOCKS=0`。

### Changed
- `train run` 的工作樹紀錄（VCP-041，#31）：
  - 追蹤檔有未提交改動 → WARN（`modified=`），diff 存成 `train/git.<n>.patch`（10 MiB 以內），在該
    commit 上 `git apply` 就能還原。
  - `env.<n>.json` 的 `git` 多記改動與未追蹤的數量和路徑，以及 status 與 diff 的 sha256。
  - 結束時 HEAD 或追蹤檔的 diff 變了 → `note` 事件 `git_changed`，並 WARN。
  - 未追蹤檔（含 submodule 裡的）不算改動。
  - VERDICT 多 `commit=` / `modified=` / `untracked=`。
- `register_checkpoint` 只給權重；run 讀的其他檔改用 `attach_evidence`（VCP-040）。
- provenance 圖：`evidence` 產物只經由 run 自己的清單連到 run。沒被參照的副本（例如 ingest 失敗留下的）
  不再畫成 consumed。

### 相容性
- 新 vcp 照讀舊紀錄。
- 舊 vcp 讀不了這兩種新東西：
  - 帶 `evidence` 的 `run.yaml` / `train.yaml`，會 FAIL `extra_forbidden`；
  - 帶新角色的備份清單。
- 訓練 venv 要跟著升級。若它裝的是舊版、非 editable 的 vcp，wrapper 附上 `--evidence` / `--labels` 後，
  `Session.register_checkpoint` 就會失敗。比賽工作區的兩個 venv 應以 editable 指向新 tag 的 worktree。
- `GitInfo.dirty` 的意思不變，只有 submodule 裡的未追蹤檔不再算。
- `env.<n>.json` 的新欄位都有預設值，而且 vcp 本身不讀回它。

## [0.10.0] - 2026-09-25

RSNA Knee 第二輪回報（VCP-035 … VCP-043）裡可以先修的五件，各一個 PR（#24–#28）；tag `v0.10.0`
打在發版 PR 的合併 commit 上。MINOR 的理由：`submit upload` 的 VERDICT 多了 `platform_ref=` /
`readback=` / `detail=`；`reason=` 字彙多了 `weights_not_in_candidate:`、`upload_failed:`；
`uploaded` 列可以帶 `platform_ref`，`sync` 對新的 (id, ref) 即使分數沒變也寫 `scored` 列；backup
manifest 對多折 run 多出條目；`train upload` 的遠端名稱對同名 checkpoint 帶上層資料夾；訓練子程序
多了環境變數 `VCP_ATTEMPT`。

### Added
- `Session.attempt`（公開唯讀）與子程序環境變數 `VCP_ATTEMPT`：`--resume` 後每個 attempt 可以各寫
  自己的證據檔，不必呼叫私有的 `_attempt()`（保留為別名）；`note`、`register_checkpoint` 與收據 id
  `<run>-a<n>-<seq>` 都讀同一個數字（VCP-043，#26）。
- `vcp submit upload` 的 VERDICT：`platform_ref=`（有才帶）、`readback=matched|not_listed|ambiguous|
  known_ref|failed|interrupted`（有回讀才帶）、`detail=`（平台回覆，redact 後截 160 字）；VERDICT
  進 `logs/`，平台說了什麼從此查得到（VCP-037，#27）。
- `vcp.submit.kernel`（kernel 提交可以載入哪些權重）與 `vcp.submit.matching`（`mentions` / `leads`：
  vcp 怎麼從平台列表認出自己的上傳）。

### Changed
- **kernel candidate 的 `--weights` 必須是被判決的那組**（VCP-036，#25，使用者選的嚴格規則）：每個
  權重 run 必須是 eval run 或它遞迴展開的融合成員，否則 FAIL `weights_not_in_candidate`；candidate 的
  每個權重 run 也過 `trained_on_sealed` / `observed_sealed` / `provenance_required`；baseline 只受成員
  資格約束；probe 豁免但每項發現記進 `pairing.checks`（`weights:<run>=<finding>`）。
  `Staged.provenance` 與 `submit final` 對 kernel 提交取所有相關 run 最低的等級。
- `train upload`：run 內同名、不同路徑的 checkpoint（多折的 `fold-k/model.pt`）不再 FAIL
  `name_collision`，遠端名稱取能分開它們的最少上層資料夾以 `__` 串接（`fold-0__model.pt`）；檔名不
  重複的 run 名稱不變，舊的純檔名副本照舊算數（VCP-039，#24）。
- `sync`：配到的平台發若這個 id 還沒有帶它 ref 的 `scored` 列，分數沒變也寫一列——同檔重傳分數必然
  相同，以前那一發永遠沒有自己的分數、ref 也綁不上 id（VCP-038，#28）。
- `submit status` 的 `foreign=` 改成數到達裡的 foreign（不含其實是自己上傳的那些）。

### Fixed
- backup manifest 與 `backup verify` 的一致性層以檔名去重：一個 run 登記 `fold-0/model.pt` …
  `fold-4/model.pt` 時只留最後一個，其餘靜默消失、VERDICT 仍 OK。現在以路徑為身分（VCP-035，#24）。
- Kaggle kernel 上傳永遠 `WARN confirmed=false`：CLI 2.2.4 對 code submission 只印伺服器的 message，
  沒有成功字樣也不印 ref。現在 CLI 印了 `Submission ref:` 就採用，否則回讀提交列表：description 以
  這個 id 開頭、平台時間落在「CLI 開始前 2 分鐘到回來後 2 分鐘」的恰好一筆 → 確認並記 ref；回讀永不讓
  上傳失敗（VCP-037，#27）。
- Kaggle CLI 2.2.4 在檔案上傳送出前失敗時仍回 0、只說 `Could not submit to competition`：以前寫下一筆
  `uploaded` 列（WARN），現在是 FAIL `upload_failed:`、不寫列（#27）。
- Kaggle CLI 2.2.4 對還沒有任何 submission 的比賽印純文字 `No submissions found`：`sync` 以前 FAIL
  `platform_response: not JSON`，現在當空列表（#27）。
- 同一發被 `uploaded` 與 `foreign` 各算一次配額：台帳還不認得某一發時 `sync` 會記成 foreign，之後
  id 認領同一個 ref 也不會抵銷。`arrivals()` 現在排除其實是自己上傳的 foreign ref——`uploaded` 自帶
  那個 ref，或 `scored` 把它綁到某個 id、且與那個 id 的一發上傳相差不到 10 分鐘（最近的先配、一發只
  吸收一個）。配額、榜面現任、`final` 的 `needs_reupload` 與 `report` 一起修正（VCP-038 第 1 段，#28）。

### Upgrade notes
- 既有台帳升級後第一次 `sync`，會替過去「同分重傳」的每一發補一列 `scored`，`scored=` 一次性變大；
  之後照常冪等。
- 多折 run 之後的 `train upload` 會用新名稱上傳其餘幾折；已在遠端的純檔名副本留在原處、照舊算數。

### Not in this release
- VCP-038 第 2–4 段（上傳前先讀平台、台帳位置可設定、多寫入者拓樸與 `merge=union`）、VCP-040 + 042、
  VCP-041（稽核 Wave 1c）：先寫 spec。
- 已知的既有問題：同一個 id 的幾發分數不同時，`sync` 每次都會重寫一輪 `scored` 列（與 id 最新一列
  比較）；修它要一併重想 `latest_score` 的語意。同 id 重傳要 `--force` 的護欄（呼應 VCP-014）仍待辦。

## [0.9.1] - 2026-09-24

vcp 的 skill 變成可在任何專案使用的 Claude Code plugin，並帶上 0.9.0 之後合併的 SQLite gaps 修正（PR #21）；
tag `v0.9.1` 打在 PR 的合併 commit 上。PATCH 的理由：CLI 命令、VERDICT 欄位、exit code、`reason=`
字彙與產物 / 台帳內容都沒變；改的是衍生索引的 metadata 與打包。

### Added
- Claude Code plugin `vcp`：`.claude/` 是 plugin 根（`.claude/.claude-plugin/plugin.json`，skill 就是現有的
  `.claude/skills/`，不複製），repo 根的 `.claude-plugin/marketplace.json` 是 marketplace
  `vision-contest-pipeline`。在別的專案以 `/vcp:<skill>` 呼叫（例如 `/vcp:vcp-orientation`）。安裝：
  `claude plugin marketplace add <本機 checkout 或 eric20041027/Vision-contest-pipeline>`，再
  `claude plugin install vcp@vision-contest-pipeline`；本機 marketplace 原地載入，`git pull` 後下一個
  session 就是新版 skill。
- `tests/unit/test_skills_plugin.py`：plugin 版本 = `__version__`、marketplace 指向的根確實有 skill、每個
  skill 的 `name` 等於目錄名且 description 是觸發句、`.agents/skills` 與 `.claude/skills` 逐位元組相同
  （以前只靠人記得 `cp -r`）。

### Fixed
- SQLite provenance 索引把 canonical graph 的 gaps 存進 metadata，`verify-index` 能逐項比對（PR #21）。
  **升級注意**：0.9.0 以前建的 SQLite 索引在 0.9.1 讀取時會 FAIL
  `mismatch: graph_gaps metadata; rebuild required`，每個 data root 要 `vcp provenance rebuild` 一次
  （RSNA 規模約 3 分鐘）；重建後的索引舊版 vcp 仍讀得動。PostgreSQL 後端不受影響。

## [0.9.0] - 2026-09-23

把 provenance 索引畫成圖；tag `v0.9.0` 打在 PR 的合併 commit 上。MINOR 的理由：新增 CLI 命令
`vcp provenance graph`，連同它的 VERDICT 欄位與 `reason=` 字彙（`unsupported_format:`、
`out_in_data_root:`、`out_not_absolute:`、`exists:`、`not_a_file:`、`unsupported_detail:`、
`scope_conflict:`）。沒有任何
產物或台帳欄位的語意改變，索引 schema 不變。

### Added
- `vcp provenance graph --out FILE [--dataset D | --entity type:id] [--head D] [--detail overview|full]`：
  把 provenance 索引（SQLite 或 `--backend postgresql`）畫成 Mermaid 圖，副檔名決定格式——`.html`
  （瀏覽器直接開；Mermaid 11.17.2 從 jsDelivr 載入、以 SRI 驗證）、`.md`（GitHub 會渲染）、`.mmd`。
  只讀索引、只寫 `--out`：`--out` 不能落在 data root，也只覆寫檔頭帶標記的舊圖。`overview` 把 reading
  摺進 run → judgement 的箭頭，收據 / export / 解碼快取 / source audit / diff / backup 變成 run 的
  provenance 等級、粗框與改版箭頭的標籤，比賽自訂的 artifact kind 每種併成一個帶計數的節點；節點取
  相對各現行 head 最差的狀態（或 `--head` 指定）。範圍內有 BROKEN / REVIEW、或 `.md` / `.mmd` 超過
  Mermaid 預設上限時 WARN；`--json` 的 result 是摺好的節點與邊。vcp 自己加在節點與邊上的文字一律
  ASCII；索引裡沒有任何 dataset 時，BROKEN 仍沿著邊傳給下游。
- `vcp.provenance.render`（範圍、摺疊、狀態合併，純函式）與 `vcp.provenance.render_mermaid`（Mermaid
  文字、三種檔案、輸出路徑護欄、原子換寫）。
- skill `vcp-provenance-graph`（`.claude/skills/`，鏡射 `.agents/skills/`）：找比賽的 data / configs
  root、確認索引、畫圖、交付、怎麼讀顏色與往下追；路由表、`docs/guides/AGENT_SKILLS.md` 與 README 的
  時機表各加一列。

### Fixed
- 所有命令：輸出行含終端機 code page 編不出的字元時（例如管線化的 Windows stdout 是 cp950、run id 是
  簡體字），改印 ASCII 跳脫，`--json` 改印 ASCII 跳脫的同一份 JSON；以前會在工作做完後丟出
  `UnicodeEncodeError`，VERDICT 印不出來（`vcp provenance stale` 也會）。

### Evidence
- RSNA Knee 的真實索引（217 個實體、347 條邊）以 overview 畫成 63 個節點、69 條邊，`.html` 在瀏覽器
  由 Mermaid 11.17.2 完整渲染（9 個 dataset 框、0 個渲染錯誤）；`.md` 約 10 KB，在 GitHub 的上限內。

## [0.8.1] - 2026-09-21

PostgreSQL adaptive provenance 的五份 live 證據落地後、RSNA 訓練開跑前的 PATCH release；tag `v0.8.1`
打在 PR 的合併 commit 上。PATCH 的理由：沒有任何產物／台帳欄位的語意或 CLI 契約改變（命令、VERDICT
欄位、exit code、`reason=` 字彙、登記項都不動）。

### Added
- `python -m vcp` 可用（`src/vcp/__main__.py`），與 `vcp` 入口同一個 CLI。
- 可執行的入門範例 `examples/quickstart.py`（CI 會跑）；MIT 授權與開源專案規格檔（CONTRIBUTING、
  CODE_OF_CONDUCT、SECURITY、issue / PR 模板、CI 在 ubuntu + windows）。

### Changed
- canonical graph replay 改為串流（`open_dataset_diff`：先整檔驗證、再逐筆重放，兩遍 SHA-256 互驗）；
  `load_dataset_diff` 的 API、錯誤型別與訊息不變，exact parity 不變。
- 正式 adaptive 矩陣的 entity scales 由 1K/10K/100K/1M 縮為 1K/10K/100K（`workloads.SCALES`；
  `production_benchmark.py` 另用 `PRODUCTION_SCALES` 保留 1M）；裁決在 Plan 12 後記 §1。
- README 改為開源門面（英文主檔 + `README.zh-TW.md`），完整命令表移到 `docs/reference/cli.md`。

### Fixed
- standalone access receipt id 的 nonce 由 16 bit 加寬到 64 bit：同一秒內建立多張收據時不再撞號；
  id 形狀 `<purpose>-<dataset>-<plan>-<stamp>-<nonce>` 不變，舊 id 仍可讀。
- `evaluate_adaptive.py` 的 strict 環境模型接受 `runtime_environment()` 新增的 `system_release` /
  `system_version`；契約測試直接以真的 `runtime_environment()` 輸出驗模型。
- CLI `--help` 測試在 `GITHUB_ACTIONS` 下強制關閉 typer 的色彩與樣式。

### Evidence（不改程式碼，記在 `docs/benchmarks/postgres-provenance-v1.md`）
- 正式 calibration v2（policy `postgres-adaptive-v1-9f4e58346529`）、six-method v1（648 列、parity
  648/648）、held-out v1（aggregate gate PASS 1.018 / 1.017；every-scenario diagnostic 在 1K FAIL）、
  real RSNA v1（18 列、parity 18/18）；acceptance 表自 2026-09-20 起沒有 Absent 格。十項總結在
  `postgres-provenance-report-v1.md`，課程簡報素材在 `postgres-provenance-course-brief-v1.md`。
- 已知限制（不是 0.8.1 修的）：`auto` 的信心帶是絕對毫秒，小圖與接近全量變更的 transition 會落入 FULL
  （操作指南建議這兩種情況直接 `--strategy incremental`）；1M 未量。

## [0.8.0] - 2026-09-13

PostgreSQL Adaptive Provenance；tag `v0.8.0` 打在 PR 的合併 commit 上。MINOR 的理由：八個
`vcp provenance` 命令新增 backend/service CLI 契約，`ingest` 新增 strategy/policy 契約，並新增
PostgreSQL schema、maintenance decision 與 immutable policy artifact 內容。

### Added
- optional `postgres` extra 與 lazy Psycopg 載入；base install 和未帶 `--backend` 的流程維持 SQLite。
- noncanonical PostgreSQL v1 normalized index，包含 transactional advisory lock、atomic generation
  publication、full/incremental/NO_OP maintenance、canonical parity verification 與安全去敏錯誤。
- deterministic `incremental|full|auto` selector、safe FULL fallback、immutable calibration policy artifact
  驗證與 decision telemetry。
- localhost-only PostgreSQL 17.11 Compose harness、opt-in integration cases（`-m postgres`，未配置 service
  即 skip）、six-method benchmark、calibration-only fitter 與 frozen held-out evaluator。

### Changed
- `vcp provenance rebuild|sync|ingest|impact|stale|explain|status|verify-index` 接受
  `--backend sqlite|postgresql` 與 `--pg-service`；`ingest` 另接受 `--strategy` 與 `--policy`。
- package version 升為 `0.8.0`；README、AGENTS、操作指南、benchmark evidence boundary 與
  handoff 同步更新。

### Evidence boundary
- 2026-09-14 起本機有原生 PostgreSQL 17.11（非 Docker）：live integration v3 PASS 51/51
  （`docs/benchmarks/postgres-provenance-integration-v3.json`）；未配置 service 時 integration cases 仍是
  skipped，不是 pass。
- 沒有 large-scale、calibration、held-out、policy ID/hash 或 RSNA six-method 結果：calibration v1 與 1M
  memory gate v2–v4 都被 RAM 護欄中止，證據與下一步在 `docs/benchmarks/postgres-provenance-v1.md`；不得從
  offline doubles、SQLite smoke 或方法文件推導效能結論。

## [0.7.0] - 2026-09-13

Dataset Evolution 與 Incremental Impact Provenance。MINOR 的理由：新增 `dataset_diff` canonical
artifact、`vcp data diff` 與 `vcp provenance` CLI 契約、status/reason 字彙及衍生 SQLite schema。

### Added
- `SampleChange` strict event、deterministic ID、ADDED/REMOVED/MODIFIED/no-op summary、通用
  field-domain/effect policy 與外部 plugin registry。
- verified immutable `dataset_diff` artifact；兩端 samples/source-audit identity pin、stable order、
  fallback provenance grade、commit-last manifest。
- canonical full-replay graph與 `VALID|STALE|REVIEW|BROKEN` impact semantics，涵蓋 dataset、changed
  sample version、split、export pin、materialized cache、run/access receipt/prediction、append-only
  judgement event、fusion、artifact supersession、submission、backup。
- disposable WAL SQLite index、transactional idempotent diff ingest、canonical append sync、dirty closure、
  prefix/manifest checkpoint、atomic verified rebuild與 full parity verifier。
- CLI `data diff`、`provenance rebuild|sync|ingest|impact|stale|explain|status|verify-index`。
- real metadata-copy validation、固定 seed 1k/10k/100k/1M algorithm microbenchmark，以及直接走
  production schema / `ProvenanceIndex.ingest_diff()` 的正式 benchmark runner/result。

### Changed
- package version升為 `0.7.0`；README、AGENTS與 handover 補上 storage authority、repair、benchmark。

## [0.6.0] - 2026-09-13

稽核 **Wave 1b-2**：來源稽核與選取列存取——VCP-002（大型來源每個 job 都整檔 hash，與 train-only 列存取衝突）。MINOR 的理由：新產物 kind `source_audit`、收據與 `run.yaml` / `train.yaml` 的 `AccessRef` 多 `identity` / `source_audit`、`import` / `validate` / `measure` / `export` / `stage` / `train run` 的新 VERDICT 欄位與 WARN 字彙 `source_audit=missing`、備份角色 `source_audit`。

### Added
- **`source_audit` 產物**（`vcp.data.source_audit`）：`vcp data import` / `validate` 結束時一趟讀 `samples.jsonl`，寫 `audit.json`（`samples_hash`、大小、行數）與 `index.jsonl`（每列 `sample_id` / offset / length / 該列 bytes 的 sha256），id `src-<dataset>-<samples_hash 前 16 碼>`，同內容 `store.reuse` 不重算；VERDICT `source_audit=` `source_audit_state=created|reused`。
- **存取器走稽核**：`DatasetAccess.open` 有稽核就不再掃 `samples.jsonl`，只 seek 授權的列；每列先比稽核的 sha 再比 `sample_id` 再解析（同長度篡改也會 `mismatch:`）；稽核壞掉 `mismatch:` FAIL；缺席退回整檔 hash。收據多 `identity: source_audit|full_hash`、`source_audit`、`source_audit_sha256`；`AccessRef` 多 `identity` / `source_audit`；走稽核時收據產物的 `inputs` 列稽核的 `manifest.json`。
- **消費者**：`vcp train run` 任一收據退回整檔 hash → WARN `source_audit=missing`；`vcp eval measure` / `vcp data export` 印 `identity=` 並在退回時 WARN `source_audit=missing`（VERDICT 欄位）；`vcp submit stage` 印 `identity=`。
- 備份證據圖收收據背後的稽核（角色 `source_audit`，tier 2）；回歸門檻多一列 `wave 1b-2 (VCP-002)`；真資料唯讀整合測試。

### Changed
- `_LINE` / `peek_sample_id` 移到 `vcp.data.source_audit`；`index_samples` 介面不變。
- README、AGENTS.md / CLAUDE.md、交接文件、RSNA RUNBOOK；spec §15 補充決定。

## [0.5.0] - 2026-09-12

稽核 **Wave 1b-1**：角色範圍存取與收據——VCP-001（`Dataset.load()` 無法證明 train-only 存取）、VCP-003（access flags 是自我宣告）。MINOR 的理由：新產物 kind `access_receipt`、`run.yaml` / `train.yaml` 的 `access`、三本台帳與 `stage.json` 的 `provenance`、`submit.yaml` 的 `require_provenance`、新 VERDICT 欄位與字彙（`denied:`、`contaminated:`、`observed_sealed:`、`provenance_required:`）、`MaterializedReader.dataset` 移除。

### Added
- **`DatasetAccess`**（`vcp.data.access`）：card-only 載入 + 按 plan 角色授權的列讀取；open 時整檔 hash 身分並只 peek 行首 `sample_id` 建索引，未授權的列永不解析；未授權存取 `AccessDeniedError`（`denied:`）並計數；sealed 沿用 unseal 留痕。關閉時（含例外）存取器把 `AccessReceipt` 寫成 `artifacts/access_receipt/<id>/receipt.json`（v0.4.0 的 `ArtifactWriter`），呼叫端只能加 `notes`。
- **收據綁定**：`Session.access()` / `MaterializedReader`（現在是 context manager）在 `vcp train run` 下把收據登記進 `train.yaml`（`access` 事件），`train run` 結束抄進 `run.yaml`；`vcp eval ingest --receipt` 掛外部收據。
- **provenance**（`vcp.measure.provenance`）：`receipt > export > declared` 讀取時算出；`Reading` / `Judgement` / `Staged` / `FinalEntry` 記等級；`vcp eval status` / `report`、`vcp submit status` 印它。
- **強制點**：`measure` 的乾淨基底 = `trained_on ∪ 收據觀測`（點名被讀過的子集 → `contaminated:`）；`judge` 候選或基準讀過主張子集 → `INVALID contaminated:<run>/<subset>`；`submit stage` / `final` 的 `observed_sealed:` 與 `submit.yaml` `require_provenance`（預設 `declared`）；`train run` WARN `observed_beyond_trained_on=` / `receipt_invalid=`。
- `vcp eval status` 多 `provenance_failed=`（算不出 provenance 的 run，WARN；run 仍計入 `runs=`）與每個 run 一行 `provenance=… observed=…`；`vcp submit status` 印每筆提交的 `provenance: <id>=<grade>`（缺 `stage.json` → `-`）。
- 備份證據圖收 run 的收據（角色 `access_receipt`）；`vcp data export` 每次留收據並記進 manifest；回歸門檻多一列；真資料唯讀整合測試。

### Changed
- `Dataset.load_card`、`append_unseal`；`assert_run_matches` 收 card；`train run` 父程序不再解析 `samples.jsonl`。
- `MaterializedReader`：`reader.dataset` 移除（改 `card` / `access` / `sample()`），在 `VCP_RUN_ID` 下必須給 `plan_id` / `subset`。
- 存取器字彙：`roles:`（角色沒對到子集）、`sealed:`（一次多於一個 sealed 子集）為 `ValidationFailed`；`mismatch:`（身分、覆蓋、列在 open 後被改寫）為 `IntegrityError`；`InvariantError` 不再用於資料層的輸入錯誤。
- README、AGENTS.md / CLAUDE.md、交接文件；spec §16 補充決定。

## [0.4.0] - 2026-09-12

稽核（2026-09-11）的 **Wave 1a**：不可變產物層——VCP-005（產物路徑可被覆寫，破壞不可變證據）與 VCP-007（ID、seed、輸出缺少共同 contract）。MINOR 的理由：新命令群 `vcp artifact`、新產物形態 `artifacts/<kind>/<id>/manifest.json` 與 `supersession.jsonl`、新 `reason=` 字彙（`partial:`、`unsafe_path:`、`reserved_name:`、`closed:`、`drift:`、`spec_mismatch:`）。

### Added
- **不可變產物**（`vcp.artifact`）：`ArtifactWriter.create(spec, data_root=…)` 以 `os.mkdir` 獨佔搶 `<data_root>/artifacts/<kind>/<id>/`，`spec.json` 記 open 時的宣告，每個檔經 `write_once`，`manifest.json` 最後寫 = commit 點（沒有它就不是產物）；例外離開留半途目錄與經 redact 的 `failure.json`；writer 不刪任何東西。`ArtifactSpec.id_pattern` 的具名群組必須等於同名欄位（RSNA「id 說 s42、CLI 收 seed 43」在 open 就擋）；`inputs` 在 open 雜湊、commit 重驗（`drift:`）。`store.reuse` 要完整 spec 逐欄相等（`notes` 除外）；`supersedes` 在 open 查舊產物存在且已 commit、commit 驗逐檔 sha 並記舊 manifest sha、之後 append `supersession.jsonl`；`lineage` / `head` 允許分叉但 WARN；`verify` 四項（mismatch / missing / extra / unlinked）；`relink` 補台帳缺列；`clean` 只移除超過寬限期的半途目錄與 `.tmp`。
- **`vcp artifact create|show|verify|lineage|status|relink|clean`**（`cmd=artifact.<name>`；失敗時保留 `kind=` / `id=`）。
- **`vcp.core.atomic`**：`write_once` / `write_once_text` / `write_once_stream`（同目錄 `.<name>.<nonce>.tmp` → fsync → 目標不存在才 `os.replace`；不是跨程序鎖）。`core/paths.py` 新增 `artifacts_root` / `artifact_dir`，`check_relative_path` 從備份層搬來；`core/config.py` 新增 `dump_yaml_text`。
- Release 回歸門檻多一列（`wave 1a (VCP-005, VCP-007)`）；真資料整合測試 `tests/integration/test_artifact_status.py`（唯讀）。
- `vcp artifact verify` 重讀 `supersedes_sha256` 所指的舊產物 `manifest.json`（不在 → `missing` 多 `<old>/manifest.json`，sha 不符 → `mismatch` 多 `<old>/manifest.json`），並比對台帳列的 `supersedes_id` / `supersedes_sha256` 是否與 manifest 相符（不符 → `mismatch` 多 `supersession.jsonl`）：沒有這一步，重寫根產物的 `manifest.json` 對根與接替者兩邊的 `verify` 都會回報乾淨（VCP-005 最終審查 Important #1）。
- `ArtifactWriter` 每次寫入 / `commit()` 前確認自己搶下的目錄還在（被 `vcp artifact clean` 移走 → `not_found: … removed while the job was open`）；例外離開時若目錄已不在就不再寫 `failure.json`（否則會把目錄復活成沒有 `spec.json` 的外來目錄，任何命令都不能再清或建）。`vcp artifact create` 在搶 id 前先驗每個 `--file` 的檔名（`unsafe_path:` / `reserved_name:`）與是否重複（`exists: --file … given twice`），避免留下卡住 id 的半途目錄（最終審查 Important #2 與 minor）。

### Changed
- vcp 自己的四個寫一次檔——split plan（`save_plan`）、預登記 yaml（`create_prereg`）、融合配方（`save_recipe`）、backup manifest（`write_manifest`）——改經 `write_once_text`；訊息、例外類型、`fields` 與寫出的位元組不變。
- README、AGENTS.md / CLAUDE.md、交接文件；spec §16 補充決定。

## [0.3.0] - 2026-09-11

稽核（2026-09-11，VCP-001..034）的 **Wave 0**：兩個已寫好但未進 main 的正確性修正、既有預登記的稽核、release 回歸門檻。MINOR 的理由：台帳的 `foreign` 列語意改變（同 ref 可多筆快照）、`sync` 的 VERDICT 多 `refreshed=`、預登記錯誤的 `reason=` 字彙與 `prereg=` 欄位改變。

### Fixed
- **預登記綁定第一筆 log 列**（measure，VCP-008，原 Codex commit d193113）：`load_prereg` 只信任 bytes 仍 hash 到 `prereg.log.jsonl` 該 id 第一筆列的 yaml——沒有列 → `ValidationFailed("not_found: … never registered")`，hash 變了 → `IntegrityError("mismatch: …")`，兩者 `fields={"prereg": id}`。登記後改 `t_min` / `min_bases` / claim 再借用早先時戳的「事後預登記」從此不可能。judge、提交層 gate、備份層 `judgement:` 走法都經它，所以一份被竄改的預登記讓三者一起 FAIL。既有 `configs/datasets/rsna-knee/prereg/` 三份 yaml 對第一筆 log 列 **3/3 相符**（不改歷史）。
- **foreign submission 的狀態刷新**（submit，VCP-009，原 Codex commit d881a1d）：同一 `platform_ref` 可有多筆 `foreign` 快照，`sync` 在狀態或分數改變時 append（PENDING → COMPLETE / ERROR、COMPLETE 分數修正），`arrivals()` 每個 ref 只取最新，quota 與 `status` 的 `foreign=` 每個 ref 算一次；同頁重跑零新列。原問題：PENDING 時記下 ref 後，同 ref 的 COMPLETE/分數因「ref 已知」被跳過，台帳永遠沒有分數。

### Added
- `SyncResult.refreshed` 與 `vcp submit sync` 的 VERDICT / `--json` `refreshed=`（已知 ref 的新快照數；不觸發 WARN）。
- **Release 回歸門檻** `tests/unit/test_regression_gate.py`：稽核 §11 點名的六個修正（8c6b5c4 台帳快照與路徑安全、883da62 備份端到端與隱私掃描、d9b2331 CLI 失敗身分、7c31c3d 遺失 fuse.json 的完整重建、12a9cd4 事件單一來源與上傳檢查順序、6a4cc58 重複表頭 / 巢狀配對 / 最新判決）加 Wave 0 兩項，各對應其測試函式；刪掉或改名任一個測試即紅。
- 狀態序列測試：PENDING → ERROR、COMPLETE 分數修正、平台無 `ref` 的列、同頁重複 / 同時間兩個 ref；端到端多一次「隊友的發轉 COMPLETE」。

### Changed
- 量測 spec §17、提交治理 spec §17-26、README `sync` 列。

## [0.2.0] - 2026-09-11

第一個有 tag 的版本。內容 = 六個子專案（資料、量測、融合、訓練、提交治理、備份審計）+ 各層後記的 hygiene 處置（Plan 6c、7c、Hygiene A/B/C、cli context、fuse 遺失紀錄）+ RSNA Knee 基準流程（`projects/rsna-knee/`）+ 操作與交接指南。

### Added
- **建置身分**（core）：`vcp.core.build`——`probe_build` / `build_info` / `build_string` / `parse_build_string` / `git_head`。只有 git 真的追蹤 `__init__.py` 的目錄才算 checkout；裝進別人 repo 內 venv 的 wheel 不會借用那個 repo 的 commit。
- **單一版本來源**：`pyproject.toml` 改 `dynamic = ["version"]`，以 `[tool.hatch.version]` 讀 `src/vcp/__init__.py`。
- 本檔與版本規則；`tests/unit/test_package.py` 的三方一致性測試。

### Changed
- **產物的 `vcp_version`**（fuse、submit、train、backup）：`fuse.json`、`stage.json`、訓練環境快照、backup manifest 從此記 build string 而非裸版本號。欄位型別不變，`0.1.0` 時期的產物照讀。
- **`vcp version`**：印 build string；VERDICT 帶 `version= build=` 與（從 checkout 執行時）`commit= dirty=`；`--json` 給 `version` / `build` / `commit` / `dirty`。
- 訓練層 `git_info()` 改為 `vcp.core.build.git_head` 的薄包裝，行為不變。

## [0.1.0] - 未發版

從第一個 commit（`4d6ed00`，2026-09-02）到 `03aecc3`（2026-09-07）共 240 個 commit 都宣告 `0.1.0`，沒有 tag，`__version__` 從未改過。這段期間寫出的產物——含 RSNA Knee 的 backup manifest、`stage.json`、`fuse.json` 與訓練環境快照——裡的 `"vcp_version": "0.1.0"` **不能回推到單一 commit**。要定位它們只有兩條路：產物自己的時戳對照 `git log`，或訓練環境快照另記的 `git.commit`（只有那一種產物有）。這些產物不可改寫；本註記是它們唯一的解析說明。
