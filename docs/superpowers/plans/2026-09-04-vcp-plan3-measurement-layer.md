# vcp 量測層（子專案 2）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `vcp eval` 量測層：標準預測檔與 run、四個預測轉換器、九個指標的登記表、只增不改的讀數台帳與錨點護欄、預登記 → 判決的閉環、σ_p 三法與準入規則。

**Architecture:** 新套件 `src/vcp/measure/`（schema、predictions、runs、converters/、metrics/、masks、ledger、anchors、measure、stats、sigma、prereg、judge、report、plugins）；CLI 拆出共用層 `src/vcp/cli_common.py`，新增 `src/vcp/cli_eval.py` 掛成 `vcp eval`。資料層只動一處：YOLO 匯出 manifest 加 `images` 對照表；`DatasetPaths` 加 runs / measure / prereg 路徑；`GuardrailError` 加進錯誤階層。一切以 hash 綁定：預測檔 sha → 讀數 id → 判決。

**Tech Stack:** Python 3.12（uv）、pydantic v2、typer、numpy、scikit-learn（既有）、pycocotools（新 `eval` extra，dev 群組亦裝）、Pillow（mask 柵格化）、pytest / ruff。

**Spec:** `docs/superpowers/specs/2026-09-04-vcp-measurement-layer-design.md`（v1，全部章節已核可）。資料層介面見 `docs/superpowers/specs/2026-09-02-vcp-skeleton-and-data-layer-design.md` v4。

## Global Constraints

- 每個專案命令前綴 `uv run`；改 `pyproject.toml` 後先 `uv sync`。
- 三條機械鐵則（`CLAUDE.md`）：取時只用 `vcp.core.time.utc_now()` / `stamp()`（ruff TID251 會擋 `datetime.now`、`time.time`）；每個 CLI 命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT` 收尾、exit 0 / 0 / 1 / 2、永不互動、`--json` 時 JSON 到 stdout、VERDICT 到 stderr；venv 隔離。
- 通用性：`src/vcp` 不得出現比賽名稱或比賽專屬欄名；指標依任務登記、轉換器依格式登記；比賽專屬計分器經 `--plugin` 從 `projects/` 登記。
- 台帳只 append（`readings.jsonl`、`judgements.jsonl`、`sigma.jsonl`、`prereg.log.jsonl`、`anchors.log.jsonl`、`runs/<id>/history.jsonl`）；任何命令不得改寫既有列。
- 寫入會被 hash 或被 git 紀錄的文字檔一律 `encoding="utf-8", newline="\n"`；讀檔一律指定 `encoding="utf-8"`；JSON 列用 `json.dumps(..., ensure_ascii=False)`。
- 錯誤語意：使用者資料或選項問題 → `ValidationFailed`（FAIL）；hash 不符 → `IntegrityError`（FAIL）；run 與資料集或 plan 不符 → `PlanMismatchError`（ABORT）；護欄不符 → `GuardrailError`（ABORT）；缺選用套件 → `VcpError`（ABORT）。judge 的結論在 `verdict=` 欄位，命令本身 `status=OK`；`--strict` 時 FAIL / INVALID → exit 1。
- 測試：`tests/conftest.py` 的 autouse fixture 已把兩個根目錄指到 tmp；需要真實路徑物件時用 `roots` fixture（`roots.data`、`roots.configs`）；夾具 helper 在 `tests/helpers.py`（`from helpers import ...`）；真資料測試放 `tests/integration/`、標記 `realdata`、資料缺席即 skip。
- ruff：line-length 100、select `E F I UP B TID`；`uv run ruff format --check .` 也要過。覆蓋率門檻 80%。
- 檔案上限 800 行（`cli.py` 現約 500 行，故拆出 `cli_common.py` 與 `cli_eval.py`）。
- 每個 commit 訊息結尾加空行與 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`；永不 `git add -A`（`.superpowers/`、`.claude/worktrees/` 為 scratch）。

---

## File Structure

| 檔案 | 責任 | 任務 |
|---|---|---|
| `pyproject.toml` | `eval` extra 與 dev 群組加 `pycocotools>=2.0.8` | 1 |
| `src/vcp/core/errors.py` | `GuardrailError` | 1 |
| `src/vcp/core/paths.py` | `runs_dir`、`run_dir(run_id)`、`measure_dir`、`prereg_dir`、`prereg_log` | 1 |
| `src/vcp/data/exporters/yolo.py` | manifest `images: {扁平檔名: sample_id}` | 1 |
| `src/vcp/cli_common.py`（新）、`src/vcp/cli.py` | 共用 `run_command`、`CmdResult`、選項型別、`parse_opts`、`render_table` 搬到 cli_common；cli.py 從它 import 並 re-export | 1 |
| `src/vcp/measure/__init__.py`、`schema.py` | 預測、run、讀數、錨點、預登記、判決、σ_p 的 pydantic 模型；`payload_field(task)` | 2 |
| `src/vcp/measure/predictions.py` | 讀寫標準預測檔、對資料集驗證、`PredictionStats` | 2 |
| `src/vcp/measure/runs.py` | run 目錄、`RunCard` 讀寫、預測檔 sha 核對、history | 2 |
| `src/vcp/measure/converters/{__init__,base,jsonl,scores_csv}.py` | 轉換器協定、登記表、兩個表格類轉換器 | 3 |
| `src/vcp/measure/converters/{coco_results,yolo_txt}.py` | 兩個偵測框架格式轉換器 | 4 |
| `src/vcp/measure/ingest.py`、`src/vcp/cli_eval.py`（新，`eval_app`） | `ingest` 流程與 `vcp eval ingest` | 5 |
| `src/vcp/measure/metrics/{__init__,base,tabular}.py` | 指標協定、登記表、`params_key`；accuracy / macro_f1 / log_loss / macro_auc / rmse / mae | 6 |
| `src/vcp/measure/metrics/coco_map.py` | COCO mAP（pycocotools） | 7 |
| `src/vcp/measure/masks.py`、`src/vcp/measure/metrics/seg.py` | 柵格化（polygon、RLE）與 dice / miou | 8 |
| `src/vcp/measure/{ledger,anchors,measure}.py`、`cli_eval.py` | 讀數台帳、錨點、護欄、`vcp eval measure` / `anchor` | 9 |
| `src/vcp/measure/{stats,sigma}.py`、`cli_eval.py` | 配對 bootstrap、σ_p 三法、`vcp eval sigma` | 10 |
| `src/vcp/measure/{prereg,judge}.py`、`cli_eval.py` | 預登記、判決、`vcp eval preregister` / `judge` | 11 |
| `src/vcp/measure/{report,plugins}.py`、`cli_eval.py`、docs、integration | `status` / `report` / `--plugin`、端到端、README、CLAUDE.md、真資料測試 | 12 |
| `tests/helpers.py` | `seg_samples`、`perfect_predictions`、`noisy_predictions` | 2, 8 |
| `tests/unit/measure/...` | 每任務的單元測試 | 各任務 |

任務順序 1 → 12。Task 2 的 schema 與 helper 被所有後續任務使用；Task 6 的登記表被 7、8、9、10 使用；Task 9 的台帳被 10、11、12 使用。

---

### Task 1: 前置——`eval` extra、`GuardrailError`、路徑、YOLO manifest `images`、CLI 共用層拆分（spec §5、§9、§11）

**Files:**
- Modify: `pyproject.toml:17-30`
- Modify: `src/vcp/core/errors.py`（末尾）
- Modify: `src/vcp/core/paths.py:114-136`（`DatasetPaths` 新增 property / method）
- Modify: `src/vcp/data/exporters/yolo.py:56-58,86,124-129`
- Create: `src/vcp/cli_common.py`
- Modify: `src/vcp/cli.py:1-122`
- Modify: `tests/unit/test_cli.py:221`
- Test: `tests/unit/core/test_errors.py`、`tests/unit/core/test_paths.py`、`tests/unit/data/exporters/test_exporters.py`、`tests/unit/test_cli.py`

**Interfaces:**
- Produces:
  - `vcp.core.errors.GuardrailError(VcpError)`（status ABORT）
  - `DatasetPaths.runs_dir -> Path`（`data_root/runs`）、`DatasetPaths.run_dir(run_id) -> Path`（`validate_name`）、`DatasetPaths.measure_dir -> Path`（`data_root/measure/<name>`）、`DatasetPaths.prereg_dir -> Path`（`config_dir/prereg`）、`DatasetPaths.prereg_log -> Path`（`config_dir/prereg.log.jsonl`）
  - YOLO 匯出 `manifest.json["images"] == {扁平檔名: sample_id}`
  - `vcp.cli_common`：`CmdResult`、`JsonOpt`、`DataRootOpt`、`ConfigsRootOpt`、`NameOpt`、`parse_opts`、`render_table`、`run_command`（與現行 `cli.py` 同名同語意）；`vcp.cli` 繼續 re-export 這些名稱
  - `pyproject`：`[project.optional-dependencies] eval = ["pycocotools>=2.0.8"]`，dev 群組加同一行

- [ ] **Step 1: 依賴**

`pyproject.toml` 的 `[project.optional-dependencies]` 加：

```toml
eval = ["pycocotools>=2.0.8"]
```

dev 群組加 `"pycocotools>=2.0.8",`。Run: `uv sync && uv run python -c "from pycocotools.cocoeval import COCOeval; print('ok')"`。Expected: `ok`（Windows / py3.12 wheel 已驗證可裝）。

- [ ] **Step 2: 錯誤與路徑的失敗測試**

`tests/unit/core/test_errors.py` 末尾加（補 import `GuardrailError`）：

```python
def test_guardrail_error_is_abort():
    e = GuardrailError("anchor mismatch", location="valA/coco_map")
    assert isinstance(e, VcpError) and e.status == "ABORT"
    assert "anchor mismatch" in str(e) and "valA/coco_map" in str(e)
```

`tests/unit/core/test_paths.py` 末尾加：

```python
def test_measure_paths(roots):
    p = DatasetPaths.resolve("ds", data_root=roots.data, configs_root=roots.configs)
    assert p.runs_dir == roots.data / "runs"
    assert p.run_dir("run-a") == roots.data / "runs" / "run-a"
    assert p.measure_dir == roots.data / "measure" / "ds"
    assert p.prereg_dir == roots.configs / "datasets" / "ds" / "prereg"
    assert p.prereg_log == roots.configs / "datasets" / "ds" / "prereg.log.jsonl"
    with pytest.raises(ValidationFailed):
        p.run_dir("bad/name")
```

Run: `uv run pytest tests/unit/core/test_errors.py tests/unit/core/test_paths.py -q`
Expected: FAIL（ImportError / AttributeError）。

- [ ] **Step 3: 實作錯誤與路徑**

`src/vcp/core/errors.py` 末尾：

```python
class GuardrailError(VcpError):
    """The anchor reading could not be reproduced: the measurement environment is suspect."""
```

`src/vcp/core/paths.py` 的 `DatasetPaths` 在 `resolve_image_root` 之前加：

```python
    @property
    def runs_dir(self) -> Path:
        return self.data_root / "runs"

    def run_dir(self, run_id: str) -> Path:
        validate_name(run_id)
        return self.runs_dir / run_id

    @property
    def measure_dir(self) -> Path:
        return self.data_root / "measure" / self.name

    @property
    def prereg_dir(self) -> Path:
        return self.config_dir / "prereg"

    @property
    def prereg_log(self) -> Path:
        return self.config_dir / "prereg.log.jsonl"
```

Run: 同上。Expected: PASS。

- [ ] **Step 4: YOLO manifest `images` 的失敗測試**

`tests/unit/data/exporters/test_exporters.py` 的 `test_yolo_manifest_categories_and_images_field` 末尾加：

```python
    ids = {s.sample_id for s in det_ds[0].subset("valA", det_ds[1])}
    assert set(manifest["images"].values()) == ids
    assert all("/" not in flat for flat in manifest["images"])
```

（`det_ds` fixture 回傳 `(ds, plan, paths)`。）Run: `uv run pytest tests/unit/data/exporters -k manifest_categories -q`。Expected: FAIL（`KeyError: 'images'`）。

- [ ] **Step 5: 實作**

`src/vcp/data/exporters/yolo.py`：迴圈前加 `images_map: dict[str, str] = {}`；在 `dst = images_out / flat` 之前加 `images_map[flat] = s.sample_id`；`ExportOutput(...)` 的 `manifest={...}` 改為

```python
            manifest={
                "categories": [
                    {"index": i, "id": c.id, "name": c.name}
                    for i, c in enumerate(dataset.card.categories)
                ],
                "images": images_map,
            },
```

Run: `uv run pytest tests/unit/data/exporters -q`。Expected: PASS。

- [ ] **Step 6: 拆出 `cli_common.py`（純搬移）**

建 `src/vcp/cli_common.py`，內容為現行 `cli.py` 的：模組 docstring 改「Shared CLI plumbing: VERDICT-terminated command runner and common option types.」；`import json, logging`、`from collections.abc import Callable`、`from pathlib import Path`、`from typing import Annotated, Any`、`import typer`、`from vcp.core.errors import ValidationFailed, VcpError`、`from vcp.core.log import FieldValue, Status, Verdict, exit_code, setup_logging`、`from vcp.core.paths import logs_dir, resolve_data_root`；然後原樣搬入 `CmdResult`、`JsonOpt`、`DataRootOpt`、`ConfigsRootOpt`、`NameOpt`、`parse_opts`、`render_table`、`_logger`、`run_command`（函式體一字不改）。

`src/vcp/cli.py`：刪除上述定義與不再需要的 import（`json`、`logging`、`Callable`、`Annotated` 仍需要保留給命令參數、`Any` 仍需要保留給 `CmdResult` 型別引用則改從 cli_common 匯入），加

```python
from vcp.cli_common import (
    CmdResult,
    ConfigsRootOpt,
    DataRootOpt,
    JsonOpt,
    NameOpt,
    parse_opts,
    render_table,
    run_command,
)
```

保留 `app`、`data_app`、`_root`、`version_cmd` 與全部 `data` 命令。`tests/unit/test_cli.py:221` 的 `monkeypatch.setattr("vcp.cli.setup_logging", _boom)` 改為 `monkeypatch.setattr("vcp.cli_common.setup_logging", _boom)`。

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: 全綠（既有 259 + 3 新測試），`from vcp.cli import app, parse_opts, render_table` 仍可用。

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/vcp/core/errors.py src/vcp/core/paths.py src/vcp/data/exporters/yolo.py src/vcp/cli_common.py src/vcp/cli.py tests/unit/core/test_errors.py tests/unit/core/test_paths.py tests/unit/data/exporters/test_exporters.py tests/unit/test_cli.py
git commit -m "feat(core,cli): eval extra、GuardrailError、量測路徑、YOLO manifest images 對照、CLI 共用層拆出 cli_common

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: 量測 schema、標準預測檔 IO 與驗證、run 目錄（spec §4.1、§4.2、§9）

**Files:**
- Create: `src/vcp/measure/__init__.py`、`src/vcp/measure/schema.py`、`src/vcp/measure/predictions.py`、`src/vcp/measure/runs.py`
- Modify: `tests/helpers.py`（`seg_samples`、`perfect_predictions`、`noisy_predictions`）
- Test: `tests/unit/measure/__init__.py`（空）、`tests/unit/measure/test_schema.py`、`tests/unit/measure/test_predictions.py`、`tests/unit/measure/test_runs.py`

**Interfaces:**
- Consumes: `vcp.data.schema`（`Sample`、`DatasetCard`、`Labels`、`Box`、`Mask`）、`vcp.data.tasks.get_task(...).label_field`、`vcp.core.hashing.sha256_file / sha256_text / sha256_json`、`vcp.core.time.stamp`、`vcp.core.config.load_yaml_model / dump_yaml_model`、Task 1 的 `DatasetPaths.run_dir`。
- Produces:
  - `measure.schema`：`PredBox`、`PredMask`、`Prediction`、`PAYLOAD_FOR_TASK`、`payload_field(task) -> str`、`RunSource`、`PredictionFile`、`RunCard`、`MetricResult`、`GuardrailInfo`、`Reading`、`Anchor`、`PreRegistration`、`SubsetJudgement`、`SigmaRef`、`Judgement`、`SigmaEstimate`（全部 `extra="forbid"`）
  - `measure.predictions`：`read_predictions(path) -> list[Prediction]`、`write_predictions(path, preds) -> str`（回傳 sha256，依 sample_id 排序、LF）、`PredictionStats(samples, predicted, empty, unknown)`、`check_predictions(preds, dataset, subset_ids, *, allow_unknown=False) -> tuple[list[Prediction], PredictionStats]`、`predictions_by_id(preds) -> dict[str, Prediction]`
  - `measure.runs`：`run_dir(data_root, run_id)`、`prediction_path(data_root, run_id, subset)`、`load_run(data_root, run_id) -> RunCard`（缺 → `ValidationFailed`）、`save_run(data_root, card)`、`verify_prediction(data_root, card, subset) -> Path`（sha 不符 → `IntegrityError`；缺子集 → `ValidationFailed`）、`append_history(data_root, run_id, row)`
  - `helpers.seg_samples(n, *, seed=0)`、`helpers.perfect_predictions(samples, card) -> list[Prediction]`、`helpers.noisy_predictions(samples, card, *, seed=0, flip=0.3) -> list[Prediction]`

- [ ] **Step 1: 寫 schema 的失敗測試**

`tests/unit/measure/test_schema.py`：

```python
import pytest
from pydantic import ValidationError

from vcp.measure.schema import (
    PredBox,
    PredMask,
    Prediction,
    PreRegistration,
    RunCard,
    RunSource,
    payload_field,
)


def test_payload_field_per_task():
    assert payload_field("det") == "boxes"
    assert payload_field("seg") == "masks"
    assert payload_field("cls") == "scores" and payload_field("multilabel") == "scores"
    assert payload_field("regression") == "targets"
    with pytest.raises(ValueError):
        payload_field("pose")


def test_prediction_payload_rules():
    p = Prediction(sample_id="a", boxes=[PredBox(x=0, y=0, w=1, h=1, category_id=0, score=0.5)])
    assert p.payload_field() == "boxes"
    assert Prediction(sample_id="b", boxes=[]).payload_field() == "boxes"
    with pytest.raises(ValidationError):
        Prediction(sample_id="c")  # no payload at all
    with pytest.raises(ValidationError):
        Prediction(sample_id="d", boxes=[], scores={"x": 0.1})  # two payloads
    with pytest.raises(ValidationError):
        PredBox(x=0, y=0, w=1, h=1, category_id=0, score=1.5)
    with pytest.raises(ValidationError):
        PredMask(category_id=0, score=0.5)  # needs rle or polygon
    with pytest.raises(ValidationError):
        Prediction(sample_id="e", scores={"x": float("nan")})


def test_run_card_and_prereg_defaults():
    card = RunCard(
        run_id="r1", dataset="ds", samples_hash="abc", plan_id="p", trained_on=["train"],
        source=RunSource(framework="test"), created_at="2026-09-04T00:00:00.000Z",
    )
    assert card.predictions == {} and card.source.notes == ""
    pr = PreRegistration(
        prereg_id="p001", claim="x", component="c", component_class="tuning",
        baseline_run="a", candidate_run="b", metric="accuracy", subsets=["valA", "valB"],
        created_at="2026-09-04T00:00:00.000Z",
    )
    assert (pr.t_min, pr.min_bases, pr.sigma_method, pr.sigma_ratio) == (2.0, 2, "splithalf", 1.0)
    with pytest.raises(ValidationError):
        PreRegistration(**{**pr.model_dump(), "component_class": "other"})
```

Run: `uv run pytest tests/unit/measure/test_schema.py -q`。Expected: FAIL（`ModuleNotFoundError: vcp.measure`）。

- [ ] **Step 2: 實作 schema**

`src/vcp/measure/__init__.py`：

```python
"""Measurement layer: prediction contract, runs, readings ledger, judgements (spec 2026-09-04)."""
```

`src/vcp/measure/schema.py`：

```python
"""Pydantic models of the measurement layer. Field names describe data shape, never a contest."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PAYLOAD_FOR_TASK: dict[str, str] = {
    "det": "boxes",
    "seg": "masks",
    "cls": "scores",
    "multilabel": "scores",
    "regression": "targets",
}
PAYLOAD_FIELDS = ("boxes", "masks", "scores", "targets")


def payload_field(task: str) -> str:
    try:
        return PAYLOAD_FOR_TASK[task]
    except KeyError:
        raise ValueError(f"no prediction payload defined for task {task!r}") from None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _finite(values: list[float], what: str) -> None:
    bad = [v for v in values if not math.isfinite(v)]
    if bad:
        raise ValueError(f"{what} must be finite, got {bad[:3]}")


class PredBox(_Strict):
    x: float
    y: float
    w: float
    h: float
    category_id: int
    score: float = Field(ge=0.0, le=1.0)
    view: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _finite_geometry(self) -> PredBox:
        _finite([self.x, self.y, self.w, self.h], "box geometry")
        return self


class PredMask(_Strict):
    category_id: int
    score: float = Field(ge=0.0, le=1.0)
    rle: str | None = None
    polygon: list[list[float]] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one(self) -> PredMask:
        if (self.rle is None) == (self.polygon is None):
            raise ValueError("PredMask needs exactly one of rle / polygon")
        return self


class Prediction(_Strict):
    sample_id: str = Field(min_length=1)
    boxes: list[PredBox] | None = None
    masks: list[PredMask] | None = None
    scores: dict[str, float] | None = None
    targets: dict[str, float] | None = None

    @field_validator("scores", "targets")
    @classmethod
    def _finite_values(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is not None:
            _finite(list(v.values()), "prediction values")
        return v

    @model_validator(mode="after")
    def _one_payload(self) -> Prediction:
        present = [f for f in PAYLOAD_FIELDS if getattr(self, f) is not None]
        if len(present) != 1:
            raise ValueError(f"Prediction needs exactly one payload field, got {present}")
        return self

    def payload_field(self) -> str:
        return next(f for f in PAYLOAD_FIELDS if getattr(self, f) is not None)


class RunSource(_Strict):
    framework: str = ""
    config_hash: str | None = None
    weights_hash: str | None = None
    export_manifest_sha: str | None = None
    notes: str = ""


class PredictionFile(_Strict):
    path: str
    sha256: str
    samples: int
    empty: int
    format_in: str
    ingested_at: str


class RunCard(_Strict):
    run_id: str
    dataset: str
    samples_hash: str
    plan_id: str
    trained_on: list[str]
    source: RunSource
    created_at: str
    predictions: dict[str, PredictionFile] = Field(default_factory=dict)


class MetricResult(_Strict):
    value: float
    per_class: dict[str, float | None] | None = None
    n: int


class GuardrailInfo(_Strict):
    anchor_reading_id: str
    ok: bool


class Reading(_Strict):
    reading_id: str
    ts: str
    run_id: str
    dataset: str
    samples_hash: str
    plan_id: str
    subset: str
    metric: str
    metric_version: str
    params: dict[str, str]
    value: float
    per_class: dict[str, float | None] | None
    n_samples: int
    prediction_sha: str
    guardrail: GuardrailInfo | None = None


class Anchor(_Strict):
    run_id: str
    reading_id: str
    value: float
    tolerance: float
    set_at: str


class PreRegistration(_Strict):
    prereg_id: str
    claim: str
    component: str
    component_class: Literal["model", "tuning"]
    baseline_run: str
    candidate_run: str
    metric: str
    params: dict[str, str] = Field(default_factory=dict)
    subsets: list[str]
    t_min: float = 2.0
    min_bases: int = 2
    sigma_method: str = "splithalf"
    sigma_ratio: float = 1.0
    created_at: str


class SubsetJudgement(_Strict):
    baseline: float
    candidate: float
    delta: float
    se: float
    t: float
    n: int


class SigmaRef(_Strict):
    method: str
    value: float
    estimate_id: str


class Judgement(_Strict):
    prereg_id: str
    ts: str
    baseline_run: str
    candidate_run: str
    metric: str
    params: dict[str, str]
    per_subset: dict[str, SubsetJudgement]
    bases_positive: int
    sigma_p: SigmaRef | None
    verdict: Literal["PASS", "FAIL", "INVALID"]
    reasons: list[str]
    reading_ids: list[str]
    bootstrap: dict[str, int]


class SigmaEstimate(_Strict):
    estimate_id: str
    ts: str
    plan_id: str
    metric: str
    params: dict[str, str]
    method: str
    value: float
    inputs: dict[str, Any]
    note: str = ""
```

Run: `uv run pytest tests/unit/measure/test_schema.py -q`。Expected: PASS。

- [ ] **Step 3: 夾具 helper**

`tests/helpers.py` 末尾加（檔頭已有 `random`、`np`、schema 類別；補 `from vcp.data.schema import Mask` 與 `from vcp.measure.schema import PredBox, PredMask, Prediction`）：

