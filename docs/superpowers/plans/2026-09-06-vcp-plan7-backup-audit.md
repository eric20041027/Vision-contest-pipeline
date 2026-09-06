# vcp 備份審計層實作計畫（子專案 6）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 從結論反向產生證據清單，先小後大推到 rclone 遠端或本機目錄並逐檔驗證，事後在任何機器上驗副本、本機一致性與時戳，並能拉回重建。

**Architecture:** 新套件 `src/vcp/backup/`（schema、ledger、manifest、evidence 證據圖、dest 目的地抽象、push、verify、pull、status）加 `src/vcp/cli_backup.py`（`vcp backup` 五個命令）。子程序 runner 與 redact 搬到 `src/vcp/core/proc.py`，提交層與訓練層改 import 它。清單與台帳進 git（`configs/datasets/<name>/backup/`），目的地保留相對路徑（`<dest>/data|configs|external/...`）。

**Tech Stack:** Python 3.12、pydantic v2、typer 0.27、標準庫 `json` / `shutil` / `subprocess`、rclone（外部 CLI，經可注入 runner）、pytest、ruff（line-length 100）。

**Spec:** `docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`

## Global Constraints

- 取時只能用 `vcp.core.time.utc_now()` / `stamp()` / `parse_stamp()`（ruff TID251）。
- 每個 CLI 命令以 `VERDICT cmd=backup.<name> status=OK|WARN|FAIL|ABORT …` 收尾，exit 0 / 0 / 1 / 2；`--json` 時結果 JSON 到 stdout、VERDICT 到 stderr；永不互動提問；不用 Click 層的參數驗證（`min=` 之類）——所有範圍檢查在函式層，才有 VERDICT。
- 錯誤對應：`ValidationFailed` / `IntegrityError` / `PlatformError` = FAIL；`PlanMismatchError` / `RegistryError` / `VcpError` = ABORT。訊息以 `reason=` 字彙開頭（`not_found: …`、`exists: …`、`mismatch: …`、`conflict: …`、`forget_refused: …`、`rclone_not_found: …`）；`VcpError.fields` 只放機器可讀鍵。
- pydantic 模型 `extra="forbid"`；清單與台帳只含路徑、sha、大小、時間、dest 字串；vcp 不讀 rclone 設定檔內容；rclone 的 stdout / stderr 落地前一律 `redact`；子程序繼承環境但不記錄環境。
- 清單寫一次不改；台帳只 append（`exclude_none`）；push 已發生的動作一定記列（即使最後 FAIL）。
- 目的地佈局：`<dest>/data/<相對 data root>`、`<dest>/configs/<相對 configs root>`、`<dest>/external/<絕對路徑去磁碟與冒號>`；`remote_copy` 到 `<其 dest>/<run_id>/<name>`（訓練層佈局）。
- 檔案 utf-8、LF；`src/vcp` 不出現比賽名；覆蓋率 ≥ 80%；`uv run ruff check .` 與 `uv run ruff format --check .` 乾淨；測試永不碰真資料根（`roots` fixture）。
- commit 訊息結尾加一行 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`（用第二個 `-m`）。
- **不要對本計畫的 markdown 跑 `ruff format`**（Plan 6 R1：它會把獨立片段改壞）。

## 計畫層決定（spec 未明說之處；收尾時寫進 spec §14 補充決定）

1. **`FileEntry.source`**：`root=external` 的條目多一個 `source`（絕對 posix 路徑），否則 push / pull / verify 找不到本機檔。
2. **`RemoteCopy.run`**：`remote_copy` 記 `{dest, run, name}`，verify / pull 才能定位 `<dest>/<run_id>/<name>`。
3. **`checkpoint_final` 角色**：tier 3 分 `checkpoint_final`（排在前）與 `checkpoint`，實現「final 排最前」。
4. **`for` 是關鍵字**：欄位名 `for_`、alias `for`、`populate_by_name=True`；寫檔 `by_alias=True`。
5. **清單 JSON**：`json.dumps(..., ensure_ascii=False, indent=1)` + LF；讀回 `Manifest.model_validate_json`。
6. **`logs` 只在 `all`**（spec §7.2）；其他結論不帶 logs。
7. **時戳稽核跳過 `downloaded_at`**（人填的日期，不是 stamp）；掃描鍵 `ts` 與 `*_at`（遞迴）。
8. **push 先記列再拋錯**：驗證失敗或 `forget_refused` 都在 `push` 列寫完後才拋，`IntegrityError.fields` 帶 `pushed` / `skipped` / `verified` / `failed`。
9. **pull 不留錯位元組**：拉回後 sha 不符就刪掉該檔再拋 `IntegrityError`。
10. **`status` 的 rclone 設定檔檢查**：只在 `shutil.which("rclone")` 找得到時跑 `rclone config file`，取最後一行非空白為路徑做 `is_file()`；否則 `rclone_conf=unknown`。
11. **`hashsum` 解析沿用訓練層**：`<sha>  <相對路徑>`，列不出來（非 0）視為空目錄。
12. **`--dataset` 與結論一致**：`run:<id>` 的 run 必須屬於 `--dataset`（否則 FAIL）；`submission:` 的 `--dataset` 是 test dataset；`judgement:` 的預登記在 `--dataset` 下。
13. **`--forget-remote` 配本機 dest 在推送前就拒絕**（`forget_refused`，不推、不記列）：多半是 dest 打錯。
14. **push 前逐檔預檢，任何位元組移動前**：清單說 present 但本機沒有 → `not_found:`；sha 變了（含台帳在清單之後長大）→ `drift:`，都提示「重寫清單」。清單是快照，push 只推快照描述的位元組。
15. **verify 回傳結果、不拋錯**：CLI 依結果定 FAIL 並帶 `reason=`（優先序 `mismatch` > `missing` > `drift` > `bad_stamps`），`--json` 才列得出 drift 明細；spec §8 的錯誤類別對應到 `reason=` 字彙。
16. **verify 順帶檢查 `backup.log.jsonl` 自己的時戳**（它不進清單）。
17. **一致性層對台帳角色用「前綴 sha」**：現在的檔前 `bytes` 個位元組的 sha 等於清單 sha → 只增長、不算 drift；被改或截短才算。
18. **rclone 命令前綴是模組常數 `vcp.backup.dest.RCLONE`**：端到端測試把它指到假 rclone 腳本；訓練層的 `upload.py` 不動。
19. **pull 也「先記列再拋錯」**，優先序 `mismatch` > `missing` > `conflict`；`.bak-<UTC 時戳含微秒>`；目的地的 sha 先比對，不符就不拉、拉回不符就刪。
20. **status 的定義**：「已推 tier」= 成功（`failed` 空）push 的最大 `--tier` 以下全部；「verify 通過」= 任一 verify 列三層皆無問題；rclone 不在 → `rclone_conf=unknown`。

## 檔案結構

| 檔案 | 責任 |
|---|---|
| `src/vcp/core/proc.py` | `Runner`、`default_runner`、`redact`、`last_line`（從 `submit/platforms/base.py` 搬來 + 一個新 helper） |
| `src/vcp/submit/platforms/base.py`、`src/vcp/train/upload.py`、`src/vcp/core/errors.py` | 改 import；rclone 錯誤訊息 redact；`PlatformError` docstring |
| `src/vcp/core/paths.py` | `DatasetPaths.backup_dir` / `backup_log` / `backup_manifest(id)` |
| `src/vcp/backup/__init__.py` | 套件 docstring |
| `src/vcp/backup/schema.py` | `ROLES`、`TIER_OF`、`LEDGER_ROLES`、`CARD_ROLES`、`RemoteCopy`、`FileEntry`、`Manifest`、`BackupRow` |
| `src/vcp/backup/ledger.py` | `BackupLedger` |
| `src/vcp/backup/manifest.py` | `default_manifest_id`、`write_manifest`、`load_manifest`、`local_path` |
| `src/vcp/backup/evidence.py` | `parse_conclusion`、`external_path`、`Collector`（`walk_run` / `walk_judgement` / `walk_submission` / `walk_all`）、`build_manifest` |
| `src/vcp/backup/dest.py` | `Destination` protocol、`LocalDest`、`RcloneDest`、`open_dest` |
| `src/vcp/backup/push.py` | `push` |
| `src/vcp/backup/verify.py` | `verify`（副本、一致性、時戳） |
| `src/vcp/backup/pull.py` | `pull` |
| `src/vcp/backup/status.py` | `status` |
| `src/vcp/cli_backup.py` | `backup_app`：manifest / push / verify / pull / status |
| `src/vcp/cli.py` | `app.add_typer(backup_app, name="backup")` |
| `tests/backup_fixtures.py` | `make_world`、`make_fusion`、`SECRET`、`FakeRemote` |
| `tests/unit/core/test_proc.py`、`tests/unit/backup/test_*.py`、`tests/unit/test_cli_backup.py`、`tests/unit/test_e2e_backup.py`、`tests/integration/test_rsna_knee_backup.py` | 測試 |
| `README.md`、`CLAUDE.md` | 文件 |

## 給實作者的共用約定

- 測試用 `roots` fixture、`tests/submit_fixtures.py`（`make_pair`、`seed_eval_runs`、`seed_judgements`、`seed_test_runs`、`EVAL`、`TEST`、`STAMP`）與本計畫新增的 `tests/backup_fixtures.py`；CLI 測試用 `typer.testing.CliRunner` 對 `vcp.cli.app`，VERDICT 從 `r.output` 取最後一行 `VERDICT `。
- 寫完程式碼先 `uv run ruff format <檔案>` 再跑測試；pytest 用 `uv run pytest <路徑> -o addopts="" -q`；每個任務結尾 `uv run ruff check . && uv run ruff format --check .` 乾淨才 commit。
- `ruff` 的 isort 把 `helpers` / `submit_fixtures` / `backup_fixtures` 當第一方，接受它排出來的順序。
- 種判決與量測 holdout 的測試各要幾秒（真 bootstrap）。

---

### Task 1: `vcp/core/proc.py` 搬家與 rclone 錯誤訊息 redact

**Files:**
- Create: `src/vcp/core/proc.py`
- Modify: `src/vcp/submit/platforms/base.py:1-30`、`src/vcp/train/upload.py:1-45,120-135`、`src/vcp/core/errors.py`（`PlatformError` docstring）
- Test: `tests/unit/core/test_proc.py`、`tests/unit/train/test_upload.py`（追加一個測試）

**Interfaces:**
- Consumes: 既有的 `redact` 正則與 `default_runner`（`submit/platforms/base.py`）、`train/upload.py` 的 `Runner` / `default_runner` / `_upload_rclone`。
- Produces: `vcp.core.proc.Runner`（`Callable[[list[str]], subprocess.CompletedProcess[str]]`）、`default_runner(args)`、`redact(text) -> str`、`last_line(text) -> str`（最後一行非空白、已 redact；空輸出 → `""`）。`vcp.submit.platforms.base` 與 `vcp.submit.platforms` 仍匯出 `Runner` / `default_runner` / `redact`（測試與 kaggle.py 不改）。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/core/test_proc.py`

```python
import sys

from vcp.core.proc import default_runner, last_line, redact

SECRET = "fakesecretfakesecretfakesecret1234"


def test_redact_rules():
    assert redact(f"KAGGLE_KEY={SECRET} done") == "KAGGLE_KEY=<redacted>"
    assert redact("Authorization: Bearer abc.def") == "Authorization=<redacted>"
    assert redact("token: xyz") == "token=<redacted>"
    assert redact(f"echo {SECRET}") == "echo <redacted>"
    assert redact("short id S1 ok") == "short id S1 ok"


def test_last_line_is_redacted_and_handles_empty():
    assert last_line(f"first\n\n401 denied key={SECRET}\n \n") == "401 denied key=<redacted>"
    assert last_line("") == "" and last_line(" \n\n") == ""


def test_default_runner_runs_a_subprocess():
    proc = default_runner([sys.executable, "-c", "print('hi')"])
    assert proc.returncode == 0 and proc.stdout.strip() == "hi"
```

追加到 `tests/unit/train/test_upload.py` 檔尾：

```python
def test_rclone_failure_message_is_redacted(roots):
    rec, _ = _registered(roots)
    secret = "fakesecretfakesecretfakesecret1234"

    def runner(args):
        if args[1] == "hashsum":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=f"copy failed key={secret}")

    with pytest.raises(VcpError, match="copyto failed") as ei:
        upload(rec, "gdrive:w", data_root=roots.data, runner=runner)
    assert secret not in str(ei.value) and "<redacted>" in str(ei.value)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/core/test_proc.py tests/unit/train/test_upload.py::test_rclone_failure_message_is_redacted -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: vcp.core.proc`；redact 測試的 secret 出現在訊息裡）

- [ ] **Step 3: 寫 `src/vcp/core/proc.py`**

```python
"""Subprocess plumbing shared by every layer that shells out (rclone, kaggle): an injectable
runner that inherits the environment and records none of it, and the redaction every byte of a
third-party CLI's output passes through before it can reach a message, a VERDICT, a log or a
ledger."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

_KV = re.compile(r"(?im)(key|token|secret|password|authorization)\s*[=:]\s*.+$")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_LONG = re.compile(r"[A-Za-z0-9+/_-]{32,}")


def default_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Inherits the environment (the CLI needs its credentials) and records none of it."""
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def redact(text: str) -> str:
    text = _KV.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = _BEARER.sub("bearer <redacted>", text)
    return _LONG.sub("<redacted>", text)


def last_line(text: str) -> str:
    """The last non-empty line of a CLI's output, redacted: what an error message may quote."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return redact(lines[-1]) if lines else ""
```

- [ ] **Step 4: 改三個既有檔**

`src/vcp/submit/platforms/base.py`：刪掉 `import re`、`import subprocess`、`from collections.abc import Callable`、`Runner = …`、三個正則、`default_runner`、`redact` 的定義；在 `from vcp.core.errors import RegistryError` 之後加 `from vcp.core.proc import Runner, default_runner, redact`，並在 import 區之後加

```python
__all__ = [
    "PLATFORMS",
    "Platform",
    "PlatformSubmission",
    "Runner",
    "UploadResult",
    "default_runner",
    "get_platform",
    "redact",
    "register_platform",
]
```

（`__all__` 讓 ruff 知道 `default_runner` / `redact` 是刻意匯出的。）`kaggle.py`、`manual.py`、`platforms/__init__.py`、`actions.py`、`sync.py` 不改。

`src/vcp/train/upload.py`：刪掉 `import subprocess`、`from collections.abc import Callable`、`Runner = Callable[...]` 與 `default_runner` 的定義；加 `from vcp.core.proc import Runner, default_runner, last_line`；`_upload_rclone` 裡的錯誤改為

```python
        if proc.returncode != 0:
            raise VcpError(
                f"rclone copyto failed (exit {proc.returncode}) for {name}: "
                f"{last_line(proc.stderr or proc.stdout)}"
            )
```

`shutil`、`re` 仍有用，保留。

`src/vcp/core/errors.py` 的 `PlatformError` docstring 改為 `"""An external tool's CLI (kaggle, rclone) ran and failed. The message is already redacted."""`。

- [ ] **Step 5: 跑受影響的測試、ruff、commit**

Run: `uv run pytest tests/unit/core tests/unit/train tests/unit/submit/test_platforms.py tests/unit/submit/test_actions.py tests/unit/submit/test_sync.py -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/core/proc.py src/vcp/core/errors.py src/vcp/submit/platforms/base.py src/vcp/train/upload.py tests/unit/core/test_proc.py tests/unit/train/test_upload.py
git commit -m "refactor(core): 子程序 runner 與 redact 收進 vcp/core/proc.py；訓練層 rclone 錯誤訊息也 redact" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: schema、台帳、清單檔、路徑

**Files:**
- Create: `src/vcp/backup/__init__.py`、`src/vcp/backup/schema.py`、`src/vcp/backup/ledger.py`、`src/vcp/backup/manifest.py`
- Modify: `src/vcp/core/paths.py`（`DatasetPaths` 在 `submission_dir` 之後加三個成員）
- Test: `tests/unit/backup/__init__.py`（空）、`tests/unit/backup/test_schema.py`、`tests/unit/backup/test_ledger_manifest.py`

**Interfaces:**
- Consumes: `vcp.measure.ledger.read_rows`、`vcp.core.time.utc_now` / `stamp`、`vcp.core.paths.validate_name`。
- Produces: `vcp.backup.schema`：`ROLES`（tuple，順序即清單順序）、`TIER_OF`、`LEDGER_ROLES`、`CARD_ROLES`、`RemoteCopy(dest, run, name)`、`FileEntry(root, path, sha256, bytes, role, tier, kind="file", present=True, remote=None, source=None, for_=[...] alias "for")` + `.key`、`Manifest(manifest_id, dataset, conclusion, created_at, vcp_version, data_root, files)` + `.bytes_by_tier()`、`BackupRow`（事件 `manifest` / `push` / `verify` / `pull` / `remote_forgotten`）；`vcp.backup.ledger.BackupLedger(path)` 的 `rows` / `append` / `of(event, manifest_id=None)` / `latest(event, manifest_id)` / `manifest_ids()`；`vcp.backup.manifest.default_manifest_id(conclusion)`、`write_manifest(paths, manifest) -> Path`、`load_manifest(paths, manifest_id) -> Manifest`、`local_path(entry, data_root, configs_root) -> Path`；`DatasetPaths.backup_dir` / `backup_log` / `backup_manifest(id)`。

- [ ] **Step 1: 路徑**

`src/vcp/core/paths.py` 的 `DatasetPaths` 在 `submission_dir` 之後、`resolve_image_root` 之前加：

```python
    @property
    def backup_dir(self) -> Path:
        return self.config_dir / "backup"

    @property
    def backup_log(self) -> Path:
        return self.config_dir / "backup.log.jsonl"

    def backup_manifest(self, manifest_id: str) -> Path:
        validate_name(manifest_id)
        return self.backup_dir / f"{manifest_id}.json"
```

- [ ] **Step 2: 寫失敗的測試** `tests/unit/backup/test_schema.py`

```python
import pytest
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.backup.schema import (
    CARD_ROLES,
    LEDGER_ROLES,
    ROLES,
    TIER_OF,
    BackupRow,
    FileEntry,
    Manifest,
    RemoteCopy,
)

STAMP = "2026-09-06T00:00:00.000Z"


def _entry(**over) -> FileEntry:
    base = {
        "root": "data",
        "path": "runs/r1/run.yaml",
        "sha256": "a" * 64,
        "bytes": 10,
        "role": "run_card",
        "tier": 1,
        "for": ["run:r1"],
    }
    return FileEntry.model_validate({**base, **over})


def test_roles_and_tiers():
    assert ROLES[0] == "submit_profile" and ROLES[-1] == "checkpoint"
    assert TIER_OF["run_card"] == 1 and TIER_OF["prediction"] == 2
    assert TIER_OF["checkpoint_final"] == 3 and TIER_OF["checkpoint"] == 3
    assert ROLES.index("checkpoint_final") < ROLES.index("checkpoint")
    assert "readings" in LEDGER_ROLES and "logs" in LEDGER_ROLES
    assert "run_card" in CARD_ROLES and "anchors" in CARD_ROLES
    assert set(TIER_OF) == set(ROLES)


def test_entry_alias_and_key():
    e = _entry()
    assert e.for_ == ["run:r1"] and e.key == "data/runs/r1/run.yaml"
    assert FileEntry(root="data", path="x", sha256="b" * 64, bytes=1, role="logs", tier=2, for_=["all"]).for_ == ["all"]
    dumped = e.model_dump(mode="json", by_alias=True)
    assert dumped["for"] == ["run:r1"] and "for_" not in dumped


@pytest.mark.parametrize(
    "bad",
    [
        {"role": "photo"},
        {"tier": 2},
        {"kind": "remote_copy"},
        {"remote": {"dest": "d", "run": "r1", "name": "best.pt"}},
        {"root": "external"},
        {"source": "C:/x"},
        {"for": []},
        {"extra": 1},
    ],
)
def test_entry_rejects(bad):
    with pytest.raises(ValidationError):
        _entry(**bad)


def test_remote_copy_and_external_entries():
    r = _entry(
        role="checkpoint",
        tier=3,
        kind="remote_copy",
        remote={"dest": "gdrive:w", "run": "r1", "name": "best.pt"},
        path="work/best.pt",
    )
    assert r.remote == RemoteCopy(dest="gdrive:w", run="r1", name="best.pt")
    x = _entry(root="external", path="D/weights/best.pt", source="D:/weights/best.pt", role="checkpoint", tier=3)
    assert x.key == "external/D/weights/best.pt"


