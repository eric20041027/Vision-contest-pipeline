# vcp 訓練層（子專案 3）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `vcp train` 訓練層：包在任何訓練命令外面的 `run`（開始就寫 run.yaml、`trained_on` 由 export manifest 推導、環境快照、console 落檔、checkpoint sha 登記、上傳驗證）、事後補傳的 `upload`、唯讀的 `status`，以及給自寫 loop 的 `MaterializedReader` 與 `Session`。

**Architecture:** 新套件 `src/vcp/train/`（`schema`、`records`、`env`、`checkpoints`、`upload`、`run`、`status`、`reader`、`session`），CLI 新增 `src/vcp/cli_train.py` 掛成 `vcp train`。產出是量測層格式的 run（`run.yaml`）加上訓練專屬的 `train.yaml`（快照，整份重寫）、`train.log.jsonl`（只增事件）、`train/`（console、config 副本、環境快照）。資料層只加一個公開函式 `assert_plan_matches`；量測層、融合層零改動。

**Tech Stack:** Python 3.12（uv）、pydantic v2、typer、numpy、Pillow（既有）、`subprocess`（訓練命令、環境探針、rclone、nvidia-smi、git）；不新增執行期依賴，rclone 只 shell out。

**Spec:** `docs/superpowers/specs/2026-09-05-vcp-training-layer-design.md`（v1，全部章節已核可）。量測層 run 格式見 `docs/superpowers/specs/2026-09-04-vcp-measurement-layer-design.md` §4.2；materialize manifest 見資料層 spec §15.4 第 21 條。

## Global Constraints

- 每個專案命令前綴 `uv run`；pytest 的 `addopts=-q` 會藏摘要，看細節用 `uv run pytest -o addopts="" -q ...`。
- 三條機械鐵則（`CLAUDE.md`）：取時只用 `vcp.core.time.utc_now()` / `stamp()`（ruff TID251 會擋 `datetime.now`、`time.time`；量 duration 用兩次 `utc_now()` 相減）；每個 CLI 命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT` 收尾、exit 0 / 0 / 1 / 2、永不互動、`--json` 時 JSON 到 stdout、VERDICT 到 stderr（子程序的 console 回顯在 `--json` 時改到 stderr）；venv 隔離——訓練命令在 `--venv` 裡跑，vcp 核心 venv 從不 import 訓練框架。
- 通用性：`src/vcp` 不得出現比賽名稱、框架名稱不得寫死成邏輯分支（`framework` 只是自由文字）。
- 檔案語意：`train.yaml` 每次事件後整份重寫（`dump_yaml_model`）；`train.log.jsonl` 只 append；`run.yaml` 只在建立與寫 `weights_hash` 時寫；`raw/` 永不修改；checkpoint 不搬動，只記路徑（`store_path`）與 sha。
- 全有或全無：`run` 在寫第一個檔之前做完 spec §6.1 第 1–4 步的所有檢查（含訓練命令可執行）。
- 寫入會被 hash 或進 git 的文字檔一律 `encoding="utf-8", newline="\n"`；讀檔指定 `encoding="utf-8"`；JSON 用 `json.dumps(..., ensure_ascii=False)`；console 檔 `errors="replace"`。
- 錯誤語意：選項 / 檔案 / run 狀態 / `trained_on` / `--final` / 撞名 / 驗證失敗 → `ValidationFailed`（FAIL）；export manifest 或 plan 與 dataset 不符 → `PlanMismatchError`（ABORT）；venv 的 python 不存在、探針失敗、rclone 不在 PATH → `VcpError`（ABORT）；訓練命令 exit ≠ 0 或被中斷 → 命令 `status=FAIL exit_code=N`（不是例外）。`reason=` 固定字彙（`run_exists`、`run_bound_elsewhere`、`final_ambiguous`、`name_collision`、`trained_on_mismatch`）是訊息前綴；`fields` 只放 `run=` `attempt=` `export=` `checkpoint=` `dest=`。
- 測試：`tests/conftest.py` 的 autouse fixture 已把兩個根目錄指到 tmp；需要真實路徑用 `roots` fixture（`roots.data`、`roots.configs`）；夾具 helper 在 `tests/helpers.py`（`from helpers import ...`）；訓練命令一律用 `sys.executable` 跑測試自己寫的假腳本；rclone 一律經注入的 runner；真資料測試放 `tests/integration/`、標記 `realdata`、資料缺席即 skip。
- ruff：line-length 100、select `E F I UP B TID`；`uv run ruff format --check .` 也要過。覆蓋率門檻 80%。
- 檔案上限 800 行；函式盡量 < 50 行。
- 每個 commit 訊息結尾加空行與 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`；永不 `git add -A`。

### 計畫層決定（spec 沒寫死、本計畫定案的小事）

1. **`--` 的執行**：Typer / Click 在 `--` 之後把剩餘參數原封放進 `ctx.args` 並把 `--` 本身拿掉（已實測：`--seed 3 -- python train.py --seed 9` → vcp 的 seed=3，命令 `['python','train.py','--seed','9']`），所以「沒有 `--` 就 FAIL」無法可靠偵測。改為：命令必須非空且第一個 token 不以 `-` 開頭，否則 FAIL；文件寫明 `--` 是分隔符（訓練命令的選項名與 vcp 的撞名時必要）。
2. **`assert_plan_matches(plan, card)`** 新增在 `src/vcp/data/split.py`（plan 的 `dataset` / `dataset_hash` 對 `DatasetCard`），本層用它；`dataset.py` / `ingest.py` / `fuse/members.py` 既有的三份留給 Plan 4 後記待辦 2，本計畫不動。
3. **`execute()` 的 `on_line` 回呼**是 console 回顯的接縫，也是測試中斷路徑的接縫：測試在回呼裡拋 `KeyboardInterrupt`，程式碼走 terminate → `interrupted`。
4. **rclone 經 `Runner`**（`Callable[[list[str]], subprocess.CompletedProcess[str]]`）注入；預設 runner 用 `subprocess.run`，且只有預設 runner 才檢查 `shutil.which("rclone")`。
5. **環境探針在測試裡用當前直譯器**（`python=None` → `sys.executable`）；`--venv` 只測 python 路徑解析。
6. **`upload()` 對已在目的地且 sha 相同的檔也回一筆 `verified=True` 的紀錄**（計入 `skipped=`），`merge_uploads` 以 `(dest, name, sha256)` 去重——否則手動放到目的地的檔永遠算 `unbacked`。
7. **命令可執行性預檢**：`shutil.which(command[0], path=子程序的 PATH)` 為 `None` → `ValidationFailed`（FAIL）在寫任何檔之前；否則 `Popen` 的 `FileNotFoundError` 會留下 `running` 的 attempt。

---

## File Structure

| 檔案 | 責任 | 任務 |
|---|---|---|
| `src/vcp/train/__init__.py`、`src/vcp/train/schema.py` | 套件說明；`ExportRef`、`ConfigRef`、`Attempt`、`CheckpointRecord`、`UploadRecord`、`TrainRecord`、`GitInfo`、`EnvSnapshot`、`EVENTS` | 1 |
| `src/vcp/train/records.py` | `train.yaml` 讀寫、`train.log.jsonl` 追加與讀取、路徑 helper | 1 |
| `src/vcp/train/env.py` | 探針、`venv_python`、`gpus`、`git_info`、`snapshot` | 2 |
| `src/vcp/train/checkpoints.py` | glob 展開、登記、`resolve_final`、`drift` / `missing` | 3 |
| `src/vcp/train/upload.py` | `dest_kind`、本機與 rclone 上傳、驗證、`merge_uploads` | 4 |
| `src/vcp/data/split.py` | `assert_plan_matches` | 5 |
| `src/vcp/train/run.py`、`src/vcp/cli_train.py`（新）、`src/vcp/cli.py` | `RunSpec` / `train_run` / `execute` / `derive_trained_on` / `config_hash` / `child_env`；`vcp train run`；掛 `train_app` | 5 |
| `src/vcp/train/status.py`、`cli_train.py` | `status()`；`vcp train upload` / `status` | 6 |
| `src/vcp/train/reader.py`、`src/vcp/train/session.py`、`src/vcp/train/__init__.py` | `MaterializedReader` / `Record`；`Session`；套件匯出 | 7 |
| `tests/unit/test_e2e_train.py`、`tests/integration/test_rsna_knee_train.py`、`README.md`、`CLAUDE.md` | 端到端、真資料、文件 | 8 |
| `tests/unit/train/...`、`tests/unit/test_cli_train.py` | 每任務的單元測試 | 各任務 |

任務順序 1 → 8。Task 1 的 schema / records 被所有後續任務使用；Task 3、4 被 5、6、7 使用；Task 5 的 `run` 被 8 使用。

---

### Task 1: 訓練紀錄的資料模型與檔案讀寫（spec §4.2、§4.3、§4.4、§5）

**Files:**
- Create: `src/vcp/train/__init__.py`
- Create: `src/vcp/train/schema.py`
- Create: `src/vcp/train/records.py`
- Create: `tests/unit/train/__init__.py`（空檔）
- Test: `tests/unit/train/test_schema_records.py`

**Interfaces:**
- Consumes: `vcp.core.config.load_yaml_model / dump_yaml_model`、`vcp.core.time.stamp`、`vcp.core.errors.ValidationFailed`、`vcp.measure.runs.run_dir`。
- Produces:
  - `schema`: `AttemptStatus = Literal["running", "finished", "failed", "interrupted"]`、`CheckpointSource = Literal["glob", "session"]`、`UploadKind = Literal["rclone", "local"]`、`EVENTS = ("started", "env", "checkpoint", "uploaded", "finished", "note")`；模型 `ExportRef(dir, subset, format, manifest_sha256, sample_count)`、`ConfigRef(path, sha256, copy)`、`Attempt(n, started_at, finished_at=None, duration_s=None, exit_code=None, status="running", console, env=None)`、`CheckpointRecord(path, sha256, bytes, registered_at, attempt, final=False, source="glob")`、`UploadRecord(dest, kind, name, sha256, verified, uploaded_at)`、`TrainRecord(run_id, dataset, plan_id, trained_on, exports=[], config=None, config_hash, seed=None, framework="", venv=None, cwd, command, attempts=[], checkpoints=[], uploads=[], notes="")`、`GitInfo(commit, dirty)`、`EnvSnapshot(python, executable, platform, hostname, packages, torch=None, cuda=None, cudnn=None, gpus=[], nvidia_driver=None, vcp_version, git=None, taken_at)`——全部 `extra="forbid"`。
  - `records`: 常數 `TRAIN_YAML = "train.yaml"`、`EVENTS_LOG = "train.log.jsonl"`、`TRAIN_DIR = "train"`；`train_yaml(data_root, run_id) -> Path`、`events_path(data_root, run_id) -> Path`、`train_dir(data_root, run_id) -> Path`、`has_record(data_root, run_id) -> bool`、`load_record(data_root, run_id) -> TrainRecord`（缺 → `ValidationFailed` `fields={"run"}`；`run_id` 不符 → `ValidationFailed`）、`save_record(data_root, record) -> Path`、`append_event(data_root, run_id, event, attempt, **payload) -> None`（未知 event → `ValueError`）、`read_events(data_root, run_id) -> list[dict]`。

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/train/__init__.py`：空檔。

`tests/unit/train/test_schema_records.py`：

```python
import json

import pytest
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.train.records import (
    EVENTS_LOG,
    TRAIN_YAML,
    append_event,
    events_path,
    has_record,
    load_record,
    read_events,
    save_record,
    train_dir,
    train_yaml,
)
from vcp.train.schema import (
    EVENTS,
    Attempt,
    CheckpointRecord,
    EnvSnapshot,
    ExportRef,
    TrainRecord,
    UploadRecord,
)

STAMP = "2026-09-05T00:00:00.000Z"


def _record(**kw) -> TrainRecord:
    base = dict(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="projects/tiny",
        command=["python", "train.py"],
    )
    return TrainRecord(**{**base, **kw})


def test_models_reject_unknown_fields_and_bad_literals():
    with pytest.raises(ValidationError):
        Attempt(n=1, started_at=STAMP, console="train/console.1.log", status="done")
    with pytest.raises(ValidationError):
        CheckpointRecord(path="a", sha256="x", bytes=1, registered_at=STAMP, attempt=1, source="disk")
    with pytest.raises(ValidationError):
        UploadRecord(dest="d", kind="ftp", name="a", sha256="x", verified=True, uploaded_at=STAMP)
    with pytest.raises(ValidationError):
        TrainRecord(**{**_record().model_dump(), "extra": 1})
    assert EVENTS == ("started", "env", "checkpoint", "uploaded", "finished", "note")


def test_record_defaults():
    r = _record()
    assert r.exports == [] and r.config is None and r.seed is None and r.framework == ""
    assert r.attempts == [] and r.checkpoints == [] and r.uploads == [] and r.notes == ""
    a = Attempt(n=1, started_at=STAMP, console="train/console.1.log")
    assert a.status == "running" and a.exit_code is None and a.env is None
    e = ExportRef(dir="exports/x", subset="train", format="yolo", manifest_sha256="cd" * 32, sample_count=3)
    assert e.sample_count == 3


def test_paths(roots):
    assert train_yaml(roots.data, "r1") == roots.data / "runs" / "r1" / TRAIN_YAML
    assert events_path(roots.data, "r1") == roots.data / "runs" / "r1" / EVENTS_LOG
    assert train_dir(roots.data, "r1") == roots.data / "runs" / "r1" / "train"
    with pytest.raises(ValidationFailed, match="invalid name"):
        train_yaml(roots.data, "../r1")


