# vcp 來源稽核與選取列存取實作計畫（稽核 Wave 1b-2，Plan 10）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 整檔 hash 只在資料準備期做一次（`source_audit` 產物：逐列 sha 索引），之後每個 job 只驗自己讀到的列、未授權的列連讀都不讀；收據記下身分來源；release 為 `0.6.0`。

**Architecture:** 新模組 `src/vcp/data/source_audit.py`（`SourceAudit` / `IndexRow` 模型、content-addressed id、`write_source_audit` 一趟建索引、`load_source_audit` 驗證載入）；`DatasetAccess.open` 三條路（稽核 → 不掃檔；缺 → 整檔 hash；壞 → `mismatch:`），`_read` 先比列 sha；收據與 `AccessRef` 多 `identity` / `source_audit`；`vcp data import` / `validate` 自動產稽核；`train run` / `measure` / `export` WARN `source_audit=missing`，`stage` 印 `identity=`；備份角色 `source_audit`（tier 2）。所有產物走 v0.4.0 `ArtifactWriter`。

**Tech Stack:** Python 3.12、pydantic v2、typer 0.27、標準庫 `hashlib` / `json` / `re`、pytest、ruff（line-length 100）。

**Spec:** `docs/superpowers/specs/2026-09-13-vcp-source-audit-design.md`（來源 `docs/audits/2026-09-11-vcp-improvement-audit.md` VCP-002；前置 1b-1 spec `docs/superpowers/specs/2026-09-12-vcp-access-receipts-design.md`）

## Global Constraints

- 取時只能用 `vcp.core.time.utc_now()` / `stamp()`（ruff TID251）。
- 每個 CLI 命令以 `VERDICT cmd=… status=OK|WARN|FAIL|ABORT …` 收尾；`--json` 時 JSON 到 stdout、VERDICT 到 stderr；永不互動提問；新欄位加在該命令的 `fields` dict。
- 錯誤字彙：`mismatch:`（`IntegrityError`，FAIL）= 稽核 verify 失敗、`samples_hash` / `size_bytes` / `line_count` 不符、列 sha 不符、產稽核時整檔 sha ≠ card；`partial:` / `exists:` / `spec_mismatch:` 沿用 1a；WARN 欄位 `source_audit=missing`（train run / measure / export）。
- **有稽核時未授權的列不被讀取**（open 不掃 `samples.jsonl`，只 seek 授權的列）；**沒稽核時只被 hash、不被解析**（1b-1 規則不變）。**讀到的每一列先比 sha 再比 `sample_id` 再解析。**
- **稽核是內容定址的不可變產物**：id `src-<dataset>-<samples_hash 前 16 碼>`；同內容 `store.reuse`；重新匯入就是新 id；沒有 supersedes 鏈。**稽核壞掉 fail closed；稽核缺席退回整檔 hash。**
- **收據說出身分來源**（`identity: source_audit | full_hash`、`source_audit` id、`source_audit_sha256`）；**provenance 等級不變**。
- 走稽核時收據產物的 `inputs` 列稽核的 `manifest.json`，不列 `samples.jsonl`（`ArtifactWriter.create` 與 `store.reuse` 會 hash 有 path 的 inputs）；稽核產物本身**不列 inputs**（`samples_hash` 放 `params`），否則 `validate` 要掃三趟。
- import 規則：`source_audit.py` 只 import `vcp.core.*`、`vcp.artifact.*`、`vcp.data.schema`；`access.py` / `receipt.py` import `source_audit`；`source_audit.py` 永不 import `vcp.data.access.*`（`_LINE` 與 `peek_sample_id` 搬到 `source_audit.py`）。
- 隱私：稽核只含 `sample_id`、offset、length、sha、計數、時戳、build string；收據新欄位只多稽核 id 與 sha；manifest 只記 data root 相對路徑。
- pydantic `extra="forbid"`；新欄位一律有預設（v0.5.0 的收據、`run.yaml` / `train.yaml` 照讀）；檔案 utf-8、LF；測試永不碰真資料根（`roots` fixture）；覆蓋率 ≥ 80%；`uv run ruff check .` 與 `uv run ruff format --check .` 乾淨。
- 一件事一個分支一個 commit（`type(scope): 說明`），不用 `git add -A`；commit 訊息結尾加一行 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`（第二個 `-m`，不論實作者是哪個模型）；commit 後 `git log -1 --format=%B` 確認尾行，不符就 amend。**不用 `git stash`**。
- **不要對 markdown 跑 `ruff format`**。

## 計畫層決定（spec 未明說之處；Task 6 寫進 spec §15）

1. **稽核產物不列 `inputs`**：`params={"samples_hash": …}`，`dataset` 欄位 + id 已足夠定址；`write_source_audit` 只掃一趟。
2. **`_LINE` 正規式與 `peek_sample_id()` 搬到 `source_audit.py`**，`access.py` 從那裡 import；`index_samples` 維持回傳 `(digest, {id: (offset, length)})`，`open` 再補 `None` sha。
3. **`load_source_audit` 對半途目錄（沒有 `manifest.json`）回 `None`**（退回路線），對有 manifest 但驗不過的回 `mismatch:`。
4. **測試 fixture 寫稽核**：`tests/helpers.py` 的 `det_with_runs` / `dataset_with_perfect_run`、`tests/submit_fixtures.py` 的 `make_pair`、`tests/unit/data/exporters/test_exporters.py` 的 `det_ds` 在 `ds.save(paths)` 之後呼叫 `write_source_audit`——量測 / 提交 / 匯出的既有測試代表「準備好的資料集」，不該因新 WARN 改斷言；只在明確測退回路線的測試裡不寫稽核。
5. **`StageResult.identity: str | None`**（kernel 提交沒有 test 讀取 → `None`，CLI 不印）；`Staged` 不加欄位。
6. **備份**：`ROLES` 在 `access_receipt` 之後插入 `source_audit`，加進 `_TIER2`，不進 `CARD_ROLES`（`index.jsonl` 不是 card）；`walk_run` 從 `AccessRef.source_audit` 找到稽核。
7. **`measure` 的 identity 取自它自己開的那份存取器**；`train run` 的 `source_audit_missing` 數 `card.access` 裡 `identity == "full_hash"` 的 ref。
8. **`MeasureResult.identity` / `ExportResult.identity` 型別 `Identity`**（從 `vcp.data.access.schema` import）。

## 檔案結構

| 檔案 | 責任 |
|---|---|
| `src/vcp/data/source_audit.py` | `KIND` / `AUDIT_FILE` / `INDEX_FILE` / `ID_PATTERN`、`SourceAudit`、`IndexRow`、`SourceAuditResult`、`LoadedAudit`、`peek_sample_id`、`audit_id`、`audit_spec`、`write_source_audit`、`load_source_audit` |
| `src/vcp/data/access/schema.py` | `Identity`；`AccessReceipt` / `AccessRef` 新欄位與驗證 |
| `src/vcp/data/access/receipt.py` | `receipt_spec(..., source_audit=)` |
| `src/vcp/data/access/access.py` | `open` 三條路、`_read` 列 sha、`identity` / `source_audit_id` 屬性、收據與 ref 新欄位 |
| `src/vcp/measure/provenance.py` | `attach_receipts` 的 `AccessRef` 帶 `identity` / `source_audit` |
| `src/vcp/cli.py` | `import` / `validate` 產稽核；`export` 印 `identity=` |
| `src/vcp/train/run.py`、`cli_train.py` | `RunResult.source_audit_missing`、WARN |
| `src/vcp/measure/measure.py`、`cli_eval.py` | `MeasureResult.identity`、WARN |
| `src/vcp/data/exporters/base.py` | `ExportResult.identity`、WARN |
| `src/vcp/submit/stage.py`、`cli_submit.py` | `StageResult.identity`、VERDICT `identity=` |
| `src/vcp/backup/schema.py`、`evidence.py` | 角色 `source_audit`、`walk_run` |
| `tests/unit/data/test_source_audit.py`、`test_access.py`、`test_access_schema.py`、`tests/unit/test_cli.py`、`tests/unit/train/test_run.py`、`tests/unit/measure/test_measure.py`、`tests/unit/data/exporters/test_exporters.py`、`tests/unit/submit/test_stage.py`、`tests/unit/backup/test_evidence_run.py`、`tests/unit/test_e2e_source_audit.py`、`tests/unit/test_regression_gate.py`、`tests/integration/test_source_audit.py`、`tests/helpers.py`、`tests/submit_fixtures.py` | 測試與 fixture |
| `README.md`、`CLAUDE.md`、`AGENTS.md`、`docs/handover/HANDOVER.md`、`projects/rsna-knee/RUNBOOK.md`、spec §15、`CHANGELOG.md`、`src/vcp/__init__.py` | 文件與發版 |

## 給實作者的共用約定

- 測試用 `roots` fixture（`tests/conftest.py`）、`tests/helpers.py`（`det_samples`、`make_card`、`write_images`、`det_with_runs`、`perfect_predictions`、`noisy_predictions`）、`tests/submit_fixtures.py`（`make_pair`、`seed_eval_runs`、`seed_judgements`、`seed_test_runs`、`EVAL`、`TEST`、`STAMP`）；CLI 測試用 `typer.testing.CliRunner` 對 `vcp.cli.app`，VERDICT 取 `r.output` 最後一行 `VERDICT `。
- 寫完 `.py` 先 `uv run ruff format <檔案>` 再跑測試；pytest 用 `uv run pytest <路徑> -o addopts="" -q`；每個任務結尾 `uv run ruff check . && uv run ruff format --check .` 乾淨才 commit。
- `ruff` 的 isort 把 `helpers` / `submit_fixtures` / `backup_fixtures` 當第一方，接受它排出來的順序；新 import 用 `uv run ruff check --fix <檔案>` 排。
- 本計畫的程式碼片段是要照抄的實作；型別、函式名、`reason=` 字彙以片段為準。

---

### Task 1: `source_audit.py`——模型、id、`write_source_audit`、`load_source_audit`

**Files:**
- Create: `src/vcp/data/source_audit.py`
- Modify: `src/vcp/data/access/schema.py:11-17`（加 `Identity`）
- Test: `tests/unit/data/test_source_audit.py`

**Interfaces:**
- Consumes: `vcp.artifact.store`（`reuse`、`verify`、`manifest_path`、`MANIFEST`）、`vcp.artifact.writer.ArtifactWriter`（`create` / `reserve` / `write_json` / `commit`）、`vcp.artifact.schema.ArtifactSpec`、`vcp.core.paths.DatasetPaths` / `artifact_dir`、`vcp.core.hashing.sha256_file`、`vcp.core.time.stamp`、`vcp.core.build.build_string`。
- Produces: `KIND = "source_audit"`、`AUDIT_FILE`、`INDEX_FILE`、`ID_PATTERN`、`SourceAudit`、`IndexRow`、`SourceAuditResult(artifact_id, state, manifest_sha256)`、`LoadedAudit(artifact_id, manifest_sha256, audit, index)`、`peek_sample_id(raw, path, lineno) -> str`、`audit_id(dataset, samples_hash) -> str`、`audit_spec(paths, card) -> ArtifactSpec`、`write_source_audit(paths, card, *, data_root) -> SourceAuditResult`、`load_source_audit(paths, card, *, data_root) -> LoadedAudit | None`；`vcp.data.access.schema.Identity`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/data/test_source_audit.py`

