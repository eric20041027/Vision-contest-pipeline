# vcp 0.18.0 後記：程式碼快照、import 追蹤與產物授權（稽核 Wave 1c，待 0.18.0）

- spec：`../specs/2026-10-31-vcp-wave1c-code-snapshot-and-authorization-design.md`（99d8073，builder-check 裁決 c81511d，改以 0.18.0 並由使用者批准 §16 的 1e94798）。計畫：`2026-11-03-vcp-wave1c-code-snapshot-and-authorization.md`（78f0a8f），二十一個任務，大致一個任務一個 commit（21 個任務裡有 9 個另有審查修正的 commit：Task 2、3、4、8、10、13、14、15、18）。
- 分支 `feat/wave1c-code-snapshot`，base `main` 8b46ad4。
- commit（依任務）：
  - Task 1：`be3edcc` fix(core): vcp_version reads vcp's own repository whatever GIT_DIR says
  - Task 2：`735d6bc` feat(code): rules v1 and the in-memory scan of one code root；`2918171` fix(code): a tree with a .git entry stays git when git cannot be asked（審查修正）
  - Task 3：`162c069` feat(code): generated-code registry and code-root planning；`c5884b0` fix(code): exclusions never cover a root or a directory that holds one（審查修正）
  - Task 4：`fb690b8` feat(code): content-addressed code_snapshot artifacts under a per-id lock；`f1a3d39` fix(code): snapshot check reports absent files; length limit covers the temp path and case clashes refuse everywhere（審查修正）
  - Task 5：`248473b` feat(train): env snapshot code sections, new evidence kinds and reserved names
  - Task 6：`549a09e` feat(train): the environment probe reports distributions and an identity
  - Task 7：`0034964` refactor(data): artifact_ref is the public maker of every evidence ref；`61d3d03` feat(backup): a run's code snapshot is a tier-2 backup role；`f437a0e` feat(train): train run snapshots every code root before the first write
  - Task 8：`c35d8d0` refactor(train): the code WARN lines come from coderun, computed once per result；`a4f93dc` feat(train): a listed code file changed during the run becomes a code_changed note；`805c7b9` fix(train): the end-of-run code compare uses the scan's entries, never the snapshot directory（審查修正）
  - Task 9：`43b1e97` feat(code): a Session child traces its imports into a code_trace artifact at exit
  - Task 10：`238bf9d` feat(code): code grade traced > snapshot > commit > declared, computed on read；`2adb75b` perf(code): one IntactMemo shared by the code grade and train status --verify（審查修正）
  - Task 11：`06ab294` feat(artifact): code_snapshot inputs are checked and in-process imports traced
  - Task 12：`a89ae4f` feat(artifact): authorization artifacts bind a job to what its benchmark proved
  - Task 13：`a2615ab` feat(artifact): vcp artifact authorize from an artifact or a training run；`8b59bba` fix(artifact): authorization errors keep the command's identity and name an unreadable neighbour
  - Task 14：`44e70ed` feat(train): train run --authorization checks the run against its benchmark before any write；`1ad0bab` fix(artifact): authorize reads the configs root only for a run basis, which must have an environment identity；`7f852a2` fix(train): a code ref name another ref holds fails before the first write
  - Task 15：`021ad16` feat(code): vcp code snapshot, verify, diff and export；`eadcbad` fix(code): verify --dir names the snapshot it is, shares the scan's walk, follows no link; snapshot refuses data and configs roots（審查修正）
  - Task 16：`d6cbda6` feat(eval): eval ingest --code attaches a snapshot and eval status shows each run's code
  - Task 17：`8e2a089` feat(submit): require_code gates stage and final on the code grade
  - Task 18：`0096c94` feat(backup): backup and gc follow code snapshots, traces and authorizations；`cb3952b` test(backup): pin that a rewritten authorization basis is backed up as it is and a gone basis run fails the walk
  - Task 19：`eda66fa` feat(provenance): code grade on runs and edges for code snapshots and authorizations
  - Task 20：`27b0482` test(gate): guard the 0.18.0 code snapshot, trace and authorization behaviour；`ad9102f` docs(examples): quickstart snapshots a training run and verifies its code
  - Task 21：文件一個 commit（本檔、`cli.md`、CLAUDE.md / AGENTS.md、README 兩份、五個 skill 與鏡射、五份既有 spec 的指回、稽核文件、dirty 工作樹後記 §3 第 3 條、HANDOVER / CODEX_PROMPT）。
  - 最終審查的修正（2026-11-04，§2.1 第 46–49 條）：`7d9ae1d` fix(train): a trace module that cannot be imported never fails Session.current；`4ce5895` fix(train): the environment probe never reads the wrapper's working directory；`b0b3f3f` fix(code): a plain-directory walk does not enter a nested work tree；`b9c3378` fix(code): no git at trace exit, code= on every eval status line, quickstart checks verify；接著文件一個 commit（本檔、`cli.md`、CLAUDE.md / AGENTS.md、README 兩份、`vcp-train-submit-backup` 的 reference 與鏡射、HANDOVER / CODEX_PROMPT）。
- 每個行為變更都先寫測試、看它失敗再實作。例外是只鎖現狀的測試：0.17 的 golden（`tests/compat_v0170.py`，Task 5 與 Task 17：沒有新 ref 的 `TrainRecord` / `RunCard`、`require_code: declared` 的 profile、沒有 `code` 的 `stage.json` 都跟 0.17 逐位元組相同）、Task 18 審查後補的兩個釘行為的測試（`cb3952b`：被改寫而仍在的依據照現況備份、不見的 run 依據讓走法 `not_found:`）、回歸門檻那一列（`27b0482`，`tests/unit/test_regression_gate.py`）。
- 分支在 Task 9 與 Task 14 各派過一次 `workflow_dispatch`，早一點抓 Linux 的失敗：`43b1e97`（run 41743187799）與 `44e70ed`（run 41764890681），Lint、Tests ubuntu、Tests windows 都綠。
- 版本、`CHANGELOG.md`、`.claude/.claude-plugin/plugin.json`、tag 不在本分支（§2.2 第 1 條）；`docs/handover/` 與 README 的版本行在發版時一起改。

## 1. 已知限制

完整的列表見 spec §12；這裡只記使用時會踩到的，與實作中發現的。

