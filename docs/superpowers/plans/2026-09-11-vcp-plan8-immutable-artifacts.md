# vcp 不可變產物層實作計畫（稽核 Wave 1a，Plan 8）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 給 vcp 與比賽程式一個「寫一次、可驗證、可接替、不可覆寫」的正式產物容器（`<data_root>/artifacts/<kind>/<id>/` + `manifest.json` 當 commit 標記），並把 vcp 自己四個寫一次的正式檔改經同一個原子原語；release 為 `0.4.0`。

**Architecture:** 新 `src/vcp/core/atomic.py`（`write_once` 三個函式）；新套件 `src/vcp/artifact/`（`schema` 資料模型、`ledger` 只增索引、`store` 載入 / 重用 / verify / relink、`writer` 寫入協定、`lineage` 供給鏈、`clean` 掃描與清理）；新 `src/vcp/cli_artifact.py`（`vcp artifact` 七個命令）。`check_relative_path` 從備份層搬到 `core/paths.py`；`save_plan` / `create_prereg` / `save_recipe` / `backup.write_manifest` 改經 `write_once_text`。

**Tech Stack:** Python 3.12、pydantic v2、typer 0.27、標準庫 `os` / `json` / `shutil` / `re`、numpy（只在端到端測試）、pytest、ruff（line-length 100）。

**Spec:** `docs/superpowers/specs/2026-09-11-vcp-immutable-artifacts-design.md`（來源稽核：`docs/audits/2026-09-11-vcp-improvement-audit.md` VCP-005 / VCP-007）

## Global Constraints