def test_manifest_uniqueness_and_bytes():
    m = Manifest(
        manifest_id="run-r1-20260906T000000Z",
        dataset="d",
        conclusion="run:r1",
        created_at=STAMP,
        vcp_version="0",
        data_root="C:/vcp-data",
        files=[_entry(), _entry(path="runs/r1/predictions/valA.jsonl", role="prediction", tier=2, bytes=5)],
    )
    assert m.bytes_by_tier() == {"1": 10, "2": 5, "3": 0}
    with pytest.raises(ValidationError, match="duplicate"):
        Manifest(**{**m.model_dump(by_alias=True), "files": [_entry(), _entry()]})


def test_backup_rows_require_event_fields():
    BackupRow(
        event="manifest",
        ts=STAMP,
        manifest_id="m",
        conclusion="all",
        files=3,
        bytes_by_tier={"1": 1, "2": 0, "3": 0},
        missing=0,
        remote_copies=0,
    )
    with pytest.raises(ValidationError, match="push needs"):
        BackupRow(event="push", ts=STAMP, manifest_id="m")
    BackupRow(event="verify", ts=STAMP, manifest_id="m", drift=0, bad_stamps=0)
    BackupRow(event="remote_forgotten", ts=STAMP, manifest_id="m", remote="gdrive")
    with pytest.raises(ValidationError):
        BackupRow(event="party", ts=STAMP)


def test_paths(roots):
    paths = DatasetPaths.resolve("t", data_root=roots.data, configs_root=roots.configs)
    assert paths.backup_dir == roots.configs / "datasets" / "t" / "backup"
    assert paths.backup_log == roots.configs / "datasets" / "t" / "backup.log.jsonl"
    assert paths.backup_manifest("m1") == paths.backup_dir / "m1.json"
    with pytest.raises(ValidationFailed):
        paths.backup_manifest("bad id")
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/unit/backup/test_schema.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: vcp.backup`）

- [ ] **Step 4: 寫 `src/vcp/backup/__init__.py` 與 `src/vcp/backup/schema.py`**

`__init__.py`：

```python
"""Backup and audit (spec 6): evidence manifests generated from conclusions, verified copies,
and a three-layer audit (copies, local consistency, timestamps)."""
```

`schema.py`：

```python
"""Pydantic models of the backup layer (spec 4): the evidence manifest and the backup ledger."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Root = Literal["data", "configs", "external"]
Kind = Literal["file", "remote_copy"]
Event = Literal["manifest", "push", "verify", "pull", "remote_forgotten"]

# Manifest order: tier 1 decision layer, tier 2 reproduction layer, tier 3 weights. `push --tier 1`
# therefore sends the small decisive files first (spec 2, 7.1).
ROLES: tuple[str, ...] = (
    "submit_profile",
    "submissions_log",
    "stage",
    "artifact",
    "judgements",
    "readings",
    "sigma",
    "anchors",
    "anchors_log",
    "prereg",
    "prereg_log",
    "recipe",
    "run_card",
    "history",
    "fuse_record",
    "train_record",
    "train_log",
    "dataset_card",
    "plan",
    "unseal_log",
    "prediction",
    "samples",
    "raw_manifest",
    "train_dir",
    "logs",
    "checkpoint_final",
    "checkpoint",
)
_TIER2 = ("prediction", "samples", "raw_manifest", "train_dir", "logs")
TIER_OF: dict[str, int] = {
    role: 3 if role.startswith("checkpoint") else (2 if role in _TIER2 else 1) for role in ROLES
}
# Append-only jsonl files whose every row carries a `ts` (spec 6.2 layer 3).
LEDGER_ROLES = frozenset(
    {
        "submissions_log",
        "readings",
        "judgements",
        "sigma",
        "anchors_log",
        "prereg_log",
        "train_log",
        "history",
        "unseal_log",
        "logs",
    }
)
# yaml / json documents whose `created_at` / `*_at` / `ts` values must parse as UTC stamps.
CARD_ROLES = frozenset(
    {
        "submit_profile",
        "stage",
        "prereg",
        "recipe",
        "run_card",
        "fuse_record",
        "train_record",
        "dataset_card",
        "plan",
        "anchors",
    }
)
_REQUIRED: dict[str, tuple[str, ...]] = {
    "manifest": ("manifest_id", "conclusion", "files", "bytes_by_tier", "missing", "remote_copies"),
    "push": ("manifest_id", "dest", "tier", "pushed", "skipped", "verified", "failed", "bytes"),
    "verify": ("manifest_id", "drift", "bad_stamps"),
    "pull": ("manifest_id", "dest", "tier", "pulled", "skipped", "conflicts"),
    "remote_forgotten": ("manifest_id", "remote"),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RemoteCopy(_Strict):
    """A checkpoint copy `vcp train upload` already verified: at `<dest>/<run>/<name>`."""

    dest: str
    run: str
    name: str


class FileEntry(_Strict):
    """One file the conclusion rests on (spec 4.1)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    root: Root
    path: str
    sha256: str
    bytes: int = Field(ge=0)
    role: str
    tier: int = Field(ge=1, le=3)
    kind: Kind = "file"
    present: bool = True
    remote: RemoteCopy | None = None
    source: str | None = None
    for_: list[str] = Field(default_factory=list, alias="for")

    @model_validator(mode="after")
    def _shape(self) -> FileEntry:
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}")
        if self.tier != TIER_OF[self.role]:
            raise ValueError(f"role {self.role!r} is tier {TIER_OF[self.role]}, got {self.tier}")
        if (self.kind == "remote_copy") != (self.remote is not None):
            raise ValueError("kind=remote_copy needs remote, and only then")
        if (self.root == "external") != (self.source is not None):
            raise ValueError("root=external needs source, and only then")
        if not self.for_:
            raise ValueError("an entry must serve at least one conclusion")
        return self

    @property
    def key(self) -> str:
        return f"{self.root}/{self.path}"


class Manifest(_Strict):
    """``configs/datasets/<name>/backup/<manifest_id>.json``: written once, never edited."""

    manifest_id: str
    dataset: str
    conclusion: str
    created_at: str
    vcp_version: str
    data_root: str
    files: list[FileEntry]

    @model_validator(mode="after")
    def _unique(self) -> Manifest:
        keys = [f.key for f in self.files]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate entries in manifest")
        return self

    def bytes_by_tier(self) -> dict[str, int]:
        out = {"1": 0, "2": 0, "3": 0}
        for f in self.files:
            out[str(f.tier)] += f.bytes
        return out


class BackupRow(_Strict):
    """One line of ``backup.log.jsonl`` (spec 4.2); each event must carry its own fields."""

    event: Event
    ts: str
    manifest_id: str | None = None
    conclusion: str | None = None
    files: int | None = None
    bytes_by_tier: dict[str, int] | None = None
    missing: int | None = None
    remote_copies: int | None = None
    dest: str | None = None
    tier: int | None = None
    pushed: int | None = None
    skipped: int | None = None
    verified: int | None = None
    failed: list[str] | None = None
    bytes: int | None = None
    copies: dict[str, int] | None = None
    drift: int | None = None
    bad_stamps: int | None = None
    first_bad: str | None = None
    pulled: int | None = None
    conflicts: list[str] | None = None
    remote: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> BackupRow:
        missing = [f for f in _REQUIRED[self.event] if getattr(self, f) is None]
        if missing:
            raise ValueError(f"{self.event} needs {missing}")
        return self
```

- [ ] **Step 5: 跑 schema 測試確認通過**

Run: `uv run pytest tests/unit/backup/test_schema.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 6: 寫失敗的測試** `tests/unit/backup/test_ledger_manifest.py`

```python
import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import default_manifest_id, load_manifest, local_path, write_manifest
from vcp.backup.schema import BackupRow, FileEntry, Manifest

T0 = "2026-09-06T00:00:00.000Z"
T1 = "2026-09-06T01:00:00.000Z"


def _manifest(mid="run-r1-20260906T000000Z") -> Manifest:
    return Manifest(
        manifest_id=mid,
        dataset="d",
        conclusion="run:r1",
        created_at=T0,
        vcp_version="0",
        data_root="C:/vcp-data",
        files=[
            FileEntry(root="data", path="runs/r1/run.yaml", sha256="a" * 64, bytes=3, role="run_card", tier=1, for_=["run:r1"]),
            FileEntry(root="configs", path="datasets/d/dataset.yaml", sha256="b" * 64, bytes=4, role="dataset_card", tier=1, for_=["run:r1"]),
            FileEntry(root="external", path="D/w/best.pt", source="D:/w/best.pt", sha256="c" * 64, bytes=5, role="checkpoint", tier=3, for_=["run:r1"]),
        ],
    )


def test_ledger_round_trip_and_latest(tmp_path):
    led = BackupLedger(tmp_path / "backup.log.jsonl")
    assert led.rows == [] and led.manifest_ids() == []
    led.append(BackupRow(event="manifest", ts=T0, manifest_id="m1", conclusion="all", files=1, bytes_by_tier={"1": 1, "2": 0, "3": 0}, missing=0, remote_copies=0))
    led.append(BackupRow(event="push", ts=T0, manifest_id="m1", dest="v", tier=1, pushed=1, skipped=0, verified=1, failed=[], bytes=1))
    led.append(BackupRow(event="push", ts=T1, manifest_id="m1", dest="v", tier=2, pushed=0, skipped=1, verified=1, failed=[], bytes=0))
    text = (tmp_path / "backup.log.jsonl").read_text(encoding="utf-8")
    assert "null" not in text and text.count("\n") == 3
    again = BackupLedger(tmp_path / "backup.log.jsonl")
    assert again.rows == led.rows and again.manifest_ids() == ["m1"]
    assert again.latest("push", "m1").tier == 2 and again.latest("verify", "m1") is None
    assert len(again.of("push")) == 2 and again.of("push", "m9") == []


def test_manifest_write_load_and_paths(roots):
    paths = DatasetPaths.resolve("d", data_root=roots.data, configs_root=roots.configs)
    m = _manifest()
    path = write_manifest(paths, m)
    assert path == paths.backup_manifest(m.manifest_id)
    text = path.read_text(encoding="utf-8")
    assert '"for": [' in text and "for_" not in text and "\r" not in path.read_bytes().decode()
    assert load_manifest(paths, m.manifest_id) == m
    with pytest.raises(ValidationFailed, match="exists"):
        write_manifest(paths, m)
    with pytest.raises(ValidationFailed, match="not_found"):
        load_manifest(paths, "nope")
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="bad manifest"):
        load_manifest(paths, m.manifest_id)
    d, c, x = m.files
    assert local_path(d, roots.data, roots.configs) == roots.data / "runs" / "r1" / "run.yaml"
    assert local_path(c, roots.data, roots.configs) == roots.configs / "datasets" / "d" / "dataset.yaml"
    assert local_path(x, roots.data, roots.configs).as_posix() == "D:/w/best.pt"


def test_default_manifest_id():
    mid = default_manifest_id("submission:SUB34")
    assert mid.startswith("submission-SUB34-") and mid.endswith("Z") and len(mid) == len("submission-SUB34-20260906T000000Z")
    assert default_manifest_id("all").startswith("all-")
```

- [ ] **Step 7: 寫 `src/vcp/backup/ledger.py` 與 `src/vcp/backup/manifest.py`**

`ledger.py`：

```python
"""``backup.log.jsonl``: every manifest, push, verify, pull and credential wipe, appended and
never edited."""

from __future__ import annotations

from pathlib import Path

from vcp.measure.ledger import read_rows
from vcp.backup.schema import BackupRow


class BackupLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[BackupRow] = read_rows(path, BackupRow)

    def append(self, row: BackupRow) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(row.model_dump_json(exclude_none=True) + "\n")
        self.rows.append(row)

    def of(self, event: str, manifest_id: str | None = None) -> list[BackupRow]:
        return [
            r
            for r in self.rows
            if r.event == event and (manifest_id is None or r.manifest_id == manifest_id)
        ]

    def latest(self, event: str, manifest_id: str) -> BackupRow | None:
        rows = self.of(event, manifest_id)
        return rows[-1] if rows else None

    def manifest_ids(self) -> list[str]:
        return [r.manifest_id for r in self.of("manifest") if r.manifest_id]
```

`manifest.py`：

```python
"""The evidence manifest file (spec 4.1): written once, loaded by push / verify / pull."""

from __future__ import annotations

import json
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import utc_now
from vcp.backup.schema import FileEntry, Manifest


def default_manifest_id(conclusion: str) -> str:
    kind, _, ident = conclusion.partition(":")
    ts = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return f"{kind}-{ident}-{ts}" if ident else f"{kind}-{ts}"


def write_manifest(paths: DatasetPaths, manifest: Manifest) -> Path:
    path = paths.backup_manifest(manifest.manifest_id)
    if path.exists():
        raise ValidationFailed(
            f"exists: manifest {manifest.manifest_id!r} is already written at {path}",
            fields={"manifest": manifest.manifest_id},
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = manifest.model_dump(mode="json", by_alias=True)
    path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"
    )
    return path


def load_manifest(paths: DatasetPaths, manifest_id: str) -> Manifest:
    path = paths.backup_manifest(manifest_id)
    if not path.is_file():
        raise ValidationFailed(
            f"not_found: manifest {manifest_id!r} ({path})", fields={"manifest": manifest_id}
        )
    try:
        return Manifest.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValidationFailed(f"bad manifest: {e}", location=str(path)) from e


def local_path(entry: FileEntry, data_root: Path, configs_root: Path) -> Path:
    if entry.root == "data":
        return data_root / entry.path
    if entry.root == "configs":
        return configs_root / entry.path
    return Path(str(entry.source))
```

- [ ] **Step 8: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/backup -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/core/paths.py src/vcp/backup/__init__.py src/vcp/backup/schema.py src/vcp/backup/ledger.py src/vcp/backup/manifest.py tests/unit/backup/__init__.py tests/unit/backup/test_schema.py tests/unit/backup/test_ledger_manifest.py
git commit -m "feat(backup): 清單與台帳的資料模型（角色 / tier / remote_copy）、只增台帳、清單檔讀寫、backup 路徑" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---
### Task 3: 證據圖——`run:` 走法、結論解析、測試夾具

**Files:**
- Create: `src/vcp/backup/evidence.py`（本任務只放 `parse_conclusion`、`external_path`、`Collector` 的共用方法與 `walk_run`；Task 4 補其餘走法與 `build_manifest`）、`tests/backup_fixtures.py`
- Test: `tests/unit/backup/test_evidence_run.py`

**Interfaces:**
- Consumes: Task 2 的 `FileEntry` / `RemoteCopy` / `ROLES` / `TIER_OF`；`vcp.measure.runs.load_run` / `run_dir`；`vcp.fuse.build.load_record` / `record_path`；`vcp.fuse.recipes.recipe_path`；`vcp.train.records.has_record` / `train_yaml` / `events_path` / `train_dir` / `load_record`；`vcp.core.paths.DatasetPaths` / `resolve_stored_path` / `validate_name`；`vcp.measure.report.READINGS_LEDGER` / `JUDGEMENTS_LEDGER` / `SIGMA_LEDGER`。
- Produces: `parse_conclusion(text) -> tuple[str, str]`（`("all", "")` 或 `(kind, ident)`）、`external_path(path) -> str`、`Collector(data_root, configs_root)` 的 `add(path, role, conclusion, *, sha256=None, size=None, remote=None)`、`locate(path)`、`files_of()`、`measure_ledgers(dpaths, conclusion)`、`dataset_basics(dpaths, plan_id, conclusion)`、`walk_run(run_id, conclusion)`；屬性 `entries`、`missing`、`unlisted`；常數 `CONCLUSIONS`、`HISTORY`、`ANCHORS`、`ANCHORS_LOG`、`MEASURE_LEDGERS`。夾具 `tests/backup_fixtures.py`：`make_world(roots, tmp_path)`（回 `SimpleNamespace(pair, roots, tmp, vault, weights)`）、`make_fusion(world, run_id="fx") -> str`。

- [ ] **Step 1: 寫夾具** `tests/backup_fixtures.py`

```python
"""Shared by the backup-layer tests: the submission layer's world (eval + test datasets, runs
good / bad, judgements, a staged submission S1) plus a training record for `good` with one
checkpoint already uploaded and verified, and a hand-built fusion run."""

from __future__ import annotations

from types import SimpleNamespace

from submit_fixtures import EVAL, STAMP, TEST, make_pair, seed_eval_runs, seed_judgements, seed_test_runs
from vcp.core.hashing import sha256_file
from vcp.fuse.build import write_record
from vcp.fuse.recipes import save_recipe
from vcp.fuse.schema import FuseRecord, Member, MemberRecord, Recipe, SubsetBuild
from vcp.measure.runs import load_run, run_dir, save_run
from vcp.measure.schema import PredictionFile, RunCard, RunSource
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile
from vcp.submit.stage import StageSpec, stage
from vcp.train.checkpoints import mark_final, register
from vcp.train.records import append_event, save_record, train_dir
from vcp.train.schema import TrainRecord
from vcp.train.upload import merge_uploads, upload


def make_world(roots, tmp_path) -> SimpleNamespace:
    pair = make_pair(roots, tmp_path)
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(
        PlatformProfile(
            dataset=TEST,
            eval_dataset=EVAL,
            plan_id="fixed-v1",
            sealed_subset="holdout",
            platform="manual",
            board_rule="last",
            metric="accuracy",
            writer="scores_csv",
            created_at=STAMP,
        ),
        data_root=roots.data,
        configs_root=roots.configs,
    )
    seed_test_runs(pair)
    stage(
        StageSpec(
            dataset=TEST,
            submission_id="S1",
            eval_run="good",
            test_run="good.test",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    weights = roots.data / "work" / "good" / "weights"
    weights.mkdir(parents=True)
    (weights / "best.pt").write_bytes(b"best weights")
    (weights / "last.pt").write_bytes(b"last weights")
    rec = TrainRecord(
        run_id="good",
        dataset=EVAL,
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="w",
        command=["python"],
    )
    rec, _ = register(rec, [weights / "best.pt", weights / "last.pt"], data_root=roots.data, attempt=1)
    rec = mark_final(rec, "work/good/weights/best.pt", sha256_file(weights / "best.pt"))
    vault = tmp_path / "vault-train"
    out = upload(rec, str(vault), data_root=roots.data, only_final=True)
    rec = merge_uploads(rec, out.records)
    save_record(roots.data, rec)
    append_event(roots.data, "good", "checkpoint", 1, path="work/good/weights/best.pt", final=True)
    tdir = train_dir(roots.data, "good")
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "console.1.log").write_text("epoch 1 done\n", encoding="utf-8")
    return SimpleNamespace(pair=pair, roots=roots, tmp=tmp_path, vault=vault, weights=weights)


def make_fusion(world, run_id: str = "fx") -> str:
    """A fused run over good + bad on valA, with fuse.json and a recipe file, built by hand."""
    roots = world.roots
    good = load_run(roots.data, "good")
    bad = load_run(roots.data, "bad")
    src = run_dir(roots.data, "good") / good.predictions["valA"].path
    dst = run_dir(roots.data, run_id) / "predictions" / "valA.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(src.read_bytes())
    sha = sha256_file(dst)
    entry = good.predictions["valA"]
    save_run(
        roots.data,
        RunCard(
            run_id=run_id,
            dataset=EVAL,
            samples_hash=good.samples_hash,
            plan_id="fixed-v1",
            trained_on=["train"],
            source=RunSource(framework="vcp.fuse", config_hash="cc" * 32),
            created_at=STAMP,
            predictions={
                "valA": PredictionFile(
                    path="predictions/valA.jsonl",
                    sha256=sha,
                    samples=entry.samples,
                    empty=entry.empty,
                    format_in="fuse:mean",
                    ingested_at=STAMP,
                )
            },
        ),
    )
    write_record(
        roots.data,
        run_id,
        FuseRecord(
            run_id=run_id,
            recipe_id="r1",
            recipe_sha256="cc" * 32,
            method="mean",
            method_version="1",
            params={},
            members=[
                MemberRecord(run="good", weight=1.0, trained_on=["train"]),
                MemberRecord(run="bad", weight=1.0, trained_on=["train"]),
            ],
            subsets={
                "valA": SubsetBuild(
                    member_sha256={"good": entry.sha256, "bad": bad.predictions["valA"].sha256},
                    output_sha256=sha,
                    samples=entry.samples,
                    empty=entry.empty,
                    built_at=STAMP,
                )
            },
            vcp_version="0",
        ),
    )
    save_recipe(
        world.pair.eval_paths,
        Recipe(
            recipe_id="r1",
            dataset=EVAL,
            plan_id="fixed-v1",
            method="mean",
            params={},
            members=[Member(run="good", weight=1.0), Member(run="bad", weight=1.0)],
            created_at=STAMP,
        ),
    )
    return run_id
```