def test_save_load_roundtrip_and_identity(roots):
    assert not has_record(roots.data, "r1")
    path = save_record(roots.data, _record(seed=7, framework="fake 1.0"))
    assert path == train_yaml(roots.data, "r1") and has_record(roots.data, "r1")
    assert b"\r" not in path.read_bytes()
    loaded = load_record(roots.data, "r1")
    assert loaded.seed == 7 and loaded.framework == "fake 1.0" and loaded.command == ["python", "train.py"]
    with pytest.raises(ValidationFailed, match="not found") as ei:
        load_record(roots.data, "nope")
    assert ei.value.fields == {"run": "nope"}
    save_record(roots.data, _record(run_id="other"))
    (roots.data / "runs" / "r1" / TRAIN_YAML).write_text(
        (roots.data / "runs" / "other" / TRAIN_YAML).read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(ValidationFailed, match="names run 'other'") as ei:
        load_record(roots.data, "r1")
    assert ei.value.fields == {"run": "r1"}


def test_events_append_only(roots):
    append_event(roots.data, "r1", "started", 1, command=["python"], seed=None)
    append_event(roots.data, "r1", "checkpoint", 1, path="w/best.pt", sha256="ab" * 32, bytes=10)
    rows = read_events(roots.data, "r1")
    assert [r["event"] for r in rows] == ["started", "checkpoint"]
    assert rows[0]["attempt"] == 1 and rows[0]["command"] == ["python"] and rows[0]["seed"] is None
    assert rows[0]["ts"].endswith("Z") and rows[1]["bytes"] == 10
    raw = events_path(roots.data, "r1").read_bytes()
    assert raw.count(b"\n") == 2 and b"\r" not in raw
    assert json.loads(raw.splitlines()[1])["path"] == "w/best.pt"
    with pytest.raises(ValueError, match="unknown event"):
        append_event(roots.data, "r1", "bogus", 1)
    assert read_events(roots.data, "none") == []


def test_env_snapshot_model():
    snap = EnvSnapshot(
        python="3.12.0",
        executable="C:/venv/Scripts/python.exe",
        platform="Windows-11",
        hostname="box",
        packages={"numpy": "2.1.0"},
        vcp_version="0.1.0",
        taken_at=STAMP,
    )
    assert snap.gpus == [] and snap.torch is None and snap.git is None
    assert EnvSnapshot.model_validate(snap.model_dump(mode="json")) == snap
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/train/test_schema_records.py -o addopts="" -q`
Expected: `ModuleNotFoundError: No module named 'vcp.train'`。

- [ ] **Step 3: 寫 schema 與 records**

`src/vcp/train/__init__.py`：

```python
"""Training layer: wrap any training command, record its identity, keep checkpoints backed up."""
```

`src/vcp/train/schema.py`：

```python
"""Pydantic models of the training layer (spec 4): the run's training record and its snapshots."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AttemptStatus = Literal["running", "finished", "failed", "interrupted"]
CheckpointSource = Literal["glob", "session"]
UploadKind = Literal["rclone", "local"]
EVENTS = ("started", "env", "checkpoint", "uploaded", "finished", "note")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExportRef(_Strict):
    """One ``vcp data export`` directory the run trained on: the evidence behind trained_on."""

    dir: str
    subset: str
    format: str
    manifest_sha256: str
    sample_count: int


class ConfigRef(_Strict):
    path: str
    sha256: str
    copy: str


class Attempt(_Strict):
    """One execution of the training command. ``exit_code`` is None while running."""

    n: int
    started_at: str
    finished_at: str | None = None
    duration_s: float | None = None
    exit_code: int | None = None
    status: AttemptStatus = "running"
    console: str
    env: str | None = None


class CheckpointRecord(_Strict):
    """Identity of one checkpoint file: (path, sha256). The file itself is never moved."""

    path: str
    sha256: str
    bytes: int
    registered_at: str
    attempt: int
    final: bool = False
    source: CheckpointSource = "glob"


class UploadRecord(_Strict):
    dest: str
    kind: UploadKind
    name: str
    sha256: str
    verified: bool
    uploaded_at: str


class TrainRecord(_Strict):
    """``runs/<run_id>/train.yaml`` (spec 4.2): rewritten whole after every event."""

    run_id: str
    dataset: str
    plan_id: str
    trained_on: list[str]
    exports: list[ExportRef] = Field(default_factory=list)
    config: ConfigRef | None = None
    config_hash: str
    seed: int | None = None
    framework: str = ""
    venv: str | None = None
    cwd: str
    command: list[str]
    attempts: list[Attempt] = Field(default_factory=list)
    checkpoints: list[CheckpointRecord] = Field(default_factory=list)
    uploads: list[UploadRecord] = Field(default_factory=list)
    notes: str = ""


class GitInfo(_Strict):
    commit: str
    dirty: bool


class EnvSnapshot(_Strict):
    """``runs/<run_id>/train/env.<n>.json`` (spec 4.4): what the framework venv reported."""

    python: str
    executable: str
    platform: str
    hostname: str
    packages: dict[str, str]
    torch: str | None = None
    cuda: str | None = None
    cudnn: str | None = None
    gpus: list[str] = Field(default_factory=list)
    nvidia_driver: str | None = None
    vcp_version: str
    git: GitInfo | None = None
    taken_at: str
```

`src/vcp/train/records.py`：

```python
"""``train.yaml`` (a snapshot, rewritten whole) and ``train.log.jsonl`` (append-only events).

Both live in the run directory next to ``run.yaml``. The yaml answers "what is the state now";
the events answer "how did it get there" -- every checkpoint, upload and attempt is a row that
is never rewritten, so a machine reclaimed mid-training still leaves a readable history.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.core.time import stamp
from vcp.measure.runs import run_dir
from vcp.train.schema import EVENTS, TrainRecord

TRAIN_YAML = "train.yaml"
EVENTS_LOG = "train.log.jsonl"
TRAIN_DIR = "train"


def train_yaml(data_root: Path, run_id: str) -> Path:
    return run_dir(data_root, run_id) / TRAIN_YAML


def events_path(data_root: Path, run_id: str) -> Path:
    return run_dir(data_root, run_id) / EVENTS_LOG


def train_dir(data_root: Path, run_id: str) -> Path:
    return run_dir(data_root, run_id) / TRAIN_DIR


def has_record(data_root: Path, run_id: str) -> bool:
    return train_yaml(data_root, run_id).is_file()


def load_record(data_root: Path, run_id: str) -> TrainRecord:
    path = train_yaml(data_root, run_id)
    if not path.is_file():
        raise ValidationFailed(f"training record not found: {path}", fields={"run": run_id})
    record = load_yaml_model(path, TrainRecord)
    if record.run_id != run_id:
        raise ValidationFailed(
            f"train.yaml names run {record.run_id!r}, not {run_id!r}",
            location=str(path),
            fields={"run": run_id},
        )
    return record


def save_record(data_root: Path, record: TrainRecord) -> Path:
    path = train_yaml(data_root, record.run_id)
    dump_yaml_model(record, path)
    return path


def append_event(data_root: Path, run_id: str, event: str, attempt: int, **payload: Any) -> None:
    """One row of ``train.log.jsonl``; ``ts`` is this module's clock, never the caller's."""
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}; known: {EVENTS}")
    path = events_path(data_root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": stamp(), "event": event, "attempt": attempt, **payload}
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_events(data_root: Path, run_id: str) -> list[dict[str, Any]]:
    path = events_path(data_root, run_id)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/train -o addopts="" -q`
Expected: 全部 PASS；`uv run ruff format src/vcp/train tests/unit/train && uv run ruff check src/vcp/train tests/unit/train && uv run ruff format --check src/vcp/train tests/unit/train` 乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/train/__init__.py src/vcp/train/schema.py src/vcp/train/records.py tests/unit/train/__init__.py tests/unit/train/test_schema_records.py
git commit -m "feat(train): 訓練紀錄的資料模型、train.yaml 快照與 train.log.jsonl 事件

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: 環境快照（spec §4.4、§7）

**Files:**
- Create: `src/vcp/train/env.py`
- Test: `tests/unit/train/test_env.py`

**Interfaces:**
- Consumes: Task 1 的 `EnvSnapshot` / `GitInfo`；`vcp.__version__`、`vcp.core.errors.VcpError`、`vcp.core.time.stamp`。
- Produces: `PROBE: str`（探針程式）、`venv_python(venv: Path) -> Path`（`Scripts/python.exe` 或 `bin/python`，皆無 → `VcpError`）、`gpus() -> tuple[list[str], str | None]`、`git_info(cwd: Path) -> GitInfo | None`、`snapshot(python: Path | None, cwd: Path) -> EnvSnapshot`（探針非零或非 JSON → `VcpError`）。

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/train/test_env.py`：

```python
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vcp import __version__
from vcp.core.errors import VcpError
from vcp.train import env as envmod
from vcp.train.env import PROBE, git_info, gpus, snapshot, venv_python

REPO = Path(__file__).resolve().parents[3]


def test_probe_prints_json_for_this_interpreter():
    out = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, check=True)
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data["python"].startswith(sys.version.split()[0])
    assert "pydantic" in data["packages"] and data["executable"]
    assert set(data) == {"python", "executable", "platform", "hostname", "packages", "torch", "cuda", "cudnn"}


def test_snapshot_uses_current_interpreter_and_repo_git(monkeypatch):
    real_which = shutil.which  # captured before patching: the module object is shared
    monkeypatch.setattr(envmod.shutil, "which", lambda name, *a, **k: None if name == "nvidia-smi" else real_which(name))
    snap = snapshot(None, REPO)
    assert snap.vcp_version == __version__ and snap.taken_at.endswith("Z")
    assert snap.gpus == [] and snap.nvidia_driver is None
    assert snap.git is not None and len(snap.git.commit) == 40 and isinstance(snap.git.dirty, bool)
    assert "pydantic" in snap.packages


def test_git_info_outside_a_repo(tmp_path):
    assert git_info(tmp_path) is None


def test_gpus_parses_nvidia_smi_csv(monkeypatch):
    monkeypatch.setattr(envmod.shutil, "which", lambda name, *a, **k: "C:/fake/nvidia-smi.exe")

    def fake_run(args, **kw):
        assert args[0] == "C:/fake/nvidia-smi.exe" and "--query-gpu=name,driver_version" in args
        return subprocess.CompletedProcess(args, 0, stdout="NVIDIA GeForce RTX 5070 Ti, 616.56\nNVIDIA T4, 616.56\n", stderr="")

    monkeypatch.setattr(envmod.subprocess, "run", fake_run)
    assert gpus() == (["NVIDIA GeForce RTX 5070 Ti", "NVIDIA T4"], "616.56")


def test_gpus_when_tool_missing_or_failing(monkeypatch):
    monkeypatch.setattr(envmod.shutil, "which", lambda name, *a, **k: None)
    assert gpus() == ([], None)
    monkeypatch.setattr(envmod.shutil, "which", lambda name, *a, **k: "smi")
    monkeypatch.setattr(envmod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="boom"))
    assert gpus() == ([], None)


def test_venv_python_resolution(tmp_path):
    venv = tmp_path / "venv"
    with pytest.raises(VcpError, match="no python"):
        venv_python(venv)
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"")
    assert venv_python(venv) == venv / "Scripts" / "python.exe"
    posix = tmp_path / "penv"
    (posix / "bin").mkdir(parents=True)
    (posix / "bin" / "python").write_bytes(b"")
    assert venv_python(posix) == posix / "bin" / "python"


def test_probe_failures_are_aborts(monkeypatch, tmp_path):
    monkeypatch.setattr(envmod, "PROBE", "import sys; sys.stderr.write('bad venv'); sys.exit(3)")
    with pytest.raises(VcpError, match="exit 3.*bad venv"):
        snapshot(None, tmp_path)
    monkeypatch.setattr(envmod, "PROBE", "print('not json')")
    with pytest.raises(VcpError, match="no JSON"):
        snapshot(None, tmp_path)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/train/test_env.py -o addopts="" -q`
Expected: `ModuleNotFoundError: No module named 'vcp.train.env'`。

- [ ] **Step 3: 寫 `env.py`**

```python
"""Environment snapshot (spec 7): the framework venv's own python reports on itself.

vcp's core venv never imports a training framework (iron rule 3), so the packages, torch and
CUDA versions come from a probe executed with the venv's interpreter; vcp adds what it can see
from outside -- GPUs via nvidia-smi, the git commit of the working directory, its own version.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from vcp import __version__
from vcp.core.errors import VcpError
from vcp.core.time import stamp
from vcp.train.schema import EnvSnapshot, GitInfo

PROBE = """
import importlib.metadata as m, json, platform, socket, sys
out = {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
       "hostname": socket.gethostname(), "packages": {}, "torch": None, "cuda": None, "cudnn": None}
for d in m.distributions():
    try:
        out["packages"][d.name] = d.version
    except Exception:
        pass
try:
    import torch
    out["torch"] = torch.__version__
    out["cuda"] = torch.version.cuda
    out["cudnn"] = str(torch.backends.cudnn.version()) if torch.backends.cudnn.is_available() else None
except Exception:
    pass
print(json.dumps(out))
"""


def venv_python(venv: Path) -> Path:
    """The interpreter of a virtualenv directory, Windows or POSIX layout."""
    for candidate in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        if candidate.is_file():
            return candidate
    raise VcpError(f"no python found in venv {venv} (looked for Scripts/python.exe and bin/python)")


def _probe(python: Path) -> dict:
    proc = subprocess.run(
        [str(python), "-c", PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise VcpError(
            f"environment probe failed (exit {proc.returncode}): {proc.stderr.strip()[-500:]}"
        )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        data = json.loads(lines[-1])
    except (ValueError, IndexError) as e:
        raise VcpError(f"environment probe printed no JSON: {proc.stdout[-200:]!r}") from e
    if not isinstance(data, dict):
        raise VcpError(f"environment probe printed no JSON object: {proc.stdout[-200:]!r}")
    return data


def gpus() -> tuple[list[str], str | None]:
    """GPU names and the driver version from nvidia-smi; empty when the tool is absent or fails."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return [], None
    proc = subprocess.run(
        [exe, "--query-gpu=name,driver_version", "--format=csv,noheader"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return [], None
    names: list[str] = []
    driver: str | None = None
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0]:
            names.append(parts[0])
            driver = parts[1]
    return names, driver


def git_info(cwd: Path) -> GitInfo | None:
    """Commit and dirty flag of the repository containing ``cwd``; None outside any repo."""
    exe = shutil.which("git")
    if not exe:
        return None
    head = subprocess.run([exe, "-C", str(cwd), "rev-parse", "HEAD"], capture_output=True, text=True)
    if head.returncode != 0:
        return None
    status = subprocess.run(
        [exe, "-C", str(cwd), "status", "--porcelain"], capture_output=True, text=True
    )
    return GitInfo(commit=head.stdout.strip(), dirty=bool(status.stdout.strip()))


def snapshot(python: Path | None, cwd: Path) -> EnvSnapshot:
    """spec 4.4: the venv's report plus what vcp sees from outside."""
    data = _probe(python or Path(sys.executable))
    names, driver = gpus()
    return EnvSnapshot(
        **data,
        gpus=names,
        nvidia_driver=driver,
        vcp_version=__version__,
        git=git_info(cwd),
        taken_at=stamp(),
    )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/train -o addopts="" -q`
Expected: 全部 PASS；ruff format / check 乾淨。（`test_snapshot_uses_current_interpreter_and_repo_git` 真的跑一次探針，約 1 秒。）

- [ ] **Step 5: Commit**

```bash
git add src/vcp/train/env.py tests/unit/train/test_env.py
git commit -m "feat(train): 環境快照——venv 的 python 自報套件與 torch/CUDA，vcp 補 GPU、git、版本

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: checkpoint 登記、final 解析、漂移檢查（spec §6.2、§6.1 第 8 步、§6 status 列）

**Files:**
- Create: `src/vcp/train/checkpoints.py`
- Test: `tests/unit/train/test_checkpoints.py`

**Interfaces:**
- Consumes: Task 1 的 `CheckpointRecord` / `TrainRecord`；`vcp.core.hashing.sha256_file`、`vcp.core.paths.store_path / resolve_stored_path`、`vcp.core.time.stamp`、`vcp.core.errors.ValidationFailed`。
- Produces: `FINAL_AMBIGUOUS = "final_ambiguous"`；`expand(patterns: list[str], cwd: Path) -> list[Path]`（相對 `cwd` 的 glob，遞迴，只取檔案，排序去重）；`register(record, files, *, data_root, attempt, source="glob") -> tuple[TrainRecord, list[CheckpointRecord]]`（身分 `(path, sha256)` 去重）；`resolve_final(record, pattern, *, cwd, data_root) -> tuple[TrainRecord, CheckpointRecord | None]`（`pattern` 須恰好命中一檔，否則 `ValidationFailed` `fields={"checkpoint": pattern}`；`None` 時取 session 標的 final）；`mark_final(record, path, sha256) -> TrainRecord`（只有這一筆 `final=True`）；`missing(record, data_root) -> list[str]`（檔案不存在的 `path`）；`drift(record, data_root) -> list[str]`（不存在或 sha 不符的 `path`）。

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/train/test_checkpoints.py`：

```python
import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import store_path
from vcp.train.checkpoints import (
    FINAL_AMBIGUOUS,
    drift,
    expand,
    mark_final,
    missing,
    register,
    resolve_final,
)
from vcp.train.schema import TrainRecord


def _record() -> TrainRecord:
    return TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="w",
        command=["python", "train.py"],
    )


def _weights(root):
    (root / "weights").mkdir(parents=True)
    (root / "weights" / "best.pt").write_bytes(b"best")
    (root / "weights" / "last.pt").write_bytes(b"last")
    (root / "weights" / "notes.txt").write_text("x", encoding="utf-8")
    (root / "weights" / "sub").mkdir()
    (root / "weights" / "sub" / "epoch1.pt").write_bytes(b"e1")
    return root / "weights"


def test_expand_globs_relative_to_cwd(tmp_path):
    w = _weights(tmp_path)
    assert expand(["weights/*.pt"], tmp_path) == [w / "best.pt", w / "last.pt"]
    assert expand(["weights/**/*.pt"], tmp_path) == [w / "best.pt", w / "last.pt", w / "sub" / "epoch1.pt"]
    assert expand(["weights/*.pt", "weights/best.pt", str(w / "last.pt")], tmp_path) == [w / "best.pt", w / "last.pt"]
    assert expand(["weights"], tmp_path) == []  # directories are not checkpoints
    assert expand(["nothing/*.pt"], tmp_path) == []


def test_register_dedups_by_path_and_sha(roots, tmp_path):
    w = _weights(roots.data / "work")
    rec, added = register(_record(), [w / "best.pt", w / "last.pt"], data_root=roots.data, attempt=1)
    assert [c.path for c in added] == ["work/weights/best.pt", "work/weights/last.pt"]
    assert added[0].sha256 == sha256_file(w / "best.pt") and added[0].bytes == 4
    assert added[0].attempt == 1 and added[0].source == "glob" and not added[0].final
    again, added2 = register(rec, [w / "best.pt"], data_root=roots.data, attempt=2)
    assert added2 == [] and again == rec
    (w / "best.pt").write_bytes(b"best-v2")
    third, added3 = register(again, [w / "best.pt"], data_root=roots.data, attempt=2, source="session")
    assert len(added3) == 1 and added3[0].attempt == 2 and added3[0].source == "session"
    assert [c.path for c in third.checkpoints] == ["work/weights/best.pt", "work/weights/last.pt", "work/weights/best.pt"]
    outside = tmp_path / "elsewhere.pt"
    outside.write_bytes(b"o")
    fourth, added4 = register(third, [outside], data_root=roots.data, attempt=2)
    assert added4[0].path == store_path(outside, roots.data) and added4[0].path.endswith("elsewhere.pt")


def test_resolve_final(roots):
    w = _weights(roots.data / "work")
    rec, _ = register(_record(), [w / "best.pt", w / "last.pt"], data_root=roots.data, attempt=1)
    rec, final = resolve_final(rec, "weights/best.pt", cwd=roots.data / "work", data_root=roots.data)
    assert final is not None and final.path == "work/weights/best.pt"
    assert [c.final for c in rec.checkpoints] == [True, False]
    with pytest.raises(ValidationFailed, match=FINAL_AMBIGUOUS) as ei:
        resolve_final(rec, "weights/*.pt", cwd=roots.data / "work", data_root=roots.data)
    assert ei.value.fields == {"checkpoint": "weights/*.pt"}
    with pytest.raises(ValidationFailed, match="matched 0"):
        resolve_final(rec, "weights/none.pt", cwd=roots.data / "work", data_root=roots.data)
    # no pattern: a session-marked final wins, else None
    rec2, _ = register(_record(), [w / "last.pt"], data_root=roots.data, attempt=1)
    assert resolve_final(rec2, None, cwd=roots.data / "work", data_root=roots.data)[1] is None
    rec2 = mark_final(rec2, "work/weights/last.pt", sha256_file(w / "last.pt"))
    assert resolve_final(rec2, None, cwd=roots.data / "work", data_root=roots.data)[1].path == "work/weights/last.pt"


def test_mark_final_is_exclusive(roots):
    w = _weights(roots.data / "work")
    rec, _ = register(_record(), [w / "best.pt", w / "last.pt"], data_root=roots.data, attempt=1)
    rec = mark_final(rec, "work/weights/last.pt", sha256_file(w / "last.pt"))
    assert [c.final for c in rec.checkpoints] == [False, True]
    rec = mark_final(rec, "work/weights/best.pt", sha256_file(w / "best.pt"))
    assert [c.final for c in rec.checkpoints] == [True, False]


def test_missing_and_drift(roots):
    w = _weights(roots.data / "work")
    rec, _ = register(_record(), [w / "best.pt", w / "last.pt"], data_root=roots.data, attempt=1)
    assert missing(rec, roots.data) == [] and drift(rec, roots.data) == []
    (w / "best.pt").write_bytes(b"tampered")
    (w / "last.pt").unlink()
    assert missing(rec, roots.data) == ["work/weights/last.pt"]
    assert drift(rec, roots.data) == ["work/weights/best.pt", "work/weights/last.pt"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/train/test_checkpoints.py -o addopts="" -q`
Expected: `ModuleNotFoundError: No module named 'vcp.train.checkpoints'`。

- [ ] **Step 3: 寫 `checkpoints.py`**

```python
"""Checkpoint registration (spec 6.2): identity is (path, sha256); the file is never moved.

A framework writes checkpoints wherever its config says; vcp only records where they are and
what bytes they hold, so a later upload, a status check or a reproduction can tell whether the
file on disk is still the one that was trained.
"""

from __future__ import annotations

import glob as globlib
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import resolve_stored_path, store_path
from vcp.core.time import stamp
from vcp.train.schema import CheckpointRecord, CheckpointSource, TrainRecord

FINAL_AMBIGUOUS = "final_ambiguous"


def expand(patterns: list[str], cwd: Path) -> list[Path]:
    """Files matched by any pattern (relative to ``cwd``, ``**`` allowed), sorted, deduplicated."""
    out: set[Path] = set()
    for pattern in patterns:
        for hit in globlib.glob(str(cwd / pattern), recursive=True):
            path = Path(hit)
            if path.is_file():
                out.add(path.resolve())
    return sorted(out)


def register(
    record: TrainRecord,
    files: list[Path],
    *,
    data_root: Path,
    attempt: int,
    source: CheckpointSource = "glob",
) -> tuple[TrainRecord, list[CheckpointRecord]]:
    """Add every file not already known by (path, sha256); return the record and what was new."""
    known = {(c.path, c.sha256) for c in record.checkpoints}
    added: list[CheckpointRecord] = []
    for f in files:
        stored = store_path(f, data_root)
        digest = sha256_file(f)
        if (stored, digest) in known:
            continue
        added.append(
            CheckpointRecord(
                path=stored,
                sha256=digest,
                bytes=f.stat().st_size,
                registered_at=stamp(),
                attempt=attempt,
                source=source,
            )
        )
        known.add((stored, digest))
    if not added:
        return record, []
    return record.model_copy(update={"checkpoints": [*record.checkpoints, *added]}), added


def mark_final(record: TrainRecord, path: str, sha256: str) -> TrainRecord:
    """Exactly one checkpoint carries ``final=True``: the one with this stored path and sha."""
    marks = [
        c.model_copy(update={"final": c.path == path and c.sha256 == sha256})
        for c in record.checkpoints
    ]
    return record.model_copy(update={"checkpoints": marks})


def resolve_final(
    record: TrainRecord, pattern: str | None, *, cwd: Path, data_root: Path
) -> tuple[TrainRecord, CheckpointRecord | None]:
    """spec 6.1 step 8: ``--final`` must match exactly one file; without it, a session-marked
    final (if any) is the answer. The matched file is already registered because the run
    treats ``--final`` as one more ``--checkpoints`` glob."""
    if pattern is None:
        finals = [c for c in record.checkpoints if c.final]
        return record, (finals[-1] if finals else None)
    hits = expand([pattern], cwd)
    if len(hits) != 1:
        raise ValidationFailed(
            f"{FINAL_AMBIGUOUS}: --final {pattern!r} matched {len(hits)} files, need exactly 1",
            fields={"checkpoint": pattern},
        )
    stored, digest = store_path(hits[0], data_root), sha256_file(hits[0])
    record = mark_final(record, stored, digest)
    final = next((c for c in record.checkpoints if c.final), None)
    if final is None:
        raise ValidationFailed(
            f"{FINAL_AMBIGUOUS}: --final {pattern!r} matched {stored}, which is not registered",
            fields={"checkpoint": pattern},
        )
    return record, final


def missing(record: TrainRecord, data_root: Path) -> list[str]:
    """Registered checkpoints whose file is gone (cheap: no hashing)."""
    return [
        c.path for c in record.checkpoints if not resolve_stored_path(c.path, data_root).is_file()
    ]


def drift(record: TrainRecord, data_root: Path) -> list[str]:
    """Registered checkpoints whose file is gone or hashes differently now (status --verify)."""
    bad: list[str] = []
    for c in record.checkpoints:
        path = resolve_stored_path(c.path, data_root)
        if not path.is_file() or sha256_file(path) != c.sha256:
            bad.append(c.path)
    return bad
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/train -o addopts="" -q`
Expected: 全部 PASS；ruff format / check 乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/train/checkpoints.py tests/unit/train/test_checkpoints.py
git commit -m "feat(train): checkpoint 登記——(path, sha256) 身分、--final 恰一命中、漂移檢查

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: 上傳與驗證——本機目錄與 rclone（spec §6.3）

**Files:**
- Create: `src/vcp/train/upload.py`
- Test: `tests/unit/train/test_upload.py`

**Interfaces:**
- Consumes: Task 1 的 `CheckpointRecord` / `TrainRecord` / `UploadRecord`；`vcp.core.hashing.sha256_file`、`vcp.core.paths.resolve_stored_path`、`vcp.core.time.stamp`、`vcp.core.errors.ValidationFailed / VcpError`。
- Produces: `NAME_COLLISION = "name_collision"`；`Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]`；`default_runner`；`dest_kind(dest) -> Literal["rclone", "local"]`；`UploadOutcome(records: list[UploadRecord], uploaded: int, skipped: int)`；`upload(record, dest, *, data_root, only_final=False, runner=None) -> UploadOutcome`（本機：複製 + 讀回 sha；rclone：`copyto --checksum` + `hashsum sha256`；已在且 sha 相同 → 計 `skipped` 並回 `verified=True` 的紀錄；來源檔缺或 sha 已變 → `ValidationFailed` `fields={"checkpoint"}`；同名不同 sha → `ValidationFailed` 前綴 `name_collision`；rclone 缺 → `VcpError`）；`merge_uploads(record, new) -> TrainRecord`（以 `(dest, name, sha256)` 取代舊紀錄）。

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/train/test_upload.py`：

```python
import subprocess
from pathlib import Path

import pytest

from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.hashing import sha256_file
from vcp.train import upload as upmod
from vcp.train.checkpoints import mark_final, register
from vcp.train.schema import TrainRecord
from vcp.train.upload import NAME_COLLISION, dest_kind, merge_uploads, upload


def _record() -> TrainRecord:
    return TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="w",
        command=["python"],
    )