- 取時只能用 `vcp.core.time.utc_now()` / `stamp()` / `parse_stamp()`（ruff TID251）；`datetime.fromtimestamp(st_mtime, tz=UTC)` 讀檔案 mtime 不在禁令內。
- 每個 CLI 命令以 `VERDICT cmd=artifact.<name> status=OK|WARN|FAIL|ABORT …` 收尾，exit 0 / 0 / 1 / 2；`--json` 時結果 JSON 到 stdout、VERDICT 到 stderr；永不互動提問；不用 Click 層的參數驗證——所有檢查在函式層，才有 VERDICT；`run_command(context=)` 帶 `kind=` / `id=`。
- 錯誤字彙：`exists:`、`not_found:`、`partial:`、`unsafe_path:`、`reserved_name:`、`closed:` 是 `ValidationFailed`；`mismatch:`、`drift:`、`spec_mismatch:` 是 `IntegrityError`；沒有 ABORT 類。`VcpError.fields` 只放機器可讀鍵（`kind`、`id`、`file`、`input`、`supersedes`、`differs`）。pydantic 的 `ValidationError` 包成 `ValidationFailed(str(e), location=…)`。
- 唯一判定：**有 `manifest.json` 才是產物**。writer 沒有任何刪除方法；`clean` 沒有程式路徑能刪到有 `manifest.json` 的目錄、台帳或沒有 `spec.json` 的目錄。
- 位置只由 spec 決定：`artifact_dir(data_root, kind, id)`；`kind` / `id` 走 `validate_name`。
- 隱私：manifest / `spec.json` / 台帳只含路徑、sha、大小、時戳、build string、結構化參數；`failure.json` 的訊息經 `vcp.core.proc.redact`；不做憑證偵測。
- pydantic 模型 `extra="forbid"`；檔案 utf-8、LF（測試重寫台帳 / 卡一律 `newline="\n"`）；`src/vcp` 不出現比賽名；覆蓋率 ≥ 80%；`uv run ruff check .` 與 `uv run ruff format --check .` 乾淨；測試永不碰真資料根（`roots` fixture；真資料整合測試只讀）。
- 一件事一個分支一個 commit（`type(scope): 說明`），不用 `git add -A`；commit 訊息結尾加一行 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`（用第二個 `-m`）。
- **不要對 markdown 跑 `ruff format`**。

## 計畫層決定（spec 未明說之處；Task 12 寫進 spec §16）

1. **`reuse` 的關鍵字叫 `check_files`**（spec 寫 `verify=`，會遮蔽同模組的 `verify` 函式）。
2. **`lineage` 回 `Lineage(chain, successors, heads, forks)`**，不是 `list[ArtifactManifest]`：`chain` 根→id，`successors` 是 id 的前向閉包（BFS），`heads` 是前向閉包裡沒被接替的末端（id 自己沒被接替就是 `[id]`），`forks` 數 chain + successors 裡有 >1 接替者的節點。
3. **`VerifyResult` 的 `mismatch` / `missing` / `extra` 是檔名清單**，`unlinked: bool`；CLI 印個數。台帳列的 `manifest_sha256` 與現在的 `manifest.json` 不符 → `mismatch` 裡多一個 `"manifest.json"`。
4. **`clean` 的 `.tmp` 也套 `--older-than`**（以 mtime），否則會刪到正在跑的 job 的暫存；讀不到 `spec.json` 的半途目錄永不列入候選（讀不懂的不刪）；`--apply` 移除每個目錄前再查一次 `manifest.json`。
5. **台帳 jsonl 讀寫沿用 `vcp.measure.ledger.append_row` / `read_rows`**（備份層 `BackupLedger` 的先例）；`ledger.row_for(manifest, manifest_sha256)` 是 writer 與 `relink` 共用的列建構。
6. **`load_manifest` 多驗一項**：manifest 裡的 `spec.kind` / `spec.id` 必須等於目錄的 kind / id（複製來的 manifest → `IntegrityError mismatch:`）。
7. **保留名只在產物根目錄**（`sub/manifest.json` 可寫）；`.<name>.<8 hex>.tmp` 形式的檔名也是保留名（`reserved_name:`），免得 `clean` 刪到它。
8. **`ArtifactManifest.files` 必須已依 name 排序**（validator 擋）：manifest 的位元組因此只有一種形狀。
9. **`--file` 解析**：第一個 `=` 之前是名字，沒有 `=` 就用 basename；路徑本身含 `=` 時用 `NAME=PATH`。`--input name=PATH` 的路徑相對 CWD 解析成絕對路徑後才交給 `resolve_inputs`（它再以 `store_path` 相對化）。
10. **`create` 在搶 id 之前先確認每個 `--file` 存在**，否則會留下一個半途目錄。
11. **`status` 的 VERDICT 欄位是各 kind 的加總**（`kinds=` `complete=` `partial=` `unlinked=` `forks=` `foreign=`）；每 kind 明細在 human 行與 `--json`。
12. **`write_once` 不建立跨程序互斥**（存在檢查與 `os.replace` 是兩步）；Windows 不 fsync 目錄。
13. **`InputRef.name` 走 `validate_name` 規則**；schema 的 validator 一律拋 `ValueError`（pydantic 慣例），writer 把 `check_file_name` 的訊息原樣包成 `ValidationFailed`（訊息已帶 `unsafe_path:` / `reserved_name:` 前綴）。
14. **四個既有寫一次點保留各自的存在預檢**（訊息、例外類型、`fields` 不變），只把位元組落地換成 `write_once_text`；證明 = 行為測試（第二次寫 → 拒絕且原檔位元組不變）+ 結構測試（四個原始碼檔都 import 並呼叫 `write_once_text`）。
15. **真資料整合測試只呼叫 `scan()`**（不經 CLI，CLI 的 logger 會往 `<data_root>/logs/` 寫），並斷言 `artifacts/` 樹在前後完全相同。
16. **`show` 的 `superseded_by=`** 只列直接接替者（`successors` 裡 `supersedes == id` 的）；完整前向閉包看 `lineage`。

## 檔案結構

| 檔案 | 責任 |
|---|---|
| `src/vcp/core/atomic.py` | `write_once_stream` / `write_once` / `write_once_text` / `is_tmp_name` |
| `src/vcp/core/paths.py` | 新增 `check_relative_path`（從備份層搬來）、`artifacts_root`、`artifact_dir` |
| `src/vcp/core/config.py` | 新增 `dump_yaml_text`；`dump_yaml_model` 改用它 |
| `src/vcp/backup/schema.py` | 改從 `vcp.core.paths` import `check_relative_path` |
| `src/vcp/data/split.py`、`src/vcp/measure/prereg.py`、`src/vcp/fuse/recipes.py`、`src/vcp/backup/manifest.py` | 四個寫一次點改經 `write_once_text` |
| `src/vcp/artifact/__init__.py` | 套件 docstring |
| `src/vcp/artifact/schema.py` | `RESERVED_NAMES`、`check_file_name`、`InputRef`、`ArtifactSpec`、`FileEntry`、`ArtifactManifest`、`SpecRecord`、`FailureRecord`、`SupersessionRow` |
| `src/vcp/artifact/ledger.py` | `supersession_log`、`append_supersession`、`read_supersession`、`supersession_of`、`row_for` |
| `src/vcp/artifact/store.py` | `MANIFEST` / `SPEC` / `FAILURE`、`manifest_path`、`is_partial`、`load_manifest`、`resolve_inputs`、`spec_diff`、`VerifyResult`、`verify`、`reuse`、`relink` |
| `src/vcp/artifact/writer.py` | `ArtifactWriter`（`create` / `write_json` / `write_text` / `write_bytes` / `add_file` / `reserve` / `commit`；context manager） |
| `src/vcp/artifact/lineage.py` | `list_manifests`、`successors_of`、`Lineage`、`lineage`、`head` |
| `src/vcp/artifact/clean.py` | `parse_age`、`PartialInfo`、`KindStatus`、`scan`、`CleanResult`、`clean` |
| `src/vcp/cli_artifact.py` | `artifact_app`：create / show / verify / lineage / status / relink / clean |
| `src/vcp/cli.py` | `app.add_typer(artifact_app, name="artifact")` |
| `tests/unit/core/test_atomic.py`、`tests/unit/core/test_paths.py`、`tests/unit/core/test_config.py`、`tests/unit/artifact/test_*.py`、`tests/unit/test_cli_artifact.py`、`tests/unit/test_e2e_artifact.py`、`tests/unit/test_regression_gate.py`、`tests/integration/test_artifact_status.py` | 測試 |
| `README.md`、`CLAUDE.md`、`AGENTS.md`、`docs/handover/HANDOVER.md`、`CHANGELOG.md`、`src/vcp/__init__.py`、spec §16 | 文件與發版 |

## 給實作者的共用約定

- 測試用 `roots` fixture（`tests/conftest.py`：`roots.data` / `roots.configs` 兩個空目錄，環境變數已指過去）；CLI 測試用 `typer.testing.CliRunner` 對 `vcp.cli.app`，VERDICT 從 `r.output` 取最後一行 `VERDICT `；`--json` 時結果 JSON 是 stdout 第一行以 `{` 開頭的行。
- 寫完程式碼先 `uv run ruff format <檔案>` 再跑測試（只對 `.py`）；pytest 用 `uv run pytest <路徑> -o addopts="" -q`；每個任務結尾 `uv run ruff check . && uv run ruff format --check .` 乾淨才 commit。
- `ruff` 的 isort 把 `helpers` / `conftest` / `backup_fixtures` 當第一方，接受它排出來的順序。
- 本計畫的程式碼片段是要照抄的實作，不是示意；型別、函式名、`reason=` 字彙以片段為準。

---

### Task 1: `vcp/core/atomic.py`——寫一次的原子原語

**Files:**
- Create: `src/vcp/core/atomic.py`
- Test: `tests/unit/core/test_atomic.py`

**Interfaces:**
- Consumes: `vcp.core.errors.ValidationFailed`。
- Produces: `write_once_stream(path: Path, chunks: Iterable[bytes]) -> tuple[int, str]`（bytes, sha256）、`write_once(path: Path, data: bytes) -> str`（sha256）、`write_once_text(path: Path, text: str) -> str`、`is_tmp_name(name: str) -> bool`。目標已存在 → `ValidationFailed("exists: …", location=str(path))`；任何失敗都移除暫存、目標不存在。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/core/test_atomic.py`

```python
import hashlib

import pytest

from vcp.core import atomic
from vcp.core.errors import ValidationFailed


def test_write_once_publishes_bytes_and_returns_sha(tmp_path):
    target = tmp_path / "a" / "b.json"
    data = b'{"x": 1}\n'
    assert atomic.write_once(target, data) == hashlib.sha256(data).hexdigest()
    assert target.read_bytes() == data
    assert [p.name for p in target.parent.iterdir()] == ["b.json"]


def test_second_write_is_refused_and_the_original_is_untouched(tmp_path):
    target = tmp_path / "once.txt"
    atomic.write_once_text(target, "first\n")
    with pytest.raises(ValidationFailed, match="^exists: ") as ei:
        atomic.write_once_text(target, "second\n")
    assert ei.value.location == str(target)
    assert target.read_text(encoding="utf-8") == "first\n"
    assert [p.name for p in tmp_path.iterdir()] == ["once.txt"]


def test_stream_returns_size_and_sha_and_leaves_no_temp(tmp_path):
    target = tmp_path / "big.bin"
    size, sha = atomic.write_once_stream(target, iter([b"a" * 10, b"b" * 5]))
    assert size == 15 and sha == hashlib.sha256(b"a" * 10 + b"b" * 5).hexdigest()
    assert target.read_bytes() == b"a" * 10 + b"b" * 5
    assert not [p for p in tmp_path.iterdir() if atomic.is_tmp_name(p.name)]


def test_failed_publish_leaves_no_target_and_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "x.txt"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(atomic.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        atomic.write_once_text(target, "x\n")
    assert not target.exists() and list(tmp_path.iterdir()) == []


def test_failing_chunk_source_leaves_no_temp(tmp_path):
    target = tmp_path / "y.bin"

    def chunks():
        yield b"partial"
        raise RuntimeError("source died")

    with pytest.raises(RuntimeError, match="source died"):
        atomic.write_once_stream(target, chunks())
    assert not target.exists() and list(tmp_path.iterdir()) == []


def test_text_is_utf8_with_the_given_line_ends(tmp_path):
    target = tmp_path / "t.txt"
    atomic.write_once_text(target, "中文\nline2\n")
    assert target.read_bytes() == "中文\nline2\n".encode()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (".manifest.json.0a1b2c3d.tmp", True),
        (".a.b.c.deadbeef.tmp", True),
        ("manifest.json", False),
        (".x.tmp", False),
        (".a.b.tmp", False),
        (".a.0A1B2C3D.tmp", False),
        ("a.0a1b2c3d.tmp", False),
    ],
)
def test_is_tmp_name(name, expected):
    assert atomic.is_tmp_name(name) is expected
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/core/test_atomic.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'vcp.core.atomic'`）

- [ ] **Step 3: 寫 `src/vcp/core/atomic.py`**

```python
"""Write-once files: a same-directory temp file, fsync, then one ``os.replace`` into a name that
must not exist yet. The primitive under every formal file vcp writes exactly once (split plans,
pre-registrations, fuse recipes, backup manifests) and under every file of an immutable artifact.

Not a cross-process lock: the existence check and the ``os.replace`` are two steps. The artifact
layer gets its mutual exclusion from ``os.mkdir`` of the artifact directory; the four write-once
sites accept the window (spec 15)."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path

from vcp.core.errors import ValidationFailed

_TMP = re.compile(r"^\.(?P<name>.+)\.(?P<nonce>[0-9a-f]{8})\.tmp$")


def is_tmp_name(name: str) -> bool:
    """``.<name>.<8 hex>.tmp``: what an interrupted ``write_once`` leaves behind, and the only
    file name ``vcp artifact clean`` may remove on its own."""
    return _TMP.match(name) is not None


def _tmp_path(target: Path) -> Path:
    return target.parent / f".{target.name}.{os.urandom(4).hex()}.tmp"


def _exists_error(target: Path) -> ValidationFailed:
    return ValidationFailed(
        f"exists: {target} is already written; a write-once file is never rewritten",
        location=str(target),
    )


def _fsync_dir(directory: Path) -> None:
    """Persist the rename itself. Windows cannot open a directory for fsync: skipped there."""
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_once_stream(path: Path, chunks: Iterable[bytes]) -> tuple[int, str]:
    """Write ``chunks`` to ``path``, which must not exist. Returns ``(bytes, sha256)``.

    The bytes go to ``.<name>.<nonce>.tmp`` beside the target, are fsynced, and are published by
    one ``os.replace``; any failure removes the temp file and leaves the target absent."""
    target = Path(path)
    if target.exists():
        raise _exists_error(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(target)
    digest = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as f:
            for chunk in chunks:
                f.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            f.flush()
            os.fsync(f.fileno())
        if target.exists():
            raise _exists_error(target)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(target.parent)
    return size, digest.hexdigest()


def write_once(path: Path, data: bytes) -> str:
    """``write_once_stream`` for bytes already in memory. Returns the sha256."""
    return write_once_stream(path, (data,))[1]


def write_once_text(path: Path, text: str) -> str:
    """UTF-8, line ends as given: the caller builds the text with ``\\n``."""
    return write_once(path, text.encode("utf-8"))
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/core/atomic.py tests/unit/core/test_atomic.py && uv run pytest tests/unit/core/test_atomic.py -o addopts="" -q`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/core/atomic.py tests/unit/core/test_atomic.py
git commit -m "feat(core): write_once 原子原語——同目錄暫存、fsync、目標不存在才 replace" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `core/paths.py` 的產物路徑與 `check_relative_path` 搬家、`core/config.py` 的 `dump_yaml_text`

**Files:**
- Modify: `src/vcp/core/paths.py`（`validate_name` 之後）、`src/vcp/backup/schema.py:1-10,95-107`、`src/vcp/core/config.py:41-46`
- Test: `tests/unit/core/test_paths.py`、`tests/unit/core/test_config.py`

**Interfaces:**
- Consumes: 既有 `validate_name`、`store_path` / `resolve_stored_path`。
- Produces: `vcp.core.paths.check_relative_path(path: str) -> None`（拋 `ValueError`，行為與備份層原函式完全相同）、`artifacts_root(data_root: Path) -> Path`、`artifact_dir(data_root: Path, kind: str, artifact_id: str) -> Path`（兩個名字都 `validate_name`）；`vcp.core.config.dump_yaml_text(model: BaseModel) -> str`。`vcp.backup.schema` 不再定義 `check_relative_path`（`FileEntry` 驗證行為不變）。

- [ ] **Step 1: 寫失敗的測試**——追加到 `tests/unit/core/test_paths.py` 檔尾

```python
def test_artifact_paths(tmp_path):
    assert paths.artifacts_root(tmp_path) == tmp_path / "artifacts"
    assert paths.artifact_dir(tmp_path, "receipt", "r1") == tmp_path / "artifacts" / "receipt" / "r1"
    with pytest.raises(ValidationFailed):
        paths.artifact_dir(tmp_path, "../k", "r1")
    with pytest.raises(ValidationFailed):
        paths.artifact_dir(tmp_path, "receipt", "a/b")


@pytest.mark.parametrize("bad", ["", "/abs", "C:/x", "a\\b", "a//b", "./a", "a/../b", "a/"])
def test_check_relative_path_rejects(bad):
    with pytest.raises(ValueError):
        paths.check_relative_path(bad)


@pytest.mark.parametrize("good", ["a", "a/b.txt", "runs/r1/run.yaml", ".hidden/x"])
def test_check_relative_path_accepts(good):
    paths.check_relative_path(good)
```

追加到 `tests/unit/core/test_config.py` 檔尾，並把該檔的 import 行改成 `from vcp.core.config import dump_yaml_model, dump_yaml_text, load_yaml_model`：

```python
def test_dump_yaml_text_is_what_dump_yaml_model_writes(tmp_path):
    m = M(name="中文", n=3, tags=["a"])
    path = tmp_path / "m.yaml"
    dump_yaml_model(m, path)
    assert path.read_text(encoding="utf-8") == dump_yaml_text(m)
    assert dump_yaml_text(m) == "name: 中文\nn: 3\ntags:\n- a\n"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/core/test_paths.py tests/unit/core/test_config.py -o addopts="" -q`
Expected: FAIL（`AttributeError: module 'vcp.core.paths' has no attribute 'artifacts_root'`；`ImportError: cannot import name 'dump_yaml_text'`）

- [ ] **Step 3: 改 `src/vcp/core/paths.py`**——在 `validate_name` 之後、`logs_dir` 之前加

```python
_DRIVE = re.compile(r"^[A-Za-z]:")
_BAD_SEGMENTS = frozenset({"", ".", ".."})


def check_relative_path(path: str) -> None:
    """A path stored inside a card, a manifest or an artifact is a relative posix path under one
    root. Nothing else is accepted: restores and artifact writes land at ``<root>/<path>``, so a
    path that can climb out of the root is a way to write anywhere on the machine. Raises
    ``ValueError`` (the pydantic-validator convention); callers outside a validator wrap it."""
    if not path or "\\" in path or path.startswith("/") or _DRIVE.match(path):
        raise ValueError(f"path must be a relative posix path, got {path!r}")
    if any(segment in _BAD_SEGMENTS for segment in path.split("/")):
        raise ValueError(f"path must have no empty, '.' or '..' segment, got {path!r}")


def artifacts_root(data_root: Path) -> Path:
    return data_root / "artifacts"


def artifact_dir(data_root: Path, kind: str, artifact_id: str) -> Path:
    """``<data_root>/artifacts/<kind>/<id>/``: the only place an artifact can live (spec 5)."""
    validate_name(kind)
    validate_name(artifact_id)
    return artifacts_root(data_root) / kind / artifact_id
```

改 `src/vcp/backup/schema.py`：刪掉 `import re`（第 5 行）與 `_DRIVE = …`、`_BAD_SEGMENTS = …`、整個 `def check_relative_path(...)`（第 96–107 行）；在 `from pydantic import …` 之後加一行 `from vcp.core.paths import check_relative_path`。`FileEntry._shape` 裡的 `check_relative_path(self.path)` 呼叫不動。

改 `src/vcp/core/config.py`：把 `dump_yaml_model` 換成

```python
def dump_yaml_text(model: BaseModel) -> str:
    """The yaml ``dump_yaml_model`` writes, as text: what a write-once site hands to
    ``vcp.core.atomic.write_once_text``."""
    return yaml.safe_dump(model.model_dump(mode="json"), sort_keys=False, allow_unicode=True)


def dump_yaml_model(model: BaseModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(dump_yaml_text(model))
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/core/paths.py src/vcp/backup/schema.py src/vcp/core/config.py tests/unit/core/test_paths.py tests/unit/core/test_config.py && uv run pytest tests/unit/core tests/unit/backup/test_schema.py -o addopts="" -q`
Expected: 全部通過（含備份層既有的 `test_entry_rejects_paths_that_can_leave_the_root`）

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/core/paths.py src/vcp/backup/schema.py src/vcp/core/config.py tests/unit/core/test_paths.py tests/unit/core/test_config.py
git commit -m "refactor(core): artifact_dir 與 check_relative_path 進 core/paths，config 加 dump_yaml_text" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: 四個既有寫一次點改經 `write_once_text`

**Files:**
- Modify: `src/vcp/data/split.py:118-128`、`src/vcp/measure/prereg.py:17,156`、`src/vcp/fuse/recipes.py:12,47-57`、`src/vcp/backup/manifest.py:5-11,20-34`
- Test: `tests/unit/core/test_atomic.py`（結構測試）、`tests/unit/data/test_split_plan.py:113-124`、`tests/unit/measure/test_prereg_judge.py:192-205`、`tests/unit/fuse/test_schema_recipes.py:66-73`、`tests/unit/backup/test_ledger_manifest.py`

**Interfaces:**
- Consumes: Task 1 的 `write_once_text`、Task 2 的 `dump_yaml_text`。
- Produces: 四個函式的簽名、訊息、例外類型、`fields` 與寫出的位元組**完全不變**；只有落地方式變成 tmp → fsync → replace。

- [ ] **Step 1: 寫失敗的測試**

追加到 `tests/unit/core/test_atomic.py` 檔尾（結構測試：四個檔都經同一個原語）：

```python
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src" / "vcp"
WRITE_ONCE_SITES = (
    "data/split.py",
    "measure/prereg.py",
    "fuse/recipes.py",
    "backup/manifest.py",
)


@pytest.mark.parametrize("rel", WRITE_ONCE_SITES)
def test_the_four_write_once_sites_use_the_primitive(rel):
    text = (SRC / rel).read_text(encoding="utf-8")
    assert "from vcp.core.atomic import write_once_text" in text, rel
    assert "write_once_text(" in text, rel
```

（`from pathlib import Path` 併進檔頭的 import 區，ruff isort 會排。）

`tests/unit/data/test_split_plan.py` 的 `test_save_and_load_plan`：把

```python
    with pytest.raises(VcpError, match="already exists"):
        save_plan(plan, paths)
```

改成

```python
    before = target.read_bytes()
    with pytest.raises(VcpError, match="already exists"):
        save_plan(plan, paths)
    assert target.read_bytes() == before
    assert [p.name for p in target.parent.iterdir()] == ["p1.json"]
```

`tests/unit/measure/test_prereg_judge.py` 的 `test_create_prereg_refuses_measured_candidate`：把

```python
    with pytest.raises(ValidationFailed, match="already exists"):
        create_prereg(paths, _pr(), ledger)
```

改成

```python
    before = path.read_bytes()
    with pytest.raises(ValidationFailed, match="already exists"):
        create_prereg(paths, _pr(), ledger)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["p001.yaml"]
```

`tests/unit/fuse/test_schema_recipes.py` 的 `test_save_refuses_existing` 已斷言位元組不變；在最後一行之後加

```python
    assert [p.name for p in fuse_dir(paths).iterdir()] == ["r1.yaml"]
```

`tests/unit/backup/test_ledger_manifest.py` 的 `test_manifest_write_load_and_paths`：把

```python
    with pytest.raises(ValidationFailed, match="exists"):
        write_manifest(paths, m)
```

改成

```python
    before = path.read_bytes()
    with pytest.raises(ValidationFailed, match="exists"):
        write_manifest(paths, m)
    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir()] == [f"{m.manifest_id}.json"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/core/test_atomic.py -o addopts="" -q`
Expected: 4 FAIL（`assert "from vcp.core.atomic import write_once_text" in text`）；其餘四個檔的既有測試此時仍通過（行為不變是重點）。

- [ ] **Step 3: 改四個檔**

`src/vcp/data/split.py`：在 import 區加 `from vcp.core.atomic import write_once_text`；`save_plan` 改成

```python
def save_plan(plan: SplitPlan, paths: DatasetPaths) -> Path:
    target = paths.plan_json(plan.plan_id)
    if target.exists():
        raise VcpError(
            f"plan file already exists: {target}; plans are immutable, choose a new plan-id"
        )
    write_once_text(target, json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=1) + "\n")
    return target
```

`src/vcp/measure/prereg.py`：第 17 行改成 `from vcp.core.config import dump_yaml_text, load_yaml_model`，加 `from vcp.core.atomic import write_once_text`；第 156 行 `dump_yaml_model(pr.model_copy(update={"params": params}), path)` 改成

```python
    write_once_text(path, dump_yaml_text(pr.model_copy(update={"params": params})))
```

（其後的 `try: append_row(...) except Exception: path.unlink(missing_ok=True); raise` 不動。）

`src/vcp/fuse/recipes.py`：第 12 行改成 `from vcp.core.config import dump_yaml_text, load_yaml_model`，加 `from vcp.core.atomic import write_once_text`；`save_recipe` 最後的 `dump_yaml_model(recipe, path)` 改成 `write_once_text(path, dump_yaml_text(recipe))`。

`src/vcp/backup/manifest.py`：加 `from vcp.core.atomic import write_once_text`；`write_manifest` 改成

```python
def write_manifest(paths: DatasetPaths, manifest: Manifest) -> Path:
    path = paths.backup_manifest(manifest.manifest_id)
    if path.exists():
        raise ValidationFailed(
            f"exists: manifest {manifest.manifest_id!r} is already written at {path}",
            fields={"manifest": manifest.manifest_id},
        )
    doc = manifest.model_dump(mode="json", by_alias=True)
    write_once_text(path, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    return path
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/data/split.py src/vcp/measure/prereg.py src/vcp/fuse/recipes.py src/vcp/backup/manifest.py tests/unit/core/test_atomic.py tests/unit/data/test_split_plan.py tests/unit/measure/test_prereg_judge.py tests/unit/fuse/test_schema_recipes.py tests/unit/backup/test_ledger_manifest.py && uv run pytest tests/unit/core/test_atomic.py tests/unit/data/test_split_plan.py tests/unit/measure/test_prereg_judge.py tests/unit/fuse/test_schema_recipes.py tests/unit/backup -o addopts="" -q`
Expected: 全部通過；`uv run ruff check .` 若報 `dump_yaml_model` 未使用（F401），把該 import 拿掉。

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/data/split.py src/vcp/measure/prereg.py src/vcp/fuse/recipes.py src/vcp/backup/manifest.py tests/unit/core/test_atomic.py tests/unit/data/test_split_plan.py tests/unit/measure/test_prereg_judge.py tests/unit/fuse/test_schema_recipes.py tests/unit/backup/test_ledger_manifest.py
git commit -m "refactor: split plan、預登記、融合配方、backup manifest 改經 write_once_text" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `vcp/artifact/schema.py`——資料模型

**Files:**
- Create: `src/vcp/artifact/__init__.py`、`src/vcp/artifact/schema.py`
- Test: `tests/unit/artifact/__init__.py`（空檔）、`tests/unit/artifact/test_schema.py`

**Interfaces:**
- Consumes: Task 1 `is_tmp_name`、Task 2 `check_relative_path`、既有 `validate_name`。
- Produces: `RESERVED_NAMES`、`check_file_name(name) -> None`（`ValueError`，訊息以 `unsafe_path:` / `reserved_name:` 開頭）、`InputRef(name, path=None, sha256=None)`、`ArtifactSpec(kind, id, dataset=None, plan_id=None, seed=None, params={}, inputs=[], id_pattern=None, supersedes=None, supersedes_reason=None, notes="")`、`FileEntry(name, bytes, sha256)`、`ArtifactManifest(schema_version=1, spec, files, created_at, vcp_version, supersedes_sha256=None)`（`.bytes` 屬性、`.file(name)`）、`SpecRecord(spec, opened_at, vcp_version)`、`FailureRecord(ts, exception, message)`、`SupersessionRow(ts, kind, id, manifest_sha256, supersedes_id, supersedes_sha256, reason)`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/artifact/test_schema.py`（先建空的 `tests/unit/artifact/__init__.py`）

```python
import pytest
from pydantic import ValidationError

from vcp.artifact.schema import (
    RESERVED_NAMES,
    ArtifactManifest,
    ArtifactSpec,
    FileEntry,
    InputRef,
    SupersessionRow,
    check_file_name,
)

SHA = "a" * 64
STAMP = "2026-09-11T00:00:00.000Z"


def _spec(**over) -> ArtifactSpec:
    return ArtifactSpec.model_validate({"kind": "receipt", "id": "six-slot-v2-s42", "seed": 42, **over})


def _entry(name: str) -> FileEntry:
    return FileEntry(name=name, bytes=1, sha256=SHA)


def test_spec_defaults_and_names():
    s = _spec()
    assert s.params == {} and s.inputs == [] and s.notes == "" and s.supersedes is None
    with pytest.raises(ValidationError, match="invalid name"):
        _spec(kind="../k")
    with pytest.raises(ValidationError, match="invalid name"):
        _spec(id="a b")
    with pytest.raises(ValidationError, match="Extra inputs"):
        _spec(extra=1)


def test_id_pattern_groups_must_equal_the_fields():
    _spec(id_pattern=r"six-slot-v2-s(?P<seed>\d+)")
    with pytest.raises(ValidationError, match="reads '42' from the id but the spec says '43'"):
        _spec(seed=43, id_pattern=r"six-slot-v2-s(?P<seed>\d+)")
    with pytest.raises(ValidationError, match="does not match id_pattern"):
        _spec(id_pattern=r"other-(?P<seed>\d+)")
    with pytest.raises(ValidationError, match="names no spec field"):
        _spec(id_pattern=r"six-slot-(?P<version>v\d)-s42")
    with pytest.raises(ValidationError, match="names no spec field"):
        _spec(seed=None, id_pattern=r"six-slot-v2-s(?P<seed>\d+)")
    with pytest.raises(ValidationError, match="does not compile"):
        _spec(id_pattern=r"(?P<seed>")
    ok = _spec(
        id="knee-fixed-v1-s42",
        dataset="knee",
        plan_id="fixed-v1",
        params={"fold": "3"},
        id_pattern=r"(?P<dataset>[a-z]+)-(?P<plan_id>[a-z0-9-]+)-s(?P<seed>\d+)",
    )
    assert ok.dataset == "knee"
    with pytest.raises(ValidationError, match="group 'fold'"):
        _spec(id="f3-s42", params={"fold": "4"}, id_pattern=r"f(?P<fold>\d)-s(?P<seed>\d+)")
    _spec(id="f3-s42", params={"fold": "3"}, id_pattern=r"f(?P<fold>\d)-s(?P<seed>\d+)")
    # a pattern without named groups still checks the shape of the id
    _spec(id_pattern=r"six-slot-v2-s\d+")


def test_inputs_need_path_or_sha_and_unique_names():
    with pytest.raises(ValidationError, match="needs a path or a sha256"):
        InputRef(name="plan")
    with pytest.raises(ValidationError, match="64 hex"):
        InputRef(name="plan", sha256="abc")
    with pytest.raises(ValidationError, match="invalid name"):
        InputRef(name="a/b", sha256=SHA)
    with pytest.raises(ValidationError, match="must not be empty"):
        InputRef(name="plan", path="")
    assert InputRef(name="plan", path="configs/x.json").sha256 is None
    with pytest.raises(ValidationError, match="duplicate input names"):
        _spec(inputs=[{"name": "p", "sha256": SHA}, {"name": "p", "sha256": SHA}])


def test_supersedes_and_reason_go_together():
    with pytest.raises(ValidationError, match="go together"):
        _spec(supersedes="old")
    with pytest.raises(ValidationError, match="go together"):
        _spec(supersedes_reason="why")
    with pytest.raises(ValidationError, match="cannot supersede itself"):
        _spec(supersedes="six-slot-v2-s42", supersedes_reason="why")
    with pytest.raises(ValidationError, match="must not be empty"):
        _spec(supersedes="old", supersedes_reason="")
    with pytest.raises(ValidationError, match="invalid name"):
        _spec(supersedes="../old", supersedes_reason="why")
    assert _spec(supersedes="old", supersedes_reason="seed mismatch").supersedes == "old"


@pytest.mark.parametrize("bad", ["", "/abs", "C:/x", "a\\b", "../x", "a/../b"])
def test_file_names_cannot_leave_the_artifact(bad):
    with pytest.raises(ValueError, match="^unsafe_path: "):
        check_file_name(bad)
    with pytest.raises(ValidationError, match="unsafe_path"):
        _entry(bad)


@pytest.mark.parametrize(
    "bad", [*sorted(RESERVED_NAMES), ".weights.pt.0a1b2c3d.tmp", "sub/.x.00000000.tmp"]
)
def test_reserved_file_names(bad):
    with pytest.raises(ValueError, match="^reserved_name: "):
        check_file_name(bad)


def test_file_names_accepted_and_entry_shape():
    for name in ("receipt.json", "features/train.npy", "sub/manifest.json", ".hidden"):
        check_file_name(name)
    assert _entry("receipt.json").bytes == 1
    with pytest.raises(ValidationError, match="64 hex"):
        FileEntry(name="x", bytes=1, sha256="zz")
    with pytest.raises(ValidationError):
        FileEntry(name="x", bytes=-1, sha256=SHA)


def _manifest(**over) -> ArtifactManifest:
    base = {
        "spec": _spec().model_dump(),
        "files": [_entry("a.json").model_dump(), _entry("b.npy").model_dump()],
        "created_at": STAMP,
        "vcp_version": "0.4.0",
    }
    return ArtifactManifest.model_validate({**base, **over})


def test_manifest_shape():
    m = _manifest()
    assert m.schema_version == 1 and m.bytes == 2 and m.file("a.json") is not None
    assert m.file("nope") is None and m.supersedes_sha256 is None
    with pytest.raises(ValidationError, match="schema_version"):
        _manifest(schema_version=2)
    with pytest.raises(ValidationError, match="duplicate file names"):
        _manifest(files=[_entry("a").model_dump(), _entry("a").model_dump()])
    with pytest.raises(ValidationError, match="sorted by name"):
        _manifest(files=[_entry("b").model_dump(), _entry("a").model_dump()])
    with pytest.raises(ValidationError, match="never resolved"):
        _manifest(spec=_spec(inputs=[{"name": "p", "path": "x"}]).model_dump())
    with pytest.raises(ValidationError, match="recorded exactly when"):
        _manifest(supersedes_sha256=SHA)
    with pytest.raises(ValidationError, match="recorded exactly when"):
        _manifest(spec=_spec(supersedes="old", supersedes_reason="r").model_dump())
    with pytest.raises(ValidationError, match="64 hex"):
        _manifest(spec=_spec(supersedes="old", supersedes_reason="r").model_dump(), supersedes_sha256="x")
    ok = _manifest(spec=_spec(supersedes="old", supersedes_reason="r").model_dump(), supersedes_sha256=SHA)
    assert ok.supersedes_sha256 == SHA
    assert ArtifactManifest.model_validate_json(ok.model_dump_json()) == ok


def test_supersession_row_round_trip():
    row = SupersessionRow(
        ts=STAMP,
        kind="receipt",
        id="new",
        manifest_sha256=SHA,
        supersedes_id="old",
        supersedes_sha256="b" * 64,
        reason="seed mismatch",
    )
    assert SupersessionRow.model_validate_json(row.model_dump_json()) == row
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/artifact/test_schema.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'vcp.artifact'`）

- [ ] **Step 3: 寫 `src/vcp/artifact/__init__.py`**

```python
"""Immutable artifacts (spec 2026-09-11): a directory claimed by ``os.mkdir``, files written once,
``manifest.json`` written last as the commit mark. No manifest, no artifact."""
```

**寫 `src/vcp/artifact/schema.py`**

```python
"""Pydantic models of the immutable artifact layer (spec 4): what a job declares before it writes
(``ArtifactSpec``), what a committed directory carries (``ArtifactManifest``), the two records of
an open / failed directory, and the append-only supersession row.

Every validator raises ``ValueError`` (pydantic wraps it); the writer and the store turn the same
messages into ``ValidationFailed`` with the same ``reason=`` prefixes."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vcp.core.atomic import is_tmp_name
from vcp.core.errors import ValidationFailed
from vcp.core.paths import check_relative_path, validate_name

SCHEMA_VERSION = 1
RESERVED_NAMES = frozenset({"manifest.json", "spec.json", "failure.json"})
_SHA = re.compile(r"^[0-9a-f]{64}$")


def _name(value: str) -> str:
    """``validate_name`` inside a validator: same rule, ``ValueError`` instead of
    ``ValidationFailed`` so pydantic reports it as a field error."""
    try:
        validate_name(value)
    except ValidationFailed as e:
        raise ValueError(str(e)) from e
    return value


def _sha(value: str, what: str) -> str:
    if not _SHA.fullmatch(value):
        raise ValueError(f"{what}: sha256 must be 64 hex characters")
    return value


def check_file_name(name: str) -> None:
    """A file name inside an artifact: relative posix under the artifact directory, not one of
    the layer's own files, not a temp name ``clean`` may remove. Raises ``ValueError`` whose
    message starts with the ``reason=`` word (``unsafe_path:`` / ``reserved_name:``)."""
    try:
        check_relative_path(name)
    except ValueError as e:
        raise ValueError(f"unsafe_path: {e}") from e
    if name in RESERVED_NAMES:
        raise ValueError(f"reserved_name: {name!r} is written by the artifact layer itself")
    if is_tmp_name(name.rsplit("/", 1)[-1]):
        raise ValueError(f"reserved_name: {name!r} looks like a temp file `clean` may remove")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputRef(_Strict):
    """Something the job read. ``path`` is posix: relative to the data root when inside it
    (``store_path``), absolute otherwise. At least one of ``path`` / ``sha256``; after
    ``resolve_inputs`` a path's sha and its stored form are both filled."""

    name: str
    path: str | None = None
    sha256: str | None = None

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        return _name(v)

    @model_validator(mode="after")
    def _shape(self) -> InputRef:
        if self.path is None and self.sha256 is None:
            raise ValueError(f"input {self.name!r} needs a path or a sha256")
        if self.path is not None and not self.path:
            raise ValueError(f"input {self.name!r}: path must not be empty")
        if self.sha256 is not None:
            _sha(self.sha256, f"input {self.name!r}")
        return self


class ArtifactSpec(_Strict):
    """What a job declares at open. The location follows from ``kind`` / ``id`` alone; the
    structured fields are what ``reuse`` compares and what ``id_pattern`` must agree with."""

    kind: str
    id: str
    dataset: str | None = None
    plan_id: str | None = None
    seed: int | None = None
    params: dict[str, str] = Field(default_factory=dict)
    inputs: list[InputRef] = Field(default_factory=list)
    id_pattern: str | None = None
    supersedes: str | None = None
    supersedes_reason: str | None = None
    notes: str = ""

    @field_validator("kind", "id")
    @classmethod
    def _names_ok(cls, v: str) -> str:
        return _name(v)

    @model_validator(mode="after")
    def _shape(self) -> ArtifactSpec:
        names = [i.name for i in self.inputs]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"duplicate input names: {dup}")
        if (self.supersedes is None) != (self.supersedes_reason is None):
            raise ValueError("supersedes and supersedes_reason go together")
        if self.supersedes is not None:
            _name(self.supersedes)
            if self.supersedes == self.id:
                raise ValueError(f"artifact {self.id!r} cannot supersede itself")
            if not self.supersedes_reason:
                raise ValueError("supersedes_reason must not be empty")
        if self.id_pattern is not None:
            self._check_pattern(self.id_pattern)
        return self

    def _pattern_value(self, group: str) -> str | None:
        if group == "seed":
            return None if self.seed is None else str(self.seed)
        if group == "dataset":
            return self.dataset
        if group == "plan_id":
            return self.plan_id
        return self.params.get(group)

    def _check_pattern(self, pattern: str) -> None:
        """Every named group of ``id_pattern`` must equal the spec field of the same name
        (VCP-007: an id that says s42 while the spec says seed 43 is refused at open)."""
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"id_pattern {pattern!r} does not compile: {e}") from e
        m = rx.fullmatch(self.id)
        if m is None:
            raise ValueError(f"id {self.id!r} does not match id_pattern {pattern!r}")
        for group, value in m.groupdict().items():
            expected = self._pattern_value(group)
            if expected is None:
                raise ValueError(
                    f"id_pattern group {group!r} names no spec field: give seed / dataset / "
                    f"plan_id or params[{group!r}]"
                )
            if value != expected:
                raise ValueError(
                    f"id_pattern group {group!r} reads {value!r} from the id but the spec says "
                    f"{expected!r}"
                )