`Recipe.params` 的預設由融合層決定；若 `save_recipe` 拒絕空 `params`（有效參數必須填齊），改傳 `params=get_fuser("mean").defaults` 之類——看 `src/vcp/fuse/recipes.py` 與 `fusers/base.py` 怎麼要求，在報告裡說明。`register(...)` 的 `store_path` 把 `work/good/weights/best.pt` 存成相對 data root 的路徑，`mark_final` 用同一字串。

`tests/unit/backup/conftest.py`：

```python
import pytest
from backup_fixtures import make_world


@pytest.fixture
def world(roots, tmp_path):
    return make_world(roots, tmp_path)
```

- [ ] **Step 2: 寫失敗的測試** `tests/unit/backup/test_evidence_run.py`

```python
from pathlib import Path

import pytest
from backup_fixtures import make_fusion
from submit_fixtures import EVAL

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.measure.runs import load_run, run_dir
from vcp.backup.evidence import CONCLUSIONS, Collector, external_path, parse_conclusion


def _col(world) -> Collector:
    return Collector(world.roots.data, world.roots.configs)


def _roles(col) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for e in col.files_of():
        out.setdefault(e.role, []).append(e.path)
    return out


def test_parse_conclusion():
    assert CONCLUSIONS == ("submission", "judgement", "run", "all")
    assert parse_conclusion("all") == ("all", "")
    assert parse_conclusion("run:good.test") == ("run", "good.test")
    for bad in ("all:x", "run", "run:", "photo:1", "run:bad id"):
        with pytest.raises(ValidationFailed):
            parse_conclusion(bad)


def test_external_path():
    assert external_path(Path("C:/w/best.pt")).endswith("C/w/best.pt")
    assert ":" not in external_path(Path("D:/x/y.pt"))


def test_walk_run_collects_the_run_its_dataset_and_its_training(world):
    col = _col(world)
    col.walk_run("good", "run:good")
    roles = _roles(col)
    assert roles["run_card"] == ["runs/good/run.yaml"]
    assert sorted(roles["prediction"]) == [
        "runs/good/predictions/holdout.jsonl",
        "runs/good/predictions/valA.jsonl",
        "runs/good/predictions/valB.jsonl",
    ]
    assert roles["dataset_card"] == [f"datasets/{EVAL}/dataset.yaml"]
    assert roles["plan"] == [f"datasets/{EVAL}/splits/fixed-v1.json"]
    assert roles["unseal_log"] == [f"datasets/{EVAL}/splits/fixed-v1.unseal.jsonl"]
    assert "readings" in roles and "judgements" in roles
    assert roles["train_record"] == ["runs/good/train.yaml"] and roles["train_log"] == ["runs/good/train.log.jsonl"]
    assert roles["train_dir"] == ["runs/good/train/console.1.log"]
    assert roles["checkpoint_final"] == ["work/good/weights/best.pt"]
    assert roles["checkpoint"] == ["work/good/weights/last.pt"]
    by_key = {e.key: e for e in col.files_of()}
    final = by_key["data/work/good/weights/best.pt"]
    assert final.kind == "remote_copy" and final.remote.dest == str(world.vault) and final.remote.run == "good"
    assert final.remote.name == "best.pt" and final.tier == 3 and final.present
    last = by_key["data/work/good/weights/last.pt"]
    assert last.kind == "file" and last.sha256 == sha256_file(world.weights / "last.pt")
    assert all(e.for_ == ["run:good"] for e in col.files_of())
    assert col.missing == [] and col.unlisted == []
    tiers = [e.tier for e in col.files_of()]
    assert tiers == sorted(tiers)


def test_walk_run_merges_conclusions_and_records_missing_files(world):
    col = _col(world)
    col.walk_run("good", "run:good")
    col.walk_run("good", "judgement:p-good")
    by_key = {e.key: e for e in col.files_of()}
    assert by_key["data/runs/good/run.yaml"].for_ == ["run:good", "judgement:p-good"]
    card = load_run(world.roots.data, "bad")
    gone = run_dir(world.roots.data, "bad") / card.predictions["valB"].path
    gone.unlink()
    col.walk_run("bad", "run:bad")
    entry = {e.key: e for e in col.files_of()}["data/runs/bad/predictions/valB.jsonl"]
    assert not entry.present and entry.sha256 == card.predictions["valB"].sha256 and entry.bytes == 0
    assert col.missing == ["data/runs/bad/predictions/valB.jsonl"]
    with pytest.raises(ValidationFailed, match="not_found"):
        col.walk_run("nope", "run:nope")


def test_walk_run_recurses_into_fusion_members(world):
    fx = make_fusion(world)
    col = _col(world)
    col.walk_run(fx, f"run:{fx}")
    roles = _roles(col)
    assert roles["fuse_record"] == [f"runs/{fx}/fuse.json"]
    assert roles["recipe"] == [f"datasets/{EVAL}/fuse/r1.yaml"]
    assert sorted(roles["run_card"]) == ["runs/bad/run.yaml", f"runs/{fx}/run.yaml", "runs/good/run.yaml"]
    assert "checkpoint_final" in roles  # good's training came along


def test_external_checkpoint(world, tmp_path):
    outside = tmp_path / "elsewhere" / "extra.pt"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"extra")
    from vcp.train.checkpoints import register
    from vcp.train.records import load_record, save_record

    rec = load_record(world.roots.data, "good")
    rec, _ = register(rec, [outside], data_root=world.roots.data, attempt=2)
    save_record(world.roots.data, rec)
    col = _col(world)
    col.walk_run("good", "run:good")
    ext = [e for e in col.files_of() if e.root == "external"]
    assert len(ext) == 1 and ext[0].source == outside.resolve().as_posix()
    assert ext[0].path == external_path(outside) and ext[0].role == "checkpoint"
    paths = DatasetPaths.resolve(EVAL, data_root=world.roots.data, configs_root=world.roots.configs)
    assert col.locate(paths.card_yaml)[0] == "configs"
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/unit/backup/test_evidence_run.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: vcp.backup.evidence`）

- [ ] **Step 4: 寫 `src/vcp/backup/evidence.py`（本任務的部分）**

```python
"""The evidence graph (spec 7): from a conclusion to every file that supports it.

A conclusion is a final submission, a judgement, a run, or a whole dataset. Walking it yields the
files a reader would need to reproduce or re-verify that conclusion, each tagged with a role (and
so a tier) and with the conclusions it serves. Nothing here copies anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, resolve_stored_path, validate_name
from vcp.fuse.build import load_record, record_path
from vcp.fuse.recipes import recipe_path
from vcp.measure.report import JUDGEMENTS_LEDGER, READINGS_LEDGER, SIGMA_LEDGER
from vcp.measure.runs import load_run, run_dir
from vcp.train.records import events_path, has_record, train_dir, train_yaml
from vcp.train.records import load_record as load_train_record
from vcp.train.schema import CheckpointRecord, TrainRecord
from vcp.backup.schema import ROLES, TIER_OF, FileEntry, RemoteCopy

CONCLUSIONS = ("submission", "judgement", "run", "all")
HISTORY = "history.jsonl"
ANCHORS = "anchors.json"
ANCHORS_LOG = "anchors.log.jsonl"
MEASURE_LEDGERS = (
    (READINGS_LEDGER, "readings"),
    (JUDGEMENTS_LEDGER, "judgements"),
    (SIGMA_LEDGER, "sigma"),
    (ANCHORS, "anchors"),
    (ANCHORS_LOG, "anchors_log"),
)


def parse_conclusion(text: str) -> tuple[str, str]:
    """``all`` -> ("all", ""); ``run:<id>`` -> ("run", "<id>"); anything else is a FAIL."""
    kind, sep, ident = text.partition(":")
    if kind == "all" and not sep:
        return "all", ""
    if kind not in CONCLUSIONS or kind == "all" or not ident:
        raise ValidationFailed(
            f"bad conclusion {text!r}: expected submission:<id>, judgement:<prereg>, run:<id> or all"
        )
    validate_name(ident)
    return kind, ident


def external_path(path: Path) -> str:
    """``C:/x/y`` -> ``C/x/y``, ``/mnt/x`` -> ``mnt/x``: a relative posix path that keeps the origin."""
    return path.resolve().as_posix().replace(":", "").lstrip("/")


@dataclass
class Collector:
    data_root: Path
    configs_root: Path
    entries: dict[str, FileEntry] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)
    _seen: set[tuple[str, str]] = field(default_factory=set)

    def locate(self, path: Path) -> tuple[str, str, str | None]:
        """(root, relative posix path, source): data / configs by containment, else external."""
        resolved = path.resolve()
        for root, base in (("data", self.data_root), ("configs", self.configs_root)):
            try:
                return root, resolved.relative_to(base.resolve()).as_posix(), None
            except ValueError:
                continue
        return "external", external_path(resolved), resolved.as_posix()

    def add(
        self,
        path: Path,
        role: str,
        conclusion: str,
        *,
        sha256: str | None = None,
        size: int | None = None,
        remote: RemoteCopy | None = None,
    ) -> None:
        """Record one file. A present file is hashed now; an absent one is listed with the sha a
        record vouches for (so verify / pull can still act on it), or dropped as `unlisted`."""
        root, rel, source = self.locate(path)
        key = f"{root}/{rel}"
        entry = self.entries.get(key)
        if entry is not None:
            if conclusion not in entry.for_:
                entry.for_.append(conclusion)
            return
        present = path.is_file()
        if present:
            sha256 = sha256_file(path)
            size = path.stat().st_size
        elif sha256 is None:
            self.unlisted.append(key)
            return
        else:
            self.missing.append(key)
            size = size or 0
        self.entries[key] = FileEntry(
            root=root,  # type: ignore[arg-type]
            path=rel,
            sha256=sha256,
            bytes=size,
            role=role,
            tier=TIER_OF[role],
            kind="remote_copy" if remote is not None else "file",
            present=present,
            remote=remote,
            source=source,
            for_=[conclusion],
        )

    def files_of(self) -> list[FileEntry]:
        return sorted(
            self.entries.values(), key=lambda e: (e.tier, ROLES.index(e.role), e.root, e.path)
        )

    def measure_ledgers(self, dpaths: DatasetPaths, conclusion: str) -> None:
        for name, role in MEASURE_LEDGERS:
            p = dpaths.measure_dir / name
            if p.is_file():
                self.add(p, role, conclusion)

    def dataset_basics(self, dpaths: DatasetPaths, plan_id: str | None, conclusion: str) -> None:
        if dpaths.card_yaml.is_file():
            self.add(dpaths.card_yaml, "dataset_card", conclusion)
        if plan_id is not None:
            self.add(dpaths.plan_json(plan_id), "plan", conclusion)
            unseal = dpaths.unseal_jsonl(plan_id)
            if unseal.is_file():
                self.add(unseal, "unseal_log", conclusion)

    def _dataset_paths(self, name: str) -> DatasetPaths:
        return DatasetPaths.resolve(name, data_root=self.data_root, configs_root=self.configs_root)

    def walk_run(self, run_id: str, conclusion: str) -> None:
        if (run_id, conclusion) in self._seen:
            return
        self._seen.add((run_id, conclusion))
        rdir = run_dir(self.data_root, run_id)
        if not (rdir / "run.yaml").is_file():
            raise ValidationFailed(
                f"not_found: run {run_id!r} has no run.yaml under {rdir}", fields={"run": run_id}
            )
        card = load_run(self.data_root, run_id)
        self.add(rdir / "run.yaml", "run_card", conclusion)
        if (rdir / HISTORY).is_file():
            self.add(rdir / HISTORY, "history", conclusion)
        for entry in card.predictions.values():
            self.add(rdir / entry.path, "prediction", conclusion, sha256=entry.sha256)
        if record_path(self.data_root, run_id).is_file():
            self.add(record_path(self.data_root, run_id), "fuse_record", conclusion)
            rec = load_record(self.data_root, run_id)
            rpath = recipe_path(self._dataset_paths(card.dataset), rec.recipe_id)
            if rpath.is_file():
                self.add(rpath, "recipe", conclusion)
            for m in rec.members:
                self.walk_run(m.run, conclusion)
        if has_record(self.data_root, run_id):
            self.add(train_yaml(self.data_root, run_id), "train_record", conclusion)
            if events_path(self.data_root, run_id).is_file():
                self.add(events_path(self.data_root, run_id), "train_log", conclusion)
            tdir = train_dir(self.data_root, run_id)
            if tdir.is_dir():
                for p in sorted(tdir.rglob("*")):
                    if p.is_file():
                        self.add(p, "train_dir", conclusion)
            self._checkpoints(load_train_record(self.data_root, run_id), conclusion)
        dpaths = self._dataset_paths(card.dataset)
        self.dataset_basics(dpaths, card.plan_id, conclusion)
        self.measure_ledgers(dpaths, conclusion)

    def _checkpoints(self, record: TrainRecord, conclusion: str) -> None:
        """The newest record per checkpoint file name (a --resume that changed the bytes is
        history); a copy `train upload` verified becomes a remote_copy instead of a file."""
        newest: dict[str, CheckpointRecord] = {}
        for c in record.checkpoints:
            newest[Path(c.path).name] = c
        for name, c in newest.items():
            remote = None
            for u in reversed(record.uploads):
                if u.name == name and u.sha256 == c.sha256 and u.verified:
                    remote = RemoteCopy(dest=u.dest, run=record.run_id, name=name)
                    break
            self.add(
                resolve_stored_path(c.path, self.data_root),
                "checkpoint_final" if c.final else "checkpoint",
                conclusion,
                sha256=c.sha256,
                size=c.bytes,
                remote=remote,
            )
```

- [ ] **Step 5: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/backup -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/backup/evidence.py tests/backup_fixtures.py tests/unit/backup/conftest.py tests/unit/backup/test_evidence_run.py
git commit -m "feat(backup): 證據圖的 run 走法（預測、融合成員遞迴、訓練紀錄與 checkpoint / remote_copy、dataset 與量測台帳）與結論解析" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: 證據圖——`judgement:` / `submission:` / `all`、`build_manifest`、`vcp backup manifest`

**Files:**
- Modify: `src/vcp/backup/evidence.py`（加 `walk_judgement`、`walk_submission`、`walk_all`、`build_manifest`、`ManifestResult`）、`src/vcp/cli.py`
- Create: `src/vcp/cli_backup.py`
- Test: `tests/unit/backup/test_evidence_more.py`、`tests/unit/test_cli_backup.py`

**Interfaces:**
- Consumes: Task 3 的 `Collector`；`vcp.measure.prereg.prereg_path` / `list_preregs` / `load_prereg`；`vcp.submit.profile.load_profile`、`vcp.submit.stage.load_staged` / `stage_json`、`vcp.submit.ledger.SubmissionLedger`；`vcp.core.config.load_yaml_model`、`vcp.measure.schema.RunCard`；`vcp.core.paths.logs_dir`；Task 2 的 `write_manifest` / `default_manifest_id` / `BackupLedger` / `BackupRow` / `Manifest`；`vcp.__version__`。
- Produces: `Collector.walk_judgement(dpaths, prereg_id, conclusion)`、`walk_submission(tpaths, submission_id, conclusion)`、`walk_all(dpaths)`；`build_manifest(dataset, conclusion, *, manifest_id=None, data_root=None, configs_root=None) -> ManifestResult(manifest, path, missing, unlisted)`；`vcp.cli_backup.backup_app`、`DatasetOpt`、`ManifestOpt`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/backup/test_evidence_more.py`

```python
import pytest
from backup_fixtures import make_fusion
from submit_fixtures import EVAL, TEST

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths, logs_dir
from vcp.backup.evidence import Collector, build_manifest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest


def _paths(world, name):
    return DatasetPaths.resolve(name, data_root=world.roots.data, configs_root=world.roots.configs)


def _roles(entries):
    out = {}
    for e in entries:
        out.setdefault(e.role, []).append(e.path)
    return out


def test_walk_judgement(world):
    col = Collector(world.roots.data, world.roots.configs)
    col.walk_judgement(_paths(world, EVAL), "p-good", "judgement:p-good")
    roles = _roles(col.files_of())
    assert roles["prereg"] == [f"datasets/{EVAL}/prereg/p-good.yaml"]
    assert roles["prereg_log"] == [f"datasets/{EVAL}/prereg.log.jsonl"]
    assert sorted(roles["run_card"]) == ["runs/bad/run.yaml", "runs/good/run.yaml"]
    with pytest.raises(ValidationFailed, match="not_found"):
        col.walk_judgement(_paths(world, EVAL), "p-none", "judgement:p-none")


def test_walk_submission(world):
    col = Collector(world.roots.data, world.roots.configs)
    col.walk_submission(_paths(world, TEST), "S1", "submission:S1")
    roles = _roles(col.files_of())
    assert roles["submit_profile"] == [f"datasets/{TEST}/submit.yaml"]
    assert roles["submissions_log"] == [f"datasets/{TEST}/submissions.jsonl"]
    assert roles["stage"] == [f"submit/{TEST}/S1/stage.json"]
    assert roles["artifact"] == [f"submit/{TEST}/S1/submission.csv"]
    assert sorted(roles["run_card"]) == ["runs/bad/run.yaml", "runs/good.test/run.yaml", "runs/good/run.yaml"]
    assert roles["prereg"] == [f"datasets/{EVAL}/prereg/p-good.yaml"]
    assert sorted(roles["dataset_card"]) == [f"datasets/{TEST}/dataset.yaml", f"datasets/{EVAL}/dataset.yaml"]
    assert f"datasets/{TEST}/splits/all-v1.json" in roles["plan"]
    assert "logs" not in roles
    with pytest.raises(ValidationFailed, match="not_found"):
        col.walk_submission(_paths(world, TEST), "S9", "submission:S9")


def test_walk_all_on_eval_and_test_datasets(world):
    make_fusion(world)
    (logs_dir(world.roots.data)).mkdir(parents=True, exist_ok=True)
    (logs_dir(world.roots.data) / "vcp-2026-09-06.jsonl").write_text('{"ts": "2026-09-06T00:00:00.000Z"}\n', encoding="utf-8")
    col = Collector(world.roots.data, world.roots.configs)
    col.walk_all(_paths(world, EVAL))
    roles = _roles(col.files_of())
    assert sorted(roles["run_card"]) == ["runs/bad/run.yaml", "runs/fx/run.yaml", "runs/good/run.yaml"]
    assert sorted(roles["prereg"]) == [f"datasets/{EVAL}/prereg/p-bad.yaml", f"datasets/{EVAL}/prereg/p-good.yaml"]
    assert roles["samples"] == [f"datasets/{EVAL}/samples.jsonl"]
    assert roles["logs"] == ["logs/vcp-2026-09-06.jsonl"]
    assert all(e.for_ == ["all"] for e in col.files_of())
    col2 = Collector(world.roots.data, world.roots.configs)
    col2.walk_all(_paths(world, TEST))
    roles2 = _roles(col2.files_of())
    assert roles2["stage"] == [f"submit/{TEST}/S1/stage.json"]
    assert "runs/good.test/run.yaml" in roles2["run_card"] and "runs/good/run.yaml" in roles2["run_card"]


