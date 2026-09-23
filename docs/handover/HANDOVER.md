# vcp 交接文件（2026-09-21 更新）

給接手開發的人或代理（Codex）。讀完這份就能不靠對話紀錄繼續做。搭配根目錄的 `AGENTS.md` / `CLAUDE.md`（機械鐵則與常用命令）、`README.md`（每層的命令表）。

## 1. 這是什麼

`vcp`（vision contest pipeline）：給 Kaggle / 台灣視覺比賽用的通用框架，八層全部交付（六個子專案 + 不可變產物 + provenance）：

| 子專案 | 層 | 套件 | CLI 群 |
|---|---|---|---|
| 0 / 1 | 骨架 + 資料層 | `src/vcp/core`、`src/vcp/data` | `vcp data import\|validate\|split\|lineage\|export\|audit\|materialize` |
| 2 | 量測層 | `src/vcp/measure` | `vcp eval ingest\|measure\|anchor\|preregister\|judge\|sigma\|status\|report` |
| 4 | 融合層 | `src/vcp/fuse` | `vcp fuse recipe\|build\|ablate` |
| 3 | 訓練層 | `src/vcp/train` | `vcp train run\|upload\|status` |
| 5 | 提交治理 | `src/vcp/submit` | `vcp submit` ×12（init/stage/verify/upload/record/score/sync/final/lock/unlock/status/report） |
| 6 | 備份審計 | `src/vcp/backup` | `vcp backup manifest\|push\|verify\|pull\|status` |
| 7 | Dataset evolution provenance | `src/vcp/provenance` | `vcp data diff`、`vcp provenance rebuild\|sync\|ingest\|impact\|stale\|explain\|status\|verify-index` |
| 稽核 Wave 1a/1b | 不可變產物、access receipt、source audit | `src/vcp/artifact`、`src/vcp/data/access`、`src/vcp/data/source_audit.py` | `vcp artifact create\|show\|verify\|lineage\|status\|relink\|clean`；收據與稽核由 export / train / validate 自動留下 |

起點是賽後報告 `docs/postmortems/2026-08-aidea-marine-debris-detection.md`（§9 藍圖）：public→private 掉分的根因是「只在一個儀器上驗證」「元件準入不一致」「σ_p 太晚估」；框架把這些變成機制（≥2 個互斥驗證集 + 1 個 sealed holdout、護欄先於讀數、預登記 t 門檻、元件準入需 ≥2 個基底、台帳與上傳原子、UTC 時戳）。

## 2. 現況

