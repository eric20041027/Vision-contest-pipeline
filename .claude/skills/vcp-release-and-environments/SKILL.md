---
name: vcp-release-and-environments
description: Use when bumping or tagging a vcp version, deciding PATCH vs MINOR, setting up or repointing a contest's virtualenvs and tagged worktree, working on the framework while a training or benchmark run is live, opening a branch, PR or CI run in this repository, or writing commit messages here.
---

# 發版、環境與 git 慣例

## 版本規則（CHANGELOG 表頭是權威）
- SemVer 停在 `0.x`。**MINOR**：寫進產物／台帳的內容或語意改了，或 CLI 契約改了（命令、選項、VERDICT 欄位、exit code、`reason=` 字彙、登記項）。**PATCH**：其餘（bug、訊息、效能、測試、文件、內部重構）。`1.0.0` 留給稽核 Wave 1 落地。
- 「登記項改變」只指 `src/vcp` 內建登記表；`projects/<contest>/` 以 `--plugin` 登記的指標、轉換器、融合器等不動框架，**不需要發版**，也不算 MINOR。
- `src/vcp/__init__.py` 的 `__version__` 是唯一來源；產物寫 build string `X.Y.Z[+g<commit>[.dirty]]`。發版前的產物帶 `+g<sha>` 是合法的，不必為了「好看」發版；為比賽開跑前發一個 PATCH 讓產物寫乾淨版號是可以的。

## 發版四步（一個 commit 一個 tag）
1. 改 `__version__`；**同步改 `tests/unit/test_package.py` 的 `EXPECTED_CANDIDATE_VERSION` 與 Claude Code plugin 的 `.claude/.claude-plugin/plugin.json` `version`**（別的專案靠它收到新版 skill）。
2. `CHANGELOG.md` 最上方加 `## [x.y.z] - YYYY-MM-DD`，寫 MINOR/PATCH 的理由與 Added / Changed / Fixed。
3. `uv sync --frozen --reinstall-package vcp`（editable metadata 不會自動更新）→ `uv run pytest --cov=vcp`（`test_package` 擋三者不一致）→ `uv run ruff check . && uv run ruff format --check .`。
4. commit `chore(release): vx.y.z` → PR → CI 綠 → merge → 在**合併 commit** 上 `git tag -a vx.y.z -m "vcp x.y.z"` → `git push origin vx.y.z`。main 有 branch protection（三個 required checks、strict），所以走 PR。

## 環境拓樸（一場比賽一個釘版 worktree）
- **main checkout**（`Vision-contest-pipeline/`）只做開發；它的 `.venv` 不拿來跑比賽。
- **比賽 worktree**：`git worktree add --detach ../Vision-contest-pipeline-v<XYZ>-<contest> v<X.Y.Z>`，裡面 `uv sync --frozen [--extra dicom]` 產生自己的 `.venv/Scripts/vcp.exe`。現有例：`…-v081-rsna`（tag v0.8.1）、`…-v060-rsna`、`…-wave1a-rsna`。
- **比賽工作區的 venv**（核心 + 訓練，各自 Python 3.12.14）以 editable 指向那個 worktree：`uv pip install --python <venv>/Scripts/python.exe --no-deps -e "<worktree>[dicom]"`；`--no-deps` 只換 vcp 一個套件，torch 等不動。訓練與量測一律從該 worktree 或工作區的 `vcp.exe` 啟動。
- 升版：新 worktree at 新 tag → `uv sync` → 重跑上面兩行 `uv pip install` → 改工作區 `ENVIRONMENT.md` / `AGENTS.md` 的 `$framework` 路徑並 commit。舊 worktree 留著，直到沒有 run 依賴它。
- 量測 venv 凍結後禁 install；訓練 venv 只在建環境時裝套件，之後 `uv pip freeze` 進 `requirements-*.txt`。

## 訓練或量測進行中的禁區
- 不在被 editable 指到的 checkout 上 `git pull / checkout / merge`，不 `uv sync --reinstall-package`（`vcp.exe` 被鎖會失敗，且磁碟上的程式碼會在執行中被換掉）。開發一律在另一個 worktree。
- benchmark runner 釘住原始檔雜湊：該 worktree 任何檔改動都會讓續跑被拒。
- 別的專案的訓練會吃掉記憶體：看管程式（`C:/vcp-data/bench/supervise.py`）會先停 benchmark；反過來跑訓練前先確認沒有 benchmark 在跑。

## git 與 PR 慣例
- 每個任務一個 worktree（`.claude/worktrees/<name>`，branch 從 `main`）；合併後 `git worktree remove` + `git branch -d`；移除前先停掉 cwd 在該 worktree 的背景程序。
- commit `<type>: <description>`（feat / fix / refactor / docs / test / chore / perf / ci），內文寫理由；**不加 Claude co-author trailer**（2026-09-15 起的專案決定）。
- 永不 `git add -A`（明列檔案）、永不裸 `git stash`、不對 markdown 跑 `ruff format`、Windows 產生的檔要 LF（`git diff --check`）。
- PR 描述：摘要 + 測試計畫；docs-only PR 只有 3 個 check（沒有 `postgres` job）。CI 的 flaky 錯誤先看是不是同一秒撞號類的既知問題，再 rerun。
- 測試永不碰真實資料根（用 `roots` fixture 與 `tests/helpers.py`）；覆蓋率門檻 80%。

## 常見錯誤
- 只改 `__version__` 不改 `EXPECTED_CANDIDATE_VERSION` 或 CHANGELOG → `test_package` 紅；不改 `plugin.json` 的 `version` → `test_skills_plugin` 紅。
- 在訓練跑的時候把 main 合併進被 editable 指到的 checkout（2026-09-21 發生過一次，紀錄在 HANDOVER）。
- 用 `Vision-contest-pipeline/projects/rsna-knee/.venv`（repo 內的專案 venv）當比賽工作區的 venv：它指 main。