```python
"""source_audit artifacts (spec 4.1, 6, 7.1): one preparation-time pass over samples.jsonl
becomes a content-addressed, immutable row index that consumers verify against instead of
hashing the whole file again."""

import hashlib
import json
import shutil

import pytest

from helpers import det_samples, make_card, write_images
from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, artifact_dir
from vcp.data.access.access import index_samples
from vcp.data.dataset import Dataset
from vcp.data.source_audit import (
    AUDIT_FILE,
    INDEX_FILE,
    KIND,
    IndexRow,
    SourceAudit,
    audit_id,
    audit_spec,
    load_source_audit,
    peek_sample_id,
    write_source_audit,
)
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan


def _seed(roots, name="tiny", n=40):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def test_peek_sample_id_reads_only_the_leading_key(tmp_path):
    assert peek_sample_id(b'{"sample_id": "s1", "views": BROKEN\n', tmp_path / "x", 1) == "s1"
    assert peek_sample_id(b'{"sample_id":"a\\"b"}\n', tmp_path / "x", 2) == 'a"b'
    with pytest.raises(ValidationFailed, match="not a samples.jsonl line"):
        peek_sample_id(b'{"views": []}\n', tmp_path / "x", 3)


def test_audit_id_and_spec_are_content_addressed():
    h = "0123456789abcdef" + "f" * 48
    assert audit_id("beach-test", h) == "src-beach-test-0123456789abcdef"
    card = make_card("det", name="beach-test", image_root="raw/x").model_copy(
        update={"samples_hash": h}
    )
    paths = DatasetPaths.resolve("beach-test", data_root=None, configs_root=None)
    spec = audit_spec(paths, card)
    assert isinstance(spec, ArtifactSpec)
    assert (spec.kind, spec.id, spec.dataset) == (KIND, "src-beach-test-0123456789abcdef", "beach-test")
    assert spec.params == {"samples_hash": h} and spec.inputs == []
    assert spec.id_pattern is not None  # the dataset group must equal the dataset field


def test_write_source_audit_creates_then_reuses(roots):
    ds, plan, paths = _seed(roots)
    res = write_source_audit(paths, ds.card, data_root=roots.data)
    assert res.state == "created" and res.artifact_id == audit_id("tiny", ds.card.samples_hash)
    d = artifact_dir(roots.data, KIND, res.artifact_id)
    assert (d / AUDIT_FILE).is_file() and (d / INDEX_FILE).is_file() and (d / "manifest.json").is_file()
    assert res.manifest_sha256 == sha256_file(d / "manifest.json")
    audit = SourceAudit.model_validate_json((d / AUDIT_FILE).read_text(encoding="utf-8"))
    assert (audit.dataset, audit.samples_hash) == ("tiny", ds.card.samples_hash)
    assert audit.size_bytes == paths.samples_jsonl.stat().st_size and audit.line_count == 40
    rows = [
        IndexRow.model_validate_json(line)
        for line in (d / INDEX_FILE).read_text(encoding="utf-8").splitlines()
    ]
    assert [r.sample_id for r in rows] == [s.sample_id for s in ds.samples]
    raw = paths.samples_jsonl.read_bytes()
    for r in rows:
        chunk = raw[r.offset : r.offset + r.length]
        assert chunk.endswith(b"\n") and hashlib.sha256(chunk).hexdigest() == r.sha256
    assert not store.verify(roots.data, KIND, res.artifact_id).failed
    again = write_source_audit(paths, ds.card, data_root=roots.data)
    assert again.state == "reused" and again.artifact_id == res.artifact_id
    assert again.manifest_sha256 == res.manifest_sha256
    assert [p.name for p in (roots.data / "artifacts" / KIND).iterdir() if p.is_dir()] == [
        res.artifact_id
    ]


def test_write_source_audit_refuses_a_file_that_disagrees_with_the_card(roots):
    ds, plan, paths = _seed(roots)
    paths.samples_jsonl.write_bytes(paths.samples_jsonl.read_bytes() + b"\n")
    with pytest.raises(IntegrityError, match="^mismatch: samples.jsonl sha256"):
        write_source_audit(paths, ds.card, data_root=roots.data)
    d = artifact_dir(roots.data, KIND, audit_id("tiny", ds.card.samples_hash))
    assert (d / "failure.json").is_file() and not (d / "manifest.json").is_file()
    with pytest.raises(ValidationFailed, match="^partial:"):  # the claim is taken, never committed
        write_source_audit(paths, ds.card, data_root=roots.data)


def test_load_source_audit_matches_index_samples_and_handles_absence(roots):
    ds, plan, paths = _seed(roots)
    assert load_source_audit(paths, ds.card, data_root=roots.data) is None
    res = write_source_audit(paths, ds.card, data_root=roots.data)
    loaded = load_source_audit(paths, ds.card, data_root=roots.data)
    assert loaded is not None and loaded.artifact_id == res.artifact_id
    assert loaded.manifest_sha256 == res.manifest_sha256
    assert loaded.audit.line_count == 40
    _, plain = index_samples(paths.samples_jsonl)
    assert {k: (o, n) for k, (o, n, _) in loaded.index.items()} == plain
    # a half-written audit is not an audit: the consumer falls back
    d = artifact_dir(roots.data, KIND, res.artifact_id)
    (d / "manifest.json").unlink()
    assert load_source_audit(paths, ds.card, data_root=roots.data) is None


def test_load_source_audit_fails_closed_on_tampering(roots):
    ds, plan, paths = _seed(roots)
    res = write_source_audit(paths, ds.card, data_root=roots.data)
    d = artifact_dir(roots.data, KIND, res.artifact_id)
    index_bytes = (d / INDEX_FILE).read_bytes()
    (d / INDEX_FILE).write_bytes(index_bytes.replace(b'"offset": 0,', b'"offset": 1,', 1))
    with pytest.raises(IntegrityError, match="^mismatch: source audit"):
        load_source_audit(paths, ds.card, data_root=roots.data)
    (d / INDEX_FILE).write_bytes(index_bytes)
    assert load_source_audit(paths, ds.card, data_root=roots.data) is not None
    audit_bytes = (d / AUDIT_FILE).read_bytes()
    (d / AUDIT_FILE).write_bytes(audit_bytes.replace(b'"line_count": 40', b'"line_count": 39'))
    with pytest.raises(IntegrityError, match="^mismatch: source audit"):
        load_source_audit(paths, ds.card, data_root=roots.data)
    (d / AUDIT_FILE).write_bytes(audit_bytes)
    # the file grew after the audit (card untouched): size disagrees
    paths.samples_jsonl.write_bytes(paths.samples_jsonl.read_bytes() + b'{"sample_id": "zz"}\n')
    with pytest.raises(IntegrityError, match="^mismatch: samples.jsonl is"):
        load_source_audit(paths, ds.card, data_root=roots.data)
    shutil.rmtree(d)
    assert load_source_audit(paths, ds.card, data_root=roots.data) is None


def test_models_validate_their_hashes():
    with pytest.raises(ValueError, match="samples_hash must be 64 hex"):
        SourceAudit(
            dataset="x", samples_hash="nope", size_bytes=1, line_count=1,
            created_at="2026-09-13T00:00:00.000Z", vcp_version="0.6.0",
        )
    with pytest.raises(ValueError, match="sha256 must be 64 hex"):
        IndexRow(sample_id="a", offset=0, length=1, sha256="nope")
    row = IndexRow(sample_id="a", offset=0, length=1, sha256="a" * 64)
    assert json.loads(row.model_dump_json())["length"] == 1
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/test_source_audit.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'vcp.data.source_audit'`）

- [ ] **Step 3: 實作**

`src/vcp/data/access/schema.py`：在 `Outcome = ...` 之後加一行 `Identity = Literal["source_audit", "full_hash"]`（本任務只加型別；欄位在 Task 2）。

`src/vcp/data/source_audit.py`：