```python
SEG_CATS = [Category(id=0, name="road"), Category(id=1, name="water")]


def seg_samples(n: int, *, seed: int = 0) -> list[Sample]:
    """8x8 views with one axis-aligned polygon per sample (category alternates)."""
    rng = random.Random(seed)
    out: list[Sample] = []
    for i in range(n):
        x0, y0 = rng.randint(0, 3), rng.randint(0, 3)
        x1, y1 = x0 + rng.randint(2, 4), y0 + rng.randint(2, 4)
        poly = [[x0, y0, x1, y0, x1, y1, x0, y1]]
        out.append(
            Sample(
                sample_id=f"s{i:04d}",
                views=[_view(i)],
                labels=Labels(masks=[Mask(category_id=i % 2, polygon=poly)]),
                label_source="gold",
            )
        )
    return out


def perfect_predictions(samples: list[Sample], card: DatasetCard) -> list[Prediction]:
    """Predictions that reproduce the gold labels exactly (score 1.0 / one-hot)."""
    names = [c.name for c in card.categories]
    by_id = {c.id: c.name for c in card.categories}
    out: list[Prediction] = []
    for s in samples:
        labels = s.labels
        if card.task == "det":
            boxes = [
                PredBox(x=b.x, y=b.y, w=b.w, h=b.h, category_id=b.category_id, score=1.0, view=b.view)
                for b in ((labels.boxes if labels else None) or [])
            ]
            out.append(Prediction(sample_id=s.sample_id, boxes=boxes))
        elif card.task == "seg":
            masks = [
                PredMask(category_id=m.category_id, score=1.0, rle=m.rle, polygon=m.polygon,
                         meta=dict(m.meta))
                for m in ((labels.masks if labels else None) or [])
            ]
            out.append(Prediction(sample_id=s.sample_id, masks=masks))
        elif card.task == "cls":
            gold = by_id[labels.cls] if labels and labels.cls is not None else names[0]
            out.append(Prediction(sample_id=s.sample_id,
                                  scores={n: (1.0 if n == gold else 0.0) for n in names}))
        elif card.task == "multilabel":
            targets = (labels.targets if labels else None) or {n: 0.0 for n in names}
            out.append(Prediction(sample_id=s.sample_id, scores={n: float(targets[n]) for n in names}))
        else:  # regression
            out.append(Prediction(sample_id=s.sample_id,
                                  targets=dict((labels.targets if labels else None) or {})))
    return out


def noisy_predictions(
    samples: list[Sample], card: DatasetCard, *, seed: int = 0, flip: float = 0.3
) -> list[Prediction]:
    """Perfect predictions degraded at random: scores jittered, a fraction of labels flipped,
    boxes shifted by up to 2 px. Deterministic per seed."""
    rng = random.Random(seed)
    names = [c.name for c in card.categories]
    out: list[Prediction] = []
    for p in perfect_predictions(samples, card):
        if p.boxes is not None:
            boxes = [
                b.model_copy(update={"x": b.x + rng.uniform(-2, 2), "y": b.y + rng.uniform(-2, 2),
                                     "score": rng.uniform(0.3, 1.0)})
                for b in p.boxes
                if rng.random() > flip
            ]
            out.append(Prediction(sample_id=p.sample_id, boxes=boxes))
        elif p.scores is not None:
            scores = {}
            for n in names:
                v = p.scores[n]
                if rng.random() < flip:
                    v = 1.0 - v
                scores[n] = min(1.0, max(0.0, v * 0.7 + rng.uniform(0.0, 0.3)))
            out.append(Prediction(sample_id=p.sample_id, scores=scores))
        elif p.targets is not None:
            out.append(Prediction(sample_id=p.sample_id,
                                  targets={k: v + rng.uniform(-5, 5) for k, v in p.targets.items()}))
        else:
            out.append(p)
    return out
```

- [ ] **Step 4: 預測檔 IO 與驗證的失敗測試**

`tests/unit/measure/test_predictions.py`：

```python
import pytest

from helpers import CATS, cls_samples, det_samples, make_card, perfect_predictions
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.measure.predictions import (
    check_predictions,
    predictions_by_id,
    read_predictions,
    write_predictions,
)
from vcp.measure.schema import PredBox, Prediction


def test_write_read_roundtrip_sorted_lf(tmp_path):
    preds = [Prediction(sample_id="b", boxes=[]), Prediction(sample_id="a", boxes=[])]
    sha = write_predictions(tmp_path / "p.jsonl", preds)
    raw = (tmp_path / "p.jsonl").read_bytes()
    assert b"\r\n" not in raw and raw.startswith(b'{"sample_id": "a"')
    again = read_predictions(tmp_path / "p.jsonl")
    assert [p.sample_id for p in again] == ["a", "b"]
    assert len(sha) == 64 and write_predictions(tmp_path / "q.jsonl", again) == sha
    (tmp_path / "bad.jsonl").write_text('{"sample_id": "a"}\n', encoding="utf-8")
    with pytest.raises(ValidationFailed, match="bad.jsonl:1"):
        read_predictions(tmp_path / "bad.jsonl")


def test_check_predictions_det_rules():
    samples = det_samples(6, seed=0)
    ds = Dataset.from_parts(make_card("det"), samples)
    ids = {s.sample_id for s in samples}
    preds = perfect_predictions(samples[:4], ds.card)
    kept, stats = check_predictions(preds, ds, ids)
    assert (stats.samples, stats.predicted, stats.empty, stats.unknown) == (6, 4, 2, [])
    assert len(kept) == 4
    wrong = [Prediction(sample_id="s0000", scores={"cat": 1.0, "dog": 0.0, "bird": 0.0})]
    with pytest.raises(ValidationFailed, match="boxes"):
        check_predictions(wrong, ds, ids)
    bad_cat = [Prediction(sample_id="s0000", boxes=[PredBox(x=0, y=0, w=1, h=1, category_id=9, score=1)])]
    with pytest.raises(ValidationFailed, match="category"):
        check_predictions(bad_cat, ds, ids)
    ghost = [Prediction(sample_id="ghost", boxes=[])]
    with pytest.raises(ValidationFailed, match="unknown"):
        check_predictions(ghost, ds, ids)
    kept, stats = check_predictions(ghost, ds, ids, allow_unknown=True)
    assert kept == [] and stats.unknown == ["ghost"] and stats.empty == 6
    dup = [Prediction(sample_id="s0000", boxes=[]), Prediction(sample_id="s0000", boxes=[])]
    with pytest.raises(ValidationFailed, match="duplicate"):
        check_predictions(dup, ds, ids)


def test_check_predictions_cls_requires_every_sample():
    samples = cls_samples(5, seed=0)
    ds = Dataset.from_parts(make_card("cls"), samples)
    ids = {s.sample_id for s in samples}
    preds = perfect_predictions(samples, ds.card)
    kept, stats = check_predictions(preds, ds, ids)
    assert stats.predicted == 5 and stats.empty == 0
    with pytest.raises(ValidationFailed, match="missing predictions for 1"):
        check_predictions(preds[:-1], ds, ids)
    wrong_keys = [Prediction(sample_id=s.sample_id, scores={"cat": 1.0}) for s in samples]
    with pytest.raises(ValidationFailed, match="category names"):
        check_predictions(wrong_keys, ds, ids)
    assert set(predictions_by_id(preds)) == ids
```

Run: `uv run pytest tests/unit/measure/test_predictions.py -q`。Expected: FAIL（ImportError）。

- [ ] **Step 5: 實作 `predictions.py`**

```python
"""Canonical prediction files: read / write / validate against a dataset subset."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.data.dataset import Dataset
from vcp.measure.schema import Prediction, payload_field

CLS_LIKE = frozenset({"cls", "multilabel", "regression"})


def read_predictions(path: Path) -> list[Prediction]:
    if not path.is_file():
        raise ValidationFailed(f"prediction file not found: {path}")
    out: list[Prediction] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            text = line.rstrip("\r\n")
            if not text.strip():
                continue
            try:
                out.append(Prediction.model_validate_json(text))
            except ValidationError as e:
                raise ValidationFailed(str(e), location=f"{path.name}:{lineno}") from e
    return out


def write_predictions(path: Path, preds: Iterable[Prediction]) -> str:
    """Sorted by sample_id, one JSON object per line, LF. Returns the file's sha256."""
    ordered = sorted(preds, key=lambda p: p.sample_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for p in ordered:
            f.write(p.model_dump_json(exclude_none=True) + "\n")
    return sha256_file(path)


def predictions_by_id(preds: Iterable[Prediction]) -> dict[str, Prediction]:
    return {p.sample_id: p for p in preds}


@dataclass(frozen=True)
class PredictionStats:
    samples: int
    predicted: int
    empty: int
    unknown: list[str] = field(default_factory=list)


def check_predictions(
    preds: list[Prediction], dataset: Dataset, subset_ids: set[str], *, allow_unknown: bool = False
) -> tuple[list[Prediction], PredictionStats]:
    """Enforce spec §4.1: payload matches the task, categories known, ids in the subset,
    no duplicates; cls-like tasks need every sample, det/seg may omit (= empty)."""
    task = dataset.card.task
    field_name = payload_field(task)
    names = {c.name for c in dataset.card.categories}
    ids = {c.id for c in dataset.card.categories}
    seen: set[str] = set()
    kept: list[Prediction] = []
    unknown: list[str] = []
    for p in preds:
        if p.sample_id in seen:
            raise ValidationFailed(f"duplicate prediction for sample {p.sample_id!r}")
        seen.add(p.sample_id)
        if p.sample_id not in subset_ids:
            if allow_unknown:
                unknown.append(p.sample_id)
                continue
            raise ValidationFailed(f"unknown sample_id {p.sample_id!r} (not in this subset)")
        if p.payload_field() != field_name:
            raise ValidationFailed(
                f"sample {p.sample_id!r}: task {task!r} expects {field_name!r} predictions, "
                f"got {p.payload_field()!r}"
            )
        _check_categories(p, task, names, ids)
        kept.append(p)
    missing = sorted(subset_ids - seen)
    if task in CLS_LIKE and missing:
        raise ValidationFailed(
            f"missing predictions for {len(missing)} samples: {missing[:5]}"
        )
    stats = PredictionStats(
        samples=len(subset_ids), predicted=len(kept), empty=len(missing), unknown=unknown
    )
    return kept, stats


def _check_categories(p: Prediction, task: str, names: set[str], ids: set[int]) -> None:
    if p.boxes is not None:
        bad = sorted({b.category_id for b in p.boxes} - ids)
    elif p.masks is not None:
        bad = sorted({m.category_id for m in p.masks} - ids)
    elif p.scores is not None:
        if set(p.scores) != names:
            raise ValidationFailed(
                f"sample {p.sample_id!r}: score keys {sorted(p.scores)} must equal the "
                f"category names {sorted(names)}"
            )
        return
    else:
        extra = sorted(set(p.targets or {}) - names)
        if extra:
            raise ValidationFailed(f"sample {p.sample_id!r}: unknown targets {extra}")
        return
    if bad:
        raise ValidationFailed(f"sample {p.sample_id!r}: unknown category ids {bad}")
```

Run: `uv run pytest tests/unit/measure/test_predictions.py -q`。Expected: PASS。

- [ ] **Step 6: run 目錄的失敗測試**

`tests/unit/measure/test_runs.py`：

```python
import pytest

from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.measure.predictions import write_predictions
from vcp.measure.runs import (
    append_history,
    load_run,
    prediction_path,
    run_dir,
    save_run,
    verify_prediction,
)
from vcp.measure.schema import PredictionFile, Prediction, RunCard, RunSource


def _card(**kw):
    base = dict(run_id="r1", dataset="ds", samples_hash="h", plan_id="p", trained_on=["train"],
                source=RunSource(framework="t"), created_at="2026-09-04T00:00:00.000Z")
    return RunCard(**{**base, **kw})


def test_save_load_and_verify(roots):
    card = _card()
    save_run(roots.data, card)
    assert run_dir(roots.data, "r1") == roots.data / "runs" / "r1"
    assert load_run(roots.data, "r1") == card
    with pytest.raises(ValidationFailed, match="run not found"):
        load_run(roots.data, "nope")
    path = prediction_path(roots.data, "r1", "valA")
    sha = write_predictions(path, [Prediction(sample_id="a", boxes=[])])
    card = card.model_copy(update={"predictions": {"valA": PredictionFile(
        path="predictions/valA.jsonl", sha256=sha, samples=1, empty=0, format_in="jsonl",
        ingested_at="2026-09-04T00:00:00.000Z")}})
    save_run(roots.data, card)
    assert verify_prediction(roots.data, card, "valA") == path
    with pytest.raises(ValidationFailed, match="no predictions for subset"):
        verify_prediction(roots.data, card, "valB")
    path.write_text('{"sample_id": "a", "boxes": []}\n\n', encoding="utf-8", newline="\n")
    with pytest.raises(IntegrityError):
        verify_prediction(roots.data, card, "valA")
    append_history(roots.data, "r1", {"event": "replace", "subset": "valA", "old_sha": sha})
    lines = (roots.data / "runs" / "r1" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and '"event": "replace"' in lines[0]
```

Run: `uv run pytest tests/unit/measure/test_runs.py -q`。Expected: FAIL（ImportError）。

- [ ] **Step 7: 實作 `runs.py`**

```python
"""Run directories: <data_root>/runs/<run_id>/{run.yaml, predictions/<subset>.jsonl, history.jsonl}."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import validate_name
from vcp.core.time import stamp
from vcp.measure.schema import RunCard


def run_dir(data_root: Path, run_id: str) -> Path:
    validate_name(run_id)
    return Path(data_root) / "runs" / run_id


def prediction_path(data_root: Path, run_id: str, subset: str) -> Path:
    validate_name(subset)
    return run_dir(data_root, run_id) / "predictions" / f"{subset}.jsonl"


def load_run(data_root: Path, run_id: str) -> RunCard:
    path = run_dir(data_root, run_id) / "run.yaml"
    if not path.is_file():
        raise ValidationFailed(f"run not found: {path}")
    card = load_yaml_model(path, RunCard)
    if card.run_id != run_id:
        raise ValidationFailed(f"run.yaml run_id {card.run_id!r} != {run_id!r}", location=str(path))
    return card


def save_run(data_root: Path, card: RunCard) -> Path:
    path = run_dir(data_root, card.run_id) / "run.yaml"
    dump_yaml_model(card, path)
    return path


def verify_prediction(data_root: Path, card: RunCard, subset: str) -> Path:
    """Path of the subset's prediction file after checking its recorded sha256."""
    entry = card.predictions.get(subset)
    if entry is None:
        raise ValidationFailed(f"run {card.run_id!r} has no predictions for subset {subset!r}")
    path = run_dir(data_root, card.run_id) / entry.path
    if not path.is_file():
        raise ValidationFailed(f"prediction file missing: {path}")
    actual = sha256_file(path)
    if actual != entry.sha256:
        raise IntegrityError(
            f"predictions {subset!r} sha256 {actual[:12]} != recorded {entry.sha256[:12]}",
            location=str(path),
        )
    return path


def append_history(data_root: Path, run_id: str, row: dict[str, Any]) -> None:
    path = run_dir(data_root, run_id) / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"ts": stamp(), **row}, ensure_ascii=False, default=str) + "\n")
```

Run: `uv run pytest tests/unit/measure -q && uv run ruff check . && uv run ruff format --check .`。Expected: PASS。

- [ ] **Step 8: Commit**

```bash
git add src/vcp/measure tests/helpers.py tests/unit/measure
git commit -m "feat(measure): 量測 schema、標準預測檔讀寫與驗證、run 目錄與 history、測試夾具

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: 轉換器登記表、`jsonl` 與 `scores_csv` 轉換器（spec §8）

**Files:**
- Create: `src/vcp/measure/converters/__init__.py`、`base.py`、`jsonl.py`、`scores_csv.py`
- Test: `tests/unit/measure/test_converters_tabular.py`

**Interfaces:**
- Consumes: Task 2 的 `Prediction`、`read_predictions`；`vcp.data.importers.common.read_csv(path, *, required)`；`vcp.core.errors.RegistryError`。
- Produces:
  - `converters.base.ConvertContext(dataset, subset_ids, export_dir, options)`（frozen dataclass）
  - `Converter` 協定：`name`、`version`、`convert(self, src: Path, ctx: ConvertContext) -> list[Prediction]`
  - `CONVERTERS`、`register_converter`、`get_converter(name)`（未知 → `RegistryError`）
  - `JsonlConverter`（name `jsonl`）、`ScoresCsvConverter`（name `scores_csv`；選項 `id_col`、`columns=a:b,c:d`、`ignore_extra=true`）

- [ ] **Step 1: 寫失敗測試**

`tests/unit/measure/test_converters_tabular.py`：

```python
from pathlib import Path

import pytest

from helpers import ML_CATS, REG_CATS, cls_samples, make_card, multilabel_samples, regression_samples
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.dataset import Dataset
from vcp.measure.converters import CONVERTERS, get_converter
from vcp.measure.converters.base import ConvertContext
from vcp.measure.predictions import write_predictions
from vcp.measure.schema import Prediction


def _ctx(ds, **opts):
    return ConvertContext(dataset=ds, subset_ids={s.sample_id for s in ds.samples},
                          export_dir=None, options=opts)


def test_registry():
    assert set(CONVERTERS) == {"jsonl", "scores_csv"}
    assert get_converter("jsonl").name == "jsonl"
    with pytest.raises(RegistryError):
        get_converter("parquet")


def test_jsonl_passthrough(tmp_path):
    samples = cls_samples(3, seed=0)
    ds = Dataset.from_parts(make_card("cls"), samples)
    preds = [Prediction(sample_id=s.sample_id, scores={"cat": 1.0, "dog": 0.0, "bird": 0.0})
             for s in samples]
    write_predictions(tmp_path / "p.jsonl", preds)
    assert get_converter("jsonl").convert(tmp_path / "p.jsonl", _ctx(ds)) == preds