- **spec §12 的限制照舊成立**：只追得到 Python 模組（不用 `Session` 的子程序、非 Python 程式、worker、非 0 rank、被強制結束的程序都沒有 trace，等級最多 `snapshot`）；未追蹤的設定不是身分（C1）；就地編譯的擴充是 `unlisted`；生成程式碼登記表只內建三項；「改過」是事後比對；Windows 上 `autocrlf=true` 的身分跟 Linux 不同；儲存不去重；中斷留下的半途目錄擋住那個內容直到 `vcp artifact clean`；重用的粒度是 root；機密只靠檔名、`.` 開頭的規則與 `.gitignore`；授權不判斷 benchmark 的內容、沒有時效；等級會隨產物損壞往下掉，provenance 索引要 `rebuild`。
- **snapshot editable 的 vcp checkout 很貴，快照 id 隨文件改動而變**（計畫的 Open point 3，使用者沿用 spec 的決定：root = PEP 610 的目錄，不改成套件頂層）。訓練 venv 裡 editable 的 vcp 是整個 checkout：追蹤的 `docs/benchmarks/*.json`（`CONFIG`，約 120 MB）每個 attempt 雜湊兩次（開跑時的掃描與結束時的比對），在 512 MiB 的上限內、但慢且吵；保留的位元組仍受單檔 1 MiB、每 root 64 MiB 的上限，所以磁碟上的副本有上限，不是 120 MB；任何一份被追蹤的文件或 benchmark 檔改了，這個 root 的身分就是新的 id，另存一份。釘在 tag 的 detached worktree 不會動，所以那種比賽 id 穩定、同內容只有一份；editable 指向會動的 main checkout 就一直產生新快照，也一直 `code_changed`（若訓練中動了它）。單元測試以 autouse fixture 把 editable roots 換成 `[]`，要追蹤的測試換成 `src/vcp`（§2.1 第 18 條）。替代方案（editable root 改成套件頂層目錄，即 `vcp.__file__` 的上層）未採用，列在 §2.2 第 6 條。
- **Windows 的 `MAX_PATH` 預檢照樣適用於 `LongPathsEnabled=1`**：保留位元組時，`unwritable` 的預檢（`snapshot.MAX_PATH = 259`）量的是 `write_once` 實際會開的暫存檔路徑（最終路徑加上 `.<名>.<8 位十六進位>.tmp`），不管機器有沒有開長路徑。Task 20 量到：vcp 的 checkout 追蹤的最長路徑是 139 個字元（`docs/benchmarks/postgres-provenance-calibration-v2-artifacts/…`），算式是 data root 的絕對路徑 + `/artifacts/code_snapshot/code-<16 hex>/files/`（53 字元）+ 檔案的相對路徑 + 暫存檔尾巴（14 字元）> 259，所以 data root 超過約 53 個字元（臨時目錄底下的 data root 很容易），整個 vcp root 就是 `unwritable`，`train run` 降到 `declared`（`code_snapshot=partial`）；`C:/vcp-data` 這種短的 data root 沒有這個問題。`examples/quickstart.py` 為此在它自己建的空 venv 裡訓練（它的暫存 data root 很長），所以沒有示範 editable vcp 的 root（§2.1 第 36 條）。要不要偵測 `LongPathsEnabled` 見 §2.2 第 6 條。
- **`code=` 是「能證明多少」，不是 WARN**：`eval ingest --code` 掛在已有 `train.yaml` 的 run 上時，ref 照記、但等級仍由 `train.yaml` 算，不會因它升級（spec §5.4）。沒有 trace 的 run 最多 `snapshot`，不 WARN（spec §14 第 17 條）。
- **等級的計算每次都全量雜湊**：`train status`、`eval status`、`submit stage` / `final` 每次呼叫都重算每份 pinned 快照與 trace 的完好（`ref_intact`），沒有跨呼叫的快取；一個 20,000 檔的 root 在 Windows 上約 12 秒（每檔約 0.6 毫秒），`train status --verify` 加倍。同一個指令內的迴圈共用一個 `IntactMemo`（§2.1 第 21 條）。
- **`code_grade` 缺席 = `declared`**：provenance 的 run entity 只在等級高於 `declared` 時寫這個屬性（§2.1 第 44 條），讀的人用 `.get("code_grade", "declared")`。
- **同一個 attempt 裡兩個非 multiprocessing 的 `Session` 程序**（例如子程序再起的另一個 python）可以同時綁 ref，spec §14 第 14 條的 rank-0 / 主程序限制擋不到它們，沒有檔案鎖；`train.yaml` 的重寫是原子的，但後寫的可能蓋掉先寫的 ref。
- **在快照之後、被 import 之前才改的模組**：`snapshot_inputs` 看到的是磁碟上的位元組，記憶體裡的是舊的，這一關看不出來（spec 層級的限制）。
- **`--resume` 要重複 `--authorization`、`--evidence` 與 `--labels`**（它們是 claim 的一部分）；沒給 `--config` 時 `param:config_sha256` 是命令的 sha（`config_hash = sha256_json(command)`），所以只 `--free param:command_sha256` 不允許「多跑幾個 epoch」，兩項都要 free，或改用 `--config`。
- **授權的 run 依據只算 `train run` 自己掛的 ref**：session 綁定的證據（`Session.attach_evidence`）與後來的 attempt 才綁上的 ref 不是 claim（preflight R6），因為 `train run` 在開跑前算不出它們。
- **一般目錄 root 裡 clone 進來的 repository 不在快照裡**（§2.1 第 48 條）：含 `.git` 項目的子目錄不往下走、只記進 `excluded_paths`，所以它的程式碼不是外層快照的身分；從它 import 的模組是 trace 的 `unlisted`，那個 attempt 到不了 `traced`，要涵蓋就加 `--code-root`。git root 裡未追蹤的巢狀 repository 本來就不列（git 把它報成一個目錄）。
- **一個 label set 若叫 `authorization` 或 `code.x`**：`--labels` 的 id 跟保留的 ref 名稱撞名時，`train run` 在第一次寫入前 FAIL `evidence_conflict:`（Task 14）；`eval ingest` 也是 `evidence_conflict:`。

## 2. 執行期裁決與開放待辦

### 2.1 執行期裁決

格式：決定 — 依據 — 代價（錯了的話）。

**預掃（pre-flight scan，開工前，controller 2026-11-03）**：由一個 opus 子代理把計畫逐節對照 spec 與現有程式碼（表 A 57 列中 14 列不一致，表 B 21 個任務中 12 個、表 C 19 項中 14 項、表 D 15 項中 14 項有出入），提出 R1–R27。決定 — 全部照提議接受，每一條都是 spec 已決定的事的最小修正（逐任務的對照在各任務的簡報裡）；R9 窄化（`_TRACKED` 的清除由 Task 11 加進 autouse fixture，因為 Task 7 時 `_TRACKED` 還不存在）、R25 窄化（gc 的 `path_key` 改成 import `vcp.core.paths.path_key`，主體相同；`raise_for_outcome` 在 Task 11 建立、Task 14 使用）。代價 — 錯了的話某個任務會建出名字或測試不對的東西，由該任務的審查抓到。

**計畫的十五個 Open points（controller 2026-11-03 全部照提議，對實作有約束力）**

