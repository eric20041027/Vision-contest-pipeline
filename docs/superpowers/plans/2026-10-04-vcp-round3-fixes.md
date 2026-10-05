# vcp 0.13.0：第三輪回報修正（VCP-044–047）與文件同步 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉第三輪回報的四個缺陷——provenance 索引不認 configs root（VCP-044）、舊備份清單缺檔看不出來（VCP-045）、本機 `remote_copy` 被當成異機備份（VCP-046）、Kaggle CLI 失敗時不回讀（VCP-047）——把文件同步到現況，發 0.13.0。

**Architecture:**
- `vcp.core.paths.path_id` 是 root id 的唯一規則，鎖檔也用它。新模組 `vcp.provenance.roots` 說明「索引服務哪一組 root」：SQLite 每個 configs root 一個檔、schema 3；PostgreSQL 每個 generation 的 metadata 記下 root。每條打開索引的路徑先比 root，才做任何 replay 或前綴檢查。
- 新模組 `vcp.backup.completeness`（`manifest_gaps`）定義「清單完整」；verify、status、tier 3 push、`--forget-remote` 與建清單的自檢都問它。清單鍵的算法從 `Collector.locate` 抽成 `vcp.backup.manifest.entry_key`，「每個路徑取最新一筆」抽成 `vcp.train.checkpoints.newest_per_path`。
- `vcp.backup.dest.covers` / `travels` 決定一份 `remote_copy` 在某個目的地是就地驗、還是當成檔案送；push、verify、status、pull、forget 都經它。
- Kaggle 平台在 CLI 非 0 時，於同一個上傳交易裡回讀（`_after_failure`），排除台帳已知的 ref；列表呼叫經 `vcp.core.proc.timed_runner` 加 120 秒逾時，逾時是新例外 `PlatformTimeout`。
- 文件分兩批：每個缺陷改到的 spec、指南、命令參考、skill 與 CLAUDE.md／AGENTS.md（Task 6）；spec §10 的文件同步與 commit 對照表（Task 7）。

**Tech Stack:** Python 3.12、pydantic v2、typer、pytest、uv、ruff（line length 100）。PostgreSQL 的單元測試用 `FakePostgres`（以 SQLite 做的替身），不需要 PostgreSQL 服務。

**Spec:** `docs/superpowers/specs/2026-10-04-vcp-round3-fixes-design.md`（commit c951b9d）。先讀它；本計畫從它推論。

## Global Constraints

- **從 spec 照抄的硬性規定。**
  - root id（§3.1）：「`vcp.core.paths.path_id(path)` = `sha256(os.path.normcase(str(Path(path).resolve())))` 的前 16 碼。」「跟 0.12.0 鎖檔名的規則相同；`vcp.core.lock.lock_path` 改用這個函式。」「同一個目錄的不同寫法（磁碟代號大小寫、8.3 短名、junction／symlink）得到同一個 id。」「不在 configs root 裡放 id 檔」。
  - SQLite（§3.2）：「檔名：`<data_root>/indexes/provenance-<configs root id>.sqlite3`；`provenance_index_path(data_root, configs_root)`。」「metadata 新增 `configs_root_id`、`configs_root`、`data_root_id`、`data_root`（路徑只供顯示）。`SCHEMA_VERSION` 從 2 變 3。」舊檔 `indexes/provenance.sqlite3` 不再讀：新檔不存在 → FAIL `not_found:`，訊息指出舊檔並說「每個 checkout 跑一次 `vcp provenance rebuild`；沒有 0.12 使用者之後可以刪掉舊檔」；直接打開 schema 2 的檔 → FAIL `mismatch:`。
  - PostgreSQL（§3.3）：「每個 generation 的 metadata 新增同樣四個鍵。不改 DDL，`POSTGRES_SCHEMA_VERSION` 維持 1。」「一個 database（service）只服務一組（data root, configs root）。」`sync` 遇到別的 root 的 generation → FAIL `root_mismatch:`，不取代；`rebuild` 可以取代，但 WARN `replaced_root=<id>`；0.13.0 以前、沒記 root 的 generation，除了 `rebuild` 一律 FAIL `root_mismatch:`（`index_root=none`），`rebuild` 取代它並在人類訊息裡說明。
  - 檢查順序（§3.4）：「每個會打開索引的路徑，第一件事是比對 root（configs root id 與 data root id），在任何 replay 或前綴檢查之前。」命令是 `verify-index`、`status`、`sync`、`ingest`、`impact`、`stale`、`explain`、`graph`。不符 → `ValidationFailed` `root_mismatch:`，訊息寫出兩邊的路徑，不出現「became shorter」或「consumed prefix」。同一個 root 內真的截短或改寫台帳 → 仍是 `prefix_drift:`。`impact`、`stale`、`explain`、`graph` 沿用既有的 `--configs-root` 解析 configs root。
  - 清單完整性（§4.1）：每個登記路徑取 `registered_at` 不晚於清單 `created_at` 的最新一筆，`registered_at` 解析不了的也算必須；「『列出』= 清單裡有那個鍵，任何角色、任何種類都算」，`runs/<id>/train/` 底下以 tier 2 `train_dir` 列出的也算；證據與標籤集同一條規則（`attached_at`，鍵是 `artifacts/<kind>/<id>/manifest.json`）；鍵用 `entry_key`；四種結論都檢查。清單建立之後才登記的權重不算缺。
  - 誰檢查（§4.2、§4.3）：verify 每個缺口一個問題 `manifest_incomplete:<run>/train.yaml:checkpoints.<path>`，`reason=manifest_incomplete` 排第一順位，`incomplete=N` 一律印出，FAIL；status 每份清單都重算，舊的 verify 列不能背書，VERDICT `incomplete=<缺檔的清單數>`，本機沒有 train record 時看 verify 列的 `incomplete`，再不行看清單的 `vcp_version`（早於 0.10.0 → `unchecked`）；tier 3 push 動任何檔案之前 FAIL `manifest_incomplete:`、不寫 push 列，tier 1、2 不受影響；`--forget-remote` → `forget_refused:` 帶 `incomplete=N`；`backup manifest` 自檢有缺口 → `InvariantError`（ABORT）。verify 列的 `incomplete` 大於 0 才寫；清單格式不變。
  - 本機副本（§5）：`covers(dest, remote_copy)` 只有兩種情況就地驗、不推：副本在 rclone remote 上；或目的地是本機、副本就在目的地裡。其他的在這個目的地當成 `file`：推到 `<dest>/<root>/<path>`，在那裡驗、從那裡拉，`--forget-remote` 也算它，不管清單的 `present` 是什麼都必須在。push 的來源先原 checkpoint（sha 相符），再 `train upload` 的本機副本；兩者都不符 → 動任何檔案之前 FAIL（`not_found:` / `drift:`）。verify 時它不在目的地 → `missing`，不是 `absent`。status 只認帶 `local_copies` 的 verify 列與 tier 3 push 列。pull 先目的地，沒有再退回本機副本。manifest 同一路徑優先選 rclone 的上傳，同種裡取最新；`local_copies=N` 只供參考。不新增判決字。
  - Kaggle 回讀（§6）：CLI 非 0 時在同一個上傳交易裡（持有台帳鎖）做 VCP-037 的回讀，時間窗與比對規則相同；回讀排除台帳已記的 ref（exit 0 也排除）。`UploadResult.exit_code` 預設 0。`matched` → 寫 `uploaded`（`source=vcp`、`confirmed=true`、`platform_ref`），WARN，VERDICT `confirmed=true platform_ref=<ref> readback=matched exit_code=N detail=<CLI 錯誤>`；`not_listed` → FAIL `upload_failed:`；`ambiguous`、`failed`、`interrupted` 或只對上已知 ref → FAIL `upload_unconfirmed:`；兩種 FAIL 都不寫列、帶 `exit_code=`、`readback=`。訊息保留 `kaggle CLI failed (exit N)` 與 redact 後的 CLI 最後一行。台帳格式不變。
  - 逾時（§6.3）：Kaggle 的列表呼叫（上傳前同步、`sync`、回讀）加 120 秒逾時；上傳前同步與 `sync` 逾時 → `sync_failed:`，回讀逾時 → 結果 `failed`；上傳本身不加。
  - VERDICT 與字彙（§7）：新判決字 `root_mismatch:`、`manifest_incomplete:`、`upload_unconfirmed:`；所有 `vcp provenance` 命令帶 `root=<configs root id>`，失敗時也帶；root 不符帶 `index_root=<id|none>`；PostgreSQL `rebuild` 取代別的 root 時 WARN `replaced_root=<id>`；`backup verify` 帶 `incomplete=`、`local_copies=`；`backup status` 帶 `incomplete=`；`backup push` 與 `backup manifest` 帶 `local_copies=`；`submit upload` 帶 `exit_code=`（FAIL 時加 `readback=`）。
  - 相容性（§8）：0.13.0 是 MINOR；升級四步與「0.12 讀不動帶 `incomplete` / `local_copies` 的 `backup.log.jsonl` 列」「提交台帳的格式不變」寫進 CHANGELOG（Task 9）。
  - 不在範圍（§2）：索引路徑的覆寫（環境變數或選項）、`train status` 的 `backed=`、`backup pull` 還原後的完整性 WARN、1.0 的發版與「1.0 承諾什麼」。
- **本計畫在 spec 沒寫細的地方做的決定**（理由在各 task 的「設計說明」）：
  - CLI 的接線（`root=`、`index_root=`、`impact` / `stale` / `explain` / `graph` 解析 configs root）跟 SQLite 一起放在 Task 1：`provenance_index_path` 與 `make_backend` 的簽名一改，CLI 不跟著改，測試就紅。
  - SQLite 的 `sync` 在 replay 之前先比 root；PostgreSQL 的 `sync` 在寫入交易裡、任何寫入之前比，canonical 圖仍在交易外先建（跟 0.12 一樣，不拉長鎖）。PostgreSQL 沒有前綴檢查。
  - PostgreSQL `rebuild` 取代沒記 root 的 generation 時仍是 OK，只多一行人類訊息；`replaced_root=` 的 WARN 只給「取代了別的 root」。
  - configs root 解析不了時，VERDICT 不帶 `root=`，錯誤照舊。
  - `vcp provenance status --json` 多一個 `index`（索引的位置）；`vcp-provenance-graph` skill 從這裡讀，不再自己拼路徑。
  - 清單的 `created_at` 改在走訪之前取，所以自檢只會被建清單程式的 bug 觸發。
  - 新例外 `PlatformTimeout(PlatformError)`（FAIL）；`sync` 只把逾時改報 `sync_failed:`，其他列表失敗的字不變。
  - `Platform.upload` 多一個關鍵字參數 `known_refs`（`manual` 平台接受、不用）。
  - 端到端測試加在既有的 `tests/unit/test_e2e_backup.py`。
  - HANDOVER 與 CODEX_PROMPT 標題的日期、0.13.0 的版本與測試數字放在 Task 9（發版當天才知道）；Task 7 只把文件裡過時的數字更正到 0.12.0 的實數。
- **時間。** 只用 `vcp.core.time.utc_now()` / `stamp()` / `parse_stamp()`。ruff TID251 擋 `datetime.now` / `utcnow` / `today` 與 `time.time`；`time.sleep` 可以用（回讀的等待就是它，測試用 `no_wait` 換掉）。
- **VERDICT 與 exit code。** 每個 CLI 命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT ...` 收尾；exit 0 / 0 / 1 / 2。`ValidationFailed`、`IntegrityError`、`PlatformError` 是 FAIL，`VcpError`（含 `InvariantError`）是 ABORT；訊息以 `reason:` 字開頭。`--json` 時 JSON 到 stdout、VERDICT 到 stderr。命令永不互動提問。
- **測試。**
  - 永不碰真資料根：用 `roots` fixture（設好 `VCP_DATA_ROOT` / `VCP_CONFIGS_ROOT`）、`tests/helpers.py`、`tests/backup_fixtures.py`、`tests/submit_fixtures.py`。
  - PostgreSQL 用 `tests/unit/provenance/test_postgres_incremental.py` 的 `FakePostgres`。`uv sync --frozen` 就夠：psycopg 在 dev group 裡，不必加 `--extra postgres`；沒設定 service 的整合測試會 skip。
  - Kaggle 用 `FakeRunner`（固定的 `CompletedProcess`）；逾時測試用真的子程序（0.5 秒）。
  - 跑法：`uv run pytest -o addopts="" -q <路徑>`；全套 `uv run pytest --cov=vcp -o addopts="" -p no:cacheprovider -q`。
  - 回歸 gate（`tests/unit/test_regression_gate.py`）點名的測試不改名、不刪。本計畫改了其中三個的內容：`test_forget_remote_only_after_everything_verified`、`test_fake_rclone_story`（Task 4）、`test_a_read_back_that_meets_a_ref_the_ledger_holds_confirms_nothing`（Task 5）。
- **檔案與編碼。** UTF-8、LF。repo 裡的檔用 Edit / Write 工具改，不用 Python `Path.write_text` 或文字模式 `open`（Windows 上會寫出 CRLF）；測試自己的 tmp 檔照既有寫法。不要在工具輸入裡打反斜線加 u 的跳脫。`git diff --check` 要乾淨。
- **公開 repo。** 程式碼、測試、文件裡不放比賽名稱、比賽的 id、本機路徑或資料。
- **Skill 與代理說明。** 只改 `.claude/skills/`，改完 `cp -r` 鏡射到 `.agents/skills/`（`tests/unit/test_skills_plugin.py` 擋兩邊不一致）。`CLAUDE.md` 與 `AGENTS.md` 做同一個修改；Task 7 之後兩份逐位元相同。
- **Git。**
  - commit 訊息是 `<type>: <繁中說明>`，照各 task 給的寫；寫進 scratchpad 的檔（Write 工具），`git commit -F <訊息檔>`。**不加 `Co-Authored-By` 或任何署名行。** 發版 commit 照 CHANGELOG 的發版步驟用 `chore(release): v0.13.0`。
  - 永不 `git add -A`，明列檔案；永不裸 `git stash`。
  - commit 前：改過的 `.py` 跑 `uv run ruff format <檔>`、`uv run ruff check --fix <檔>`，再 `uv run ruff check . && uv run ruff format --check .`。永不對 markdown 跑 `ruff format`。
- **環境。** Windows 11 上的 Git Bash。裸 `python` 不在 PATH，用 `uv run python`。Git Bash 的 heredoc 餵 Python 會壞：腳本用 Write 工具寫成檔再跑。只在 `.claude/worktrees/vcp-040`（branch `feat/vcp-044-047-round3`）工作；不 push、不開 PR。Task 9 例外，而且要等使用者核可。

## File Structure

- **Create：**
  - `src/vcp/provenance/roots.py`：`ROOT_KEYS`、`IndexRoots`、`check_roots`（Task 1）。
  - `src/vcp/backup/completeness.py`：`RECORD_ROLES`、`Gap`、`checkable`、`manifest_gaps`（Task 3）。
  - `docs/reference/commit-map-2026-10.tsv`：由腳本產生（Task 7）。
- **Modify（程式）：**
  - `src/vcp/core/paths.py`（`path_id`、`LEGACY_PROVENANCE_INDEX`、`provenance_index_path`）、`src/vcp/core/lock.py`（`lock_path` 改用 `path_id`）：Task 1。
  - `src/vcp/provenance/index.py`（schema 3、root 比對；Task 2 加 `RebuildResult.replaced_roots`）、`src/vcp/provenance/backend.py`（`make_backend` 收 configs root）、`src/vcp/cli_provenance.py`（`root=`、`index_root=`；Task 2 加 `replaced_root=`）：Task 1、2。
  - `src/vcp/provenance/postgres.py`：generation 的 root、`sync` 不取代、`rebuild` 回報取代（Task 2）。
  - `src/vcp/train/checkpoints.py`（`newest_per_path`）、`src/vcp/train/upload.py`：Task 3。
  - `src/vcp/backup/manifest.py`（`external_path`、`locate_file`、`entry_key`）、`evidence.py`、`schema.py`、`verify.py`、`status.py`、`push.py`、`src/vcp/cli_backup.py`：Task 3（完整性）與 Task 4（本機副本）。
  - `src/vcp/backup/dest.py`（`covers`、`travels`、`copy_path`）、`src/vcp/backup/pull.py`：Task 4。
  - `src/vcp/core/errors.py`（`PlatformTimeout`）、`src/vcp/core/proc.py`（`timed_runner`）、`src/vcp/submit/platforms/base.py`、`manual.py`、`kaggle.py`、`src/vcp/submit/actions.py`、`src/vcp/submit/sync.py`、`src/vcp/cli_submit.py`：Task 5。
- **Tests：**
  - Create：`tests/unit/provenance/test_index_roots.py`（Task 1）、`tests/unit/backup/test_completeness.py`（Task 3）、`tests/unit/backup/test_local_copies.py`（Task 4）。
  - Modify：Task 1 跟著新簽名改 `tests/unit/provenance/{test_index,test_backend,test_cli_graph,test_cli_postgres,test_postgres_connection}.py` 與 `tests/performance/provenance/{real_validation,production_benchmark}.py`；Task 2 追加 `tests/unit/provenance/test_postgres_incremental.py`；Task 3 擴充 `tests/backup_fixtures.py`；Task 4 改 `tests/unit/backup/{test_dest_push,test_verify,test_pull_status}.py` 與 `tests/unit/test_e2e_backup.py`；Task 5 改 `tests/unit/submit/{test_platforms,test_actions}.py`；Task 8 追加 `tests/unit/test_e2e_backup.py`、`tests/unit/test_regression_gate.py`。
- **文件（Task 6，隨缺陷）：** 五份 spec 的補充決定、`docs/guides/POSTGRESQL_PROVENANCE.md`、`docs/guides/DATASET_EVOLUTION_PROVENANCE.md`、`docs/reference/cli.md`、`.claude/skills/` 的六個 skill（鏡射到 `.agents/skills/`）、`CLAUDE.md`、`AGENTS.md`。
- **文件（Task 7，spec §10）：** 稽核文件、`CHANGELOG.md` 表頭、兩份 README、`docs/handover/{HANDOVER,CODEX_PROMPT,DATASET_EVOLUTION_PROVENANCE_HANDOFF}.md`、`CLAUDE.md` 與 `AGENTS.md`、`vcp-orientation/map.md`、`vcp-release-and-environments/SKILL.md`、commit 對照表。
- **發版（Task 9）：** `src/vcp/__init__.py`、`.claude/.claude-plugin/plugin.json`、`tests/unit/test_package.py`、`CHANGELOG.md`、`docs/handover/HANDOVER.md`、`docs/handover/CODEX_PROMPT.md`、`README.md`、`README.zh-TW.md`。

每個 task 一個 commit，照編號順序做：Task 2 建在 Task 1 上，Task 4 建在 Task 3 上（錨點是前一個 task 之後的文字），Task 5 與前四個無關；Task 6–8 依賴 Task 1–5；Task 9 在功能 PR 合併之後。

---

### Task 1: root id 與 SQLite 索引的 root 身分（VCP-044 之一）

**Files:**
- Modify: `src/vcp/core/paths.py`（import、`provenance_index_path` 一帶）
- Modify: `src/vcp/core/lock.py`（import、`lock_path`）
- Create: `src/vcp/provenance/roots.py`
- Modify: `src/vcp/provenance/index.py`（import、`SCHEMA_VERSION`、`ProvenanceIndex` 開檔／重建／同步／ingest／驗證）
- Modify: `src/vcp/provenance/backend.py`（`SQLiteBackend.ingest_diff`、`make_backend`）
- Modify: `src/vcp/cli_provenance.py`（`_backend`、新 `_root`、九個命令）
- Test: `tests/unit/provenance/test_index_roots.py`（新建）
- Modify（跟著新簽名改呼叫）：`tests/unit/provenance/test_index.py`、`tests/unit/provenance/test_backend.py`、`tests/unit/provenance/test_cli_graph.py`、`tests/unit/provenance/test_cli_postgres.py`、`tests/unit/provenance/test_postgres_connection.py`、`tests/performance/provenance/real_validation.py`、`tests/performance/provenance/production_benchmark.py`

**Interfaces:**
- Consumes：無（第一個 task）。
- Produces：
  - `vcp.core.paths.path_id(path: Path) -> str`：`sha256(os.path.normcase(str(Path(path).resolve())))` 的前 16 碼；`vcp.core.lock.lock_path` 改用它（檔名不變）。
  - `vcp.core.paths.LEGACY_PROVENANCE_INDEX = "provenance.sqlite3"`；`provenance_index_path(data_root: Path, configs_root: Path) -> Path` = `<data_root>/indexes/provenance-<path_id(configs_root)>.sqlite3`。
  - `vcp.provenance.roots`：`ROOT_KEYS = ("configs_root_id", "configs_root", "data_root_id", "data_root")`；`IndexRoots(data_root, configs_root)`（frozen，`IndexRoots.of(data_root, configs_root)` 先 resolve；`.configs_root_id`、`.data_root_id`、`.metadata() -> dict[str, str]`、`.matches(recorded) -> bool`）；`check_roots(recorded: Mapping[str, str], roots: IndexRoots, *, remedy: str) -> None`，不符時 `ValidationFailed("root_mismatch: ...", fields={"index_root": <記下的 configs root id 或 "none">})`。
  - `vcp.provenance.index`：`SCHEMA_VERSION = 3`；`ProvenanceIndex(path, *, roots: IndexRoots | None = None)`，`.roots`；`.check_roots(data_root, configs_root) -> None`；`rebuild` 寫入四個 root 鍵。
  - `vcp.provenance.backend.make_backend(config: BackendConfig, data_root: Path, configs_root: Path) -> ProvenanceBackend`（Task 2 再把 roots 交給 PostgreSQL）。
  - `vcp.cli_provenance`：`_backend(data_root, configs_root, backend, pg_service) -> tuple[Path, Path, ProvenanceBackend]`、`_root(configs_root) -> dict[str, FieldValue]`；每個 provenance VERDICT 帶 `root=`（失敗也帶），root 不符時另帶 `index_root=`；`status --json` 的結果多一個 `index`（索引位置）。

設計說明（spec 沒寫細的地方）：
- root 比對放在 `ProvenanceIndex._open` 之後、任何 replay 或前綴檢查之前。讀取路徑（`load_graph`、`statuses`、`stats`、`normalized`）比對建構時給的 `roots`；寫入與驗證路徑（`sync`、`ingest_diff`、`verify`）比對呼叫時傳入的 roots（`_open_for`）。`_open` 維持不帶參數，既有測試對它的 monkeypatch 不必改。
- `SQLiteBackend.ingest_diff` 在 `load_dataset_diff`（會重新 hash diff 的輸入）之前先 `check_roots`，root 不符時不白算。
- `rebuild` 不比對：它本來就整份換寫這個 root 的檔。
- schema 檢查排在 root 比對之前：schema 2 的檔沒有 root 鍵，先報 `mismatch:` 才是正確的補救方向。

- [ ] **Step 1: 寫失敗的測試** — 新建 `tests/unit/provenance/test_index_roots.py`

```python
"""VCP-044 (spec 2026-10-04 §3, §9): a provenance index serves one checkout. SQLite keeps one
file per configs root; an index records its roots, and every command that opens it compares
them first -- another checkout's index is ``root_mismatch:``, never a ledger that "became
shorter" or "changed inside the consumed prefix"."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from helpers import det_samples, make_card
from vcp.backup.manifest import write_manifest
from vcp.backup.schema import Manifest
from vcp.cli import app
from vcp.core.lock import lock_path
from vcp.core.paths import DatasetPaths, path_id, provenance_index_path
from vcp.data.dataset import Dataset
from vcp.data.source_audit import write_source_audit
from vcp.provenance.diff import DatasetDiffSpec, create_dataset_diff
from vcp.provenance.index import ProvenanceIndex
from vcp.submit.location import shared_ledger

runner = CliRunner()
LEDGER = "datasets/idx-old/events.jsonl"


def _dataset(roots, name, samples):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    dataset = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    dataset.save(paths)
    write_source_audit(paths, dataset.card, data_root=roots.data)


@pytest.fixture
def two(roots, tmp_path):
    """One data root and two checkouts of the same configs, A and B. A then appends a row to a
    ledger B has too, so B's copy is shorter than the prefix A's index consumed: the topology
    VCP-044 was reported on."""
    old = det_samples(4, seed=21)
    new = [sample.model_copy(deep=True) for sample in old]
    new[0] = new[0].model_copy(update={"group": "new"})
    _dataset(roots, "idx-old", old)
    _dataset(roots, "idx-new", new)
    create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="idx-old",
            to_dataset="idx-new",
            artifact_id="idx-diff",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    (roots.configs / LEDGER).write_bytes(b'{"event": "both checkouts"}\n')
    other = tmp_path / "configs-b"
    shutil.copytree(roots.configs, other)
    with (roots.configs / LEDGER).open("ab") as f:
        f.write(b'{"event": "only checkout A"}\n')
    return SimpleNamespace(data=roots.data, a=roots.configs, b=other)


def _cli(configs: Path, data: Path, *args: str):
    result = runner.invoke(
        app, ["provenance", *args, "--data-root", str(data), "--configs-root", str(configs)]
    )
    lines = [line for line in result.output.splitlines() if line.startswith("VERDICT ")]
    return result, (lines[-1] if lines else "")


def _short_name(path: Path) -> str | None:
    """The 8.3 spelling Windows gives ``path``, or None when the volume keeps none."""
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
        return None
    return buffer.value if buffer.value != str(path) else None


def test_root_id_uses_the_lock_path_normalisation(tmp_path):
    target = tmp_path / "configs"
    target.mkdir()
    key = os.path.normcase(str(target.resolve()))
    assert path_id(target) == hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    lock = lock_path(tmp_path / "data", "submissions", target)
    assert lock.name == f"submissions-{path_id(target)}.lock"


def test_two_spellings_of_one_directory_share_an_id(tmp_path):
    root = tmp_path / "configs"
    root.mkdir()
    assert path_id(root.parent / "." / ".." / tmp_path.name / "configs") == path_id(root)
    assert path_id(tmp_path / "another") != path_id(root)
    if sys.platform == "win32":
        assert path_id(Path(str(root).upper())) == path_id(root)
        short = _short_name(root)
        if short is not None:
            assert path_id(Path(short)) == path_id(root)
    link = tmp_path / "link"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        return  # this machine may not create symlinks (Windows without developer mode)
    assert path_id(link) == path_id(root)


def test_each_configs_root_gets_its_own_sqlite_index_file(tmp_path):
    data, a, b = tmp_path / "data", tmp_path / "a", tmp_path / "b"
    assert provenance_index_path(data, a) == data / "indexes" / f"provenance-{path_id(a)}.sqlite3"
    assert provenance_index_path(data, a) != provenance_index_path(data, b)
    assert provenance_index_path(data, a).name != "provenance.sqlite3"


def test_an_index_records_its_roots_in_schema_3(roots):
    index = ProvenanceIndex(provenance_index_path(roots.data, roots.configs))
    index.rebuild(roots.data, roots.configs)
    stats = index.stats()
    assert stats["schema_version"] == "3"
    assert stats["configs_root_id"] == path_id(roots.configs)
    assert stats["data_root_id"] == path_id(roots.data)
    assert stats["configs_root"] == roots.configs.resolve().as_posix()
    assert stats["data_root"] == roots.data.resolve().as_posix()


def test_two_configs_roots_on_one_data_root_both_verify_ok(two):
    for configs in (two.a, two.b):
        result, verdict = _cli(configs, two.data, "rebuild")
        assert result.exit_code == 0 and f"root={path_id(configs)}" in verdict, result.output
    for configs in (two.a, two.b):
        result, verdict = _cli(configs, two.data, "verify-index")
        assert result.exit_code == 0 and "status=OK" in verdict, result.output
    names = sorted(path.name for path in (two.data / "indexes").glob("*.sqlite3"))
    assert names == sorted(f"provenance-{path_id(c)}.sqlite3" for c in (two.a, two.b))
    status = runner.invoke(
        app,
        ["provenance", "status", "--json", "--data-root", str(two.data)]
        + ["--configs-root", str(two.b)],
    )
    index = json.loads(status.stdout)["result"]["index"]
    assert index == str(provenance_index_path(two.data, two.b))


def test_graph_from_each_root_shows_its_own_backup_manifests(two, tmp_path):
    paths = DatasetPaths.resolve("idx-old", data_root=two.data, configs_root=two.b)
    write_manifest(
        paths,
        Manifest(
            manifest_id="only-b",
            dataset="idx-old",
            conclusion="all",
            created_at="2026-10-04T00:00:00.000Z",
            vcp_version="0.13.0",
            data_root=two.data.as_posix(),
            files=[],
        ),
    )
    drawn = {}
    for name, configs in (("a", two.a), ("b", two.b)):
        assert _cli(configs, two.data, "rebuild")[0].exit_code == 0
        out = tmp_path / f"graph-{name}.md"
        result = runner.invoke(
            app,
            ["provenance", "graph", "--out", str(out), "--detail", "full", "--json"]
            + ["--data-root", str(two.data), "--configs-root", str(configs)],
        )
        assert result.exit_code == 0, result.output
        nodes = json.loads(result.stdout)["result"]["nodes"]
        drawn[name] = {member for node in nodes for member in node["members"]}
    assert "backup:idx-old/only-b" in drawn["b"]
    assert "backup:idx-old/only-b" not in drawn["a"]


COMMANDS = [
    ("verify-index", []),
    ("status", []),
    ("sync", []),
    ("ingest", ["--artifact", "idx-diff"]),
    ("impact", ["--dataset", "idx-old"]),
    ("stale", ["--head", "idx-new"]),
    ("explain", ["--entity", "run:anything"]),
    ("graph", ["--out", "<out>"]),
]


@pytest.mark.parametrize(("command", "args"), COMMANDS, ids=[c for c, _ in COMMANDS])
def test_another_roots_index_fails_root_mismatch_not_prefix_drift(two, tmp_path, command, args):
    assert _cli(two.a, two.data, "rebuild")[0].exit_code == 0
    shutil.copyfile(provenance_index_path(two.data, two.a), provenance_index_path(two.data, two.b))
    args = [str(tmp_path / "graph.md") if arg == "<out>" else arg for arg in args]
    result, verdict = _cli(two.b, two.data, command, *args)
    assert result.exit_code == 1 and "status=FAIL" in verdict, result.output
    assert "root_mismatch:" in verdict and "prefix_drift" not in verdict
    assert f"root={path_id(two.b)}" in verdict and f"index_root={path_id(two.a)}" in verdict
    assert "became shorter" not in result.output and "consumed prefix" not in result.output


@pytest.mark.parametrize("damage", ["truncate", "rewrite"])
def test_a_damaged_ledger_in_the_same_root_is_still_prefix_drift(two, damage):
    assert _cli(two.a, two.data, "rebuild")[0].exit_code == 0
    ledger = two.a / LEDGER
    raw = ledger.read_bytes()
    ledger.write_bytes(raw[:-5] if damage == "truncate" else b"X" + raw[1:])
    result, verdict = _cli(two.a, two.data, "verify-index")
    words = "became shorter" if damage == "truncate" else "changed inside the consumed prefix"
    assert result.exit_code == 1 and "prefix_drift:" in verdict, result.output
    assert words in verdict and "root_mismatch" not in verdict


def test_a_shared_ledger_another_checkout_appends_to_is_not_drift(two):
    """spec 2026-10-04 §3.4: a ``ledger: shared`` file lives in the data root and only grows,
    under its lock, so every checkout's index keeps a valid prefix of it."""
    paths = DatasetPaths.resolve("idx-new", data_root=two.data, configs_root=two.a)
    ledger = shared_ledger(paths)
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b'{"event": "note", "ts": "2026-10-04T00:00:00.000Z", "text": "a"}\n')
    for configs in (two.a, two.b):
        assert _cli(configs, two.data, "rebuild")[0].exit_code == 0
    with ledger.open("ab") as f:  # checkout B appends, as `vcp submit` does under the lock
        f.write(b'{"event": "note", "ts": "2026-10-04T00:01:00.000Z", "text": "b"}\n')
    result, verdict = _cli(two.a, two.data, "sync")
    assert result.exit_code == 0 and "status=OK" in verdict, result.output
    assert _cli(two.a, two.data, "verify-index")[0].exit_code == 0


def test_the_legacy_index_is_not_read_and_the_failure_names_it(two):
    legacy = two.data / "indexes" / "provenance.sqlite3"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"the index vcp 0.12 kept")
    result, verdict = _cli(two.a, two.data, "status")
    assert result.exit_code == 1 and "not_found:" in verdict, result.output
    assert "provenance.sqlite3 is the index vcp 0.12 and earlier kept" in verdict
    assert "run `vcp provenance rebuild` once in each checkout" in verdict
    assert _cli(two.a, two.data, "rebuild")[0].exit_code == 0
    assert _cli(two.a, two.data, "status")[0].exit_code == 0
    assert legacy.read_bytes() == b"the index vcp 0.12 kept"


def test_a_schema_2_index_is_a_schema_mismatch(two):
    assert _cli(two.a, two.data, "rebuild")[0].exit_code == 0
    connection = sqlite3.connect(provenance_index_path(two.data, two.a))
    with connection:
        connection.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
    connection.close()
    result, verdict = _cli(two.a, two.data, "status")
    assert result.exit_code == 1 and "mismatch: provenance index schema version" in verdict


def test_a_moved_configs_root_needs_a_rebuild(two, tmp_path):
    assert _cli(two.a, two.data, "rebuild")[0].exit_code == 0
    moved = tmp_path / "configs-moved"
    shutil.move(two.a, moved)
    result, verdict = _cli(moved, two.data, "status")
    assert result.exit_code == 1 and "not_found:" in verdict, result.output
    assert _cli(moved, two.data, "rebuild")[0].exit_code == 0
    assert _cli(moved, two.data, "status")[0].exit_code == 0


def test_a_data_root_copied_elsewhere_is_a_root_mismatch(two, tmp_path):
    """The index travels with its data root, but its checkpoints name the old one. A copy, not a
    move: the CLI's log file stays open in the data root, and Windows cannot move a folder that
    holds an open file."""
    assert _cli(two.a, two.data, "rebuild")[0].exit_code == 0
    copied = tmp_path / "data-copy"
    shutil.copytree(two.data, copied)
    result, verdict = _cli(two.a, copied, "status")
    assert result.exit_code == 1 and "root_mismatch:" in verdict, result.output
    assert f"index_root={path_id(two.a)}" in verdict
```

- [ ] **Step 2: 跑測試，確認失敗**

Run: `uv run pytest -o addopts="" -q tests/unit/provenance/test_index_roots.py`
Expected: 收集階段就 FAIL：`ImportError: cannot import name 'path_id' from 'vcp.core.paths'`。

- [ ] **Step 3: `path_id` 與每個 configs root 一份的索引路徑** — `src/vcp/core/paths.py`

在 `src/vcp/core/paths.py`，把

```python
from __future__ import annotations

import os
import re
```

換成

```python
from __future__ import annotations

import hashlib
import os
import re
```

在 `src/vcp/core/paths.py`，把

```python
def provenance_index_path(data_root: Path) -> Path:
    return indexes_root(data_root) / "provenance.sqlite3"
```

換成

```python
# vcp 0.12 and earlier kept one index per data root under this name; 0.13 never reads it.
LEGACY_PROVENANCE_INDEX = "provenance.sqlite3"


def path_id(path: Path) -> str:
    """The first 16 hex of sha256 over the resolved, platform-case-folded path (spec 2026-10-04
    §3.1): the identity of a configs root or a data root, and the name of a lock file. Spellings
    of one directory -- drive-letter case, an 8.3 short name, a junction or a symlink -- share an
    id; moving or renaming the directory gives it a new one."""
    key = os.path.normcase(str(Path(path).resolve()))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def provenance_index_path(data_root: Path, configs_root: Path) -> Path:
    """``<data_root>/indexes/provenance-<configs root id>.sqlite3``: one SQLite index per
    checkout (spec 2026-10-04 §3.2), so two checkouts that share a data root never take turns
    with one file."""
    return indexes_root(data_root) / f"provenance-{path_id(configs_root)}.sqlite3"
```

- [ ] **Step 4: 鎖檔名改用同一個函式** — `src/vcp/core/lock.py`

在 `src/vcp/core/lock.py`，把

```python
import errno
import hashlib
import json
```

換成

```python
import errno
import json
```

在 `src/vcp/core/lock.py`，把

```python
from vcp.core.errors import VcpError
from vcp.core.time import stamp
```

換成

```python
from vcp.core.errors import VcpError
from vcp.core.paths import path_id
from vcp.core.time import stamp
```

在 `src/vcp/core/lock.py`，把

```python
    spellings of one Windows path share a lock."""
    key = os.path.normcase(str(Path(target).resolve()))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return Path(data_root) / LOCKS_DIR / f"{prefix}-{digest}.lock"
```

換成

```python
    spellings of one Windows path share a lock. The hash is ``vcp.core.paths.path_id``, the rule
    a provenance index uses for its roots (spec 2026-10-04 §3.1)."""
    return Path(data_root) / LOCKS_DIR / f"{prefix}-{path_id(target)}.lock"
```

- [ ] **Step 5: 索引記下並比對 root** — 新建 `src/vcp/provenance/roots.py`

新建 `src/vcp/provenance/roots.py`

```python
"""Which checkout a provenance index serves (spec 2026-10-04 §3, VCP-044).

A ledger checkpoint is keyed ``configs/<path>`` or ``data/<path>`` and re-anchored at the roots
of the command that reads it, so an index built from one checkout and read from another compares
two different ledgers and reports their differences as a shortened or rewritten ledger. An index
therefore records the roots it was built for -- the ids by ``vcp.core.paths.path_id``, the paths
for people -- and every path that opens it compares them first, before any replay or prefix
check.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.paths import path_id

ROOT_KEYS = ("configs_root_id", "configs_root", "data_root_id", "data_root")


@dataclass(frozen=True)
class IndexRoots:
    """The (data root, configs root) pair one index serves, both resolved."""

    data_root: Path
    configs_root: Path

    @classmethod
    def of(cls, data_root: Path, configs_root: Path) -> IndexRoots:
        return cls(Path(data_root).resolve(), Path(configs_root).resolve())

    @property
    def configs_root_id(self) -> str:
        return path_id(self.configs_root)

    @property
    def data_root_id(self) -> str:
        return path_id(self.data_root)

    def metadata(self) -> dict[str, str]:
        """What an index records: the two ids, and the two paths (for messages only)."""
        return {
            "configs_root_id": self.configs_root_id,
            "configs_root": self.configs_root.as_posix(),
            "data_root_id": self.data_root_id,
            "data_root": self.data_root.as_posix(),
        }

    def matches(self, recorded: Mapping[str, str]) -> bool:
        return (
            recorded.get("configs_root_id") == self.configs_root_id
            and recorded.get("data_root_id") == self.data_root_id
        )


def check_roots(recorded: Mapping[str, str], roots: IndexRoots, *, remedy: str) -> None:
    """``root_mismatch:`` (FAIL) unless the index records exactly these roots. The failure
    carries ``index_root=``: the configs root id the index records, or ``none``."""
    if roots.matches(recorded):
        return
    index_root = recorded.get("configs_root_id")
    if not index_root:
        raise ValidationFailed(
            "root_mismatch: this provenance index records no root (it was built before vcp "
            f"0.13.0); {remedy}",
            fields={"index_root": "none"},
        )
    raise ValidationFailed(
        "root_mismatch: this provenance index was built for configs root "
        f"{recorded.get('configs_root')} and data root {recorded.get('data_root')}, not for "
        f"configs root {roots.configs_root.as_posix()} and data root "
        f"{roots.data_root.as_posix()}; {remedy}",
        fields={"index_root": index_root},
    )
```

- [ ] **Step 6: SQLite 索引：schema 3、寫入與比對 root、舊檔名的 `not_found:`** — `src/vcp/provenance/index.py`

在 `src/vcp/provenance/index.py`，把

```python
from vcp.core.hashing import sha256_file, sha256_text
from vcp.core.time import stamp
```

換成

```python
from vcp.core.hashing import sha256_file, sha256_text
from vcp.core.paths import LEGACY_PROVENANCE_INDEX
from vcp.core.time import stamp
```

在 `src/vcp/provenance/index.py`，把

```python
    dataset_version_id,
)
from vcp.provenance.schema import ProvenanceEdge, ProvenanceEntity, SampleChange, StatusRecord
```

換成

```python
    dataset_version_id,
)
from vcp.provenance.roots import ROOT_KEYS, IndexRoots, check_roots
from vcp.provenance.schema import ProvenanceEdge, ProvenanceEntity, SampleChange, StatusRecord
```

在 `src/vcp/provenance/index.py`，把

```python
SCHEMA_VERSION = 2
```

換成

```python
# 3 (0.13.0): an index serves one checkout and records its roots (spec 2026-10-04 §3.2).
SCHEMA_VERSION = 3
```

在 `src/vcp/provenance/index.py`，把

```python
class ProvenanceIndex:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()

    def _open(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise ValidationFailed(
                f"not_found: provenance index {self.path}; run `vcp provenance rebuild`"
            )
        connection = _connection(self.path)
        row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if row is None or int(row["value"]) != SCHEMA_VERSION:
            connection.close()
            raise IntegrityError("mismatch: provenance index schema version")
        return connection
```

換成

```python
_SQLITE_REMEDY = "run `vcp provenance rebuild` from this checkout (every configs root has its own)"


def _write_roots(connection: sqlite3.Connection, roots: IndexRoots) -> None:
    for key, value in roots.metadata().items():
        connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", (key, value))


def _check_index_roots(connection: sqlite3.Connection, roots: IndexRoots) -> None:
    placeholders = ",".join("?" for _ in ROOT_KEYS)
    recorded = {
        row["key"]: row["value"]
        for row in connection.execute(
            f"SELECT key,value FROM metadata WHERE key IN ({placeholders})", ROOT_KEYS
        )
    }
    check_roots(recorded, roots, remedy=_SQLITE_REMEDY)


class ProvenanceIndex:
    def __init__(self, path: Path, *, roots: IndexRoots | None = None) -> None:
        """``roots``: the checkout this index must serve; every read checks them first (spec
        2026-10-04 §3.4). ``sync``, ``ingest_diff`` and ``verify`` check the roots they are
        given instead, and ``rebuild`` records them."""
        self.path = Path(path).resolve()
        self.roots = roots

    def _not_found(self) -> str:
        message = f"not_found: provenance index {self.path}; run `vcp provenance rebuild`"
        legacy = self.path.with_name(LEGACY_PROVENANCE_INDEX)
        if legacy != self.path and legacy.is_file():
            message += (
                f". {legacy} is the index vcp 0.12 and earlier kept for the whole data root, "
                "which 0.13 no longer reads: run `vcp provenance rebuild` once in each checkout, "
                "and delete it once no 0.12 user remains"
            )
        return message

    def _open(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise ValidationFailed(self._not_found())
        connection = _connection(self.path)
        try:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if row is None or int(row["value"]) != SCHEMA_VERSION:
                raise IntegrityError("mismatch: provenance index schema version")
            if self.roots is not None:
                _check_index_roots(connection, self.roots)
        except BaseException:
            connection.close()
            raise
        return connection

    def _open_for(self, data_root: Path, configs_root: Path) -> sqlite3.Connection:
        """``_open`` for a command that names its roots: they are compared first, before any
        replay or prefix check (spec 2026-10-04 §3.4)."""
        connection = self._open()
        try:
            _check_index_roots(connection, IndexRoots.of(data_root, configs_root))
        except BaseException:
            connection.close()
            raise
        return connection

    def check_roots(self, data_root: Path, configs_root: Path) -> None:
        """``root_mismatch:`` unless this index serves these roots; nothing else is read."""
        self._open_for(data_root, configs_root).close()
```

在 `src/vcp/provenance/index.py`，把

```python
            with connection:
                _schema(connection)
                _write_graph(connection, graph)
```

換成

```python
            with connection:
                _schema(connection)
                _write_roots(connection, IndexRoots.of(data_root, configs_root))
                _write_graph(connection, graph)
```

在 `src/vcp/provenance/index.py`，把

```python
        """Append newly arrived canonical records while rejecting deletion or mutation."""
        data_root = Path(data_root).resolve()
        configs_root = Path(configs_root).resolve()
        before = _canonical_snapshot(data_root, configs_root)
```

換成

```python
        """Append newly arrived canonical records while rejecting deletion or mutation."""
        data_root = Path(data_root).resolve()
        configs_root = Path(configs_root).resolve()
        self.check_roots(data_root, configs_root)  # before the replay (spec 2026-10-04 §3.4)
        before = _canonical_snapshot(data_root, configs_root)
```

在 `src/vcp/provenance/index.py`，把

```python
            raise IntegrityError("canonical_drift: inputs changed during provenance sync")
        connection = self._open()
```

換成

```python
            raise IntegrityError("canonical_drift: inputs changed during provenance sync")
        connection = self._open_for(data_root, configs_root)
```

在 `src/vcp/provenance/index.py`，把

```python
        manifest_path = store.manifest_path(data_root, DIFF_KIND, artifact_id)
        connection = self._open()
```

換成

```python
        manifest_path = store.manifest_path(data_root, DIFF_KIND, artifact_id)
        connection = self._open_for(data_root, configs_root)
```

在 `src/vcp/provenance/index.py`，把

```python
        configs_root = Path(configs_root).resolve()
        connection = self._open()
        try:
            _verify_checkpoints(connection, data_root, configs_root)
            indexed = _load_graph(connection)
```

換成

```python
        configs_root = Path(configs_root).resolve()
        connection = self._open_for(data_root, configs_root)
        try:
            _verify_checkpoints(connection, data_root, configs_root)
            indexed = _load_graph(connection)
```

- [ ] **Step 7: 後端工廠帶上 configs root** — `src/vcp/provenance/backend.py`

在 `src/vcp/provenance/backend.py`，把

```python
from vcp.provenance.schema import StatusRecord
```

換成

```python
from vcp.provenance.roots import IndexRoots
from vcp.provenance.schema import StatusRecord
```

在 `src/vcp/provenance/backend.py`，把

```python
        if requested_strategy != "incremental":
            raise ValidationFailed(f"unsupported_strategy: {requested_strategy}")
        verified = load_dataset_diff(Path(data_root), artifact_id, verify_inputs=True)
```

換成

```python
        if requested_strategy != "incremental":
            raise ValidationFailed(f"unsupported_strategy: {requested_strategy}")
        self.index.check_roots(data_root, configs_root)  # before the diff's inputs are re-hashed
        verified = load_dataset_diff(Path(data_root), artifact_id, verify_inputs=True)
```

在 `src/vcp/provenance/backend.py`，把

```python
def make_backend(config: BackendConfig, data_root: Path) -> ProvenanceBackend:
    name = parse_backend(config.name)
    if name is BackendName.SQLITE:
        return SQLiteBackend(ProvenanceIndex(provenance_index_path(Path(data_root))))
```

換成

```python
def make_backend(config: BackendConfig, data_root: Path, configs_root: Path) -> ProvenanceBackend:
    """The backend of one checkout: its reads check the index serves these roots first (spec
    2026-10-04 §3.4)."""
    name = parse_backend(config.name)
    roots = IndexRoots.of(data_root, configs_root)
    if name is BackendName.SQLITE:
        path = provenance_index_path(roots.data_root, roots.configs_root)
        return SQLiteBackend(ProvenanceIndex(path, roots=roots))
```

- [ ] **Step 8: CLI：每個命令解析 configs root，VERDICT 帶 `root=`** — `src/vcp/cli_provenance.py`

在 `src/vcp/cli_provenance.py`，把

```python
from vcp.core.errors import ValidationFailed
from vcp.core.log import FieldValue, Status, format_value
from vcp.core.paths import resolve_configs_root, resolve_data_root
```

換成

```python
from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.log import FieldValue, Status, format_value
from vcp.core.paths import path_id, resolve_configs_root, resolve_data_root
```

在 `src/vcp/cli_provenance.py`，把

```python
def _backend(
    data_root: Path | None,
    backend: BackendName | str,
    pg_service: str | None,
) -> tuple[Path, ProvenanceBackend]:
    root = resolve_data_root(data_root)
    name = _backend_name(backend)
    if pg_service is not None and name is not BackendName.POSTGRESQL:
        raise ValidationFailed("pg_service_requires_postgresql")
    return root, make_backend(BackendConfig(name=name, pg_service=pg_service), root)
```

換成

```python
def _backend(
    data_root: Path | None,
    configs_root: Path | None,
    backend: BackendName | str,
    pg_service: str | None,
) -> tuple[Path, Path, ProvenanceBackend]:
    """The data root, the configs root and the backend of this checkout. Every command resolves
    the configs root, the read-only ones too (spec 2026-10-04 §3.4): an index serves one."""
    root = resolve_data_root(data_root)
    name = _backend_name(backend)
    if pg_service is not None and name is not BackendName.POSTGRESQL:
        raise ValidationFailed("pg_service_requires_postgresql")
    configs = resolve_configs_root(configs_root)
    config = BackendConfig(name=name, pg_service=pg_service)
    return root, configs, make_backend(config, root, configs)


def _root(configs_root: Path | None) -> dict[str, FieldValue]:
    """``root=<configs root id>`` on every provenance VERDICT, a failure's included (spec
    2026-10-04 §7). A configs root that cannot be resolved has no id; the command then fails
    saying so."""
    try:
        return {"root": path_id(resolve_configs_root(configs_root))}
    except VcpError:
        return {}
```

在 `src/vcp/cli_provenance.py`，把

```python
        root, index = _backend(data_root, backend, pg_service)
        result = index.rebuild(root, resolve_configs_root(configs_root))
```

換成

```python
        root, configs, index = _backend(data_root, configs_root, backend, pg_service)
        result = index.rebuild(root, configs)
```

在 `src/vcp/cli_provenance.py`，把

```python
    run_command("provenance.rebuild", json_mode, data_root, fn)
```

換成

```python
    run_command("provenance.rebuild", json_mode, data_root, fn, context=_root(configs_root))
```

在 `src/vcp/cli_provenance.py`，把

```python
        root, index = _backend(data_root, backend_name, pg_service)
        result = index.ingest_diff(
            artifact_id,
            root,
            resolve_configs_root(configs_root),
```

換成

```python
        root, configs, index = _backend(data_root, configs_root, backend_name, pg_service)
        result = index.ingest_diff(
            artifact_id,
            root,
            configs,
```

在 `src/vcp/cli_provenance.py`，把

```python
        context={"artifact": artifact_id},
```

換成

```python
        context={"artifact": artifact_id, **_root(configs_root)},
```

在 `src/vcp/cli_provenance.py`，把

```python
        root, index = _backend(data_root, backend, pg_service)
        result = index.sync(root, resolve_configs_root(configs_root))
```

換成

```python
        root, configs, index = _backend(data_root, configs_root, backend, pg_service)
        result = index.sync(root, configs)
```

在 `src/vcp/cli_provenance.py`，把

```python
    run_command("provenance.sync", json_mode, data_root, fn)
```

換成

```python
    run_command("provenance.sync", json_mode, data_root, fn, context=_root(configs_root))
```

在 `src/vcp/cli_provenance.py`，把所有

```python
        _root, index = _backend(data_root, backend, pg_service)
```

全部換成

```python
        _data, _configs, index = _backend(data_root, configs_root, backend, pg_service)
```

在 `src/vcp/cli_provenance.py`，把

```python
    context: dict[str, FieldValue] = {"dataset": dataset}
```

換成

```python
    context: dict[str, FieldValue] = {"dataset": dataset, **_root(configs_root)}
```

在 `src/vcp/cli_provenance.py`，把

```python
    run_command("provenance.stale", json_mode, data_root, fn, context={"head": head})
```

換成

```python
    context = {"head": head, **_root(configs_root)}
    run_command("provenance.stale", json_mode, data_root, fn, context=context)
```

在 `src/vcp/cli_provenance.py`，把

```python
    run_command("provenance.explain", json_mode, data_root, fn, context={"entity": entity})
```

換成

```python
    context = {"entity": entity, **_root(configs_root)}
    run_command("provenance.explain", json_mode, data_root, fn, context=context)
```

在 `src/vcp/cli_provenance.py`，把

```python
        root, index = _backend(data_root, backend, pg_service)
        target, fmt = prepare_output(out, root)
```

換成

```python
        root, _configs, index = _backend(data_root, configs_root, backend, pg_service)
        target, fmt = prepare_output(out, root)
```

在 `src/vcp/cli_provenance.py`，把

```python
    run_command("provenance.graph", json_mode, data_root, fn, context={"out": str(out)})
```

換成

```python
    context = {"out": str(out), **_root(configs_root)}
    run_command("provenance.graph", json_mode, data_root, fn, context=context)
```

在 `src/vcp/cli_provenance.py`，把

```python
        root, index = _backend(data_root, backend, pg_service)
        with index.read_snapshot() as reader:
            stats = reader.stats()
            verified = reader.verify(root, resolve_configs_root(configs_root))
```

換成

```python
        root, configs, index = _backend(data_root, configs_root, backend, pg_service)
        with index.read_snapshot() as reader:
            stats = reader.stats()
            verified = reader.verify(root, configs)
```

在 `src/vcp/cli_provenance.py`，把

```python
        payload = {
            "backend": index.name.value,
            **stats,
```

換成

```python
        payload = {
            "backend": index.name.value,
            "index": index.location_label,  # where this checkout's index is (spec 2026-10-04 §11)
            **stats,
```

在 `src/vcp/cli_provenance.py`，把

```python
    run_command("provenance.status", json_mode, data_root, fn)
```

換成

```python
    run_command("provenance.status", json_mode, data_root, fn, context=_root(configs_root))
```

在 `src/vcp/cli_provenance.py`，把

```python
        root, index = _backend(data_root, backend, pg_service)
        result = index.verify(root, resolve_configs_root(configs_root))
```

換成

```python
        root, configs, index = _backend(data_root, configs_root, backend, pg_service)
        result = index.verify(root, configs)
```

在 `src/vcp/cli_provenance.py`，把

```python
    run_command("provenance.verify-index", json_mode, data_root, fn)
```

換成

```python
    run_command("provenance.verify-index", json_mode, data_root, fn, context=_root(configs_root))
```

- [ ] **Step 9: 既有呼叫改用新簽名**（只改呼叫，不改斷言的意思）

在 `tests/unit/provenance/test_index.py`，把所有

```python
provenance_index_path(roots.data)
```

全部換成

```python
provenance_index_path(roots.data, roots.configs)
```

在 `tests/unit/provenance/test_backend.py`，把所有

```python
provenance_index_path(roots.data)
```

全部換成

```python
provenance_index_path(roots.data, roots.configs)
```

在 `tests/unit/provenance/test_backend.py`，把所有

```python
make_backend(BackendConfig(), roots.data)
```

全部換成

```python
make_backend(BackendConfig(), roots.data, roots.configs)
```

在 `tests/unit/provenance/test_cli_graph.py`，把

```python
    index = provenance_index_path(roots.data)
```

換成

```python
    index = provenance_index_path(roots.data, roots.configs)
```

在 `tests/unit/provenance/test_cli_postgres.py`，把

```python
    def factory(config, data_root):
        configs.append((config, data_root))
```

換成

```python
    def factory(config, data_root, configs_root):
        configs.append((config, data_root))
```

在 `tests/unit/provenance/test_cli_postgres.py`，把

```python
    monkeypatch.setattr("vcp.cli_provenance.make_backend", lambda config, data_root: backend)
```

換成

```python
    monkeypatch.setattr("vcp.cli_provenance.make_backend", lambda config, data, configs: backend)
```

在 `tests/unit/provenance/test_postgres_connection.py`，把

```python
        make_backend(BackendConfig(BackendName.POSTGRESQL), roots.data)
```

換成

```python
        make_backend(BackendConfig(BackendName.POSTGRESQL), roots.data, roots.configs)
```

在 `tests/performance/provenance/real_validation.py`，把

```python
        index = ProvenanceIndex(provenance_index_path(data))
```

換成

```python
        index = ProvenanceIndex(provenance_index_path(data, configs))
```

`tests/performance/provenance/production_benchmark.py` 直接用 `_schema` 種出索引、再對它 `ingest_diff`；索引現在要記 root，種的時候一起寫（`tests/unit/provenance/test_adaptive_benchmark.py::test_production_runner_reuses_generator_and_preserves_legacy_result_fields` 會跑到這裡）。

在 `tests/performance/provenance/production_benchmark.py`，把

```python
    _schema,
    graph_hash,
)
from vcp.provenance.schema import (
```

換成

```python
    _schema,
    _write_roots,
    graph_hash,
)
from vcp.provenance.roots import IndexRoots
from vcp.provenance.schema import (
```

在 `tests/performance/provenance/production_benchmark.py`，把

```python
def _seed_index(path: Path, count: int, source_hash: str, target_hash: str) -> None:
```

換成

```python
def _seed_index(
    path: Path, count: int, source_hash: str, target_hash: str, roots: IndexRoots
) -> None:
```

在 `tests/performance/provenance/production_benchmark.py`，把

```python
    with connection:
        _schema(connection)
        for entity in graph.entities.values():
```

換成

```python
    with connection:
        _schema(connection)
        _write_roots(connection, roots)
        for entity in graph.entities.values():
```

在 `tests/performance/provenance/production_benchmark.py`，把

```python
    _seed_index(baseline, count, sha256_file(source), sha256_file(target))
```

換成

```python
    roots = IndexRoots.of(data_root, configs_root)
    _seed_index(baseline, count, sha256_file(source), sha256_file(target), roots)
```

- [ ] **Step 10: 跑測試，確認通過**

Run: `uv run pytest -o addopts="" -q tests/unit/provenance tests/unit/core/test_lock.py tests/unit/submit/test_transactions.py`
Expected: 全部 PASS（symlink 那一段在不能建 symlink 的 Windows 上提早 return）。

- [ ] **Step 11: Lint，然後 commit**

```bash
uv run ruff format src/vcp/core/paths.py src/vcp/core/lock.py src/vcp/provenance/roots.py src/vcp/provenance/index.py src/vcp/provenance/backend.py src/vcp/cli_provenance.py tests/unit/provenance/test_index_roots.py tests/unit/provenance/test_index.py tests/unit/provenance/test_backend.py tests/unit/provenance/test_cli_graph.py tests/unit/provenance/test_cli_postgres.py tests/unit/provenance/test_postgres_connection.py tests/performance/provenance/real_validation.py tests/performance/provenance/production_benchmark.py
uv run ruff check --fix src/vcp/core/paths.py src/vcp/core/lock.py src/vcp/provenance/roots.py src/vcp/provenance/index.py src/vcp/provenance/backend.py src/vcp/cli_provenance.py tests/unit/provenance/test_index_roots.py
uv run ruff check . && uv run ruff format --check .
git diff --check
git add src/vcp/core/paths.py src/vcp/core/lock.py src/vcp/provenance/roots.py src/vcp/provenance/index.py src/vcp/provenance/backend.py src/vcp/cli_provenance.py tests/unit/provenance/test_index_roots.py tests/unit/provenance/test_index.py tests/unit/provenance/test_backend.py tests/unit/provenance/test_cli_graph.py tests/unit/provenance/test_cli_postgres.py tests/unit/provenance/test_postgres_connection.py tests/performance/provenance/real_validation.py tests/performance/provenance/production_benchmark.py
git commit -F <訊息檔>
```

訊息：`fix: provenance 索引每個 configs root 一份並記下 root，root 不符先報 root_mismatch:（VCP-044）`

---

### Task 2: PostgreSQL generation 的 root（VCP-044 之二）

**Files:**
- Modify: `src/vcp/provenance/postgres.py`（import、root 輔助函式、`PostgresProvenanceBackend` 的建構、`rebuild` / `sync`、`_publish_generation`、`ingest_diff`、`read_snapshot`、`_PostgresReader.verify`）
- Modify: `src/vcp/provenance/index.py`（`RebuildResult.replaced_roots`）
- Modify: `src/vcp/provenance/backend.py`（`make_backend` 把 roots 交給 PostgreSQL）
- Modify: `src/vcp/cli_provenance.py`（`rebuild` 取代別的 root 時 WARN）
- Test: `tests/unit/provenance/test_postgres_incremental.py`（import 與檔尾追加）

**Interfaces:**
- Consumes（Task 1）：`IndexRoots`、`ROOT_KEYS`、`check_roots`（`vcp.provenance.roots`）；`path_id`；`make_backend(config, data_root, configs_root)`；`_root(configs_root)`。
- Produces：
  - `RebuildResult.replaced_roots: dict[str, str] | None = None`：`None` = 沒有取代別人的 generation；`{}` = 取代了沒記 root 的（0.13.0 以前建的）；其他 = 被取代的 generation 記下的四個 root 鍵。SQLite 永遠是 `None`。
  - `PostgresProvenanceBackend(config, *, roots: IndexRoots | None = None)`；`read_snapshot()` 在 `roots` 不是 `None` 時先比對 root。
  - `postgres._generation_roots(connection, generation_id) -> dict[str, str]`、`postgres._check_generation_roots(connection, generation_id, roots) -> None`。
  - `vcp provenance rebuild`：取代別的 root → WARN、VERDICT `replaced_root=<被取代的 configs root id>`；取代沒記 root 的 → 仍 OK，人類訊息說明。

設計說明：
- 每個 generation 的 `metadata` 多四個鍵（`configs_root_id`、`configs_root`、`data_root_id`、`data_root`），不改 DDL，`POSTGRES_SCHEMA_VERSION` 維持 1。
- `sync` 與 `rebuild` 共用 `_publish_full`；差別只在拿到寫入鎖、讀出前一個 generation 之後：`sync`（`takeover=False`）比對不符就 `root_mismatch:`、整個交易 rollback；`rebuild`（`takeover=True`）照樣發布，回報被取代的 root。資料庫還沒有 generation 時兩者都直接發布（跟 0.12 一樣）。
- PostgreSQL 的 `sync` 在交易內、任何寫入之前比對；canonical 圖仍在交易外先建（跟 0.12 相同，不拉長寫入鎖的時間）。它沒有前綴檢查，所以不會出現「became shorter」。
- 讀取命令（`impact`、`stale`、`explain`、`graph`、`status`）經 `read_snapshot`：CLI 用 `make_backend` 建的後端帶著 roots，在同一個 MVCC 快照裡比對，不會在比對與讀取之間被別的 rebuild 換掉。`_PostgresReader.verify` 另外比對呼叫時傳入的 roots。

- [ ] **Step 1: 寫失敗的測試** — `tests/unit/provenance/test_postgres_incremental.py`

在 `tests/unit/provenance/test_postgres_incremental.py`，把

```python
import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from helpers import dataset_with_perfect_run, det_samples, det_with_runs, make_card
from vcp.core.errors import IntegrityError
from vcp.core.paths import DatasetPaths
```

換成

```python
import json
import shutil
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from helpers import dataset_with_perfect_run, det_samples, det_with_runs, make_card
from vcp.cli import app
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.paths import DatasetPaths, path_id
```

在 `tests/unit/provenance/test_postgres_incremental.py`，把

```python
from vcp.provenance.index import ProvenanceIndex, graph_hash
from vcp.provenance.views import compute_statuses
```

換成

```python
from vcp.provenance.index import ProvenanceIndex, graph_hash
from vcp.provenance.roots import IndexRoots
from vcp.provenance.views import compute_statuses
```

在 `tests/unit/provenance/test_postgres_incremental.py` 末尾追加：

```python


# --- VCP-044: one database serves one checkout (spec 2026-10-04 §3.3, §9) --------------------


def _second_checkout(roots, tmp_path):
    """Another configs root on the same data root: a second checkout of the same git content."""
    other = tmp_path / "configs-b"
    shutil.copytree(roots.configs, other)
    return other


def _verdict(output: str) -> str:
    return [line for line in output.splitlines() if line.startswith("VERDICT ")][-1]


def test_postgres_generation_records_its_roots(fake_postgres, roots):
    _versions(roots)
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    stats = fake_postgres.backend.stats()
    assert stats["configs_root_id"] == path_id(roots.configs)
    assert stats["data_root_id"] == path_id(roots.data)
    assert stats["configs_root"] == roots.configs.resolve().as_posix()
    assert stats["data_root"] == roots.data.resolve().as_posix()


def test_postgres_verify_and_ingest_from_another_root_fail_before_the_prefix_check(
    fake_postgres, roots, tmp_path
):
    _versions(roots)
    ledger = roots.configs / "datasets" / "idx-old" / "events.jsonl"
    ledger.write_text('{"event":"both checkouts"}\n', encoding="utf-8", newline="\n")
    other = _second_checkout(roots, tmp_path)
    with ledger.open("a", encoding="utf-8", newline="\n") as f:
        f.write('{"event":"only checkout A"}\n')
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    _diff(roots)
    before = fake_postgres.snapshot()
    with pytest.raises(ValidationFailed, match="root_mismatch:") as ei:
        fake_postgres.backend.verify(roots.data, other)
    assert ei.value.fields == {"index_root": path_id(roots.configs)}
    with pytest.raises(ValidationFailed, match="root_mismatch:") as ei:
        fake_postgres.backend.ingest_diff("idx-diff", roots.data, other)
    assert "prefix" not in str(ei.value) and fake_postgres.snapshot() == before
    rooted = postgres.PostgresProvenanceBackend(
        BackendConfig(BackendName.POSTGRESQL), roots=IndexRoots.of(roots.data, other)
    )
    with pytest.raises(ValidationFailed, match="root_mismatch:"):
        rooted.load_graph()  # impact, stale, explain and graph read through read_snapshot


def test_postgres_sync_never_replaces_another_roots_generation(fake_postgres, roots, tmp_path):
    _versions(roots)
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    other = _second_checkout(roots, tmp_path)
    before = fake_postgres.snapshot()
    with pytest.raises(ValidationFailed, match="root_mismatch:") as ei:
        fake_postgres.backend.sync(roots.data, other)
    assert ei.value.fields == {"index_root": path_id(roots.configs)}
    assert fake_postgres.snapshot() == before
    assert fake_postgres.backend.sync(roots.data, roots.configs).replaced_roots is None


def test_postgres_rebuild_over_another_root_replaces_it_and_warns(fake_postgres, roots, tmp_path):
    _versions(roots)
    other = _second_checkout(roots, tmp_path)
    runner = CliRunner()
    base = ["provenance", "rebuild", "--backend", "postgresql", "--data-root", str(roots.data)]
    first = runner.invoke(app, [*base, "--configs-root", str(roots.configs)])
    assert first.exit_code == 0 and "status=OK" in _verdict(first.output), first.output
    second = runner.invoke(app, [*base, "--configs-root", str(other)])
    verdict = _verdict(second.output)
    assert second.exit_code == 0 and "status=WARN" in verdict, second.output
    assert f"replaced_root={path_id(roots.configs)}" in verdict
    assert f"root={path_id(other)}" in verdict
    assert "one database serves one checkout" in second.output
    status = runner.invoke(
        app,
        ["provenance", "status", "--backend", "postgresql", "--data-root", str(roots.data)]
        + ["--configs-root", str(roots.configs)],
    )
    verdict = _verdict(status.output)
    assert status.exit_code == 1 and "root_mismatch:" in verdict, status.output
    assert f"index_root={path_id(other)}" in verdict


def test_postgres_generation_without_roots_fails_closed_until_rebuilt(fake_postgres, roots):
    _versions(roots)
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    fake_postgres.db.execute(
        "DELETE FROM vcp_provenance.metadata WHERE key IN "
        "('configs_root_id', 'configs_root', 'data_root_id', 'data_root')"
    )  # what vcp 0.12 published
    _diff(roots)
    rooted = postgres.PostgresProvenanceBackend(
        BackendConfig(BackendName.POSTGRESQL), roots=IndexRoots.of(roots.data, roots.configs)
    )
    for attempt in (
        lambda: fake_postgres.backend.verify(roots.data, roots.configs),
        lambda: fake_postgres.backend.ingest_diff("idx-diff", roots.data, roots.configs),
        lambda: fake_postgres.backend.sync(roots.data, roots.configs),
        rooted.load_graph,
    ):
        with pytest.raises(ValidationFailed, match="records no root") as ei:
            attempt()
        assert ei.value.fields == {"index_root": "none"}
    assert fake_postgres.backend.rebuild(roots.data, roots.configs).replaced_roots == {}
    assert fake_postgres.backend.verify(roots.data, roots.configs).ok
    assert rooted.load_graph().entities


def test_postgres_another_data_root_fails_root_mismatch(fake_postgres, roots, tmp_path):
    _versions(roots)
    fake_postgres.backend.rebuild(roots.data, roots.configs)
    with pytest.raises(ValidationFailed, match="root_mismatch:") as ei:
        fake_postgres.backend.verify(tmp_path / "another-data", roots.configs)
    assert ei.value.fields == {"index_root": path_id(roots.configs)}
```

- [ ] **Step 2: 跑測試，確認失敗**

Run: `uv run pytest -o addopts="" -q tests/unit/provenance/test_postgres_incremental.py`
Expected: FAIL：新的六個測試失敗（`KeyError: 'configs_root_id'`、`DID NOT RAISE`、`unexpected keyword argument 'roots'` 之類），原有的測試仍 PASS。

- [ ] **Step 3: `RebuildResult` 記下被取代的 root** — `src/vcp/provenance/index.py`

在 `src/vcp/provenance/index.py`，把

```python
@dataclass(frozen=True)
class RebuildResult:
    entities: int
    edges: int
    changes: int
    heads: int
    graph_hash: str
```

換成

```python
@dataclass(frozen=True)
class RebuildResult:
    entities: int
    edges: int
    changes: int
    heads: int
    graph_hash: str
    # PostgreSQL only (spec 2026-10-04 §3.3): the roots of the generation a rebuild replaced when
    # another checkout built it -- ``{}`` when that generation recorded none -- else None.
    replaced_roots: dict[str, str] | None = None
```

- [ ] **Step 4: generation 記 root；`sync` 拒絕、`rebuild` 取代；讀與 ingest 先比對** — `src/vcp/provenance/postgres.py`

在 `src/vcp/provenance/postgres.py`，把

```python
    validate_schema_in_transaction,
)
from vcp.provenance.schema import ProvenanceEdge, ProvenanceEntity, SampleChange, StatusRecord
```

換成

```python
    validate_schema_in_transaction,
)
from vcp.provenance.roots import ROOT_KEYS, IndexRoots, check_roots
from vcp.provenance.schema import ProvenanceEdge, ProvenanceEntity, SampleChange, StatusRecord
```

在 `src/vcp/provenance/postgres.py`，把

```python
    if generation_id is None:
        raise ValidationFailed("not_found: PostgreSQL provenance generation; run rebuild")
    return generation_id
```

換成

```python
    if generation_id is None:
        raise ValidationFailed("not_found: PostgreSQL provenance generation; run rebuild")
    return generation_id


_POSTGRES_REMEDY = (
    "one database serves one checkout: give this checkout its own --pg-service, or run "
    "`vcp provenance rebuild` to take this database over"
)


def _generation_roots(connection: Any, generation_id: UUID | str) -> dict[str, str]:
    """The root keys a generation recorded; none before 0.13.0 (spec 2026-10-04 §3.3)."""
    rows = connection.execute(
        f"SELECT key, value FROM {SCHEMA_NAME}.metadata WHERE generation_id=%s ORDER BY key",
        (generation_id,),
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows if row[0] in ROOT_KEYS}


def _check_generation_roots(connection: Any, generation_id: UUID | str, roots: IndexRoots) -> None:
    """``root_mismatch:`` unless the generation records these roots, before any replay or
    prefix check. A generation 0.12 published records none and fails closed until a rebuild."""
    recorded = _generation_roots(connection, generation_id)
    remedy = _POSTGRES_REMEDY if recorded else "run `vcp provenance rebuild`"
    check_roots(recorded, roots, remedy=remedy)
```

在 `src/vcp/provenance/postgres.py`，把

```python
    def __init__(self, config: BackendConfig) -> None:
        self.config = BackendConfig(
            name=self.name, pg_service=validate_pg_service(config.pg_service)
        )
        self._psycopg = _load_psycopg()

    def rebuild(self, data_root: Path, configs_root: Path) -> RebuildResult:
        data_root = Path(data_root).resolve()
        configs_root = Path(configs_root).resolve()
        before = _canonical_snapshot(data_root, configs_root)
```

換成

```python
    def __init__(self, config: BackendConfig, *, roots: IndexRoots | None = None) -> None:
        """``roots``: the checkout this database must serve; every read snapshot checks them
        first (spec 2026-10-04 §3.4)."""
        self.config = BackendConfig(
            name=self.name, pg_service=validate_pg_service(config.pg_service)
        )
        self._psycopg = _load_psycopg()
        self.roots = roots

    def rebuild(self, data_root: Path, configs_root: Path) -> RebuildResult:
        """The full publication path. It is the documented recovery, so it may replace a
        generation another checkout built, or one that records no root; ``replaced_roots``
        then says whose (spec 2026-10-04 §3.3)."""
        return self._publish_full(data_root, configs_root, takeover=True)

    def _publish_full(
        self, data_root: Path, configs_root: Path, *, takeover: bool
    ) -> RebuildResult:
        data_root = Path(data_root).resolve()
        configs_root = Path(configs_root).resolve()
        roots = IndexRoots.of(data_root, configs_root)
        before = _canonical_snapshot(data_root, configs_root)
```

在 `src/vcp/provenance/postgres.py`，把

```python
        artifact_rows = _ingested_artifact_rows(generation_id, canonical, data_root)
        with _connection(self.config, self._psycopg) as connection:
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
                install_schema_in_transaction(connection)
                previous = _scalar(
                    connection.execute(
                        f"SELECT generation_id FROM {SCHEMA_NAME}.active_generation "
                        "WHERE singleton=TRUE"
                    ).fetchone()
                )
                self._publish_generation(
```

換成

```python
        artifact_rows = _ingested_artifact_rows(generation_id, canonical, data_root)
        replaced: dict[str, str] | None = None
        with _connection(self.config, self._psycopg) as connection:
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
                install_schema_in_transaction(connection)
                previous = _scalar(
                    connection.execute(
                        f"SELECT generation_id FROM {SCHEMA_NAME}.active_generation "
                        "WHERE singleton=TRUE"
                    ).fetchone()
                )
                if previous is not None and not takeover:
                    _check_generation_roots(connection, previous, roots)
                elif previous is not None:
                    recorded = _generation_roots(connection, previous)
                    replaced = None if roots.matches(recorded) else recorded
                self._publish_generation(
```

在 `src/vcp/provenance/postgres.py`，把

```python
            heads=len(dataset_heads(canonical)),
            graph_hash=digest,
        )

    def _publish_generation(
```

換成

```python
            heads=len(dataset_heads(canonical)),
            graph_hash=digest,
            replaced_roots=replaced,
        )

    def _publish_generation(
```

在 `src/vcp/provenance/postgres.py`，把

```python
                (generation_id, "canonical_snapshot", _json(before)),
            ),
        )
```

換成

```python
                (generation_id, "canonical_snapshot", _json(before)),
                *(
                    (generation_id, key, value)
                    for key, value in IndexRoots.of(data_root, configs_root).metadata().items()
                ),
            ),
        )
```

在 `src/vcp/provenance/postgres.py`，把

```python
    def sync(self, data_root: Path, configs_root: Path) -> RebuildResult:
        """PostgreSQL v1 sync deliberately uses the same atomic full publication path."""
        return self.rebuild(data_root, configs_root)
```

換成

```python
    def sync(self, data_root: Path, configs_root: Path) -> RebuildResult:
        """PostgreSQL v1 sync deliberately uses the same atomic full publication path -- but
        never over a generation another checkout built, or one that records no root:
        ``root_mismatch:``, and nothing is written (spec 2026-10-04 §3.3)."""
        return self._publish_full(data_root, configs_root, takeover=False)
```

在 `src/vcp/provenance/postgres.py`，把

```python
                policy = load_policy()
                generation_id = _active_generation(connection)
                original_checkpoints = _verify_incremental_evidence(
```

換成

```python
                policy = load_policy()
                generation_id = _active_generation(connection)
                roots = IndexRoots.of(data_root, configs_root)
                _check_generation_roots(connection, generation_id, roots)
                original_checkpoints = _verify_incremental_evidence(
```

在 `src/vcp/provenance/postgres.py`，把

```python
        """Pin one generation and MVCC snapshot for all reads in this context."""
        with _read_transaction(self.config, self._psycopg) as (connection, generation_id):
            yield _PostgresReader(connection, generation_id)
```

換成

```python
        """Pin one generation and MVCC snapshot for all reads in this context; a backend made
        for a checkout first checks that generation serves it (spec 2026-10-04 §3.4)."""
        with _read_transaction(self.config, self._psycopg) as (connection, generation_id):
            if self.roots is not None:
                _check_generation_roots(connection, generation_id, self.roots)
            yield _PostgresReader(connection, generation_id)
```

在 `src/vcp/provenance/postgres.py`，把

```python
    def verify(self, data_root: Path, configs_root: Path) -> VerifyIndexResult:
        data_root = Path(data_root).resolve()
        configs_root = Path(configs_root).resolve()
        canonical_snapshot = _canonical_snapshot(data_root, configs_root)
```

換成

```python
    def verify(self, data_root: Path, configs_root: Path) -> VerifyIndexResult:
        data_root = Path(data_root).resolve()
        configs_root = Path(configs_root).resolve()
        roots = IndexRoots.of(data_root, configs_root)
        _check_generation_roots(self.connection, self.generation_id, roots)
        canonical_snapshot = _canonical_snapshot(data_root, configs_root)
```

- [ ] **Step 5: 工廠把 roots 交給 PostgreSQL** — `src/vcp/provenance/backend.py`

在 `src/vcp/provenance/backend.py`，把

```python
    return module.PostgresProvenanceBackend(config)
```

換成

```python
    return module.PostgresProvenanceBackend(config, roots=roots)
```

- [ ] **Step 6: `rebuild` 取代別的 root 時 WARN** — `src/vcp/cli_provenance.py`

在 `src/vcp/cli_provenance.py`，把

```python
        human = [f"rebuilt {index.location_label}", f"graph={result.graph_hash}"]
        return "OK", fields, _payload(index, result.__dict__), human
```

換成

```python
        human = [f"rebuilt {index.location_label}", f"graph={result.graph_hash}"]
        status: Status = "OK"
        replaced = result.replaced_roots
        if replaced:  # spec 2026-10-04 §3.3: another checkout's generation is gone now
            status = "WARN"
            fields["replaced_root"] = replaced.get("configs_root_id", "none")
            human.append(
                f"replaced the index of configs root {replaced.get('configs_root')} and data root "
                f"{replaced.get('data_root')}: one database serves one checkout, so that checkout "
                "needs its own --pg-service now"
            )
        elif replaced is not None:
            human.append("replaced a generation that records no root (built before vcp 0.13.0)")
        return status, fields, _payload(index, result.__dict__), human
```

- [ ] **Step 7: 跑測試，確認通過**

Run: `uv run pytest -o addopts="" -q tests/unit/provenance`
Expected: 全部 PASS；需要真 PostgreSQL 服務的整合測試照舊 skip。

- [ ] **Step 8: Lint，然後 commit**

```bash
uv run ruff format src/vcp/provenance/postgres.py src/vcp/provenance/index.py src/vcp/provenance/backend.py src/vcp/cli_provenance.py tests/unit/provenance/test_postgres_incremental.py
uv run ruff check --fix src/vcp/provenance/postgres.py src/vcp/provenance/index.py src/vcp/provenance/backend.py src/vcp/cli_provenance.py tests/unit/provenance/test_postgres_incremental.py
uv run ruff check . && uv run ruff format --check .
git diff --check
git add src/vcp/provenance/postgres.py src/vcp/provenance/index.py src/vcp/provenance/backend.py src/vcp/cli_provenance.py tests/unit/provenance/test_postgres_incremental.py
git commit -F <訊息檔>
```

訊息：`fix: PostgreSQL generation 記下 root，sync 不取代別的 root、rebuild 取代時 WARN（VCP-044）`

---

### Task 3: 備份清單的完整性（VCP-045）

**Files:**
- Modify: `src/vcp/train/checkpoints.py`（新 `newest_per_path`）
- Modify: `src/vcp/train/upload.py`（`_targets` 改用它）
- Modify: `src/vcp/backup/manifest.py`（從 `Collector.locate` 抽出 `external_path`、`locate_file`、`entry_key`）
- Create: `src/vcp/backup/completeness.py`
- Modify: `src/vcp/backup/evidence.py`（import、`Collector.locate`、`_checkpoints`、`build_manifest` 的時戳與自檢）
- Modify: `src/vcp/backup/schema.py`（`BackupRow.incomplete`）
- Modify: `src/vcp/backup/verify.py`（`REASONS`、`VerifyResult.incomplete`、`_train_record`、`verify`）
- Modify: `src/vcp/backup/status.py`（整份改寫：每份清單重算完整性）
- Modify: `src/vcp/backup/push.py`（tier 3 預檢、`--forget-remote`）
- Modify: `src/vcp/cli_backup.py`（`verify` 與 `status` 的 VERDICT、人類訊息、`--json`）
- Modify: `tests/backup_fixtures.py`（`FOLDS`、`register_folds`、`write_old_manifest`）
- Test: `tests/unit/backup/test_completeness.py`（新建）

**Interfaces:**
- Consumes：無（跟 Task 1、2 無關）。
- Produces：
  - `vcp.train.checkpoints.newest_per_path(checkpoints: Iterable[CheckpointRecord]) -> dict[str, CheckpointRecord]`。
  - `vcp.backup.manifest.external_path(path) -> str`、`locate_file(path, data_root, configs_root) -> tuple[str, str, str | None]`、`entry_key(path, data_root, configs_root) -> str`；`vcp.backup.evidence.external_path` 仍可 import（再匯出）。
  - `vcp.backup.completeness`：`RECORD_ROLES = ("run_card", "train_record")`；`Gap(run: str, what: str, key: str)`（frozen）；`checkable(manifest, paths) -> bool`；`manifest_gaps(manifest: Manifest, paths: DatasetPaths) -> list[Gap]`。
  - `BackupRow.incomplete: int | None = None`（只在大於 0 時寫出）。
  - `VerifyResult.incomplete: list[Gap]`（預設空）；`verify.REASONS[0] == "manifest_incomplete"`。
  - `vcp.backup.status`：`ManifestStatus.incomplete: int | None`（`None` = 這台機器判斷不了）、`.completeness -> "complete" | "incomplete" | "unchecked"`；`StatusView.incomplete`、`.unchecked`（清單 id 列表）；`gaps_of(manifest, paths, verifies) -> int | None`；`COMPLETE_SINCE = (0, 10, 0)`。
  - VERDICT：`backup verify` 一律帶 `incomplete=`；`backup status` 帶 `incomplete=<缺檔的清單數>`；`backup push --tier 3` FAIL `manifest_incomplete:`（`incomplete=`）；`--forget-remote` 遇到缺檔 FAIL `forget_refused:`（`incomplete=`）；`backup manifest` 自檢失敗 ABORT `manifest_incomplete:`（`incomplete=`）。

設計說明（spec 沒寫細的地方）：
- 清單的 `created_at` 改在走訪**之前**取：走訪途中才登記的權重，`registered_at` 一定晚於 `created_at`，不會被當成缺口，自檢也就只可能被建清單程式的 bug 觸發。
- 讀不到的 `created_at`（被手改）當成「無限晚」：每一筆都必須列出。讀不到的 `registered_at` / `attached_at` 也算必須（時戳層會另外報）。
- 「本機沒有 run 紀錄」= 清單列出的 `run.yaml` / `train.yaml`（`run_card`、`train_record`）有任何一個不在這台機器上。這時 `status` 退回看 verify 列的 `incomplete`，再退回看清單的 `vcp_version`。清單檔本身讀不到時也是 `unchecked`。
- 同一個檔被兩份紀錄要求（例如 `run.yaml` 與 `train.yaml` 掛同一份證據），只算一個缺口（依清單鍵去重）。
- `--tier 3 --forget-remote` 遇到缺檔，先報 tier 3 的 `manifest_incomplete:`（動任何檔案之前）；`--forget-remote` 搭 tier 1、2 時照推，最後 `forget_refused:` 帶 `incomplete=`。

- [ ] **Step 1: 共用的測試輔助** — `tests/backup_fixtures.py`

在 `tests/backup_fixtures.py`，把

```python
from vcp.core.hashing import sha256_file
from vcp.fuse.build import write_record
```

換成

```python
from vcp.backup.evidence import Collector, parse_conclusion
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import write_manifest
from vcp.backup.schema import BackupRow, Manifest
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.fuse.build import write_record
```

在 `tests/backup_fixtures.py`，把

```python
from vcp.train.records import append_event, save_record, train_dir
```

換成

```python
from vcp.train.records import append_event, load_record, save_record, train_dir
```

在 `tests/backup_fixtures.py` 末尾追加：

```python


FOLDS = [f"data/work/good/fold-{k}/model.pt" for k in range(5)]


def register_folds(world, count: int = 5) -> list[Path]:
    """Folds that each write ``model.pt`` (VCP-035, VCP-045), registered on run ``good``."""
    folds = []
    for k in range(count):
        fold = world.roots.data / "work" / "good" / f"fold-{k}" / "model.pt"
        fold.parent.mkdir(parents=True, exist_ok=True)
        fold.write_bytes(f"fold {k} weights".encode())
        folds.append(fold)
    record, _ = register(
        load_record(world.roots.data, "good"), folds, data_root=world.roots.data, attempt=2
    )
    save_record(world.roots.data, record)
    return folds


def write_old_manifest(
    world, dataset: str, conclusion: str, manifest_id: str, *, drop: set[str], version="0.9.1"
) -> Manifest:
    """A manifest the way an older vcp wrote it, with its ledger row: today's walk less the
    entries in ``drop`` (manifest keys), and no self-check. Before 0.10.0 the five folds of
    ``register_folds`` were listed as the last one alone (VCP-045)."""
    paths = DatasetPaths.resolve(
        dataset, data_root=world.roots.data, configs_root=world.roots.configs
    )
    created = stamp()
    col = Collector(paths.data_root, paths.configs_root)
    kind, ident = parse_conclusion(conclusion)
    if kind == "run":
        col.walk_run(ident, conclusion)
    elif kind == "judgement":
        col.walk_judgement(paths, ident, conclusion)
    elif kind == "submission":
        col.walk_submission(paths, ident, conclusion)
    else:
        col.walk_all(paths)
    files = [f for f in col.files_of() if f.key not in drop]
    manifest = Manifest(
        manifest_id=manifest_id,
        dataset=dataset,
        conclusion=conclusion,
        created_at=created,
        vcp_version=version,
        data_root=paths.data_root.as_posix(),
        files=files,
    )
    write_manifest(paths, manifest)
    BackupLedger(paths.backup_log).append(
        BackupRow(
            event="manifest",
            ts=stamp(),
            manifest_id=manifest_id,
            conclusion=conclusion,
            files=len(files),
            bytes_by_tier=manifest.bytes_by_tier(),
            missing=0,
            remote_copies=sum(1 for f in files if f.kind == "remote_copy"),
        )
    )
    return manifest
```

- [ ] **Step 2: 寫失敗的測試** — 新建 `tests/unit/backup/test_completeness.py`

```python
"""VCP-045 (spec 2026-10-04 §4, §9): a manifest is complete when it lists every checkpoint its
runs had registered -- and every evidence or label set they had attached -- by the time it was
written. verify, status, a tier-3 push, --forget-remote and the manifest's own self-check all
judge any manifest by that one rule, whatever its conclusion."""

import json

import pytest
from typer.testing import CliRunner

from backup_fixtures import FOLDS, FakeRemote, register_folds, write_old_manifest
from helpers import make_label_set
from submit_fixtures import EVAL, TEST
from vcp.backup import dest as destmod
from vcp.backup import evidence
from vcp.backup.completeness import manifest_gaps
from vcp.backup.evidence import build_manifest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import entry_key
from vcp.backup.push import push
from vcp.backup.schema import BackupRow
from vcp.backup.status import status
from vcp.backup.verify import verify
from vcp.cli import app
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.evidence import RunScope, label_ref
from vcp.data.split import load_plan
from vcp.measure.runs import load_run, save_run
from vcp.train.checkpoints import register
from vcp.train.records import load_record, save_record, train_dir, train_yaml

runner = CliRunner()


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _paths(world, name=EVAL):
    return DatasetPaths.resolve(name, **_kw(world))


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _old_091(world, dataset=EVAL, conclusion="run:good"):
    """Five folds that each wrote model.pt, and the manifest vcp 0.9.1 wrote of them: fold 4."""
    register_folds(world)
    return write_old_manifest(world, dataset, conclusion, "old-091", drop=set(FOLDS[:4]))


def _register(world, files):
    record, _ = register(
        load_record(world.roots.data, "good"), files, data_root=world.roots.data, attempt=3
    )
    save_record(world.roots.data, record)


# --- the rule ---------------------------------------------------------------------------------


def test_a_manifest_written_the_091_way_lists_one_fold_and_is_incomplete(world):
    gaps = manifest_gaps(_old_091(world), _paths(world))
    assert [g.key for g in gaps] == FOLDS[:4]
    assert gaps[0].run == "good"
    assert gaps[0].what == "good/train.yaml:checkpoints.work/good/fold-0/model.pt"


def test_a_manifest_built_today_of_five_folds_is_complete(world):
    register_folds(world)
    res = build_manifest(EVAL, "run:good", manifest_id="today", **_kw(world))
    assert set(FOLDS) <= {f.key for f in res.manifest.files}
    assert manifest_gaps(res.manifest, _paths(world)) == []


def test_checkpoints_registered_after_the_manifest_are_not_required(world):
    res = build_manifest(EVAL, "run:good", manifest_id="before", **_kw(world))
    register_folds(world)  # the manifest is stale now -- drift says so -- not incomplete
    assert manifest_gaps(res.manifest, _paths(world)) == []


def test_a_re_registered_path_is_required_once(world):
    folds = register_folds(world, 1)
    folds[0].write_bytes(b"fold 0 weights, resumed")
    _register(world, folds)
    old = write_old_manifest(world, EVAL, "run:good", "old", drop={FOLDS[0]})
    assert [g.key for g in manifest_gaps(old, _paths(world))] == [FOLDS[0]]


def test_a_checkpoint_listed_as_a_train_dir_file_counts_as_listed(world):
    inside = train_dir(world.roots.data, "good") / "ckpt" / "epoch3.pt"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"epoch 3")
    _register(world, [inside])
    res = build_manifest(EVAL, "run:good", manifest_id="m", **_kw(world))
    entry = {f.key: f for f in res.manifest.files}["data/runs/good/train/ckpt/epoch3.pt"]
    assert (entry.role, entry.tier) == ("train_dir", 2)
    assert manifest_gaps(res.manifest, _paths(world)) == []


def test_an_external_checkpoint_is_keyed_the_way_the_walk_keys_it(world, tmp_path):
    outside = tmp_path / "elsewhere" / "extra.pt"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"extra")
    _register(world, [outside])
    key = entry_key(outside, world.roots.data, world.roots.configs)
    assert key.startswith("external/")
    old = write_old_manifest(world, EVAL, "run:good", "old", drop={key})
    assert [g.key for g in manifest_gaps(old, _paths(world))] == [key]


@pytest.mark.parametrize(
    ("dataset", "conclusion"),
    [(EVAL, "run:good"), (EVAL, "judgement:p-good"), (TEST, "submission:S1"), (EVAL, "all")],
)
def test_gaps_are_found_whatever_the_conclusion(world, dataset, conclusion):
    old = _old_091(world, dataset, conclusion)
    assert [g.key for g in manifest_gaps(old, _paths(world, dataset))] == FOLDS[:4]


def test_evidence_attached_before_the_manifest_must_be_listed(world, tmp_path):
    make_label_set(world.roots, tmp_path, load_plan(_paths(world), "fixed-v1"), dataset=EVAL)
    card = load_run(world.roots.data, "good")
    scope = RunScope("good", EVAL, card.samples_hash, "fixed-v1", tuple(card.trained_on))
    ref = label_ref(world.roots.data, scope, "pseudo-v1", attempt=None, binding="manual")
    save_run(world.roots.data, card.model_copy(update={"evidence": [ref]}))
    key = "data/artifacts/label_set/pseudo-v1/manifest.json"
    old = write_old_manifest(world, EVAL, "run:good", "old", drop={key})
    what = "good/run.yaml:evidence.label_set/pseudo-v1"
    assert [(g.key, g.what) for g in manifest_gaps(old, _paths(world))] == [(key, what)]


def test_an_unreadable_train_yaml_fails_as_in_the_consistency_layer(world):
    res = build_manifest(EVAL, "run:good", manifest_id="m", **_kw(world))
    train_yaml(world.roots.data, "good").write_bytes(b"run_id: [unclosed\n")
    with pytest.raises(ValidationFailed, match="invalid YAML"):
        manifest_gaps(res.manifest, _paths(world))


# --- verify -----------------------------------------------------------------------------------


def test_verify_reports_manifest_incomplete_with_its_count(world):
    _old_091(world)
    res = verify(EVAL, "old-091", **_kw(world))
    assert not res.ok and res.reason == "manifest_incomplete" and len(res.incomplete) == 4
    first = "manifest_incomplete:good/train.yaml:checkpoints.work/good/fold-0/model.pt"
    assert res.problems[0] == first
    assert BackupLedger(_paths(world).backup_log).latest("verify", "old-091").incomplete == 4
    result = runner.invoke(app, ["backup", "verify", "--dataset", EVAL, "--manifest", "old-091"])
    verdict = _verdict(result.output)
    assert result.exit_code == 1 and "reason=manifest_incomplete" in verdict
    assert "incomplete=4" in verdict and "drift=0" in verdict


def test_the_verify_row_of_a_complete_manifest_is_written_as_before(world):
    build_manifest(EVAL, "run:good", manifest_id="m", **_kw(world))
    res = verify(EVAL, "m", **_kw(world))
    assert res.ok and res.incomplete == []
    row = _paths(world).backup_log.read_text(encoding="utf-8").splitlines()[-1]
    assert set(json.loads(row)) == {"event", "ts", "manifest_id", "drift", "bad_stamps"}
    result = runner.invoke(app, ["backup", "verify", "--dataset", EVAL, "--manifest", "m"])
    assert result.exit_code == 0 and "incomplete=0" in _verdict(result.output)


# --- status -----------------------------------------------------------------------------------


def _row_012_wrote(world, manifest_id):
    """A passing verify row of vcp 0.12: copies checked at a destination, no ``incomplete``."""
    BackupLedger(_paths(world).backup_log).append(
        BackupRow(
            event="verify",
            ts=stamp(),
            manifest_id=manifest_id,
            dest=str(world.tmp / "vault"),
            tier=3,
            copies={"ok": 9, "missing": 0, "mismatch": 0, "absent": 0},
            drift=0,
            bad_stamps=0,
        )
    )


def test_status_recomputes_so_an_old_passing_row_cannot_vouch(world, monkeypatch):
    monkeypatch.setattr(destmod.shutil, "which", lambda name, *a, **k: None)  # no real rclone
    _old_091(world)
    _row_012_wrote(world, "old-091")
    [m] = status(EVAL, **_kw(world)).manifests
    assert (m.verified, m.local_ok) == (False, False)
    assert (m.incomplete, m.completeness) == (4, "incomplete")
    result = runner.invoke(app, ["backup", "status", "--dataset", EVAL])
    verdict = _verdict(result.output)
    assert result.exit_code == 0 and "status=WARN" in verdict and "incomplete=1" in verdict
    note = "manifest_incomplete: these list fewer files than their runs registered: old-091"
    assert note in result.output


def test_status_without_the_run_records_falls_back_to_the_rows(world):
    _old_091(world)
    verify(EVAL, "old-091", **_kw(world))  # its row records incomplete=4
    train_yaml(world.roots.data, "good").unlink()  # a new machine, before a pull
    [m] = status(EVAL, runner=FakeRemote(conf=world.tmp / "rclone.conf"), **_kw(world)).manifests
    assert (m.incomplete, m.verified) == (4, False)


def test_status_without_the_run_records_cannot_check_a_manifest_older_than_010(world):
    write_old_manifest(world, EVAL, "run:good", "old", drop=set(), version="0.9.1")
    write_old_manifest(world, EVAL, "run:good", "new", drop=set(), version="0.10.0")
    train_yaml(world.roots.data, "good").unlink()
    view = status(EVAL, runner=FakeRemote(conf=world.tmp / "rclone.conf"), **_kw(world))
    by_id = {m.manifest_id: m for m in view.manifests}
    assert by_id["old"].completeness == "unchecked" and not by_id["old"].verified
    assert by_id["new"].completeness == "complete"
    assert view.unchecked == ["old"]


# --- push -------------------------------------------------------------------------------------


def test_a_tier_3_push_of_an_incomplete_manifest_fails_before_any_byte_moves(world):
    _old_091(world)
    vault = world.tmp / "vault"
    with pytest.raises(ValidationFailed, match="manifest_incomplete:") as ei:
        push(EVAL, "old-091", str(vault), tier=3, **_kw(world))
    assert ei.value.fields == {"incomplete": 4} and not vault.exists()
    assert BackupLedger(_paths(world).backup_log).of("push") == []
    for tier in (1, 2):  # the lower tiers hold no checkpoint: unaffected
        assert push(EVAL, "old-091", str(vault), tier=tier, **_kw(world)).failed == []


def test_forget_remote_refuses_an_incomplete_manifest(world):
    _old_091(world)
    remote = FakeRemote()
    with pytest.raises(ValidationFailed, match="forget_refused:") as ei:
        push(EVAL, "old-091", "fake:vault", tier=2, forget_remote=True, runner=remote, **_kw(world))
    assert ei.value.fields["incomplete"] == 4 and remote.deleted == []


# --- manifest ---------------------------------------------------------------------------------


def test_the_manifest_self_check_aborts_on_a_walk_that_drops_a_checkpoint(world, monkeypatch):
    monkeypatch.setattr(evidence.Collector, "_checkpoints", lambda self, record, conclusion: None)
    args = ["backup", "manifest", "--dataset", EVAL, "--conclusion", "run:good", "--id", "bug"]
    result = runner.invoke(app, args)
    verdict = _verdict(result.output)
    assert result.exit_code == 2 and "status=ABORT" in verdict, result.output
    assert "manifest_incomplete:" in verdict and "incomplete=2" in verdict
    assert not _paths(world).backup_manifest("bug").exists()
    assert BackupLedger(_paths(world).backup_log).of("manifest") == []
```

- [ ] **Step 3: 跑測試，確認失敗**

Run: `uv run pytest -o addopts="" -q tests/unit/backup/test_completeness.py`
Expected: 收集階段就 FAIL：`ModuleNotFoundError: No module named 'vcp.backup.completeness'`。

- [ ] **Step 4: 「每個路徑取最新一筆」抽成共用函式** — `src/vcp/train/checkpoints.py`、`src/vcp/train/upload.py`

在 `src/vcp/train/checkpoints.py`，把

```python
import glob as globlib
from pathlib import Path
```

換成

```python
import glob as globlib
from collections.abc import Iterable
from pathlib import Path
```

在 `src/vcp/train/checkpoints.py`，把

```python
def mark_final(record: TrainRecord, path: str, sha256: str) -> TrainRecord:
```

換成

```python
def newest_per_path(checkpoints: Iterable[CheckpointRecord]) -> dict[str, CheckpointRecord]:
    """The newest record of every checkpoint path, paths in the order first registered.
    Registration appends, so a later record of the SAME path is a ``--resume`` that changed its
    bytes -- history, not a second checkpoint -- while five folds that each write ``model.pt``
    are five paths (VCP-035). Backup's walk, its verify, its completeness rule and ``train
    upload`` all count checkpoints this one way."""
    newest: dict[str, CheckpointRecord] = {}
    for c in checkpoints:
        newest[c.path] = c
    return newest


def mark_final(record: TrainRecord, path: str, sha256: str) -> TrainRecord:
```

在 `src/vcp/train/upload.py`，把

```python
from vcp.core.time import stamp
from vcp.train.schema import CheckpointRecord, TrainRecord, UploadRecord
```

換成

```python
from vcp.core.time import stamp
from vcp.train.checkpoints import newest_per_path
from vcp.train.schema import CheckpointRecord, TrainRecord, UploadRecord
```

在 `src/vcp/train/upload.py`，把

```python
    current: dict[str, CheckpointRecord] = {}
    for c in record.checkpoints:
        current[c.path] = c
    names = remote_names(current)
```

換成

```python
    current = newest_per_path(record.checkpoints)
    names = remote_names(current)
```

- [ ] **Step 5: 清單鍵的算法抽到 `vcp.backup.manifest`** — `src/vcp/backup/manifest.py`、`src/vcp/backup/evidence.py`

在 `src/vcp/backup/manifest.py` 末尾追加：

```python


def external_path(path: Path) -> str:
    """``C:/x/y`` -> ``C/x/y``, ``/mnt/x`` -> ``mnt/x``: a relative posix path that keeps the
    origin."""
    return path.resolve().as_posix().replace(":", "").lstrip("/")


def locate_file(path: Path, data_root: Path, configs_root: Path) -> tuple[str, str, str | None]:
    """(root, relative posix path, source) a manifest lists ``path`` under: ``data`` / ``configs``
    by containment, else ``external`` with its absolute source. The inverse of ``local_path``."""
    resolved = path.resolve()
    for root, base in (("data", data_root), ("configs", configs_root)):
        try:
            return root, resolved.relative_to(base.resolve()).as_posix(), None
        except ValueError:
            continue
    return "external", external_path(resolved), resolved.as_posix()


def entry_key(path: Path, data_root: Path, configs_root: Path) -> str:
    """The manifest key ``<root>/<path>`` of a file: what the walk lists it under, and what the
    completeness rule looks for (spec 2026-10-04 §4.1)."""
    root, rel, _ = locate_file(path, data_root, configs_root)
    return f"{root}/{rel}"
```

在 `src/vcp/backup/evidence.py`，把

```python
from vcp.artifact import store
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import default_manifest_id, write_manifest
from vcp.backup.schema import ROLES, TIER_OF, BackupRow, FileEntry, Manifest, RemoteCopy
from vcp.core.build import build_string
from vcp.core.config import load_yaml_model
from vcp.core.errors import IntegrityError, ValidationFailed
```

換成

```python
from vcp.artifact import store
from vcp.backup.completeness import manifest_gaps
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import default_manifest_id, locate_file, write_manifest
from vcp.backup.manifest import external_path as external_path
from vcp.backup.schema import ROLES, TIER_OF, BackupRow, FileEntry, Manifest, RemoteCopy
from vcp.core.build import build_string
from vcp.core.config import load_yaml_model
from vcp.core.errors import IntegrityError, InvariantError, ValidationFailed
```

在 `src/vcp/backup/evidence.py`，把

```python
from vcp.submit.stage import load_staged, stage_json
from vcp.train.records import events_path, has_record, train_dir, train_yaml
from vcp.train.records import load_record as load_train_record
from vcp.train.schema import CheckpointRecord, TrainRecord
```

換成

```python
from vcp.submit.stage import load_staged, stage_json
from vcp.train.checkpoints import newest_per_path
from vcp.train.records import events_path, has_record, train_dir, train_yaml
from vcp.train.records import load_record as load_train_record
from vcp.train.schema import TrainRecord
```

在 `src/vcp/backup/evidence.py`，把

```python
def external_path(path: Path) -> str:
    """``C:/x/y`` -> ``C/x/y``, ``/mnt/x`` -> ``mnt/x``: a relative posix path that keeps the
    origin."""
    return path.resolve().as_posix().replace(":", "").lstrip("/")


@dataclass
class Collector:
```

換成

```python
@dataclass
class Collector:
```

在 `src/vcp/backup/evidence.py`，把

```python
    def locate(self, path: Path) -> tuple[str, str, str | None]:
        """(root, relative posix path, source): data / configs by containment, else external."""
        resolved = path.resolve()
        for root, base in (("data", self.data_root), ("configs", self.configs_root)):
            try:
                return root, resolved.relative_to(base.resolve()).as_posix(), None
            except ValueError:
                continue
        return "external", external_path(resolved), resolved.as_posix()
```

換成

```python
    def locate(self, path: Path) -> tuple[str, str, str | None]:
        """(root, relative posix path, source): ``vcp.backup.manifest.locate_file``, the one rule
        the completeness check keys files by as well."""
        return locate_file(path, self.data_root, self.configs_root)
```

在 `src/vcp/backup/evidence.py`，把

```python
        newest: dict[str, CheckpointRecord] = {}
        for c in record.checkpoints:
            newest[c.path] = c
        try:
```

換成

```python
        newest = newest_per_path(record.checkpoints)
        try:
```

在 `src/vcp/backup/evidence.py`，把

```python
    col = Collector(paths.data_root, paths.configs_root)
    if kind == "run":
```

換成

```python
    # spec 2026-10-04 §4.1: stamped before the walk, so a checkpoint registered while it runs is
    # not this manifest's to list -- it makes the next one differ instead
    created = stamp()
    col = Collector(paths.data_root, paths.configs_root)
    if kind == "run":
```

在 `src/vcp/backup/evidence.py`，把

```python
        created_at=stamp(),
        vcp_version=build_string(),
        data_root=paths.data_root.as_posix(),
        files=col.files_of(),
    )
    path = write_manifest(paths, manifest)
```

換成

```python
        created_at=created,
        vcp_version=build_string(),
        data_root=paths.data_root.as_posix(),
        files=col.files_of(),
    )
    gaps = manifest_gaps(manifest, paths)
    if gaps:  # the walk left out a file its runs registered: a vcp bug, so nothing is written
        raise InvariantError(
            f"manifest_incomplete: the walk left out {len(gaps)} file(s) its runs registered, "
            f"first {gaps[0].what}; nothing was written",
            fields={"incomplete": len(gaps)},
        )
    path = write_manifest(paths, manifest)
```

- [ ] **Step 6: 完整性的規則** — 新建 `src/vcp/backup/completeness.py`

新建 `src/vcp/backup/completeness.py`

```python
"""Is a backup manifest complete (spec 2026-10-04 §4, VCP-045)?

Before 0.10.0 a manifest kept one checkpoint per file name, so a run whose five folds each wrote
``model.pt`` was listed with one of them; 0.10.0 fixed how manifests are built, not how old ones
are judged. The rule here judges any manifest by its own content: every run record it lists
names the checkpoints the run had registered, and the evidence and label sets it had attached,
when the manifest was written -- each of those files must be listed, under any role or kind (a
checkpoint under ``runs/<id>/train/`` is listed as a tier-2 ``train_dir`` file, and that counts).
A record only grows, so what it gained after ``created_at`` makes the manifest stale (the
consistency layer's drift says so), not incomplete. ``verify``, ``status``, a tier-3 ``push``,
``--forget-remote`` and the manifest's own self-check all ask ``manifest_gaps``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vcp.backup.manifest import entry_key, local_path
from vcp.backup.schema import Manifest
from vcp.core.config import load_yaml_model
from vcp.core.paths import DatasetPaths, artifact_dir, resolve_stored_path
from vcp.core.time import parse_stamp
from vcp.data.evidence_ref import EvidenceRef
from vcp.measure.schema import RunCard
from vcp.train.checkpoints import newest_per_path
from vcp.train.schema import TrainRecord

RECORD_ROLES = ("run_card", "train_record")


@dataclass(frozen=True)
class Gap:
    """A file the manifest should list and does not. ``what`` names the record field that
    registered it (the verify problem is ``manifest_incomplete:<what>``); ``key`` is the manifest
    key it would have had."""

    run: str
    what: str
    key: str


def _created(manifest: Manifest) -> datetime | None:
    """When the manifest was written; None (later than everything) if its stamp was edited."""
    try:
        return parse_stamp(manifest.created_at)
    except ValueError:
        return None


def _by(text: str, created: datetime | None) -> bool:
    """Registered or attached no later than the manifest. A stamp that does not parse counts as
    earlier: the timestamp layer reports the stamp itself."""
    if created is None:
        return True
    try:
        return parse_stamp(text) <= created
    except ValueError:
        return True


def checkable(manifest: Manifest, paths: DatasetPaths) -> bool:
    """Whether this machine holds every run record the manifest lists; a new machine before
    ``pull`` does not, and then ``status`` falls back to what the verify rows recorded."""
    return all(
        local_path(e, paths.data_root, paths.configs_root).is_file()
        for e in manifest.files
        if e.role in RECORD_ROLES
    )


def manifest_gaps(manifest: Manifest, paths: DatasetPaths) -> list[Gap]:
    """Every file the manifest's run records had registered or attached by ``created_at`` that
    it does not list, one gap per manifest key, in manifest order. A record that is not on this
    machine is skipped (see ``checkable``); one that does not load is ``ValidationFailed``, as in
    the consistency layer."""
    listed = {e.key for e in manifest.files}
    created = _created(manifest)
    gaps: dict[str, Gap] = {}

    def need(run: str, what: str, path: Path) -> None:
        key = entry_key(path, paths.data_root, paths.configs_root)
        if key not in listed and key not in gaps:
            gaps[key] = Gap(run, what, key)

    for e in manifest.files:
        if e.role not in RECORD_ROLES:
            continue
        local = local_path(e, paths.data_root, paths.configs_root)
        if not local.is_file():
            continue
        refs: list[EvidenceRef]
        if e.role == "train_record":
            record = load_yaml_model(local, TrainRecord)
            run, owner, refs = record.run_id, f"{record.run_id}/train.yaml", record.evidence
            registered = [c for c in record.checkpoints if _by(c.registered_at, created)]
            for path in newest_per_path(registered):
                stored = resolve_stored_path(path, paths.data_root)
                need(run, f"{owner}:checkpoints.{path}", stored)
        else:
            card = load_yaml_model(local, RunCard)
            run, owner, refs = card.run_id, f"{card.run_id}/run.yaml", card.evidence
        for ref in refs:
            if _by(ref.attached_at, created):
                adir = artifact_dir(paths.data_root, ref.kind, ref.artifact_id)
                what = f"{owner}:evidence.{ref.kind}/{ref.artifact_id}"
                need(run, what, adir / "manifest.json")
    return list(gaps.values())
```

- [ ] **Step 7: verify 的一致性層報缺口** — `src/vcp/backup/schema.py`、`src/vcp/backup/verify.py`

在 `src/vcp/backup/schema.py`，把

```python
    drift: int | None = None
    bad_stamps: int | None = None
```

換成

```python
    drift: int | None = None
    bad_stamps: int | None = None
    incomplete: int | None = None  # spec 2026-10-04 §4.3: verify rows, written only when > 0
```

在 `src/vcp/backup/verify.py`，把

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Any
```

換成

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
```

在 `src/vcp/backup/verify.py`，把

```python
from vcp.artifact import store
from vcp.backup.dest import Destination, open_dest
```

換成

```python
from vcp.artifact import store
from vcp.backup.completeness import Gap, manifest_gaps
from vcp.backup.dest import Destination, open_dest
```

在 `src/vcp/backup/verify.py`，把

```python
from vcp.submit.schema import Staged
from vcp.train.schema import TrainRecord
```

換成

```python
from vcp.submit.schema import Staged
from vcp.train.checkpoints import newest_per_path
from vcp.train.schema import TrainRecord
```

在 `src/vcp/backup/verify.py`，把

```python
REASONS = ("mismatch", "missing", "drift", "bad_stamps")
```

換成

```python
# `reason=` priority. manifest_incomplete leads (spec 2026-10-04 §4.2): its remedy is a new
# manifest, which makes every other finding about this one moot.
REASONS = ("manifest_incomplete", "mismatch", "missing", "drift", "bad_stamps")
```

在 `src/vcp/backup/verify.py`，把

```python
    drift: list[Drift]
    bad_stamps: list[str]

    @property
    def first_bad(self) -> str | None:
        return self.bad_stamps[0] if self.bad_stamps else None

    @property
    def problems(self) -> list[str]:
        out = list(self.copy_problems)
```

換成

```python
    drift: list[Drift]
    bad_stamps: list[str]
    # spec 2026-10-04 §4.2: files the manifest's runs registered that it does not list
    incomplete: list[Gap] = field(default_factory=list)

    @property
    def first_bad(self) -> str | None:
        return self.bad_stamps[0] if self.bad_stamps else None

    @property
    def problems(self) -> list[str]:
        out = [f"manifest_incomplete:{g.what}" for g in self.incomplete]
        out += self.copy_problems
```

在 `src/vcp/backup/verify.py`，把

```python
    newest = {c.path: c for c in rec.checkpoints}  # per path: a --resume that changed bytes wins
    for path, c in newest.items():
```

換成

```python
    for path, c in newest_per_path(rec.checkpoints).items():
```

在 `src/vcp/backup/verify.py`，把

```python
    drift = _check_consistency(manifest, paths)
    bad = _check_stamps(manifest, paths)
    res = VerifyResult(manifest_id, dest, copies, problems, drift, bad)
```

換成

```python
    drift = _check_consistency(manifest, paths)
    gaps = manifest_gaps(manifest, paths)  # the consistency layer's other half (§4.2)
    bad = _check_stamps(manifest, paths)
    res = VerifyResult(manifest_id, dest, copies, problems, drift, bad, gaps)
```

在 `src/vcp/backup/verify.py`，把

```python
            bad_stamps=len(bad),
            first_bad=res.first_bad,
```

換成

```python
            bad_stamps=len(bad),
            incomplete=len(gaps) or None,
            first_bad=res.first_bad,
```

- [ ] **Step 8: status 每份清單重算完整性** — `src/vcp/backup/status.py` 整份改寫

整份改寫 `src/vcp/backup/status.py`

```python
"""``vcp backup status`` (read-only): each manifest's last push and verify, the tiers never
pushed, whether it is complete, and whether an rclone config file is still on this machine.

Completeness is recomputed from the manifest every time (spec 2026-10-04 §4.2): a verify row
written before 0.13.0 never asked, so a passing one cannot vouch for a manifest that lists fewer
files than its runs registered. Without the run records on this machine the rows' own
``incomplete`` decides; with none, a manifest written before 0.10.0 stays ``unchecked``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.backup.completeness import checkable, manifest_gaps
from vcp.backup.dest import rclone_conf_state
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest
from vcp.backup.push import TIERS
from vcp.backup.schema import BackupRow, Manifest
from vcp.core.build import parse_build_string
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.proc import Runner

# The first release whose manifests list every checkpoint path (VCP-035).
COMPLETE_SINCE = (0, 10, 0)


@dataclass(frozen=True)
class ManifestStatus:
    manifest_id: str
    conclusion: str
    files: int
    created: str
    last_push: BackupRow | None
    last_verify: BackupRow | None
    pushed_tiers: list[int]
    verified: bool
    local_ok: bool
    incomplete: int | None = 0  # files its runs registered that it does not list; None: unknown

    @property
    def unpushed_tiers(self) -> list[int]:
        return [t for t in TIERS if t not in self.pushed_tiers]

    @property
    def completeness(self) -> str:
        if self.incomplete is None:
            return "unchecked"
        return "incomplete" if self.incomplete else "complete"


@dataclass(frozen=True)
class StatusView:
    dataset: str
    manifests: list[ManifestStatus]
    rclone_conf: str

    @property
    def unverified(self) -> list[str]:
        return [m.manifest_id for m in self.manifests if not m.verified]

    @property
    def incomplete(self) -> list[str]:
        return [m.manifest_id for m in self.manifests if m.incomplete]

    @property
    def unchecked(self) -> list[str]:
        return [m.manifest_id for m in self.manifests if m.incomplete is None]


def local_ok(row: BackupRow) -> bool:
    """The two layers that need no destination: local consistency -- completeness included
    (spec 2026-10-04 §4.3) -- and timestamps."""
    return not row.drift and not row.bad_stamps and not row.incomplete


def passed(row: BackupRow) -> bool:
    """A verify row with nothing wrong in any layer -- copies included, over every tier. A row
    that checked no destination, or only the lower tiers, says nothing about the copies: calling
    that "verified" is exactly the claim a machine about to be wiped must not be given."""
    copies = row.copies
    return (
        copies is not None
        and row.tier == 3
        and copies.get("missing", 0) == 0
        and copies.get("mismatch", 0) == 0
        and local_ok(row)
    )


def _load(paths: DatasetPaths, manifest_id: str) -> Manifest | None:
    try:
        return load_manifest(paths, manifest_id)
    except ValidationFailed:
        return None  # gone or unreadable: nothing about it can be verified


def _older_than_complete(build: str) -> bool:
    try:
        version = parse_build_string(build).version
    except ValueError:
        return True
    return tuple(int(part) for part in version.split(".")) < COMPLETE_SINCE


def gaps_of(
    manifest: Manifest | None, paths: DatasetPaths, verifies: list[BackupRow]
) -> int | None:
    """How many files the manifest's runs registered that it does not list -- or None when this
    machine cannot tell (spec 2026-10-04 §4.2)."""
    if manifest is None:
        return None
    if checkable(manifest, paths):
        return len(manifest_gaps(manifest, paths))
    recorded = max((v.incomplete or 0 for v in verifies), default=0)
    if recorded:
        return recorded
    return None if _older_than_complete(manifest.vcp_version) else 0


def status(
    dataset: str,
    *,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> StatusView:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    ledger = BackupLedger(paths.backup_log)
    out: list[ManifestStatus] = []
    for row in ledger.of("manifest"):
        mid = str(row.manifest_id)
        pushes = ledger.of("push", mid)
        verifies = ledger.of("verify", mid)
        gaps = gaps_of(_load(paths, mid), paths, verifies)
        complete = gaps == 0
        covered = max((int(p.tier or 0) for p in pushes if p.failed == []), default=0)
        out.append(
            ManifestStatus(
                manifest_id=mid,
                conclusion=str(row.conclusion),
                files=row.files or 0,
                created=row.ts,
                last_push=pushes[-1] if pushes else None,
                last_verify=verifies[-1] if verifies else None,
                pushed_tiers=[t for t in TIERS if t <= covered],
                verified=complete and any(passed(v) for v in verifies),
                local_ok=complete and any(local_ok(v) for v in verifies),
                incomplete=gaps,
            )
        )
    return StatusView(dataset, out, rclone_conf_state(runner))
```

- [ ] **Step 9: tier 3 的預檢與 `--forget-remote`** — `src/vcp/backup/push.py`

在 `src/vcp/backup/push.py`，把

```python
from vcp.backup.dest import Destination, RcloneDest, open_dest
```

換成

```python
from vcp.backup.completeness import manifest_gaps
from vcp.backup.dest import Destination, RcloneDest, open_dest
```

在 `src/vcp/backup/push.py`，把

```python
    chosen = [f for f in manifest.files if f.kind == "file" and f.present and f.tier <= tier]
    with tempfile.TemporaryDirectory(prefix="vcp-push-") as tmp:
```

換成

```python
    gaps = manifest_gaps(manifest, paths) if tier == 3 or forget_remote else []
    if tier == 3 and gaps:  # spec 2026-10-04 §4.2: before any byte moves, and no push row
        raise ValidationFailed(
            f"manifest_incomplete: {len(gaps)} file(s) its runs registered are not in manifest "
            f"{manifest_id!r} (first {gaps[0].what}); write a new manifest under a new id, then "
            "push and verify that one",
            fields={"incomplete": len(gaps)},
        )
    chosen = [f for f in manifest.files if f.kind == "file" and f.present and f.tier <= tier]
    with tempfile.TemporaryDirectory(prefix="vcp-push-") as tmp:
```

在 `src/vcp/backup/push.py`，把

```python
        listed = sum(1 for f in manifest.files if f.kind == "file")
        if left or sent.verified == 0 or listed == 0:
```

換成

```python
        listed = sum(1 for f in manifest.files if f.kind == "file")
        if gaps:  # spec 2026-10-04 §4.2: what was never listed was never pushed either
            raise ValidationFailed(
                f"forget_refused: manifest {manifest_id!r} lacks {len(gaps)} file(s) its runs "
                "registered; write a new manifest under a new id and push every tier of it",
                fields={**counts, "unverified": len(left), "incomplete": len(gaps)},
            )
        if left or sent.verified == 0 or listed == 0:
```

- [ ] **Step 10: CLI：`verify` 與 `status` 的新欄位** — `src/vcp/cli_backup.py`

在 `src/vcp/cli_backup.py`，把

```python
        fields["drift"] = len(res.drift)
        fields["bad_stamps"] = len(res.bad_stamps)
```

換成

```python
        fields["incomplete"] = len(res.incomplete)  # always printed, like drift (§4.2)
        fields["drift"] = len(res.drift)
        fields["bad_stamps"] = len(res.bad_stamps)
```

在 `src/vcp/cli_backup.py`，把

```python
        human = [f"copies: {res.copies}" if res.copies else "copies: not checked (no --dest)"]
        human += res.copy_problems
```

換成

```python
        human = [f"copies: {res.copies}" if res.copies else "copies: not checked (no --dest)"]
        human += [
            f"manifest_incomplete: {g.what} is not listed ({g.key}); write a new manifest "
            "under a new id"
            for g in res.incomplete
        ]
        human += res.copy_problems
```

在 `src/vcp/cli_backup.py`，把

```python
            "drift": [asdict(d) for d in res.drift],
            "bad_stamps": res.bad_stamps,
        }
```

換成

```python
            "drift": [asdict(d) for d in res.drift],
            "bad_stamps": res.bad_stamps,
            "incomplete": [asdict(g) for g in res.incomplete],
        }
```

在 `src/vcp/cli_backup.py`，把

```python
            "unverified": len(view.unverified),
            "rclone_conf": view.rclone_conf,
        }
        notes: list[str] = []
        if not view.manifests:
            notes.append("no manifests yet: run `vcp backup manifest`")
```

換成

```python
            "unverified": len(view.unverified),
            "incomplete": len(view.incomplete),
            "rclone_conf": view.rclone_conf,
        }
        notes: list[str] = []
        if not view.manifests:
            notes.append("no manifests yet: run `vcp backup manifest`")
        if view.incomplete:
            notes.append(
                "manifest_incomplete: these list fewer files than their runs registered: "
                f"{', '.join(view.incomplete)}; write a new manifest under a new id, then push "
                "and verify it"
            )
        if view.unchecked:
            notes.append(
                "completeness unchecked (written before vcp 0.10.0, and this machine lacks their "
                f"run records): {', '.join(view.unchecked)}"
            )
```

在 `src/vcp/cli_backup.py`，把

```python
            f"verified={m.verified}  local_ok={m.local_ok}"
            for m in view.manifests
```

換成

```python
            f"verified={m.verified}  local_ok={m.local_ok}  completeness={m.completeness}"
            for m in view.manifests
```

在 `src/vcp/cli_backup.py`，把

```python
                    "verified": m.verified,
                    "local_ok": m.local_ok,
                }
```

換成

```python
                    "verified": m.verified,
                    "local_ok": m.local_ok,
                    "completeness": m.completeness,
                    "incomplete": m.incomplete,
                }
```

- [ ] **Step 11: 跑測試，確認通過**

Run: `uv run pytest -o addopts="" -q tests/unit/backup tests/unit/test_e2e_backup.py tests/unit/test_cli_backup.py tests/unit/train`
Expected: 全部 PASS。完整的清單、0.12 寫過的列都照舊：verify 列在沒有缺口時位元組不變，`status` 的既有斷言不動。

- [ ] **Step 12: Lint，然後 commit**

```bash
uv run ruff format src/vcp/train/checkpoints.py src/vcp/train/upload.py src/vcp/backup/manifest.py src/vcp/backup/completeness.py src/vcp/backup/evidence.py src/vcp/backup/schema.py src/vcp/backup/verify.py src/vcp/backup/status.py src/vcp/backup/push.py src/vcp/cli_backup.py tests/backup_fixtures.py tests/unit/backup/test_completeness.py
uv run ruff check --fix src/vcp/train/checkpoints.py src/vcp/train/upload.py src/vcp/backup/manifest.py src/vcp/backup/completeness.py src/vcp/backup/evidence.py src/vcp/backup/schema.py src/vcp/backup/verify.py src/vcp/backup/status.py src/vcp/backup/push.py src/vcp/cli_backup.py tests/backup_fixtures.py tests/unit/backup/test_completeness.py
uv run ruff check . && uv run ruff format --check .
git diff --check
git add src/vcp/train/checkpoints.py src/vcp/train/upload.py src/vcp/backup/manifest.py src/vcp/backup/completeness.py src/vcp/backup/evidence.py src/vcp/backup/schema.py src/vcp/backup/verify.py src/vcp/backup/status.py src/vcp/backup/push.py src/vcp/cli_backup.py tests/backup_fixtures.py tests/unit/backup/test_completeness.py
git commit -F <訊息檔>
```

訊息：`fix: 備份清單檢查完整性，verify、status、tier 3 push 與 forget 都看得出缺檔（VCP-045）`

---

### Task 4: 本機的 `remote_copy` 跟著目的地走（VCP-046）

**Files:**
- Modify: `src/vcp/backup/dest.py`（新 `covers`、`travels`、`copy_path`）
- Modify: `src/vcp/backup/schema.py`（`BackupRow.local_copies`）
- Modify: `src/vcp/backup/push.py`（送出的項目、來源、`_unverified`、`listed`、`PushResult.local_copies`、push 列）
- Modify: `src/vcp/backup/verify.py`（`_check_copies`、`VerifyResult.local_copies`、verify 列）
- Modify: `src/vcp/backup/status.py`（0.13.0 以前的列不替本機副本背書）
- Modify: `src/vcp/backup/pull.py`（先從目的地拉，再退回本機副本）
- Modify: `src/vcp/backup/evidence.py`（`_checkpoints` 優先選 rclone 的上傳）
- Modify: `src/vcp/cli_backup.py`（`manifest` / `push` / `verify` 的 `local_copies=`）
- Test: `tests/unit/backup/test_local_copies.py`（新建）
- Modify（行為變了的既有斷言）：`tests/unit/backup/test_dest_push.py`、`tests/unit/backup/test_verify.py`、`tests/unit/backup/test_pull_status.py`、`tests/unit/test_e2e_backup.py`。其中回歸 gate 點名的 `test_forget_remote_only_after_everything_verified`（`test_dest_push.py`）與 `test_fake_rclone_story`（`test_e2e_backup.py`）只改內容、不改名。

**Interfaces:**
- Consumes（Task 3）：`status.py` 的 `_load`、`gaps_of`；push 的 tier 3 預檢與 `gaps`；`VerifyResult.incomplete`；cli 的 `"incomplete"` payload 鍵。
- Produces：
  - `vcp.backup.dest.covers(dest: str, copy: RemoteCopy) -> bool`、`travels(entry: FileEntry, dest: str) -> bool`、`copy_path(copy: RemoteCopy) -> Path`（`<copy.dest>/<copy.run>/<copy.name>`）。
  - `BackupRow.local_copies: int | None = None`（push 與 verify 列，只在大於 0 時寫出）。
  - `PushResult.local_copies: int = 0`；`VerifyResult.local_copies: int = 0`；`verify._check_copies(...) -> tuple[dict[str, int], list[str], int]`。
  - VERDICT：`backup manifest` 一律帶 `local_copies=`（本機副本數，只供參考）；`backup push` 一律帶 `local_copies=`；`backup verify --dest` 帶 `local_copies=`。

設計說明（spec 沒寫細的地方）：
- 「副本在目的地裡」比的是 `remote.dest` 這個資料夾（`train upload` 放副本的地方）是否就是目的地或在它底下；兩邊都先 `resolve()` 再 `os.path.normcase`，Windows 上不分大小寫。
- 沒被 cover 的副本在 verify 一律算「應該在」：`present=false` 不會讓它變成 `absent`（`present` 說的是原 checkpoint，不是副本）。
- pull 對沒被 cover 的副本：目的地沒有、或目的地那份 sha 不對，而本機副本 sha 相符 → 從本機副本還原。兩者都不符，照舊報 `missing` / `mismatch`。
- `status` 的「背書」規則對 push 列與 verify 列一樣：列所在的目的地 D 若沒有 cover 清單裡的每一份副本，只有帶 `local_copies` 的列（0.13.0 起寫）算數；不算數的 tier 3 push 列只當 tier 2。

- [ ] **Step 1: 寫失敗的測試** — 新建 `tests/unit/backup/test_local_copies.py`

```python
"""VCP-046 (spec 2026-10-04 §5, §9): a checkpoint copy `train upload` left on this machine is not
a backup somewhere else. A remote_copy is checked in place only when it is on an rclone remote or
inside the destination itself; any other is pushed to the destination like a file, verified
there, pulled from there (its local copy is the fallback), and counted by --forget-remote."""

import pytest

from backup_fixtures import FakeRemote
from submit_fixtures import EVAL, TEST
from vcp.backup.dest import covers
from vcp.backup.evidence import build_manifest
from vcp.backup.ledger import BackupLedger
from vcp.backup.pull import pull
from vcp.backup.push import push
from vcp.backup.schema import BackupRow, RemoteCopy
from vcp.backup.status import status
from vcp.backup.verify import verify
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.train.records import load_record, save_record
from vcp.train.schema import UploadRecord
from vcp.train.status import upload_run
from vcp.train.upload import merge_uploads

BEST = "data/work/good/weights/best.pt"
REMOTE_BEST = f"fake:vault/{BEST}"


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _ledger(world, name=TEST):
    return BackupLedger(DatasetPaths.resolve(name, **_kw(world)).backup_log)


def _manifest(world):
    """S1's manifest: best.pt is a remote_copy whose copy `train upload` put in a local folder."""
    res = build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))
    [entry] = [f for f in res.manifest.files if f.kind == "remote_copy"]
    assert (entry.key, entry.remote.dest) == (BEST, str(world.vault))
    return res.manifest


def _status(world):
    [m] = status(TEST, runner=FakeRemote(conf=world.tmp / "rclone.conf"), **_kw(world)).manifests
    return m


def _uploads(world, *uploads):
    record = load_record(world.roots.data, "good")
    save_record(world.roots.data, merge_uploads(record, list(uploads)))


def _upload(world, dest: str, kind: str, at: str) -> UploadRecord:
    best = next(c for c in load_record(world.roots.data, "good").checkpoints if c.final)
    return UploadRecord(
        dest=dest, kind=kind, name="best.pt", sha256=best.sha256, verified=True, uploaded_at=at
    )


def test_covers_matrix(tmp_path):
    usb = tmp_path / "usb"
    local = RemoteCopy(dest=str(usb / "weights"), run="good", name="best.pt")
    remote = RemoteCopy(dest="gd:weights", run="good", name="best.pt")
    assert covers("fake:vault", remote) and covers(str(usb), remote)  # off this machine already
    assert not covers("fake:vault", local)  # a local copy, an rclone destination
    assert covers(str(usb), local)  # inside this local destination
    assert not covers(str(tmp_path / "other"), local)  # outside it


def test_a_tier_3_push_to_rclone_sends_a_local_copy_from_its_checkpoint(world):
    _manifest(world)
    remote = FakeRemote()
    out = push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    assert out.local_copies == 1 and out.failed == []
    assert remote.store[REMOTE_BEST] == b"best weights"
    assert _ledger(world).latest("push", "m1").local_copies == 1


def test_a_deleted_checkpoint_is_sent_from_its_local_copy(world):
    _manifest(world)
    (world.weights / "best.pt").unlink()  # disk freed after `train upload`
    remote = FakeRemote()
    out = push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    assert out.local_copies == 1 and remote.store[REMOTE_BEST] == b"best weights"


def test_push_fails_before_any_byte_moves_when_no_copy_holds_the_bytes(world):
    _manifest(world)
    (world.weights / "best.pt").write_bytes(b"retrained")
    (world.vault / "good" / "best.pt").write_bytes(b"corrupt")
    remote = FakeRemote()
    with pytest.raises(IntegrityError, match="drift:") as ei:
        push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    assert ei.value.fields == {"file": BEST} and remote.calls == []
    (world.weights / "best.pt").unlink()
    (world.vault / "good" / "best.pt").unlink()
    with pytest.raises(ValidationFailed, match="not_found:") as ei:
        push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    assert ei.value.fields == {"file": BEST} and remote.calls == []
    assert _ledger(world).of("push") == []


def test_a_tier_2_push_sends_no_local_copy(world):
    _manifest(world)
    remote = FakeRemote()
    out = push(TEST, "m1", "fake:vault", tier=2, runner=remote, **_kw(world))
    assert out.local_copies == 0 and not any(key.endswith(".pt") for key in remote.store)
    assert _ledger(world).latest("push", "m1").local_copies is None


def test_the_manifest_prefers_an_rclone_upload_to_a_newer_local_one(world):
    _uploads(
        world,
        _upload(world, "fake:w", "rclone", "2026-10-04T00:00:00.000Z"),
        _upload(world, str(world.tmp / "later"), "local", "2026-10-04T01:00:00.000Z"),
    )
    res = build_manifest(EVAL, "run:good", manifest_id="mg", **_kw(world))
    [entry] = [f for f in res.manifest.files if f.kind == "remote_copy"]
    assert entry.remote.dest == "fake:w"


def test_an_rclone_remote_copy_is_verified_in_place_and_never_pushed(world):
    _uploads(world, _upload(world, "fake:w", "rclone", "2026-10-04T00:00:00.000Z"))
    build_manifest(EVAL, "run:good", manifest_id="mg", **_kw(world))
    remote = FakeRemote()
    remote.store["fake:w/good/best.pt"] = (world.weights / "best.pt").read_bytes()
    out = push(EVAL, "mg", "fake:vault", tier=3, runner=remote, **_kw(world))
    assert out.local_copies == 0 and REMOTE_BEST not in remote.store
    res = verify(EVAL, "mg", dest="fake:vault", runner=remote, **_kw(world))
    assert res.ok and res.local_copies == 0


def test_verify_checks_a_local_copy_at_the_rclone_destination(world):
    _manifest(world)
    remote = FakeRemote()
    push(TEST, "m1", "fake:vault", tier=2, runner=remote, **_kw(world))
    res = verify(TEST, "m1", dest="fake:vault", runner=remote, **_kw(world))
    assert f"missing:{BEST}" in res.copy_problems and res.local_copies == 1
    push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    res = verify(TEST, "m1", dest="fake:vault", runner=remote, **_kw(world))
    assert res.ok and res.local_copies == 1
    assert _ledger(world).latest("verify", "m1").local_copies == 1


def test_a_local_copy_listed_as_gone_is_missing_not_absent(world):
    (world.weights / "best.pt").unlink()
    _manifest(world)  # best.pt: present=false, still a remote_copy
    res = verify(TEST, "m1", dest="fake:vault", runner=FakeRemote(), **_kw(world))
    assert f"missing:{BEST}" in res.copy_problems and res.copies["absent"] == 0


def test_rows_written_before_013_do_not_vouch_for_a_local_copy(world):
    manifest = _manifest(world)
    n = len(manifest.files)
    ledger = _ledger(world)
    ledger.append(  # what 0.12 wrote: a tier-3 push without best.pt, best.pt checked in place
        BackupRow(
            event="push",
            ts=stamp(),
            manifest_id="m1",
            dest="fake:vault",
            tier=3,
            pushed=n - 1,
            skipped=0,
            verified=n - 1,
            failed=[],
            bytes=0,
        )
    )
    ledger.append(
        BackupRow(
            event="verify",
            ts=stamp(),
            manifest_id="m1",
            dest="fake:vault",
            tier=3,
            copies={"ok": n, "missing": 0, "mismatch": 0, "absent": 0},
            drift=0,
            bad_stamps=0,
        )
    )
    m = _status(world)
    assert not m.verified and m.pushed_tiers == [1, 2]
    remote = FakeRemote()
    push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    verify(TEST, "m1", dest="fake:vault", runner=remote, **_kw(world))
    m = _status(world)
    assert m.verified and m.pushed_tiers == [1, 2, 3]


def test_forget_remote_refuses_until_the_local_copy_is_at_the_destination(world):
    _manifest(world)
    remote = FakeRemote()
    push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    del remote.store[REMOTE_BEST]
    with pytest.raises(ValidationFailed, match="forget_refused") as ei:
        push(TEST, "m1", "fake:vault", tier=2, forget_remote=True, runner=remote, **_kw(world))
    assert ei.value.fields["unverified"] == 1 and remote.deleted == []
    out = push(TEST, "m1", "fake:vault", tier=3, forget_remote=True, runner=remote, **_kw(world))
    assert out.forgotten == "fake" and remote.deleted == ["fake"]


def test_pull_takes_a_local_copy_from_the_destination_then_falls_back_to_it(world):
    _manifest(world)
    remote = FakeRemote()
    push(TEST, "m1", "fake:vault", tier=3, runner=remote, **_kw(world))
    copy = world.vault / "good" / "best.pt"
    (world.weights / "best.pt").unlink()
    copy.unlink()  # as on a new machine: only the destination has it
    res = pull(TEST, "m1", "fake:vault", runner=remote, **_kw(world))
    assert res.pulled == 1 and (world.weights / "best.pt").read_bytes() == b"best weights"
    (world.weights / "best.pt").unlink()
    copy.write_bytes(b"best weights")  # this machine, and a destination that never got it
    res = pull(TEST, "m1", str(world.tmp / "empty-vault"), **_kw(world))
    assert res.pulled == 1 and (world.weights / "best.pt").read_bytes() == b"best weights"


def test_a_local_destination_that_holds_the_copy_checks_it_in_place(world):
    usb = world.tmp / "usb"
    upload_run(world.roots.data, "good", str(usb / "weights"), only_final=True)
    manifest = build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world)).manifest
    [entry] = [f for f in manifest.files if f.kind == "remote_copy"]
    assert entry.remote.dest == str(usb / "weights")  # the newest local upload
    out = push(TEST, "m1", str(usb), tier=3, **_kw(world))
    assert out.local_copies == 0 and out.failed == []
    assert not (usb / "data" / "work" / "good" / "weights" / "best.pt").exists()
    res = verify(TEST, "m1", dest=str(usb), **_kw(world))
    assert res.ok and res.local_copies == 0


def test_a_local_destination_without_the_copy_receives_it(world):
    _manifest(world)
    usb = world.tmp / "usb"
    out = push(TEST, "m1", str(usb), tier=3, **_kw(world))
    assert out.local_copies == 1
    assert (usb / "data" / "work" / "good" / "weights" / "best.pt").read_bytes() == b"best weights"
```

- [ ] **Step 2: 跑測試，確認失敗**

Run: `uv run pytest -o addopts="" -q tests/unit/backup/test_local_copies.py`
Expected: 收集階段就 FAIL：`ImportError: cannot import name 'covers' from 'vcp.backup.dest'`。

- [ ] **Step 3: 哪些副本由目的地 cover** — `src/vcp/backup/dest.py`、`src/vcp/backup/schema.py`

在 `src/vcp/backup/dest.py`，把

```python
import re
import shutil
import sys
from collections.abc import Iterable
```

換成

```python
import os
import re
import shutil
import sys
from collections.abc import Iterable
```

在 `src/vcp/backup/dest.py`，把

```python
from vcp.core.errors import PlatformError, VcpError
from vcp.core.hashing import sha256_file
```

換成

```python
from vcp.backup.schema import FileEntry, RemoteCopy
from vcp.core.errors import PlatformError, VcpError
from vcp.core.hashing import sha256_file
```

在 `src/vcp/backup/dest.py`，把

```python
    return "rclone" if _REMOTE.match(dest) else "local"


def _failed(what: str, proc: Any) -> PlatformError:
```

換成

```python
    return "rclone" if _REMOTE.match(dest) else "local"


def _inside(path: str, base: str) -> bool:
    child = os.path.normcase(str(Path(path).resolve()))
    parent = os.path.normcase(str(Path(base).resolve()))
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def covers(dest: str, copy: RemoteCopy) -> bool:
    """spec 2026-10-04 §5.1: a remote_copy is checked where it lies, and never pushed, only when
    it is off this machine already (on an rclone remote) or lies inside this very local
    destination. Any other copy is handled at ``dest`` exactly like a file."""
    if dest_kind(copy.dest) == "rclone":
        return True
    return dest_kind(dest) == "local" and _inside(copy.dest, dest)


def travels(entry: FileEntry, dest: str) -> bool:
    """Whether ``entry`` belongs at ``dest`` under the layout ``<dest>/<root>/<path>``: every
    file, and every remote_copy ``dest`` does not cover."""
    return entry.remote is None or not covers(dest, entry.remote)


def copy_path(copy: RemoteCopy) -> Path:
    """Where ``train upload`` put a local copy: ``<dest>/<run>/<name>``."""
    return Path(copy.dest) / copy.run / copy.name


def _failed(what: str, proc: Any) -> PlatformError:
```

在 `src/vcp/backup/schema.py`，把

```python
    bytes: int | None = None
    copies: dict[str, int] | None = None
```

換成

```python
    bytes: int | None = None
    copies: dict[str, int] | None = None
    local_copies: int | None = None  # spec 2026-10-04 §5.2: push / verify rows, only when > 0
```

- [ ] **Step 4: push 送出沒被 cover 的副本** — `src/vcp/backup/push.py`

在 `src/vcp/backup/push.py`，把

```python
from vcp.backup.dest import Destination, RcloneDest, open_dest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest, local_path
from vcp.backup.schema import LEDGER_ROLES, BackupRow, FileEntry, Manifest
```

換成

```python
from vcp.backup.dest import Destination, RcloneDest, copy_path, open_dest, travels
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest, local_path
from vcp.backup.schema import LEDGER_ROLES, BackupRow, FileEntry, Manifest, RemoteCopy
```

在 `src/vcp/backup/push.py`，把

```python
    bytes: int
    forgotten: str | None = None
```

換成

```python
    bytes: int
    forgotten: str | None = None
    local_copies: int = 0  # remote_copies this push sent like files (spec 2026-10-04 §5.2)
```

在 `src/vcp/backup/push.py`，把

```python
    """Every file about to be pushed, holding exactly the bytes the manifest recorded -- an
    append-only ledger that only grew contributes a snapshot of its first ``bytes`` bytes. All
    checks happen before any byte moves: an evacuation must not half-run on a stale manifest."""
    out: dict[str, Path] = {}
    for index, e in enumerate(entries):
        src = local_path(e, paths.data_root, paths.configs_root)
```

換成

```python
    """Every file about to be pushed, holding exactly the bytes the manifest recorded -- an
    append-only ledger that only grew contributes a snapshot of its first ``bytes`` bytes, and a
    local remote_copy its checkpoint or that copy. All checks happen before any byte moves: an
    evacuation must not half-run on a stale manifest."""
    out: dict[str, Path] = {}
    for index, e in enumerate(entries):
        if e.remote is not None:
            out[e.key] = _copy_source(e, e.remote, paths)
            continue
        src = local_path(e, paths.data_root, paths.configs_root)
```

在 `src/vcp/backup/push.py`，把

```python
@dataclass(frozen=True)
class _Transfer:
```

換成

```python
def _copy_source(e: FileEntry, copy: RemoteCopy, paths: DatasetPaths) -> Path:
    """A local remote_copy's bytes (spec 2026-10-04 §5.2): the checkpoint itself while it holds
    what the manifest recorded, else the copy ``train upload`` verified. Neither: nothing moves."""
    original = local_path(e, paths.data_root, paths.configs_root)
    kept = copy_path(copy)
    for candidate in (original, kept):
        if candidate.is_file() and sha256_file(candidate) == e.sha256:
            return candidate
    if original.is_file() or kept.is_file():
        raise IntegrityError(
            f"drift: neither {e.key} nor its copy {kept.as_posix()} holds the bytes the manifest "
            "recorded; write a new manifest",
            fields={"file": e.key},
        )
    raise ValidationFailed(
        f"not_found: {e.key} and its copy {kept.as_posix()} are both gone; write a new manifest",
        fields={"file": e.key},
    )


@dataclass(frozen=True)
class _Transfer:
```

在 `src/vcp/backup/push.py`，把

```python
    """Every ``kind=file`` entry this push did not send -- a higher tier, or one the manifest
    already recorded as gone -- whose copy at the destination is absent or different. Forgetting
    the credential is the last act before the machine goes: the whole manifest must be there."""
    sent = {e.key for e in chosen}
    rest = [f for f in manifest.files if f.kind == "file" and f.key not in sent]
```

換成

```python
    """Every entry that belongs at the destination -- every file, and every remote_copy it does
    not cover (spec 2026-10-04 §5.2) -- this push did not send (a higher tier, or one the
    manifest recorded as gone) and whose copy there is absent or different. Forgetting the
    credential is the last act before the machine goes: the whole manifest must be there."""
    sent = {e.key for e in chosen}
    rest = [f for f in manifest.files if travels(f, target.dest) and f.key not in sent]
```

在 `src/vcp/backup/push.py`，把

```python
    chosen = [f for f in manifest.files if f.kind == "file" and f.present and f.tier <= tier]
    with tempfile.TemporaryDirectory(prefix="vcp-push-") as tmp:
        sent = _send(target, chosen, _sources(chosen, paths, Path(tmp)))
```

換成

```python
    # spec 2026-10-04 §5.2: the present files of tiers 1..N, and every remote_copy this
    # destination does not cover -- present or not: the copy `train upload` made holds the bytes
    chosen = [
        f
        for f in manifest.files
        if f.tier <= tier and travels(f, dest) and (f.present or f.remote is not None)
    ]
    local = sum(1 for f in chosen if f.remote is not None)
    with tempfile.TemporaryDirectory(prefix="vcp-push-") as tmp:
        sent = _send(target, chosen, _sources(chosen, paths, Path(tmp)))
```

在 `src/vcp/backup/push.py`，把

```python
            failed=sent.failed,
            bytes=sent.bytes,
        )
    )
```

換成

```python
            failed=sent.failed,
            bytes=sent.bytes,
            local_copies=local or None,
        )
    )
```

在 `src/vcp/backup/push.py`，把

```python
        listed = sum(1 for f in manifest.files if f.kind == "file")
```

換成

```python
        listed = sum(1 for f in manifest.files if travels(f, dest))
```

在 `src/vcp/backup/push.py`，把

```python
        sent.bytes,
        forgotten,
    )
```

換成

```python
        sent.bytes,
        forgotten,
        local,
    )
```

- [ ] **Step 5: verify 在目的地查沒被 cover 的副本** — `src/vcp/backup/verify.py`

在 `src/vcp/backup/verify.py`，把

```python
from vcp.backup.dest import Destination, open_dest
```

換成

```python
from vcp.backup.dest import Destination, open_dest, travels
```

在 `src/vcp/backup/verify.py`，把

```python
    # spec 2026-10-04 §4.2: files the manifest's runs registered that it does not list
    incomplete: list[Gap] = field(default_factory=list)
```

換成

```python
    # spec 2026-10-04 §4.2: files the manifest's runs registered that it does not list
    incomplete: list[Gap] = field(default_factory=list)
    local_copies: int = 0  # remote_copies checked at dest like files (spec 2026-10-04 §5.2)
```

在 `src/vcp/backup/verify.py`，把

```python
) -> tuple[dict[str, int], list[str]]:
    """Only entries with ``tier <= tier``: a destination that holds tiers 1..N is complete for
    them even though the weights were never pushed. An entry the manifest already recorded as
    gone (``present=false``) is ``absent`` at a destination that never got it -- a fact, not a
    failure; a copy of it that is there still has to match."""
    counts = {"ok": 0, "missing": 0, "mismatch": 0, "absent": 0}
    problems: list[str] = []
    dests: dict[str, Destination] = {dest: open_dest(dest, runner)}
    groups: dict[tuple[str, str], list[tuple[str, str, str, bool]]] = {}
    for e in manifest.files:
        if e.tier > tier:
            continue
        if e.remote is None:
            groups.setdefault((dest, e.root), []).append((e.path, e.sha256, e.key, e.present))
        else:
```

換成

```python
) -> tuple[dict[str, int], list[str], int]:
    """Only entries with ``tier <= tier``: a destination that holds tiers 1..N is complete for
    them even though the weights were never pushed. An entry the manifest already recorded as
    gone (``present=false``) is ``absent`` at a destination that never got it -- a fact, not a
    failure; a copy of it that is there still has to match. A remote_copy is checked where it
    lies only when ``dest`` covers it; any other is held at ``dest`` like a file, and has to be
    (spec 2026-10-04 §5.2) -- the third value counts those."""
    counts = {"ok": 0, "missing": 0, "mismatch": 0, "absent": 0}
    problems: list[str] = []
    local = 0
    dests: dict[str, Destination] = {dest: open_dest(dest, runner)}
    groups: dict[tuple[str, str], list[tuple[str, str, str, bool]]] = {}
    for e in manifest.files:
        if e.tier > tier:
            continue
        if travels(e, dest):
            if e.remote is not None:
                local += 1
            present = e.present or e.remote is not None  # the copy's bytes still exist
            groups.setdefault((dest, e.root), []).append((e.path, e.sha256, e.key, present))
        elif e.remote is not None:
```

在 `src/vcp/backup/verify.py`，把

```python
            else:
                counts["ok"] += 1
    return counts, problems
```

換成

```python
            else:
                counts["ok"] += 1
    return counts, problems, local
```

在 `src/vcp/backup/verify.py`，把

```python
    problems: list[str] = []
    copies_error: PlatformError | None = None
    if dest is not None:
        try:
            copies, problems = _check_copies(manifest, dest, tier, runner)
```

換成

```python
    problems: list[str] = []
    local = 0
    copies_error: PlatformError | None = None
    if dest is not None:
        try:
            copies, problems, local = _check_copies(manifest, dest, tier, runner)
```

在 `src/vcp/backup/verify.py`，把

```python
    res = VerifyResult(manifest_id, dest, copies, problems, drift, bad, gaps)
```

換成

```python
    res = VerifyResult(manifest_id, dest, copies, problems, drift, bad, gaps, local)
```

在 `src/vcp/backup/verify.py`，把

```python
            copies=copies,
            drift=len(drift),
```

換成

```python
            copies=copies,
            local_copies=local or None,
            drift=len(drift),
```

- [ ] **Step 6: status：0.13.0 以前的列不替本機副本背書** — `src/vcp/backup/status.py`

在 `src/vcp/backup/status.py`，把

```python
``incomplete`` decides; with none, a manifest written before 0.10.0 stays ``unchecked``."""
```

換成

```python
``incomplete`` decides; with none, a manifest written before 0.10.0 stays ``unchecked``.

A push or verify row about a destination that does not cover the manifest's local copies
vouches only if it handled them (``local_copies``, written since 0.13.0, spec §5.2)."""
```

在 `src/vcp/backup/status.py`，把

```python
from vcp.backup.dest import rclone_conf_state
```

換成

```python
from vcp.backup.dest import covers, rclone_conf_state
```

在 `src/vcp/backup/status.py`，把

```python
def status(
    dataset: str,
```

換成

```python
def _vouches(row: BackupRow, manifest: Manifest | None) -> bool:
    """Whether a push or verify row about destination D speaks for the manifest's remote_copies
    there: D covers every one, or the row handled them (``local_copies``, written since 0.13.0).
    An older row never looked for a local copy at D (spec 2026-10-04 §5.2)."""
    if manifest is None or row.dest is None or row.local_copies is not None:
        return True
    return all(covers(row.dest, f.remote) for f in manifest.files if f.remote is not None)


def _pushed_tier(row: BackupRow, manifest: Manifest | None) -> int:
    """The tiers a push row covers: a tier-3 push that left the local copies out covers two."""
    tier = int(row.tier or 0)
    return 2 if tier == 3 and not _vouches(row, manifest) else tier


def status(
    dataset: str,
```

在 `src/vcp/backup/status.py`，把

```python
        gaps = gaps_of(_load(paths, mid), paths, verifies)
        complete = gaps == 0
        covered = max((int(p.tier or 0) for p in pushes if p.failed == []), default=0)
```

換成

```python
        manifest = _load(paths, mid)
        gaps = gaps_of(manifest, paths, verifies)
        complete = gaps == 0
        covered = max((_pushed_tier(p, manifest) for p in pushes if p.failed == []), default=0)
```

在 `src/vcp/backup/status.py`，把

```python
                verified=complete and any(passed(v) for v in verifies),
```

換成

```python
                verified=complete and any(passed(v) and _vouches(v, manifest) for v in verifies),
```

- [ ] **Step 7: pull 先從目的地拉，再退回本機副本** — `src/vcp/backup/pull.py`

在 `src/vcp/backup/pull.py`，把

```python
from vcp.backup.dest import Destination, open_dest
```

換成

```python
from vcp.backup.dest import Destination, copy_path, covers, open_dest
```

在 `src/vcp/backup/pull.py`，把

```python
    for e in chosen:
        if e.remote is None:
            sources[e.key] = (dest, e.root, e.path)
        else:
```

換成

```python
    for e in chosen:
        if e.remote is None or not covers(dest, e.remote):
            # a remote_copy this destination does not cover was pushed here like a file (spec
            # 2026-10-04 §5.2); its own local copy is only the fallback, in the loop below
            sources[e.key] = (dest, e.root, e.path)
        else:
```

在 `src/vcp/backup/pull.py`，把

```python
            got = have[(d, sub)].get(rel)
            if got is None:
                missing.append(e.key)
                continue
```

換成

```python
            got = have[(d, sub)].get(rel)
            if got != e.sha256 and e.remote is not None and not covers(dest, e.remote):
                # not at the destination, or other bytes there: the copy `train upload` left on
                # this machine still restores it, as it did before 0.13.0
                kept = copy_path(e.remote)
                if kept.is_file() and sha256_file(kept) == e.sha256:
                    d, sub, rel, got = e.remote.dest, e.remote.run, e.remote.name, e.sha256
                    dests.setdefault(d, open_dest(d, runner))
            if got is None:
                missing.append(e.key)
                continue
```

- [ ] **Step 8: 建清單時優先選 rclone 的上傳** — `src/vcp/backup/evidence.py`

在 `src/vcp/backup/evidence.py`，把

```python
            own = {upload_names.get(path), Path(path).name}
            remote = None
            for u in reversed(record.uploads):
                if u.verified and u.sha256 == c.sha256 and u.name in own:
                    remote = RemoteCopy(dest=u.dest, run=record.run_id, name=u.name)
                    break
```

換成

```python
            own = {upload_names.get(path), Path(path).name}
            matches = [
                u for u in record.uploads if u.verified and u.sha256 == c.sha256 and u.name in own
            ]
            # spec 2026-10-04 §5.2: a copy on an rclone remote is off this machine already, so
            # it wins over a local one; within one kind the newest wins
            pick = next((u for u in reversed(matches) if u.kind == "rclone"), None)
            pick = pick or (matches[-1] if matches else None)
            remote = None
            if pick is not None:
                remote = RemoteCopy(dest=pick.dest, run=record.run_id, name=pick.name)
```

- [ ] **Step 9: CLI 的 `local_copies=`** — `src/vcp/cli_backup.py`

在 `src/vcp/cli_backup.py`，把

```python
from vcp.backup.evidence import build_manifest
```

換成

```python
from vcp.backup.dest import dest_kind
from vcp.backup.evidence import build_manifest
```

在 `src/vcp/cli_backup.py`，把

```python
            "remote_copies": sum(1 for f in m.files if f.kind == "remote_copy"),
            "missing": len(res.missing),
```

換成

```python
            "remote_copies": sum(1 for f in m.files if f.kind == "remote_copy"),
            # spec 2026-10-04 §5.2: copies on this machine, informational; tier 3 sends them on
            "local_copies": sum(
                1 for f in m.files if f.remote is not None and dest_kind(f.remote.dest) == "local"
            ),
            "missing": len(res.missing),
```

在 `src/vcp/cli_backup.py`，把

```python
            "failed": len(res.failed),
            "bytes": res.bytes,
        }
        human = [f"pushed {res.pushed}, skipped {res.skipped}, verified {res.verified} -> {dest}"]
```

換成

```python
            "failed": len(res.failed),
            "bytes": res.bytes,
            "local_copies": res.local_copies,
        }
        human = [f"pushed {res.pushed}, skipped {res.skipped}, verified {res.verified} -> {dest}"]
        if res.local_copies:
            human.append(
                f"{res.local_copies} checkpoint(s) whose only copy was on this machine went to "
                f"{dest} as well"
            )
```

在 `src/vcp/cli_backup.py`，把

```python
            "bytes": res.bytes,
            "forgotten": res.forgotten,
```

換成

```python
            "bytes": res.bytes,
            "local_copies": res.local_copies,
            "forgotten": res.forgotten,
```

在 `src/vcp/cli_backup.py`，把

```python
            if res.copies["absent"]:  # listed as gone when the manifest was written
                fields["absent"] = res.copies["absent"]
```

換成

```python
            if res.copies["absent"]:  # listed as gone when the manifest was written
                fields["absent"] = res.copies["absent"]
            fields["local_copies"] = res.local_copies
```

在 `src/vcp/cli_backup.py`，把

```python
            "incomplete": [asdict(g) for g in res.incomplete],
        }
```

換成

```python
            "incomplete": [asdict(g) for g in res.incomplete],
            "local_copies": res.local_copies,
        }
```

- [ ] **Step 10: 跟著改既有測試的斷言**（`make_world` 的 `best.pt` 有一份放在 `vault-train` 的本機副本，現在它會被送到目的地）

在 `tests/unit/backup/test_dest_push.py`，把

```python
    assert not (vault / "data" / "work" / "good" / "weights" / "best.pt").exists()  # remote_copy
```

換成

```python
    best = vault / "data" / "work" / "good" / "weights" / "best.pt"
    assert best.read_bytes() == b"best weights"  # its copy is outside this vault: it travels too
```

在 `tests/unit/backup/test_dest_push.py`，把

```python
    above = sum(1 for f in res.manifest.files if f.kind == "file" and f.tier > 1)
```

換成

```python
    above = sum(1 for f in res.manifest.files if f.tier > 1)  # the local remote_copy as well
```

在 `tests/unit/backup/test_verify.py`，把

```python
    # the remote_copy verified in place
    assert res.copies == {"ok": n, "missing": 0, "mismatch": 0, "absent": 0}
```

換成

```python
    # the remote_copy's own copy lies outside the vault: pushed there, and checked there
    assert res.copies == {"ok": n, "missing": 0, "mismatch": 0, "absent": 0}
    assert res.local_copies == 1
```

在 `tests/unit/backup/test_verify.py`，把

```python
    assert res.copies["missing"] == 1 and res.reason == "missing"
    assert res.copy_problems == ["missing:data/work/good/weights/last.pt"]
```

換成

```python
    assert res.copies["missing"] == 2 and res.reason == "missing"
    assert res.copy_problems == [
        "missing:data/work/good/weights/best.pt",  # a local copy belongs at the vault too
        "missing:data/work/good/weights/last.pt",
    ]
```

在 `tests/unit/backup/test_verify.py`，把

```python
    (world.vault / "good" / "best.pt").write_bytes(b"corrupt")
```

換成

```python
    (pushed / "data" / "work" / "good" / "weights" / "best.pt").write_bytes(b"corrupt")
```

在 `tests/unit/backup/test_pull_status.py`，把

```python
    (world.weights / "best.pt").unlink()  # the remote_copy comes back from vault-train
```

換成

```python
    (world.weights / "best.pt").unlink()  # pushed to the vault like a file: back from there
```

在 `tests/unit/test_e2e_backup.py`，把

```python
    rest = sum(1 for f in manifest.files if f.tier > 1 and f.kind == "file")
```

換成

```python
    rest = sum(1 for f in manifest.files if f.tier > 1)  # the local remote_copy travels too
```

在 `tests/unit/test_e2e_backup.py`，把

```python
    files = sum(1 for f in manifest.files if f.kind == "file")
    assert r.exit_code == 0 and f"ok={files + 1}" in v and "missing=0" in v
    # +1: the remote_copy, verified in place at the destination `train upload` used
```

換成

```python
    assert r.exit_code == 0 and f"ok={len(manifest.files)}" in v and "missing=0" in v
    assert "local_copies=1" in v  # the remote_copy's local copy, pushed and checked at fake:vault
```

- [ ] **Step 11: 跑測試，確認通過**

Run: `uv run pytest -o addopts="" -q tests/unit/backup tests/unit/test_e2e_backup.py tests/unit/test_cli_backup.py tests/unit/train`
Expected: 全部 PASS。

- [ ] **Step 12: Lint，然後 commit**

```bash
uv run ruff format src/vcp/backup/dest.py src/vcp/backup/schema.py src/vcp/backup/push.py src/vcp/backup/verify.py src/vcp/backup/status.py src/vcp/backup/pull.py src/vcp/backup/evidence.py src/vcp/cli_backup.py tests/unit/backup/test_local_copies.py tests/unit/backup/test_dest_push.py tests/unit/backup/test_verify.py tests/unit/backup/test_pull_status.py tests/unit/test_e2e_backup.py
uv run ruff check --fix src/vcp/backup/dest.py src/vcp/backup/schema.py src/vcp/backup/push.py src/vcp/backup/verify.py src/vcp/backup/status.py src/vcp/backup/pull.py src/vcp/backup/evidence.py src/vcp/cli_backup.py tests/unit/backup/test_local_copies.py
uv run ruff check . && uv run ruff format --check .
git diff --check
git add src/vcp/backup/dest.py src/vcp/backup/schema.py src/vcp/backup/push.py src/vcp/backup/verify.py src/vcp/backup/status.py src/vcp/backup/pull.py src/vcp/backup/evidence.py src/vcp/cli_backup.py tests/unit/backup/test_local_copies.py tests/unit/backup/test_dest_push.py tests/unit/backup/test_verify.py tests/unit/backup/test_pull_status.py tests/unit/test_e2e_backup.py
git commit -F <訊息檔>
```

訊息：`fix: 本機的 remote_copy 跟著目的地走：tier 3 推送、在目的地驗、forget 與 status 都算進去（VCP-046）`

---

### Task 5: Kaggle CLI 失敗時回讀，列表呼叫加逾時（VCP-047）

**Files:**
- Modify: `src/vcp/core/errors.py`（新 `PlatformTimeout`）
- Modify: `src/vcp/core/proc.py`（新 `timed_runner`）
- Modify: `src/vcp/submit/platforms/base.py`（`UploadResult.exit_code`、`Platform.upload(..., known_refs=)`）
- Modify: `src/vcp/submit/platforms/manual.py`（接受 `known_refs`）
- Modify: `src/vcp/submit/platforms/kaggle.py`（失敗後回讀、排除已知 ref、列表逾時）
- Modify: `src/vcp/submit/actions.py`（把台帳已知的 ref 交給平台）
- Modify: `src/vcp/submit/sync.py`（列表逾時 → `sync_failed:`）
- Modify: `src/vcp/cli_submit.py`（CLI 失敗但回讀對上 → WARN、`exit_code=`）
- Test: `tests/unit/submit/test_platforms.py`、`tests/unit/submit/test_actions.py`（修改與追加）

**Interfaces:**
- Consumes：無（跟 Task 1–4 無關）。
- Produces：
  - `vcp.core.errors.PlatformTimeout(PlatformError)`（FAIL）。
  - `vcp.core.proc.timed_runner(seconds: float) -> Runner`：時間到就殺掉子程序、丟 `subprocess.TimeoutExpired`。
  - `UploadResult.exit_code: int = 0`；`Platform.upload(staged, artifact, message, profile, runner, *, known_refs: frozenset[str] = frozenset()) -> UploadResult`。
  - `vcp.submit.platforms.kaggle.LIST_TIMEOUT = 120.0`；`KagglePlatform._read_back(..., known)` 的新結果 `known_ref`；`KagglePlatform._after_failure(...)`。
  - `vcp.submit.actions._known_refs(ledger) -> frozenset[str]`。
  - VERDICT（`submit upload`）：CLI 失敗但回讀對上 → WARN，`confirmed=true platform_ref= readback=matched exit_code= detail=`；回讀沒對上 → FAIL `upload_failed:`（`not_listed`）或 `upload_unconfirmed:`（`ambiguous` / `failed` / `interrupted` / `known_ref`），帶 `exit_code=`、`readback=`（加上原有的 `sync=`、`bound=`）。

設計說明（spec 沒寫細的地方）：
- 失敗後的判斷放在 Kaggle 平台裡（它有 CLI 的原始輸出與送出時間）；`actions` 只負責把台帳已知的 ref 交進去、照舊寫列。對上時寫的列跟 VCP-037 對上時完全一樣（`source=vcp`、`confirmed=true`、`platform_ref`），台帳格式不變。
- 「只對上已知的 ref」：時間窗內以這個 id 開頭的提交全都是台帳已有的 ref → 看完四次仍沒有新的 → 結果 `known_ref`。exit 0 時照樣寫一列未確認的 `uploaded`（跟 0.12 相同的結果，只是現在會等完四次）；CLI 失敗時是 `upload_unconfirmed:`。
- 逾時只加在列表呼叫上（上傳前同步、`sync`、回讀），每一次 CLI 呼叫各 120 秒；上傳本身不加。注入的 runner（測試）不套逾時，由測試自己丟 `TimeoutExpired`。`sync` 命令只把逾時改報 `sync_failed:`，其他列表失敗的字不變。
- 訊息保留 `kaggle CLI failed (exit N): <redact 後的最後一行>`，前面加上判決字。

- [ ] **Step 1: 寫失敗的測試（平台層）** — `tests/unit/submit/test_platforms.py`

在 `tests/unit/submit/test_platforms.py`，把

```python
import json
import logging
import subprocess
from datetime import timedelta

import pytest

from vcp.core.errors import PlatformError, ValidationFailed, VcpError
```

換成

```python
import json
import logging
import subprocess
import sys
from datetime import timedelta

import pytest

from vcp.core.errors import PlatformError, PlatformTimeout, ValidationFailed, VcpError
```

在 `tests/unit/submit/test_platforms.py`，把

```python
def test_kaggle_upload_commands_and_outcomes(tmp_path):
```

換成

```python
def test_kaggle_upload_commands_and_outcomes(tmp_path, no_wait):
```

在 `tests/unit/submit/test_platforms.py`，把

```python
    runner = FakeRunner([(1, "", f"401 Unauthorized key={SECRET}")])
    with pytest.raises(PlatformError, match="exit 1") as ei:
        p.upload(_staged(), art, "S1", _profile(), runner)
    assert SECRET not in str(ei.value) and ei.value.fields == {"exit_code": 1}
    assert ei.value.status == "FAIL"
```

換成

```python
    # a failed CLI is read back (VCP-047): nothing listed, so nothing was submitted
    looks = [_listing()] * len(kaggle.READBACK_DELAYS)
    runner = FakeRunner([(1, "", f"401 Unauthorized key={SECRET}"), *looks])
    with pytest.raises(PlatformError, match=r"upload_failed: kaggle CLI failed \(exit 1\)") as ei:
        p.upload(_staged(), art, "S1", _profile(), runner)
    assert SECRET not in str(ei.value)
    assert ei.value.fields == {"exit_code": 1, "readback": "not_listed"}
    assert ei.value.status == "FAIL" and runner.calls[1:] == [LIST] * len(looks)
```

在 `tests/unit/submit/test_platforms.py`，把

```python
        (RuntimeError("anything at all"), "failed"),
        (KeyboardInterrupt(), "interrupted"),
    ],
)
```

換成

```python
        (RuntimeError("anything at all"), "failed"),
        (subprocess.TimeoutExpired(["kaggle"], 120), "failed"),  # a list that hung (VCP-047)
        (KeyboardInterrupt(), "interrupted"),
    ],
)
```

在 `tests/unit/submit/test_platforms.py` 末尾追加：

```python


# --- VCP-047: a failed CLI is read back; known refs are looked past; lists time out -----------

CLI_DOWN = (1, "", f"('Connection aborted.', RemoteDisconnected('closed')) key={SECRET}")
LOOKS = len(kaggle.READBACK_DELAYS)
TWO_IN_THE_WINDOW = _listing((51234, "S1", 0), (51230, "S1 b", 1))
ONLY_THE_KNOWN_ONE = _listing((51234, "S1", 0))


def _upload(runner, *, known=frozenset()):
    return get_platform("kaggle").upload(
        _staged("kernel"), None, "S1", _profile(submission_kind="kernel"), runner, known_refs=known
    )


def test_a_failed_cli_is_read_back_and_an_upload_the_list_shows_is_returned(no_wait):
    runner = FakeRunner([CLI_DOWN, _listing((51234, "S1", 0))])
    res = _upload(runner)
    assert (res.confirmed, res.platform_ref, res.readback) == (True, "51234", "matched")
    assert res.exit_code == 1 and runner.calls[1:] == [LIST]
    assert "Connection aborted" in res.detail and SECRET not in res.detail


@pytest.mark.parametrize(
    ("answers", "known", "word", "outcome"),
    [
        ([_listing()] * LOOKS, frozenset(), "upload_failed", "not_listed"),
        ([TWO_IN_THE_WINDOW], frozenset(), "upload_unconfirmed", "ambiguous"),
        ([(2, "", f"503 key={SECRET}")], frozenset(), "upload_unconfirmed", "failed"),
        ([ONLY_THE_KNOWN_ONE] * LOOKS, frozenset({"51234"}), "upload_unconfirmed", "known_ref"),
    ],
    ids=["not-listed", "ambiguous", "list-failed", "only-a-known-ref"],
)
def test_a_failed_cli_the_list_does_not_settle_fails(no_wait, answers, known, word, outcome):
    runner = FakeRunner([CLI_DOWN, *answers])
    with pytest.raises(PlatformError, match=rf"{word}: kaggle CLI failed \(exit 1\)") as ei:
        _upload(runner, known=known)
    assert ei.value.fields == {"exit_code": 1, "readback": outcome}
    assert ei.value.status == "FAIL" and SECRET not in str(ei.value)


def test_a_failed_cli_whose_read_back_is_interrupted_fails_unconfirmed(no_wait):
    calls = []

    def runner(args):
        calls.append(args)
        if len(calls) == 1:
            return subprocess.CompletedProcess(args, 1, "", "connection reset")
        raise KeyboardInterrupt

    with pytest.raises(PlatformError, match="upload_unconfirmed:") as ei:
        _upload(runner)
    assert ei.value.fields == {"exit_code": 1, "readback": "interrupted"}


def test_a_read_back_looks_past_the_refs_the_ledger_holds(no_wait):
    """spec 2026-10-04 §6.1: a --force re-send while the earlier upload is still listed."""
    earlier = _listing((51230, "S1", 1))
    both = _listing((51230, "S1", 1), (51234, "S1 again", 0))
    res = _upload(FakeRunner([(0, KERNEL_REPLY, ""), earlier, both]), known=frozenset({"51230"}))
    assert res.confirmed and res.exit_code == 0
    assert (res.platform_ref, res.readback) == ("51234", "matched")


def test_a_list_that_hangs_is_cut_off_after_the_timeout(monkeypatch):
    """spec 2026-10-04 §6.3: every list call gets LIST_TIMEOUT seconds, and a real child that
    overruns it is killed."""
    assert kaggle.LIST_TIMEOUT == 120.0
    monkeypatch.setattr(kaggle, "LIST_TIMEOUT", 0.5)
    profile = _profile(kaggle_command=[sys.executable, "-c", "import time; time.sleep(5)"])
    with pytest.raises(
        PlatformTimeout, match="no answer to `competitions submissions` within 0.5 s"
    ):
        get_platform("kaggle").list_submissions(profile, None)
```

- [ ] **Step 2: 寫失敗的測試（交易層與 CLI）** — `tests/unit/submit/test_actions.py`

在 `tests/unit/submit/test_actions.py`，把

```python
import json
import subprocess
from datetime import timedelta
```

換成

```python
import json
import subprocess
import sys
from datetime import timedelta
```

在 `tests/unit/submit/test_actions.py`，把

```python
from vcp.core.errors import IntegrityError, ValidationFailed
```

換成

```python
from vcp.core.errors import IntegrityError, PlatformError, ValidationFailed
```

在 `tests/unit/submit/test_actions.py`，把

```python
from vcp.submit.stage import StageSpec, stage
```

換成

```python
from vcp.submit.stage import StageSpec, stage
from vcp.submit.sync import sync
```

在 `tests/unit/submit/test_actions.py`，把

```python
    runner = FakeRunner([EMPTY, (1, "", f"denied key={SECRET}")])
    with pytest.raises(Exception, match="exit 1") as ei:
        upload(TEST, "S1", runner=runner, **_kw(pair))
    assert SECRET not in str(ei.value)
    assert ei.value.fields == {"exit_code": 1, "sync": "ok", "bound": 0}
```

換成

```python
    looks = [EMPTY] * len(kaggle.READBACK_DELAYS)  # a failed CLI is read back (VCP-047)
    runner = FakeRunner([EMPTY, (1, "", f"denied key={SECRET}"), *looks])
    with pytest.raises(Exception, match=r"upload_failed: kaggle CLI failed \(exit 1\)") as ei:
        upload(TEST, "S1", runner=runner, **_kw(pair))
    assert SECRET not in str(ei.value)
    assert ei.value.fields == {"exit_code": 1, "readback": "not_listed", "sync": "ok", "bound": 0}
```

下一個修改落在回歸 gate 點名的 `test_a_read_back_that_meets_a_ref_the_ledger_holds_confirms_nothing` 裡：只改內容、不改名。回讀現在每一次都看過已知的 777，看完四次才說 `known_ref`，所以要給四份列表。

在 `tests/unit/submit/test_actions.py`，把

```python
    runner = FakeRunner([_listing(listed), *answers])
    again = upload(TEST, "S1", force="the first looked lost", runner=runner, **_kw(pair))
```

換成

```python
    # spec 2026-10-04 §6.1: the read-back looks past 777 at every look, then says so
    looks = [_listing(listed)] * len(kaggle.READBACK_DELAYS)
    runner = FakeRunner([_listing(listed), (0, "queued", ""), *looks])
    again = upload(TEST, "S1", force="the first looked lost", runner=runner, **_kw(pair))
```

在 `tests/unit/submit/test_actions.py` 末尾追加：

```python


# --- VCP-047: the kaggle CLI failed, so the list is read back in the same transaction ---------

CLI_DOWN = (1, "", f"('Connection aborted.', RemoteDisconnected('closed')) key={SECRET}")


def _first_row(ref: int, description: str = "S1") -> dict:
    return {"ref": ref, "fileName": "submission.csv", "date": stamp(), "description": description}


def test_a_failed_cli_whose_upload_the_list_shows_is_recorded_once(pair, no_wait):
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
    listed = _first_row(31)
    out = upload(TEST, "S1", runner=FakeRunner([EMPTY, CLI_DOWN, _listing(listed)]), **_kw(pair))
    assert (out.row.confirmed, out.row.platform_ref, out.row.source) == (True, "31", "vcp")
    assert (out.result.exit_code, out.result.readback, out.quota.used) == (1, "matched", 1)
    again = sync(TEST, runner=FakeRunner([_listing(listed)]), **_kw(pair))
    assert (again.foreign, again.bound) == (0, 0)  # its ref ties the entry to this very row
    with pytest.raises(ValidationFailed, match="quota_exhausted") as ei:
        upload(TEST, "S2", runner=FakeRunner([_listing(listed)]), **_kw(pair))
    assert ei.value.fields["quota"] == "1/1"  # one upload, counted once
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert len(led.uploads("S1")) == 1


def test_a_failed_cli_the_list_does_not_show_fails_and_writes_nothing(pair, no_wait):
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
    before = pair.test_paths.submissions_log.read_bytes()
    looks = [EMPTY] * len(kaggle.READBACK_DELAYS)
    with pytest.raises(PlatformError, match="upload_failed:") as ei:
        upload(TEST, "S1", runner=FakeRunner([EMPTY, CLI_DOWN, *looks]), **_kw(pair))
    assert ei.value.fields == {"exit_code": 1, "readback": "not_listed", "sync": "ok", "bound": 0}
    assert pair.test_paths.submissions_log.read_bytes() == before


def test_a_failed_cli_vcp_cannot_settle_fails_unconfirmed_and_writes_nothing(pair, no_wait):
    quota = Quota(per_day=5, day_tz="UTC")
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best", quota=quota))
    first = _first_row(777)
    upload(TEST, "S1", runner=FakeRunner([EMPTY, (0, "queued", ""), _listing(first)]), **_kw(pair))
    before = pair.test_paths.submissions_log.read_bytes()
    looks = len(kaggle.READBACK_DELAYS)
    twins = _listing(first, _first_row(778), _first_row(779))
    for answers, outcome in (
        ([_listing(first)] * looks, "known_ref"),  # only the earlier upload is listed
        ([twins], "ambiguous"),
        ([(2, "", "503 Service Unavailable")], "failed"),
    ):
        runner = FakeRunner([_listing(first), CLI_DOWN, *answers])
        with pytest.raises(PlatformError, match="upload_unconfirmed:") as ei:
            upload(TEST, "S1", force="the first looked lost", runner=runner, **_kw(pair))
        assert ei.value.fields["readback"] == outcome and ei.value.fields["exit_code"] == 1
        assert pair.test_paths.submissions_log.read_bytes() == before


def test_no_sync_still_reads_back_a_failed_cli(pair, no_wait):
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
    runner = FakeRunner([CLI_DOWN, _listing(_first_row(31))])
    out = upload(TEST, "S1", no_sync=True, runner=runner, **_kw(pair))
    assert (out.sync, out.result.exit_code, out.row.platform_ref) == ("skipped", 1, "31")


def test_force_after_a_failed_cli_matches_the_new_upload_past_the_known_one(pair, no_wait):
    quota = Quota(per_day=5, day_tz="UTC")
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best", quota=quota))
    first = _first_row(777)
    upload(TEST, "S1", runner=FakeRunner([EMPTY, (0, "queued", ""), _listing(first)]), **_kw(pair))
    both = _listing(first, _first_row(778, "S1 again"))
    runner = FakeRunner([_listing(first), CLI_DOWN, both])
    out = upload(TEST, "S1", force="scorer was down", runner=runner, **_kw(pair))
    assert (out.row.platform_ref, out.result.readback) == ("778", "matched")
    assert out.result.exit_code == 1 and out.row.reason == "scorer was down"


def test_a_list_that_times_out_stops_the_upload_and_sync(pair):
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))

    def hangs(args):
        raise subprocess.TimeoutExpired(args, 120)

    before = pair.test_paths.submissions_log.read_bytes()
    with pytest.raises(ValidationFailed, match="sync_failed: kaggle CLI timed out"):
        upload(TEST, "S1", runner=hangs, **_kw(pair))
    with pytest.raises(ValidationFailed, match="sync_failed: kaggle CLI timed out"):
        sync(TEST, runner=hangs, **_kw(pair))
    assert pair.test_paths.submissions_log.read_bytes() == before


def test_a_failed_cli_reads_redacted_in_the_ledger_the_log_and_the_verdict(
    pair, monkeypatch, no_wait
):
    profile = _profile(
        platform="kaggle", competition="c1", board_rule="best", kaggle_command=[sys.executable]
    )
    _staged(pair, profile)
    answers = iter([EMPTY, CLI_DOWN, _listing(_first_row(31))])

    def fake(args):
        code, out, err = next(answers)
        return subprocess.CompletedProcess(args, code, out, err)

    monkeypatch.setattr(kaggle, "default_runner", fake)
    monkeypatch.setattr(kaggle, "timed_runner", lambda seconds: fake)
    r = CliRunner().invoke(app, ["submit", "upload", "--dataset", TEST, "--id", "S1"])
    verdict = [line for line in r.output.splitlines() if line.startswith("VERDICT ")][-1]
    assert r.exit_code == 0 and "status=WARN" in verdict and "exit_code=1" in verdict, r.output
    assert "platform_ref=31" in verdict and "readback=matched" in verdict
    assert "Connection aborted" in verdict and "do not send it again" in r.output
    logs = "".join(p.read_text(encoding="utf-8") for p in (pair.roots.data / "logs").iterdir())
    ledger = pair.test_paths.submissions_log.read_text(encoding="utf-8")
    for text in (r.output, logs, ledger):
        assert SECRET not in text
```

- [ ] **Step 3: 跑測試，確認失敗**

Run: `uv run pytest -o addopts="" -q tests/unit/submit/test_platforms.py tests/unit/submit/test_actions.py`
Expected: 收集階段就 FAIL：`ImportError: cannot import name 'PlatformTimeout' from 'vcp.core.errors'`。

- [ ] **Step 4: 逾時的例外與 runner** — `src/vcp/core/errors.py`、`src/vcp/core/proc.py`

在 `src/vcp/core/errors.py`，把

```python
class PlatformError(VcpError):
    """An external tool's CLI (kaggle, rclone) ran and failed. The message is already redacted."""

    status = "FAIL"
```

換成

```python
class PlatformError(VcpError):
    """An external tool's CLI (kaggle, rclone) ran and failed. The message is already redacted."""

    status = "FAIL"


class PlatformTimeout(PlatformError):
    """An external tool's CLI gave no answer within its time limit and was stopped. The command
    that waited names it under its own word -- ``sync_failed:`` for a submissions list."""
```

在 `src/vcp/core/proc.py`，把

```python
def redact(text: str) -> str:
```

換成

```python
def timed_runner(seconds: float) -> Runner:
    """``default_runner`` with a time limit: past it the child is killed and
    ``subprocess.TimeoutExpired`` raised. For calls made while a lock is held -- a submissions
    list inside the ledger's transaction (spec 2026-10-04 §6.3) -- never for an upload itself."""

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=seconds,
        )

    return run


def redact(text: str) -> str:
```

- [ ] **Step 5: 平台契約** — `src/vcp/submit/platforms/base.py`、`src/vcp/submit/platforms/manual.py`

在 `src/vcp/submit/platforms/base.py`，把

```python
    readback: Readback | None = None  # None: no read-back happened
```

換成

```python
    readback: Readback | None = None  # None: no read-back happened
    # The CLI's own exit status: not 0 only when it failed and the read-back found the upload
    # anyway (spec 2026-10-04 §6.1) -- the row is written, the command WARNs.
    exit_code: int = 0
```

在 `src/vcp/submit/platforms/base.py`，把

```python
        profile: PlatformProfile,
        runner: Runner | None,
    ) -> UploadResult: ...
```

換成

```python
        profile: PlatformProfile,
        runner: Runner | None,
        *,
        known_refs: frozenset[str] = frozenset(),
    ) -> UploadResult: ...
```

在 `src/vcp/submit/platforms/manual.py`，把

```python
        profile: PlatformProfile,
        runner: Runner | None,
    ) -> UploadResult:
```

換成

```python
        profile: PlatformProfile,
        runner: Runner | None,
        *,
        known_refs: frozenset[str] = frozenset(),
    ) -> UploadResult:
```

- [ ] **Step 6: Kaggle：失敗後回讀、排除已知 ref、列表逾時** — `src/vcp/submit/platforms/kaggle.py`

在 `src/vcp/submit/platforms/kaggle.py`，把

```python
ref -- so the answer alone never confirmed one. The same CLI exits 0 on a file upload that
failed before anything was submitted; its words for that are a FAIL here, not a WARN.
"""
```

換成

```python
ref -- so the answer alone never confirmed one. The same CLI exits 0 on a file upload that
failed before anything was submitted; its words for that are a FAIL here, not a WARN.

A CLI that exits non-zero is read back too (VCP-047): it can lose its answer after the platform
took the upload. Every read-back looks past the refs the ledger already holds, and every call
for the list gets ``LIST_TIMEOUT`` seconds -- the list is read while the ledger's lock is held.
"""
```

在 `src/vcp/submit/platforms/kaggle.py`，把

```python
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vcp.core.errors import PlatformError, ValidationFailed, VcpError
from vcp.core.hashing import sha256_text
from vcp.core.proc import last_line
```

換成

```python
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from vcp.core.errors import PlatformError, PlatformTimeout, ValidationFailed, VcpError
from vcp.core.hashing import sha256_text
from vcp.core.proc import last_line, timed_runner
```

在 `src/vcp/submit/platforms/kaggle.py`，把

```python
from vcp.submit.schema import PlatformProfile, Staged

if TYPE_CHECKING:
    import subprocess

log = logging.getLogger("vcp")
```

換成

```python
from vcp.submit.schema import PlatformProfile, Staged

log = logging.getLogger("vcp")
```

在 `src/vcp/submit/platforms/kaggle.py`，把

```python
PAGE_SIZE = "200"
MAX_PAGES = 100
```

換成

```python
PAGE_SIZE = "200"
MAX_PAGES = 100
# Seconds one call for the submissions list may take (spec 2026-10-04 §6.3): such calls run
# while the ledger's lock is held. An upload itself has no limit -- a big file can take long.
LIST_TIMEOUT = 120.0
```

在 `src/vcp/submit/platforms/kaggle.py`，把

```python
def _command(profile: PlatformProfile, runner: Runner | None) -> tuple[list[str], Runner]:
    if runner is None:
        exe = profile.kaggle_command[0]
        if shutil.which(exe) is None:
            raise VcpError(
                f"kaggle_not_found: {exe!r} is not on PATH; set kaggle_command in submit.yaml",
                fields={"command": exe},
            )
        runner = default_runner
    return list(profile.kaggle_command), runner


def _failed(proc: subprocess.CompletedProcess[str]) -> PlatformError:
    text = (proc.stderr or "").strip() or (proc.stdout or "")
    return PlatformError(
        f"kaggle CLI failed (exit {proc.returncode}): {last_line(text)}",
        fields={"exit_code": proc.returncode},
    )
```

換成

```python
def _command(
    profile: PlatformProfile, runner: Runner | None, *, timeout: float | None = None
) -> tuple[list[str], Runner]:
    """The CLI and the runner to call it with; ``timeout`` bounds each call of the real CLI (an
    injected runner keeps its own time)."""
    if runner is None:
        exe = profile.kaggle_command[0]
        if shutil.which(exe) is None:
            raise VcpError(
                f"kaggle_not_found: {exe!r} is not on PATH; set kaggle_command in submit.yaml",
                fields={"command": exe},
            )
        runner = default_runner if timeout is None else timed_runner(timeout)
    return list(profile.kaggle_command), runner


def _cli_error(proc: subprocess.CompletedProcess[str]) -> str:
    """The redacted last line a failed CLI printed: stderr first, else stdout."""
    return last_line((proc.stderr or "").strip() or (proc.stdout or ""))


def _failed(proc: subprocess.CompletedProcess[str]) -> PlatformError:
    return PlatformError(
        f"kaggle CLI failed (exit {proc.returncode}): {_cli_error(proc)}",
        fields={"exit_code": proc.returncode},
    )
```

在 `src/vcp/submit/platforms/kaggle.py`，把

```python
        runner: Runner | None,
    ) -> UploadResult:
        base, run = _command(profile, runner)
        cmd = [*base, "competitions", "submit"]
```

換成

```python
        runner: Runner | None,
        *,
        known_refs: frozenset[str] = frozenset(),
    ) -> UploadResult:
        """``known_refs``: the refs the ledger already holds; a read-back looks past them (spec
        2026-10-04 §6.1)."""
        base, run = _command(profile, runner)
        cmd = [*base, "competitions", "submit"]
```

在 `src/vcp/submit/platforms/kaggle.py`，把

```python
        proc = run(cmd)
        if proc.returncode != 0:
            raise _failed(proc)
```

換成

```python
        proc = run(cmd)
        if proc.returncode != 0:
            return self._after_failure(
                proc, staged.submission_id, started, profile, runner, known_refs
            )
```

在 `src/vcp/submit/platforms/kaggle.py`，把

```python
        ref, outcome = self._read_back(staged.submission_id, started, profile, runner)
        return UploadResult(ref is not None, ref, detail, readback=outcome)

    def _read_back(
        self,
        submission_id: str,
        started: datetime,
        profile: PlatformProfile,
        runner: Runner | None,
    ) -> tuple[str | None, Readback]:
        """The ref of the submission this upload just made, and how the looks went:
        ``matched`` (one listed entry opens with the id and is stamped between the moment the
        CLI started and the moment it returned, give or take ``READBACK_SKEW``), ``ambiguous``
        (two), ``not_listed``, ``failed`` or ``interrupted``. The CLI accepted the upload, so
        nothing here may fail it: a look that breaks only ends the wait, and ``sync`` settles
        the rest."""
        earliest, latest = started - READBACK_SKEW, utc_now() + READBACK_SKEW
        for delay in READBACK_DELAYS:
```

換成

```python
        ref, outcome = self._read_back(staged.submission_id, started, profile, runner, known_refs)
        return UploadResult(ref is not None, ref, detail, readback=outcome)

    def _after_failure(
        self,
        proc: subprocess.CompletedProcess[str],
        submission_id: str,
        started: datetime,
        profile: PlatformProfile,
        runner: Runner | None,
        known: frozenset[str],
    ) -> UploadResult:
        """spec 2026-10-04 §6: the CLI can lose its answer after the platform took the upload,
        so a non-zero exit is read back too -- the same window and matching, inside the same
        ledger transaction. A match is the upload (recorded; the command WARNs); anything else
        fails without a row: ``upload_failed:`` when nothing for the id was listed,
        ``upload_unconfirmed:`` when vcp cannot tell."""
        head = f"kaggle CLI failed (exit {proc.returncode}): {_cli_error(proc)}"
        ref, outcome = self._read_back(submission_id, started, profile, runner, known)
        if ref is not None:
            return UploadResult(
                True, ref, _cli_error(proc), readback=outcome, exit_code=proc.returncode
            )
        fields = {"exit_code": proc.returncode, "readback": outcome}
        if outcome == "not_listed":
            raise PlatformError(
                f"upload_failed: {head}; for {sum(READBACK_DELAYS):g} s the platform listed no "
                f"submission whose description opens with {submission_id}, so it may be sent "
                "again: the next upload reads the list first and stops at already_uploaded: if "
                "it shows up late -- under --no-sync, look at the platform first",
                fields=fields,
            )
        raise PlatformError(
            f"upload_unconfirmed: {head}; read-back {outcome}: vcp cannot tell whether the "
            "platform took this upload. Before sending it again, look on the platform for a "
            f"{submission_id} submission near {stamp(started)}; if one is there, "
            "`vcp submit sync` records it",
            fields=fields,
        )

    def _read_back(
        self,
        submission_id: str,
        started: datetime,
        profile: PlatformProfile,
        runner: Runner | None,
        known: frozenset[str] = frozenset(),
    ) -> tuple[str | None, Readback]:
        """The ref of the submission this upload just made, and how the looks went:
        ``matched`` (one listed entry opens with the id, is stamped between the moment the CLI
        started and the moment it returned, give or take ``READBACK_SKEW``, and is not a ref
        the ledger holds -- an earlier upload of the id, still listed, is looked past: spec
        2026-10-04 §6.1), ``ambiguous`` (two), ``known_ref`` (only refs the ledger holds),
        ``not_listed``, ``failed`` or ``interrupted``. Nothing here raises: a look that breaks
        only ends the wait."""
        earliest, latest = started - READBACK_SKEW, utc_now() + READBACK_SKEW
        seen_known = False
        for delay in READBACK_DELAYS:
```

在 `src/vcp/submit/platforms/kaggle.py`，把

```python
            if len(refs) == 1:
                return next(iter(refs)), "matched"
            if refs:
                return None, "ambiguous"  # two uploads of this id in the window: which is it?
        return None, "not_listed"
```

換成

```python
            fresh = refs - known
            seen_known = seen_known or len(fresh) < len(refs)
            if len(fresh) == 1:
                return next(iter(fresh)), "matched"
            if fresh:
                return None, "ambiguous"  # two uploads of this id in the window: which is it?
        return None, "known_ref" if seen_known else "not_listed"
```

在 `src/vcp/submit/platforms/kaggle.py`，把

```python
    ) -> list[PlatformSubmission]:
        base, run = _command(profile, runner)
        cmd = [*base, "competitions", "submissions", "--format", "json", "--page-size", PAGE_SIZE]
        cmd.append(str(profile.competition))
        out: list[PlatformSubmission] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            proc = run(cmd + (["--page-token", token] if token else []))
```

換成

```python
    ) -> list[PlatformSubmission]:
        """Every page of the list; each call of the real CLI gets ``LIST_TIMEOUT`` seconds (spec
        2026-10-04 §6.3), then ``PlatformTimeout``."""
        base, run = _command(profile, runner, timeout=LIST_TIMEOUT)
        cmd = [*base, "competitions", "submissions", "--format", "json", "--page-size", PAGE_SIZE]
        cmd.append(str(profile.competition))
        out: list[PlatformSubmission] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            try:
                proc = run(cmd + (["--page-token", token] if token else []))
            except subprocess.TimeoutExpired:
                raise PlatformTimeout(
                    "kaggle CLI timed out: no answer to `competitions submissions` within "
                    f"{LIST_TIMEOUT:g} s"
                ) from None
```

- [ ] **Step 7: 交易層把已知 ref 交給平台；`sync` 的逾時** — `src/vcp/submit/actions.py`、`src/vcp/submit/sync.py`

在 `src/vcp/submit/actions.py`，把

```python
def _unclaimed(result: UploadResult, ledger: SubmissionLedger) -> UploadResult:
    """A ref the ledger already holds is an earlier submission's, not this upload's (VCP-037):
    a read-back can meet the last upload of the same id while this one is not listed yet."""
    known = {r.platform_ref for r in ledger.rows if r.platform_ref}
```

換成

```python
def _known_refs(ledger: SubmissionLedger) -> frozenset[str]:
    """Every platform ref the ledger already holds, in any row: earlier submissions."""
    return frozenset(r.platform_ref for r in ledger.rows if r.platform_ref)


def _unclaimed(result: UploadResult, ledger: SubmissionLedger) -> UploadResult:
    """A ref the ledger already holds is an earlier submission's, not this upload's (VCP-037).
    The read-back looks past such refs itself (spec 2026-10-04 §6.1); this guards a ref the
    CLI printed, and a platform that ignores ``known_refs``."""
    known = _known_refs(ledger)
```

在 `src/vcp/submit/actions.py`，把

```python
    result = get_platform(p.profile.platform).upload(p.staged, p.artifact, msg, p.profile, runner)
    result = _unclaimed(result, p.ledger)
```

換成

```python
    platform = get_platform(p.profile.platform)
    known = _known_refs(p.ledger)
    result = platform.upload(p.staged, p.artifact, msg, p.profile, runner, known_refs=known)
    result = _unclaimed(result, p.ledger)
```

在 `src/vcp/submit/sync.py`，把

```python
from vcp.core.errors import ValidationFailed
```

換成

```python
from vcp.core.errors import PlatformTimeout, ValidationFailed
```

在 `src/vcp/submit/sync.py`，把

```python
    with transaction(paths, profile, command="submit.sync") as ledger:
        subs = get_platform(profile.platform).list_submissions(profile, runner)
        return reconcile(paths, profile_sha, ledger, subs)
```

換成

```python
    with transaction(paths, profile, command="submit.sync") as ledger:
        try:
            subs = get_platform(profile.platform).list_submissions(profile, runner)
        except PlatformTimeout as e:  # spec 2026-10-04 §6.3: the word upload's pre-sync uses
            raise ValidationFailed(f"sync_failed: {e}") from e
        return reconcile(paths, profile_sha, ledger, subs)
```

- [ ] **Step 8: CLI：CLI 失敗但回讀對上 → WARN** — `src/vcp/cli_submit.py`

在 `src/vcp/cli_submit.py`，把

```python
        if out.result.readback:
            fields["readback"] = out.result.readback
```

換成

```python
        if out.result.readback:
            fields["readback"] = out.result.readback
        if out.result.exit_code:  # spec 2026-10-04 §6.2: the CLI failed, the list showed it
            fields["exit_code"] = out.result.exit_code
```

在 `src/vcp/cli_submit.py`，把

```python
        human = [out.result.detail] if out.result.detail else []
        if out.sync == "skipped":
```

換成

```python
        human = [out.result.detail] if out.result.detail else []
        if out.result.exit_code:
            human.append(
                f"warning: the upload CLI failed (exit {out.result.exit_code}), but the platform "
                f"lists this upload as {out.row.platform_ref}: it is recorded; do not send it "
                "again"
            )
        if out.sync == "skipped":
```

在 `src/vcp/cli_submit.py`，把

```python
        status: Status = "OK" if out.row.confirmed and out.sync == "ok" else "WARN"
        return status, fields, out.row.model_dump(mode="json", exclude_none=True), human
```

換成

```python
        ok = out.row.confirmed and out.sync == "ok" and not out.result.exit_code
        status: Status = "OK" if ok else "WARN"
        return status, fields, out.row.model_dump(mode="json", exclude_none=True), human
```

- [ ] **Step 9: 跑測試，確認通過**

Run: `uv run pytest -o addopts="" -q tests/unit/submit tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py tests/unit/test_e2e_shared_ledger.py tests/unit/core`
Expected: 全部 PASS。`test_a_list_that_hangs_is_cut_off_after_the_timeout` 用真的子程序，約 0.5 秒。

- [ ] **Step 10: Lint，然後 commit**

```bash
uv run ruff format src/vcp/core/errors.py src/vcp/core/proc.py src/vcp/submit/platforms/base.py src/vcp/submit/platforms/manual.py src/vcp/submit/platforms/kaggle.py src/vcp/submit/actions.py src/vcp/submit/sync.py src/vcp/cli_submit.py tests/unit/submit/test_platforms.py tests/unit/submit/test_actions.py
uv run ruff check --fix src/vcp/core/errors.py src/vcp/core/proc.py src/vcp/submit/platforms/base.py src/vcp/submit/platforms/manual.py src/vcp/submit/platforms/kaggle.py src/vcp/submit/actions.py src/vcp/submit/sync.py src/vcp/cli_submit.py tests/unit/submit/test_platforms.py tests/unit/submit/test_actions.py
uv run ruff check . && uv run ruff format --check .
git diff --check
git add src/vcp/core/errors.py src/vcp/core/proc.py src/vcp/submit/platforms/base.py src/vcp/submit/platforms/manual.py src/vcp/submit/platforms/kaggle.py src/vcp/submit/actions.py src/vcp/submit/sync.py src/vcp/cli_submit.py tests/unit/submit/test_platforms.py tests/unit/submit/test_actions.py
git commit -F <訊息檔>
```

訊息：`fix: Kaggle CLI 失敗時在同一個交易裡回讀，回讀排除已知 ref，列表呼叫加 120 秒逾時（VCP-047）`

---

### Task 6: 隨各缺陷更新的文件與 skill（spec §11）

**Files:**
- Modify（spec）：`docs/superpowers/specs/2026-09-13-vcp-dataset-evolution-provenance-design.md`（§6）、`docs/superpowers/specs/2026-09-13-vcp-postgresql-adaptive-provenance-design.md`（§7、§15、§20）、`docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`（§2、§3、§4.2、§5、§6、§6.1、§6.2、§8、§14）、`docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md`（§10.1、§10.2、§12、§17 第 31 條）、`docs/superpowers/specs/2026-09-28-vcp-shared-ledger-design.md`（§4.2、§4.3、§8）
- Modify（指南）：`docs/guides/POSTGRESQL_PROVENANCE.md`、`docs/guides/DATASET_EVOLUTION_PROVENANCE.md`
- Modify：`docs/reference/cli.md`（provenance 一節、備份命令表與其後一段、`submit upload` 那一列）
- Modify（skill，改完 `cp -r` 鏡射到 `.agents/skills/`）：`.claude/skills/vcp-provenance/{SKILL.md,reference.md}`、`.claude/skills/vcp-provenance-graph/{SKILL.md,reference.md}`、`.claude/skills/vcp-orientation/map.md`、`.claude/skills/vcp-release-and-environments/SKILL.md`、`.claude/skills/vcp-train-submit-backup/{SKILL.md,reference.md}`、`.claude/skills/vcp-running-contests/{workflow.md,operator-guide.md}`
- Modify：`CLAUDE.md`、`AGENTS.md`（同一個修改，各做一次）

**Interfaces:**
- Consumes：Task 1–5 的行為與字彙（`root_mismatch:`、`index_root=`、`replaced_root=`、`manifest_incomplete:`、`incomplete=`、`local_copies=`、`upload_unconfirmed:`、`exit_code=`、`readback=`、`status --json` 的 `index`）。
- Produces：文件；沒有程式介面。

規則：全部用 Edit 工具（UTF-8、LF）；spec 只加「補充決定」或在原句後補充，不改寫歷史段落的原意；skill 的路徑不寫任何比賽名稱或本機路徑。

- [ ] **Step 1: dataset evolution provenance spec §6 的補充決定**

在 `docs/superpowers/specs/2026-09-13-vcp-dataset-evolution-provenance-design.md`，把

```markdown
canonical replay 的逐 record exact comparison，不以 accumulator 取代資料完整性。
```

換成

```markdown
canonical replay 的逐 record exact comparison，不以 accumulator 取代資料完整性。

**補充決定（2026-10-04，VCP-044，0.13.0）**：索引改成每個 configs root 一份：
`<data_root>/indexes/provenance-<configs root id>.sqlite3`，id 是 `vcp.core.paths.path_id`（解析後路徑經
`os.path.normcase` 的 sha256 前 16 碼，跟台帳鎖檔同一個規則）。metadata 多記 `configs_root_id`、
`configs_root`、`data_root_id`、`data_root`（路徑只供顯示），`SCHEMA_VERSION` 從 2 變 3。每個會打開索引的
命令（`verify-index`、`status`、`sync`、`ingest`、`impact`、`stale`、`explain`、`graph`）第一件事是比對兩個
id，不符 → `root_mismatch:`（VERDICT `index_root=`），在任何 replay 或前綴檢查之前；同一個 root 內台帳真的
被截短或改寫才是 `prefix_drift:`。舊檔 `indexes/provenance.sqlite3` 不再讀（`not_found:` 會指出它），搬移或
改名 checkout 後 rebuild 一次。細節見 `2026-10-04-vcp-round3-fixes-design.md` §3。
```

- [ ] **Step 2: PostgreSQL provenance spec §7、§15、§20**

在 `docs/superpowers/specs/2026-09-13-vcp-postgresql-adaptive-provenance-design.md`，把

```markdown
index；多個 VCP data roots 使用不同 database/service，v1 不新增 namespace option。

PostgreSQL schema version為 `1`，與 SQLite `SCHEMA_VERSION = 2` 各自獨立。
```

換成

```markdown
index；多個 VCP data roots 使用不同 database/service，v1 不新增 namespace option。0.13.0 起（VCP-044）一個
database 只服務一組（data root, configs root）：同一個 data root 的不同 checkout 也各用自己的
database/service，每個 generation 的 `metadata` 記 `configs_root_id`、`configs_root`、`data_root_id`、
`data_root`（見 §20 第 2 條）。

PostgreSQL schema version為 `1`，與 SQLite `SCHEMA_VERSION`（0.13.0 起為 3）各自獨立；root metadata 是
key/value，不改 DDL，PostgreSQL 仍為 `1`。
```

在 `docs/superpowers/specs/2026-09-13-vcp-postgresql-adaptive-provenance-design.md`，把

```markdown
- schema version mismatch：FAIL並要求使用已驗證rebuild流程；不silent migration。
```

換成

```markdown
- schema version mismatch：FAIL並要求使用已驗證rebuild流程；不silent migration。
- root mismatch（0.13.0，VCP-044）：active generation 記的 root 跟命令的不同，或沒記 root（0.13.0 以前
  建的）→ `root_mismatch:`（FAIL，`index_root=<id|none>`），在任何 replay 或前綴檢查之前；`sync` 不取代，
  只有 `rebuild` 取代並 WARN `replaced_root=`。
```

在 `docs/superpowers/specs/2026-09-13-vcp-postgresql-adaptive-provenance-design.md` 末尾追加：

```markdown
2. **一個 database 只服務一個 checkout**（2026-10-04，VCP-044，0.13.0）：同一個 data root 的兩個 checkout 共用一個 database 時，另一個 root 的 `sync`（v1 是整份重建）會無聲取代前一份，`ingest` 則把對方的台帳報成 `prefix_drift:`。現在每個 generation 的 `metadata` 記 `configs_root_id`、`configs_root`、`data_root_id`、`data_root`（id 是 `vcp.core.paths.path_id`，路徑只供顯示；不改 DDL）。`verify-index`、`status`、`ingest`、`impact`、`stale`、`explain`、`graph` 在同一個交易或快照裡、任何 replay 與前綴檢查之前比對，不符 → `root_mismatch:`（`index_root=`）；`sync` 遇到別的 root 的 generation → `root_mismatch:`、整個交易 rollback；`rebuild` 是既有的修復路徑，照樣取代並 WARN `replaced_root=<id>`。0.13.0 以前建的 generation 沒記 root：除了 `rebuild`，每個命令都 `root_mismatch:`（`index_root=none`），`rebuild` 取代它並在人類訊息說明。操作規則：一個 database（service）只服務一組（data root, configs root）。細節見 `2026-10-04-vcp-round3-fixes-design.md` §3。
```

- [ ] **Step 3: 備份 spec**

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
已經由 `train upload` 驗證過的權重副本記成 `remote_copy`，verify 到原地驗、不重推。
```

換成

```markdown
已經由 `train upload` 驗證過的權重副本記成 `remote_copy`：在 rclone 遠端（或就在目的地裡）的就地驗、不重推；只在這台機器上的跟著 tier 3 推到目的地、在那裡驗（2026-10-04，VCP-046，見 §14）。
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
| 權重副本 | `train.yaml.uploads` 有 `verified=true` 的 checkpoint 記 `remote_copy`，verify 到 `<其 dest>/<run_id>/<name>` 驗 | 不重推 GB 級檔；訓練層的佈局是既定介面 |
```

換成

```markdown
| 權重副本 | `train.yaml.uploads` 有 `verified=true` 的 checkpoint 記 `remote_copy`（多筆時優先 rclone 的）；在 rclone 遠端或目的地裡的到 `<其 dest>/<run_id>/<name>` 驗，其他（只在這台機器上的）在目的地當 `file`（2026-10-04，VCP-046） | 已在別處的 GB 級檔不重推；只在這台機器上的不算備份 |
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
| `push` | `dest`、`tier`、`pushed`、`skipped`、`verified`、`failed`（清單）、`bytes` |
| `verify` | `dest`（可 null）、`copies`（`{"ok", "missing", "mismatch"}` 各為個數）、`drift`（個數）、`bad_stamps`（個數）、`first_bad`（`file:line` 或 null） |
```

換成

```markdown
| `push` | `dest`、`tier`、`pushed`、`skipped`、`verified`、`failed`（清單）、`bytes`、`local_copies`（0.13.0，大於 0 才寫） |
| `verify` | `dest`（可 null）、`copies`（`{"ok", "missing", "mismatch"}` 各為個數）、`drift`（個數）、`bad_stamps`（個數）、`first_bad`（`file:line` 或 null）、`incomplete`（0.13.0，缺口數，大於 0 才寫）、`local_copies`（0.13.0，大於 0 才寫） |
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
<其他 dest>/<run_id>/<checkpoint 檔名>       # remote_copy：訓練層 train upload 的既有佈局
```

換成

```markdown
<其他 dest>/<run_id>/<checkpoint 檔名>       # remote_copy：訓練層 train upload 的既有佈局；只在這台機器上的副本另推到上面三處（2026-10-04）
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
checkpoint 本機沒有也無副本紀錄 → 計入 `missing=` |
```

換成

```markdown
checkpoint 本機沒有也無副本紀錄 → 計入 `missing=`；VERDICT `local_copies=`（只在這台機器上的副本數，只供參考，0.13.0）；建完自檢：清單漏了 run 紀錄登記的檔 → ABORT `manifest_incomplete:`（建清單程式的 bug，什麼都不寫，0.13.0） |
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
`--forget-remote` 配本機 dest 或本次有任何失敗 → FAIL `reason=forget_refused`（不刪） |
```

換成

```markdown
`--forget-remote` 配本機 dest 或本次有任何失敗 → FAIL `reason=forget_refused`（不刪）；0.13.0：tier 3 先查完整性，缺 → FAIL `manifest_incomplete:`（`incomplete=`，不動任何檔、不寫列）；沒被目的地 cover 的 `remote_copy` 當 `file` 推，VERDICT `local_copies=`；`--forget-remote` 遇到清單缺檔或本機副本不在目的地也 `forget_refused` |
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
`present=false` 的檔本機部分略過 |
```

換成

```markdown
`present=false` 的檔本機部分略過；0.13.0：清單缺 run 紀錄登記的檔 → FAIL `reason=manifest_incomplete`（第一順位），VERDICT 一律帶 `incomplete=`；沒被 cover 的 `remote_copy` 在目的地驗，VERDICT `local_copies=` |
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
`remote_copy` 從它記錄的 dest 拉；`pull` 列 |
```

換成

```markdown
`remote_copy`：被目的地 cover 的從它記錄的 dest 拉，其他的先從目的地拉、沒有再退回本機副本（0.13.0）；`pull` 列 |
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
`rclone_conf=present` → WARN |
```

換成

```markdown
`rclone_conf=present` → WARN；0.13.0：每份清單重算完整性，缺檔 → WARN `incomplete=`、不算 verified（本機沒有 run 紀錄時看 verify 列，再看清單版本：0.10.0 以前 → `unchecked`）；清單有沒被 cover 的本機副本時，只有 0.13.0 起帶 `local_copies` 的 verify 列與 tier 3 push 列算數 |
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
`remote_copy` 條目不推。
```

換成

```markdown
`remote_copy` 條目：在 rclone 遠端或就在目的地裡的不推；其他的（只在這台機器上的）照 `file` 推，來源先用原 checkpoint、sha 不符再用 `train upload` 的副本，都不符 → 動任何檔案之前 FAIL（2026-10-04，VCP-046）。tier 3 另在動任何檔案之前檢查清單完整性（2026-10-04，VCP-045）。
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
`remote_copy` 條目：`rclone hashsum sha256 <其 dest>/<run_id>` 找 `name`。
```

換成

```markdown
`remote_copy` 條目：被目的地 cover 的（rclone 遠端上、或就在目的地裡）到 `<其 dest>/<run_id>` 找 `name`；其他的在目的地當 `file` 查，不在就是 `missing`，不看 `present`（2026-10-04，VCP-046）。
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
每個不符計一個 `drift`，`--json` 列出 `(what, expected, actual)`。
```

換成

```markdown
每個不符計一個 `drift`，`--json` 列出 `(what, expected, actual)`。另查完整性（2026-10-04，VCP-045）：清單裡每份 `train.yaml` 在 `created_at` 以前登記的 checkpoint 路徑、每份 `run.yaml` / `train.yaml` 在那以前掛上的證據與標籤集，都必須在清單裡（任何角色）；缺一個計一個 `manifest_incomplete`。
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
| 本機漂移 / 壞時戳 | `IntegrityError` `drift=` / `ValidationFailed` `bad_stamps=` | FAIL |
```

換成

```markdown
| 本機漂移 / 壞時戳 | `IntegrityError` `drift=` / `ValidationFailed` `bad_stamps=` | FAIL |
| 清單缺 run 紀錄登記的檔（0.13.0） | verify：`manifest_incomplete`（reason 第一順位）、`incomplete=`；push tier 3：`ValidationFailed` `manifest_incomplete:`；manifest 自檢：`InvariantError` `manifest_incomplete:` | FAIL／FAIL／ABORT |
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
`pulled=` `conflicts=` `rclone_conf=`。
```

換成

```markdown
`pulled=` `conflicts=` `rclone_conf=` `incomplete=` `local_copies=`（後兩個 0.13.0）。
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`，把

```markdown
- **本機副本的語意**（2026-09-07 實跑澄清，未改程式）：
```

換成

```markdown
- **本機副本的語意**（2026-09-07 實跑澄清，未改程式；已由本節最後「本機副本跟著目的地走」取代）：
```

在 `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md` 末尾追加：

```markdown

- **清單的完整性**（2026-10-04，VCP-045，0.13.0）：0.10.0 只改了清單怎麼建，沒改舊清單怎麼判。`vcp.backup.completeness.manifest_gaps`：清單裡每份 `train.yaml` 在 `created_at` 以前登記的每個 checkpoint 路徑（每個路徑取最新一筆；`registered_at` 讀不了也算），與每份 `run.yaml` / `train.yaml` 在那以前掛上的證據與標籤集的 `manifest.json`，都必須以清單鍵（`vcp.backup.manifest.entry_key`，跟建清單同一個函式）列在清單裡——任何角色、任何種類都算（`runs/<id>/train/` 下的權重是 tier 2 的 `train_dir`）。之後才登記的不算缺（那是過期，drift 會報）。verify 的一致性層每個缺口一個 `manifest_incomplete:<run>/train.yaml:checkpoints.<path>`，reason 第一順位，VERDICT 一律帶 `incomplete=`，verify 列大於 0 才寫 `incomplete`；status 每份清單重算（舊的通過列不能背書；本機沒有 run 紀錄時看 verify 列的 `incomplete`，再看清單的 `vcp_version`：早於 0.10.0 → `unchecked`）；tier 3 push 在動任何檔之前 FAIL `manifest_incomplete:`、不寫列；`--forget-remote` → `forget_refused:` 帶 `incomplete=`；`backup manifest` 建完自檢，有缺口就 `InvariantError`（ABORT），清單的 `created_at` 改在走訪之前取。細節見 `2026-10-04-vcp-round3-fixes-design.md` §4。
- **本機副本跟著目的地走**（2026-10-04，VCP-046，0.13.0；取代 2026-09-07 的本機副本註記）：`vcp.backup.dest.covers(dest, remote_copy)`——副本在 rclone 遠端上，或目的地是本機而副本就在目的地裡，才就地驗、不推；其他副本在這個目的地當 `file`：tier 3 推到 `<dest>/<root>/<path>`（來源先用原 checkpoint，sha 不符再用 `train upload` 那份；都不符 → 動任何檔之前 `drift:` / `not_found:`），verify 在目的地查（不在就是 `missing`，不看 `present`），pull 先從目的地拉、沒有再退回本機副本，`--forget-remote` 也要它在目的地。push 與 verify 的 VERDICT 帶 `local_copies=`，台帳列大於 0 才寫 `local_copies`；status 對有沒被 cover 副本的（清單, 目的地），只認帶 `local_copies` 的 verify 列與 tier 3 push 列（0.13.0 以前的 tier 3 push 列只當 tier 2）。建清單時同一路徑有多個驗過的上傳，優先選 rclone 的，同一種取最新；`backup manifest` 的 VERDICT `local_copies=` 只供參考。本機目的地（例如外接硬碟）同樣適用。細節見 `2026-10-04-vcp-round3-fixes-design.md` §5。
```

- [ ] **Step 4: 提交治理 spec**

在 `docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md`，把

```markdown
    readback: str | None = None  # CLI 的回覆確認不了時，回讀平台列表的結果（§17 第 28 條）
```

換成

```markdown
    readback: str | None = None  # CLI 的回覆確認不了時，回讀平台列表的結果（§17 第 28 條）
    exit_code: int = 0  # CLI 非 0 而回讀仍對上時，記那個退出碼（§17 第 31 條）
```

在 `docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md`，把

```markdown
        self, staged: Staged, message: str, profile: PlatformProfile, runner: Runner
    ) -> UploadResult: ...
```

換成

```markdown
        self, staged: Staged, message: str, profile: PlatformProfile, runner: Runner,
        *, known_refs: frozenset[str] = frozenset(),  # 台帳已有的 ref，回讀時排除（§17 第 31 條）
    ) -> UploadResult: ...
```

在 `docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md`，把

```markdown
都沒有 → 回讀列表（§17 第 28 條），結果記在 `readback`。
```

換成

```markdown
都沒有 → 回讀列表（§17 第 28 條），結果記在 `readback`。CLI 非 0 時也回讀，對上才寫列（§17 第 31 條）；回讀一律排除台帳已有的 ref。
```

在 `docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md`，把

```markdown
回應含下一頁 token 就帶 `--page-token` 續讀到底；
```

換成

```markdown
回應含下一頁 token 就帶 `--page-token` 續讀到底，每次 CLI 呼叫 120 秒逾時（`LIST_TIMEOUT`，§17 第 31 條）；
```

在 `docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md`，把

```markdown
| kaggle CLI 非 0 | `VcpError`（redact 後的最後一行） | FAIL |
```

換成

```markdown
| kaggle CLI 非 0，回讀沒對上（§17 第 31 條） | `PlatformError` `upload_failed:`（平台沒列出）／`upload_unconfirmed:`（判斷不了），訊息保留 `kaggle CLI failed (exit N)` 與 redact 後的最後一行，帶 `exit_code=`、`readback=`，不寫列 | FAIL |
| kaggle CLI 非 0，回讀對上（§17 第 31 條） | 寫 `uploaded` 列，VERDICT `exit_code=`、`readback=matched`、`detail=` | WARN |
| 平台列表 120 秒沒回應 | 上傳前同步與 `sync`：`ValidationFailed` `sync_failed:`；回讀：結果 `failed` | FAIL |
```

在 `docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md`，把

```markdown
`platform_ref=` `readback=` `detail=` `missing=`。
```

換成

```markdown
`platform_ref=` `readback=` `detail=` `exit_code=` `missing=`。
```

在 `docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md` 末尾追加：

```markdown

31. **CLI 失敗時回讀（VCP-047，2026-10-04；補充第 28、30 條）**：Kaggle CLI 送出後在等回應時斷線會非 0 退出，但平台其實已經收下；以前直接 FAIL、不寫列，下一次同步前台帳、`status` 與配額都少算，`--no-sync`、`--force` 或平台晚列出時還會多扣一格。現在 CLI 非 0 時在同一個上傳交易裡（持有台帳鎖）做第 28 條的回讀，時間窗與比對規則相同：對上、而且台帳還沒有這個 ref → 寫 `uploaded`（`source=vcp`、`confirmed=true`、`platform_ref`，跟第 28 條對上時同一個樣子），WARN，VERDICT 帶 `exit_code=`、`readback=matched`、`detail=<redact 後的 CLI 錯誤>`；平台約 17 秒內沒有列出以這個 id 開頭的提交 → FAIL `upload_failed:`、不寫列，可以重傳（之後才出現的，下一次 upload 的同步會擋成 `already_uploaded:`；用 `--no-sync` 前先看平台）；`ambiguous`、`failed`、`interrupted`，或只對上台帳已有的 ref → FAIL `upload_unconfirmed:`、不寫列，重傳前先到平台找這個 id、時間在送出時間附近的提交，有就 `vcp submit sync`。訊息保留 `kaggle CLI failed (exit N)` 與 redact 後的最後一行。回讀一律排除台帳已有的 ref（exit 0 也是）：`--force` 重傳時舊的那一發還在列表上，排除之後才對得到新的那一發；整個時間窗只看到已知的 ref → 結果 `known_ref`。`UploadResult` 新增 `exit_code`（預設 0），`Platform.upload` 多一個 `known_refs`。列表呼叫（上傳前同步、`sync`、回讀）每次 CLI 呼叫 120 秒逾時（`vcp.submit.platforms.kaggle.LIST_TIMEOUT`）：逾時時上傳前同步與 `sync` → `sync_failed:`，回讀 → `failed`；上傳本身不加逾時。台帳格式不變。細節見 `2026-10-04-vcp-round3-fixes-design.md` §6。
```

- [ ] **Step 5: 共用台帳 spec 的交叉引用**

在 `docs/superpowers/specs/2026-09-28-vcp-shared-ledger-design.md`，把

```markdown
其中 `upload` 的交易包含上傳前同步、配額、重傳護欄、平台上傳，以及寫入 `uploaded` 列。
```

換成

```markdown
其中 `upload` 的交易包含上傳前同步、配額、重傳護欄、平台上傳，以及寫入 `uploaded` 列；CLI 失敗後的回讀也在這個交易裡（2026-10-04，VCP-047），列表呼叫各有 120 秒逾時，卡住的列表不會一直拿著鎖（見 `2026-10-04-vcp-round3-fixes-design.md` §6）。
```

在 `docs/superpowers/specs/2026-09-28-vcp-shared-ledger-design.md`，把

```markdown
- 平台列表讀不到 → FAIL `sync_failed: <原因>`，不上傳。
```

換成

```markdown
- 平台列表讀不到 → FAIL `sync_failed: <原因>`，不上傳。列表 120 秒沒回應也是 `sync_failed:`（2026-10-04，VCP-047）。
```

在 `docs/superpowers/specs/2026-09-28-vcp-shared-ledger-design.md`，把

```markdown
- **升級**：索引在 `configs` 時建好，切到 `shared` 並 adopt 之後，`provenance sync` 加上正本的檢查點、不報漂移；
```

換成

```markdown
- **升級**：索引在 `configs` 時建好，切到 `shared` 並 adopt 之後，`provenance sync` 加上正本的檢查點、不報漂移（0.13.0 起每個 configs root 各有一份索引、記著自己的 root；共用台帳在 data root、只在鎖下增長，每份索引各自記它的檢查點，見 `2026-10-04-vcp-round3-fixes-design.md` §3）；
```

- [ ] **Step 6: 兩份指南**

在 `docs/guides/DATASET_EVOLUTION_PROVENANCE.md`，把

```markdown
`<data_root>/indexes/provenance.sqlite3` 只是可刪除的查詢索引，不進 Git。
```

換成

```markdown
`<data_root>/indexes/provenance-<configs root id>.sqlite3`（每個 checkout 一份；id 是 configs root 解析後路徑的
sha256 前 16 碼）只是可刪除的查詢索引，不進 Git。
```

在 `docs/guides/DATASET_EVOLUTION_PROVENANCE.md`，把

```markdown
- ledger checkpoint 記 consumed bytes/prefix SHA；既有 prefix 被改會 `prefix_drift`。
```

換成

```markdown
- ledger checkpoint 記 consumed bytes/prefix SHA；既有 prefix 被改會 `prefix_drift`。
- 索引記著它服務的 configs root 與 data root（0.13.0 起）：從別的 checkout、或從換了路徑的 data root 打開 →
  `root_mismatch:`（VERDICT `index_root=`），在任何前綴檢查之前，不會報成台帳被截短或改寫。`impact`、
  `stale`、`explain`、`graph` 也要能解析 configs root。搬移或改名 checkout 後 `rebuild` 一次；0.12 以前的
  `indexes/provenance.sqlite3` 不再讀（`not_found:` 會指出它），沒有 0.12 使用者之後可刪。
```

在 `docs/guides/POSTGRESQL_PROVENANCE.md`，把

```markdown
SQLite index 仍在 `<data_root>/indexes/provenance.sqlite3`。
```

換成

```markdown
SQLite index 在 `<data_root>/indexes/provenance-<configs root id>.sqlite3`，每個 checkout 一份（0.13.0 起；
0.12 以前的 `provenance.sqlite3` 不再讀）。
```

在 `docs/guides/POSTGRESQL_PROVENANCE.md`，把

```markdown
一個 active VCP provenance index；不同 data roots 應使用不同 database/service。
```

換成

```markdown
一個 active VCP provenance index，而且只服務一組（data root, configs root）：不同 data roots、同一個 data root
的不同 checkout（worktree）都各用自己的 database/service（0.13.0 起，VCP-044）。每個 generation 記下兩個
root；從別的 checkout 打開 → `root_mismatch:`（`index_root=`），`sync` 不會取代它，只有 `rebuild` 會取代並
WARN `replaced_root=`；0.13.0 以前建的 generation 沒記 root，`rebuild` 一次之前每個命令都 `root_mismatch:`。
```

在 `docs/guides/POSTGRESQL_PROVENANCE.md`，把

```markdown
- 連線或 transaction 失敗不會留下 partial active generation、checkpoint、status 或 artifact rows。
```

換成

```markdown
- 連線或 transaction 失敗不會留下 partial active generation、checkpoint、status 或 artifact rows。
- `root_mismatch:`：這個 database 屬於另一個 checkout（或還沒記 root）。給這個 checkout 自己的
  `--pg-service`；確定要接手時才 `rebuild`（WARN `replaced_root=`，原來的 checkout 之後要換 database）。
```

- [ ] **Step 7: `docs/reference/cli.md`**

在 `docs/reference/cli.md`，把

```markdown
`--backend` 時仍使用 SQLite；檔案位於 `<data_root>/indexes/provenance.sqlite3`，可刪除、可由
canonical records 完整重建，不進 Git。
```

換成

```markdown
`--backend` 時仍使用 SQLite；每個 checkout 一份，檔案位於
`<data_root>/indexes/provenance-<configs root id>.sqlite3`（0.13.0 起；id 是 configs root 解析後路徑的 sha256 前
16 碼，跟台帳鎖檔同一個規則），可刪除、可由 canonical records 完整重建，不進 Git。
```

在 `docs/reference/cli.md`，把

```markdown
policy 時會 WARN 並安全選 FULL（已驗證的零 semantic work 除外）。
```

換成

```markdown
policy 時會 WARN 並安全選 FULL（已驗證的零 semantic work 除外）。

索引的 root（0.13.0，VCP-044）：索引記下它服務的 configs root 與 data root。每個 `vcp provenance` 命令都
解析 configs root（`impact`、`stale`、`explain`、`graph` 也是；`--configs-root`，預設 repo 的 `configs/` 或
`VCP_CONFIGS_ROOT`），VERDICT 帶 `root=<configs root id>`（失敗也帶）。打開別的 checkout 的索引、或 data root
換了路徑 → 在任何前綴檢查之前 FAIL `root_mismatch:`（`index_root=<索引記的 id|none>`）；同一個 root 內台帳
被截短或改寫才是 `prefix_drift:`。0.12 以前的 `indexes/provenance.sqlite3` 不再讀（`not_found:` 會指出它；每
個 checkout `rebuild` 一次，沒有 0.12 使用者之後可刪）。PostgreSQL 一個 database 只服務一個 checkout：
`sync` 遇到別的 root 的 generation → `root_mismatch:`、不取代；`rebuild` 取代它並 WARN `replaced_root=`；
0.13.0 以前建的 generation 沒記 root，`rebuild` 之前每個命令都 `root_mismatch:`。`status --json` 的 result
帶 `index`（這個 checkout 的索引在哪）。
```

在 `docs/reference/cli.md`，把

```markdown
同名不同路徑的都會進清單 |
```

換成

```markdown
同名不同路徑的都會進清單；同一路徑有多個驗過的上傳時優先選 rclone 的。VERDICT 帶 `local_copies=`（只在這台機器上的副本數，只供參考）。建完自檢：清單漏了 run 紀錄登記的檔 → ABORT `manifest_incomplete:`（建清單程式的 bug，什麼都不寫） |
```

在 `docs/reference/cli.md`，把

```markdown
`train upload` 驗過的權重副本（`remote_copy`）不重推 |
```

換成

```markdown
`train upload` 驗過的權重副本（`remote_copy`）在 rclone 遠端上、或就在目的地裡的不重推，其他（只在這台機器上的）跟著 `--tier 3` 當一般檔推到目的地：來源先用原 checkpoint，sha 不符再用 `train upload` 那份，都不符 → 動任何檔之前 FAIL `drift:` / `not_found:`；VERDICT `local_copies=`。`--tier 3` 先查清單完整性：缺 run 紀錄登記的檔 → FAIL `manifest_incomplete:`（`incomplete=`），不動任何檔、不寫列 |
```

在 `docs/reference/cli.md`，把

```markdown
`--forget-remote`（整份清單的每個檔都在目的地驗過（含更高 tier）才 `rclone config delete <remote>`）
```

換成

```markdown
`--forget-remote`（整份清單的每個檔——含更高 tier 與只在這台機器上的副本——都在目的地驗過才 `rclone config delete <remote>`；清單缺檔 → `forget_refused:`、`incomplete=`）
```

在 `docs/reference/cli.md`，把

```markdown
清單 ↔ 現在的檔）、時戳（台帳逐列 `ts` 可解析且單調、卡的 `*_at` 可解析） |
```

換成

```markdown
清單 ↔ 現在的檔）、時戳（台帳逐列 `ts` 可解析且單調、卡的 `*_at` 可解析）。副本層對沒被目的地 cover 的 `remote_copy` 在目的地查（不在就是 `missing`，不看 `present`），VERDICT `local_copies=`；一致性層另查完整性：清單的 `run.yaml` / `train.yaml` 在清單建立前登記的每個 checkpoint 路徑與掛上的證據都要在清單裡（任何角色都算）→ 缺的是 `manifest_incomplete`（`reason=` 第一順位，補救是換新清單），VERDICT 一律帶 `incomplete=` |
```

在 `docs/reference/cli.md`，把

```markdown
只在目錄已存在時還原 |
```

換成

```markdown
只在目錄已存在時還原；被目的地 cover 的 `remote_copy` 從它自己的位置拉，其他的先從目的地拉、目的地沒有再退回 `train upload` 留在本機的那份 |
```

在 `docs/reference/cli.md`，把

```markdown
只驗本機兩層記 `local_ok` |
```

換成

```markdown
只驗本機兩層記 `local_ok`；每份清單都重算完整性：缺檔 → `verified=false`、WARN，VERDICT `incomplete=<缺檔的清單數>`（本機沒有 run 紀錄時看 verify 列的 `incomplete`，再看清單的 `vcp_version`：0.10.0 以前 → `unchecked`）；清單有沒被目的地 cover 的本機副本時，只有帶 `local_copies` 的 verify 列與 tier 3 push 列（0.13.0 起寫）算數 |
```

在 `docs/reference/cli.md`，把

```markdown
push 推的是清單那一刻的快照（前 N 位元組），pull 視為已有；被改或截短才算漂移。
```

換成

```markdown
push 推的是清單那一刻的快照（前 N 位元組），pull 視為已有；被改或截短才算漂移。0.10.0 以前、為「同檔名不同路徑」的多折 run 建的清單，0.13.0 起會報 `manifest_incomplete:`：用新 id 重建清單，重推、重驗（舊清單留著當歷史）。
```

在 `docs/reference/cli.md`，把

```markdown
CLI 回 0 卻說 `Could not submit to competition` 是 FAIL `upload_failed:`、不寫列；
```

換成

```markdown
CLI 回 0 卻說 `Could not submit to competition` 是 FAIL `upload_failed:`、不寫列；CLI 非 0（例如等回應時斷線）也在同一個交易裡回讀：對上 → 照樣寫 `uploaded` 列（`confirmed=true`）並 WARN，VERDICT 帶 `exit_code=`；平台沒列出 → FAIL `upload_failed:`；判斷不了（`ambiguous` / `failed` / `interrupted` / 只對上已知的 ref）→ FAIL `upload_unconfirmed:`，兩者都不寫列、VERDICT 帶 `exit_code=`、`readback=`；
```

在 `docs/reference/cli.md`，把

```markdown
（那一筆若台帳上已有——同 id 的上一發——就不算，`known_ref`）
```

換成

```markdown
（回讀看過台帳已有的 ref——同 id 還列著的上一發——只認新的一筆；整個時間窗只看到已知的 → `known_ref`。列表呼叫——上傳前同步、`sync`、回讀——各有 120 秒逾時：同步逾時是 `sync_failed:`，回讀逾時是 `failed`）
```

- [ ] **Step 8: skill（只改 `.claude/skills/`，最後一次鏡射）**

在 `.claude/skills/vcp-provenance/SKILL.md`，把

```markdown
- **索引是衍生品**：SQLite（`indexes/provenance.sqlite3`，預設，可刪可重建，不進 git）
```

換成

```markdown
- **索引是衍生品**：SQLite（`indexes/provenance-<configs root id>.sqlite3`，每個 checkout 一份；預設，可刪可重建，不進 git）
```

在 `.claude/skills/vcp-provenance/SKILL.md`，把

```markdown
vcp 不讀憑證檔；一個 database 一個 index；
```

換成

```markdown
vcp 不讀憑證檔；一個 database 一個 index，只服務一個 checkout（data root + configs root）；
```

在 `.claude/skills/vcp-provenance/SKILL.md`，把

```markdown
不是資料壞了，`rebuild` 一次（先講時間）。
```

換成

```markdown
不是資料壞了，`rebuild` 一次（先講時間）。
- 0.13.0 起索引記著它服務的 configs root 與 data root（VCP-044）：升級後每個 checkout `rebuild` 一次（0.12 的 `indexes/provenance.sqlite3` 不再讀，`not_found:` 會指出它）。從別的 checkout 打開 → `root_mismatch:`（`index_root=`），**不是**台帳被截短或改寫——別去查台帳：SQLite 在這個 checkout `rebuild`；PostgreSQL 給這個 checkout 自己的 `--pg-service`。同一個 root 內才會出現 `prefix_drift:`。`impact` / `stale` / `explain` / `graph` 也要解析得到 configs root（`--configs-root` 或 repo 的 `configs/`）。
```

在 `.claude/skills/vcp-provenance/reference.md`，把

```markdown
| `sync` 拒絕：既有 record 改寫 / 刪除 / prefix drift | 人工查明是誰動了台帳；確認後 `rebuild` |
```

換成

```markdown
| `sync` 拒絕：既有 record 改寫 / 刪除 / prefix drift | 人工查明是誰動了台帳；確認後 `rebuild` |
| `root_mismatch:`（`index_root=`） | 索引屬於別的 checkout（或 data root 換了路徑、或 PostgreSQL generation 是 0.13.0 以前建的）。SQLite：在這個 checkout `rebuild`；PostgreSQL：每個 checkout 用自己的 service，確定要接手才 `rebuild`（WARN `replaced_root=`） |
| `not_found:` 並指出 `indexes/provenance.sqlite3` | 那是 0.12 以前整個 data root 共用的索引；在這個 checkout `rebuild` 一次，沒有 0.12 使用者之後可刪 |
```

在 `.claude/skills/vcp-provenance-graph/SKILL.md`，把

```markdown
`vcp provenance graph` 只讀 `<data_root>/indexes/provenance.sqlite3`（或 `--backend postgresql --pg-service S`）
```

換成

```markdown
`vcp provenance graph` 只讀這個 checkout 的索引 `<data_root>/indexes/provenance-<configs root id>.sqlite3`（0.13.0 起每個 checkout 一份；或 `--backend postgresql --pg-service S`）
```

在 `.claude/skills/vcp-provenance-graph/SKILL.md`，把

```markdown
換了 vcp 版本不是 rebuild 的理由：索引 schema 沒變，真不相容時 graph 會 FAIL 並叫你 rebuild。
```

換成

```markdown
換了 vcp 版本通常不是 rebuild 的理由，真不相容時 graph 會 FAIL 並叫你 rebuild；例外是升到 0.13.0：索引改成每個 checkout 一份，`not_found:` 時 `rebuild` 一次。`root_mismatch:` 代表這份索引屬於別的 checkout（`--configs-root` 指錯了，或 checkout 搬過）。
```

在 `.claude/skills/vcp-provenance-graph/reference.md`，把

```markdown
$data = '<data_root>'
$built = (Get-Item "$data/indexes/provenance.sqlite3").LastWriteTime
```

換成

```markdown
$data = '<data_root>'; $configs = '<configs_root>'
# 索引位置每個 checkout 固定（indexes/provenance-<configs root id>.sqlite3）：讀一次就記下來
$index = (uv run vcp provenance status --json --data-root $data --configs-root $configs | ConvertFrom-Json).result.index
$built = (Get-Item $index).LastWriteTime
```

在 `.claude/skills/vcp-provenance-graph/reference.md`，把

```markdown
有輸出 → `sync`；沒輸出 → 直接畫。這只是便宜的預判。
```

換成

```markdown
有輸出 → `sync`；沒輸出 → 直接畫。這只是便宜的預判：`status` 本身要全掃一次，但同一個 checkout 的索引路徑不會變，記下來之後只看檔案時間；`status` FAIL `not_found:` → 先 `rebuild`。
```

在 `.claude/skills/vcp-orientation/map.md`，把

```markdown
`indexes/provenance.sqlite3`、`logs/`。不進 git。
```

換成

```markdown
`indexes/provenance-<configs root id>.sqlite3`（provenance 索引，每個 checkout 一份）、`logs/`。不進 git。
```

在 `.claude/skills/vcp-release-and-environments/SKILL.md`，把

```markdown
- 量測 venv 凍結後禁 install；訓練 venv 只在建環境時裝套件，之後 `uv pip freeze` 進 `requirements-*.txt`。
```

換成

```markdown
- 量測 venv 凍結後禁 install；訓練 venv 只在建環境時裝套件，之後 `uv pip freeze` 進 `requirements-*.txt`。
- **provenance 索引跟著 checkout 走**（0.13.0 起）：每個 worktree 有自己的 SQLite 索引 `<data_root>/indexes/provenance-<configs root id>.sqlite3`，在那個 worktree 跑過一次 `vcp provenance rebuild` 才有；搬移或改名 worktree 之後也要 rebuild 一次。用 PostgreSQL 時一個 worktree 一個 service／database：兩個 worktree 共用一個 database 會 `root_mismatch:`，`rebuild` 則取代別人的 generation 並 WARN `replaced_root=`。
```

在 `.claude/skills/vcp-train-submit-backup/SKILL.md`，把

```markdown
C 槽副本不是異機備份；`--forget-remote` 要整份清單在目的地驗過。
```

換成

```markdown
C 槽副本不是異機備份——`train upload --dest <本機目錄>` 留下的副本會跟著 `--tier 3` 推到目的地、在那裡驗（0.13.0 起；rclone 遠端上的副本才就地驗）；`--forget-remote` 要整份清單（含這些本機副本）在目的地驗過。清單要完整：0.10.0 以前為多折 run 建的清單會報 `manifest_incomplete:`（verify FAIL、status `incomplete=`、tier 3 push 擋下），用新 id 重建清單再推、再驗。
```

在 `.claude/skills/vcp-train-submit-backup/SKILL.md`，把

```markdown
local `remote_copy` ≠ 異機備份；
```

換成

```markdown
local `remote_copy` ≠ 異機備份（0.13.0 起 tier 3 會把它推到目的地；推完、驗完 `backup status` 才說 verified）；
```

在 `.claude/skills/vcp-train-submit-backup/SKILL.md`，把

```markdown
CLI 回 0 卻說 `Could not submit to competition` 會是 FAIL `upload_failed:`、不寫列，可以直接重傳。
```

換成

```markdown
CLI 回 0 卻說 `Could not submit to competition` 會是 FAIL `upload_failed:`、不寫列，可以直接重傳。CLI 非 0（例如等回應時斷線）時 vcp 也回讀：平台列出了 → 照寫一列、WARN `exit_code=`，**不要再傳**；沒列出 → FAIL `upload_failed:`，可以重傳（之後才出現的會被下一次 upload 的同步擋成 `already_uploaded:`；用 `--no-sync` 之前先看平台）；判斷不了 → FAIL `upload_unconfirmed:`，先到平台找這個 id 在送出時間附近的提交，有就 `vcp submit sync` 記下。
```

在 `.claude/skills/vcp-train-submit-backup/reference.md`，把

```markdown
（CLI 沒確認就回讀列表：描述以 id 開頭 + 上傳前後 2 分鐘；`readback=` 記結果）
```

換成

```markdown
（CLI 沒確認、或 CLI 非 0 時都回讀列表：描述以 id 開頭 + 上傳前後 2 分鐘，台帳已有的 ref 不算；`readback=` 記結果。CLI 非 0 而回讀對上 → 寫列、WARN `exit_code=`；沒列出 → FAIL `upload_failed:`；判斷不了 → FAIL `upload_unconfirmed:`；列表呼叫各 120 秒逾時）
```

在 `.claude/skills/vcp-train-submit-backup/reference.md`，把

```markdown
`train upload` 驗過的權重記成 `remote_copy`，verify 到原地驗、不重推。verify 三層：副本（要 `--dest`；`absent=` 是清單時已缺）、本機一致性（卡 ↔ 預測、`train.yaml` ↔ checkpoint、`fuse.json` ↔ 成員、`stage.json` ↔ 候選檔）、時戳。`verified` 要有一次連 tier 3 都過的 verify。
```

換成

```markdown
`train upload` 驗過的權重記成 `remote_copy`（同一路徑有多個時優先 rclone 的）：在 rclone 遠端上、或就在目的地裡的就地驗、不重推；其他的（只在這台機器上的）在目的地當一般檔——`--tier 3` 推過去、在那裡驗、從那裡拉（拉不到再退回本機副本），`--forget-remote` 也算它（VERDICT `local_copies=`）。verify 三層：副本（要 `--dest`；`absent=` 是清單時已缺）、本機一致性（卡 ↔ 預測、`train.yaml` ↔ checkpoint、`fuse.json` ↔ 成員、`stage.json` ↔ 候選檔；另查完整性：清單的 `run.yaml` / `train.yaml` 在清單建立前登記的每個 checkpoint 與掛上的證據都要在清單裡，缺的是 `manifest_incomplete`，`reason=` 第一順位、`incomplete=`）、時戳。`status` 每次重算完整性，舊的通過列不能背書；tier 3 push 先擋缺檔的清單；補救是用新 id 重建清單。`verified` 要有一次連 tier 3 都過的 verify（清單完整；有本機副本時要是 0.13.0 起帶 `local_copies` 的列）。
```

在 `.claude/skills/vcp-running-contests/workflow.md`，把

```markdown
| `$VCP_DATA_ROOT/indexes/` | provenance 的 SQLite 索引 | 衍生品，可刪可 rebuild，不進 git |
```

換成

```markdown
| `$VCP_DATA_ROOT/indexes/` | provenance 的 SQLite 索引，每個 checkout 一份（`provenance-<configs root id>.sqlite3`） | 衍生品，可刪可 rebuild，不進 git；新 checkout 或搬過的 checkout 先 rebuild |
```

在 `.claude/skills/vcp-running-contests/workflow.md`，把

```markdown
`remote_copy` 只證明那個目的地的副本，C 槽副本不是異機備份。
```

換成

```markdown
`remote_copy` 只證明那個目的地的副本，C 槽副本不是異機備份：0.13.0 起本機副本跟著 `--tier 3` 推到備份目的地、在那裡驗。0.10.0 以前為多折 run 建的清單會報 `manifest_incomplete:`，換新 id 重建再推、再驗。
```

在 `.claude/skills/vcp-running-contests/workflow.md`，把

```markdown
有 local backup 不代表異機備份。
```

換成

```markdown
有 local backup 不代表異機備份（`backup status` 說 `verified=True` 才算，且清單要完整）。
```

在 `.claude/skills/vcp-running-contests/operator-guide.md`，把

```markdown
本機 `remote_copy` 也不是異機備份。
```

換成

```markdown
本機 `remote_copy` 也不是異機備份（0.13.0 起 `backup push --tier 3` 會把它送到目的地；推完、驗完 `backup status` 才說 verified）。
```

鏡射並檢查：

```bash
for s in vcp-provenance vcp-provenance-graph vcp-orientation vcp-release-and-environments vcp-train-submit-backup vcp-running-contests; do cp -r ".claude/skills/$s/." ".agents/skills/$s/"; done
diff -r .claude/skills .agents/skills
uv run pytest -o addopts="" -q tests/unit/test_skills_plugin.py tests/unit/provenance/test_postgres_docs.py
```

Expected: `diff` 沒有輸出；兩個測試 PASS。

- [ ] **Step 9: `CLAUDE.md` 與 `AGENTS.md`（同一個修改，兩個檔各做一次）**

在 `CLAUDE.md`，把

```markdown
同一個 id 再傳要 `--force "<理由>"`。輸出檔與 `stage.json`
```

換成

```markdown
同一個 id 再傳要 `--force "<理由>"`；kaggle CLI 非 0 時在同一個交易裡回讀：平台列出了就照寫一列並 WARN（`exit_code=`），沒列出 `upload_failed:`，判斷不了 `upload_unconfirmed:`；列表呼叫各 120 秒逾時。輸出檔與 `stage.json`
```

在 `AGENTS.md`，把

```markdown
同一個 id 再傳要 `--force "<理由>"`。輸出檔與 `stage.json`
```

換成

```markdown
同一個 id 再傳要 `--force "<理由>"`；kaggle CLI 非 0 時在同一個交易裡回讀：平台列出了就照寫一列並 WARN（`exit_code=`），沒列出 `upload_failed:`，判斷不了 `upload_unconfirmed:`；列表呼叫各 120 秒逾時。輸出檔與 `stage.json`
```

在 `CLAUDE.md`，把

```markdown
；`train upload` 驗過的權重副本記成 `remote_copy`，verify 到原地驗、不重推。`cache/`、`raw/` 永不進清單；push 推台帳的快照，`--forget-remote` 要整份清單驗證通過。
```

換成

```markdown
；`train upload` 驗過的權重副本記成 `remote_copy`：在 rclone 遠端（或就在目的地裡）的就地驗、不重推，只在這台機器上的跟著 `--tier 3` 推到目的地、在那裡驗。清單要完整：run 紀錄在清單建立前登記的 checkpoint 與證據都要在清單裡，否則 verify FAIL `manifest_incomplete`、status 不算 verified、tier 3 push 擋下（補救是用新 id 重建）。`cache/`、`raw/` 永不進清單；push 推台帳的快照，`--forget-remote` 要整份清單（含本機副本）驗證通過。
```

在 `AGENTS.md`，把

```markdown
；`train upload` 驗過的權重副本記成 `remote_copy`，verify 到原地驗、不重推。`cache/`、`raw/` 永不進清單；push 推台帳的快照，`--forget-remote` 要整份清單驗證通過。
```

換成

```markdown
；`train upload` 驗過的權重副本記成 `remote_copy`：在 rclone 遠端（或就在目的地裡）的就地驗、不重推，只在這台機器上的跟著 `--tier 3` 推到目的地、在那裡驗。清單要完整：run 紀錄在清單建立前登記的 checkpoint 與證據都要在清單裡，否則 verify FAIL `manifest_incomplete`、status 不算 verified、tier 3 push 擋下（補救是用新 id 重建）。`cache/`、`raw/` 永不進清單；push 推台帳的快照，`--forget-remote` 要整份清單（含本機副本）驗證通過。
```

在 `CLAUDE.md`，把

```markdown
`indexes/provenance.sqlite3` 是可刪除衍生索引，絕不進 Git；
```

換成

```markdown
`indexes/provenance-<configs root id>.sqlite3` 是可刪除衍生索引（每個 checkout 一份，記著它服務的 configs root 與 data root；從別的 checkout 打開 → `root_mismatch:`；0.12 的 `provenance.sqlite3` 不再讀），絕不進 Git；
```

在 `AGENTS.md`，把

```markdown
`indexes/provenance.sqlite3` 是可刪除衍生索引，絕不進 Git；
```

換成

```markdown
`indexes/provenance-<configs root id>.sqlite3` 是可刪除衍生索引（每個 checkout 一份，記著它服務的 configs root 與 data root；從別的 checkout 打開 → `root_mismatch:`；0.12 的 `provenance.sqlite3` 不再讀），絕不進 Git；
```

在 `CLAUDE.md`，把

```markdown
PostgreSQL v1 一個 database 對一個 active index，透過 libpq service 連線
```

換成

```markdown
PostgreSQL v1 一個 database 對一個 active index、只服務一個 checkout（`sync` 不取代別的 root 的 generation，`rebuild` 取代時 WARN `replaced_root=`），透過 libpq service 連線
```

在 `AGENTS.md`，把

```markdown
PostgreSQL v1 一個 database 對一個 active index，透過 libpq service 連線
```

換成

```markdown
PostgreSQL v1 一個 database 對一個 active index、只服務一個 checkout（`sync` 不取代別的 root 的 generation，`rebuild` 取代時 WARN `replaced_root=`），透過 libpq service 連線
```

- [ ] **Step 10: 檢查並 commit**

```bash
git diff --check
diff -r .claude/skills .agents/skills
git add docs/superpowers/specs/2026-09-13-vcp-dataset-evolution-provenance-design.md docs/superpowers/specs/2026-09-13-vcp-postgresql-adaptive-provenance-design.md docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md docs/superpowers/specs/2026-09-28-vcp-shared-ledger-design.md docs/guides/POSTGRESQL_PROVENANCE.md docs/guides/DATASET_EVOLUTION_PROVENANCE.md docs/reference/cli.md CLAUDE.md AGENTS.md .claude/skills/vcp-provenance/SKILL.md .claude/skills/vcp-provenance/reference.md .claude/skills/vcp-provenance-graph/SKILL.md .claude/skills/vcp-provenance-graph/reference.md .claude/skills/vcp-orientation/map.md .claude/skills/vcp-release-and-environments/SKILL.md .claude/skills/vcp-train-submit-backup/SKILL.md .claude/skills/vcp-train-submit-backup/reference.md .claude/skills/vcp-running-contests/workflow.md .claude/skills/vcp-running-contests/operator-guide.md .agents/skills/vcp-provenance/SKILL.md .agents/skills/vcp-provenance/reference.md .agents/skills/vcp-provenance-graph/SKILL.md .agents/skills/vcp-provenance-graph/reference.md .agents/skills/vcp-orientation/map.md .agents/skills/vcp-release-and-environments/SKILL.md .agents/skills/vcp-train-submit-backup/SKILL.md .agents/skills/vcp-train-submit-backup/reference.md .agents/skills/vcp-running-contests/workflow.md .agents/skills/vcp-running-contests/operator-guide.md
git commit -F <訊息檔>
```

訊息：`docs: VCP-044～047 的 spec 補充決定、指南、命令參考、skill 與 CLAUDE.md／AGENTS.md`

---

### Task 7: 文件同步（spec §10）

**Files:**
- Modify：`docs/audits/2026-09-11-vcp-improvement-audit.md`（檔頭、§1 表、VCP-001／002／003／005／007／008／009／014／034 的狀態行、檔尾追加第 17 節）
- Modify：`CHANGELOG.md`（表頭第 3、9 行；版本規則多一條）
- Modify：`README.md`、`README.zh-TW.md`（「Status and roadmap／狀態與路線」的兩句）
- Modify：`docs/handover/HANDOVER.md`（§1 表的 `vcp submit` 命令數、§3 程式碼地圖、§2 真資料測試、§6、§8）、`docs/handover/CODEX_PROMPT.md`（版本與測試數字、開放待辦）、`docs/handover/DATASET_EVOLUTION_PROVENANCE_HANDOFF.md`（§8、§9 標成歷史）
- Modify：`CLAUDE.md`，然後 `AGENTS.md` 整份複製成同一份
- Modify（skill，改完 `cp -r` 鏡射）：`.claude/skills/vcp-orientation/map.md`、`.claude/skills/vcp-release-and-environments/SKILL.md`
- Create：`docs/reference/commit-map-2026-10.tsv`（由腳本產生，不手寫）

**Interfaces:**
- Consumes：Task 1–6 的行為與字彙（只寫進文件）；Task 6 改過的 `CLAUDE.md`、`AGENTS.md`、`map.md`、`vcp-release-and-environments/SKILL.md`（本 task 的錨點是 Task 6 之後的文字）。
- Produces：`docs/reference/commit-map-2026-10.tsv`（tab 分隔，表頭四欄 `old`、`new`、`date`、`subject`，之後 481 列）；Task 9 會再改 `CHANGELOG.md`、`HANDOVER.md`、`CODEX_PROMPT.md`、兩份 README 的版本與數字，錨點是本 task 寫下的文字。

規則：
- 全部用 Edit 工具（UTF-8、LF）。新寫的文字不放比賽名稱、比賽的 id 或本機路徑；既有段落裡已經有的不動（稽核文件是比賽工作區的副本）。
- 1.0 門檻一律用 spec §10.2 這一句（英文 README 用同義的英文）：「`1.0.0` 留給稽核 Wave 1 全部落地之後，包括 1c（程式碼快照與產物授權，VCP-004／006）。稽核 §11 Wave 1 的第 4 項（單一大陣列的選取列存取器）與第 5 項（合成插件端到端、比賽原型遷移）排在 1.0 之後。」
- HANDOVER 與 CODEX_PROMPT 標題的日期、以及版本號與測試數字的 0.13.0 部分，留給 Task 9（發版當天才知道日期與數字，寫在這裡會再過時一次）。本 task 先把 CODEX_PROMPT 的數字更正到 0.12.0 的實數。

- [ ] **Step 1: 稽核文件的檔頭、§1 表與狀態行**

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
本 repo 的 Wave 0 已於 v0.3.0 完成（見 CHANGELOG）；Wave 1 拆成 1a 不可變產物（spec：docs/superpowers/specs/2026-09-11-vcp-immutable-artifacts-design.md）、1b 角色範圍存取與收據、1c 程式碼快照與授權。第 16 節是 2026-09-25 第二輪回報（VCP-035 – 043）的補遺，只收通用的缺陷與處置。
```

換成

```markdown
本 repo 的 Wave 0 已於 v0.3.0 完成（見 CHANGELOG）；Wave 1 拆成 1a 不可變產物（v0.4.0，spec：docs/superpowers/specs/2026-09-11-vcp-immutable-artifacts-design.md）、1b 角色範圍存取與收據（1b-1，v0.5.0）與來源稽核（1b-2，v0.6.0，只涵蓋 samples.jsonl）、1c 程式碼快照與產物授權（尚未做）。各項的「狀態」行與第 1 節表格的「目前狀態」欄隨 0.13.0 依 CHANGELOG 更新過，其餘段落保留 2026-09-11 的原文。第 16 節是 2026-09-25 第二輪回報（VCP-035 – 043）的補遺，第 17 節是 2026-09-26 第三輪回報（VCP-044 – 047）的補遺，只收通用的缺陷與處置。
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
| 開放問題；RSNA 有專案 workaround |
```

換成

```markdown
| v0.5.0 已修（Wave 1b-1；收據只證明經存取器的讀取） |
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
| 開放問題；RSNA 已做局部原型 |
```

換成

```markdown
| 部分：v0.6.0（Wave 1b-2）只涵蓋 `samples.jsonl`；大陣列的選取列存取器排在 1.0 之後 |
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
| 開放問題；曾發生 evidence overwrite |
```

換成

```markdown
| v0.4.0 已修（Wave 1a） |
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
| `d193113` 已修、已上遠端分支、未進 main |
```

換成

```markdown
| v0.3.0 已修（Wave 0） |
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
| `d881a1d` 已修、本機分支、未進 main |
```

換成

```markdown
| v0.3.0 已修（Wave 0） |
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
| P0 | 自動 code/environment inventory | 開放問題 |
```

換成

```markdown
| P0 | 自動 code/environment inventory | 開放問題（Wave 1c）；v0.11.0 起 `train run` 記下工作樹狀態與追蹤檔的 patch（VCP-041） |
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
| P1 | Submission watch/reconcile/idempotency | 開放問題 |
```

換成

```markdown
| P1 | Submission watch/reconcile/idempotency | 部分：同 id 重傳護欄隨 0.12.0（VCP-014）、CLI 失敗時回讀隨 0.13.0（VCP-047）；`watch` / `reconcile` 未做 |
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
| 外部工作分支進行中 |
```

換成

```markdown
| 已進 main（PR #6，隨 v0.8.1） |
```

狀態行（一項一個；VCP-003 的狀態行跟別項同字，所以連標題一起當錨點）：

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
**狀態：設計缺口；RSNA 已有 selected-only workaround。**
```

換成

```markdown
**狀態：v0.5.0 已修（Wave 1b-1）：`DatasetAccess` 只解析授權子集的列，關閉時由框架寫存取收據；見 CHANGELOG [0.5.0]。收據只證明經存取器的讀取，自己開檔的程序不在證明範圍內。**
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
**狀態：設計缺口；RSNA six-slot v2 已實作局部原型。**
```

換成

```markdown
**狀態：部分修正。v0.6.0（Wave 1b-2）只涵蓋 `samples.jsonl`：`source_audit` 逐列 sha 索引，存取器只驗讀到的列，不再每個 job 整檔 hash。單一大陣列的選取列存取器與 `array_audit`（§11 Wave 1 第 4 項）排在 1.0 之後。**
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
### VCP-003：access flags 是自我宣告，無法代表真實 I/O

**狀態：設計缺口。**
```

換成

```markdown
### VCP-003：access flags 是自我宣告，無法代表真實 I/O

**狀態：v0.5.0 已修（Wave 1b-1）：存取器關閉時由框架寫 `AccessReceipt`，呼叫端只能加 `notes`；provenance 等級在讀取時由收據算出（`receipt > export > declared`），不再採信自我宣告的 flags。見 CHANGELOG [0.5.0]。**
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
**狀態：設計缺口；RSNA 已改用 exclusive-create workaround。**
```

換成

```markdown
**狀態：v0.4.0 已修（Wave 1a）：`ArtifactWriter` 以獨佔 mkdir 搶 id，`manifest.json` 是 commit 點，修正走新 id 加 `supersedes`；見 CHANGELOG [0.4.0]。**
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
**狀態：設計缺口；由 review 發現並修正專案設定。**
```

換成

```markdown
**狀態：v0.4.0 已修（Wave 1a）：`ArtifactSpec.id_pattern` 的具名群組必須等於同名欄位，`inputs` 在 open 時雜湊、commit 時重驗；見 CHANGELOG [0.4.0]。commit 時比對程式碼快照的部分等 Wave 1c。**
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
**狀態：已在 `codex/prereg-sha-integrity` 修正；commit `d193113`；遠端分支存在；未進 main。**
```

換成

```markdown
**狀態：v0.3.0 已修（Wave 0，原 commit `d193113`）；見 CHANGELOG [0.3.0]。**
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
**狀態：已在 `codex/submit-foreign-score-refresh` 修正；commit `d881a1d`；本機分支；未進 main。**
```

換成

```markdown
**狀態：v0.3.0 已修（Wave 0，原 commit `d881a1d`）；見 CHANGELOG [0.3.0]。**
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
下面的 `reconcile` 與配額保留仍是建議。**
```

換成

```markdown
下面的 `reconcile` 與配額保留仍是建議。kaggle CLI 非 0 退出時在同一個交易裡回讀（VCP-047）隨 0.13.0 發出，見第 17 節。**
```

在 `docs/audits/2026-09-11-vcp-improvement-audit.md`，把

```markdown
**狀態：外部 `codex/vcp-visual-guide` 工作中；不是 main 已完成能力。**
```

換成

```markdown
**狀態：已進 main（PR #6，2026-09-15，隨 v0.8.1 發出）：`docs/guides/VCP_VISUAL_GUIDE.md`，README 與 HANDOVER 都有連結。**
```

- [ ] **Step 2: 稽核文件的第 17 節（第三輪回報）**

沿用第 16 節的格式：來源一段、一張表、每項一個 `### VCP-0xx` 與 `**狀態：…**`。嚴重度照回報原文。

在 `docs/audits/2026-09-11-vcp-improvement-audit.md` 末尾追加：

```markdown

## 17. 補遺：第三輪回報（VCP-044 – VCP-047，2026-09-26）

來源：比賽工作區在 2026-09-26 升到 0.10.0 時交給 VCP 的第三輪回報（不在本 repo）。以下只留通用的缺陷與修法，比賽的資料、路徑與 id 都不收。四件都在 0.12.0 上重現過；使用者 2026-10-04 裁決一起修，隨 0.13.0 發出（spec `2026-10-04-vcp-round3-fixes-design.md`）。

| ID | 類型 | 嚴重度 | 問題 | 處置 |
|---|---|---|---|---|
| VCP-044 | DEFECT（含 FEATURE_GAP） | 低中 | provenance 索引不認 configs root：兩個 checkout 共用一個 data root 時，分岔的台帳被報成 `prefix_drift:`；PostgreSQL 的 `sync` 無聲取代另一個 root 的索引 | 隨 0.13.0 |
| VCP-045 | DEFECT | 中 | 0.10.0 以前建的多折備份清單缺權重，verify、tier 3 push 與 status 都看不出來 | 隨 0.13.0 |
| VCP-046 | DEFECT | 中 | `train upload` 到本機目錄的副本被 tier 3 push、verify、status 與 `--forget-remote` 當成異機備份 | 隨 0.13.0 |
| VCP-047 | DEFECT | 中 | Kaggle CLI 非 0 退出時不回讀，平台其實已收下；列表呼叫沒有逾時 | 隨 0.13.0 |

### VCP-044：provenance 索引不認 configs root

**狀態：已實作，隨 0.13.0 發出。** SQLite 索引一個 data root 只有一份，台帳檢查點只記 `configs/<相對路徑>`、驗證時用當下的 configs root 解析；兩個 checkout 共用 data root 時，從沒建索引的那邊驗證，分岔的台帳看起來像被截短或竄改，任一邊 rebuild 之後失敗就換到另一邊。現在每個 configs root 一份索引（`indexes/provenance-<configs root id>.sqlite3`；root id 跟鎖檔同一個規則，是解析後路徑的 sha256 前 16 碼），兩種後端都記下索引服務的 configs root 與 data root，root 不符時在任何前綴檢查之前 FAIL `root_mismatch:`（`index_root=`）。PostgreSQL 一個 database 只服務一個 checkout：`sync` 不取代別的 root 的 generation，`rebuild` 取代時 WARN `replaced_root=`；沒記 root 的舊 generation 在 rebuild 之前一律 FAIL。0.12 的 `provenance.sqlite3` 不再讀，每個 checkout rebuild 一次。

### VCP-045：舊備份清單缺檔看不出來

**狀態：已實作，隨 0.13.0 發出。** VCP-035 只改了新清單的建法；0.10.0 以前為「同檔名、不同路徑」的多折 run 建的清單只列最後一折，照樣通過 verify、tier 3 的 push 與 verify，status 也說 verified。現在清單要完整：run 紀錄在清單建立前登記的每個 checkpoint 路徑與每個證據，都要在清單裡（任何角色都算）。缺了 → verify FAIL `manifest_incomplete`（`incomplete=`）；status WARN 且不算 verified（每次從檔案重算，舊的通過列不能背書）；tier 3 push 在動任何檔案之前 FAIL；`--forget-remote` 拒絕；`backup manifest` 寫出前自檢，有缺口 ABORT。補救是用新 id 重建清單，重推、重驗。

### VCP-046：本機的 `remote_copy` 被當成異機備份

**狀態：已實作，隨 0.13.0 發出。** `train upload --dest <本機目錄>` 驗過的權重在清單裡記成 `remote_copy`；tier 3 push 不送它、verify 到它自己的本機目錄就地驗、status 說 verified、`--forget-remote` 照刪憑證，權重其實沒離開過這台機器。現在只有在 rclone 遠端、或就在目的地裡的副本才就地驗；其餘的本機副本跟著 `--tier 3` 推到目的地（從原檔送，原檔不在或 sha 不符就從本機副本送），在目的地驗，VERDICT 帶 `local_copies=`。0.13 以前的推送與驗證列不替本機副本背書，用 0.13 推、驗一次之後 status 才說 verified；`--forget-remote` 在權重到目的地之前拒絕；`backup pull` 先從目的地拉，沒有再退回本機副本。

### VCP-047：Kaggle CLI 失敗時不回讀

**狀態：已實作，隨 0.13.0 發出。** CLI 送出後在等回應時斷線（非 0 退出），vcp 直接 FAIL、不寫列，但平台其實已收下；0.12 的上傳前同步要到下一次 upload 才補登，這之前台帳、`status` 與配額都少算，`--no-sync`、`--force` 或平台晚一點才列出時還會多扣一格。現在 CLI 非 0 也在同一個交易裡回讀（沿用 VCP-037 的時間窗與比對，排除台帳已知的 ref）：對上 → 照寫一列，WARN 帶 `exit_code=`；平台沒列出 → FAIL `upload_failed:`，不寫列，可以重傳；判斷不了（ambiguous、列表失敗、中斷、只看到已知的 ref）→ FAIL `upload_unconfirmed:`，不寫列，先到平台確認再決定。Kaggle 的列表呼叫各加 120 秒逾時，上傳前同步與 `sync` 逾時是 `sync_failed:`。
```

- [ ] **Step 3: 1.0 門檻統一成一種說法**

在 `CHANGELOG.md`，把

```markdown
停在 `0.x` 直到稽核的 P0 provenance substrate 落地（見「版本規則」）。
```

換成

```markdown
停在 `0.x` 直到稽核的 Wave 1 全部落地（見「版本規則」）。
```

在 `CHANGELOG.md`，把

```markdown
- **`1.0.0`**：留給 2026-09-11 稽核的 Wave 1（role-scoped access、immutable artifact writer、access receipt、code snapshot）落地之後——屆時產物契約才是可以對外承諾的契約。
```

換成

```markdown
- **`1.0.0`**：留給 2026-09-11 稽核的 Wave 1 全部落地之後，包括 1c（程式碼快照與產物授權，VCP-004／006）——屆時產物契約才是可以對外承諾的契約。稽核 §11 Wave 1 的第 4 項（單一大陣列的選取列存取器）與第 5 項（合成插件端到端、比賽原型遷移）排在 1.0 之後。
```

在 `README.md`，把

```markdown
- **Next**: audit wave 1c (code snapshot and authorisation receipts) is the last item before `1.0.0`; then adaptive policy v2.
```

換成

```markdown
- **Next**: audit wave 1c (code snapshot and artifact authorisation, VCP-004/006). `1.0.0` comes once all of audit wave 1 has landed, 1c included; items 4 (a selected-row accessor for one large array) and 5 (a synthetic plugin end to end, then migrating the contest prototypes) of wave 1 in the audit's §11 come after 1.0, and so does adaptive policy v2.
```

在 `README.zh-TW.md`，把

```markdown
- **下一步**：稽核 Wave 1c（程式碼快照與授權收據）是 `1.0.0` 前最後一項；之後是 adaptive policy v2。
```

換成

```markdown
- **下一步**：稽核 Wave 1c（程式碼快照與產物授權，VCP-004／006）。`1.0.0` 留給稽核 Wave 1 全部落地之後，包括 1c；稽核 §11 Wave 1 的第 4 項（單一大陣列的選取列存取器）與第 5 項（合成插件端到端、比賽原型遷移）排在 1.0 之後，adaptive policy v2 也是。
```

在 `.claude/skills/vcp-release-and-environments/SKILL.md`，把

```markdown
**PATCH**：其餘（bug、訊息、效能、測試、文件、內部重構）。`1.0.0` 留給稽核 Wave 1 落地。
```

換成

```markdown
**PATCH**：其餘（bug、訊息、效能、測試、文件、內部重構）。`1.0.0` 留給稽核 Wave 1 全部落地之後，包括 1c（程式碼快照與產物授權，VCP-004／006）。稽核 §11 Wave 1 的第 4 項（單一大陣列的選取列存取器）與第 5 項（合成插件端到端、比賽原型遷移）排在 1.0 之後。
```

`CLAUDE.md` / `AGENTS.md` 的同一句在 Step 6，CODEX_PROMPT 的在 Step 5。

- [ ] **Step 4: 兩份 README 的穩定度說法**

在 `README.md`，把

```markdown
`0.12.0`, pre-1.0: artifact and ledger formats are stable enough to build on; the CLI contract can still change on a minor version ([CHANGELOG.md](CHANGELOG.md) says what each bump means).
```

換成

```markdown
`0.12.0`, pre-1.0: a minor version may still change what vcp writes (artifact and ledger formats) or the CLI contract, and [CHANGELOG.md](CHANGELOG.md) says what each one changed. Once you use a feature a newer version added, an older vcp may not read the records it wrote.
```

在 `README.zh-TW.md`，把

```markdown
`0.12.0`，尚未到 1.0：產物與台帳格式已經穩定到可以在上面蓋東西；CLI 契約在 minor 版本仍可能改變（每次 bump 的意義見 [CHANGELOG.md](CHANGELOG.md)）。
```

換成

```markdown
`0.12.0`，尚未到 1.0：MINOR 版仍可能改寫入格式（產物與台帳）或 CLI 契約，每次改了什麼寫在 [CHANGELOG.md](CHANGELOG.md)；用到新版加的功能之後，舊版 vcp 可能讀不動新紀錄。
```

- [ ] **Step 5: 交接文件**

HANDOVER §1 表：`vcp submit` 從 0.12.0 起是 13 個命令。

在 `docs/handover/HANDOVER.md`，把

```markdown
`vcp submit` ×12（init/stage/verify/upload/record/score/sync/final/lock/unlock/status/report）
```

換成

```markdown
`vcp submit` ×13（init/stage/verify/upload/record/score/sync/final/lock/unlock/status/report/ledger adopt）
```

HANDOVER §2 的真資料測試：`-m realdata` 現在收 15 個；文件記的 9 / 3 是 2026-09-07 的 12 個，之後加的三個還沒有真資料上的紀錄。這裡不重跑（要有匯入真資料的機器），只把數字說實。

在 `docs/handover/HANDOVER.md`，把

```markdown
`uv run pytest tests/integration -o addopts="" -q -m realdata` → 9 passed / 3 skipped
```

換成

```markdown
`uv run pytest tests/integration -o addopts="" -q -m realdata` 收 15 個唯讀測試，其中 `test_artifact_status`、`test_access_receipts`、`test_audited_access` 是之後加的，還沒有在真資料上跑過的紀錄（跑過就把數字補在這裡）；有紀錄的最後一次是 2026-09-07 的另外 12 個：9 passed / 3 skipped
```

HANDOVER §3 程式碼地圖：補上 0.4.0 以來加的模組（含本計畫的 `provenance/roots.py`、`backup/completeness.py`）。`projects/` 那一行照舊（那個目錄真的在 repo 裡）。

在 `docs/handover/HANDOVER.md`，把

```text
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
```

換成

```text
src/vcp/core      time（唯一時鐘）errors（VERDICT 狀態）log（VERDICT 行 / jsonl log）paths（DatasetPaths、path_id）
                  hashing config（YAML ↔ pydantic、is_true）proc（子程序 runner + redact、timed_runner）atomic（write_once 原語）
                  lock（跨程序檔案鎖）build（build string）
src/vcp/data      schema tasks（任務登記表）dataset split（plan、assert_plan_matches）lineage
                  importers/ exporters/ audit/ materialize/ dicomio access/（DatasetAccess 與收據）source_audit
                  labels（label_set）evidence evidence_ref（run 讀過的證據）
src/vcp/measure   schema runs（run.yaml、FUSE_FRAMEWORK）predictions converters/ metrics/ masks ingest
                  measure（護欄 → 讀數）anchors ledger（三個台帳檔名）prereg judge sigma stats report plugins
                  provenance（receipt > export > declared）
src/vcp/fuse      schema recipes（配方進 git）fusers/（wbf/mean/rank_mean）members build（fuse.json）ablate
src/vcp/train     schema records（train.yaml + train.log.jsonl）env gitstate（dirty 工作樹）checkpoints upload
                  attach（--evidence / --labels）run session reader status
src/vcp/submit    schema profile（submit.yaml）ledger（submissions.jsonl）location（台帳位置與交易）adopt
                  timewin guards pairing matching kernel gate
                  writers/（scores_csv/coco_results/csv_boxes）platforms/（manual/kaggle）stage actions sync final report
src/vcp/backup    schema ledger manifest completeness（清單完整性）evidence（證據圖）dest（本機 / rclone）
                  push verify pull status
src/vcp/artifact  schema（pydantic 模型、check_file_name）ledger（supersession.jsonl 讀寫）store（load/reuse/verify）
                  writer（ArtifactWriter：claim/write/commit）lineage（chain/successors/head/forks）clean（scan/clean）
src/vcp/provenance schema/policy/diff（dataset_diff artifact）graph/views（full oracle）roots（索引服務的 root）
                  backend（SQLite adapter / optional PostgreSQL）postgres + postgres_schema（v1 normalized derived index）
                  strategy（immutable adaptive policy）index（SQLite cache）render + render_mermaid（provenance graph）
src/vcp/cli*.py   每層一個 typer app（含 cli_artifact.py 的 `vcp artifact` 群：create/show/verify/lineage/status/
```

在 `docs/handover/HANDOVER.md`，把

```text
tests/            unit/<layer>、integration（真資料）、helpers.py
```

換成

```text
tests/            unit/<layer>、integration（真資料）、performance/、helpers.py
```

HANDOVER §6：拿掉已完成的兩項（改成一句說明），寫明第 3、5 項（原第 5、7 項）說的是 repo 內的開發副本，加上第三輪回報的一項。

在 `docs/handover/HANDOVER.md`，把

```markdown
1. **Hygiene C 已完成**：兩個 commit `6a4cc58`、`12a9cd4`；處置與驗證見 Plan 6 後記 §9。
2. **接續修復已完成**：遺失 `fuse.json` 時拒絕不完整紀錄，`--replace` 重建所有宣告子集（`7c31c3d`，Plan 4 §9）；eval / fuse / train / submit 的 26 個命令全部採用 `run_command(context=)`（Plan 7 §8）。歷史「未做」清單已逐層核對，處置在各後記最新節；不要依舊節再做一遍。
3. **效能回合**
```

換成

```markdown
已完成的不再列（Hygiene C、遺失 `fuse.json` 的接續修復、`run_command(context=)` 全面採用；處置在 Plan 4、6、7 的後記）。歷史「未做」清單已逐層核對，處置在各後記最新節；不要依舊節再做一遍。

1. **效能回合**
```

在 `docs/handover/HANDOVER.md`，把

```markdown
4. **設計層級**：
```

換成

```markdown
2. **設計層級**：
```

在 `docs/handover/HANDOVER.md`，把

```markdown
5. **RSNA Knee 已實跑本機基準**：
```

換成

```markdown
3. **repo 內的開發副本已實跑本機基準（2026-09-07；比賽本身在另一個工作區）**：
```

在 `docs/handover/HANDOVER.md`，把

```markdown
**尚未完成外部里程碑**：指定私有 Kaggle dataset 上傳待核准，notebook 執行 / 提交 / scored / sealed final 未發生；備份目的地未提供。
```

換成

```markdown
**這份副本沒有外部里程碑**：指定私有 Kaggle dataset 上傳待核准，notebook 執行 / 提交 / scored / sealed final 在這裡都未發生，備份目的地也未提供；比賽工作區後來的實際上傳、回讀與異機備份見稽核文件第 16、17 節（紀錄在那個工作區，不在本 repo）。
```

在 `docs/handover/HANDOVER.md`，把

```markdown
6. **Windows 命令解析**：
```

換成

```markdown
4. **Windows 命令解析**：
```

在 `docs/handover/HANDOVER.md`，把

```markdown
7. **本機證據已備份**：
```

換成

```markdown
5. **repo 內開發副本的本機證據已備份（同一份副本，2026-09-07）**：
```

在 `docs/handover/HANDOVER.md`，把

```markdown
勿把指著 C 槽的 remote_copy 當異機證據。
```

換成

```markdown
勿把指著 C 槽的 remote_copy 當異機證據。0.13.0 起，清單裡只在這台機器上的 `remote_copy` 會跟著 `backup push --tier 3` 送到異機目的地、在那裡驗（VCP-046）；推、驗之前仍不算異機證據。
```

在 `docs/handover/HANDOVER.md`，把

```markdown
8. **PostgreSQL Adaptive Provenance 外部驗收
```

換成

```markdown
6. **PostgreSQL Adaptive Provenance 外部驗收
```

在 `docs/handover/HANDOVER.md`，把

```markdown
9. **RSNA 第二輪回報（2026-09-25）剩下的**：
```

換成

```markdown
7. **第二輪回報（2026-09-25）剩下的**：
```

在 `docs/handover/HANDOVER.md`，把

```markdown
   - 處置表在稽核文件第 16 節。
```

換成

```markdown
   - 處置表在稽核文件第 16 節。
8. **第三輪回報（VCP-044～047）**：隨 0.13.0 處理（spec `docs/superpowers/specs/2026-10-04-vcp-round3-fixes-design.md`，處置表在稽核文件第 17 節）。刻意不在範圍、留給之後：`train status` 的 `backed=` 仍把本機上傳算成已備份；`backup pull` 還原後的完整性 WARN；索引路徑的覆寫（環境變數或選項，要加是 MINOR）。
```

CODEX_PROMPT：數字更正到 0.12.0 的實數（0.13.0 的數字在 Task 9 填），開放待辦依 spec §10.4 改成 0.13.0 → Wave 1c → 凍結前的契約項 → 1.0，PostgreSQL 的幾項排在後面。

在 `docs/handover/CODEX_PROMPT.md`，把

```markdown
版本 `0.8.1`（tag `v0.8.1`），全套測試綠（1612 passed / 76 skipped，覆蓋率 94.85%）。
```

換成

```markdown
版本 `0.12.0`（tag `v0.12.0`），全套測試綠（1924 passed / 76 skipped，覆蓋率 95.35%）。
```

在 `docs/handover/CODEX_PROMPT.md`，把

```markdown
1. **稽核 Wave 1c：程式碼快照與授權**（VCP-004 / VCP-006，`docs/audits/2026-09-11-vcp-improvement-audit.md`）：`train run` 從 0.11.0 起記下工作樹狀態與追蹤檔的 patch（VCP-041），但訓練程式的快照與授權邊界還沒有做成產物。這是 `1.0.0` 前最後一個 wave。
2. **PostgreSQL adaptive policy v2**（Plan 12 後記 §3-6、§5、§7）：selector 的信心帶改為相對預估差距或分層 RMSE；需重跑 calibration → six-method → held-out → real；v1 的 policy 與五份證據不改。
3. **full rebuild 後的 dead tuples**（§3-7）：是否在 rebuild 收尾 `VACUUM`，或只寫進操作指南。
4. **EXPLAIN 覆蓋 `status` / `impact` 查詢計畫**（§3-4）；**Linux host 的 live integration record**（§3-3）；**1M 規模**要 ≥128 GB 的機器。
5. 各層後記最後一節標「未做」的小項。
```

換成

```markdown
1. **0.13.0：第三輪回報 VCP-044～047 與文件同步**（spec `docs/superpowers/specs/2026-10-04-vcp-round3-fixes-design.md`，計畫 `docs/superpowers/plans/2026-10-04-vcp-round3-fixes.md`）：provenance 索引每個 checkout 一份並記下 root、備份清單的完整性、本機 `remote_copy` 跟著 tier 3 走、Kaggle CLI 失敗時回讀與列表逾時。
2. **稽核 Wave 1c：程式碼快照與產物授權**（VCP-004 / VCP-006，`docs/audits/2026-09-11-vcp-improvement-audit.md`）：`train run` 從 0.11.0 起記下工作樹狀態與追蹤檔的 patch（VCP-041），但訓練程式的快照與授權邊界還沒有做成產物。先寫 spec。
3. **凍結前的契約項**：各後記最後一節的待辦裡，會改變寫入內容、VERDICT 欄位、`reason=` 字彙或 exit 類別的那些（例如共用台帳後記 §3 的幾項），在 1.0 之前做完，或明寫留到 1.0 之後；1.0 之後再改就是 MAJOR。
4. **`1.0.0`**：留給稽核 Wave 1 全部落地之後，包括 1c（程式碼快照與產物授權，VCP-004／006）。稽核 §11 Wave 1 的第 4 項（單一大陣列的選取列存取器）與第 5 項（合成插件端到端、比賽原型遷移）排在 1.0 之後。
5. **PostgreSQL adaptive policy v2**（Plan 12 後記 §3-6、§5、§7）：selector 的信心帶改為相對預估差距或分層 RMSE；需重跑 calibration → six-method → held-out → real；v1 的 policy 與五份證據不改。
6. **full rebuild 後的 dead tuples**（§3-7）：是否在 rebuild 收尾 `VACUUM`，或只寫進操作指南。
7. **EXPLAIN 覆蓋 `status` / `impact` 查詢計畫**（§3-4）；**Linux host 的 live integration record**（§3-3）；**1M 規模**要 ≥128 GB 的機器。
8. 各層後記最後一節標「未做」的小項。
```

`DATASET_EVOLUTION_PROVENANCE_HANDOFF.md` 的 §8、§9 是合併前寫的，標成歷史：

在 `docs/handover/DATASET_EVOLUTION_PROVENANCE_HANDOFF.md`，把

```markdown
## 8. 下一個 agent 的接手清單
```

換成

```markdown
## 8. 下一個 agent 的接手清單（歷史：2026-09-15 合併前）

> 本節與 §9 是合併前寫的。功能已於 2026-09-15 以 PR 合併、tag `v0.7.0`（§1、§7）；之後的接手入口是 `docs/handover/HANDOVER.md` 與 `docs/handover/CODEX_PROMPT.md`，不要照本節與 §9 再整合一次。
```

在 `docs/handover/DATASET_EVOLUTION_PROVENANCE_HANDOFF.md`，把

```markdown
## 9. 可直接貼給下一個 agent 的提示
```

換成

```markdown
## 9. 可直接貼給下一個 agent 的提示（歷史：2026-09-15 合併前，不再適用）
```

- [ ] **Step 6: `CLAUDE.md` 與 `AGENTS.md` 對齊**

兩份目前有 6 處不同（Task 6 的四個修改兩邊都做了，不在這 6 處裡）。做法：把 `AGENTS.md` 多的內容補進 `CLAUDE.md`（比賽專屬的那一行寫成通用的 `projects/<contest>/`），加上 1.0 的那一句，再把 `CLAUDE.md` 整份複製成 `AGENTS.md`。之後兩份逐位元相同，「改一邊要改另一邊」就是複製一次。

在 `CLAUDE.md`，把

```markdown
命令永不互動提問；`--json` 時結果 JSON 到 stdout、VERDICT 到 stderr。
```

換成

```markdown
命令永不互動提問；`--json` 時結果 JSON 到 stdout、VERDICT 到 stderr。命令以 `run_command(context=)` 保留失敗時已知的識別欄位（dataset / run / recipe / id / root 等）；可選值未給時省略。
```

在 `CLAUDE.md`，把

```markdown
`uv run vcp fuse ablate --dataset D --recipe R --preregister --metric M`（每位成員
```

換成

```markdown
`uv run vcp fuse ablate --dataset D --recipe R --preregister --metric M [--replace]`（每位成員
```

在 `CLAUDE.md`，把

```markdown
PATCH = 其餘修正；`1.0.0` 留給稽核 Wave 1 落地。
```

換成

```markdown
PATCH = 其餘修正；`1.0.0` 留給稽核 Wave 1 全部落地之後，包括 1c（程式碼快照與產物授權，VCP-004／006），稽核 §11 Wave 1 的第 4 項（單一大陣列的選取列存取器）與第 5 項（合成插件端到端、比賽原型遷移）排在 1.0 之後。
```

在 `CLAUDE.md`，把

```markdown
## 文件
- PostgreSQL provenance 操作與安全邊界：
```

換成

```markdown
## 文件
- 比賽的實際基準流程：`projects/<contest>/RUNBOOK.md`（真實命令、讀數、外部待續條件）；設計與裁決：同目錄 `DESIGN.md`。Windows 訓練命令使用獨立 venv 的絕對 interpreter；checkpoint 綁定的前處理 / 模型檔不可在訓練後靜默改動。
- PostgreSQL provenance 操作與安全邊界：
```

在 `CLAUDE.md`，把

```markdown
的最後一節。`AGENTS.md` 是本檔給 Codex 的同步版本，改一邊要改另一邊。
```

換成

```markdown
的最後一節。`CLAUDE.md`（Claude Code 讀）與 `AGENTS.md`（Codex 讀）逐位元相同：改了一份就整份複製到另一份，`diff CLAUDE.md AGENTS.md` 不該有輸出。

## 給 Codex / 其他代理
- 先讀本檔與 `docs/handover/HANDOVER.md`，再讀要改的那一層的 spec 與後記；spec 的「補充決定」以程式碼為準。
- 一件事一個分支，一個 commit 一件事（`type(scope): 說明`，scope 可省，不加 co-author trailer），不用 `git add -A`；每個行為變更先寫失敗的測試；commit 前 `uv run ruff check . && uv run ruff format --check .`，合併前全套 `uv run pytest --cov=vcp`。
- 裁決（spec 沒說的決定）與處置寫進該層後記的最後一節；不對 markdown 跑 `ruff format`；測試重寫台帳 / 卡一律 `newline="\n"`。
```

然後讓 `AGENTS.md` 跟它一模一樣：

```bash
cp CLAUDE.md AGENTS.md
diff CLAUDE.md AGENTS.md
```

Expected：`diff` 沒有輸出。

- [ ] **Step 7: orientation 地圖的版本歷史與 build string**

在 `.claude/skills/vcp-orientation/map.md`，把

```markdown
產物寫 `vcp.core.build.build_string()`：`0.8.1`（發版 wheel）、`0.8.1+g<commit>`（checkout）、`…dirty`（有未提交變更）。`vcp version` 印同一字串。
```

換成

```markdown
產物寫 `vcp.core.build.build_string()`：`0.13.0`（發版 wheel）、`0.13.0+g<commit>`（checkout）、`…dirty`（有未提交變更）。`vcp version` 印同一字串。GitHub 上的歷史在 2026-10 改寫過一次：改寫前寫下的 `+g<舊 hash>` 用 `docs/reference/commit-map-2026-10.tsv` 對到新 hash。
```

在 `.claude/skills/vcp-orientation/map.md`，把

```markdown
、SQLite 索引保存 graph gaps（舊索引 rebuild 一次）。Wave 1c（程式碼快照與授權）尚未做。
```

換成

```markdown
、SQLite 索引保存 graph gaps（舊索引 rebuild 一次）→ 0.10.0 第二輪回報的五件（kernel 權重受檢、Kaggle 上傳回讀、配額不重算、多折同名 checkpoint、`VCP_ATTEMPT`）→ 0.11.0 run 的證據檔與標籤集（`--evidence` / `--labels`）、dirty 工作樹（`--require-clean`）→ 0.12.0 共用台帳（`ledger: shared`、`submit ledger adopt`、檔案鎖、上傳前同步）→ 0.13.0 provenance 索引每個 checkout 一份、備份清單的完整性與本機副本、Kaggle CLI 失敗時回讀（第三輪回報 VCP-044～047）。Wave 1c（程式碼快照與產物授權，VCP-004／006）尚未做；`1.0.0` 在 Wave 1 全部落地之後。
```

鏡射兩個改過的 skill 並檢查：

```bash
for s in vcp-orientation vcp-release-and-environments; do cp -r ".claude/skills/$s/." ".agents/skills/$s/"; done
diff -r .claude/skills .agents/skills
```

Expected：`diff` 沒有輸出。

- [ ] **Step 8: 2026-10 歷史改寫的 commit 對照表**

GitHub 上的歷史在 0.12.0 之後改寫過一次：拿掉 307 個 `Co-Authored-By` trailer，481 個 commit 的檔案樹、作者與時間都沒變，但 hash 全部改了。對照表以（檔案樹、作者時間、作者 email、標題）一對一配對。

這一步需要這台機器的 `refs/archive/pre-rewrite-2026-10/main`（舊歷史只在本機）。先確認：

```bash
git rev-parse --verify refs/archive/pre-rewrite-2026-10/main
```

Expected：印出 `561cc46df4b9bc4be8d25e83717bfac718fe6fa7`。印不出來就停下來回報，不要改用別的 ref。

用 Write 工具把下面的腳本存成 scratchpad（不是 repo）裡的 `commit_map.py`：

```python
"""Pair the pre-rewrite and rewritten histories commit by commit (dry run).

Only commit messages changed in the 2026-10 rewrite (Co-Authored-By trailers removed), so a
commit is identified by (tree, author time, author email, subject). Prints a summary and writes
the TSV to the path given as argv[3]."""

import subprocess
import sys

OLD, NEW, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
FMT = "%H%x1f%T%x1f%at%x1f%ae%x1f%s%x1f%ad"

def commits(rev: str) -> list[tuple[str, tuple[str, str, str, str], str, str]]:
    out = subprocess.run(
        ["git", "log", "--date=short", f"--format={FMT}", rev],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    rows = []
    for line in out.splitlines():
        sha, tree, at, ae, subject, day = line.split("\x1f")
        rows.append((sha, (tree, at, ae, subject), subject, day))
    return rows

old, new = commits(OLD), commits(NEW)
by_key = {}
for sha, key, _, _ in new:
    assert key not in by_key, f"duplicate key in new history: {key}"
    by_key[key] = sha
pairs = []
for sha, key, subject, day in old:
    assert key in by_key, f"no partner for {sha} {subject}"
    pairs.append((sha, by_key.pop(key), day, subject))
assert not by_key, f"{len(by_key)} new commits without a partner"
print(f"old={len(old)} new={len(new)} paired={len(pairs)} unpaired=0")
lines = ["old\tnew\tdate\tsubject"]
lines += [f"{o}\t{n}\t{d}\t{s}" for o, n, d, s in pairs]
with open(OUT, "wb") as fh:
    fh.write(("\n".join(lines) + "\n").encode("utf-8"))
print(f"wrote {OUT}")
```

在 repo 根目錄跑（新歷史釘在 tag `v0.12.0`，也就是改寫後的最後一個 commit；用 `origin/main` 的話，之後合併的 commit 沒有舊的對應，腳本會停）：

```bash
uv run python <scratchpad>/commit_map.py refs/archive/pre-rewrite-2026-10/main v0.12.0 docs/reference/commit-map-2026-10.tsv
head -n 2 docs/reference/commit-map-2026-10.tsv
wc -l docs/reference/commit-map-2026-10.tsv
```

Expected：
- 第一行印 `old=481 new=481 paired=481 unpaired=0`。
- 表頭是 `old`、`new`、`date`、`subject` 四欄（tab 分隔）；第一列的四欄是 `561cc46df4b9bc4be8d25e83717bfac718fe6fa7`、`d4e49e5d6799866973c50cd2492027f1e0c276dd`、`2026-09-28`、`Merge pull request #34 from eric20041027/chore/release-0.12.0`。
- `wc -l` 是 482。

腳本不進 repo；跑完可以刪掉。標題照 GitHub 上的 commit 原文（spec §10.7 要求每列含標題）。

在 `CHANGELOG.md`，把

```markdown
`vcp version` 印同一字串；`vcp.core.build.parse_build_string` 解析它。
- **發版步驟**
```

換成

```markdown
`vcp version` 印同一字串；`vcp.core.build.parse_build_string` 解析它。
- **2026-10 的歷史改寫**：GitHub 上的歷史在 0.12.0 之後改寫過一次，拿掉了 307 個 `Co-Authored-By` trailer；481 個 commit 的檔案樹、作者與時間都沒變，但 hash 全部改了。改寫前寫下的 build string（`<version>+g<舊 hash>`），以及本檔與其他文件引用的舊 hash，用 `docs/reference/commit-map-2026-10.tsv`（舊 hash、新 hash、日期、標題）對到新 hash；表只含 main 的歷史，從沒進 main 的分支 commit 不在裡面。`vcp version` 與產物格式都不變。
- **發版步驟**
```

在 `docs/handover/HANDOVER.md`，把

```markdown
- `--forget-remote` 要整份清單在目的地驗過才刪憑證；帶 `present=false` 條目的清單永遠不能 forget。
```

換成

```markdown
- `--forget-remote` 要整份清單在目的地驗過才刪憑證；帶 `present=false` 條目的清單永遠不能 forget。
- GitHub 上的歷史在 2026-10 改寫過一次（hash 全部換了，見 CHANGELOG 的版本規則）。本機的 `refs/archive/pre-rewrite-2026-10/` 保留了舊歷史，釘在舊 commit 上的 worktree 照常能用；舊 hash 對新 hash 查 `docs/reference/commit-map-2026-10.tsv`。
```

- [ ] **Step 9: 檢查**

```bash
git diff --check
diff CLAUDE.md AGENTS.md
diff -r .claude/skills .agents/skills
grep -rn "留給稽核 Wave 1 落地" CLAUDE.md AGENTS.md CHANGELOG.md .claude/skills .agents/skills
grep -c "1c（程式碼快照與產物授權，VCP-004／006）" CHANGELOG.md CLAUDE.md AGENTS.md README.zh-TW.md docs/handover/CODEX_PROMPT.md .claude/skills/vcp-release-and-environments/SKILL.md
grep -n "狀態：設計缺口；RSNA 已\|未進 main。\*\*\|codex/vcp-visual-guide\` 工作中" docs/audits/2026-09-11-vcp-improvement-audit.md
uv run pytest -o addopts="" -q tests/unit/test_skills_plugin.py tests/unit/test_package.py
```

Expected：`git diff --check`、兩個 `diff`、第一個與最後一個 `grep` 都沒有輸出；`grep -c` 每個檔都至少 1；測試 PASS。

- [ ] **Step 10: Commit**

```bash
git add docs/audits/2026-09-11-vcp-improvement-audit.md CHANGELOG.md README.md README.zh-TW.md docs/handover/HANDOVER.md docs/handover/CODEX_PROMPT.md docs/handover/DATASET_EVOLUTION_PROVENANCE_HANDOFF.md CLAUDE.md AGENTS.md .claude/skills/vcp-orientation/map.md .agents/skills/vcp-orientation/map.md .claude/skills/vcp-release-and-environments/SKILL.md .agents/skills/vcp-release-and-environments/SKILL.md docs/reference/commit-map-2026-10.tsv
git diff --cached --check
git commit -F <訊息檔>
```

`git diff --cached --check` 也檢查新加的 `.tsv`（Step 9 的 `git diff --check` 看不到還沒加進 index 的檔）；它沒有輸出才 commit。

訊息：`docs: 稽核狀態與第三輪回報、1.0 門檻一種說法、交接文件、CLAUDE.md 與 AGENTS.md 對齊、2026-10 commit 對照表`

---

### Task 8: 端到端測試與回歸 gate

**Files:**
- Modify: `tests/unit/test_e2e_backup.py`（import 與檔尾追加兩個端到端測試）
- Modify: `tests/unit/test_regression_gate.py`（`GATE` 多一列）

**Interfaces:**
- Consumes：Task 1–5 的測試名稱（gate 列只列名字）；Task 3 的 `FOLDS`、`register_folds`、`write_old_manifest`（`tests/backup_fixtures.py`）；`test_e2e_backup.py` 既有的 `FAKE_RCLONE`、`_backup`、`_verdict`、`world`。
- Produces：`test_an_old_incomplete_manifest_is_caught_and_replaced`、`test_a_local_copy_reaches_the_rclone_destination_before_the_credential_goes`；gate 列 `"VCP-044/045/046/047 (0.13.0)"`。

這兩個是 spec §9 列的端到端：VCP-045「重現 → 用新 id 重建 → push、verify OK」，VCP-046「`train upload --dest <本機>` → 建清單 → tier 3 push 到 fake rclone → verify、status verified → forget」。都走 CLI；rclone 是 `test_e2e_backup.py` 既有的假 rclone（真的子程序）。VCP-044 與 VCP-047 的端到端行為已經在 Task 1、2、5 的 CLI 測試裡（兩個 checkout 共用 data root、CLI 失敗的 VERDICT 與 redact）。

- [ ] **Step 1: 寫端到端測試** — `tests/unit/test_e2e_backup.py`

在 `tests/unit/test_e2e_backup.py`，把

```python
from backup_fixtures import SECRET, make_world
```

換成

```python
from backup_fixtures import FOLDS, SECRET, make_world, register_folds, write_old_manifest
```

在 `tests/unit/test_e2e_backup.py` 末尾追加：

```python


# --- 0.13.0: VCP-045 and VCP-046 end to end (spec 2026-10-04 §9) -----------------------------


def _fake_rclone(tmp_path, monkeypatch):
    script = tmp_path / "fake_rclone.py"
    script.write_text(FAKE_RCLONE, encoding="utf-8")
    store = tmp_path / "remote-store"
    conf = tmp_path / "rclone.conf"
    conf.write_text("[fake]\ntype = local\n", encoding="utf-8")
    monkeypatch.setattr(destmod, "RCLONE", [sys.executable, str(script)])
    monkeypatch.setenv("FAKE_RCLONE_STORE", str(store))
    monkeypatch.setenv("FAKE_RCLONE_CONF", str(conf))
    return store


def test_an_old_incomplete_manifest_is_caught_and_replaced(world, monkeypatch):
    """VCP-045: the five-fold manifest vcp 0.9.1 wrote lists one fold. verify, status and a
    tier-3 push all say so; a manifest written now, under a new id, pushes and verifies."""
    monkeypatch.setattr(destmod.shutil, "which", lambda name, *a, **k: None)  # never a real rclone
    register_folds(world)
    write_old_manifest(world, "beach", "run:good", "old-091", drop=set(FOLDS[:4]))
    r = _backup("verify", "--dataset", "beach", "--manifest", "old-091")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "reason=manifest_incomplete" in v and "incomplete=4" in v
    r = _backup("status", "--dataset", "beach")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "incomplete=1" in v
    vault = world.tmp / "vault"
    common = ["--dataset", "beach", "--dest", str(vault), "--tier", "3"]
    r = _backup("push", "--manifest", "old-091", *common)
    v = _verdict(r.output)
    assert r.exit_code == 1 and "manifest_incomplete:" in v and "incomplete=4" in v
    assert not vault.exists()
    r = _backup("manifest", "--dataset", "beach", "--conclusion", "run:good", "--id", "new")
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output), r.output
    r = _backup("push", "--manifest", "new", *common)
    assert r.exit_code == 0 and "failed=0" in _verdict(r.output), r.output
    folds = sorted(path.parent.name for path in vault.rglob("model.pt"))
    assert folds == [f"fold-{k}" for k in range(5)]
    r = _backup("verify", "--dataset", "beach", "--manifest", "new", "--dest", str(vault))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and "incomplete=0" in v, r.output
    r = _backup("status", "--dataset", "beach", "--json")
    by_id = {m["manifest_id"]: m for m in json.loads(r.stdout)["result"]["manifests"]}
    assert by_id["new"]["verified"] and by_id["old-091"]["completeness"] == "incomplete"


def test_a_local_copy_reaches_the_rclone_destination_before_the_credential_goes(
    world, tmp_path, monkeypatch
):
    """VCP-046: `train upload` to a local folder, a manifest, then a tier-3 push to an rclone
    remote: the weights are sent there and checked there, status says verified, and only then
    may the credential go."""
    store = _fake_rclone(tmp_path, monkeypatch)
    r = runner.invoke(app, ["train", "upload", "--run", "good", "--dest", str(world.tmp / "usb")])
    assert r.exit_code == 0, r.output
    r = _backup("manifest", "--dataset", "beach", "--conclusion", "run:good", "--id", "r1")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "local_copies=2" in v, r.output
    common = ["--dataset", "beach", "--manifest", "r1", "--dest", "fake:vault"]
    r = _backup("push", *common, "--tier", "3")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "local_copies=2" in v and "failed=0" in v, r.output
    weights = store / "fake" / "vault" / "data" / "work" / "good" / "weights"
    assert (weights / "best.pt").read_bytes() == b"best weights"
    assert (weights / "last.pt").read_bytes() == b"last weights"
    r = _backup("verify", *common)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and "local_copies=2" in v, r.output
    r = _backup("status", "--dataset", "beach", "--json")
    [m] = json.loads(r.stdout)["result"]["manifests"]
    assert m["verified"] and m["pushed_tiers"] == [1, 2, 3]
    r = _backup("push", *common, "--tier", "3", "--forget-remote")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "forgotten=fake" in v, r.output
    assert (store / "deleted-fake").is_file()
```

- [ ] **Step 2: 跑端到端測試**

Run: `uv run pytest -o addopts="" -q tests/unit/test_e2e_backup.py`
Expected: PASS。若失敗，那是 Task 3、4 之間真的整合問題：改程式、不改斷言，並在 task 回報裡寫明。

- [ ] **Step 3: 回歸 gate 的一列** — `tests/unit/test_regression_gate.py`

在 `tests/unit/test_regression_gate.py`，把

```python
            "tests/unit/test_e2e_shared_ledger.py": ["test_two_checkouts_share_one_ledger"],
        },
    ),
]
```

換成

```python
            "tests/unit/test_e2e_shared_ledger.py": ["test_two_checkouts_share_one_ledger"],
        },
    ),
    (
        "VCP-044/045/046/047 (0.13.0)",
        "provenance: an index serves one checkout -- another root's index is root_mismatch:, "
        "never prefix drift, and PostgreSQL sync never replaces another root's generation; "
        "backup: a manifest that lacks a file its runs registered is caught by verify, status "
        "and a tier-3 push, and a local remote_copy travels to the destination and is checked "
        "there before the credential may go; submit: a failed kaggle CLI is read back, and an "
        "upload the list shows is recorded once",
        {
            "tests/unit/provenance/test_index_roots.py": [
                "test_two_configs_roots_on_one_data_root_both_verify_ok",
                "test_another_roots_index_fails_root_mismatch_not_prefix_drift",
                "test_a_damaged_ledger_in_the_same_root_is_still_prefix_drift",
            ],
            "tests/unit/provenance/test_postgres_incremental.py": [
                "test_postgres_sync_never_replaces_another_roots_generation",
                "test_postgres_generation_without_roots_fails_closed_until_rebuilt",
            ],
            "tests/unit/backup/test_completeness.py": [
                "test_a_manifest_written_the_091_way_lists_one_fold_and_is_incomplete",
                "test_verify_reports_manifest_incomplete_with_its_count",
                "test_status_recomputes_so_an_old_passing_row_cannot_vouch",
                "test_a_tier_3_push_of_an_incomplete_manifest_fails_before_any_byte_moves",
            ],
            "tests/unit/backup/test_local_copies.py": [
                "test_a_tier_3_push_to_rclone_sends_a_local_copy_from_its_checkpoint",
                "test_rows_written_before_013_do_not_vouch_for_a_local_copy",
                "test_forget_remote_refuses_until_the_local_copy_is_at_the_destination",
            ],
            "tests/unit/submit/test_actions.py": [
                "test_a_failed_cli_whose_upload_the_list_shows_is_recorded_once",
                "test_a_failed_cli_the_list_does_not_show_fails_and_writes_nothing",
                "test_a_failed_cli_vcp_cannot_settle_fails_unconfirmed_and_writes_nothing",
            ],
            "tests/unit/test_e2e_backup.py": [
                "test_an_old_incomplete_manifest_is_caught_and_replaced",
                "test_a_local_copy_reaches_the_rclone_destination_before_the_credential_goes",
            ],
        },
    ),
]
```

- [ ] **Step 4: 跑 gate 與整套測試**

Run: `uv run pytest -o addopts="" -q tests/unit/test_regression_gate.py`
Expected: PASS。

Run: `uv run pytest --cov=vcp -o addopts="" -p no:cacheprovider -q`
Expected: 全部 PASS（需要真資料或 PostgreSQL 服務的整合測試照舊 skip），覆蓋率不低於 80%。

- [ ] **Step 5: Lint，然後 commit**

```bash
uv run ruff format tests/unit/test_e2e_backup.py tests/unit/test_regression_gate.py
uv run ruff check . && uv run ruff format --check .
git diff --check
git add tests/unit/test_e2e_backup.py tests/unit/test_regression_gate.py
git commit -F <訊息檔>
```

訊息：`test: VCP-045／046 的端到端測試與 0.13.0 的回歸 gate`

---

### Task 9: 發版 0.13.0（功能 PR 合併、使用者核可之後）

這個 task 只在功能 PR 合併進 `main`、而且使用者核可發版之後才做。照 `.claude/skills/vcp-release-and-environments/SKILL.md` 的發版四步，在從更新後的 `main` 開的發版分支上做，跟 0.12.0（PR #34）一樣。回歸 gate 的一列已經在 Task 8 隨功能 PR 進去，這裡不再加。

**Files:**
- Modify：`src/vcp/__init__.py`、`.claude/.claude-plugin/plugin.json`、`tests/unit/test_package.py`
- Modify：`CHANGELOG.md`
- Modify：`docs/handover/HANDOVER.md`、`docs/handover/CODEX_PROMPT.md`、`README.md`、`README.zh-TW.md`

**Interfaces:**
- Consumes：Task 7 寫下的文字（HANDOVER §6 第 8 項、CODEX_PROMPT 的版本行與開放待辦、兩份 README 的狀態行）當錨點；Task 8 的 gate 列。
- Produces：版本 `0.13.0`；tag `v0.13.0` 打在發版 PR 的合併 commit 上。

填入值（每個都有產生它的命令；計畫裡只有這些要填）：
- `<day>`：`uv run python -c "from vcp.core.time import stamp; print(stamp()[:10])"` 的輸出。
- `<feature PR>`：`gh pr list --state merged --head feat/vcp-044-047-round3 --json number --jq ".[0].number"` 的輸出。
- `P`、`S`、`C`：Step 4 全套測試的 `P passed`、`S skipped` 與總覆蓋率 `C%`。

- [ ] **Step 1: 從合併後的 main 開分支**

```bash
git fetch origin
git worktree add .claude/worktrees/vcp-release-0130 -b chore/release-0.13.0 origin/main
```

之後都在這個 worktree 裡做。

- [ ] **Step 2: 三處版本字串**（發版四步的第 1 步）

在 `src/vcp/__init__.py`，把

```python
__version__ = "0.12.0"
```

換成

```python
__version__ = "0.13.0"
```

在 `.claude/.claude-plugin/plugin.json`，把

```json
  "version": "0.12.0",
```

換成

```json
  "version": "0.13.0",
```

在 `tests/unit/test_package.py`，把

```python
EXPECTED_CANDIDATE_VERSION = "0.12.0"
```

換成

```python
EXPECTED_CANDIDATE_VERSION = "0.13.0"
```

- [ ] **Step 3: CHANGELOG 條目**（第 2 步）— 插在 `## [0.12.0] - 2026-09-29` 之上

升級步驟與相容性照抄 spec §8。

在 `CHANGELOG.md`，把

```markdown
## [0.12.0] - 2026-09-29
```

換成

```markdown
## [0.13.0] - <day>

第三輪回報的 VCP-044～047 與文件同步（#<feature PR>）；tag `v0.13.0` 打在發版 PR 的合併 commit 上。
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
  之前一律 FAIL。`impact`、`stale`、`explain`、`graph` 也解析 configs root。
- **VCP-045：備份清單的完整性。** run 紀錄在清單建立前登記的 checkpoint 與證據都要在清單裡（任何角色
  都算）。缺了 → `backup verify` FAIL `manifest_incomplete`（`incomplete=`）；`backup status` 每份
  清單都重算，WARN 且不算 verified；tier 3 push 在動任何檔案之前 FAIL；`--forget-remote` 拒絕；
  `backup manifest` 寫出前自檢，有缺口 ABORT。
- **VCP-046：本機的 `remote_copy` 跟著目的地走。** 不在 rclone、也不在目的地裡的副本，跟著
  `--tier 3` 推到目的地（從原檔送，原檔不在或不符就從本機副本送）、在目的地驗；0.13 以前的推送與
  驗證列不替它背書；`--forget-remote` 在它到目的地之前拒絕；`backup pull` 先從目的地拉，沒有再退回
  本機副本。
- **VCP-047：Kaggle CLI 失敗時回讀。** CLI 非 0 退出時在同一個上傳交易裡回讀（排除台帳已知的 ref，
  exit 0 的回讀也排除）：平台列出了 → 照寫一列、WARN `exit_code=`；沒列出 → FAIL `upload_failed:`；
  判斷不了 → FAIL `upload_unconfirmed:`。兩種 FAIL 都不寫列。Kaggle 的列表呼叫（上傳前同步、`sync`、
  回讀）各加 120 秒逾時；上傳本身不加。

### Changed
- 文件同步：稽核文件的狀態與第 17 節；1.0 門檻統一成一種說法；README 的穩定度說法；交接文件；
  `CLAUDE.md` 與 `AGENTS.md` 逐位元相同；orientation 地圖的版本歷史。新增
  `docs/reference/commit-map-2026-10.tsv`（2026-10 歷史改寫前後的 commit 對照，見「版本規則」）。

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
- 提交台帳（`submissions.jsonl`）的格式不變。

## [0.12.0] - 2026-09-29
```

- [ ] **Step 4: 重裝、跑全套測試**（第 3 步）

```bash
uv sync --frozen --reinstall-package vcp
uv run pytest --cov=vcp -o addopts="" -p no:cacheprovider
```

從輸出取 `P passed`、`S skipped` 與總覆蓋率 `C%`。全部 PASS、覆蓋率 ≥ 80% 才往下。

- [ ] **Step 5: 交接文件與 README 的版本、日期、數字**

在 `docs/handover/HANDOVER.md`，把

```markdown
# vcp 交接文件（2026-09-21 更新）
```

換成

```markdown
# vcp 交接文件（<day> 更新）
```

在 `docs/handover/HANDOVER.md`，把

```markdown
- 版本：`0.12.0`（tag
```

換成

```markdown
- 版本：`0.13.0`（tag `v0.13.0`，<day>，MINOR：第三輪回報的 VCP-044～047——provenance 索引每個 checkout 一份並記下 root（`root_mismatch:`）、備份清單的完整性（`manifest_incomplete`）、本機 `remote_copy` 跟著 tier 3 推到目的地（`local_copies=`）、Kaggle CLI 失敗時回讀（`upload_failed:` / `upload_unconfirmed:`）與列表逾時；文件同步；PR #<feature PR>）；`0.12.0`（tag
```

在 `docs/handover/HANDOVER.md`，把

```markdown
在 0.12.0 = 1924 passed / 76 skipped，覆蓋率 95.35%（0.11.0 是
```

換成

```markdown
在 0.13.0 = P passed / S skipped，覆蓋率 C%（0.12.0 是 1924 / 76 / 95.35%，0.11.0 是
```

在 `docs/handover/HANDOVER.md`，把

```markdown
8. **第三輪回報（VCP-044～047）**：隨 0.13.0 處理（spec
```

換成

```markdown
8. **第三輪回報（VCP-044～047）**：已隨 0.13.0 發出（PR #<feature PR>；spec
```

在 `docs/handover/CODEX_PROMPT.md`，把

```markdown
# 給 Codex 的接續開發 prompt（2026-09-21）
```

換成

```markdown
# 給 Codex 的接續開發 prompt（<day>）
```

在 `docs/handover/CODEX_PROMPT.md`，把

```markdown
版本 `0.12.0`（tag `v0.12.0`），全套測試綠（1924 passed / 76 skipped，覆蓋率 95.35%）。
```

換成

```markdown
版本 `0.13.0`（tag `v0.13.0`），全套測試綠（P passed / S skipped，覆蓋率 C%）。
```

在 `docs/handover/CODEX_PROMPT.md`，把

```markdown
（現在 95.35%，不要掉）
```

換成

```markdown
（現在 C%，不要掉）
```

0.13.0 發出之後，開放待辦的第 1 項就做完了：拿掉它，其餘往前補號。

在 `docs/handover/CODEX_PROMPT.md`，把

```markdown
1. **0.13.0：第三輪回報 VCP-044～047 與文件同步**（spec `docs/superpowers/specs/2026-10-04-vcp-round3-fixes-design.md`，計畫 `docs/superpowers/plans/2026-10-04-vcp-round3-fixes.md`）：provenance 索引每個 checkout 一份並記下 root、備份清單的完整性、本機 `remote_copy` 跟著 tier 3 走、Kaggle CLI 失敗時回讀與列表逾時。
2. **稽核 Wave 1c：程式碼快照與產物授權**
```

換成

```markdown
1. **稽核 Wave 1c：程式碼快照與產物授權**
```

在 `docs/handover/CODEX_PROMPT.md`，把

```markdown
3. **凍結前的契約項**：
```

換成

```markdown
2. **凍結前的契約項**：
```

在 `docs/handover/CODEX_PROMPT.md`，把

```markdown
4. **`1.0.0`**：
```

換成

```markdown
3. **`1.0.0`**：
```

在 `docs/handover/CODEX_PROMPT.md`，把

```markdown
5. **PostgreSQL adaptive policy v2**
```

換成

```markdown
4. **PostgreSQL adaptive policy v2**
```

在 `docs/handover/CODEX_PROMPT.md`，把

```markdown
6. **full rebuild 後的 dead tuples**
```

換成

```markdown
5. **full rebuild 後的 dead tuples**
```

在 `docs/handover/CODEX_PROMPT.md`，把

```markdown
7. **EXPLAIN 覆蓋
```

換成

```markdown
6. **EXPLAIN 覆蓋
```

在 `docs/handover/CODEX_PROMPT.md`，把

```markdown
8. 各層後記最後一節標「未做」的小項。
```

換成

```markdown
7. 各層後記最後一節標「未做」的小項。
```

兩份 README 各三處（版本徽章、測試徽章、狀態行）：

在 `README.md`，把

```markdown
[![Version 0.12.0](https://img.shields.io/badge/version-0.12.0-informational.svg)](CHANGELOG.md)
[![Tests 1924](https://img.shields.io/badge/tests-1924%20passed-success.svg)](CONTRIBUTING.md)
```

換成

```markdown
[![Version 0.13.0](https://img.shields.io/badge/version-0.13.0-informational.svg)](CHANGELOG.md)
[![Tests P](https://img.shields.io/badge/tests-P%20passed-success.svg)](CONTRIBUTING.md)
```

在 `README.md`，把

```markdown
`0.12.0`, pre-1.0:
```

換成

```markdown
`0.13.0`, pre-1.0:
```

在 `README.zh-TW.md`，把

```markdown
[![Version 0.12.0](https://img.shields.io/badge/version-0.12.0-informational.svg)](CHANGELOG.md)
[![Tests 1924](https://img.shields.io/badge/tests-1924%20passed-success.svg)](CONTRIBUTING.md)
```

換成

```markdown
[![Version 0.13.0](https://img.shields.io/badge/version-0.13.0-informational.svg)](CHANGELOG.md)
[![Tests P](https://img.shields.io/badge/tests-P%20passed-success.svg)](CONTRIBUTING.md)
```

在 `README.zh-TW.md`，把

```markdown
`0.12.0`，尚未到 1.0：
```

換成

```markdown
`0.13.0`，尚未到 1.0：
```

- [ ] **Step 6: 檢查、commit、開 PR**（第 4 步）

```bash
uv run ruff check . && uv run ruff format --check .
git diff --check
grep -rn "<day>\|<feature PR>" CHANGELOG.md docs/handover/HANDOVER.md docs/handover/CODEX_PROMPT.md
git add src/vcp/__init__.py .claude/.claude-plugin/plugin.json tests/unit/test_package.py CHANGELOG.md docs/handover/HANDOVER.md docs/handover/CODEX_PROMPT.md README.md README.zh-TW.md
git commit -m "chore(release): v0.13.0"
git push -u origin chore/release-0.13.0
gh pr create --base main --head chore/release-0.13.0 --title "chore(release): v0.13.0" --body-file <PR 描述檔>
```

Expected：`grep` 沒有輸出（填入值都換掉了；`P`、`S`、`C` 也要確認已換成數字）。PR 描述檔用 Write 工具寫在 scratchpad：摘要（MINOR 的理由與四個缺陷）、測試計畫（Step 4 的數字、CI 三個 check）。不加任何署名行。

- [ ] **Step 7: CI 綠、使用者核可合併之後** — 合併、在合併 commit 上打 tag、推 tag

```bash
gh pr merge <PR> --merge
git fetch origin
git tag -a v0.13.0 <合併 commit> -m "vcp 0.13.0"
git push origin v0.13.0
```

`<PR>` 是 Step 6 `gh pr create` 印出的編號；`<合併 commit>` 是 `git log -1 --format=%H origin/main` 在合併之後的輸出（先確認它的標題是 `Merge pull request #<PR> …`）。

---

## Self-review（寫計畫時做的）

**1. Spec 覆蓋**

| Spec 的節 | Task |
|---|---|
| §3.1 root id；`lock_path` 改用 `path_id`；不同寫法同一個 id | 1 |
| §3.2 SQLite 檔名、schema 3、四個 root 鍵、舊檔 `not_found:`（訊息指出舊檔）、schema 2 `mismatch:` | 1 |
| §3.3 PostgreSQL generation 記 root、DDL 不變、`sync` 不取代、`rebuild` WARN `replaced_root=`、沒記 root 的 generation fail closed | 2 |
| §3.4 八個命令先比 root、`prefix_drift:` 照舊、`impact` / `stale` / `explain` / `graph` 解析 configs root、共用台帳照常 | 1（SQLite 與 CLI）、2（PostgreSQL） |
| §4.1 `manifest_gaps` 的規則、`entry_key`、`newest_per_path` | 3 |
| §4.2 verify / status / tier 3 push / `--forget-remote` / manifest 自檢 | 3 |
| §4.3 verify 列的 `incomplete`、`local_ok` / `passed` | 3 |
| §5.1 `covers` | 4 |
| §5.2 push 的項目與來源、verify、status、`--forget-remote`、pull、manifest | 4 |
| §5.3 不新增判決字 | 4 |
| §6.1 失敗後回讀、排除已知 ref、`UploadResult.exit_code` | 5 |
| §6.2 `matched` / `not_listed` / 其他三種結果與訊息 | 5 |
| §6.3 列表呼叫 120 秒逾時 | 5 |
| §7 VERDICT 欄位與判決字 | 1、2、3、4、5（各自的 CLI 步驟） |
| §8 相容性與升級（寫進 CHANGELOG） | 9；寫入內容的改變在 1–4 |
| §9 測試 | 1–5（單元與 CLI）、8（VCP-045 / 046 端到端、回歸 gate 一列） |
| §10 文件同步第 1–7 項 | 7（標題日期與 0.13.0 的數字在 9） |
| §11 隨缺陷更新的文件與 skill | 6 |

**2. Dry run**

寫計畫的同時，把計畫機械地套在一份 scratch worktree 上（`git worktree add --detach <scratch> c951b9d`，`uv sync --frozen`）：一支腳本依標記（「新建」「整份改寫」「末尾追加」「在 … 把 … 換成」）抽出每個區塊、照字面套上，錨點必須剛好出現一次（「所有」的至少一次），否則停下。計畫裡的 bash 步驟（`cp -r` 鏡射、`cp CLAUDE.md AGENTS.md`、對照表腳本、`uv sync --frozen --reinstall-package vcp`）照寫的手動執行。結果：

- Task 1–9 共 330 個修改，每個錨點都對上（最後一版的計畫另外在一份 `git archive c951b9d` 的乾淨副本上再套一次，結果相同）。
- 各 task 的 RED → GREEN（套到前一個 task 為止、再只套這個 task 的測試修改 → 全部套上）：

  | Task | 先失敗 | 再通過 |
  |---|---|---|
  | 1 | `path_id` import 失敗（collection error） | `tests/unit/provenance`、`test_lock`、`test_transactions`：554 passed、5 skipped |
  | 2 | 6 failed、31 passed | `tests/unit/provenance`：551 passed、5 skipped |
  | 3 | collection error（`vcp.backup.completeness` 不存在） | backup、`test_e2e_backup`、`test_cli_backup`、train：232 passed |
  | 4 | collection error（`covers` 不存在） | 同上：246 passed |
  | 5 | collection error（`PlatformTimeout` 不存在） | submit、`test_cli_submit`、`test_e2e_submit`、`test_e2e_shared_ledger`、core：386 passed；逾時測試 0.52 秒 |
  | 6 | — | 鏡射後 `diff -r` 沒有輸出；`test_skills_plugin` + `test_postgres_docs` 9 passed |
  | 7 | — | 兩個 `diff` 沒有輸出、`grep` 如預期；對照表 482 行、481 對；`test_skills_plugin` + `test_package` 8 passed |
  | 8 | — | `test_e2e_backup` + 回歸 gate：23 passed |
  | 9 | — | 重裝後 `vcp version` 印 `0.13.0+g<commit>.dirty` |

- 全套（Task 1–9 都套上）：`uv run pytest --cov=vcp -o addopts="" -p no:cacheprovider -q` → 2003 passed、77 skipped，覆蓋率 95.33%（9 分 44 秒）。跑的時候把 `VCP_REALDATA_ROOT` 指到不存在的暫存目錄，所以真資料整合測試全部 skip（有真資料根的機器上，`test_artifact_status` 會以唯讀方式跑，skip 少一個）。0.12.0 的基準是 1924 passed / 76 skipped / 95.35%。之後只改了計畫裡 `CHANGELOG.md` 與 `HANDOVER.md` 的文字：最後一版重新套上之後，`.py` 的修改與新檔跟跑全套的那一版逐位元相同，另跑 `test_package`、`test_skills_plugin`、`test_regression_gate`、`test_postgres_docs`：32 passed。
- `uv run ruff check .`：All checks passed；`uv run ruff format --check .`：414 files already formatted；`git diff --check`（含新檔）乾淨；`uv run python examples/quickstart.py`：跑完，judge PASS。
- dry run 與複查讓計畫改過的地方：效能基準的 `_seed_index` 也要寫 root（否則 `test_adaptive_benchmark` 報 `root_mismatch:`）；Task 5 一行呼叫 ruff format 會併成一行；超過 100 字的測試行拆開；Windows 上開著 CLI log 的 data root 搬不動，所以「data root 換地方」的測試用複製；文字裡的 tab 改成文字說明；新的 `.tsv` 要 `git diff --cached --check` 才檢查得到；HANDOVER 的錨點避開本機路徑。

**3. Placeholder 掃描**

只有 Task 9 有填入值：`<day>`、`<feature PR>`、`P`、`S`、`C`、`<PR>`、`<合併 commit>`，每個都寫了產生它的命令。`<訊息檔>`、`<PR 描述檔>` 是執行者用 Write 工具寫的檔；Task 7 的 `<scratchpad>` 是執行者自己的暫存目錄。沒有待補的空白，也沒有「同 Task N」式的省略：每個 task 的程式碼都完整寫出。

**4. 型別與名稱一致**

- `path_id(path) -> str`（Task 1）：`lock_path`、`provenance_index_path`、`IndexRoots`、`cli_provenance._root` 都用它；Task 2 的測試也用。
- `IndexRoots.of(data_root, configs_root)`、`.metadata()`、`.matches(recorded)`、`check_roots(recorded, roots, *, remedy)`、`ROOT_KEYS`（Task 1）：Task 2 的 `_generation_roots`、`_check_generation_roots` 用同樣的鍵與函式。
- `make_backend(config, data_root, configs_root)`（Task 1）：Task 2 把 `roots=` 交給 `PostgresProvenanceBackend(config, *, roots=None)`；`RebuildResult.replaced_roots`（Task 2）由 `cli_provenance.rebuild` 讀。
- `newest_per_path`（Task 3）：`train.upload._targets`、`backup.verify._train_record`、`backup.evidence._checkpoints`（Task 3、4）都用它。`entry_key` / `locate_file` / `external_path`（Task 3）：`Collector.locate` 與 `completeness` 共用。
- `manifest_gaps(manifest, paths) -> list[Gap]`（Task 3）：verify、`status.gaps_of`、push 與 `build_manifest` 的自檢用它。`BackupRow.incomplete`（Task 3）、`BackupRow.local_copies`（Task 4）；`VerifyResult.incomplete` / `.local_copies`；`PushResult.local_copies`。
- `covers(dest, copy)`、`travels(entry, dest)`、`copy_path(copy)`（Task 4）：push、verify、status、pull 用它；Task 8 的端到端走 CLI。
- `UploadResult.exit_code`、`Platform.upload(..., *, known_refs)`、`actions._known_refs(ledger)`、`PlatformTimeout`、`timed_runner(seconds)`、`kaggle.LIST_TIMEOUT`（Task 5）：`manual` 平台、`sync`、`cli_submit` 用同樣的名字。
- Task 8 的 gate 列點名的測試都在 Task 1–5、8 裡定義；`test_regression_gate.py` 會在任一個改名或刪掉時變紅，dry run 時它通過。