def test_build_manifest_writes_file_and_ledger_row(world):
    res = build_manifest("beach-test", "submission:S1", data_root=world.roots.data, configs_root=world.roots.configs)
    assert res.manifest.manifest_id.startswith("submission-S1-") and res.path.is_file()
    assert res.missing == [] and res.unlisted == []
    loaded = load_manifest(_paths(world, TEST), res.manifest.manifest_id)
    assert loaded == res.manifest and loaded.conclusion == "submission:S1"
    row = BackupLedger(_paths(world, TEST).backup_log).rows[-1]
    assert row.event == "manifest" and row.files == len(loaded.files) and row.remote_copies == 1
    assert row.bytes_by_tier["1"] > 0 and row.bytes_by_tier["3"] > 0
    with pytest.raises(ValidationFailed, match="exists"):
        build_manifest("beach-test", "submission:S1", manifest_id=res.manifest.manifest_id, data_root=world.roots.data, configs_root=world.roots.configs)
    with pytest.raises(ValidationFailed, match="belongs to dataset"):
        build_manifest("beach-test", "run:good", data_root=world.roots.data, configs_root=world.roots.configs)
    with pytest.raises(ValidationFailed, match="not_found"):
        build_manifest("nope", "all", data_root=world.roots.data, configs_root=world.roots.configs)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/backup/test_evidence_more.py -o addopts="" -q`
Expected: FAIL（`AttributeError: walk_judgement` / `ImportError: build_manifest`）

- [ ] **Step 3: 補 `src/vcp/backup/evidence.py`**

在 import 區加：

```python
from dataclasses import dataclass, field  # 已有
from vcp import __version__
from vcp.core.config import load_yaml_model
from vcp.core.paths import DatasetPaths, logs_dir, resolve_stored_path, validate_name
from vcp.core.time import stamp
from vcp.measure.prereg import list_preregs, load_prereg, prereg_path
from vcp.measure.schema import RunCard
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import load_profile
from vcp.submit.stage import load_staged, stage_json
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import default_manifest_id, write_manifest
from vcp.backup.schema import ROLES, TIER_OF, BackupRow, FileEntry, Manifest, RemoteCopy
```

（合併成一行 import 各模組即可，ruff 會排。）`Collector` 加三個方法：

```python
    def walk_judgement(self, dpaths: DatasetPaths, prereg_id: str, conclusion: str) -> None:
        path = prereg_path(dpaths, prereg_id)
        if not path.is_file():
            raise ValidationFailed(
                f"not_found: pre-registration {prereg_id!r} ({path})", fields={"prereg": prereg_id}
            )
        pr = load_prereg(dpaths, prereg_id)
        self.add(path, "prereg", conclusion)
        if dpaths.prereg_log.is_file():
            self.add(dpaths.prereg_log, "prereg_log", conclusion)
        self.measure_ledgers(dpaths, conclusion)
        self.walk_run(pr.baseline_run, conclusion)
        self.walk_run(pr.candidate_run, conclusion)

    def walk_submission(self, tpaths: DatasetPaths, submission_id: str, conclusion: str) -> None:
        profile, _ = load_profile(tpaths)
        if not stage_json(tpaths, submission_id).is_file():
            raise ValidationFailed(
                f"not_found: submission {submission_id!r} has no stage.json under "
                f"{tpaths.submission_dir(submission_id)}",
                fields={"id": submission_id},
            )
        staged = load_staged(tpaths, submission_id)
        self.add(tpaths.submit_yaml, "submit_profile", conclusion)
        if tpaths.submissions_log.is_file():
            self.add(tpaths.submissions_log, "submissions_log", conclusion)
        self.add(stage_json(tpaths, submission_id), "stage", conclusion)
        if staged.artifact.kind == "file":
            self.add(
                tpaths.submission_dir(submission_id) / str(staged.artifact.path),
                "artifact",
                conclusion,
                sha256=staged.artifact.sha256,
                size=staged.artifact.bytes,
            )
        self.walk_run(staged.eval_run, conclusion)
        if staged.test_run:
            self.walk_run(staged.test_run, conclusion)
        for w in staged.artifact.weights:
            self.walk_run(w.run, conclusion)
        epaths = self._dataset_paths(profile.eval_dataset)
        for pid in staged.gate.judgements:
            self.walk_judgement(epaths, pid, conclusion)
        self.dataset_basics(tpaths, profile.test_plan, conclusion)

    def walk_all(self, dpaths: DatasetPaths) -> None:
        conclusion = "all"
        if dpaths.runs_dir.is_dir():
            for p in sorted(dpaths.runs_dir.glob("*/run.yaml")):
                try:
                    card = load_yaml_model(p, RunCard)
                except (ValidationFailed, OSError, UnicodeDecodeError):
                    continue  # another project's or a half-written card is not this dataset's
                if card.dataset == dpaths.name:
                    self.walk_run(card.run_id, conclusion)
        for pid in list_preregs(dpaths):
            self.walk_judgement(dpaths, pid, conclusion)
        if dpaths.submit_yaml.is_file():
            for sid in SubmissionLedger(dpaths.submissions_log).ids():
                if stage_json(dpaths, sid).is_file():
                    self.walk_submission(dpaths, sid, conclusion)
        self.dataset_basics(dpaths, None, conclusion)
        if dpaths.splits_dir.is_dir():
            for p in sorted(dpaths.splits_dir.glob("*.json")):
                self.add(p, "plan", conclusion)
            for p in sorted(dpaths.splits_dir.glob("*.unseal.jsonl")):
                self.add(p, "unseal_log", conclusion)
        self.measure_ledgers(dpaths, conclusion)
        if dpaths.samples_jsonl.is_file():
            self.add(dpaths.samples_jsonl, "samples", conclusion)
        if dpaths.raw_manifest.is_file():
            self.add(dpaths.raw_manifest, "raw_manifest", conclusion)
        for p in sorted(logs_dir(self.data_root).glob("vcp-*.jsonl")):
            self.add(p, "logs", conclusion)
```

模組層加：

```python
@dataclass(frozen=True)
class ManifestResult:
    manifest: Manifest
    path: Path
    missing: list[str]
    unlisted: list[str]


def build_manifest(
    dataset: str,
    conclusion: str,
    *,
    manifest_id: str | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> ManifestResult:
    """Walk the conclusion, write the manifest (once) and its ledger row."""
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    kind, ident = parse_conclusion(conclusion)
    if not paths.card_yaml.is_file():
        raise ValidationFailed(f"not_found: dataset {dataset!r} ({paths.card_yaml})")
    mid = manifest_id or default_manifest_id(conclusion)
    validate_name(mid)
    if paths.backup_manifest(mid).exists():
        raise ValidationFailed(f"exists: manifest {mid!r}", fields={"manifest": mid})
    col = Collector(paths.data_root, paths.configs_root)
    if kind == "run":
        if load_run(paths.data_root, ident).dataset != dataset:
            raise ValidationFailed(
                f"run {ident!r} belongs to dataset {load_run(paths.data_root, ident).dataset!r}, "
                f"not {dataset!r}",
                fields={"run": ident},
            )
        col.walk_run(ident, conclusion)
    elif kind == "judgement":
        col.walk_judgement(paths, ident, conclusion)
    elif kind == "submission":
        col.walk_submission(paths, ident, conclusion)
    else:
        col.walk_all(paths)
    manifest = Manifest(
        manifest_id=mid,
        dataset=dataset,
        conclusion=conclusion,
        created_at=stamp(),
        vcp_version=__version__,
        data_root=paths.data_root.as_posix(),
        files=col.files_of(),
    )
    path = write_manifest(paths, manifest)
    BackupLedger(paths.backup_log).append(
        BackupRow(
            event="manifest",
            ts=stamp(),
            manifest_id=mid,
            conclusion=conclusion,
            files=len(manifest.files),
            bytes_by_tier=manifest.bytes_by_tier(),
            missing=len(col.missing),
            remote_copies=sum(1 for f in manifest.files if f.kind == "remote_copy"),
        )
    )
    return ManifestResult(manifest, path, col.missing, col.unlisted)
```

`run:<id>` 的 run 不存在時 `load_run` 拋 `ValidationFailed("run not found …")`——訊息不以 `not_found:` 開頭；在 `kind == "run"` 分支先 `if not (run_dir(paths.data_root, ident) / "run.yaml").is_file(): raise ValidationFailed(f"not_found: run {ident!r}", fields={"run": ident})`。

- [ ] **Step 4: 寫失敗的 CLI 測試** `tests/unit/test_cli_backup.py`

```python
"""``vcp backup`` through the CLI: VERDICT lines, exit codes, --json. Later tasks append."""

import json

import pytest
from backup_fixtures import make_world
from typer.testing import CliRunner

from vcp.cli import app

runner = CliRunner()


@pytest.fixture
def world(roots, tmp_path):
    return make_world(roots, tmp_path)


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _json(r) -> dict:
    return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))


def _run(*args):
    return runner.invoke(app, ["backup", *args])


def test_manifest_cli(world):
    r = _run("manifest", "--dataset", "beach-test", "--conclusion", "submission:S1", "--id", "m-s1", "--json")
    assert r.exit_code == 0, r.output
    doc = _json(r)
    assert doc["status"] == "OK" and doc["fields"]["manifest"] == "m-s1"
    assert doc["fields"]["remote_copies"] == 1 and doc["fields"]["missing"] == 0
    assert doc["result"]["files"] > 10 and doc["result"]["path"].endswith("m-s1.json")
    r = _run("manifest", "--dataset", "beach-test", "--conclusion", "submission:S1", "--id", "m-s1")
    assert r.exit_code == 1 and "exists" in _verdict(r.output)
    r = _run("manifest", "--dataset", "beach-test", "--conclusion", "photo:1")
    assert r.exit_code == 1 and "bad conclusion" in _verdict(r.output)
    (world.weights / "last.pt").unlink()
    r = _run("manifest", "--dataset", "beach", "--conclusion", "run:good", "--id", "m-good")
    assert r.exit_code == 0 and "status=WARN" in _verdict(r.output) and "missing=1" in _verdict(r.output)
```

- [ ] **Step 5: 寫 `src/vcp/cli_backup.py`（manifest）並掛到 `vcp.cli`**

```python
"""``vcp backup``: evidence manifests, verified copies and the audit. Every command ends with a
VERDICT line."""

from __future__ import annotations

from typing import Annotated

import typer

from vcp.cli_common import CmdResult, ConfigsRootOpt, DataRootOpt, JsonOpt, run_command
from vcp.core.log import FieldValue, Status
from vcp.backup.evidence import build_manifest

backup_app = typer.Typer(no_args_is_help=True, help="backup and audit commands")

DatasetOpt = Annotated[str, typer.Option("--dataset", help="dataset the manifest belongs to")]
ManifestOpt = Annotated[str, typer.Option("--manifest", help="manifest id")]