1. **測試目錄不放 `__init__.py`**：`tests/unit/code/` 照 spec，檔名一律 `test_code_*.py`。依據：`tests/unit` 沒有 `__init__.py`，prepend 模式會把它放上 `sys.path`，`code/__init__.py` 蓋掉標準庫的 `code`（`pdb` 用它），跟 0.17 的 `gc` 同一個陷阱；沒有 `__init__.py` 的目錄只是 namespace 片段，標準庫優先。代價：無（HANDOVER §8 記下這個陷阱）。
2. **git 子程序環境搬到 `vcp.core.proc.git_environment()`**：`vcp.core.build` import `vcp.train.gitstate` 會循環（`vcp.train/__init__` → `session` → `artifact.writer` → `core.build`），所以環境函式放在核心，`gitstate` 的 `_environment` / `_LOCAL_ENV` 指向它，是同一個環境（去掉 `GIT_DIR` 等 13 個決定 repo 的變數、設 `GIT_OPTIONAL_LOCKS=0`）。代價：無；dirty 工作樹後記 §3 第 3 條因此處理。
3. **editable 的 vcp checkout 照 spec 當整個 root**（見 §1 的成本）；替代方案（套件頂層）不採用。代價：慢、id 隨文件變；寫進後記而不是改設計。
4. **在 root 裡、但被規則跳過的位置**：`<root>/.venv/.../site-packages/numpy` 這種相對路徑有被規則跳過的段（或 venv 的上層）時不算「在 root 裡」，往下分到 stdlib / distribution / outside。依據：照字面它會先對到 root、不在 index → `unlisted`，venv 放在專案裡的 run 永遠到不了 `traced`。代價：無。
5. **Windows 的 `DLLs`**：Windows 上把存在的 `sys.base_prefix/DLLs`、`sys.prefix/DLLs` 算 stdlib，由 `trace._WINDOWS = os.name == "nt"` 把關（測試 patch `trace._WINDOWS`，不 patch `os.name`；preflight R5）。依據：標準庫的擴充模組在那裡，不在 `sysconfig` 的 `stdlib` / `platstdlib`，照字面每個 Windows trace 都 incomplete。代價：無。
6. **重用時的 `mismatch:`**：data root 裡同 id 的快照被竄改時，`train run` 一律 FAIL `mismatch:`（同 source audit，訊息說搬走目錄再跑），不新增第九個 reason 字。代價：降級成某個既有 reason 的替代方案沒採用。
7. **`--code-root` 指到不存在的目錄**：它是使用者給的輸入，第一次寫入前 FAIL `not_found:`；`reason: not_found` 留給探針回報、但已不存在的 editable 目錄（那個 root `partial`，`code_snapshot=partial`，preflight R1 的測試）。
8. **run 依據缺快照**：`authorize` FAIL `invalid:`（不默默少綁一項）。實作時再補兩個字：一個沒有 `train.yaml` 的 run（`eval ingest` 的）或沒有 attempt 的 run 當依據是 `invalid:`；依據的快照、plan 或證據產物不見是 `not_found:`；attempt 的環境沒有有效的 `identity` 是 `invalid: basis run <id> attempt <n> has no environment identity`（Task 13 的 ruling，Task 14 補齊）。
9. **`code_trace` 的 input 名是 `snapshot.<label>`**，不是保留名 `code_snapshot.<label>`：後者會讓 writer 在子程序結束時再對每份快照做完整的 `verify`。provenance 的 input 邊與 gc 的引用照樣連得到。
10. **`require_code` 只讓 candidate FAIL `code_required:`**：baseline 與 probe 對每個低於要求的 run 寫 pairing check `code:<run>=<等級>`。已對照 `src/vcp/submit/stage.py`：`require_provenance` 也只擋 candidate。
11. **授權與依據的完好性用哪個字**：消耗時授權或依據壞了、被取代、不見都是 `superseded:`（訊息說是哪一種）；建立時依據驗不過 `mismatch:`、被取代 `superseded:`。
12. **plan 釘死的格式細節**（spec 沒寫到位元組）：`packages_sha256` 每列 `"<PEP 503 名>==<version> <record_sha256 或 -> [commit_id]"`、排序、各帶 `\n`；`capped` 保留依 path 順序貪婪（之後較小的檔仍可放進 64 MiB）；`--free` 不在 claims 裡的名字 FAIL `invalid:`；授權的 `params` 在 `consumer: train` 時不寫 `target_kind`（`params` 只收字串）；`code.roots` 是空的不算「快照齊全」；開跑前那一行的路徑寫成 `<label>:<path>`、以 `, ` 串接；warning 文字 `code_snapshot=<state> (code_missing=<labels>)` 與 `code_trace=incomplete (code_gaps=<n>)`。
13. **明要快照遇到讀不動的 root**：ABORT `code_unavailable:`、`cause=unreadable`（權限是環境問題，比照 `locked` / `partial` / `unwritable`）。
14. **run 依據的 `input:plan`** 讀現在的 plan 檔的 sha：`train.yaml` 沒記 plan 的 sha，plan 寫了不改，現在的 sha 就是當時的。
15. **`unwritable` 的預檢**：Windows 上在宣告 id 之前先檢查路徑長度與大小寫重名，預檢不過就什麼都不寫；寫到一半的 `OSError` 仍是 `unwritable` 並照一般中斷留下半途目錄（見下面第 26、27 條 Task 4 的兩個修正）。

**結構與介面**

16. **`ProbeReport` 與 `snapshot(report=)`**：`probe()` 回傳 `ProbeReport`（`data` 是探針印出的 JSON、`distributions`、`editables`、`identity`、`gpus`、`driver`，加 `child_vcp` 屬性）；`train run` 在第一次寫入前跑一次探針、用它規劃 root 與檢查子程序的 vcp，再把同一份 report 交給 `env.snapshot(report=…)`，所以 `env.<n>.json` 的環境跟規劃 root 時看到的是同一次探針。`snapshot()` 把 `distributions` / `identity_python` 從 `report.data` 拿掉再建 `EnvSnapshot`，明確傳 `distributions=report.distributions, identity=report.identity`（preflight R18）；讀 distribution 的程式碼只有一份（`_DISTRIBUTIONS_SRC`，嵌在探針腳本裡、`local_distributions` 也用它，R25b）。`ProbeReport.data` 保留未清理的 `direct_url`（含憑證的 URL），只在記憶體裡、從不寫出（§2.2 第 9 條 train run 小項）。探針缺欄位 → ABORT。
17. **新邏輯放 `vcp/train/coderun.py`**（root 規劃、掃描、寫快照、結束比對、WARN 行），`run.py` 只加呼叫與欄位（`run.py` 已超過 800 行，見 §2.2 第 9 條）。`coderun.warnings` 的 WARN 行在 `_result` 算一次（Task 8 審查）。
18. **`_code_tracking_off` 與 `trace_on`**：從 Task 7 起 `tests/conftest.py` 有 autouse 的 `_code_tracking_off`（`VCP_CODE_TRACE=0`、`vcp.code.roots.editables_of` → `[]`；Task 11 起也清 `vcp.code.process._TRACKED`），既有的 `train run` 測試只多一個 plain `cwd` root，沒有新 WARN，也不去雜湊真的 vcp checkout；要追蹤或要 editable root 的測試呼叫 `helpers.trace_on(monkeypatch)`（editable root 換成 `src/vcp`）。共用 fixture（`proj`、`bench`）放 `tests/code_fixtures.py`，由 `tests/conftest.py` 的 `pytest_plugins` 載入，因為逐模組 import 會觸發 ruff F811 / F401、逐模組 `pytest_plugins` 會警告（Task 11 的 R13 ruling）。`tests/compat_v0170.py` 放 0.17 的 golden（R15）。`_TRACKED` 以 `(path_key(data_root), snapshot_id)` 為鍵（R9，窄化版：不另開 `process.Tracked`，用 trace 的 `TracedRoot`）。
19. **`src/vcp/code` 的分層**（preflight R8）：`trace` / `grade` / `diff` 可以 import `vcp.train.records` / `vcp.train.env` 與 `vcp.data.evidence`；`vcp.train.schema` / `vcp.train.env` import 的模組（`vcp.code.schema`、`scan`、`rules`、`roots`、`generated`）永不 import `vcp.train`。`path_key` / `is_under` 放 `vcp.core.paths`，gc 的 `path_key` 改成 import 同一個（同一個 body，R25a 窄化）；`process.raise_for_outcome` 是 explicit 路徑共用的拒絕（R25c）。
20. **append 保護測試的名字**：計畫的 Global Constraints 寫 `test_append_only_ledgers.py`，實際是 `tests/unit/core/test_atomic.py::test_no_module_but_atomic_opens_a_file_for_appending`（R22）。
21. **等級計算的快取（Task 10）**：決定 — 每次呼叫保留完整的 `ref_intact` 語意（沒有樂觀模式），加一個呼叫者提供的 `IntactMemo`（`vcp.data.evidence`），迴圈（`eval status`、`stage` / `final`）共用一個，`train status` 在 `--verify` 下讓 `broken_refs` 與 `code_info` 共用一個；memo 只在一個操作之內、不跨寫入保留。依據 — spec §5.4 說產物被刪、被改時等級要降，`require_code` 的門檻需要它。代價 — 沒有 memo 的單次呼叫仍是全量雜湊（§1 的數字）。Task 10 審查的 Minor 1 一併處理：`test_code_grade.py` 的 `monkeypatch.undo()` 改成 `monkeypatch.context()`（`undo()` 會還原 autouse 的隔離 fixture，違反測試不碰真的 data root）。