def test_scores_csv_cls_multilabel_regression(tmp_path):
    cls_ds = Dataset.from_parts(make_card("cls"), cls_samples(3, seed=0))
    (tmp_path / "cls.csv").write_text(
        "id,cat,dog,bird\ns0000,0.7,0.2,0.1\ns0001,0.1,0.8,0.1\ns0002,0.2,0.2,0.6\n",
        encoding="utf-8",
    )
    preds = get_converter("scores_csv").convert(tmp_path / "cls.csv", _ctx(cls_ds))
    assert [p.sample_id for p in preds] == ["s0000", "s0001", "s0002"]
    assert preds[1].scores == {"cat": 0.1, "dog": 0.8, "bird": 0.1}

    ml_ds = Dataset.from_parts(make_card("multilabel", categories=ML_CATS), multilabel_samples(2))
    (tmp_path / "ml.csv").write_text(
        "StudyID,ACL,MCL,EFF\ns0000,0.9,0.1,0.5\ns0001,0.2,0.2,0.2\n", encoding="utf-8"
    )
    preds = get_converter("scores_csv").convert(
        tmp_path / "ml.csv", _ctx(ml_ds, id_col="StudyID", columns="ACL:acl,MCL:mcl,EFF:effusion")
    )
    assert preds[0].scores == {"acl": 0.9, "mcl": 0.1, "effusion": 0.5}

    reg_ds = Dataset.from_parts(make_card("regression", categories=REG_CATS), regression_samples(2))
    (tmp_path / "reg.csv").write_text("path,age,note\ns0000,31.5,x\ns0001,40,y\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="unexpected columns"):
        get_converter("scores_csv").convert(tmp_path / "reg.csv", _ctx(reg_ds))
    preds = get_converter("scores_csv").convert(tmp_path / "reg.csv", _ctx(reg_ds, ignore_extra="true"))
    assert preds[1].targets == {"age": 40.0}


def test_scores_csv_errors(tmp_path):
    cls_ds = Dataset.from_parts(make_card("cls"), cls_samples(2, seed=0))
    (tmp_path / "missing.csv").write_text("id,cat,dog\ns0000,1,0\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="missing columns"):
        get_converter("scores_csv").convert(tmp_path / "missing.csv", _ctx(cls_ds))
    (tmp_path / "bad.csv").write_text("id,cat,dog,bird\ns0000,x,0,0\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="bad.csv:2"):
        get_converter("scores_csv").convert(tmp_path / "bad.csv", _ctx(cls_ds))
    with pytest.raises(ValidationFailed, match="columns="):
        get_converter("scores_csv").convert(tmp_path / "bad.csv", _ctx(cls_ds, columns="nocolon"))
```

Run: `uv run pytest tests/unit/measure/test_converters_tabular.py -q`。Expected: FAIL（ImportError）。

- [ ] **Step 2: 實作 `base.py`**

```python
"""Prediction converters: framework / contest output -> canonical Prediction list."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from vcp.core.errors import RegistryError
from vcp.data.dataset import Dataset
from vcp.measure.schema import Prediction


@dataclass(frozen=True)
class ConvertContext:
    dataset: Dataset
    subset_ids: set[str]
    export_dir: Path | None = None  # the `vcp data export` directory the predictions came from
    options: dict[str, str] = field(default_factory=dict)


class Converter(Protocol):
    name: str
    version: str

    def convert(self, src: Path, ctx: ConvertContext) -> list[Prediction]: ...


CONVERTERS: dict[str, Converter] = {}


def register_converter(converter: Converter) -> None:
    if converter.name in CONVERTERS:
        raise RegistryError(f"converter {converter.name!r} already registered")
    CONVERTERS[converter.name] = converter


def get_converter(name: str) -> Converter:
    try:
        return CONVERTERS[name]
    except KeyError:
        raise RegistryError(f"unknown converter {name!r}; known: {sorted(CONVERTERS)}") from None


def parse_mapping(value: str | None, option: str) -> dict[str, str]:
    """``a:b,c:d`` -> {"a": "b", "c": "d"}; malformed -> ValidationFailed."""
    from vcp.core.errors import ValidationFailed

    out: dict[str, str] = {}
    for item in (value or "").split(","):
        if not item.strip():
            continue
        src, sep, dst = item.partition(":")
        if not sep or not src.strip() or not dst.strip():
            raise ValidationFailed(f"--opt {option}= expects a:b,c:d pairs, got {item!r}")
        out[src.strip()] = dst.strip()
    return out
```

- [ ] **Step 3: 實作 `jsonl.py` 與 `scores_csv.py`**

`jsonl.py`：

```python
"""Canonical jsonl pass-through (validated on read)."""

from __future__ import annotations

from pathlib import Path

from vcp.measure.converters.base import ConvertContext
from vcp.measure.predictions import read_predictions
from vcp.measure.schema import Prediction


class JsonlConverter:
    name = "jsonl"
    version = "1"

    def convert(self, src: Path, ctx: ConvertContext) -> list[Prediction]:
        return read_predictions(src)
```

`scores_csv.py`：

```python
"""One row per sample: an id column plus one column per category (probabilities) or per
regression target. Kaggle submission files are usually this shape."""

from __future__ import annotations

from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.data.importers.common import read_csv
from vcp.measure.converters.base import ConvertContext, parse_mapping
from vcp.measure.schema import Prediction

_TRUE = {"1", "true", "yes"}


class ScoresCsvConverter:
    name = "scores_csv"
    version = "1"

    def convert(self, src: Path, ctx: ConvertContext) -> list[Prediction]:
        task = ctx.dataset.card.task
        if task not in ("cls", "multilabel", "regression"):
            raise ValidationFailed(f"scores_csv converts cls/multilabel/regression, not {task!r}")
        rename = parse_mapping(ctx.options.get("columns"), "columns")
        header, rows = read_csv(src, required=[])
        if not rows:
            raise ValidationFailed(f"{src.name} has no data rows")
        id_col = ctx.options.get("id_col") or header[0]
        if id_col not in header:
            raise ValidationFailed(f"id column {id_col!r} not in {src.name} header {header}")
        wanted = [c.name for c in ctx.dataset.card.categories]
        value_cols = {rename.get(col, col): col for col in header if col != id_col}
        missing = [n for n in wanted if n not in value_cols]
        if missing:
            raise ValidationFailed(f"{src.name}: missing columns for {missing} (header {header})")
        extra = sorted(set(value_cols) - set(wanted))
        if extra and ctx.options.get("ignore_extra", "false").lower() not in _TRUE:
            raise ValidationFailed(
                f"{src.name}: unexpected columns {extra}; pass --opt ignore_extra=true to drop them"
            )
        preds: list[Prediction] = []
        for lineno, row in enumerate(rows, start=2):
            try:
                values = {n: float(row[value_cols[n]]) for n in wanted}
            except ValueError as e:
                raise ValidationFailed(f"unparsable number: {e}", location=f"{src.name}:{lineno}") from e
            sample_id = row[id_col].strip()
            if task == "regression":
                preds.append(Prediction(sample_id=sample_id, targets=values))
            else:
                preds.append(Prediction(sample_id=sample_id, scores=values))
        return preds
```

`converters/__init__.py`：

```python
"""Converter registry. Importing this package registers the built-in converters."""

from vcp.measure.converters.base import (
    CONVERTERS,
    ConvertContext,
    Converter,
    get_converter,
    parse_mapping,
    register_converter,
)
from vcp.measure.converters.jsonl import JsonlConverter
from vcp.measure.converters.scores_csv import ScoresCsvConverter

register_converter(JsonlConverter())
register_converter(ScoresCsvConverter())

__all__ = ["CONVERTERS", "ConvertContext", "Converter", "JsonlConverter", "ScoresCsvConverter",
           "get_converter", "parse_mapping", "register_converter"]
```

Run: `uv run pytest tests/unit/measure -q && uv run ruff check . && uv run ruff format --check .`。Expected: PASS（`Prediction` 的 finite 檢查會把 NaN 分數擋成 `ValidationError`，轉換器不必另檢查；若測試需要，把 pydantic `ValidationError` 包成 `ValidationFailed` 並帶行號）。

- [ ] **Step 4: Commit**

```bash
git add src/vcp/measure/converters tests/unit/measure/test_converters_tabular.py
git commit -m "feat(measure): 轉換器登記表與 jsonl / scores_csv 轉換器

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `coco_results` 與 `yolo_txt` 轉換器（spec §8、§11）

**Files:**
- Create: `src/vcp/measure/converters/coco_results.py`、`src/vcp/measure/converters/yolo_txt.py`
- Modify: `src/vcp/measure/converters/__init__.py`（登記，順序 coco_results、jsonl、scores_csv、yolo_txt）
- Test: `tests/unit/measure/test_converters_det.py`

**Interfaces:**
- Consumes: Task 3 的 `ConvertContext`、`register_converter`；Task 1 的 YOLO manifest `images`；既有 COCO 匯出的 `instances.json`（`images[].id`、`images[].sample_id`）與 `manifest.json["categories"]`（YOLO：`{index, id, name}`）；`tests/helpers.perfect_predictions`。
- Produces: `CocoResultsConverter`（name `coco_results`；`ctx.export_dir` 的 `instances.json` 或 `--opt id_map=<json>` 對 image_id）、`YoloTxtConverter`（name `yolo_txt`；`src` 是含 `labels/*.txt` 的目錄，`ctx.export_dir` 必填）。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/measure/test_converters_det.py`：

```python
import json
from pathlib import Path

import pytest

from helpers import det_samples, make_card, perfect_predictions, write_images
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.exporters.base import ExportSpec, export_subset
from vcp.data.split import build_plan, parse_subsets, save_plan
from vcp.measure.converters import CONVERTERS, get_converter
from vcp.measure.converters.base import ConvertContext


@pytest.fixture
def det_export(roots, tmp_path):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(12, seed=1)
    write_images(roots.data / "raw" / "tiny", samples)
    ds = Dataset.from_parts(make_card("det", image_root="raw/tiny"), samples)
    ds.save(paths)
    save_plan(build_plan(ds, plan_id="p", subsets=parse_subsets("train:train:1.0"), seed=0), paths)
    outs = {}
    for fmt in ("coco", "yolo"):
        outs[fmt] = tmp_path / fmt
        export_subset(ExportSpec(name="tiny", plan_id="p", subset="train", format=fmt, out=outs[fmt],
                                 options={"copy": "true"}, data_root=roots.data,
                                 configs_root=roots.configs))
    return ds, outs


def _ctx(ds, export_dir=None, **opts):
    return ConvertContext(dataset=ds, subset_ids={s.sample_id for s in ds.samples},
                          export_dir=export_dir, options=opts)


def _almost(a, b):
    return all(abs(x.x - y.x) < 1e-6 and abs(x.w - y.w) < 1e-6 and x.category_id == y.category_id
               and abs(x.score - y.score) < 1e-6 for x, y in zip(a, b, strict=True))


def test_registry_has_four():
    assert set(CONVERTERS) == {"coco_results", "jsonl", "scores_csv", "yolo_txt"}


def test_coco_results_roundtrip(det_export, tmp_path):
    ds, outs = det_export
    instances = json.loads((outs["coco"] / "instances.json").read_text(encoding="utf-8"))
    image_id = {im["sample_id"]: im["id"] for im in instances["images"]}
    perfect = perfect_predictions(list(ds.samples), ds.card)
    results = [
        {"image_id": image_id[p.sample_id], "category_id": b.category_id,
         "bbox": [b.x, b.y, b.w, b.h], "score": 0.9}
        for p in perfect for b in p.boxes
    ]
    (tmp_path / "results.json").write_text(json.dumps(results), encoding="utf-8")
    preds = get_converter("coco_results").convert(tmp_path / "results.json", _ctx(ds, outs["coco"]))
    got = {p.sample_id: p.boxes for p in preds}
    for p in perfect:
        if p.boxes:
            assert _almost(sorted(got[p.sample_id], key=lambda b: (b.x, b.y)),
                           sorted([b.model_copy(update={"score": 0.9}) for b in p.boxes],
                                  key=lambda b: (b.x, b.y)))
    assert all(p.sample_id in got for p in perfect if p.boxes)
    # explicit id map instead of an export dir
    (tmp_path / "ids.json").write_text(json.dumps({str(v): k for k, v in image_id.items()}),
                                       encoding="utf-8")
    preds2 = get_converter("coco_results").convert(
        tmp_path / "results.json", _ctx(ds, None, id_map=str(tmp_path / "ids.json")))
    assert {p.sample_id for p in preds2} == set(got)
    with pytest.raises(ValidationFailed, match="image_id"):
        get_converter("coco_results").convert(tmp_path / "results.json", _ctx(ds, None))
    (tmp_path / "bad.json").write_text(json.dumps([{"image_id": 999999, "category_id": 0,
                                                    "bbox": [0, 0, 1, 1], "score": 0.5}]),
                                       encoding="utf-8")
    with pytest.raises(ValidationFailed, match="unknown image_id"):
        get_converter("coco_results").convert(tmp_path / "bad.json", _ctx(ds, outs["coco"]))


def test_yolo_txt_roundtrip(det_export, tmp_path):
    ds, outs = det_export
    manifest = json.loads((outs["yolo"] / "manifest.json").read_text(encoding="utf-8"))
    flat_of = {v: k for k, v in manifest["images"].items()}
    index_of = {c["id"]: c["index"] for c in manifest["categories"]}
    pred_dir = tmp_path / "pred"
    (pred_dir / "labels").mkdir(parents=True)
    perfect = perfect_predictions(list(ds.samples), ds.card)
    for p in perfect:
        if not p.boxes:
            continue
        view = ds.by_id[p.sample_id].views[0]
        lines = [
            f"{index_of[b.category_id]} {(b.x + b.w / 2) / view.width:.6f} "
            f"{(b.y + b.h / 2) / view.height:.6f} {b.w / view.width:.6f} {b.h / view.height:.6f} 0.8"
            for b in p.boxes
        ]
        (pred_dir / "labels" / (Path(flat_of[p.sample_id]).stem + ".txt")).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
    preds = get_converter("yolo_txt").convert(pred_dir, _ctx(ds, outs["yolo"]))
    got = {p.sample_id: p.boxes for p in preds}
    for p in perfect:
        if p.boxes:
            assert _almost(sorted(got[p.sample_id], key=lambda b: (b.x, b.y)),
                           sorted([b.model_copy(update={"score": 0.8}) for b in p.boxes],
                                  key=lambda b: (b.x, b.y)))
    with pytest.raises(ValidationFailed, match="export-manifest"):
        get_converter("yolo_txt").convert(pred_dir, _ctx(ds, None))
    (pred_dir / "labels" / "stranger.txt").write_text("0 0.5 0.5 0.1 0.1 0.5\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="stranger"):
        get_converter("yolo_txt").convert(pred_dir, _ctx(ds, outs["yolo"]))
    (pred_dir / "labels" / "stranger.txt").unlink()
    first = next(f for f in (pred_dir / "labels").glob("*.txt"))
    first.write_text("0 0.5 0.5 0.1 0.1\n", encoding="utf-8")  # no confidence column
    with pytest.raises(ValidationFailed, match="conf"):
        get_converter("yolo_txt").convert(pred_dir, _ctx(ds, outs["yolo"]))
```

Run: `uv run pytest tests/unit/measure/test_converters_det.py -q`。Expected: FAIL（registry has two, ImportError）。

- [ ] **Step 2: 實作 `coco_results.py`**

```python
"""COCO results JSON ([{image_id, category_id, bbox, score, segmentation?}]) -> Predictions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vcp.core.errors import ValidationFailed
from vcp.measure.converters.base import ConvertContext
from vcp.measure.schema import PredBox, PredMask, Prediction


def image_id_map(ctx: ConvertContext) -> dict[int, str]:
    """image_id -> sample_id from the COCO export's instances.json, or --opt id_map=<json>."""
    if ctx.options.get("id_map"):
        path = Path(ctx.options["id_map"])
        if not path.is_file():
            raise ValidationFailed(f"id_map file not found: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {int(k): str(v) for k, v in raw.items()}
    if ctx.export_dir is None:
        raise ValidationFailed(
            "coco_results needs --export-manifest <coco export dir> (its instances.json maps "
            "image_id to sample_id) or --opt id_map=<json file>"
        )
    instances = ctx.export_dir / "instances.json"
    if not instances.is_file():
        raise ValidationFailed(f"instances.json not found in export dir {ctx.export_dir}")
    doc = json.loads(instances.read_text(encoding="utf-8"))
    return {int(im["id"]): str(im["sample_id"]) for im in doc["images"]}


def _mask(seg: Any, category_id: int, score: float) -> PredMask:
    if isinstance(seg, list) and seg:
        return PredMask(category_id=category_id, score=score,
                        polygon=[[float(v) for v in poly] for poly in seg])
    if isinstance(seg, dict) and "counts" in seg:
        counts = seg["counts"]
        meta: dict[str, Any] = {"size": list(seg.get("size", []))}
        if isinstance(counts, str):
            return PredMask(category_id=category_id, score=score, rle=counts, meta=meta)
        meta["rle_encoding"] = "uncompressed"
        return PredMask(category_id=category_id, score=score,
                        rle=",".join(str(c) for c in counts), meta=meta)
    raise ValidationFailed(f"unsupported segmentation value: {type(seg).__name__}")


class CocoResultsConverter:
    name = "coco_results"
    version = "1"

    def convert(self, src: Path, ctx: ConvertContext) -> list[Prediction]:
        task = ctx.dataset.card.task
        if task not in ("det", "seg"):
            raise ValidationFailed(f"coco_results converts det/seg datasets, not {task!r}")
        if not src.is_file():
            raise ValidationFailed(f"results file not found: {src}")
        results = json.loads(src.read_text(encoding="utf-8"))
        if not isinstance(results, list):
            raise ValidationFailed(f"{src.name}: expected a JSON list of results")
        ids = image_id_map(ctx)
        boxes: dict[str, list[PredBox]] = {}
        masks: dict[str, list[PredMask]] = {}
        for i, r in enumerate(results):
            try:
                image_id = int(r["image_id"])
                category_id = int(r["category_id"])
                score = float(r["score"])
            except (KeyError, TypeError, ValueError) as e:
                raise ValidationFailed(f"result {i}: bad field ({type(e).__name__}: {e})",
                                       location=src.name) from e
            if image_id not in ids:
                raise ValidationFailed(f"result {i}: unknown image_id {image_id}", location=src.name)
            sid = ids[image_id]
            if task == "det":
                bbox = r.get("bbox")
                if not isinstance(bbox, list) or len(bbox) != 4:
                    raise ValidationFailed(f"result {i}: bbox must have 4 numbers", location=src.name)
                x, y, w, h = (float(v) for v in bbox)
                boxes.setdefault(sid, []).append(
                    PredBox(x=x, y=y, w=w, h=h, category_id=category_id, score=score))
            else:
                masks.setdefault(sid, []).append(_mask(r.get("segmentation"), category_id, score))
        if task == "det":
            return [Prediction(sample_id=s, boxes=b) for s, b in sorted(boxes.items())]
        return [Prediction(sample_id=s, masks=m) for s, m in sorted(masks.items())]
```

- [ ] **Step 3: 實作 `yolo_txt.py`**

```python
"""ultralytics `predict --save-txt --save-conf` output (labels/<flat stem>.txt, rows
`class cx cy w h conf`, normalised) -> Predictions. Needs the YOLO export dir's manifest.json for
the flat-name -> sample_id map and the class index -> category id map."""

from __future__ import annotations

import json
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.measure.converters.base import ConvertContext
from vcp.measure.schema import PredBox, Prediction


def _manifest(ctx: ConvertContext) -> tuple[dict[str, str], dict[int, int]]:
    if ctx.export_dir is None:
        raise ValidationFailed(
            "yolo_txt needs --export-manifest <yolo export dir> (its manifest.json maps flattened "
            "image names to sample ids and class indexes to category ids)"
        )
    path = ctx.export_dir / "manifest.json"
    if not path.is_file():
        raise ValidationFailed(f"manifest.json not found in export dir {ctx.export_dir}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    images = doc.get("images")
    categories = doc.get("categories")
    if not isinstance(images, dict) or not isinstance(categories, list):
        raise ValidationFailed(
            f"{path}: manifest lacks 'images' / 'categories' (re-export with the current vcp)"
        )
    stem_to_sample = {Path(flat).stem: sid for flat, sid in images.items()}
    index_to_id = {int(c["index"]): int(c["id"]) for c in categories}
    return stem_to_sample, index_to_id


class YoloTxtConverter:
    name = "yolo_txt"
    version = "1"

    def convert(self, src: Path, ctx: ConvertContext) -> list[Prediction]:
        if ctx.dataset.card.task != "det":
            raise ValidationFailed("yolo_txt converts det datasets only")
        labels_dir = src / "labels" if (src / "labels").is_dir() else src
        files = sorted(labels_dir.glob("*.txt"))
        if not files:
            raise ValidationFailed(f"no *.txt prediction files under {labels_dir}")
        stem_to_sample, index_to_id = _manifest(ctx)
        preds: list[Prediction] = []
        for path in files:
            sid = stem_to_sample.get(path.stem)
            if sid is None:
                raise ValidationFailed(f"{path.name}: no exported image matches stem {path.stem!r}")
            sample = ctx.dataset.by_id.get(sid)
            if sample is None:
                raise ValidationFailed(f"{path.name}: sample {sid!r} not in dataset")
            view = sample.views[0]
            if view.width is None or view.height is None:
                raise ValidationFailed(f"sample {sid!r} view has no size; re-import the dataset")
            boxes: list[PredBox] = []
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                parts = line.split()
                if not parts:
                    continue
                if len(parts) != 6:
                    raise ValidationFailed(
                        "expected 'class cx cy w h conf' (predict with --save-conf)",
                        location=f"{path.name}:{lineno}",
                    )
                try:
                    index = int(parts[0])
                    cx, cy, w, h, conf = (float(v) for v in parts[1:])
                except ValueError as e:
                    raise ValidationFailed(f"unparsable number: {e}",
                                           location=f"{path.name}:{lineno}") from e
                if index not in index_to_id:
                    raise ValidationFailed(f"unknown class index {index}",
                                           location=f"{path.name}:{lineno}")
                boxes.append(PredBox(
                    x=(cx - w / 2) * view.width, y=(cy - h / 2) * view.height,
                    w=w * view.width, h=h * view.height,
                    category_id=index_to_id[index], score=conf,
                ))
            preds.append(Prediction(sample_id=sid, boxes=boxes))
        return preds
```

`converters/__init__.py`：加 import 與 `register_converter(CocoResultsConverter())`、`register_converter(YoloTxtConverter())`（登記順序 coco_results、jsonl、scores_csv、yolo_txt），`__all__` 加兩個類別名。

Run: `uv run pytest tests/unit/measure -q && uv run ruff check . && uv run ruff format --check .`。Expected: PASS。`test_registry` in `test_converters_tabular.py` 斷言只有兩個 → 改為四個（`{"coco_results", "jsonl", "scores_csv", "yolo_txt"}`）。

- [ ] **Step 4: Commit**

```bash
git add src/vcp/measure/converters tests/unit/measure/test_converters_det.py tests/unit/measure/test_converters_tabular.py
git commit -m "feat(measure): coco_results 與 yolo_txt 轉換器（以匯出 manifest 對回 sample_id）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `ingest` 流程與 `vcp eval ingest`（spec §4.2、§6、§9）

**Files:**
- Create: `src/vcp/measure/ingest.py`、`src/vcp/cli_eval.py`
- Modify: `src/vcp/cli.py`（掛 `eval_app`）
- Test: `tests/unit/measure/test_ingest.py`、`tests/unit/test_cli_eval.py`

**Interfaces:**
- Consumes: Task 2（`RunCard`、`RunSource`、`PredictionFile`、`write_predictions`、`check_predictions`、`load_run`、`save_run`、`prediction_path`、`append_history`）、Task 3/4（`get_converter`、`ConvertContext`）、`vcp.data.split.load_plan`、`SplitPlan.ids_in(name)` / `SplitPlan.subset(name)`、`vcp.cli_common.run_command` 等。
- Produces:
  - `measure.ingest.IngestSpec(run_id, dataset, plan_id, subset, format, src, export_dir=None, trained_on=[], framework="", notes="", keep_input=False, replace=False, options={}, data_root=None, configs_root=None)`
  - `measure.ingest.IngestResult(run: RunCard, subset, samples, predicted, empty, unknown: list[str], path, sha256, replaced: bool, created_run: bool)`
  - `measure.ingest.ingest(spec) -> IngestResult`
  - `vcp.cli_eval.eval_app`（typer）與 `vcp eval ingest`；`vcp.cli.app` 掛上 `eval`

- [ ] **Step 1: 寫 ingest 的失敗測試**

`tests/unit/measure/test_ingest.py`：

```python
import json

import pytest

from helpers import cls_samples, det_samples, make_card, perfect_predictions, write_images
from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.predictions import read_predictions, write_predictions
from vcp.measure.runs import load_run


def _det(roots, name="tiny", n=40):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def _spec(roots, **kw):
    base = dict(run_id="m1", dataset="tiny", plan_id="fixed-v1", subset="valA", format="jsonl",
                data_root=roots.data, configs_root=roots.configs)
    return IngestSpec(**{**base, **kw})


def test_ingest_jsonl_creates_run_and_records_sha(roots, tmp_path):
    ds, plan, _ = _det(roots)
    val = ds.subset("valA", plan)
    preds = perfect_predictions(val[:-1], ds.card)  # one sample left without predictions
    write_predictions(tmp_path / "valA.jsonl", preds)
    res = ingest(_spec(roots, src=tmp_path / "valA.jsonl", trained_on=["train"], framework="test"))
    assert res.created_run and not res.replaced
    assert (res.samples, res.predicted, res.empty, res.unknown) == (len(val), len(val) - 1, 1, [])
    run = load_run(roots.data, "m1")
    assert run.dataset == "tiny" and run.samples_hash == ds.card.samples_hash
    assert run.plan_id == "fixed-v1" and run.trained_on == ["train"]
    assert run.predictions["valA"].sha256 == res.sha256 and run.predictions["valA"].empty == 1
    assert res.path == roots.data / "runs" / "m1" / "predictions" / "valA.jsonl"
    assert len(read_predictions(res.path)) == len(val) - 1


def test_ingest_second_subset_replace_and_mismatches(roots, tmp_path):
    ds, plan, paths = _det(roots)
    for subset in ("valA", "valB"):
        write_predictions(tmp_path / f"{subset}.jsonl",
                          perfect_predictions(ds.subset(subset, plan), ds.card))
    ingest(_spec(roots, src=tmp_path / "valA.jsonl", trained_on=["train"]))
    res = ingest(_spec(roots, subset="valB", src=tmp_path / "valB.jsonl"))
    assert not res.created_run and set(res.run.predictions) == {"valA", "valB"}
    with pytest.raises(ValidationFailed, match="--replace"):
        ingest(_spec(roots, src=tmp_path / "valA.jsonl"))
    res = ingest(_spec(roots, src=tmp_path / "valA.jsonl", replace=True))
    assert res.replaced
    history = (roots.data / "runs" / "m1" / "history.jsonl").read_text(encoding="utf-8")
    assert '"event": "replace"' in history and '"subset": "valA"' in history
    with pytest.raises(ValidationFailed, match="trained_on"):
        ingest(_spec(roots, run_id="m2", src=tmp_path / "valA.jsonl", trained_on=["nope"]))
    with pytest.raises(PlanMismatchError, match="subset"):
        ingest(_spec(roots, run_id="m3", subset="ghost", src=tmp_path / "valA.jsonl"))
    # a second dataset must not be mixed into an existing run
    _det(roots, name="other", n=10)
    with pytest.raises(PlanMismatchError, match="dataset"):
        ingest(_spec(roots, dataset="other", subset="train", src=tmp_path / "valA.jsonl",
                     plan_id="fixed-v1"))


def test_ingest_scores_csv_with_unknown_ids(roots, tmp_path):
    paths = DatasetPaths.resolve("c", data_root=roots.data, configs_root=roots.configs)
    samples = cls_samples(20, seed=0)
    ds = Dataset.from_parts(make_card("cls", name="c"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets("train:train:0.5,val:eval:0.5"), seed=0)
    save_plan(plan, paths)
    val = ds.subset("val", plan)
    rows = ["id,cat,dog,bird"] + [f"{s.sample_id},1,0,0" for s in val] + ["stranger,1,0,0"]
    (tmp_path / "s.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    spec = _spec(roots, run_id="c1", dataset="c", plan_id="p", subset="val", format="scores_csv",
                 src=tmp_path / "s.csv")
    with pytest.raises(ValidationFailed, match="unknown"):
        ingest(spec)
    res = ingest(spec.model_copy(update={"options": {"allow_unknown": "skip"}}))
    assert res.unknown == ["stranger"] and res.predicted == len(val)
    assert res.run.predictions["val"].format_in == "scores_csv"
    assert json.loads(res.path.read_text(encoding="utf-8").splitlines()[0])["scores"]["cat"] == 1.0
```

Run: `uv run pytest tests/unit/measure/test_ingest.py -q`。Expected: FAIL（ImportError）。

- [ ] **Step 2: 實作 `ingest.py`**

```python
"""vcp eval ingest: framework output -> canonical predictions inside a run directory."""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.split import load_plan
from vcp.measure.converters import ConvertContext, get_converter
from vcp.measure.predictions import check_predictions, write_predictions
from vcp.measure.runs import append_history, load_run, prediction_path, run_dir, save_run
from vcp.measure.schema import PredictionFile, RunCard, RunSource


class IngestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    dataset: str
    plan_id: str
    subset: str
    format: str
    src: Path
    export_dir: Path | None = None
    trained_on: list[str] = Field(default_factory=list)
    framework: str = ""
    notes: str = ""
    keep_input: bool = False
    replace: bool = False
    options: dict[str, str] = Field(default_factory=dict)
    data_root: Path | None = None
    configs_root: Path | None = None


class IngestResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    run: RunCard
    subset: str
    samples: int
    predicted: int
    empty: int
    unknown: list[str]
    path: Path
    sha256: str
    replaced: bool
    created_run: bool


def _run_card(spec: IngestSpec, data_root: Path, dataset: Dataset, plan_subsets: set[str]) -> tuple[RunCard, bool]:
    """Existing run (checked against dataset / plan) or a fresh card."""
    if (run_dir(data_root, spec.run_id) / "run.yaml").is_file():
        card = load_run(data_root, spec.run_id)
        if card.dataset != spec.dataset:
            raise PlanMismatchError(
                f"run {spec.run_id!r} belongs to dataset {card.dataset!r}, not {spec.dataset!r}"
            )
        if card.samples_hash != dataset.card.samples_hash:
            raise PlanMismatchError(
                f"run {spec.run_id!r} was created on samples_hash {card.samples_hash[:12]}, "
                f"dataset now has {dataset.card.samples_hash[:12]}"
            )
        if card.plan_id != spec.plan_id:
            raise PlanMismatchError(f"run {spec.run_id!r} uses plan {card.plan_id!r}, not {spec.plan_id!r}")
        if spec.trained_on and spec.trained_on != card.trained_on:
            raise ValidationFailed(
                f"run {spec.run_id!r} already declares trained_on={card.trained_on}; "
                f"got {spec.trained_on}"
            )
        return card, False
    unknown = sorted(set(spec.trained_on) - plan_subsets)
    if unknown:
        raise ValidationFailed(f"trained_on names unknown subsets {unknown}; plan has {sorted(plan_subsets)}")
    export_sha = None
    if spec.export_dir is not None and (spec.export_dir / "manifest.json").is_file():
        export_sha = sha256_file(spec.export_dir / "manifest.json")
    card = RunCard(
        run_id=spec.run_id,
        dataset=spec.dataset,
        samples_hash=dataset.card.samples_hash,
        plan_id=spec.plan_id,
        trained_on=list(spec.trained_on),
        source=RunSource(framework=spec.framework, notes=spec.notes, export_manifest_sha=export_sha),
        created_at=stamp(),
    )
    return card, True


def ingest(spec: IngestSpec) -> IngestResult:
    paths = DatasetPaths.resolve(spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root)
    dataset = Dataset.load(spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root)
    plan = load_plan(paths, spec.plan_id)
    if plan.dataset_hash != dataset.card.samples_hash:
        raise PlanMismatchError(
            f"plan {spec.plan_id!r} was built on samples_hash {plan.dataset_hash[:12]}, "
            f"dataset now has {dataset.card.samples_hash[:12]}"
        )
    plan.subset(spec.subset)  # PlanMismatchError for an unknown subset
    ids = plan.ids_in(spec.subset)
    card, created = _run_card(spec, paths.data_root, dataset, {s.name for s in plan.subsets})
    converter = get_converter(spec.format)
    preds = converter.convert(spec.src, ConvertContext(dataset, ids, spec.export_dir, dict(spec.options)))
    kept, stats = check_predictions(
        preds, dataset, ids, allow_unknown=spec.options.get("allow_unknown") == "skip"
    )
    replaced = spec.subset in card.predictions
    if replaced and not spec.replace:
        raise ValidationFailed(
            f"run {spec.run_id!r} already has predictions for {spec.subset!r}; pass --replace"
        )
    if replaced:
        append_history(paths.data_root, spec.run_id, {
            "event": "replace", "subset": spec.subset,
            "old_sha256": card.predictions[spec.subset].sha256,
        })
    path = prediction_path(paths.data_root, spec.run_id, spec.subset)
    sha = write_predictions(path, kept)
    if spec.keep_input:
        dest = run_dir(paths.data_root, spec.run_id) / "inputs" / spec.subset
        if dest.exists():
            shutil.rmtree(dest)
        if spec.src.is_dir():
            shutil.copytree(spec.src, dest)
        else:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(spec.src, dest / spec.src.name)
    entry = PredictionFile(
        path=f"predictions/{spec.subset}.jsonl", sha256=sha, samples=stats.predicted,
        empty=stats.empty, format_in=spec.format, ingested_at=stamp(),
    )
    card = card.model_copy(update={"predictions": {**card.predictions, spec.subset: entry}})
    save_run(paths.data_root, card)
    return IngestResult(
        run=card, subset=spec.subset, samples=stats.samples, predicted=stats.predicted,
        empty=stats.empty, unknown=stats.unknown, path=path, sha256=sha, replaced=replaced,
        created_run=created,
    )
```

Run: `uv run pytest tests/unit/measure/test_ingest.py -q`。Expected: PASS。

- [ ] **Step 3: CLI 的失敗測試**

`tests/unit/test_cli_eval.py`（新檔；之後的任務都在此加 `vcp eval` 測試）：

```python
import json

from typer.testing import CliRunner

from helpers import det_samples, make_card, perfect_predictions, write_images
from vcp.cli import app
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.predictions import write_predictions

runner = CliRunner()


def _last_verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def seed_det(roots, name="tiny", n=40, seed=0):
    """Saved det dataset + fixed-v1 plan under the isolated roots; returns (ds, plan, paths)."""
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=seed)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def ingest_perfect(roots, tmp_path, ds, plan, run_id, subset, *, drop=0, extra=()):
    preds = perfect_predictions(ds.subset(subset, plan), ds.card)
    src = tmp_path / f"{run_id}-{subset}.jsonl"
    write_predictions(src, preds[: len(preds) - drop] if drop else preds)
    return runner.invoke(app, [
        "eval", "ingest", "--run", run_id, "--dataset", ds.card.name, "--plan", plan.plan_id,
        "--subset", subset, "--format", "jsonl", "--src", str(src), "--trained-on", "train", *extra,
    ])


def test_eval_ingest_cli(roots, tmp_path):
    ds, plan, _ = seed_det(roots)
    r = ingest_perfect(roots, tmp_path, ds, plan, "m1", "valA", drop=1)
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert v.startswith("VERDICT cmd=eval.ingest status=OK") and "run=m1" in v and "empty=1" in v
    r = ingest_perfect(roots, tmp_path, ds, plan, "m1", "valA")
    assert r.exit_code == 1 and "replace" in _last_verdict(r.output)
    r = ingest_perfect(roots, tmp_path, ds, plan, "m1", "valA", extra=["--replace", "--json"])
    assert r.exit_code == 0
    doc = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert doc["fields"]["replaced"] is True and doc["result"]["run"]["run_id"] == "m1"
    assert "VERDICT" not in r.stdout and "VERDICT cmd=eval.ingest" in r.stderr
    r = runner.invoke(app, ["eval", "ingest", "--run", "m1", "--dataset", "tiny", "--plan",
                            "fixed-v1", "--subset", "valB", "--format", "nope", "--src", "x"])
    assert r.exit_code == 2 and "RegistryError" in _last_verdict(r.output)
```

Run: `uv run pytest tests/unit/test_cli_eval.py -q`。Expected: FAIL（typer：`No such command 'eval'`）。

- [ ] **Step 4: 實作 `cli_eval.py` 與掛載**

`src/vcp/cli_eval.py`：

```python
"""``vcp eval``: measurement-layer commands. Every command ends with a VERDICT line."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from vcp.cli_common import (
    CmdResult,
    ConfigsRootOpt,
    DataRootOpt,
    JsonOpt,
    parse_opts,
    run_command,
)
from vcp.core.log import FieldValue, Status
from vcp.measure.ingest import IngestSpec, ingest

eval_app = typer.Typer(no_args_is_help=True, help="measurement commands")

RunOpt = Annotated[str, typer.Option("--run", help="run id (path-safe name)")]
DatasetOpt = Annotated[str, typer.Option("--dataset", help="dataset name")]
PluginOpt = Annotated[
    list[str] | None,
    typer.Option("--plugin", help="python module to import (registers metrics / converters)"),
]


@eval_app.command("ingest")
def ingest_cmd(
    run: RunOpt,
    dataset: DatasetOpt,
    plan: Annotated[str, typer.Option("--plan", help="plan id")],
    subset: Annotated[str, typer.Option("--subset", help="subset the predictions cover")],
    fmt: Annotated[str, typer.Option("--format", help="registered converter name")],
    src: Annotated[Path, typer.Option("--src", help="prediction file or directory")],
    export_manifest: Annotated[
        Path | None, typer.Option("--export-manifest", help="vcp data export directory")
    ] = None,
    trained_on: Annotated[
        str | None, typer.Option("--trained-on", help="comma-separated subsets the run trained on")
    ] = None,
    framework: Annotated[str, typer.Option("--framework")] = "",
    notes: Annotated[str, typer.Option("--notes")] = "",
    keep_input: Annotated[bool, typer.Option("--keep-input", help="copy the source into the run")] = False,
    replace: Annotated[bool, typer.Option("--replace", help="overwrite existing predictions")] = False,
    opt: Annotated[list[str] | None, typer.Option("--opt", help="converter option key=value")] = None,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Convert framework output into a run's canonical predictions."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        spec = IngestSpec(
            run_id=run, dataset=dataset, plan_id=plan, subset=subset, format=fmt, src=src,
            export_dir=export_manifest,
            trained_on=[t.strip() for t in (trained_on or "").split(",") if t.strip()],
            framework=framework, notes=notes, keep_input=keep_input, replace=replace,
            options=parse_opts(opt), data_root=data_root, configs_root=configs_root,
        )
        res = ingest(spec)
        det_like = res.run.dataset and res.predicted == 0
        status: Status = "WARN" if res.unknown or det_like else "OK"
        fields: dict[str, FieldValue] = {
            "run": run, "subset": subset, "format": fmt, "samples": res.samples,
            "predicted": res.predicted, "empty": res.empty, "sha": res.sha256[:12],
            "replaced": res.replaced,
        }
        if res.unknown:
            fields["unknown"] = len(res.unknown)
        human = [f"ingested {res.predicted} predictions for {subset!r} into run {run!r} ({res.path})"]
        payload = {"run": res.run.model_dump(mode="json"), "path": str(res.path),
                   "unknown": res.unknown}
        return status, fields, payload, human

    run_command("eval.ingest", json_mode, data_root, fn)


def load_plugins(modules: list[str] | None) -> None:
    """Import user modules so they can register metrics / converters (Task 12 moves this to
    measure/plugins.py; keep the import-based behaviour identical)."""
    import importlib

    from vcp.core.errors import VcpError

    for name in modules or []:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 - surface the plugin's own error text
            raise VcpError(f"cannot import plugin {name!r}: {type(e).__name__}: {e}") from e
```

`src/vcp/cli.py`：在 `app.add_typer(data_app, name="data")` 之後加 `from vcp.cli_eval import eval_app` 與 `app.add_typer(eval_app, name="eval")`（import 放檔頭的 import 區）。

Run: `uv run pytest tests/unit/test_cli_eval.py tests/unit/test_cli.py -q && uv run ruff check . && uv run ruff format --check .`。Expected: PASS（`det_like` 判斷：`predicted == 0` 且沒有 unknown 也 WARN——det/seg 全空、或 cls 不可能為 0；名稱可改為 `nothing_predicted`）。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/measure/ingest.py src/vcp/cli_eval.py src/vcp/cli.py tests/unit/measure/test_ingest.py tests/unit/test_cli_eval.py
git commit -m "feat(measure,cli): ingest 流程與 vcp eval ingest——run 建立與核對、替換留 history、未知 id 失敗封閉

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: 指標登記表與表格類指標（spec §7）

**Files:**
- Create: `src/vcp/measure/metrics/__init__.py`、`base.py`、`tabular.py`
- Test: `tests/unit/measure/test_metrics_tabular.py`

**Interfaces:**
- Consumes: Task 2 的 `MetricResult`、`Prediction`；`vcp.data.schema.Sample / DatasetCard`；scikit-learn（`accuracy_score`、`f1_score`、`log_loss`、`roc_auc_score`）。
- Produces:
  - `metrics.base.Metric` 協定：`name`、`version`、`tasks: frozenset[str]`、`defaults: dict[str, str]`、`compute(self, samples, predictions: dict[str, Prediction], card, params: dict[str, str]) -> MetricResult`
  - `METRICS`、`register_metric`、`get_metric(name)`、`applicable_metrics(task) -> list[str]`（登記順序）、`effective_params(metric, params) -> dict[str, str]`（未知 key → `ValidationFailed`）、`params_key(params) -> str`（`k=v` 依 key 排序以逗號相接）
  - `metrics.base.gold_only(samples) -> list[Sample]`（`labels is None` → `ValidationFailed`）
  - 指標：`accuracy`、`macro_f1`、`log_loss`（tasks `{"cls"}`）、`macro_auc`（`{"multilabel"}`）、`rmse`、`mae`（`{"regression"}`），版本 `"1"`

- [ ] **Step 1: 寫失敗測試**

`tests/unit/measure/test_metrics_tabular.py`：

```python
import math

import pytest

from helpers import (
    ML_CATS,
    REG_CATS,
    cls_samples,
    make_card,
    multilabel_samples,
    noisy_predictions,
    perfect_predictions,
    regression_samples,
)
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.measure.metrics import METRICS, applicable_metrics, get_metric
from vcp.measure.metrics.base import effective_params, params_key
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import Prediction


def _run(metric, samples, card, preds, params=None):
    return get_metric(metric).compute(samples, predictions_by_id(preds), card, params or {})


def test_registry_and_params():
    assert list(METRICS)[:6] == ["accuracy", "macro_f1", "log_loss", "macro_auc", "rmse", "mae"]
    assert applicable_metrics("cls") == ["accuracy", "macro_f1", "log_loss"]
    assert applicable_metrics("multilabel") == ["macro_auc"]
    assert applicable_metrics("regression") == ["rmse", "mae"]
    assert applicable_metrics("det") == []
    with pytest.raises(RegistryError):
        get_metric("bleu")
    assert params_key({"b": "2", "a": "1"}) == "a=1,b=2" and params_key({}) == ""
    assert effective_params(get_metric("accuracy"), {}) == {}
    with pytest.raises(ValidationFailed, match="params"):
        effective_params(get_metric("accuracy"), {"iou": "50"})


def test_cls_metrics_perfect_and_noisy():
    samples = cls_samples(40, seed=1)
    card = make_card("cls")
    perfect = perfect_predictions(samples, card)
    assert _run("accuracy", samples, card, perfect).value == 1.0
    f1 = _run("macro_f1", samples, card, perfect)
    assert f1.value == 1.0 and set(f1.per_class) == {"cat", "dog", "bird"}
    assert _run("log_loss", samples, card, perfect).value < 1e-6
    noisy = noisy_predictions(samples, card, seed=3, flip=0.4)
    assert _run("accuracy", samples, card, noisy).value < 1.0
    assert _run("log_loss", samples, card, noisy).value > 0.05
    res = _run("accuracy", samples, card, perfect)
    assert res.n == 40


def test_multilabel_auc_perfect_and_undefined_class():
    samples = multilabel_samples(60, seed=2, probs=(0.5, 0.3, 0.0))  # third class never positive
    card = make_card("multilabel", categories=ML_CATS)
    res = _run("macro_auc", samples, card, perfect_predictions(samples, card))
    assert res.value == 1.0
    assert res.per_class["acl"] == 1.0 and res.per_class["effusion"] is None
    noisy = noisy_predictions(samples, card, seed=5, flip=0.4)
    assert 0.0 <= _run("macro_auc", samples, card, noisy).value < 1.0
    constant = [Prediction(sample_id=s.sample_id, scores={"acl": 0.5, "mcl": 0.5, "effusion": 0.5})
                for s in samples]
    assert abs(_run("macro_auc", samples, card, constant).value - 0.5) < 1e-9


def test_regression_metrics_and_gold_only():
    samples = regression_samples(30, seed=0)
    card = make_card("regression", categories=REG_CATS)
    perfect = perfect_predictions(samples, card)
    assert _run("rmse", samples, card, perfect).value == 0.0
    assert _run("mae", samples, card, perfect).value == 0.0
    shifted = [Prediction(sample_id=p.sample_id, targets={"age": p.targets["age"] + 2.0}) for p in perfect]
    assert math.isclose(_run("rmse", samples, card, shifted).value, 2.0)
    assert math.isclose(_run("mae", samples, card, shifted).value, 2.0)
    missing = [Prediction(sample_id=p.sample_id, targets={}) for p in perfect]
    with pytest.raises(ValidationFailed, match="missing target"):
        _run("rmse", samples, card, missing)
    unlabeled = cls_samples(5, seed=0, gold_frac=0.0)
    with pytest.raises(ValidationFailed, match="gold labels"):
        _run("accuracy", unlabeled, make_card("cls"), perfect_predictions(unlabeled, make_card("cls")))
    with pytest.raises(ValidationFailed, match="missing prediction"):
        _run("accuracy", cls_samples(3), make_card("cls"), [])
```

Run: `uv run pytest tests/unit/measure/test_metrics_tabular.py -q`。Expected: FAIL（ImportError）。

- [ ] **Step 2: 實作 `metrics/base.py`**

```python
"""Metric contract and registry. A metric must work on any subset of samples (bootstrap needs it)."""

from __future__ import annotations

from typing import Protocol

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.schema import DatasetCard, Sample
from vcp.measure.schema import MetricResult, Prediction


class Metric(Protocol):
    name: str
    version: str
    tasks: frozenset[str]
    defaults: dict[str, str]

    def compute(
        self,
        samples: list[Sample],
        predictions: dict[str, Prediction],
        card: DatasetCard,
        params: dict[str, str],
    ) -> MetricResult: ...


METRICS: dict[str, Metric] = {}


def register_metric(metric: Metric) -> None:
    if metric.name in METRICS:
        raise RegistryError(f"metric {metric.name!r} already registered")
    METRICS[metric.name] = metric


def get_metric(name: str) -> Metric:
    try:
        return METRICS[name]
    except KeyError:
        raise RegistryError(f"unknown metric {name!r}; known: {sorted(METRICS)}") from None


def applicable_metrics(task: str) -> list[str]:
    return [name for name, m in METRICS.items() if task in m.tasks]


def effective_params(metric: Metric, params: dict[str, str]) -> dict[str, str]:
    """Metric defaults overridden by the given params; unknown keys are the user's mistake."""
    unknown = sorted(set(params) - set(metric.defaults))
    if unknown:
        raise ValidationFailed(
            f"metric {metric.name!r} has no params {unknown}; known: {sorted(metric.defaults)}"
        )
    return {**metric.defaults, **params}


def params_key(params: dict[str, str]) -> str:
    return ",".join(f"{k}={params[k]}" for k in sorted(params))


def gold_only(samples: list[Sample]) -> list[Sample]:
    missing = [s.sample_id for s in samples if s.labels is None]
    if missing:
        raise ValidationFailed(
            f"{len(missing)} samples have no gold labels (e.g. {missing[:3]}); evaluate on gold-only subsets"
        )
    return samples


def require_predictions(samples: list[Sample], predictions: dict[str, Prediction]) -> list[Prediction]:
    missing = [s.sample_id for s in samples if s.sample_id not in predictions]
    if missing:
        raise ValidationFailed(f"missing prediction for {len(missing)} samples (e.g. {missing[:3]})")
    return [predictions[s.sample_id] for s in samples]
```

- [ ] **Step 3: 實作 `metrics/tabular.py`**

```python
"""Classification, multi-label and regression metrics on top of scikit-learn."""

from __future__ import annotations

import math

import numpy as np
from sklearn.metrics import accuracy_score, f1_score, log_loss, roc_auc_score

from vcp.core.errors import ValidationFailed
from vcp.data.schema import DatasetCard, Sample
from vcp.measure.metrics.base import gold_only, require_predictions
from vcp.measure.schema import MetricResult, Prediction

EPS = 1e-15


def _names(card: DatasetCard) -> list[str]:
    return [c.name for c in card.categories]


def _cls_arrays(samples, predictions, card) -> tuple[np.ndarray, np.ndarray, list[str]]:
    names = _names(card)
    index_of = {c.id: i for i, c in enumerate(card.categories)}
    preds = require_predictions(gold_only(samples), predictions)
    y_true = np.array([index_of[s.labels.cls] for s in samples], dtype=int)  # type: ignore[union-attr]
    scores = np.array([[p.scores[n] for n in names] for p in preds], dtype=float)  # type: ignore[index]
    return y_true, scores, names


class Accuracy:
    name, version, tasks, defaults = "accuracy", "1", frozenset({"cls"}), {}

    def compute(self, samples, predictions, card, params) -> MetricResult:
        y_true, scores, _ = _cls_arrays(samples, predictions, card)
        return MetricResult(value=float(accuracy_score(y_true, scores.argmax(axis=1))), n=len(y_true))


class MacroF1:
    name, version, tasks, defaults = "macro_f1", "1", frozenset({"cls"}), {}

    def compute(self, samples, predictions, card, params) -> MetricResult:
        y_true, scores, names = _cls_arrays(samples, predictions, card)
        labels = list(range(len(names)))
        per = f1_score(y_true, scores.argmax(axis=1), labels=labels, average=None, zero_division=0)
        return MetricResult(
            value=float(np.mean(per)), per_class={n: float(v) for n, v in zip(names, per, strict=True)},
            n=len(y_true),
        )


class LogLoss:
    name, version, tasks, defaults = "log_loss", "1", frozenset({"cls"}), {}

    def compute(self, samples, predictions, card, params) -> MetricResult:
        y_true, scores, names = _cls_arrays(samples, predictions, card)
        probs = np.clip(scores, EPS, 1.0)
        probs = probs / probs.sum(axis=1, keepdims=True)
        return MetricResult(value=float(log_loss(y_true, probs, labels=list(range(len(names))))), n=len(y_true))


class MacroAuc:
    name, version, tasks, defaults = "macro_auc", "1", frozenset({"multilabel"}), {}

    def compute(self, samples, predictions, card, params) -> MetricResult:
        names = _names(card)
        preds = require_predictions(gold_only(samples), predictions)
        y_true = np.array([[s.labels.targets[n] for n in names] for s in samples], dtype=float)  # type: ignore[index]
        scores = np.array([[p.scores[n] for n in names] for p in preds], dtype=float)  # type: ignore[index]
        per_class: dict[str, float | None] = {}
        for j, n in enumerate(names):
            col = y_true[:, j]
            per_class[n] = float(roc_auc_score(col, scores[:, j])) if 0 < col.sum() < len(col) else None
        defined = [v for v in per_class.values() if v is not None]
        if not defined:
            raise ValidationFailed("macro_auc undefined: every class is all-positive or all-negative")
        return MetricResult(value=float(np.mean(defined)), per_class=per_class, n=len(samples))


def _regression_errors(samples, predictions, card) -> np.ndarray:
    names = _names(card)
    preds = require_predictions(gold_only(samples), predictions)
    errors = []
    for s, p in zip(samples, preds, strict=True):
        for n in names:
            if p.targets is None or n not in p.targets:
                raise ValidationFailed(f"sample {s.sample_id!r}: missing target {n!r}")
            errors.append(p.targets[n] - s.labels.targets[n])  # type: ignore[index]
    return np.array(errors, dtype=float)


class Rmse:
    name, version, tasks, defaults = "rmse", "1", frozenset({"regression"}), {}

    def compute(self, samples, predictions, card, params) -> MetricResult:
        e = _regression_errors(samples, predictions, card)
        return MetricResult(value=float(math.sqrt(np.mean(e**2))), n=len(samples))


class Mae:
    name, version, tasks, defaults = "mae", "1", frozenset({"regression"}), {}

    def compute(self, samples, predictions, card, params) -> MetricResult:
        e = _regression_errors(samples, predictions, card)
        return MetricResult(value=float(np.mean(np.abs(e))), n=len(samples))
```

`metrics/__init__.py`：

```python
"""Metric registry. Importing this package registers the built-in metrics in a fixed order."""

from vcp.measure.metrics.base import (
    METRICS,
    Metric,
    applicable_metrics,
    effective_params,
    get_metric,
    params_key,
    register_metric,
)
from vcp.measure.metrics.tabular import Accuracy, LogLoss, MacroAuc, MacroF1, Mae, Rmse

for _metric in (Accuracy(), MacroF1(), LogLoss(), MacroAuc(), Rmse(), Mae()):
    register_metric(_metric)

__all__ = ["METRICS", "Metric", "applicable_metrics", "effective_params", "get_metric",
           "params_key", "register_metric"]
```

Run: `uv run pytest tests/unit/measure -q && uv run ruff check . && uv run ruff format --check .`。Expected: PASS。若 `multilabel_samples(..., probs=(0.5, 0.3, 0.0))` 讓 `effusion` 全 0 但 `acl` 也恰好全同值，換 seed 讓測試穩定（種子固定即決定性）。

- [ ] **Step 4: Commit**

```bash
git add src/vcp/measure/metrics tests/unit/measure/test_metrics_tabular.py
git commit -m "feat(measure): 指標登記表與 accuracy / macro_f1 / log_loss / macro_auc / rmse / mae

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: `coco_map` 指標（spec §7，pycocotools）

**Files:**
- Create: `src/vcp/measure/metrics/coco_map.py`
- Modify: `src/vcp/measure/metrics/__init__.py`（登記，排在 `accuracy` 之前，使 `applicable_metrics("det") == ["coco_map"]`）
- Test: `tests/unit/measure/test_metrics_coco.py`

**Interfaces:**
- Consumes: Task 6 的 `gold_only`、`register_metric`、`MetricResult`；pycocotools（`COCO`、`COCOeval`）。
- Produces: `CocoMap`（name `coco_map`、version `1`、tasks `{"det"}`、defaults `{"iou": "50:95", "max_dets": "100"}`）、`coco_map.require_pycocotools()`（缺 → `VcpError` 含 `uv sync --extra eval`）、`coco_map.INSTALL_HINT`。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/measure/test_metrics_coco.py`：

```python
import builtins

import pytest

from helpers import det_samples, make_card, perfect_predictions
from vcp.core.errors import ValidationFailed, VcpError
from vcp.data.schema import Sample, View
from vcp.measure.metrics import applicable_metrics, get_metric
from vcp.measure.metrics import coco_map as coco_module
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import PredBox, Prediction


def _run(samples, card, preds, params=None):
    return get_metric("coco_map").compute(samples, predictions_by_id(preds), card, params or {})


def test_registered_for_det():
    assert applicable_metrics("det") == ["coco_map"]
    m = get_metric("coco_map")
    assert m.defaults == {"iou": "50:95", "max_dets": "100"}


def test_perfect_shifted_and_empty():
    samples = det_samples(30, seed=4)
    card = make_card("det")
    perfect = perfect_predictions(samples, card)
    res = _run(samples, card, perfect)
    assert res.value == pytest.approx(1.0) and res.n == 30
    assert set(res.per_class) == {"cat", "dog", "bird"}
    assert all(v in (None, pytest.approx(1.0)) for v in res.per_class.values())
    assert _run(samples, card, perfect, {"iou": "50"}).value == pytest.approx(1.0)
    shifted = [
        Prediction(sample_id=p.sample_id,
                   boxes=[b.model_copy(update={"x": b.x + 3.0}) for b in p.boxes])
        for p in perfect
    ]
    assert _run(samples, card, shifted).value < 0.9
    empty = [Prediction(sample_id=p.sample_id, boxes=[]) for p in perfect]
    assert _run(samples, card, empty).value == 0.0
    with pytest.raises(ValidationFailed, match="params"):
        _run(samples, card, perfect, {"nms": "0.5"})


def test_requires_sizes_gold_and_gt_boxes():
    card = make_card("det")
    unsized = [Sample(sample_id="a", views=[View(path="a.jpg")], labels=None, label_source="none")]
    with pytest.raises(ValidationFailed, match="gold"):
        _run(unsized, card, [Prediction(sample_id="a", boxes=[])])
    nosize = [Sample(sample_id="a", views=[View(path="a.jpg")],
                     labels={"boxes": [{"x": 0, "y": 0, "w": 1, "h": 1, "category_id": 0}]},
                     label_source="gold")]
    with pytest.raises(ValidationFailed, match="size"):
        _run(nosize, card, [Prediction(sample_id="a", boxes=[])])
    negatives = [Sample(sample_id="a", views=[View(path="a.jpg", width=8, height=8)],
                        labels={"boxes": []}, label_source="gold")]
    with pytest.raises(ValidationFailed, match="ground-truth"):
        _run(negatives, card, [Prediction(sample_id="a", boxes=[PredBox(x=0, y=0, w=1, h=1,
                                                                        category_id=0, score=1)])])


def test_missing_pycocotools_is_abort(monkeypatch):
    real_import = builtins.__import__

    def fake(name, *args, **kwargs):
        if name.startswith("pycocotools"):
            raise ImportError("no pycocotools")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(VcpError, match="uv sync --extra eval"):
        coco_module.require_pycocotools()
```

Run: `uv run pytest tests/unit/measure/test_metrics_coco.py -q`。Expected: FAIL（ImportError）。

- [ ] **Step 2: 實作 `coco_map.py`**

```python
"""COCO-style mAP (pycocotools). Ground truth and detections are built in memory from the
canonical samples / predictions, so the metric works on any subset of samples."""

from __future__ import annotations

import contextlib
import io
from typing import Any

import numpy as np

from vcp.core.errors import ValidationFailed, VcpError
from vcp.data.schema import DatasetCard, Sample
from vcp.measure.metrics.base import gold_only
from vcp.measure.schema import MetricResult, Prediction

INSTALL_HINT = "COCO mAP needs the 'eval' extra: uv sync --extra eval"
IOU_PRESETS: dict[str, list[float]] = {
    "50": [0.5],
    "75": [0.75],
    "50:95": [round(x, 2) for x in np.linspace(0.5, 0.95, 10)],
}


def require_pycocotools() -> tuple[Any, Any]:
    try:
        from pycocotools.coco import COCO
        from pycocotools.cocoeval import COCOeval
    except ImportError as e:
        raise VcpError(INSTALL_HINT) from e
    return COCO, COCOeval


def _gt_dataset(samples: list[Sample], card: DatasetCard) -> dict[str, Any]:
    images, annotations = [], []
    for i, s in enumerate(samples, start=1):
        v = s.views[0]
        if v.width is None or v.height is None:
            raise ValidationFailed(f"sample {s.sample_id!r} view has no size; re-import the dataset")
        images.append({"id": i, "width": v.width, "height": v.height})
        for b in (s.labels.boxes if s.labels else None) or []:  # type: ignore[union-attr]
            annotations.append({
                "id": len(annotations) + 1, "image_id": i, "category_id": b.category_id,
                "bbox": [b.x, b.y, b.w, b.h], "area": b.w * b.h, "iscrowd": 0,
            })
    if not annotations:
        raise ValidationFailed("no ground-truth boxes in this subset; mAP is undefined")
    return {"images": images, "annotations": annotations,
            "categories": [{"id": c.id, "name": c.name} for c in card.categories]}


class CocoMap:
    name = "coco_map"
    version = "1"
    tasks = frozenset({"det"})
    defaults = {"iou": "50:95", "max_dets": "100"}

    def compute(
        self, samples: list[Sample], predictions: dict[str, Prediction], card: DatasetCard,
        params: dict[str, str],
    ) -> MetricResult:
        from vcp.measure.metrics.base import effective_params

        p = effective_params(self, params)
        if p["iou"] not in IOU_PRESETS:
            raise ValidationFailed(f"iou must be one of {sorted(IOU_PRESETS)}, got {p['iou']!r}")
        max_dets = int(p["max_dets"])
        COCO, COCOeval = require_pycocotools()
        samples = gold_only(samples)
        gt = COCO()
        gt.dataset = _gt_dataset(samples, card)
        with contextlib.redirect_stdout(io.StringIO()):
            gt.createIndex()
        dts = [
            {"image_id": i, "category_id": b.category_id, "bbox": [b.x, b.y, b.w, b.h],
             "score": b.score}
            for i, s in enumerate(samples, start=1)
            for b in ((predictions[s.sample_id].boxes if s.sample_id in predictions else None) or [])
        ]
        names = {c.id: c.name for c in card.categories}
        if not dts:
            return MetricResult(value=0.0, per_class={n: 0.0 for n in names.values()}, n=len(samples))
        with contextlib.redirect_stdout(io.StringIO()):
            dt = gt.loadRes(dts)
            ev = COCOeval(gt, dt, "bbox")
            ev.params.iouThrs = np.array(IOU_PRESETS[p["iou"]])
            ev.params.maxDets = sorted({1, 10, max_dets})
            ev.evaluate()
            ev.accumulate()
        precision = ev.eval["precision"][:, :, :, 0, -1]  # thresholds × recall × class (area all, maxDets)
        valid = precision > -1
        value = float(precision[valid].mean()) if valid.any() else 0.0
        per_class: dict[str, float | None] = {}
        for k, cat_id in enumerate(ev.params.catIds):
            pk = precision[:, :, k]
            per_class[names[cat_id]] = float(pk[pk > -1].mean()) if (pk > -1).any() else None
        return MetricResult(value=value, per_class=per_class, n=len(samples))
```

`metrics/__init__.py`：import `CocoMap` 並把登記迴圈改為 `(CocoMap(), Accuracy(), MacroF1(), LogLoss(), MacroAuc(), Rmse(), Mae())`；同步更新 `test_metrics_tabular.py::test_registry_and_params` 的 `list(METRICS)[:7]` 期望為 `["coco_map", "accuracy", ...]`。

Run: `uv run pytest tests/unit/measure -q && uv run ruff check . && uv run ruff format --check .`。Expected: PASS。若 `shifted` 的 mAP 沒有 < 0.9（框太小、位移 3 px 後仍有重疊），把位移改 5 px；勿改實作。

- [ ] **Step 3: Commit**

```bash
git add src/vcp/measure/metrics tests/unit/measure/test_metrics_coco.py tests/unit/measure/test_metrics_tabular.py
git commit -m "feat(measure): coco_map 指標（pycocotools，iou 50/75/50:95，逐類 AP）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: mask 柵格化與 `dice` / `miou`（spec §7）

**Files:**
- Create: `src/vcp/measure/masks.py`、`src/vcp/measure/metrics/seg.py`
- Modify: `src/vcp/measure/metrics/__init__.py`（登記 `Dice()`、`MIoU()` 在最後）
- Test: `tests/unit/measure/test_masks.py`、`tests/unit/measure/test_metrics_seg.py`

**Interfaces:**
- Consumes: Task 2 的 `PredMask`、`seg_samples`、`perfect_predictions`；Task 6 的 `gold_only`、`require_predictions`；Task 7 的 `require_pycocotools`（壓縮 RLE 解碼）；Pillow `ImageDraw`。
- Produces:
  - `masks.rasterize_polygon(polygons: list[list[float]], width, height) -> np.ndarray[bool]`
  - `masks.decode_rle(rle: str, meta: dict, width, height) -> np.ndarray[bool]`（`meta["rle_encoding"] == "uncompressed"` 時 counts 為逗號分隔、column-major；否則視為 COCO 壓縮字串，經 pycocotools）
  - `masks.mask_array(mask: Mask | PredMask, width, height) -> np.ndarray[bool]`
  - `Dice`（name `dice`、tasks `{"seg"}`、defaults `{"threshold": "0.5"}`）、`MIoU`（name `miou`）——資料集層級逐類累積交集 / 面積，再對有定義的類別取平均；`per_class` 無定義者為 None

- [ ] **Step 1: 寫失敗測試**

`tests/unit/measure/test_masks.py`：

```python
import numpy as np
import pytest

from vcp.core.errors import ValidationFailed
from vcp.data.schema import Mask
from vcp.measure.masks import decode_rle, mask_array, rasterize_polygon
from vcp.measure.schema import PredMask


def test_rasterize_polygon_axis_aligned():
    m = rasterize_polygon([[1, 1, 4, 1, 4, 3, 1, 3]], 6, 5)
    assert m.shape == (5, 6) and m.dtype == bool
    assert m.sum() == 12 and m[1, 1] and m[3, 4] and not m[0, 0] and not m[4, 5]


def test_uncompressed_rle_is_column_major():
    # 3 wide x 2 high; counts over column-major order: 1 zero, 2 ones, 3 zeros -> column 0 rows 1.. set
    m = decode_rle("1,2,3", {"rle_encoding": "uncompressed", "size": [2, 3]}, 3, 2)
    assert m.shape == (2, 3) and m.tolist() == [[False, True, False], [True, False, False]]


def test_compressed_rle_roundtrip_via_pycocotools():
    from pycocotools import mask as mask_util

    arr = np.zeros((4, 5), dtype=np.uint8)
    arr[1:3, 2:4] = 1
    enc = mask_util.encode(np.asfortranarray(arr))
    counts = enc["counts"].decode("ascii")
    m = decode_rle(counts, {"size": [4, 5]}, 5, 4)
    assert m.tolist() == arr.astype(bool).tolist()


def test_mask_array_dispatch_and_errors():
    poly = Mask(category_id=0, polygon=[[0, 0, 2, 0, 2, 2, 0, 2]])
    assert mask_array(poly, 4, 4).sum() == 4
    pred = PredMask(category_id=0, score=0.9, rle="1,2,3", meta={"rle_encoding": "uncompressed"})
    assert mask_array(pred, 3, 2).sum() == 2
    with pytest.raises(ValidationFailed, match="degenerate"):
        rasterize_polygon([[0, 0, 1, 1]], 4, 4)
```

`tests/unit/measure/test_metrics_seg.py`：

```python
import pytest

from helpers import SEG_CATS, make_card, perfect_predictions, seg_samples
from vcp.measure.metrics import applicable_metrics, get_metric
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import PredMask, Prediction


def _run(metric, samples, card, preds, params=None):
    return get_metric(metric).compute(samples, predictions_by_id(preds), card, params or {})


def test_registered_and_perfect():
    assert applicable_metrics("seg") == ["dice", "miou"]
    samples = seg_samples(20, seed=0)
    card = make_card("seg", categories=SEG_CATS)
    perfect = perfect_predictions(samples, card)
    for name in ("dice", "miou"):
        res = _run(name, samples, card, perfect)
        assert res.value == pytest.approx(1.0) and res.n == 20
        assert res.per_class == {"road": pytest.approx(1.0), "water": pytest.approx(1.0)}


def test_empty_shifted_and_threshold():
    samples = seg_samples(20, seed=1)
    card = make_card("seg", categories=SEG_CATS)
    perfect = perfect_predictions(samples, card)
    empty = [Prediction(sample_id=p.sample_id, masks=[]) for p in perfect]
    assert _run("dice", samples, card, empty).value == 0.0
    assert _run("miou", samples, card, empty).value == 0.0
    shifted = [
        Prediction(sample_id=p.sample_id, masks=[
            PredMask(category_id=m.category_id, score=1.0,
                     polygon=[[v + (1 if i % 2 == 0 else 0) for i, v in enumerate(ring)] for ring in m.polygon])
            for m in p.masks
        ])
        for p in perfect
    ]
    d = _run("dice", samples, card, shifted).value
    assert 0.0 < d < 1.0
    assert _run("miou", samples, card, shifted).value < d
    low = [Prediction(sample_id=p.sample_id,
                      masks=[m.model_copy(update={"score": 0.2}) for m in p.masks]) for p in perfect]
    assert _run("dice", samples, card, low).value == 0.0
    assert _run("dice", samples, card, low, {"threshold": "0.1"}).value == pytest.approx(1.0)
```

Run: `uv run pytest tests/unit/measure/test_masks.py tests/unit/measure/test_metrics_seg.py -q`。Expected: FAIL（ImportError）。

- [ ] **Step 2: 實作 `masks.py`**

```python
"""Rasterise canonical masks (polygon / RLE) into boolean arrays of a view's size."""

from __future__ import annotations

from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from vcp.core.errors import ValidationFailed
from vcp.data.schema import Mask
from vcp.measure.schema import PredMask


def rasterize_polygon(polygons: list[list[float]], width: int, height: int) -> np.ndarray:
    img = Image.new("1", (width, height), 0)
    draw = ImageDraw.Draw(img)
    for ring in polygons:
        if len(ring) < 6 or len(ring) % 2:
            raise ValidationFailed(f"degenerate polygon ring with {len(ring)} numbers")
        draw.polygon([(ring[i], ring[i + 1]) for i in range(0, len(ring), 2)], fill=1)
    return np.asarray(img, dtype=bool)


def decode_rle(rle: str, meta: dict[str, Any], width: int, height: int) -> np.ndarray:
    size = meta.get("size") or [height, width]
    h, w = int(size[0]), int(size[1])
    if (h, w) != (height, width):
        raise ValidationFailed(f"RLE size {[h, w]} does not match the view {[height, width]}")
    if meta.get("rle_encoding") == "uncompressed":
        try:
            counts = [int(c) for c in rle.split(",") if c.strip()]
        except ValueError as e:
            raise ValidationFailed(f"bad uncompressed RLE counts: {e}") from e
        flat = np.zeros(h * w, dtype=bool)
        pos, value = 0, False
        for run in counts:
            if value:
                flat[pos : pos + run] = True
            pos += run
            value = not value
        if pos != h * w:
            raise ValidationFailed(f"RLE counts cover {pos} pixels, view has {h * w}")
        return flat.reshape((h, w), order="F")
    from vcp.measure.metrics.coco_map import require_pycocotools

    require_pycocotools()
    from pycocotools import mask as mask_util

    decoded = mask_util.decode({"size": [h, w], "counts": rle.encode("ascii")})
    return np.asarray(decoded, dtype=bool)


def mask_array(mask: Mask | PredMask, width: int, height: int) -> np.ndarray:
    if mask.polygon is not None:
        return rasterize_polygon(mask.polygon, width, height)
    if mask.rle is not None:
        return decode_rle(mask.rle, mask.meta, width, height)
    raise ValidationFailed("mask has neither polygon nor rle")
```

- [ ] **Step 3: 實作 `metrics/seg.py`**

```python
"""Dataset-level Dice and mIoU: intersection / areas accumulated per class over all samples,
then averaged over the classes that have any ground truth or prediction."""

from __future__ import annotations

import numpy as np

from vcp.core.errors import ValidationFailed
from vcp.data.schema import DatasetCard, Sample
from vcp.measure.masks import mask_array
from vcp.measure.metrics.base import effective_params, gold_only
from vcp.measure.schema import MetricResult, Prediction


def _class_totals(samples, predictions, card, threshold) -> tuple[dict[int, float], dict[int, float], dict[int, float]]:
    inter: dict[int, float] = {c.id: 0.0 for c in card.categories}
    gt_area = dict(inter)
    pr_area = dict(inter)
    for s in gold_only(samples):
        v = s.views[0]
        if v.width is None or v.height is None:
            raise ValidationFailed(f"sample {s.sample_id!r} view has no size; re-import the dataset")
        gt = {cid: np.zeros((v.height, v.width), dtype=bool) for cid in inter}
        for m in (s.labels.masks if s.labels else None) or []:  # type: ignore[union-attr]
            gt[m.category_id] |= mask_array(m, v.width, v.height)
        pr = {cid: np.zeros((v.height, v.width), dtype=bool) for cid in inter}
        pred = predictions.get(s.sample_id)
        for m in (pred.masks if pred else None) or []:
            if m.score >= threshold:
                pr[m.category_id] |= mask_array(m, v.width, v.height)
        for cid in inter:
            inter[cid] += float(np.logical_and(gt[cid], pr[cid]).sum())
            gt_area[cid] += float(gt[cid].sum())
            pr_area[cid] += float(pr[cid].sum())
    return inter, gt_area, pr_area


def _summarise(card, per_id: dict[int, float | None], n: int) -> MetricResult:
    names = {c.id: c.name for c in card.categories}
    per_class = {names[cid]: v for cid, v in per_id.items()}
    defined = [v for v in per_class.values() if v is not None]
    return MetricResult(value=float(np.mean(defined)) if defined else 0.0, per_class=per_class, n=n)


class Dice:
    name, version, tasks, defaults = "dice", "1", frozenset({"seg"}), {"threshold": "0.5"}

    def compute(self, samples, predictions, card, params) -> MetricResult:
        threshold = float(effective_params(self, params)["threshold"])
        inter, gt, pr = _class_totals(samples, predictions, card, threshold)
        per = {cid: (2 * inter[cid] / (gt[cid] + pr[cid]) if gt[cid] + pr[cid] > 0 else None) for cid in inter}
        return _summarise(card, per, len(samples))


class MIoU:
    name, version, tasks, defaults = "miou", "1", frozenset({"seg"}), {"threshold": "0.5"}

    def compute(self, samples, predictions, card, params) -> MetricResult:
        threshold = float(effective_params(self, params)["threshold"])
        inter, gt, pr = _class_totals(samples, predictions, card, threshold)
        per = {}
        for cid in inter:
            union = gt[cid] + pr[cid] - inter[cid]
            per[cid] = inter[cid] / union if union > 0 else None
        return _summarise(card, per, len(samples))
```

`metrics/__init__.py`：import `Dice, MIoU` 並登記在最後（順序：coco_map, accuracy, macro_f1, log_loss, macro_auc, rmse, mae, dice, miou）。

Run: `uv run pytest tests/unit/measure -q && uv run ruff check . && uv run ruff format --check .`。Expected: PASS。`seg_samples` 的矩形頂點在 8×8 內；Pillow 的 `polygon` 填滿含邊界，`rasterize_polygon([[1,1,4,1,4,3,1,3]], 6, 5)` 為 4×3 = 12 像素。

- [ ] **Step 4: Commit**

```bash
git add src/vcp/measure/masks.py src/vcp/measure/metrics tests/unit/measure/test_masks.py tests/unit/measure/test_metrics_seg.py
git commit -m "feat(measure): mask 柵格化（polygon / RLE）與 dice / miou 指標

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: 讀數台帳、錨點護欄、`measure_run`、`vcp eval measure` / `anchor`（spec §4.3、§4.4、§6、§6.1、§9）

**Files:**
- Create: `src/vcp/measure/ledger.py`、`src/vcp/measure/anchors.py`、`src/vcp/measure/measure.py`
- Modify: `src/vcp/cli_eval.py`（`measure`、`anchor` 命令）
- Test: `tests/unit/measure/test_ledger.py`、`tests/unit/measure/test_measure.py`、`tests/unit/test_cli_eval.py`

**Interfaces:**
- Consumes: Task 2（`Reading`、`GuardrailInfo`、`Anchor`、`load_run`、`verify_prediction`、`read_predictions`、`predictions_by_id`）、Task 6（`get_metric`、`applicable_metrics`、`effective_params`、`params_key`）、Task 1（`DatasetPaths.measure_dir`、`GuardrailError`）、`vcp.data.lineage.clean_eval_subsets`、`Dataset.subset(..., unseal, reason, caller, paths)`、`vcp.data.split.load_plan`、`vcp.core.paths.resolve_data_root`。
- Produces:
  - `ledger.append_row(path, model)`、`ledger.read_rows(path, model_cls) -> list[T]`、`ledger.reading_id(run_id, plan_id, subset, metric, metric_version, params_key, prediction_sha) -> str`、`ledger.ReadingsLedger(path)`（`.rows`、`.by_id`、`.append(reading)`）
  - `anchors.anchor_key(plan_id, subset, metric, params_key) -> str`、`anchors.load_anchors(paths) -> dict[str, Anchor]`、`anchors.set_anchor(paths, key, anchor, *, replace=False)`（已存在且未 replace → `ValidationFailed`；寫 `anchors.json` 並 append `anchors.log.jsonl`）
  - `measure.MeasureSpec(run_id, metrics=[], subsets=[], params={}, unseal=False, reason=None, data_root=None, configs_root=None)`、`measure.MeasureResult(run_id, dataset, readings: list[Reading], new: int, cached: int, guardrail: str, warnings: list[str])`
  - `measure.load_context(run_id, data_root, configs_root) -> tuple[RunCard, Dataset, SplitPlan, DatasetPaths]`（run 與資料集 hash 不符 → `PlanMismatchError`）
  - `measure.default_subsets(plan, card, *, unseal) -> list[str]`
  - `measure.measure_run(spec) -> MeasureResult`（護欄不符 → `GuardrailError`，且不寫任何讀數）
  - CLI `vcp eval measure`、`vcp eval anchor`

- [ ] **Step 1: 台帳與錨點的失敗測試**

`tests/unit/measure/test_ledger.py`：

```python
import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.measure.anchors import anchor_key, load_anchors, set_anchor
from vcp.measure.ledger import ReadingsLedger, append_row, read_rows, reading_id
from vcp.measure.schema import Anchor, Reading


def _reading(rid="x", value=0.5):
    return Reading(reading_id=rid, ts="2026-09-04T00:00:00.000Z", run_id="r", dataset="ds",
                   samples_hash="h", plan_id="p", subset="valA", metric="accuracy",
                   metric_version="1", params={}, value=value, per_class=None, n_samples=3,
                   prediction_sha="s")


def test_reading_id_is_deterministic_and_sensitive():
    a = reading_id("r", "p", "valA", "accuracy", "1", "", "s")
    assert a == reading_id("r", "p", "valA", "accuracy", "1", "", "s") and len(a) == 64
    assert a != reading_id("r", "p", "valA", "accuracy", "1", "", "s2")
    assert a != reading_id("r", "p", "valA", "accuracy", "2", "", "s")


def test_ledger_append_only_and_dedup(tmp_path):
    path = tmp_path / "readings.jsonl"
    ledger = ReadingsLedger(path)
    assert ledger.rows == [] and read_rows(path, Reading) == []
    ledger.append(_reading("a"))
    ledger.append(_reading("b", 0.7))
    with pytest.raises(ValidationFailed, match="already"):
        ledger.append(_reading("a"))
    again = ReadingsLedger(path)
    assert [r.reading_id for r in again.rows] == ["a", "b"] and again.by_id["b"].value == 0.7
    raw = path.read_bytes()
    assert b"\r\n" not in raw and raw.count(b"\n") == 2
    path.write_text(raw.decode("utf-8") + "not json\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValidationFailed, match="readings.jsonl:3"):
        read_rows(path, Reading)
    append_row(tmp_path / "other.jsonl", _reading("c"))
    assert len(read_rows(tmp_path / "other.jsonl", Reading)) == 1


def test_anchors_set_replace_and_log(roots):
    paths = DatasetPaths.resolve("ds", data_root=roots.data, configs_root=roots.configs)
    key = anchor_key("p", "valA", "accuracy", "")
    assert key == "p/valA/accuracy/"
    a = Anchor(run_id="r", reading_id="x", value=0.5, tolerance=1e-6, set_at="2026-09-04T00:00:00.000Z")
    set_anchor(paths, key, a)
    assert load_anchors(paths)[key] == a
    with pytest.raises(ValidationFailed, match="--replace"):
        set_anchor(paths, key, a.model_copy(update={"value": 0.6}))
    set_anchor(paths, key, a.model_copy(update={"value": 0.6}), replace=True)
    assert load_anchors(paths)[key].value == 0.6
    log = (paths.measure_dir / "anchors.log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) == 2 and '"action": "replace"' in log[1]
```

Run: `uv run pytest tests/unit/measure/test_ledger.py -q`。Expected: FAIL（ImportError）。

- [ ] **Step 2: 實作 `ledger.py` 與 `anchors.py`**

`ledger.py`：

```python
"""Append-only jsonl ledgers and the identity of a reading."""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_text
from vcp.measure.schema import Reading

T = TypeVar("T", bound=BaseModel)


def append_row(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(model.model_dump_json() + "\n")


def read_rows(path: Path, model_cls: type[T]) -> list[T]:
    if not path.is_file():
        return []
    rows: list[T] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                rows.append(model_cls.model_validate_json(line))
            except ValidationError as e:
                raise ValidationFailed(f"bad ledger row: {e}", location=f"{path.name}:{lineno}") from e
    return rows


def reading_id(
    run_id: str, plan_id: str, subset: str, metric: str, metric_version: str, params_key: str,
    prediction_sha: str,
) -> str:
    return sha256_text("|".join([run_id, plan_id, subset, metric, metric_version, params_key, prediction_sha]))


class ReadingsLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[Reading] = read_rows(path, Reading)
        self.by_id: dict[str, Reading] = {r.reading_id: r for r in self.rows}

    def append(self, reading: Reading) -> None:
        if reading.reading_id in self.by_id:
            raise ValidationFailed(f"reading {reading.reading_id[:12]} already in the ledger")
        append_row(self.path, reading)
        self.rows.append(reading)
        self.by_id[reading.reading_id] = reading
```

`anchors.py`：

```python
"""One anchor reading per plan/subset/metric/params: the guardrail every measurement must reproduce."""

from __future__ import annotations

import json

from pydantic import TypeAdapter

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.measure.schema import Anchor

_ADAPTER = TypeAdapter(dict[str, Anchor])


def anchor_key(plan_id: str, subset: str, metric: str, params_key: str) -> str:
    return f"{plan_id}/{subset}/{metric}/{params_key}"


def load_anchors(paths: DatasetPaths) -> dict[str, Anchor]:
    path = paths.measure_dir / "anchors.json"
    if not path.is_file():
        return {}
    return _ADAPTER.validate_json(path.read_text(encoding="utf-8"))


def set_anchor(paths: DatasetPaths, key: str, anchor: Anchor, *, replace: bool = False) -> None:
    anchors = load_anchors(paths)
    action = "set"
    if key in anchors:
        if not replace:
            raise ValidationFailed(f"anchor {key!r} already set (run {anchors[key].run_id!r}); pass --replace")
        action = "replace"
    anchors[key] = anchor
    paths.measure_dir.mkdir(parents=True, exist_ok=True)
    with (paths.measure_dir / "anchors.json").open("w", encoding="utf-8", newline="\n") as f:
        json.dump({k: a.model_dump(mode="json") for k, a in sorted(anchors.items())}, f,
                  ensure_ascii=False, indent=1)
        f.write("\n")
    with (paths.measure_dir / "anchors.log.jsonl").open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"ts": stamp(), "action": action, "key": key,
                            **anchor.model_dump(mode="json")}, ensure_ascii=False) + "\n")
```

Run: `uv run pytest tests/unit/measure/test_ledger.py -q`。Expected: PASS。

- [ ] **Step 3: `measure_run` 的失敗測試**

`tests/unit/measure/test_measure.py`：

```python
import pytest

from helpers import det_samples, make_card, noisy_predictions, perfect_predictions, write_images
from vcp.core.errors import GuardrailError, PlanMismatchError, SealedSubsetError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.anchors import anchor_key, set_anchor
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.measure import MeasureSpec, default_subsets, measure_run
from vcp.measure.metrics import get_metric
from vcp.measure.predictions import write_predictions
from vcp.measure.schema import Anchor


def det_with_runs(roots, tmp_path, *, n=60):
    """det dataset + fixed-v1 plan + run 'perfect' (valA, valB, holdout) and run 'noisy' (valA, valB)."""
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / "tiny", samples)
    ds = Dataset.from_parts(make_card("det", image_root="raw/tiny"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    for run_id, maker in (("perfect", perfect_predictions), ("noisy", noisy_predictions)):
        for subset in ("valA", "valB", "holdout"):
            if run_id == "noisy" and subset == "holdout":
                continue
            sub = ds.subset(subset, plan, unseal=True, reason="fixture", paths=paths)
            src = tmp_path / f"{run_id}-{subset}.jsonl"
            write_predictions(src, maker(sub, ds.card))
            ingest(IngestSpec(run_id=run_id, dataset="tiny", plan_id="fixed-v1", subset=subset,
                              format="jsonl", src=src, trained_on=["train"],
                              data_root=roots.data, configs_root=roots.configs))
    return ds, plan, paths


def _spec(roots, run_id, **kw):
    return MeasureSpec(run_id=run_id, data_root=roots.data, configs_root=roots.configs, **kw)


def test_measure_default_subsets_and_cache(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    res = measure_run(_spec(roots, "perfect"))
    assert {r.subset for r in res.readings} == {"valA", "valB"}  # holdout sealed, train excluded
    assert {r.metric for r in res.readings} == {"coco_map"}
    assert all(r.value == pytest.approx(1.0) for r in res.readings)
    assert res.new == 2 and res.cached == 0 and res.guardrail == "none" and res.warnings
    again = measure_run(_spec(roots, "perfect"))
    assert again.new == 0 and again.cached == 2 and again.guardrail == "cached"
    ledger = ReadingsLedger(paths.measure_dir / "readings.jsonl")
    assert len(ledger.rows) == 2 and ledger.rows[0].prediction_sha
    noisy = measure_run(_spec(roots, "noisy"))
    assert all(r.value < 1.0 for r in noisy.readings)
    with pytest.raises(ValidationFailed, match="trained_on"):
        measure_run(_spec(roots, "perfect", subsets=["train"]))
    with pytest.raises(ValidationFailed, match="no predictions"):
        measure_run(_spec(roots, "noisy", subsets=["holdout"]))
    with pytest.raises(SealedSubsetError):
        measure_run(_spec(roots, "perfect", subsets=["holdout"]))
    sealed = measure_run(_spec(roots, "perfect", subsets=["holdout"], unseal=True, reason="final"))
    assert sealed.new == 1
    assert (paths.splits_dir / "fixed-v1.unseal.jsonl").is_file()
    with pytest.raises(ValidationFailed, match="not applicable"):
        measure_run(_spec(roots, "perfect", metrics=["accuracy"]))
    assert default_subsets(plan, res.readings and __import__("vcp.measure.runs", fromlist=["load_run"]).load_run(roots.data, "perfect"), unseal=False) == ["valA", "valB"]


def test_params_only_reach_metrics_that_declare_them(roots, tmp_path):
    det_with_runs(roots, tmp_path, n=40)
    res = measure_run(_spec(roots, "perfect", params={"iou": "50"}))
    assert all(r.params == {"iou": "50", "max_dets": "100"} for r in res.readings)
    with pytest.raises(ValidationFailed, match="params"):
        measure_run(_spec(roots, "perfect", params={"nms": "1"}))


def test_guardrail_aborts_before_writing(roots, tmp_path, monkeypatch):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=40)
    first = measure_run(_spec(roots, "perfect"))
    valA = next(r for r in first.readings if r.subset == "valA")
    set_anchor(paths, anchor_key("fixed-v1", "valA", "coco_map", "iou=50:95,max_dets=100"),
               Anchor(run_id="perfect", reading_id=valA.reading_id, value=valA.value,
                      tolerance=1e-6, set_at="2026-09-04T00:00:00.000Z"))
    ok = measure_run(_spec(roots, "noisy"))
    assert ok.guardrail == "partial"  # valA anchored, valB not
    assert next(r for r in ok.readings if r.subset == "valA").guardrail.ok is True
    before = len(ReadingsLedger(paths.measure_dir / "readings.jsonl").rows)
    metric = get_metric("coco_map")
    real = metric.compute

    def drifted(samples, predictions, card, params):
        r = real(samples, predictions, card, params)
        return r.model_copy(update={"value": r.value - 0.01})

    monkeypatch.setattr(type(metric), "compute", lambda self, *a, **k: drifted(*a, **k))
    with pytest.raises(GuardrailError, match="anchor"):
        measure_run(_spec(roots, "noisy", subsets=["valA", "valB"], metrics=["coco_map"],
                          params={"iou": "50"}))  # new params -> no cache, guardrail recomputed
    assert len(ReadingsLedger(paths.measure_dir / "readings.jsonl").rows) == before


def test_run_dataset_mismatch(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=30)
    samples = det_samples(31, seed=9)
    write_images(roots.data / "raw" / "tiny", samples)
    Dataset.from_parts(make_card("det", image_root="raw/tiny"), samples).save(paths)  # re-import
    with pytest.raises(PlanMismatchError, match="samples_hash"):
        measure_run(_spec(roots, "perfect"))
```

Run: `uv run pytest tests/unit/measure/test_measure.py -q`。Expected: FAIL（ImportError）。（`test_measure_default_subsets_and_cache` 最後一行的 `default_subsets` 呼叫請改寫成先 `from vcp.measure.runs import load_run` 再 `default_subsets(plan, load_run(roots.data, "perfect"), unseal=False)`。）

- [ ] **Step 4: 實作 `measure.py`**

```python
"""vcp eval measure: guardrail first, then one reading per (subset, metric) into the ledger."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import GuardrailError, PlanMismatchError, ValidationFailed
from vcp.core.paths import DatasetPaths, resolve_data_root
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.lineage import clean_eval_subsets
from vcp.data.split import SplitPlan, load_plan
from vcp.measure.anchors import anchor_key, load_anchors
from vcp.measure.ledger import ReadingsLedger, reading_id
from vcp.measure.metrics import applicable_metrics, effective_params, get_metric, params_key
from vcp.measure.predictions import predictions_by_id, read_predictions
from vcp.measure.runs import load_run, verify_prediction
from vcp.measure.schema import GuardrailInfo, Reading, RunCard


class MeasureSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    metrics: list[str] = Field(default_factory=list)
    subsets: list[str] = Field(default_factory=list)
    params: dict[str, str] = Field(default_factory=dict)
    unseal: bool = False
    reason: str | None = None
    data_root: Path | None = None
    configs_root: Path | None = None


class MeasureResult(BaseModel):
    run_id: str
    dataset: str
    readings: list[Reading]
    new: int
    cached: int
    guardrail: str  # OK | partial | none | cached
    warnings: list[str]


def load_context(
    run_id: str, data_root: Path | None, configs_root: Path | None
) -> tuple[RunCard, Dataset, SplitPlan, DatasetPaths]:
    root = resolve_data_root(data_root)
    card = load_run(root, run_id)
    paths = DatasetPaths.resolve(card.dataset, data_root=data_root, configs_root=configs_root)
    dataset = Dataset.load(card.dataset, data_root=data_root, configs_root=configs_root)
    if dataset.card.samples_hash != card.samples_hash:
        raise PlanMismatchError(
            f"run {run_id!r} was created on samples_hash {card.samples_hash[:12]}, "
            f"dataset now has {dataset.card.samples_hash[:12]}"
        )
    plan = load_plan(paths, card.plan_id)
    return card, dataset, plan, paths


def default_subsets(plan: SplitPlan, card: RunCard, *, unseal: bool) -> list[str]:
    """Clean eval subsets for this run; sealed ones only when unsealing."""
    if card.trained_on:
        names = clean_eval_subsets(plan, set(card.trained_on))
    else:
        names = [s.name for s in plan.subsets if s.role in ("eval", "sealed")]
    roles = {s.name: s.role for s in plan.subsets}
    return [n for n in names if roles[n] != "sealed" or unseal]


def _metric_params(metric, spec_params: dict[str, str]) -> dict[str, str]:
    own = {k: v for k, v in spec_params.items() if k in metric.defaults}
    unknown = sorted(set(spec_params) - set(own))
    return effective_params(metric, {**own, **{k: spec_params[k] for k in unknown}}) if unknown else effective_params(metric, own)


def measure_run(spec: MeasureSpec) -> MeasureResult:
    card, dataset, plan, paths = load_context(spec.run_id, spec.data_root, spec.configs_root)
    subsets = spec.subsets or default_subsets(plan, card, unseal=spec.unseal)
    for name in subsets:
        if name in card.trained_on:
            raise ValidationFailed(f"subset {name!r} is in the run's trained_on {card.trained_on}")
        if name not in card.predictions:
            raise ValidationFailed(f"run {spec.run_id!r} has no predictions for subset {name!r}")
    task = dataset.card.task
    metric_names = spec.metrics or applicable_metrics(task)
    for name in metric_names:
        if task not in get_metric(name).tasks:
            raise ValidationFailed(f"metric {name!r} is not applicable to task {task!r}")
    if not metric_names:
        raise ValidationFailed(f"no registered metric applies to task {task!r}")
    ledger = ReadingsLedger(paths.measure_dir / "readings.jsonl")
    anchors = load_anchors(paths)
    root = paths.data_root
    readings: list[Reading] = []
    pending: list[Reading] = []
    guard_states: list[str] = []
    warnings: list[str] = []
    cached = 0
    for subset in subsets:
        samples = dataset.subset(subset, plan, unseal=spec.unseal, reason=spec.reason,
                                 caller="vcp eval measure", paths=paths)
        preds = predictions_by_id(read_predictions(verify_prediction(root, card, subset)))
        sha = card.predictions[subset].sha256
        for name in metric_names:
            metric = get_metric(name)
            params = _metric_params(metric, spec.params)
            pk = params_key(params)
            rid = reading_id(card.run_id, plan.plan_id, subset, name, metric.version, pk, sha)
            if rid in ledger.by_id:
                readings.append(ledger.by_id[rid])
                cached += 1
                continue
            key = anchor_key(plan.plan_id, subset, name, pk)
            guard: GuardrailInfo | None = None
            anchor = anchors.get(key)
            if anchor is not None:
                anchor_run = load_run(root, anchor.run_id)
                anchor_preds = predictions_by_id(
                    read_predictions(verify_prediction(root, anchor_run, subset)))
                got = metric.compute(samples, anchor_preds, dataset.card, params).value
                if abs(got - anchor.value) > anchor.tolerance:
                    raise GuardrailError(
                        f"anchor {key!r} expected {anchor.value!r}, got {got!r} "
                        f"(tolerance {anchor.tolerance}); no readings written"
                    )
                guard = GuardrailInfo(anchor_reading_id=anchor.reading_id, ok=True)
                guard_states.append("OK")
            else:
                guard_states.append("none")
                warnings.append(f"no anchor for {key}; run `vcp eval anchor` once a reference run exists")
            result = metric.compute(samples, preds, dataset.card, params)
            reading = Reading(
                reading_id=rid, ts=stamp(), run_id=card.run_id, dataset=card.dataset,
                samples_hash=card.samples_hash, plan_id=plan.plan_id, subset=subset, metric=name,
                metric_version=metric.version, params=params, value=result.value,
                per_class=result.per_class, n_samples=result.n, prediction_sha=sha, guardrail=guard,
            )
            pending.append(reading)
            readings.append(reading)
    for reading in pending:  # only after every guardrail passed
        ledger.append(reading)
    if not pending:
        guardrail = "cached"
    elif all(s == "OK" for s in guard_states):
        guardrail = "OK"
    elif all(s == "none" for s in guard_states):
        guardrail = "none"
    else:
        guardrail = "partial"
    return MeasureResult(run_id=card.run_id, dataset=card.dataset, readings=readings,
                         new=len(pending), cached=cached, guardrail=guardrail, warnings=warnings)
```

（`_metric_params`：spec 的參數只傳給宣告該 key 的指標；完全沒有指標認得的 key 才報 `ValidationFailed`——實作時在 `measure_run` 開頭先算 `known = set().union(*(get_metric(n).defaults for n in metric_names))`，`spec.params` 有不在 `known` 的 key 就 raise，並把 `_metric_params` 簡化為 `effective_params(metric, {k: v for k, v in spec.params.items() if k in metric.defaults})`。）

Run: `uv run pytest tests/unit/measure/test_measure.py -q`。Expected: PASS。

- [ ] **Step 5: CLI `measure` / `anchor` 的失敗測試**

`tests/unit/test_cli_eval.py` 加：

```python
def test_eval_measure_and_anchor_cli(roots, tmp_path):
    ds, plan, paths = seed_det(roots)
    assert ingest_perfect(roots, tmp_path, ds, plan, "m1", "valA").exit_code == 0
    assert ingest_perfect(roots, tmp_path, ds, plan, "m1", "valB").exit_code == 0
    r = runner.invoke(app, ["eval", "measure", "--run", "m1"])
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "status=WARN" in v and "readings=2" in v and "guardrail=none" in v
    assert "valA" in r.output and "coco_map" in r.output
    r = runner.invoke(app, ["eval", "anchor", "--run", "m1", "--subset", "valA", "--metric", "coco_map"])
    assert r.exit_code == 0, r.output
    assert "key=fixed-v1/valA/coco_map/iou=50:95,max_dets=100" in _last_verdict(r.output).replace('"', "")
    r = runner.invoke(app, ["eval", "anchor", "--run", "m1", "--subset", "valA", "--metric", "coco_map"])
    assert r.exit_code == 1 and "replace" in _last_verdict(r.output)
    r = runner.invoke(app, ["eval", "measure", "--run", "m1", "--params", "iou=50", "--subsets", "valA"])
    assert r.exit_code == 0 and "guardrail=none" in _last_verdict(r.output)  # different params: no anchor
    r = runner.invoke(app, ["eval", "measure", "--run", "m1", "--subsets", "valA", "--json"])
    assert r.exit_code == 0
    doc = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert doc["fields"]["cached"] == 1 and doc["result"]["readings"][0]["subset"] == "valA"
    r = runner.invoke(app, ["eval", "measure", "--run", "m1", "--subsets", "holdout"])
    assert r.exit_code == 2 and "SealedSubsetError" in _last_verdict(r.output)
```

Run: `uv run pytest tests/unit/test_cli_eval.py -k "measure_and_anchor" -q`。Expected: FAIL（No such command 'measure'）。

- [ ] **Step 6: 實作 CLI 命令**

`src/vcp/cli_eval.py` 加（import `MeasureSpec, measure_run, load_context` 自 `vcp.measure.measure`；`anchor_key, set_anchor` 自 `vcp.measure.anchors`；`ReadingsLedger` 自 `vcp.measure.ledger`；`get_metric, effective_params, params_key` 自 `vcp.measure.metrics`；`Anchor` 自 `vcp.measure.schema`；`stamp` 自 `vcp.core.time`；`ValidationFailed`）：

```python
def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@eval_app.command("measure")
def measure_cmd(
    run: RunOpt,
    metrics: Annotated[str | None, typer.Option("--metrics", help="comma-separated; default: all applicable")] = None,
    subsets: Annotated[str | None, typer.Option("--subsets", help="comma-separated; default: clean eval subsets")] = None,
    params: Annotated[list[str] | None, typer.Option("--params", help="metric param key=value (repeatable)")] = None,
    unseal: Annotated[bool, typer.Option("--unseal", help="open a sealed subset (recorded)")] = False,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Guardrail, then one reading per clean eval subset and applicable metric."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        res = measure_run(MeasureSpec(
            run_id=run, metrics=_csv(metrics), subsets=_csv(subsets), params=parse_opts(params),
            unseal=unseal, reason=reason, data_root=data_root, configs_root=configs_root,
        ))
        status: Status = "WARN" if res.guardrail in ("none", "partial") or res.warnings else "OK"
        fields: dict[str, FieldValue] = {
            "run": run, "dataset": res.dataset,
            "subsets": ",".join(sorted({r.subset for r in res.readings})),
            "metrics": ",".join(sorted({r.metric for r in res.readings})),
            "readings": res.new, "cached": res.cached, "guardrail": res.guardrail,
        }
        human = [f"{r.subset:>10}  {r.metric:<12} {r.value!r}  n={r.n_samples}" for r in res.readings]
        human += res.warnings
        payload = {"readings": [r.model_dump(mode="json") for r in res.readings], "warnings": res.warnings}
        return status, fields, payload, human

    run_command("eval.measure", json_mode, data_root, fn)


@eval_app.command("anchor")
def anchor_cmd(
    run: RunOpt,
    subset: Annotated[str, typer.Option("--subset")],
    metric: Annotated[str, typer.Option("--metric")],
    params: Annotated[list[str] | None, typer.Option("--params")] = None,
    tolerance: Annotated[float, typer.Option("--tolerance")] = 1e-6,
    replace: Annotated[bool, typer.Option("--replace")] = False,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Make an existing reading the guardrail for its plan/subset/metric."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        card, dataset, plan, paths = load_context(run, data_root, configs_root)
        m = get_metric(metric)
        p = effective_params(m, parse_opts(params))
        pk = params_key(p)
        sha = card.predictions.get(subset).sha256 if subset in card.predictions else None
        if sha is None:
            raise ValidationFailed(f"run {run!r} has no predictions for subset {subset!r}")
        ledger = ReadingsLedger(paths.measure_dir / "readings.jsonl")
        match = [r for r in ledger.rows if r.run_id == run and r.subset == subset and r.metric == metric
                 and params_key(r.params) == pk and r.prediction_sha == sha]
        if not match:
            raise ValidationFailed(f"no reading for run {run!r} {subset}/{metric}/{pk}; run `vcp eval measure` first")
        reading = match[-1]
        key = anchor_key(plan.plan_id, subset, metric, pk)
        set_anchor(paths, key, Anchor(run_id=run, reading_id=reading.reading_id, value=reading.value,
                                      tolerance=tolerance, set_at=stamp()), replace=replace)
        fields: dict[str, FieldValue] = {"key": key, "value": reading.value, "tolerance": tolerance,
                                         "reading": reading.reading_id[:12]}
        return "OK", fields, {"key": key, "anchor": reading.model_dump(mode="json")}, [f"anchor {key} = {reading.value!r}"]

    run_command("eval.anchor", json_mode, data_root, fn)
```

Run: `uv run pytest tests/unit/test_cli_eval.py tests/unit/measure -q && uv run ruff check . && uv run ruff format --check .`。Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add src/vcp/measure/ledger.py src/vcp/measure/anchors.py src/vcp/measure/measure.py src/vcp/cli_eval.py tests/unit/measure/test_ledger.py tests/unit/measure/test_measure.py tests/unit/test_cli_eval.py
git commit -m "feat(measure,cli): 讀數台帳（只 append、reading_id 去重）、錨點護欄、measure_run 與 vcp eval measure / anchor

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: 配對 bootstrap、σ_p 三法、`vcp eval sigma`（spec §6.3、§4.7）

**Files:**
- Create: `src/vcp/measure/stats.py`、`src/vcp/measure/sigma.py`
- Modify: `src/vcp/cli_eval.py`（`sigma` 命令）
- Test: `tests/unit/measure/test_stats.py`、`tests/unit/measure/test_sigma.py`、`tests/unit/test_cli_eval.py`

**Interfaces:**
- Consumes: Task 6 的 `Metric.compute`；Task 9 的 `ReadingsLedger`、`load_context`、`load_anchors`、`anchor_key`、`append_row`、`read_rows`；Task 2 的 `SigmaEstimate`；numpy。
- Produces:
  - `stats.paired_bootstrap(samples, preds_a, preds_b, metric, card, params, *, resamples=200, seed=0) -> tuple[float, float, list[float]]`（`delta = b − a`、`se`、每次重抽的 Δ）
  - `stats.bootstrap_sd(samples, preds, metric, card, params, *, resamples=200, seed=0) -> float`
  - `sigma.SigmaSpec(dataset, plan_id, metric, params={}, method, subsets=[], run_id=None, prior=None, note="", resamples=200, seed=0, data_root=None, configs_root=None)`、`sigma.SIGMA_METHODS = ("splithalf", "bootstrap", "prior")`、`sigma.estimate_sigma(spec) -> SigmaEstimate`（append 到 `measure/<dataset>/sigma.jsonl`）、`sigma.latest_sigma(paths, plan_id, metric, params_key, method) -> SigmaEstimate | None`
  - CLI `vcp eval sigma`

- [ ] **Step 1: bootstrap 的失敗測試**

`tests/unit/measure/test_stats.py`：

```python
import pytest

from helpers import cls_samples, make_card, noisy_predictions, perfect_predictions
from vcp.measure.metrics import get_metric
from vcp.measure.predictions import predictions_by_id
from vcp.measure.stats import bootstrap_sd, paired_bootstrap


def test_paired_bootstrap_is_deterministic_and_signed():
    samples = cls_samples(80, seed=0)
    card = make_card("cls")
    perfect = predictions_by_id(perfect_predictions(samples, card))
    noisy = predictions_by_id(noisy_predictions(samples, card, seed=1, flip=0.3))
    metric = get_metric("accuracy")
    delta, se, deltas = paired_bootstrap(samples, perfect, noisy, metric, card, {}, resamples=50, seed=0)
    assert delta < 0 and se > 0 and len(deltas) == 50
    delta2, se2, deltas2 = paired_bootstrap(samples, perfect, noisy, metric, card, {}, resamples=50, seed=0)
    assert (delta2, se2, deltas2) == (delta, se, deltas)
    _, _, deltas3 = paired_bootstrap(samples, perfect, noisy, metric, card, {}, resamples=50, seed=1)
    assert deltas3 != deltas
    d_same, se_same, _ = paired_bootstrap(samples, perfect, perfect, metric, card, {}, resamples=20, seed=0)
    assert d_same == 0.0 and se_same == 0.0


def test_bootstrap_sd():
    samples = cls_samples(60, seed=2)
    card = make_card("cls")
    metric = get_metric("accuracy")
    assert bootstrap_sd(samples, predictions_by_id(perfect_predictions(samples, card)), metric, card, {},
                        resamples=30, seed=0) == 0.0
    sd = bootstrap_sd(samples, predictions_by_id(noisy_predictions(samples, card, seed=3, flip=0.3)),
                      metric, card, {}, resamples=30, seed=0)
    assert 0.0 < sd < 0.2
    with pytest.raises(ValueError):
        bootstrap_sd(samples, {}, metric, card, {}, resamples=1, seed=0)
```

Run: `uv run pytest tests/unit/measure/test_stats.py -q`。Expected: FAIL（ImportError）。

- [ ] **Step 2: 實作 `stats.py`**

```python
"""Bootstrap over samples: paired deltas between two prediction sets, and the sampling noise
of one reading. Metrics are recomputed on resampled sample lists (duplicates allowed)."""

from __future__ import annotations

import numpy as np

from vcp.data.schema import DatasetCard, Sample
from vcp.measure.metrics.base import Metric
from vcp.measure.schema import Prediction


def _resamples(n: int, resamples: int, seed: int) -> list[np.ndarray]:
    if resamples < 2:
        raise ValueError("resamples must be >= 2")
    rng = np.random.default_rng(seed)
    return [rng.integers(0, n, n) for _ in range(resamples)]


def paired_bootstrap(
    samples: list[Sample],
    preds_a: dict[str, Prediction],
    preds_b: dict[str, Prediction],
    metric: Metric,
    card: DatasetCard,
    params: dict[str, str],
    *,
    resamples: int = 200,
    seed: int = 0,
) -> tuple[float, float, list[float]]:
    """(delta, se, deltas): delta = metric(b) - metric(a) on the full list; se = std of the
    paired resampled deltas (ddof=1)."""
    base = metric.compute(samples, preds_b, card, params).value - metric.compute(samples, preds_a, card, params).value
    deltas: list[float] = []
    for idx in _resamples(len(samples), resamples, seed):
        sub = [samples[i] for i in idx]
        deltas.append(
            metric.compute(sub, preds_b, card, params).value
            - metric.compute(sub, preds_a, card, params).value
        )
    se = float(np.std(deltas, ddof=1))
    return float(base), se, deltas


def bootstrap_sd(
    samples: list[Sample],
    preds: dict[str, Prediction],
    metric: Metric,
    card: DatasetCard,
    params: dict[str, str],
    *,
    resamples: int = 200,
    seed: int = 0,
) -> float:
    values = [
        metric.compute([samples[i] for i in idx], preds, card, params).value
        for idx in _resamples(len(samples), resamples, seed)
    ]
    return float(np.std(values, ddof=1))
```

Run: `uv run pytest tests/unit/measure/test_stats.py -q`。Expected: PASS（`bootstrap_sd(..., {}, ...)` 對空預測會先在 `_resamples` 因 `resamples=1` 拋 `ValueError`，這是測試要的順序）。

- [ ] **Step 3: σ_p 的失敗測試**

`tests/unit/measure/test_sigma.py`：

```python
import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.measure.ledger import ReadingsLedger, reading_id
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.measure.metrics import params_key
from vcp.measure.schema import Reading
from vcp.measure.sigma import SigmaSpec, estimate_sigma, latest_sigma
from test_measure import det_with_runs


def _fake_reading(run, subset, value, ts):
    params = {"iou": "50:95", "max_dets": "100"}
    return Reading(reading_id=reading_id(run, "fixed-v1", subset, "coco_map", "1", params_key(params), f"sha-{run}"),
                   ts=ts, run_id=run, dataset="tiny", samples_hash="h", plan_id="fixed-v1", subset=subset,
                   metric="coco_map", metric_version="1", params=params, value=value, per_class=None,
                   n_samples=6, prediction_sha=f"sha-{run}")


def _spec(roots, **kw):
    return SigmaSpec(dataset="tiny", plan_id="fixed-v1", metric="coco_map", data_root=roots.data,
                     configs_root=roots.configs, **kw)


def test_splithalf_needs_three_runs_then_estimates(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    ledger = ReadingsLedger(paths.measure_dir / "readings.jsonl")
    ledger.append(_fake_reading("r1", "valA", 0.50, "2026-09-04T00:00:01.000Z"))
    ledger.append(_fake_reading("r1", "valB", 0.52, "2026-09-04T00:00:02.000Z"))
    ledger.append(_fake_reading("r2", "valA", 0.60, "2026-09-04T00:00:03.000Z"))
    with pytest.raises(ValidationFailed, match="at least 3 runs"):
        estimate_sigma(_spec(roots, method="splithalf", subsets=["valA", "valB"]))
    ledger.append(_fake_reading("r2", "valB", 0.58, "2026-09-04T00:00:04.000Z"))
    ledger.append(_fake_reading("r3", "valA", 0.70, "2026-09-04T00:00:05.000Z"))
    ledger.append(_fake_reading("r3", "valB", 0.71, "2026-09-04T00:00:06.000Z"))
    est = estimate_sigma(_spec(roots, method="splithalf", subsets=["valA", "valB"]))
    # d = [-0.02, 0.02, -0.01] -> std(ddof=1)=0.0208167 -> /sqrt(2)
    assert est.value == pytest.approx(0.020816659994661 / 2**0.5)
    assert est.method == "splithalf" and est.inputs["runs"] == ["r1", "r2", "r3"]
    assert latest_sigma(paths, "fixed-v1", "coco_map", "iou=50:95,max_dets=100", "splithalf").estimate_id == est.estimate_id
    assert latest_sigma(paths, "fixed-v1", "coco_map", "iou=50:95,max_dets=100", "prior") is None


def test_prior_and_bootstrap(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=40)
    with pytest.raises(ValidationFailed, match="note"):
        estimate_sigma(_spec(roots, method="prior", prior=0.008))
    est = estimate_sigma(_spec(roots, method="prior", prior=0.008, note="platform history 2024-2026"))
    assert est.value == 0.008 and est.inputs["note"] == "platform history 2024-2026"
    measure_run(MeasureSpec(run_id="noisy", data_root=roots.data, configs_root=roots.configs))
    boot = estimate_sigma(_spec(roots, method="bootstrap", run_id="noisy", subsets=["valA"], resamples=20))
    assert boot.value > 0 and boot.inputs == {"run_id": "noisy", "subset": "valA", "resamples": 20, "seed": 0}
    with pytest.raises(ValidationFailed, match="--run"):
        estimate_sigma(_spec(roots, method="bootstrap", subsets=["valA"]))
    with pytest.raises(ValidationFailed, match="method"):
        estimate_sigma(_spec(roots, method="magic"))
    rows = (paths.measure_dir / "sigma.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2
```

（`from test_measure import det_with_runs`：`tests/unit/measure/__init__.py` 存在且 `pythonpath` 含 `tests`，測試模組間可直接 import；若 pytest 的 rootdir 解析讓 `test_measure` 不可 import，改成 `from tests.unit.measure.test_measure import det_with_runs`。）

Run: `uv run pytest tests/unit/measure/test_sigma.py -q`。Expected: FAIL（ImportError）。

- [ ] **Step 4: 實作 `sigma.py`**

```python
"""sigma_p: how much a reading can move between two 'equivalent' evaluation sets."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_json
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.measure.ledger import ReadingsLedger, append_row, read_rows
from vcp.measure.measure import load_context
from vcp.measure.metrics import effective_params, get_metric, params_key
from vcp.measure.predictions import predictions_by_id, read_predictions
from vcp.measure.runs import verify_prediction
from vcp.measure.schema import Reading, SigmaEstimate
from vcp.measure.stats import bootstrap_sd

SIGMA_METHODS = ("splithalf", "bootstrap", "prior")


class SigmaSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    plan_id: str
    metric: str
    params: dict[str, str] = Field(default_factory=dict)
    method: str
    subsets: list[str] = Field(default_factory=list)
    run_id: str | None = None
    prior: float | None = None
    note: str = ""
    resamples: int = 200
    seed: int = 0
    data_root: Path | None = None
    configs_root: Path | None = None


def latest_sigma(paths: DatasetPaths, plan_id: str, metric: str, pk: str, method: str) -> SigmaEstimate | None:
    rows = [r for r in read_rows(paths.measure_dir / "sigma.jsonl", SigmaEstimate)
            if r.plan_id == plan_id and r.metric == metric and params_key(r.params) == pk and r.method == method]
    return max(rows, key=lambda r: r.ts) if rows else None


def _latest_per_run(rows: list[Reading], subset: str) -> dict[str, Reading]:
    out: dict[str, Reading] = {}
    for r in sorted(rows, key=lambda r: r.ts):
        if r.subset == subset:
            out[r.run_id] = r
    return out


def _splithalf(spec: SigmaSpec, paths: DatasetPaths, pk: str, plan) -> tuple[float, dict[str, Any]]:
    subsets = spec.subsets or [s.name for s in plan.subsets if s.role == "eval"][:2]
    if len(subsets) != 2:
        raise ValidationFailed(f"splithalf needs exactly two eval subsets, got {subsets}")
    rows = [r for r in ReadingsLedger(paths.measure_dir / "readings.jsonl").rows
            if r.plan_id == spec.plan_id and r.metric == spec.metric and params_key(r.params) == pk]
    a, b = _latest_per_run(rows, subsets[0]), _latest_per_run(rows, subsets[1])
    runs = sorted(set(a) & set(b))
    if len(runs) < 3:
        raise ValidationFailed(
            f"splithalf needs at least 3 runs with readings on both {subsets}; found {len(runs)}"
        )
    diffs = [a[r].value - b[r].value for r in runs]
    value = float(np.std(diffs, ddof=1) / math.sqrt(2))
    inputs = {"subsets": subsets, "runs": runs,
              "reading_ids": [a[r].reading_id for r in runs] + [b[r].reading_id for r in runs],
              "diffs": diffs}
    return value, inputs


def _bootstrap(spec: SigmaSpec, paths: DatasetPaths, params: dict[str, str], plan) -> tuple[float, dict[str, Any]]:
    if not spec.run_id:
        raise ValidationFailed("bootstrap needs --run <run_id> (the run to resample)")
    subset = spec.subsets[0] if spec.subsets else next(s.name for s in plan.subsets if s.role == "eval")
    card, dataset, plan, paths = load_context(spec.run_id, spec.data_root, spec.configs_root)
    samples = dataset.subset(subset, plan, paths=paths)
    preds = predictions_by_id(read_predictions(verify_prediction(paths.data_root, card, subset)))
    value = bootstrap_sd(samples, preds, get_metric(spec.metric), dataset.card, params,
                         resamples=spec.resamples, seed=spec.seed)
    return value, {"run_id": spec.run_id, "subset": subset, "resamples": spec.resamples, "seed": spec.seed}


def estimate_sigma(spec: SigmaSpec) -> SigmaEstimate:
    if spec.method not in SIGMA_METHODS:
        raise ValidationFailed(f"--method must be one of {SIGMA_METHODS}, got {spec.method!r}")
    paths = DatasetPaths.resolve(spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root)
    from vcp.data.split import load_plan

    plan = load_plan(paths, spec.plan_id)
    metric = get_metric(spec.metric)
    params = effective_params(metric, spec.params)
    pk = params_key(params)
    if spec.method == "prior":
        if spec.prior is None or not spec.note.strip():
            raise ValidationFailed("prior needs --prior <value> and --note <where it comes from>")
        value, inputs = float(spec.prior), {"note": spec.note}
    elif spec.method == "splithalf":
        value, inputs = _splithalf(spec, paths, pk, plan)
    else:
        value, inputs = _bootstrap(spec, paths, params, plan)
    ts = stamp()
    est = SigmaEstimate(
        estimate_id=sha256_json({"method": spec.method, "plan": spec.plan_id, "metric": spec.metric,
                                 "params": params, "inputs": inputs, "ts": ts}),
        ts=ts, plan_id=spec.plan_id, metric=spec.metric, params=params, method=spec.method,
        value=value, inputs=inputs, note=spec.note,
    )
    append_row(paths.measure_dir / "sigma.jsonl", est)
    return est
```

Run: `uv run pytest tests/unit/measure/test_sigma.py -q`。Expected: PASS。

- [ ] **Step 5: CLI `sigma`**

`tests/unit/test_cli_eval.py` 加：

```python
def test_eval_sigma_cli(roots, tmp_path):
    ds, plan, paths = seed_det(roots)
    r = runner.invoke(app, ["eval", "sigma", "--dataset", "tiny", "--plan", "fixed-v1", "--metric",
                            "coco_map", "--method", "prior", "--prior", "0.008", "--note", "history"])
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "method=prior" in v and "value=0.008" in v
    r = runner.invoke(app, ["eval", "sigma", "--dataset", "tiny", "--plan", "fixed-v1", "--metric",
                            "coco_map", "--method", "splithalf"])
    assert r.exit_code == 1 and "at least 3 runs" in _last_verdict(r.output)
```

`src/vcp/cli_eval.py` 加：

```python
@eval_app.command("sigma")
def sigma_cmd(
    dataset: DatasetOpt,
    plan: Annotated[str, typer.Option("--plan")],
    metric: Annotated[str, typer.Option("--metric")],
    method: Annotated[str, typer.Option("--method", help="splithalf | bootstrap | prior")],
    params: Annotated[list[str] | None, typer.Option("--params")] = None,
    subsets: Annotated[str | None, typer.Option("--subsets", help="two eval subsets (splithalf) or one (bootstrap)")] = None,
    run: Annotated[str | None, typer.Option("--run", help="run to resample (bootstrap)")] = None,
    prior: Annotated[float | None, typer.Option("--prior")] = None,
    note: Annotated[str, typer.Option("--note", help="source of a prior")] = "",
    resamples: Annotated[int, typer.Option("--resamples")] = 200,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Estimate sigma_p and append it to the sigma ledger."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        est = estimate_sigma(SigmaSpec(
            dataset=dataset, plan_id=plan, metric=metric, params=parse_opts(params), method=method,
            subsets=_csv(subsets), run_id=run, prior=prior, note=note, resamples=resamples, seed=seed,
            data_root=data_root, configs_root=configs_root,
        ))
        fields: dict[str, FieldValue] = {"dataset": dataset, "plan": plan, "metric": metric,
                                         "method": method, "value": est.value, "estimate": est.estimate_id[:12]}
        if "runs" in est.inputs:
            fields["runs"] = len(est.inputs["runs"])
        return "OK", fields, {"estimate": est.model_dump(mode="json")}, [f"sigma_p ({method}) = {est.value!r}"]

    run_command("eval.sigma", json_mode, data_root, fn)
```

Run: `uv run pytest tests/unit/test_cli_eval.py tests/unit/measure -q && uv run ruff check . && uv run ruff format --check .`。Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add src/vcp/measure/stats.py src/vcp/measure/sigma.py src/vcp/cli_eval.py tests/unit/measure/test_stats.py tests/unit/measure/test_sigma.py tests/unit/test_cli_eval.py
git commit -m "feat(measure,cli): 配對 bootstrap、σ_p 三法（splithalf / bootstrap / prior）與 vcp eval sigma

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: 預登記與判決、`vcp eval preregister` / `judge`（spec §4.5、§4.6、§6.2）

**Files:**
- Create: `src/vcp/measure/prereg.py`、`src/vcp/measure/judge.py`
- Modify: `src/vcp/cli_eval.py`（`preregister`、`judge` 命令）
- Test: `tests/unit/measure/test_prereg_judge.py`、`tests/unit/test_cli_eval.py`

**Interfaces:**
- Consumes: Task 2（`PreRegistration`、`Judgement`、`SubsetJudgement`、`SigmaRef`）、Task 9（`ReadingsLedger`、`load_context`、`append_row`、`read_rows`）、Task 10（`paired_bootstrap`、`latest_sigma`）、Task 1（`DatasetPaths.prereg_dir`、`prereg_log`）、`vcp.core.config.dump_yaml_model / load_yaml_model`、`vcp.core.hashing.sha256_file`。
- Produces:
  - `prereg.prereg_path(paths, prereg_id) -> Path`、`prereg.create_prereg(paths, pr, readings: ReadingsLedger) -> Path`（id 已存在 → `ValidationFailed`；候選在該指標 / 參數 / 子集已有讀數 → `ValidationFailed` 含 `already_measured`；寫 yaml + append `prereg.log.jsonl {prereg_id, sha256, ts}`）、`prereg.load_prereg(paths, prereg_id) -> PreRegistration`、`prereg.prereg_time(paths, prereg_id) -> str | None`（log 中該 id 的第一筆 ts）、`prereg.list_preregs(paths) -> list[str]`
  - `judge.JudgeSpec(dataset, prereg_id, resamples=200, seed=0, data_root=None, configs_root=None)`、`judge.judge(spec) -> Judgement`（append 到 `judgements.jsonl`）、`judge.T_CAP = 1e9`（se 為 0 時 t 以 ±T_CAP 表示）
  - CLI `vcp eval preregister`、`vcp eval judge [--strict]`

- [ ] **Step 1: 寫失敗測試**

`tests/unit/measure/test_prereg_judge.py`：

```python
import json

import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.measure.judge import T_CAP, JudgeSpec, judge
from vcp.measure.ledger import ReadingsLedger, read_rows
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.measure.prereg import create_prereg, list_preregs, load_prereg, prereg_time
from vcp.measure.schema import Judgement, PreRegistration
from vcp.measure.sigma import SigmaSpec, estimate_sigma
from test_measure import det_with_runs


def _pr(**kw):
    base = dict(prereg_id="p001", claim="noisy is worse", component="noise", component_class="model",
                baseline_run="perfect", candidate_run="noisy", metric="coco_map",
                subsets=["valA", "valB"], created_at="2026-09-04T00:00:00.000Z")
    return PreRegistration(**{**base, **kw})


def _measure(roots, run):
    return measure_run(MeasureSpec(run_id=run, data_root=roots.data, configs_root=roots.configs))


def _judge(roots, pid="p001", **kw):
    return judge(JudgeSpec(dataset="tiny", prereg_id=pid, resamples=30, seed=0,
                           data_root=roots.data, configs_root=roots.configs, **kw))


def test_create_prereg_refuses_measured_candidate(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=40)
    _measure(roots, "perfect")
    ledger = ReadingsLedger(paths.measure_dir / "readings.jsonl")
    path = create_prereg(paths, _pr(), ledger)
    assert path == paths.prereg_dir / "p001.yaml" and load_prereg(paths, "p001").params == {
        "iou": "50:95", "max_dets": "100"}
    assert prereg_time(paths, "p001") is not None and list_preregs(paths) == ["p001"]
    log = json.loads(paths.prereg_log.read_text(encoding="utf-8").splitlines()[0])
    assert log["prereg_id"] == "p001" and len(log["sha256"]) == 64
    with pytest.raises(ValidationFailed, match="already exists"):
        create_prereg(paths, _pr(), ledger)
    _measure(roots, "noisy")
    with pytest.raises(ValidationFailed, match="already_measured"):
        create_prereg(paths, _pr(prereg_id="p002"), ReadingsLedger(paths.measure_dir / "readings.jsonl"))
    assert prereg_time(paths, "nope") is None


def test_judge_fail_pass_and_sigma_rules(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=60)
    _measure(roots, "perfect")
    ledger = ReadingsLedger(paths.measure_dir / "readings.jsonl")
    create_prereg(paths, _pr(), ledger)                                   # candidate noisy (worse)
    create_prereg(paths, _pr(prereg_id="p002", baseline_run="noisy", candidate_run="perfect",
                      component_class="tuning"), ledger)                 # candidate perfect (better)
    _measure(roots, "noisy")
    j1 = _judge(roots, "p001")
    assert j1.verdict == "FAIL" and j1.bases_positive == 0
    assert all(s.delta < 0 for s in j1.per_subset.values()) and set(j1.per_subset) == {"valA", "valB"}
    assert "bases_positive 0 < 2" in " ".join(j1.reasons)
    j2 = _judge(roots, "p002")
    assert j2.verdict == "FAIL" and "no_sigma" in j2.reasons and j2.sigma_p is None
    estimate_sigma(SigmaSpec(dataset="tiny", plan_id="fixed-v1", metric="coco_map", method="prior",
                             prior=0.001, note="test", data_root=roots.data, configs_root=roots.configs))
    j3 = _judge(roots, "p002")
    assert j3.verdict == "PASS" and j3.sigma_p.method == "prior" and j3.bases_positive == 2
    assert all(s.t >= 2.0 for s in j3.per_subset.values())
    estimate_sigma(SigmaSpec(dataset="tiny", plan_id="fixed-v1", metric="coco_map", method="prior",
                             prior=5.0, note="huge", data_root=roots.data, configs_root=roots.configs))
    j4 = _judge(roots, "p002")
    assert j4.verdict == "FAIL" and any("sigma" in r for r in j4.reasons)
    rows = read_rows(paths.measure_dir / "judgements.jsonl", Judgement)
    assert [r.verdict for r in rows] == ["FAIL", "FAIL", "PASS", "FAIL"]
    assert rows[0].bootstrap == {"resamples": 30, "seed": 0} and rows[0].reading_ids


def test_judge_missing_readings_and_invalid_ordering(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=40)
    _measure(roots, "perfect")
    create_prereg(paths, _pr(), ReadingsLedger(paths.measure_dir / "readings.jsonl"))
    j = _judge(roots)                                                     # noisy never measured
    assert j.verdict == "FAIL" and any("missing_readings" in r for r in j.reasons)
    _measure(roots, "noisy")
    # forge a pre-registration whose log time is later than the candidate's readings
    pr = _pr(prereg_id="p009", baseline_run="perfect", candidate_run="noisy")
    (paths.prereg_dir / "p009.yaml").write_text(
        __import__("yaml").safe_dump(pr.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    with paths.prereg_log.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"prereg_id": "p009", "sha256": "0" * 64, "ts": "2099-01-01T00:00:00.000Z"}) + "\n")
    j = _judge(roots, "p009")
    assert j.verdict == "INVALID" and "measured_before_prereg" in j.reasons
    with pytest.raises(ValidationFailed, match="not found"):
        _judge(roots, "p404")


def test_t_is_capped_when_se_is_zero(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=40)
    _measure(roots, "perfect")
    ledger = ReadingsLedger(paths.measure_dir / "readings.jsonl")
    create_prereg(paths, _pr(prereg_id="same", baseline_run="perfect", candidate_run="perfect"), ledger)
    j = _judge(roots, "same")
    assert all(s.delta == 0.0 and s.se == 0.0 and s.t == 0.0 for s in j.per_subset.values())
    assert T_CAP == 1e9 and j.verdict == "FAIL"
```

Run: `uv run pytest tests/unit/measure/test_prereg_judge.py -q`。Expected: FAIL（ImportError）。

- [ ] **Step 2: 實作 `prereg.py`**

```python
"""Pre-registration files (git) plus an append-only log whose timestamps prove ordering."""

from __future__ import annotations

import json
from pathlib import Path

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, validate_name
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.metrics import effective_params, get_metric, params_key
from vcp.measure.schema import PreRegistration


def prereg_path(paths: DatasetPaths, prereg_id: str) -> Path:
    validate_name(prereg_id)
    return paths.prereg_dir / f"{prereg_id}.yaml"


def list_preregs(paths: DatasetPaths) -> list[str]:
    if not paths.prereg_dir.is_dir():
        return []
    return sorted(p.stem for p in paths.prereg_dir.glob("*.yaml"))


def load_prereg(paths: DatasetPaths, prereg_id: str) -> PreRegistration:
    path = prereg_path(paths, prereg_id)
    if not path.is_file():
        raise ValidationFailed(f"pre-registration not found: {path}")
    return load_yaml_model(path, PreRegistration)


def prereg_time(paths: DatasetPaths, prereg_id: str) -> str | None:
    if not paths.prereg_log.is_file():
        return None
    with paths.prereg_log.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                if row.get("prereg_id") == prereg_id:
                    return str(row["ts"])
    return None


def create_prereg(paths: DatasetPaths, pr: PreRegistration, readings: ReadingsLedger) -> Path:
    path = prereg_path(paths, pr.prereg_id)
    if path.exists():
        raise ValidationFailed(f"pre-registration {pr.prereg_id!r} already exists: {path}")
    metric = get_metric(pr.metric)
    params = effective_params(metric, pr.params)
    pk = params_key(params)
    measured = sorted({r.subset for r in readings.rows
                       if r.run_id == pr.candidate_run and r.metric == pr.metric
                       and params_key(r.params) == pk and r.subset in pr.subsets})
    if measured:
        raise ValidationFailed(
            f"already_measured: candidate {pr.candidate_run!r} has {pr.metric} readings on {measured}; "
            "pre-register before measuring the candidate"
        )
    pr = pr.model_copy(update={"params": params})
    dump_yaml_model(pr, path)
    paths.prereg_log.parent.mkdir(parents=True, exist_ok=True)
    with paths.prereg_log.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"prereg_id": pr.prereg_id, "sha256": sha256_file(path), "ts": pr.created_at},
                           ensure_ascii=False) + "\n")
    return path
```

- [ ] **Step 3: 實作 `judge.py`**

```python
"""vcp eval judge: pre-registered baseline vs candidate on every named subset, paired bootstrap,
bases-positive rule and the sigma_p condition for tuning-class components. Never max-of-N."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.measure.ledger import ReadingsLedger, append_row
from vcp.measure.measure import load_context
from vcp.measure.metrics import get_metric, params_key
from vcp.measure.predictions import predictions_by_id, read_predictions
from vcp.measure.prereg import load_prereg, prereg_time
from vcp.measure.runs import verify_prediction
from vcp.measure.schema import Judgement, Reading, SigmaRef, SubsetJudgement
from vcp.measure.sigma import latest_sigma
from vcp.measure.stats import paired_bootstrap

T_CAP = 1e9


class JudgeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    prereg_id: str
    resamples: int = 200
    seed: int = 0
    data_root: Path | None = None
    configs_root: Path | None = None


def _latest(rows: list[Reading], run_id: str, subset: str, metric: str, pk: str) -> Reading | None:
    hits = [r for r in rows if r.run_id == run_id and r.subset == subset and r.metric == metric
            and params_key(r.params) == pk]
    return max(hits, key=lambda r: r.ts) if hits else None


def _t(delta: float, se: float) -> float:
    if se > 0:
        return delta / se
    return 0.0 if delta == 0 else (T_CAP if delta > 0 else -T_CAP)


def judge(spec: JudgeSpec) -> Judgement:
    paths = DatasetPaths.resolve(spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root)
    pr = load_prereg(paths, spec.prereg_id)
    logged_at = prereg_time(paths, spec.prereg_id)
    if logged_at is None:
        raise ValidationFailed(f"pre-registration {spec.prereg_id!r} is not in {paths.prereg_log}")
    metric = get_metric(pr.metric)
    pk = params_key(pr.params)
    ledger = ReadingsLedger(paths.measure_dir / "readings.jsonl")
    reasons: list[str] = []
    pairs: dict[str, tuple[Reading, Reading]] = {}
    for subset in pr.subsets:
        a = _latest(ledger.rows, pr.baseline_run, subset, pr.metric, pk)
        b = _latest(ledger.rows, pr.candidate_run, subset, pr.metric, pk)
        if a is None or b is None:
            missing = [n for n, r in (("baseline", a), ("candidate", b)) if r is None]
            reasons.append(f"missing_readings: {subset} {missing}")
            continue
        pairs[subset] = (a, b)
    verdict = "FAIL"
    per_subset: dict[str, SubsetJudgement] = {}
    if any(b.ts < logged_at for _, b in pairs.values()):
        verdict = "INVALID"
        reasons.append("measured_before_prereg")
    elif pairs and not reasons:
        card_b, dataset, plan, paths = load_context(pr.candidate_run, spec.data_root, spec.configs_root)
        card_a, _, _, _ = load_context(pr.baseline_run, spec.data_root, spec.configs_root)
        for subset, (a, b) in pairs.items():
            samples = dataset.subset(subset, plan, paths=paths)
            preds_a = predictions_by_id(read_predictions(verify_prediction(paths.data_root, card_a, subset)))
            preds_b = predictions_by_id(read_predictions(verify_prediction(paths.data_root, card_b, subset)))
            delta, se, _ = paired_bootstrap(samples, preds_a, preds_b, metric, dataset.card, pr.params,
                                            resamples=spec.resamples, seed=spec.seed)
            per_subset[subset] = SubsetJudgement(baseline=a.value, candidate=b.value, delta=delta,
                                                 se=se, t=_t(delta, se), n=len(samples))
    bases_positive = sum(1 for s in per_subset.values() if s.delta > 0 and s.t >= pr.t_min)
    sigma_ref: SigmaRef | None = None
    if verdict != "INVALID" and not reasons:
        if bases_positive < pr.min_bases:
            reasons.append(f"bases_positive {bases_positive} < {pr.min_bases}")
        if pr.component_class == "tuning":
            est = latest_sigma(paths, plan.plan_id, pr.metric, pk, pr.sigma_method)
            if est is None:
                reasons.append("no_sigma")
            else:
                sigma_ref = SigmaRef(method=est.method, value=est.value, estimate_id=est.estimate_id)
                mean_delta = float(np.mean([s.delta for s in per_subset.values()]))
                if mean_delta < pr.sigma_ratio * est.value:
                    reasons.append(f"mean delta {mean_delta!r} < sigma_ratio {pr.sigma_ratio} x sigma_p {est.value!r}")
        if not reasons:
            verdict = "PASS"
    judgement = Judgement(
        prereg_id=pr.prereg_id, ts=stamp(), baseline_run=pr.baseline_run, candidate_run=pr.candidate_run,
        metric=pr.metric, params=pr.params, per_subset=per_subset, bases_positive=bases_positive,
        sigma_p=sigma_ref, verdict=verdict, reasons=reasons,
        reading_ids=[r.reading_id for pair in pairs.values() for r in pair],
        bootstrap={"resamples": spec.resamples, "seed": spec.seed},
    )
    append_row(paths.measure_dir / "judgements.jsonl", judgement)
    return judgement
```

（`plan` 只在 `pairs` 非空時定義；`latest_sigma` 需要 `plan_id`，改用 `pr` 對應 run 的 `plan_id`：在函式開頭先 `card_b = load_run(...)` 取 `plan_id`，或直接從 `pairs` 的讀數取 `a.plan_id`。實作時用讀數的 `plan_id`，避免未定義變數。）

Run: `uv run pytest tests/unit/measure/test_prereg_judge.py -q`。Expected: PASS。

- [ ] **Step 4: CLI `preregister` / `judge`**

`tests/unit/test_cli_eval.py` 加：

```python
def test_eval_preregister_and_judge_cli(roots, tmp_path):
    ds, plan, paths = seed_det(roots, n=60)
    for run_id, drop in (("base", 8), ("cand", 0)):
        for subset in ("valA", "valB"):
            assert ingest_perfect(roots, tmp_path, ds, plan, run_id, subset, drop=drop).exit_code == 0
    assert runner.invoke(app, ["eval", "measure", "--run", "base"]).exit_code == 0
    args = ["eval", "preregister", "--dataset", "tiny", "--id", "p1", "--claim", "cand beats base",
            "--component", "full-coverage", "--class", "model", "--baseline-run", "base",
            "--candidate-run", "cand", "--metric", "coco_map"]
    r = runner.invoke(app, args)
    assert r.exit_code == 0, r.output
    assert "prereg=p1" in _last_verdict(r.output)
    assert (roots.configs / "datasets" / "tiny" / "prereg" / "p1.yaml").is_file()
    r = runner.invoke(app, ["eval", "judge", "--dataset", "tiny", "--prereg", "p1"])
    assert r.exit_code == 0 and "verdict=FAIL" in _last_verdict(r.output)  # candidate not measured yet
    assert "missing_readings" in r.output
    assert runner.invoke(app, ["eval", "measure", "--run", "cand"]).exit_code == 0
    r = runner.invoke(app, ["eval", "judge", "--dataset", "tiny", "--prereg", "p1", "--resamples", "30"])
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "verdict=PASS" in v and "bases_positive=2" in v
    r = runner.invoke(app, [*args[:3], "--id", "p2", *args[5:]])  # same candidate, now measured
    assert r.exit_code == 1 and "already_measured" in _last_verdict(r.output)
    r = runner.invoke(app, ["eval", "judge", "--dataset", "tiny", "--prereg", "p404", "--strict"])
    assert r.exit_code == 1
```

`src/vcp/cli_eval.py` 加（import `create_prereg` 自 `vcp.measure.prereg`、`JudgeSpec, judge` 自 `vcp.measure.judge`、`PreRegistration`、`DatasetPaths`）：

```python
@eval_app.command("preregister")
def preregister_cmd(
    dataset: DatasetOpt,
    prereg_id: Annotated[str, typer.Option("--id", help="pre-registration id (path-safe)")],
    claim: Annotated[str, typer.Option("--claim")],
    component: Annotated[str, typer.Option("--component")],
    component_class: Annotated[str, typer.Option("--class", help="model | tuning")],
    baseline_run: Annotated[str, typer.Option("--baseline-run")],
    candidate_run: Annotated[str, typer.Option("--candidate-run")],
    metric: Annotated[str, typer.Option("--metric")],
    params: Annotated[list[str] | None, typer.Option("--params")] = None,
    subsets: Annotated[str, typer.Option("--subsets")] = "valA,valB",
    t_min: Annotated[float, typer.Option("--t-min")] = 2.0,
    min_bases: Annotated[int, typer.Option("--min-bases")] = 2,
    sigma_method: Annotated[str, typer.Option("--sigma-method")] = "splithalf",
    sigma_ratio: Annotated[float, typer.Option("--sigma-ratio")] = 1.0,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Write the claim down before measuring the candidate."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        if component_class not in ("model", "tuning"):
            raise ValidationFailed(f"--class must be model or tuning, got {component_class!r}")
        paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
        pr = PreRegistration(
            prereg_id=prereg_id, claim=claim, component=component, component_class=component_class,
            baseline_run=baseline_run, candidate_run=candidate_run, metric=metric,
            params=parse_opts(params), subsets=_csv(subsets), t_min=t_min, min_bases=min_bases,
            sigma_method=sigma_method, sigma_ratio=sigma_ratio, created_at=stamp(),
        )
        path = create_prereg(paths, pr, ReadingsLedger(paths.measure_dir / "readings.jsonl"))
        fields: dict[str, FieldValue] = {"dataset": dataset, "prereg": prereg_id, "metric": metric,
                                         "candidate": candidate_run, "baseline": baseline_run,
                                         "path": str(path)}
        return "OK", fields, {"prereg": pr.model_dump(mode="json"), "path": str(path)}, [f"pre-registered {prereg_id} -> {path}"]

    run_command("eval.preregister", json_mode, data_root, fn)


@eval_app.command("judge")
def judge_cmd(
    dataset: DatasetOpt,
    prereg_id: Annotated[str, typer.Option("--prereg")],
    resamples: Annotated[int, typer.Option("--resamples")] = 200,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    strict: Annotated[bool, typer.Option("--strict", help="exit 1 unless the verdict is PASS")] = False,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Judge a pre-registered claim from the readings ledger (never max-of-N)."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        j = judge(JudgeSpec(dataset=dataset, prereg_id=prereg_id, resamples=resamples, seed=seed,
                            data_root=data_root, configs_root=configs_root))
        fields: dict[str, FieldValue] = {"dataset": dataset, "prereg": prereg_id, "verdict": j.verdict,
                                         "bases_positive": j.bases_positive}
        if j.sigma_p is not None:
            fields["sigma_p"] = j.sigma_p.value
        human = [f"{s:>10}  baseline={v.baseline!r} candidate={v.candidate!r} delta={v.delta!r} t={v.t:.2f}"
                 for s, v in j.per_subset.items()]
        human += [f"reason: {r}" for r in j.reasons]
        status: Status = "FAIL" if strict and j.verdict != "PASS" else "OK"
        return status, fields, {"judgement": j.model_dump(mode="json")}, human

    run_command("eval.judge", json_mode, data_root, fn)
```

Run: `uv run pytest tests/unit/test_cli_eval.py tests/unit/measure -q && uv run ruff check . && uv run ruff format --check .`。Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/measure/prereg.py src/vcp/measure/judge.py src/vcp/cli_eval.py tests/unit/measure/test_prereg_judge.py tests/unit/test_cli_eval.py
git commit -m "feat(measure,cli): 預登記（先寫死、append log）與判決（配對 bootstrap、雙基底、σ_p 條件、INVALID）——vcp eval preregister / judge

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: `status` / `report` / `--plugin`、端到端流程、真資料測試、文件、最終驗證（spec §6、§2.1、§12、§13）

**Files:**
- Create: `src/vcp/measure/report.py`、`src/vcp/measure/plugins.py`、`tests/unit/test_e2e_eval.py`、`tests/integration/test_rsna_knee_eval.py`
- Modify: `src/vcp/cli_eval.py`（`status`、`report`；`load_plugins` 改從 `measure.plugins` import）、`README.md`、`CLAUDE.md`、`tests/integration/README.md`
- Test: `tests/unit/measure/test_report.py`、`tests/unit/test_cli_eval.py`

**Interfaces:**
- Consumes: 全部前置任務。
- Produces:
  - `plugins.load_plugins(modules: list[str] | None) -> list[str]`（`importlib.import_module`，失敗 → `VcpError` 含模組名與原錯誤）
  - `report.StatusResult(orphans: list[str], preregs: int, judged: int, anchors: int, runs: int, sigma: dict[str, float])`、`report.status(paths, *, max_age_hours=48, now=None) -> StatusResult`
  - `report.report_rows(paths, *, plan_id=None, metric=None) -> list[dict]`（每個 run × subset × metric 的最新讀數，欄位 `run_id, subset, metric, params, value, ts, reading_id`）與 `report.last_vs_last(paths) -> list[dict]`（每個判決：`prereg_id, baseline_run, candidate_run, subset, delta, t, verdict`）
  - CLI `vcp eval status`、`vcp eval report`

- [ ] **Step 1: 寫失敗測試**

`tests/unit/measure/test_report.py`：

```python
import json

import pytest

from vcp.core.errors import VcpError
from vcp.core.paths import DatasetPaths
from vcp.measure.judge import JudgeSpec, judge
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.measure.plugins import load_plugins
from vcp.measure.prereg import create_prereg
from vcp.measure.report import last_vs_last, report_rows, status
from vcp.measure.schema import PreRegistration
from test_measure import det_with_runs


def test_status_orphans_and_report(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=40)
    measure_run(MeasureSpec(run_id="perfect", data_root=roots.data, configs_root=roots.configs))
    empty = status(paths)
    assert (empty.orphans, empty.preregs, empty.judged, empty.anchors, empty.runs) == ([], 0, 0, 0, 2)
    pr = PreRegistration(prereg_id="p1", claim="c", component="x", component_class="model",
                         baseline_run="perfect", candidate_run="noisy", metric="coco_map",
                         subsets=["valA", "valB"], created_at="2026-09-01T00:00:00.000Z")
    create_prereg(paths, pr, ReadingsLedger(paths.measure_dir / "readings.jsonl"))
    st = status(paths, max_age_hours=48, now="2026-09-04T00:00:00.000Z")
    assert st.orphans == ["p1"] and st.preregs == 1
    assert status(paths, max_age_hours=48, now="2026-09-01T12:00:00.000Z").orphans == []
    measure_run(MeasureSpec(run_id="noisy", data_root=roots.data, configs_root=roots.configs))
    judge(JudgeSpec(dataset="tiny", prereg_id="p1", resamples=20, data_root=roots.data,
                    configs_root=roots.configs))
    st = status(paths, max_age_hours=48, now="2026-09-04T00:00:00.000Z")
    assert st.orphans == [] and st.judged == 1
    rows = report_rows(paths)
    assert {(r["run_id"], r["subset"]) for r in rows} == {("perfect", "valA"), ("perfect", "valB"),
                                                          ("noisy", "valA"), ("noisy", "valB")}
    assert report_rows(paths, metric="accuracy") == []
    lvl = last_vs_last(paths)
    assert len(lvl) == 2 and {r["subset"] for r in lvl} == {"valA", "valB"}
    assert all(r["verdict"] == "FAIL" and r["delta"] < 0 for r in lvl)


def test_load_plugins(tmp_path, monkeypatch):
    (tmp_path / "myplug.py").write_text(
        "from vcp.measure.metrics import register_metric\n"
        "from vcp.measure.schema import MetricResult\n"
        "class One:\n"
        "    name, version, tasks, defaults = 'constant_one', '1', frozenset({'det'}), {}\n"
        "    def compute(self, samples, predictions, card, params):\n"
        "        return MetricResult(value=1.0, n=len(samples))\n"
        "register_metric(One())\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    assert load_plugins(["myplug"]) == ["myplug"]
    from vcp.measure.metrics import METRICS, applicable_metrics

    assert "constant_one" in METRICS and "constant_one" in applicable_metrics("det")
    METRICS.pop("constant_one")
    with pytest.raises(VcpError, match="cannot import plugin"):
        load_plugins(["definitely_missing_module"])
    assert load_plugins(None) == []
```

Run: `uv run pytest tests/unit/measure/test_report.py -q`。Expected: FAIL（ImportError）。

- [ ] **Step 2: 實作 `plugins.py` 與 `report.py`**

`plugins.py`：

```python
"""--plugin: import user modules (projects/<contest>/...) so they can register metrics and converters."""

from __future__ import annotations

import importlib

from vcp.core.errors import VcpError


def load_plugins(modules: list[str] | None) -> list[str]:
    loaded: list[str] = []
    for name in modules or []:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 - the plugin's own error text is what the user needs
            raise VcpError(f"cannot import plugin {name!r}: {type(e).__name__}: {e}") from e
        loaded.append(name)
    return loaded
```

`report.py`：

```python
"""vcp eval status / report: read-only views over the ledgers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from vcp.core.paths import DatasetPaths
from vcp.core.time import utc_now
from vcp.measure.anchors import load_anchors
from vcp.measure.ledger import ReadingsLedger, read_rows
from vcp.measure.metrics import params_key
from vcp.measure.prereg import list_preregs, prereg_time
from vcp.measure.schema import Judgement, SigmaEstimate


@dataclass(frozen=True)
class StatusResult:
    orphans: list[str]
    preregs: int
    judged: int
    anchors: int
    runs: int
    sigma: dict[str, float] = field(default_factory=dict)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def status(paths: DatasetPaths, *, max_age_hours: int = 48, now: str | None = None) -> StatusResult:
    current = _parse(now) if now else utc_now()
    judgements = read_rows(paths.measure_dir / "judgements.jsonl", Judgement)
    judged_ids = {j.prereg_id for j in judgements}
    orphans = []
    preregs = list_preregs(paths)
    for pid in preregs:
        ts = prereg_time(paths, pid)
        if pid not in judged_ids and ts and current - _parse(ts) > timedelta(hours=max_age_hours):
            orphans.append(pid)
    runs = [p for p in paths.runs_dir.glob("*/run.yaml")] if paths.runs_dir.is_dir() else []
    runs_here = 0
    from vcp.core.config import load_yaml_model
    from vcp.measure.schema import RunCard

    for p in runs:
        if load_yaml_model(p, RunCard).dataset == paths.name:
            runs_here += 1
    sigma: dict[str, float] = {}
    for est in sorted(read_rows(paths.measure_dir / "sigma.jsonl", SigmaEstimate), key=lambda e: e.ts):
        sigma[f"{est.metric}/{est.method}"] = est.value
    return StatusResult(orphans=orphans, preregs=len(preregs), judged=len(judged_ids),
                        anchors=len(load_anchors(paths)), runs=runs_here, sigma=sigma)


def report_rows(paths: DatasetPaths, *, plan_id: str | None = None, metric: str | None = None) -> list[dict]:
    latest: dict[tuple[str, str, str, str], object] = {}
    for r in sorted(ReadingsLedger(paths.measure_dir / "readings.jsonl").rows, key=lambda r: r.ts):
        if (plan_id and r.plan_id != plan_id) or (metric and r.metric != metric):
            continue
        latest[(r.run_id, r.subset, r.metric, params_key(r.params))] = r
    return [
        {"run_id": r.run_id, "subset": r.subset, "metric": r.metric, "params": params_key(r.params),
         "value": r.value, "ts": r.ts, "reading_id": r.reading_id}
        for r in sorted(latest.values(), key=lambda r: (r.run_id, r.subset, r.metric))
    ]


def last_vs_last(paths: DatasetPaths) -> list[dict]:
    out = []
    for j in read_rows(paths.measure_dir / "judgements.jsonl", Judgement):
        for subset, s in j.per_subset.items():
            out.append({"prereg_id": j.prereg_id, "baseline_run": j.baseline_run,
                        "candidate_run": j.candidate_run, "subset": subset, "delta": s.delta,
                        "t": s.t, "verdict": j.verdict, "ts": j.ts})
    return out
```

（`datetime.fromisoformat` / `timedelta` 不在 TID251 的禁清單；`utc_now()` 來自 `vcp.core.time`。`status` 內對 `RunCard` 的 import 可移到檔頭。）

`cli_eval.py`：刪掉本地 `load_plugins`，改 `from vcp.measure.plugins import load_plugins`。

Run: `uv run pytest tests/unit/measure/test_report.py -q`。Expected: PASS。

- [ ] **Step 3: CLI `status` / `report` 與端到端**

`src/vcp/cli_eval.py` 加：

```python
@eval_app.command("status")
def status_cmd(
    dataset: DatasetOpt,
    max_age_hours: Annotated[int, typer.Option("--max-age-hours")] = 48,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Orphan pre-registrations, anchors, latest sigma_p, run count."""

    def fn() -> CmdResult:
        paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
        st = status(paths, max_age_hours=max_age_hours)
        fields: dict[str, FieldValue] = {"dataset": dataset, "runs": st.runs, "preregs": st.preregs,
                                         "judged": st.judged, "anchors": st.anchors}
        if st.orphans:
            fields["orphans"] = ",".join(st.orphans)
        for k, v in st.sigma.items():
            fields[f"sigma[{k}]"] = v
        human = [f"orphan pre-registration (> {max_age_hours}h without a judgement): {p}" for p in st.orphans]
        return ("WARN" if st.orphans else "OK"), fields, {"status": st.__dict__}, human

    run_command("eval.status", json_mode, data_root, fn)


@eval_app.command("report")
def report_cmd(
    dataset: DatasetOpt,
    metric: Annotated[str | None, typer.Option("--metric")] = None,
    plan: Annotated[str | None, typer.Option("--plan")] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Every run x subset reading at full precision, plus last-vs-last deltas per judgement."""

    def fn() -> CmdResult:
        paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
        rows = report_rows(paths, plan_id=plan, metric=metric)
        lvl = last_vs_last(paths)
        human = [f"{r['run_id']:<20} {r['subset']:>8} {r['metric']:<12} {r['value']!r}" for r in rows]
        human += [f"{r['prereg_id']:<12} {r['subset']:>8} delta={r['delta']!r} t={r['t']:.2f} {r['verdict']}" for r in lvl]
        fields: dict[str, FieldValue] = {"dataset": dataset, "readings": len(rows), "judgements": len(lvl)}
        return "OK", fields, {"readings": rows, "last_vs_last": lvl}, human

    run_command("eval.report", json_mode, data_root, fn)
```

`tests/unit/test_e2e_eval.py`（全部走 CLI）：

```python
"""import -> split -> export yolo -> predictions -> ingest (yolo_txt) x2 -> measure -> anchor
-> preregister -> measure candidate -> judge -> status -> report."""

import json
from pathlib import Path

from typer.testing import CliRunner

from helpers import CATS, det_samples, noisy_predictions, perfect_predictions, write_images
from vcp.cli import app
from vcp.data.dataset import Dataset, write_samples_jsonl

runner = CliRunner()


def _verdicts(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("VERDICT ")]


def _yolo_txt(pred_dir: Path, ds: Dataset, preds, manifest: dict) -> None:
    flat_of = {v: k for k, v in manifest["images"].items()}
    index_of = {c["id"]: c["index"] for c in manifest["categories"]}
    (pred_dir / "labels").mkdir(parents=True, exist_ok=True)
    for p in preds:
        if not p.boxes:
            continue
        v = ds.by_id[p.sample_id].views[0]
        lines = [f"{index_of[b.category_id]} {(b.x + b.w / 2) / v.width:.6f} {(b.y + b.h / 2) / v.height:.6f} "
                 f"{b.w / v.width:.6f} {b.h / v.height:.6f} {b.score:.4f}" for b in p.boxes]
        (pred_dir / "labels" / (Path(flat_of[p.sample_id]).stem + ".txt")).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")


def test_eval_flow(roots, tmp_path):
    src = roots.data / "raw" / "flow"
    samples = det_samples(80, seed=5)
    write_images(src, samples)
    write_samples_jsonl(src / "samples.jsonl", samples)
    (src / "cats.json").write_text(json.dumps([c.model_dump() for c in CATS]), encoding="utf-8")
    common = ["--license", "CC0", "--url", "u", "--downloaded-at", "2026-09-04"]
    assert runner.invoke(app, ["data", "import", "--importer", "jsonl", "--src", str(src), "--name", "flow",
                               *common, "--opt", "task=det", "--opt", "categories=cats.json"]).exit_code == 0
    assert runner.invoke(app, ["data", "split", "--name", "flow", "--plan-id", "fixed-v1", "--seed", "3"]).exit_code == 0
    ds = Dataset.load("flow", data_root=roots.data, configs_root=roots.configs)
    from vcp.data.split import load_plan
    from vcp.core.paths import DatasetPaths
    plan = load_plan(DatasetPaths.resolve("flow", data_root=roots.data, configs_root=roots.configs), "fixed-v1")
    exports = {}
    for subset in ("valA", "valB"):
        out = tmp_path / f"yolo-{subset}"
        assert runner.invoke(app, ["data", "export", "--name", "flow", "--plan", "fixed-v1", "--subset", subset,
                                   "--format", "yolo", "--out", str(out), "--opt", "copy=true"]).exit_code == 0
        exports[subset] = out
    for run_id, maker in (("base", lambda s: noisy_predictions(s, ds.card, seed=7, flip=0.5)),
                          ("cand", lambda s: perfect_predictions(s, ds.card))):
        for subset in ("valA", "valB"):
            manifest = json.loads((exports[subset] / "manifest.json").read_text(encoding="utf-8"))
            pred_dir = tmp_path / f"{run_id}-{subset}"
            _yolo_txt(pred_dir, ds, maker(ds.subset(subset, plan)), manifest)
            r = runner.invoke(app, ["eval", "ingest", "--run", run_id, "--dataset", "flow", "--plan", "fixed-v1",
                                    "--subset", subset, "--format", "yolo_txt", "--src", str(pred_dir),
                                    "--export-manifest", str(exports[subset]), "--trained-on", "train"])
            assert r.exit_code == 0, r.output
    assert "readings=2" in _verdicts(runner.invoke(app, ["eval", "measure", "--run", "base"]).output)[-1]
    assert runner.invoke(app, ["eval", "anchor", "--run", "base", "--subset", "valA", "--metric", "coco_map"]).exit_code == 0
    r = runner.invoke(app, ["eval", "preregister", "--dataset", "flow", "--id", "p1", "--claim", "perfect wins",
                            "--component", "cand", "--class", "model", "--baseline-run", "base",
                            "--candidate-run", "cand", "--metric", "coco_map"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["eval", "measure", "--run", "cand"])
    assert r.exit_code == 0 and "guardrail=partial" in _verdicts(r.output)[-1]
    r = runner.invoke(app, ["eval", "judge", "--dataset", "flow", "--prereg", "p1", "--resamples", "40", "--strict"])
    assert r.exit_code == 0, r.output
    assert "verdict=PASS" in _verdicts(r.output)[-1]
    r = runner.invoke(app, ["eval", "status", "--dataset", "flow"])
    assert r.exit_code == 0 and "judged=1" in _verdicts(r.output)[-1]
    r = runner.invoke(app, ["eval", "report", "--dataset", "flow", "--json"])
    assert r.exit_code == 0
    doc = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert doc["fields"]["readings"] == 4 and doc["result"]["last_vs_last"][0]["verdict"] == "PASS"
```

`tests/unit/test_cli_eval.py` 加一個 `--plugin` 的 CLI 測試（用 `test_report.py` 同樣的暫存模組，`monkeypatch.syspath_prepend`，`vcp eval measure --run m1 --metrics constant_one --plugin myplug` → VERDICT `metrics=constant_one`，結尾 `METRICS.pop("constant_one")`）。

Run: `uv run pytest tests/unit/test_e2e_eval.py tests/unit/test_cli_eval.py -q`。Expected: PASS。

- [ ] **Step 4: 真資料測試與文件**

`tests/integration/test_rsna_knee_eval.py`：

```python
"""Measurement layer on the RSNA Knee subset: perfect and random predictions bracket macro AUC."""

from __future__ import annotations

import random

import pytest

from conftest import load_real
from vcp.measure.metrics import get_metric
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import Prediction

pytestmark = pytest.mark.realdata


@pytest.fixture(scope="module")
def knee(real_roots):
    return load_real("rsna-knee", real_roots)


def test_macro_auc_brackets(knee):
    gold = [s for s in knee.samples if s.label_source == "gold"]
    names = [c.name for c in knee.card.categories]
    perfect = predictions_by_id([Prediction(sample_id=s.sample_id,
                                            scores={n: float(s.labels.targets[n]) for n in names}) for s in gold])
    res = get_metric("macro_auc").compute(gold, perfect, knee.card, {})
    assert res.value == pytest.approx(1.0) and res.n == len(gold)
    rng = random.Random(0)
    noise = predictions_by_id([Prediction(sample_id=s.sample_id, scores={n: rng.random() for n in names})
                               for s in gold])
    assert 0.25 < get_metric("macro_auc").compute(gold, noise, knee.card, {}).value < 0.75
```

`README.md` 新增「量測層命令 `vcp eval`」一節（表格：`ingest`、`measure`、`anchor`、`preregister`、`judge`、`sigma`、`status`、`report` 各一列，含主要選項；一段「標準預測格式」四種 payload 範例；一段「一次判決的流程」六條命令；一句「比賽官方計分器以 `--plugin projects.<contest>.metrics` 登記」）；`CLAUDE.md`「常用命令」加 `uv run vcp eval measure --run R` 與 `uv run vcp eval judge --dataset D --prereg ID`，「路徑」加 `runs/<run_id>/` 與 `measure/<name>/` 只增不改；`tests/integration/README.md` 加 `-m realdata` 會跑量測層的 RSNA 測試。

- [ ] **Step 5: 最終驗證**

Run:
```bash
uv run pytest -q
uv run pytest --cov=vcp -q | tail -3
uv run ruff check . && uv run ruff format --check .
uv run vcp eval --help
wc -l src/vcp/cli.py src/vcp/cli_eval.py src/vcp/measure/*.py
```
Expected: 全綠；TOTAL ≥ 80%（預期 ≥ 92%）；`vcp eval --help` 列出八個命令；每個檔案 < 800 行。

- [ ] **Step 6: Commit**

```bash
git add src/vcp/measure/report.py src/vcp/measure/plugins.py src/vcp/cli_eval.py tests/unit/measure/test_report.py tests/unit/test_cli_eval.py tests/unit/test_e2e_eval.py tests/integration/test_rsna_knee_eval.py tests/integration/README.md README.md CLAUDE.md
git commit -m "feat(measure,cli)+docs: vcp eval status / report、--plugin、端到端流程、RSNA 真資料 AUC 測試、README 量測層一節

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## 自我審查紀錄（撰寫計畫時）

- **Spec 覆蓋**：§4.1 → T2；§4.2 → T2、T5；§4.3、§4.4 → T9；§4.5、§4.6 → T11；§4.7 → T10；§5 → T1、T2；§6 八命令 → T5（ingest）、T9（measure、anchor）、T10（sigma）、T11（preregister、judge）、T12（status、report、`--plugin`）；§6.1 → T9 `default_subsets`；§6.2 → T11 `judge`；§6.3 → T10；§7 → T6、T7、T8；§8 → T3、T4；§9 → T9（護欄、不寫）、T2/T5（sha、history）；§10 → 各任務的錯誤類型；§11 → T1（YOLO manifest）；§12 → 各任務測試 + T12 e2e / realdata；§13 驗收 1 → T5、2 → T9 + T6/7/8、3 → T9 護欄測試、4 → T11、5 → T10/T11、6 → T12、7 → T12 plugin、8 → T12 Step 5。
- **計畫層決定（spec 未明說）**：`t` 在 se 為 0 時以 ±1e9 代替無限大（JSON 可寫）；`measure` 的 `--params` 只傳給宣告該 key 的指標，沒有任何指標認得的 key 才 FAIL；`judge` 缺讀數回 `FAIL reason=missing_readings`（不是例外）；`status` 的 runs 數只算屬於該資料集的 run；`report` 每個 (run, subset, metric, params) 取最新讀數；bootstrap 重抽允許重複樣本；`create_prereg` 把有效參數寫進檔案（含預設值）以固定 `params_key`。
- **型別一致性**：`Prediction.payload_field()`（T2）用於 T2 `check_predictions`；`ConvertContext(dataset, subset_ids, export_dir, options)` 位置參數順序在 T3/T4/T5 一致；`Metric.compute(samples, predictions: dict, card, params)` 在 T6–T11 一致；`effective_params(metric, params)` 與 `params_key(params)` 的用法在 T9/T10/T11 一致；`load_context(run_id, data_root, configs_root) -> (card, dataset, plan, paths)` 在 T9/T10/T11/T12 一致；`ReadingsLedger(path).rows/.by_id/.append` 在 T9/T11/T12 一致；`Reading.params` 為有效參數（含預設）故 `params_key(r.params)` 可與 `params_key(effective_params(...))` 比對。
