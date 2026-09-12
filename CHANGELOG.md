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

## [0.4.0] - 2026-09-12

稽核（2026-09-11）的 **Wave 1a**：不可變產物層——VCP-005（產物路徑可被覆寫，破壞不可變證據）與 VCP-007（ID、seed、輸出缺少共同 contract）。MINOR 的理由：新命令群 `vcp artifact`、新產物形態 `artifacts/<kind>/<id>/manifest.json` 與 `supersession.jsonl`、新 `reason=` 字彙（`partial:`、`unsafe_path:`、`reserved_name:`、`closed:`、`drift:`、`spec_mismatch:`）。

### Added
- **不可變產物**（`vcp.artifact`）：`ArtifactWriter.create(spec, data_root=…)` 以 `os.mkdir` 獨佔搶 `<data_root>/artifacts/<kind>/<id>/`，`spec.json` 記 open 時的宣告，每個檔經 `write_once`，`manifest.json` 最後寫 = commit 點（沒有它就不是產物）；例外離開留半途目錄與經 redact 的 `failure.json`；writer 不刪任何東西。`ArtifactSpec.id_pattern` 的具名群組必須等於同名欄位（RSNA「id 說 s42、CLI 收 seed 43」在 open 就擋）；`inputs` 在 open 雜湊、commit 重驗（`drift:`）。`store.reuse` 要完整 spec 逐欄相等（`notes` 除外）；`supersedes` 在 open 查舊產物存在且已 commit、commit 驗逐檔 sha 並記舊 manifest sha、之後 append `supersession.jsonl`；`lineage` / `head` 允許分叉但 WARN；`verify` 四項（mismatch / missing / extra / unlinked）；`relink` 補台帳缺列；`clean` 只移除超過寬限期的半途目錄與 `.tmp`。
- **`vcp artifact create|show|verify|lineage|status|relink|clean`**（`cmd=artifact.<name>`；失敗時保留 `kind=` / `id=`）。
- **`vcp.core.atomic`**：`write_once` / `write_once_text` / `write_once_stream`（同目錄 `.<name>.<nonce>.tmp` → fsync → 目標不存在才 `os.replace`；不是跨程序鎖）。`core/paths.py` 新增 `artifacts_root` / `artifact_dir`，`check_relative_path` 從備份層搬來；`core/config.py` 新增 `dump_yaml_text`。
- Release 回歸門檻多一列（`wave 1a (VCP-005, VCP-007)`）；真資料整合測試 `tests/integration/test_artifact_status.py`（唯讀）。

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