**snapshot / scan（Task 2–4、15）**

22. **`root_kind` 的退路**（Task 2）：計畫寫「沒有 git 或失敗 → 目錄」，改成：`path` 或任一祖先有 `.git` 項目（檔或目錄）就是 `git`，即使 git 跑不動；`scan_root` 此時 fail closed 成 `unreadable`。依據 — spec §7 禁止讀被 git 忽略的檔，退回目錄 root 就會讀到它們。代價 — git 拒絕運作的機器（dubious ownership、沒裝 git）上，repo 沒有快照（WARN `partial`），而不是一份目錄式的。`repo_top` 同樣退到最近的含 `.git` 的祖先（Task 3 的帶入項）。
23. **掃描的 `not_found` 與走訪**：`ScanReason` 多 `"not_found"`，`scan_root` 對不是目錄的 root 回 `RootScan(reason="not_found", entries=())`（R1）；一般目錄 root 的走訪不進任何 `skip_dirs`，被跳過的項目不算進 `MAX_WALK_ENTRIES`（R3：包住 data root 的 plain root 不會因為 data root 的項目多而 `too_large`）；submodule 測試傳 `-c protocol.file.allow=always`（R27）。
24. **`script_of` 與 `plan_roots`**（R2）：`script_of` 回傳命令裡每個存在的 `.py`，`plan_roots` 取第一個不在已規劃 root 底下的（`python run.py ../other/train.py`）。
25. **checkpoint glob 的基底**：基底目錄裡有巢狀 root 時不排除任何東西（Task 3 審查的 Important）；所以直接放在基底裡的 `.py` 仍算外層 root 的身分（spec §4.2 的字面）。
26. **路徑預檢量暫存檔的路徑**（Task 4）：決定 — Windows 的路徑長度預檢量 `write_once` 實際開的暫存路徑（最終路徑加暫存尾巴，常數與 `atomic` 的命名連動），並涵蓋頂層的 `.manifest.json` 暫存檔。依據 — OP15 的用意是預檢不過就什麼都不寫，字面的 `artifact_dir/files/<path>` 量不到暫存檔。代價 — 多拒絕幾個路徑接近上限的 root（`unwritable`）。
27. **大小寫重名的預檢在每個平台都做**（Task 4）：依據 — spec §2 反目標（快照失敗不擋訓練），大小寫不分的 POSIX 磁碟（macOS、共用）也會寫到一半 `exists:`。代價 — Linux 上只差大小寫的兩個程式檔的 repo 也沒有快照（`unwritable`）。已知的邊角：`casefold` 把 `ß` 摺成 `ss`，`straße.py` 與 `strasse.py` 在 POSIX 上也被拒，`lower()` 才是精確的（§2.2 第 9 條 snapshot / scan 小項）。
28. **`verify --dir` 報 `id=` 並接受 `--id`**（Task 15；與 spec §6 的表不同）：只要 index 能解析、`.vcp-code/snapshot.json` 與之相符，VERDICT / JSON 就帶 `id=code-<身分前 16 碼>`（`dir=` 旁邊）；`--id` 可以同時給，必須等於它，否則 FAIL `mismatch:`（原本同時給是 `invalid:`）。依據 — spec §12 的用法（驗 `submit stage --code` 記下的 kernel bundle）需要回答「這個目錄是 code-X 嗎」。代價 — 多一個 VERDICT 欄位。
29. **`verify --dir` 不跟隨連結、讀不動的檔是 `mismatch:`**（Task 15 Minor 6）：index 路徑上的連結一律 `mismatch:`、從不跟隨（讀 DIR 以外的東西是安全缺口）；`.vcp-code/` 檔的 `OSError` 是 `mismatch:`，不是 ABORT。`extras` 與快照共用 `scan.walk_files` / `is_linked` / `ScanStopped`（原本是掃描器走訪的近似複本，錯誤處理較弱）。
30. **`vcp code snapshot --root` 在 data root 或 configs root 裡面 → `invalid:`**（Task 15 Minor 3；與 spec 不同）：依據 — 明要的快照不能默默得到 `files=0`。代價 — 想要空快照的人做不出來。已知邊角：configs root 的猜測會拒絕 MMDetection 風格 repo 的 `--root configs`（§2.2 第 9 條 vcp code 小項）。
31. **明要的快照遇到不存在的 root → FAIL `not_found:`**（Task 11；與計畫的 controller 清單不同）：依據 — spec §6 `code snapshot` 列 FAIL `not_found:` 與 Open point 7；`snapshot_inputs` 的 editable 目錄已不存在也一樣。代價 — 想改成 ABORT 只是換一個字。

**train run（Task 5–8、14）**

32. **把 `code_snapshot` 備份 role（tier 2）從 Task 18 提前到 Task 7**（`61d3d03`）：每個 0.18 的 `train run` 都掛一個程式碼 ref，沒有這個 role，`backup manifest` 會 ABORT（`test_e2e_evidence`）。代價 — 一個 commit 的搬動；Task 18 的預期紅燈改成 `code_trace` / `authorization` / `authorization_basis` 的 `KeyError`。
33. **`attach_code_refs` 接在 `attach_cli_evidence` 之後**（R7）：`record, digests = attach_cli_evidence(...)`，再 `record = attach_code_refs(p.data_root, record, results, n)`，不丟掉帶 `--evidence` 的 record。R12：`FAKE` 與以 `seed_tiny` 為底的 seed helper 搬進 `tests/helpers.py`，共用的 `work` fixture 在 `tests/conftest.py`。R24：Task 7 的 `rg` 步驟（找沒傳 `cwd` / `--cwd` 的 `train_run(` 與 `"train", "run"`）**沒有找到需要補的測試**，所有現有呼叫都已傳 `cwd`；報告記為 none found。
34. **結束時的比對用記憶體裡的掃描項**（Task 8；與計畫的「Consumes `load_index`」不同）：決定 — `changed_since_snapshot(results)` 用 `RootResult.scan.entries`（快照身分是由這些項算的，重用時已對過磁碟上的 index），不重新讀快照目錄的 index。依據 — spec §2 反目標與 §14 第 15 條（追蹤永不改變 exit code）：`load_index` 在子程序結束後才炸會丟掉 exit code 與 `_finish_checkpoints`。代價 — 子程序執行中被竄改的磁碟 index 在這一步看不出來（`check_snapshot` 與 grade 看得到）。簽名是 `changed_since_snapshot(results)`，計畫第 521 行與 `task-8-brief` 寫的 `(data_root, results)` 已過時。
35. **有授權時，被 `--free` 放行、掃描失敗的 root 只 WARN**（Task 14）：決定 — 掃描失敗（`too_large` / `unreadable`）的 root 若授權沒綁它的 claim 仍只 WARN，綁了的才 `unauthorized:`。依據 — spec §5.1 第 6、8 步只在綁住的 claim 與四種寫入失敗 fail closed。代價 — 被放行的 root 在授權的 run 底下沒有快照。另：第一次寫入前的名稱檢查也拒絕同一次呼叫的 `--labels` id 跟 `code.<label>` / `authorization` ref 撞名（代價 — 字面叫 `code.cwd` 的 label set 掛不上 `train run`）。
36. **quickstart 在它自己建的空 venv 裡訓練**（Task 20）：代價 — quickstart 不示範 editable vcp 的 root（§1 的 `MAX_PATH` 條目）。