@backup_app.command("manifest")
def manifest_cmd(
    dataset: DatasetOpt,
    conclusion: Annotated[
        str,
        typer.Option("--conclusion", help="submission:<id> | judgement:<prereg> | run:<id> | all"),
    ],
    manifest_id: Annotated[
        str | None, typer.Option("--id", help="manifest id (default <conclusion>-<UTC stamp>)")
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Walk the evidence graph of a conclusion and write its manifest."""

    def fn() -> CmdResult:
        res = build_manifest(
            dataset,
            conclusion,
            manifest_id=manifest_id,
            data_root=data_root,
            configs_root=configs_root,
        )
        m = res.manifest
        by_tier = m.bytes_by_tier()
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "conclusion": conclusion,
            "manifest": m.manifest_id,
            "files": len(m.files),
            "bytes1": by_tier["1"],
            "bytes2": by_tier["2"],
            "bytes3": by_tier["3"],
            "remote_copies": sum(1 for f in m.files if f.kind == "remote_copy"),
            "missing": len(res.missing),
        }
        if res.unlisted:
            fields["unlisted"] = len(res.unlisted)
        human = [f"manifest written to {res.path}"]
        human += [f"missing: {k}" for k in res.missing]
        human += [f"unlisted (no sha on record): {k}" for k in res.unlisted]
        status: Status = "WARN" if res.missing or res.unlisted else "OK"
        payload = {
            "path": str(res.path),
            "files": len(m.files),
            "missing": res.missing,
            "unlisted": res.unlisted,
        }
        return status, fields, payload, human

    run_command("backup.manifest", json_mode, data_root, fn)
```

`src/vcp/cli.py`：加 `from vcp.cli_backup import backup_app`，`app.add_typer(backup_app, name="backup")` 放在 `data` 之後、`eval` 之前（字母序：backup、data、eval、fuse、submit、train）。

- [ ] **Step 6: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/backup tests/unit/test_cli_backup.py tests/unit/test_cli.py -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/backup/evidence.py src/vcp/cli_backup.py src/vcp/cli.py tests/unit/backup/test_evidence_more.py tests/unit/test_cli_backup.py
git commit -m "feat(backup): judgement / submission / all 走法、build_manifest 與 vcp backup manifest" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---
### Task 5: 目的地抽象、`push`、`vcp backup push`

**Files:**
- Create: `src/vcp/backup/dest.py`、`src/vcp/backup/push.py`
- Modify: `src/vcp/cli_backup.py`（加 `push`）、`tests/backup_fixtures.py`（加 `SECRET`、`FakeRemote`）
- Test: `tests/unit/backup/test_dest_push.py`、`tests/unit/test_cli_backup.py`（追加）

**Interfaces:**
- Consumes: Task 1 `vcp.core.proc.Runner` / `default_runner` / `last_line`；Task 2 `load_manifest` / `local_path` / `BackupLedger` / `BackupRow` / `FileEntry`；Task 4 `build_manifest`（測試）與 `cli_backup.DatasetOpt` / `ManifestOpt`；`vcp.train.upload.dest_kind`；`vcp.core.hashing.sha256_file`。
- Produces: `vcp.backup.dest`：`RCLONE: list[str]`（rclone 命令前綴，測試可換）、`LocalDest(dest)` 與 `RcloneDest(dest, runner)`（皆有 `.dest`、`.kind`、`.hashes(sub, rels) -> dict[str, str]`、`.put(src, sub, rel)`、`.get(sub, rel, dst)`；`RcloneDest.forget() -> str`）、`Destination = LocalDest | RcloneDest`、`open_dest(dest, runner=None) -> Destination`、`rclone_conf_state(runner=None) -> "present" | "absent" | "unknown"`。`vcp.backup.push`：`TIERS`、`check_tier(tier)`、`PushResult(manifest_id, dest, tier, pushed, skipped, verified, failed, bytes, forgotten)`、`push(dataset, manifest_id, dest, *, tier=1, forget_remote=False, runner=None, data_root=None, configs_root=None) -> PushResult`。夾具：`backup_fixtures.SECRET`、`backup_fixtures.FakeRemote(*, corrupt=None, fail=None, conf=None, deliver=None)`（`.store: dict[str, bytes]`、`.calls`、`.deleted`）。

- [ ] **Step 1: 夾具加假 rclone** — 追加到 `tests/backup_fixtures.py` 檔尾（import 區加 `import hashlib`、`import subprocess`、`from pathlib import Path`）

```python
SECRET = "fakesecretfakesecretfakesecret1234"


class FakeRemote:
    """An rclone stand-in for unit tests: a dict of remote path -> bytes. ``hashsum`` lists what
    it holds under a prefix, ``copyto`` moves bytes in either direction, ``config delete``
    records the remote name, ``config file`` prints ``conf``. Every call leaks a secret on
    stderr, the way a chatty CLI might, so redaction is exercised everywhere."""

    def __init__(
        self,
        *,
        corrupt: str | None = None,
        fail: str | None = None,
        conf: Path | None = None,
        deliver: bytes | None = None,
    ):
        self.store: dict[str, bytes] = {}
        self.calls: list[list[str]] = []
        self.deleted: list[str] = []
        self.corrupt = corrupt  # hashsum misreports paths ending with this
        self.fail = fail  # this subcommand exits 1
        self.conf = conf  # what `config file` prints
        self.deliver = deliver  # remote->local copyto writes these bytes instead

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        sub = args[1]
        err = f"RCLONE_CONFIG_PASS={SECRET}\n"
        if sub == self.fail:
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr=err + f"{sub} failed token={SECRET}"
            )
        if sub == "hashsum":
            base = args[3].rstrip("/") + "/"
            lines = []
            for path, data in sorted(self.store.items()):
                if path.startswith(base):
                    bad = self.corrupt is not None and path.endswith(self.corrupt)
                    digest = "0" * 64 if bad else hashlib.sha256(data).hexdigest()
                    lines.append(f"{digest}  {path[len(base):]}")
            if not lines:
                return subprocess.CompletedProcess(
                    args, 3, stdout="", stderr=err + "directory not found"
                )
            return subprocess.CompletedProcess(args, 0, stdout="\n".join(lines) + "\n", stderr=err)
        if sub == "copyto":
            src, dst = args[2], args[3]
            if src in self.store:
                data = self.deliver if self.deliver is not None else self.store[src]
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                Path(dst).write_bytes(data)
            else:
                self.store[dst] = Path(src).read_bytes()
            return subprocess.CompletedProcess(args, 0, stdout="", stderr=err)
        if sub == "config" and args[2] == "delete":
            self.deleted.append(args[3])
            return subprocess.CompletedProcess(args, 0, stdout="", stderr=err)
        if sub == "config" and args[2] == "file":
            out = f"Configuration file is stored at:\n{self.conf}\n"
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr=err)
        raise AssertionError(f"unexpected rclone call {args}")
```

- [ ] **Step 2: 寫失敗的測試** `tests/unit/backup/test_dest_push.py`

```python
import pytest
from backup_fixtures import SECRET, FakeRemote
from submit_fixtures import TEST

from vcp.backup import dest as destmod
from vcp.backup.dest import LocalDest, RcloneDest, open_dest, rclone_conf_state
from vcp.backup.evidence import build_manifest
from vcp.backup.ledger import BackupLedger
from vcp.backup.push import PushResult, push
from vcp.core.errors import IntegrityError, PlatformError, ValidationFailed, VcpError
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _manifest(world):
    return build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))


def _push(world, dest, **kw) -> PushResult:
    return push(TEST, "m1", dest, **_kw(world), **kw)


def _ledger(world) -> BackupLedger:
    return BackupLedger(DatasetPaths.resolve(TEST, **_kw(world)).backup_log)


def _files(res, tier):
    return [f for f in res.manifest.files if f.tier == tier and f.kind == "file"]


def test_local_dest_round_trip(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello", encoding="utf-8")
    d = LocalDest(str(tmp_path / "vault"))
    assert d.kind == "local" and d.hashes("data", ["x/a.txt"]) == {}
    d.put(src, "data", "x/a.txt")
    assert d.hashes("data", ["x/a.txt", "nope"]) == {"x/a.txt": sha256_file(src)}
    back = tmp_path / "back" / "a.txt"
    d.get("data", "x/a.txt", back)
    assert back.read_text(encoding="utf-8") == "hello"


def test_rclone_dest_commands_and_redaction(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"hello")
    remote = FakeRemote()
    d = RcloneDest("fake:vault", remote)
    assert d.kind == "rclone" and d.hashes("data", ["x/a.txt"]) == {}
    assert remote.calls[-1] == ["rclone", "hashsum", "sha256", "fake:vault/data"]
    d.put(src, "data", "x/a.txt")
    assert remote.calls[-1] == ["rclone", "copyto", str(src), "fake:vault/data/x/a.txt", "--checksum"]
    assert d.hashes("data", ["x/a.txt"]) == {"x/a.txt": sha256_file(src)}
    back = tmp_path / "back.txt"
    d.get("data", "x/a.txt", back)
    assert remote.calls[-1] == ["rclone", "copyto", "fake:vault/data/x/a.txt", str(back)]
    assert back.read_bytes() == b"hello"
    assert d.forget() == "fake" and remote.deleted == ["fake"]
    assert remote.calls[-1] == ["rclone", "config", "delete", "fake"]
    with pytest.raises(PlatformError, match="copyto failed") as ei:
        RcloneDest("fake:vault", FakeRemote(fail="copyto")).put(src, "data", "x/a.txt")
    assert SECRET not in str(ei.value) and "<redacted>" in str(ei.value)
    assert ei.value.fields == {"exit_code": 1}
    with pytest.raises(PlatformError, match="config delete failed"):
        RcloneDest("fake:vault", FakeRemote(fail="config")).forget()


def test_open_dest_and_conf_state(monkeypatch, tmp_path):
    assert isinstance(open_dest(str(tmp_path)), LocalDest)
    assert isinstance(open_dest("gdrive:x", FakeRemote()), RcloneDest)
    monkeypatch.setattr(destmod.shutil, "which", lambda name, *a, **k: None)
    with pytest.raises(VcpError, match="rclone_not_found") as ei:
        open_dest("gdrive:x")
    assert ei.value.fields == {"dest": "gdrive:x"}
    assert rclone_conf_state() == "unknown"
    conf = tmp_path / "rclone.conf"
    assert rclone_conf_state(FakeRemote(conf=conf)) == "absent"
    conf.write_text("[gdrive]\ntoken = x\n", encoding="utf-8")
    assert rclone_conf_state(FakeRemote(conf=conf)) == "present"
    assert rclone_conf_state(FakeRemote(fail="config")) == "unknown"


def test_push_local_copies_verifies_and_is_idempotent(world):
    res = _manifest(world)
    vault = world.tmp / "vault"
    out = _push(world, str(vault), tier=1)
    tier1 = _files(res, 1)
    assert out.pushed == len(tier1) and out.skipped == 0 and out.verified == len(tier1)
    assert out.failed == [] and out.bytes == sum(f.bytes for f in tier1) and out.forgotten is None
    assert (vault / "configs" / "datasets" / TEST / "submit.yaml").is_file()
    assert (vault / "data" / "runs" / "good" / "run.yaml").is_file()
    assert not (vault / "data" / "runs" / "good" / "predictions").exists()  # tier 2 not yet
    assert not (vault / "data" / "work").exists()  # never a checkpoint at tier 1
    again = _push(world, str(vault), tier=2)
    tier2 = _files(res, 2)
    assert again.pushed == len(tier2) and again.skipped == len(tier1)
    third = _push(world, str(vault), tier=2)
    assert third.pushed == 0 and third.skipped == len(tier1) + len(tier2) and third.failed == []
    rows = _ledger(world).of("push", "m1")
    assert [r.tier for r in rows] == [1, 2, 2] and rows[-1].pushed == 0 and rows[-1].bytes == 0
    _push(world, str(vault), tier=3)
    assert (vault / "data" / "work" / "good" / "weights" / "last.pt").is_file()
    assert not (vault / "data" / "work" / "good" / "weights" / "best.pt").exists()  # remote_copy


def test_push_refuses_a_stale_manifest_before_moving_anything(world):
    _manifest(world)
    vault = world.tmp / "vault"
    card = world.roots.data / "runs" / "good" / "run.yaml"
    card.write_bytes(card.read_bytes() + b"# touched\n")
    with pytest.raises(IntegrityError, match="drift") as ei:
        _push(world, str(vault), tier=1)
    assert ei.value.fields == {"file": "data/runs/good/run.yaml"} and not vault.exists()
    card.unlink()
    with pytest.raises(ValidationFailed, match="not_found") as ei:
        _push(world, str(vault), tier=1)
    assert ei.value.fields == {"file": "data/runs/good/run.yaml"} and not vault.exists()
    assert _ledger(world).of("push") == []
    with pytest.raises(ValidationFailed, match="tier") as ei:
        _push(world, str(vault), tier=4)
    assert ei.value.fields == {"tier": 4}
    with pytest.raises(ValidationFailed, match="not_found"):
        push(TEST, "m9", str(vault), **_kw(world))


def test_push_rclone_reports_mismatch_and_records_before_raising(world):
    res = _manifest(world)
    remote = FakeRemote(corrupt="submit.yaml")
    with pytest.raises(IntegrityError, match="mismatch") as ei:
        _push(world, "fake:vault", tier=1, runner=remote)
    n = len(_files(res, 1))
    assert ei.value.fields == {"pushed": n, "skipped": 0, "verified": n - 1, "failed": 1}
    row = _ledger(world).latest("push", "m1")
    assert row.failed == [f"configs/datasets/{TEST}/submit.yaml"] and row.pushed == n
    assert remote.deleted == []
    assert len([c for c in remote.calls if c[1] == "hashsum"]) == 4  # 2 roots x before/after
    assert SECRET not in str(ei.value)


def test_push_rclone_failure_mid_way_still_writes_the_row(world):
    res = _manifest(world)
    remote = FakeRemote(fail="copyto")
    with pytest.raises(PlatformError, match="copyto failed") as ei:
        _push(world, "fake:vault", tier=1, runner=remote)
    n = len(_files(res, 1))
    assert ei.value.fields["pushed"] == 0 and ei.value.fields["failed"] == n
    assert ei.value.fields["exit_code"] == 1 and SECRET not in str(ei.value)
    row = _ledger(world).latest("push", "m1")
    assert row.pushed == 0 and row.verified == 0 and len(row.failed) == n


def test_forget_remote_only_after_everything_verified(world):
    _manifest(world)
    with pytest.raises(ValidationFailed, match="forget_refused"):
        _push(world, str(world.tmp / "vault"), tier=1, forget_remote=True)
    assert not (world.tmp / "vault").exists() and _ledger(world).of("push") == []
    remote = FakeRemote(corrupt="submit.yaml")
    with pytest.raises(IntegrityError):
        _push(world, "fake:vault", tier=1, forget_remote=True, runner=remote)
    assert remote.deleted == []
    good = FakeRemote()
    out = _push(world, "fake:vault", tier=1, forget_remote=True, runner=good)
    assert out.forgotten == "fake" and good.deleted == ["fake"]
    rows = _ledger(world).rows
    assert [r.event for r in rows[-2:]] == ["push", "remote_forgotten"] and rows[-1].remote == "fake"
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/unit/backup/test_dest_push.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: vcp.backup.dest`）

- [ ] **Step 4: 寫 `src/vcp/backup/dest.py`**

```python
"""Where copies live (spec 5, 6.1): an rclone remote or a local directory behind one small
interface, so push / verify / pull never branch on the kind again. rclone is shelled out through
an injectable runner and every byte it prints is redacted before it can reach a message
(spec 9)."""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from vcp.core.errors import PlatformError, VcpError
from vcp.core.hashing import sha256_file
from vcp.core.proc import Runner, default_runner, last_line
from vcp.train.upload import dest_kind

# The rclone command prefix; the end-to-end test points it at a stand-in script.
RCLONE: list[str] = ["rclone"]
ConfState = Literal["present", "absent", "unknown"]


def _failed(what: str, proc: Any) -> PlatformError:
    detail = last_line(proc.stderr or proc.stdout or "")
    return PlatformError(
        f"rclone {what} failed (exit {proc.returncode}): {detail}",
        fields={"exit_code": proc.returncode},
    )


@dataclass(frozen=True)
class LocalDest:
    """A directory on this machine or a mounted drive; copies are read back to verify."""

    dest: str
    kind: Literal["local"] = "local"

    def _target(self, sub: str, rel: str) -> Path:
        return Path(self.dest) / sub / rel

    def hashes(self, sub: str, rels: Iterable[str]) -> dict[str, str]:
        """{relative path: sha256} of the listed files that exist under ``<dest>/<sub>``."""
        out: dict[str, str] = {}
        for rel in rels:
            p = self._target(sub, rel)
            if p.is_file():
                out[rel] = sha256_file(p)
        return out

    def put(self, src: Path, sub: str, rel: str) -> None:
        target = self._target(sub, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)

    def get(self, sub: str, rel: str, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._target(sub, rel), dst)


@dataclass(frozen=True)
class RcloneDest:
    """``remote:path``; hashes come from ``rclone hashsum sha256``, copies go through
    ``copyto --checksum``, and ``forget`` is ``rclone config delete <remote>``."""

    dest: str
    runner: Runner
    kind: Literal["rclone"] = "rclone"

    def _path(self, sub: str, rel: str = "") -> str:
        base = f"{self.dest.rstrip('/')}/{sub}"
        return f"{base}/{rel}" if rel else base

    def _run(self, *args: str) -> Any:
        return self.runner([*RCLONE, *args])

    def hashes(self, sub: str, rels: Iterable[str]) -> dict[str, str]:
        """``rclone hashsum sha256 <dest>/<sub>`` as {relative path: sha}, limited to ``rels``;
        an unlistable base (nothing there yet) is simply empty."""
        proc = self._run("hashsum", "sha256", self._path(sub))
        if proc.returncode != 0:
            return {}
        wanted = set(rels)
        out: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[1].strip() in wanted:
                out[parts[1].strip()] = parts[0]
        return out

    def put(self, src: Path, sub: str, rel: str) -> None:
        proc = self._run("copyto", str(src), self._path(sub, rel), "--checksum")
        if proc.returncode != 0:
            raise _failed("copyto", proc)

    def get(self, sub: str, rel: str, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        proc = self._run("copyto", self._path(sub, rel), str(dst))
        if proc.returncode != 0:
            raise _failed("copyto", proc)

    def forget(self) -> str:
        """``rclone config delete <remote>``: the credential is gone from this machine. vcp
        never reads what the config held."""
        remote = self.dest.split(":", 1)[0]
        proc = self._run("config", "delete", remote)
        if proc.returncode != 0:
            raise _failed("config delete", proc)
        return remote


Destination = LocalDest | RcloneDest


def _rclone_runner(runner: Runner | None, dest: str) -> Runner:
    if runner is not None:
        return runner
    if shutil.which(RCLONE[0]) is None:
        raise VcpError(
            f"rclone_not_found: {RCLONE[0]!r} is not on PATH; install it or use a local directory",
            fields={"dest": dest},
        )
    return default_runner


def open_dest(dest: str, runner: Runner | None = None) -> Destination:
    """``remote:path`` -> rclone (a Windows drive is local, as in the training layer)."""
    if dest_kind(dest) == "local":
        return LocalDest(dest)
    return RcloneDest(dest, _rclone_runner(runner, dest))


def rclone_conf_state(runner: Runner | None = None) -> ConfState:
    """Whether an rclone config file exists here -- judged by the path ``rclone config file``
    prints, never by reading the file (spec 9). ``unknown`` when rclone is not installed."""
    if runner is None:
        if shutil.which(RCLONE[0]) is None:
            return "unknown"
        runner = default_runner
    proc = runner([*RCLONE, "config", "file"])
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if proc.returncode != 0 or not lines:
        return "unknown"
    return "present" if Path(lines[-1]).is_file() else "absent"
```

- [ ] **Step 5: 寫 `src/vcp/backup/push.py`**

```python
"""``vcp backup push`` (spec 6.1): the manifest's present files, tier by tier, only those the
destination does not already hold; every copy verified against the manifest; the ledger row
written whatever happened; and only when nothing failed, the credential wiped."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import IntegrityError, PlatformError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.proc import Runner
from vcp.core.time import stamp
from vcp.backup.dest import RcloneDest, open_dest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest, local_path
from vcp.backup.schema import BackupRow, FileEntry

TIERS = (1, 2, 3)


@dataclass(frozen=True)
class PushResult:
    manifest_id: str
    dest: str
    tier: int
    pushed: int
    skipped: int
    verified: int
    failed: list[str]
    bytes: int
    forgotten: str | None = None


def check_tier(tier: int) -> None:
    """Range check in the function layer (never Click's), so a bad value still gets a VERDICT."""
    if tier not in TIERS:
        raise ValidationFailed(f"tier: must be 1, 2 or 3, got {tier}", fields={"tier": tier})


def _sources(entries: list[FileEntry], paths: DatasetPaths) -> dict[str, Path]:
    """Every file about to be pushed, still holding the bytes the manifest recorded. All checks
    happen before any byte moves: an evacuation must not half-run on a stale manifest."""
    out: dict[str, Path] = {}
    for e in entries:
        src = local_path(e, paths.data_root, paths.configs_root)
        if not src.is_file():
            raise ValidationFailed(
                f"not_found: {e.key} is listed as present but is gone locally; "
                "write a new manifest",
                fields={"file": e.key},
            )
        if sha256_file(src) != e.sha256:
            raise IntegrityError(
                f"drift: {e.key} changed since the manifest was written; write a new manifest",
                fields={"file": e.key},
            )
        out[e.key] = src
    return out


def push(
    dataset: str,
    manifest_id: str,
    dest: str,
    *,
    tier: int = 1,
    forget_remote: bool = False,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> PushResult:
    check_tier(tier)
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    manifest = load_manifest(paths, manifest_id)
    target = open_dest(dest, runner)
    if forget_remote and not isinstance(target, RcloneDest):
        raise ValidationFailed(
            "forget_refused: --forget-remote needs an rclone destination; a local directory "
            "holds no credential",
            fields={"dest": dest},
        )
    chosen = [f for f in manifest.files if f.kind == "file" and f.present and f.tier <= tier]
    sources = _sources(chosen, paths)
    roots = sorted({e.root for e in chosen})
    rels = {root: [e.path for e in chosen if e.root == root] for root in roots}
    before = {root: target.hashes(root, rels[root]) for root in roots}
    pushed = skipped = sent = verified = 0
    done: list[str] = []
    failed: list[str] = []
    failure: PlatformError | None = None
    try:
        for e in chosen:  # manifest order: tier 1 first
            if before[e.root].get(e.path) == e.sha256:
                skipped += 1
            else:
                target.put(sources[e.key], e.root, e.path)
                pushed += 1
                sent += e.bytes
            done.append(e.key)
        after = {root: target.hashes(root, rels[root]) for root in roots}
        failed = [e.key for e in chosen if after[e.root].get(e.path) != e.sha256]
        verified = len(chosen) - len(failed)
    except PlatformError as exc:
        failure = exc
        failed = [e.key for e in chosen if e.key not in done]
    ledger = BackupLedger(paths.backup_log)
    ledger.append(
        BackupRow(
            event="push",
            ts=stamp(),
            manifest_id=manifest_id,
            dest=dest,
            tier=tier,
            pushed=pushed,
            skipped=skipped,
            verified=verified,
            failed=failed,
            bytes=sent,
        )
    )
    counts = {"pushed": pushed, "skipped": skipped, "verified": verified, "failed": len(failed)}
    if failure is not None:
        failure.fields.update(counts)
        raise failure
    if failed:
        raise IntegrityError(
            f"mismatch: {len(failed)} file(s) not verified at {dest}: {', '.join(failed[:5])}",
            fields=counts,
        )
    forgotten: str | None = None
    if forget_remote and isinstance(target, RcloneDest):
        forgotten = target.forget()
        ledger.append(
            BackupRow(event="remote_forgotten", ts=stamp(), manifest_id=manifest_id, remote=forgotten)
        )
    return PushResult(manifest_id, dest, tier, pushed, skipped, verified, failed, sent, forgotten)
```

- [ ] **Step 6: 跑測試確認通過**

Run: `uv run pytest tests/unit/backup/test_dest_push.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 7: 寫失敗的 CLI 測試** — 追加到 `tests/unit/test_cli_backup.py`（import 區加 `from vcp.backup import dest as destmod`）

```python
def test_push_cli(world, monkeypatch):
    r = _run("manifest", "--dataset", "beach-test", "--conclusion", "submission:S1", "--id", "m1")
    assert r.exit_code == 0, r.output
    vault = world.tmp / "vault"
    common = ["push", "--dataset", "beach-test", "--manifest", "m1", "--dest", str(vault)]
    r = _run(*common, "--tier", "2", "--json")
    assert r.exit_code == 0, r.output
    doc = _json(r)
    assert doc["status"] == "OK" and doc["fields"]["failed"] == 0 and doc["fields"]["pushed"] > 0
    assert doc["fields"]["tier"] == 2 and "forgotten" not in doc["fields"]
    assert doc["result"]["failed"] == [] and doc["result"]["forgotten"] is None
    r = _run(*common, "--tier", "2")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "pushed=0" in v and f"skipped={doc['fields']['pushed']}" in v
    r = _run(*common, "--tier", "9")
    assert r.exit_code == 1 and "tier" in _verdict(r.output)
    r = _run(*common, "--forget-remote")
    assert r.exit_code == 1 and "forget_refused" in _verdict(r.output)
    monkeypatch.setattr(destmod.shutil, "which", lambda name, *a, **k: None)
    r = _run("push", "--dataset", "beach-test", "--manifest", "m1", "--dest", "gdrive:x")
    assert r.exit_code == 2 and "rclone_not_found" in _verdict(r.output)
```

- [ ] **Step 8: `src/vcp/cli_backup.py` 加 `push`**

import 區加 `from vcp.backup.push import push`。在 `manifest_cmd` 之後加：

```python
@backup_app.command("push")
def push_cmd(
    dataset: DatasetOpt,
    manifest_id: ManifestOpt,
    dest: Annotated[str, typer.Option("--dest", help="rclone remote:path or a local directory")],
    tier: Annotated[
        int, typer.Option("--tier", help="push tiers 1..N (1 decision, 2 reproduction, 3 weights)")
    ] = 1,
    forget_remote: Annotated[
        bool,
        typer.Option(
            "--forget-remote", help="after every copy verified: rclone config delete <remote>"
        ),
    ] = False,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Copy the manifest's files to a destination and verify every copy."""

    def fn() -> CmdResult:
        res = push(
            dataset,
            manifest_id,
            dest,
            tier=tier,
            forget_remote=forget_remote,
            data_root=data_root,
            configs_root=configs_root,
        )
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "manifest": manifest_id,
            "dest": dest,
            "tier": tier,
            "pushed": res.pushed,
            "skipped": res.skipped,
            "verified": res.verified,
            "failed": len(res.failed),
            "bytes": res.bytes,
        }
        human = [f"pushed {res.pushed}, skipped {res.skipped}, verified {res.verified} -> {dest}"]
        if res.forgotten:
            fields["forgotten"] = res.forgotten
            human.append(f"rclone remote {res.forgotten!r} forgotten")
        payload = {
            "pushed": res.pushed,
            "skipped": res.skipped,
            "verified": res.verified,
            "failed": res.failed,
            "bytes": res.bytes,
            "forgotten": res.forgotten,
        }
        return "OK", fields, payload, human

    run_command("backup.push", json_mode, data_root, fn)
```

- [ ] **Step 9: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/backup tests/unit/test_cli_backup.py -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/backup/dest.py src/vcp/backup/push.py src/vcp/cli_backup.py tests/backup_fixtures.py tests/unit/backup/test_dest_push.py tests/unit/test_cli_backup.py
git commit -m "feat(backup): 目的地抽象（本機 / rclone）、逐檔驗證的冪等 push、--forget-remote 與 vcp backup push" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `verify` 三層與 `vcp backup verify`

**Files:**
- Create: `src/vcp/backup/verify.py`
- Modify: `src/vcp/cli_backup.py`（加 `verify`）
- Test: `tests/unit/backup/test_verify.py`、`tests/unit/test_cli_backup.py`（追加）

**Interfaces:**
- Consumes: Task 5 `open_dest` / `Destination`、`push`（測試）；Task 2 `load_manifest` / `local_path` / `BackupLedger` / `BackupRow` / `LEDGER_ROLES` / `CARD_ROLES` / `Manifest` / `FileEntry`；`vcp.core.time.parse_stamp` / `stamp`；`vcp.core.config.load_yaml_model`；`vcp.measure.schema.RunCard`、`vcp.measure.runs.prediction_path`；`vcp.train.schema.TrainRecord`、`vcp.core.paths.resolve_stored_path`；`vcp.fuse.build.load_record`；`vcp.submit.schema.Staged`、`vcp.submit.ledger.SubmissionLedger`；`vcp.data.schema.DatasetCard`。
- Produces: `vcp.backup.verify`：`Drift(what, expected, actual)`、`VerifyResult(manifest_id, dest, copies, copy_problems, drift, bad_stamps)` 與屬性 `first_bad`、`problems`、`reason`（`mismatch` > `missing` > `drift` > `bad_stamps`，無問題 `None`）、`ok`；`verify(dataset, manifest_id, *, dest=None, runner=None, data_root=None, configs_root=None) -> VerifyResult`；`sha256_prefix(path, n)`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/backup/test_verify.py`

```python
import json

import pytest
from backup_fixtures import SECRET, FakeRemote
from submit_fixtures import EVAL, STAMP, TEST

from vcp.backup.evidence import build_manifest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest
from vcp.backup.push import push
from vcp.backup.verify import Drift, sha256_prefix, verify
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.measure.report import READINGS_LEDGER
from vcp.measure.runs import load_run, run_dir
from vcp.train.records import load_record, save_record
from vcp.train.schema import UploadRecord
from vcp.train.upload import merge_uploads


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _paths(world, name=TEST):
    return DatasetPaths.resolve(name, **_kw(world))


def _pred(world, run="good", subset="valB"):
    card = load_run(world.roots.data, run)
    return run_dir(world.roots.data, run) / card.predictions[subset].path


@pytest.fixture
def pushed(world):
    build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))
    vault = world.tmp / "vault"
    push(TEST, "m1", str(vault), tier=3, **_kw(world))
    return vault


def test_sha256_prefix(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"abcdef")
    (tmp_path / "g").write_bytes(b"abc")
    assert sha256_prefix(p, 3) == sha256_file(tmp_path / "g")
    assert sha256_prefix(p, 6) == sha256_file(p) and sha256_prefix(p, 99) == sha256_file(p)


def test_verify_clean_world(world, pushed):
    res = verify(TEST, "m1", dest=str(pushed), **_kw(world))
    assert res.ok and res.drift == [] and res.bad_stamps == [] and res.first_bad is None
    assert res.reason is None and res.problems == []
    n = len(load_manifest(_paths(world), "m1").files)
    assert res.copies == {"ok": n, "missing": 0, "mismatch": 0}  # the remote_copy verified in place
    row = BackupLedger(_paths(world).backup_log).latest("verify", "m1")
    assert row.copies == res.copies and row.drift == 0 and row.bad_stamps == 0
    assert row.dest == str(pushed) and row.first_bad is None
    offline = verify(TEST, "m1", **_kw(world))
    assert offline.ok and offline.copies is None and offline.copy_problems == []
    assert BackupLedger(_paths(world).backup_log).latest("verify", "m1").dest is None
    with pytest.raises(ValidationFailed, match="not_found"):
        verify(TEST, "nope", **_kw(world))


def test_verify_copies_missing_and_mismatch(world, pushed):
    (pushed / "data" / "runs" / "good" / "run.yaml").unlink()
    (pushed / "configs" / "datasets" / TEST / "submit.yaml").write_text("dataset: x\n", encoding="utf-8")
    (world.vault / "good" / "best.pt").write_bytes(b"corrupt")
    res = verify(TEST, "m1", dest=str(pushed), **_kw(world))
    assert res.copies["missing"] == 1 and res.copies["mismatch"] == 2 and not res.ok
    assert sorted(res.copy_problems) == [
        f"mismatch:configs/datasets/{TEST}/submit.yaml",
        "mismatch:data/work/good/weights/best.pt",
        "missing:data/runs/good/run.yaml",
    ]
    assert res.reason == "mismatch" and res.drift == [] and res.bad_stamps == []
    row = BackupLedger(_paths(world).backup_log).latest("verify", "m1")
    assert row.copies == {"ok": res.copies["ok"], "missing": 1, "mismatch": 2}


def test_verify_consistency_drift(world, pushed):
    card = load_run(world.roots.data, "good")
    pred = _pred(world)
    pred.write_bytes(pred.read_bytes() + b'{"sample_id": "zzz", "scores": {"a": 1.0}}\n')
    res = verify(TEST, "m1", **_kw(world))
    assert sorted(d.what for d in res.drift) == [
        "data/runs/good/predictions/valB.jsonl",
        "good/run.yaml:predictions.valB",
    ]
    d = next(d for d in res.drift if d.what == "good/run.yaml:predictions.valB")
    assert d == Drift(d.what, card.predictions["valB"].sha256, sha256_file(pred))
    assert res.reason == "drift" and res.bad_stamps == [] and res.copies is None
    stage = _paths(world).submission_dir("S1") / "stage.json"
    doc = json.loads(stage.read_text(encoding="utf-8"))
    doc["artifact"]["sha256"] = "f" * 64
    stage.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    res = verify(TEST, "m1", **_kw(world))
    whats = {d.what for d in res.drift}
    assert f"submit/{TEST}/S1/stage.json:artifact" in whats
    assert f"datasets/{TEST}/submissions.jsonl:staged.S1" in whats
    assert f"data/submit/{TEST}/S1/stage.json" in whats


def test_verify_stamps(world, pushed):
    readings = _paths(world, EVAL).measure_dir / READINGS_LEDGER
    lines = readings.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    grown = {**last, "reading_id": "f" * 64, "ts": "2999-01-01T00:00:00.000Z"}
    readings.write_text("\n".join([*lines, json.dumps(grown)]) + "\n", encoding="utf-8")
    res = verify(TEST, "m1", **_kw(world))
    assert res.ok  # an append-only ledger that only grew is not drift
    stale = {**grown, "ts": "2000-01-01T00:00:00.000Z"}
    readings.write_text(
        "\n".join([*lines, json.dumps(grown), json.dumps(stale), "not json"]) + "\n",
        encoding="utf-8",
    )
    res = verify(TEST, "m1", **_kw(world))
    n = len(lines)
    label = f"measure/{EVAL}/{READINGS_LEDGER}"
    assert res.drift == [] and res.bad_stamps == [f"{label}:{n + 2}", f"{label}:{n + 3}"]
    assert res.first_bad == f"{label}:{n + 2}" and res.reason == "bad_stamps" and not res.ok
    profile = _paths(world).submit_yaml
    profile.write_text(profile.read_text(encoding="utf-8").replace(STAMP, "yesterday"), encoding="utf-8")
    res = verify(TEST, "m1", **_kw(world))
    assert f"datasets/{TEST}/submit.yaml:created_at" in res.bad_stamps
    assert any(d.what == f"configs/datasets/{TEST}/submit.yaml" for d in res.drift)
    row = BackupLedger(_paths(world).backup_log).latest("verify", "m1")
    assert row.bad_stamps == len(res.bad_stamps) and row.first_bad == res.first_bad
    assert row.drift == len(res.drift)
    log = _paths(world).backup_log
    rows = log.read_text(encoding="utf-8").splitlines()
    bad = json.loads(rows[-1])
    bad["ts"] = "2000-01-01T00:00:00.000Z"
    log.write_text("\n".join([*rows[:-1], json.dumps(bad)]) + "\n", encoding="utf-8")
    res = verify(TEST, "m1", **_kw(world))
    assert f"backup.log.jsonl:{len(rows)}" in res.bad_stamps


def test_verify_remote_copy_on_rclone(world):
    rec = load_record(world.roots.data, "good")
    best = next(c for c in rec.checkpoints if c.final)
    rec = merge_uploads(
        rec,
        [
            UploadRecord(
                dest="fake:w",
                kind="rclone",
                name="best.pt",
                sha256=best.sha256,
                verified=True,
                uploaded_at="2026-09-06T00:00:00.000Z",
            )
        ],
    )
    save_record(world.roots.data, rec)
    build_manifest(EVAL, "run:good", manifest_id="mg", **_kw(world))
    m = load_manifest(_paths(world, EVAL), "mg")
    entry = next(f for f in m.files if f.kind == "remote_copy")
    assert entry.remote.dest == "fake:w"  # the newest verified upload wins
    remote = FakeRemote()
    remote.store["fake:w/good/best.pt"] = (world.weights / "best.pt").read_bytes()
    res = verify(EVAL, "mg", dest="fake:vault", runner=remote, **_kw(world))
    assert res.copies == {"ok": 1, "missing": len(m.files) - 1, "mismatch": 0}
    assert ["rclone", "hashsum", "sha256", "fake:w/good"] in remote.calls
    assert res.reason == "missing"
    assert SECRET not in json.dumps(res.copy_problems)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/backup/test_verify.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: vcp.backup.verify`）

- [ ] **Step 3: 寫 `src/vcp/backup/verify.py`**

```python
"""``vcp backup verify`` (spec 6.2): three layers, none of which needs the machine that wrote
the manifest. Copies: what a destination holds against the manifest. Consistency: the sha chains
between local files (cards -> predictions, records -> checkpoints, ledgers -> stage files, the
manifest -> everything). Timestamps: every ledger row's ``ts`` parses and never runs backwards;
every card's ``*_at`` / ``ts`` parses (``downloaded_at`` is a human date and is skipped)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from vcp.core.config import load_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, resolve_stored_path
from vcp.core.proc import Runner
from vcp.core.time import parse_stamp, stamp
from vcp.data.schema import DatasetCard
from vcp.fuse.build import load_record as load_fuse_record
from vcp.measure.runs import prediction_path
from vcp.measure.schema import RunCard
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.schema import Staged
from vcp.train.schema import TrainRecord
from vcp.backup.dest import Destination, open_dest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest, local_path
from vcp.backup.schema import CARD_ROLES, LEDGER_ROLES, BackupRow, Manifest

SKIP_KEYS = frozenset({"downloaded_at"})
REASONS = ("mismatch", "missing", "drift", "bad_stamps")
_CHUNK = 1 << 20
Adder = Callable[[str, str, str], None]


@dataclass(frozen=True)
class Drift:
    what: str
    expected: str
    actual: str


@dataclass(frozen=True)
class VerifyResult:
    manifest_id: str
    dest: str | None
    copies: dict[str, int] | None
    copy_problems: list[str]
    drift: list[Drift]
    bad_stamps: list[str]

    @property
    def first_bad(self) -> str | None:
        return self.bad_stamps[0] if self.bad_stamps else None

    @property
    def problems(self) -> list[str]:
        out = list(self.copy_problems)
        out += [f"drift:{d.what}" for d in self.drift]
        out += [f"bad_stamps:{b}" for b in self.bad_stamps]
        return out

    @property
    def reason(self) -> str | None:
        kinds = {p.split(":", 1)[0] for p in self.problems}
        return next((r for r in REASONS if r in kinds), None)

    @property
    def ok(self) -> bool:
        return not self.problems


def sha256_prefix(path: Path, n: int) -> str:
    """sha256 of the first ``n`` bytes: an append-only ledger that only grew still matches the
    manifest that hashed it shorter."""
    h = hashlib.sha256()
    left = n
    with path.open("rb") as f:
        while left > 0:
            chunk = f.read(min(_CHUNK, left))
            if not chunk:
                break
            h.update(chunk)
            left -= len(chunk)
    return h.hexdigest()


def _load_json_model[T: BaseModel](path: Path, model_cls: type[T]) -> T:
    try:
        return model_cls.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError) as e:
        raise ValidationFailed(f"bad {path.name}: {e}", location=str(path)) from e


# --- layer 1: copies -------------------------------------------------------------------------


def _check_copies(
    manifest: Manifest, dest: str, runner: Runner | None
) -> tuple[dict[str, int], list[str]]:
    counts = {"ok": 0, "missing": 0, "mismatch": 0}
    problems: list[str] = []
    dests: dict[str, Destination] = {dest: open_dest(dest, runner)}
    groups: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for e in manifest.files:
        if e.remote is None:
            groups.setdefault((dest, e.root), []).append((e.path, e.sha256, e.key))
        else:
            dests.setdefault(e.remote.dest, open_dest(e.remote.dest, runner))
            groups.setdefault((e.remote.dest, e.remote.run), []).append(
                (e.remote.name, e.sha256, e.key)
            )
    for (d, sub), items in groups.items():
        have = dests[d].hashes(sub, [rel for rel, _, _ in items])
        for rel, sha, key in items:
            got = have.get(rel)
            if got is None:
                counts["missing"] += 1
                problems.append(f"missing:{key}")
            elif got != sha:
                counts["mismatch"] += 1
                problems.append(f"mismatch:{key}")
            else:
                counts["ok"] += 1
    return counts, problems


# --- layer 2: local consistency --------------------------------------------------------------


def _run_card(local: Path, paths: DatasetPaths, add: Adder) -> None:
    card = load_yaml_model(local, RunCard)
    for subset, entry in card.predictions.items():
        p = local.parent / entry.path
        if p.is_file():
            add(f"{card.run_id}/run.yaml:predictions.{subset}", entry.sha256, sha256_file(p))


def _train_record(local: Path, paths: DatasetPaths, add: Adder) -> None:
    rec = load_yaml_model(local, TrainRecord)
    newest = {Path(c.path).name: c for c in rec.checkpoints}  # a --resume that changed bytes wins
    for name, c in newest.items():
        p = resolve_stored_path(c.path, paths.data_root)
        if p.is_file():
            add(f"{rec.run_id}/train.yaml:checkpoints.{name}", c.sha256, sha256_file(p))


def _fuse_record(local: Path, paths: DatasetPaths, add: Adder) -> None:
    rec = load_fuse_record(paths.data_root, local.parent.name)
    for subset, build in rec.subsets.items():
        out = prediction_path(paths.data_root, rec.run_id, subset)
        if out.is_file():
            add(f"{rec.run_id}/fuse.json:subsets.{subset}.output", build.output_sha256, sha256_file(out))
        for member, sha in build.member_sha256.items():
            p = prediction_path(paths.data_root, member, subset)
            if p.is_file():
                add(f"{rec.run_id}/fuse.json:subsets.{subset}.members.{member}", sha, sha256_file(p))


def _stage(local: Path, paths: DatasetPaths, add: Adder) -> None:
    staged = _load_json_model(local, Staged)
    if staged.artifact.kind == "file" and staged.artifact.sha256 and staged.artifact.path:
        p = local.parent / staged.artifact.path
        if p.is_file():
            what = f"submit/{staged.dataset}/{staged.submission_id}/stage.json:artifact"
            add(what, staged.artifact.sha256, sha256_file(p))


def _dataset_card(local: Path, paths: DatasetPaths, add: Adder) -> None:
    card = load_yaml_model(local, DatasetCard)
    dpaths = DatasetPaths.resolve(
        card.name, data_root=paths.data_root, configs_root=paths.configs_root
    )
    if dpaths.samples_jsonl.is_file():
        add(f"datasets/{card.name}/dataset.yaml:samples_hash", card.samples_hash, sha256_file(dpaths.samples_jsonl))


def _submissions_log(local: Path, paths: DatasetPaths, add: Adder) -> None:
    name = local.parent.name
    tpaths = DatasetPaths.resolve(name, data_root=paths.data_root, configs_root=paths.configs_root)
    for row in SubmissionLedger(local).of("staged"):
        sid = str(row.submission_id)
        sj = tpaths.submission_dir(sid) / "stage.json"
        if row.sha256 and sj.is_file():
            staged = _load_json_model(sj, Staged)
            add(f"datasets/{name}/submissions.jsonl:staged.{sid}", row.sha256, str(staged.artifact.sha256))


_CHECKERS: dict[str, Callable[[Path, DatasetPaths, Adder], None]] = {
    "run_card": _run_card,
    "train_record": _train_record,
    "fuse_record": _fuse_record,
    "stage": _stage,
    "dataset_card": _dataset_card,
    "submissions_log": _submissions_log,
}


def _check_consistency(manifest: Manifest, paths: DatasetPaths) -> list[Drift]:
    drift: list[Drift] = []

    def add(what: str, expected: str, actual: str) -> None:
        if expected != actual:
            drift.append(Drift(what, expected, actual))

    for e in manifest.files:
        if not e.present:
            continue
        local = local_path(e, paths.data_root, paths.configs_root)
        if not local.is_file():
            continue
        if e.role in LEDGER_ROLES and local.stat().st_size >= e.bytes:
            add(e.key, e.sha256, sha256_prefix(local, e.bytes))  # growth is not drift
        else:
            add(e.key, e.sha256, sha256_file(local))
        checker = _CHECKERS.get(e.role)
        if checker is not None:
            checker(local, paths, add)
    return drift


# --- layer 3: timestamps ---------------------------------------------------------------------


def _ledger_stamps(local: Path, label: str, bad: list[str]) -> None:
    prev = None
    with local.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                cur = parse_stamp(json.loads(line)["ts"])
            except (ValueError, KeyError, TypeError):
                bad.append(f"{label}:{lineno}")
                continue
            if prev is not None and cur < prev:
                bad.append(f"{label}:{lineno}")
            prev = cur


def _load_doc(local: Path) -> Any:
    try:
        text = local.read_text(encoding="utf-8")
        return json.loads(text) if local.suffix == ".json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValidationFailed(f"bad card: {e}", location=str(local)) from e


def _walk_stamps(obj: Any, label: str, bad: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k)
            if (key == "ts" or key.endswith("_at")) and key not in SKIP_KEYS and v is not None:
                try:
                    parse_stamp(v) if isinstance(v, str) else parse_stamp("")
                except ValueError:
                    bad.append(f"{label}:{key}")
            _walk_stamps(v, label, bad)
    elif isinstance(obj, list):
        for v in obj:
            _walk_stamps(v, label, bad)


def _check_stamps(manifest: Manifest, paths: DatasetPaths) -> list[str]:
    bad: list[str] = []
    for e in manifest.files:
        if not e.present:
            continue
        local = local_path(e, paths.data_root, paths.configs_root)
        if not local.is_file():
            continue
        if e.role in LEDGER_ROLES:
            _ledger_stamps(local, e.path, bad)
        elif e.role in CARD_ROLES:
            _walk_stamps(_load_doc(local), e.path, bad)
    if paths.backup_log.is_file():  # the audit's own ledger, never listed in a manifest
        _ledger_stamps(paths.backup_log, paths.backup_log.name, bad)
    return bad


def verify(
    dataset: str,
    manifest_id: str,
    *,
    dest: str | None = None,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> VerifyResult:
    """All three layers; the result is returned, not raised, so a VERDICT can carry every count
    and ``--json`` every detail. The ledger row is written before returning."""
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    manifest = load_manifest(paths, manifest_id)
    copies: dict[str, int] | None = None
    problems: list[str] = []
    if dest is not None:
        copies, problems = _check_copies(manifest, dest, runner)
    drift = _check_consistency(manifest, paths)
    bad = _check_stamps(manifest, paths)
    res = VerifyResult(manifest_id, dest, copies, problems, drift, bad)
    BackupLedger(paths.backup_log).append(
        BackupRow(
            event="verify",
            ts=stamp(),
            manifest_id=manifest_id,
            dest=dest,
            copies=copies,
            drift=len(drift),
            bad_stamps=len(bad),
            first_bad=res.first_bad,
        )
    )
    return res
```

`_walk_stamps` 裡 `parse_stamp(v) if isinstance(v, str) else parse_stamp("")`：非字串（例如 YAML 未加引號的時間被解成 `datetime`）一律當壞時戳——`parse_stamp("")` 保證拋 `ValueError`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/backup/test_verify.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: 寫失敗的 CLI 測試** — 追加到 `tests/unit/test_cli_backup.py`

```python
def test_verify_cli(world):
    r = _run("manifest", "--dataset", "beach-test", "--conclusion", "submission:S1", "--id", "m1")
    assert r.exit_code == 0, r.output
    vault = world.tmp / "vault"
    r = _run("push", "--dataset", "beach-test", "--manifest", "m1", "--dest", str(vault), "--tier", "3")
    assert r.exit_code == 0, r.output
    common = ["verify", "--dataset", "beach-test", "--manifest", "m1"]
    r = _run(*common, "--dest", str(vault))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v
    assert "missing=0" in v and "mismatch=0" in v and "drift=0" in v and "bad_stamps=0" in v
    r = _run(*common)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "ok=" not in v and "drift=0" in v
    (vault / "data" / "runs" / "good" / "run.yaml").unlink()
    r = _run(*common, "--dest", str(vault), "--json")
    assert r.exit_code == 1
    doc = _json(r)
    assert doc["status"] == "FAIL" and doc["fields"]["reason"] == "missing"
    assert doc["fields"]["missing"] == 1 and doc["result"]["copy_problems"] == ["missing:data/runs/good/run.yaml"]
    assert doc["result"]["drift"] == [] and doc["result"]["bad_stamps"] == []
    r = _run("verify", "--dataset", "beach-test", "--manifest", "nope")
    assert r.exit_code == 1 and "not_found" in _verdict(r.output)
```

- [ ] **Step 6: `src/vcp/cli_backup.py` 加 `verify`**

import 區加 `from dataclasses import asdict` 與 `from vcp.backup.verify import verify`。在 `push_cmd` 之後加：

```python
@backup_app.command("verify")
def verify_cmd(
    dataset: DatasetOpt,
    manifest_id: ManifestOpt,
    dest: Annotated[
        str | None, typer.Option("--dest", help="also check the copies at this destination")
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Audit copies (with --dest), local consistency and timestamps."""

    def fn() -> CmdResult:
        res = verify(
            dataset, manifest_id, dest=dest, data_root=data_root, configs_root=configs_root
        )
        fields: dict[str, FieldValue] = {}
        if res.reason is not None:
            fields["reason"] = res.reason
        fields.update({"dataset": dataset, "manifest": manifest_id})
        if dest is not None:
            fields["dest"] = dest
        if res.copies is not None:
            fields.update(
                {
                    "ok": res.copies["ok"],
                    "missing": res.copies["missing"],
                    "mismatch": res.copies["mismatch"],
                }
            )
        fields["drift"] = len(res.drift)
        fields["bad_stamps"] = len(res.bad_stamps)
        if res.first_bad is not None:
            fields["first_bad"] = res.first_bad
        status: Status = "OK" if res.ok else "FAIL"
        human = [f"copies: {res.copies}" if res.copies else "copies: not checked (no --dest)"]
        human += res.copy_problems
        human += [
            f"drift: {d.what} expected {d.expected[:12]} actual {d.actual[:12]}" for d in res.drift
        ]
        human += [f"bad stamp: {b}" for b in res.bad_stamps]
        payload = {
            "copies": res.copies,
            "copy_problems": res.copy_problems,
            "drift": [asdict(d) for d in res.drift],
            "bad_stamps": res.bad_stamps,
        }
        return status, fields, payload, human

    run_command("backup.verify", json_mode, data_root, fn)
```

- [ ] **Step 7: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/backup tests/unit/test_cli_backup.py -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/backup/verify.py src/vcp/cli_backup.py tests/unit/backup/test_verify.py tests/unit/test_cli_backup.py
git commit -m "feat(backup): verify 三層（副本 / 本機一致性 / 時戳）與 vcp backup verify" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---
### Task 7: `pull`、`status`、`vcp backup pull` / `status`

**Files:**
- Create: `src/vcp/backup/pull.py`、`src/vcp/backup/status.py`
- Modify: `src/vcp/cli_backup.py`（加 `pull`、`status`）
- Test: `tests/unit/backup/test_pull_status.py`、`tests/unit/test_cli_backup.py`（追加）

**Interfaces:**
- Consumes: Task 5 `open_dest` / `Destination` / `rclone_conf_state` / `check_tier` / `push`（測試）；Task 6 `verify`（測試）；Task 2 `load_manifest` / `local_path` / `BackupLedger` / `BackupRow`；`vcp.core.time.utc_now` / `stamp`；`vcp.core.hashing.sha256_file`。
- Produces: `vcp.backup.pull`：`PullResult(manifest_id, dest, tier, pulled, skipped, conflicts, missing, mismatch)`、`pull(dataset, manifest_id, dest, *, tier=3, overwrite=False, runner=None, data_root=None, configs_root=None) -> PullResult`。`vcp.backup.status`：`ManifestStatus(manifest_id, conclusion, files, created, last_push, last_verify, pushed_tiers, verified)` + `.unpushed_tiers`、`StatusView(dataset, manifests, rclone_conf)` + `.unverified`、`passed(row) -> bool`、`status(dataset, *, runner=None, data_root=None, configs_root=None) -> StatusView`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/backup/test_pull_status.py`

```python
import pytest
from backup_fixtures import FakeRemote
from submit_fixtures import TEST

from vcp.backup import dest as destmod
from vcp.backup.evidence import build_manifest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest
from vcp.backup.pull import pull
from vcp.backup.push import push
from vcp.backup.status import passed, status
from vcp.backup.verify import verify
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.measure.runs import load_run, run_dir


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _paths(world):
    return DatasetPaths.resolve(TEST, **_kw(world))


def _pred(world, subset="valB"):
    card = load_run(world.roots.data, "good")
    return run_dir(world.roots.data, "good") / card.predictions[subset].path


def _ready(world, tier=3):
    build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))
    vault = world.tmp / "vault"
    push(TEST, "m1", str(vault), tier=tier, **_kw(world))
    return vault, len(load_manifest(_paths(world), "m1").files)


def test_pull_restores_missing_files_and_verifies(world):
    vault, n = _ready(world)
    pred = _pred(world)
    original = pred.read_bytes()
    pred.unlink()
    (world.weights / "best.pt").unlink()  # the remote_copy comes back from vault-train
    res = pull(TEST, "m1", str(vault), **_kw(world))
    assert res.pulled == 2 and res.skipped == n - 2
    assert res.conflicts == [] and res.missing == [] and res.mismatch == []
    assert pred.read_bytes() == original
    assert (world.weights / "best.pt").read_bytes() == b"best weights"
    row = BackupLedger(_paths(world).backup_log).latest("pull", "m1")
    assert row.pulled == 2 and row.tier == 3 and row.conflicts == [] and row.dest == str(vault)
    again = pull(TEST, "m1", str(vault), **_kw(world))
    assert again.pulled == 0 and again.skipped == n
    with pytest.raises(ValidationFailed, match="tier"):
        pull(TEST, "m1", str(vault), tier=0, **_kw(world))
    with pytest.raises(ValidationFailed, match="not_found"):
        pull(TEST, "m9", str(vault), **_kw(world))


def test_pull_conflicts_overwrite_and_missing(world):
    vault, n = _ready(world, tier=2)
    pred = _pred(world)
    original = pred.read_bytes()
    pred.write_bytes(b"edited locally\n")
    with pytest.raises(ValidationFailed, match="conflict") as ei:
        pull(TEST, "m1", str(vault), tier=2, **_kw(world))
    assert ei.value.fields["conflicts"] == 1 and pred.read_bytes() == b"edited locally\n"
    row = BackupLedger(_paths(world).backup_log).latest("pull", "m1")
    assert row.conflicts == ["data/runs/good/predictions/valB.jsonl"] and row.pulled == 0
    res = pull(TEST, "m1", str(vault), tier=2, overwrite=True, **_kw(world))
    assert res.pulled == 1 and res.conflicts == [] and pred.read_bytes() == original
    baks = list(pred.parent.glob("valB.jsonl.bak-*"))
    assert len(baks) == 1 and baks[0].read_bytes() == b"edited locally\n"
    (world.weights / "last.pt").unlink()  # tier 3 was never pushed to the vault
    with pytest.raises(IntegrityError, match="missing") as ei:
        pull(TEST, "m1", str(vault), tier=3, **_kw(world))
    assert ei.value.fields["missing"] == 1 and not (world.weights / "last.pt").exists()
    assert BackupLedger(_paths(world).backup_log).latest("pull", "m1").missing == 1


def test_pull_rejects_corrupt_copies_without_keeping_them(world):
    vault, _ = _ready(world, tier=2)
    pred = _pred(world)
    original = pred.read_bytes()
    pred.unlink()
    copy = vault / "data" / "runs" / "good" / "predictions" / "valB.jsonl"
    copy.write_bytes(b"corrupt")
    with pytest.raises(IntegrityError, match="mismatch") as ei:
        pull(TEST, "m1", str(vault), tier=2, **_kw(world))
    assert not pred.exists() and ei.value.fields["mismatch"] == 1
    liar = FakeRemote(deliver=b"garbage")  # honest hashsum, dishonest copyto
    pred.write_bytes(original)
    push(TEST, "m1", "fake:vault", tier=2, runner=liar, **_kw(world))
    pred.unlink()
    with pytest.raises(IntegrityError, match="mismatch") as ei:
        pull(TEST, "m1", "fake:vault", tier=2, runner=liar, **_kw(world))
    assert not pred.exists() and ei.value.fields["mismatch"] == 1


def test_status_view(world, monkeypatch):
    conf = world.tmp / "rclone.conf"
    view = status(TEST, runner=FakeRemote(conf=conf), **_kw(world))
    assert view.manifests == [] and view.rclone_conf == "absent" and view.unverified == []
    build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))
    view = status(TEST, runner=FakeRemote(conf=conf), **_kw(world))
    m = view.manifests[0]
    assert m.manifest_id == "m1" and m.conclusion == "submission:S1" and m.files > 10
    assert m.pushed_tiers == [] and m.unpushed_tiers == [1, 2, 3] and not m.verified
    assert m.last_push is None and m.last_verify is None and view.unverified == ["m1"]
    vault = world.tmp / "vault"
    push(TEST, "m1", str(vault), tier=2, **_kw(world))
    verify(TEST, "m1", dest=str(vault), **_kw(world))
    conf.write_text("[gdrive]\n", encoding="utf-8")
    view = status(TEST, runner=FakeRemote(conf=conf), **_kw(world))
    m = view.manifests[0]
    assert m.pushed_tiers == [1, 2] and m.unpushed_tiers == [3] and m.verified
    assert m.last_push.tier == 2 and m.last_verify.drift == 0 and view.unverified == []
    assert view.rclone_conf == "present"
    monkeypatch.setattr(destmod.shutil, "which", lambda name, *a, **k: None)
    assert status(TEST, **_kw(world)).rclone_conf == "unknown"
    rows = BackupLedger(_paths(world).backup_log).of("verify", "m1")
    assert passed(rows[-1])
    bad = rows[-1].model_copy(update={"copies": {"ok": 1, "missing": 1, "mismatch": 0}})
    assert not passed(bad) and not passed(rows[-1].model_copy(update={"bad_stamps": 1}))
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/backup/test_pull_status.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: vcp.backup.pull`）

- [ ] **Step 3: 寫 `src/vcp/backup/pull.py`**

```python
"""``vcp backup pull``: rebuild the manifest's files on this machine from a destination, each
read back against the manifest's sha. A local file that differs is a conflict, never silently
replaced; a fetched copy that does not match is deleted, never kept."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import IntegrityError, PlatformError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.proc import Runner
from vcp.core.time import stamp, utc_now
from vcp.backup.dest import Destination, open_dest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest, local_path
from vcp.backup.push import check_tier
from vcp.backup.schema import BackupRow


@dataclass(frozen=True)
class PullResult:
    manifest_id: str
    dest: str
    tier: int
    pulled: int
    skipped: int
    conflicts: list[str]
    missing: list[str]
    mismatch: list[str]


def _backup_name(local: Path) -> Path:
    return local.with_name(f"{local.name}.bak-{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}")


def pull(
    dataset: str,
    manifest_id: str,
    dest: str,
    *,
    tier: int = 3,
    overwrite: bool = False,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> PullResult:
    check_tier(tier)
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    manifest = load_manifest(paths, manifest_id)
    dests: dict[str, Destination] = {dest: open_dest(dest, runner)}
    chosen = [f for f in manifest.files if f.tier <= tier]
    sources: dict[str, tuple[str, str, str]] = {}  # key -> (dest, sub, rel)
    for e in chosen:
        if e.remote is None:
            sources[e.key] = (dest, e.root, e.path)
        else:
            dests.setdefault(e.remote.dest, open_dest(e.remote.dest, runner))
            sources[e.key] = (e.remote.dest, e.remote.run, e.remote.name)
    groups: dict[tuple[str, str], list[str]] = {}
    for d, sub, rel in sources.values():
        groups.setdefault((d, sub), []).append(rel)
    have = {(d, sub): dests[d].hashes(sub, rels) for (d, sub), rels in groups.items()}
    pulled = skipped = 0
    conflicts: list[str] = []
    missing: list[str] = []
    mismatch: list[str] = []
    failure: PlatformError | None = None
    try:
        for e in chosen:
            d, sub, rel = sources[e.key]
            local = local_path(e, paths.data_root, paths.configs_root)
            exists = local.is_file()
            if exists and sha256_file(local) == e.sha256:
                skipped += 1
                continue
            got = have[(d, sub)].get(rel)
            if got is None:
                missing.append(e.key)
                continue
            if got != e.sha256:
                mismatch.append(e.key)
                continue
            if exists:
                if not overwrite:
                    conflicts.append(e.key)
                    continue
                local.replace(_backup_name(local))
            dests[d].get(sub, rel, local)
            if sha256_file(local) != e.sha256:
                local.unlink()  # never leave bytes the manifest does not vouch for
                mismatch.append(e.key)
                continue
            pulled += 1
    except PlatformError as exc:
        failure = exc
    BackupLedger(paths.backup_log).append(
        BackupRow(
            event="pull",
            ts=stamp(),
            manifest_id=manifest_id,
            dest=dest,
            tier=tier,
            pulled=pulled,
            skipped=skipped,
            conflicts=conflicts,
            missing=len(missing),
            failed=mismatch,
        )
    )
    counts = {
        "pulled": pulled,
        "skipped": skipped,
        "conflicts": len(conflicts),
        "missing": len(missing),
        "mismatch": len(mismatch),
    }
    if failure is not None:
        failure.fields.update(counts)
        raise failure
    if mismatch:
        raise IntegrityError(
            f"mismatch: {len(mismatch)} copy(ies) at {dest} do not match the manifest: "
            f"{', '.join(mismatch[:5])}",
            fields=counts,
        )
    if missing:
        raise IntegrityError(
            f"missing: {len(missing)} file(s) not at {dest}: {', '.join(missing[:5])}",
            fields=counts,
        )
    if conflicts:
        raise ValidationFailed(
            f"conflict: {len(conflicts)} local file(s) differ from the manifest (use --overwrite "
            f"to replace them, the old bytes are kept as .bak-<stamp>): {', '.join(conflicts[:5])}",
            fields=counts,
        )
    return PullResult(manifest_id, dest, tier, pulled, skipped, conflicts, missing, mismatch)
```

- [ ] **Step 4: 寫 `src/vcp/backup/status.py`**

```python
"""``vcp backup status`` (read-only): each manifest's last push and verify, the tiers never
pushed, and whether an rclone config file is still on this machine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.core.paths import DatasetPaths
from vcp.core.proc import Runner
from vcp.backup.dest import rclone_conf_state
from vcp.backup.ledger import BackupLedger
from vcp.backup.push import TIERS
from vcp.backup.schema import BackupRow


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

    @property
    def unpushed_tiers(self) -> list[int]:
        return [t for t in TIERS if t not in self.pushed_tiers]


@dataclass(frozen=True)
class StatusView:
    dataset: str
    manifests: list[ManifestStatus]
    rclone_conf: str

    @property
    def unverified(self) -> list[str]:
        return [m.manifest_id for m in self.manifests if not m.verified]


def passed(row: BackupRow) -> bool:
    """A verify row with nothing wrong in any layer it ran."""
    copies = row.copies or {}
    return (
        copies.get("missing", 0) == 0
        and copies.get("mismatch", 0) == 0
        and not row.drift
        and not row.bad_stamps
    )


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
                verified=any(passed(v) for v in verifies),
            )
        )
    return StatusView(dataset, out, rclone_conf_state(runner))
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/unit/backup/test_pull_status.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 6: 寫失敗的 CLI 測試** — 追加到 `tests/unit/test_cli_backup.py`（import 區加 `from vcp.measure.runs import load_run, run_dir`）

```python
def test_pull_and_status_cli(world, monkeypatch):
    monkeypatch.setattr(destmod.shutil, "which", lambda name, *a, **k: None)  # no rclone here
    r = _run("status", "--dataset", "beach-test")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "manifests=0" in v
    assert "rclone_conf=unknown" in v
    r = _run("manifest", "--dataset", "beach-test", "--conclusion", "submission:S1", "--id", "m1")
    assert r.exit_code == 0, r.output
    vault = world.tmp / "vault"
    r = _run("push", "--dataset", "beach-test", "--manifest", "m1", "--dest", str(vault), "--tier", "2")
    assert r.exit_code == 0, r.output
    r = _run("status", "--dataset", "beach-test", "--json")
    doc = _json(r)
    assert doc["status"] == "WARN" and doc["fields"]["unverified"] == 1
    assert doc["result"]["manifests"][0]["unpushed_tiers"] == [3]
    assert doc["result"]["manifests"][0]["last_push"]["tier"] == 2
    r = _run("verify", "--dataset", "beach-test", "--manifest", "m1", "--dest", str(vault))
    assert r.exit_code == 0, r.output
    r = _run("status", "--dataset", "beach-test")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and "unverified=0" in v and "m1" in r.output
    card = load_run(world.roots.data, "good")
    pred = run_dir(world.roots.data, "good") / card.predictions["valB"].path
    pred.unlink()
    common = ["pull", "--dataset", "beach-test", "--manifest", "m1", "--dest", str(vault)]
    r = _run(*common, "--tier", "2")
    assert r.exit_code == 0 and "pulled=1" in _verdict(r.output) and pred.is_file()
    pred.write_bytes(b"edited\n")
    r = _run(*common, "--tier", "2")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "conflict" in v and "conflicts=1" in v
    r = _run(*common, "--tier", "2", "--overwrite", "--json")
    assert r.exit_code == 0
    doc = _json(r)
    assert doc["fields"]["pulled"] == 1 and doc["result"]["conflicts"] == []
    r = _run(*common, "--tier", "3")
    assert r.exit_code == 1 and "missing" in _verdict(r.output)  # tier 3 never pushed
```

- [ ] **Step 7: `src/vcp/cli_backup.py` 加 `pull` 與 `status`**

import 區加 `from vcp.backup.pull import pull` 與 `from vcp.backup.status import status as status_view`。在 `verify_cmd` 之後加：

```python
@backup_app.command("pull")
def pull_cmd(
    dataset: DatasetOpt,
    manifest_id: ManifestOpt,
    dest: Annotated[str, typer.Option("--dest", help="rclone remote:path or a local directory")],
    tier: Annotated[int, typer.Option("--tier", help="pull tiers 1..N")] = 3,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="replace differing local files (old kept as .bak)")
    ] = False,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Bring the manifest's files back from a destination, verifying each one."""

    def fn() -> CmdResult:
        res = pull(
            dataset,
            manifest_id,
            dest,
            tier=tier,
            overwrite=overwrite,
            data_root=data_root,
            configs_root=configs_root,
        )
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "manifest": manifest_id,
            "dest": dest,
            "tier": tier,
            "pulled": res.pulled,
            "skipped": res.skipped,
            "conflicts": len(res.conflicts),
        }
        human = [f"pulled {res.pulled}, skipped {res.skipped} <- {dest}"]
        payload = {"pulled": res.pulled, "skipped": res.skipped, "conflicts": res.conflicts}
        return "OK", fields, payload, human

    run_command("backup.pull", json_mode, data_root, fn)