- 分支：`main` = `origin/main`（GitHub `eric20041027/Vision-contest-pipeline`）。Codex 的 `codex/dataset-evolution-provenance`（0.7.0）與 `codex/postgresql-adaptive-provenance`（0.8.0）已各以一個 PR 合併，tag 打在合併 commit。2026-09-23 另有 Codex 的 PR #21（`codex/provenance-sqlite-gaps-20260923`：SQLite 索引保存 graph gaps，合併後舊索引讀取會 FAIL 並要求 rebuild）尚未合併。
- 版本：`0.9.0`（tag `v0.9.0`，2026-09-23，MINOR：`vcp provenance graph` 把索引畫成 Mermaid 圖，加 skill `vcp-provenance-graph`）；`0.8.1`（tag `v0.8.1`，2026-09-21，PATCH：串流 replay、`python -m vcp`、receipt nonce 加寬、開源門面；五份 provenance 證據齊全後、RSNA 訓練開跑前發）；`0.8.0`（tag `v0.8.0`）= PostgreSQL Adaptive Provenance：未給 `--backend` 仍是 SQLite，PostgreSQL 是 optional、noncanonical、可重建的 derived index；`0.7.0`（tag `v0.7.0`）= Dataset Evolution 與 Incremental Impact Provenance；`0.6.0`（tag `v0.6.0`）= 稽核 Wave 1b-2 source audit；`0.5.0` = Wave 1b-1；`0.4.0` = Wave 1a；規則與發版步驟在 `CHANGELOG.md` 表頭。下一步：稽核 Wave 1c（程式碼快照與授權，VCP-004/006）；provenance 的開放項在 PostgreSQL 後記最後一節（policy v2、full rebuild 後 VACUUM、EXPLAIN、Linux live、1M）。
- 測試：`uv run pytest --cov=vcp` 在 0.9.0 = 1677 passed / 76 skipped，覆蓋率 95.01%（0.8.1 是 1612 / 76 / 94.85%；PostgreSQL 整合案例未配置 service 時 skip；CI 的 ubuntu `postgres` job 有 Docker Compose 的 17.11）。核心環境不裝 torch，project checkpoint 測試在獨立訓練 venv 另跑；ruff 另明列新增 project Python 檔。
- 真資料（本機 `C:/vcp-data`）：RSNA Knee 200-study 子集已匯入為 dataset `rsna-knee`，另有 3-study `rsna-knee-test`；`uv run pytest tests/integration -o addopts="" -q -m realdata` → 9 passed / 3 skipped（marine-debris 未匯入）。
- 環境：Windows 11、`uv` 管 Python 3.12、typer 0.27。本次實查 `uv tool list` 為空，Kaggle 改用 `uvx --from kaggle==2.2.4 kaggle`（profile 已設定，可讀自己的 notebooks）；rclone 1.75.1 官方 portable binary 與 PATH 用法見 RSNA RUNBOOK §9，實測 `rclone_conf=absent`。RSNA 比賽在獨立工作區 `C:/Users/smallfire123123/Desktop/RSNA_Knee_Abnormality_Detection`（自己的 `vcp-data`、`configs`、`projects/rsna-knee`、核心 venv 與訓練 venv），兩個 venv 自 2026-09-21 起 editable 指向釘在 tag `v0.8.1` 的 detached worktree `Vision-contest-pipeline-v081-rsna`（規則見 `vcp-release-and-environments` skill）；repo 內的 `projects/rsna-knee/.venv` 仍指 main，是開發用。torch 2.11.0+cu128。
- Skills：`.claude/skills/` 十個（`vcp-orientation` 入口 + 九個；`vcp-provenance-graph` 是 0.9.0 加的），鏡射到 `.agents/skills/`；路由與時機在 README「Working with an agent」一節與 CLAUDE.md / AGENTS.md 的「文件」。

## 3. 程式碼地圖

```
src/vcp/core      time（唯一時鐘）errors（VERDICT 狀態）log（VERDICT 行 / jsonl log）paths（DatasetPaths）
                  hashing config（YAML ↔ pydantic、is_true）proc（子程序 runner + redact）atomic（write_once 原語）
src/vcp/data      schema tasks（任務登記表）dataset split（plan、assert_plan_matches）lineage
                  importers/ exporters/ audit/ materialize/ dicomio
src/vcp/measure   schema runs（run.yaml、FUSE_FRAMEWORK）predictions converters/ metrics/ ingest
                  measure（護欄 → 讀數）anchors ledger（三個台帳檔名）prereg judge sigma stats report plugins
src/vcp/fuse      schema recipes（配方進 git）fusers/（wbf/mean/rank_mean）members build（fuse.json）ablate
src/vcp/train     schema records（train.yaml + train.log.jsonl）env checkpoints upload run session reader status
src/vcp/submit    schema profile（submit.yaml）ledger（submissions.jsonl）timewin guards pairing gate
                  writers/（scores_csv/coco_results/csv_boxes）platforms/（manual/kaggle）stage actions sync final report
src/vcp/backup    schema ledger manifest evidence（證據圖）dest（本機 / rclone）push verify pull status
src/vcp/artifact  schema（pydantic 模型、check_file_name）ledger（supersession.jsonl 讀寫）store（load/reuse/verify）
                  writer（ArtifactWriter：claim/write/commit）lineage（chain/successors/head/forks）clean（scan/clean）
src/vcp/provenance schema/policy/diff（dataset_diff artifact）graph/views（full oracle）backend（SQLite adapter / optional PostgreSQL）postgres（v1 normalized derived index）strategy（immutable adaptive policy）index（SQLite cache）
src/vcp/cli*.py   每層一個 typer app（含 cli_artifact.py 的 `vcp artifact` 群：create/show/verify/lineage/status/
                  relink/clean）；cli_common.run_command 統一 VERDICT / exit code / --json / context
projects/rsna-knee  prepare/train/predict/bundle CLI、rsna_knee 共用轉換與模型、RUNBOOK；比賽程式只在這裡
tests/            unit/<layer>、integration（真資料）、helpers.py、submit_fixtures.py、backup_fixtures.py
configs/          datasets/<name>/（dataset.yaml、splits/、prereg/、fuse/、submit.yaml、backup/…）進 git
```