**trace（Task 9、11）**

37. **stdlib 與 site 目錄重疊時取較深的那個**（Task 9）：Windows venv 的 `platstdlib`（`<venv>/Lib`）與 Linux 的系統 stdlib 都包含 `site-packages`，照 spec 的字面順序每個 distribution 都會算 stdlib。代價 — 只影響 `trace.json` 裡 stdlib / distribution 的計數，`complete` 不受影響。
38. **`\\?\` 前綴**（Task 9 帶入 Task 11）：uv 的 junction 之後 `resolve()` 在 Windows 可能給 `\\?\C:\…`；`trace._plain` 去掉它（§2.2 第 9 條 trace 小項要求把這個搬進 `core.paths`）。
39. **`snapshot_inputs` 的 `raise_for_outcome`**（R25c，窄化）：explicit 路徑（`snapshot_inputs`、`snapshot_roots`、`coderun.write_all(strict=True)`）共用 `process.raise_for_outcome(label, reason)` 把 root 的 reason 變成 `too_large:` / `not_found:` / `drift:` FAIL 或 `code_unavailable:` ABORT。

**authorization（Task 12、13、18）**

40. **Task 13 的小裁決**（controller 接受實作者的）：沒有 `train.yaml` 的 run 或沒有 attempt 的 run 當依據是 `invalid:`；依據的快照 / plan / 證據產物不見是 `not_found:`；漏給 `--reason` 是 VERDICT `invalid:`（不是 click 的用法錯誤）；`authorize` 要求依據快照的 `snapshot.json` 存在並綁它的 sha、不重新雜湊保留的檔（消耗時比的是 sha，消耗者自己的快照在寫入時驗）。代價 — 依據的保留檔在它的 run 之後損壞，仍可以被授權。
41. **授權的 run 依據沒有 `run.yaml`**（Task 18）：`walk_run` 丟 `not_found:`，整個 `run:<id>` 備份結論失敗（`all` 與 gc 的單位則略過並 WARN），跟遺失的融合成員同一個處理；備份不能默默漏掉紀錄點名的證據。代價 — 刪掉一個 benchmark run，會讓所有以它授權的 run 無法備份，直到授權被 supersede。
42. **被改寫而仍在的依據照現況備份**（Task 18，範圍外）：備份記證據現在的樣子；spec §4.8 沒要求 `backup verify` 比對授權釘住的依據 sha，`consume` 已經在使用時拒絕被改寫的依據（`superseded:`）；`cb3952b` 用測試釘住這個較窄的行為。代價 — 依據被改寫的授權，備份還原回來的是改寫後的依據，之後讀成 `superseded:`。

**eval / submit（Task 16、17）**

43. **`submit stage` / `final` 的 VERDICT `code=`**（Task 17）：是等級；kernel candidate 沒給 `--code` 時是 `none`；`--code` 的 `not_found:` / `mismatch:` 時是那個快照 id。

**provenance（Task 19）**

44. **run entity 只在等級高於 `declared` 時帶 `code_grade`**（Task 19）：決定 — 缺席 = `declared`。依據 — 一律寫會改變 `build_graph` 的 hash，也就改變已預登記的 E5 與已發表的 held-out benchmark 的 `workload_hash`，那些不能被重新釘住。代價 — 讀的人必須用 `.get("code_grade", "declared")`；`graph.DECLARED` 是 schema 等級字串的複本（§2.2 第 9 條 provenance 小項）。spec §4.8 / §8 的文字是「run entity 多了 `code_grade`」，沒說缺席；文件（cli.md、`vcp-provenance-graph` skill）都寫了缺席的語意。

**與 spec 文字不同的地方（文件以程式碼為準，spec 沒改）**

45. 列在這裡方便查：(a) `code_grade` 只在高於 `declared` 時寫（第 44 條）；(b) `verify --dir` 的 VERDICT / JSON 帶 `id=`，且接受 `--id`（第 28 條）；(c) 明要的快照遇到不存在的 root FAIL `not_found:`（第 31 條）；(d) `--root` 在 data root 或 configs root 裡面 FAIL `invalid:`（第 30 條）；(e) `code_trace` 的 input 名是 `snapshot.<label>` 而不是 `code_snapshot.<label>`（第 9 條）；(f) 結束時的比對用記憶體裡的掃描項，不讀快照的 index（第 34 條）；(g) 有 `.git` 項目但 git 問不動的樹仍是 git root、掃描 fail closed（第 22 條）；(h) 大小寫重名的預檢每個平台都做，路徑長度預檢量暫存檔路徑（第 26、27 條）；(i) `authorize` 多 `--supersedes OLD`；(j) 授權消耗時授權或依據壞了、被取代、不見都是 `superseded:`（第 11 條）；(k) 探針提前之後，`vcp code snapshot` 與 `train run` 對 `unreadable` root 的處置是 ABORT `code_unavailable:`（第 13 條）；(l) 同一份 `ProbeReport` 同時用於規劃與 `env.<n>.json`（第 16 條）；(m) 一般目錄 root 不進含 `.git` 的子目錄、把它記進 `excluded_paths`（第 48 條；spec §4.2 只說 git root 的規則）。

**最終審查（whole-branch review，2026-11-04；controller 的裁決在 `final-findings.md`）**

46. **trace 模組的 import 也在 `try` 裡**（Important 1）：`Session.current()` 的 `from vcp.code import trace` 原本在保護 `trace.arm` 的 `try` 外面，import 失敗會讓 `Session.current()` 丟例外、訓練子程序的 exit code 跟著變（spec §5.2「整段包在 try 裡」、§2 反目標）。現在 import 與 `arm` 同一個 `try/except Exception`，失敗印同一行 `[vcp] code trace skipped: <原因>`（`session._trace_skipped`；trace 模組載不進來時沒辦法用它自己的 `_skipped`，所以這一行的格式有兩份）。測試：`sys.modules["vcp.code.trace"] = None` 時 `Session.current()` 照常回來、stderr 一行；子程序裡同樣毒化後 exit code 仍是它自己的 3（回歸門檻那一列多了這個測試）。
47. **探針不讀 wrapper 的 cwd**（Important 2）：`python -c PROBE` 的 `sys.path[0] == ''`，`importlib.metadata.distributions()` 先列 `<cwd>/*.egg-info`，先到先贏的去重就取它（審查者重現：cwd 裡過時的 `pydantic.egg-info` 讓紀錄變成 pydantic 0.0.1、沒有 RECORD，`packages_sha256` 改變；遮住 editable 時它的 `dist-<name>` root 根本不會被規劃，`child_vcp` 也可能報錯）。現在 `_DISTRIBUTIONS_SRC`（`local_distributions` 共用）與探針的 `packages` 迴圈都用 `distributions(path=[p for p in sys.path if p])`。代價：無；同一個環境在任何 cwd 下的列、身分、editables 相同（測試從乾淨的 cwd 與放了假 egg-info 的 cwd 各探一次比對）。探針的 `import torch` 仍走含 `''` 的 `sys.path`（cwd 裡叫 `torch` 的目錄會被 import），不在這次裁決的範圍，見 §2.2 第 11 條。
48. **一般目錄 root 裡含 `.git` 的子目錄是邊界**（Important 3）：決定 — `_walk_candidates` 遇到含 `.git` 項目（檔或目錄）的子目錄不往下走、把它記進 `excluded_paths`（它是另一個 repository，不是這個 root 的身分）；trace 因此把從它 import 的模組報成 `unlisted`，使用者用 `--code-root` 把它加進來。`verify --dir` 用的 `walk_files` 是同一個走訪，同一條規則。root 本身不檢查（有 `.git` 的 root 已經是 git root），本身是另一個 root 的巢狀目錄先被 `skip_dirs` 跳過、不記成排除。依據 — spec §7：被 git 忽略的檔永不讀，快照是 tier 2、會到異地；git root 已經不列未追蹤的巢狀 repository（`scan.py` 的 `nested/`）。代價 — 沒登記的巢狀 clone 的程式碼不在外層快照的身分裡（§1）。
49. **最終審查的小項，在這一輪做完**：`trace._warm` 也叫 `build_string()`（`build_info` 是 `lru_cache`，整個程序穩定），結束時不再跑最多三個沒有逾時的 git 子程序；`eval status` 在 provenance 算不出來的 run 那一行也印 ` code=<等級>`（spec §5.8：文字與 `--json` 都有每個 run 的 code）；quickstart 第 14b 步要求 `code verify` 的 VERDICT 是 `status=OK`（跟第 13 步的 `code=snapshot` 同一個 `expect`），WARN 不再靠 exit 0 過關；測試名 `..._wants_acode_snapshot` 改成 `..._wants_a_code_snapshot`；Task 21 審查的文件小項（發版要翻的「待發版」位置不含 `vcp-release-and-environments`、HANDOVER 表格裡的 `|` 跳脫、`cli.md` 的 `diff --id --root` 也排除生成程式碼目錄、本檔第 3 行的審查修正數、README 功能列加「待發版」）。

### 2.2 開放待辦（依優先順序）

1. **發版 0.18.0**（使用者同意之後；發版 commit 不在功能分支）。`CHANGELOG.md` 加 `## [0.18.0]`：MINOR 的理由（spec §9）；Added / Changed / Fixed；相容性與升級（spec §8：整個 data root 與每個訓練 venv 先升、`provenance rebuild`、`require_code` 的 `extra_forbidden`）；已知限制（spec §12 與本檔 §1）。`src/vcp/__init__.py` 的 `__version__`、`tests/unit/test_package.py` 的 `EXPECTED_CANDIDATE_VERSION` 與 `.claude/.claude-plugin/plugin.json` 同步改成 0.18.0；README 兩份的版本行，與「待發版」字樣所在的地方——README 兩份「Roadmap / 路線圖」的 0.18.0 那條與「How it works / 它怎麼運作」的 0.18.0 功能列、`vcp-orientation/map.md` 的「歷史」、`docs/handover/HANDOVER.md` 與 `CODEX_PROMPT.md`——以及 `docs/handover/` 的現況與測試數字一起更新。目標在課程凍結（2026-11-13）前合併；趕不上就留在分支，12/06 之後再合。
2. **凍結前的契約項**（CODEX_PROMPT 第 3 條）：下面第 9 條的審查遺留裡，會改變寫入內容、VERDICT 欄位、`reason=` 字彙或 exit 類別的，要在 1.0 之前做完，或明寫留到 1.0 之後。各任務審查標「should triage / must triage」的：trace 的武裝在 `try` 外（§14 第 15 條，**must**）——**已修**（§2.1 第 46 條）；`write_snapshot` 的 `OSError` 逸出（§2 反目標）——留待，見第 11 條；`--evidence` 檔雜湊與複製之間的變動（授權綁了未綁定的 sha）——留待，見第 11 條；`eval status` 人看的行漏掉 `code=`（§5.8）——**已修**（第 49 條）；PROBE 的 `sys.path[0]==''`——**已修**（第 47 條），distribution 列的順序（身分決定性）仍開放；`build_string()` 在結束時的 git 子程序沒有逾時——**已修**（第 49 條）。
3. **精確到「被 import 的檔」的重用**（spec §12）：現在重用的粒度是 root，同一個 root 裡沒被 import 的程式檔改了，新 spec 的快照就不同，`reuse` 報 `spec_mismatch:`。要精確到被 import 的檔，要把 trace 寫進產物並讓 `reuse` 比對它。
4. **標準庫寫的 Kaggle 內驗證腳本**（spec §12）：Kaggle kernel 裡驗 bundle 現在要裝 vcp 跑 `code verify --dir`；`index.jsonl` 是純 JSONL 加 sha256，可以補一個只用標準函式庫的腳本（`kept: false` 的檔 WARN `not_kept=` 的語意要一致）。
5. **其他框架的生成程式碼登記項**（spec §12）：內建只有 `torch_inductor`、`torch_extensions`、`triton`；JAX、TensorFlow、numba 等在登記之前是 `outside`，那個 attempt 到不了 `traced`、產物 job 會 `code_outside:`。`register_generated(name, resolver)` 已經是介面，要補的是登記項與 spec 的表。
6. **editable vcp 的成本與 Windows `MAX_PATH`**：(a) 若實際比賽覺得慢，把 editable root 改成套件頂層目錄（`vcp.__file__` 的上層，計畫 Open point 3 的替代方案；身分會改、`rules` 要不要升版要再議）；(b) 開跑與結束比對各雜湊一次、provenance 建圖每份 pinned 快照又雜湊兩次（`store.verify` + `ref_intact`），可以共用雜湊；(c) 偵測 `LongPathsEnabled`（登錄檔 `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem`）並在開了的機器上放寬預檢，現在 259 的預檢照樣擋；(d) 長 data root 上 editable vcp 的 root 降級時，WARN 可以直接說「路徑太長」。
7. **wandb 之類輸出目錄的提示**：工具把程式碼存進沒被忽略的輸出目錄（例如 `wandb/`）時，`untracked_code=` 的 WARN 已經在開跑前列出前 5 個路徑，但沒有說「這是同一個目錄，加進 `.gitignore`」。可以在 WARN 的行裡把共同的上層目錄抽出來提示。
8. **使用者覆核 spec §16 之外的裁決**：本檔 §2.1 第 22、26–31、34–35、44 條偏離了 spec 或計畫的字面，發版前集中看一次。
9. **審查留下的小項**（沒有急迫性；除第 2 條點名的外，最終審查判斷都可以等）。依區域：
   - **snapshot / scan**：
     - Task 1：`test_proc` 只設與檢查 13 個 `GIT_LOCAL_ENV` 名稱裡的 4 個（改成逐個迴圈）；core 的測試 import `vcp.train.gitstate`（計畫要求的等式檢查）；沒有測試 `GIT_OPTIONAL_LOCKS=0` 對 `build._git` 的效果。
     - Task 2：位元組數取自 `lstat`、sha 取自之後的讀取（改成邊雜湊邊數）；`key_within` 跟 `gc/scan.py` 的 `_within` 重複、`scan._git` 是第三個 which-git 複本；每個走訪候選多一次 `lstat`，prune / pyvenv 規則重複；`core.symlinks=false` 的 Windows checkout 把追蹤的 symlink 當文字雜湊（跨 OS 身分，spec §12 或後記）；重複的 junction 測試，沒有 `excluded_paths` 的 posix 拼法與排序測試；`scan.py` 一行註解的措辭；`_has_dot_git` 不看 `GIT_CEILING_DIRECTORIES`（"dir" 測試因此依賴 `tmp_path` 上面沒有 `.git`）、`resolve()` 出錯被當成沒有 `.git`。
     - Task 3：`test_a_relative_script_is_found_from_cwd` 前半沒有斷言具體的東西；`plan_roots` 每個 cwd root 約五次 git 子程序（兩個 root 0.14 秒）；`generated._resolve` 的 `except Exception` 在解析函式出錯時不留痕跡；`test_repo_top_agrees_with_root_kind` 的迴圈裡 `shutil.which` 一直被 patch；相對的 `TORCHINDUCTOR_CACHE_DIR` 以 wrapper 的 cwd 解析而不是子程序的；`_within_roots` 對每個 root 重算 `path_key(resolve)`（可忽略）。
     - Task 4：**（最終審查要對照 §2 反目標）** `write_snapshot` 仍有 `OSError` 逸出——鎖目錄 / 鎖檔建立、重用時非 `FileNotFoundError` 的讀取錯誤、id 路徑是檔時 `ArtifactWriter` 的 `exists:`、`artifact clean` 在作業中途移走目錄時 `_claim` 的 `not_found:`；`check_snapshot` 不比 manifest 的 `params.identity_sha256` 跟 index 重算的身分（重用有比）；既有問題（Task 4 之前）：`ArtifactWriter.create` 在寫 `spec.json` 之前先 `mkdir`，沒有 `spec.json` 的目錄永遠是 partial（`clean` 當外來目錄不碰）；測試的小毛病（被格式化器拆開的 tuple、沒用到的 `src`）；`casefold` 把 `ß` 摺成 `ss`（`lower()` 才精確）；`files/` 底下巢狀目錄的 `mkdir` 長度沒預檢（寫到一半 `unwritable` 並留下半途目錄，接受）。
   - **train run**：
     - Task 5：label set 叫 `authorization` / `code.x` 會得到保留名稱的 `label_set` ref（之後以 `evidence_conflict:` 大聲失敗），`evidence_ref.py:360` 的註解說得過頭；選 ref 一律用 kind + role，不要只看名稱（Task 5 帶給 Task 7 / 12 / 13 / 14 的要求；HANDOVER §8 也記了）；沒有測試 `eval ingest --evidence` 用保留名稱；`read_sections` 建了兩次 `EnvSections(None…)`。
     - Task 6：~~探針以 `sys.path[0]==''` 執行，所以 cwd 裡的 `.egg-info` 會蓋掉真正的 distribution 列~~——**已修**（§2.1 第 47 條）；distribution 列的順序是 `sys.path` / 檔案系統順序，`env.<n>.json` 的位元組與 editable 的勝者可能不同（去重後排序）。其餘：snippet 裡的正規名稱 regex 重複且沒有漂移測試；`packages` dict（後者勝）與 `distributions`（前者勝）可能不一致；`identity.gpus` 沒有用非空名稱測；`ProbeReport.data` 在記憶體裡留著原始 `direct_url`（含憑證），`probe()` 可以丟掉它。
     - Task 7：git root 的掃描有 `reason` 時 `untracked_code=0`（其實是未知，只加總沒有 `reason` 的 root）；開跑前那行寫「(in the snapshot; …)」，即使那個 root 寫失敗了；沒有測試 `low_disk:` ABORT 時不留 `artifacts/code_snapshot`；沒有測試改了程式碼之後 `--resume`（第二個 `code.cwd` 列變成現行）。
     - Task 8：`coderun.warnings` 跟標準庫模組同名（`run.py` 用別名）且位置參數越來越多（考慮 `code_warning_lines` 或摘要物件）；`_differs` 跟隨 symlink、遇到 FIFO 可能卡住；`test_a_new_file_or_nothing_is_no_change` 的 resume 半邊沒斷言 `new.py` 有列；`run.py` 已 1015 行、`_note_changes` 回傳混合的三元組。
     - Task 14：**（最終審查要 triage）** `--evidence` 檔在 `_prepare` 為 claim 雜湊、之後才複製，兩者之間若變了，授權會釘到一個未綁定的 sha（比對 digest → `drift:`）；`OUTPUT_FAKE` 在 `test_run_authorization.py` 與 `test_run_code.py` 各一份（搬進 `helpers`）；沒有測試 `--resume` 上的 C3 拒絕讓 `train.yaml` / `train.log.jsonl` 位元組不變，參數化的 claims 測試沒有 labels 的案例。
   - **trace**：
     - Task 9：~~`from vcp.code import trace` 在 `try` 外面~~——**已修**（§2.1 第 46 條）；~~結束時 `build_string()` 最多三個沒有逾時的 git 子程序~~——**已修**（`_warm` 先暖，第 49 條）；`_ARMED` 的檢查與設定沒有鎖（兩個執行緒 → 兩份 trace），武裝失敗會重試並重印；`trace._plain` / `_resolved` / `_key` 跟 `core.paths.path_key` 重複並多一個 `\\?\` 修正，要搬進 `core.paths`；env 缺失 / 空、或沒有快照時仍寫一份 incomplete 的 trace（保守，待確認）；專案裡的 conda 環境（沒有 `pyvenv.cfg`）的標準庫模組是 `unlisted`；`site_dirs` 的測試近乎套套邏輯。另：同一個 attempt 的兩個非 multiprocessing `Session` 程序可以同時綁 ref 而沒有鎖（§1）。
     - Task 11：兩個內容完全相同的 root 在 `_TRACKED` 撞鍵（fail closed 的誤報 `code_outside:`，訊息裡「add extra_roots」的建議幫不上）——改成每個鍵一個 `TracedRoot` tuple 或把路徑放進鍵；`code_outside:` 只列前 5 個的截斷沒有測試，保留輸入的 `partial:` 路徑也沒有測（記錄既有的 `partial:`）；`writer.py` 的 `type(e)(...)` 丟掉 location，extra-root 的 `not_found:` 沒有 `code_root` 欄位；測試約 20 次呼叫真的 `local_distributions()`（應該在 `proj` 裡 stub，正式環境的成本見 OP3）；一個模組在 import 之後、`snapshot_inputs` 之前被改會通過（spec 層級的限制，§1）。
   - **grade**（Task 10）：traced 的測試從沒把 trace 配上不完整或已刪的快照（mutant 活下來）；以「全部 evidence 列」而不是「現行」比對快照 ref 沒有測試，`git_changed` + 完整快照也沒有；`SNAPSHOT_KIND` 與 note 的鍵是重複的字面（共用常數）；`_trace_complete` 與 attempt 的 trace 過濾重複 3–4 次（公開 `read_trace` / `attempt_traces`），`notes_by_attempt` 重複 `_started_globs` 的迴圈；`_fused_members` 在檢查 `has_record` 之前就解析 `run.yaml`，一個 attempt 的非預期錯誤得到 `declared` 而不是 `commit`；`--verify` 的 `drift=0` 但被取代的舊快照讓 `code=` 掉下去。
   - **writer / process**：`type(e)(...)` 丟掉 location 等見 trace 的 Task 11；`BASIS_INPUT` 的查找在兩個地方重複，且不在 `authorization.__all__`（Task 18）。
   - **authorization**：Task 12：一個 regex 比對測試裡所有依據失敗的原因（改成斷言具體的那一個）。Task 13：`is_superseded` 的 docstring 要說它可能丟 `superseded:`；`_with_fields` 丟掉原本的 fields / `cli_warnings`（加註解）。
   - **vcp code CLI**（Task 15）：匯出的 index 裡 `kept` 旗標不受保護（把 `kept` 改成 false 並刪檔 → WARN 而不是 FAIL）；`diff --id` 對含巢狀 root 的快照會把巢狀的檔報成 `added`；`diff` 只認現在的 configs root；`export` 丟掉 `write_once_stream` 回傳的 sha（應與 `entry.sha256` 比）；測試缺口（bundle 層級的壞 index 路徑；no-write 測試沒斷言 exit code）；`diff --run` 的 attempt 沒進 VERDICT / context；三處 "first 5" 常數（`SHOWN_PATHS`）；`bundle._behind_a_link` 用 `joinpath(*parts)` 組路徑、跟讀檔用的目錄 / `entry.path` 在 Windows 上對 `C:y` 這種段（精心構造的 index）會不一致；index 項在讀不動的目錄裡會是原始的 `PermissionError` ABORT，而走訪是 `code_unavailable:`；configs root 的猜測會拒絕 `--root configs`。
   - **eval / submit**：Task 16：~~`eval status` 人看的行在 provenance 失敗的 run 上漏掉 `code=`~~——**已修**（§2.1 第 49 條）；`SHOWN_PATHS = 5` 在三個 `vcp.code` 模組各一份，加上三份 "first N files" 區塊；`cli_eval` 的 `st.code.get(run, 'declared')` 掩蓋了缺鍵；`report.status()` 約 62 行；`ingest --code` 雜湊快照兩次；測試設定靠 `--labels` 接受 id `code.cwd`。Task 17：~~測試名稱拼錯（`..._wants_acode_snapshot`）~~——**已改**；`_final_entries` 約 56 行；`check_notebook_code` 是 `check_snapshot` → `mismatch` 階梯的第三份複本；`final` 對每個提交重檢查 kernel 快照（沒有每個表的快取）；沒有測試 stage 之後被刪的 run 層級快照 → `final` 的 `code_required`，也沒有測試 probe / baseline 給壞的 `--code`。
   - **backup / gc**（Task 18）：`walk_submission` 的 kernel 快照沒有釘 sha（不見 → `unlisted`）；沒有 gc 測試用真的 `locks/code_snapshot-<id>.lock`。
   - **provenance**（Task 19）：`graph.DECLARED` 重複 schema 的等級字串（改用 `CodeGrade` / `CODE_RANK`）；每份 pinned 快照在一次 build 裡被雜湊兩次（`store.verify` + `ref_intact`，與 §2.2 第 6 條 (b) 同一件）；sync 拒絕的測試直接改 SQLite 的 `attributes_json`。
   - **docs**（Task 20、21）：Task 20：~~quickstart 第 14b 步只檢查 exit 0~~——**已修**（斷言 `status=OK`，§2.1 第 49 條）；真資料測試只比 `runs/` 與 `artifacts/` 兩棵樹；回歸門檻那一列可以列出直接的「every root」「before any write」測試；e2e 測試約 64 行；README 的「walks the whole loop」沒有 train / code verify——**Task 21 已處理**（README 兩份的 demo 句子加了一個包起來的訓練與 `code verify`）。Task 21：`AGENT_SKILLS.md` 沒有改（不在任務的檔案清單；它列的是十個 skill 的觸發時機，`vcp-train-submit-backup` 的觸發描述多了程式碼快照與授權的詞，之後可以同步）。
10. **其他 Wave 1 之外、仍然開放的**：稽核 §11 Wave 1 清單的第 4 項（單一大陣列的選取列存取器）與第 5 項（合成插件端到端、比賽原型遷移）排在 1.0 之後；VCP-024（產物 job 的環境身分）、VCP-025（BenchmarkReceipt 的 schema 與 STOP/GO）、VCP-028（blob 去重與 GC）、VCP-030（`vcp audit lineage`）、VCP-031（plugin SDK）都不在本版範圍。
11. **最終審查（2026-11-04）記下、這一輪不改程式的**（controller 裁決：記錄、留待之後）。每條寫在哪裡、為什麼：
    - **沒有授權時 `write_all` 仍可能 ABORT**（`src/vcp/train/coderun.py` 的 `write_all`、`src/vcp/code/snapshot.py` 的 `write_snapshot`）：不是磁碟滿的 `OSError`（建鎖目錄、重用時的讀取）或 `ArtifactWriter` 的 `exists:` / `not_found:` 會變成 ABORT——乾淨、一個檔都沒寫，但 spec §2 與 §14 第 9 條說沒有授權時快照寫不成只 WARN。之後把它們對應到 `unwritable`（那個 root WARN、訓練照跑），`mismatch:` 保持原樣。這是第 2 條的契約項（exit 類別）。
    - **證據在 claim 雜湊與複製之間可以改變**（`src/vcp/train/run.py` 的 `_prepare` 雜湊、之後 `attach_cli_evidence` 複製）：給了 `--authorization` 時，兩者之間被改的證據檔會被默默接受（事後查得出來：副本的 sha 跟 claim 不同）。之後拿 `attach_cli_evidence` 回傳的 digest 跟 claim 的 sha 比，不同就 `drift:`。
    - **`partial` 的提示與 WARN 不夠**（`src/vcp/code/process.py` 的 `partial` 訊息）：提示寫 `vcp artifact clean`，但 24 小時的寬限期永遠不會移走剛中斷的半途目錄，要寫 `vcp artifact clean --older-than … --apply`；WARN `code_snapshot=partial` 沒有說原因的那一行（哪個 root、哪個 reason）。
    - **`\\?\` 前綴只有 trace 處理**（`src/vcp/code/trace.py` 的 `_plain`，§2.1 第 38 條）：`vcp.core.paths.path_key` 應該自己去掉它，trace 的 `_plain` / `_resolved` / `_key` 才能併回 `core.paths`（第 9 條 trace 小項）。
    - **一般目錄 root 裡的 conda 環境不被當成環境**（`src/vcp/code/scan.py` 的 `pyvenv.cfg` 判斷、`trace._is_venv`）：只認 `pyvenv.cfg`，conda prefix 沒有它，它的檔會被走訪、它的標準庫模組在 trace 裡是 `unlisted`。之後加 `conda-meta/` 當標記（rules 要不要升版要再議）。
    - **解析 inductor 快取目錄可能有副作用**（`src/vcp/code/generated.py` 的 `_torch_inductor`，呼叫 torch 的 `cache_dir()`）：它可能設 `TORCHINDUCTOR_CACHE_DIR` 並建立那個目錄。無害，記下。
    - **每個不同內容一個鎖檔，會累積**（`<data_root>/locks/code_snapshot-<id>.lock`）：從不清；`gc scan` 把 `locks/` 當 `protected`，所以也不會被列成可刪。量大了再決定要不要清。
    - **DRY**：「前 5 個」的常數有六處（`cli_code.SHOWN_IN_REASON`、`bundle` / `process` / `snapshot` 的 `SHOWN_PATHS`、`coderun.NOTICE_PATHS` / `CHANGED_PATHS`）；`check_snapshot` → `mismatch:` 的階梯有四份；trace ref 的過濾有三份。審查沒找到彼此漂移，之後收成一份。
    - **位元組數與 sha 不是同一次讀**（Task 2，`scan.py` 的 `bytes` 取自 `lstat`、sha 取自之後的讀取）：改成邊雜湊邊數；**`export` 丟掉 `write_once_stream` 回傳的 sha**（Task 15，`bundle.py`）：應該跟 `entry.sha256` 比。
    - **探針的 `import torch` 仍會看 cwd**（`src/vcp/train/env.py` 的 PROBE，§2.1 第 47 條）：distribution 的讀取已經不看 `''`，但 `sys.path` 本身沒改，cwd 裡叫 `torch` 的目錄或 `torch.py` 會被當成 torch，`torch` / `cuda` / `cudnn` 欄位就錯。修法是探針一開始就把 `''` 從 `sys.path` 拿掉（只在探針裡，`local_distributions` 在本程序執行，不能改 `sys.path`）。這些欄位不進 `identity`，所以不影響授權。