```python
"""``source_audit`` artifacts (spec 4.1, 6, 7.1): the one-time, content-addressed row index of
a dataset's ``samples.jsonl``. The preparation commands (``vcp data import`` / ``validate``)
hold full access and write it; consumers (``DatasetAccess``) load it instead of hashing the
whole file again, and verify only the rows they read. This module never imports the access
layer (the access layer imports it)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec
from vcp.artifact.writer import ArtifactWriter
from vcp.core.build import build_string
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, artifact_dir
from vcp.core.time import stamp
from vcp.data.schema import DatasetCard

KIND = "source_audit"
AUDIT_FILE = "audit.json"
INDEX_FILE = "index.jsonl"
ID_PATTERN = r"^src-(?P<dataset>.+)-[0-9a-f]{16}$"
_SHA = re.compile(r"^[0-9a-f]{64}$")
# ``sample_json_line`` writes ``sample_id`` as the first key with ``": "`` separators; the
# ``\s*`` also accepts compact separators. Only this leading key is ever decoded.
_LINE = re.compile(rb'^\{"sample_id":\s*"((?:[^"\\]|\\.)*)"')


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceAudit(_Strict):
    """``audit.json``: the identity the index was built against."""

    schema_version: int = 1
    dataset: str
    samples_hash: str
    size_bytes: int = Field(ge=0)
    line_count: int = Field(ge=0)
    created_at: str
    vcp_version: str

    @model_validator(mode="after")
    def _shape(self) -> SourceAudit:
        if not _SHA.fullmatch(self.samples_hash):
            raise ValueError("samples_hash must be 64 hex characters")
        return self


class IndexRow(_Strict):
    """One line of ``index.jsonl``: where a sample's row sits and what its bytes hash to.
    ``length`` includes the trailing newline; ``sha256`` is over exactly those bytes."""

    sample_id: str
    offset: int = Field(ge=0)
    length: int = Field(ge=1)
    sha256: str

    @model_validator(mode="after")
    def _shape(self) -> IndexRow:
        if not _SHA.fullmatch(self.sha256):
            raise ValueError("sha256 must be 64 hex characters")
        return self


class SourceAuditResult(NamedTuple):
    artifact_id: str
    state: Literal["created", "reused"]
    manifest_sha256: str


class LoadedAudit(NamedTuple):
    artifact_id: str
    manifest_sha256: str
    audit: SourceAudit
    index: dict[str, tuple[int, int, str]]  # sample_id -> (offset, length, sha256)


def peek_sample_id(raw: bytes, path: Path, lineno: int) -> str:
    """The leading ``sample_id`` of one ``samples.jsonl`` line; the line itself is not parsed."""
    m = _LINE.match(raw)
    if m is None:
        raise ValidationFailed("not a samples.jsonl line", location=f"{path}:{lineno}")
    return json.loads(b'"' + m.group(1) + b'"')


def audit_id(dataset: str, samples_hash: str) -> str:
    """Content-addressed: the same samples.jsonl always maps to the same audit."""
    return f"src-{dataset}-{samples_hash[:16]}"


def audit_spec(paths: DatasetPaths, card: DatasetCard) -> ArtifactSpec:
    """No ``inputs``: ``ArtifactWriter.create`` and ``store.reuse`` hash every input that has
    a path, and the whole point of this artifact is to read samples.jsonl once."""
    return ArtifactSpec(
        kind=KIND,
        id=audit_id(card.name, card.samples_hash),
        dataset=card.name,
        params={"samples_hash": card.samples_hash},
        id_pattern=ID_PATTERN,
    )


def write_source_audit(paths: DatasetPaths, card: DatasetCard, *, data_root: Path) -> SourceAuditResult:
    """Spec 6: reuse the audit this samples.jsonl already has, else build it in one pass.
    Preparation-time only: the whole file is read, and its sha must equal the card's."""
    spec = audit_spec(paths, card)
    if store.reuse(spec, data_root, check_files=True) is not None:
        return SourceAuditResult(
            spec.id, "reused", sha256_file(store.manifest_path(data_root, KIND, spec.id))
        )
    with ArtifactWriter.create(spec, data_root=data_root) as writer:
        index_path = writer.reserve(INDEX_FILE)
        digest = hashlib.sha256()
        offset = 0
        seen: set[str] = set()
        with paths.samples_jsonl.open("rb") as src, index_path.open("wb") as out:
            for lineno, raw in enumerate(iter(src.readline, b""), start=1):
                sample_id = peek_sample_id(raw, paths.samples_jsonl, lineno)
                if sample_id in seen:
                    raise ValidationFailed(
                        f"duplicate sample_id {sample_id!r}",
                        location=f"{paths.samples_jsonl}:{lineno}",
                    )
                seen.add(sample_id)
                digest.update(raw)
                row = IndexRow(
                    sample_id=sample_id,
                    offset=offset,
                    length=len(raw),
                    sha256=hashlib.sha256(raw).hexdigest(),
                )
                out.write(row.model_dump_json().encode("utf-8") + b"\n")
                offset += len(raw)
        if digest.hexdigest() != card.samples_hash:
            raise IntegrityError(
                f"mismatch: samples.jsonl sha256 {digest.hexdigest()[:12]} != card "
                f"samples_hash {card.samples_hash[:12]}",
                location=str(paths.samples_jsonl),
            )
        audit = SourceAudit(
            dataset=card.name,
            samples_hash=card.samples_hash,
            size_bytes=offset,
            line_count=len(seen),
            created_at=stamp(),
            vcp_version=build_string(),
        )
        writer.write_json(AUDIT_FILE, audit.model_dump(mode="json"))
        writer.commit()
    return SourceAuditResult(
        spec.id, "created", sha256_file(store.manifest_path(data_root, KIND, spec.id))
    )


def load_source_audit(paths: DatasetPaths, card: DatasetCard, *, data_root: Path) -> LoadedAudit | None:
    """Spec 7.1: the audit for exactly this samples.jsonl, verified. ``None`` when there is no
    committed audit (absent, or a half-written claim -- the consumer falls back to hashing the
    file); an audit that is there but does not hold is ``mismatch:``, never a fallback."""
    artifact_id = audit_id(card.name, card.samples_hash)
    d = artifact_dir(data_root, KIND, artifact_id)
    if not (d / store.MANIFEST).is_file():
        return None
    res = store.verify(data_root, KIND, artifact_id)
    if res.failed:
        raise IntegrityError(
            f"mismatch: source audit {artifact_id!r} no longer matches its manifest "
            f"(mismatch={len(res.mismatch)} missing={len(res.missing)} extra={len(res.extra)})",
            fields={"audit": artifact_id},
        )
    try:
        audit = SourceAudit.model_validate_json((d / AUDIT_FILE).read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValidationFailed(f"bad source audit: {e}", location=str(d / AUDIT_FILE)) from e
    if audit.dataset != card.name or audit.samples_hash != card.samples_hash:
        raise IntegrityError(
            f"mismatch: source audit {artifact_id!r} describes {audit.dataset}/"
            f"{audit.samples_hash[:12]}, not {card.name}/{card.samples_hash[:12]}",
            fields={"audit": artifact_id},
        )
    size = paths.samples_jsonl.stat().st_size
    if size != audit.size_bytes:
        raise IntegrityError(
            f"mismatch: samples.jsonl is {size} bytes, source audit {artifact_id!r} recorded "
            f"{audit.size_bytes}; re-run `vcp data validate`",
            location=str(paths.samples_jsonl),
            fields={"audit": artifact_id},
        )
    index: dict[str, tuple[int, int, str]] = {}
    with (d / INDEX_FILE).open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            try:
                row = IndexRow.model_validate_json(line)
            except ValueError as e:
                raise ValidationFailed(
                    f"bad index row: {e}", location=f"{d / INDEX_FILE}:{lineno}"
                ) from e
            if row.sample_id in index:
                raise IntegrityError(
                    f"mismatch: source audit {artifact_id!r} lists {row.sample_id!r} twice",
                    fields={"audit": artifact_id},
                )
            index[row.sample_id] = (row.offset, row.length, row.sha256)
    if len(index) != audit.line_count:
        raise IntegrityError(
            f"mismatch: source audit {artifact_id!r} indexes {len(index)} rows, audit.json says "
            f"{audit.line_count}",
            fields={"audit": artifact_id},
        )
    return LoadedAudit(artifact_id, sha256_file(d / store.MANIFEST), audit, index)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/data/source_audit.py src/vcp/data/access/schema.py tests/unit/data/test_source_audit.py && uv run pytest tests/unit/data/test_source_audit.py tests/unit/data/test_access.py tests/unit/artifact -o addopts="" -q`
Expected: 全部通過（7 個新測試；`test_write_source_audit_refuses_a_file_that_disagrees_with_the_card` 第二次呼叫的 `partial:` 由 `store.reuse` → `load_manifest` 丟出）

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/data/source_audit.py src/vcp/data/access/schema.py tests/unit/data/test_source_audit.py
git commit -m "feat(data): source_audit 產物——一趟建逐列 sha 索引、content-addressed 重用、驗證載入" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: 存取器走稽核——open 三條路、`_read` 列 sha、收據與 `AccessRef` 的 `identity`

**Files:**
- Modify: `src/vcp/data/access/schema.py`（`AccessReceipt` / `AccessRef` 新欄位）、`src/vcp/data/access/receipt.py:44-77`（`receipt_spec`）、`src/vcp/data/access/access.py`（`_LINE` 移除、`index_samples`、`__init__`、`open`、`_read`、`_build_receipt`、`_close`）、`src/vcp/measure/provenance.py`（`attach_receipts` 的 `AccessRef`）
- Test: `tests/unit/data/test_access.py`、`tests/unit/data/test_access_schema.py`