@backup_app.command("status")
def status_cmd(
    dataset: DatasetOpt,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Every manifest's last push / verify, unpushed tiers, and whether rclone still has a config."""

    def fn() -> CmdResult:
        view = status_view(dataset, data_root=data_root, configs_root=configs_root)
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "manifests": len(view.manifests),
            "unverified": len(view.unverified),
            "rclone_conf": view.rclone_conf,
        }
        notes: list[str] = []
        if not view.manifests:
            notes.append("no manifests yet: run `vcp backup manifest`")
        if view.unverified:
            notes.append(f"never verified: {', '.join(view.unverified)}")
        if view.rclone_conf == "present":
            notes.append("an rclone config file is still on this machine (push --forget-remote)")
        status: Status = "WARN" if notes else "OK"
        human = [
            f"{m.manifest_id}  {m.conclusion}  files={m.files}  "
            f"pushed_tiers={','.join(map(str, m.pushed_tiers)) or '-'}  "
            f"last_push={m.last_push.ts if m.last_push else '-'}  "
            f"last_verify={m.last_verify.ts if m.last_verify else '-'}  verified={m.verified}"
            for m in view.manifests
        ] + notes
        payload = {
            "manifests": [
                {
                    "manifest_id": m.manifest_id,
                    "conclusion": m.conclusion,
                    "files": m.files,
                    "created": m.created,
                    "pushed_tiers": m.pushed_tiers,
                    "unpushed_tiers": m.unpushed_tiers,
                    "last_push": m.last_push.model_dump(exclude_none=True) if m.last_push else None,
                    "last_verify": (
                        m.last_verify.model_dump(exclude_none=True) if m.last_verify else None
                    ),
                    "verified": m.verified,
                }
                for m in view.manifests
            ],
            "rclone_conf": view.rclone_conf,
        }
        return status, fields, payload, human

    run_command("backup.status", json_mode, data_root, fn)