def _registered(roots):
    w = roots.data / "work" / "weights"
    w.mkdir(parents=True)
    (w / "best.pt").write_bytes(b"best")
    (w / "last.pt").write_bytes(b"last")
    rec, _ = register(_record(), [w / "best.pt", w / "last.pt"], data_root=roots.data, attempt=1)
    return mark_final(rec, "work/weights/best.pt", sha256_file(w / "best.pt")), w


@pytest.mark.parametrize(
    "dest, kind",
    [
        ("gdrive:vcp/weights", "rclone"),
        ("s3-team:bucket/w", "rclone"),
        ("remote:", "rclone"),
        ("C:/vault/weights", "local"),
        ("C:\\vault", "local"),
        ("/mnt/vault", "local"),
        ("vault", "local"),
    ],
)
def test_dest_kind(dest, kind):
    assert dest_kind(dest) == kind


def test_local_upload_copies_verifies_and_is_idempotent(roots, tmp_path):
    rec, w = _registered(roots)
    dest = tmp_path / "vault"
    out = upload(rec, str(dest), data_root=roots.data)
    assert out.uploaded == 2 and out.skipped == 0
    assert sorted(r.name for r in out.records) == ["best.pt", "last.pt"]
    assert all(r.verified and r.kind == "local" and r.dest == str(dest) for r in out.records)
    assert sha256_file(dest / "r1" / "best.pt") == sha256_file(w / "best.pt")
    again = upload(rec, str(dest), data_root=roots.data)
    assert again.uploaded == 0 and again.skipped == 2 and all(r.verified for r in again.records)
    only = upload(rec, str(tmp_path / "v2"), data_root=roots.data, only_final=True)
    assert [r.name for r in only.records] == ["best.pt"]


def test_local_upload_refuses_changed_or_missing_source(roots, tmp_path):
    rec, w = _registered(roots)
    (w / "best.pt").write_bytes(b"changed")
    with pytest.raises(ValidationFailed, match="changed since") as ei:
        upload(rec, str(tmp_path / "vault"), data_root=roots.data)
    assert ei.value.fields == {"checkpoint": "work/weights/best.pt"}
    (w / "best.pt").unlink()
    with pytest.raises(ValidationFailed, match="missing") as ei:
        upload(rec, str(tmp_path / "vault"), data_root=roots.data)
    assert ei.value.fields == {"checkpoint": "work/weights/best.pt"}
    assert not (tmp_path / "vault" / "r1" / "last.pt").exists()  # nothing copied before the check


def test_name_collision(roots, tmp_path):
    rec, w = _registered(roots)
    other = roots.data / "work" / "run2" / "best.pt"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"different best")
    rec, _ = register(rec, [other], data_root=roots.data, attempt=1)
    with pytest.raises(ValidationFailed, match=NAME_COLLISION) as ei:
        upload(rec, str(tmp_path / "vault"), data_root=roots.data)
    assert ei.value.fields == {"checkpoint": "best.pt"}


class FakeRclone:
    """A remote that remembers what was copied; ``hashsum`` reports what it holds."""

    def __init__(self, *, corrupt: str | None = None, fail_copy: bool = False):
        self.store: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.corrupt = corrupt
        self.fail_copy = fail_copy

    def __call__(self, args):
        self.calls.append(args)
        if args[1] == "copyto":
            if self.fail_copy:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="copy failed")
            src, target = args[2], args[3]
            self.store[target.rsplit("/", 1)[1]] = sha256_file(Path(src))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[1] == "hashsum":
            lines = [
                f"{'0' * 64 if name == self.corrupt else sha}  {name}" for name, sha in self.store.items()
            ]
            return subprocess.CompletedProcess(args, 0, stdout="\n".join(lines) + "\n", stderr="")
        raise AssertionError(args)


def test_rclone_upload_verifies_via_hashsum(roots):
    rec, w = _registered(roots)
    remote = FakeRclone()
    out = upload(rec, "gdrive:vcp/weights", data_root=roots.data, runner=remote)
    assert out.uploaded == 2 and out.skipped == 0 and all(r.verified for r in out.records)
    assert all(r.kind == "rclone" and r.dest == "gdrive:vcp/weights" for r in out.records)
    copy_calls = [c for c in remote.calls if c[1] == "copyto"]
    assert copy_calls[0][3] == "gdrive:vcp/weights/r1/best.pt" and "--checksum" in copy_calls[0]
    again = upload(rec, "gdrive:vcp/weights", data_root=roots.data, runner=remote)
    assert again.uploaded == 0 and again.skipped == 2 and all(r.verified for r in again.records)


def test_rclone_upload_reports_unverified_and_failures(roots):
    rec, w = _registered(roots)
    bad = FakeRclone(corrupt="last.pt")
    out = upload(rec, "gdrive:w", data_root=roots.data, runner=bad)
    assert {r.name: r.verified for r in out.records} == {"best.pt": True, "last.pt": False}
    with pytest.raises(VcpError, match="copyto failed"):
        upload(rec, "gdrive:w", data_root=roots.data, runner=FakeRclone(fail_copy=True))


def test_default_runner_requires_rclone(roots, monkeypatch):
    rec, _ = _registered(roots)
    monkeypatch.setattr(upmod.shutil, "which", lambda name, *a, **k: None)
    with pytest.raises(VcpError, match="rclone not found"):
        upload(rec, "gdrive:w", data_root=roots.data)


def test_merge_uploads_replaces_same_identity(roots, tmp_path):
    rec, w = _registered(roots)
    out = upload(rec, str(tmp_path / "vault"), data_root=roots.data)
    merged = merge_uploads(rec, out.records)
    assert len(merged.uploads) == 2
    merged2 = merge_uploads(merged, upload(rec, str(tmp_path / "vault"), data_root=roots.data).records)
    assert len(merged2.uploads) == 2 and merged2.uploads[0].uploaded_at >= merged.uploads[0].uploaded_at
    merged3 = merge_uploads(merged2, upload(rec, str(tmp_path / "v2"), data_root=roots.data).records)
    assert len(merged3.uploads) == 4
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/train/test_upload.py -o addopts="" -q`
Expected: `ModuleNotFoundError: No module named 'vcp.train.upload'`。

- [ ] **Step 3: 寫 `upload.py`**

```python
"""Upload registered checkpoints to an rclone remote or a local directory, and verify (spec 6.3).

A copy is not a backup until its bytes are known to match: rclone destinations are checked with
``rclone hashsum sha256``, local ones by reading the copy back. rclone is not a dependency; it is
shelled out through an injectable runner so the whole path is testable without a remote.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.hashing import sha256_file
from vcp.core.paths import resolve_stored_path
from vcp.core.time import stamp
from vcp.train.schema import CheckpointRecord, TrainRecord, UploadRecord

NAME_COLLISION = "name_collision"
Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

_REMOTE = re.compile(r"^[A-Za-z0-9_-]+:")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class UploadOutcome:
    records: list[UploadRecord]
    uploaded: int
    skipped: int


def dest_kind(dest: str) -> Literal["rclone", "local"]:
    """``remote:path`` is rclone unless it is a Windows drive like ``C:/``."""
    return "rclone" if _REMOTE.match(dest) and not _DRIVE.match(dest) else "local"


def default_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _targets(checkpoints: list[CheckpointRecord]) -> dict[str, CheckpointRecord]:
    """Checkpoints by destination file name; two different files with one name cannot coexist."""
    by_name: dict[str, CheckpointRecord] = {}
    for c in checkpoints:
        name = Path(c.path).name
        if name in by_name and by_name[name].sha256 != c.sha256:
            raise ValidationFailed(
                f"{NAME_COLLISION}: two checkpoints named {name!r} "
                f"({by_name[name].path} and {c.path}); rename one before uploading",
                fields={"checkpoint": name},
            )
        by_name[name] = c
    return by_name


def _source(c: CheckpointRecord, data_root: Path) -> Path:
    """The registered file, still holding the registered bytes."""
    src = resolve_stored_path(c.path, data_root)
    if not src.is_file():
        raise ValidationFailed(f"checkpoint missing: {src}", fields={"checkpoint": c.path})
    if sha256_file(src) != c.sha256:
        raise ValidationFailed(
            f"checkpoint {c.path} changed since it was registered (sha256 differs); "
            "register the new file with a new attempt instead of uploading it under the old sha",
            fields={"checkpoint": c.path},
        )
    return src


def _record(dest: str, kind: str, name: str, sha: str, verified: bool) -> UploadRecord:
    return UploadRecord(
        dest=dest, kind=kind, name=name, sha256=sha, verified=verified, uploaded_at=stamp()
    )


def _upload_local(
    record: TrainRecord, dest: str, targets: dict[str, CheckpointRecord], data_root: Path
) -> UploadOutcome:
    sources = {name: _source(c, data_root) for name, c in targets.items()}  # all checks first
    base = Path(dest) / record.run_id
    base.mkdir(parents=True, exist_ok=True)
    records: list[UploadRecord] = []
    uploaded = skipped = 0
    for name, c in targets.items():
        target = base / name
        if target.is_file() and sha256_file(target) == c.sha256:
            skipped += 1
            records.append(_record(dest, "local", name, c.sha256, True))
            continue
        shutil.copy2(sources[name], target)
        uploaded += 1
        records.append(_record(dest, "local", name, c.sha256, sha256_file(target) == c.sha256))
    return UploadOutcome(records, uploaded, skipped)


def _hashsum(runner: Runner, base: str) -> dict[str, str]:
    """``rclone hashsum sha256 <base>`` as {name: sha}; an unlistable base is simply empty."""
    proc = runner(["rclone", "hashsum", "sha256", base])
    if proc.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            out[parts[1].strip()] = parts[0]
    return out


def _upload_rclone(
    record: TrainRecord,
    dest: str,
    targets: dict[str, CheckpointRecord],
    data_root: Path,
    runner: Runner,
) -> UploadOutcome:
    sources = {name: _source(c, data_root) for name, c in targets.items()}
    base = f"{dest.rstrip('/')}/{record.run_id}"
    before = _hashsum(runner, base)
    uploaded = skipped = 0
    for name, c in targets.items():
        if before.get(name) == c.sha256:
            skipped += 1
            continue
        proc = runner(["rclone", "copyto", str(sources[name]), f"{base}/{name}", "--checksum"])
        if proc.returncode != 0:
            raise VcpError(
                f"rclone copyto failed (exit {proc.returncode}) for {name}: "
                f"{proc.stderr.strip()[-300:]}"
            )
        uploaded += 1
    after = _hashsum(runner, base)
    records = [
        _record(dest, "rclone", name, c.sha256, after.get(name) == c.sha256)
        for name, c in targets.items()
    ]
    return UploadOutcome(records, uploaded, skipped)


def upload(
    record: TrainRecord,
    dest: str,
    *,
    data_root: Path,
    only_final: bool = False,
    runner: Runner | None = None,
) -> UploadOutcome:
    """Copy the run's registered checkpoints to ``dest/<run_id>/`` and verify every one."""
    chosen = [c for c in record.checkpoints if c.final] if only_final else list(record.checkpoints)
    targets = _targets(chosen)
    if dest_kind(dest) == "local":
        return _upload_local(record, dest, targets, data_root)
    if runner is None:
        if shutil.which("rclone") is None:
            raise VcpError("rclone not found on PATH; install it or use a local --upload directory")
        runner = default_runner
    return _upload_rclone(record, dest, targets, data_root, runner)