**Interfaces:**
- Consumes: Task 1 `load_source_audit` / `LoadedAudit` / `peek_sample_id` / `KIND` / `Identity`；`store.manifest_path`。
- Produces: `AccessReceipt.identity: Identity = "full_hash"`、`.source_audit: str | None`、`.source_audit_sha256: str | None`；`AccessRef.identity`、`.source_audit`；`receipt_spec(..., source_audit: LoadedAudit | None = None)`；`DatasetAccess.identity: Identity`、`DatasetAccess.source_audit_id: str | None`；`index_samples` 不變。

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/data/test_access_schema.py` 檔尾加：

```python
def test_identity_fields_are_consistent():
    from vcp.data.access.schema import AccessReceipt, AccessRef

    base = dict(
        dataset="tiny", samples_hash=SHA, card_sha256=SHA, plan_id="p", plan_sha256=SHA,
        authorization_sha256=SHA, purpose="custom", allowed=["train"], roles={"train": "train"},
        accessed={}, outcome="completed", started_at="2026-09-13T00:00:00.000Z",
        finished_at="2026-09-13T00:00:01.000Z", vcp_version="0.6.0",
    )
    receipt = AccessReceipt(**base)
    assert receipt.identity == "full_hash" and receipt.source_audit is None
    audited = AccessReceipt(
        **base, identity="source_audit", source_audit="src-tiny-" + "a" * 16, source_audit_sha256=SHA
    )
    assert audited.identity == "source_audit"
    with pytest.raises(ValidationError, match="go together"):
        AccessReceipt(**base, identity="source_audit", source_audit="src-tiny-" + "a" * 16)
    with pytest.raises(ValidationError, match="identity=source_audit needs source_audit"):
        AccessReceipt(**base, identity="source_audit")
    with pytest.raises(ValidationError, match="identity=source_audit needs source_audit"):
        AccessReceipt(**base, source_audit="src-tiny-" + "a" * 16, source_audit_sha256=SHA)
    with pytest.raises(ValidationError, match="source_audit_sha256 must be 64 hex"):
        AccessReceipt(
            **base, identity="source_audit", source_audit="src-tiny-" + "a" * 16, source_audit_sha256="x"
        )
    ref = AccessRef(
        artifact_id="r1-a1-1", purpose="train", subsets=["train"], sealed_accessed=False,
        denied=0, receipt_sha256=SHA, binding="session",
    )
    assert ref.identity == "full_hash" and ref.source_audit is None
    with pytest.raises(ValidationError, match="identity=source_audit needs source_audit"):
        AccessRef(
            artifact_id="r1-a1-1", purpose="train", subsets=["train"], sealed_accessed=False,
            denied=0, receipt_sha256=SHA, binding="session", identity="source_audit",
        )
```

`tests/unit/data/test_access.py`：import 區加 `import shutil`、`from vcp.data.source_audit import KIND as AUDIT_KIND, audit_id, write_source_audit`；檔尾加：

```python
def _audit(roots, ds, paths):
    return write_source_audit(paths, ds.card, data_root=roots.data)


def test_an_audited_open_never_hashes_the_whole_file(roots, monkeypatch):
    ds, plan, paths = _seed(roots)
    res = _audit(roots, ds, paths)
    import vcp.data.access.access as access_module

    def no_full_pass(path):
        raise AssertionError(f"whole-file pass over {path}")

    real_sha = access_module.sha256_file

    def guarded_sha(path):
        if Path(path).name == "samples.jsonl":
            raise AssertionError("samples.jsonl was hashed")
        return real_sha(path)

    monkeypatch.setattr(access_module, "index_samples", no_full_pass)
    monkeypatch.setattr(access_module, "sha256_file", guarded_sha)
    with _open(roots) as access:
        assert access.identity == "source_audit" and access.source_audit_id == res.artifact_id
        train = list(access.iter("train"))
    assert [s.sample_id for s in train] == sorted(plan.ids_in("train"))
    receipt = access.receipt
    assert receipt.identity == "source_audit" and receipt.source_audit == res.artifact_id
    assert receipt.source_audit_sha256 == res.manifest_sha256
    manifest = store.load_manifest(roots.data, "access_receipt", access.receipt_id)
    assert [i.name for i in manifest.spec.inputs] == ["source_audit"]
    assert all(not Path(i.path).is_absolute() for i in manifest.spec.inputs if i.path)
    assert manifest.spec.params["samples_hash"] == ds.card.samples_hash


def test_an_unaudited_open_is_full_hash(roots):
    ds, plan, paths = _seed(roots)
    with _open(roots) as access:
        assert access.identity == "full_hash" and access.source_audit_id is None
        list(access.iter("train"))
    assert access.receipt.identity == "full_hash" and access.receipt.source_audit is None
    manifest = store.load_manifest(roots.data, "access_receipt", access.receipt_id)
    assert [i.name for i in manifest.spec.inputs] == ["samples"]


def test_tampering_the_audit_fails_closed(roots):
    ds, plan, paths = _seed(roots)
    res = _audit(roots, ds, paths)
    d = artifact_dir(roots.data, AUDIT_KIND, res.artifact_id)
    original = (d / "index.jsonl").read_bytes()
    (d / "index.jsonl").write_bytes(original.replace(b'"length": ', b'"length":  ', 1))
    with pytest.raises(IntegrityError, match="^mismatch: source audit"):
        _open(roots)
    (d / "index.jsonl").write_bytes(original)
    with _open(roots) as access:  # restored: the audit holds again
        assert access.identity == "source_audit"
    paths.samples_jsonl.write_bytes(paths.samples_jsonl.read_bytes() + b"\n")
    with pytest.raises(IntegrityError, match="^mismatch: samples.jsonl is"):
        _open(roots)


def test_a_tampered_selected_row_is_refused_and_an_unselected_one_is_not(roots):
    ds, plan, paths = _seed(roots)
    res = _audit(roots, ds, paths)
    raw = paths.samples_jsonl.read_bytes()
    lines = raw.split(b"\n")
    by_id = {s.sample_id: i for i, s in enumerate(ds.samples)}
    train_id = sorted(plan.ids_in("train"))[0]
    val_id = sorted(plan.ids_in("valA"))[0]

    def flip(line: bytes) -> bytes:  # same length, one byte inside the JSON body changed
        pos = line.index(b'"views"')
        return line[:pos] + b'"viewz"' + line[pos + 7 :]

    tampered = list(lines)
    tampered[by_id[val_id]] = flip(lines[by_id[val_id]])
    paths.samples_jsonl.write_bytes(b"\n".join(tampered))
    with _open(roots) as access:  # never reads valA: the tamper is invisible to a train-only job
        assert len(list(access.iter("train"))) == len(plan.ids_in("train"))
    assert access.receipt.identity == "source_audit"
    tampered[by_id[train_id]] = flip(lines[by_id[train_id]])
    paths.samples_jsonl.write_bytes(b"\n".join(tampered))
    with pytest.raises(IntegrityError, match=f"^mismatch: row {train_id!r} differs from source audit"):
        with _open(roots) as access:
            access.by_id(train_id)
    assert access.receipt.outcome == "failed" and access.receipt.exception == "IntegrityError"


def test_audited_and_full_hash_reads_are_equivalent(roots):
    ds, plan, paths = _seed(roots)
    res = _audit(roots, ds, paths)
    with _open(roots, subsets={"train", "valA"}) as audited:
        first = {s: audited.records(s) for s in ("train", "valA")}
    shutil.rmtree(artifact_dir(roots.data, AUDIT_KIND, res.artifact_id))
    with _open(roots, subsets={"train", "valA"}) as plain:
        second = {s: plain.records(s) for s in ("train", "valA")}
    assert audited.identity == "source_audit" and plain.identity == "full_hash"
    assert first == second
    assert audited.receipt.accessed == plain.receipt.accessed


def test_two_accesses_share_one_audit(roots):
    ds, plan, paths = _seed(roots)
    res = _audit(roots, ds, paths)
    assert _audit(roots, ds, paths).state == "reused"
    ids = []
    for _ in range(2):
        with _open(roots) as access:
            list(access.iter("train"))
        ids.append(access.receipt.source_audit)
    assert ids == [res.artifact_id, res.artifact_id]
    assert len([p for p in (roots.data / "artifacts" / AUDIT_KIND).iterdir() if p.is_dir()]) == 1
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/test_access_schema.py tests/unit/data/test_access.py -o addopts="" -q -k "identity or audited or unaudited or tamper or equivalent or share"`
Expected: FAIL（`ValidationError: … identity … Extra inputs are not permitted`；`AttributeError: 'DatasetAccess' object has no attribute 'identity'`）

- [ ] **Step 3: 實作**

`src/vcp/data/access/schema.py`：

`AccessReceipt` 在 `unseal_event_sha256` 之後加

```python
    identity: Identity = "full_hash"
    source_audit: str | None = None
    source_audit_sha256: str | None = None
```

`_shape` 的 `unseal_event_sha256` 檢查之後加

```python
        if (self.source_audit is None) != (self.source_audit_sha256 is None):
            raise ValueError("source_audit and source_audit_sha256 go together")
        if (self.identity == "source_audit") != (self.source_audit is not None):
            raise ValueError("identity=source_audit needs source_audit (and vice versa)")
        if self.source_audit_sha256 is not None and not _SHA.fullmatch(self.source_audit_sha256):
            raise ValueError("source_audit_sha256 must be 64 hex characters")
```

`AccessRef` 在 `binding` 之後加 `identity: Identity = "full_hash"`、`source_audit: str | None = None`；`_validate_hashes` 加

```python
        if (self.identity == "source_audit") != (self.source_audit is not None):
            raise ValueError("identity=source_audit needs source_audit (and vice versa)")