Dataset provenance 的操作、修復、status 語意與基準命令見
`docs/guides/DATASET_EVOLUTION_PROVENANCE.md`；設計與執行計畫為
`docs/superpowers/specs/2026-09-13-vcp-dataset-evolution-provenance-design.md` 與
`docs/superpowers/plans/2026-09-13-vcp-plan11-dataset-evolution-provenance.md`。真實驗證只在 temporary
metadata copy 寫 diff/index，live RSNA roots 僅讀取。
完整實作範圍、review 修正、驗證數字與下一位 agent 的接手清單見
`docs/handover/DATASET_EVOLUTION_PROVENANCE_HANDOFF.md`。

PostgreSQL provenance 的 optional install、libpq service 安全設定、all flags、repair、opt-in harness 與
benchmark/calibration/held-out命令見 `docs/guides/POSTGRESQL_PROVENANCE.md`；pending evidence record 見
`docs/benchmarks/postgres-provenance-v1.md`，完整實作/交接界線見
`docs/handover/POSTGRESQL_ADAPTIVE_PROVENANCE_HANDOFF.md`。本機自 2026-09-14 起有原生 PostgreSQL 17.11
（使用者排程工作 `VCP-PostgreSQL-17.11`、只聽 `127.0.0.1:55432`、service `vcp-pg17-evidence`；佈建紀錄在
`C:/vcp-data/services/postgresql-17.11-vcp/`，憑證檔只能以路徑指給 `PGSERVICEFILE` / `PGPASSFILE`，不讀
不抄）；live integration v3 PASS 51/51、正式 calibration v2（policy `postgres-adaptive-v1-9f4e58346529`）與正式
six-method v1（2026-09-18，1K–100K，648 列全 ok、parity 648/648、無交會點；adaptive 在 1K 因絕對 RMSE 信心帶
全部落入 FULL，見 Plan 12 後記 §5）與正式 held-out v1（2026-09-20，seeds 20261001/20261002，324 列全 ok、
overlap 0，aggregate gate PASS p50 1.018 / p95 1.017，every-scenario diagnostic 在 1K FAIL）與 real RSNA six-method
（2026-09-20，3 個真實 transition、18 列全 ok、parity 18/18；adaptive 在 98.7% 變更的 8.8K 圖上選錯 FULL）都已保存，
acceptance 表已無空格（calibration v1 與 1M memory gate v2–v4 被 RAM 護欄中止的紀錄保留）。

十二個變異軸都是登記表（任務、匯入器、匯出器、解碼器、切分策略、稽核、轉換器、指標、σ_p 方法、融合器、輸出格式、平台）：加一種形態 = 加一個登記項，不改 schema、不改 CLI；比賽自己的指標 / 格式用 `--plugin projects.<contest>.metrics`。

## 4. 鐵則與慣例（違反即審查 FAIL）