class FileEntry(_Strict):
    """One file of a committed artifact: its name inside the directory, size and sha."""

    name: str
    bytes: int = Field(ge=0)
    sha256: str

    @field_validator("name")
    @classmethod
    def _file_name_ok(cls, v: str) -> str:
        check_file_name(v)
        return v

    @field_validator("sha256")
    @classmethod
    def _sha_ok(cls, v: str) -> str:
        return _sha(v, "file")


class ArtifactManifest(_Strict):
    """``manifest.json``: its presence is what makes the directory an artifact (spec 2)."""

    schema_version: int = SCHEMA_VERSION
    spec: ArtifactSpec
    files: list[FileEntry]
    created_at: str
    vcp_version: str
    supersedes_sha256: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> ArtifactManifest:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version {self.schema_version} is not {SCHEMA_VERSION}")
        names = [f.name for f in self.files]
        if len(set(names)) != len(names):
            raise ValueError("duplicate file names")
        if names != sorted(names):
            raise ValueError("files must be sorted by name")
        unresolved = [i.name for i in self.spec.inputs if i.sha256 is None]
        if unresolved:
            raise ValueError(f"inputs without a sha256 (never resolved): {unresolved}")
        if (self.spec.supersedes is None) != (self.supersedes_sha256 is None):
            raise ValueError("supersedes_sha256 is recorded exactly when the spec supersedes")
        if self.supersedes_sha256 is not None:
            _sha(self.supersedes_sha256, "supersedes_sha256")
        return self

    @property
    def bytes(self) -> int:
        return sum(f.bytes for f in self.files)

    def file(self, name: str) -> FileEntry | None:
        return next((f for f in self.files if f.name == name), None)


class SpecRecord(_Strict):
    """``spec.json``, written at open: the claim on the id, before any file."""

    spec: ArtifactSpec
    opened_at: str
    vcp_version: str


class FailureRecord(_Strict):
    """``failure.json``, written when the job left the ``with`` block by an exception. The
    message has been through ``redact``."""

    ts: str
    exception: str
    message: str


class SupersessionRow(_Strict):
    """One line of ``<kind>/supersession.jsonl``: an index over committed manifests, appended
    after the commit, rebuildable from the manifests (``relink``)."""

    ts: str
    kind: str
    id: str
    manifest_sha256: str
    supersedes_id: str
    supersedes_sha256: str
    reason: str
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/artifact tests/unit/artifact && uv run pytest tests/unit/artifact/test_schema.py -o addopts="" -q`
Expected: 全部通過（`test_reserved_file_names` 5 個參數、`test_file_names_cannot_leave_the_artifact` 6 個）

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/artifact/__init__.py src/vcp/artifact/schema.py tests/unit/artifact/__init__.py tests/unit/artifact/test_schema.py
git commit -m "feat(artifact): 資料模型——spec、manifest、id_pattern 對欄位、檔名規則、supersession 列" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `ledger.py` 與 `store.py` 第一部分——台帳、三態載入、input 解析、spec 比對

**Files:**
- Create: `src/vcp/artifact/ledger.py`、`src/vcp/artifact/store.py`
- Test: `tests/unit/artifact/test_store.py`

**Interfaces:**
- Consumes: Task 4 的模型；既有 `vcp.measure.ledger.append_row` / `read_rows`、`vcp.core.hashing.sha256_file`、`vcp.core.paths.artifact_dir` / `store_path` / `resolve_stored_path`。
- Produces: `ledger.SUPERSESSION_LEDGER`、`supersession_log(data_root, kind) -> Path`、`append_supersession(data_root, row)`、`read_supersession(data_root, kind) -> list[SupersessionRow]`、`supersession_of(data_root, kind, artifact_id) -> SupersessionRow | None`；`store.MANIFEST` / `SPEC` / `FAILURE`、`manifest_path(data_root, kind, artifact_id) -> Path`、`is_partial(...) -> bool`、`load_manifest(...) -> ArtifactManifest`（`not_found:` / `partial:` / `bad manifest` / `mismatch:`）、`resolve_inputs(spec, data_root) -> ArtifactSpec`（`not_found:` / `mismatch:`，`fields={"input": name}`）、`spec_diff(recorded, wanted) -> list[str]`。（`verify` / `reuse` / `relink` 在 Task 7–8 加進同一個檔。）

- [ ] **Step 1: 寫失敗的測試** `tests/unit/artifact/test_store.py`

```python
import json

import pytest

from vcp.artifact import store
from vcp.artifact.ledger import (
    SUPERSESSION_LEDGER,
    append_supersession,
    read_supersession,
    supersession_log,
    supersession_of,
)
from vcp.artifact.schema import ArtifactManifest, ArtifactSpec, FileEntry, InputRef, SupersessionRow
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir

SHA = "a" * 64
STAMP = "2026-09-11T00:00:00.000Z"


def _write_by_hand(data_root, kind, artifact_id, *, manifest=True):
    """A committed-looking directory made without the writer (Task 6 adds the writer)."""
    d = artifact_dir(data_root, kind, artifact_id)
    d.mkdir(parents=True)
    spec = ArtifactSpec(kind=kind, id=artifact_id)
    (d / "spec.json").write_text(
        json.dumps({"spec": spec.model_dump(), "opened_at": STAMP, "vcp_version": "t"}),
        encoding="utf-8",
        newline="\n",
    )
    if manifest:
        m = ArtifactManifest(
            spec=spec,
            files=[FileEntry(name="a.txt", bytes=1, sha256=SHA)],
            created_at=STAMP,
            vcp_version="t",
        )
        (d / "manifest.json").write_text(
            m.model_dump_json(indent=1) + "\n", encoding="utf-8", newline="\n"
        )
    return d


def test_ledger_round_trip(roots):
    assert supersession_log(roots.data, "k").name == SUPERSESSION_LEDGER == "supersession.jsonl"
    assert supersession_log(roots.data, "k") == roots.data / "artifacts" / "k" / "supersession.jsonl"
    assert read_supersession(roots.data, "k") == [] and supersession_of(roots.data, "k", "x") is None
    row = SupersessionRow(
        ts=STAMP,
        kind="k",
        id="new",
        manifest_sha256=SHA,
        supersedes_id="old",
        supersedes_sha256="b" * 64,
        reason="r",
    )
    append_supersession(roots.data, row)
    append_supersession(roots.data, row.model_copy(update={"id": "new2"}))
    assert read_supersession(roots.data, "k") == [row, row.model_copy(update={"id": "new2"})]
    assert supersession_of(roots.data, "k", "new2").id == "new2"
    assert supersession_of(roots.data, "k", "old") is None
    assert b"\r" not in supersession_log(roots.data, "k").read_bytes()
    with pytest.raises(ValidationFailed):
        supersession_log(roots.data, "../k")


def test_load_manifest_three_states(roots):
    with pytest.raises(ValidationFailed, match="^not_found: artifact k/none") as ei:
        store.load_manifest(roots.data, "k", "none")
    assert ei.value.fields == {"kind": "k", "id": "none"}
    _write_by_hand(roots.data, "k", "half", manifest=False)
    with pytest.raises(ValidationFailed, match="^partial: artifact k/half") as ei:
        store.load_manifest(roots.data, "k", "half")
    assert ei.value.fields == {"kind": "k", "id": "half"}
    assert store.is_partial(roots.data, "k", "half")
    assert not store.is_partial(roots.data, "k", "none")
    d = _write_by_hand(roots.data, "k", "full")
    m = store.load_manifest(roots.data, "k", "full")
    assert m.spec.id == "full" and m.files[0].name == "a.txt"
    assert not store.is_partial(roots.data, "k", "full")
    assert store.manifest_path(roots.data, "k", "full") == d / "manifest.json"
    (d / "manifest.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="bad manifest") as ei:
        store.load_manifest(roots.data, "k", "full")
    assert ei.value.location == str(d / "manifest.json")


def test_load_manifest_refuses_a_manifest_from_another_id(roots):
    src = _write_by_hand(roots.data, "k", "one")
    other = artifact_dir(roots.data, "k", "two")
    other.mkdir()
    (other / "manifest.json").write_bytes((src / "manifest.json").read_bytes())
    with pytest.raises(IntegrityError, match="^mismatch: .*says it is k/one"):
        store.load_manifest(roots.data, "k", "two")


def test_resolve_inputs_hashes_relativises_and_checks(roots, tmp_path):
    inside = roots.data / "plans" / "plan.json"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"plan")
    outside = tmp_path / "elsewhere.bin"
    outside.write_bytes(b"out")
    spec = ArtifactSpec(
        kind="k",
        id="i",
        inputs=[
            InputRef(name="plan", path=str(inside)),
            InputRef(name="weights", path=str(outside)),
            InputRef(name="code", sha256=SHA),
            InputRef(name="rel", path="plans/plan.json"),
        ],
    )
    r = store.resolve_inputs(spec, roots.data)
    by = {i.name: i for i in r.inputs}
    assert by["plan"].path == "plans/plan.json" and by["plan"].sha256 == sha256_file(inside)
    assert by["rel"].path == "plans/plan.json" and by["rel"].sha256 == sha256_file(inside)
    assert by["weights"].path == outside.resolve().as_posix()
    assert by["weights"].sha256 == sha256_file(outside)
    assert by["code"] == InputRef(name="code", sha256=SHA)
    assert spec.inputs[0].sha256 is None  # the caller's spec is not mutated
    gone = ArtifactSpec(kind="k", id="i", inputs=[InputRef(name="gone", path=str(tmp_path / "gone"))])
    with pytest.raises(ValidationFailed, match="^not_found: input 'gone'") as ei:
        store.resolve_inputs(gone, roots.data)
    assert ei.value.fields == {"input": "gone"}
    wrong = ArtifactSpec(kind="k", id="i", inputs=[InputRef(name="plan", path=str(inside), sha256=SHA)])
    with pytest.raises(IntegrityError, match="^mismatch: input 'plan'") as ei:
        store.resolve_inputs(wrong, roots.data)
    assert ei.value.fields == {"input": "plan"} and ei.value.location == str(inside)
    right = ArtifactSpec(
        kind="k", id="i", inputs=[InputRef(name="plan", path=str(inside), sha256=sha256_file(inside))]
    )
    assert store.resolve_inputs(right, roots.data).inputs[0].path == "plans/plan.json"