def merge_uploads(record: TrainRecord, new: list[UploadRecord]) -> TrainRecord:
    """Newest record per (dest, name, sha256) wins; everything else is kept."""
    fresh = {(r.dest, r.name, r.sha256): r for r in new}
    kept = [r for r in record.uploads if (r.dest, r.name, r.sha256) not in fresh]
    return record.model_copy(update={"uploads": [*kept, *fresh.values()]})
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/train -o addopts="" -q`
Expected: 全部 PASS；ruff format / check 乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/train/upload.py tests/unit/train/test_upload.py
git commit -m "feat(train): checkpoint 上傳——本機目錄讀回驗 sha、rclone copyto + hashsum 驗證、冪等

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `vcp train run`——推導 trained_on、寫 run、快照、執行、登記、上傳（spec §4.1、§6 run 列、§6.1、§6.2、§6.3、§9）

**Files:**
- Modify: `src/vcp/data/split.py`（在 `load_plan` 之後加 `assert_plan_matches`）
- Create: `src/vcp/train/run.py`
- Create: `src/vcp/cli_train.py`
- Modify: `src/vcp/cli.py`（import `train_app`、`add_typer`）
- Test: `tests/unit/train/test_run.py`、`tests/unit/test_cli_train.py`、`tests/unit/data/test_split_plan_matches.py`

**Interfaces:**
- Consumes: Task 1（`records`、`schema`）、Task 2（`snapshot`、`venv_python`）、Task 3（`expand`、`register`、`resolve_final`）、Task 4（`upload`、`merge_uploads`、`Runner`）；量測層 `load_run` / `save_run` / `assert_run_matches` / `run_dir`、`RunCard` / `RunSource`；資料層 `Dataset.load`、`load_plan`、`SplitPlan.subset`；`vcp.core.hashing.sha256_file / sha256_json`、`vcp.core.paths.store_path / DatasetPaths`、`vcp.core.time.stamp / utc_now`；`vcp.cli_common.*`。
- Produces:
  - `vcp.data.split.assert_plan_matches(plan: SplitPlan, card: DatasetCard) -> None`（`dataset` 或 `dataset_hash` 不符 → `PlanMismatchError`）
  - `run.py`：常數 `RUN_EXISTS`、`RUN_BOUND_ELSEWHERE`、`TRAINED_ON_MISMATCH`；`RunSpec`（pydantic，`arbitrary_types_allowed`：`run_id, dataset, plan_id, exports: list[Path] = [], trained_on: list[str] = [], venv: Path | None, config: Path | None, seed: int | None, framework: str = "", cwd: Path | None, checkpoints: list[str] = [], final: str | None, uploads: list[str] = [], resume: bool = False, notes: str = "", command: list[str], on_line: Callable[[str], None] | None = None, rclone_runner: Runner | None = None, data_root, configs_root`）；`RunResult(record: TrainRecord, card: RunCard, attempt: Attempt, final: CheckpointRecord | None, registered: int, uploaded: int, verified: int, skipped: int, warnings: list[str])`；`read_export(dir) -> dict`；`derive_trained_on(exports, explicit, *, dataset, plan, data_root) -> tuple[list[str], list[ExportRef]]`；`config_hash(config, command) -> str`；`child_env(spec, *, data_root, configs_root, python) -> dict[str, str]`；`execute(command, *, cwd, env, console, on_line) -> tuple[int, AttemptStatus]`；`train_run(spec) -> RunResult`。
  - CLI `run`：`cmd=train.run`，欄位 `run= attempt= exit_code= duration_s= checkpoints= final= uploaded= verified= seed= venv=`；`train_app` 掛為 `vcp train`。

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/data/test_split_plan_matches.py`：

```python
import pytest

from helpers import det_samples, make_card
from vcp.core.errors import PlanMismatchError
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, assert_plan_matches, build_plan, parse_subsets


def test_assert_plan_matches():
    ds = Dataset.from_parts(make_card("det"), det_samples(20, seed=0))
    ds.card = ds.card.model_copy(update={"samples_hash": "a" * 64})
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    assert_plan_matches(plan, ds.card)
    with pytest.raises(PlanMismatchError, match="samples_hash"):
        assert_plan_matches(plan, ds.card.model_copy(update={"samples_hash": "b" * 64}))
    with pytest.raises(PlanMismatchError, match="belongs to dataset"):
        assert_plan_matches(plan, ds.card.model_copy(update={"name": "other"}))
```

`tests/unit/train/test_run.py`（假訓練腳本 `fake_train.py` 由夾具寫進 tmp；它印出環境變數、寫兩個 checkpoint、依第一個參數決定 exit code、第二個參數 `sleep` 時等待）：

```python
import json
import sys
from pathlib import Path

import pytest

from helpers import det_samples, det_with_runs, make_card, write_images
from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_json
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.exporters import ExportSpec, export_subset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, load_plan, parse_subsets, save_plan
from vcp.measure.runs import load_run, run_dir
from vcp.train.records import load_record, read_events
from vcp.train.run import (
    RUN_BOUND_ELSEWHERE,
    RUN_EXISTS,
    TRAINED_ON_MISMATCH,
    RunSpec,
    config_hash,
    derive_trained_on,
    execute,
    train_run,
)

FAKE = '''
import os, sys, time
from pathlib import Path
print("VCP_RUN_ID", os.environ.get("VCP_RUN_ID"))
print("VCP_SEED", os.environ.get("VCP_SEED"), "HASHSEED", os.environ.get("PYTHONHASHSEED"))
code = int(sys.argv[1]) if len(sys.argv) > 1 else 0
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best-" + str(code).encode())
Path("weights/last.pt").write_bytes(b"last")
if len(sys.argv) > 2 and sys.argv[2] == "sleep":
    for i in range(50):
        print("tick", i, flush=True)
        time.sleep(0.1)
sys.exit(code)
'''


def _seed(roots, name="tiny", n=40):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def _export(roots, subset, out):
    return export_subset(
        ExportSpec(name="tiny", plan_id="fixed-v1", subset=subset, format="yolo", out=out, data_root=roots.data, configs_root=roots.configs)
    ).out


@pytest.fixture
def work(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "fake_train.py").write_text(FAKE, encoding="utf-8")
    return tmp_path / "work"


def _spec(roots, work, **kw):
    base = dict(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        cwd=work,
        command=[sys.executable, "fake_train.py", "0"],
        checkpoints=["weights/*.pt"],
        final="weights/best.pt",
        data_root=roots.data,
        configs_root=roots.configs,
    )
    return RunSpec(**{**base, **kw})


def test_derive_trained_on_from_exports(roots, tmp_path):
    ds, plan, paths = _seed(roots)
    train = _export(roots, "train", tmp_path / "e-train")
    val = _export(roots, "valA", tmp_path / "e-valA")
    names, refs = derive_trained_on([train, val], [], dataset=ds, plan=plan, data_root=roots.data)
    assert names == ["train", "valA"] and [r.subset for r in refs] == ["train", "valA"]
    assert refs[0].format == "yolo" and refs[0].manifest_sha256 == sha256_file(train / "manifest.json")
    assert refs[0].sample_count == len(plan.ids_in("train"))
    assert derive_trained_on([train], ["train"], dataset=ds, plan=plan, data_root=roots.data)[0] == ["train"]
    with pytest.raises(ValidationFailed, match=TRAINED_ON_MISMATCH):
        derive_trained_on([train], ["valA"], dataset=ds, plan=plan, data_root=roots.data)
    with pytest.raises(ValidationFailed, match="trained_on is required"):
        derive_trained_on([], [], dataset=ds, plan=plan, data_root=roots.data)
    with pytest.raises(PlanMismatchError, match="no subset"):
        derive_trained_on([], ["nope"], dataset=ds, plan=plan, data_root=roots.data)
    manifest = train / "manifest.json"
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    manifest.write_text(json.dumps({**doc, "plan_id": "other"}), encoding="utf-8")
    with pytest.raises(PlanMismatchError, match="plan_id") as ei:
        derive_trained_on([train], [], dataset=ds, plan=plan, data_root=roots.data)
    assert ei.value.fields == {"export": str(train)}
    with pytest.raises(ValidationFailed, match="manifest.json not found"):
        derive_trained_on([tmp_path / "nowhere"], [], dataset=ds, plan=plan, data_root=roots.data)


def test_config_hash_two_routes(tmp_path):
    cmd = ["yolo", "train", "imgsz=640"]
    assert config_hash(None, cmd) == sha256_json(cmd)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("lr: 0.01\n", encoding="utf-8")
    assert config_hash(cfg, cmd) == sha256_file(cfg)
    with pytest.raises(ValidationFailed, match="--config not found"):
        config_hash(tmp_path / "none.yaml", cmd)


def test_execute_tees_and_reports_exit_code(work, tmp_path):
    console = tmp_path / "console.log"
    seen = []
    code, status = execute([sys.executable, "fake_train.py", "0"], cwd=work, env=None, console=console, on_line=seen.append)
    assert (code, status) == (0, "finished")
    text = console.read_text(encoding="utf-8")
    assert "VCP_RUN_ID None" in text and "".join(seen) == text
    code, status = execute([sys.executable, "fake_train.py", "3"], cwd=work, env=None, console=console, on_line=None)
    assert (code, status) == (3, "failed")


def test_execute_interrupt_terminates_child(work, tmp_path):
    def boom(line):
        if line.startswith("tick 2"):
            raise KeyboardInterrupt

    code, status = execute([sys.executable, "fake_train.py", "0", "sleep"], cwd=work, env=None, console=tmp_path / "c.log", on_line=boom)
    assert status == "interrupted" and code != 0
    assert "[vcp] interrupted" in (tmp_path / "c.log").read_text(encoding="utf-8")


def test_train_run_happy_path_writes_run_and_record(roots, work, tmp_path):
    ds, plan, paths = _seed(roots)
    export = _export(roots, "train", tmp_path / "e-train")
    seen = []
    res = train_run(_spec(roots, work, exports=[export], trained_on=[], seed=7, framework="fake 1.0", uploads=[str(tmp_path / "vault")], on_line=seen.append))
    assert res.attempt.n == 1 and res.attempt.status == "finished" and res.attempt.exit_code == 0
    assert res.registered == 2 and res.final is not None and res.final.path.endswith("weights/best.pt")
    assert res.uploaded == 2 and res.verified == 2 and res.skipped == 0 and res.warnings == ["venv=inherited"]
    assert "VCP_RUN_ID r1" in "".join(seen) and "VCP_SEED 7 HASHSEED 7" in "".join(seen)
    card = load_run(roots.data, "r1")
    assert card.trained_on == ["train"] and card.source.framework == "fake 1.0"
    assert card.source.config_hash == sha256_json([sys.executable, "fake_train.py", "0"])
    assert card.source.export_manifest_sha == sha256_file(export / "manifest.json")
    assert card.source.weights_hash == sha256_file(work / "weights" / "best.pt") and card.predictions == {}
    rec = load_record(roots.data, "r1")
    assert rec.exports[0].subset == "train" and rec.seed == 7 and rec.config is None
    assert rec.attempts[0].console == "train/console.1.log" and rec.attempts[0].env == "train/env.1.json"
    assert rec.attempts[0].duration_s is not None and rec.attempts[0].duration_s >= 0
    assert [c.final for c in rec.checkpoints] == [True, False] and all(u.verified for u in rec.uploads)
    run = run_dir(roots.data, "r1")
    assert "VCP_RUN_ID r1" in (run / "train" / "console.1.log").read_text(encoding="utf-8")
    env = json.loads((run / "train" / "env.1.json").read_text(encoding="utf-8"))
    assert "pydantic" in env["packages"] and env["taken_at"].endswith("Z")
    assert [e["event"] for e in read_events(roots.data, "r1")] == ["started", "env", "finished", "checkpoint", "checkpoint", "uploaded", "uploaded"]
    assert sha256_file(tmp_path / "vault" / "r1" / "best.pt") == card.source.weights_hash


def test_train_run_config_file_is_copied_and_hashed(roots, work, tmp_path):
    _seed(roots)
    cfg = work / "cfg.yaml"
    cfg.write_text("epochs: 1\n", encoding="utf-8")
    res = train_run(_spec(roots, work, config=cfg))
    assert res.card.source.config_hash == sha256_file(cfg)
    assert res.record.config is not None and res.record.config.copy == "train/config.1.yaml"
    assert (run_dir(roots.data, "r1") / "train" / "config.1.yaml").read_text(encoding="utf-8") == "epochs: 1\n"


def test_train_run_failed_command_registers_but_does_not_upload(roots, work, tmp_path):
    _seed(roots)
    res = train_run(_spec(roots, work, command=[sys.executable, "fake_train.py", "2"], uploads=[str(tmp_path / "vault")]))
    assert res.attempt.status == "failed" and res.attempt.exit_code == 2
    assert res.registered == 2 and res.final is None and res.uploaded == 0
    assert "final=skipped (command failed)" in res.warnings
    assert not (tmp_path / "vault").exists()
    assert load_run(roots.data, "r1").source.weights_hash is None


def test_train_run_checks_before_writing(roots, work, tmp_path):
    ds, plan, paths = _seed(roots)
    with pytest.raises(ValidationFailed, match="training command is required"):
        train_run(_spec(roots, work, command=[]))
    with pytest.raises(ValidationFailed, match="command not found"):
        train_run(_spec(roots, work, command=["no-such-binary-xyz", "a"]))
    with pytest.raises(PlanMismatchError):
        train_run(_spec(roots, work, plan_id="fixed-v1", trained_on=["ghost"]))
    with pytest.raises(ValidationFailed, match="--final .* matched 0"):
        train_run(_spec(roots, work, final="weights/none.pt", run_id="r-final"))
    assert not run_dir(roots.data, "r1").exists()
    # r-final wrote its record before the final check (spec 6.1: final is resolved after the command)
    assert load_record(roots.data, "r-final").attempts[0].status == "finished"


def test_train_run_existing_runs(roots, work, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)  # run "perfect" is an ingested run
    (work / "weights").mkdir(exist_ok=True)
    with pytest.raises(ValidationFailed, match=RUN_BOUND_ELSEWHERE) as ei:
        train_run(_spec(roots, work, run_id="perfect"))
    assert ei.value.fields == {"run": "perfect"}
    first = train_run(_spec(roots, work))
    with pytest.raises(ValidationFailed, match=RUN_EXISTS):
        train_run(_spec(roots, work))
    with pytest.raises(ValidationFailed, match="different config"):
        train_run(_spec(roots, work, resume=True, command=[sys.executable, "fake_train.py", "0", "x"]))
    second = train_run(_spec(roots, work, resume=True))
    assert second.attempt.n == 2 and len(second.record.attempts) == 2
    assert second.registered == 0  # same files, same shas: nothing new to register
    assert second.record.attempts[0].status == "finished" and first.record.attempts[0] == second.record.attempts[0]
    assert (run_dir(roots.data, "r1") / "train" / "console.2.log").is_file()


def test_train_run_venv_python_must_exist(roots, work, tmp_path):
    _seed(roots)
    with pytest.raises(Exception, match="no python"):
        train_run(_spec(roots, work, venv=tmp_path / "venv"))
    assert not run_dir(roots.data, "r1").exists()
```

