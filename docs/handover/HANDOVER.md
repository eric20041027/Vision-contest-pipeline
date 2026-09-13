# vcp 交接文件（2026-09-07）

給接手開發的人或代理（Codex）。讀完這份就能不靠對話紀錄繼續做。搭配根目錄的 `AGENTS.md` / `CLAUDE.md`（機械鐵則與常用命令）、`README.md`（每層的命令表）。

## 1. 這是什麼

`vcp`（vision contest pipeline）：給 Kaggle / 台灣視覺比賽用的通用框架，六個子專案全部交付：

| 子專案 | 層 | 套件 | CLI 群 |
|---|---|---|---|
| 0 / 1 | 骨架 + 資料層 | `src/vcp/core`、`src/vcp/data` | `vcp data import\|validate\|split\|lineage\|export\|audit\|materialize` |
| 2 | 量測層 | `src/vcp/measure` | `vcp eval ingest\|measure\|anchor\|preregister\|judge\|sigma\|status\|report` |
| 4 | 融合層 | `src/vcp/fuse` | `vcp fuse recipe\|build\|ablate` |
| 3 | 訓練層 | `src/vcp/train` | `vcp train run\|upload\|status` |
| 5 | 提交治理 | `src/vcp/submit` | `vcp submit` ×12（init/stage/verify/upload/record/score/sync/final/lock/unlock/status/report） |
| 6 | 備份審計 | `src/vcp/backup` | `vcp backup manifest\|push\|verify\|pull\|status` |

起點是賽後報告 `docs/postmortems/2026-08-aidea-marine-debris-detection.md`（§9 藍圖）：public→private 掉分的根因是「只在一個儀器上驗證」「元件準入不一致」「σ_p 太晚估」；框架把這些變成機制（≥2 個互斥驗證集 + 1 個 sealed holdout、護欄先於讀數、預登記 t 門檻、元件準入需 ≥2 個基底、台帳與上傳原子、UTC 時戳）。

## 2. 現況

- 分支：`main` = `origin/main`（GitHub `eric20041027/Vision-contest-pipeline`），工作樹乾淨；沒有未合併的分支。
- 版本：`0.5.0`（tag `v0.5.0`，2026-09-12）= 稽核 Wave 1b-1（角色範圍存取 `DatasetAccess`、`access_receipt` 產物、provenance 三級與四個強制點，VCP-001/003）；`0.4.0` = Wave 1a（不可變產物層 `vcp artifact` + `core/atomic.write_once`）；`0.3.0` = Wave 0（prereg SHA 綁定、foreign 狀態刷新、回歸門檻）；`0.2.0` 是第一個有 tag 的 release。規則與發版步驟在 `CHANGELOG.md` 表頭。`0.2.0` 之前 240 個 commit 都宣告 `0.1.0` 且無 tag——RSNA 早期產物裡的 `"vcp_version": "0.1.0"` 回推不到單一 commit；0.2.0 起產物記 `版本+g<commit>[.dirty]`。下一步是 Wave 1b-2（SourceAudit 與選取列存取器，VCP-002）、1c（程式碼快照與授權，VCP-004/006）；稽核文件副本在 `docs/audits/`。
- 測試：`uv run pytest --cov=vcp`，覆蓋率約 96.63%；實際最新數字見 RSNA RUNBOOK 驗證紀錄。核心環境不裝 torch，project checkpoint 測試在獨立訓練 venv 另跑；ruff 另明列新增 project Python 檔。
- 真資料（本機 `C:/vcp-data`）：RSNA Knee 200-study 子集已匯入為 dataset `rsna-knee`，另有 3-study `rsna-knee-test`；`uv run pytest tests/integration -o addopts="" -q -m realdata` → 9 passed / 3 skipped（marine-debris 未匯入）。
- 環境：Windows 11、`uv` 管 Python 3.12、typer 0.27。本次實查 `uv tool list` 為空，Kaggle 改用 `uvx --from kaggle==2.2.4 kaggle`（profile 已設定，可讀自己的 notebooks）；rclone 1.75.1 官方 portable binary 與 PATH 用法見 RSNA RUNBOOK §9，實測 `rclone_conf=absent`。訓練 venv 為 `projects/rsna-knee/.venv`，torch 2.11.0+cu128。

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
src/vcp/cli*.py   每層一個 typer app（含 cli_artifact.py 的 `vcp artifact` 群：create/show/verify/lineage/status/
                  relink/clean）；cli_common.run_command 統一 VERDICT / exit code / --json / context
projects/rsna-knee  prepare/train/predict/bundle CLI、rsna_knee 共用轉換與模型、RUNBOOK；比賽程式只在這裡
tests/            unit/<layer>、integration（真資料）、helpers.py、submit_fixtures.py、backup_fixtures.py
configs/          datasets/<name>/（dataset.yaml、splits/、prereg/、fuse/、submit.yaml、backup/…）進 git
```

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
- 專案 skill：`.claude/skills/` 與 `.agents/skills/`。`vcp-running-contests` 是完整比賽、使用者介紹與人機交接的入口；新賽事、資料層與通用登記項再分別使用 vcp-contest-onboarding、vcp-data-pipeline、vcp-extend-registry。

## 6. 開放的待辦（依優先序）

1. **Hygiene C 已完成**：兩個 commit `6a4cc58`、`12a9cd4`；處置與驗證見 Plan 6 後記 §9。
2. **接續修復已完成**：遺失 `fuse.json` 時拒絕不完整紀錄，`--replace` 重建所有宣告子集（`7c31c3d`，Plan 4 §9）；eval / fuse / train / submit 的 26 個命令全部採用 `run_command(context=)`（Plan 7 §8）。歷史「未做」清單已逐層核對，處置在各後記最新節；不要依舊節再做一遍。
3. **效能回合**（都刻意延後）：Plan 2c §5-2（materialize 的 `is_dir`/`stat` 兩百萬次）、Plan 3 §5-4（seg 指標配置、護欄重算、judge 載兩次）、Plan 7 §5-6。
4. **設計層級**：Plan 3 §5-12 的跨程序鎖（兩個程序同時 append 同一 `reading_id`）、Plan 5 checkpoint TOCTOU 仍延後；`Manifest.data_root` 刻意保留作人讀來源標記。
5. **RSNA Knee 已實跑本機基準**：200 study 固定切分、PNG256、兩個種子訓練、預登記 / macro AUC / judge、平均融合消融、test profile、離線 bundle 已完成；讀數與命令見 `projects/rsna-knee/RUNBOOK.md`。seed 43 與融合均未準入，第一個 seed 42 是 baseline。**尚未完成外部里程碑**：指定私有 Kaggle dataset 上傳待核准，notebook 執行 / 提交 / scored / sealed final 未發生；備份目的地未提供。不要把手冊中待執行命令當成已完成，也不要先解封 holdout。
6. **Windows 命令解析**：裸 `python` 可啟動到 venv 以外，即使 `--venv` 探針正確；專案已用絕對 interpreter 完成訓練，通用解析修復另列 Plan 5 §10，與效能回合分開。
7. **本機證據已備份**：`knee-local-v1` 結論 `all`、51 項驗證成功；兩份權重、notebook bundle、Git source bundle 均已存 `C:/vcp-backup/rsna-knee`。這是同機副本；異機撤離仍待目的地，先將權重 upload 到該遠端後再產新清單，勿把指著 C 槽的 remote_copy 當異機證據。

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
