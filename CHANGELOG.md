# Changelog

vcp 的每個 release 一條，最新在最上面。格式依 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，版本號依 [SemVer](https://semver.org/lang/zh-TW/)，停在 `0.x` 直到稽核的 P0 provenance substrate 落地（見「版本規則」）。

## 版本規則

- **MINOR（`0.N.0`）**：任何會改變**寫進產物或台帳的內容**（run.yaml、train.yaml、fuse.json、stage.json、各 `*.jsonl` 台帳、backup manifest 的欄位或語意），或改變 **CLI 契約**（命令與選項、VERDICT 欄位、exit code、`reason=` 字彙、登記表項目）的變更。讀舊產物的人靠這個數字判斷「欄位的意思有沒有變」。
- **PATCH（`0.N.P`）**：不改寫入位元組、不改契約的修正——bug、訊息措辭、效能、測試、文件、內部重構。
- **`1.0.0`**：留給 2026-09-11 稽核的 Wave 1（role-scoped access、immutable artifact writer、access receipt、code snapshot）落地之後——屆時產物契約才是可以對外承諾的契約。
- **產物記的 `vcp_version` 是 build string**，不只是版本號：`<version>`（已發版的 wheel，或 git 未追蹤的副本）、`<version>+g<40 hex commit>`（從 checkout 執行）、`<version>+g<commit>.dirty`（該 checkout 的 repo 有未提交變更——刻意過度回報，因為 commit 已不能完整描述跑過的程式碼）。`vcp version` 印同一字串；`vcp.core.build.parse_build_string` 解析它。
- **發版步驟**（一個 commit 一個 tag）：
  1. 改 `src/vcp/__init__.py` 的 `__version__`——它是唯一來源，`pyproject.toml` 以 `[tool.hatch.version]` 動態讀它，`uv.lock` 不記版本字面值。
  2. 在本檔最上方加一條 `## [x.y.z] - YYYY-MM-DD`，列 Added / Changed / Fixed / Removed 與影響的層。
  3. `uv sync --reinstall-package vcp`（editable 安裝的 metadata 不會因 `__init__.py` 改動自動重建），再 `uv run pytest --cov=vcp`（`tests/unit/test_package.py` 會擋住 `__version__`、安裝 metadata 與本檔最新條目三者不一致）與 `uv run ruff check . && uv run ruff format --check .`。
  4. commit（`chore(release): vx.y.z`）、fast-forward 到 `main`、`git tag -a vx.y.z -m "vcp x.y.z"`、`git push origin main vx.y.z`。
- 產物不可改寫（專案鐵則）：舊版本寫下的 `vcp_version` 永遠留著，本檔是它們的解析路徑。

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
