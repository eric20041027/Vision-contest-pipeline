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
  3. `uv run pytest --cov=vcp`（`tests/unit/test_package.py` 會擋住 `__version__`、安裝 metadata 與本檔最新條目三者不一致）與 `uv run ruff check . && uv run ruff format --check .`。
  4. commit（`chore(release): vx.y.z`）、fast-forward 到 `main`、`git tag -a vx.y.z -m "vcp x.y.z"`、`git push origin main vx.y.z`。
- 產物不可改寫（專案鐵則）：舊版本寫下的 `vcp_version` 永遠留著，本檔是它們的解析路徑。

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