def test_spec_diff_ignores_notes_and_names_inputs():
    a = ArtifactSpec(
        kind="k",
        id="i",
        seed=1,
        params={"x": "1"},
        inputs=[InputRef(name="p", sha256=SHA)],
        notes="one",
    )
    assert store.spec_diff(a, a.model_copy(update={"notes": "two"})) == []
    b = a.model_copy(
        update={
            "seed": 2,
            "params": {"x": "2"},
            "inputs": [InputRef(name="p", sha256="b" * 64), InputRef(name="q", sha256=SHA)],
        }
    )
    assert store.spec_diff(a, b) == ["seed", "params", "inputs.p", "inputs.q"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/artifact/test_store.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'vcp.artifact.ledger'`）

- [ ] **Step 3: 寫 `src/vcp/artifact/ledger.py`**

```python
"""``<data_root>/artifacts/<kind>/supersession.jsonl``: append-only, written after a commit, an
index that ``relink`` can rebuild from committed manifests (spec 2). The jsonl helpers are the
measurement layer's, as the backup ledger already does."""

from __future__ import annotations

from pathlib import Path

from vcp.artifact.schema import ArtifactManifest, SupersessionRow
from vcp.core.paths import artifacts_root, validate_name
from vcp.core.time import stamp
from vcp.measure.ledger import append_row, read_rows

SUPERSESSION_LEDGER = "supersession.jsonl"


def supersession_log(data_root: Path, kind: str) -> Path:
    validate_name(kind)
    return artifacts_root(data_root) / kind / SUPERSESSION_LEDGER


def append_supersession(data_root: Path, row: SupersessionRow) -> None:
    append_row(supersession_log(data_root, row.kind), row)


def read_supersession(data_root: Path, kind: str) -> list[SupersessionRow]:
    return read_rows(supersession_log(data_root, kind), SupersessionRow)


def supersession_of(data_root: Path, kind: str, artifact_id: str) -> SupersessionRow | None:
    """The first row recording ``artifact_id`` superseding something (``relink`` is idempotent,
    so there is at most one)."""
    return next((r for r in read_supersession(data_root, kind) if r.id == artifact_id), None)


def row_for(manifest: ArtifactManifest, manifest_sha256: str) -> SupersessionRow:
    """The row a committed manifest implies. The writer appends it right after the commit and
    ``relink`` appends the same one when that append never happened."""
    spec = manifest.spec
    if spec.supersedes is None or spec.supersedes_reason is None or manifest.supersedes_sha256 is None:
        raise ValueError(f"artifact {spec.kind}/{spec.id} supersedes nothing")
    return SupersessionRow(
        ts=stamp(),
        kind=spec.kind,
        id=spec.id,
        manifest_sha256=manifest_sha256,
        supersedes_id=spec.supersedes,
        supersedes_sha256=manifest.supersedes_sha256,
        reason=spec.supersedes_reason,
    )
```

**寫 `src/vcp/artifact/store.py`**（Task 7 與 8 會在檔尾追加 `VerifyResult` / `verify` / `reuse` / `relink`）

```python
"""Reading committed artifacts (spec 8): the three states of a directory, input resolution, what
makes two specs the same job, verification, reuse and the ledger repair."""

from __future__ import annotations

from pathlib import Path

from vcp.artifact.schema import ArtifactManifest, ArtifactSpec, InputRef
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir, resolve_stored_path, store_path

MANIFEST = "manifest.json"
SPEC = "spec.json"
FAILURE = "failure.json"


def _ident(kind: str, artifact_id: str) -> dict[str, str]:
    return {"kind": kind, "id": artifact_id}


def manifest_path(data_root: Path, kind: str, artifact_id: str) -> Path:
    return artifact_dir(data_root, kind, artifact_id) / MANIFEST


def is_partial(data_root: Path, kind: str, artifact_id: str) -> bool:
    """A directory without ``manifest.json``: claimed, never committed. Not an artifact."""
    d = artifact_dir(data_root, kind, artifact_id)
    return d.is_dir() and not (d / MANIFEST).is_file()


def load_manifest(data_root: Path, kind: str, artifact_id: str) -> ArtifactManifest:
    """``not_found:`` (no directory), ``partial:`` (a directory, no manifest) and a located error
    for a manifest that does not parse: a consumer never mistakes a half-written job for a
    missing one, or for an artifact."""
    d = artifact_dir(data_root, kind, artifact_id)
    if not d.is_dir():
        raise ValidationFailed(
            f"not_found: artifact {kind}/{artifact_id} ({d})", fields=_ident(kind, artifact_id)
        )
    path = d / MANIFEST
    if not path.is_file():
        raise ValidationFailed(
            f"partial: artifact {kind}/{artifact_id} has no {MANIFEST}: the job that claimed "
            "the id never committed (see `vcp artifact status`)",
            fields=_ident(kind, artifact_id),
        )
    try:
        manifest = ArtifactManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValidationFailed(f"bad manifest: {e}", location=str(path)) from e
    if manifest.spec.kind != kind or manifest.spec.id != artifact_id:
        raise IntegrityError(
            f"mismatch: {path} says it is {manifest.spec.kind}/{manifest.spec.id}",
            location=str(path),
            fields=_ident(kind, artifact_id),
        )
    return manifest


def resolve_inputs(spec: ArtifactSpec, data_root: Path) -> ArtifactSpec:
    """Hash every input that has a path (``not_found:`` when it is not a file, ``mismatch:`` when
    a declared sha disagrees) and store the path the way cards do (``store_path``). Inputs given
    by sha alone pass through. Returns a new spec; the caller's is untouched."""
    resolved: list[InputRef] = []
    for ref in spec.inputs:
        if ref.path is None:
            resolved.append(ref)
            continue
        path = resolve_stored_path(ref.path, data_root)
        if not path.is_file():
            raise ValidationFailed(
                f"not_found: input {ref.name!r} at {path}", fields={"input": ref.name}
            )
        digest = sha256_file(path)
        if ref.sha256 is not None and ref.sha256 != digest:
            raise IntegrityError(
                f"mismatch: input {ref.name!r} hashes to {digest[:12]}, not the declared "
                f"{ref.sha256[:12]}",
                location=str(path),
                fields={"input": ref.name},
            )
        resolved.append(InputRef(name=ref.name, path=store_path(path, data_root), sha256=digest))
    return spec.model_copy(update={"inputs": resolved})


def spec_diff(recorded: ArtifactSpec, wanted: ArtifactSpec) -> list[str]:
    """Field names where two specs describe different jobs: every field but ``notes``; inputs by
    name (``inputs.<name>``), path and sha both."""
    a = recorded.model_dump(exclude={"notes", "inputs"})
    b = wanted.model_dump(exclude={"notes", "inputs"})
    diff = [k for k in a if a[k] != b[k]]
    ins_a = {i.name: (i.path, i.sha256) for i in recorded.inputs}
    ins_b = {i.name: (i.path, i.sha256) for i in wanted.inputs}
    diff += [f"inputs.{n}" for n in sorted(set(ins_a) | set(ins_b)) if ins_a.get(n) != ins_b.get(n)]
    return diff
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/artifact tests/unit/artifact && uv run pytest tests/unit/artifact -o addopts="" -q`
Expected: 全部通過

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/artifact/ledger.py src/vcp/artifact/store.py tests/unit/artifact/test_store.py
git commit -m "feat(artifact): supersession 台帳、三態載入、input 解析與 spec 比對" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `writer.py`——`ArtifactWriter`（open、寫檔、reserve、commit、例外離開）

**Files:**
- Create: `src/vcp/artifact/writer.py`
- Test: `tests/unit/artifact/test_writer.py`

**Interfaces:**
- Consumes: Task 5 的 `store.resolve_inputs` / `load_manifest` / `MANIFEST` / `SPEC` / `FAILURE`、Task 4 的模型與 `check_file_name`、Task 1 的 `write_once` / `write_once_stream`、既有 `build_string`、`redact`、`stamp`、`sha256_file`、`resolve_stored_path`。
- Produces: `ArtifactWriter.create(spec, *, data_root) -> ArtifactWriter`（屬性 `spec`（已解析）、`data_root`、`dir`、`manifest: ArtifactManifest | None`）、`write_json(name, obj) -> FileEntry`、`write_text(name, text) -> FileEntry`、`write_bytes(name, data) -> FileEntry`、`add_file(name, src: Path) -> FileEntry`、`reserve(name) -> Path`、`commit() -> ArtifactManifest`；可當 context manager。Task 8 在 `commit` 裡加 supersession；本任務的 `commit` 對有 `supersedes` 的 spec 只在 open 查舊產物存在且已 commit。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/artifact/test_writer.py`

```python
import os

import pytest

from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec, FailureRecord, InputRef, SpecRecord
from vcp.artifact.writer import ArtifactWriter
from vcp.core import atomic
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir

SECRET = "fakesecretfakesecretfakesecret1234"


def _spec(**over) -> ArtifactSpec:
    return ArtifactSpec.model_validate({"kind": "receipt", "id": "r1", **over})


def test_open_claims_the_id_and_writes_spec_json(roots):
    with ArtifactWriter.create(_spec(seed=7), data_root=roots.data) as art:
        d = artifact_dir(roots.data, "receipt", "r1")
        assert art.dir == d and d.is_dir() and art.manifest is None
        rec = SpecRecord.model_validate_json((d / "spec.json").read_text(encoding="utf-8"))
        assert rec.spec.seed == 7 and rec.opened_at.endswith("Z") and rec.vcp_version
        assert not (d / "manifest.json").exists()
        with pytest.raises(ValidationFailed, match="^exists: artifact receipt/r1") as ei:
            ArtifactWriter.create(_spec(), data_root=roots.data)
        assert ei.value.fields == {"kind": "receipt", "id": "r1"}
    # left without commit: a partial, no failure.json, and the id stays taken
    assert store.is_partial(roots.data, "receipt", "r1")
    assert not (d / "failure.json").exists()
    with pytest.raises(ValidationFailed, match="^exists: "):
        ArtifactWriter.create(_spec(), data_root=roots.data)


def test_open_rejects_bad_inputs_before_claiming(roots, tmp_path):
    bad = _spec(inputs=[InputRef(name="plan", path=str(tmp_path / "gone"))])
    with pytest.raises(ValidationFailed, match="^not_found: input 'plan'"):
        ArtifactWriter.create(bad, data_root=roots.data)
    assert not artifact_dir(roots.data, "receipt", "r1").exists()
    plan = tmp_path / "plan.json"
    plan.write_bytes(b"{}")
    wrong = _spec(inputs=[InputRef(name="plan", path=str(plan), sha256="a" * 64)])
    with pytest.raises(IntegrityError, match="^mismatch: input 'plan'"):
        ArtifactWriter.create(wrong, data_root=roots.data)
    assert not artifact_dir(roots.data, "receipt", "r1").exists()


def test_write_add_reserve_and_commit(roots, tmp_path):
    src = tmp_path / "weights.pt"
    src.write_bytes(b"w" * 3000)
    plan = roots.data / "plan.json"
    plan.write_bytes(b"plan")
    spec = _spec(inputs=[InputRef(name="plan", path=str(plan))], params={"fold": "1"})
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        e1 = art.write_json("receipt.json", {"ok": True})
        art.write_text("notes/readme.txt", "hi\n")
        art.write_bytes("raw.bin", b"\x00\x01")
        e2 = art.add_file("weights.pt", src)
        out = art.reserve("features.npy")
        assert out == art.dir / "features.npy" and not out.exists()
        out.write_bytes(b"f" * 10)
        manifest = art.commit()
    d = art.dir
    assert (d / "receipt.json").read_text(encoding="utf-8") == '{\n "ok": true\n}\n'
    assert e1.bytes == 16 and e1.sha256 == sha256_file(d / "receipt.json")
    assert e2.bytes == 3000 and e2.sha256 == sha256_file(src)
    assert (d / "weights.pt").read_bytes() == b"w" * 3000
    assert [f.name for f in manifest.files] == [
        "features.npy",
        "notes/readme.txt",
        "raw.bin",
        "receipt.json",
        "weights.pt",
    ]
    features = manifest.file("features.npy")
    assert features is not None and features.bytes == 10 and features.sha256 == sha256_file(out)
    assert manifest.spec.inputs[0].path == "plan.json"
    assert manifest.spec.inputs[0].sha256 == sha256_file(plan)
    assert manifest.spec.params == {"fold": "1"} and manifest.created_at.endswith("Z")
    assert manifest.supersedes_sha256 is None and manifest.schema_version == 1
    assert store.load_manifest(roots.data, "receipt", "r1") == manifest
    assert art.manifest == manifest
    assert not [p for p in d.rglob("*") if atomic.is_tmp_name(p.name)]
    assert (d / "manifest.json").read_bytes().count(b"\r") == 0


def test_file_name_rules_and_double_names(roots, tmp_path):
    with ArtifactWriter.create(_spec(), data_root=roots.data) as art:
        art.write_text("a.txt", "a")
        with pytest.raises(ValidationFailed, match="^exists: 'a.txt'") as ei:
            art.write_text("a.txt", "b")
        assert ei.value.fields == {"kind": "receipt", "id": "r1", "file": "a.txt"}
        art.reserve("big.npy")
        with pytest.raises(ValidationFailed, match="^exists: 'big.npy'"):
            art.write_text("big.npy", "x")
        with pytest.raises(ValidationFailed, match="^exists: 'big.npy'"):
            art.reserve("big.npy")
        for bad, prefix in (
            ("../x", "unsafe_path"),
            ("/abs", "unsafe_path"),
            ("manifest.json", "reserved_name"),
            ("spec.json", "reserved_name"),
            ("failure.json", "reserved_name"),
            (".x.0a1b2c3d.tmp", "reserved_name"),
        ):
            with pytest.raises(ValidationFailed, match=f"^{prefix}: ") as ei:
                art.write_text(bad, "x")
            assert ei.value.fields == {"kind": "receipt", "id": "r1", "file": bad}
        with pytest.raises(ValidationFailed, match="^not_found: .*add_file source"):
            art.add_file("w.pt", tmp_path / "missing.pt")
        assert (art.dir / "a.txt").read_text(encoding="utf-8") == "a"
        with pytest.raises(ValidationFailed, match="^not_found: reserved file 'big.npy'"):
            art.commit()
    assert store.is_partial(roots.data, "receipt", "r1")


def test_commit_refuses_drifted_inputs(roots):
    plan = roots.data / "plan.json"
    plan.write_bytes(b"v1")
    spec = _spec(inputs=[InputRef(name="plan", path=str(plan))])
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        plan.write_bytes(b"v2")
        with pytest.raises(IntegrityError, match="^drift: input 'plan' changed during the job") as ei:
            art.commit()
        assert ei.value.fields == {"kind": "receipt", "id": "r1", "input": "plan"}
    assert store.is_partial(roots.data, "receipt", "r1")
    plan.write_bytes(b"v1")
    with ArtifactWriter.create(_spec(id="r2", inputs=[InputRef(name="plan", path=str(plan))]), data_root=roots.data) as art:
        plan.unlink()
        with pytest.raises(IntegrityError, match="^drift: input 'plan' .*gone"):
            art.commit()


def test_closed_after_commit(roots):
    with ArtifactWriter.create(_spec(), data_root=roots.data) as art:
        art.write_text("a.txt", "a")
        art.commit()
        with pytest.raises(ValidationFailed, match="^closed: ") as ei:
            art.write_text("b.txt", "b")
        assert ei.value.fields == {"kind": "receipt", "id": "r1"}
        with pytest.raises(ValidationFailed, match="^closed: "):
            art.commit()
    with pytest.raises(ValidationFailed, match="^closed: "):
        art.reserve("c.npy")
    assert [f.name for f in store.load_manifest(roots.data, "receipt", "r1").files] == ["a.txt"]
    assert not (art.dir / "failure.json").exists()


def test_exception_leaves_a_partial_with_a_redacted_failure_record(roots):
    with pytest.raises(RuntimeError, match="upload failed"):
        with ArtifactWriter.create(_spec(), data_root=roots.data) as art:
            art.write_text("a.txt", "a")
            raise RuntimeError(f"upload failed key={SECRET}")
    d = art.dir
    assert store.is_partial(roots.data, "receipt", "r1")
    rec = FailureRecord.model_validate_json((d / "failure.json").read_text(encoding="utf-8"))
    assert rec.exception == "RuntimeError" and rec.message == "upload failed key=<redacted>"
    assert rec.ts.endswith("Z")
    for p in d.rglob("*"):
        if p.is_file():
            assert SECRET.encode() not in p.read_bytes(), p
    with pytest.raises(ValidationFailed, match="^closed: "):
        art.write_text("late.txt", "x")


def test_manifest_publish_failure_leaves_a_partial(roots, monkeypatch):
    with ArtifactWriter.create(_spec(), data_root=roots.data) as art:
        art.write_text("a.txt", "a")
        real_replace = os.replace

        def flaky(src, dst):
            if str(dst).endswith("manifest.json"):
                raise OSError("power cut")
            return real_replace(src, dst)

        monkeypatch.setattr(atomic.os, "replace", flaky)
        with pytest.raises(OSError, match="power cut"):
            art.commit()
    assert store.is_partial(roots.data, "receipt", "r1") and art.manifest is None
    assert sorted(p.name for p in art.dir.iterdir()) == ["a.txt", "failure.json", "spec.json"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/artifact/test_writer.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'vcp.artifact.writer'`）

- [ ] **Step 3: 寫 `src/vcp/artifact/writer.py`**

```python
"""``ArtifactWriter`` (spec 7): claims an id by ``os.mkdir``, writes every file through
``write_once``, and commits by writing ``manifest.json`` last. Nothing before that line is an
artifact; nothing after it can be changed. The writer never deletes anything."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from vcp.artifact import store
from vcp.artifact.schema import (
    ArtifactManifest,
    ArtifactSpec,
    FailureRecord,
    FileEntry,
    SpecRecord,
    check_file_name,
)
from vcp.core.atomic import write_once, write_once_stream
from vcp.core.build import build_string
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir, resolve_stored_path
from vcp.core.proc import redact
from vcp.core.time import stamp

_CHUNK = 1 << 20


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            yield chunk


class ArtifactWriter:
    """``with ArtifactWriter.create(spec, data_root=root) as art:`` ... ``art.commit()``.

    Leaving the block without ``commit()`` keeps the directory as a partial (plus
    ``failure.json`` when an exception did it); the id stays claimed until ``vcp artifact clean``.
    """

    def __init__(self, spec: ArtifactSpec, data_root: Path, directory: Path) -> None:
        self.spec = spec
        self.data_root = data_root
        self.dir = directory
        self.manifest: ArtifactManifest | None = None
        self._files: dict[str, FileEntry] = {}
        self._reserved: list[str] = []
        self._closed = False

    @classmethod
    def create(cls, spec: ArtifactSpec, *, data_root: Path) -> ArtifactWriter:
        """Open: resolve the inputs (their sha is fixed now), require a supersedes target that
        exists and is committed, claim the directory, write ``spec.json``."""
        data_root = Path(data_root)
        resolved = store.resolve_inputs(spec, data_root)
        if resolved.supersedes is not None:
            store.load_manifest(data_root, resolved.kind, resolved.supersedes)
        directory = artifact_dir(data_root, resolved.kind, resolved.id)
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir()
        except FileExistsError:
            raise ValidationFailed(
                f"exists: artifact {resolved.kind}/{resolved.id} already exists (complete or "
                "partial); pick a new id, or supersede it",
                fields={"kind": resolved.kind, "id": resolved.id},
            ) from None
        record = SpecRecord(spec=resolved, opened_at=stamp(), vcp_version=build_string())
        write_once(directory / store.SPEC, _json_bytes(record.model_dump(mode="json")))
        return cls(resolved, data_root, directory)

    # -- context manager ----------------------------------------------------------------------

    def __enter__(self) -> ArtifactWriter:
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        if exc is not None and self.manifest is None:
            self._record_failure(exc)
        self._closed = True

    def _record_failure(self, exc: BaseException) -> None:
        record = FailureRecord(ts=stamp(), exception=type(exc).__name__, message=redact(str(exc)))
        try:
            write_once(self.dir / store.FAILURE, _json_bytes(record.model_dump(mode="json")))
        except Exception:
            # The job's own exception is the one to surface; failing to record it must not hide it.
            pass

    # -- files --------------------------------------------------------------------------------

    def _ident(self) -> dict[str, str]:
        return {"kind": self.spec.kind, "id": self.spec.id}

    def _claim(self, name: str) -> Path:
        if self._closed:
            raise ValidationFailed(
                f"closed: artifact {self.spec.kind}/{self.spec.id} is committed or closed; "
                "open a new id",
                fields=self._ident(),
            )
        try:
            check_file_name(name)
        except ValueError as e:
            raise ValidationFailed(str(e), fields={**self._ident(), "file": name}) from e
        if name in self._files or name in self._reserved:
            raise ValidationFailed(
                f"exists: {name!r} is already written or reserved in artifact "
                f"{self.spec.kind}/{self.spec.id}",
                fields={**self._ident(), "file": name},
            )
        target = self.dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _record(self, name: str, size: int, digest: str) -> FileEntry:
        entry = FileEntry(name=name, bytes=size, sha256=digest)
        self._files[name] = entry
        return entry

    def write_bytes(self, name: str, data: bytes) -> FileEntry:
        target = self._claim(name)
        return self._record(name, len(data), write_once(target, data))

    def write_text(self, name: str, text: str) -> FileEntry:
        return self.write_bytes(name, text.encode("utf-8"))

    def write_json(self, name: str, obj: Any) -> FileEntry:
        return self.write_bytes(name, _json_bytes(obj))

    def add_file(self, name: str, src: Path) -> FileEntry:
        """Copy ``src`` in, hashing as it streams (a 20 GB file is read once)."""
        src = Path(src)
        if not src.is_file():
            raise ValidationFailed(
                f"not_found: {src} (add_file source)", fields={**self._ident(), "file": name}
            )
        target = self._claim(name)
        size, digest = write_once_stream(target, _chunks(src))
        return self._record(name, size, digest)

    def reserve(self, name: str) -> Path:
        """Register a name and return its final path for the caller to write directly (numpy,
        memmap). Hashed at commit; ``not_found:`` then if it was never written."""
        target = self._claim(name)
        self._reserved.append(name)
        return target

    # -- commit -------------------------------------------------------------------------------

    def _check_reserved(self) -> None:
        for name in self._reserved:
            path = self.dir / name
            if not path.is_file():
                raise ValidationFailed(
                    f"not_found: reserved file {name!r} was never written",
                    fields={**self._ident(), "file": name},
                )
            self._record(name, path.stat().st_size, sha256_file(path))

    def _check_drift(self) -> None:
        """Re-hash every input that has a path; a sha that moved since open is ``drift:``."""
        for ref in self.spec.inputs:
            if ref.path is None or ref.sha256 is None:
                continue
            path = resolve_stored_path(ref.path, self.data_root)
            now = sha256_file(path) if path.is_file() else "gone"
            if now != ref.sha256:
                raise IntegrityError(
                    f"drift: input {ref.name!r} changed during the job "
                    f"({ref.sha256[:12]} -> {now[:12]})",
                    location=str(path),
                    fields={**self._ident(), "input": ref.name},
                )

    def commit(self) -> ArtifactManifest:
        """Reserved files present → inputs unchanged → ``manifest.json`` (the commit point).
        Afterwards the writer is closed."""
        if self._closed:
            raise ValidationFailed(
                f"closed: artifact {self.spec.kind}/{self.spec.id} is committed or closed",
                fields=self._ident(),
            )
        self._check_reserved()
        self._check_drift()
        manifest = ArtifactManifest(
            spec=self.spec,
            files=[self._files[n] for n in sorted(self._files)],
            created_at=stamp(),
            vcp_version=build_string(),
        )
        write_once(self.dir / store.MANIFEST, _json_bytes(manifest.model_dump(mode="json")))
        self.manifest = manifest
        self._closed = True
        return manifest
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/artifact tests/unit/artifact && uv run pytest tests/unit/artifact -o addopts="" -q`
Expected: 全部通過

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/artifact/writer.py tests/unit/artifact/test_writer.py
git commit -m "feat(artifact): ArtifactWriter——mkdir 搶 id、write_once 逐檔、manifest 最後寫" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: `store.py` 第二部分——`verify` 與 `reuse`

**Files:**
- Modify: `src/vcp/artifact/store.py`（檔尾追加；import 區加 `from dataclasses import dataclass`、`from vcp.artifact.ledger import supersession_of`、`from vcp.artifact.schema import RESERVED_NAMES`）
- Test: `tests/unit/artifact/test_store.py`（追加）

**Interfaces:**
- Consumes: Task 5 的 `load_manifest` / `resolve_inputs` / `spec_diff`、`ledger.supersession_of`、Task 6 的 `ArtifactWriter`（測試用）。
- Produces: `VerifyResult(mismatch: list[str], missing: list[str], extra: list[str], unlinked: bool)`（`.failed` 屬性 = 前三者任一非空）、`verify(data_root, kind, artifact_id) -> VerifyResult`、`reuse(spec, data_root, *, check_files=False) -> ArtifactManifest | None`。

- [ ] **Step 1: 寫失敗的測試**——追加到 `tests/unit/artifact/test_store.py`（import 區加 `from vcp.artifact.writer import ArtifactWriter`）

```python
def _commit(roots, artifact_id="r1", *, files=("a.txt",), **over) -> ArtifactManifest:
    spec = ArtifactSpec.model_validate({"kind": "receipt", "id": artifact_id, **over})
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        for name in files:
            art.write_text(name, f"{name}\n")
        return art.commit()


def test_verify_finds_mismatch_missing_and_extra(roots):
    _commit(roots, files=("a.txt", "b/c.bin"))
    d = artifact_dir(roots.data, "receipt", "r1")
    ok = store.verify(roots.data, "receipt", "r1")
    assert ok == store.VerifyResult([], [], [], False) and not ok.failed
    (d / "a.txt").write_text("changed\n", encoding="utf-8")
    (d / "b" / "c.bin").unlink()
    (d / "extra.txt").write_text("x", encoding="utf-8")
    (d / ".a.txt.0a1b2c3d.tmp").write_text("x", encoding="utf-8")
    res = store.verify(roots.data, "receipt", "r1")
    assert res.mismatch == ["a.txt"] and res.missing == ["b/c.bin"]
    assert res.extra == [".a.txt.0a1b2c3d.tmp", "extra.txt"] and res.failed
    assert not res.unlinked
    with pytest.raises(ValidationFailed, match="^not_found: "):
        store.verify(roots.data, "receipt", "nope")


def test_reuse_requires_the_whole_spec_to_match(roots):
    plan = roots.data / "plan.json"
    plan.write_bytes(b"v1")
    spec = ArtifactSpec(
        kind="receipt",
        id="r1",
        seed=1,
        inputs=[InputRef(name="plan", path=str(plan))],
        notes="first",
    )
    assert store.reuse(spec, roots.data) is None
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        art.write_text("a.txt", "a")
        with pytest.raises(ValidationFailed, match="^partial: "):
            store.reuse(spec, roots.data)
        manifest = art.commit()
    assert store.reuse(spec.model_copy(update={"notes": "second"}), roots.data) == manifest
    with pytest.raises(IntegrityError, match=r"^spec_mismatch: .*\(seed\)") as ei:
        store.reuse(spec.model_copy(update={"seed": 2}), roots.data)
    assert ei.value.fields == {"kind": "receipt", "id": "r1", "differs": "seed"}
    plan.write_bytes(b"v2")
    with pytest.raises(IntegrityError, match=r"\(inputs\.plan\)"):
        store.reuse(spec, roots.data)
    plan.write_bytes(b"v1")
    (art.dir / "a.txt").write_text("tampered", encoding="utf-8")
    assert store.reuse(spec, roots.data) == manifest  # files are not re-hashed by default
    with pytest.raises(IntegrityError, match="^mismatch: artifact receipt/r1 no longer matches"):
        store.reuse(spec, roots.data, check_files=True)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/artifact/test_store.py -o addopts="" -q`
Expected: 2 FAIL（`AttributeError: module 'vcp.artifact.store' has no attribute 'verify'`）

- [ ] **Step 3: 追加到 `src/vcp/artifact/store.py` 檔尾**（並補上三個 import）

```python
@dataclass(frozen=True)
class VerifyResult:
    """What ``verify`` found: names of manifest files whose bytes differ / are gone, files the
    manifest never named (a committed artifact takes no new files; leftover temps count), and
    whether a superseding artifact lacks its ledger row."""

    mismatch: list[str]
    missing: list[str]
    extra: list[str]
    unlinked: bool

    @property
    def failed(self) -> bool:
        return bool(self.mismatch or self.missing or self.extra)


def verify(data_root: Path, kind: str, artifact_id: str) -> VerifyResult:
    manifest = load_manifest(data_root, kind, artifact_id)
    d = artifact_dir(data_root, kind, artifact_id)
    mismatch: list[str] = []
    missing: list[str] = []
    for entry in manifest.files:
        path = d / entry.name
        if not path.is_file():
            missing.append(entry.name)
        elif path.stat().st_size != entry.bytes or sha256_file(path) != entry.sha256:
            mismatch.append(entry.name)
    named = {f.name for f in manifest.files}
    extra = sorted(
        rel
        for rel in (p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())
        if rel not in named and rel not in RESERVED_NAMES
    )
    unlinked = False
    if manifest.spec.supersedes is not None:
        row = supersession_of(data_root, kind, artifact_id)
        if row is None:
            unlinked = True
        elif row.manifest_sha256 != sha256_file(d / MANIFEST):
            mismatch.append(MANIFEST)
    return VerifyResult(mismatch, missing, extra, unlinked)


def reuse(
    spec: ArtifactSpec, data_root: Path, *, check_files: bool = False
) -> ArtifactManifest | None:
    """The artifact this spec would produce, if it is already there: ``None`` when nothing claims
    the id, ``partial:`` when a job did and never committed, ``spec_mismatch:`` when the committed
    spec differs in any field but ``notes`` (inputs by their current sha). A directory that exists
    is not a cache hit; a spec that matches is. Files are re-hashed only with ``check_files``."""
    resolved = resolve_inputs(spec, data_root)
    if not artifact_dir(data_root, resolved.kind, resolved.id).is_dir():
        return None
    manifest = load_manifest(data_root, resolved.kind, resolved.id)
    diff = spec_diff(manifest.spec, resolved)
    if diff:
        raise IntegrityError(
            f"spec_mismatch: artifact {resolved.kind}/{resolved.id} was committed from a "
            f"different spec ({', '.join(diff)}); use a new id or supersede it",
            fields={**_ident(resolved.kind, resolved.id), "differs": ",".join(diff)},
        )
    if check_files:
        res = verify(data_root, resolved.kind, resolved.id)
        if res.failed:
            raise IntegrityError(
                f"mismatch: artifact {resolved.kind}/{resolved.id} no longer matches its "
                f"manifest (mismatch={len(res.mismatch)} missing={len(res.missing)} "
                f"extra={len(res.extra)})",
                fields=_ident(resolved.kind, resolved.id),
            )
    return manifest
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/artifact tests/unit/artifact && uv run pytest tests/unit/artifact -o addopts="" -q`
Expected: 全部通過

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/artifact/store.py tests/unit/artifact/test_store.py
git commit -m "feat(artifact): verify 四項檢查與 reuse 的完整 spec 相等" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: 接替——writer 的 supersession、`lineage.py`、`store.relink`

**Files:**
- Modify: `src/vcp/artifact/writer.py`（`commit` 與兩個新方法；import 區加 `from vcp.artifact.ledger import append_supersession, row_for`）、`src/vcp/artifact/store.py`（檔尾加 `relink`；import 區的 ledger 行改成 `from vcp.artifact.ledger import append_supersession, row_for, supersession_of`、schema 行加 `SupersessionRow` 不需要）
- Create: `src/vcp/artifact/lineage.py`
- Test: `tests/unit/artifact/test_lineage.py`

**Interfaces:**
- Consumes: Task 7 `verify`、Task 5 `row_for` / `append_supersession` / `supersession_of` / `manifest_path`。
- Produces: `ArtifactWriter.commit` 對有 `supersedes` 的 spec：驗舊產物、記 `supersedes_sha256`、寫 manifest 後 append 台帳列；`lineage.list_manifests(data_root, kind) -> dict[str, ArtifactManifest]`、`successors_of(manifests) -> dict[str, list[str]]`、`Lineage(chain, successors, heads, forks)`、`lineage(data_root, kind, artifact_id) -> Lineage`、`head(data_root, kind, artifact_id) -> list[str]`；`store.relink(data_root, kind, artifact_id) -> bool`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/artifact/test_lineage.py`

```python
import json

import pytest

from vcp.artifact import store
from vcp.artifact.ledger import read_supersession, supersession_log, supersession_of
from vcp.artifact.lineage import head, lineage, list_manifests
from vcp.artifact.schema import ArtifactSpec
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file


def _commit(roots, artifact_id, **over):
    spec = ArtifactSpec.model_validate({"kind": "receipt", "id": artifact_id, **over})
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        art.write_text("a.txt", artifact_id)
        return art.commit()


def _rewrite_log(log, rows):
    log.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")


def _rows(log):
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_supersession_is_checked_at_open_and_commit_and_indexed(roots):
    new = ArtifactSpec(kind="receipt", id="new", supersedes="old", supersedes_reason="r")
    with pytest.raises(ValidationFailed, match="^not_found: artifact receipt/old"):
        ArtifactWriter.create(new, data_root=roots.data)
    assert not store.manifest_path(roots.data, "receipt", "new").parent.exists()
    half = ArtifactWriter.create(ArtifactSpec(kind="receipt", id="old"), data_root=roots.data)
    with pytest.raises(ValidationFailed, match="^partial: artifact receipt/old"):
        ArtifactWriter.create(new, data_root=roots.data)
    half.write_text("a.txt", "old")
    old = half.commit()
    old_sha = sha256_file(store.manifest_path(roots.data, "receipt", "old"))
    committed = _commit(roots, "new", supersedes="old", supersedes_reason="seed was 43 not 42")
    assert committed.supersedes_sha256 == old_sha and old.spec.id == "old"
    rows = read_supersession(roots.data, "receipt")
    assert len(rows) == 1 and rows[0].id == "new" and rows[0].supersedes_id == "old"
    assert rows[0].supersedes_sha256 == old_sha and rows[0].reason == "seed was 43 not 42"
    assert rows[0].manifest_sha256 == sha256_file(store.manifest_path(roots.data, "receipt", "new"))
    assert rows[0].ts.endswith("Z")
    assert store.verify(roots.data, "receipt", "new") == store.VerifyResult([], [], [], False)
    # a tampered old artifact cannot be superseded again until it verifies
    (store.manifest_path(roots.data, "receipt", "old").parent / "a.txt").write_text("x", encoding="utf-8")
    again = ArtifactSpec(kind="receipt", id="new2", supersedes="old", supersedes_reason="again")
    with ArtifactWriter.create(again, data_root=roots.data) as art:
        art.write_text("a.txt", "n2")
        with pytest.raises(IntegrityError, match="^mismatch: superseded artifact receipt/old") as ei:
            art.commit()
        assert ei.value.fields == {"kind": "receipt", "id": "new2", "supersedes": "old"}
    assert store.is_partial(roots.data, "receipt", "new2")
    assert len(read_supersession(roots.data, "receipt")) == 1


def test_lineage_head_forks_and_relink(roots):
    _commit(roots, "r1")
    _commit(roots, "r2", supersedes="r1", supersedes_reason="a")
    _commit(roots, "r3", supersedes="r2", supersedes_reason="b")
    _commit(roots, "r3b", supersedes="r2", supersedes_reason="fork")
    lin = lineage(roots.data, "receipt", "r2")
    assert [m.spec.id for m in lin.chain] == ["r1", "r2"]
    assert [m.spec.id for m in lin.successors] == ["r3", "r3b"]
    assert lin.heads == ["r3", "r3b"] and lin.forks == 1
    assert head(roots.data, "receipt", "r1") == ["r3", "r3b"]
    tip = lineage(roots.data, "receipt", "r3")
    assert [m.spec.id for m in tip.chain] == ["r1", "r2", "r3"]
    assert tip.successors == [] and tip.heads == ["r3"] and tip.forks == 1
    alone = _commit(roots, "solo")
    assert lineage(roots.data, "receipt", "solo").chain == [alone]
    assert head(roots.data, "receipt", "solo") == ["solo"]
    assert set(list_manifests(roots.data, "receipt")) == {"r1", "r2", "r3", "r3b", "solo"}
    assert list_manifests(roots.data, "nothing") == {}
    with pytest.raises(ValidationFailed, match="^not_found: "):
        lineage(roots.data, "receipt", "r9")
    # the crash window after manifest.json: drop r3's row, verify warns, relink restores it
    log = supersession_log(roots.data, "receipt")
    _rewrite_log(log, [r for r in _rows(log) if r["id"] != "r3"])
    assert supersession_of(roots.data, "receipt", "r3") is None
    assert store.verify(roots.data, "receipt", "r3").unlinked
    assert store.relink(roots.data, "receipt", "r3") is True
    assert store.relink(roots.data, "receipt", "r3") is False
    assert store.relink(roots.data, "receipt", "r1") is False  # supersedes nothing
    assert not store.verify(roots.data, "receipt", "r3").unlinked
    row = supersession_of(roots.data, "receipt", "r3")
    assert row is not None and row.reason == "b" and row.supersedes_id == "r2"
    # a ledger row that disagrees with the manifest on disk is a mismatch
    rows = _rows(log)
    for r in rows:
        if r["id"] == "r3":
            r["manifest_sha256"] = "0" * 64
    _rewrite_log(log, rows)
    assert store.verify(roots.data, "receipt", "r3").mismatch == ["manifest.json"]
    # a partial id is neither a chain member nor a valid start
    ArtifactWriter.create(ArtifactSpec(kind="receipt", id="half"), data_root=roots.data)
    with pytest.raises(ValidationFailed, match="^partial: "):
        lineage(roots.data, "receipt", "half")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/artifact/test_lineage.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'vcp.artifact.lineage'`）

- [ ] **Step 3: 改 `src/vcp/artifact/writer.py`**——import 區加 `from vcp.artifact.ledger import append_supersession, row_for`；在 `_check_drift` 之後加兩個方法，並把 `commit` 換掉：

```python
    def _check_supersedes(self) -> str | None:
        """The old artifact must exist, be committed and verify clean (mismatch / missing /
        extra); a missing ledger row on it does not block. Returns the sha of its manifest."""
        old = self.spec.supersedes
        if old is None:
            return None
        res = store.verify(self.data_root, self.spec.kind, old)
        if res.failed:
            raise IntegrityError(
                f"mismatch: superseded artifact {self.spec.kind}/{old} no longer matches its "
                f"manifest (mismatch={len(res.mismatch)} missing={len(res.missing)} "
                f"extra={len(res.extra)}); it cannot be superseded until `vcp artifact verify` "
                "passes",
                fields={**self._ident(), "supersedes": old},
            )
        return sha256_file(store.manifest_path(self.data_root, self.spec.kind, old))

    def _link(self, manifest: ArtifactManifest) -> None:
        """The ledger row, after the commit point. A crash between the two leaves a valid
        artifact that ``verify`` reports as unlinked and ``relink`` repairs."""
        if manifest.spec.supersedes is None:
            return
        append_supersession(
            self.data_root, row_for(manifest, sha256_file(self.dir / store.MANIFEST))
        )

    def commit(self) -> ArtifactManifest:
        """Reserved files present → inputs unchanged → superseded artifact verified →
        ``manifest.json`` (the commit point) → ledger row. Afterwards the writer is closed."""
        if self._closed:
            raise ValidationFailed(
                f"closed: artifact {self.spec.kind}/{self.spec.id} is committed or closed",
                fields=self._ident(),
            )
        self._check_reserved()
        self._check_drift()
        supersedes_sha256 = self._check_supersedes()
        manifest = ArtifactManifest(
            spec=self.spec,
            files=[self._files[n] for n in sorted(self._files)],
            created_at=stamp(),
            vcp_version=build_string(),
            supersedes_sha256=supersedes_sha256,
        )
        write_once(self.dir / store.MANIFEST, _json_bytes(manifest.model_dump(mode="json")))
        self.manifest = manifest
        self._closed = True
        self._link(manifest)
        return manifest
```

**寫 `src/vcp/artifact/lineage.py`**

```python
"""Supersession chains (spec 8): root → … → id → successors. Forks are allowed and visible."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

from vcp.artifact.schema import ArtifactManifest
from vcp.artifact.store import MANIFEST, load_manifest
from vcp.core.errors import ValidationFailed
from vcp.core.paths import artifacts_root, validate_name


def list_manifests(data_root: Path, kind: str) -> dict[str, ArtifactManifest]:
    """Every committed artifact of a kind, by id. Partial and foreign directories are skipped."""
    validate_name(kind)
    kind_dir = artifacts_root(data_root) / kind
    if not kind_dir.is_dir():
        return {}
    return {
        d.name: load_manifest(data_root, kind, d.name)
        for d in sorted(kind_dir.iterdir())
        if d.is_dir() and (d / MANIFEST).is_file()
    }


def successors_of(manifests: dict[str, ArtifactManifest]) -> dict[str, list[str]]:
    """``old id -> ids that supersede it`` over a kind's committed manifests."""
    out: dict[str, list[str]] = {}
    for m in manifests.values():
        if m.spec.supersedes is not None:
            out.setdefault(m.spec.supersedes, []).append(m.spec.id)
    return out


@dataclass(frozen=True)
class Lineage:
    chain: list[ArtifactManifest]  # root first, the asked-for artifact last
    successors: list[ArtifactManifest]  # everything that supersedes it, breadth first
    heads: list[str]  # the artifact or its successors that nothing supersedes
    forks: int  # members with more than one successor


def lineage(data_root: Path, kind: str, artifact_id: str) -> Lineage:
    manifests = list_manifests(data_root, kind)
    if artifact_id not in manifests:
        load_manifest(data_root, kind, artifact_id)  # not_found: / partial:
    chain: list[ArtifactManifest] = []
    seen: set[str] = set()
    cursor: str | None = artifact_id
    while cursor is not None and cursor not in seen:
        seen.add(cursor)
        m = manifests.get(cursor)
        if m is None:
            raise ValidationFailed(
                f"not_found: artifact {kind}/{cursor} in the supersession chain of "
                f"{artifact_id!r} is missing or partial",
                fields={"kind": kind, "id": cursor},
            )
        chain.append(m)
        cursor = m.spec.supersedes
    chain.reverse()
    after = successors_of(manifests)
    successors: list[ArtifactManifest] = []
    queue = deque(after.get(artifact_id, []))
    while queue:
        nxt = queue.popleft()
        if nxt in seen:
            continue
        seen.add(nxt)
        successors.append(manifests[nxt])
        queue.extend(after.get(nxt, []))
    members = [m.spec.id for m in chain] + [m.spec.id for m in successors]
    heads = [i for i in members if not after.get(i)]
    forks = sum(1 for i in members if len(after.get(i, [])) > 1)
    return Lineage(chain, successors, heads, forks)


def head(data_root: Path, kind: str, artifact_id: str) -> list[str]:
    return lineage(data_root, kind, artifact_id).heads
```

**追加到 `src/vcp/artifact/store.py` 檔尾**（import 區的 ledger 行改成 `from vcp.artifact.ledger import append_supersession, row_for, supersession_of`）

```python
def relink(data_root: Path, kind: str, artifact_id: str) -> bool:
    """Append the supersession row a committed manifest implies when the ledger lacks it (the
    crash window after ``manifest.json``). Idempotent: ``False`` when nothing was appended."""
    manifest = load_manifest(data_root, kind, artifact_id)
    if manifest.spec.supersedes is None:
        return False
    if supersession_of(data_root, kind, artifact_id) is not None:
        return False
    sha = sha256_file(manifest_path(data_root, kind, artifact_id))
    append_supersession(data_root, row_for(manifest, sha))
    return True
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/artifact tests/unit/artifact && uv run pytest tests/unit/artifact -o addopts="" -q`
Expected: 全部通過（Task 6 的 writer 測試不變）

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/artifact/writer.py src/vcp/artifact/lineage.py src/vcp/artifact/store.py tests/unit/artifact/test_lineage.py
git commit -m "feat(artifact): supersession——open 查舊產物、commit 驗 sha 並記台帳、lineage / head / relink" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: `clean.py`——`scan`（status 的資料）、`clean`、`parse_age`

**Files:**
- Create: `src/vcp/artifact/clean.py`
- Test: `tests/unit/artifact/test_clean.py`

**Interfaces:**
- Consumes: Task 8 `list_manifests` / `successors_of`、Task 5 `read_supersession`、`store.MANIFEST` / `SPEC` / `FAILURE`、Task 4 `SpecRecord` / `FailureRecord`、Task 1 `is_tmp_name`、`vcp.core.time.utc_now` / `parse_stamp`。
- Produces: `parse_age(text) -> timedelta`（`ValidationFailed`）、`PartialInfo(id, opened_at: str | None, failure: str | None)`、`KindStatus(kind, complete: list[str], partial: list[PartialInfo], unlinked: list[str], forks: int, foreign: list[str])`、`scan(data_root, kind=None) -> list[KindStatus]`、`CleanResult(candidates: list[str], removed: list[str])`（路徑相對 `artifacts/`、posix）、`clean(data_root, *, kind=None, older_than=timedelta(hours=24), apply=False) -> CleanResult`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/artifact/test_clean.py`

```python
import json
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import ANY

import pytest

from vcp.artifact import clean as cleanmod
from vcp.artifact.clean import CleanResult, KindStatus, PartialInfo, clean, parse_age, scan
from vcp.artifact.ledger import supersession_log
from vcp.artifact.schema import ArtifactSpec
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import ValidationFailed
from vcp.core.paths import artifact_dir, artifacts_root
from vcp.core.time import stamp, utc_now


def _commit(roots, kind, artifact_id, **over):
    spec = ArtifactSpec.model_validate({"kind": kind, "id": artifact_id, **over})
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        art.write_text("a.txt", artifact_id)
        return art.commit()


def _partial(roots, kind, artifact_id, *, fail=False) -> Path:
    art = ArtifactWriter.create(ArtifactSpec(kind=kind, id=artifact_id), data_root=roots.data)
    if fail:
        with pytest.raises(RuntimeError):
            with art:
                raise RuntimeError("boom")
    return art.dir


def _age(spec_json: Path, *, days: int) -> None:
    doc = json.loads(spec_json.read_text(encoding="utf-8"))
    doc["opened_at"] = stamp(utc_now() - timedelta(days=days))
    spec_json.write_text(json.dumps(doc), encoding="utf-8", newline="\n")


def _touch_old(p: Path, *, days: int) -> None:
    t = (utc_now() - timedelta(days=days)).timestamp()
    os.utime(p, (t, t))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", timedelta(0)),
        ("24h", timedelta(hours=24)),
        ("30m", timedelta(minutes=30)),
        ("2d", timedelta(days=2)),
        (" 1h ", timedelta(hours=1)),
    ],
)
def test_parse_age(text, expected):
    assert parse_age(text) == expected


@pytest.mark.parametrize("bad", ["", "24", "1w", "h", "-1h", "1.5h", "0h0"])
def test_parse_age_rejects(bad):
    with pytest.raises(ValidationFailed, match="--older-than"):
        parse_age(bad)


def test_scan_classifies_every_directory(roots):
    assert scan(roots.data) == []
    _commit(roots, "receipt", "r1")
    _commit(roots, "receipt", "r2", supersedes="r1", supersedes_reason="x")
    _commit(roots, "receipt", "r2b", supersedes="r1", supersedes_reason="fork")
    _partial(roots, "receipt", "half")
    _partial(roots, "receipt", "crashed", fail=True)
    (artifacts_root(roots.data) / "receipt" / "stray").mkdir()
    (artifacts_root(roots.data) / "receipt" / "loose.txt").write_text("x", encoding="utf-8")
    _commit(roots, "snapshot", "s1")
    supersession_log(roots.data, "receipt").unlink()
    receipt, snapshot = scan(roots.data)
    assert receipt.kind == "receipt" and receipt.complete == ["r1", "r2", "r2b"]
    assert receipt.partial == [
        PartialInfo("crashed", ANY, "RuntimeError"),
        PartialInfo("half", ANY, None),
    ]
    assert all(p.opened_at.endswith("Z") for p in receipt.partial)
    assert receipt.unlinked == ["r2", "r2b"] and receipt.forks == 1
    assert receipt.foreign == ["stray"]
    assert snapshot == KindStatus("snapshot", ["s1"], [], [], 0, [])
    assert scan(roots.data, "snapshot") == [snapshot] and scan(roots.data, "nothing") == []
    with pytest.raises(ValidationFailed):
        scan(roots.data, "../x")


def test_clean_lists_then_removes_only_old_partials_and_temps(roots):
    _commit(roots, "receipt", "done")
    old = _partial(roots, "receipt", "old", fail=True)
    fresh = _partial(roots, "receipt", "fresh")
    stray = artifacts_root(roots.data) / "receipt" / "stray"
    stray.mkdir()
    (stray / "x.bin").write_bytes(b"x")
    done_tmp = artifact_dir(roots.data, "receipt", "done") / ".a.txt.0a1b2c3d.tmp"
    kind_tmp = artifacts_root(roots.data) / "receipt" / ".supersession.jsonl.deadbeef.tmp"
    fresh_tmp = fresh / ".b.txt.01234567.tmp"
    for p in (done_tmp, kind_tmp, fresh_tmp):
        p.write_bytes(b"t")
    _age(old / "spec.json", days=2)
    for p in (done_tmp, kind_tmp):
        _touch_old(p, days=2)
    expected = [
        "receipt/old",
        "receipt/.supersession.jsonl.deadbeef.tmp",
        "receipt/done/.a.txt.0a1b2c3d.tmp",
    ]
    assert clean(roots.data) == CleanResult(expected, [])
    assert old.is_dir() and done_tmp.exists() and kind_tmp.exists()
    assert clean(roots.data, kind="snapshot") == CleanResult([], [])
    res = clean(roots.data, kind="receipt", apply=True)
    assert res == CleanResult(expected, expected)
    assert not old.exists() and not done_tmp.exists() and not kind_tmp.exists()
    assert fresh.is_dir() and fresh_tmp.exists() and stray.is_dir()
    assert (artifact_dir(roots.data, "receipt", "done") / "manifest.json").is_file()
    assert (artifact_dir(roots.data, "receipt", "done") / "a.txt").read_text(encoding="utf-8") == "done"
    assert clean(roots.data, apply=True) == CleanResult([], [])
    # a temp inside a candidate directory is not listed on its own
    assert clean(roots.data, older_than=timedelta(0)).candidates == ["receipt/fresh"]


def test_clean_never_touches_what_it_cannot_read_or_what_committed_meanwhile(roots, monkeypatch):
    broken = _partial(roots, "receipt", "broken")
    (broken / "spec.json").write_text("{", encoding="utf-8")
    _commit(roots, "receipt", "done")
    assert clean(roots.data, older_than=timedelta(0), apply=True) == CleanResult([], [])
    assert broken.is_dir()
    late = _partial(roots, "receipt", "late")
    _age(late / "spec.json", days=2)
    real_info = cleanmod._partial_info

    def commit_meanwhile(d):
        info = real_info(d)
        (d / "manifest.json").write_text("{}", encoding="utf-8")  # a job commits after listing
        return info

    monkeypatch.setattr(cleanmod, "_partial_info", commit_meanwhile)
    res = clean(roots.data, apply=True)
    assert res.candidates == ["receipt/late"] and res.removed == []
    assert (late / "manifest.json").is_file()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/artifact/test_clean.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'vcp.artifact.clean'`）

- [ ] **Step 3: 寫 `src/vcp/artifact/clean.py`**

```python
"""What is under ``artifacts/`` (spec 6 ``status``) and the only code that removes any of it
(spec 8 ``clean``): partial directories past a grace period, and temp files. One rule classifies
a directory: ``manifest.json`` → complete; else ``spec.json`` → partial; else foreign, never
touched."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vcp.artifact.ledger import read_supersession
from vcp.artifact.lineage import list_manifests, successors_of
from vcp.artifact.schema import FailureRecord, SpecRecord
from vcp.artifact.store import FAILURE, MANIFEST, SPEC
from vcp.core.atomic import is_tmp_name
from vcp.core.errors import ValidationFailed
from vcp.core.paths import artifacts_root, validate_name
from vcp.core.time import parse_stamp, utc_now

_AGE = re.compile(r"^(?P<n>\d+)(?P<unit>[mhd])?$")
_UNITS = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}


def parse_age(text: str) -> timedelta:
    """``--older-than``: ``<N>m`` / ``<N>h`` / ``<N>d`` (``24h``), or ``0``."""
    m = _AGE.match(text.strip())
    if m is None or (m.group("unit") is None and m.group("n") != "0"):
        raise ValidationFailed(f"--older-than expects <N>m|h|d or 0, got {text!r}")
    if m.group("unit") is None:
        return timedelta(0)
    return int(m.group("n")) * _UNITS[m.group("unit")]


@dataclass(frozen=True)
class PartialInfo:
    id: str
    opened_at: str | None  # None when spec.json does not parse
    failure: str | None  # exception class from failure.json, when the job raised


@dataclass(frozen=True)
class KindStatus:
    kind: str
    complete: list[str]
    partial: list[PartialInfo]
    unlinked: list[str]
    forks: int
    foreign: list[str]


def _kind_dirs(data_root: Path, kind: str | None) -> list[Path]:
    root = artifacts_root(data_root)
    if kind is not None:
        validate_name(kind)
        d = root / kind
        return [d] if d.is_dir() else []
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _partial_info(d: Path) -> PartialInfo:
    opened_at: str | None = None
    try:
        opened_at = SpecRecord.model_validate_json((d / SPEC).read_text(encoding="utf-8")).opened_at
    except (OSError, ValueError):
        pass
    failure: str | None = None
    if (d / FAILURE).is_file():
        try:
            failure = FailureRecord.model_validate_json(
                (d / FAILURE).read_text(encoding="utf-8")
            ).exception
        except (OSError, ValueError):
            failure = "unreadable"
    return PartialInfo(d.name, opened_at, failure)


def _partial_dirs(kind_dir: Path) -> list[Path]:
    return [
        d
        for d in sorted(p for p in kind_dir.iterdir() if p.is_dir())
        if not (d / MANIFEST).is_file() and (d / SPEC).is_file()
    ]


def scan(data_root: Path, kind: str | None = None) -> list[KindStatus]:
    out: list[KindStatus] = []
    for kind_dir in _kind_dirs(data_root, kind):
        manifests = list_manifests(data_root, kind_dir.name)
        partial = [_partial_info(d) for d in _partial_dirs(kind_dir)]
        foreign = [
            d.name
            for d in sorted(p for p in kind_dir.iterdir() if p.is_dir())
            if not (d / MANIFEST).is_file() and not (d / SPEC).is_file()
        ]
        linked = {r.id for r in read_supersession(data_root, kind_dir.name)}
        unlinked = [i for i, m in manifests.items() if m.spec.supersedes is not None and i not in linked]
        forks = sum(1 for ids in successors_of(manifests).values() if len(ids) > 1)
        out.append(KindStatus(kind_dir.name, list(manifests), partial, unlinked, forks, foreign))
    return out


@dataclass(frozen=True)
class CleanResult:
    candidates: list[str]  # "<kind>/<id>" for a partial directory, "<kind>/…/.x.<nonce>.tmp" for a temp
    removed: list[str]


def _opened_before(d: Path, threshold: datetime) -> bool:
    opened_at = _partial_info(d).opened_at
    if opened_at is None:
        return False  # unreadable: never a candidate
    try:
        return parse_stamp(opened_at) <= threshold
    except ValueError:
        return False


def _modified_before(p: Path, threshold: datetime) -> bool:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=UTC) <= threshold


def clean(
    data_root: Path,
    *,
    kind: str | None = None,
    older_than: timedelta = timedelta(hours=24),
    apply: bool = False,
) -> CleanResult:
    """Candidates: partial directories whose ``opened_at`` is older than ``older_than`` (a
    ``spec.json`` that does not parse is never one: nothing unreadable is deleted), and temp files
    under the kind at least that old by mtime. Committed artifacts, ledgers and foreign
    directories are never touched; without ``apply`` the candidates are only listed."""
    threshold = utc_now() - older_than
    root = artifacts_root(data_root)
    dirs: list[Path] = []
    tmps: list[Path] = []
    for kind_dir in _kind_dirs(data_root, kind):
        dirs += [d for d in _partial_dirs(kind_dir) if _opened_before(d, threshold)]
        tmps += [
            p
            for p in sorted(kind_dir.rglob("*"))
            if p.is_file()
            and is_tmp_name(p.name)
            and not any(p.is_relative_to(d) for d in dirs)
            and _modified_before(p, threshold)
        ]
    candidates = [p.relative_to(root).as_posix() for p in dirs + tmps]
    removed: list[str] = []
    if apply:
        for d in dirs:
            if (d / MANIFEST).exists():  # re-checked at the moment of removal
                continue
            shutil.rmtree(d)
            removed.append(d.relative_to(root).as_posix())
        for p in tmps:
            p.unlink(missing_ok=True)
            removed.append(p.relative_to(root).as_posix())
    return CleanResult(candidates, removed)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/artifact tests/unit/artifact && uv run pytest tests/unit/artifact -o addopts="" -q`
Expected: 全部通過。`test_clean_lists_then_removes_only_old_partials_and_temps` 裡 `tmps` 的排序：`sorted(kind_dir.rglob("*"))` 對 `Path` 排序，`.supersession…` 在 `done/…` 之前（`.` 排在字母前）。

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/artifact/clean.py tests/unit/artifact/test_clean.py
git commit -m "feat(artifact): scan 三態分類與 clean 寬限期清理" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: `cli_artifact.py`——`vcp artifact` 七個命令

**Files:**
- Create: `src/vcp/cli_artifact.py`
- Modify: `src/vcp/cli.py:11-53`（import 與 `add_typer`）
- Test: `tests/unit/test_cli_artifact.py`

**Interfaces:**
- Consumes: Task 5–9 的 `store` / `lineage` / `clean` / `ArtifactWriter`、`cli_common.run_command` / `parse_opts` / `JsonOpt` / `DataRootOpt`、`resolve_data_root`。
- Produces: `artifact_app`；`cmd=artifact.create|show|verify|lineage|status|relink|clean`，欄位如 spec §6 / §10。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/test_cli_artifact.py`

```python
"""``vcp artifact`` through the CLI: VERDICT lines, exit codes, --json."""

import json

from typer.testing import CliRunner

from vcp.cli import app
from vcp.core.paths import artifact_dir

runner = CliRunner()


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _json(r) -> dict:
    return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))