```

`src/vcp/data/access/receipt.py`：import 區加 `from vcp.data.source_audit import KIND as SOURCE_AUDIT_KIND, LoadedAudit`；`receipt_spec` 加參數 `source_audit: LoadedAudit | None = None`，`inputs` 改為

```python
    if source_audit is None:
        inputs = [InputRef(name="samples", path=str(paths.samples_jsonl), sha256=samples_hash)]
    else:
        # Spec 4.3: with an audit the corpus is never hashed again -- not even by the writer.
        params["samples_hash"] = samples_hash
        params["source_audit"] = source_audit.artifact_id
        inputs = [
            InputRef(
                name="source_audit",
                path=str(store.manifest_path(paths.data_root, SOURCE_AUDIT_KIND, source_audit.artifact_id)),
                sha256=source_audit.manifest_sha256,
            )
        ]
    return ArtifactSpec(kind=KIND, id=receipt_id, dataset=paths.name, plan_id=plan_id, params=params, inputs=inputs)
```

`src/vcp/data/access/access.py`：

- 模組 docstring 加一句 `With a ``source_audit`` (spec 7) the open pass is skipped entirely and every row read is checked against its audited sha.`；刪 `_LINE`、`import re`、`import json`（若無他用）；import 區加 `from vcp.data.source_audit import LoadedAudit, load_source_audit, peek_sample_id`、`Identity` 加進 `vcp.data.access.schema` 的 import。
- `index_samples` 迴圈改用 `sample_id = peek_sample_id(raw, path, lineno)`（重複 id 檢查留著）。
- `__init__` 加參數 `source_audit: LoadedAudit | None`，`index` 型別 `dict[str, tuple[int, int, str | None]]`；本體加

```python
        self.identity: Identity = "source_audit" if source_audit is not None else "full_hash"
        self.source_audit_id = source_audit.artifact_id if source_audit is not None else None
        self._source_audit_sha256 = source_audit.manifest_sha256 if source_audit is not None else None
```

  並把 `receipt_spec(...)` 呼叫加 `source_audit=source_audit`。
- `open`：把

```python
        digest, index = index_samples(paths.samples_jsonl)
        if digest != card.samples_hash:
            raise IntegrityError(...)
```

  改成

```python
        loaded = load_source_audit(paths, card, data_root=paths.data_root)
        index: dict[str, tuple[int, int, str | None]]
        if loaded is not None:
            index = dict(loaded.index)  # spec 7.1: no pass over samples.jsonl at all
        else:
            digest, plain = index_samples(paths.samples_jsonl)
            if digest != card.samples_hash:
                raise IntegrityError(
                    f"mismatch: samples.jsonl sha256 {digest[:12]} != card samples_hash "
                    f"{card.samples_hash[:12]}",
                    location=str(paths.samples_jsonl),
                )
            index = {k: (o, n, None) for k, (o, n) in plain.items()}
```

  coverage 檢查不變；`cls(...)` 多傳 `source_audit=loaded`。
- `_read`：

```python
            offset, length, expected = self._index[sample_id]
            f.seek(offset)
            raw = f.read(length)
            if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
                raise IntegrityError(
                    f"mismatch: row {sample_id!r} differs from source audit "
                    f"{self.source_audit_id!r}; samples.jsonl changed after the audit",
                    fields={"sample": sample_id},
                )
            raw = raw.rstrip(b"\r\n")
```

- `_build_receipt`：`AccessReceipt(...)` 加 `identity=self.identity, source_audit=self.source_audit_id, source_audit_sha256=self._source_audit_sha256`。
- `_close`：`AccessRef(...)` 加 `identity=self.identity, source_audit=self.source_audit_id`。

`src/vcp/measure/provenance.py` `attach_receipts` 的 `AccessRef(...)` 加 `identity=receipt.identity, source_audit=receipt.source_audit`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/data/access src/vcp/measure/provenance.py tests/unit/data/test_access.py tests/unit/data/test_access_schema.py && uv run pytest tests/unit/data tests/unit/measure tests/unit/train -o addopts="" -q`
Expected: 全部通過（既有測試沒寫稽核 → 走 `full_hash`，行為不變）

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/data/access/schema.py src/vcp/data/access/receipt.py src/vcp/data/access/access.py src/vcp/measure/provenance.py tests/unit/data/test_access.py tests/unit/data/test_access_schema.py
git commit -m "feat(access): open 走 source_audit 不再整檔 hash，讀列先比 sha，收據記 identity" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `vcp data import` / `validate` 自動產稽核

**Files:**
- Modify: `src/vcp/cli.py:85-183`（`import_cmd`、`validate_cmd`）
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: Task 1 `write_source_audit(paths, card, *, data_root) -> SourceAuditResult(artifact_id, state, manifest_sha256)`；`DatasetPaths.resolve`。
- Produces: `import` / `validate` VERDICT 欄位 `source_audit=<id>`、`source_audit_state=created|reused`；`--json` 的 `fields` 同名鍵，`result` 多 `"source_audit": <id>`。

- [ ] **Step 1: 寫失敗的測試**——`tests/unit/test_cli.py`：`test_import_validate_split_lineage_flow` 裡 import 之後的斷言改成

```python
    v = _last_verdict(r.output)
    assert v.startswith("VERDICT cmd=import status=OK") and "samples=60" in v
    assert "source_audit=src-tiny-" in v and "source_audit_state=created" in v

    r = runner.invoke(app, ["data", "validate", "--name", "tiny"])
    v = _last_verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and "source_audit_state=reused" in v
```

`test_json_mode_puts_result_on_stdout` 加 `assert doc["fields"]["source_audit"].startswith("src-tiny-") and doc["result"]["source_audit"] == doc["fields"]["source_audit"]`。檔尾加：

```python
def test_validate_recreates_a_removed_source_audit(roots, tmp_path):
    import shutil

    assert _import_tiny(roots, tmp_path).exit_code == 0
    shutil.rmtree(roots.data / "artifacts" / "source_audit")
    r = runner.invoke(app, ["data", "validate", "--name", "tiny"])
    assert r.exit_code == 0 and "source_audit_state=created" in _last_verdict(r.output)
    r = runner.invoke(app, ["artifact", "status", "--kind", "source_audit"])
    assert r.exit_code == 0 and "source_audit" in r.output
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/test_cli.py -o addopts="" -q -k "import_validate or json_mode or recreates"`
Expected: FAIL（VERDICT 沒有 `source_audit=`）

- [ ] **Step 3: 實作**——`src/vcp/cli.py`：import 區加 `from vcp.data.source_audit import write_source_audit`（`DatasetPaths` 若尚未 import 也加）。`import_cmd` 的 `res = get_importer(importer).run(spec)` 之後加

```python
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        audit = write_source_audit(paths, res.dataset.card, data_root=paths.data_root)
```

`fields` 加 `"source_audit": audit.artifact_id, "source_audit_state": audit.state`；payload dict 加 `"source_audit": audit.artifact_id`。`validate_cmd` 的 `ds = Dataset.load(...)` 之後加同樣兩行（card 用 `ds.card`），`fields` / payload 同上。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/cli.py tests/unit/test_cli.py && uv run pytest tests/unit/test_cli.py tests/unit/test_e2e_flow.py tests/unit/data -o addopts="" -q`
Expected: 全部通過

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/cli.py tests/unit/test_cli.py
git commit -m "feat(data): import / validate 結束自動產 source_audit，VERDICT 記 id 與 created|reused" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: 消費者——`train run` / `measure` / `export` WARN、`stage` 印 `identity=`、fixture 寫稽核

**Files:**
- Modify: `src/vcp/train/run.py`（`RunResult`、`train_run` 尾段）、`src/vcp/cli_train.py:104-125`、`src/vcp/measure/measure.py`（`MeasureResult`、`measure_run`）、`src/vcp/cli_eval.py`（measure fields）、`src/vcp/data/exporters/base.py`（`ExportResult`、`export_subset`）、`src/vcp/cli.py`（export fields）、`src/vcp/submit/stage.py`（`StageResult`、`_render`、`stage`）、`src/vcp/cli_submit.py`（stage fields）、`tests/helpers.py`（`det_with_runs`、`dataset_with_perfect_run`）、`tests/submit_fixtures.py`（`make_pair`）、`tests/unit/data/exporters/test_exporters.py`（`det_ds`）
- Test: `tests/unit/train/test_run.py`、`tests/unit/measure/test_measure.py`、`tests/unit/data/exporters/test_exporters.py`、`tests/unit/submit/test_stage.py`、`tests/unit/test_cli_eval.py`

**Interfaces:**
- Consumes: Task 2 `DatasetAccess.identity`、`AccessRef.identity`；Task 1 `write_source_audit`；`vcp.data.access.schema.Identity`。
- Produces: `RunResult.source_audit_missing: int`；`train run` VERDICT `source_audit=missing`（有時才印，status WARN）；`MeasureResult.identity: Identity`、`measure` VERDICT `identity=`；`ExportResult.identity: Identity`、`export` VERDICT `identity=`；`StageResult.identity: str | None`、`stage` VERDICT `identity=`；warning 字串 `source_audit=missing`。

- [ ] **Step 1: fixture 先寫稽核**——`tests/helpers.py`：import `from vcp.data.source_audit import write_source_audit`；`det_with_runs` 與 `dataset_with_perfect_run` 裡 `ds.save(paths)` 之後加 `write_source_audit(paths, ds.card, data_root=roots.data)`。`tests/submit_fixtures.py` `make_pair`：eval 與 test 兩個 `save(...)` 之後各加一行（用各自的 `paths` 與 `card`）。`tests/unit/data/exporters/test_exporters.py` `det_ds`：`ds.save(paths)` 之後加一行。這些 fixture 代表「準備好的資料集」；只有明確測退回路線的測試不寫稽核。

- [ ] **Step 2: 寫失敗的測試**

`tests/unit/train/test_run.py` 檔尾（import 區加 `from vcp.data.source_audit import write_source_audit`）：

```python
def test_train_run_warns_when_the_dataset_has_no_source_audit(roots, work):
    ds, plan, paths = _seed(roots)
    assert materialize(
        MaterializeSpec(name="tiny", mode="npy", data_root=roots.data, configs_root=roots.configs)
    ).failed == 0
    (work / "access_train.py").write_text(ACCESS_FAKE, encoding="utf-8")
    res = train_run(_spec(roots, work, command=[sys.executable, "access_train.py"]))
    assert res.source_audit_missing == 1 and "source_audit=missing" in res.warnings
    assert load_run(roots.data, "r1").access[0].identity == "full_hash"
    write_source_audit(paths, ds.card, data_root=roots.data)
    res = train_run(_spec(roots, work, run_id="r2", command=[sys.executable, "access_train.py"]))
    assert res.source_audit_missing == 0 and "source_audit=missing" not in res.warnings
    ref = load_run(roots.data, "r2").access[0]
    assert ref.identity == "source_audit" and ref.source_audit is not None