`tests/unit/test_cli_train.py`：

```python
import json
import sys

from typer.testing import CliRunner

from helpers import det_samples, make_card, write_images
from vcp.cli import app
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan

runner = CliRunner()

FAKE = '''
import sys
from pathlib import Path
print("hello from fake")
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best")
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
'''


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def seed_det(roots, name="tiny", n=40):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def _run(work, *extra, code="0"):
    return runner.invoke(
        app,
        [
            "train", "run", "--run", "r1", "--dataset", "tiny", "--plan", "fixed-v1", "--trained-on", "train",
            "--cwd", str(work), "--checkpoints", "weights/*.pt", "--final", "weights/best.pt", *extra,
            "--", sys.executable, "fake_train.py", code,
        ],
    )


def test_train_run_cli(roots, tmp_path):
    seed_det(roots)
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_text(FAKE, encoding="utf-8")
    r = _run(work, "--seed", "3", "--framework", "fake", "--upload", str(tmp_path / "vault"))
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert v.startswith("VERDICT cmd=train.run status=WARN") and "run=r1" in v and "attempt=1" in v
    assert "exit_code=0" in v and "checkpoints=1" in v and "uploaded=1" in v and "verified=1" in v
    assert "seed=3" in v and "venv=inherited" in v and "final=none" not in v
    assert "hello from fake" in r.output
    r = _run(work, "--resume", "--seed", "3", "--framework", "fake", "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert doc["fields"]["attempt"] == 2 and doc["result"]["run_id"] == "r1"
    assert "hello from fake" in r.output  # still echoed (to stderr under --json; CliRunner merges)
    r = _run(work, "--resume", "--seed", "3", "--framework", "fake", code="1")
    assert r.exit_code == 1 and "status=FAIL" in _verdict(r.output) and "exit_code=1" in _verdict(r.output)


def test_train_run_cli_failures(roots, tmp_path):
    seed_det(roots)
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_text(FAKE, encoding="utf-8")
    r = runner.invoke(app, ["train", "run", "--run", "r1", "--dataset", "tiny", "--plan", "fixed-v1", "--trained-on", "train", "--cwd", str(work)])
    assert r.exit_code == 1 and "training command is required" in _verdict(r.output)
    r = runner.invoke(app, ["train", "run", "--run", "r1", "--dataset", "tiny", "--plan", "fixed-v1", "--trained-on", "nope", "--cwd", str(work), "--", sys.executable, "fake_train.py"])
    assert r.exit_code == 2
    r = runner.invoke(app, ["train", "--help"])
    assert r.exit_code == 0 and "run" in r.output
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/test_split_plan_matches.py tests/unit/train/test_run.py tests/unit/test_cli_train.py -o addopts="" -q`
Expected: `ImportError: cannot import name 'assert_plan_matches'`、`ModuleNotFoundError: No module named 'vcp.train.run'`、CLI `No such command 'train'`。

- [ ] **Step 3: 寫 `assert_plan_matches`、`run.py`、`cli_train.py`，掛上群組**

`src/vcp/data/split.py`，在 `load_plan` 之後加（`DatasetCard` 已可從 `vcp.data.schema` import；檔頭 `from vcp.data.schema import Sample` 改為 `from vcp.data.schema import DatasetCard, Sample`）：

```python
def assert_plan_matches(plan: SplitPlan, card: DatasetCard) -> None:
    """The plan must belong to this dataset version: same name, same samples_hash."""
    if plan.dataset != card.name:
        raise PlanMismatchError(
            f"plan {plan.plan_id!r} belongs to dataset {plan.dataset!r}, not {card.name!r}"
        )
    if plan.dataset_hash != card.samples_hash:
        raise PlanMismatchError(
            f"plan {plan.plan_id!r} was built on samples_hash {plan.dataset_hash[:12]}, "
            f"dataset now has {card.samples_hash[:12]}"
        )
```

`src/vcp/train/run.py`：

```python
"""``vcp train run``: wrap one training command and make the result a run (spec 6.1).

Every check runs before the first file is written: the dataset and plan agree, the export
manifests say which subsets were trained on, the config has a hash, the venv has a python, the
command can be found, and the run id is free (or resumable). Only then the run card, the
training record, the console log and the environment snapshot appear -- and whatever the
command does next is recorded, exit code and all.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_json
from vcp.core.paths import DatasetPaths, store_path
from vcp.core.time import stamp, utc_now
from vcp.data.dataset import Dataset
from vcp.data.split import SplitPlan, assert_plan_matches, load_plan
from vcp.measure.runs import assert_run_matches, load_run, run_dir, save_run
from vcp.measure.schema import RunCard, RunSource
from vcp.train.checkpoints import expand, register, resolve_final
from vcp.train.env import snapshot, venv_python
from vcp.train.records import (
    TRAIN_DIR,
    append_event,
    has_record,
    load_record,
    save_record,
    train_dir,
)
from vcp.train.schema import (
    Attempt,
    AttemptStatus,
    CheckpointRecord,
    ConfigRef,
    ExportRef,
    TrainRecord,
)
from vcp.train.upload import Runner, merge_uploads, upload

RUN_EXISTS = "run_exists"
RUN_BOUND_ELSEWHERE = "run_bound_elsewhere"
TRAINED_ON_MISMATCH = "trained_on_mismatch"
TERMINATE_TIMEOUT_S = 30


class RunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_id: str
    dataset: str
    plan_id: str
    exports: list[Path] = Field(default_factory=list)
    trained_on: list[str] = Field(default_factory=list)
    venv: Path | None = None
    config: Path | None = None
    seed: int | None = None
    framework: str = ""
    cwd: Path | None = None
    checkpoints: list[str] = Field(default_factory=list)
    final: str | None = None
    uploads: list[str] = Field(default_factory=list)
    resume: bool = False
    notes: str = ""
    command: list[str]
    on_line: Callable[[str], None] | None = None
    rclone_runner: Runner | None = None
    data_root: Path | None = None
    configs_root: Path | None = None


class RunResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    record: TrainRecord
    card: RunCard
    attempt: Attempt
    final: CheckpointRecord | None
    registered: int
    uploaded: int
    verified: int
    skipped: int
    warnings: list[str]


def read_export(export_dir: Path) -> dict[str, Any]:
    manifest = export_dir / "manifest.json"
    if not manifest.is_file():
        raise ValidationFailed(
            f"--export {export_dir}: manifest.json not found (the directory must be a vcp export)",
            fields={"export": str(export_dir)},
        )
    return json.loads(manifest.read_text(encoding="utf-8"))


def derive_trained_on(
    exports: list[Path],
    explicit: list[str],
    *,
    dataset: Dataset,
    plan: SplitPlan,
    data_root: Path,
) -> tuple[list[str], list[ExportRef]]:
    """spec 6.1 step 2: subsets come from export manifests, which must belong to this dataset,
    version and plan; an explicit --trained-on must agree with them."""
    refs: list[ExportRef] = []
    for export_dir in exports:
        m = read_export(export_dir)
        expected = {
            "dataset": dataset.card.name,
            "samples_hash": dataset.card.samples_hash,
            "plan_id": plan.plan_id,
        }
        for key, want in expected.items():
            if m.get(key) != want:
                raise PlanMismatchError(
                    f"export {export_dir}: {key} is {m.get(key)!r}, expected {want!r}",
                    fields={"export": str(export_dir)},
                )
        subset = str(m.get("subset", ""))
        try:
            plan.subset(subset)
        except PlanMismatchError as e:
            e.fields.setdefault("export", str(export_dir))
            raise
        refs.append(
            ExportRef(
                dir=store_path(export_dir, data_root),
                subset=subset,
                format=str(m.get("format", "")),
                manifest_sha256=sha256_file(export_dir / "manifest.json"),
                sample_count=int(m.get("sample_count", 0)),
            )
        )
    derived = sorted({r.subset for r in refs})
    if explicit:
        wanted = sorted(set(explicit))
        for name in wanted:
            plan.subset(name)
        if refs and wanted != derived:
            raise ValidationFailed(
                f"{TRAINED_ON_MISMATCH}: --trained-on {wanted} but the exports say {derived}"
            )
        derived = wanted
    if not derived:
        raise ValidationFailed("trained_on is required: give --export DIR or --trained-on a,b")
    return derived, refs


def config_hash(config: Path | None, command: list[str]) -> str:
    """spec 3: the config file's sha256, or the command's canonical JSON sha256 -- never empty."""
    if config is None:
        return sha256_json(command)
    if not config.is_file():
        raise ValidationFailed(f"--config not found: {config}")
    return sha256_file(config)


def child_env(
    spec: RunSpec, *, data_root: Path, configs_root: Path, python: Path | None
) -> dict[str, str]:
    env = dict(os.environ)
    env["VCP_RUN_ID"] = spec.run_id
    env["VCP_DATA_ROOT"] = str(data_root)
    env["VCP_CONFIGS_ROOT"] = str(configs_root)
    if spec.seed is not None:
        env["VCP_SEED"] = str(spec.seed)
        env["PYTHONHASHSEED"] = str(spec.seed)
    if spec.venv is not None and python is not None:
        env["VIRTUAL_ENV"] = str(spec.venv)
        env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    return env


def execute(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    console: Path,
    on_line: Callable[[str], None] | None,
) -> tuple[int, AttemptStatus]:
    """Run the command, tee its output to the console file, return (exit code, status).

    A KeyboardInterrupt (Ctrl+C, or a test's ``on_line`` raising it) terminates the child and
    is recorded as ``interrupted`` rather than propagating: the attempt must reach the record.
    """
    console.parent.mkdir(parents=True, exist_ok=True)
    status: AttemptStatus = "finished"
    with console.open("w", encoding="utf-8", newline="\n") as log:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                if on_line is not None:
                    on_line(line)
            code = proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                code = proc.wait(timeout=TERMINATE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
            status = "interrupted"
            log.write("\n[vcp] interrupted\n")
    if status == "finished" and code != 0:
        status = "failed"
    return code, status


def _existing(
    spec: RunSpec, data_root: Path, dataset: Dataset, trained_on: list[str], chash: str
) -> tuple[RunCard | None, TrainRecord | None]:
    """spec 6.1 step 4: a free id, a resumable training run, or a refusal."""
    has_card = (run_dir(data_root, spec.run_id) / "run.yaml").is_file()
    if not has_record(data_root, spec.run_id):
        if has_card:
            raise ValidationFailed(
                f"{RUN_BOUND_ELSEWHERE}: run {spec.run_id!r} exists but is not a training run",
                fields={"run": spec.run_id},
            )
        return None, None
    if not spec.resume:
        raise ValidationFailed(
            f"{RUN_EXISTS}: run {spec.run_id!r} already has a training record; "
            "pass --resume to add an attempt",
            fields={"run": spec.run_id},
        )
    record = load_record(data_root, spec.run_id)
    card = load_run(data_root, spec.run_id)
    if record.config_hash != chash:
        raise ValidationFailed(
            f"{RUN_EXISTS}: run {spec.run_id!r} was trained with config_hash "
            f"{record.config_hash[:12]}, this command gives {chash[:12]}; "
            "a different config is a new run",
            fields={"run": spec.run_id},
        )
    assert_run_matches(card, dataset)
    if card.plan_id != spec.plan_id or card.trained_on != trained_on:
        raise ValidationFailed(
            f"{RUN_EXISTS}: run {spec.run_id!r} was trained on {card.trained_on} under plan "
            f"{card.plan_id!r}; --resume must keep them",
            fields={"run": spec.run_id},
        )
    return card, record


def _new(
    spec: RunSpec,
    *,
    data_root: Path,
    dataset: Dataset,
    trained_on: list[str],
    refs: list[ExportRef],
    chash: str,
    cwd: Path,
) -> tuple[RunCard, TrainRecord]:
    card = RunCard(
        run_id=spec.run_id,
        dataset=dataset.card.name,
        samples_hash=dataset.card.samples_hash,
        plan_id=spec.plan_id,
        trained_on=trained_on,
        source=RunSource(
            framework=spec.framework,
            config_hash=chash,
            export_manifest_sha=refs[0].manifest_sha256 if refs else None,
            notes=spec.notes,
        ),
        created_at=stamp(),
    )
    record = TrainRecord(
        run_id=spec.run_id,
        dataset=dataset.card.name,
        plan_id=spec.plan_id,
        trained_on=trained_on,
        exports=refs,
        config_hash=chash,
        seed=spec.seed,
        framework=spec.framework,
        venv=store_path(spec.venv, data_root) if spec.venv is not None else None,
        cwd=store_path(cwd, data_root),
        command=list(spec.command),
        notes=spec.notes,
    )
    return card, record


def _replace_attempt(record: TrainRecord, attempt: Attempt) -> TrainRecord:
    attempts = [attempt if a.n == attempt.n else a for a in record.attempts]
    return record.model_copy(update={"attempts": attempts})


def _finish_checkpoints(
    spec: RunSpec, record: TrainRecord, card: RunCard, *, data_root: Path, cwd: Path, n: int, status: str
) -> tuple[TrainRecord, RunCard, CheckpointRecord | None, int, list[str]]:
    """spec 6.1 step 8: register every glob hit; resolve the final one when the command
    succeeded; write weights_hash to the run card."""
    warnings: list[str] = []
    patterns = [*spec.checkpoints, *([spec.final] if spec.final else [])]
    record, added = register(record, expand(patterns, cwd), data_root=data_root, attempt=n)
    for c in added:
        append_event(
            data_root, spec.run_id, "checkpoint", n,
            path=c.path, sha256=c.sha256, bytes=c.bytes, source=c.source,
        )
    save_record(data_root, record)  # registrations survive a failing --final below
    final: CheckpointRecord | None = None
    if status == "finished":
        record, final = resolve_final(record, spec.final, cwd=cwd, data_root=data_root)
        if final is None:
            warnings.append("final=none")
    elif spec.final:
        warnings.append("final=skipped (command failed)")
    if final is not None:
        source = card.source.model_copy(update={"weights_hash": final.sha256})
        card = card.model_copy(update={"source": source})
        save_run(data_root, card)
    save_record(data_root, record)
    return record, card, final, len(added), warnings


def _upload_all(
    spec: RunSpec, record: TrainRecord, *, data_root: Path, n: int
) -> tuple[TrainRecord, int, int, int]:
    uploaded = verified = skipped = 0
    for dest in spec.uploads:
        outcome = upload(record, dest, data_root=data_root, runner=spec.rclone_runner)
        record = merge_uploads(record, outcome.records)
        save_record(data_root, record)
        for r in outcome.records:
            append_event(
                data_root, spec.run_id, "uploaded", n,
                dest=dest, name=r.name, sha256=r.sha256, verified=r.verified,
            )
        uploaded += outcome.uploaded
        skipped += outcome.skipped
        verified += sum(1 for r in outcome.records if r.verified)
    return record, uploaded, verified, skipped


def train_run(spec: RunSpec) -> RunResult:
    paths = DatasetPaths.resolve(
        spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    data_root, configs_root = paths.data_root, paths.configs_root
    dataset = Dataset.load(spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root)
    plan = load_plan(paths, spec.plan_id)
    assert_plan_matches(plan, dataset.card)
    if not spec.command or spec.command[0].startswith("-"):
        raise ValidationFailed("a training command is required after -- (e.g. -- python train.py)")
    cwd = (spec.cwd or Path.cwd()).resolve()
    trained_on, refs = derive_trained_on(
        spec.exports, spec.trained_on, dataset=dataset, plan=plan, data_root=data_root
    )
    chash = config_hash(spec.config, spec.command)
    python = venv_python(spec.venv) if spec.venv is not None else None
    env = child_env(spec, data_root=data_root, configs_root=configs_root, python=python)
    if shutil.which(spec.command[0], path=env.get("PATH")) is None and not Path(spec.command[0]).is_file():
        raise ValidationFailed(f"command not found: {spec.command[0]!r}")
    card, record = _existing(spec, data_root, dataset, trained_on, chash)
    created = card is None
    if card is None or record is None:
        card, record = _new(
            spec, data_root=data_root, dataset=dataset, trained_on=trained_on, refs=refs, chash=chash, cwd=cwd
        )
    n = len(record.attempts) + 1
    # spec 6.1 step 5: the first writes.
    run_root = run_dir(data_root, spec.run_id)
    train_dir(data_root, spec.run_id).mkdir(parents=True, exist_ok=True)
    if spec.config is not None:
        copy_rel = f"{TRAIN_DIR}/config.{n}{spec.config.suffix}"
        shutil.copy2(spec.config, run_root / copy_rel)
        record = record.model_copy(
            update={"config": ConfigRef(path=store_path(spec.config, data_root), sha256=chash, copy=copy_rel)}
        )
    attempt = Attempt(n=n, started_at=stamp(), console=f"{TRAIN_DIR}/console.{n}.log")
    record = record.model_copy(update={"attempts": [*record.attempts, attempt]})
    if created:
        save_run(data_root, card)
    save_record(data_root, record)
    append_event(data_root, spec.run_id, "started", n, command=spec.command, cwd=str(cwd), seed=spec.seed)
    # step 6: environment snapshot
    snap = snapshot(python, cwd)
    env_rel = f"{TRAIN_DIR}/env.{n}.json"
    with (run_root / env_rel).open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(snap.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
    attempt = attempt.model_copy(update={"env": env_rel})
    record = _replace_attempt(record, attempt)
    save_record(data_root, record)
    append_event(data_root, spec.run_id, "env", n, python=snap.python, gpus=snap.gpus, torch=snap.torch)
    # step 7: the command
    started = utc_now()
    code, status = execute(
        spec.command, cwd=cwd, env=env, console=run_root / attempt.console, on_line=spec.on_line
    )
    attempt = attempt.model_copy(
        update={
            "finished_at": stamp(),
            "duration_s": round((utc_now() - started).total_seconds(), 3),
            "exit_code": code,
            "status": status,
        }
    )
    record = _replace_attempt(record, attempt)
    save_record(data_root, record)
    append_event(data_root, spec.run_id, "finished", n, exit_code=code, status=status, duration_s=attempt.duration_s)
    # steps 8-9
    record, card, final, registered, warnings = _finish_checkpoints(
        spec, record, card, data_root=data_root, cwd=cwd, n=n, status=status
    )
    uploaded = verified = skipped = 0
    if status == "finished" and spec.uploads:
        record, uploaded, verified, skipped = _upload_all(spec, record, data_root=data_root, n=n)
    if spec.seed is None:
        warnings.append("seed=none")
    if spec.venv is None:
        warnings.append("venv=inherited")
    return RunResult(
        record=record,
        card=card,
        attempt=attempt,
        final=final,
        registered=registered,
        uploaded=uploaded,
        verified=verified,
        skipped=skipped,
        warnings=warnings,
    )
```

