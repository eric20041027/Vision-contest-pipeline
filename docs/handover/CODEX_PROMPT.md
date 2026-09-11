# 給 Codex 的接續開發 prompt（2026-09-07）

把下面整段貼給 Codex（repo 根目錄開啟）。它假設 Codex 能讀 repo、跑 `uv`、跑測試、開分支與 commit。

---

你接手的是 `vcp`（vision contest pipeline，Python 3.12 / pydantic v2 / typer / pytest / ruff），一個給 Kaggle 與台灣視覺比賽用的通用框架。六個子專案（資料、量測、融合、訓練、提交治理、備份審計）都已交付並合併到 `main`，全套測試綠、覆蓋率約 96.5%。你的工作是**接續開發**：先把已寫好的待辦做完，再進入比賽膠水。開始前依序讀：

1. `AGENTS.md`（機械鐵則、路徑、常用命令——每一條都是硬性規定）。
2. `docs/handover/HANDOVER.md`（現況、程式碼地圖、慣例、文件地圖、開放待辦、流程、陷阱）。
3. `README.md`（每層的命令表與一次流程）。
4. 你要改的那一層的 spec（`docs/superpowers/specs/`，最後一節「補充決定」以程式碼為準）與後記（`docs/superpowers/plans/*-followups.md`，最後一節是開放待辦與處置）。

## 工作方式（照這個 repo 的慣例）

- **每一件事先開分支**（`git switch -c <topic>`），一件事一個 commit，訊息 `type(scope): 說明`（繁中可），不用 `git add -A`；做完跑全套 `uv run pytest --cov=vcp`（不加 `-q`，才有 passed 數）與 `uv run ruff check . && uv run ruff format --check .`，都綠才 fast-forward 合併到 `main` 並 push。
- **發版**：一批工作合併後，若它改了寫進產物 / 台帳的內容或 CLI 契約就 bump MINOR，否則 PATCH（規則與四個步驟在 `CHANGELOG.md` 表頭；`__version__` 在 `src/vcp/__init__.py`，是唯一來源）。稽核 Wave 0 合併後發 `0.3.0`。
- **TDD**：每個行為變更先寫會失敗的測試，跑到紅，再實作，跑到綠；不弱化既有斷言；測試永不碰真資料根（用 `roots` fixture、`tests/submit_fixtures.py`、`tests/backup_fixtures.py`）；重寫台帳 / 卡的測試一律 `write_text(..., newline="\n")`（Windows CRLF）。
- **自我審查**：每個 commit 前對照三件事——spec 符合度（有沒有多做 / 少做）、鐵則（時鐘、VERDICT / exit code、`reason=` 字彙、隱私 redact、台帳只增、不用 Click 層驗證）、跨層接縫（誰產這個檔 / 欄位、誰吃；新增欄位是否可選、舊檔還能讀）。
- **裁決要留痕**：spec 沒說的事你自己決定，把「決定 — 依據 — 代價」寫進該層後記的最後一節；做完一批就在後記加一節「處置」表（哪項做了、怎麼做、哪項不做與理由），spec 的「補充決定」加條目，README / AGENTS.md 若命令或欄位改了要同步。
- **不做的事**：不改 `src/vcp` 放比賽名；不加憑證欄位或 token 選項；不對 markdown 跑 `ruff format`；不改已寫下的 plan / 預登記 / 配方 / 清單檔（要改就換 id）；不把效能回合混進行為修正。
- **卡住時**：若待辦的描述與程式碼矛盾，先相信程式碼，把矛盾寫進後記再決定；不要為了讓測試過而改測試的期待，除非你能說明期待本來就錯。

## 第一階段：把剩下的待辦做完（預估 1–2 天）