```

`tests/unit/measure/test_measure.py` 檔尾（import `import shutil`、`from vcp.data.source_audit import KIND as AUDIT_KIND`）：

```python
def test_measure_reports_its_identity_and_warns_without_an_audit(roots, tmp_path):
    _, plan, paths = det_with_runs(roots, tmp_path, n=40)
    res = measure_run(_spec(roots, "perfect"))
    assert res.identity == "source_audit" and "source_audit=missing" not in res.warnings
    shutil.rmtree(roots.data / "artifacts" / AUDIT_KIND)
    res = measure_run(_spec(roots, "perfect"))
    assert res.identity == "full_hash" and "source_audit=missing" in res.warnings
```

`tests/unit/test_cli_eval.py` 檔尾：

```python
def test_measure_verdict_prints_identity(roots, tmp_path):
    det_with_runs(roots, tmp_path, n=40)
    r = runner.invoke(app, ["eval", "measure", "--run", "perfect"])
    assert r.exit_code == 0 and "identity=source_audit" in _verdict(r.output)
```

`tests/unit/data/exporters/test_exporters.py` 檔尾（import `import shutil`、`from vcp.data.source_audit import KIND as AUDIT_KIND`）：

```python
def test_export_reports_identity_and_warns_without_an_audit(roots, tmp_path, det_ds):
    res = export_subset(_spec(roots, "coco", tmp_path / "a", subset="train"))
    assert res.identity == "source_audit" and "source_audit=missing" not in res.warnings
    shutil.rmtree(roots.data / "artifacts" / AUDIT_KIND)
    res = export_subset(_spec(roots, "coco", tmp_path / "b", subset="train"))
    assert res.identity == "full_hash" and "source_audit=missing" in res.warnings
```

`tests/unit/submit/test_stage.py` 的 `test_stage_reads_the_test_subset_through_a_receipt` 最後加 `assert res.identity == "source_audit" and receipt.identity == "source_audit"`。

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/unit/train/test_run.py tests/unit/measure/test_measure.py tests/unit/test_cli_eval.py tests/unit/data/exporters/test_exporters.py tests/unit/submit/test_stage.py -o addopts="" -q -k "source_audit or identity"`
Expected: FAIL（`AttributeError: … 'source_audit_missing'` / `'identity'`）

- [ ] **Step 4: 實作**

`src/vcp/train/run.py`：`RunResult` 加 `source_audit_missing: int = 0`；`train_run` 在 `if info.invalid:` 區塊之後加

```python
    source_audit_missing = sum(1 for r in card.access if r.identity == "full_hash")
    if source_audit_missing:
        warnings.append("source_audit=missing")
```

`RunResult(...)` 加 `source_audit_missing=source_audit_missing`。`src/vcp/cli_train.py` 在 `if res.receipt_invalid:` 之後加 `if res.source_audit_missing: fields["source_audit"] = "missing"`。

`src/vcp/measure/measure.py`：import `from vcp.data.access.schema import Identity`；`MeasureResult` 加 `identity: Identity = "full_hash"`；`measure_run` 的 `with DatasetAccess.open(...) as access:` 區塊之後加

```python
    identity = access.identity
    if identity == "full_hash":
        warnings.append("source_audit=missing")
```

`MeasureResult(...)` 加 `identity=identity`。`src/vcp/cli_eval.py` `measure_cmd` 的 `fields` 加 `"identity": res.identity`。

`src/vcp/data/exporters/base.py`：import `Identity`；`ExportResult` 加 `identity: Identity`；`export_subset` 的 `receipt_id = access.receipt_id` 之後加 `identity = access.identity`，`warnings` 建好後加 `if identity == "full_hash": warnings.append("source_audit=missing")`，`ExportResult(...)` 加 `identity=identity`。`src/vcp/cli.py` `export_cmd` 的 `fields` 加 `"identity": res.identity`。

`src/vcp/submit/stage.py`：`StageResult` 加 `identity: str | None = None`（dataclass 最後一個欄位）；`_render` 回傳型別改 `tuple[Artifact, Path, str]`，`with` 區塊裡加 `identity = access.identity`，`return artifact, tmp, identity`；`stage()` 改 `artifact, tmp, identity = _render(...)`（kernel 分支 `identity = None`），`return StageResult(staged, final_dir, warnings, identity)`。`src/vcp/cli_submit.py` `stage_cmd` 在 `if st.provenance:` 之後加 `if res.identity: fields["identity"] = res.identity`。

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run ruff format src/vcp/train/run.py src/vcp/cli_train.py src/vcp/measure/measure.py src/vcp/cli_eval.py src/vcp/data/exporters/base.py src/vcp/cli.py src/vcp/submit/stage.py src/vcp/cli_submit.py tests/helpers.py tests/submit_fixtures.py tests/unit/train/test_run.py tests/unit/measure/test_measure.py tests/unit/test_cli_eval.py tests/unit/data/exporters/test_exporters.py tests/unit/submit/test_stage.py && uv run pytest tests/unit -o addopts="" -q`
Expected: 全部通過。若某個既有測試因新的 `source_audit=missing` WARN 或 `artifacts/source_audit` 目錄的出現而失敗（例如對 `artifact status` 的 kind 數量做精確斷言），先讓該測試的 fixture 寫稽核或把稽核 kind 納入預期，不要拿掉 WARN。

- [ ] **Step 6: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/train/run.py src/vcp/cli_train.py src/vcp/measure/measure.py src/vcp/cli_eval.py src/vcp/data/exporters/base.py src/vcp/cli.py src/vcp/submit/stage.py src/vcp/cli_submit.py tests/helpers.py tests/submit_fixtures.py tests/unit/train/test_run.py tests/unit/measure/test_measure.py tests/unit/test_cli_eval.py tests/unit/data/exporters/test_exporters.py tests/unit/submit/test_stage.py
git commit -m "feat(consumers): train run / measure / export 缺 source_audit 時 WARN，measure / export / stage 印 identity" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: 備份角色、端到端、回歸門檻列、真資料唯讀測試、RUNBOOK

**Files:**
- Modify: `src/vcp/backup/schema.py:18-51`、`src/vcp/backup/evidence.py:174-177`、`tests/unit/test_regression_gate.py`、`projects/rsna-knee/RUNBOOK.md:66`
- Create: `tests/unit/test_e2e_source_audit.py`、`tests/integration/test_source_audit.py`
- Test: `tests/unit/backup/test_evidence_run.py`

**Interfaces:**
- Consumes: Task 2 `AccessRef.source_audit` / `identity`；Task 3 CLI 欄位；Task 4 WARN；`vcp.core.paths.artifact_dir`。
- Produces: `ROLES` 含 `"source_audit"`（`"access_receipt"` 之後）、`_TIER2` 含它（tier 2）、不進 `CARD_ROLES`；`walk_run` 收每份有稽核的收據背後的 `manifest.json` / `audit.json` / `index.jsonl`；門檻列 `wave 1b-2 (VCP-002)`。

- [ ] **Step 1: 備份層測試**——`tests/unit/backup/test_evidence_run.py` 檔尾（`ROLES`、`TIER_OF`、`CARD_ROLES`、`DatasetAccess`、`attach_receipts`、`save_run`、`load_run`、`EVAL` 已在 Task 11（1b-1）時 import）：

```python
def test_walk_run_collects_the_source_audit_behind_a_receipt(world):
    assert ROLES.index("source_audit") == ROLES.index("access_receipt") + 1
    assert TIER_OF["source_audit"] == 2 and "source_audit" not in CARD_ROLES
    with DatasetAccess.open(
        EVAL, "fixed-v1", subsets={"train"}, purpose="train", run_id="good",
        data_root=world.roots.data, configs_root=world.roots.configs,
    ) as access:
        list(access.iter("train"))
    card = attach_receipts(
        load_run(world.roots.data, "good"), [access.receipt_id], data_root=world.roots.data
    )
    save_run(world.roots.data, card)
    aid = card.access[0].source_audit
    assert card.access[0].identity == "source_audit" and aid is not None
    col = _col(world)
    col.walk_run("good", "run:good")
    roles = _roles(col)
    assert roles["source_audit"] == [
        f"artifacts/source_audit/{aid}/audit.json",
        f"artifacts/source_audit/{aid}/index.jsonl",
        f"artifacts/source_audit/{aid}/manifest.json",
    ]
    by_key = {e.key: e for e in col.files_of()}
    entry = by_key[f"data/artifacts/source_audit/{aid}/index.jsonl"]
    assert entry.tier == 2 and entry.present and entry.for_ == ["run:good"]
    assert col.missing == [] and col.unlisted == []