`src/vcp/cli_train.py`（本任務只有 `run`；Task 6 加 `upload` / `status`）：

```python
"""``vcp train``: training-layer commands. Every command ends with a VERDICT line."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from vcp.cli_common import (
    CmdResult,
    ConfigsRootOpt,
    DataRootOpt,
    JsonOpt,
    parse_csv,
    run_command,
)
from vcp.core.errors import ValidationFailed
from vcp.core.log import FieldValue, Status
from vcp.train.run import RunSpec, train_run

train_app = typer.Typer(no_args_is_help=True, help="training commands")

RunOpt = Annotated[str, typer.Option("--run", help="run id (path-safe name)")]


@train_app.command(
    "run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def run_cmd(
    ctx: typer.Context,
    run: RunOpt,
    dataset: Annotated[str, typer.Option("--dataset", help="dataset name")],
    plan: Annotated[str, typer.Option("--plan", help="plan id")],
    export: Annotated[
        list[Path] | None, typer.Option("--export", help="vcp data export directory (repeatable)")
    ] = None,
    trained_on: Annotated[
        str | None, typer.Option("--trained-on", help="comma-separated subsets (when no --export)")
    ] = None,
    venv: Annotated[Path | None, typer.Option("--venv", help="framework virtualenv directory")] = None,
    config: Annotated[Path | None, typer.Option("--config", help="config file to hash and copy")] = None,
    seed: Annotated[int | None, typer.Option("--seed")] = None,
    framework: Annotated[str, typer.Option("--framework", help="free text, e.g. 'ultralytics 8.3.0'")] = "",
    cwd: Annotated[Path | None, typer.Option("--cwd", help="where the command runs (default: here)")] = None,
    checkpoints: Annotated[
        list[str] | None, typer.Option("--checkpoints", help="glob relative to --cwd (repeatable)")
    ] = None,
    final: Annotated[str | None, typer.Option("--final", help="glob of THE checkpoint (exactly one file)")] = None,
    upload: Annotated[
        list[str] | None, typer.Option("--upload", help="remote:path (rclone) or a directory (repeatable)")
    ] = None,
    resume: Annotated[bool, typer.Option("--resume", help="add an attempt to an existing training run")] = False,
    notes: Annotated[str, typer.Option("--notes")] = "",
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Run a training command and record it as a run: -- COMMAND... after the options."""

    def fn() -> CmdResult:
        try:
            spec = RunSpec(
                run_id=run,
                dataset=dataset,
                plan_id=plan,
                exports=list(export or []),
                trained_on=parse_csv(trained_on),
                venv=venv,
                config=config,
                seed=seed,
                framework=framework,
                cwd=cwd,
                checkpoints=list(checkpoints or []),
                final=final,
                uploads=list(upload or []),
                resume=resume,
                notes=notes,
                command=list(ctx.args),
                on_line=lambda line: typer.echo(line, nl=False, err=json_mode),
                data_root=data_root,
                configs_root=configs_root,
            )
        except ValidationError as e:
            raise ValidationFailed(str(e), location="vcp train run") from e
        res = train_run(spec)
        fields: dict[str, FieldValue] = {
            "run": run,
            "attempt": res.attempt.n,
            "exit_code": res.attempt.exit_code if res.attempt.exit_code is not None else -1,
            "duration_s": res.attempt.duration_s or 0.0,
            "checkpoints": len(res.record.checkpoints),
            "final": res.final.sha256[:12] if res.final else "none",
            "uploaded": res.uploaded,
            "verified": res.verified,
            "seed": spec.seed if spec.seed is not None else "none",
            "venv": spec.venv.name if spec.venv is not None else "inherited",
        }
        if res.skipped:
            fields["skipped"] = res.skipped
        failed = res.attempt.status != "finished" or res.verified < res.uploaded + res.skipped
        status: Status = "FAIL" if failed else ("WARN" if res.warnings else "OK")
        if res.attempt.status != "finished":
            fields["status_attempt"] = res.attempt.status
        human = [f"attempt {res.attempt.n}: {res.attempt.status} (exit {res.attempt.exit_code})"]
        human += [f"warning: {w}" for w in res.warnings]
        return status, fields, res.record.model_dump(mode="json"), human

    run_command("train.run", json_mode, data_root, fn)
```

`src/vcp/cli.py`：在 `from vcp.cli_fuse import fuse_app` 下一行加 `from vcp.cli_train import train_app`；在 `app.add_typer(fuse_app, name="fuse")` 下一行加 `app.add_typer(train_app, name="train")`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/data/test_split_plan_matches.py tests/unit/train tests/unit/test_cli_train.py tests/unit/test_cli.py -o addopts="" -q`
Expected: 全部 PASS（`test_run.py` 會真的跑好幾次假腳本與環境探針，約 20 秒）；ruff format / check 乾淨。若 `test_execute_interrupt_terminates_child` 在 Windows 上 `code` 為 1 而非負數，斷言 `code != 0` 已涵蓋。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/split.py src/vcp/train/run.py src/vcp/cli_train.py src/vcp/cli.py tests/unit/data/test_split_plan_matches.py tests/unit/train/test_run.py tests/unit/test_cli_train.py
git commit -m "feat(train): vcp train run——trained_on 由 export manifest 推導、寫 run 後快照、tee 執行、登記 checkpoint、上傳驗證

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `vcp train upload` 與 `vcp train status`（spec §6 upload / status 列、§6.3）

**Files:**
- Create: `src/vcp/train/status.py`
- Modify: `src/vcp/cli_train.py`（加 `upload` / `status` 命令）
- Test: `tests/unit/train/test_status.py`、`tests/unit/test_cli_train.py`（加兩個 CLI 測試）

**Interfaces:**
- Consumes: Task 1 `load_record` / `save_record` / `append_event`；Task 3 `missing` / `drift`；Task 4 `upload` / `merge_uploads` / `Runner`。
- Produces: `status.StatusResult(record: TrainRecord, backed: int, unbacked: list[str], missing: list[str], drift: list[str], running: int)`；`status(data_root, run_id, *, verify=False) -> StatusResult`（`backed` = 有 `verified=True` 且 sha 相同的上傳紀錄的 checkpoint 數；`unbacked` = 其餘的 path；`drift` 只在 `verify=True` 時算）；`upload_run(data_root, run_id, dest, *, only_final=False, runner=None) -> tuple[TrainRecord, UploadOutcome]`（載入紀錄、上傳、合併、存檔、事件，attempt = 最後一個 attempt 的 n）；CLI `upload`（`cmd=train.upload`，欄位 `run= dest= uploaded= verified= skipped=`）與 `status`（`cmd=train.status`，欄位 `run= attempts= checkpoints= backed= unbacked= running=`，`--verify` 加 `drift=`）。

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/train/test_status.py`：

```python
import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.train.checkpoints import mark_final, register
from vcp.train.records import read_events, save_record
from vcp.train.schema import Attempt, TrainRecord
from vcp.train.status import status, upload_run

STAMP = "2026-09-05T00:00:00.000Z"


def _seeded(roots):
    w = roots.data / "work" / "weights"
    w.mkdir(parents=True)
    (w / "best.pt").write_bytes(b"best")
    (w / "last.pt").write_bytes(b"last")
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="work",
        command=["python"],
        attempts=[Attempt(n=1, started_at=STAMP, console="train/console.1.log", status="finished", exit_code=0)],
    )
    rec, _ = register(rec, [w / "best.pt", w / "last.pt"], data_root=roots.data, attempt=1)
    rec = mark_final(rec, "work/weights/best.pt", sha256_file(w / "best.pt"))
    save_record(roots.data, rec)
    return rec, w


def test_status_counts_backed_and_running(roots, tmp_path):
    rec, w = _seeded(roots)
    st = status(roots.data, "r1")
    assert st.backed == 0 and st.unbacked == ["work/weights/best.pt", "work/weights/last.pt"]
    assert st.missing == [] and st.drift == [] and st.running == 0
    upload_run(roots.data, "r1", str(tmp_path / "vault"), only_final=True)
    st = status(roots.data, "r1")
    assert st.backed == 1 and st.unbacked == ["work/weights/last.pt"]
    (w / "best.pt").write_bytes(b"tampered")
    (w / "last.pt").unlink()
    st = status(roots.data, "r1")
    assert st.missing == ["work/weights/last.pt"] and st.drift == []
    st = status(roots.data, "r1", verify=True)
    assert st.drift == ["work/weights/best.pt", "work/weights/last.pt"]
    running = rec.model_copy(update={"attempts": [rec.attempts[0].model_copy(update={"status": "running", "exit_code": None})]})
    save_record(roots.data, running)
    assert status(roots.data, "r1").running == 1
    with pytest.raises(ValidationFailed, match="not found"):
        status(roots.data, "ghost")


def test_upload_run_records_and_is_idempotent(roots, tmp_path):
    rec, w = _seeded(roots)
    rec2, out = upload_run(roots.data, "r1", str(tmp_path / "vault"))
    assert out.uploaded == 2 and len(rec2.uploads) == 2 and all(u.verified for u in rec2.uploads)
    events = read_events(roots.data, "r1")
    assert [e["event"] for e in events] == ["uploaded", "uploaded"] and events[0]["attempt"] == 1
    rec3, again = upload_run(roots.data, "r1", str(tmp_path / "vault"))
    assert again.uploaded == 0 and again.skipped == 2 and len(rec3.uploads) == 2
```

`tests/unit/test_cli_train.py` 加：

```python
def test_train_upload_and_status_cli(roots, tmp_path):
    seed_det(roots)
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_text(FAKE, encoding="utf-8")
    assert _run(work, "--seed", "1").exit_code == 0
    r = runner.invoke(app, ["train", "status", "--run", "r1"])
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "status=WARN" in v and "checkpoints=1" in v and "unbacked=1" in v and "backed=0" in v and "running=0" in v
    r = runner.invoke(app, ["train", "upload", "--run", "r1", "--dest", str(tmp_path / "vault")])
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "status=OK" in v and "uploaded=1" in v and "verified=1" in v and "skipped=0" in v
    r = runner.invoke(app, ["train", "upload", "--run", "r1", "--dest", str(tmp_path / "vault"), "--json"])
    assert r.exit_code == 0 and "skipped=1" in _verdict(r.output)
    doc = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert len(doc["result"]["uploads"]) == 1
    r = runner.invoke(app, ["train", "status", "--run", "r1", "--verify"])
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output) and "unbacked=0" in _verdict(r.output)
    (work / "weights" / "best.pt").write_bytes(b"tampered")
    r = runner.invoke(app, ["train", "status", "--run", "r1", "--verify"])
    assert r.exit_code == 0 and "status=WARN" in _verdict(r.output) and "drift=1" in _verdict(r.output)
    r = runner.invoke(app, ["train", "upload", "--run", "r1", "--dest", str(tmp_path / "v2")])
    assert r.exit_code == 1 and "changed since" in _verdict(r.output)
    r = runner.invoke(app, ["train", "status", "--run", "ghost"])
    assert r.exit_code == 1 and "run=ghost" in _verdict(r.output)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/train/test_status.py tests/unit/test_cli_train.py -o addopts="" -q`
Expected: `ModuleNotFoundError: No module named 'vcp.train.status'`；CLI `No such command 'status'`。

- [ ] **Step 3: 寫 `status.py` 與兩個命令**

`src/vcp/train/status.py`：

```python
"""``vcp train status`` (read-only) and ``vcp train upload`` (a later or second backup)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.train.checkpoints import drift as _drift
from vcp.train.checkpoints import missing as _missing
from vcp.train.records import append_event, load_record, save_record
from vcp.train.schema import TrainRecord
from vcp.train.upload import Runner, UploadOutcome, merge_uploads, upload


@dataclass(frozen=True)
class StatusResult:
    record: TrainRecord
    backed: int
    unbacked: list[str]
    missing: list[str]
    drift: list[str]
    running: int


def _backed_paths(record: TrainRecord) -> set[str]:
    """Checkpoints that have at least one verified upload of exactly their bytes."""
    verified = {u.sha256 for u in record.uploads if u.verified}
    return {c.path for c in record.checkpoints if c.sha256 in verified}


def status(data_root: Path, run_id: str, *, verify: bool = False) -> StatusResult:
    record = load_record(data_root, run_id)
    backed = _backed_paths(record)
    return StatusResult(
        record=record,
        backed=len(backed),
        unbacked=[c.path for c in record.checkpoints if c.path not in backed],
        missing=_missing(record, data_root),
        drift=_drift(record, data_root) if verify else [],
        running=sum(1 for a in record.attempts if a.status == "running"),
    )


def upload_run(
    data_root: Path,
    run_id: str,
    dest: str,
    *,
    only_final: bool = False,
    runner: Runner | None = None,
) -> tuple[TrainRecord, UploadOutcome]:
    """Upload a recorded run's checkpoints now; the record and the event log both learn of it."""
    record = load_record(data_root, run_id)
    outcome = upload(record, dest, data_root=data_root, only_final=only_final, runner=runner)
    record = merge_uploads(record, outcome.records)
    save_record(data_root, record)
    attempt = record.attempts[-1].n if record.attempts else 0
    for r in outcome.records:
        append_event(
            data_root, run_id, "uploaded", attempt,
            dest=dest, name=r.name, sha256=r.sha256, verified=r.verified,
        )
    return record, outcome
```

`src/vcp/cli_train.py` 加（import `from vcp.train.status import status as status_view, upload_run`）：