```

- [ ] **Step 8: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/backup tests/unit/test_cli_backup.py -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/backup/pull.py src/vcp/backup/status.py src/vcp/cli_backup.py tests/unit/backup/test_pull_status.py tests/unit/test_cli_backup.py
git commit -m "feat(backup): pull（讀回驗證、衝突不覆蓋、.bak 留舊檔）、status（每份清單的推送 / 驗證與 rclone 設定檔存在與否）與兩個 CLI 命令" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: 端到端、真資料、文件

**Files:**
- Create: `tests/unit/test_e2e_backup.py`、`tests/integration/test_rsna_knee_backup.py`
- Modify: `README.md`（「提交治理命令」一節之後、「匯入器與 `rows_read` 的語意」之前插入新節）、`CLAUDE.md`（路徑一條、常用命令一條）

**Interfaces:**
- Consumes: 全部 `vcp backup` 命令；`backup_fixtures.make_world` / `SECRET`；`vcp.backup.dest.RCLONE`（monkeypatch 成假 rclone 腳本）；`vcp.backup.evidence.Collector`（真資料只列不寫）；`tests/integration/conftest.py` 的 `real_roots` / `load_real`。
- Produces: 兩個測試檔與文件；不新增介面。

- [ ] **Step 1: 寫端到端測試** `tests/unit/test_e2e_backup.py`

```python
# ruff: noqa: E501
"""The whole backup layer through the CLI (spec 11): the manual-platform story to `final`, a
manifest of the final submission, push / verify / pull against a local vault, a timestamp
tamper, then the same manifest against a fake rclone (a real subprocess that leaks a secret on
every call) with --forget-remote and a privacy scan."""

import json
import sys

import pytest
from backup_fixtures import SECRET, make_world
from typer.testing import CliRunner

from vcp.backup import dest as destmod
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest
from vcp.cli import app
from vcp.core.paths import DatasetPaths
from vcp.core.time import utc_now
from vcp.measure.report import READINGS_LEDGER
from vcp.measure.runs import load_run, run_dir

runner = CliRunner()

