# vcp 骨架與資料核心 實作計畫（Plan 1 / 2）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `vcp` 套件的骨架與資料層核心：標準資料格式、任務登記表、Dataset 載入驗證、`jsonl` 直通匯入器、固定切分產生器、lineage、以及 `vcp data import|validate|split|lineage` 四個 CLI 命令。

**Architecture:** `src/` 佈局的 Python 套件；`core/` 提供時戳、路徑、hash、日誌、錯誤五個無狀態工具；`data/` 以 pydantic schema 定義 sample 為原子的標準格式，任務型態與切分策略都是登記表；所有檔案邊界進出都經 pydantic 驗證，CLI 以一行 `VERDICT` 收尾。Plan 2 會在此之上加其餘匯入器、匯出器、稽核與 materialize。

**Tech Stack:** Python 3.12、uv、pydantic v2、typer、PyYAML、numpy、Pillow、scikit-learn、iterative-stratification、pytest、pytest-cov、ruff。

**Spec:** `docs/superpowers/specs/2026-09-02-vcp-skeleton-and-data-layer-design.md`（下稱 spec）。本計畫實作 spec §4、§5、§6.1（僅 `jsonl`）、§7、§9（四個命令）、§10、§11（單元測試部分）。

## Global Constraints

- Python `>=3.12,<3.13`；uv 管理 venv；套件名 `vcp`，`src/vcp/` 佈局；CLI 入口 `vcp = "vcp.cli:app"`。
- 全 repo 只能透過 `vcp.core.time.utc_now()` / `stamp()` 取時；ruff TID251 banned-api 禁 `datetime.datetime.now`、`datetime.datetime.utcnow`、`datetime.datetime.today`、`time.time`；唯一例外 `src/vcp/core/time.py`（per-file-ignores）。已用 ruff 實測：三種寫法（含 `from datetime import datetime; datetime.now()`）都被攔。
- 每個 CLI 命令結尾必輸出 `VERDICT cmd=<名> status=OK|WARN|FAIL|ABORT key=value ...`；exit code OK/WARN → 0、FAIL → 1、ABORT → 2；`--json` 時結果 JSON 到 stdout、VERDICT 行到 stderr；永不互動提問。
- 資料根目錄 `VCP_DATA_ROOT`（預設 Windows `C:/vcp-data`、Linux `~/vcp-data`）；設定根目錄 `VCP_CONFIGS_ROOT`（預設 repo `configs/`）。`raw/` 永不修改。
- 會被 hash 的文字檔（`samples.jsonl`、`raw_manifest.txt`、plan JSON）一律以 `newline="\n"` 寫出，避免 Windows 的 `\r\n` 讓 hash 跨平台不一致。
- 壞資料報位置後中止，不跳過、不猜；未預期例外 → `VERDICT status=ABORT`、exit 2。
- 路徑一律 `pathlib`，不呼叫 shell。
- 覆蓋率門檻 80%（`uv run pytest --cov=vcp`）。
- 通用性：`src/vcp` 內不得出現任何比賽名稱或比賽專屬假設；欄位名描述資料形態（`seq_id`、`targets`、`role`）。
- 每個任務結尾 commit；訊息用 conventional commits（`feat:` / `test:` / `chore:`），中文描述，結尾加 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`。
- 執行環境是 Windows 原生 PowerShell 或 Git Bash；所有命令以 `uv run ...` 開頭，不假設 venv 已啟用。

## 檔案結構（本計畫建立的全部檔案）

```
pyproject.toml            專案定義、依賴、ruff / pytest / coverage 設定
.python-version           3.12
.gitignore
README.md                 一段簡介 + 三個命令
CLAUDE.md                 agent 操作慣例
src/vcp/__init__.py       __version__
src/vcp/core/__init__.py
src/vcp/core/time.py      utc_now / stamp / parse_stamp
src/vcp/core/errors.py    VcpError 階層，每個例外帶 status
src/vcp/core/hashing.py   sha256_file / md5_file / canonical_json / sha256_json / dir_manifest / manifest_hash / write_manifest
src/vcp/core/paths.py     resolve_data_root / resolve_configs_root / validate_name / DatasetPaths / logs_dir
src/vcp/core/config.py    load_yaml_model / dump_yaml_model
src/vcp/core/log.py       Verdict / format_value / worst / exit_code / setup_logging
src/vcp/data/__init__.py
src/vcp/data/schema.py    View / Box / Mask / Labels / Sample / Category / SourceInfo / DatasetCard
src/vcp/data/tasks.py     TaskSpec / TASKS / get_task / register_task + 五個任務
src/vcp/data/dataset.py   write_samples_jsonl / read_samples_jsonl / Dataset
src/vcp/data/importers/__init__.py   登記表匯出 + 登記 jsonl
src/vcp/data/importers/base.py       ImportSpec / ImportResult / Importer / IMPORTERS / get_importer / register_importer / finalize_import
src/vcp/data/importers/jsonl.py      JsonlImporter
src/vcp/data/split.py     SubsetSpec / SplitPlan / parse_subsets / save_plan / load_plan / assert_plan_invariants / stratified_take / generate_fixed / build_plan / distribution_table / STRATEGIES
src/vcp/data/lineage.py   clean_eval_subsets
src/vcp/cli.py            typer app：data import / validate / split / lineage
tests/conftest.py         roots fixture
tests/helpers.py          合成資料建構器
tests/unit/test_package.py
tests/unit/core/test_time.py  test_errors.py  test_hashing.py  test_paths.py  test_config.py  test_log.py
tests/unit/data/test_schema.py  test_tasks.py  test_dataset.py  test_split_plan.py  test_split_generate.py  test_lineage.py
tests/unit/data/importers/test_jsonl.py
tests/unit/test_cli.py
configs/datasets/.gitkeep
projects/.gitkeep
```

每個任務只看自己的區塊也能做：**Interfaces** 列出它消費與產出的確切名稱與簽名。

---

### Task 1: 專案骨架與工具鏈

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`, `README.md`, `CLAUDE.md`, `src/vcp/__init__.py`, `src/vcp/core/__init__.py`, `src/vcp/data/__init__.py`, `src/vcp/data/importers/__init__.py`（暫為空）, `configs/datasets/.gitkeep`, `projects/.gitkeep`, `tests/conftest.py`（暫為空）, `tests/helpers.py`（暫為空）
- Test: `tests/unit/test_package.py`

**Interfaces:**
- Produces: 套件 `vcp` 可 import，`vcp.__version__ == "0.1.0"`；`uv run vcp --help` 在 Task 13 之前會失敗（`vcp.cli` 尚不存在），這是預期的。

- [ ] **Step 1: 寫 pyproject.toml**

```toml
[project]
name = "vcp"
version = "0.1.0"
description = "vision contest pipeline: reusable data / eval / submission framework for image competitions"
readme = "README.md"
requires-python = ">=3.12,<3.13"
dependencies = [
  "pydantic>=2.7",
  "typer>=0.12",
  "pyyaml>=6.0",
  "numpy>=1.26",
  "pillow>=10.0",
  "scikit-learn>=1.4",
  "iterative-stratification>=0.1.9",
]

[project.optional-dependencies]
dicom = [
  "pydicom>=3.0",
  "pylibjpeg>=2.0",
  "pylibjpeg-libjpeg>=2.0",
  "pylibjpeg-openjpeg>=2.0",
]

[project.scripts]
vcp = "vcp.cli:app"

[dependency-groups]
dev = ["pytest>=8.0", "pytest-cov>=5.0", "ruff>=0.5"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/vcp"]

[tool.ruff]
line-length = 100
target-version = "py312"
src = ["src", "tests"]
extend-exclude = ["projects"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "TID"]

[tool.ruff.lint.flake8-tidy-imports.banned-api]
"datetime.datetime.now".msg = "use vcp.core.time.utc_now()"
"datetime.datetime.utcnow".msg = "use vcp.core.time.utc_now()"
"datetime.datetime.today".msg = "use vcp.core.time.utc_now()"
"time.time".msg = "use vcp.core.time.utc_now()"

[tool.ruff.lint.per-file-ignores]
"src/vcp/core/time.py" = ["TID251"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["tests"]
addopts = "-q"
markers = ["realdata: needs real datasets under VCP_DATA_ROOT; skipped otherwise"]

[tool.coverage.run]
source = ["vcp"]
branch = true

[tool.coverage.report]
fail_under = 80
show_missing = true
```

- [ ] **Step 2: 寫其餘骨架檔**

`.python-version`：

```
3.12
```

`.gitignore`：

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/
dist/
*.egg-info/
projects/*/data/
projects/*/runs/
```

`src/vcp/__init__.py`：

```python
"""vcp: vision contest pipeline."""

__version__ = "0.1.0"
```

`src/vcp/core/__init__.py`、`src/vcp/data/__init__.py`、`src/vcp/data/importers/__init__.py`、`tests/conftest.py`、`tests/helpers.py`：空檔（只含一行 docstring 亦可）。`configs/datasets/.gitkeep`、`projects/.gitkeep`：空檔。

`README.md`：

```markdown
# vcp — vision contest pipeline

可重複使用的影像競賽框架：標準資料格式、多重驗證集切分、lineage、量測護欄、提交治理。設計文件見 `docs/superpowers/specs/`。

```bash
uv sync
uv run vcp --help
uv run pytest --cov=vcp
```
```

`CLAUDE.md`：

```markdown
# vcp 操作慣例（給 agent 與人看）