```python
@train_app.command("upload")
def upload_cmd(
    run: RunOpt,
    dest: Annotated[str, typer.Option("--dest", help="remote:path (rclone) or a directory")],
    only: Annotated[str | None, typer.Option("--only", help="'final' to upload only the final checkpoint")] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Upload a run's registered checkpoints and verify them (idempotent)."""

    def fn() -> CmdResult:
        if only not in (None, "final"):
            raise ValidationFailed(f"--only accepts 'final', got {only!r}")
        root = resolve_data_root(data_root)
        record, out = upload_run(root, run, dest, only_final=only == "final")
        verified = sum(1 for r in out.records if r.verified)
        fields: dict[str, FieldValue] = {
            "run": run,
            "dest": dest,
            "uploaded": out.uploaded,
            "verified": verified,
            "skipped": out.skipped,
        }
        status: Status = "FAIL" if verified < len(out.records) else "OK"
        human = [f"{r.name}: {'verified' if r.verified else 'NOT verified'}" for r in out.records]
        return status, fields, record.model_dump(mode="json"), human

    run_command("train.upload", json_mode, data_root, fn)


@train_app.command("status")
def status_cmd(
    run: RunOpt,
    verify: Annotated[bool, typer.Option("--verify", help="re-hash every checkpoint")] = False,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Attempts, checkpoints and their backups. Reads, never writes."""

    def fn() -> CmdResult:
        st = status_view(resolve_data_root(data_root), run, verify=verify)
        fields: dict[str, FieldValue] = {
            "run": run,
            "attempts": len(st.record.attempts),
            "checkpoints": len(st.record.checkpoints),
            "backed": st.backed,
            "unbacked": len(st.unbacked),
            "running": st.running,
        }
        if verify:
            fields["drift"] = len(st.drift)
        if st.missing:
            fields["missing"] = len(st.missing)
        human = [f"unbacked: {p}" for p in st.unbacked]
        human += [f"missing: {p}" for p in st.missing]
        human += [f"drift: {p}" for p in st.drift]
        warn = bool(st.unbacked or st.missing or st.drift or st.running)
        payload = {**st.record.model_dump(mode="json"), "drift": st.drift, "missing": st.missing}
        return ("WARN" if warn else "OK"), fields, payload, human

    run_command("train.status", json_mode, data_root, fn)
```

（`resolve_data_root` 從 `vcp.core.paths` import。）

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/train tests/unit/test_cli_train.py -o addopts="" -q`
Expected: 全部 PASS；ruff format / check 乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/train/status.py src/vcp/cli_train.py tests/unit/train/test_status.py tests/unit/test_cli_train.py
git commit -m "feat(train): vcp train upload（事後補傳、冪等）與 vcp train status（唯讀；無副本 / 漂移 / 未結束皆 WARN）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: `MaterializedReader` 與 `Session`（spec §8）

**Files:**
- Create: `src/vcp/train/reader.py`
- Create: `src/vcp/train/session.py`
- Modify: `src/vcp/train/__init__.py`（匯出 `MaterializedReader`、`Record`、`Session`）
- Test: `tests/unit/train/test_reader.py`、`tests/unit/train/test_session.py`

**Interfaces:**
- Consumes: 資料層 `Dataset.load / subset`、`load_plan`、`DatasetPaths`、`vcp.data.materialize.manifest.read_manifest / ManifestRow`；Task 1 `has_record` / `load_record` / `save_record` / `append_event`；Task 3 `register` / `mark_final`；`vcp.core.paths.resolve_data_root / store_path`、`vcp.core.hashing.sha256_file`、`vcp.core.errors.ValidationFailed / IntegrityError`。
- Produces: `Record(sample_id, sample, labels, arrays: dict[str, np.ndarray])`（frozen dataclass）；`MaterializedReader(name, mode_dir, *, plan_id=None, subset=None, unseal=False, reason=None, data_root=None, configs_root=None, verify=False)` with `ids: list[str]`、`__len__`、`__iter__`、`__getitem__(sample_id) -> Record`、`rows(sample_id) -> list[ManifestRow]`；`Session(run_id, data_root)`、`Session.current(data_root=None) -> Session`、`register_checkpoint(path, *, final=False) -> CheckpointRecord`、`note(key, value) -> None`；`vcp.train.__all__ = ["MaterializedReader", "Record", "Session"]`。

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/train/test_reader.py`：

```python
import numpy as np
import pytest

from helpers import det_samples, make_card, write_dicom_study, write_images
from vcp.core.errors import IntegrityError, SealedSubsetError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.train import MaterializedReader, Record


def _image_ds(roots, name="tiny", n=8):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def _dicom_ds(roots, name="dcm"):
    write_dicom_study(roots.data / "raw" / name, study_uid="1.2.1", series=2, slices=3)
    get_importer("dicom").run(
        ImportSpec(importer="dicom", src=roots.data / "raw" / name, name=name, options={}, license="CC0", url="u", downloaded_at="2026-09-03", data_root=roots.data, configs_root=roots.configs)
    )


def _mat(roots, name, **kw):
    return materialize(MaterializeSpec(name=name, data_root=roots.data, configs_root=roots.configs, **kw))


def test_reader_iterates_npy_rows_per_view(roots):
    ds, plan, paths = _image_ds(roots)
    res = _mat(roots, "tiny", mode="npy")
    assert res.failed == 0
    reader = MaterializedReader("tiny", "npy", data_root=roots.data, configs_root=roots.configs)
    assert len(reader) == 8 and reader.ids == [s.sample_id for s in ds.samples]
    recs = list(reader)
    assert all(isinstance(r, Record) for r in recs)
    first = recs[0]
    assert first.sample_id == "s0000" and first.labels is not None and set(first.arrays) == {"0"}
    row = reader.rows("s0000")[0]
    assert list(first.arrays["0"].shape) == row.shape and str(first.arrays["0"].dtype) == row.dtype
    assert reader["s0003"].sample.sample_id == "s0003"


def test_reader_png_resized_and_subset_restriction(roots):
    ds, plan, paths = _image_ds(roots)
    _mat(roots, "tiny", mode="png", resize=4)
    reader = MaterializedReader("tiny", "png-r4", plan_id="fixed-v1", subset="train", data_root=roots.data, configs_root=roots.configs)
    assert set(reader.ids) == plan.ids_in("train") and len(reader) == len(plan.ids_in("train"))
    arr = next(iter(reader)).arrays["0"]
    assert arr.dtype == np.uint8 and max(arr.shape[:2]) == 4
    with pytest.raises(SealedSubsetError):
        MaterializedReader("tiny", "png-r4", plan_id="fixed-v1", subset="holdout", data_root=roots.data, configs_root=roots.configs)
    sealed = MaterializedReader("tiny", "png-r4", plan_id="fixed-v1", subset="holdout", unseal=True, reason="test", data_root=roots.data, configs_root=roots.configs)
    assert len(sealed) == len(plan.ids_in("holdout"))
    with pytest.raises(ValidationFailed, match="plan_id and subset"):
        MaterializedReader("tiny", "png-r4", subset="train", data_root=roots.data, configs_root=roots.configs)


def test_reader_stacked_sequences_keyed_by_seq_id(roots):
    _dicom_ds(roots)
    res = _mat(roots, "dcm", mode="npy", stack_seq=True)
    assert res.failed == 0
    reader = MaterializedReader("dcm", "npy", data_root=roots.data, configs_root=roots.configs)
    rec = next(iter(reader))
    assert len(rec.arrays) == 2 and all(a.ndim == 3 and a.shape[0] == 3 for a in rec.arrays.values())
    assert set(rec.arrays) == {r.seq_id for r in reader.rows(rec.sample_id)}


def test_reader_failures(roots):
    ds, plan, paths = _image_ds(roots)
    with pytest.raises(ValidationFailed, match="materialize cache not found"):
        MaterializedReader("tiny", "npy", data_root=roots.data, configs_root=roots.configs)
    _mat(roots, "tiny", mode="npy")
    manifest = paths.cache_dir / "materialize" / "npy" / "manifest.jsonl"
    lines = manifest.read_text(encoding="utf-8").splitlines()
    without = [line for line in lines if '"s0000"' not in line]  # drop s0000's row(s)
    assert len(without) < len(lines)
    manifest.write_text("\n".join(without) + "\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="no materialized rows") as ei:
        MaterializedReader("tiny", "npy", data_root=roots.data, configs_root=roots.configs)
    assert ei.value.location == "s0000"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    reader = MaterializedReader("tiny", "npy", verify=True, data_root=roots.data, configs_root=roots.configs)
    row = reader.rows("s0001")[0]
    target = paths.cache_dir / "materialize" / "npy" / row.out
    np.save(target, np.zeros((2, 2), dtype=np.uint8))
    with pytest.raises(IntegrityError):
        reader["s0001"]
```

`tests/unit/train/test_session.py`：

```python
import os
import subprocess
import sys

import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.train import Session
from vcp.train.records import load_record, read_events, save_record
from vcp.train.schema import Attempt, TrainRecord

STAMP = "2026-09-05T00:00:00.000Z"


def _running(roots):
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="work",
        command=["python"],
        attempts=[Attempt(n=2, started_at=STAMP, console="train/console.2.log")],
    )
    save_record(roots.data, rec)
    return rec


def test_current_requires_the_wrapper_env(roots, monkeypatch):
    monkeypatch.delenv("VCP_RUN_ID", raising=False)
    with pytest.raises(ValidationFailed, match="VCP_RUN_ID"):
        Session.current()
    monkeypatch.setenv("VCP_RUN_ID", "ghost")
    with pytest.raises(ValidationFailed, match="no training record"):
        Session.current(roots.data)


def test_register_and_note_in_process(roots, monkeypatch, tmp_path):
    _running(roots)
    monkeypatch.setenv("VCP_RUN_ID", "r1")
    monkeypatch.setenv("VCP_DATA_ROOT", str(roots.data))
    ckpt = tmp_path / "epoch3.pt"
    ckpt.write_bytes(b"e3")
    s = Session.current()
    rec = s.register_checkpoint(ckpt)
    assert rec.sha256 == sha256_file(ckpt) and rec.attempt == 2 and rec.source == "session" and not rec.final
    best = tmp_path / "best.pt"
    best.write_bytes(b"b")
    final = s.register_checkpoint(best, final=True)
    assert final.final
    s.note("val_auc", 0.91)
    stored = load_record(roots.data, "r1")
    assert [c.final for c in stored.checkpoints] == [False, True]
    events = read_events(roots.data, "r1")
    assert [e["event"] for e in events] == ["checkpoint", "checkpoint", "note"]
    assert events[2] == {**events[2], "key": "val_auc", "value": 0.91, "attempt": 2}
    assert s.register_checkpoint(ckpt).sha256 == rec.sha256  # same bytes: no duplicate
    assert len(load_record(roots.data, "r1").checkpoints) == 2
    with pytest.raises(ValidationFailed, match="not a file"):
        s.register_checkpoint(tmp_path / "nope.pt")


def test_session_from_a_child_process(roots, tmp_path):
    _running(roots)
    ckpt = tmp_path / "child.pt"
    ckpt.write_bytes(b"child")
    code = (
        "from vcp.train import Session; import sys; "
        "s = Session.current(); s.register_checkpoint(sys.argv[1], final=True); s.note('epoch', 1)"
    )
    env = {**os.environ, "VCP_RUN_ID": "r1", "VCP_DATA_ROOT": str(roots.data)}
    proc = subprocess.run([sys.executable, "-c", code, str(ckpt)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    stored = load_record(roots.data, "r1")
    assert stored.checkpoints[0].final and stored.checkpoints[0].sha256 == sha256_file(ckpt)
    assert read_events(roots.data, "r1")[-1]["key"] == "epoch"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/train/test_reader.py tests/unit/train/test_session.py -o addopts="" -q`
Expected: `ImportError: cannot import name 'MaterializedReader' from 'vcp.train'`。

- [ ] **Step 3: 寫 `reader.py`、`session.py`，更新套件匯出**

`src/vcp/train/reader.py`：

```python
"""Read a materialize cache from a training loop (spec 8.1).

Arrays come from ``cache/materialize/<mode_dir>/`` through its manifest -- the data layer's
authoritative map -- and labels from ``samples.jsonl``. Nothing here augments, batches or
depends on a training framework; wrapping a record in a torch Dataset is the caller's ten
lines (see README).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.materialize.manifest import ManifestRow, read_manifest
from vcp.data.schema import Labels, Sample
from vcp.data.split import load_plan


@dataclass(frozen=True)
class Record:
    """One sample: its arrays keyed by view index (``"0"``) or, for stacked rows, by seq_id."""

    sample_id: str
    sample: Sample
    labels: Labels | None
    arrays: dict[str, np.ndarray]


def _key(row: ManifestRow) -> str:
    return str(row.view) if row.view is not None else (row.seq_id or "")


class MaterializedReader:
    def __init__(
        self,
        name: str,
        mode_dir: str,
        *,
        plan_id: str | None = None,
        subset: str | None = None,
        unseal: bool = False,
        reason: str | None = None,
        data_root: Path | None = None,
        configs_root: Path | None = None,
        verify: bool = False,
    ) -> None:
        if (plan_id is None) != (subset is None):
            raise ValidationFailed("plan_id and subset must be given together")
        self.paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        self.dataset = Dataset.load(name, data_root=data_root, configs_root=configs_root)
        self.root = self.paths.cache_dir / "materialize" / mode_dir
        self.verify = verify
        manifest = self.root / "manifest.jsonl"
        if not manifest.is_file():
            raise ValidationFailed(f"materialize cache not found: {manifest}")
        self._rows: dict[str, list[ManifestRow]] = {}
        for row in read_manifest(manifest).values():
            self._rows.setdefault(row.sample_id, []).append(row)
        if plan_id is not None and subset is not None:
            plan = load_plan(self.paths, plan_id)
            samples = self.dataset.subset(
                subset, plan, unseal=unseal, reason=reason, paths=self.paths
            )
        else:
            samples = list(self.dataset.samples)
        absent = [s.sample_id for s in samples if s.sample_id not in self._rows]
        if absent:
            raise ValidationFailed(
                f"{len(absent)} samples have no materialized rows in {mode_dir!r} "
                f"(e.g. {absent[:3]}); run vcp data materialize first",
                location=absent[0],
            )
        self._samples = {s.sample_id: s for s in samples}
        self.ids: list[str] = [s.sample_id for s in samples]

    def __len__(self) -> int:
        return len(self.ids)

    def __iter__(self) -> Iterator[Record]:
        for sid in self.ids:
            yield self[sid]

    def rows(self, sample_id: str) -> list[ManifestRow]:
        return list(self._rows[sample_id])

    def __getitem__(self, sample_id: str) -> Record:
        sample = self._samples[sample_id]
        arrays = {_key(row): self._load(row) for row in self._rows[sample_id]}
        return Record(sample_id=sample_id, sample=sample, labels=sample.labels, arrays=arrays)

    def _load(self, row: ManifestRow) -> np.ndarray:
        path = self.root / row.out
        if self.verify and sha256_file(path) != row.sha256:
            raise IntegrityError(
                f"materialized file {row.out} does not match its manifest sha256",
                location=row.sample_id,
            )
        if path.suffix == ".npy":
            return np.load(path)
        with Image.open(path) as im:
            return np.asarray(im)
```

`src/vcp/train/session.py`：

```python
"""``Session``: the one thing a hand-written training loop needs from vcp (spec 8.2).

``vcp train run`` exports ``VCP_RUN_ID`` / ``VCP_DATA_ROOT`` to the command it wraps; a loop
running under it can register checkpoints as they are written and leave notes in the event
log. Only the session writes ``train.yaml`` while the command runs -- the wrapper writes it
before and after -- so there is no concurrent writer.
"""

from __future__ import annotations

import os
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import resolve_data_root, store_path
from vcp.train.checkpoints import mark_final, register
from vcp.train.records import append_event, has_record, load_record, save_record
from vcp.train.schema import CheckpointRecord


class Session:
    def __init__(self, run_id: str, data_root: Path) -> None:
        self.run_id = run_id
        self.data_root = data_root

    @classmethod
    def current(cls, data_root: Path | None = None) -> Session:
        run_id = os.environ.get("VCP_RUN_ID")
        if not run_id:
            raise ValidationFailed("not running under `vcp train run` (VCP_RUN_ID is not set)")
        root = resolve_data_root(data_root)
        if not has_record(root, run_id):
            raise ValidationFailed(f"no training record for run {run_id!r} under {root}")
        return cls(run_id, root)

    def _attempt(self) -> int:
        record = load_record(self.data_root, self.run_id)
        return record.attempts[-1].n if record.attempts else 1

    def register_checkpoint(self, path: str | Path, *, final: bool = False) -> CheckpointRecord:
        file = Path(path).resolve()
        if not file.is_file():
            raise ValidationFailed(f"checkpoint is not a file: {file}")
        record = load_record(self.data_root, self.run_id)
        n = record.attempts[-1].n if record.attempts else 1
        record, added = register(record, [file], data_root=self.data_root, attempt=n, source="session")
        stored, digest = store_path(file, self.data_root), sha256_file(file)
        if final:
            record = mark_final(record, stored, digest)
        save_record(self.data_root, record)
        entry = next(c for c in record.checkpoints if c.path == stored and c.sha256 == digest)
        append_event(
            self.data_root, self.run_id, "checkpoint", n,
            path=entry.path, sha256=entry.sha256, bytes=entry.bytes, source="session", final=final,
        )
        return entry

    def note(self, key: str, value: str | int | float | bool) -> None:
        append_event(self.data_root, self.run_id, "note", self._attempt(), key=key, value=value)
```