FAKE_RCLONE = """
import hashlib, os, re, shutil, sys
from pathlib import Path
store = Path(os.environ["FAKE_RCLONE_STORE"])
args = sys.argv[1:]
print("RCLONE_CONFIG_PASS=" + os.environ.get("RCLONE_CONFIG_PASS", ""), file=sys.stderr)


def is_remote(s):
    return re.match(r"^[A-Za-z0-9_-]+:", s) and not re.match(r"^[A-Za-z]:[\\\\/]", s)


def local(spec):
    remote, _, path = spec.partition(":")
    return store / remote / path


if args[:2] == ["hashsum", "sha256"]:
    base = local(args[2])
    if not base.is_dir():
        print("directory not found", file=sys.stderr)
        sys.exit(3)
    for p in sorted(base.rglob("*")):
        if p.is_file():
            corrupt = p.name == os.environ.get("FAKE_RCLONE_CORRUPT")
            digest = "0" * 64 if corrupt else hashlib.sha256(p.read_bytes()).hexdigest()
            print(f"{digest}  {p.relative_to(base).as_posix()}")
elif args[0] == "copyto":
    src = local(args[1]) if is_remote(args[1]) else Path(args[1])
    dst = local(args[2]) if is_remote(args[2]) else Path(args[2])
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
elif args[:2] == ["config", "delete"]:
    (store / f"deleted-{args[2]}").write_text("", encoding="utf-8")
elif args[:2] == ["config", "file"]:
    print("Configuration file is stored at:")
    print(os.environ["FAKE_RCLONE_CONF"])
else:
    sys.exit(2)
"""


@pytest.fixture
def world(roots, tmp_path):
    return make_world(roots, tmp_path)


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _backup(*args):
    return runner.invoke(app, ["backup", *args])


def _paths(world, name):
    return DatasetPaths.resolve(name, data_root=world.roots.data, configs_root=world.roots.configs)


def _to_final(world) -> None:
    """record S1, unseal + measure holdout, final: the submission story's tail (Plan 6)."""
    at = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    r = runner.invoke(app, ["submit", "record", "--dataset", "beach-test", "--id", "S1", "--tz", "utc", "--at", at])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["eval", "measure", "--run", "good", "--metrics", "accuracy", "--subsets", "holdout", "--unseal", "--reason", "final pick"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["submit", "final", "--dataset", "beach-test"])
    assert r.exit_code == 0 and "chosen=S1" in _verdict(r.output), r.output


def _scan(world, outputs: list[str]) -> None:
    scanned = 0
    for root in (world.roots.data, world.roots.configs):
        for p in root.rglob("*"):
            if p.is_file():
                scanned += 1
                assert SECRET.encode() not in p.read_bytes(), p
    assert scanned > 0, "privacy scan found no files to inspect"
    for text in outputs:
        assert SECRET not in text


def test_local_vault_story(world):
    _to_final(world)
    r = _backup("manifest", "--dataset", "beach-test", "--conclusion", "submission:S1", "--id", "m1")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and "remote_copies=1" in v and "missing=0" in v
    manifest = load_manifest(_paths(world, "beach-test"), "m1")
    files = {f.role: f for f in manifest.files}
    assert {"submit_profile", "submissions_log", "stage", "artifact", "run_card", "prediction", "judgements", "prereg", "train_record", "checkpoint_final", "unseal_log"} <= set(files)
    assert sum(f.bytes for f in manifest.files if f.tier == 1) < 16 << 20
    vault = world.tmp / "vault"
    common = ["--dataset", "beach-test", "--manifest", "m1", "--dest", str(vault)]
    r = _backup("push", *common, "--tier", "1")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "failed=0" in v and "skipped=0" in v
    assert not (vault / "data" / "work").exists() and not list(vault.rglob("*.pt"))
    assert not (vault / "data" / "runs" / "good" / "predictions").exists()
    r = _backup("push", *common, "--tier", "2")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "failed=0" in v
    tier2 = sum(1 for f in manifest.files if f.tier == 2 and f.kind == "file")
    assert f"pushed={tier2}" in v
    r = _backup("push", *common, "--tier", "2")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "pushed=0" in v and "failed=0" in v
    ledger = BackupLedger(_paths(world, "beach-test").backup_log)
    assert [row.event for row in ledger.rows] == ["manifest", "push", "push", "push"]
    r = _backup("verify", *common)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and "missing=0" in v and "mismatch=0" in v
    assert "drift=0" in v and "bad_stamps=0" in v
    card = load_run(world.roots.data, "good")
    pred = run_dir(world.roots.data, "good") / card.predictions["valB"].path
    original = pred.read_bytes()
    pred.unlink()
    r = _backup("pull", *common, "--tier", "2")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "pulled=1" in v and pred.read_bytes() == original
    (vault / "data" / "runs" / "good" / "predictions" / "valA.jsonl").unlink()
    r = _backup("verify", *common)
    v = _verdict(r.output)
    assert r.exit_code == 1 and "reason=missing" in v and "missing=1" in v
    readings = _paths(world, "beach").measure_dir / READINGS_LEDGER
    lines = readings.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    lines[-1] = json.dumps({**last, "ts": "2000-01-01T00:00:00.000Z"})
    readings.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = _backup("verify", "--dataset", "beach-test", "--manifest", "m1")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "bad_stamps=1" in v and f"first_bad=measure/beach/{READINGS_LEDGER}:{len(lines)}" in v
    assert "drift=1" in v  # the edited row sits inside the bytes the manifest hashed
    r = _backup("status", "--dataset", "beach-test")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "manifests=1" in v and "unverified=0" in v  # one verify did pass


def test_fake_rclone_story(world, tmp_path, monkeypatch):
    script = tmp_path / "fake_rclone.py"
    script.write_text(FAKE_RCLONE, encoding="utf-8")
    store = tmp_path / "remote-store"
    conf = tmp_path / "rclone.conf"
    conf.write_text("[fake]\ntype = local\n", encoding="utf-8")
    monkeypatch.setattr(destmod, "RCLONE", [sys.executable, str(script)])
    monkeypatch.setenv("FAKE_RCLONE_STORE", str(store))
    monkeypatch.setenv("FAKE_RCLONE_CONF", str(conf))
    monkeypatch.setenv("RCLONE_CONFIG_PASS", SECRET)
    outputs: list[str] = []
    r = _backup("manifest", "--dataset", "beach-test", "--conclusion", "submission:S1", "--id", "r1")
    assert r.exit_code == 0, r.output
    r = _backup("status", "--dataset", "beach-test")
    outputs.append(r.output)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "rclone_conf=present" in v and "unverified=1" in v
    common = ["--dataset", "beach-test", "--manifest", "r1", "--dest", "fake:vault"]
    monkeypatch.setenv("FAKE_RCLONE_CORRUPT", "submit.yaml")
    r = _backup("push", *common, "--tier", "1", "--forget-remote")
    outputs.append(r.output)
    v = _verdict(r.output)
    assert r.exit_code == 1 and "mismatch" in v and "failed=1" in v
    assert not (store / "deleted-fake").exists()
    monkeypatch.delenv("FAKE_RCLONE_CORRUPT")
    r = _backup("push", *common, "--tier", "1", "--forget-remote")
    outputs.append(r.output)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "failed=0" in v and "forgotten=fake" in v
    assert (store / "deleted-fake").is_file()
    assert (store / "fake" / "vault" / "configs" / "datasets" / "beach-test" / "submit.yaml").is_file()
    assert not (store / "fake" / "vault" / "data" / "runs" / "good" / "predictions").exists()
    ledger = BackupLedger(_paths(world, "beach-test").backup_log)
    assert [row.event for row in ledger.rows[-3:]] == ["push", "push", "remote_forgotten"]
    assert ledger.rows[-1].remote == "fake" and ledger.rows[-3].failed == ["configs/datasets/beach-test/submit.yaml"]
    r = _backup("verify", *common)
    outputs.append(r.output)
    v = _verdict(r.output)
    manifest = load_manifest(_paths(world, "beach-test"), "r1")
    tier1 = sum(1 for f in manifest.files if f.tier == 1 and f.kind == "file")
    rest = sum(1 for f in manifest.files if f.tier > 1 and f.kind == "file")
    assert r.exit_code == 1 and f"ok={tier1 + 1}" in v and f"missing={rest}" in v  # +1: the remote_copy, verified in place
    profile = _paths(world, "beach-test").submit_yaml
    original = profile.read_bytes()
    profile.unlink()
    r = _backup("pull", *common, "--tier", "1")
    outputs.append(r.output)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "pulled=1" in v and profile.read_bytes() == original
    _scan(world, outputs)
```

- [ ] **Step 2: 跑端到端**

Run: `uv run pytest tests/unit/test_e2e_backup.py -o addopts="" -q`
Expected: PASS（兩個測試各約 10 秒——holdout 量測與判決是真 bootstrap；假 rclone 每次呼叫起一個 Python 子程序）

- [ ] **Step 3: 寫真資料測試** `tests/integration/test_rsna_knee_backup.py`

```python
"""The evidence graph of a real dataset (`all`): listed only -- nothing is written under either
real root, so the collector is used directly instead of `vcp backup manifest`."""

from __future__ import annotations

import pytest

from conftest import load_real
from vcp.backup.evidence import Collector
from vcp.core.paths import DatasetPaths

pytestmark = pytest.mark.realdata
NAME = "rsna-knee"
TIER1_LIMIT = 16 << 20


@pytest.fixture(scope="module")
def knee(real_roots):
    return load_real(NAME, real_roots)


def test_manifest_all_lists_without_writing(knee, real_roots):
    paths = DatasetPaths.resolve(NAME, data_root=real_roots.data, configs_root=real_roots.configs)
    log = paths.backup_log
    before = log.read_bytes() if log.is_file() else None
    col = Collector(paths.data_root, paths.configs_root)
    col.walk_all(paths)
    files = col.files_of()
    roles = {e.role for e in files}
    assert {"dataset_card", "samples"} <= roles
    assert all(e.present for e in files) and col.unlisted == [] and col.missing == []
    assert all(e.for_ == ["all"] for e in files)
    for e in files:
        assert not e.path.startswith("raw/") and "/cache/" not in f"/{e.path}/", e.path
    tiers = [e.tier for e in files]
    assert tiers == sorted(tiers)
    assert sum(e.bytes for e in files if e.tier == 1) < TIER1_LIMIT
    after = log.read_bytes() if log.is_file() else None
    assert after == before
```

Run: `uv run pytest tests/integration/test_rsna_knee_backup.py -o addopts="" -q -m realdata`
Expected: PASS（真資料在 `C:/vcp-data` 時）或 1 skipped

- [ ] **Step 4: README** — 在「### 一次提交」的程式碼區塊之後、「## 匯入器與 `rows_read` 的語意」之前插入：

````markdown
## 備份審計命令 `vcp backup`

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp backup manifest` | 從結論反向走證據圖，寫 `configs/datasets/<name>/backup/<id>.json`（進 git、寫一次不改）：每個檔的角色、tier、sha、大小、服務的結論 | `--dataset`、`--conclusion submission:<id>\|judgement:<prereg>\|run:<id>\|all`、`--id` |
| `vcp backup push` | 先小後大推到 rclone 遠端或本機目錄（`<dest>/data\|configs\|external/…`），只推目的地沒有或不同的檔，推完逐檔比對 sha；`train upload` 驗過的權重副本（`remote_copy`）不重推 | `--manifest`、`--dest`、`--tier 1\|2\|3`（累積到 N；預設 1）、`--forget-remote`（全數驗證通過後 `rclone config delete <remote>`） |
| `vcp backup verify` | 三層稽核：副本（給 `--dest` 才做）、本機一致性（卡 ↔ 預測檔、`train.yaml` ↔ checkpoint、`fuse.json` ↔ 成員、`stage.json` ↔ 候選檔、台帳 ↔ `stage.json`、清單 ↔ 現在的檔）、時戳（台帳逐列 `ts` 可解析且單調、卡的 `*_at` 可解析） | `--manifest`、`--dest` |
| `vcp backup pull` | 從目的地把清單裡的檔拉回原相對路徑、讀回驗 sha；本機已有且不同 → `conflict`，`--overwrite` 才蓋（舊檔留 `.bak-<時戳>`） | `--manifest`、`--dest`、`--tier`（預設 3）、`--overwrite` |
| `vcp backup status` | 每份清單最新的 push / verify、從未推過的 tier、`rclone_conf=present\|absent\|unknown`（唯讀） | |

tier 1 決策層（台帳、卡、判決、預登記、配方、快照、候選檔；KB 級）、tier 2 重現層（預測檔、樣本、`train/`、logs；MB 級）、tier 3 權重層（GB 級，只在要求時）。`cache/`、`raw/` 永不進清單。清單只含路徑、sha、大小、時間與 dest 字串；vcp 不讀 rclone 設定檔內容，rclone 的輸出經 redact 才落地，台帳 `backup.log.jsonl` 只增。台帳在清單之後長大不算漂移；被改或截短才算。

### 機器回收前的撤離順序

```bash
uv run vcp backup manifest --dataset D-test --conclusion submission:SUB34 --id sub34   # 最終發需要的一切
uv run vcp backup push --dataset D-test --manifest sub34 --dest gdrive:vcp/backup --tier 1   # 先救決策層
uv run vcp backup push --dataset D-test --manifest sub34 --dest gdrive:vcp/backup --tier 2   # 再救重現層
uv run vcp backup manifest --dataset D --conclusion all --id all-final                        # 有空再救全部
uv run vcp backup push --dataset D --manifest all-final --dest gdrive:vcp/backup --tier 3 --forget-remote
uv run vcp backup status --dataset D-test                                                    # rclone_conf=absent 才走
```

### 賽後重建

```bash
git pull                                                                # 清單與台帳跟著 configs/ 回來
uv run vcp backup pull --dataset D-test --manifest sub34 --dest gdrive:vcp/backup --tier 2
uv run vcp backup verify --dataset D-test --manifest sub34 --dest gdrive:vcp/backup
uv run vcp submit verify --dataset D-test --id SUB34                    # 位元級重現候選檔
```
````

- [ ] **Step 5: CLAUDE.md** — 「## 路徑」最後一條之後加一條、「## 常用命令」最後一條之後加一條：

```markdown
- `configs/datasets/<name>/backup/<manifest_id>.json` 是證據清單（從結論反向生成，寫一次不改，進 git），`backup.log.jsonl` 只增（manifest / push / verify / pull / remote_forgotten）。目的地佈局 `<dest>/data|configs|external/<相對路徑>`；`train upload` 驗過的權重副本記成 `remote_copy`，verify 到原地驗、不重推。`cache/`、`raw/` 永不進清單。
```

```markdown
- `uv run vcp backup manifest --dataset D --conclusion submission:ID|judgement:P|run:R|all [--id M]` / `uv run vcp backup push --dataset D --manifest M --dest DEST [--tier 1|2|3] [--forget-remote]`（先小後大、逐檔驗、冪等）/ `uv run vcp backup verify --dataset D --manifest M [--dest DEST]`（副本 / 一致性 / 時戳三層）/ `uv run vcp backup pull --dataset D --manifest M --dest DEST [--tier N] [--overwrite]` / `uv run vcp backup status --dataset D`（唯讀）
```

- [ ] **Step 6: 全套測試、覆蓋率、ruff、commit**

Run: `uv run pytest --cov=vcp -q` 然後 `uv run ruff check . && uv run ruff format --check .`
Expected: 全綠、`fail_under = 80` 通過、乾淨

```bash
git add tests/unit/test_e2e_backup.py tests/integration/test_rsna_knee_backup.py README.md CLAUDE.md
git commit -m "test(backup): 端到端（本機 vault 與假 rclone、--forget-remote、隱私掃描）與 RSNA 真資料證據圖；README / CLAUDE.md 補備份審計命令" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## 自我審查（寫完計畫後對 spec 逐節檢查）

**1. Spec 覆蓋：**

| spec 節 | 任務 |
|---|---|
| §2 原則（反向生成、先小後大、副本要驗、三層稽核、進 git、憑證零接觸、runner 收攏） | T3/T4（證據圖）、T5（tier 順序、驗證、`--forget-remote`）、T6（三層）、T2（git 內清單 / 台帳）、T1（`core/proc.py`） |
| §3 決策表 | 同上；錯誤類別（rclone 非 0 → `PlatformError`、不在 → `VcpError`）在 T5 `dest.py` |
| §4.1 清單模型（欄位、`manifest_id` 預設、root / path 規則、`present=false` 取紀錄 sha、`unlisted`、排序、去重） | T2 schema、T3 `Collector.add` / `files_of` / `locate` / `external_path`、T2 `default_manifest_id` |
| §4.2 台帳事件與欄位 | T2 `BackupRow`；`manifest` 列 T4、`push` / `remote_forgotten` T5、`verify` T6、`pull` T7 |
| §5 目錄佈局、`DatasetPaths` 三個成員 | T2 路徑、T5 `LocalDest` / `RcloneDest._path` |
| §6 CLI 總表與狀態規則 | T4 `manifest`（WARN missing / unlisted、FAIL not_found / exists）、T5 `push`、T6 `verify`、T7 `pull` / `status` |
| §6.1 push 順序（hashsum 前後各一次、tier 升冪、跳過相同、`remote_copy` 不推、先記列、forget 只在全驗證後） | T5 `push` |
| §6.2 verify 三層（每條 sha 鏈、每個台帳與卡、`downloaded_at` 跳過） | T6 `_CHECKERS`、`_ledger_stamps`、`_walk_stamps` |
| §7.1 角色與 tier、`cache/` / `raw/` 排除 | T2 `ROLES` / `TIER_OF`；證據圖只列具名路徑，T8 真資料測試斷言 |
| §7.2 四種走法、`remote_copy` 判定、`external` root | T3 `walk_run` / `_checkpoints`、T4 其餘走法 |
| §8 錯誤字彙 | 各任務的 `reason=` 前綴；verify 以回傳結果 + `reason=` 表達（決定 15） |
| §9 隱私 | T1 redact / `last_line`、T5 `RcloneDest`、T7 `rclone_conf_state`、T8 隱私掃描 |
| §10 介面（`core/proc.py`、各層零改動） | T1；其餘任務只 import 既有函式 |
| §11 測試策略 | T1–T8 各測試檔；`proc.py` 搬家後既有測試不改（T1 Step 5 跑提交 / 訓練測試） |
| §12 驗收 1–8 | T8 端到端（1、3、4、5、6、7）、T5 測試與 T8 斷言（2）、T8 Step 6（8） |
| §13 不在範圍 | 未實作任何一項 |

**2. 佔位詞掃描：** 對計畫全文 grep `TBD|TODO|implement later|fill in|appropriate error handling|similar to Task`——零命中；每個步驟都有完整程式碼或完整命令。

**3. 型別一致性（跨任務的名字逐一對過）：**
- `FileEntry.for_`（alias `for`）、`.key`、`RemoteCopy(dest, run, name)`：T2 定義，T3/T4/T5/T6/T7 使用。
- `Collector.add(path, role, conclusion, *, sha256=None, size=None, remote=None)`、`files_of()`、`missing` / `unlisted`：T3 定義，T4 `build_manifest` 與 T8 真資料測試使用。
- `load_manifest(paths, id)` / `local_path(entry, data_root, configs_root)` / `BackupLedger.append / of / latest`：T2 定義，T5/T6/T7 使用。
- `open_dest(dest, runner=None)`、`.hashes(sub, rels)` / `.put` / `.get`、`RcloneDest.forget()`、`rclone_conf_state(runner=None)`、`RCLONE`：T5 定義，T6 `_check_copies`、T7 `pull` / `status`、T8 monkeypatch 使用。
- `check_tier` / `TIERS`：T5 定義，T7 使用。
- `FakeRemote(*, corrupt, fail, conf, deliver)` 與 `SECRET`：T5 夾具，T6/T7 使用；端到端另用子程序版假 rclone。
- `verify()` 回傳 `VerifyResult`（`copies` / `copy_problems` / `drift` / `bad_stamps` / `first_bad` / `reason` / `ok`）：T6 定義，T7 `status` 只讀台帳列（`passed(row)`），T8 讀 VERDICT 欄位。
- `BackupRow` 的 `pull` 列用 `missing: int` 與 `failed: list[str]` 記缺檔數與不符清單（欄位已在 T2 存在，`_REQUIRED["pull"]` 不含它們，可選）。
- CLI 欄位名：`manifest`、`dest`、`tier`、`pushed`、`skipped`、`verified`、`failed`、`bytes`、`forgotten`、`ok`、`missing`、`mismatch`、`drift`、`bad_stamps`、`first_bad`、`pulled`、`conflicts`、`manifests`、`unverified`、`rclone_conf`——與 spec §8 共用欄位一致。