def _run(*args):
    return runner.invoke(app, ["artifact", *args])


def _create(tmp_path, artifact_id="r1", *extra):
    src = tmp_path / f"{artifact_id}.txt"
    src.write_text(artifact_id, encoding="utf-8")
    return _run("create", "--kind", "receipt", "--id", artifact_id, "--file", str(src), *extra)


def test_create_show_and_the_second_write(roots, tmp_path):
    plan = roots.data / "plan.json"
    plan.write_bytes(b"plan")
    r = _create(
        tmp_path,
        "r1",
        "--seed",
        "42",
        "--param",
        "fold=1",
        "--input",
        f"plan={plan}",
        "--id-pattern",
        r"r(?P<fold>\d)",
        "--dataset",
        "knee",
        "--json",
    )
    assert r.exit_code == 0, r.output
    doc = _json(r)
    assert doc["status"] == "OK" and doc["fields"]["kind"] == "receipt"
    assert doc["fields"]["id"] == "r1" and doc["fields"]["files"] == 1 and doc["fields"]["bytes"] == 2
    assert doc["result"]["spec"]["params"] == {"fold": "1"} and doc["result"]["spec"]["seed"] == 42
    assert doc["result"]["spec"]["inputs"][0]["path"] == "plan.json"
    assert len(doc["result"]["spec"]["inputs"][0]["sha256"]) == 64
    assert (artifact_dir(roots.data, "receipt", "r1") / "manifest.json").is_file()
    r = _create(tmp_path, "r1")
    v = _verdict(r.output)
    assert r.exit_code == 1 and 'reason="ValidationFailed: exists: ' in v and "kind=receipt id=r1" in v
    r = _run("show", "--kind", "receipt", "--id", "r1")
    assert r.exit_code == 0 and "files=1" in _verdict(r.output) and "file r1.txt: 2 bytes" in r.output
    assert "input plan:" in r.output and "dataset=knee" in r.output
    r = _run("show", "--kind", "receipt", "--id", "nope")
    assert r.exit_code == 1 and "not_found: artifact receipt/nope" in _verdict(r.output)
    r = _run("create", "--kind", "receipt", "--id", "r2", "--file", str(tmp_path / "missing.txt"))
    assert r.exit_code == 1 and "not_found:" in _verdict(r.output)
    assert not artifact_dir(roots.data, "receipt", "r2").exists()
    r = _run("create", "--kind", "receipt", "--id", "r3")
    assert r.exit_code == 1 and "at least one --file" in _verdict(r.output)
    r = _create(tmp_path, "r4", "--seed", "43", "--id-pattern", r"r(?P<seed>\d)")
    assert r.exit_code == 1 and "reads '4' from the id but the spec says '43'" in r.output
    assert not artifact_dir(roots.data, "receipt", "r4").exists()
    r = _run("create", "--kind", "receipt", "--id", "r5", "--file", f"renamed.txt={tmp_path / 'r1.txt'}", "--input", "noequals")
    assert r.exit_code == 1 and "--input expects name=PATH" in _verdict(r.output)
    r = _run("create", "--kind", "receipt", "--id", "r5", "--file", f"renamed.txt={tmp_path / 'r1.txt'}", "--json")
    assert r.exit_code == 0 and [f["name"] for f in _json(r)["result"]["files"]] == ["renamed.txt"]