1. 取時只用 `vcp.core.time.utc_now()/stamp()/parse_stamp()`（ruff TID251 擋其他時鐘）。
2. 每個 CLI 命令以 `VERDICT cmd=<group>.<name> status=OK|WARN|FAIL|ABORT k=v…` 收尾；exit 0/0/1/2；`--json` 時 JSON 到 stdout、VERDICT 到 stderr；永不互動提問；不用 Click 層的參數驗證（`min=` 之類）——範圍檢查在函式層，才有 VERDICT。
3. 錯誤對應：`ValidationFailed` / `IntegrityError` / `PlatformError` = FAIL；`VcpError` / `PlanMismatchError` / `RegistryError` = ABORT；訊息以 `reason=` 字彙開頭（`not_found:`、`exists:`、`mismatch:`、…）；`VcpError.fields` 只放機器可讀鍵。
4. 隱私：vcp 永不讀 / 寫 / 驗 / 記憑證；沒有 token 選項或憑證欄位；子程序繼承環境但不記錄環境；第三方 CLI 的每個位元組落地前經 `vcp.core.proc.redact`。
5. 台帳只增（`exclude_none`），卡「換寫留痕」，plan / 預登記 / 配方 / 清單寫了不改（要改就換 id）。
6. venv 隔離：核心一個 venv（`uv sync`）；訓練框架各自 venv 以 editable 裝 vcp。
7. `src/vcp` 不出現比賽名；檔案 utf-8 / LF；ruff line-length 100；覆蓋率 ≥ 80%；測試永不碰真資料根（`roots` fixture）。
8. commit 一律 `type(scope): 說明`（繁中說明可），一個 commit 一件事；不用 `git add -A`。

## 5. 文件地圖

- 設計 spec：`docs/superpowers/specs/`（每層一份；每份最後的「補充決定」一節是實作期的定案，**以程式碼為準**）。
- 實作計畫：`docs/superpowers/plans/<date>-vcp-planN-*.md`（執行當時的程式碼；已被後來修正的地方在計畫末的「執行期修正」一節或後記）。
- 後記：`docs/superpowers/plans/<date>-vcp-planN-followups.md`——裁決、審查發現、待辦與處置；**開放的待辦都在各後記的最後一節**。
- 比賽膠水：`projects/rsna-knee/RUNBOOK.md`（實際步驟與讀數）、`DESIGN.md`（設計與裁決），以及歷史下載腳本。
- 新手視覺導覽：`docs/guides/VCP_VISUAL_GUIDE.md`（六層架構、完整生命週期、證據地圖、VERDICT、人機分工與第一次跑通）。
- 命令參考：`docs/reference/cli.md`（原 README 主體，2026-09-15 開源整理時移出，內容未改）。入門範例 `examples/quickstart.py` 由 CI 每次跑，確保 README 的流程不會腐化。
- 專案 skill：`.claude/skills/` 與 `.agents/skills/`。`vcp-running-contests` 是完整比賽、使用者介紹與人機交接的入口；新賽事、資料層與通用登記項再分別使用 vcp-contest-onboarding、vcp-data-pipeline、vcp-extend-registry。

## 6. 開放的待辦（依優先序）