## 三條機械鐵則
1. 取時只能用 `vcp.core.time.utc_now()` / `stamp()`。ruff TID251 會擋 `datetime.now` / `utcnow` / `today` / `time.time`；唯一例外是 `src/vcp/core/time.py`。
2. 每個 CLI 命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT ...` 收尾；exit 0 / 1（FAIL）/ 2（ABORT）。命令永不互動提問；`--json` 時結果 JSON 到 stdout、VERDICT 到 stderr。
3. venv 隔離：核心 `vcp` 一個 venv（`uv sync`）；訓練框架各自 venv，以 editable 裝 `vcp`；量測 venv 凍結後禁 install。

## 通用性原則
以資料形態與任務類型為軸，不以特定比賽為軸。比賽專屬程式碼放 `projects/<contest>/`，不進 `src/vcp`。六個變異軸（任務、匯入器、匯出器、解碼器、切分策略、稽核）都是登記表；加一種形態 = 加一個登記項，不改 schema、不改 CLI。

## 路徑
- 資料根目錄：`VCP_DATA_ROOT`（預設 Windows `C:/vcp-data`、Linux `~/vcp-data`）。`raw/<name>/` 永不修改；`datasets/<name>/` 放 `samples.jsonl`、`raw_manifest.txt`、`cache/`。
- 設定根目錄：`VCP_CONFIGS_ROOT`（預設 repo 的 `configs/`）。`configs/datasets/<name>/dataset.yaml` 與 `splits/*.json` 進 git；plan 檔一旦寫入不可修改，要改就換 plan-id。

## 常用命令
- `uv sync` / `uv run vcp --help` / `uv run pytest --cov=vcp` / `uv run ruff check .`

## 文件
- 設計 spec：`docs/superpowers/specs/`；實作計畫：`docs/superpowers/plans/`；賽後報告：`docs/postmortems/`
```

- [ ] **Step 3: 寫 smoke test**

`tests/unit/test_package.py`：

```python
import vcp


def test_version_is_non_empty_string():
    assert isinstance(vcp.__version__, str)
    assert vcp.__version__ == "0.1.0"
```

- [ ] **Step 4: 建 venv 並跑測試**

Run: `uv sync`（第一次會自動下載 Python 3.12，需要網路）
Expected: 結尾類似 `Installed N packages`，產生 `uv.lock` 與 `.venv/`。

Run: `uv run pytest tests/unit/test_package.py -v`
Expected: `1 passed`

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock .python-version .gitignore README.md CLAUDE.md src tests configs projects
git commit -m "chore: vcp 專案骨架（uv、pydantic、typer、ruff banned-api、pytest）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: core.time（唯一合法的取時方式）

**Files:**
- Create: `src/vcp/core/time.py`
- Test: `tests/unit/core/test_time.py`

**Interfaces:**
- Produces:
  - `utc_now() -> datetime`：tz-aware UTC。
  - `stamp(dt: datetime | None = None) -> str`：`YYYY-MM-DDTHH:MM:SS.mmmZ`（毫秒）；naive datetime 拋 `ValueError`；非 UTC 時區會先轉 UTC。
  - `parse_stamp(s: str) -> datetime`：接受毫秒或秒精度，回 tz-aware UTC。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/core/test_time.py`：

```python
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from vcp.core import time as vtime

STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
REPO = Path(__file__).resolve().parents[3]


def test_utc_now_is_tz_aware_utc():
    now = vtime.utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_stamp_format_and_roundtrip():
    s = vtime.stamp()
    assert STAMP_RE.match(s), s
    parsed = vtime.parse_stamp(s)
    assert parsed.utcoffset() == timedelta(0)
    assert abs((vtime.utc_now() - parsed).total_seconds()) < 5


def test_stamp_converts_other_timezones_to_utc():
    taipei = timezone(timedelta(hours=8))
    dt = datetime(2026, 9, 2, 20, 0, 0, tzinfo=taipei)
    assert vtime.stamp(dt) == "2026-09-02T12:00:00.000Z"


def test_stamp_rejects_naive_datetime():
    with pytest.raises(ValueError):
        vtime.stamp(datetime(2026, 9, 2))


def test_parse_stamp_accepts_seconds_precision():
    assert vtime.parse_stamp("2026-09-02T12:00:00Z") == datetime(2026, 9, 2, 12, tzinfo=UTC)


def test_parse_stamp_rejects_missing_z():
    with pytest.raises(ValueError):
        vtime.parse_stamp("2026-09-02T12:00:00")


@pytest.mark.parametrize(
    "code",
    [
        "import datetime\nx = datetime.datetime.now()\n",
        "from datetime import datetime\nx = datetime.now()\n",
        "import time\nx = time.time()\n",
    ],
)
def test_ruff_bans_naive_clock_calls(tmp_path, code):
    target = tmp_path / "bad_clock.py"
    target.write_text(code, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--select",
            "TID251",
            "--config",
            str(REPO / "pyproject.toml"),
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "TID251" in proc.stdout, proc.stdout + proc.stderr
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/core/test_time.py -v`
Expected: 收集階段 `ImportError`（`vcp.core.time` 不存在）。

- [ ] **Step 3: 實作**

`src/vcp/core/time.py`：

```python
"""The only sanctioned way to read the clock (postmortem error #1: timezone chaos).

Everything else in the repo must call ``utc_now()`` / ``stamp()``; ruff TID251 bans the
raw clock APIs outside this file.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Current time as a tz-aware UTC datetime."""
    return datetime.now(tz=UTC)


def stamp(dt: datetime | None = None) -> str:
    """ISO-8601 UTC string with millisecond precision and a trailing ``Z``."""
    if dt is None:
        dt = utc_now()
    if dt.tzinfo is None:
        raise ValueError("stamp() requires a tz-aware datetime")
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_stamp(s: str) -> datetime:
    """Parse a ``stamp()`` string (or a seconds-precision variant) into a UTC datetime."""
    if not s.endswith("Z"):
        raise ValueError(f"stamp must end with Z: {s!r}")
    body = s[:-1]
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in body else "%Y-%m-%dT%H:%M:%S"
    return datetime.strptime(body, fmt).replace(tzinfo=UTC)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/core/test_time.py -v`
Expected: `9 passed`

Run: `uv run ruff check .`
Expected: `All checks passed!`（time.py 內的 `datetime.now(tz=UTC)` 由 per-file-ignores 放行）

- [ ] **Step 5: Commit**

```bash
git add src/vcp/core/time.py tests/unit/core/test_time.py
git commit -m "feat(core): utc_now/stamp/parse_stamp 與 ruff banned-api 驗證測試

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: core.errors 與 core.hashing

**Files:**
- Create: `src/vcp/core/errors.py`, `src/vcp/core/hashing.py`
- Test: `tests/unit/core/test_errors.py`, `tests/unit/core/test_hashing.py`

**Interfaces:**
- Produces（errors）：
  - `VcpError(message, *, location: str | None = None)`，類別屬性 `status: str = "ABORT"`；`str(err)` 在有 location 時為 `"<message> (at <location>)"`。
  - 子類：`ValidationFailed`（status `FAIL`）、`IntegrityError`（`FAIL`）、`RegistryError`、`PlanMismatchError`、`SealedSubsetError`、`InvariantError`（皆 `ABORT`）。
- Produces（hashing）：
  - `sha256_file(path: Path) -> str`、`md5_file(path: Path) -> str`
  - `sha256_text(text: str) -> str`
  - `canonical_json(obj) -> str`（sorted keys、緊湊分隔、`ensure_ascii=False`）、`sha256_json(obj) -> str`
  - `dir_manifest(root: Path) -> list[str]`：每檔一行 `relpath\tsize\tmd5`，relpath 為 posix、依 relpath 排序
  - `manifest_hash(lines: list[str]) -> str`：`sha256_text("\n".join(lines) + "\n")`
  - `write_manifest(lines: list[str], path: Path) -> str`：寫檔（`newline="\n"`）並回傳 `manifest_hash`

- [ ] **Step 1: 寫失敗測試**

`tests/unit/core/test_errors.py`：

```python
from vcp.core.errors import (
    IntegrityError,
    InvariantError,
    PlanMismatchError,
    RegistryError,
    SealedSubsetError,
    ValidationFailed,
    VcpError,
)


def test_statuses():
    assert ValidationFailed("x").status == "FAIL"
    assert IntegrityError("x").status == "FAIL"
    for cls in (RegistryError, PlanMismatchError, SealedSubsetError, InvariantError):
        assert cls("x").status == "ABORT"
        assert issubclass(cls, VcpError)
    assert VcpError("x").status == "ABORT"


def test_location_in_message():
    err = ValidationFailed("bad box", location="sample s1")
    assert str(err) == "bad box (at sample s1)"
    assert err.location == "sample s1"
    assert str(ValidationFailed("plain")) == "plain"
```

`tests/unit/core/test_hashing.py`：

```python
from vcp.core import hashing as h


def test_canonical_json_is_order_independent():
    assert h.canonical_json({"b": 1, "a": [1, 2]}) == h.canonical_json({"a": [1, 2], "b": 1})
    assert h.canonical_json({"a": 1}) == '{"a":1}'
    assert h.sha256_json({"x": "中"}) == h.sha256_text('{"x":"中"}')


def test_file_digests(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello")
    assert h.sha256_file(p) == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert h.md5_file(p) == "5d41402abc4b2a76b9719d911017c592"


def test_dir_manifest_sorted_posix_and_hash_stable(tmp_path):
    root = tmp_path / "raw"
    (root / "b").mkdir(parents=True)
    (root / "b" / "y.txt").write_bytes(b"y")
    (root / "a.txt").write_bytes(b"a")
    lines = h.dir_manifest(root)
    assert lines == [
        f"a.txt\t1\t{h.md5_file(root / 'a.txt')}",
        f"b/y.txt\t1\t{h.md5_file(root / 'b' / 'y.txt')}",
    ]
    out = tmp_path / "manifest.txt"
    digest = h.write_manifest(lines, out)
    assert digest == h.manifest_hash(lines) == h.sha256_file(out)
    assert b"\r\n" not in out.read_bytes()


def test_dir_manifest_empty_dir(tmp_path):
    assert h.dir_manifest(tmp_path) == []
    assert h.manifest_hash([]) == h.sha256_text("\n")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/core/test_errors.py tests/unit/core/test_hashing.py -v`
Expected: `ImportError`。

- [ ] **Step 3: 實作**

`src/vcp/core/errors.py`：

```python
"""Error hierarchy. Every error carries the VERDICT status the CLI should report."""

from __future__ import annotations


class VcpError(Exception):
    """Base class. ``status`` is the VERDICT status; ``location`` points at the offending data."""

    status: str = "ABORT"

    def __init__(self, message: str, *, location: str | None = None) -> None:
        super().__init__(message)
        self.location = location

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} (at {self.location})" if self.location else base


class ValidationFailed(VcpError):
    """Input data violates the schema or task rules. Actionable by the user."""

    status = "FAIL"


class IntegrityError(VcpError):
    """A recorded hash no longer matches the file on disk."""

    status = "FAIL"


class RegistryError(VcpError):
    """Unknown or duplicate registry entry (task, importer, strategy, ...)."""


class PlanMismatchError(VcpError):
    """A split plan does not belong to this dataset version, or names an unknown subset."""


class SealedSubsetError(VcpError):
    """Attempt to read a sealed subset without an explicit, recorded unseal."""


class InvariantError(VcpError):
    """A generator produced output that violates its own invariants (a bug, not bad input)."""
```

`src/vcp/core/hashing.py`：

```python
"""Content hashing helpers. All hashes are hex strings."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_CHUNK = 1 << 20


def _digest_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    return _digest_file(path, "sha256")


def md5_file(path: Path) -> str:
    return _digest_file(path, "md5")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, unicode kept as-is."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def dir_manifest(root: Path) -> list[str]:
    """One line per file under ``root``: ``relpath<TAB>size<TAB>md5``, sorted by posix relpath."""
    entries = sorted((p.relative_to(root).as_posix(), p) for p in root.rglob("*") if p.is_file())
    return [f"{rel}\t{p.stat().st_size}\t{md5_file(p)}" for rel, p in entries]


def manifest_hash(lines: list[str]) -> str:
    return sha256_text("\n".join(lines) + "\n")


def write_manifest(lines: list[str], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return manifest_hash(lines)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/core -v`
Expected: 全部 passed（含 Task 2 的 9 個）。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/core/errors.py src/vcp/core/hashing.py tests/unit/core/test_errors.py tests/unit/core/test_hashing.py
git commit -m "feat(core): 錯誤階層（帶 VERDICT status）與 hash / 目錄 manifest 工具

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: core.paths 與 core.config

**Files:**
- Create: `src/vcp/core/paths.py`, `src/vcp/core/config.py`
- Test: `tests/unit/core/test_paths.py`, `tests/unit/core/test_config.py`

**Interfaces:**
- Consumes: `ValidationFailed`（Task 3）。
- Produces（paths）：
  - 常數 `ENV_DATA_ROOT = "VCP_DATA_ROOT"`、`ENV_CONFIGS_ROOT = "VCP_CONFIGS_ROOT"`。
  - `default_data_root() -> Path`；`resolve_data_root(override: Path | None = None) -> Path`（override → env → 預設；有 override/env 時 `.expanduser().resolve()`）。
  - `resolve_configs_root(override: Path | None = None) -> Path`（override → env → 從 cwd 往上找同時含 `pyproject.toml` 與 `configs/` 的目錄 → 套件相對路徑 `<repo>/configs`）。
  - `validate_name(name: str) -> None`：不符 `^[A-Za-z0-9][A-Za-z0-9._-]*$` 拋 `ValidationFailed`。
  - `logs_dir(data_root: Path) -> Path` = `data_root / "logs"`。
  - `DatasetPaths`（frozen dataclass，欄位 `name`、`data_root`、`configs_root`）：`DatasetPaths.resolve(name, *, data_root=None, configs_root=None)`；屬性 `raw_dir`、`dataset_dir`、`samples_jsonl`、`raw_manifest`、`cache_dir`、`config_dir`、`card_yaml`、`splits_dir`；方法 `plan_json(plan_id) -> Path`、`unseal_jsonl(plan_id) -> Path`。
- Produces（config）：
  - `load_yaml_model(path: Path, model_cls: type[T]) -> T`：非 mapping 或 pydantic 錯誤 → `ValidationFailed(..., location=str(path))`。
  - `dump_yaml_model(model: BaseModel, path: Path) -> None`：建父目錄、`safe_dump(model.model_dump(mode="json"), sort_keys=False, allow_unicode=True)`。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/core/test_paths.py`：

```python
import sys
from pathlib import Path

import pytest

from vcp.core import paths
from vcp.core.errors import ValidationFailed


def test_data_root_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.ENV_DATA_ROOT, raising=False)
    expected_default = Path("C:/vcp-data") if sys.platform == "win32" else Path.home() / "vcp-data"
    assert paths.resolve_data_root() == expected_default
    monkeypatch.setenv(paths.ENV_DATA_ROOT, str(tmp_path / "env"))
    assert paths.resolve_data_root() == (tmp_path / "env").resolve()
    assert paths.resolve_data_root(tmp_path / "override") == (tmp_path / "override").resolve()


def test_configs_root_env_and_walk_up(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.ENV_CONFIGS_ROOT, str(tmp_path / "cfg"))
    assert paths.resolve_configs_root() == (tmp_path / "cfg").resolve()
    monkeypatch.delenv(paths.ENV_CONFIGS_ROOT)
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert paths.resolve_configs_root() == (repo / "configs").resolve()


def test_configs_root_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.ENV_CONFIGS_ROOT, str(tmp_path / "cfg"))
    assert paths.resolve_configs_root(tmp_path / "o") == (tmp_path / "o").resolve()


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a b", ".hidden", "a\\b"])
def test_validate_name_rejects(bad):
    with pytest.raises(ValidationFailed):
        paths.validate_name(bad)


@pytest.mark.parametrize("good", ["ds1", "marine-debris", "rsna.knee_2026", "A"])
def test_validate_name_accepts(good):
    paths.validate_name(good)


def test_dataset_paths_layout(tmp_path):
    p = paths.DatasetPaths.resolve("ds1", data_root=tmp_path / "d", configs_root=tmp_path / "c")
    d = (tmp_path / "d").resolve()
    c = (tmp_path / "c").resolve()
    assert p.raw_dir == d / "raw" / "ds1"
    assert p.dataset_dir == d / "datasets" / "ds1"
    assert p.samples_jsonl == d / "datasets" / "ds1" / "samples.jsonl"
    assert p.raw_manifest == d / "datasets" / "ds1" / "raw_manifest.txt"
    assert p.cache_dir == d / "datasets" / "ds1" / "cache"
    assert p.config_dir == c / "datasets" / "ds1"
    assert p.card_yaml == c / "datasets" / "ds1" / "dataset.yaml"
    assert p.splits_dir == c / "datasets" / "ds1" / "splits"
    assert p.plan_json("fixed-v1") == c / "datasets" / "ds1" / "splits" / "fixed-v1.json"
    assert p.unseal_jsonl("fixed-v1") == c / "datasets" / "ds1" / "splits" / "fixed-v1.unseal.jsonl"
    assert paths.logs_dir(d) == d / "logs"


def test_dataset_paths_validates_name(tmp_path):
    with pytest.raises(ValidationFailed):
        paths.DatasetPaths.resolve("../evil", data_root=tmp_path, configs_root=tmp_path)
```

`tests/unit/core/test_config.py`：

```python
import pytest
from pydantic import BaseModel

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import ValidationFailed


class M(BaseModel):
    name: str
    n: int = 1
    tags: list[str] = []


def test_yaml_roundtrip_creates_parent_and_keeps_unicode(tmp_path):
    p = tmp_path / "x" / "m.yaml"
    dump_yaml_model(M(name="中文", n=3, tags=["a"]), p)
    assert load_yaml_model(p, M) == M(name="中文", n=3, tags=["a"])
    assert "中文" in p.read_text(encoding="utf-8")


def test_yaml_invalid_reports_path(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("name: ok\nn: notint\n", encoding="utf-8")
    with pytest.raises(ValidationFailed) as ei:
        load_yaml_model(p, M)
    assert str(p) in str(ei.value)


def test_yaml_non_mapping_rejected(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        load_yaml_model(p, M)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/core/test_paths.py tests/unit/core/test_config.py -v`
Expected: `ImportError`。

- [ ] **Step 3: 實作**

`src/vcp/core/paths.py`：

```python
"""Where things live: data root (big, not in git) and configs root (small, in git)."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import ValidationFailed

ENV_DATA_ROOT = "VCP_DATA_ROOT"
ENV_CONFIGS_ROOT = "VCP_CONFIGS_ROOT"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def default_data_root() -> Path:
    if sys.platform == "win32":
        return Path("C:/vcp-data")
    return Path.home() / "vcp-data"


def resolve_data_root(override: Path | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve()
    env = os.environ.get(ENV_DATA_ROOT)
    if env:
        return Path(env).expanduser().resolve()
    return default_data_root()


def resolve_configs_root(override: Path | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve()
    env = os.environ.get(ENV_CONFIGS_ROOT)
    if env:
        return Path(env).expanduser().resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "configs").is_dir():
            return candidate / "configs"
    repo_root = Path(__file__).resolve().parents[3]  # src/vcp/core/paths.py -> repo
    return repo_root / "configs"


def validate_name(name: str) -> None:
    """Dataset / plan ids become path segments; keep them boring."""
    if not _NAME_RE.match(name):
        raise ValidationFailed(
            f"invalid name {name!r}: must match {_NAME_RE.pattern} (letters, digits, . _ -)"
        )


def logs_dir(data_root: Path) -> Path:
    return data_root / "logs"


@dataclass(frozen=True)
class DatasetPaths:
    name: str
    data_root: Path
    configs_root: Path

    @classmethod
    def resolve(
        cls, name: str, *, data_root: Path | None = None, configs_root: Path | None = None
    ) -> DatasetPaths:
        validate_name(name)
        return cls(name, resolve_data_root(data_root), resolve_configs_root(configs_root))

    @property
    def raw_dir(self) -> Path:
        return self.data_root / "raw" / self.name

    @property
    def dataset_dir(self) -> Path:
        return self.data_root / "datasets" / self.name

    @property
    def samples_jsonl(self) -> Path:
        return self.dataset_dir / "samples.jsonl"

    @property
    def raw_manifest(self) -> Path:
        return self.dataset_dir / "raw_manifest.txt"

    @property
    def cache_dir(self) -> Path:
        return self.dataset_dir / "cache"

    @property
    def config_dir(self) -> Path:
        return self.configs_root / "datasets" / self.name

    @property
    def card_yaml(self) -> Path:
        return self.config_dir / "dataset.yaml"

    @property
    def splits_dir(self) -> Path:
        return self.config_dir / "splits"

    def plan_json(self, plan_id: str) -> Path:
        validate_name(plan_id)
        return self.splits_dir / f"{plan_id}.json"

    def unseal_jsonl(self, plan_id: str) -> Path:
        validate_name(plan_id)
        return self.splits_dir / f"{plan_id}.unseal.jsonl"
```

`src/vcp/core/config.py`：

```python
"""YAML <-> pydantic model helpers. Every YAML boundary goes through here."""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from vcp.core.errors import ValidationFailed

T = TypeVar("T", bound=BaseModel)


def load_yaml_model(path: Path, model_cls: type[T]) -> T:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValidationFailed("expected a YAML mapping at top level", location=str(path))
    try:
        return model_cls.model_validate(data)
    except ValidationError as e:
        raise ValidationFailed(str(e), location=str(path)) from e


def dump_yaml_model(model: BaseModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(model.model_dump(mode="json"), f, sort_keys=False, allow_unicode=True)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/core -v`
Expected: 全部 passed。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/core/paths.py src/vcp/core/config.py tests/unit/core/test_paths.py tests/unit/core/test_config.py
git commit -m "feat(core): 資料/設定根目錄解析、DatasetPaths、YAML 模型讀寫

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: core.log（VERDICT 與 JSON lines 日誌）

**Files:**
- Create: `src/vcp/core/log.py`
- Test: `tests/unit/core/test_log.py`

**Interfaces:**
- Consumes: `stamp()`（Task 2）。
- Produces:
  - `Status = Literal["OK", "WARN", "FAIL", "ABORT"]`；`STATUS_ORDER`、`EXIT_CODES` 字典。
  - `FieldValue = str | int | float | bool`；`format_value(v) -> str`：bool → `true`/`false`；float → `repr`（全精度）；str 含空白、引號或 `=` 時用 JSON 引號包起。
  - `Verdict(BaseModel)`：`cmd: str`、`status: Status`、`fields: dict[str, FieldValue]`；`line() -> str`。
  - `worst(*statuses) -> Status`（空 → `OK`）；`exit_code(status) -> int`。
  - `setup_logging(log_dir: Path, *, level=logging.INFO) -> logging.Logger`：logger 名 `vcp`，寫 `log_dir / f"vcp-{YYYY-MM-DD}.jsonl"`，每次呼叫先移除舊的 FileHandler；記錄格式 `{"ts","level","logger","msg", ...record.vcp}`，例外時加 `exc`。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/core/test_log.py`：

```python
import json

from vcp.core.log import Verdict, exit_code, format_value, setup_logging, worst


def test_verdict_line_and_quoting():
    v = Verdict(
        cmd="split",
        status="OK",
        fields={"plan": "fixed-v1", "train": 70, "ratio": 0.1, "ok": True, "note": "has space"},
    )
    assert v.line() == (
        'VERDICT cmd=split status=OK plan=fixed-v1 train=70 ratio=0.1 ok=true note="has space"'
    )


def test_full_precision_floats_and_bool_before_int():
    assert format_value(0.7989123456789) == "0.7989123456789"
    assert format_value(False) == "false"
    assert format_value(3) == "3"
    assert format_value("a=b") == '"a=b"'


def test_worst_and_exit_codes():
    assert worst("OK", "WARN", "OK") == "WARN"
    assert worst("FAIL", "ABORT") == "ABORT"
    assert worst() == "OK"
    assert [exit_code(s) for s in ("OK", "WARN", "FAIL", "ABORT")] == [0, 0, 1, 2]


def test_setup_logging_writes_json_lines(tmp_path):
    logger = setup_logging(tmp_path / "logs")
    logger.info("hello", extra={"vcp": {"k": 1}})
    for h in logger.handlers:
        h.flush()
    files = list((tmp_path / "logs").glob("vcp-*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
    assert rec["msg"] == "hello" and rec["k"] == 1 and rec["ts"].endswith("Z")
    assert rec["level"] == "INFO"


def test_setup_logging_replaces_previous_file_handler(tmp_path):
    a = setup_logging(tmp_path / "a")
    b = setup_logging(tmp_path / "b")
    assert a is b
    assert sum(1 for h in b.handlers if h.__class__.__name__ == "FileHandler") == 1
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/core/test_log.py -v`
Expected: `ImportError`。

- [ ] **Step 3: 實作**

`src/vcp/core/log.py`：

```python
"""Machine-readable VERDICT lines and JSON-lines logging."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from vcp.core.time import stamp

Status = Literal["OK", "WARN", "FAIL", "ABORT"]
STATUS_ORDER: dict[str, int] = {"OK": 0, "WARN": 1, "FAIL": 2, "ABORT": 3}
EXIT_CODES: dict[str, int] = {"OK": 0, "WARN": 0, "FAIL": 1, "ABORT": 2}
FieldValue = str | int | float | bool
_BARE = re.compile(r'^[^\s"=]+$')


def format_value(v: FieldValue) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    s = str(v)
    return s if _BARE.match(s) else json.dumps(s, ensure_ascii=False)


class Verdict(BaseModel):
    cmd: str
    status: Status
    fields: dict[str, FieldValue] = Field(default_factory=dict)

    def line(self) -> str:
        parts = [f"VERDICT cmd={self.cmd}", f"status={self.status}"]
        parts += [f"{k}={format_value(v)}" for k, v in self.fields.items()]
        return " ".join(parts)


def worst(*statuses: str) -> Status:
    if not statuses:
        return "OK"
    return max(statuses, key=lambda s: STATUS_ORDER[s])  # type: ignore[return-value]


def exit_code(status: str) -> int:
    return EXIT_CODES[status]


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": stamp(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "vcp", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(log_dir: Path, *, level: int = logging.INFO) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("vcp")
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()
    path = log_dir / f"vcp-{stamp()[:10]}.jsonl"
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(JsonLinesFormatter())
    logger.addHandler(fh)
    return logger
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/core -v`
Expected: 全部 passed。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/core/log.py tests/unit/core/test_log.py
git commit -m "feat(core): Verdict 終局行、狀態序、JSON lines 日誌

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: data.schema（標準資料格式）

**Files:**
- Create: `src/vcp/data/schema.py`
- Test: `tests/unit/data/test_schema.py`

**Interfaces:**
- Produces（全部 pydantic v2 模型，`extra="forbid"`）：
  - `LabelSource = Literal["gold", "derived", "pseudo", "none"]`
  - `View(path, width=None, height=None, role=None, seq_id=None, seq_index=None, meta={})`
  - `Box(x, y, w, h, category_id, view=0, meta={})`
  - `Mask(category_id, view=0, rle=None, polygon=None, path=None, meta={})`，`rle`/`polygon`/`path` 恰一
  - `Labels(cls=None, targets=None, boxes=None, masks=None, extra={})`，`targets: dict[str, float]`
  - `Sample(sample_id, views, labels=None, label_source, group=None, meta={})`；驗證 `label_source != "none"` ⇔ `labels is not None`，同一 `seq_id` 內 `seq_index` 不重複
  - `Category(id, name, meta={})`
  - `SourceInfo(importer, importer_version, raw_path, raw_hash, license, url, downloaded_at, notes="")`
  - `DatasetCard(name, task, categories=[], image_root, source, created_at, sample_count, samples_hash, schema_version=1)`；categories 的 id 與 name 各自唯一
  - `dump_sample(sample) -> dict`：`model_dump(mode="json", exclude_none=True, exclude_defaults=True)`；`sample_json_line(sample) -> str`：`json.dumps(dump_sample(s), ensure_ascii=False)`（鍵序 = 宣告序）

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/test_schema.py`：

```python
import json

import pytest
from pydantic import ValidationError

from vcp.data.schema import (
    Box,
    Category,
    DatasetCard,
    Labels,
    Mask,
    Sample,
    SourceInfo,
    View,
    dump_sample,
    sample_json_line,
)


def view(**kw):
    return View(path="a.jpg", **kw)


def source():
    return SourceInfo(
        importer="jsonl",
        importer_version="1",
        raw_path="/raw",
        raw_hash="h",
        license="CC-BY",
        url="https://x",
        downloaded_at="2026-09-02T00:00:00.000Z",
    )


def test_single_view_sample_dump_is_minimal_and_roundtrips():
    s = Sample(
        sample_id="s1",
        views=[view(width=8, height=8)],
        labels=Labels(boxes=[Box(x=0, y=0, w=4, h=4, category_id=1)]),
        label_source="gold",
    )
    dumped = dump_sample(s)
    assert Sample.model_validate(dumped) == s
    assert "group" not in dumped and "meta" not in dumped
    assert "view" not in dumped["labels"]["boxes"][0]
    line = sample_json_line(s)
    assert line.startswith('{"sample_id": "s1", "views": [')
    assert json.loads(line) == dumped


def test_multi_view_sequence_sample_coerces_targets_to_float():
    s = Sample(
        sample_id="st1",
        views=[view(seq_id="A", seq_index=i, role="t2") for i in range(3)],
        labels=Labels(targets={"ACL": 1, "MCL": 0}),
        label_source="gold",
    )
    assert s.labels is not None and s.labels.targets == {"ACL": 1.0, "MCL": 0.0}
    assert s.views[2].seq_index == 2 and s.views[2].role == "t2"


def test_duplicate_seq_index_rejected_but_distinct_sequences_ok():
    with pytest.raises(ValidationError, match="duplicate seq_index"):
        Sample(
            sample_id="x",
            views=[view(seq_id="A", seq_index=0), view(seq_id="A", seq_index=0)],
            label_source="none",
        )
    Sample(
        sample_id="x",
        views=[view(seq_id="A", seq_index=0), view(seq_id="B", seq_index=0)],
        label_source="none",
    )


def test_label_source_must_match_labels():
    with pytest.raises(ValidationError):
        Sample(sample_id="x", views=[view()], label_source="gold")
    with pytest.raises(ValidationError):
        Sample(sample_id="x", views=[view()], labels=Labels(cls=1), label_source="none")
    assert Sample(sample_id="x", views=[view()], label_source="none").labels is None


def test_mask_exactly_one_representation():
    Mask(category_id=1, rle="abc")
    Mask(category_id=1, polygon=[[0, 0, 1, 0, 1, 1]])
    Mask(category_id=1, path="m.png")
    with pytest.raises(ValidationError):
        Mask(category_id=1)
    with pytest.raises(ValidationError):
        Mask(category_id=1, rle="a", path="m.png")


def test_unknown_keys_rejected_everywhere():
    with pytest.raises(ValidationError):
        View(path="a.jpg", widht=3)
    with pytest.raises(ValidationError):
        Labels(clas=1)


def test_views_non_empty_and_ids_non_empty():
    with pytest.raises(ValidationError):
        Sample(sample_id="x", views=[], label_source="none")
    with pytest.raises(ValidationError):
        Sample(sample_id="", views=[view()], label_source="none")


def test_card_category_uniqueness_and_empty_categories_allowed():
    DatasetCard(
        name="d",
        task="det",
        categories=[Category(id=1, name="a"), Category(id=2, name="b")],
        image_root="/img",
        source=source(),
        created_at="t",
        sample_count=0,
        samples_hash="",
    )
    DatasetCard(
        name="d",
        task="regression",
        image_root="/img",
        source=source(),
        created_at="t",
        sample_count=0,
        samples_hash="",
    )
    with pytest.raises(ValidationError, match="unique"):
        DatasetCard(
            name="d",
            task="det",
            categories=[Category(id=1, name="a"), Category(id=1, name="b")],
            image_root="/img",
            source=source(),
            created_at="t",
            sample_count=0,
            samples_hash="",
        )
    with pytest.raises(ValidationError, match="unique"):
        DatasetCard(
            name="d",
            task="det",
            categories=[Category(id=1, name="a"), Category(id=2, name="a")],
            image_root="/img",
            source=source(),
            created_at="t",
            sample_count=0,
            samples_hash="",
        )


def test_extra_labels_stored_unvalidated():
    lab = Labels(cls=0, extra={"keypoints": [[1, 2, 3]]})
    assert lab.extra["keypoints"] == [[1, 2, 3]]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/test_schema.py -v`
Expected: `ImportError`。

- [ ] **Step 3: 實作**

`src/vcp/data/schema.py`：

```python
"""Canonical dataset format. A *sample* is the unit of prediction; it owns one or more views.

Field names describe data shape, never a source domain: ``seq_id`` covers DICOM series,
video clips and time series alike; ``targets`` covers multi-label, regression and soft labels.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LabelSource = Literal["gold", "derived", "pseudo", "none"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class View(_Strict):
    path: str = Field(min_length=1)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    role: str | None = None
    seq_id: str | None = None
    seq_index: int | None = Field(default=None, ge=0)
    meta: dict[str, Any] = Field(default_factory=dict)


class Box(_Strict):
    """Absolute pixels, top-left origin, xywh. Rotation parameters, if any, go to meta.rotated."""

    x: float
    y: float
    w: float
    h: float
    category_id: int
    view: int = Field(default=0, ge=0)
    meta: dict[str, Any] = Field(default_factory=dict)


class Mask(_Strict):
    category_id: int
    view: int = Field(default=0, ge=0)
    rle: str | None = None
    polygon: list[list[float]] | None = None
    path: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_representation(self) -> Mask:
        present = sum(x is not None for x in (self.rle, self.polygon, self.path))
        if present != 1:
            raise ValueError("Mask needs exactly one of rle / polygon / path")
        return self


class Labels(_Strict):
    cls: int | None = None
    targets: dict[str, float] | None = None
    boxes: list[Box] | None = None
    masks: list[Mask] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Sample(_Strict):
    sample_id: str = Field(min_length=1)
    views: list[View] = Field(min_length=1)
    labels: Labels | None = None
    label_source: LabelSource
    group: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistency(self) -> Sample:
        if (self.label_source != "none") != (self.labels is not None):
            raise ValueError("label_source must be 'none' exactly when labels is absent")
        seen: set[tuple[str | None, int]] = set()
        for v in self.views:
            if v.seq_index is None:
                continue
            key = (v.seq_id, v.seq_index)
            if key in seen:
                raise ValueError(f"duplicate seq_index {v.seq_index} in seq {v.seq_id!r}")
            seen.add(key)
        return self


class Category(_Strict):
    id: int
    name: str = Field(min_length=1)
    meta: dict[str, Any] = Field(default_factory=dict)


class SourceInfo(_Strict):
    importer: str
    importer_version: str
    raw_path: str
    raw_hash: str
    license: str
    url: str
    downloaded_at: str
    notes: str = ""


class DatasetCard(_Strict):
    name: str
    task: str
    categories: list[Category] = Field(default_factory=list)
    image_root: str
    source: SourceInfo
    created_at: str
    sample_count: int = Field(ge=0)
    samples_hash: str
    schema_version: int = 1

    @model_validator(mode="after")
    def _unique_categories(self) -> DatasetCard:
        ids = [c.id for c in self.categories]
        names = [c.name for c in self.categories]
        if len(set(ids)) != len(ids):
            raise ValueError("category ids must be unique")
        if len(set(names)) != len(names):
            raise ValueError("category names must be unique")
        return self


def dump_sample(sample: Sample) -> dict[str, Any]:
    """Minimal, stable dict: defaults and None dropped, key order = declaration order."""
    return sample.model_dump(mode="json", exclude_none=True, exclude_defaults=True)


def sample_json_line(sample: Sample) -> str:
    return json.dumps(dump_sample(sample), ensure_ascii=False)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/data/test_schema.py -v`
Expected: `9 passed`

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/schema.py tests/unit/data/test_schema.py
git commit -m "feat(data): 標準資料格式 schema（sample/view/labels/card）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: data.tasks（任務登記表）

**Files:**
- Create: `src/vcp/data/tasks.py`
- Test: `tests/unit/data/test_tasks.py`

**Interfaces:**
- Consumes: schema 模型（Task 6）、`RegistryError`、`ValidationFailed`（Task 3）。
- Produces:
  - `StratKey = int | str | float | tuple[int, ...]`；`LabelField = Literal["cls", "targets", "boxes", "masks"]`；常數 `BOUNDS_TOLERANCE_PX = 1.0`。
  - `TaskSpec`（frozen dataclass）：`name`、`label_field`、`validate(sample, card) -> None`、`stratify_key(sample, card) -> StratKey | None`（未標註回 `None`）。
  - `TASKS: dict[str, TaskSpec]`、`register_task(spec)`（重複 → `RegistryError`）、`get_task(name)`（未知 → `RegistryError`）。
  - 登記 `cls`、`multilabel`、`regression`、`det`、`seg`；分層鍵：cls → 類別 id、multilabel → 依 categories 宣告序的 0/1 tuple、regression → 首個 category 名對應的 float、det/seg → 依 categories 宣告序的「是否出現」tuple（向量索引一律對應 `card.categories` 的順序，分布表靠這個對應）。
  - 驗證訊息關鍵字（測試依賴）：`"requires labels.<field>"`、`"unknown category id"`、`"view index"`、`"exceeds view bounds"`、`"targets keys"`、`"must be 0/1"`、`"unknown target names"`、`"must be finite"`。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/test_tasks.py`：

```python
import pytest

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.schema import Box, Category, DatasetCard, Labels, Mask, Sample, SourceInfo, View
from vcp.data.tasks import TASKS, get_task, register_task


def card(task, names=("a", "b", "c")):
    return DatasetCard(
        name="d",
        task=task,
        categories=[Category(id=i, name=n) for i, n in enumerate(names)],
        image_root="/img",
        source=SourceInfo(
            importer="t",
            importer_version="1",
            raw_path="/r",
            raw_hash="h",
            license="",
            url="",
            downloaded_at="",
        ),
        created_at="",
        sample_count=0,
        samples_hash="",
    )


def sample(labels, source="gold", **view_kw):
    return Sample(
        sample_id="s", views=[View(path="a.jpg", **view_kw)], labels=labels, label_source=source
    )


def test_registry_has_five_tasks_and_rejects_unknown_or_duplicate():
    assert set(TASKS) >= {"cls", "multilabel", "regression", "det", "seg"}
    with pytest.raises(RegistryError):
        get_task("pose")
    with pytest.raises(RegistryError):
        register_task(TASKS["cls"])


def test_cls_validation_and_key():
    t = get_task("cls")
    c = card("cls")
    assert t.label_field == "cls"
    t.validate(sample(Labels(cls=2)), c)
    assert t.stratify_key(sample(Labels(cls=2)), c) == 2
    with pytest.raises(ValidationFailed, match="unknown category id"):
        t.validate(sample(Labels(cls=9)), c)
    with pytest.raises(ValidationFailed, match="requires labels.cls"):
        t.validate(sample(Labels(targets={"a": 1})), c)
    t.validate(sample(None, source="none"), c)
    assert t.stratify_key(sample(None, source="none"), c) is None


def test_multilabel_validation_and_vector_key():
    t = get_task("multilabel")
    c = card("multilabel")
    s = sample(Labels(targets={"a": 1, "b": 0, "c": 1}))
    t.validate(s, c)
    assert t.stratify_key(s, c) == (1, 0, 1)
    with pytest.raises(ValidationFailed, match="targets keys"):
        t.validate(sample(Labels(targets={"a": 1})), c)
    with pytest.raises(ValidationFailed, match="must be 0/1"):
        t.validate(sample(Labels(targets={"a": 0.5, "b": 0, "c": 0})), c)
    with pytest.raises(ValidationFailed, match="requires labels.targets"):
        t.validate(sample(Labels(cls=1)), c)


def test_regression_validation_and_float_key():
    t = get_task("regression")
    c = card("regression", names=("age",))
    s = sample(Labels(targets={"age": 37.5}))
    t.validate(s, c)
    assert t.stratify_key(s, c) == 37.5
    with pytest.raises(ValidationFailed, match="unknown target names"):
        t.validate(sample(Labels(targets={"height": 1.0})), c)
    with pytest.raises(ValidationFailed, match="must be finite"):
        t.validate(sample(Labels(targets={"age": float("nan")})), c)
    assert t.stratify_key(sample(Labels(targets={})), c) is None


def test_det_validation_bounds_and_presence_key():
    t = get_task("det")
    c = card("det")
    ok = sample(
        Labels(
            boxes=[Box(x=0, y=0, w=8, h=8, category_id=0), Box(x=1, y=1, w=2, h=2, category_id=2)]
        ),
        width=8,
        height=8,
    )
    t.validate(ok, c)
    assert t.stratify_key(ok, c) == (1, 0, 1)
    t.validate(sample(Labels(boxes=[]), width=8, height=8), c)
    assert t.stratify_key(sample(Labels(boxes=[]), width=8, height=8), c) == (0, 0, 0)
    t.validate(
        sample(Labels(boxes=[Box(x=0, y=0, w=8.9, h=8, category_id=0)]), width=8, height=8), c
    )
    with pytest.raises(ValidationFailed, match="exceeds view bounds"):
        t.validate(
            sample(Labels(boxes=[Box(x=0, y=0, w=10, h=8, category_id=0)]), width=8, height=8), c
        )
    with pytest.raises(ValidationFailed, match="view index"):
        t.validate(sample(Labels(boxes=[Box(x=0, y=0, w=1, h=1, category_id=0, view=3)])), c)
    with pytest.raises(ValidationFailed, match="unknown category id"):
        t.validate(sample(Labels(boxes=[Box(x=0, y=0, w=1, h=1, category_id=7)])), c)
    with pytest.raises(ValidationFailed, match="requires labels.boxes"):
        t.validate(sample(Labels(cls=1)), c)
    t.validate(sample(Labels(boxes=[Box(x=0, y=0, w=100, h=100, category_id=0)])), c)


def test_seg_validation_and_key():
    t = get_task("seg")
    c = card("seg")
    s = sample(Labels(masks=[Mask(category_id=1, rle="x")]))
    t.validate(s, c)
    assert t.stratify_key(s, c) == (0, 1, 0)
    with pytest.raises(ValidationFailed, match="requires labels.masks"):
        t.validate(sample(Labels(boxes=[])), c)
    with pytest.raises(ValidationFailed, match="unknown category id"):
        t.validate(sample(Labels(masks=[Mask(category_id=5, rle="x")])), c)
    with pytest.raises(ValidationFailed, match="view index"):
        t.validate(sample(Labels(masks=[Mask(category_id=1, rle="x", view=2)])), c)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/test_tasks.py -v`
Expected: `ImportError`。

- [ ] **Step 3: 實作**

`src/vcp/data/tasks.py`：

```python
"""Task registry: what a label must look like and how to stratify it, per task type.

Adding a task type = one ``TaskSpec`` + ``register_task``; schema and CLI stay untouched.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, NoReturn

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.schema import Box, DatasetCard, Sample

StratKey = int | str | float | tuple[int, ...]
LabelField = Literal["cls", "targets", "boxes", "masks"]
BOUNDS_TOLERANCE_PX = 1.0


@dataclass(frozen=True)
class TaskSpec:
    name: str
    label_field: LabelField
    validate: Callable[[Sample, DatasetCard], None]
    stratify_key: Callable[[Sample, DatasetCard], StratKey | None]


TASKS: dict[str, TaskSpec] = {}


def register_task(spec: TaskSpec) -> None:
    if spec.name in TASKS:
        raise RegistryError(f"task {spec.name!r} already registered")
    TASKS[spec.name] = spec


def get_task(name: str) -> TaskSpec:
    try:
        return TASKS[name]
    except KeyError:
        raise RegistryError(f"unknown task {name!r}; known: {sorted(TASKS)}") from None


def _fail(sample: Sample, msg: str) -> NoReturn:
    raise ValidationFailed(msg, location=f"sample {sample.sample_id}")


def _cat_ids(card: DatasetCard) -> set[int]:
    return {c.id for c in card.categories}


def _cat_names(card: DatasetCard) -> list[str]:
    return [c.name for c in card.categories]


def _validate_cls(sample: Sample, card: DatasetCard) -> None:
    if sample.labels is None:
        return
    if sample.labels.cls is None:
        _fail(sample, "task cls requires labels.cls")
    if sample.labels.cls not in _cat_ids(card):
        _fail(sample, f"unknown category id {sample.labels.cls}")


def _validate_multilabel(sample: Sample, card: DatasetCard) -> None:
    if sample.labels is None:
        return
    targets = sample.labels.targets
    if targets is None:
        _fail(sample, "task multilabel requires labels.targets")
    names = set(_cat_names(card))
    if set(targets) != names:
        _fail(sample, f"targets keys {sorted(targets)} must equal category names {sorted(names)}")
    bad = {k: v for k, v in targets.items() if v not in (0.0, 1.0)}
    if bad:
        _fail(sample, f"multilabel targets must be 0/1, got {bad}")


def _validate_regression(sample: Sample, card: DatasetCard) -> None:
    if sample.labels is None:
        return
    targets = sample.labels.targets
    if targets is None:
        _fail(sample, "task regression requires labels.targets")
    unknown = sorted(set(targets) - set(_cat_names(card)))
    if unknown:
        _fail(sample, f"unknown target names {unknown}; declared: {_cat_names(card)}")
    bad = {k: v for k, v in targets.items() if not math.isfinite(v)}
    if bad:
        _fail(sample, f"regression targets must be finite, got {bad}")


def _check_view_and_category(
    sample: Sample, card: DatasetCard, idx: int, kind: str, view: int, category_id: int
) -> None:
    if view >= len(sample.views):
        _fail(
            sample,
            f"{kind} {idx}: view index {view} out of range (sample has {len(sample.views)} views)",
        )
    if category_id not in _cat_ids(card):
        _fail(sample, f"{kind} {idx}: unknown category id {category_id}")


def _check_bounds(sample: Sample, idx: int, box: Box) -> None:
    v = sample.views[box.view]
    if v.width is None or v.height is None:
        return
    tol = BOUNDS_TOLERANCE_PX
    if (
        box.x < -tol
        or box.y < -tol
        or box.x + box.w > v.width + tol
        or box.y + box.h > v.height + tol
    ):
        _fail(
            sample,
            f"box {idx} exceeds view bounds: xywh=({box.x}, {box.y}, {box.w}, {box.h}) "
            f"view={v.width}x{v.height}",
        )


def _validate_det(sample: Sample, card: DatasetCard) -> None:
    if sample.labels is None:
        return
    boxes = sample.labels.boxes
    if boxes is None:
        _fail(sample, "task det requires labels.boxes (use [] for a negative sample)")
    for i, b in enumerate(boxes):
        _check_view_and_category(sample, card, i, "box", b.view, b.category_id)
        _check_bounds(sample, i, b)


def _validate_seg(sample: Sample, card: DatasetCard) -> None:
    if sample.labels is None:
        return
    masks = sample.labels.masks
    if masks is None:
        _fail(sample, "task seg requires labels.masks")
    for i, m in enumerate(masks):
        _check_view_and_category(sample, card, i, "mask", m.view, m.category_id)


def _key_cls(sample: Sample, card: DatasetCard) -> StratKey | None:
    return None if sample.labels is None else sample.labels.cls


def _key_vector(sample: Sample, card: DatasetCard) -> StratKey | None:
    if sample.labels is None or sample.labels.targets is None:
        return None
    return tuple(int(sample.labels.targets.get(n, 0)) for n in _cat_names(card))


def _key_regression(sample: Sample, card: DatasetCard) -> StratKey | None:
    if sample.labels is None or sample.labels.targets is None or not card.categories:
        return None
    value = sample.labels.targets.get(card.categories[0].name)
    return None if value is None else float(value)


def _key_presence(sample: Sample, card: DatasetCard) -> StratKey | None:
    if sample.labels is None:
        return None
    items = sample.labels.boxes if sample.labels.boxes is not None else (sample.labels.masks or [])
    present = {it.category_id for it in items}
    return tuple(int(c.id in present) for c in card.categories)


for _spec in (
    TaskSpec("cls", "cls", _validate_cls, _key_cls),
    TaskSpec("multilabel", "targets", _validate_multilabel, _key_vector),
    TaskSpec("regression", "targets", _validate_regression, _key_regression),
    TaskSpec("det", "boxes", _validate_det, _key_presence),
    TaskSpec("seg", "masks", _validate_seg, _key_presence),
):
    register_task(_spec)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/data/test_tasks.py -v`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/tasks.py tests/unit/data/test_tasks.py
git commit -m "feat(data): 任務登記表（cls/multilabel/regression/det/seg 的驗證與分層鍵）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: data.dataset（jsonl 讀寫、Dataset 載入/驗證/儲存）與測試建構器

**Files:**
- Create: `src/vcp/data/dataset.py`
- Modify: `tests/helpers.py`（從空檔填入建構器）、`tests/conftest.py`（加 `roots` fixture）
- Test: `tests/unit/data/test_dataset.py`

**Interfaces:**
- Consumes: `sample_json_line`、schema（Task 6）；`get_task`（Task 7）；`DatasetPaths`、`load_yaml_model`、`dump_yaml_model`、`sha256_file`、錯誤類別（Task 3–4）。
- Produces:
  - `write_samples_jsonl(path: Path, samples: Iterable[Sample]) -> str`：依 `sample_id` 排序、每列一個 `sample_json_line`、`newline="\n"`；回傳檔案 sha256。
  - `read_samples_jsonl(path: Path) -> Iterator[Sample]`：壞列 → `ValidationFailed(..., location=f"{path}:{lineno}")`。
  - `class Dataset`：`__init__(card, samples)`（排序、重複 id → `ValidationFailed("duplicate sample_id ...")`）；屬性 `card`、`samples`、`by_id`；`validate()`（`sample_count` 對不上 → `ValidationFailed`，再逐 sample 跑任務驗證）；`from_parts(card, samples)`（自動填 `sample_count` 後驗證）；`load(name, *, data_root=None, configs_root=None, verify_hash=True)`（hash 不符 → `IntegrityError`）；`save(paths: DatasetPaths)`（寫 jsonl、以 `model_copy` 更新 card 的 `sample_count` / `samples_hash`、寫 yaml）。
  - `tests/helpers.py`：`CATS`、`ML_CATS`、`REG_CATS`、`STAMP`、`make_source()`、`make_card(task, *, name="tiny", categories=None, image_root="/img")`、`det_samples(n, *, seed=0, gold_frac=1.0, group_every=None)`、`cls_samples(n, *, seed=0, gold_frac=1.0, weights=(0.6, 0.3, 0.1))`、`multilabel_samples(n, *, seed=0, gold_frac=1.0, probs=(0.5, 0.2, 0.05))`、`regression_samples(n, *, seed=0)`、`write_images(directory, samples, size=(8, 8))`。sample_id 格式 `s0000`；view 為 `s0000.jpg`、8×8。
  - `tests/conftest.py`：fixture `roots` → `SimpleNamespace(data=<tmp>/data, configs=<tmp>/configs)`，並設定兩個環境變數。

- [ ] **Step 1: 寫建構器與 fixture**

`tests/helpers.py`：

```python
"""Synthetic fixture builders shared by unit tests (a few KB, generated in-process)."""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image

from vcp.data.schema import Box, Category, DatasetCard, Labels, Sample, SourceInfo, View

CATS = [Category(id=0, name="cat"), Category(id=1, name="dog"), Category(id=2, name="bird")]
ML_CATS = [Category(id=0, name="acl"), Category(id=1, name="mcl"), Category(id=2, name="effusion")]
REG_CATS = [Category(id=0, name="age")]
STAMP = "2026-09-02T00:00:00.000Z"


def make_source() -> SourceInfo:
    return SourceInfo(
        importer="test",
        importer_version="1",
        raw_path="/raw",
        raw_hash="deadbeef",
        license="CC0",
        url="https://example.org",
        downloaded_at=STAMP,
    )


def make_card(
    task: str,
    *,
    name: str = "tiny",
    categories: list[Category] | None = None,
    image_root: str = "/img",
) -> DatasetCard:
    return DatasetCard(
        name=name,
        task=task,
        categories=CATS if categories is None else categories,
        image_root=image_root,
        source=make_source(),
        created_at=STAMP,
        sample_count=0,
        samples_hash="",
    )


def _view(i: int) -> View:
    return View(path=f"s{i:04d}.jpg", width=8, height=8)


def det_samples(
    n: int, *, seed: int = 0, gold_frac: float = 1.0, group_every: int | None = None
) -> list[Sample]:
    rng = random.Random(seed)
    out: list[Sample] = []
    for i in range(n):
        gold = rng.random() < gold_frac
        boxes = [
            Box(
                x=rng.randint(0, 4),
                y=rng.randint(0, 4),
                w=rng.randint(1, 4),
                h=rng.randint(1, 4),
                category_id=rng.choice([0, 1, 2]),
            )
            for _ in range(rng.randint(0, 3))
        ]
        group = f"g{i // group_every}" if group_every else None
        out.append(
            Sample(
                sample_id=f"s{i:04d}",
                views=[_view(i)],
                labels=Labels(boxes=boxes) if gold else None,
                label_source="gold" if gold else "none",
                group=group,
            )
        )
    return out


def cls_samples(
    n: int, *, seed: int = 0, gold_frac: float = 1.0, weights: tuple[float, ...] = (0.6, 0.3, 0.1)
) -> list[Sample]:
    rng = random.Random(seed)
    out: list[Sample] = []
    for i in range(n):
        gold = rng.random() < gold_frac
        cls = rng.choices([0, 1, 2], weights=weights)[0]
        out.append(
            Sample(
                sample_id=f"s{i:04d}",
                views=[_view(i)],
                labels=Labels(cls=cls) if gold else None,
                label_source="gold" if gold else "none",
            )
        )
    return out


def multilabel_samples(
    n: int, *, seed: int = 0, gold_frac: float = 1.0, probs: tuple[float, ...] = (0.5, 0.2, 0.05)
) -> list[Sample]:
    rng = random.Random(seed)
    names = [c.name for c in ML_CATS]
    out: list[Sample] = []
    for i in range(n):
        gold = rng.random() < gold_frac
        targets = {name: float(rng.random() < p) for name, p in zip(names, probs, strict=True)}
        out.append(
            Sample(
                sample_id=f"s{i:04d}",
                views=[_view(i)],
                labels=Labels(targets=targets) if gold else None,
                label_source="gold" if gold else "none",
            )
        )
    return out


def regression_samples(n: int, *, seed: int = 0) -> list[Sample]:
    rng = random.Random(seed)
    return [
        Sample(
            sample_id=f"s{i:04d}",
            views=[_view(i)],
            labels=Labels(targets={"age": rng.uniform(0, 100)}),
            label_source="gold",
        )
        for i in range(n)
    ]


def write_images(directory: Path, samples: list[Sample], size: tuple[int, int] = (8, 8)) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for i, s in enumerate(samples):
        for v in s.views:
            Image.new("RGB", size, (i % 256, 64, 128)).save(directory / v.path)
```

`tests/conftest.py`：

```python
from types import SimpleNamespace

import pytest


@pytest.fixture
def roots(tmp_path, monkeypatch):
    data = tmp_path / "data"
    configs = tmp_path / "configs"
    data.mkdir()
    configs.mkdir()
    monkeypatch.setenv("VCP_DATA_ROOT", str(data))
    monkeypatch.setenv("VCP_CONFIGS_ROOT", str(configs))
    return SimpleNamespace(data=data, configs=configs)
```

- [ ] **Step 2: 寫失敗測試**

`tests/unit/data/test_dataset.py`：

```python
import json

import pytest
from helpers import det_samples, make_card

from vcp.core.errors import IntegrityError, RegistryError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset, read_samples_jsonl, write_samples_jsonl
from vcp.data.schema import Box, Labels


def test_write_is_sorted_and_hash_stable(tmp_path):
    samples = det_samples(5, seed=1)
    p1 = tmp_path / "a.jsonl"
    p2 = tmp_path / "b.jsonl"
    h1 = write_samples_jsonl(p1, list(reversed(samples)))
    h2 = write_samples_jsonl(p2, samples)
    assert h1 == h2 == sha256_file(p1)
    ids = [json.loads(line)["sample_id"] for line in p1.read_text(encoding="utf-8").splitlines()]
    assert ids == sorted(ids) and len(ids) == 5
    assert b"\r\n" not in p1.read_bytes()
    assert [s.sample_id for s in read_samples_jsonl(p1)] == ids


def test_read_reports_line_number(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text(
        '{"sample_id":"a","views":[{"path":"a.jpg"}],"label_source":"none"}\n{"sample_id":"b"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed) as ei:
        list(read_samples_jsonl(p))
    assert str(ei.value).endswith("bad.jsonl:2)")


def test_read_rejects_blank_line(tmp_path):
    p = tmp_path / "blank.jsonl"
    p.write_text(
        '{"sample_id":"a","views":[{"path":"a.jpg"}],"label_source":"none"}\n\n', encoding="utf-8"
    )
    with pytest.raises(ValidationFailed, match="blank"):
        list(read_samples_jsonl(p))


def test_duplicate_sample_id_rejected():
    samples = det_samples(2, seed=0)
    with pytest.raises(ValidationFailed, match="duplicate sample_id"):
        Dataset.from_parts(make_card("det"), samples + [samples[0]])


def test_from_parts_fills_count_runs_task_validation_and_rejects_unknown_task():
    ds = Dataset.from_parts(make_card("det"), det_samples(3))
    assert ds.card.sample_count == 3
    bad = det_samples(1)
    bad[0] = bad[0].model_copy(
        update={"labels": Labels(boxes=[Box(x=0, y=0, w=1, h=1, category_id=9)])}
    )
    with pytest.raises(ValidationFailed, match="unknown category id"):
        Dataset.from_parts(make_card("det"), bad)
    with pytest.raises(RegistryError):
        Dataset.from_parts(make_card("pose"), det_samples(1))


def test_validate_checks_sample_count():
    ds = Dataset.from_parts(make_card("det"), det_samples(3))
    ds.card = ds.card.model_copy(update={"sample_count": 99})
    with pytest.raises(ValidationFailed, match="sample_count"):
        ds.validate()


def test_save_and_load_roundtrip(roots):
    paths = DatasetPaths.resolve("ds", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(6, seed=2)
    ds = Dataset.from_parts(make_card("det", name="ds"), samples)
    ds.save(paths)
    assert paths.card_yaml.is_file() and paths.samples_jsonl.is_file()
    assert ds.card.sample_count == 6
    assert ds.card.samples_hash == sha256_file(paths.samples_jsonl)
    loaded = Dataset.load("ds", data_root=roots.data, configs_root=roots.configs)
    assert loaded.card == ds.card
    assert loaded.samples == ds.samples
    assert loaded.by_id[samples[0].sample_id] == samples[0]


def test_save_refuses_name_mismatch(roots):
    paths = DatasetPaths.resolve("other", data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(make_card("det", name="ds"), det_samples(1))
    with pytest.raises(ValidationFailed, match="name"):
        ds.save(paths)


def test_load_detects_tampered_samples_and_missing_files(roots):
    paths = DatasetPaths.resolve("ds", data_root=roots.data, configs_root=roots.configs)
    with pytest.raises(ValidationFailed, match="not found"):
        Dataset.load("ds", data_root=roots.data, configs_root=roots.configs)
    Dataset.from_parts(make_card("det", name="ds"), det_samples(3, seed=0)).save(paths)
    with paths.samples_jsonl.open("a", encoding="utf-8", newline="\n") as f:
        f.write('{"sample_id":"zzz","views":[{"path":"z.jpg"}],"label_source":"none"}\n')
    with pytest.raises(IntegrityError):
        Dataset.load("ds", data_root=roots.data, configs_root=roots.configs)
    with pytest.raises(ValidationFailed, match="sample_count"):
        Dataset.load("ds", data_root=roots.data, configs_root=roots.configs, verify_hash=False)
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/test_dataset.py -v`
Expected: `ImportError`。

- [ ] **Step 4: 實作**

`src/vcp/data/dataset.py`：

```python
"""Dataset = card + samples sorted by id. Every file boundary is validated and hash-checked."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.schema import DatasetCard, Sample, sample_json_line
from vcp.data.tasks import get_task

if TYPE_CHECKING:
    from vcp.data.split import SplitPlan  # noqa: F401  (used by subset() in a later task)


def write_samples_jsonl(path: Path, samples: Iterable[Sample]) -> str:
    """Write samples sorted by id, one JSON object per line, LF newlines. Returns sha256."""
    ordered = sorted(samples, key=lambda s: s.sample_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for s in ordered:
            f.write(sample_json_line(s))
            f.write("\n")
    return sha256_file(path)


def read_samples_jsonl(path: Path) -> Iterator[Sample]:
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            text = line.rstrip("\r\n")
            if not text.strip():
                raise ValidationFailed("blank line in samples file", location=f"{path}:{lineno}")
            try:
                yield Sample.model_validate_json(text)
            except ValidationError as e:
                raise ValidationFailed(str(e), location=f"{path}:{lineno}") from e


class Dataset:
    def __init__(self, card: DatasetCard, samples: Iterable[Sample]) -> None:
        self.card = card
        self.samples: list[Sample] = sorted(samples, key=lambda s: s.sample_id)
        self._by_id: dict[str, Sample] = {}
        for s in self.samples:
            if s.sample_id in self._by_id:
                raise ValidationFailed(f"duplicate sample_id {s.sample_id!r}")
            self._by_id[s.sample_id] = s

    @property
    def by_id(self) -> dict[str, Sample]:
        return self._by_id

    def validate(self) -> None:
        task = get_task(self.card.task)
        if self.card.sample_count != len(self.samples):
            raise ValidationFailed(
                f"card sample_count {self.card.sample_count} != {len(self.samples)} samples",
                location=self.card.name,
            )
        for s in self.samples:
            task.validate(s, self.card)

    @classmethod
    def from_parts(cls, card: DatasetCard, samples: Iterable[Sample]) -> Dataset:
        ds = cls(card, samples)
        ds.card = ds.card.model_copy(update={"sample_count": len(ds.samples)})
        ds.validate()
        return ds

    @classmethod
    def load(
        cls,
        name: str,
        *,
        data_root: Path | None = None,
        configs_root: Path | None = None,
        verify_hash: bool = True,
    ) -> Dataset:
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        if not paths.card_yaml.is_file():
            raise ValidationFailed(f"dataset card not found: {paths.card_yaml}")
        card = load_yaml_model(paths.card_yaml, DatasetCard)
        if card.name != name:
            raise ValidationFailed(
                f"card name {card.name!r} != {name!r}", location=str(paths.card_yaml)
            )
        if not paths.samples_jsonl.is_file():
            raise ValidationFailed(f"samples file not found: {paths.samples_jsonl}")
        if verify_hash:
            actual = sha256_file(paths.samples_jsonl)
            if actual != card.samples_hash:
                raise IntegrityError(
                    f"samples.jsonl sha256 {actual[:12]} != card samples_hash "
                    f"{card.samples_hash[:12]}",
                    location=str(paths.samples_jsonl),
                )
        ds = cls(card, read_samples_jsonl(paths.samples_jsonl))
        ds.validate()
        return ds

    def save(self, paths: DatasetPaths) -> None:
        if paths.name != self.card.name:
            raise ValidationFailed(f"paths name {paths.name!r} != card name {self.card.name!r}")
        digest = write_samples_jsonl(paths.samples_jsonl, self.samples)
        self.card = self.card.model_copy(
            update={"sample_count": len(self.samples), "samples_hash": digest}
        )
        dump_yaml_model(self.card, paths.card_yaml)
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/unit/data/test_dataset.py -v`
Expected: `9 passed`

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/vcp/data/dataset.py tests/helpers.py tests/conftest.py tests/unit/data/test_dataset.py
git commit -m "feat(data): Dataset 載入/驗證/儲存與 samples.jsonl 讀寫；測試建構器

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: 匯入器介面、登記表與 jsonl 直通匯入器

**Files:**
- Create: `src/vcp/data/importers/base.py`, `src/vcp/data/importers/jsonl.py`
- Modify: `src/vcp/data/importers/__init__.py`（從空檔填入登記）
- Test: `tests/unit/data/importers/test_jsonl.py`

**Interfaces:**
- Consumes: `Dataset`、`read_samples_jsonl`（Task 8）；`get_task`（Task 7）；`dir_manifest`、`write_manifest`（Task 3）；`DatasetPaths`（Task 4）；`stamp`（Task 2）。
- Produces（base）：
  - `ImportSpec(BaseModel)`：`importer`、`src: Path`、`name`、`options: dict[str, str] = {}`、`license`、`url`、`downloaded_at`、`notes=""`、`data_root: Path | None = None`、`configs_root: Path | None = None`；方法 `paths() -> DatasetPaths`。
  - `ImportResult(BaseModel, arbitrary_types_allowed)`：`dataset: Dataset`、`rows_read`、`samples_written`、`rows_skipped`、`skipped_reasons_path: Path | None`。
  - `Importer(Protocol)`：`name: str`、`version: str`、`run(spec) -> ImportResult`。
  - `IMPORTERS: dict[str, Importer]`、`register_importer(importer)`（重複 → `RegistryError`）、`get_importer(name)`（未知 → `RegistryError`）。
  - `finalize_import(*, spec, importer, task, categories, image_root, samples, rows_read, skipped: list[dict]) -> ImportResult`：掃 `spec.src` 寫 `raw_manifest.txt` 得 `raw_hash`、組 `SourceInfo` 與 `DatasetCard`（`created_at=stamp()`）、`Dataset.from_parts` 驗證、`save`、有 skipped 則寫 `cache/import_skipped.jsonl`。
- Produces（jsonl）：`JsonlImporter`（`name="jsonl"`、`version="1"`），選項 `task`（必填）、`categories`（JSON 文字或檔案路徑，相對 `src`）、`image_root`（預設 `str(src)`）、`samples`（相對 `src` 的檔名，預設 `samples.jsonl`）；`load_categories(value: str | None, base: Path) -> list[Category]`。
- `vcp.data.importers` 套件匯入時登記 `JsonlImporter()`。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/importers/test_jsonl.py`：

```python
import json

import pytest
from helpers import CATS, det_samples

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.core.hashing import dir_manifest, manifest_hash
from vcp.data.dataset import Dataset, write_samples_jsonl
from vcp.data.importers import IMPORTERS, get_importer, register_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.importers.jsonl import load_categories
from vcp.data.schema import Box, Labels


def _spec(roots, src, **opts):
    return ImportSpec(
        importer="jsonl",
        src=src,
        name="ds",
        options=opts,
        license="CC0",
        url="https://example.org",
        downloaded_at="2026-09-02T00:00:00.000Z",
        data_root=roots.data,
        configs_root=roots.configs,
    )


def _src(tmp_path, samples):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    write_samples_jsonl(src / "samples.jsonl", samples)
    return src


def test_jsonl_import_writes_dataset(roots, tmp_path):
    samples = det_samples(4)
    src = _src(tmp_path, samples)
    (src / "categories.json").write_text(
        json.dumps([c.model_dump() for c in CATS]), encoding="utf-8"
    )
    res = get_importer("jsonl").run(
        _spec(roots, src, task="det", categories="categories.json", image_root=str(src))
    )
    assert (res.rows_read, res.samples_written, res.rows_skipped) == (4, 4, 0)
    assert res.skipped_reasons_path is None
    ds = Dataset.load("ds", data_root=roots.data, configs_root=roots.configs)
    assert ds.samples == samples
    assert ds.card.task == "det"
    assert [c.name for c in ds.card.categories] == ["cat", "dog", "bird"]
    assert ds.card.source.importer == "jsonl" and ds.card.source.license == "CC0"
    assert ds.card.source.raw_path == str(src)
    manifest = roots.data / "datasets" / "ds" / "raw_manifest.txt"
    assert manifest.is_file()
    assert ds.card.source.raw_hash == manifest_hash(dir_manifest(src))
    assert ds.card.created_at.endswith("Z")


def test_jsonl_inline_categories_and_default_image_root(roots, tmp_path):
    src = _src(tmp_path, det_samples(2))
    cats = json.dumps(
        [{"id": 0, "name": "cat"}, {"id": 1, "name": "dog"}, {"id": 2, "name": "bird"}]
    )
    res = get_importer("jsonl").run(_spec(roots, src, task="det", categories=cats))
    assert res.dataset.card.image_root == str(src)
    assert len(res.dataset.card.categories) == 3


def test_jsonl_requires_task_and_validates_samples(roots, tmp_path):
    src = _src(tmp_path, det_samples(2))
    with pytest.raises(ValidationFailed, match="task"):
        get_importer("jsonl").run(_spec(roots, src))
    with pytest.raises(RegistryError):
        get_importer("jsonl").run(_spec(roots, src, task="pose"))
    bad = det_samples(1)
    bad[0] = bad[0].model_copy(
        update={"labels": Labels(boxes=[Box(x=0, y=0, w=1, h=1, category_id=9)])}
    )
    write_samples_jsonl(src / "samples.jsonl", bad)
    with pytest.raises(ValidationFailed, match="unknown category id"):
        get_importer("jsonl").run(_spec(roots, src, task="det", categories="[]"))


def test_jsonl_missing_files_reported(roots, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(ValidationFailed, match="samples file not found"):
        get_importer("jsonl").run(_spec(roots, src, task="det"))
    with pytest.raises(ValidationFailed, match="categories file not found"):
        load_categories("nope.json", src)
    with pytest.raises(ValidationFailed, match="bad categories"):
        load_categories('[{"id": "x"}]', src)
    assert load_categories(None, src) == []


def test_registry():
    assert "jsonl" in IMPORTERS
    with pytest.raises(RegistryError):
        get_importer("nope")
    with pytest.raises(RegistryError):
        register_importer(IMPORTERS["jsonl"])
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/importers/test_jsonl.py -v`
Expected: `ImportError`。

- [ ] **Step 3: 實作**

`src/vcp/data/importers/base.py`：

```python
"""Importer contract and registry. Every importer ends by calling ``finalize_import``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.core.hashing import dir_manifest, write_manifest
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.schema import Category, DatasetCard, Sample, SourceInfo


class ImportSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    importer: str
    src: Path
    name: str
    options: dict[str, str] = Field(default_factory=dict)
    license: str
    url: str
    downloaded_at: str
    notes: str = ""
    data_root: Path | None = None
    configs_root: Path | None = None

    def paths(self) -> DatasetPaths:
        return DatasetPaths.resolve(
            self.name, data_root=self.data_root, configs_root=self.configs_root
        )


class ImportResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    dataset: Dataset
    rows_read: int
    samples_written: int
    rows_skipped: int
    skipped_reasons_path: Path | None


class Importer(Protocol):
    name: str
    version: str

    def run(self, spec: ImportSpec) -> ImportResult: ...


IMPORTERS: dict[str, Importer] = {}


def register_importer(importer: Importer) -> None:
    if importer.name in IMPORTERS:
        raise RegistryError(f"importer {importer.name!r} already registered")
    IMPORTERS[importer.name] = importer


def get_importer(name: str) -> Importer:
    try:
        return IMPORTERS[name]
    except KeyError:
        raise RegistryError(f"unknown importer {name!r}; known: {sorted(IMPORTERS)}") from None


def finalize_import(
    *,
    spec: ImportSpec,
    importer: Importer,
    task: str,
    categories: list[Category],
    image_root: str,
    samples: list[Sample],
    rows_read: int,
    skipped: list[dict[str, Any]],
) -> ImportResult:
    """Common tail of every importer: provenance, card, validation, save, skip report."""
    paths = spec.paths()
    if not spec.src.is_dir():
        raise ValidationFailed(f"source directory not found: {spec.src}")
    raw_hash = write_manifest(dir_manifest(spec.src), paths.raw_manifest)
    source = SourceInfo(
        importer=importer.name,
        importer_version=importer.version,
        raw_path=str(spec.src),
        raw_hash=raw_hash,
        license=spec.license,
        url=spec.url,
        downloaded_at=spec.downloaded_at,
        notes=spec.notes,
    )
    card = DatasetCard(
        name=spec.name,
        task=task,
        categories=categories,
        image_root=image_root,
        source=source,
        created_at=stamp(),
        sample_count=len(samples),
        samples_hash="",
    )
    dataset = Dataset.from_parts(card, samples)
    dataset.save(paths)
    skipped_path: Path | None = None
    if skipped:
        paths.cache_dir.mkdir(parents=True, exist_ok=True)
        skipped_path = paths.cache_dir / "import_skipped.jsonl"
        with skipped_path.open("w", encoding="utf-8", newline="\n") as f:
            for row in skipped:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return ImportResult(
        dataset=dataset,
        rows_read=rows_read,
        samples_written=len(dataset.samples),
        rows_skipped=len(skipped),
        skipped_reasons_path=skipped_path,
    )
```

`src/vcp/data/importers/jsonl.py`：

```python
"""Pass-through importer for a samples.jsonl already in canonical form.

This is the universal escape hatch: any contest format not covered by a registered importer
can be converted by a small project-level script and enter the framework here, with
validation, hashing and card generation still done by the framework.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.data.dataset import read_samples_jsonl
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.schema import Category
from vcp.data.tasks import get_task


def load_categories(value: str | None, base: Path) -> list[Category]:
    """``value`` is inline JSON (starts with ``[``) or a path to a JSON file (relative to base)."""
    if not value:
        return []
    text = value.strip()
    if not text.startswith("["):
        path = Path(text) if Path(text).is_absolute() else base / text
        if not path.is_file():
            raise ValidationFailed(f"categories file not found: {path}")
        text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
        return [Category.model_validate(item) for item in raw]
    except (json.JSONDecodeError, ValidationError, TypeError) as e:
        raise ValidationFailed(f"bad categories: {e}") from e


class JsonlImporter:
    name = "jsonl"
    version = "1"

    def run(self, spec: ImportSpec) -> ImportResult:
        task = spec.options.get("task")
        if not task:
            raise ValidationFailed("jsonl importer needs --opt task=<name>")
        get_task(task)
        categories = load_categories(spec.options.get("categories"), spec.src)
        image_root = spec.options.get("image_root", str(spec.src))
        samples_file = spec.src / spec.options.get("samples", "samples.jsonl")
        if not samples_file.is_file():
            raise ValidationFailed(f"samples file not found: {samples_file}")
        samples = list(read_samples_jsonl(samples_file))
        return finalize_import(
            spec=spec,
            importer=self,
            task=task,
            categories=categories,
            image_root=image_root,
            samples=samples,
            rows_read=len(samples),
            skipped=[],
        )
```

`src/vcp/data/importers/__init__.py`：

```python
"""Importer registry. Importing this package registers the built-in importers."""

from vcp.data.importers.base import (
    IMPORTERS,
    Importer,
    ImportResult,
    ImportSpec,
    finalize_import,
    get_importer,
    register_importer,
)
from vcp.data.importers.jsonl import JsonlImporter

register_importer(JsonlImporter())

__all__ = [
    "IMPORTERS",
    "Importer",
    "ImportResult",
    "ImportSpec",
    "JsonlImporter",
    "finalize_import",
    "get_importer",
    "register_importer",
]
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/data/importers -v`
Expected: `5 passed`

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/importers tests/unit/data/importers
git commit -m "feat(data): 匯入器介面/登記表與 jsonl 直通匯入器

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: 切分方案模型、解析、存取與不變量

**Files:**
- Create: `src/vcp/data/split.py`（本任務只放模型、解析、IO、分組解析、不變量；產生器在 Task 11 加入同一檔）
- Test: `tests/unit/data/test_split_plan.py`

**Interfaces:**
- Consumes: `Dataset`（Task 8）、`DatasetPaths`、`validate_name`（Task 4）、錯誤類別（Task 3）、`Sample`（Task 6）。
- Produces:
  - `Role = Literal["train", "eval", "sealed"]`；`DEFAULT_SUBSETS = "train:train:0.7,valA:eval:0.1,valB:eval:0.1,holdout:sealed:0.1"`；`RATIO_TOL = 1e-6`。
  - `SubsetSpec(name, role, ratio)`；名稱 `^[A-Za-z0-9_]+$`，ratio 0..1。
  - `SplitPlan(plan_id, dataset, dataset_hash, strategy, params, subsets, assignment, created_at)`；驗證恰一個 train、ratio 和為 1、名稱唯一、assignment 值都在 subsets 內；方法 `subset(name) -> SubsetSpec`（未知 → `PlanMismatchError`）、`ids_in(name) -> set[str]`。
  - `parse_subsets(text) -> list[SubsetSpec]`（錯誤 → `ValidationFailed`）。
  - `save_plan(plan, paths) -> Path`（檔案已存在 → `VcpError`；`indent=1`、LF）；`load_plan(paths, plan_id) -> SplitPlan`（不存在 → `PlanMismatchError`；壞檔 → `ValidationFailed`）。
  - `GroupFn = Callable[[Sample], str | None]`；`resolve_group_fn(group_key: str, audit_groups: dict[str, str] | None = None) -> GroupFn`（`auto` → `Sample.group`；`meta.<欄>`；其他 → `ValidationFailed`；audit 群只補沒有明確 group 的 sample）。
  - `assert_plan_invariants(plan, dataset, *, group_of: GroupFn | None = None) -> None`：覆蓋全部 sample、無未知 id、`params["eval_gold_only"]`（預設 True）時非 train 子集零非 gold、group 未被拆；違反 → `InvariantError`，訊息分別含 `"does not cover"`、`"unknown sample"`、`"non-gold"`、`"is split across"`。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/test_split_plan.py`：

```python
import pytest
from helpers import det_samples, make_card

from vcp.core.errors import InvariantError, PlanMismatchError, ValidationFailed, VcpError
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import (
    DEFAULT_SUBSETS,
    SplitPlan,
    assert_plan_invariants,
    load_plan,
    parse_subsets,
    resolve_group_fn,
    save_plan,
)


def test_parse_default_subsets():
    subs = parse_subsets(DEFAULT_SUBSETS)
    assert [(s.name, s.role, s.ratio) for s in subs] == [
        ("train", "train", 0.7),
        ("valA", "eval", 0.1),
        ("valB", "eval", 0.1),
        ("holdout", "sealed", 0.1),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        "train:train:0.5,val:eval:0.4",
        "a:eval:0.5,b:eval:0.5",
        "a:train:0.5,b:train:0.5",
        "train:train:0.5,val:magic:0.5",
        "train:train:0.5,train:eval:0.5",
        "train:train",
        "train:train:abc",
        "tr ain:train:1.0",
    ],
)
def test_parse_rejects(bad):
    with pytest.raises(ValidationFailed):
        parse_subsets(bad)


def _plan(ds, assignment, subsets=None, eval_gold_only=True):
    return SplitPlan(
        plan_id="p1",
        dataset=ds.card.name,
        dataset_hash=ds.card.samples_hash,
        strategy="fixed",
        params={"eval_gold_only": eval_gold_only, "group_key": "auto"},
        subsets=subsets or parse_subsets("train:train:0.5,val:eval:0.5"),
        assignment=assignment,
        created_at="2026-09-02T00:00:00.000Z",
    )


def test_plan_helpers_and_model_validation():
    ds = Dataset.from_parts(make_card("det"), det_samples(2))
    ids = [s.sample_id for s in ds.samples]
    plan = _plan(ds, {ids[0]: "train", ids[1]: "val"})
    assert plan.subset("val").role == "eval"
    assert plan.ids_in("val") == {ids[1]}
    with pytest.raises(PlanMismatchError):
        plan.subset("nope")
    with pytest.raises(ValueError, match="unknown subsets"):
        _plan(ds, {ids[0]: "ghost"})


def test_invariants_pass_and_fail():
    samples = det_samples(6, seed=0, group_every=2)
    ds = Dataset.from_parts(make_card("det"), samples)
    ids = [s.sample_id for s in samples]
    good = {sid: ("train" if i < 4 else "val") for i, sid in enumerate(ids)}
    assert_plan_invariants(_plan(ds, good), ds)
    missing = dict(good)
    missing.pop(ids[0])
    with pytest.raises(InvariantError, match="does not cover"):
        assert_plan_invariants(_plan(ds, missing), ds)
    extra = dict(good)
    extra["ghost"] = "train"
    with pytest.raises(InvariantError, match="unknown sample"):
        assert_plan_invariants(_plan(ds, extra), ds)
    split_group = dict(good)
    split_group[ids[4]] = "train"
    with pytest.raises(InvariantError, match="is split across"):
        assert_plan_invariants(_plan(ds, split_group), ds)


def test_invariant_eval_gold_only():
    samples = det_samples(4, seed=0, gold_frac=0.0)
    ds = Dataset.from_parts(make_card("det"), samples)
    ids = [s.sample_id for s in samples]
    bad = {sid: ("val" if i == 0 else "train") for i, sid in enumerate(ids)}
    with pytest.raises(InvariantError, match="non-gold"):
        assert_plan_invariants(_plan(ds, bad), ds)
    assert_plan_invariants(_plan(ds, bad, eval_gold_only=False), ds)


def test_resolve_group_fn():
    s = det_samples(1)[0].model_copy(update={"group": "g", "meta": {"patient": "p7"}})
    assert resolve_group_fn("auto")(s) == "g"
    assert resolve_group_fn("meta.patient")(s) == "p7"
    assert resolve_group_fn("meta.missing")(s) is None
    assert resolve_group_fn("auto", {"s0000": "dup"})(s) == "g"
    ungrouped = s.model_copy(update={"group": None})
    assert resolve_group_fn("auto", {"s0000": "dup"})(ungrouped) == "dup"
    with pytest.raises(ValidationFailed):
        resolve_group_fn("patient")


def test_save_and_load_plan(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(make_card("det"), det_samples(2))
    plan = _plan(ds, {s.sample_id: "train" for s in ds.samples})
    target = save_plan(plan, paths)
    assert target == paths.plan_json("p1") and target.is_file()
    assert b"\r\n" not in target.read_bytes()
    assert load_plan(paths, "p1") == plan
    with pytest.raises(VcpError, match="already exists"):
        save_plan(plan, paths)
    with pytest.raises(PlanMismatchError, match="not found"):
        load_plan(paths, "p2")
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        load_plan(paths, "p1")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/test_split_plan.py -v`
Expected: `ImportError`。

- [ ] **Step 3: 實作**

`src/vcp/data/split.py`（第一版，Task 11 會在檔尾追加產生器）：

```python
"""Split plans: sample -> subset assignment with roles, and the generators that build them.

A plan is data, committed to git; ``clean_eval_subsets`` (lineage) is derived from it.
Strategies form a registry; ``fixed`` is the only one in v1.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vcp.core.errors import InvariantError, PlanMismatchError, ValidationFailed, VcpError
from vcp.core.paths import DatasetPaths
from vcp.data.schema import Sample

if TYPE_CHECKING:
    from vcp.data.dataset import Dataset

log = logging.getLogger("vcp")

Role = Literal["train", "eval", "sealed"]
DEFAULT_SUBSETS = "train:train:0.7,valA:eval:0.1,valB:eval:0.1,holdout:sealed:0.1"
RATIO_TOL = 1e-6
GroupFn = Callable[[Sample], str | None]


class SubsetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    role: Role
    ratio: float = Field(ge=0.0, le=1.0)


def _check_subsets(subsets: list[SubsetSpec]) -> None:
    if not subsets:
        raise ValueError("at least one subset is required")
    names = [s.name for s in subsets]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate subset names: {names}")
    if sum(s.role == "train" for s in subsets) != 1:
        raise ValueError("exactly one subset must have role=train")
    total = sum(s.ratio for s in subsets)
    if abs(total - 1.0) > RATIO_TOL:
        raise ValueError(f"subset ratios must sum to 1.0, got {total}")


class SplitPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    dataset: str
    dataset_hash: str
    strategy: str
    params: dict[str, Any]
    subsets: list[SubsetSpec]
    assignment: dict[str, str]
    created_at: str

    @model_validator(mode="after")
    def _consistent(self) -> SplitPlan:
        _check_subsets(self.subsets)
        names = {s.name for s in self.subsets}
        bad = sorted({v for v in self.assignment.values() if v not in names})
        if bad:
            raise ValueError(f"assignment references unknown subsets: {bad}")
        return self

    def subset(self, name: str) -> SubsetSpec:
        for s in self.subsets:
            if s.name == name:
                return s
        raise PlanMismatchError(
            f"plan {self.plan_id!r} has no subset {name!r}; known: {[s.name for s in self.subsets]}"
        )

    def ids_in(self, name: str) -> set[str]:
        return {sid for sid, sub in self.assignment.items() if sub == name}


def parse_subsets(text: str) -> list[SubsetSpec]:
    out: list[SubsetSpec] = []
    for chunk in text.split(","):
        parts = chunk.strip().split(":")
        if len(parts) != 3:
            raise ValidationFailed(f"bad subset spec {chunk!r}; expected name:role:ratio")
        name, role, ratio = parts
        try:
            out.append(SubsetSpec(name=name, role=role, ratio=float(ratio)))  # type: ignore[arg-type]
        except ValueError as e:
            raise ValidationFailed(f"bad subset spec {chunk!r}: {e}") from e
    try:
        _check_subsets(out)
    except ValueError as e:
        raise ValidationFailed(str(e)) from e
    return out


def save_plan(plan: SplitPlan, paths: DatasetPaths) -> Path:
    target = paths.plan_json(plan.plan_id)
    if target.exists():
        raise VcpError(
            f"plan file already exists: {target}; plans are immutable, choose a new plan-id"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(plan.model_dump(mode="json"), f, ensure_ascii=False, indent=1)
        f.write("\n")
    return target


def load_plan(paths: DatasetPaths, plan_id: str) -> SplitPlan:
    target = paths.plan_json(plan_id)
    if not target.is_file():
        raise PlanMismatchError(f"plan not found: {target}")
    try:
        return SplitPlan.model_validate_json(target.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValidationFailed(str(e), location=str(target)) from e


def _group_auto(s: Sample) -> str | None:
    return s.group


def _make_meta_group(field: str) -> GroupFn:
    def fn(s: Sample) -> str | None:
        value = s.meta.get(field)
        return None if value is None else str(value)

    return fn


def resolve_group_fn(group_key: str, audit_groups: dict[str, str] | None = None) -> GroupFn:
    """``auto`` -> Sample.group; ``meta.<field>``; audit groups only fill in ungrouped samples."""
    if group_key == "auto":
        base: GroupFn = _group_auto
    elif group_key.startswith("meta."):
        base = _make_meta_group(group_key[len("meta.") :])
    else:
        raise ValidationFailed(f"group_key must be 'auto' or 'meta.<field>', got {group_key!r}")
    if not audit_groups:
        return base

    def with_audit(s: Sample) -> str | None:
        explicit = base(s)
        return explicit if explicit is not None else audit_groups.get(s.sample_id)

    return with_audit


def assert_plan_invariants(
    plan: SplitPlan, dataset: Dataset, *, group_of: GroupFn | None = None
) -> None:
    ids = set(dataset.by_id)
    assigned = set(plan.assignment)
    missing = sorted(ids - assigned)
    if missing:
        raise InvariantError(f"assignment does not cover all samples: missing {missing[:5]}")
    unknown = sorted(assigned - ids)
    if unknown:
        raise InvariantError(f"assignment has unknown sample ids: {unknown[:5]}")
    if plan.params.get("eval_gold_only", True):
        for sub in plan.subsets:
            if sub.role == "train":
                continue
            non_gold = sorted(
                sid for sid in plan.ids_in(sub.name) if dataset.by_id[sid].label_source != "gold"
            )
            if non_gold:
                raise InvariantError(
                    f"subset {sub.name!r} contains non-gold samples: {non_gold[:5]}"
                )
    group_of = group_of or _group_auto
    seen: dict[str, str] = {}
    for s in dataset.samples:
        g = group_of(s)
        if g is None:
            continue
        sub = plan.assignment[s.sample_id]
        if g in seen and seen[g] != sub:
            raise InvariantError(f"group {g!r} is split across subsets {seen[g]!r} and {sub!r}")
        seen.setdefault(g, sub)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/data/test_split_plan.py -v`
Expected: `14 passed`（含 8 個參數化）

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/split.py tests/unit/data/test_split_plan.py
git commit -m "feat(data): 切分方案模型、--subsets 解析、存取與不變量檢查

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: 固定切分產生器（分層、分組、gold-only）與分布表

**Files:**
- Modify: `src/vcp/data/split.py`（追加）
- Test: `tests/unit/data/test_split_generate.py`

**Interfaces:**
- Consumes: Task 10 全部；`get_task`（Task 7）；`stamp`（Task 2）；`validate_name`（Task 4）。
- Produces:
  - `resolve_stratify_fn(stratify_key: str, dataset) -> KeyFn`（`auto` / `none` / `meta.<欄>`）。
  - `normalize_keys(raw: dict[str, StratKey | None]) -> dict[str, NormKey]`：有 tuple → 向量模式（None → 全零）；全是 float/None → 十分位桶 `q0..q9`（None → `"None"`）；否則 `str(key)`。
  - `stratified_take(pool: list[str], keys: dict[str, NormKey], n: int, *, seed: int) -> list[str]`：向量鍵用 `MultilabelStratifiedShuffleSplit(test_size=n, random_state=seed)`；類別鍵用最大餘數法比例配額 + seeded permutation；回傳排序後的 id。
  - `generate_fixed(dataset, subsets, *, seed, stratify_key="auto", group_key="auto", eval_gold_only=True, audit_groups=None) -> tuple[dict[str, str], dict[str, Any]]`：回傳 (assignment, info)，info 含 `units`、`eligible_units`、`forced_train_units`、`audit_group_conflicts`。
  - `build_plan(dataset, *, plan_id, subsets, seed, stratify_key="auto", group_key="auto", eval_gold_only=True, audit_groups=None) -> SplitPlan`：`strategy="fixed"`，params 記錄全部參數 + info + `group_from_audit`，寫前跑 `assert_plan_invariants`。
  - `distribution_table(plan, dataset) -> dict[str, dict[str, int]]`：子集 → 分層標籤 → 計數（cls 用類別名；向量用每個 category 名的出現數；regression 用 `q0..q9`）。
  - `STRATEGIES: dict[str, Callable[..., SplitPlan]] = {"fixed": build_plan}`。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/test_split_generate.py`：

```python
import numpy as np
import pytest
from helpers import (
    ML_CATS,
    REG_CATS,
    cls_samples,
    det_samples,
    make_card,
    multilabel_samples,
    regression_samples,
)

from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.split import (
    DEFAULT_SUBSETS,
    STRATEGIES,
    assert_plan_invariants,
    build_plan,
    distribution_table,
    normalize_keys,
    parse_subsets,
    stratified_take,
)


def _counts(plan):
    out: dict[str, int] = {}
    for sub in plan.assignment.values():
        out[sub] = out.get(sub, 0) + 1
    return out


def test_stratified_take_categorical_largest_remainder():
    pool = [f"a{i}" for i in range(6)] + [f"b{i}" for i in range(3)] + ["c0"]
    keys = {sid: sid[0] for sid in pool}
    taken = stratified_take(pool, keys, 5, seed=0)
    assert len(taken) == 5 and taken == sorted(taken)
    by_key = {k: sum(1 for t in taken if t[0] == k) for k in "abc"}
    assert by_key == {"a": 3, "b": 2, "c": 0}
    assert stratified_take(pool, keys, 0, seed=0) == []
    assert stratified_take(pool, keys, 99, seed=0) == sorted(pool)
    assert stratified_take(pool, keys, 5, seed=0) == taken


def test_stratified_take_vector_path():
    rng = np.random.default_rng(0)
    pool = [f"s{i:03d}" for i in range(100)]
    keys = {sid: tuple(int(x) for x in rng.random(3) < [0.5, 0.2, 0.1]) for sid in pool}
    taken = stratified_take(pool, keys, 50, seed=0)
    assert len(taken) == 50 and taken == sorted(taken)
    assert stratified_take(pool, keys, 50, seed=0) == taken


def test_normalize_keys_modes():
    assert normalize_keys({"a": (1, 0), "b": None}) == {"a": (1, 0), "b": (0, 0)}
    binned = normalize_keys({f"s{i}": float(i) for i in range(100)} | {"n": None})
    assert binned["s0"] == "q0" and binned["s99"] == "q9" and binned["n"] == "None"
    assert normalize_keys({"a": 1, "b": "x", "c": None}) == {"a": "1", "b": "x", "c": "None"}


@pytest.mark.parametrize("seed", [0, 1, 7])
@pytest.mark.parametrize(
    "subsets",
    [
        DEFAULT_SUBSETS,
        "train:train:0.8,val:eval:0.2",
        "train:train:0.6,v1:eval:0.1,v2:eval:0.1,v3:eval:0.1,hold:sealed:0.1",
    ],
)
def test_fixed_split_invariants_and_ratios(seed, subsets):
    ds = Dataset.from_parts(make_card("det"), det_samples(200, seed=seed, gold_frac=0.8))
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets(subsets), seed=seed)
    assert_plan_invariants(plan, ds)
    assert plan.strategy == "fixed" and plan.dataset_hash == ds.card.samples_hash
    n_gold = sum(s.label_source == "gold" for s in ds.samples)
    counts = _counts(plan)
    for sub in plan.subsets:
        if sub.role != "train":
            assert abs(counts.get(sub.name, 0) - round(sub.ratio * n_gold)) <= 1
    assert all(
        plan.assignment[s.sample_id] == "train" for s in ds.samples if s.label_source != "gold"
    )
    assert plan.params["seed"] == seed and plan.params["eval_gold_only"] is True


def test_determinism_and_seed_sensitivity():
    ds = Dataset.from_parts(make_card("det"), det_samples(100, seed=3))
    a = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=42)
    b = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=42)
    c = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=43)
    assert a.assignment == b.assignment
    assert a.assignment != c.assignment


def test_groups_kept_together_and_audit_groups():
    ds = Dataset.from_parts(make_card("det"), det_samples(120, seed=0, group_every=3))
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    assert_plan_invariants(plan, ds)
    assert plan.params["units"] == 40
    ds2 = Dataset.from_parts(make_card("det"), det_samples(60, seed=0))
    audit = {"s0000": "dup1", "s0001": "dup1", "s0002": "dup1"}
    plan2 = build_plan(
        ds2, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0, audit_groups=audit
    )
    assert len({plan2.assignment[k] for k in audit}) == 1
    assert plan2.params["group_from_audit"] is True
    assert plan2.params["audit_group_conflicts"] == 0


def test_audit_group_conflict_counted_but_explicit_group_wins():
    samples = det_samples(10, seed=0)
    samples[0] = samples[0].model_copy(update={"group": "explicit"})
    ds = Dataset.from_parts(make_card("det"), samples)
    plan = build_plan(
        ds,
        plan_id="p",
        subsets=parse_subsets("train:train:0.5,val:eval:0.5"),
        seed=0,
        audit_groups={"s0000": "dup", "s0001": "dup"},
    )
    assert plan.params["audit_group_conflicts"] == 1


def test_no_eval_gold_only_allows_unlabeled_in_eval():
    ds = Dataset.from_parts(make_card("det"), det_samples(100, seed=0, gold_frac=0.0))
    plan = build_plan(
        ds,
        plan_id="p",
        subsets=parse_subsets("train:train:0.5,val:eval:0.5"),
        seed=0,
        eval_gold_only=False,
    )
    assert _counts(plan) == {"train": 50, "val": 50}


def test_cls_stratification_preserves_class_proportions():
    ds = Dataset.from_parts(make_card("cls"), cls_samples(300, seed=0))
    plan = build_plan(
        ds, plan_id="p", subsets=parse_subsets("train:train:0.5,val:eval:0.5"), seed=0
    )
    table = distribution_table(plan, ds)
    for name in ("cat", "dog", "bird"):
        assert abs(table["train"].get(name, 0) - table["val"].get(name, 0)) <= 2


def test_multilabel_uses_iterative_stratification():
    ds = Dataset.from_parts(
        make_card("multilabel", categories=ML_CATS), multilabel_samples(200, seed=0)
    )
    plan = build_plan(
        ds, plan_id="p", subsets=parse_subsets("train:train:0.5,val:eval:0.5"), seed=0
    )
    table = distribution_table(plan, ds)
    for name in [c.name for c in ML_CATS]:
        assert abs(table["train"].get(name, 0) - table["val"].get(name, 0)) <= 4


def test_regression_bins_by_quantile():
    ds = Dataset.from_parts(
        make_card("regression", categories=REG_CATS), regression_samples(200, seed=0)
    )
    plan = build_plan(
        ds, plan_id="p", subsets=parse_subsets("train:train:0.5,val:eval:0.5"), seed=0
    )
    medians = {}
    for sub in ("train", "val"):
        vals = [
            ds.by_id[sid].labels.targets["age"] for sid, s in plan.assignment.items() if s == sub
        ]
        medians[sub] = float(np.median(vals))
    assert abs(medians["train"] - medians["val"]) < 15
    table = distribution_table(plan, ds)
    assert set(table["train"]) <= {f"q{i}" for i in range(10)}


def test_meta_stratify_and_group_keys():
    samples = [
        s.model_copy(update={"meta": {"site": f"S{i % 4}", "patient": f"P{i // 2}"}})
        for i, s in enumerate(det_samples(80, seed=0))
    ]
    ds = Dataset.from_parts(make_card("det"), samples)
    plan = build_plan(
        ds,
        plan_id="p",
        subsets=parse_subsets("train:train:0.5,val:eval:0.5"),
        seed=0,
        stratify_key="meta.site",
        group_key="meta.patient",
    )
    assert_plan_invariants(plan, ds, group_of=lambda s: s.meta["patient"])
    for i in range(0, 80, 2):
        assert plan.assignment[f"s{i:04d}"] == plan.assignment[f"s{i + 1:04d}"]
    with pytest.raises(ValidationFailed):
        build_plan(
            ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0, stratify_key="site"
        )


def test_strategy_registry_and_plan_id_validation():
    assert STRATEGIES["fixed"] is build_plan
    ds = Dataset.from_parts(make_card("det"), det_samples(4))
    with pytest.raises(ValidationFailed):
        build_plan(ds, plan_id="../x", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/test_split_generate.py -v`
Expected: `ImportError`（`build_plan` 等不存在）。

- [ ] **Step 3: 實作（追加到 split.py）**

先在 `split.py` 的匯入區加入（依 isort 順序放進對應位置）：

```python
import math

import numpy as np

from vcp.core.paths import DatasetPaths, validate_name  # 取代原本只匯入 DatasetPaths 的那行
from vcp.core.time import stamp
from vcp.data.tasks import StratKey, get_task
```

並在 `GroupFn` 定義旁加入：

```python
QUANTILE_BINS = 10
KeyFn = Callable[[Sample], StratKey | None]
NormKey = str | tuple[int, ...]
```

然後在檔尾追加：

```python
def _stratify_auto(dataset: Dataset) -> KeyFn:
    task = get_task(dataset.card.task)
    card = dataset.card

    def fn(s: Sample) -> StratKey | None:
        return task.stratify_key(s, card)

    return fn


def _make_meta_key(field: str) -> KeyFn:
    def fn(s: Sample) -> StratKey | None:
        return str(s.meta.get(field))

    return fn


def resolve_stratify_fn(stratify_key: str, dataset: Dataset) -> KeyFn:
    if stratify_key == "auto":
        return _stratify_auto(dataset)
    if stratify_key == "none":
        return lambda s: 0
    if stratify_key.startswith("meta."):
        return _make_meta_key(stratify_key[len("meta.") :])
    raise ValidationFailed(
        f"stratify_key must be 'auto', 'none' or 'meta.<field>', got {stratify_key!r}"
    )


def normalize_keys(raw: dict[str, StratKey | None]) -> dict[str, NormKey]:
    """Vectors stay vectors (None -> zeros); floats become quantile bins; everything else -> str."""
    values = list(raw.values())
    tuples = [v for v in values if isinstance(v, tuple)]
    if tuples:
        lengths = {len(v) for v in tuples}
        if len(lengths) != 1:
            raise InvariantError(f"stratify vectors have inconsistent lengths: {sorted(lengths)}")
        width = lengths.pop()
        return {k: (v if isinstance(v, tuple) else tuple([0] * width)) for k, v in raw.items()}
    floats = [v for v in values if isinstance(v, float)]
    if floats and all(v is None or isinstance(v, float) for v in values):
        edges = np.quantile(np.array(floats), np.linspace(0, 1, QUANTILE_BINS + 1)[1:-1])
        return {
            k: ("None" if v is None else f"q{int(np.searchsorted(edges, v, side='right'))}")
            for k, v in raw.items()
        }
    return {k: str(v) for k, v in raw.items()}


def stratified_take(pool: list[str], keys: dict[str, NormKey], n: int, *, seed: int) -> list[str]:
    """Deterministically pick ``n`` ids from ``pool`` preserving the key distribution."""
    if n <= 0 or not pool:
        return []
    if n >= len(pool):
        return sorted(pool)
    ordered = sorted(pool)
    if isinstance(keys[ordered[0]], tuple):
        from iterstrat.ml_stratifiers import MultilabelStratifiedShuffleSplit

        y = np.array([keys[i] for i in ordered], dtype=int)
        splitter = MultilabelStratifiedShuffleSplit(n_splits=1, test_size=n, random_state=seed)
        _, test_idx = next(splitter.split(np.zeros((len(ordered), 1)), y))
        return sorted(ordered[i] for i in test_idx)
    rng = np.random.default_rng(seed)
    strata: dict[str, list[str]] = {}
    for sid in ordered:
        strata.setdefault(str(keys[sid]), []).append(sid)
    total = len(ordered)
    exact = {k: n * len(ids) / total for k, ids in strata.items()}
    quota = {k: min(len(strata[k]), math.floor(exact[k])) for k in strata}
    remaining = n - sum(quota.values())
    order = sorted(strata, key=lambda k: (-(exact[k] - math.floor(exact[k])), k))
    while remaining > 0:
        progressed = False
        for k in order:
            if remaining == 0:
                break
            if quota[k] < len(strata[k]):
                quota[k] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    taken: list[str] = []
    for k in sorted(strata):
        ids = strata[k]
        perm = rng.permutation(len(ids))
        taken.extend(ids[i] for i in perm[: quota[k]])
    return sorted(taken)


def _combine_keys(ks: list[StratKey | None]) -> StratKey | None:
    """Key of a group of samples: element-wise max for vectors, first labelled sample otherwise."""
    present = [k for k in ks if k is not None]
    if not present:
        return None
    first = present[0]
    if isinstance(first, tuple):
        vectors = [k for k in present if isinstance(k, tuple)]
        return tuple(max(v[i] for v in vectors) for i in range(len(first)))
    return first


def generate_fixed(
    dataset: Dataset,
    subsets: list[SubsetSpec],
    *,
    seed: int,
    stratify_key: str = "auto",
    group_key: str = "auto",
    eval_gold_only: bool = True,
    audit_groups: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Fixed multi-subset split. Returns (assignment, info).

    Units are groups (or single samples). Only all-gold units may enter non-train subsets when
    ``eval_gold_only``. Non-train subsets are carved first (sealed, then eval in reverse
    declaration order); everything left, plus forced units, becomes train.
    """
    try:
        _check_subsets(subsets)
    except ValueError as e:
        raise ValidationFailed(str(e)) from e
    key_fn = resolve_stratify_fn(stratify_key, dataset)
    base_group = resolve_group_fn(group_key, None)
    group_fn = resolve_group_fn(group_key, audit_groups)

    units: dict[str, list[Sample]] = {}
    conflicts = 0
    for s in dataset.samples:
        explicit = base_group(s)
        audit = audit_groups.get(s.sample_id) if audit_groups else None
        if explicit is not None and audit is not None and explicit != audit:
            conflicts += 1
        g = group_fn(s)
        unit_id = f"g:{g}" if g is not None else f"s:{s.sample_id}"
        units.setdefault(unit_id, []).append(s)
    if conflicts:
        log.warning(
            "audit groups disagree with explicit groups for %d samples; explicit wins", conflicts
        )

    eligible: list[str] = []
    forced: list[str] = []
    for uid, members in units.items():
        if eval_gold_only and any(m.label_source != "gold" for m in members):
            forced.append(uid)
        else:
            eligible.append(uid)
    raw_keys = {
        uid: _combine_keys([key_fn(m) for m in sorted(units[uid], key=lambda m: m.sample_id)])
        for uid in eligible
    }
    keys = normalize_keys(raw_keys)

    train_name = next(s.name for s in subsets if s.role == "train")
    order = [s for s in subsets if s.role == "sealed"] + [
        s for s in reversed(subsets) if s.role == "eval"
    ]
    pool = sorted(eligible)
    n_eligible = len(pool)
    assignment: dict[str, str] = {}
    for step, sub in enumerate(order):
        taken = stratified_take(pool, keys, round(sub.ratio * n_eligible), seed=seed + step)
        for uid in taken:
            for m in units[uid]:
                assignment[m.sample_id] = sub.name
        taken_set = set(taken)
        pool = [u for u in pool if u not in taken_set]
    for uid in pool + forced:
        for m in units[uid]:
            assignment[m.sample_id] = train_name
    info = {
        "units": len(units),
        "eligible_units": n_eligible,
        "forced_train_units": len(forced),
        "audit_group_conflicts": conflicts,
    }
    return assignment, info


def build_plan(
    dataset: Dataset,
    *,
    plan_id: str,
    subsets: list[SubsetSpec],
    seed: int,
    stratify_key: str = "auto",
    group_key: str = "auto",
    eval_gold_only: bool = True,
    audit_groups: dict[str, str] | None = None,
) -> SplitPlan:
    validate_name(plan_id)
    assignment, info = generate_fixed(
        dataset,
        subsets,
        seed=seed,
        stratify_key=stratify_key,
        group_key=group_key,
        eval_gold_only=eval_gold_only,
        audit_groups=audit_groups,
    )
    params: dict[str, Any] = {
        "seed": seed,
        "stratify_key": stratify_key,
        "group_key": group_key,
        "eval_gold_only": eval_gold_only,
        "group_from_audit": bool(audit_groups),
        **info,
    }
    plan = SplitPlan(
        plan_id=plan_id,
        dataset=dataset.card.name,
        dataset_hash=dataset.card.samples_hash,
        strategy="fixed",
        params=params,
        subsets=subsets,
        assignment=assignment,
        created_at=stamp(),
    )
    assert_plan_invariants(plan, dataset, group_of=resolve_group_fn(group_key, audit_groups))
    return plan


def distribution_table(plan: SplitPlan, dataset: Dataset) -> dict[str, dict[str, int]]:
    """subset -> stratification label -> count. Vector keys count per-category presence."""
    task = get_task(dataset.card.task)
    card = dataset.card
    keys = normalize_keys({s.sample_id: task.stratify_key(s, card) for s in dataset.samples})
    names = [c.name for c in card.categories]
    id_to_name = {str(c.id): c.name for c in card.categories}
    table: dict[str, dict[str, int]] = {sub.name: {} for sub in plan.subsets}
    for sid, subset_name in plan.assignment.items():
        row = table[subset_name]
        k = keys[sid]
        if isinstance(k, tuple):
            for i, flag in enumerate(k):
                if flag:
                    label = names[i] if i < len(names) else str(i)
                    row[label] = row.get(label, 0) + 1
        else:
            label = id_to_name.get(k, k)
            row[label] = row.get(label, 0) + 1
    return table


STRATEGIES: dict[str, Callable[..., SplitPlan]] = {"fixed": build_plan}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/data/test_split_generate.py tests/unit/data/test_split_plan.py -v`
Expected: 全部 passed（generate 檔 21 個 = 9 個參數化 + 12 個；plan 檔 14 個）。

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/split.py tests/unit/data/test_split_generate.py
git commit -m "feat(data): 固定切分產生器（分層/分組/gold-only）、分布表、策略登記表

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Dataset.subset（sealed 機械執行）與 lineage

**Files:**
- Modify: `src/vcp/data/dataset.py`（加 `subset()` 方法與所需匯入）
- Create: `src/vcp/data/lineage.py`
- Test: `tests/unit/data/test_lineage.py`

**Interfaces:**
- Consumes: `SplitPlan.subset()`、`SplitPlan.ids_in()`、`build_plan`、`parse_subsets`（Task 10–11）；`stamp`（Task 2）；`SealedSubsetError`、`PlanMismatchError`（Task 3）。
- Produces:
  - `Dataset.subset(name, plan, *, unseal=False, reason=None, caller=None, paths: DatasetPaths | None = None) -> list[Sample]`：先核對 `plan.dataset == card.name`（訊息含 `"belongs"`）與 `plan.dataset_hash == card.samples_hash`（訊息含 `"samples_hash"`），不符 → `PlanMismatchError`；未知子集 → `PlanMismatchError`；`role == "sealed"` 時需 `unseal=True`（否則 `SealedSubsetError`，訊息含 `"sealed"`）、非空 `reason`（訊息含 `"reason"`）、`paths`（訊息含 `"paths"`），並在回傳前追加一筆 `{ts, plan_id, dataset_hash, subset, reason, caller}` 到 `paths.unseal_jsonl(plan_id)`。
  - `clean_eval_subsets(plan: SplitPlan, trained_on: set[str]) -> list[str]`：依 plan 宣告序回傳 role 為 eval/sealed、不在 `trained_on`、且成員與訓練成員不相交的子集名，sealed 者加 `(sealed)` 後綴；`trained_on` 為空或含未知名 → `PlanMismatchError`（訊息含 `"unknown"`）。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/test_lineage.py`：

```python
import json

import pytest
from helpers import det_samples, make_card

from vcp.core.errors import PlanMismatchError, SealedSubsetError
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.lineage import clean_eval_subsets
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets


@pytest.fixture
def ds_and_plan(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(make_card("det"), det_samples(60, seed=0))
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    return ds, plan, paths


def test_clean_eval_subsets(ds_and_plan):
    _, plan, _ = ds_and_plan
    assert clean_eval_subsets(plan, {"train"}) == ["valA", "valB", "holdout(sealed)"]
    assert clean_eval_subsets(plan, {"train", "valA"}) == ["valB", "holdout(sealed)"]
    assert clean_eval_subsets(plan, {"train", "valA", "valB"}) == ["holdout(sealed)"]
    assert clean_eval_subsets(plan, {"train", "valA", "valB", "holdout"}) == []
    with pytest.raises(PlanMismatchError, match="unknown"):
        clean_eval_subsets(plan, {"train", "nope"})
    with pytest.raises(PlanMismatchError):
        clean_eval_subsets(plan, set())


def test_subset_returns_members_and_checks_plan(ds_and_plan):
    ds, plan, _ = ds_and_plan
    val_a = ds.subset("valA", plan)
    assert {s.sample_id for s in val_a} == plan.ids_in("valA")
    assert len(val_a) == 6
    stale = plan.model_copy(update={"dataset_hash": "0" * 64})
    with pytest.raises(PlanMismatchError, match="samples_hash"):
        ds.subset("valA", stale)
    other = plan.model_copy(update={"dataset": "other"})
    with pytest.raises(PlanMismatchError, match="belongs"):
        ds.subset("valA", other)
    with pytest.raises(PlanMismatchError):
        ds.subset("nope", plan)


def test_sealed_requires_recorded_unseal(ds_and_plan):
    ds, plan, paths = ds_and_plan
    with pytest.raises(SealedSubsetError, match="sealed"):
        ds.subset("holdout", plan)
    with pytest.raises(SealedSubsetError, match="reason"):
        ds.subset("holdout", plan, unseal=True, paths=paths)
    with pytest.raises(SealedSubsetError, match="paths"):
        ds.subset("holdout", plan, unseal=True, reason="final decision")
    assert not paths.unseal_jsonl("fixed-v1").exists()
    hold = ds.subset(
        "holdout", plan, unseal=True, reason="final decision", caller="test", paths=paths
    )
    assert {s.sample_id for s in hold} == plan.ids_in("holdout")
    lines = paths.unseal_jsonl("fixed-v1").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert len(records) == 1
    rec = records[0]
    assert rec["reason"] == "final decision" and rec["caller"] == "test"
    assert rec["subset"] == "holdout" and rec["plan_id"] == "fixed-v1"
    assert rec["dataset_hash"] == ds.card.samples_hash and rec["ts"].endswith("Z")
    ds.subset("holdout", plan, unseal=True, reason="again", paths=paths)
    assert len(paths.unseal_jsonl("fixed-v1").read_text(encoding="utf-8").splitlines()) == 2
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/test_lineage.py -v`
Expected: `ImportError`（`vcp.data.lineage` 不存在）。

- [ ] **Step 3: 實作**

`src/vcp/data/dataset.py`：匯入區改為

```python
import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import IntegrityError, PlanMismatchError, SealedSubsetError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.schema import DatasetCard, Sample, sample_json_line
from vcp.data.tasks import get_task

if TYPE_CHECKING:
    from vcp.data.split import SplitPlan
```

並在 `Dataset` 類別的 `save()` 之後加入：

```python
    def subset(
        self,
        name: str,
        plan: SplitPlan,
        *,
        unseal: bool = False,
        reason: str | None = None,
        caller: str | None = None,
        paths: DatasetPaths | None = None,
    ) -> list[Sample]:
        """Samples of one subset. A sealed subset opens only with an explicit, recorded unseal."""
        if plan.dataset != self.card.name:
            raise PlanMismatchError(
                f"plan {plan.plan_id!r} belongs to dataset {plan.dataset!r}, not {self.card.name!r}"
            )
        if plan.dataset_hash != self.card.samples_hash:
            raise PlanMismatchError(
                f"plan {plan.plan_id!r} was built on samples_hash {plan.dataset_hash[:12]}, "
                f"dataset now has {self.card.samples_hash[:12]}"
            )
        spec = plan.subset(name)
        if spec.role == "sealed":
            if not unseal:
                raise SealedSubsetError(
                    f"subset {name!r} is sealed; pass unseal=True with a reason to open it"
                )
            if not reason:
                raise SealedSubsetError("unseal requires a non-empty reason")
            if paths is None:
                raise SealedSubsetError("unseal requires paths so the unseal can be recorded")
            record = {
                "ts": stamp(),
                "plan_id": plan.plan_id,
                "dataset_hash": plan.dataset_hash,
                "subset": name,
                "reason": reason,
                "caller": caller or "unknown",
            }
            target = paths.unseal_jsonl(plan.plan_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return [s for s in self.samples if plan.assignment.get(s.sample_id) == name]
```

`src/vcp/data/lineage.py`：

```python
"""Lineage: which subsets are clean evaluation bases for a run trained on given subsets."""

from __future__ import annotations

from vcp.core.errors import PlanMismatchError
from vcp.data.split import SplitPlan


def clean_eval_subsets(plan: SplitPlan, trained_on: set[str]) -> list[str]:
    """Eval/sealed subsets disjoint from everything in ``trained_on``, in plan order."""
    if not trained_on:
        raise PlanMismatchError("trained_on must name at least one subset")
    known = {s.name for s in plan.subsets}
    unknown = sorted(trained_on - known)
    if unknown:
        raise PlanMismatchError(f"unknown subsets in trained_on: {unknown}; known: {sorted(known)}")
    trained_ids = {sid for sid, sub in plan.assignment.items() if sub in trained_on}
    clean: list[str] = []
    for s in plan.subsets:
        if s.role == "train" or s.name in trained_on:
            continue
        if plan.ids_in(s.name) & trained_ids:
            continue
        clean.append(f"{s.name}(sealed)" if s.role == "sealed" else s.name)
    return clean
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/data -v`
Expected: 全部 passed。

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/dataset.py src/vcp/data/lineage.py tests/unit/data/test_lineage.py
git commit -m "feat(data): Dataset.subset 含 sealed 開封留痕；lineage 乾淨基底推導

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: CLI（vcp data import / validate / split / lineage）

**Files:**
- Create: `src/vcp/cli.py`
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: 全部前面任務。特別是 `get_importer`、`ImportSpec`、`Dataset.load`、`build_plan`、`save_plan`、`load_plan`、`parse_subsets`、`distribution_table`、`clean_eval_subsets`、`Verdict`、`exit_code`、`setup_logging`、`logs_dir`、`resolve_data_root`、`DatasetPaths`。
- Produces:
  - `app: typer.Typer`（`vcp`），子群 `data`；命令 `version`、`data import`、`data validate`、`data split`、`data lineage`。
  - `parse_opts(opts: list[str] | None) -> dict[str, str]`（`key=value`，第一個 `=` 分隔；壞格式 → `ValidationFailed`）。
  - `render_table(table: dict[str, dict[str, int]], counts: dict[str, int]) -> str`：第一列 `label <子集...>`，第二列 `(total)`，之後每個分層標籤一列。
  - `run_command(cmd, json_mode, data_root, fn)`：`fn() -> (status, fields, payload, human_lines)`；捕捉 `VcpError` → 其 `status`，其他例外 → `ABORT`；寫日誌；非 JSON 模式印 human_lines 再印 VERDICT 到 stdout；JSON 模式印 `{"cmd","status","fields","result"}` 到 stdout、VERDICT 到 stderr；`typer.Exit(exit_code(status))`。
  - 共通旗標：`--json`、`--data-root`、`--configs-root`。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/test_cli.py`：

```python
import json

import pytest
from helpers import CATS, det_samples
from typer.testing import CliRunner

from vcp.cli import app, parse_opts, render_table
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import write_samples_jsonl

runner = CliRunner()


def _last_verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _import_tiny(roots, tmp_path, name="tiny", n=60):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    write_samples_jsonl(src / "samples.jsonl", det_samples(n, seed=0))
    (src / "cats.json").write_text(json.dumps([c.model_dump() for c in CATS]), encoding="utf-8")
    return runner.invoke(
        app,
        [
            "data",
            "import",
            "--importer",
            "jsonl",
            "--src",
            str(src),
            "--name",
            name,
            "--license",
            "CC0",
            "--url",
            "https://example.org",
            "--downloaded-at",
            "2026-09-02",
            "--opt",
            "task=det",
            "--opt",
            "categories=cats.json",
        ],
    )


def test_help_and_version():
    assert runner.invoke(app, ["--help"]).exit_code == 0
    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0 and "0.1.0" in r.output


def test_import_validate_split_lineage_flow(roots, tmp_path):
    r = _import_tiny(roots, tmp_path)
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert v.startswith("VERDICT cmd=import status=OK") and "samples=60" in v

    r = runner.invoke(app, ["data", "validate", "--name", "tiny"])
    assert r.exit_code == 0 and "status=OK" in _last_verdict(r.output)

    r = runner.invoke(
        app, ["data", "split", "--name", "tiny", "--plan-id", "fixed-v1", "--seed", "1"]
    )
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "status=OK" in v and "train=42" in v and "valA=6" in v and "holdout=6" in v
    assert "(total)" in r.output
    assert (roots.configs / "datasets" / "tiny" / "splits" / "fixed-v1.json").is_file()

    r = runner.invoke(app, ["data", "split", "--name", "tiny", "--plan-id", "fixed-v1"])
    assert r.exit_code == 2 and "already exists" in r.output

    r = runner.invoke(
        app,
        ["data", "lineage", "--name", "tiny", "--plan", "fixed-v1", "--trained-on", "train,valA"],
    )
    assert r.exit_code == 0 and "clean=[valB, holdout(sealed)]" in r.output

    r = runner.invoke(
        app,
        ["data", "lineage", "--name", "tiny", "--plan", "fixed-v1", "--trained-on", "train,nope"],
    )
    assert r.exit_code == 2 and "status=ABORT" in _last_verdict(r.output)


def test_json_mode_puts_result_on_stdout(roots, tmp_path):
    assert _import_tiny(roots, tmp_path).exit_code == 0
    r = runner.invoke(app, ["data", "validate", "--name", "tiny", "--json"])
    assert r.exit_code == 0
    json_line = next(line for line in r.output.splitlines() if line.startswith("{"))
    doc = json.loads(json_line)
    assert doc["cmd"] == "validate" and doc["status"] == "OK"
    assert doc["fields"]["samples"] == 60 and doc["result"]["card"]["task"] == "det"
    assert "VERDICT cmd=validate status=OK" in r.output


def test_custom_subsets_and_failures(roots, tmp_path):
    assert _import_tiny(roots, tmp_path).exit_code == 0
    r = runner.invoke(
        app,
        [
            "data",
            "split",
            "--name",
            "tiny",
            "--plan-id",
            "two",
            "--subsets",
            "train:train:0.8,val:eval:0.2",
        ],
    )
    assert r.exit_code == 0 and "val=12" in _last_verdict(r.output)

    r = runner.invoke(
        app, ["data", "split", "--name", "tiny", "--plan-id", "bad", "--subsets", "train:train:0.5"]
    )
    assert r.exit_code == 1 and "status=FAIL" in _last_verdict(r.output)

    r = runner.invoke(
        app, ["data", "split", "--name", "tiny", "--plan-id", "aud", "--group-from-audit"]
    )
    assert r.exit_code == 2 and "groups.json" in r.output

    r = runner.invoke(app, ["data", "validate", "--name", "missing"])
    assert r.exit_code == 1 and "status=FAIL" in _last_verdict(r.output)

    r = runner.invoke(
        app,
        [
            "data",
            "import",
            "--importer",
            "nope",
            "--src",
            str(tmp_path),
            "--name",
            "x",
            "--license",
            "a",
            "--url",
            "b",
            "--downloaded-at",
            "c",
        ],
    )
    assert r.exit_code == 2 and "RegistryError" in _last_verdict(r.output)


def test_tampered_dataset_fails_validate(roots, tmp_path):
    assert _import_tiny(roots, tmp_path).exit_code == 0
    samples = roots.data / "datasets" / "tiny" / "samples.jsonl"
    samples.write_bytes(
        samples.read_bytes()
        + b'{"sample_id":"zz","views":[{"path":"z.jpg"}],"label_source":"none"}\n'
    )
    r = runner.invoke(app, ["data", "validate", "--name", "tiny"])
    assert r.exit_code == 1 and "IntegrityError" in _last_verdict(r.output)


def test_parse_opts_and_render_table():
    assert parse_opts(["a=1", "b=x=y"]) == {"a": "1", "b": "x=y"}
    assert parse_opts(None) == {}
    with pytest.raises(ValidationFailed):
        parse_opts(["novalue"])
    text = render_table({"train": {"cat": 3}, "val": {"cat": 1, "dog": 2}}, {"train": 3, "val": 3})
    assert text.splitlines()[0].split() == ["label", "train", "val"]
    assert "(total)" in text and "dog" in text
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/test_cli.py -v`
Expected: `ImportError`（`vcp.cli` 不存在）。

- [ ] **Step 3: 實作**

`src/vcp/cli.py`：

```python
"""``vcp`` command line. Every command ends with a VERDICT line and never prompts."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from vcp import __version__
from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.log import FieldValue, Status, Verdict, exit_code, setup_logging
from vcp.core.paths import DatasetPaths, logs_dir, resolve_data_root
from vcp.data.dataset import Dataset
from vcp.data.importers import ImportSpec, get_importer
from vcp.data.lineage import clean_eval_subsets
from vcp.data.split import (
    DEFAULT_SUBSETS,
    build_plan,
    distribution_table,
    load_plan,
    parse_subsets,
    save_plan,
)

app = typer.Typer(no_args_is_help=True, add_completion=False, help="vision contest pipeline")
data_app = typer.Typer(no_args_is_help=True, help="dataset commands")
app.add_typer(data_app, name="data")

CmdResult = tuple[Status, dict[str, FieldValue], Any, list[str]]

JsonOpt = Annotated[bool, typer.Option("--json", help="JSON result to stdout, VERDICT to stderr")]
DataRootOpt = Annotated[Path | None, typer.Option("--data-root", help="override VCP_DATA_ROOT")]
ConfigsRootOpt = Annotated[
    Path | None, typer.Option("--configs-root", help="override VCP_CONFIGS_ROOT")
]
NameOpt = Annotated[str, typer.Option("--name", help="dataset name")]


@app.callback()
def _root() -> None:
    """vcp: vision contest pipeline."""


@app.command("version")
def version_cmd() -> None:
    typer.echo(__version__)


def parse_opts(opts: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in opts or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValidationFailed(f"--opt expects key=value, got {item!r}")
        out[key] = value
    return out


def render_table(table: dict[str, dict[str, int]], counts: dict[str, int]) -> str:
    subsets = list(table)
    labels = sorted({label for row in table.values() for label in row})
    header = ["label", *subsets]
    rows = [["(total)", *[str(counts.get(s, 0)) for s in subsets]]]
    rows += [[label, *[str(table[s].get(label, 0)) for s in subsets]] for label in labels]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]

    def fmt(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    return "\n".join([fmt(header), *(fmt(r) for r in rows)])


def _logger(data_root: Path | None) -> logging.Logger:
    try:
        return setup_logging(logs_dir(resolve_data_root(data_root)))
    except OSError:
        return logging.getLogger("vcp")


def run_command(
    cmd: str, json_mode: bool, data_root: Path | None, fn: Callable[[], CmdResult]
) -> None:
    logger = _logger(data_root)
    payload: Any = None
    human: list[str] = []
    try:
        status, fields, payload, human = fn()
    except VcpError as e:
        status, fields = e.status, {"reason": f"{type(e).__name__}: {e}"}  # type: ignore[assignment]
        logger.error("command failed", exc_info=True, extra={"vcp": {"cmd": cmd}})
    except Exception as e:
        status, fields = "ABORT", {"reason": f"{type(e).__name__}: {e}"}
        logger.error("command aborted", exc_info=True, extra={"vcp": {"cmd": cmd}})
    verdict = Verdict(cmd=cmd, status=status, fields=fields)
    logger.info(verdict.line(), extra={"vcp": {"cmd": cmd, "status": status}})
    if json_mode:
        doc = {"cmd": cmd, "status": status, "fields": fields, "result": payload}
        typer.echo(json.dumps(doc, ensure_ascii=False, default=str))
        typer.echo(verdict.line(), err=True)
    else:
        for line in human:
            typer.echo(line)
        typer.echo(verdict.line())
    raise typer.Exit(code=exit_code(status))


@data_app.command("import")
def import_cmd(
    importer: Annotated[str, typer.Option("--importer", help="registered importer name")],
    src: Annotated[Path, typer.Option("--src", help="source directory")],
    name: NameOpt,
    license_: Annotated[str, typer.Option("--license", help="license of the raw data")],
    url: Annotated[str, typer.Option("--url", help="where the raw data came from")],
    downloaded_at: Annotated[str, typer.Option("--downloaded-at", help="UTC date of download")],
    notes: Annotated[str, typer.Option("--notes")] = "",
    opt: Annotated[
        list[str] | None, typer.Option("--opt", help="importer option key=value (repeatable)")
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Raw data -> canonical dataset (dataset.yaml + samples.jsonl)."""

    def fn() -> CmdResult:
        spec = ImportSpec(
            importer=importer,
            src=src,
            name=name,
            options=parse_opts(opt),
            license=license_,
            url=url,
            downloaded_at=downloaded_at,
            notes=notes,
            data_root=data_root,
            configs_root=configs_root,
        )
        res = get_importer(importer).run(spec)
        status: Status = "WARN" if res.rows_skipped else "OK"
        fields: dict[str, FieldValue] = {
            "name": name,
            "task": res.dataset.card.task,
            "samples": res.samples_written,
            "rows_read": res.rows_read,
            "rows_skipped": res.rows_skipped,
        }
        if res.skipped_reasons_path is not None:
            fields["skipped_reasons"] = str(res.skipped_reasons_path)
        human = [
            f"imported {res.samples_written} samples into dataset {name!r} "
            f"(task={res.dataset.card.task})"
        ]
        return status, fields, {"card": res.dataset.card.model_dump(mode="json")}, human

    run_command("import", json_mode, data_root, fn)


@data_app.command("validate")
def validate_cmd(
    name: NameOpt,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Re-validate card + samples and verify samples_hash."""

    def fn() -> CmdResult:
        ds = Dataset.load(name, data_root=data_root, configs_root=configs_root)
        short = ds.card.samples_hash[:12]
        fields: dict[str, FieldValue] = {
            "name": name,
            "task": ds.card.task,
            "samples": len(ds.samples),
            "samples_hash": short,
        }
        human = [f"dataset {name!r}: {len(ds.samples)} samples, task={ds.card.task}, hash={short}"]
        return "OK", fields, {"card": ds.card.model_dump(mode="json")}, human

    run_command("validate", json_mode, data_root, fn)


@data_app.command("split")
def split_cmd(
    name: NameOpt,
    plan_id: Annotated[str, typer.Option("--plan-id", help="new plan id (immutable once written)")],
    seed: Annotated[int, typer.Option("--seed")] = 42,
    subsets: Annotated[
        str, typer.Option("--subsets", help="name:role:ratio,... roles: train|eval|sealed")
    ] = DEFAULT_SUBSETS,
    stratify_key: Annotated[
        str, typer.Option("--stratify-key", help="auto | none | meta.<field>")
    ] = "auto",
    group_key: Annotated[str, typer.Option("--group-key", help="auto | meta.<field>")] = "auto",
    group_from_audit: Annotated[
        bool, typer.Option("--group-from-audit", help="also use cache/audit/groups.json")
    ] = False,
    no_eval_gold_only: Annotated[
        bool, typer.Option("--no-eval-gold-only", help="allow non-gold samples in eval/sealed")
    ] = False,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Generate a fixed multi-subset split plan and commit-ready plan file."""

    def fn() -> CmdResult:
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        target = paths.plan_json(plan_id)
        if target.exists():
            raise VcpError(
                f"plan file already exists: {target}; plans are immutable, choose a new plan-id"
            )
        ds = Dataset.load(name, data_root=data_root, configs_root=configs_root)
        audit_groups: dict[str, str] | None = None
        if group_from_audit:
            groups_file = paths.cache_dir / "audit" / "groups.json"
            if not groups_file.is_file():
                raise VcpError(
                    f"--group-from-audit needs {groups_file}; run `vcp data audit` first"
                )
            audit_groups = json.loads(groups_file.read_text(encoding="utf-8"))
        plan = build_plan(
            ds,
            plan_id=plan_id,
            subsets=parse_subsets(subsets),
            seed=seed,
            stratify_key=stratify_key,
            group_key=group_key,
            eval_gold_only=not no_eval_gold_only,
            audit_groups=audit_groups,
        )
        save_plan(plan, paths)
        table = distribution_table(plan, ds)
        counts = {sub.name: len(plan.ids_in(sub.name)) for sub in plan.subsets}
        status: Status = "WARN" if plan.params.get("audit_group_conflicts") else "OK"
        fields: dict[str, FieldValue] = {"plan": plan_id, **counts, "seed": seed}
        human = [f"plan {plan_id!r} written to {target}", render_table(table, counts)]
        payload = {
            "plan_path": str(target),
            "counts": counts,
            "distribution": table,
            "params": plan.params,
        }
        return status, fields, payload, human

    run_command("split", json_mode, data_root, fn)


@data_app.command("lineage")
def lineage_cmd(
    name: NameOpt,
    plan: Annotated[str, typer.Option("--plan", help="plan id")],
    trained_on: Annotated[
        str, typer.Option("--trained-on", help="comma-separated subset names a run trained on")
    ],
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Which subsets are clean evaluation bases for a run trained on the given subsets."""

    def fn() -> CmdResult:
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        split_plan = load_plan(paths, plan)
        trained = {t.strip() for t in trained_on.split(",") if t.strip()}
        clean = clean_eval_subsets(split_plan, trained)
        fields: dict[str, FieldValue] = {
            "plan": plan,
            "trained_on": ",".join(sorted(trained)),
            "clean": ",".join(clean) or "-",
        }
        human = [f"clean=[{', '.join(clean)}]"]
        return "OK", fields, {"clean": clean, "trained_on": sorted(trained)}, human

    run_command("lineage", json_mode, data_root, fn)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/test_cli.py -v`
Expected: `6 passed`

Run: `uv run vcp --help`
Expected: 列出 `data` 與 `version`。

Run: `uv run vcp data split --help`
Expected: 列出 `--subsets`、`--stratify-key`、`--group-key`、`--group-from-audit`、`--no-eval-gold-only`、`--json`。

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/vcp/cli.py tests/unit/test_cli.py
git commit -m "feat(cli): vcp data import/validate/split/lineage，VERDICT 收尾與 --json

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 14: 整體驗證、覆蓋率與驗收自查

**Files:**
- Modify（僅在需要時）: 任何為了達到覆蓋率或修 lint 而需要補的測試。

**Interfaces:** 無新介面。

- [ ] **Step 1: 全套測試與覆蓋率**

Run: `uv run pytest --cov=vcp --cov-report=term-missing`
Expected: 全部 passed；`TOTAL` 覆蓋率 ≥ 80%（`fail_under = 80` 未達會讓命令非零退出）。若未達，對 `term-missing` 列出的行補單元測試（最可能缺的是 `cli.py` 的 `_logger` OSError 分支與 `run_command` 的非 VcpError 例外分支：可用 `monkeypatch` 讓 `setup_logging` 拋 `OSError`，以及註冊一個 `run()` 會拋 `RuntimeError` 的假匯入器後呼叫 `data import`，斷言 `status=ABORT` 且 exit 2）。

- [ ] **Step 2: Lint**

Run: `uv run ruff check .`
Expected: `All checks passed!`

Run: `uv run ruff format --check .`
Expected: 若有格式差異，執行 `uv run ruff format .` 後重跑測試。

- [ ] **Step 3: 端到端手動走一遍（Windows 原生）**

```bash
uv run vcp data import --importer jsonl --src tests/fixtures/e2e --name e2e --license CC0 --url https://example.org --downloaded-at 2026-09-02 --opt task=det --opt "categories=[{\"id\":0,\"name\":\"cat\"},{\"id\":1,\"name\":\"dog\"},{\"id\":2,\"name\":\"bird\"}]"
```

事前用下列一次性腳本產生 `tests/fixtures/e2e/samples.jsonl`（不進 git 也可，`tests/fixtures/e2e/` 已在 `.gitignore` 之外，建議進 git 作為手動走查夾具，約 10 KB）：

```bash
uv run python -c "from pathlib import Path; from helpers import det_samples; from vcp.data.dataset import write_samples_jsonl; p=Path('tests/fixtures/e2e'); p.mkdir(parents=True, exist_ok=True); write_samples_jsonl(p/'samples.jsonl', det_samples(80, seed=5))"
```

（`helpers` 需在路徑上：以 `PYTHONPATH=tests` 執行，PowerShell 為 `$env:PYTHONPATH='tests'; uv run python -c "..."`。）

接著：

```bash
uv run vcp data validate --name e2e
uv run vcp data split --name e2e --plan-id fixed-v1 --seed 42
uv run vcp data lineage --name e2e --plan fixed-v1 --trained-on train,valA
uv run vcp data validate --name e2e --json
```

Expected：四個命令各以 `VERDICT ... status=OK` 收尾；`configs/datasets/e2e/dataset.yaml` 與 `configs/datasets/e2e/splits/fixed-v1.json` 出現；`--json` 的 stdout 是單行 JSON。走查完成後**刪除** `configs/datasets/e2e/` 與 `C:/vcp-data/datasets/e2e/`（或你設定的資料根目錄），避免把示範資料集提交進 git。

- [ ] **Step 4: 對照 spec §13 驗收條件**

本計畫覆蓋：1（`uv sync`、`vcp --help`、pytest 全綠、覆蓋率）、7（lineage 三種輸入）、8（sealed 與 unseal 記錄）、9（ruff 與 banned-api 測試）、10 的後半（`jsonl` 直通）、11（`--subsets` 兩子集）。其餘（2、3、4、5、6、10 前半的 `image_csv`）屬 Plan 2（其餘匯入器、匯出器、稽核、materialize、海廢真實資料整合測試）。

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: Plan 1 整體驗證；補覆蓋率測試與 e2e 夾具

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## 自我檢查紀錄（撰寫計畫時已執行）

- **Spec 覆蓋**：§4 骨架 → Task 1–5；§5 資料模型 → Task 6–8；§6.1 `jsonl` → Task 9（其餘六個匯入器、§6.2 匯出、§6.3 materialize → Plan 2）；§7 切分與 lineage → Task 10–12；§8 稽核 → Plan 2；§9 CLI 四個命令 → Task 13（`export`、`materialize`、`audit` → Plan 2）；§10 錯誤處理 → Task 3、13；§11 測試 → 各任務 + Task 14（`realdata` 整合測試 → Plan 2）。
- **技術驗證**：ruff TID251 對三種取時寫法的攔截、iterstrat 0.1.9 + scikit-learn 1.9.0 + 整數 `test_size` 的可用性與確定性，皆已在 2026-09-02 以 `uv run --with` 實測通過。
- **型別一致性**：`StratKey`（tasks）→ `normalize_keys` 輸入；`NormKey` → `stratified_take`；`GroupFn` 在 `resolve_group_fn` / `assert_plan_invariants` / `build_plan` 一致；`CmdResult` 四元組在 `run_command` 與四個命令一致；`DatasetPaths.plan_json` / `unseal_jsonl` 在 split / dataset / cli 一致。
- **已知取捨**：`Dataset.subset` 與 `split.py` 互相引用型別，皆以 `TYPE_CHECKING` 匯入避免循環；`SplitPlan` 的 `strategy` 為字串 + `STRATEGIES` 登記表，K-fold 只需新增登記項。
