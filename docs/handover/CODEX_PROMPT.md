# 給 Codex 的接續開發 prompt（2026-09-21）

把下面整段貼給 Codex（repo 根目錄開啟）。它假設 Codex 能讀 repo、跑 `uv`、跑測試、開 worktree / 分支與 commit。2026-09-07 的舊版所列的第一、第二階段（待辦清理、RSNA Knee 膠水）都已完成；RSNA 比賽現在在獨立工作區推進，不在這個 repo 裡。

---

你接手的是 `vcp`（vision contest pipeline，Python 3.12 / pydantic v2 / typer / pytest / ruff，MIT 開源），一個給 Kaggle 與台灣視覺比賽用的通用框架。八層（資料、量測、融合、訓練、提交治理、備份審計、不可變產物、dataset evolution provenance 含 optional PostgreSQL adaptive backend）都已交付並合併到 `main`，版本 `0.8.1`（tag `v0.8.1`），全套測試綠（1612 passed / 76 skipped，覆蓋率 94.85%）。開始前依序讀：

1. `AGENTS.md`（機械鐵則、路徑、常用命令——每一條都是硬性規定）與 `.agents/skills/vcp-orientation/SKILL.md`（層、台帳、VERDICT、四種不可變等級、文件權威順序、skill 路由）。
2. `docs/handover/HANDOVER.md`（現況、程式碼地圖、慣例、陷阱、開放待辦）。
3. `docs/reference/cli.md`（每個命令、選項與範例流程）。
4. 你要改的那一層的 spec（`docs/superpowers/specs/`，最後一節「補充決定」以程式碼為準）與後記（`docs/superpowers/plans/*-followups.md`，最後一節是開放待辦與裁決）。
5. 對應的 skill（`.agents/skills/vcp-*`）：它們是可操作的規則摘要，改了契約要同步改 skill 並 `cp -r` 到 `.claude/skills/`。

## 工作方式（照這個 repo 的慣例；細節在 `.agents/skills/vcp-release-and-environments`）

- **每件事一個 worktree**（`git worktree add .claude/worktrees/<name> -b <name> main`），一個 PR；`main` 有 branch protection（Lint、ubuntu、windows 三個 required checks，strict），所以走 PR、CI 綠才 merge。commit `type(scope): 說明`（繁中可），不加 Claude / Codex co-author trailer，不 `git add -A`，不裸 `git stash`，不對 markdown 跑 `ruff format`。
- **發版**：MINOR = 產物／台帳內容語意或 CLI 契約改變（含 `src/vcp` 內建登記項）；`projects/` 以 `--plugin` 登記的東西不發版。四步在 `CHANGELOG.md` 表頭，並記得改 `tests/unit/test_package.py` 的 `EXPECTED_CANDIDATE_VERSION`；tag 打在合併 commit。
- **訓練或基準量測進行中**：不在被 editable 指到的 checkout 上 pull / merge / reinstall；benchmark 的 worktree 任何原始檔都不能動（run 合約釘住雜湊）。
- **TDD**：行為變更先寫會失敗的測試；不弱化既有斷言；測試永不碰真資料根（`roots` fixture、`tests/helpers.py`、`tests/submit_fixtures.py`、`tests/backup_fixtures.py`）；重寫台帳／卡的測試一律 `write_text(..., newline="\n")`。
- **自我審查**：spec 符合度、鐵則（時鐘、VERDICT / exit code、`reason=` 字彙、隱私 redact、台帳只增）、跨層接縫（誰產這個欄位、誰吃、舊檔還能讀）。
- **裁決要留痕**：spec 沒說的事自己決定，「決定 — 依據 — 代價」寫進該層後記最後一節；命令或欄位改了同步 `docs/reference/cli.md`、AGENTS.md / CLAUDE.md、對應 skill。
- **不做的事**：`src/vcp` 不放比賽名；不加憑證欄位；不改已寫下的 plan / 預登記 / 配方 / 清單 / artifact；文件的數字只從 machine-readable 結果回讀。

## 目前的開放待辦（依價值排序；每項都在對應後記有裁決脈絡）

1. **稽核 Wave 1c：程式碼快照與授權**（VCP-004 / VCP-006，`docs/audits/2026-09-11-vcp-improvement-audit.md`）：`train run` 目前只記 `git.commit` / `dirty`，要把訓練程式的快照與授權邊界做成產物。這是 `1.0.0` 前最後一個 wave。
2. **PostgreSQL adaptive policy v2**（Plan 12 後記 §3-6、§5、§7）：selector 的信心帶改為相對預估差距或分層 RMSE；需重跑 calibration → six-method → held-out → real；v1 的 policy 與五份證據不改。
3. **full rebuild 後的 dead tuples**（§3-7）：是否在 rebuild 收尾 `VACUUM`，或只寫進操作指南。
4. **EXPLAIN 覆蓋 `status` / `impact` 查詢計畫**（§3-4）；**Linux host 的 live integration record**（§3-3）；**1M 規模**要 ≥128 GB 的機器。
5. 各層後記最後一節標「未做」的小項。

## 完成的定義

- 全套測試綠、覆蓋率 ≥ 80%（現在 94.85%，不要掉）、ruff 乾淨；改了 provenance 就跑 `tests/unit/provenance/test_postgres_docs.py`（它釘住 evidence record 的精確字串）。
- 每個 CLI 命令仍以 VERDICT 收尾、exit code 對、`--json` 可解析；沒有任何憑證進 repo / 台帳 / log。
- 後記、spec 補充決定、`docs/reference/cli.md`、AGENTS.md / CLAUDE.md、對應 skill 已更新；PR 合併、需要時 tag 已 push。

---

（給人看的備註）這份 prompt 與 `HANDOVER.md` 同一天更新；若 `main` 之後又前進，以 `git log`、`CHANGELOG.md` 與各後記最後一節為準。