```

Run: `uv run pytest tests/unit/backup/test_evidence_run.py -o addopts="" -q -k source_audit` → FAIL（`ValueError: 'source_audit' is not in tuple`）。改 `src/vcp/backup/schema.py`：`ROLES` 在 `"access_receipt",` 之後插入 `"source_audit",`；`_TIER2 = ("source_audit", "prediction", "samples", "raw_manifest", "train_dir", "logs")`。改 `src/vcp/backup/evidence.py` 的收據迴圈：

```python
        for ref in card.access:  # spec 8: the receipts are the evidence of what it trained on
            adir = artifact_dir(self.data_root, "access_receipt", ref.artifact_id)
            self.add(adir / "manifest.json", "access_receipt", conclusion)
            self.add(adir / "receipt.json", "access_receipt", conclusion, sha256=ref.receipt_sha256)
            if ref.source_audit is not None:  # 1b-2: the audit the receipt verified rows against
                sdir = artifact_dir(self.data_root, "source_audit", ref.source_audit)
                for name in ("manifest.json", "audit.json", "index.jsonl"):
                    self.add(sdir / name, "source_audit", conclusion)
```

Run 同上 → PASS；`uv run pytest tests/unit/backup -o addopts="" -q` 全綠（`world` 的資料集由 `make_pair` 建，Task 4 起會寫稽核）。

- [ ] **Step 2: 端到端** `tests/unit/test_e2e_source_audit.py`

```python
"""VCP-002 through the CLI: `validate` creates the source audit once (reused after); a
training loop under `vcp train run` reads through it (no `source_audit=missing`); `measure`
reports identity=source_audit; remove the audit -> measure warns; `validate` recreates it."""

import shutil
import sys

from typer.testing import CliRunner

from helpers import det_samples, make_card, perfect_predictions, write_images
from vcp.cli import app
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.data.source_audit import KIND as AUDIT_KIND
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.predictions import write_predictions
from vcp.measure.runs import load_run

runner = CliRunner()

ACCESS_FAKE = """
from pathlib import Path
from vcp.train import MaterializedReader

with MaterializedReader("flow", "npy", plan_id="fixed-v1", subset="train") as reader:
    n = sum(1 for _ in reader)
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best-%d" % n)
"""


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _run(*args):
    return runner.invoke(app, list(args))


def _field(verdict: str, name: str) -> str:
    return verdict.split(f" {name}=", 1)[1].split()[0]