1. **Hygiene C 已完成**：兩個 commit `6a4cc58`、`12a9cd4`；處置與驗證見 Plan 6 後記 §9。
2. **接續修復已完成**：遺失 `fuse.json` 時拒絕不完整紀錄，`--replace` 重建所有宣告子集（`7c31c3d`，Plan 4 §9）；eval / fuse / train / submit 的 26 個命令全部採用 `run_command(context=)`（Plan 7 §8）。歷史「未做」清單已逐層核對，處置在各後記最新節；不要依舊節再做一遍。
3. **效能回合**（都刻意延後）：Plan 2c §5-2（materialize 的 `is_dir`/`stat` 兩百萬次）、Plan 3 §5-4（seg 指標配置、護欄重算、judge 載兩次）、Plan 7 §5-6。
4. **設計層級**：Plan 3 §5-12 的跨程序鎖（兩個程序同時 append 同一 `reading_id`）、Plan 5 checkpoint TOCTOU 仍延後；`Manifest.data_root` 刻意保留作人讀來源標記。
5. **RSNA Knee 已實跑本機基準**：200 study 固定切分、PNG256、兩個種子訓練、預登記 / macro AUC / judge、平均融合消融、test profile、離線 bundle 已完成；讀數與命令見 `projects/rsna-knee/RUNBOOK.md`。seed 43 與融合均未準入，第一個 seed 42 是 baseline。**尚未完成外部里程碑**：指定私有 Kaggle dataset 上傳待核准，notebook 執行 / 提交 / scored / sealed final 未發生；備份目的地未提供。不要把手冊中待執行命令當成已完成，也不要先解封 holdout。
6. **Windows 命令解析**：裸 `python` 可啟動到 venv 以外，即使 `--venv` 探針正確；專案已用絕對 interpreter 完成訓練，通用解析修復另列 Plan 5 §10，與效能回合分開。
7. **本機證據已備份**：`knee-local-v1` 結論 `all`、51 項驗證成功；兩份權重、notebook bundle、Git source bundle 均已存 `C:/vcp-backup/rsna-knee`。這是同機副本；異機撤離仍待目的地，先將權重 upload 到該遠端後再產新清單，勿把指著 C 槽的 remote_copy 當異機證據。
8. **PostgreSQL Adaptive Provenance 外部驗收（2026-09-20 五份證據齊全；接手點是 policy v2 與 Linux CI）**：live integration 已過（v3）。依序：(1) production graph loader 的 canonical replay 改串流——已完成（`open_dataset_diff`：先整檔驗證、再逐筆重放，API 與 exact parity 不變；Codex 2026-09-14 量到 1M 場景在 `baseline_graph_build` 把約 41 萬筆 diff 事件全部常駐，peak 5.7 GiB）；(2) 探路矩陣已跑完（`docs/benchmarks/postgres-provenance-exploratory-v1.md`：1K–100K 五方法各 1 次，8.5 小時，parity 全過，incremental 在每個非零 ratio 都快於 full）；(3) 1M 已於 2026-09-15 裁決移出正式矩陣（Plan 12 後記 §1；每 split 108 場景 / 612 次重複），calibration 已完成（2026-09-16，14.5 小時，policy `postgres-adaptive-v1-9f4e58346529`，12/12 切片沒有交會點；發布時撞到生產者／消費者 schema 漂移，處置見 evidence record 的發布註記與 Plan 12 後記 §4）；(4) six-method 含 adaptive 已完成（2026-09-18，`docs/benchmarks/postgres-provenance-six-method-v1.json`：648 列全 ok、parity 648/648、crossover 12/12 `not_observed`；adaptive 在 10K/100K 永遠 INCREMENTAL、在 1K 因絕對 RMSE 信心帶永遠 FULL——policy 凍結不改，Plan 12 後記 §5；PostgreSQL full rebuild 100K 75–140 s、重建後 dead tuples 使 storage 約 2 倍），real RSNA track 已完成（2026-09-20，`docs/benchmarks/postgres-provenance-real-rsna-v1.json`：r3→r4→r5→r6 三個 transition、18 列全 ok、parity 18/18、真實變更比例 100% / 98.7% / 0%；adaptive FULL / FULL / NO_OP，第二個 transition 選錯慢 1.79 倍——信心帶問題在真實資料上重現，Plan 12 後記 §7）；(5) 另一 process 的 frozen held-out 已完成（2026-09-20，`docs/benchmarks/postgres-provenance-heldout-v1.json`：324 列全 ok、overlap 0、aggregate gate PASS 1.018 / 1.017、every-scenario diagnostic 36/92 > 1.05 全在 1K 的信心帶問題；執行中被使用者誤啟動的訓練程序中止一次，一個場景因汙染跡象重量，看管程式改成只認命令列帶 bench 標記的 python——見 evidence record §Held-out 正式結果與 Plan 12 後記 §6）。長跑用 `C:/vcp-data/bench/supervise.py`（不進 repo）看管：機器空閒才跑、遊戲或低記憶體即停並丟棄進行中場景；主機閒置時可用記憶體可能只剩 5 GB（驅動 nonpaged pool 洩漏，重開機可解）。保存 machine-readable outputs、PostgreSQL numeric server version、policy ID / 精確 policy SHA、parity 與 honest p50/p95 gate 結果；不得用 skips、offline doubles 或 held-out refit 替代。Linux CI（Docker Compose host）證據仍缺。