`src/vcp/train/__init__.py`：

```python
"""Training layer: wrap any training command, record its identity, keep checkpoints backed up."""

from vcp.train.reader import MaterializedReader, Record
from vcp.train.session import Session

__all__ = ["MaterializedReader", "Record", "Session"]
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/train -o addopts="" -q`
Expected: 全部 PASS；ruff format / check 乾淨。`test_session_from_a_child_process` 靠測試 venv 裡以 editable 裝的 vcp；若子程序 `ImportError`，把 `env` 改成 `{**os.environ, "VCP_RUN_ID": ..., "VCP_DATA_ROOT": ...}`（保留 `PYTHONPATH` 等）。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/train/reader.py src/vcp/train/session.py src/vcp/train/__init__.py tests/unit/train/test_reader.py tests/unit/train/test_session.py
git commit -m "feat(train): MaterializedReader（依 manifest 讀快取、子集限制、驗 sha）與 Session（自寫 loop 登記 checkpoint 與 note）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: 端到端（train run → status → ingest → measure → resume → upload）、真資料、README 與 CLAUDE.md（spec §10 文件、§11、§12）

**Files:**
- Create: `tests/unit/test_e2e_train.py`
- Create: `tests/integration/test_rsna_knee_train.py`
- Modify: `README.md`（在 `## 融合層命令 \`vcp fuse\`` 一節之後、`## 匯入器與 \`rows_read\` 的語意` 之前插入一節）
- Modify: `CLAUDE.md`（路徑一節加一條、常用命令加一行）

**Interfaces:**
- Consumes: 全部前七個任務；`vcp.cli.app`；helpers `det_samples`、`make_card`、`write_images`、`perfect_predictions`；`vcp.measure.predictions.write_predictions`；`tests/integration/conftest.load_real` / `real_roots`。
- Produces: 無新介面；驗收條件 spec §12 全部落地。

- [ ] **Step 1: 寫端到端與真資料測試**

`tests/unit/test_e2e_train.py`：

```python
"""The whole training layer through the CLI alone (spec 12): export yolo -> train run (fake
command, checkpoints, final, local upload) -> status -> predictions -> eval ingest (no
--trained-on: the card already knows) -> eval measure -> train run --resume -> train upload to a
second destination -> status. Every step is asserted on its VERDICT line."""

import json
import sys

from typer.testing import CliRunner

from helpers import det_samples, make_card, perfect_predictions, write_images
from vcp.cli import app
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.predictions import write_predictions
from vcp.measure.runs import load_run

runner = CliRunner()

FAKE = '''
import os, sys
from pathlib import Path
print("training with seed", os.environ.get("VCP_SEED"), "run", os.environ.get("VCP_RUN_ID"))
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best-" + os.environ.get("VCP_SEED", "").encode())
Path("weights/last.pt").write_bytes(b"last")
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
'''


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _json(r) -> dict:
    return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))


def test_training_flow(roots, tmp_path):
    paths = DatasetPaths.resolve("flow", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(60, seed=5)
    write_images(roots.data / "raw" / "flow", samples)
    ds = Dataset.from_parts(make_card("det", name="flow", image_root="raw/flow"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=3)
    save_plan(plan, paths)
    export = tmp_path / "yolo-train"
    r = runner.invoke(app, ["data", "export", "--name", "flow", "--plan", "fixed-v1", "--subset", "train", "--format", "yolo", "--out", str(export)])
    assert r.exit_code == 0, r.output
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_text(FAKE, encoding="utf-8")
    vault = tmp_path / "vault"

    r = runner.invoke(app, [
        "train", "run", "--run", "m1", "--dataset", "flow", "--plan", "fixed-v1", "--export", str(export),
        "--seed", "7", "--framework", "fake 1.0", "--cwd", str(work),
        "--checkpoints", "weights/*.pt", "--final", "weights/best.pt", "--upload", str(vault),
        "--", sys.executable, "fake_train.py", "0",
    ])
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "status=WARN" in v and "venv=inherited" in v and "seed=7" in v
    assert "exit_code=0" in v and "checkpoints=2" in v and "uploaded=2" in v and "verified=2" in v
    assert "training with seed 7 run m1" in r.output
    card = load_run(roots.data, "m1")
    assert card.trained_on == ["train"] and card.source.framework == "fake 1.0"
    assert card.source.export_manifest_sha == sha256_file(export / "manifest.json")
    assert card.source.weights_hash == sha256_file(work / "weights" / "best.pt")
    assert sha256_file(vault / "m1" / "best.pt") == card.source.weights_hash

    r = runner.invoke(app, ["train", "status", "--run", "m1"])
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output) and "unbacked=0" in _verdict(r.output)

    for subset in ("valA", "valB"):
        src = tmp_path / f"pred-{subset}.jsonl"
        write_predictions(src, perfect_predictions(ds.subset(subset, plan), ds.card))
        r = runner.invoke(app, ["eval", "ingest", "--run", "m1", "--dataset", "flow", "--plan", "fixed-v1", "--subset", subset, "--format", "jsonl", "--src", str(src)])
        assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["eval", "measure", "--run", "m1", "--metrics", "coco_map"])
    assert r.exit_code == 0, r.output
    assert "readings=2" in _verdict(r.output)
    assert load_run(roots.data, "m1").source.framework == "fake 1.0"  # ingest kept the card's facts

    r = runner.invoke(app, [
        "train", "run", "--run", "m1", "--dataset", "flow", "--plan", "fixed-v1", "--export", str(export),
        "--seed", "7", "--framework", "fake 1.0", "--cwd", str(work), "--checkpoints", "weights/*.pt",
        "--final", "weights/best.pt", "--resume", "--json", "--", sys.executable, "fake_train.py", "0",
    ])
    assert r.exit_code == 0, r.output
    doc = _json(r)
    assert doc["fields"]["attempt"] == 2 and len(doc["result"]["attempts"]) == 2
    assert doc["fields"]["checkpoints"] == 2  # same bytes as attempt 1: nothing new registered

    r = runner.invoke(app, ["train", "upload", "--run", "m1", "--dest", str(tmp_path / "vault2")])
    assert r.exit_code == 0 and "uploaded=2" in _verdict(r.output) and "verified=2" in _verdict(r.output)
    r = runner.invoke(app, ["train", "upload", "--run", "m1", "--dest", str(tmp_path / "vault2")])
    assert r.exit_code == 0 and "skipped=2" in _verdict(r.output)
    r = runner.invoke(app, ["train", "status", "--run", "m1", "--json"])
    assert r.exit_code == 0
    assert len(_json(r)["result"]["uploads"]) == 4  # two destinations x two files

    r = runner.invoke(app, [
        "train", "run", "--run", "m2", "--dataset", "flow", "--plan", "fixed-v1", "--trained-on", "train",
        "--cwd", str(work), "--checkpoints", "weights/*.pt", "--final", "weights/best.pt", "--upload", str(vault),
        "--", sys.executable, "fake_train.py", "1",
    ])
    assert r.exit_code == 1, r.output
    v = _verdict(r.output)
    assert "status=FAIL" in v and "exit_code=1" in v and "checkpoints=2" in v and "uploaded=0" in v
    assert not (vault / "m2").exists()
```

`tests/integration/test_rsna_knee_train.py`：

```python
"""MaterializedReader on three RSNA Knee studies materialized into a throwaway root: every array
matches its manifest row, labels come through for gold samples, nothing under the real data
root is written."""

from __future__ import annotations

import pytest

from conftest import load_real
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.train import MaterializedReader

pytestmark = pytest.mark.realdata
NAME = "rsna-knee"


@pytest.fixture(scope="module")
def knee(real_roots):
    return load_real(NAME, real_roots)


def test_reader_matches_manifest_on_three_studies(knee, real_roots, tmp_path):
    real_paths = DatasetPaths.resolve(NAME, data_root=real_roots.data, configs_root=real_roots.configs)
    image_root = real_paths.resolve_image_root(knee.card)
    data, configs = tmp_path / "data", tmp_path / "configs"
    paths = DatasetPaths.resolve("knee3", data_root=data, configs_root=configs)
    three = knee.samples[:3]
    card = knee.card.model_copy(update={"name": "knee3", "image_root": str(image_root)})
    Dataset.from_parts(card, three).save(paths)
    res = materialize(MaterializeSpec(name="knee3", mode="png", resize=256, data_root=data, configs_root=configs))
    assert res.failed == 0
    reader = MaterializedReader("knee3", "png-r256", data_root=data, configs_root=configs, verify=True)
    assert reader.ids == [s.sample_id for s in three]
    for rec in reader:
        rows = {(str(r.view) if r.view is not None else r.seq_id): r for r in reader.rows(rec.sample_id)}
        assert set(rec.arrays) == set(rows)
        for key, arr in rec.arrays.items():
            assert list(arr.shape) == rows[key].shape and max(arr.shape[:2]) == 256
        assert (rec.labels is not None) == (rec.sample.label_source == "gold")
```

- [ ] **Step 2: 跑端到端**

Run: `uv run pytest tests/unit/test_e2e_train.py -o addopts="" -q` 與 `uv run pytest tests/integration/test_rsna_knee_train.py -o addopts="" -q -m realdata`
Expected: 前者 PASS（約 30 秒：三次假訓練各跑一次環境探針）；後者在有 RSNA 資料時 PASS，否則 SKIPPED。

- [ ] **Step 3: 文件**

`README.md`：在 `wbf`（boxes）… 那段之後、`## 匯入器與 \`rows_read\` 的語意` 之前插入：

````markdown
## 訓練層命令 `vcp train`

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp train run` | 包在任何訓練命令外面：開始就寫 `run.yaml`（`trained_on` 由 export manifest 推導）、複製 config、環境快照、console 落檔、結束後登記 checkpoint 的 sha、上傳並驗證 | `--run`、`--dataset`、`--plan`、`--export DIR`（可重複）或 `--trained-on a,b`、`--venv DIR`、`--config`、`--seed`、`--framework`、`--cwd`、`--checkpoints GLOB`（可重複）、`--final GLOB`、`--upload DEST`（可重複）、`--resume`、`--notes`；`--` 之後是訓練命令 |
| `vcp train upload` | 事後或換目的地上傳已登記的 checkpoint，冪等 | `--run`、`--dest`、`--only final` |
| `vcp train status` | attempts / checkpoints / 副本（唯讀） | `--run`、`--verify`（重算 sha） |

共用選項：`--json`、`--data-root`、`--configs-root`。`--upload` 的目的地：`remote:path` 走 rclone（`copyto --checksum` + `hashsum sha256` 逐檔比對；rclone 要自己裝），其餘是本機 / 掛載目錄（複製後讀回驗 sha）。訓練命令 exit ≠ 0 → `status=FAIL exit_code=N`，checkpoint 仍登記但不上傳；沒給 `--seed`、`--venv`、`--final` 各 WARN 一項。`run.yaml` 就是量測層的 run：之後 `vcp eval ingest --run R ...` 直接接上，不必再給 `--trained-on` / `--framework`。

### 一次訓練到量測

```bash
uv run vcp data export --name D --plan fixed-v1 --subset train --format yolo --out exports/D-train
uv run vcp train run --run y12x_r2 --dataset D --plan fixed-v1 --export exports/D-train \
  --venv C:/venvs/ultra --seed 42 --framework "ultralytics 8.3.0" --cwd projects/D \
  --checkpoints "runs-ultra/y12x_r2/weights/*.pt" --final "runs-ultra/y12x_r2/weights/best.pt" \
  --upload gdrive:vcp/weights -- yolo train model=yolo12x.pt data=exports/D-train/data.yaml epochs=60
uv run vcp train status --run y12x_r2                    # unbacked=0 才算有副本
uv run vcp eval ingest --run y12x_r2 --dataset D --plan fixed-v1 --subset valA --format yolo_txt --src ... --export-manifest exports/D-valA
uv run vcp eval measure --run y12x_r2
```

自寫 PyTorch loop 只需要兩個名字：

```python
from vcp.train import MaterializedReader, Session
reader = MaterializedReader("rsna-knee", "png-r256", plan_id="fixed-v1", subset="train")
class Knee(torch.utils.data.Dataset):
    def __len__(self): return len(reader)
    def __getitem__(self, i):
        rec = reader[reader.ids[i]]                       # rec.arrays: {"0": HxW(xC)} 或 {seq_id: SxHxW}
        x = torch.from_numpy(next(iter(rec.arrays.values())))
        y = torch.tensor([rec.labels.targets[n] for n in NAMES])
        return x, y
s = Session.current()                                     # 在 vcp train run 底下才有
s.register_checkpoint("ckpt/best.pt", final=True); s.note("val_auc", 0.91)
```
````

`CLAUDE.md`：
- 「路徑」一節，在 `measure/<name>/ 只增不改…` 那條之後加一條：「- `runs/<id>/train.yaml` 是訓練紀錄的快照（每次事件整份重寫）、`train.log.jsonl` 只增；`train/` 放 console、config 副本、環境快照。checkpoint 不搬動，只記路徑與 sha；`--upload` 的副本另記 sha 與驗證結果。`vcp train run` 開始就寫 `run.yaml`，之後 `eval ingest` 直接接上。」
- 「常用命令」一節，在 `vcp fuse` 那行之後加：「- `uv run vcp train run --run R --dataset D --plan P --export DIR --venv ENV --seed N --checkpoints "…" --final "…" [--upload DEST] -- <訓練命令>` / `uv run vcp train status --run R`（唯讀）/ `uv run vcp train upload --run R --dest DEST`（冪等）」

- [ ] **Step 4: 全套檢查**

Run:
```bash
uv run pytest -o addopts="" -q
uv run pytest --cov=vcp -o addopts="" -q 2>&1 | tail -n 5
uv run ruff check . && uv run ruff format --check .
uv run pytest -o addopts="" -q -W error::DeprecationWarning -W error::RuntimeWarning tests/unit/train tests/unit/test_cli_train.py tests/unit/test_e2e_train.py
```
Expected: 全綠；覆蓋率 ≥ 80%（Plan 4 結束時 96.64%，本層應維持 ≥ 90%）；ruff 乾淨；本層測試在兩類警告視為錯誤下無警告（`ResourceWarning` 不列入：子程序的管道由 `Popen` 關閉，Plan 2c 遺留的 PIL 警告已知）。

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_e2e_train.py tests/integration/test_rsna_knee_train.py README.md CLAUDE.md
git commit -m "test(train): 端到端訓練到量測流程、RSNA 真資料讀取器；README 與 CLAUDE.md 加 vcp train

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Spec 覆蓋對照

| Spec 章節 | 任務 |
|---|---|
| §4.1 run.yaml 欄位 | 5 |
| §4.2 train.yaml、§4.3 事件、§4.4 快照模型 | 1（模型）、2（快照內容）、5（寫入） |
| §5 目錄佈局 | 1、5 |
| §6 CLI 總表、VERDICT、`--json` | 5、6 |
| §6.1 run 的順序（含全有或全無、中斷） | 5 |
| §6.2 checkpoint 登記 | 3、5、7（session） |
| §6.3 上傳與驗證、冪等、`--only final` | 4、6 |
| §7 環境快照 | 2 |
| §8.1 MaterializedReader、§8.2 Session | 7 |
| §9 錯誤字彙 | 各任務的訊息前綴與 `fields` |
| §10 介面（`assert_plan_matches`、文件） | 5、8 |
| §11 測試策略、§12 驗收 | 各任務 + 8 |

## 自審紀錄

- 佔位掃描：無佔位字樣；每個程式碼步驟都附完整程式碼。
- 型別一致性：`register(record, files, *, data_root, attempt, source)` 在 Task 3 定義、Task 5 與 7 依此呼叫；`resolve_final(record, pattern, *, cwd, data_root)`；`upload(record, dest, *, data_root, only_final, runner)` 回 `UploadOutcome(records, uploaded, skipped)`——Task 5 的 `_upload_all` 與 Task 6 的 `upload_run` 都用這三個欄位；`merge_uploads(record, new)`；`snapshot(python, cwd)`；`Attempt.console` 為 run 目錄相對路徑，`execute()` 收絕對路徑（`run_root / attempt.console`）。
- 已知取捨：`execute()` 對 Ctrl+C 的處理在 `--json` 模式下仍會把中斷後的 VERDICT 印到 stderr（同其他命令）；`Session` 與包裝器對 `train.yaml` 的寫入在時間上不重疊（spec 8.2），但若使用者在訓練命令之外同時跑 `vcp train upload`，兩個寫者會互相覆蓋——留待辦（與 Plan 3 的跨程序 append 同類）。