def test_verify_relink_and_lineage(roots, tmp_path):
    assert _create(tmp_path, "r1").exit_code == 0
    r = _create(tmp_path, "r2", "--supersedes", "r1", "--reason", "seed fix")
    assert r.exit_code == 0 and "supersedes=r1" in _verdict(r.output)
    assert _create(tmp_path, "r3", "--supersedes", "r1", "--reason", "fork").exit_code == 0
    r = _run("create", "--kind", "receipt", "--id", "r4", "--file", str(tmp_path / "r1.txt"), "--supersedes", "r1")
    assert r.exit_code == 1 and "go together" in r.output
    r = _run("verify", "--kind", "receipt", "--id", "r2", "--json")
    assert r.exit_code == 0
    assert _json(r)["fields"] == {"kind": "receipt", "id": "r2", "mismatch": 0, "missing": 0, "extra": 0, "unlinked": 0}
    d = artifact_dir(roots.data, "receipt", "r2")
    (d / "extra.bin").write_bytes(b"x")
    r = _run("verify", "--kind", "receipt", "--id", "r2")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and 'reason="extra: extra.bin"' in v and "extra=1" in v
    (d / "extra.bin").unlink()
    (d / "r2.txt").write_text("tampered", encoding="utf-8")
    r = _run("verify", "--kind", "receipt", "--id", "r2")
    assert r.exit_code == 1 and 'reason="mismatch: r2.txt"' in _verdict(r.output)
    (d / "r2.txt").write_text("r2", encoding="utf-8")
    log = roots.data / "artifacts" / "receipt" / "supersession.jsonl"
    log.write_text("", encoding="utf-8", newline="\n")
    r = _run("verify", "--kind", "receipt", "--id", "r2")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "unlinked=1" in v and "relink" in r.output
    r = _run("relink", "--kind", "receipt", "--id", "r2")
    assert r.exit_code == 0 and "appended=1" in _verdict(r.output)
    r = _run("relink", "--kind", "receipt", "--id", "r2")
    assert r.exit_code == 0 and "appended=0" in _verdict(r.output)
    r = _run("verify", "--kind", "receipt", "--id", "r2")
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output)
    r = _run("show", "--kind", "receipt", "--id", "r1")
    assert "superseded_by=r2,r3" in _verdict(r.output)
    r = _run("lineage", "--kind", "receipt", "--id", "r1", "--json")
    assert r.exit_code == 0
    doc = _json(r)
    assert doc["status"] == "WARN" and doc["fields"]["forks"] == 1 and doc["fields"]["heads"] == "r2,r3"
    assert doc["fields"]["root"] == "r1" and doc["fields"]["depth"] == 1
    assert [m["spec"]["id"] for m in doc["result"]["successors"]] == ["r2", "r3"]
    r = _run("lineage", "--kind", "receipt", "--id", "r2")
    assert r.exit_code == 0 and "depth=2" in _verdict(r.output) and "heads=r2" in _verdict(r.output)
    assert "seed fix" in r.output