def test_source_audit_flow(roots, tmp_path):
    paths = DatasetPaths.resolve("flow", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(60, seed=5)
    write_images(roots.data / "raw" / "flow", samples)
    ds = Dataset.from_parts(make_card("det", name="flow", image_root="raw/flow"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=3)
    save_plan(plan, paths)
    assert materialize(
        MaterializeSpec(name="flow", mode="npy", data_root=roots.data, configs_root=roots.configs)
    ).failed == 0
    # 1. validate creates the audit; a second validate reuses it
    r = _run("data", "validate", "--name", "flow")
    v = _verdict(r.output)
    assert r.exit_code == 0 and _field(v, "source_audit_state") == "created"
    aid = _field(v, "source_audit")
    assert aid.startswith("src-flow-")
    r = _run("data", "validate", "--name", "flow")
    assert _field(_verdict(r.output), "source_audit_state") == "reused"
    # 2. a training loop reads through the audit: no warning, the ref says so
    work = tmp_path / "work"
    work.mkdir()
    (work / "access.py").write_text(ACCESS_FAKE, encoding="utf-8")
    r = _run(
        "train", "run", "--run", "good", "--dataset", "flow", "--plan", "fixed-v1",
        "--trained-on", "train", "--seed", "1", "--cwd", str(work),
        "--checkpoints", "weights/*.pt", "--final", "weights/best.pt",
        "--", sys.executable, "access.py",
    )
    v = _verdict(r.output)
    assert r.exit_code == 0, r.output
    assert "receipts=1" in v and "source_audit=missing" not in v
    ref = load_run(roots.data, "good").access[0]
    assert ref.identity == "source_audit" and ref.source_audit == aid
    # 3. measure through the audit
    for subset in ("valA", "valB"):
        src = tmp_path / f"good-{subset}.jsonl"
        write_predictions(src, perfect_predictions(ds.subset(subset, plan), ds.card))
        r = _run(
            "eval", "ingest", "--run", "good", "--dataset", "flow", "--plan", "fixed-v1",
            "--subset", subset, "--format", "jsonl", "--src", str(src),
        )
        assert r.exit_code == 0, r.output
    r = _run("eval", "measure", "--run", "good")
    v = _verdict(r.output)
    assert r.exit_code == 0 and _field(v, "identity") == "source_audit"
    assert "source_audit=missing" not in v
    # 4. without the audit the same measure falls back and warns
    shutil.rmtree(roots.data / "artifacts" / AUDIT_KIND)
    r = _run("eval", "measure", "--run", "good")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v
    assert _field(v, "identity") == "full_hash" and "source_audit=missing" in v
    # 5. validate brings it back under the same id, and the artifact layer can verify it
    r = _run("data", "validate", "--name", "flow")
    v = _verdict(r.output)
    assert _field(v, "source_audit_state") == "created" and _field(v, "source_audit") == aid
    r = _run("artifact", "verify", "--kind", AUDIT_KIND, "--id", aid)
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output)
```

Run: `uv run pytest tests/unit/test_e2e_source_audit.py -o addopts="" -q` → 全綠（若 `artifact verify` 的選項名不同，照 `uv run vcp artifact verify --help` 調整）。

- [ ] **Step 3: 門檻列**——`tests/unit/test_regression_gate.py` 的 `GATE` 最後加

```python
    (
        "wave 1b-2 (VCP-002)",
        "source audit: an audited open never hashes the whole file; a tampered audit or a "
        "tampered selected row fails closed while an unselected row is never read; audited and "
        "full-hash reads are equivalent; consumers record identity and warn without an audit",
        {
            "tests/unit/data/test_source_audit.py": [
                "test_write_source_audit_creates_then_reuses",
                "test_load_source_audit_fails_closed_on_tampering",
            ],
            "tests/unit/data/test_access.py": [
                "test_an_audited_open_never_hashes_the_whole_file",
                "test_a_tampered_selected_row_is_refused_and_an_unselected_one_is_not",
                "test_audited_and_full_hash_reads_are_equivalent",
            ],
            "tests/unit/train/test_run.py": [
                "test_train_run_warns_when_the_dataset_has_no_source_audit",
            ],
            "tests/unit/test_e2e_source_audit.py": ["test_source_audit_flow"],
        },
    ),
```

- [ ] **Step 4: 真資料唯讀測試** `tests/integration/test_source_audit.py`

```python
"""A source audit built over a COPY of the real dataset's metadata under a temporary root,
then a train-only access that must not pass over samples.jsonl at all. The real roots are
read once and never written to."""

from __future__ import annotations

import shutil

import pytest

from conftest import load_real
from vcp.core.paths import DatasetPaths
from vcp.data.access import access as access_module
from vcp.data.access.access import DatasetAccess
from vcp.data.dataset import Dataset
from vcp.data.source_audit import write_source_audit
from vcp.data.split import load_plan

pytestmark = pytest.mark.realdata
NAME = "rsna-knee"
PLAN = "fixed-v1"


def _tree(root):
    if not root.is_dir():
        return None
    return sorted(
        (p.relative_to(root).as_posix(), p.stat().st_size) for p in root.rglob("*") if p.is_file()
    )


def test_audited_train_only_access_over_a_copy_of_the_real_metadata(real_roots, tmp_path, monkeypatch):
    load_real(NAME, real_roots)
    src = DatasetPaths.resolve(NAME, data_root=real_roots.data, configs_root=real_roots.configs)
    if not src.plan_json(PLAN).is_file():
        pytest.skip(f"real plan {PLAN!r} absent")
    before = _tree(real_roots.data / "artifacts"), _tree(src.config_dir)
    dst = DatasetPaths.resolve(NAME, data_root=tmp_path / "data", configs_root=tmp_path / "configs")
    for a, b in (
        (src.card_yaml, dst.card_yaml),
        (src.samples_jsonl, dst.samples_jsonl),
        (src.plan_json(PLAN), dst.plan_json(PLAN)),
    ):
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(a, b)
    card = Dataset.load_card(NAME, data_root=dst.data_root, configs_root=dst.configs_root)
    plan = load_plan(dst, PLAN)
    if "train" not in {s.name for s in plan.subsets}:
        pytest.skip(f"real plan {PLAN!r} has no train subset")
    res = write_source_audit(dst, card, data_root=dst.data_root)
    assert res.state == "created"

    def no_full_pass(path):
        raise AssertionError(f"whole-file pass over {path}")

    monkeypatch.setattr(access_module, "index_samples", no_full_pass)
    with DatasetAccess.open(
        NAME, PLAN, subsets={"train"}, purpose="custom",
        data_root=dst.data_root, configs_root=dst.configs_root,
    ) as access:
        assert access.identity == "source_audit"
        train = list(access.iter("train"))
    assert [s.sample_id for s in train] == sorted(plan.ids_in("train"))
    assert access.receipt.source_audit == res.artifact_id
    assert (_tree(real_roots.data / "artifacts"), _tree(src.config_dir)) == before
```

Run: `uv run pytest tests/integration/test_source_audit.py -o addopts="" -q -m realdata`（有真資料就跑，否則 skip）。

- [ ] **Step 5: RUNBOOK**——`projects/rsna-knee/RUNBOOK.md` 的 `uv run vcp data validate --name rsna-knee` 那一行之後（同一個 powershell 區塊內）加一行註解：

```powershell
# v0.6.0 起 validate 同時寫 artifacts/source_audit/<id>（VERDICT source_audit=… source_audit_state=…）；之後訓練 / 量測不再整檔 hash samples.jsonl
```

- [ ] **Step 6: 全套與 commit**

Run: `uv run ruff format src/vcp/backup tests/unit/backup/test_evidence_run.py tests/unit/test_e2e_source_audit.py tests/unit/test_regression_gate.py tests/integration/test_source_audit.py && uv run pytest tests/unit -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: 全綠

```bash
git add src/vcp/backup/schema.py src/vcp/backup/evidence.py tests/unit/backup/test_evidence_run.py tests/unit/test_e2e_source_audit.py tests/unit/test_regression_gate.py tests/integration/test_source_audit.py projects/rsna-knee/RUNBOOK.md
git commit -m "feat(backup,test): 證據圖收 source_audit、端到端劇本、門檻列、真資料唯讀測試、RUNBOOK 註記" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: 文件、spec §15、release 0.6.0

**Files:**
- Modify: `README.md`（資料層表 `vcp data import` / `validate` / `export` 三列與「收據與 provenance」段）、`CLAUDE.md` 與 `AGENTS.md`（「路徑」與「常用命令」）、`docs/handover/HANDOVER.md:23`、`docs/superpowers/specs/2026-09-13-vcp-source-audit-design.md`（§15）、`CHANGELOG.md`、`src/vcp/__init__.py`

- [ ] **Step 1: README**

`vcp data import` 列的作用欄尾加「；並寫一份 `source_audit`（逐列 sha 索引，VERDICT `source_audit=` `source_audit_state=`）」；`vcp data validate` 列作用改成「重驗 card、samples 與 hash；產生或重用 `source_audit`（VERDICT `source_audit=` `source_audit_state=created\|reused`）」；`vcp data export` 列的選項欄尾加「；VERDICT `identity=source_audit\|full_hash`」。「收據與 provenance」段落最後加一句：

```markdown
v0.6.0 起 `vcp data import` / `validate` 會留一份 `artifacts/source_audit/src-<dataset>-<hash16>/`（`audit.json` + 每列一個 sha 的 `index.jsonl`，內容定址、同內容重用）；存取器有它就不再整檔 hash `samples.jsonl`——未授權的列連讀都不讀、讀到的列先比 sha 再解析——收據記 `identity: source_audit`。舊資料集沒稽核照跑（`identity: full_hash`），但 `train run` / `measure` / `export` 會 WARN `source_audit=missing`，重跑 `vcp data validate` 就升級；稽核壞掉是 `mismatch:` FAIL，不退回。
```

- [ ] **Step 2: CLAUDE.md 與 AGENTS.md**（兩檔同樣）——「路徑」在 `artifacts/access_receipt/...` 那一條之後加

```markdown
- `artifacts/source_audit/src-<dataset>-<hash16>/`（`audit.json` + `index.jsonl`）是 `vcp data import` / `validate` 留的逐列 sha 索引（內容定址、同內容重用、不做 supersedes）：存取器有它就不掃 `samples.jsonl`、只驗讀到的列；壞掉 → `mismatch:` FAIL；缺席 → 退回整檔 hash 並 WARN `source_audit=missing`；收據 `identity` 記用了哪條路。
```

「常用命令」最後加

```markdown
- `uv run vcp data validate --name X` 也負責產 / 重用 `source_audit`（VERDICT `source_audit=` `source_audit_state=`）；`uv run vcp artifact status --kind source_audit` 看現有稽核
```

`docs/handover/HANDOVER.md` 第 23 行（`- 版本：` 開頭那一整行）換成：

```markdown
- 版本：`0.6.0`（tag `v0.6.0`，2026-09-13）= 稽核 Wave 1b-2（`source_audit` 逐列 sha 索引產物、存取器只驗讀到的列、收據 `identity`，VCP-002）；`0.5.0` = Wave 1b-1（角色範圍存取 `DatasetAccess`、`access_receipt` 產物、provenance 三級與四個強制點，VCP-001/003）；`0.4.0` = Wave 1a（不可變產物層）；`0.3.0` = Wave 0；`0.2.0` 是第一個有 tag 的 release。規則與發版步驟在 `CHANGELOG.md` 表頭。`0.2.0` 之前 240 個 commit 都宣告 `0.1.0` 且無 tag——RSNA 早期產物裡的 `"vcp_version": "0.1.0"` 回推不到單一 commit；0.2.0 起產物記 `版本+g<commit>[.dirty]`。下一步是 Wave 1c（程式碼快照與授權，VCP-004/006）；1b-1 / 1b-2 的開放待辦在各自後記；稽核文件副本在 `docs/audits/`。
```

- [ ] **Step 3: spec §15**——把 `docs/superpowers/specs/2026-09-13-vcp-source-audit-design.md` 的 `## 15. 補充決定（實作期）` 內容換成

```markdown
## 15. 補充決定（實作期，Plan 10）

1. 稽核產物不列 `inputs`（`params={"samples_hash": …}`）：`ArtifactWriter.create` 與 `store.reuse` 會 hash 有 path 的 inputs，列 `samples.jsonl` 會讓 `validate` 掃三趟；§4.1 / §6 的 `inputs=[samples]` 以此為準改掉。
2. `_LINE` 正規式與 `peek_sample_id()` 住在 `source_audit.py`，`access.py` 從那裡 import；`index_samples` 維持回傳 `(digest, {id: (offset, length)})`，`open` 再補 `None` sha。
3. `load_source_audit` 對半途目錄（沒有 `manifest.json`）回 `None`（退回路線）；對有 manifest 但驗不過、欄位不符、大小不符的回 `mismatch:`。
4. 測試 fixture（`det_with_runs`、`dataset_with_perfect_run`、`make_pair`、exporter `det_ds`）在 `save` 之後寫稽核：量測 / 提交 / 匯出的既有測試代表「準備好的資料集」；只有明確測退回路線的測試不寫。
5. `StageResult.identity: str | None`（kernel 提交 `None`，CLI 不印）；`Staged` 不加欄位。
6. 備份：`ROLES` 在 `access_receipt` 之後插 `source_audit`、進 `_TIER2`、不進 `CARD_ROLES`；`walk_run` 從 `AccessRef.source_audit` 找稽核。
7. `measure` 的 `identity` 取自它自己開的存取器；`train run` 的 `source_audit_missing` 數 `card.access` 裡 `identity == "full_hash"` 的 ref。
8. `MeasureResult.identity` / `ExportResult.identity` 型別 `Identity`；收據產物走稽核時 `params` 多 `samples_hash` 與 `source_audit`。
```

- [ ] **Step 4: 發版**——`src/vcp/__init__.py` 改 `__version__ = "0.6.0"`；`CHANGELOG.md` 在 `## [0.5.0]` 之前插入：

```markdown
## [0.6.0] - 2026-09-13

稽核 **Wave 1b-2**：來源稽核與選取列存取——VCP-002（大型來源每個 job 都整檔 hash，與 train-only 列存取衝突）。MINOR 的理由：新產物 kind `source_audit`、收據與 `run.yaml` / `train.yaml` 的 `AccessRef` 多 `identity` / `source_audit`、`import` / `validate` / `measure` / `export` / `stage` / `train run` 的新 VERDICT 欄位與 WARN 字彙 `source_audit=missing`、備份角色 `source_audit`。

### Added
- **`source_audit` 產物**（`vcp.data.source_audit`）：`vcp data import` / `validate` 結束時一趟讀 `samples.jsonl`，寫 `audit.json`（`samples_hash`、大小、行數）與 `index.jsonl`（每列 `sample_id` / offset / length / 該列 bytes 的 sha256），id `src-<dataset>-<samples_hash 前 16 碼>`，同內容 `store.reuse` 不重算；VERDICT `source_audit=` `source_audit_state=created|reused`。
- **存取器走稽核**：`DatasetAccess.open` 有稽核就不再掃 `samples.jsonl`，只 seek 授權的列；每列先比稽核的 sha 再比 `sample_id` 再解析（同長度篡改也會 `mismatch:`）；稽核壞掉 `mismatch:` FAIL；缺席退回整檔 hash。收據多 `identity: source_audit|full_hash`、`source_audit`、`source_audit_sha256`；`AccessRef` 多 `identity` / `source_audit`；走稽核時收據產物的 `inputs` 列稽核的 `manifest.json`。
- **消費者**：`vcp train run` 任一收據退回整檔 hash → WARN `source_audit=missing`；`vcp eval measure` / `vcp data export` 印 `identity=` 並在退回時 WARN；`vcp submit stage` 印 `identity=`。
- 備份證據圖收收據背後的稽核（角色 `source_audit`，tier 2）；回歸門檻多一列 `wave 1b-2 (VCP-002)`；真資料唯讀整合測試。

### Changed
- `_LINE` / `peek_sample_id` 移到 `vcp.data.source_audit`；`index_samples` 介面不變。
- README、AGENTS.md / CLAUDE.md、交接文件、RSNA RUNBOOK；spec §15 補充決定。
```

然後：

```bash
uv sync --reinstall-package vcp
uv run pytest --cov=vcp
uv run ruff check . && uv run ruff format --check .
```

Expected: 全部通過、覆蓋率 ≥ 80%、`tests/unit/test_package.py` 三方一致。`docs/handover/HANDOVER.md` 第 24 行（`- 測試：`）的覆蓋率數字改成本次印出的值。

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md AGENTS.md docs/handover/HANDOVER.md docs/superpowers/specs/2026-09-13-vcp-source-audit-design.md CHANGELOG.md src/vcp/__init__.py
git commit -m "chore(release): v0.6.0——稽核 Wave 1b-2（來源稽核與選取列存取）" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

發版第四步（合併、tag `v0.6.0`、push）由收尾流程在 PR 合併後執行。