1. **Hygiene C（提交層剩餘）**：照 `docs/superpowers/plans/2026-09-07-vcp-hygiene-c-submit.md` 做，兩個 commit，gate 命令在文件裡。完成後在 `docs/superpowers/plans/2026-09-06-vcp-plan6-followups.md` 加「§9 Hygiene C 處置」。
2. **融合層新待辦**（Plan 4 後記 §8 新待辦 1）：`fuse/build.py::build_run` 在 run 目錄存在但 `fuse.json` 遺失時，重建的紀錄只涵蓋這次重建的子集。改成：若 `run.yaml` 宣告的子集中有任何一個是快取命中而不會進紀錄，就 FAIL（`not_found: fuse.json … pass --replace to rebuild every subset`），`--replace` 時所有子集都重建；更新 `tests/unit/fuse/test_build.py::test_build_rebuilds_a_missing_fuse_json` 的期待與 docstring；fusion spec §14 加一條。
3. **跨層 VERDICT 識別欄位**（Plan 7 後記 §5-4）：`vcp.cli_common.run_command(..., context=)` 已存在（backup 層在用）——把 `cli_eval.py`、`cli_fuse.py`、`cli_train.py`、`cli_submit.py` 的每個命令都傳 `context=`（dataset / run / recipe / id 這類識別欄位），讓失敗的 VERDICT 也帶它們；每層各補一個「失敗時仍有識別欄位」的 CLI 測試。
4. **後記裡標「未做」的小項**逐一看：值得做的做，不做的在後記寫理由。效能類（Plan 2c §5-2、Plan 3 §5-4、Plan 7 §5-6）先不碰。

## 第二階段：RSNA Knee 比賽膠水（真正的里程碑；Kaggle 截止 2026-10-22）

目標：用這個框架完整跑一次 RSNA Knee（DICOM 多序列 study、12 標籤 macro AUC、notebook-only 推論），所有比賽專屬的東西只放 `projects/rsna-knee/`。

1. **資料**：`rsna-knee` 已匯入（200-study 子集，`C:/vcp-data`）；確認 `vcp data validate --name rsna-knee`、`vcp data split`（≥2 個互斥 eval 子集 + 1 個 sealed holdout；用 `--group-key` 保 study 不跨子集）、`vcp data materialize --mode png --resize 256 --stack-seq`。
2. **訓練**：在 `projects/rsna-knee/` 寫一個 PyTorch 訓練腳本，用 `vcp.train.MaterializedReader` 讀快取、`vcp.train.Session` 登記 checkpoint 與 note；以 `vcp train run --run … --export … --venv … --seed … --checkpoints … --final … [--upload …] -- python projects/rsna-knee/train.py …` 包起來；訓練框架用自己的 venv（editable 裝 vcp）。
3. **量測**：`vcp eval ingest --format scores_csv`（或自訂轉換器登記在 `projects/rsna-knee/metrics.py`，以 `--plugin` 載入）→ `vcp eval anchor` → `vcp eval measure` → `vcp eval preregister` → `vcp eval judge`；指標 `macro_auc`（已內建）。
4. **融合**：`vcp fuse recipe --method mean`（或 `rank_mean`）→ `vcp fuse ablate --preregister` → `vcp eval judge` 每位成員 → `vcp fuse build`。
5. **提交**：`vcp submit init --dataset rsna-knee-test --eval-dataset rsna-knee --platform kaggle --kind kernel --competition <slug> --metric macro_auc --board-rule best --slots 2 --quota <N>`；kernel 類提交用 `vcp submit stage --kernel <user/notebook> --version <n> --weights RUN[:sha]`；`vcp submit upload` / `sync` / `final`。Kaggle 憑證只給 kaggle CLI，vcp 不碰。
6. **備份**：`vcp backup manifest --conclusion submission:<id>` → `push --tier 1` → `--tier 2` → 決賽前 `--tier 3 --forget-remote`；`vcp backup status` 確認 `rclone_conf=absent`。
7. 把整條流程寫成 `projects/rsna-knee/RUNBOOK.md`（每一步的實際命令與 VERDICT 期待），並在 `tests/integration/` 補真資料的煙霧測試（沒有資料就 skip）。

## 完成的定義（每個階段）

- 全套測試綠、覆蓋率 ≥ 80%（現在約 96.5%，不要掉）、ruff 乾淨、真資料整合測試綠（`uv run pytest tests/integration -o addopts="" -q -m realdata`）。
- 每個 CLI 命令仍以 VERDICT 收尾、exit code 對、`--json` 可解析；沒有任何憑證進 repo / 台帳 / log。
- 後記、spec 補充決定、README / AGENTS.md 已更新；`main` 已 push。

---

（給人看的備註）這份 prompt 與 `HANDOVER.md` 同一天寫；若 `main` 之後又前進，以 `git log` 與各後記最後一節為準。