def test_status_and_clean(roots, tmp_path):
    r = _run("status")
    assert r.exit_code == 0 and "kinds=0" in _verdict(r.output) and "no artifacts" in r.output
    assert _create(tmp_path, "r1").exit_code == 0
    half = artifact_dir(roots.data, "receipt", "half")
    half.mkdir()
    (half / "spec.json").write_text(
        json.dumps(
            {
                "spec": {"kind": "receipt", "id": "half"},
                "opened_at": "2026-09-01T00:00:00.000Z",
                "vcp_version": "t",
            }
        ),
        encoding="utf-8",
    )
    tmp = artifact_dir(roots.data, "receipt", "r1") / ".r1.txt.0a1b2c3d.tmp"
    tmp.write_bytes(b"t")
    r = _run("status", "--json")
    assert r.exit_code == 0
    doc = _json(r)
    assert doc["status"] == "WARN"
    assert doc["fields"] == {"kinds": 1, "complete": 1, "partial": 1, "unlinked": 0, "forks": 0, "foreign": 0}
    assert doc["result"]["kinds"][0]["partial"][0]["id"] == "half"
    r = _run("status")
    assert "partial receipt/half" in r.output and "opened_at=2026-09-01T00:00:00.000Z" in r.output
    r = _run("clean", "--kind", "receipt")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "candidates=1 removed=0" in v
    assert "candidate receipt/half" in r.output and "--apply" in r.output and half.is_dir()
    r = _run("clean", "--older-than", "0", "--apply", "--json")
    doc = _json(r)
    assert r.exit_code == 0 and doc["status"] == "OK" and doc["fields"]["removed"] == 2
    assert doc["result"]["removed"] == ["receipt/half", "receipt/r1/.r1.txt.0a1b2c3d.tmp"]
    assert not half.exists() and not tmp.exists()
    assert (artifact_dir(roots.data, "receipt", "r1") / "manifest.json").is_file()
    r = _run("clean", "--older-than", "1w")
    assert r.exit_code == 1 and "--older-than" in _verdict(r.output)
    r = _run("status", "--kind", "nothing")
    assert r.exit_code == 0 and "kinds=0" in _verdict(r.output)
    r = _run("clean")
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output) and "nothing to clean" in r.output


def test_artifact_failure_preserves_command_identity(roots):
    r = _run("verify", "--kind", "receipt", "--id", "nope")
    v = _verdict(r.output)
    assert r.exit_code == 1 and v.startswith("VERDICT cmd=artifact.verify status=FAIL reason=")
    assert "kind=receipt id=nope" in v
    r = _run("lineage", "--kind", "receipt", "--id", "nope")
    assert r.exit_code == 1 and "kind=receipt id=nope" in _verdict(r.output)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/test_cli_artifact.py -o addopts="" -q`
Expected: FAIL（`No such command 'artifact'`，exit 2）

- [ ] **Step 3: 寫 `src/vcp/cli_artifact.py`**

```python
"""``vcp artifact``: immutable artifacts (spec 6). Every command ends with a VERDICT line."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from vcp.artifact import store
from vcp.artifact.clean import clean, parse_age, scan
from vcp.artifact.lineage import lineage as lineage_of
from vcp.artifact.schema import ArtifactManifest, ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.cli_common import CmdResult, DataRootOpt, JsonOpt, parse_opts, run_command
from vcp.core.errors import ValidationFailed
from vcp.core.log import FieldValue, Status
from vcp.core.paths import resolve_data_root

artifact_app = typer.Typer(no_args_is_help=True, help="immutable artifact commands")

KindOpt = Annotated[str, typer.Option("--kind", help="artifact kind (a path segment)")]
IdOpt = Annotated[str, typer.Option("--id", help="artifact id (claimed once, never rewritten)")]
KindFilterOpt = Annotated[str | None, typer.Option("--kind", help="one kind only")]


def _ident(kind: str, artifact_id: str) -> dict[str, FieldValue]:
    return {"kind": kind, "id": artifact_id}


def _file_arg(item: str) -> tuple[str, Path]:
    """``PATH`` (named by its basename) or ``NAME=PATH`` (also for a path that contains ``=``)."""
    name, sep, path = item.partition("=")
    if sep and name and path:
        return name, Path(path).expanduser()
    return Path(item).name, Path(item).expanduser()


def _input_arg(item: str) -> InputRef:
    name, sep, path = item.partition("=")
    if not sep or not name or not path:
        raise ValidationFailed(f"--input expects name=PATH, got {item!r}")
    return InputRef(name=name, path=str(Path(path).expanduser().resolve()))


def _payload(m: ArtifactManifest) -> dict[str, Any]:
    return m.model_dump(mode="json")


def _short(sha: str | None) -> str:
    return (sha or "-")[:12]


@artifact_app.command("create")
def create_cmd(
    kind: KindOpt,
    artifact_id: IdOpt,
    file: Annotated[
        list[str] | None, typer.Option("--file", help="PATH or NAME=PATH (repeatable)")
    ] = None,
    dataset: Annotated[str | None, typer.Option("--dataset")] = None,
    plan: Annotated[str | None, typer.Option("--plan", help="plan id")] = None,
    seed: Annotated[int | None, typer.Option("--seed")] = None,
    param: Annotated[
        list[str] | None, typer.Option("--param", help="key=value (repeatable)")
    ] = None,
    input_: Annotated[
        list[str] | None,
        typer.Option("--input", help="name=PATH (repeatable; hashed now and again at commit)"),
    ] = None,
    supersedes: Annotated[
        str | None, typer.Option("--supersedes", help="id of the artifact this one replaces")
    ] = None,
    reason: Annotated[
        str | None, typer.Option("--reason", help="why it supersedes (with --supersedes)")
    ] = None,
    notes: Annotated[str, typer.Option("--notes")] = "",
    id_pattern: Annotated[
        str | None,
        typer.Option(
            "--id-pattern",
            help="regex the id must match; named groups seed / dataset / plan_id / <param> "
            "must equal those fields",
        ),
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
) -> None:
    """Seal existing files into a new artifact: claim the id, copy the files in, commit."""

    def fn() -> CmdResult:
        try:
            spec = ArtifactSpec(
                kind=kind,
                id=artifact_id,
                dataset=dataset,
                plan_id=plan,
                seed=seed,
                params=parse_opts(param, "--param"),
                inputs=[_input_arg(i) for i in input_ or []],
                id_pattern=id_pattern,
                supersedes=supersedes,
                supersedes_reason=reason,
                notes=notes,
            )
        except ValidationError as e:
            raise ValidationFailed(str(e)) from e
        files = [_file_arg(f) for f in file or []]
        if not files:
            raise ValidationFailed("create needs at least one --file")
        for name, path in files:
            if not path.is_file():
                raise ValidationFailed(f"not_found: --file {path}", fields={"file": name})
        root = resolve_data_root(data_root)
        with ArtifactWriter.create(spec, data_root=root) as art:
            for name, path in files:
                art.add_file(name, path)
            manifest = art.commit()
        fields: dict[str, FieldValue] = {
            **_ident(kind, artifact_id),
            "files": len(manifest.files),
            "bytes": manifest.bytes,
            "dir": str(art.dir),
        }
        if supersedes is not None:
            fields["supersedes"] = supersedes
        human = [
            f"artifact {kind}/{artifact_id} committed: {len(manifest.files)} files, "
            f"{manifest.bytes} bytes -> {art.dir}"
        ]
        return "OK", fields, _payload(manifest), human

    run_command("artifact.create", json_mode, data_root, fn, context=_ident(kind, artifact_id))


@artifact_app.command("show")
def show_cmd(
    kind: KindOpt, artifact_id: IdOpt, json_mode: JsonOpt = False, data_root: DataRootOpt = None
) -> None:
    """A committed artifact's manifest: spec, inputs, files, what it supersedes and what
    supersedes it (read-only)."""

    def fn() -> CmdResult:
        root = resolve_data_root(data_root)
        m = store.load_manifest(root, kind, artifact_id)
        lin = lineage_of(root, kind, artifact_id)
        superseded_by = [s.spec.id for s in lin.successors if s.spec.supersedes == artifact_id]
        s = m.spec
        fields: dict[str, FieldValue] = {
            **_ident(kind, artifact_id),
            "files": len(m.files),
            "bytes": m.bytes,
            "created_at": m.created_at,
            "vcp_version": m.vcp_version,
        }
        if s.supersedes is not None:
            fields["supersedes"] = s.supersedes
        if superseded_by:
            fields["superseded_by"] = ",".join(superseded_by)
        human = [
            f"{kind}/{artifact_id}  created_at={m.created_at}  vcp_version={m.vcp_version}",
            f"dataset={s.dataset or '-'}  plan_id={s.plan_id or '-'}  "
            f"seed={'-' if s.seed is None else s.seed}  params={s.params or '{}'}",
        ]
        human += [f"input {i.name}: {_short(i.sha256)}  {i.path or '-'}" for i in s.inputs]
        human += [f"file {f.name}: {f.bytes} bytes  {_short(f.sha256)}" for f in m.files]
        if s.supersedes is not None:
            human.append(
                f"supersedes {s.supersedes} ({s.supersedes_reason}); "
                f"its manifest sha {_short(m.supersedes_sha256)}"
            )
        if superseded_by:
            human.append(f"superseded by {', '.join(superseded_by)}")
        if s.notes:
            human.append(f"notes: {s.notes}")
        payload = {**_payload(m), "superseded_by": superseded_by}
        return "OK", fields, payload, human

    run_command("artifact.show", json_mode, data_root, fn, context=_ident(kind, artifact_id))


@artifact_app.command("verify")
def verify_cmd(
    kind: KindOpt, artifact_id: IdOpt, json_mode: JsonOpt = False, data_root: DataRootOpt = None
) -> None:
    """Re-hash every file, list files the manifest never named, check the supersession row."""

    def fn() -> CmdResult:
        root = resolve_data_root(data_root)
        res = store.verify(root, kind, artifact_id)
        fields: dict[str, FieldValue] = {}
        if res.mismatch:
            fields["reason"] = f"mismatch: {','.join(res.mismatch)}"
        elif res.missing:
            fields["reason"] = f"missing: {','.join(res.missing)}"
        elif res.extra:
            fields["reason"] = f"extra: {','.join(res.extra)}"
        fields.update(
            {
                **_ident(kind, artifact_id),
                "mismatch": len(res.mismatch),
                "missing": len(res.missing),
                "extra": len(res.extra),
                "unlinked": int(res.unlinked),
            }
        )
        status: Status = "FAIL" if res.failed else ("WARN" if res.unlinked else "OK")
        human = [f"mismatch: {n}" for n in res.mismatch]
        human += [f"missing: {n}" for n in res.missing]
        human += [f"extra: {n}" for n in res.extra]
        if res.unlinked:
            human.append(
                f"unlinked: {kind}/{artifact_id} supersedes another artifact but "
                "supersession.jsonl has no row for it; run `vcp artifact relink`"
            )
        if not human:
            human = [f"{kind}/{artifact_id} verified"]
        payload = {
            "mismatch": res.mismatch,
            "missing": res.missing,
            "extra": res.extra,
            "unlinked": res.unlinked,
        }
        return status, fields, payload, human

    run_command("artifact.verify", json_mode, data_root, fn, context=_ident(kind, artifact_id))


@artifact_app.command("lineage")
def lineage_cmd(
    kind: KindOpt, artifact_id: IdOpt, json_mode: JsonOpt = False, data_root: DataRootOpt = None
) -> None:
    """Root → id → successors; heads are the ends nothing supersedes; forks warn (read-only)."""

    def fn() -> CmdResult:
        root = resolve_data_root(data_root)
        lin = lineage_of(root, kind, artifact_id)
        fields: dict[str, FieldValue] = {
            **_ident(kind, artifact_id),
            "root": lin.chain[0].spec.id,
            "depth": len(lin.chain),
            "heads": ",".join(lin.heads),
            "forks": lin.forks,
        }
        status: Status = "WARN" if lin.forks else "OK"

        def line(m: ArtifactManifest, prefix: str) -> str:
            text = f"{prefix}{m.spec.id}  created_at={m.created_at}  files={len(m.files)}"
            if m.spec.supersedes is not None:
                text += f"  supersedes={m.spec.supersedes} ({m.spec.supersedes_reason})"
            return text

        human = [line(m, "  " * i) for i, m in enumerate(lin.chain)]
        human += [line(m, "-> ") for m in lin.successors]
        if lin.forks:
            human.append(
                f"forks={lin.forks}: more than one artifact supersedes the same one; "
                f"heads={','.join(lin.heads)}"
            )
        payload = {
            "chain": [_payload(m) for m in lin.chain],
            "successors": [_payload(m) for m in lin.successors],
            "heads": lin.heads,
            "forks": lin.forks,
        }
        return status, fields, payload, human

    run_command("artifact.lineage", json_mode, data_root, fn, context=_ident(kind, artifact_id))


@artifact_app.command("status")
def status_cmd(
    kind: KindFilterOpt = None, json_mode: JsonOpt = False, data_root: DataRootOpt = None
) -> None:
    """Every kind: complete / partial / unlinked / forks / foreign directories (read-only)."""

    def fn() -> CmdResult:
        kinds = scan(resolve_data_root(data_root), kind)
        partial = sum(len(k.partial) for k in kinds)
        unlinked = sum(len(k.unlinked) for k in kinds)
        forks = sum(k.forks for k in kinds)
        fields: dict[str, FieldValue] = {
            "kinds": len(kinds),
            "complete": sum(len(k.complete) for k in kinds),
            "partial": partial,
            "unlinked": unlinked,
            "forks": forks,
            "foreign": sum(len(k.foreign) for k in kinds),
        }
        status: Status = "WARN" if partial or unlinked or forks else "OK"
        human = [
            f"{k.kind}  complete={len(k.complete)}  partial={len(k.partial)}  "
            f"unlinked={len(k.unlinked)}  forks={k.forks}  foreign={len(k.foreign)}"
            for k in kinds
        ]
        for k in kinds:
            human += [
                f"  partial {k.kind}/{p.id}  opened_at={p.opened_at or '?'}  "
                f"failure={p.failure or '-'}"
                for p in k.partial
            ]
            human += [
                f"  unlinked {k.kind}/{i}  (run `vcp artifact relink --kind {k.kind} --id {i}`)"
                for i in k.unlinked
            ]
        if not kinds:
            human = ["no artifacts"]
        return status, fields, {"kinds": [asdict(k) for k in kinds]}, human

    context: dict[str, FieldValue] | None = {"kind": kind} if kind is not None else None
    run_command("artifact.status", json_mode, data_root, fn, context=context)


@artifact_app.command("relink")
def relink_cmd(
    kind: KindOpt, artifact_id: IdOpt, json_mode: JsonOpt = False, data_root: DataRootOpt = None
) -> None:
    """Append the supersession row a committed manifest implies when the ledger lacks it."""

    def fn() -> CmdResult:
        appended = store.relink(resolve_data_root(data_root), kind, artifact_id)
        fields: dict[str, FieldValue] = {**_ident(kind, artifact_id), "appended": int(appended)}
        human = [
            "appended the supersession row"
            if appended
            else "nothing to append (already linked, or supersedes nothing)"
        ]
        return "OK", fields, {"appended": appended}, human

    run_command("artifact.relink", json_mode, data_root, fn, context=_ident(kind, artifact_id))


@artifact_app.command("clean")
def clean_cmd(
    kind: KindFilterOpt = None,
    older_than: Annotated[str, typer.Option("--older-than", help="N[m|h|d] or 0")] = "24h",
    apply: Annotated[bool, typer.Option("--apply", help="remove; without it, only list")] = False,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
) -> None:
    """Remove partial directories past the grace period and leftover temp files; never a
    committed artifact, a ledger or a foreign directory."""

    def fn() -> CmdResult:
        res = clean(
            resolve_data_root(data_root), kind=kind, older_than=parse_age(older_than), apply=apply
        )
        fields: dict[str, FieldValue] = {
            "older_than": older_than,
            "candidates": len(res.candidates),
            "removed": len(res.removed),
        }
        status: Status = "WARN" if len(res.removed) < len(res.candidates) else "OK"
        human = [
            f"{'removed' if c in res.removed else 'candidate'} {c}" for c in res.candidates
        ] or ["nothing to clean"]
        if res.candidates and not apply:
            human.append("listed only: pass --apply to remove")
        return status, fields, {"candidates": res.candidates, "removed": res.removed}, human

    context: dict[str, FieldValue] | None = {"kind": kind} if kind is not None else None
    run_command("artifact.clean", json_mode, data_root, fn, context=context)
```

**改 `src/vcp/cli.py`**：import 區加 `from vcp.cli_artifact import artifact_app`（ruff isort 會排到 `vcp.cli_backup` 之前）；`app.add_typer(data_app, name="data")` 之後、`app.add_typer(backup_app, name="backup")` 之前加 `app.add_typer(artifact_app, name="artifact")`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/cli_artifact.py src/vcp/cli.py tests/unit/test_cli_artifact.py && uv run pytest tests/unit/test_cli_artifact.py tests/unit/test_cli.py -o addopts="" -q`
Expected: 全部通過；`uv run vcp artifact --help` 列出七個命令。

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/cli_artifact.py src/vcp/cli.py tests/unit/test_cli_artifact.py
git commit -m "feat(cli): vcp artifact create / show / verify / lineage / status / relink / clean" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: 端到端劇本、回歸門檻列、真資料唯讀測試

**Files:**
- Create: `tests/unit/test_e2e_artifact.py`、`tests/integration/test_artifact_status.py`
- Modify: `tests/unit/test_regression_gate.py`（`GATE` 尾端加一列）

**Interfaces:**
- Consumes: Task 4–10 全部。
- Produces: 無新介面；門檻列名 `wave 1a (VCP-005, VCP-007)`。

- [ ] **Step 1: 寫端到端測試** `tests/unit/test_e2e_artifact.py`

```python
"""The artifact layer end to end (spec 13): a selection job that reserves a numpy array and
writes a receipt, dies, is refused a retry under the same id, is cleaned, commits, is reused,
is superseded under a new id, is tampered with, loses its ledger row and is relinked; a secret
in the job's exception never reaches disk."""

import json

import numpy as np
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.cli import app
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir

runner = CliRunner()
SECRET = "fakesecretfakesecretfakesecret1234"
PATTERN = r"six-slot-v2-(?P<plan_id>[a-z0-9-]+)-s(?P<seed>\d+)(?:-r\d+)?"
A = "six-slot-v2-fixed-v1-s42"
B = "six-slot-v2-fixed-v1-s42-r2"
C = "six-slot-v2-fixed-v1-s42-r3"


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _json(r) -> dict:
    return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))


def _art(*args):
    return runner.invoke(app, ["artifact", *args])


def _job(data_root, artifact_id, plan, *, seed, supersedes=None, reason=None, die=False):
    spec = ArtifactSpec(
        kind="selection",
        id=artifact_id,
        dataset="knee",
        plan_id="fixed-v1",
        seed=seed,
        params={"model": "six-slot-v2"},
        inputs=[InputRef(name="plan", path=str(plan))],
        id_pattern=PATTERN,
        supersedes=supersedes,
        supersedes_reason=reason,
    )
    with ArtifactWriter.create(spec, data_root=data_root) as art:
        out = art.reserve("features.npy")
        np.save(out, np.arange(6, dtype=np.float32).reshape(2, 3) * seed)
        if die:
            raise RuntimeError(f"platform rejected upload token={SECRET}")
        art.write_json("receipt.json", {"seed": seed, "rows": 2})
        return art.commit()