## 7. 開發流程（這個 repo 一直這樣做）

1. **brainstorm → spec**：大改動先寫 `docs/superpowers/specs/<date>-<topic>-design.md`（架構、資料模型、CLI 表、錯誤字彙、測試策略、驗收、不在範圍），使用者核可後才寫計畫。
2. **plan**：`docs/superpowers/plans/<date>-<topic>.md`，每個任務含完整程式碼與測試、逐步（寫失敗測試 → 跑 → 實作 → 跑 → commit）；**計畫 markdown 不跑 `ruff format`**。
3. **執行**：一個任務一個實作者（TDD），每個任務一次審查（spec 符合度 + 品質），全部完成後一次全分支審查（跨任務接縫、隱私、錯誤處理、測試品質），最多一輪修正 + 一次範圍限定再審；每個裁決記進後記（「裁決：決定 — 依據 — 代價」）。
4. **收尾**：後記（結果、裁決、審查發現、待辦）、spec 補充決定、README / AGENTS.md、全套測試、合併到 main、push。
5. **小修（hygiene）**：不寫 spec / plan，寫一份 brief（決定 + 測試 + commit 分組），一個實作者 + 一個審查者，後記記處置。
6. **發版**：合併後若這批變更動了產物 / 台帳內容或 CLI 契約就 bump MINOR，否則 PATCH；步驟在 `CHANGELOG.md` 表頭（改 `__version__` → 加 CHANGELOG 條目 → 測試 → commit → tag → push 分支與 tag）。一個 release 可以包好幾批工作；不必每次合併都發版，但發版前的產物都會帶著 `+g<commit>` 而不是裸版本號，所以不會被誤認成 release。

## 8. 已知陷阱

- Windows：測試重寫台帳 / 卡一律 `write_text(..., newline="\n")`（否則 CRLF 讓 sha 對不上）；`Path.is_absolute()` 對另一平台的絕對路徑回 False（跨機器路徑用 `PureWindowsPath` / `PurePosixPath` 兩邊都問）。
- `uv run pytest --cov=vcp -q` 不印 passed 數（`addopts` 已含 `-q`）；要數字就不要再加 `-q`。
- 真 rclone / kaggle 不在測試裡：備份層的 `vcp.backup.dest.RCLONE` 常數可 monkeypatch 到假腳本；提交層的 `kaggle_command` 可指到假腳本。
- 計畫裡的測試輸入要先問「這個輸入真的會造成那個條件嗎」——歷史上三次實作者停下來（NEEDS_CONTEXT）都是計畫的測試錯、程式對。
- 台帳是快照：備份清單寫下後台帳還會長，`push` 推清單那一刻的前 N 位元組、`verify` 用前綴 sha、`pull` 視長大為已有。
- `--forget-remote` 要整份清單在目的地驗過才刪憑證；帶 `present=false` 條目的清單永遠不能 forget。

## 9. 交接時的數字

見 `git log -1`、`uv run pytest --cov=vcp`（不加 `-q`）與各後記最後一節；本文件不重複數字，以免過時。