def test_selection_job_story(roots):
    plan = roots.data / "plan.json"
    plan.write_bytes(b"plan v1")
    # 1. VCP-007: the id says s42, the job was handed seed 43 -- refused before anything exists
    with pytest.raises(ValidationError, match="reads '42' from the id but the spec says '43'"):
        _job(roots.data, A, plan, seed=43)
    assert not artifact_dir(roots.data, "selection", A).exists()
    # 2. the job dies after reserving its array: a partial with a redacted failure record
    with pytest.raises(RuntimeError, match="platform rejected"):
        _job(roots.data, A, plan, seed=42, die=True)
    d_a = artifact_dir(roots.data, "selection", A)
    assert store.is_partial(roots.data, "selection", A) and (d_a / "features.npy").is_file()
    failure = json.loads((d_a / "failure.json").read_text(encoding="utf-8"))
    assert failure["exception"] == "RuntimeError"
    assert failure["message"] == "platform rejected upload token=<redacted>"
    r = _art("status")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "partial=1" in v
    assert f"partial selection/{A}" in r.output and "failure=RuntimeError" in r.output
    with pytest.raises(ValidationFailed, match="^exists: "):
        _job(roots.data, A, plan, seed=42)
    r = _art("clean", "--older-than", "0", "--apply")
    assert r.exit_code == 0 and "removed=1" in _verdict(r.output) and not d_a.exists()
    # 3. the job commits; the same spec is a cache hit, a different one is not
    a = _job(roots.data, A, plan, seed=42)
    assert store.reuse(a.spec, roots.data) == a
    other = a.spec.model_copy(update={"params": {"model": "six-slot-v3"}})
    with pytest.raises(IntegrityError, match=r"^spec_mismatch: .*\(params\)"):
        store.reuse(other, roots.data)
    with pytest.raises(ValidationFailed, match="^exists: "):
        _job(roots.data, A, plan, seed=42)
    # 4. the correction is a new id that supersedes A
    b = _job(roots.data, B, plan, seed=42, supersedes=A, reason="receipt overwritten (VCP-005)")
    assert b.supersedes_sha256 == sha256_file(store.manifest_path(roots.data, "selection", A))
    r = _art("lineage", "--kind", "selection", "--id", A, "--json")
    doc = _json(r)
    assert doc["status"] == "OK" and doc["fields"]["heads"] == B and doc["fields"]["forks"] == 0
    r = _art("show", "--kind", "selection", "--id", A)
    assert f"superseded_by={B}" in _verdict(r.output)
    # 5. tampered features -> FAIL; restored -> OK
    d_b = artifact_dir(roots.data, "selection", B)
    original = (d_b / "features.npy").read_bytes()
    (d_b / "features.npy").write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))
    r = _art("verify", "--kind", "selection", "--id", B)
    assert r.exit_code == 1 and 'reason="mismatch: features.npy"' in _verdict(r.output)
    (d_b / "features.npy").write_bytes(original)
    r = _art("verify", "--kind", "selection", "--id", B)
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output)
    # 6. a manifest edited after commit disagrees with the ledger
    m_path = d_b / "manifest.json"
    m_bytes = m_path.read_bytes()
    assert b'"notes": ""' in m_bytes
    m_path.write_bytes(m_bytes.replace(b'"notes": ""', b'"notes": "edited"'))
    r = _art("verify", "--kind", "selection", "--id", B)
    assert r.exit_code == 1 and 'reason="mismatch: manifest.json"' in _verdict(r.output)
    m_path.write_bytes(m_bytes)
    # 7. the crash window after the manifest: ledger row missing -> WARN -> relink -> OK
    log = roots.data / "artifacts" / "selection" / "supersession.jsonl"
    log.write_text("", encoding="utf-8", newline="\n")
    r = _art("verify", "--kind", "selection", "--id", B)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "unlinked=1" in v
    r = _art("status")
    assert "status=WARN" in _verdict(r.output) and "unlinked=1" in _verdict(r.output)
    r = _art("relink", "--kind", "selection", "--id", B)
    assert "appended=1" in _verdict(r.output)
    r = _art("verify", "--kind", "selection", "--id", B)
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output)
    r = _art("status")
    assert "status=OK" in _verdict(r.output)
    # 8. A is intact; a second correction of A is a visible fork
    r = _art("verify", "--kind", "selection", "--id", A)
    assert "status=OK" in _verdict(r.output)
    _job(roots.data, C, plan, seed=42, supersedes=A, reason="fork on purpose")
    r = _art("lineage", "--kind", "selection", "--id", A)
    assert "status=WARN" in _verdict(r.output) and "forks=1" in _verdict(r.output)
    assert f"heads={B},{C}" in _verdict(r.output)
    # 9. privacy: the secret from step 2 is nowhere under either root
    scanned = 0
    for root in (roots.data, roots.configs):
        for p in root.rglob("*"):
            if p.is_file():
                scanned += 1
                assert SECRET.encode() not in p.read_bytes(), p
    assert scanned > 0
```

- [ ] **Step 2: 跑端到端**

Run: `uv run pytest tests/unit/test_e2e_artifact.py -o addopts="" -q`
Expected: 1 passed（若第 5 步的 `original[-1] ^ 0xFF` 讓 numpy 檔長度不變但 sha 變——這正是要的）

- [ ] **Step 3: 加門檻列**——`tests/unit/test_regression_gate.py` 的 `GATE` 最後一列之後加

```python
    (
        "wave 1a (VCP-005, VCP-007)",
        "artifact: an id is claimed at open and never rewritten; a crash before commit leaves no "
        "manifest; a supersedes target must exist, be committed and verify; clean never reaches "
        "a committed artifact; an id_pattern group must equal its spec field",
        {
            "tests/unit/artifact/test_writer.py": [
                "test_open_claims_the_id_and_writes_spec_json",
                "test_manifest_publish_failure_leaves_a_partial",
            ],
            "tests/unit/artifact/test_lineage.py": [
                "test_supersession_is_checked_at_open_and_commit_and_indexed",
            ],
            "tests/unit/artifact/test_clean.py": [
                "test_clean_lists_then_removes_only_old_partials_and_temps",
                "test_clean_never_touches_what_it_cannot_read_or_what_committed_meanwhile",
            ],
            "tests/unit/artifact/test_schema.py": ["test_id_pattern_groups_must_equal_the_fields"],
            "tests/unit/core/test_atomic.py": [
                "test_second_write_is_refused_and_the_original_is_untouched",
                "test_the_four_write_once_sites_use_the_primitive",
            ],
            "tests/unit/test_e2e_artifact.py": ["test_selection_job_story"],
        },
    ),
```

- [ ] **Step 4: 寫真資料唯讀測試** `tests/integration/test_artifact_status.py`

```python
"""``artifacts/`` under the real data root is only listed: nothing is written under it. Uses
``scan`` directly (the CLI's logger would write under ``<data_root>/logs/``)."""

from __future__ import annotations

import pytest

from vcp.artifact.clean import scan
from vcp.core.paths import artifacts_root

pytestmark = pytest.mark.realdata


def _tree(root):
    if not root.is_dir():
        return None
    return sorted(
        (p.relative_to(root).as_posix(), p.stat().st_size) for p in root.rglob("*") if p.is_file()
    )


def test_scan_lists_the_real_artifacts_root_without_writing(real_roots):
    if not real_roots.data.is_dir():
        pytest.skip(f"real data root {real_roots.data} is absent")
    root = artifacts_root(real_roots.data)
    before = _tree(root)
    for k in scan(real_roots.data):
        assert k.kind and all(isinstance(i, str) for i in k.complete)
    assert _tree(root) == before
```

- [ ] **Step 5: 跑全套並 commit**

Run: `uv run ruff format tests/unit/test_e2e_artifact.py tests/unit/test_regression_gate.py tests/integration/test_artifact_status.py && uv run pytest tests/unit/test_regression_gate.py tests/unit/test_e2e_artifact.py -o addopts="" -q && uv run pytest tests/integration/test_artifact_status.py -o addopts="" -q -m realdata`
Expected: 門檻與端到端通過；整合測試 1 passed 或 1 skipped（看本機有沒有 `C:/vcp-data`）

```bash
uv run ruff check . && uv run ruff format --check .
git add tests/unit/test_e2e_artifact.py tests/unit/test_regression_gate.py tests/integration/test_artifact_status.py
git commit -m "test(artifact): 選型 job 端到端劇本、回歸門檻列、真資料唯讀掃描" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: 文件、spec §16 補充決定、release 0.4.0

**Files:**
- Modify: `README.md`（備份審計節之後、「匯入器與 `rows_read` 的語意」之前）、`CLAUDE.md` 與 `AGENTS.md`（「路徑」節最後、「常用命令」節最後）、`docs/handover/HANDOVER.md:23`、`docs/superpowers/specs/2026-09-11-vcp-immutable-artifacts-design.md`（檔尾加 §16）、`CHANGELOG.md`（`## [0.3.0]` 之前）、`src/vcp/__init__.py`

**Interfaces:**
- Consumes: 全部。
- Produces: `__version__ == "0.4.0"`；`tests/unit/test_package.py` 三方一致。

- [ ] **Step 1: README**——在 `## 匯入器與 \`rows_read\` 的語意` 之前插入

````markdown
## 不可變產物命令 `vcp artifact`

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp artifact create` | 把既有檔案封成一個產物：`<data_root>/artifacts/<kind>/<id>/`，`mkdir` 獨佔搶 id，逐檔 `.tmp → fsync → replace`，最後寫 `manifest.json`（有它才是產物）；`--input` 在 open 與 commit 各雜湊一次（變了 → `drift:`）；`--id-pattern` 的具名群組必須等於同名欄位（`seed` / `dataset` / `plan_id` / `--param` 的鍵） | `--kind`、`--id`、`--file PATH\|NAME=PATH`…、`--dataset`、`--plan`、`--seed`、`--param k=v`…、`--input name=PATH`…、`--supersedes OLD --reason R`、`--id-pattern RE`、`--notes` |
| `vcp artifact show` | 印 manifest：欄位、input、檔案表、`supersedes=` 與 `superseded_by=`（唯讀） | `--kind`、`--id` |
| `vcp artifact verify` | 逐檔重算 sha（`mismatch=` / `missing=`）、manifest 沒列的檔（`extra=`，含殘留 `.tmp`）、supersession 台帳（缺列 `unlinked=1` → WARN；列與 manifest 不符 → `mismatch`） | `--kind`、`--id` |
| `vcp artifact lineage` | 根 → id → 接替者；`heads=` 是沒被接替的末端，`forks=` > 0 → WARN（唯讀） | `--kind`、`--id` |
| `vcp artifact status` | 每個 kind 的 `complete=` / `partial=`（有 `spec.json` 沒 manifest；附 `opened_at` 與 `failure.json` 的例外類別）/ `unlinked=` / `forks=` / `foreign=`（沒有 `spec.json` 的目錄，不碰）（唯讀） | `--kind` |
| `vcp artifact relink` | manifest 有 `supersedes` 而台帳缺列（manifest 之後、台帳之前崩潰）→ 從 manifest 補一列；冪等 `appended=0\|1` | `--kind`、`--id` |
| `vcp artifact clean` | 列出（`--apply` 才移除）`opened_at` 早於 `--older-than` 的半途目錄與同樣老的 `.<name>.<nonce>.tmp`；有 manifest 的產物、台帳、外來目錄永不碰；讀不到 `spec.json` 的目錄不列 | `--kind`、`--older-than N{m\|h\|d}`（預設 `24h`，`0` 可）、`--apply` |

程式產生的產物走 Python API：

```python
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter

spec = ArtifactSpec(
    kind="selection", id="six-slot-v2-s42", seed=42, id_pattern=r"six-slot-v2-s(?P<seed>\d+)",
    inputs=[InputRef(name="plan", path="configs/datasets/knee/splits/fixed-v1.json")],
)
with ArtifactWriter.create(spec, data_root=root) as art:
    art.write_json("receipt.json", payload)   # write_text / write_bytes 也有
    art.add_file("weights.pt", src_path)      # 串流複製並雜湊
    out = art.reserve("features.npy")         # 回傳最終路徑，自己用 numpy 寫；commit 時雜湊
    manifest = art.commit()                   # 寫 manifest.json = commit；之後任何寫入 → closed:
```

`vcp.artifact.store.reuse(spec, root)` 只在完整 spec 逐欄相等（含每個 input 現算的 sha，`notes` 除外）才回傳既有 manifest，否則 `spec_mismatch:`——目錄存在不等於可重用。例外離開 `with` 會留下半途目錄與經 redact 的 `failure.json`；同一 id 不能再開，修正一律新 id + `supersedes`（open 查舊產物存在且已 commit，commit 驗逐檔 sha 並記其 manifest sha，之後 append `artifacts/<kind>/supersession.jsonl`）。manifest / `spec.json` / 台帳只含路徑、sha、大小、時戳、build string 與結構化參數——不放憑證。
````

- [ ] **Step 2: CLAUDE.md 與 AGENTS.md**（兩檔同樣的兩段）——「路徑」節最後加

```markdown
- `artifacts/<kind>/<id>/` 是不可變產物：`spec.json`（open 時寫）、檔案們、`manifest.json`（commit 點；**有它才是產物**）、`failure.json`（例外離開時）；`artifacts/<kind>/supersession.jsonl` 只增。同 id 不能重開；修正用新 id + `supersedes`。`vcp artifact clean` 只移除超過寬限期的半途目錄與 `.tmp`。vcp 自己的四個寫一次檔（split plan、預登記 yaml、融合配方、backup manifest）與產物的每個檔都經 `vcp.core.atomic.write_once`。
```

「常用命令」節最後加

```markdown
- `uv run vcp artifact create --kind K --id I --file PATH… [--input name=PATH…] [--supersedes OLD --reason R] [--id-pattern RE]` / `uv run vcp artifact verify --kind K --id I` / `uv run vcp artifact lineage --kind K --id I` / `uv run vcp artifact status [--kind K]`（唯讀）/ `uv run vcp artifact clean [--older-than 24h] [--apply]`；程式內用 `vcp.artifact.writer.ArtifactWriter`、重用用 `vcp.artifact.store.reuse`
```

`docs/handover/HANDOVER.md` 第 23 行（`- 版本：`）改成

```markdown
- 版本：`0.4.0`（tag `v0.4.0`，2026-09-11）= 稽核 Wave 1a（不可變產物層 `vcp artifact` + `core/atomic.write_once`）；`0.3.0` = Wave 0（prereg SHA 綁定、foreign 狀態刷新、回歸門檻）；`0.2.0` 是第一個有 tag 的 release（同日）。規則與發版步驟在 `CHANGELOG.md` 表頭。`0.2.0` 之前 240 個 commit 都宣告 `0.1.0` 且無 tag——RSNA 早期產物裡的 `"vcp_version": "0.1.0"` 回推不到單一 commit；0.2.0 起產物記 `版本+g<commit>[.dirty]`。下一步是 Wave 1b（角色範圍存取與收據，VCP-001/002/003）、1c（程式碼快照與授權，VCP-004/006）；稽核文件副本在 `docs/audits/`。
```

- [ ] **Step 3: spec §16**——在 `docs/superpowers/specs/2026-09-11-vcp-immutable-artifacts-design.md` 檔尾加

```markdown
## 16. 補充決定（實作期，Plan 8）

1. `reuse` 的關鍵字參數叫 `check_files`（`verify=` 會遮蔽同模組的 `verify`）。
2. `lineage` 回 `Lineage(chain, successors, heads, forks)`：`heads` 是 id 前向閉包的末端，`forks` 數 chain + successors 裡有 >1 接替者的節點；`head()` = `lineage().heads`。
3. `VerifyResult` 的 `mismatch` / `missing` / `extra` 是檔名清單、`unlinked: bool`；台帳列 sha 不符記成 `mismatch` 裡的 `"manifest.json"`。
4. `clean` 的 `.tmp` 也套 `--older-than`（mtime）；讀不到 `spec.json` 的半途目錄永不列入；移除每個目錄前再查一次 `manifest.json`；暫存在候選目錄之內時不另列。
5. 台帳讀寫沿用 `vcp.measure.ledger.append_row` / `read_rows`；`ledger.row_for(manifest, sha)` 是 writer 與 `relink` 共用的列建構。
6. `load_manifest` 多驗 manifest 裡的 kind / id 等於目錄的（複製來的 manifest → `mismatch:`）。
7. 保留名只在產物根目錄；`.<name>.<8 hex>.tmp` 形式的檔名也是保留名。
8. `ArtifactManifest.files` 必須已依 name 排序（validator）。
9. CLI `--file`：第一個 `=` 之前是名字，沒有就 basename；`--input` 路徑相對 CWD 解析成絕對後才交給 `resolve_inputs`；`create` 搶 id 前先確認每個 `--file` 存在。
10. `status` 的 VERDICT 欄位是各 kind 加總；明細在 human 與 `--json`。`show` 的 `superseded_by=` 只列直接接替者。
11. `write_once` 不建立跨程序互斥；Windows 不 fsync 目錄。
12. 四個既有寫一次點保留各自的存在預檢（訊息、例外類型、`fields` 不變），只換位元組落地方式。
13. 真資料整合測試只呼叫 `scan()`，並斷言 `artifacts/` 樹不變。
```

- [ ] **Step 4: 發版**——依 `CHANGELOG.md` 表頭四步的前三步

`src/vcp/__init__.py`：`__version__ = "0.4.0"`。

`CHANGELOG.md` 在 `## [0.3.0] - 2026-09-11` 之前插入：

```markdown
## [0.4.0] - 2026-09-11

稽核（2026-09-11）的 **Wave 1a**：不可變產物層——VCP-005（產物路徑可被覆寫，破壞不可變證據）與 VCP-007（ID、seed、輸出缺少共同 contract）。MINOR 的理由：新命令群 `vcp artifact`、新產物形態 `artifacts/<kind>/<id>/manifest.json` 與 `supersession.jsonl`、新 `reason=` 字彙（`partial:`、`unsafe_path:`、`reserved_name:`、`closed:`、`drift:`、`spec_mismatch:`）。

### Added
- **不可變產物**（`vcp.artifact`）：`ArtifactWriter.create(spec, data_root=…)` 以 `os.mkdir` 獨佔搶 `<data_root>/artifacts/<kind>/<id>/`，`spec.json` 記 open 時的宣告，每個檔經 `write_once`，`manifest.json` 最後寫 = commit 點（沒有它就不是產物）；例外離開留半途目錄與經 redact 的 `failure.json`；writer 不刪任何東西。`ArtifactSpec.id_pattern` 的具名群組必須等於同名欄位（RSNA「id 說 s42、CLI 收 seed 43」在 open 就擋）；`inputs` 在 open 雜湊、commit 重驗（`drift:`）。`store.reuse` 要完整 spec 逐欄相等（`notes` 除外）；`supersedes` 在 open 查舊產物存在且已 commit、commit 驗逐檔 sha 並記舊 manifest sha、之後 append `supersession.jsonl`；`lineage` / `head` 允許分叉但 WARN；`verify` 四項（mismatch / missing / extra / unlinked）；`relink` 補台帳缺列；`clean` 只移除超過寬限期的半途目錄與 `.tmp`。
- **`vcp artifact create|show|verify|lineage|status|relink|clean`**（`cmd=artifact.<name>`；失敗時保留 `kind=` / `id=`）。
- **`vcp.core.atomic`**：`write_once` / `write_once_text` / `write_once_stream`（同目錄 `.<name>.<nonce>.tmp` → fsync → 目標不存在才 `os.replace`；不是跨程序鎖）。`core/paths.py` 新增 `artifacts_root` / `artifact_dir`，`check_relative_path` 從備份層搬來；`core/config.py` 新增 `dump_yaml_text`。
- Release 回歸門檻多一列（`wave 1a (VCP-005, VCP-007)`）；真資料整合測試 `tests/integration/test_artifact_status.py`（唯讀）。

### Changed
- vcp 自己的四個寫一次檔——split plan（`save_plan`）、預登記 yaml（`create_prereg`）、融合配方（`save_recipe`）、backup manifest（`write_manifest`）——改經 `write_once_text`；訊息、例外類型、`fields` 與寫出的位元組不變。
- README、AGENTS.md / CLAUDE.md、交接文件；spec §16 補充決定。
```

然後：

```bash
uv sync --reinstall-package vcp
uv run pytest --cov=vcp
uv run ruff check . && uv run ruff format --check .
```

Expected: 全部通過、覆蓋率 ≥ 80%（現況約 96%）、`tests/unit/test_package.py` 三方一致。

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md AGENTS.md docs/handover/HANDOVER.md docs/superpowers/specs/2026-09-11-vcp-immutable-artifacts-design.md CHANGELOG.md src/vcp/__init__.py
git commit -m "chore(release): v0.4.0——稽核 Wave 1a（不可變產物層 vcp artifact 與 core/atomic）" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

發版第四步（fast-forward 到 `main`、`git tag -a v0.4.0 -m "vcp 0.4.0"`、push）由收尾流程（finishing-a-development-branch）在使用者選擇合併後執行；`main` 的 checkout 目前停在 `codex/vcp-visual-guide` 且有未提交 WIP，合併用 `git merge-base --is-ancestor main <branch> && git branch -f main <branch>`，不 checkout main。
