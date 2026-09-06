# vcp 提交治理層實作計畫（子專案 5）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓每一發提交都是一個有身分（eval / test run 配對）、過了機械準入、在配額與封槍狀態機底下、台帳自動記錄的動作；最終發由凍結 holdout 的讀數依寫死的規則決定。

**Architecture:** 新套件 `src/vcp/submit/`（schema、ledger、timewin、profile、guards、pairing、gate、writers 登記表、platforms 登記表、stage、actions、sync、final、report）加 `src/vcp/cli_submit.py`（`vcp submit` 的 12 個命令）。設定檔與台帳進 git（`configs/datasets/<test>/`），輸出檔與 `stage.json` 進 data root（`submit/<test>/<id>/`）。量測層改一處：`vcp eval ingest --weights / --config` 讓 test 側 run 帶權重身分。Kaggle 經其 CLI 子程序（可注入 runner），vcp 對憑證零接觸。

**Tech Stack:** Python 3.12、pydantic v2、typer 0.27、`zoneinfo` + `tzdata`、標準庫 `csv` / `json` / `subprocess`、pytest、ruff（line-length 100）。

**Spec:** `docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md`

## Global Constraints

- 取時只能用 `vcp.core.time.utc_now()` / `stamp()` / `parse_stamp()`（ruff TID251 擋其他時鐘 API）；時區換算只經 `zoneinfo`，新增依賴 `tzdata>=2024.1`。
- 每個 CLI 命令以 `VERDICT cmd=submit.<name> status=OK|WARN|FAIL|ABORT …` 收尾，exit 0 / 0 / 1 / 2；`--json` 時結果 JSON 到 stdout、VERDICT 到 stderr；永不互動提問。
- 錯誤對應：`ValidationFailed` / `IntegrityError` / `PlatformError` = FAIL；`PlanMismatchError` / `RegistryError` / `VcpError` = ABORT。錯誤訊息以 `reason=` 字彙開頭（`locked: …`、`quota_exhausted: …`、`identity: …`），`VcpError.fields` 只放機器可讀鍵。
- pydantic 模型一律 `extra="forbid"`；設定檔沒有任何憑證欄位；vcp 不讀、不寫、不驗、不記錄憑證；第三方 CLI 的 stdout / stderr 落地前先 `redact`。
- 檔案 utf-8、LF；輸出檔決定性：同輸入同位元組（依 `sample_id` 排序、浮點 `repr`、不寫時間）。
- 台帳只 append；`stage.json` 寫一次不改；stage 全有或全無（任一門不過 → 不留目錄不留列）。
- `src/vcp` 不出現比賽名；輸出格式與平台都是登記表（`register_writer` / `register_platform`）。
- 覆蓋率 ≥ 80%（`uv run pytest --cov=vcp`），`uv run ruff check .` 與 `uv run ruff format --check .` 乾淨；測試永不碰真資料根（`roots` fixture）。
- commit 訊息結尾加一行 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`（用第二個 `-m`）。

## 計畫層決定（spec 未明說或需修正之處；收尾時寫進 spec §17 補充決定）

1. **`vcp eval ingest --weights PATH [--config PATH]`**：ingest 建 run 時把檔案 sha256 寫進 `run.yaml` 的 `source.weights_hash` / `config_hash`；既有 run 的空值可補、非空值不同即 FAIL。沒有這條，test 側 run（只經 ingest）永遠沒有權重身分，§7 的配對核對無從做起。spec §13「量測層零改動」改為「一處改動」。
2. **probe 跳過身分核對**：`kind=probe` 的 file 類候選記 `pairing.checks=["identity=skipped"]`（mode 依 test 側有無 `fuse.json` 定）；probe 永不進決選，所以放行安全。kernel 類一律核對權重 sha。
3. **封槍後的最終發**：lock 後 `stage` 一律 FAIL；`upload` / `record` 只放行最新 `final` 列 `chosen` 裡的 id（`board_rule=last` 時選中者必須重傳成最後一發）。
4. **`record --at` 的容差**：`at` 不得早於 `staged_at` 截到秒；不得晚於 `utc_now() + 60 s`（人填的是平台顯示的分鐘級時間）。
5. **`PlatformError(VcpError)`，`status="FAIL"`**：加進 `vcp/core/errors.py`；平台 CLI 非 0 用它，CLI 找不到仍是 `VcpError`（ABORT）。
6. **Kaggle 執行檔檢查只在沒注入 runner 時做**（同訓練層 rclone）；端到端測試以 `kaggle_command: [sys.executable, <假腳本>]` 走真子程序。
7. **test plan 有一個空的 `train` 子集**：`SplitPlan` 要求恰一個 role=train 的子集，所以 `init` 建的 `all-v1` 是 `train:train:0.0` + `<test_subset>:eval:1.0`，`params.eval_gold_only=false`。
8. **sync 配對規則 0**：平台列的 `platform_ref` 等於某 `uploaded` 列的 `platform_ref`（`record --platform-ref` 填的）→ 直接配給它；其後才是 description 含 id、檔名 + 時間。
9. **report 的 foreign 列**：以 `foreign:<platform_ref>` 為 id 出現在 last-vs-last 序列裡（它們在榜上真的存在）。
10. **台帳列以 `exclude_none` 寫出**：一列只帶自己事件的欄位；讀回時缺的欄位就是 `None`。

## 檔案結構

| 檔案 | 責任 |
|---|---|
| `src/vcp/submit/__init__.py` | 套件 docstring |
| `src/vcp/submit/schema.py` | `Quota`、`PlatformProfile`、`Pairing` / `PairMember`、`WeightRef`、`Artifact`、`Gate`、`Staged`、`FinalEntry`、`LedgerRow`、`EVENTS` |
| `src/vcp/submit/ledger.py` | `SubmissionLedger`（讀 / append / 各種查詢）、`append_ledger_row` |
| `src/vcp/submit/timewin.py` | `Window`、`window_for`、`parse_at`、`local_text`、`used_in` |
| `src/vcp/submit/profile.py` | `load_profile`、`init_profile`、`ensure_test_plan` |
| `src/vcp/submit/guards.py` | `assert_unlocked`、`assert_before_deadline`、`quota_state`、`assert_quota` |
| `src/vcp/submit/writers/base.py` | `WriteContext`、`WriteResult`、`Writer`、`WRITERS`、`register_writer`、`get_writer`、`writer_for`、`output_ids`、`check_options`、`parse_columns`、`fmt_float`、`is_true` |
| `src/vcp/submit/writers/{scores_csv,coco_results,csv_boxes}.py` | 三個 writer |
| `src/vcp/submit/writers/__init__.py` | 登記三個 writer |
| `src/vcp/submit/platforms/base.py` | `Runner`、`default_runner`、`redact`、`UploadResult`、`PlatformSubmission`、`Platform`、`PLATFORMS`、`register_platform`、`get_platform` |
| `src/vcp/submit/platforms/{manual,kaggle}.py` + `__init__.py` | 兩個平台 |
| `src/vcp/submit/pairing.py` | `verify_pairing`、`verify_weights`、`is_fusion` |
| `src/vcp/submit/gate.py` | `admit`、`latest_judgements` |
| `src/vcp/submit/stage.py` | `StageSpec`、`stage`、`load_staged`、`stage_json`、`verify` |
| `src/vcp/submit/actions.py` | `upload`、`record`、`score` |
| `src/vcp/submit/sync.py` | `sync`、`match_submission` |
| `src/vcp/submit/final.py` | `final`、`lock`、`unlock`、`sealed_reading`、`count_unseals` |
| `src/vcp/submit/report.py` | `status`、`report` |
| `src/vcp/cli_submit.py` | `submit_app`：init / stage / verify / upload / record / score / sync / final / lock / unlock / status / report |
| `src/vcp/cli.py` | `app.add_typer(submit_app, name="submit")` |
| `src/vcp/core/paths.py` | `DatasetPaths.submit_yaml` / `submissions_log` / `submit_dir` / `submission_dir()` |
| `src/vcp/core/errors.py` | `PlatformError` |
| `src/vcp/measure/ingest.py`、`src/vcp/cli_eval.py` | `--weights` / `--config` |
| `pyproject.toml`、`uv.lock` | `tzdata` |
| `tests/unit/submit/test_*.py`、`tests/unit/test_cli_submit.py`、`tests/unit/test_e2e_submit.py`、`tests/integration/test_rsna_knee_submit.py` | 測試 |
| `README.md`、`CLAUDE.md` | 文件 |

## 給實作者的共用約定

- 測試用 `roots` fixture（`tests/conftest.py`）與 `tests/helpers.py` 的 `make_card` / `cls_samples` / `write_images` / `perfect_predictions` / `noisy_predictions`；CLI 測試用 `typer.testing.CliRunner` 對 `vcp.cli.app`，VERDICT 從 `r.output` 取最後一行 `VERDICT `。
- 寫完程式碼先 `uv run ruff format <檔案>` 再跑測試；pytest 用 `uv run pytest <路徑> -o addopts="" -q`。
- 每個任務結尾：`uv run ruff check . && uv run ruff format --check .` 乾淨才 commit。
- 錯誤訊息：`ValidationFailed("locked: since 2026-… (final)", fields={"since": …, "why": …})`——`reason=` 由 `run_command` 自動加上例外類別與訊息，訊息開頭就是字彙。

---

### Task 1: schema、台帳、路徑、tzdata

**Files:**
- Create: `src/vcp/submit/__init__.py`、`src/vcp/submit/schema.py`、`src/vcp/submit/ledger.py`
- Modify: `src/vcp/core/paths.py`（`DatasetPaths` 末尾加四個成員）、`pyproject.toml:7-15`
- Test: `tests/unit/submit/__init__.py`（空）、`tests/unit/submit/test_schema.py`、`tests/unit/submit/test_ledger.py`

**Interfaces:**
- Consumes: `vcp.measure.ledger.read_rows`、`vcp.core.time.parse_stamp`、`vcp.core.paths.validate_name`。
- Produces: `vcp.submit.schema`（`Quota`、`PlatformProfile(...).effective_display_tz()`、`PairMember`、`Pairing`、`WeightRef`、`Artifact`、`Gate`、`Staged`、`FinalEntry`、`LedgerRow`、`EVENTS`、`check_tz`）；`vcp.submit.ledger.SubmissionLedger(path)` 的 `rows` / `append(row)` / `of(event, submission_id=None)` / `ids()` / `staged(id)` / `uploads(id)` / `latest_score(id)` / `arrivals()` / `last_uploaded()` / `foreign_refs()` / `lock_state()` / `latest_final()`；`DatasetPaths.submit_yaml` / `submissions_log` / `submit_dir` / `submission_dir(id)`。

- [ ] **Step 1: 依賴與路徑**

`pyproject.toml` 的 `dependencies` 在 `"iterative-stratification>=0.1.9",` 後加一行 `"tzdata>=2024.1",`，然後 `uv sync`（`uv.lock` 會變，一起 commit）。

`src/vcp/core/paths.py` 的 `DatasetPaths` 在 `prereg_log` 之後、`resolve_image_root` 之前加：

```python
@property
def submit_yaml(self) -> Path:
    return self.config_dir / "submit.yaml"


@property
def submissions_log(self) -> Path:
    return self.config_dir / "submissions.jsonl"


@property
def submit_dir(self) -> Path:
    return self.data_root / "submit" / self.name


def submission_dir(self, submission_id: str) -> Path:
    validate_name(submission_id)
    return self.submit_dir / submission_id
```

- [ ] **Step 2: 寫失敗的測試** `tests/unit/submit/test_schema.py`

```python
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.submit.schema import EVENTS, Artifact, Gate, LedgerRow, PlatformProfile, Quota

STAMP = "2026-09-05T00:00:00.000Z"


def _profile(**over):
    base = dict(
        dataset="t",
        eval_dataset="d",
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


def test_tzdata_is_available():
    assert ZoneInfo("Asia/Taipei") is not None


def test_profile_defaults_and_display_tz():
    p = _profile()
    assert p.test_plan == "all-v1" and p.test_subset == "test" and p.final_slots == 1
    assert p.kaggle_command == ["kaggle"] and p.quota is None
    assert p.effective_display_tz() == "UTC"
    q = _profile(quota={"per_day": 3, "day_tz": "Asia/Taipei"})
    assert q.quota == Quota(per_day=3, day_tz="Asia/Taipei", day_start="00:00")
    assert q.effective_display_tz() == "Asia/Taipei"
    assert _profile(display_tz="America/New_York").effective_display_tz() == "America/New_York"


@pytest.mark.parametrize(
    "bad",
    [
        {"kaggle_key": "abc"},
        {"platform": "kaggle"},
        {"writer": None},
        {"display_tz": "Mars/Olympus"},
        {"deadline": "2026-09-05 00:00"},
        {"quota": {"per_day": 0}},
        {"quota": {"per_day": 3, "day_start": "24:00"}},
        {"quota": {"per_day": 3, "day_tz": "Nowhere/City"}},
        {"final_slots": 0},
        {"kaggle_command": []},
        {"board_rule": "median"},
    ],
)
def test_profile_rejects(bad):
    with pytest.raises(ValidationError):
        _profile(**bad)


def test_kernel_profile_needs_no_writer():
    p = _profile(platform="kaggle", competition="c1", submission_kind="kernel", writer=None)
    assert p.writer is None and p.competition == "c1"


def test_artifact_shapes():
    Artifact(
        kind="file",
        path="submission.csv",
        bytes=1,
        sha256="a" * 64,
        md5="b" * 32,
        writer="scores_csv",
        writer_version="1",
        rows=1,
        samples=1,
        missing=0,
    )
    with pytest.raises(ValidationError, match="file artifact needs"):
        Artifact(kind="file", path="submission.csv")
    Artifact(
        kind="kernel",
        kernel="u/nb",
        version=3,
        output="submission.csv",
        weights=[{"run": "r", "sha256": "c" * 64}],
    )
    with pytest.raises(ValidationError, match="kernel artifact needs at least one"):
        Artifact(kind="kernel", kernel="u/nb", version=3, output="submission.csv")


def test_ledger_rows_require_their_event_fields():
    assert EVENTS == ("staged", "uploaded", "scored", "foreign", "final", "lock", "unlock", "note")
    gate = Gate(admission="PASS", judgements=["p1"])
    LedgerRow(
        event="staged",
        ts=STAMP,
        submission_id="S1",
        kind="candidate",
        eval_run="e",
        gate=gate,
        profile_sha256="p" * 64,
    )
    with pytest.raises(ValidationError, match="staged needs"):
        LedgerRow(event="staged", ts=STAMP, submission_id="S1")
    LedgerRow(
        event="uploaded",
        ts=STAMP,
        submission_id="S1",
        at=STAMP,
        source="vcp",
        confirmed=True,
        profile_sha256="p" * 64,
    )
    with pytest.raises(ValidationError, match="scored needs a public or private"):
        LedgerRow(event="scored", ts=STAMP, submission_id="S1", source="manual")
    with pytest.raises(ValidationError, match="finite"):
        LedgerRow(
            event="scored", ts=STAMP, submission_id="S1", source="manual", public=float("nan")
        )
    LedgerRow(event="lock", ts=STAMP, reason="final")
    with pytest.raises(ValidationError, match="lock needs"):
        LedgerRow(event="lock", ts=STAMP)
    LedgerRow(event="foreign", ts=STAMP, platform_ref="x", file_name="f.csv", at=STAMP)
    with pytest.raises(ValidationError):
        LedgerRow(event="party", ts=STAMP)


def test_paths(roots):
    paths = DatasetPaths.resolve("t", data_root=roots.data, configs_root=roots.configs)
    assert paths.submit_yaml == roots.configs / "datasets" / "t" / "submit.yaml"
    assert paths.submissions_log == roots.configs / "datasets" / "t" / "submissions.jsonl"
    assert paths.submit_dir == roots.data / "submit" / "t"
    assert paths.submission_dir("S1") == roots.data / "submit" / "t" / "S1"
    with pytest.raises(ValidationFailed):
        paths.submission_dir("bad name")
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/unit/submit/test_schema.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: vcp.submit`）

- [ ] **Step 4: 寫 `src/vcp/submit/__init__.py` 與 `src/vcp/submit/schema.py`**

`__init__.py`：

```python
"""Submission governance (spec 5): staged candidates, an append-only ledger, quota and lock
state, and a final pick decided by the sealed holdout."""
```

`schema.py`：

```python
"""Pydantic models of the submission layer (spec 4): the platform profile, a staged candidate's
snapshot, and the rows of the submissions ledger."""

from __future__ import annotations

import math
import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vcp.core.time import parse_stamp

PlatformName = Literal["manual", "kaggle"]
SubmissionKind = Literal["file", "kernel"]
BoardRule = Literal["last", "best"]
FinalRule = Literal["best_sealed"]
CandidateKind = Literal["candidate", "baseline", "probe"]
Admission = Literal["PASS", "waived"]
PairingMode = Literal["single", "fusion", "kernel"]
Event = Literal["staged", "uploaded", "scored", "foreign", "final", "lock", "unlock", "note"]
EVENTS = ("staged", "uploaded", "scored", "foreign", "final", "lock", "unlock", "note")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_REQUIRED: dict[str, tuple[str, ...]] = {
    "staged": ("submission_id", "kind", "eval_run", "gate", "profile_sha256"),
    "uploaded": ("submission_id", "at", "source", "confirmed", "profile_sha256"),
    "scored": ("submission_id", "source"),
    "foreign": ("platform_ref", "file_name", "at"),
    "final": (
        "rule",
        "slots",
        "chosen",
        "table",
        "metric",
        "params",
        "holdout_unseals",
        "profile_sha256",
    ),
    "lock": ("reason",),
    "unlock": ("reason",),
    "note": ("text",),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def check_tz(name: str) -> str:
    """A zoneinfo name, or ValueError (pydantic turns it into a validation error)."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown time zone {name!r}") from e
    return name


def _finite(name: str, v: float | None) -> None:
    if v is not None and not math.isfinite(v):
        raise ValueError(f"{name} must be finite, got {v!r}")


class Quota(_Strict):
    """Uploads allowed per platform day: the day starts at ``day_start`` wall time in ``day_tz``."""

    per_day: int = Field(ge=1)
    day_tz: str = "UTC"
    day_start: str = "00:00"

    @field_validator("day_tz")
    @classmethod
    def _tz(cls, v: str) -> str:
        return check_tz(v)

    @field_validator("day_start")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError(f"day_start must be HH:MM, got {v!r}")
        return v


class PlatformProfile(_Strict):
    """``configs/datasets/<test>/submit.yaml`` (spec 4.1). No credential field exists, and
    ``extra="forbid"`` refuses one that is added by hand."""

    dataset: str
    eval_dataset: str
    plan_id: str
    sealed_subset: str
    test_plan: str = "all-v1"
    test_subset: str = "test"
    platform: PlatformName
    competition: str | None = None
    submission_kind: SubmissionKind = "file"
    board_rule: BoardRule
    final_rule: FinalRule = "best_sealed"
    final_slots: int = Field(default=1, ge=1)
    quota: Quota | None = None
    display_tz: str | None = None
    deadline: str | None = None
    metric: str
    metric_params: dict[str, str] = Field(default_factory=dict)
    writer: str | None = None
    writer_opts: dict[str, str] = Field(default_factory=dict)
    kaggle_command: list[str] = Field(default_factory=lambda: ["kaggle"])
    created_at: str

    @field_validator("display_tz")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        return None if v is None else check_tz(v)

    @field_validator("deadline")
    @classmethod
    def _stamp(cls, v: str | None) -> str | None:
        if v is not None:
            parse_stamp(v)
        return v

    @model_validator(mode="after")
    def _requirements(self) -> PlatformProfile:
        if self.platform == "kaggle" and not self.competition:
            raise ValueError("platform=kaggle needs competition")
        if self.submission_kind == "file" and not self.writer:
            raise ValueError("submission_kind=file needs writer")
        if not self.kaggle_command:
            raise ValueError("kaggle_command must not be empty")
        return self

    def effective_display_tz(self) -> str:
        """The zone ``record --at`` is read in: display_tz, else the quota day's zone, else UTC."""
        if self.display_tz:
            return self.display_tz
        if self.quota is not None:
            return self.quota.day_tz
        return "UTC"


class PairMember(_Strict):
    eval: str
    test: str
    mode: PairingMode


class Pairing(_Strict):
    """How the eval and test sides were shown to be the same model (spec 7)."""

    mode: PairingMode
    members: list[PairMember] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)


class WeightRef(_Strict):
    run: str
    sha256: str


class Artifact(_Strict):
    """What was (or will be) uploaded: a file with hashes, or a kernel reference."""

    kind: SubmissionKind
    path: str | None = None
    bytes: int | None = None
    sha256: str | None = None
    md5: str | None = None
    writer: str | None = None
    writer_version: str | None = None
    writer_opts: dict[str, str] = Field(default_factory=dict)
    rows: int | None = None
    samples: int | None = None
    missing: int | None = None
    kernel: str | None = None
    version: int | None = None
    output: str | None = None
    weights: list[WeightRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape(self) -> Artifact:
        if self.kind == "file":
            needed = {
                "path": self.path,
                "bytes": self.bytes,
                "sha256": self.sha256,
                "md5": self.md5,
                "writer": self.writer,
                "writer_version": self.writer_version,
                "rows": self.rows,
                "samples": self.samples,
                "missing": self.missing,
            }
            missing = [k for k, v in needed.items() if v is None]
            if missing:
                raise ValueError(f"file artifact needs {missing}")
        else:
            if self.kernel is None or self.version is None or self.output is None:
                raise ValueError("kernel artifact needs kernel, version and output")
            if not self.weights:
                raise ValueError("kernel artifact needs at least one weights reference")
        return self


class Gate(_Strict):
    admission: Admission
    judgements: list[str] = Field(default_factory=list)
    reason: str = ""


class Staged(_Strict):
    """``submit/<test>/<submission_id>/stage.json`` (spec 4.2): written once, never edited."""

    submission_id: str
    dataset: str
    kind: CandidateKind
    eval_run: str
    test_run: str | None
    pairing: Pairing
    artifact: Artifact
    gate: Gate
    profile_sha256: str
    staged_at: str
    vcp_version: str


class FinalEntry(_Strict):
    submission_id: str
    eligible: bool
    why: str
    sealed_value: float | None
    sealed_reading_id: str | None
    public: float | None
    staged_at: str

    @model_validator(mode="after")
    def _finite_values(self) -> FinalEntry:
        _finite("sealed_value", self.sealed_value)
        _finite("public", self.public)
        return self


class LedgerRow(_Strict):
    """One line of ``submissions.jsonl`` (spec 4.3). Every event shares the model; which fields
    it must carry is decided per event so a row can never be silently half-written."""

    event: Event
    ts: str
    submission_id: str | None = None
    kind: CandidateKind | None = None
    eval_run: str | None = None
    test_run: str | None = None
    sha256: str | None = None
    md5: str | None = None
    gate: Gate | None = None
    profile_sha256: str | None = None
    at: str | None = None
    source: str | None = None
    platform_ref: str | None = None
    message: str | None = None
    confirmed: bool | None = None
    public: float | None = None
    private: float | None = None
    platform_status: str | None = None
    file_name: str | None = None
    submitted_by: str | None = None
    rule: str | None = None
    slots: int | None = None
    chosen: list[str] | None = None
    table: list[FinalEntry] | None = None
    metric: str | None = None
    params: dict[str, str] | None = None
    holdout_unseals: int | None = None
    reason: str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> LedgerRow:
        missing = [f for f in _REQUIRED[self.event] if getattr(self, f) is None]
        if missing:
            raise ValueError(f"{self.event} needs {missing}")
        if self.event == "scored" and self.public is None and self.private is None:
            raise ValueError("scored needs a public or private score")
        _finite("public", self.public)
        _finite("private", self.private)
        return self
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/unit/submit/test_schema.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 6: 寫失敗的測試** `tests/unit/submit/test_ledger.py`

```python
import pytest

from vcp.core.errors import ValidationFailed
from vcp.submit.ledger import SubmissionLedger, append_ledger_row
from vcp.submit.schema import Gate, LedgerRow

T0 = "2026-09-05T00:00:00.000Z"
T1 = "2026-09-05T01:00:00.000Z"
T2 = "2026-09-05T02:00:00.000Z"


def _staged(sid, kind="candidate"):
    return LedgerRow(
        event="staged",
        ts=T0,
        submission_id=sid,
        kind=kind,
        eval_run="e",
        gate=Gate(admission="PASS"),
        profile_sha256="p" * 64,
    )


def _uploaded(sid, at, ref=None):
    return LedgerRow(
        event="uploaded",
        ts=at,
        submission_id=sid,
        at=at,
        source="manual",
        platform_ref=ref,
        confirmed=True,
        profile_sha256="p" * 64,
    )


def test_rows_round_trip_without_nulls(tmp_path):
    path = tmp_path / "submissions.jsonl"
    led = SubmissionLedger(path)
    assert led.rows == []
    led.append(_staged("S1"))
    led.append(_uploaded("S1", T1, ref="k1"))
    led.append(LedgerRow(event="scored", ts=T2, submission_id="S1", source="manual", public=0.5))
    text = path.read_text(encoding="utf-8")
    assert "null" not in text and text.count("\n") == 3
    again = SubmissionLedger(path)
    assert again.rows == led.rows
    assert again.ids() == ["S1"]
    assert again.staged("S1").kind == "candidate" and again.staged("S9") is None
    assert [r.at for r in again.uploads("S1")] == [T1]
    assert again.latest_score("S1").public == 0.5 and again.latest_score("S2") is None


def test_arrivals_last_uploaded_and_foreign_refs(tmp_path):
    led = SubmissionLedger(tmp_path / "s.jsonl")
    led.append(_staged("S1"))
    led.append(_staged("S2"))
    led.append(_uploaded("S2", T2))
    led.append(
        LedgerRow(event="foreign", ts=T2, platform_ref="f1", file_name="x.csv", at=T1, public=0.1)
    )
    led.append(_uploaded("S1", T0))
    assert [r.at for r in led.arrivals()] == [T0, T1, T2]
    assert led.last_uploaded().submission_id == "S2"
    assert led.foreign_refs() == {"f1"}


def test_lock_state_and_latest_final(tmp_path):
    led = SubmissionLedger(tmp_path / "s.jsonl")
    assert led.lock_state() is None and led.latest_final() is None
    led.append(LedgerRow(event="lock", ts=T0, reason="r0"))
    assert led.lock_state().reason == "r0"
    led.append(LedgerRow(event="unlock", ts=T1, reason="extended"))
    assert led.lock_state() is None
    led.append(
        LedgerRow(
            event="final",
            ts=T2,
            rule="best_sealed",
            slots=1,
            chosen=["S1"],
            table=[],
            metric="accuracy",
            params={},
            holdout_unseals=2,
            profile_sha256="p" * 64,
        )
    )
    led.append(LedgerRow(event="lock", ts=T2, reason="final"))
    assert led.latest_final().chosen == ["S1"] and led.lock_state().reason == "final"


def test_bad_row_is_located(tmp_path):
    path = tmp_path / "s.jsonl"
    append_ledger_row(path, LedgerRow(event="note", ts=T0, text="hi"))
    with path.open("a", encoding="utf-8") as f:
        f.write('{"event": "lock", "ts": "x"}\n')
    with pytest.raises(ValidationFailed, match="s.jsonl:2"):
        SubmissionLedger(path)
```

- [ ] **Step 7: 寫 `src/vcp/submit/ledger.py`**

```python
"""``submissions.jsonl``: every staging, upload, score and decision, appended and never edited."""

from __future__ import annotations

from pathlib import Path

from vcp.measure.ledger import read_rows
from vcp.submit.schema import LedgerRow


def append_ledger_row(path: Path, row: LedgerRow) -> None:
    """One JSON object per line; ``None`` fields are left out so a row carries only its event's
    fields (plan decision 10)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(row.model_dump_json(exclude_none=True) + "\n")


class SubmissionLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[LedgerRow] = read_rows(path, LedgerRow)

    def append(self, row: LedgerRow) -> None:
        append_ledger_row(self.path, row)
        self.rows.append(row)

    def of(self, event: str, submission_id: str | None = None) -> list[LedgerRow]:
        return [
            r
            for r in self.rows
            if r.event == event and (submission_id is None or r.submission_id == submission_id)
        ]

    def ids(self) -> list[str]:
        """Staged submission ids in ledger order."""
        return [r.submission_id for r in self.of("staged") if r.submission_id]

    def staged(self, submission_id: str) -> LedgerRow | None:
        rows = self.of("staged", submission_id)
        return rows[0] if rows else None

    def uploads(self, submission_id: str) -> list[LedgerRow]:
        return self.of("uploaded", submission_id)

    def latest_score(self, submission_id: str) -> LedgerRow | None:
        rows = self.of("scored", submission_id)
        return rows[-1] if rows else None

    def arrivals(self) -> list[LedgerRow]:
        """Every upload the platform saw -- ours (``uploaded``) and others' (``foreign``) -- in
        platform-time order. Stamps share one format, so string order is time order; ties keep
        ledger order."""
        rows = [r for r in self.rows if r.event in ("uploaded", "foreign")]
        return sorted(rows, key=lambda r: r.at or "")

    def last_uploaded(self) -> LedgerRow | None:
        arrivals = self.arrivals()
        return arrivals[-1] if arrivals else None

    def foreign_refs(self) -> set[str]:
        return {r.platform_ref for r in self.of("foreign") if r.platform_ref}

    def lock_state(self) -> LedgerRow | None:
        """The lock row in force, or None: the newest of lock / unlock decides."""
        state: LedgerRow | None = None
        for r in self.rows:
            if r.event == "lock":
                state = r
            elif r.event == "unlock":
                state = None
        return state

    def latest_final(self) -> LedgerRow | None:
        rows = self.of("final")
        return rows[-1] if rows else None
```

- [ ] **Step 8: 跑兩個測試檔、ruff、commit**

Run: `uv run pytest tests/unit/submit -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add pyproject.toml uv.lock src/vcp/core/paths.py src/vcp/submit/__init__.py src/vcp/submit/schema.py src/vcp/submit/ledger.py tests/unit/submit/__init__.py tests/unit/submit/test_schema.py tests/unit/submit/test_ledger.py
git commit -m "feat(submit): 提交層資料模型（平台設定檔、候選快照、台帳事件）、只增台帳、submit 路徑、tzdata 依賴" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: 時間視窗與平台時間換算

**Files:**
- Create: `src/vcp/submit/timewin.py`
- Test: `tests/unit/submit/test_timewin.py`

**Interfaces:**
- Consumes: `vcp.submit.schema.Quota`、`vcp.core.errors.ValidationFailed`。
- Produces: `Window(start, end)`（UTC aware datetime）、`window_for(at: datetime, quota: Quota) -> Window`、`parse_at(text: str, tz_name: str) -> datetime`（UTC aware）、`local_text(at: datetime, tz_name: str) -> str`、`used_in(window: Window, ats: Iterable[datetime]) -> int`、`AT_FORMATS`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/submit/test_timewin.py`

```python
from datetime import UTC, datetime, timedelta

import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.time import parse_stamp
from vcp.submit.schema import Quota
from vcp.submit.timewin import local_text, parse_at, used_in, window_for

TPE = Quota(per_day=3, day_tz="Asia/Taipei")


def test_window_taipei_midnight():
    w = window_for(parse_stamp("2026-08-31T15:59:00Z"), TPE)
    assert w.start == datetime(2026, 8, 30, 16, 0, tzinfo=UTC)
    assert w.end == datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
    nxt = window_for(parse_stamp("2026-08-31T16:00:00Z"), TPE)
    assert nxt.start == w.end and nxt.end == w.end + timedelta(days=1)


def test_window_with_noon_day_start():
    q = Quota(per_day=3, day_tz="Asia/Taipei", day_start="12:00")
    w = window_for(parse_stamp("2026-08-31T03:00:00Z"), q)  # 11:00 Taipei -> previous noon
    assert w.start == datetime(2026, 8, 30, 4, 0, tzinfo=UTC)
    assert w.end == datetime(2026, 8, 31, 4, 0, tzinfo=UTC)


def test_window_across_dst_is_23_hours():
    ny = Quota(per_day=5, day_tz="America/New_York")
    w = window_for(parse_stamp("2026-03-08T12:00:00Z"), ny)
    assert w.start == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    assert w.end == datetime(2026, 3, 9, 4, 0, tzinfo=UTC)


def test_parse_at_formats_and_zones():
    at = parse_at("2026-08-31 21:28", "America/New_York")
    assert at == datetime(2026, 9, 1, 1, 28, tzinfo=UTC)
    assert parse_at("2026-08-31 21:28:30", "UTC") == datetime(2026, 8, 31, 21, 28, 30, tzinfo=UTC)
    with pytest.raises(ValidationFailed, match="--at must be"):
        parse_at("31/08/2026 21:28", "UTC")
    assert local_text(at, "Asia/Taipei") == "2026-09-01 09:28 CST"


def test_used_in_counts_half_open_window():
    w = window_for(parse_stamp("2026-08-31T15:59:00Z"), TPE)
    ats = [w.start, w.end - timedelta(seconds=1), w.end, w.start - timedelta(seconds=1)]
    assert used_in(w, ats) == 2
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/submit/test_timewin.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: vcp.submit.timewin`）

- [ ] **Step 3: 寫 `src/vcp/submit/timewin.py`**

```python
"""Quota windows and platform-time conversion (spec 6.5).

Postmortem error #1 was four timezone incidents; every conversion here goes through zoneinfo
and the clock itself is only ever read through ``vcp.core.time``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from vcp.core.errors import ValidationFailed
from vcp.submit.schema import Quota

AT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class Window:
    """Half-open ``[start, end)`` in UTC."""

    start: datetime
    end: datetime


def window_for(at: datetime, quota: Quota) -> Window:
    """The platform day containing ``at``: from ``day_start`` wall time in ``day_tz`` to the
    same wall time the next day. Wall-clock arithmetic, so a DST day is 23 or 25 hours."""
    tz = ZoneInfo(quota.day_tz)
    local = at.astimezone(tz)
    hh, mm = (int(x) for x in quota.day_start.split(":"))
    start = datetime(local.year, local.month, local.day, hh, mm, tzinfo=tz)
    if local < start:
        prev = local - timedelta(days=1)
        start = datetime(prev.year, prev.month, prev.day, hh, mm, tzinfo=tz)
    nxt = start + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, hh, mm, tzinfo=tz)
    return Window(start.astimezone(UTC), end.astimezone(UTC))


def parse_at(text: str, tz_name: str) -> datetime:
    """``YYYY-MM-DD HH:MM[:SS]`` read as wall time in ``tz_name`` -> UTC aware datetime."""
    for fmt in AT_FORMATS:
        try:
            naive = datetime.strptime(text.strip(), fmt)  # noqa: DTZ007 - zone attached below
        except ValueError:
            continue
        return naive.replace(tzinfo=ZoneInfo(tz_name)).astimezone(UTC)
    raise ValidationFailed(f"--at must be 'YYYY-MM-DD HH:MM[:SS]', got {text!r}")


def local_text(at: datetime, tz_name: str) -> str:
    return at.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z")


def used_in(window: Window, ats: Iterable[datetime]) -> int:
    return sum(1 for a in ats if window.start <= a < window.end)
```

如果 ruff 對 `datetime.strptime` 沒有 DTZ 規則，拿掉 `# noqa` 註解（ruff 會報 unused noqa）。TID251 只擋 `datetime.now` / `utcnow` / `today` / `time.time`，`strptime` 與 `datetime(...)` 建構子不在其中。

- [ ] **Step 4: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/submit/test_timewin.py -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/submit/timewin.py tests/unit/submit/test_timewin.py
git commit -m "feat(submit): 配額視窗（day_start @ day_tz 的牆鐘一天）與平台時間換算" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---
### Task 3: `vcp eval ingest --weights / --config`（量測層一處改動）

**Files:**
- Modify: `src/vcp/measure/ingest.py:32-50`（`IngestSpec`）、`src/vcp/measure/ingest.py:84-141`（`_run_card`）
- Modify: `src/vcp/cli_eval.py:80-160`（`ingest_cmd`）
- Test: `tests/unit/measure/test_ingest_identity.py`

**Interfaces:**
- Consumes: `vcp.core.hashing.sha256_file`、`RunSource`。
- Produces: `IngestSpec.weights: Path | None`、`IngestSpec.config: Path | None`；ingest 後 `run.yaml` 的 `source.weights_hash` / `source.config_hash` 為檔案 sha256；CLI `--weights PATH`、`--config PATH`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/measure/test_ingest_identity.py`

```python
"""Plan 6 decision 1: ingest can give a run its weights / config identity (test-side runs are
only ever ingested, and the submission layer pairs runs by these hashes)."""

import pytest
from typer.testing import CliRunner

from helpers import cls_samples, make_card, perfect_predictions, write_images
from vcp.cli import app
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.predictions import write_predictions
from vcp.measure.runs import load_run

runner = CliRunner()


def _seed(roots, tmp_path):
    name = "idt"
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = cls_samples(40, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("cls", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    weights = tmp_path / "best.pt"
    weights.write_bytes(b"weights")
    return ds, plan, weights


def _spec(roots, tmp_path, ds, plan, subset, **over):
    src = tmp_path / f"{subset}.jsonl"
    write_predictions(src, perfect_predictions(ds.subset(subset, plan), ds.card))
    return IngestSpec(
        run_id="r1",
        dataset=ds.card.name,
        plan_id=plan.plan_id,
        subset=subset,
        format="jsonl",
        src=src,
        trained_on=["train"],
        data_root=roots.data,
        configs_root=roots.configs,
        **over,
    )


def test_weights_and_config_hashes_land_on_the_run_card(roots, tmp_path):
    ds, plan, weights = _seed(roots, tmp_path)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("lr: 0.1\n", encoding="utf-8")
    ingest(_spec(roots, tmp_path, ds, plan, "valA", weights=weights, config=cfg))
    card = load_run(roots.data, "r1")
    assert card.source.weights_hash == sha256_file(weights)
    assert card.source.config_hash == sha256_file(cfg)


def test_second_ingest_may_fill_but_not_change_identity(roots, tmp_path):
    ds, plan, weights = _seed(roots, tmp_path)
    ingest(_spec(roots, tmp_path, ds, plan, "valA"))
    assert load_run(roots.data, "r1").source.weights_hash is None
    ingest(_spec(roots, tmp_path, ds, plan, "valB", weights=weights))
    assert load_run(roots.data, "r1").source.weights_hash == sha256_file(weights)
    other = tmp_path / "other.pt"
    other.write_bytes(b"other")
    with pytest.raises(ValidationFailed, match="already declares weights_hash"):
        ingest(_spec(roots, tmp_path, ds, plan, "valB", weights=other, replace=True))
    assert load_run(roots.data, "r1").source.weights_hash == sha256_file(weights)


def test_missing_weights_file_writes_nothing(roots, tmp_path):
    ds, plan, weights = _seed(roots, tmp_path)
    with pytest.raises(ValidationFailed, match="weights file not found"):
        ingest(_spec(roots, tmp_path, ds, plan, "valA", weights=tmp_path / "nope.pt"))
    assert not (roots.data / "runs" / "r1").exists()


def test_cli_passes_weights_through(roots, tmp_path):
    ds, plan, weights = _seed(roots, tmp_path)
    src = tmp_path / "valA.jsonl"
    write_predictions(src, perfect_predictions(ds.subset("valA", plan), ds.card))
    args = [
        "eval",
        "ingest",
        "--run",
        "r1",
        "--dataset",
        "idt",
        "--plan",
        "fixed-v1",
        "--subset",
        "valA",
        "--format",
        "jsonl",
        "--src",
        str(src),
        "--trained-on",
        "train",
        "--weights",
        str(weights),
    ]
    r = runner.invoke(app, args)
    assert r.exit_code == 0, r.output
    assert load_run(roots.data, "r1").source.weights_hash == sha256_file(weights)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/measure/test_ingest_identity.py -o addopts="" -q`
Expected: FAIL（`IngestSpec` 不認得 `weights`）

- [ ] **Step 3: 改 `src/vcp/measure/ingest.py`**

`IngestSpec` 在 `notes: str = ""` 之後加兩個欄位：

```python
    # Plan 6 decision 1: the files these predictions came from. Their sha256 becomes the run's
    # weights_hash / config_hash -- a test-side run has no other way to acquire an identity.
    weights: Path | None = None
    config: Path | None = None
```

在 `_run_card` 之前加兩個 helper：

```python
def _file_sha(path: Path | None, what: str) -> str | None:
    if path is None:
        return None
    if not path.is_file():
        raise ValidationFailed(f"{what} file not found: {path}")
    return sha256_file(path)


def _with_identity(card: RunCard, weights_sha: str | None, config_sha: str | None) -> RunCard:
    """Fill run.yaml's weights_hash / config_hash from files given at ingest. An empty field may
    be filled later; a filled one must agree, exactly like --framework / --notes."""
    source = card.source
    for name, sha in (("weights_hash", weights_sha), ("config_hash", config_sha)):
        if sha is None:
            continue
        current = getattr(source, name)
        if current is not None and current != sha:
            raise ValidationFailed(
                f"run {card.run_id!r} already declares {name}={current[:12]}; "
                f"the given file hashes to {sha[:12]}"
            )
        source = source.model_copy(update={name: sha})
    return card.model_copy(update={"source": source})
```

`_run_card` 開頭（`if (run_dir(...) / "run.yaml").is_file():` 之前）加：

```python
    weights_sha = _file_sha(spec.weights, "weights")
    config_sha = _file_sha(spec.config, "config")
```

既有 run 分支最後一行 `return card, False` 改為 `return _with_identity(card, weights_sha, config_sha), False`；新卡的 `RunSource(...)` 加 `weights_hash=weights_sha, config_hash=config_sha,`。`sha256_file` 已在檔案裡 import（`from vcp.core.hashing import sha256_file`；若沒有就加）。

- [ ] **Step 4: 改 `src/vcp/cli_eval.py` 的 `ingest_cmd`**

在 `notes: Annotated[str, typer.Option("--notes")] = "",` 之後加：

```python
weights: Annotated[
    Path | None,
    typer.Option("--weights", help="weights file the predictions came from (sha256 -> run)"),
] = None,
config: Annotated[
    Path | None, typer.Option("--config", help="model config file (sha256 -> run)")
] = None,
```

`IngestSpec(...)` 呼叫加 `weights=weights, config=config,`。

- [ ] **Step 5: 跑測試（含既有 ingest 測試）、ruff、commit**

Run: `uv run pytest tests/unit/measure/test_ingest_identity.py tests/unit/measure/test_ingest.py tests/unit/test_cli_eval.py -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/measure/ingest.py src/vcp/cli_eval.py tests/unit/measure/test_ingest_identity.py
git commit -m "feat(eval): ingest --weights / --config 把檔案 sha256 寫進 run 的 weights_hash / config_hash（test 側 run 的身分來源）" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: 平台設定檔、`vcp submit init`、共用測試夾具

**Files:**
- Create: `src/vcp/submit/profile.py`、`src/vcp/cli_submit.py`、`tests/unit/submit/conftest.py`
- Modify: `src/vcp/cli.py:20-45`（import 與 `add_typer`）
- Test: `tests/unit/submit/test_profile.py`、`tests/unit/test_cli_submit.py`

**Interfaces:**
- Consumes: `PlatformProfile` / `Quota`（Task 1）、`IngestSpec.weights`（Task 3）、`vcp.data.split.SplitPlan` / `SubsetSpec` / `load_plan` / `save_plan`、`vcp.core.config.load_yaml_model` / `dump_yaml_model`、`vcp.measure.metrics.get_metric` / `effective_params`。
- Produces: `load_profile(paths) -> tuple[PlatformProfile, str]`、`ensure_test_plan(paths, dataset, profile) -> bool`、`init_profile(profile, *, data_root, configs_root) -> InitResult(path, plan_created)`；`vcp.cli_submit.submit_app`、`DatasetOpt`、`IdOpt`、`PluginOpt`；測試模組 `tests/submit_fixtures.py`（`EVAL`、`TEST`、`STAMP`、`random_scores`、`make_pair(roots, tmp_path)`、`ingest_run`、`seed_eval_runs`、`seed_test_runs`、`seed_judgements`）與夾具 `pair`（`tests/unit/submit/conftest.py`）。

- [ ] **Step 1: 寫夾具模組** `tests/submit_fixtures.py`（`tests/` 已在 sys.path：`from helpers import …` 就是這樣用的）與 `tests/unit/submit/conftest.py`

`tests/unit/submit/conftest.py`：

```python
import pytest
from submit_fixtures import make_pair


@pytest.fixture
def pair(roots, tmp_path):
    return make_pair(roots, tmp_path)
```

`tests/submit_fixtures.py`：

```python
"""Shared by the submission-layer tests: an eval dataset with a sealed holdout, an unlabeled
test dataset, and helpers that ingest runs on either side and judge claims. A plain module (not
a conftest) so tests outside tests/unit/submit can build the same fixture."""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace

from helpers import (
    CATS,
    cls_samples,
    make_card,
    noisy_predictions,
    perfect_predictions,
    write_images,
)
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.judge import JudgeSpec, judge_prereg
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.measure.predictions import write_predictions
from vcp.measure.prereg import create_prereg
from vcp.measure.report import READINGS_LEDGER
from vcp.measure.schema import Prediction, PreRegistration

EVAL = "beach"
TEST = "beach-test"
STAMP = "2026-09-05T00:00:00.000Z"


def random_scores(samples, *, seed: int) -> list[Prediction]:
    """Class-probability rows for unlabeled samples (a test set has no gold to copy)."""
    rng = random.Random(seed)
    names = [c.name for c in CATS]
    out = []
    for s in samples:
        raw = [rng.random() for _ in names]
        total = sum(raw)
        scores = {n: v / total for n, v in zip(names, raw, strict=True)}
        out.append(Prediction(sample_id=s.sample_id, scores=scores))
    return out


def make_pair(roots, tmp_path) -> SimpleNamespace:
    """Eval dataset ``beach`` (cls, 200 gold samples, plan fixed-v1 with sealed ``holdout``) and
    test dataset ``beach-test`` (cls, 50 unlabeled samples); two weights files. No profile yet."""
    eval_paths = DatasetPaths.resolve(EVAL, data_root=roots.data, configs_root=roots.configs)
    eval_samples = cls_samples(200, seed=1)
    write_images(roots.data / "raw" / EVAL, eval_samples)
    eval_ds = Dataset.from_parts(
        make_card("cls", name=EVAL, image_root=f"raw/{EVAL}"), eval_samples
    )
    eval_ds.save(eval_paths)
    eval_plan = build_plan(
        eval_ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0
    )
    save_plan(eval_plan, eval_paths)
    test_paths = DatasetPaths.resolve(TEST, data_root=roots.data, configs_root=roots.configs)
    test_samples = cls_samples(50, seed=2, gold_frac=0.0)
    write_images(roots.data / "raw" / TEST, test_samples)
    test_ds = Dataset.from_parts(
        make_card("cls", name=TEST, image_root=f"raw/{TEST}"), test_samples
    )
    test_ds.save(test_paths)
    weights = {}
    for name in ("good", "bad"):
        p = tmp_path / f"{name}.pt"
        p.write_bytes(name.encode())
        weights[name] = p
    return SimpleNamespace(
        roots=roots,
        tmp=tmp_path,
        eval_ds=eval_ds,
        eval_plan=eval_plan,
        eval_paths=eval_paths,
        test_ds=test_ds,
        test_paths=test_paths,
        weights=weights,
    )


def ingest_run(
    pair,
    run_id: str,
    dataset: Dataset,
    plan_id: str,
    subset: str,
    preds: list[Prediction],
    *,
    weights: Path | None,
    trained_on: tuple[str, ...] = (),
    replace: bool = False,
) -> None:
    src = pair.tmp / f"{run_id}-{subset}.jsonl"
    write_predictions(src, preds)
    ingest(
        IngestSpec(
            run_id=run_id,
            dataset=dataset.card.name,
            plan_id=plan_id,
            subset=subset,
            format="jsonl",
            src=src,
            trained_on=list(trained_on),
            weights=weights,
            replace=replace,
            data_root=pair.roots.data,
            configs_root=pair.roots.configs,
        )
    )


def seed_eval_runs(pair) -> None:
    """Run ``good`` (perfect) and ``bad`` (noisy) on valA, valB and holdout, each with its own
    weights file, trained on ``train``."""
    ds, plan = pair.eval_ds, pair.eval_plan
    for subset in ("valA", "valB", "holdout"):
        samples = ds.subset(
            subset, plan, unseal=subset == "holdout", reason="fixture", paths=pair.eval_paths
        )
        ingest_run(
            pair,
            "good",
            ds,
            "fixed-v1",
            subset,
            perfect_predictions(samples, ds.card),
            weights=pair.weights["good"],
            trained_on=("train",),
        )
        ingest_run(
            pair,
            "bad",
            ds,
            "fixed-v1",
            subset,
            noisy_predictions(samples, ds.card, seed=3, flip=0.6),
            weights=pair.weights["bad"],
            trained_on=("train",),
        )


def seed_test_runs(pair, plan_id: str = "all-v1", subset: str = "test") -> None:
    """Test-side runs: ``good.test`` / ``bad.test`` carry their eval twin's weights;
    ``bad.mismatch`` carries good's weights under bad's name (an identity failure)."""
    ds = pair.test_ds
    samples = list(ds.samples)
    for run_id, seed, weights in (
        ("good.test", 10, "good"),
        ("bad.test", 11, "bad"),
        ("bad.mismatch", 12, "good"),
    ):
        ingest_run(
            pair,
            run_id,
            ds,
            plan_id,
            subset,
            random_scores(samples, seed=seed),
            weights=pair.weights[weights],
        )


def seed_judgements(pair) -> None:
    """Claims ``p-good`` (good beats bad -> PASS) and ``p-bad`` (bad beats good -> FAIL), both
    class model on accuracy over valA / valB. Both are registered before either candidate is
    measured, as the measurement layer demands."""
    readings = ReadingsLedger(pair.eval_paths.measure_dir / READINGS_LEDGER)
    for pid, cand, base in (("p-good", "good", "bad"), ("p-bad", "bad", "good")):
        create_prereg(
            pair.eval_paths,
            PreRegistration(
                prereg_id=pid,
                claim=f"{cand} beats {base}",
                component=cand,
                component_class="model",
                baseline_run=base,
                candidate_run=cand,
                metric="accuracy",
                subsets=["valA", "valB"],
                created_at=STAMP,
            ),
            readings,
        )
    for run in ("good", "bad"):
        measure_run(
            MeasureSpec(
                run_id=run,
                metrics=["accuracy"],
                subsets=["valA", "valB"],
                data_root=pair.roots.data,
                configs_root=pair.roots.configs,
            )
        )
    for pid in ("p-good", "p-bad"):
        judge_prereg(
            JudgeSpec(
                dataset=EVAL,
                prereg_id=pid,
                resamples=50,
                seed=0,
                data_root=pair.roots.data,
                configs_root=pair.roots.configs,
            )
        )
```

- [ ] **Step 2: 寫失敗的測試** `tests/unit/submit/test_profile.py`

```python
import pytest
from submit_fixtures import EVAL, TEST

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.split import load_plan
from vcp.submit.profile import init_profile, load_profile
from vcp.submit.schema import PlatformProfile, Quota

STAMP = "2026-09-05T00:00:00.000Z"


def _profile(**over) -> PlatformProfile:
    base = dict(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        quota=Quota(per_day=3, day_tz="Asia/Taipei"),
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


def test_init_writes_profile_and_single_subset_plan(pair):
    res = init_profile(_profile(), data_root=pair.roots.data, configs_root=pair.roots.configs)
    assert res.plan_created and res.path == pair.test_paths.submit_yaml
    plan = load_plan(pair.test_paths, "all-v1")
    assert [s.name for s in plan.subsets] == ["train", "test"]
    assert plan.ids_in("test") == {s.sample_id for s in pair.test_ds.samples}
    assert plan.ids_in("train") == set() and plan.params["eval_gold_only"] is False
    samples = pair.test_ds.subset("test", plan, paths=pair.test_paths)
    assert len(samples) == 50
    profile, sha = load_profile(pair.test_paths)
    assert profile == _profile() and len(sha) == 64


def test_init_refuses_second_run_and_bad_sealed(pair):
    init_profile(_profile(), data_root=pair.roots.data, configs_root=pair.roots.configs)
    with pytest.raises(ValidationFailed, match="exists"):
        init_profile(_profile(), data_root=pair.roots.data, configs_root=pair.roots.configs)
    pair.test_paths.submit_yaml.unlink()
    with pytest.raises(ValidationFailed, match="sealed_subset"):
        init_profile(
            _profile(sealed_subset="valA"),
            data_root=pair.roots.data,
            configs_root=pair.roots.configs,
        )
    with pytest.raises(ValidationFailed, match="sealed_subset"):
        init_profile(
            _profile(sealed_subset="nope"),
            data_root=pair.roots.data,
            configs_root=pair.roots.configs,
        )


def test_init_reuses_a_matching_plan(pair):
    init_profile(_profile(), data_root=pair.roots.data, configs_root=pair.roots.configs)
    pair.test_paths.submit_yaml.unlink()
    res = init_profile(_profile(), data_root=pair.roots.data, configs_root=pair.roots.configs)
    assert not res.plan_created


def test_load_profile_errors(pair):
    with pytest.raises(ValidationFailed, match="no_profile"):
        load_profile(pair.test_paths)
    init_profile(_profile(), data_root=pair.roots.data, configs_root=pair.roots.configs)
    other = DatasetPaths.resolve(
        "other", data_root=pair.roots.data, configs_root=pair.roots.configs
    )
    other.submit_yaml.parent.mkdir(parents=True)
    other.submit_yaml.write_bytes(pair.test_paths.submit_yaml.read_bytes())
    with pytest.raises(ValidationFailed, match="names dataset"):
        load_profile(other)
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/unit/submit/test_profile.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: vcp.submit.profile`）

- [ ] **Step 4: 寫 `src/vcp/submit/profile.py`**

```python
"""The platform profile (spec 4.1) and what ``vcp submit init`` sets up around it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.split import SplitPlan, SubsetSpec, load_plan, save_plan
from vcp.submit.schema import PlatformProfile

TEST_PLAN_STRATEGY = "all"


def load_profile(paths: DatasetPaths) -> tuple[PlatformProfile, str]:
    """The profile and the sha256 of the file it came from (ledger rows record it, so a rule
    changed in git after the fact is visible)."""
    path = paths.submit_yaml
    if not path.is_file():
        raise ValidationFailed(f"no_profile: {path} does not exist; run `vcp submit init` first")
    profile = load_yaml_model(path, PlatformProfile)
    if profile.dataset != paths.name:
        raise ValidationFailed(
            f"submit.yaml names dataset {profile.dataset!r}, not {paths.name!r}",
            location=str(path),
        )
    return profile, sha256_file(path)


def ensure_test_plan(paths: DatasetPaths, dataset: Dataset, profile: PlatformProfile) -> bool:
    """The single-subset plan every test-side run is ingested against; True when created.

    ``SplitPlan`` insists on exactly one train subset, so the plan carries an empty one (plan
    decision 7); ``eval_gold_only`` is off because a test set has no labels.
    """
    if paths.plan_json(profile.test_plan).is_file():
        plan = load_plan(paths, profile.test_plan)
        if plan.dataset_hash != dataset.card.samples_hash:
            raise ValidationFailed(
                f"plan {profile.test_plan!r} was built on another version of "
                f"{dataset.card.name!r}; choose a new test_plan"
            )
        names = {s.name for s in plan.subsets}
        if profile.test_subset not in names or plan.subset(profile.test_subset).role != "eval":
            raise ValidationFailed(
                f"plan {profile.test_plan!r} has no eval subset {profile.test_subset!r}"
            )
        return False
    try:
        subsets = [
            SubsetSpec(name="train", role="train", ratio=0.0),
            SubsetSpec(name=profile.test_subset, role="eval", ratio=1.0),
        ]
    except ValidationError as e:
        raise ValidationFailed(f"test_subset {profile.test_subset!r}: {e}") from e
    plan = SplitPlan(
        plan_id=profile.test_plan,
        dataset=dataset.card.name,
        dataset_hash=dataset.card.samples_hash,
        strategy=TEST_PLAN_STRATEGY,
        params={"eval_gold_only": False, "stratify_key": "none", "group_key": "auto", "seed": 0},
        subsets=subsets,
        assignment={s.sample_id: profile.test_subset for s in dataset.samples},
        created_at=stamp(),
    )
    save_plan(plan, paths)
    return True


@dataclass(frozen=True)
class InitResult:
    path: Path
    plan_created: bool


def init_profile(
    profile: PlatformProfile, *, data_root: Path | None, configs_root: Path | None
) -> InitResult:
    paths = DatasetPaths.resolve(profile.dataset, data_root=data_root, configs_root=configs_root)
    if paths.submit_yaml.exists():
        raise ValidationFailed(
            f"exists: {paths.submit_yaml}; edit it in git instead of re-running init"
        )
    eval_paths = DatasetPaths.resolve(
        profile.eval_dataset, data_root=data_root, configs_root=configs_root
    )
    eval_plan = load_plan(eval_paths, profile.plan_id)
    roles = {s.name: s.role for s in eval_plan.subsets}
    if roles.get(profile.sealed_subset) != "sealed":
        raise ValidationFailed(
            f"sealed_subset: {profile.sealed_subset!r} is not a sealed subset of plan "
            f"{profile.plan_id!r} (subsets: {roles})"
        )
    dataset = Dataset.load(profile.dataset, data_root=data_root, configs_root=configs_root)
    created = ensure_test_plan(paths, dataset, profile)
    dump_yaml_model(profile, paths.submit_yaml)
    return InitResult(paths.submit_yaml, created)
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/unit/submit/test_profile.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 6: 寫失敗的 CLI 測試** `tests/unit/test_cli_submit.py`

```python
"""``vcp submit`` through the CLI: VERDICT lines, exit codes, --json. Later tasks append to
this file."""

import json

import pytest
from submit_fixtures import make_pair
from typer.testing import CliRunner

from vcp.cli import app
from vcp.core.config import load_yaml_model
from vcp.core.paths import DatasetPaths
from vcp.submit.schema import PlatformProfile

runner = CliRunner()


@pytest.fixture
def pair(roots, tmp_path):
    return make_pair(roots, tmp_path)


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _json(r) -> dict:
    return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))


def _init_args(**over) -> list[str]:
    opts = {
        "--dataset": "beach-test",
        "--eval-dataset": "beach",
        "--plan": "fixed-v1",
        "--sealed": "holdout",
        "--platform": "manual",
        "--metric": "accuracy",
        "--writer": "scores_csv",
        "--quota": "3",
        "--day-tz": "Asia/Taipei",
        "--deadline": "2027-01-01T00:00:00Z",
    }
    opts.update(over)
    args = ["submit", "init"]
    for k, v in opts.items():
        if v is not None:
            args += [k, str(v)]
    return args


def test_init_ok_and_json(pair):
    r = runner.invoke(app, _init_args())
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "status=OK" in v and "plan_created=true" in v and "platform=manual" in v
    paths = DatasetPaths.resolve(
        "beach-test", data_root=pair.roots.data, configs_root=pair.roots.configs
    )
    profile = load_yaml_model(paths.submit_yaml, PlatformProfile)
    assert profile.board_rule == "last" and profile.quota.per_day == 3
    assert profile.metric == "accuracy" and profile.deadline == "2027-01-01T00:00:00Z"
    r = runner.invoke(app, [*_init_args(**{"--dataset": "beach-test"}), "--json"])
    assert r.exit_code == 1
    assert _json(r)["status"] == "FAIL" and "exists" in _json(r)["fields"]["reason"]


def test_init_defaults_board_rule_for_kaggle(pair):
    r = runner.invoke(
        app,
        _init_args(
            **{
                "--platform": "kaggle",
                "--competition": "c1",
                "--kaggle-command": "uv tool run kaggle",
            }
        ),
    )
    assert r.exit_code == 0, r.output
    paths = DatasetPaths.resolve(
        "beach-test", data_root=pair.roots.data, configs_root=pair.roots.configs
    )
    profile = load_yaml_model(paths.submit_yaml, PlatformProfile)
    assert profile.board_rule == "best"
    assert profile.kaggle_command == ["uv", "tool", "run", "kaggle"]


def test_init_failures(pair):
    r = runner.invoke(app, _init_args(**{"--day-tz": "Mars/Olympus"}))
    assert r.exit_code == 1 and "status=FAIL" in _verdict(r.output)
    r = runner.invoke(app, _init_args(**{"--metric": "nope"}))
    assert r.exit_code == 2 and "status=ABORT" in _verdict(r.output)
    r = runner.invoke(app, _init_args(**{"--platform": "kaggle"}))
    assert r.exit_code == 1 and "competition" in _verdict(r.output)
```

- [ ] **Step 7: 寫 `src/vcp/cli_submit.py`（init）並掛到 `vcp.cli`**

```python
"""``vcp submit``: submission governance commands. Every command ends with a VERDICT line."""

from __future__ import annotations

from typing import Annotated

import typer
from pydantic import ValidationError

from vcp.cli_common import (
    CmdResult,
    ConfigsRootOpt,
    DataRootOpt,
    JsonOpt,
    parse_opts,
    run_command,
)
from vcp.core.errors import ValidationFailed
from vcp.core.log import FieldValue
from vcp.core.time import stamp
from vcp.measure.metrics import effective_params, get_metric
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile, Quota

submit_app = typer.Typer(no_args_is_help=True, help="submission governance commands")

DatasetOpt = Annotated[str, typer.Option("--dataset", help="test dataset name")]
IdOpt = Annotated[str, typer.Option("--id", help="submission id (path-safe, under 32 chars)")]
PluginOpt = Annotated[
    list[str] | None, typer.Option("--plugin", help="python module to import (registers writers)")
]


@submit_app.command("init")
def init_cmd(
    dataset: DatasetOpt,
    eval_dataset: Annotated[str, typer.Option("--eval-dataset", help="eval-side dataset")],
    plan: Annotated[str, typer.Option("--plan", help="eval-side plan id (has the sealed subset)")],
    sealed: Annotated[str, typer.Option("--sealed", help="sealed subset `final` ranks on")],
    platform: Annotated[str, typer.Option("--platform", help="manual | kaggle")],
    metric: Annotated[str, typer.Option("--metric", help="metric of the sealed reading")],
    competition: Annotated[str | None, typer.Option("--competition")] = None,
    kind: Annotated[str, typer.Option("--kind", help="file | kernel")] = "file",
    board_rule: Annotated[
        str | None, typer.Option("--board-rule", help="last | best (default by platform)")
    ] = None,
    slots: Annotated[int, typer.Option("--slots", help="final picks (Kaggle allows 2)")] = 1,
    quota: Annotated[int | None, typer.Option("--quota", help="uploads per platform day")] = None,
    day_tz: Annotated[str, typer.Option("--day-tz", help="zone of the platform day")] = "UTC",
    day_start: Annotated[str, typer.Option("--day-start", help="HH:MM wall time")] = "00:00",
    display_tz: Annotated[
        str | None, typer.Option("--display-tz", help="zone `record --at` is read in")
    ] = None,
    deadline: Annotated[
        str | None, typer.Option("--deadline", help="UTC stamp, e.g. 2026-10-22T23:59:00Z")
    ] = None,
    params: Annotated[
        list[str] | None, typer.Option("--params", help="metric param key=value (repeatable)")
    ] = None,
    writer: Annotated[str | None, typer.Option("--writer", help="registered writer")] = None,
    writer_opt: Annotated[
        list[str] | None, typer.Option("--writer-opt", help="writer option key=value")
    ] = None,
    kaggle_command: Annotated[
        str, typer.Option("--kaggle-command", help="how to run the kaggle CLI")
    ] = "kaggle",
    test_plan: Annotated[str, typer.Option("--test-plan")] = "all-v1",
    test_subset: Annotated[str, typer.Option("--test-subset")] = "test",
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Write submit.yaml and the test dataset's single-subset plan."""

    def fn() -> CmdResult:
        metric_params = effective_params(get_metric(metric), parse_opts(params, "--params"))
        rule = board_rule or ("best" if platform == "kaggle" else "last")
        try:
            profile = PlatformProfile(
                dataset=dataset,
                eval_dataset=eval_dataset,
                plan_id=plan,
                sealed_subset=sealed,
                test_plan=test_plan,
                test_subset=test_subset,
                platform=platform,  # type: ignore[arg-type]
                competition=competition,
                submission_kind=kind,  # type: ignore[arg-type]
                board_rule=rule,  # type: ignore[arg-type]
                final_slots=slots,
                quota=None
                if quota is None
                else Quota(per_day=quota, day_tz=day_tz, day_start=day_start),
                display_tz=display_tz,
                deadline=deadline,
                metric=metric,
                metric_params=metric_params,
                writer=writer,
                writer_opts=parse_opts(writer_opt, "--writer-opt"),
                kaggle_command=kaggle_command.split(),
                created_at=stamp(),
            )
        except ValidationError as e:
            raise ValidationFailed(str(e), location="vcp submit init") from e
        res = init_profile(profile, data_root=data_root, configs_root=configs_root)
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "platform": platform,
            "profile": str(res.path),
            "plan": test_plan,
            "plan_created": res.plan_created,
        }
        payload = {"profile": profile.model_dump(mode="json"), "path": str(res.path)}
        return "OK", fields, payload, [f"profile written to {res.path}"]

    run_command("submit.init", json_mode, data_root, fn)
```

`src/vcp/cli.py`：在 `from vcp.cli_fuse import fuse_app` 之後加 `from vcp.cli_submit import submit_app`，在 `app.add_typer(fuse_app, name="fuse")` 之後加 `app.add_typer(submit_app, name="submit")`（保持字母序：eval、fuse、submit、train）。

- [ ] **Step 8: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py tests/unit/test_cli.py -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/submit/profile.py src/vcp/cli_submit.py src/vcp/cli.py tests/submit_fixtures.py tests/unit/submit/conftest.py tests/unit/submit/test_profile.py tests/unit/test_cli_submit.py
git commit -m "feat(submit): 平台設定檔載入與 vcp submit init（建 test dataset 的單子集 plan）；submit 測試夾具" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: writer 登記表與 `scores_csv`

**Files:**
- Create: `src/vcp/submit/writers/__init__.py`、`src/vcp/submit/writers/base.py`、`src/vcp/submit/writers/scores_csv.py`
- Test: `tests/unit/submit/test_writers_base.py`、`tests/unit/submit/test_writer_scores_csv.py`

**Interfaces:**
- Consumes: `vcp.measure.schema.Prediction` / `payload_field`、`vcp.measure.predictions.predictions_by_id`、`vcp.data.dataset.Dataset`、`vcp.core.log.FieldValue`。
- Produces: `WriteContext(dataset, samples, options, out)`、`WriteResult(rows, samples, missing, fields)`、`Writer` protocol（`name`、`version`、`payloads`、`file_name`、`options`、`write(preds, ctx)`）、`WRITERS`、`register_writer`、`get_writer`、`writer_for(name, task)`、`output_ids(samples, options) -> dict[str, str]`、`check_options(options, known)`、`parse_columns(value)`、`fmt_float`、`is_true`、`sorted_samples`；`ScoresCsvWriter`（`file_name="submission.csv"`，選項 `id_field` / `id_col` / `columns` / `allow_missing`）。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/submit/test_writers_base.py`

```python
import pytest

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.schema import Sample, View
from vcp.submit.writers import WRITERS, get_writer, register_writer, writer_for
from vcp.submit.writers.base import check_options, fmt_float, is_true, output_ids, parse_columns


def _samples():
    return [
        Sample(
            sample_id=f"s{i}",
            views=[View(path=f"dir/img_{i}.png")],
            label_source="none",
            meta={"image_id": str(100 + i)},
        )
        for i in range(3)
    ]


def test_output_ids_modes():
    samples = _samples()
    assert output_ids(samples, {}) == {"s0": "s0", "s1": "s1", "s2": "s2"}
    assert output_ids(samples, {"id_field": "view_path"})["s1"] == "dir/img_1.png"
    assert output_ids(samples, {"id_field": "view_stem"})["s1"] == "img_1"
    assert output_ids(samples, {"id_field": "meta.image_id"})["s2"] == "102"


def test_output_ids_failures():
    samples = _samples()
    with pytest.raises(ValidationFailed, match="has no meta.nope"):
        output_ids(samples, {"id_field": "meta.nope"})
    with pytest.raises(ValidationFailed, match="option=id_field"):
        output_ids(samples, {"id_field": "hash"})
    dup = [s.model_copy(update={"meta": {"k": "same"}}) for s in samples]
    with pytest.raises(ValidationFailed, match="duplicate_id") as ei:
        output_ids(dup, {"id_field": "meta.k"})
    assert ei.value.fields == {"id": "same"}


def test_options_helpers():
    check_options({"id_field": "x"}, frozenset({"id_field"}))
    with pytest.raises(ValidationFailed, match="option=colour") as ei:
        check_options({"colour": "red"}, frozenset({"id_field"}))
    assert ei.value.fields == {"option": "colour"}
    assert parse_columns("cat=Cat, dog=Dog") == {"cat": "Cat", "dog": "Dog"}
    assert parse_columns(None) == {}
    with pytest.raises(ValidationFailed, match="option=columns"):
        parse_columns("cat")
    assert fmt_float(0.1) == "0.1" and fmt_float(1) == "1.0"
    assert is_true({"allow_missing": "True"}, "allow_missing")
    assert not is_true({}, "allow_missing")


def test_registry_and_task_check():
    assert "scores_csv" in WRITERS
    with pytest.raises(RegistryError, match="already registered"):
        register_writer(get_writer("scores_csv"))
    with pytest.raises(RegistryError, match="unknown writer"):
        get_writer("nope")
    assert writer_for("scores_csv", "cls").name == "scores_csv"
    with pytest.raises(ValidationFailed, match="predicts 'boxes'"):
        writer_for("scores_csv", "det")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/submit/test_writers_base.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: vcp.submit.writers`）

- [ ] **Step 3: 寫 `src/vcp/submit/writers/base.py`**

```python
"""Writer contract and registry (spec 9): canonical predictions -> a platform's submission file.

Determinism is the contract: the same predictions and options must produce the same bytes,
because ``vcp submit verify`` re-renders the file and compares hashes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.core.log import FieldValue
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample
from vcp.measure.schema import Prediction, payload_field

_TRUE = {"1", "true", "yes"}
ID_FIELDS = ("sample_id", "view_path", "view_stem")


@dataclass(frozen=True)
class WriteContext:
    dataset: Dataset
    samples: list[Sample]
    options: dict[str, str]
    out: Path


@dataclass(frozen=True)
class WriteResult:
    rows: int
    samples: int
    missing: list[str]
    fields: dict[str, FieldValue] = field(default_factory=dict)


class Writer(Protocol):
    name: str
    version: str
    payloads: frozenset[str]
    file_name: str
    options: frozenset[str]

    def write(self, preds: list[Prediction], ctx: WriteContext) -> WriteResult: ...


WRITERS: dict[str, Writer] = {}


def register_writer(writer: Writer) -> None:
    if writer.name in WRITERS:
        raise RegistryError(f"writer {writer.name!r} already registered")
    WRITERS[writer.name] = writer


def get_writer(name: str) -> Writer:
    try:
        return WRITERS[name]
    except KeyError:
        raise RegistryError(f"unknown writer {name!r}; known: {sorted(WRITERS)}") from None


def writer_for(name: str, task: str) -> Writer:
    """The registered writer, checked against what this task's predictions carry."""
    writer = get_writer(name)
    payload = payload_field(task)
    if payload not in writer.payloads:
        raise ValidationFailed(
            f"writer {name!r} serves {sorted(writer.payloads)}, but task {task!r} "
            f"predicts {payload!r}"
        )
    return writer


def sorted_samples(samples: list[Sample]) -> list[Sample]:
    return sorted(samples, key=lambda s: s.sample_id)


def output_ids(samples: list[Sample], options: dict[str, str]) -> dict[str, str]:
    """sample_id -> the id written to the file (spec 9.2). Duplicates are refused: two rows
    under one id is a file the platform scores wrongly or rejects."""
    which = options.get("id_field", "sample_id")
    out: dict[str, str] = {}
    for s in samples:
        if which == "sample_id":
            value = s.sample_id
        elif which == "view_path":
            value = s.views[0].path
        elif which == "view_stem":
            value = Path(s.views[0].path).stem
        elif which.startswith("meta."):
            key = which[len("meta.") :]
            raw = s.meta.get(key)
            if raw is None:
                raise ValidationFailed(
                    f"sample {s.sample_id!r} has no meta.{key}", fields={"sample": s.sample_id}
                )
            value = str(raw)
        else:
            raise ValidationFailed(
                f"option=id_field: {which!r} is not one of {ID_FIELDS} or meta.<key>",
                fields={"option": "id_field"},
            )
        out[s.sample_id] = value
    seen: dict[str, str] = {}
    for sid, value in out.items():
        if value in seen:
            raise ValidationFailed(
                f"duplicate_id: {value!r} for samples {seen[value]!r} and {sid!r}",
                fields={"id": value},
            )
        seen[value] = sid
    return out


def check_options(options: dict[str, str], known: frozenset[str]) -> None:
    unknown = sorted(set(options) - known)
    if unknown:
        raise ValidationFailed(
            f"option={unknown[0]}: unknown writer option; known: {sorted(known)}",
            fields={"option": unknown[0]},
        )


def parse_columns(value: str | None) -> dict[str, str]:
    """``a=b,c=d`` -> {"a": "b", "c": "d"}: output column renames."""
    out: dict[str, str] = {}
    for item in (value or "").split(","):
        if not item.strip():
            continue
        src, sep, dst = item.partition("=")
        if not sep or not src.strip() or not dst.strip():
            raise ValidationFailed(
                f"option=columns: expects a=b,c=d pairs, got {item!r}", fields={"option": "columns"}
            )
        out[src.strip()] = dst.strip()
    return out


def fmt_float(v: float) -> str:
    return repr(float(v))


def is_true(options: dict[str, str], key: str) -> bool:
    return options.get(key, "false").lower() in _TRUE
```

- [ ] **Step 4: 寫失敗的測試** `tests/unit/submit/test_writer_scores_csv.py`

```python
import pytest

from helpers import REG_CATS, cls_samples, make_card, regression_samples
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.measure.converters import ConvertContext, ScoresCsvConverter
from vcp.measure.schema import Prediction
from vcp.submit.writers import get_writer
from vcp.submit.writers.base import WriteContext


def _cls(n=4):
    ds = Dataset.from_parts(make_card("cls", name="t"), cls_samples(n, gold_frac=0.0))
    preds = [
        Prediction(sample_id=s.sample_id, scores={"cat": 0.5, "dog": 0.25, "bird": 0.25})
        for s in ds.samples
    ]
    return ds, preds


def _ctx(ds, tmp_path, **options):
    return WriteContext(ds, list(ds.samples), options, tmp_path / "submission.csv")


def test_header_rows_and_determinism(tmp_path):
    ds, preds = _cls()
    w = get_writer("scores_csv")
    res = w.write(list(reversed(preds)), _ctx(ds, tmp_path))
    text = (tmp_path / "submission.csv").read_bytes()
    assert res.rows == 4 and res.samples == 4 and res.missing == []
    assert text.startswith(b"id,cat,dog,bird\ns0000,0.5,0.25,0.25\n") and b"\r" not in text
    again = tmp_path / "again.csv"
    w.write(preds, WriteContext(ds, list(ds.samples), {}, again))
    assert again.read_bytes() == text


def test_missing_rows_fail_unless_allowed(tmp_path):
    ds, preds = _cls()
    w = get_writer("scores_csv")
    with pytest.raises(ValidationFailed, match="missing: 1 samples") as ei:
        w.write(preds[:-1], _ctx(ds, tmp_path))
    assert ei.value.fields == {"missing": 1}
    res = w.write(preds[:-1], _ctx(ds, tmp_path, allow_missing="true"))
    assert res.rows == 3 and res.missing == ["s0003"]


def test_rename_and_id_options(tmp_path):
    ds, preds = _cls(2)
    w = get_writer("scores_csv")
    w.write(preds, _ctx(ds, tmp_path, id_col="image", columns="cat=Cat", id_field="view_stem"))
    lines = (tmp_path / "submission.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "image,Cat,dog,bird" and lines[1].startswith("s0000,")
    with pytest.raises(ValidationFailed, match="missing_key"):
        bad = [Prediction(sample_id="s0000", scores={"cat": 1.0}), preds[1]]
        w.write(bad, _ctx(ds, tmp_path))
    with pytest.raises(ValidationFailed, match="option=colour"):
        w.write(preds, _ctx(ds, tmp_path, colour="red"))


def test_round_trip_through_the_converter(tmp_path):
    ds, preds = _cls(3)
    get_writer("scores_csv").write(preds, _ctx(ds, tmp_path))
    ctx = ConvertContext(ds, {s.sample_id for s in ds.samples})
    back = ScoresCsvConverter().convert(tmp_path / "submission.csv", ctx)
    assert sorted(back, key=lambda p: p.sample_id) == preds


def test_regression_targets(tmp_path):
    card = make_card("regression", name="r", categories=REG_CATS)
    ds = Dataset.from_parts(card, regression_samples(2))
    preds = [Prediction(sample_id=s.sample_id, targets={"age": 30.5}) for s in ds.samples]
    get_writer("scores_csv").write(preds, _ctx(ds, tmp_path))
    lines = (tmp_path / "submission.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "id,age" and lines[1] == "s0000,30.5"
```

寫入與樣本有無標籤無關，所以 regression 直接用 gold 樣本。

- [ ] **Step 5: 寫 `src/vcp/submit/writers/scores_csv.py` 與 `__init__.py`**

```python
"""One row per sample: an id column and one column per category (or regression target). The
mirror of the measurement layer's ``scores_csv`` converter (spec 9.3)."""

from __future__ import annotations

import csv

from vcp.core.errors import ValidationFailed
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import Prediction, payload_field
from vcp.submit.writers.base import (
    WriteContext,
    WriteResult,
    check_options,
    fmt_float,
    is_true,
    output_ids,
    parse_columns,
    sorted_samples,
)


class ScoresCsvWriter:
    name = "scores_csv"
    version = "1"
    payloads = frozenset({"scores", "targets"})
    file_name = "submission.csv"
    options = frozenset({"id_field", "id_col", "columns", "allow_missing"})

    def write(self, preds: list[Prediction], ctx: WriteContext) -> WriteResult:
        check_options(ctx.options, self.options)
        payload = payload_field(ctx.dataset.card.task)
        ids = output_ids(ctx.samples, ctx.options)
        rename = parse_columns(ctx.options.get("columns"))
        by_id = predictions_by_id(preds)
        names = [c.name for c in ctx.dataset.card.categories]
        if not names:
            names = sorted({k for p in preds for k in (getattr(p, payload) or {})})
        missing = [s.sample_id for s in sorted_samples(ctx.samples) if s.sample_id not in by_id]
        if missing and not is_true(ctx.options, "allow_missing"):
            raise ValidationFailed(
                f"missing: {len(missing)} samples have no prediction (e.g. {missing[:3]}); "
                "pass allow_missing=true to write the rest",
                fields={"missing": len(missing)},
            )
        header = [ctx.options.get("id_col", "id"), *(rename.get(n, n) for n in names)]
        rows = 0
        ctx.out.parent.mkdir(parents=True, exist_ok=True)
        with ctx.out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(header)
            for s in sorted_samples(ctx.samples):
                p = by_id.get(s.sample_id)
                if p is None:
                    continue
                values = getattr(p, payload) or {}
                absent = [n for n in names if n not in values]
                if absent:
                    raise ValidationFailed(
                        f"missing_key: sample {s.sample_id!r} has no value for {absent[0]!r}",
                        fields={"sample": s.sample_id},
                    )
                writer.writerow([ids[s.sample_id], *(fmt_float(values[n]) for n in names)])
                rows += 1
        return WriteResult(rows=rows, samples=rows, missing=missing)
```

`src/vcp/submit/writers/__init__.py`：

```python
"""Writer registry. Importing this package registers the built-in writers."""

from vcp.submit.writers.base import (
    WRITERS,
    WriteContext,
    WriteResult,
    Writer,
    get_writer,
    register_writer,
    writer_for,
)
from vcp.submit.writers.scores_csv import ScoresCsvWriter

register_writer(ScoresCsvWriter())

__all__ = [
    "WRITERS",
    "ScoresCsvWriter",
    "WriteContext",
    "WriteResult",
    "Writer",
    "get_writer",
    "register_writer",
    "writer_for",
]
```

- [ ] **Step 6: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/submit/test_writers_base.py tests/unit/submit/test_writer_scores_csv.py -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/submit/writers tests/unit/submit/test_writers_base.py tests/unit/submit/test_writer_scores_csv.py
git commit -m "feat(submit): writer 登記表（決定性、id 對映、選項檢查）與 scores_csv" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---
### Task 6: `coco_results` 與 `csv_boxes` writer

**Files:**
- Create: `src/vcp/submit/writers/coco_results.py`、`src/vcp/submit/writers/csv_boxes.py`
- Modify: `src/vcp/submit/writers/__init__.py`（登記兩個）
- Test: `tests/unit/submit/test_writer_coco_results.py`、`tests/unit/submit/test_writer_csv_boxes.py`

**Interfaces:**
- Consumes: Task 5 的 `base`（`WriteContext`、`WriteResult`、`check_options`、`output_ids`、`fmt_float`、`parse_columns`、`sorted_samples`）、`vcp.measure.schema.PredBox` / `PredMask` / `payload_field`。
- Produces: `CocoResultsWriter`（`file_name="results.json"`，選項 `id_field`）、`CsvBoxesWriter`（`file_name="submission.csv"`，選項 `id_field` / `columns` / `box_format` / `coords` / `label`；`DEFAULT_COLUMNS`、`ORDER`、`from_abs_xywh`）。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/submit/test_writer_coco_results.py`

```python
import json

import pytest

from helpers import SEG_CATS, det_samples, make_card
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample, View
from vcp.measure.schema import PredBox, PredMask, Prediction
from vcp.submit.writers import get_writer
from vcp.submit.writers.base import WriteContext


def _det():
    samples = [
        s.model_copy(update={"meta": {"image_id": str(10 + i)}})
        for i, s in enumerate(det_samples(3, gold_frac=0.0))
    ]
    ds = Dataset.from_parts(make_card("det", name="d"), samples)
    preds = [
        Prediction(
            sample_id="s0000",
            boxes=[
                PredBox(x=1, y=2, w=3, h=4, category_id=1, score=0.9),
                PredBox(x=0.5, y=0.5, w=1, h=1, category_id=0, score=0.25),
            ],
        ),
        Prediction(sample_id="s0002", boxes=[PredBox(x=2, y=2, w=2, h=2, category_id=2, score=1)]),
    ]
    return ds, preds


def test_entries_are_sorted_compact_and_deterministic(tmp_path):
    ds, preds = _det()
    w = get_writer("coco_results")
    out = tmp_path / "results.json"
    res = w.write(list(reversed(preds)), WriteContext(ds, list(ds.samples), {}, out))
    assert res.rows == 3 and res.samples == 2 and res.missing == []
    text = out.read_bytes()
    assert text.endswith(b"\n") and b" " not in text.split(b"\n")[0]
    entries = json.loads(text)
    assert entries[0] == {
        "bbox": [1.0, 2.0, 3.0, 4.0],
        "category_id": 1,
        "image_id": "s0000",
        "score": 0.9,
    }
    assert [e["image_id"] for e in entries] == ["s0000", "s0000", "s0002"]
    again = tmp_path / "b.json"
    w.write(preds, WriteContext(ds, list(ds.samples), {}, again))
    assert again.read_bytes() == text


def test_numeric_ids_become_ints(tmp_path):
    ds, preds = _det()
    out = tmp_path / "results.json"
    get_writer("coco_results").write(
        preds, WriteContext(ds, list(ds.samples), {"id_field": "meta.image_id"}, out)
    )
    assert [e["image_id"] for e in json.loads(out.read_text(encoding="utf-8"))] == [10, 10, 12]


def test_masks_polygon_and_rle(tmp_path):
    samples = [
        Sample(sample_id="m0", views=[View(path="m0.png", width=8, height=6)], label_source="none"),
        Sample(sample_id="m1", views=[View(path="m1.png")], label_source="none"),
    ]
    ds = Dataset.from_parts(make_card("seg", name="s", categories=SEG_CATS), samples)
    w = get_writer("coco_results")
    preds = [
        Prediction(
            sample_id="m0",
            masks=[
                PredMask(category_id=0, score=0.5, polygon=[[0, 0, 4, 0, 4, 4]]),
                PredMask(category_id=1, score=0.5, rle="abc"),
            ],
        )
    ]
    out = tmp_path / "results.json"
    w.write(preds, WriteContext(ds, samples, {}, out))
    entries = json.loads(out.read_text(encoding="utf-8"))
    assert entries[0]["segmentation"] == [[0.0, 0.0, 4.0, 0.0, 4.0, 4.0]]
    assert entries[1]["segmentation"] == {"size": [6, 8], "counts": "abc"}
    bad = [Prediction(sample_id="m1", masks=[PredMask(category_id=0, score=0.5, rle="abc")])]
    with pytest.raises(ValidationFailed, match="width/height"):
        w.write(bad, WriteContext(ds, samples, {}, out))
```

- [ ] **Step 2: 寫失敗的測試** `tests/unit/submit/test_writer_csv_boxes.py`

```python
import pytest

from helpers import det_samples, make_card
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample, View
from vcp.measure.schema import PredBox, Prediction
from vcp.submit.writers import get_writer
from vcp.submit.writers.base import WriteContext
from vcp.submit.writers.csv_boxes import from_abs_xywh


def _det():
    ds = Dataset.from_parts(make_card("det", name="d"), det_samples(2, gold_frac=0.0))
    preds = [
        Prediction(sample_id="s0000", boxes=[PredBox(x=1, y=2, w=4, h=2, category_id=1, score=0.9)])
    ]
    return ds, preds


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_default_columns_and_formats(tmp_path):
    ds, preds = _det()
    w = get_writer("csv_boxes")
    out = tmp_path / "submission.csv"
    res = w.write(preds, WriteContext(ds, list(ds.samples), {}, out))
    assert res.rows == 1 and res.samples == 1
    assert _lines(out) == ["image_filename,label_id,x,y,w,h,score", "s0000,1,1.0,2.0,4.0,2.0,0.9"]
    w.write(preds, WriteContext(ds, list(ds.samples), {"box_format": "xyxy"}, out))
    assert _lines(out)[1] == "s0000,1,1.0,2.0,5.0,4.0,0.9"
    w.write(preds, WriteContext(ds, list(ds.samples), {"box_format": "cxcywh"}, out))
    assert _lines(out)[1] == "s0000,1,3.0,3.0,4.0,2.0,0.9"
    w.write(preds, WriteContext(ds, list(ds.samples), {"coords": "norm"}, out))
    assert _lines(out)[1] == "s0000,1,0.125,0.25,0.5,0.25,0.9"
    assert from_abs_xywh(1, 2, 4, 2, box_format="xyxy", coords="abs", width=8, height=8) == (
        1,
        2,
        5,
        4,
    )


def test_labels_columns_and_ids(tmp_path):
    ds, preds = _det()
    w = get_writer("csv_boxes")
    out = tmp_path / "submission.csv"
    opts = {"label": "category_name", "columns": "image=file,score=conf", "id_field": "view_stem"}
    w.write(preds, WriteContext(ds, list(ds.samples), opts, out))
    assert _lines(out) == ["file,label_id,x,y,w,h,conf", "s0000,dog,1.0,2.0,4.0,2.0,0.9"]
    with pytest.raises(ValidationFailed, match="option=columns"):
        w.write(preds, WriteContext(ds, list(ds.samples), {"columns": "area=a"}, out))
    with pytest.raises(ValidationFailed, match="option=box_format"):
        w.write(preds, WriteContext(ds, list(ds.samples), {"box_format": "polar"}, out))


def test_norm_needs_view_size(tmp_path):
    samples = [Sample(sample_id="n0", views=[View(path="n0.png")], label_source="none")]
    ds = Dataset.from_parts(make_card("det", name="n"), samples)
    preds = [
        Prediction(sample_id="n0", boxes=[PredBox(x=1, y=1, w=1, h=1, category_id=0, score=1)])
    ]
    with pytest.raises(ValidationFailed, match="coords=norm needs"):
        get_writer("csv_boxes").write(
            preds, WriteContext(ds, samples, {"coords": "norm"}, tmp_path / "s.csv")
        )
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/unit/submit/test_writer_coco_results.py tests/unit/submit/test_writer_csv_boxes.py -o addopts="" -q`
Expected: FAIL（`unknown writer 'coco_results'`）

- [ ] **Step 4: 寫 `src/vcp/submit/writers/coco_results.py`**

```python
"""COCO ``results.json``: one entry per box or mask (spec 9.4)."""

from __future__ import annotations

import json
from typing import Any

from vcp.core.errors import ValidationFailed
from vcp.data.schema import Sample
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import PredMask, Prediction, payload_field
from vcp.submit.writers.base import (
    WriteContext,
    WriteResult,
    check_options,
    output_ids,
    sorted_samples,
)


def _image_id(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def _segmentation(mask: PredMask, sample: Sample) -> Any:
    if mask.polygon is not None:
        return mask.polygon
    if mask.view >= len(sample.views):
        raise ValidationFailed(
            f"sample {sample.sample_id!r} has no view {mask.view}",
            fields={"sample": sample.sample_id},
        )
    view = sample.views[mask.view]
    if view.width is None or view.height is None:
        raise ValidationFailed(
            f"sample {sample.sample_id!r}: an RLE mask needs the view's width/height",
            fields={"sample": sample.sample_id},
        )
    return {"size": [view.height, view.width], "counts": mask.rle}


class CocoResultsWriter:
    name = "coco_results"
    version = "1"
    payloads = frozenset({"boxes", "masks"})
    file_name = "results.json"
    options = frozenset({"id_field"})

    def write(self, preds: list[Prediction], ctx: WriteContext) -> WriteResult:
        check_options(ctx.options, self.options)
        payload = payload_field(ctx.dataset.card.task)
        ids = output_ids(ctx.samples, ctx.options)
        by_id = predictions_by_id(preds)
        entries: list[dict[str, Any]] = []
        samples = 0
        for s in sorted_samples(ctx.samples):
            p = by_id.get(s.sample_id)
            items = (getattr(p, payload) or []) if p is not None else []
            if not items:
                continue
            samples += 1
            image_id = _image_id(ids[s.sample_id])
            for item in items:
                entry: dict[str, Any] = {
                    "image_id": image_id,
                    "category_id": item.category_id,
                    "score": item.score,
                }
                if payload == "boxes":
                    entry["bbox"] = [item.x, item.y, item.w, item.h]
                else:
                    entry["segmentation"] = _segmentation(item, s)
                entries.append(entry)
        text = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        ctx.out.parent.mkdir(parents=True, exist_ok=True)
        with ctx.out.open("w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
        return WriteResult(rows=len(entries), samples=samples, missing=[])
```

- [ ] **Step 5: 寫 `src/vcp/submit/writers/csv_boxes.py`**

```python
"""One row per box, ``image,label,x,y,w,h,score``: the mirror of the ``csv_boxes`` importer
(spec 9.5)."""

from __future__ import annotations

import csv

from vcp.core.errors import ValidationFailed
from vcp.data.schema import Sample
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import PredBox, Prediction
from vcp.submit.writers.base import (
    WriteContext,
    WriteResult,
    check_options,
    fmt_float,
    output_ids,
    parse_columns,
    sorted_samples,
)

DEFAULT_COLUMNS = {
    "image": "image_filename",
    "label": "label_id",
    "x": "x",
    "y": "y",
    "w": "w",
    "h": "h",
    "score": "score",
}
ORDER = ("image", "label", "x", "y", "w", "h", "score")
BOX_FORMATS = ("xywh", "xyxy", "cxcywh")
COORD_MODES = ("abs", "norm")
LABEL_MODES = ("category_id", "category_name")


def from_abs_xywh(
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    box_format: str,
    coords: str,
    width: float,
    height: float,
) -> tuple[float, float, float, float]:
    """Inverse of the importer's ``to_abs_xywh``: absolute xywh -> the requested convention."""
    if coords == "norm":
        x, w = x / width, w / width
        y, h = y / height, h / height
    if box_format == "xyxy":
        return x, y, x + w, y + h
    if box_format == "cxcywh":
        return x + w / 2, y + h / 2, w, h
    return x, y, w, h


def _choice(options: dict[str, str], key: str, allowed: tuple[str, ...]) -> str:
    value = options.get(key, allowed[0])
    if value not in allowed:
        raise ValidationFailed(
            f"option={key}: must be one of {allowed}, got {value!r}", fields={"option": key}
        )
    return value


class CsvBoxesWriter:
    name = "csv_boxes"
    version = "1"
    payloads = frozenset({"boxes"})
    file_name = "submission.csv"
    options = frozenset({"id_field", "columns", "box_format", "coords", "label"})

    def write(self, preds: list[Prediction], ctx: WriteContext) -> WriteResult:
        check_options(ctx.options, self.options)
        box_format = _choice(ctx.options, "box_format", BOX_FORMATS)
        coords = _choice(ctx.options, "coords", COORD_MODES)
        label = _choice(ctx.options, "label", LABEL_MODES)
        columns = {**DEFAULT_COLUMNS, **parse_columns(ctx.options.get("columns"))}
        unknown = sorted(set(columns) - set(ORDER))
        if unknown:
            raise ValidationFailed(
                f"option=columns: unknown column keys {unknown}; known: {list(ORDER)}",
                fields={"option": "columns"},
            )
        ids = output_ids(ctx.samples, ctx.options)
        names = {c.id: c.name for c in ctx.dataset.card.categories}
        by_id = predictions_by_id(preds)
        rows = samples = 0
        ctx.out.parent.mkdir(parents=True, exist_ok=True)
        with ctx.out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow([columns[k] for k in ORDER])
            for s in sorted_samples(ctx.samples):
                p = by_id.get(s.sample_id)
                boxes = (p.boxes or []) if p is not None else []
                if not boxes:
                    continue
                samples += 1
                for b in boxes:
                    writer.writerow(
                        [
                            ids[s.sample_id],
                            _label(b, label, names, s),
                            *_geometry(b, s, box_format, coords),
                            fmt_float(b.score),
                        ]
                    )
                    rows += 1
        return WriteResult(rows=rows, samples=samples, missing=[])


def _label(b: PredBox, mode: str, names: dict[int, str], s: Sample) -> int | str:
    if mode == "category_id":
        return b.category_id
    if b.category_id not in names:
        raise ValidationFailed(
            f"sample {s.sample_id!r}: category id {b.category_id} has no name in the card",
            fields={"sample": s.sample_id},
        )
    return names[b.category_id]


def _geometry(b: PredBox, s: Sample, box_format: str, coords: str) -> list[str]:
    width = height = 1.0
    if coords == "norm":
        if b.view >= len(s.views):
            raise ValidationFailed(
                f"sample {s.sample_id!r} has no view {b.view}", fields={"sample": s.sample_id}
            )
        view = s.views[b.view]
        if view.width is None or view.height is None:
            raise ValidationFailed(
                f"sample {s.sample_id!r}: coords=norm needs the view's width/height",
                fields={"sample": s.sample_id},
            )
        width, height = float(view.width), float(view.height)
    values = from_abs_xywh(
        b.x, b.y, b.w, b.h, box_format=box_format, coords=coords, width=width, height=height
    )
    return [fmt_float(v) for v in values]
```

`__init__.py` 加 `from vcp.submit.writers.coco_results import CocoResultsWriter`、`from vcp.submit.writers.csv_boxes import CsvBoxesWriter`，登記 `register_writer(CocoResultsWriter())`、`register_writer(CsvBoxesWriter())`（在 `ScoresCsvWriter` 之後），`__all__` 加兩個名字。

- [ ] **Step 6: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/submit -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/submit/writers tests/unit/submit/test_writer_coco_results.py tests/unit/submit/test_writer_csv_boxes.py
git commit -m "feat(submit): coco_results 與 csv_boxes writer（決定性 JSON / CSV、box_format 與 coords 轉換）" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: 配對核對與準入門

**Files:**
- Create: `src/vcp/submit/pairing.py`、`src/vcp/submit/gate.py`
- Test: `tests/unit/submit/test_pairing.py`、`tests/unit/submit/test_gate.py`

**Interfaces:**
- Consumes: `vcp.measure.runs.load_run` / `save_run`、`vcp.fuse.build.load_record` / `record_path` / `write_record`、`vcp.fuse.schema.FuseRecord`、`vcp.measure.prereg.load_prereg`、`vcp.measure.ledger.ReadingsLedger` / `read_rows` / `append_row`、`vcp.measure.report.JUDGEMENTS_LEDGER` / `READINGS_LEDGER`、`vcp.measure.metrics.params_key`、`submit_fixtures.seed_eval_runs` / `seed_judgements` / `ingest_run`。
- Produces: `is_fusion(data_root, run_id) -> bool`、`verify_pairing(data_root, eval_card, test_card, *, test_subset, index="") -> Pairing`、`verify_weights(data_root, eval_run, declared) -> tuple[Pairing, list[WeightRef]]`、`UNCHECKED = "config_hash=unchecked"`；`latest_judgements(eval_paths) -> dict[str, Judgement]`、`admit(data_root, eval_paths, eval_card, fuse, kind, reason) -> Gate`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/submit/test_pairing.py`

```python
import pytest

from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.fuse.build import write_record
from vcp.fuse.schema import FuseRecord, MemberRecord, SubsetBuild
from vcp.measure.runs import save_run
from vcp.measure.schema import PredictionFile, RunCard, RunSource
from vcp.submit.pairing import UNCHECKED, is_fusion, verify_pairing, verify_weights

STAMP = "2026-09-05T00:00:00.000Z"
W1, W2, C1 = "a" * 64, "b" * 64, "c" * 64


def _card(run_id, dataset, *, weights=None, config=None, preds=None):
    return RunCard(
        run_id=run_id,
        dataset=dataset,
        samples_hash="h" * 64,
        plan_id="p",
        trained_on=[],
        source=RunSource(weights_hash=weights, config_hash=config),
        created_at=STAMP,
        predictions=preds or {},
    )


def _pred(sha):
    return PredictionFile(
        path="predictions/test.jsonl",
        sha256=sha,
        samples=1,
        empty=0,
        format_in="jsonl",
        ingested_at=STAMP,
    )


def _fuse(run_id, members, output, member_shas, *, method="mean", params=None):
    return FuseRecord(
        run_id=run_id,
        recipe_id="r",
        recipe_sha256="s" * 64,
        method=method,
        method_version="1",
        params=params or {},
        members=[MemberRecord(run=m, weight=w, trained_on=[]) for m, w in members],
        subsets={
            "test": SubsetBuild(
                member_sha256=member_shas, output_sha256=output, samples=1, empty=0, built_at=STAMP
            )
        },
        vcp_version="0",
    )


def test_single_pairing_by_weights(roots):
    e = _card("e", "d", weights=W1, config=C1)
    t = _card("t", "d-test", weights=W1, config=C1)
    p = verify_pairing(roots.data, e, t, test_subset="test")
    assert p.mode == "single" and p.checks == [f"weights_hash={W1[:12]}", f"config_hash={C1[:12]}"]
    p = verify_pairing(roots.data, e, _card("t", "d-test", weights=W1), test_subset="test")
    assert UNCHECKED in p.checks
    with pytest.raises(ValidationFailed, match="identity: weights_hash differs") as ei:
        verify_pairing(roots.data, e, _card("t", "d-test", weights=W2), test_subset="test")
    assert ei.value.fields == {"field": "weights_hash", "side": "both"}
    with pytest.raises(ValidationFailed, match="missing on the test side") as ei:
        verify_pairing(roots.data, e, _card("t", "d-test"), test_subset="test")
    assert ei.value.fields["side"] == "test"
    with pytest.raises(ValidationFailed, match="config_hash differs"):
        verify_pairing(
            roots.data, e, _card("t", "d-test", weights=W1, config=W2), test_subset="test"
        )


def _fusion_pair(roots, *, test_weight=0.5, output_ok=True, member_ok=True, params=None):
    for run_id, dataset, weights in (
        ("m1", "d", W1),
        ("m2", "d", W2),
        ("m1.test", "d-test", W1),
        ("m2.test", "d-test", W2),
    ):
        preds = {"test": _pred("m" * 64)} if dataset == "d-test" else None
        save_run(roots.data, _card(run_id, dataset, weights=weights, preds=preds))
    e = _card("fe", "d")
    t = _card("ft", "d-test", preds={"test": _pred("o" * 64)})
    save_run(roots.data, e)
    save_run(roots.data, t)
    write_record(roots.data, "fe", _fuse("fe", [("m1", 1.0), ("m2", 0.5)], "x" * 64, {}))
    member_shas = {"m1.test": "m" * 64, "m2.test": ("m" if member_ok else "z") * 64}
    write_record(
        roots.data,
        "ft",
        _fuse(
            "ft",
            [("m1.test", 1.0), ("m2.test", test_weight)],
            ("o" if output_ok else "q") * 64,
            member_shas,
            params=params or {},
        ),
    )
    return e, t


def test_fusion_pairing_recurses_into_members(roots):
    e, t = _fusion_pair(roots)
    assert is_fusion(roots.data, "fe") and not is_fusion(roots.data, "m1")
    p = verify_pairing(roots.data, e, t, test_subset="test")
    assert p.mode == "fusion" and [m.eval for m in p.members] == ["m1", "m2"]
    assert [m.test for m in p.members] == ["m1.test", "m2.test"]
    assert "method=mean" in p.checks and "members=2" in p.checks and UNCHECKED in p.checks


def test_fusion_pairing_failures(roots):
    e, t = _fusion_pair(roots, test_weight=1.0)
    with pytest.raises(ValidationFailed, match="weight differs") as ei:
        verify_pairing(roots.data, e, t, test_subset="test")
    assert ei.value.fields == {"field": "weight", "index": "1"}
    e, t = _fusion_pair(roots, output_ok=False)
    with pytest.raises(IntegrityError, match="output_sha256"):
        verify_pairing(roots.data, e, t, test_subset="test")
    e, t = _fusion_pair(roots, member_ok=False)
    with pytest.raises(IntegrityError, match="member_sha256"):
        verify_pairing(roots.data, e, t, test_subset="test")
    e, t = _fusion_pair(roots, params={"k": "1"})
    with pytest.raises(ValidationFailed, match="params differs") as ei:
        verify_pairing(roots.data, e, t, test_subset="test")
    assert ei.value.fields == {"field": "params"}
    with pytest.raises(ValidationFailed, match="only one side is a fusion run") as ei:
        verify_pairing(roots.data, e, _card("t", "d-test", weights=W1), test_subset="test")
    assert ei.value.fields == {"field": "fuse.json", "side": "test"}


def test_verify_weights_for_kernels(roots):
    save_run(roots.data, _card("e", "d", weights=W1))
    save_run(roots.data, _card("e2", "d"))
    p, refs = verify_weights(roots.data, "e", [])
    assert p.mode == "kernel" and p.checks[0] == "notebook=declared"
    assert [(r.run, r.sha256) for r in refs] == [("e", W1)]
    p, refs = verify_weights(roots.data, "e", [f"e:{W1}"])
    assert len(refs) == 1
    with pytest.raises(ValidationFailed, match="declared sha"):
        verify_weights(roots.data, "e", [f"e:{W2}"])
    with pytest.raises(ValidationFailed, match="has no weights_hash") as ei:
        verify_weights(roots.data, "e", ["e2"])
    assert ei.value.fields == {"field": "weights_hash", "run": "e2"}
```

- [ ] **Step 2: 寫失敗的測試** `tests/unit/submit/test_gate.py`

```python
import pytest
from submit_fixtures import STAMP, ingest_run, random_scores, seed_eval_runs, seed_judgements

from helpers import perfect_predictions
from vcp.core.config import dump_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.fuse.schema import FuseRecord, MemberRecord
from vcp.measure.ledger import append_row
from vcp.measure.prereg import prereg_path
from vcp.measure.report import JUDGEMENTS_LEDGER
from vcp.measure.runs import load_run
from vcp.measure.schema import Judgement, PreRegistration
from vcp.submit.gate import admit, latest_judgements


def test_candidate_needs_a_pass_judgement(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    good = load_run(pair.roots.data, "good")
    gate = admit(pair.roots.data, pair.eval_paths, good, None, "candidate", None)
    assert gate.admission == "PASS" and gate.judgements == ["p-good"] and gate.reason == ""
    bad = load_run(pair.roots.data, "bad")
    with pytest.raises(ValidationFailed, match="not_admitted") as ei:
        admit(pair.roots.data, pair.eval_paths, bad, None, "candidate", None)
    assert ei.value.fields == {"run": "bad"}
    assert set(latest_judgements(pair.eval_paths)) == {"p-good", "p-bad"}


def test_waived_kinds_need_a_reason(pair):
    seed_eval_runs(pair)
    bad = load_run(pair.roots.data, "bad")
    gate = admit(pair.roots.data, pair.eval_paths, bad, None, "baseline", "first anchor")
    assert gate.admission == "waived" and gate.reason == "first anchor"
    with pytest.raises(ValidationFailed, match="reason_required"):
        admit(pair.roots.data, pair.eval_paths, bad, None, "probe", None)


def test_replaced_predictions_make_the_judgement_stale(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    ds, plan = pair.eval_ds, pair.eval_plan
    samples = ds.subset("valA", plan, paths=pair.eval_paths)
    shuffled = perfect_predictions(list(reversed(samples))[:-1], ds.card)
    ingest_run(
        pair,
        "good",
        ds,
        "fixed-v1",
        "valA",
        shuffled,
        weights=pair.weights["good"],
        trained_on=("train",),
        replace=True,
    )
    good = load_run(pair.roots.data, "good")
    with pytest.raises(ValidationFailed, match="stale_judgement") as ei:
        admit(pair.roots.data, pair.eval_paths, good, None, "candidate", None)
    assert ei.value.fields == {"prereg": "p-good"}


def _judgement(pid, candidate, verdict):
    return Judgement(
        prereg_id=pid,
        ts=STAMP,
        baseline_run="base",
        candidate_run=candidate,
        metric="accuracy",
        metric_version="1",
        params={},
        higher_is_better=True,
        per_subset={},
        bases_positive=2,
        sigma_p=None,
        verdict=verdict,
        reasons=[],
        reading_ids=[],
        bootstrap={"resamples": 50, "seed": 0},
    )


def _prereg(pair, pid, component, candidate):
    pr = PreRegistration(
        prereg_id=pid,
        claim="x",
        component=component,
        component_class="model",
        baseline_run="base",
        candidate_run=candidate,
        metric="accuracy",
        subsets=["valA", "valB"],
        created_at=STAMP,
    )
    dump_yaml_model(pr, prereg_path(pair.eval_paths, pid))


def test_fusion_candidate_needs_every_member_admitted(pair):
    seed_eval_runs(pair)
    ingest_run(
        pair,
        "fused",
        pair.eval_ds,
        "fixed-v1",
        "valA",
        random_scores(pair.eval_ds.subset("valA", pair.eval_plan, paths=pair.eval_paths), seed=5),
        weights=None,
        trained_on=("train",),
    )
    fuse = FuseRecord(
        run_id="fused",
        recipe_id="r",
        recipe_sha256="s" * 64,
        method="mean",
        method_version="1",
        params={},
        members=[
            MemberRecord(run="good", weight=1.0, trained_on=["train"]),
            MemberRecord(run="bad", weight=1.0, trained_on=["train"]),
        ],
        vcp_version="0",
    )
    ledger = pair.eval_paths.measure_dir / JUDGEMENTS_LEDGER
    _prereg(pair, "r-admit-good", "good", "fused")
    append_row(ledger, _judgement("r-admit-good", "fused", "PASS"))
    fused = load_run(pair.roots.data, "fused")
    with pytest.raises(ValidationFailed, match="member_not_admitted") as ei:
        admit(pair.roots.data, pair.eval_paths, fused, fuse, "candidate", None)
    assert ei.value.fields == {"member": "bad"}
    _prereg(pair, "r-admit-bad", "bad", "fused")
    append_row(ledger, _judgement("r-admit-bad", "fused", "FAIL"))
    with pytest.raises(ValidationFailed, match="member_not_admitted"):
        admit(pair.roots.data, pair.eval_paths, fused, fuse, "candidate", None)
    append_row(ledger, _judgement("r-admit-bad", "fused", "PASS"))
    gate = admit(pair.roots.data, pair.eval_paths, fused, fuse, "candidate", None)
    assert gate.judgements == ["r-admit-good", "r-admit-bad"]
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/unit/submit/test_pairing.py tests/unit/submit/test_gate.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 4: 寫 `src/vcp/submit/pairing.py`**

```python
"""Are the eval-side and test-side runs the same model? Decided by hashes, never by names
(spec 7): a name is a promise, a weights hash is a fact."""

from __future__ import annotations

from pathlib import Path

from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.log import FieldValue
from vcp.fuse.build import load_record, record_path
from vcp.fuse.schema import FuseRecord
from vcp.measure.metrics import params_key
from vcp.measure.runs import load_run
from vcp.measure.schema import RunCard
from vcp.submit.schema import PairMember, Pairing, WeightRef

UNCHECKED = "config_hash=unchecked"


def is_fusion(data_root: Path, run_id: str) -> bool:
    return record_path(data_root, run_id).is_file()


def _identity(message: str, **fields: FieldValue) -> ValidationFailed:
    return ValidationFailed(
        f"identity: {message}", fields={k: v for k, v in fields.items() if v != ""}
    )


def _single(e: RunCard, t: RunCard, index: str) -> Pairing:
    ew, tw = e.source.weights_hash, t.source.weights_hash
    if not ew or not tw:
        side = "both" if not ew and not tw else ("eval" if not ew else "test")
        raise _identity(
            f"weights_hash missing on the {side} side ({e.run_id} / {t.run_id}); "
            "ingest with --weights",
            field="weights_hash",
            side=side,
            index=index,
        )
    if ew != tw:
        raise _identity(
            f"weights_hash differs: {e.run_id} {ew[:12]} vs {t.run_id} {tw[:12]}",
            field="weights_hash",
            side="both",
            index=index,
        )
    checks = [f"weights_hash={ew[:12]}"]
    ec, tc = e.source.config_hash, t.source.config_hash
    if ec and tc:
        if ec != tc:
            raise _identity(
                f"config_hash differs: {e.run_id} {ec[:12]} vs {t.run_id} {tc[:12]}",
                field="config_hash",
                side="both",
                index=index,
            )
        checks.append(f"config_hash={ec[:12]}")
    else:
        checks.append(UNCHECKED)
    return Pairing(mode="single", checks=checks)


def _fusion(
    data_root: Path, ef: FuseRecord, tf: FuseRecord, t: RunCard, test_subset: str
) -> Pairing:
    for name in ("method", "method_version", "params"):
        if getattr(ef, name) != getattr(tf, name):
            raise _identity(
                f"{name} differs: {ef.run_id} {getattr(ef, name)!r} vs "
                f"{tf.run_id} {getattr(tf, name)!r}",
                field=name,
            )
    if len(ef.members) != len(tf.members):
        raise _identity(
            f"member count differs: {len(ef.members)} vs {len(tf.members)}", field="members"
        )
    checks = [
        f"method={ef.method}",
        f"method_version={ef.method_version}",
        f"params={params_key(ef.params)}",
        f"members={len(ef.members)}",
    ]
    build = tf.subsets.get(test_subset)
    entry = t.predictions.get(test_subset)
    if build is None or entry is None:
        raise _identity(
            f"test fusion run {t.run_id!r} has no build for subset {test_subset!r}",
            field="subset",
        )
    members: list[PairMember] = []
    for i, (em, tm) in enumerate(zip(ef.members, tf.members, strict=True)):
        if em.weight != tm.weight:
            raise _identity(
                f"member {i} weight differs: {em.run} {em.weight} vs {tm.run} {tm.weight}",
                field="weight",
                index=str(i),
            )
        tcard = load_run(data_root, tm.run)
        sub = verify_pairing(
            data_root, load_run(data_root, em.run), tcard, test_subset=test_subset, index=str(i)
        )
        members.append(PairMember(eval=em.run, test=tm.run, mode=sub.mode))
        if UNCHECKED in sub.checks and UNCHECKED not in checks:
            checks.append(UNCHECKED)
        recorded = build.member_sha256.get(tm.run)
        current = tcard.predictions.get(test_subset)
        if current is None or recorded != current.sha256:
            raise IntegrityError(
                f"fuse.json member_sha256 for {tm.run!r} ({(recorded or 'none')[:12]}) != its "
                f"current predictions ({(current.sha256 if current else 'none')[:12]})",
                fields={"run": t.run_id, "member": tm.run},
            )
    if build.output_sha256 != entry.sha256:
        raise IntegrityError(
            f"fuse.json output_sha256 {build.output_sha256[:12]} != run.yaml predictions sha "
            f"{entry.sha256[:12]} for {t.run_id!r}/{test_subset!r}",
            fields={"run": t.run_id},
        )
    return Pairing(mode="fusion", members=members, checks=checks)


def verify_pairing(
    data_root: Path, eval_card: RunCard, test_card: RunCard, *, test_subset: str, index: str = ""
) -> Pairing:
    ef = (
        load_record(data_root, eval_card.run_id) if is_fusion(data_root, eval_card.run_id) else None
    )
    tf = (
        load_record(data_root, test_card.run_id) if is_fusion(data_root, test_card.run_id) else None
    )
    if (ef is None) != (tf is None):
        raise _identity(
            f"only one side is a fusion run ({eval_card.run_id} / {test_card.run_id})",
            field="fuse.json",
            side="eval" if ef is None else "test",
            index=index,
        )
    if ef is None or tf is None:
        return _single(eval_card, test_card, index)
    return _fusion(data_root, ef, tf, test_card, test_subset)


def verify_weights(
    data_root: Path, eval_run: str, declared: list[str]
) -> tuple[Pairing, list[WeightRef]]:
    """Kernel submissions: the weights a notebook says it loads, checked against run cards.
    The notebook itself is not verified, and the checks say so."""
    refs: list[WeightRef] = []
    for item in declared or [eval_run]:
        run, _, sha = item.partition(":")
        card = load_run(data_root, run)
        have = card.source.weights_hash
        if not have:
            raise _identity(
                f"run {run!r} has no weights_hash; ingest with --weights",
                field="weights_hash",
                run=run,
            )
        if sha and sha != have:
            raise _identity(
                f"declared sha {sha[:12]} != run {run!r} weights_hash {have[:12]}",
                field="weights_hash",
                run=run,
            )
        refs.append(WeightRef(run=run, sha256=have))
    checks = ["notebook=declared", *(f"weights:{r.run}={r.sha256[:12]}" for r in refs)]
    return Pairing(mode="kernel", checks=checks), refs
```

- [ ] **Step 5: 寫 `src/vcp/submit/gate.py`**

```python
"""The admission gate (spec 8): a candidate is a PASS judgement, never a good-looking number."""

from __future__ import annotations

from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.fuse.schema import FuseRecord
from vcp.measure.ledger import ReadingsLedger, read_rows
from vcp.measure.prereg import load_prereg
from vcp.measure.report import JUDGEMENTS_LEDGER, READINGS_LEDGER
from vcp.measure.runs import load_run
from vcp.measure.schema import Judgement, RunCard
from vcp.submit.schema import CandidateKind, Gate


def latest_judgements(eval_paths: DatasetPaths) -> dict[str, Judgement]:
    """The newest judgement of every claim (a re-judged claim's last ledger row wins)."""
    latest: dict[str, Judgement] = {}
    for j in read_rows(eval_paths.measure_dir / JUDGEMENTS_LEDGER, Judgement):
        latest[j.prereg_id] = j
    return latest


def _assert_fresh(data_root: Path, j: Judgement, readings: ReadingsLedger) -> None:
    """Every reading the judgement rests on must still describe the run's current bytes."""
    for rid in j.reading_ids:
        r = readings.by_id.get(rid)
        if r is None:
            raise ValidationFailed(
                f"stale_judgement: {j.prereg_id!r} cites reading {rid[:12]} which is not in "
                "the readings ledger",
                fields={"prereg": j.prereg_id},
            )
        entry = load_run(data_root, r.run_id).predictions.get(r.subset)
        now = entry.sha256 if entry is not None else None
        if now != r.prediction_sha:
            raise ValidationFailed(
                f"stale_judgement: {j.prereg_id!r} judged {r.run_id}/{r.subset} at prediction "
                f"sha {r.prediction_sha[:12]}, the run now has {(now or 'none')[:12]}; "
                "re-judge after the replacement",
                fields={"prereg": j.prereg_id},
            )


def admit(
    data_root: Path,
    eval_paths: DatasetPaths,
    eval_card: RunCard,
    fuse: FuseRecord | None,
    kind: CandidateKind,
    reason: str | None,
) -> Gate:
    if kind != "candidate":
        if not reason:
            raise ValidationFailed(
                f"reason_required: kind={kind} needs --reason", fields={"kind": kind}
            )
        return Gate(admission="waived", judgements=[], reason=reason)
    latest = latest_judgements(eval_paths)
    passing = [
        j for j in latest.values() if j.candidate_run == eval_card.run_id and j.verdict == "PASS"
    ]
    if not passing:
        raise ValidationFailed(
            f"not_admitted: no PASS judgement names {eval_card.run_id!r} as its candidate run",
            fields={"run": eval_card.run_id},
        )
    used: list[Judgement] = []
    if fuse is None:
        used.append(passing[-1])
    else:
        for m in fuse.members:
            match = [j for j in passing if load_prereg(eval_paths, j.prereg_id).component == m.run]
            if not match:
                raise ValidationFailed(
                    f"member_not_admitted: member {m.run!r} of {eval_card.run_id!r} has no PASS "
                    "admission judgement (run `vcp fuse ablate --preregister` and judge it)",
                    fields={"member": m.run},
                )
            used.append(match[-1])
    readings = ReadingsLedger(eval_paths.measure_dir / READINGS_LEDGER)
    for j in used:
        _assert_fresh(data_root, j, readings)
    return Gate(admission="PASS", judgements=[j.prereg_id for j in used], reason=reason or "")
```

- [ ] **Step 6: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/submit -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/submit/pairing.py src/vcp/submit/gate.py tests/unit/submit/test_pairing.py tests/unit/submit/test_gate.py
git commit -m "feat(submit): eval / test 配對核對（單模比權重 hash、融合逐成員遞迴）與準入門（PASS 判決、成員準入、判決過期）" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: 平台登記表、redact、`manual` 與 `kaggle`

**Files:**
- Create: `src/vcp/submit/platforms/__init__.py`、`src/vcp/submit/platforms/base.py`、`src/vcp/submit/platforms/manual.py`、`src/vcp/submit/platforms/kaggle.py`
- Modify: `src/vcp/core/errors.py`（加 `PlatformError`）
- Test: `tests/unit/submit/test_platforms.py`

**Interfaces:**
- Consumes: `Staged` / `PlatformProfile`（Task 1）、`vcp.core.hashing.sha256_text`、`vcp.core.time.stamp`。
- Produces: `vcp.core.errors.PlatformError`（status FAIL）；`Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]`、`default_runner`、`redact(text) -> str`、`UploadResult(confirmed, platform_ref, detail)`、`PlatformSubmission(platform_ref, file_name, at, description, public, private, status, submitted_by)`、`Platform` protocol（`name`、`upload(staged, artifact, message, profile, runner)`、`list_submissions(profile, runner)`，`runner=None` 表示用真的子程序）、`PLATFORMS`、`register_platform`、`get_platform`；`ManualPlatform`；`KagglePlatform` + `parse_submissions(text) -> tuple[list[PlatformSubmission], str | None]`、`parse_score(value, key)`、`parse_date(value)`、`SUCCESS`、`PAGE_SIZE`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/submit/test_platforms.py`

```python
import json
import subprocess

import pytest

from vcp.core.errors import PlatformError, ValidationFailed, VcpError
from vcp.submit.platforms import get_platform
from vcp.submit.platforms.base import redact
from vcp.submit.platforms.kaggle import parse_date, parse_score, parse_submissions
from vcp.submit.schema import Artifact, Gate, Pairing, PlatformProfile, Staged

SECRET = "fakesecretfakesecretfakesecret1234"
STAMP = "2026-09-05T00:00:00.000Z"


def _profile(**over):
    base = dict(
        dataset="t",
        eval_dataset="d",
        plan_id="p",
        sealed_subset="holdout",
        platform="kaggle",
        competition="c1",
        board_rule="best",
        metric="accuracy",
        writer="scores_csv",
        kaggle_command=["fake-kaggle"],
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


def _staged(kind="file"):
    artifact = (
        Artifact(
            kind="file",
            path="submission.csv",
            bytes=3,
            sha256="a" * 64,
            md5="b" * 32,
            writer="scores_csv",
            writer_version="1",
            rows=1,
            samples=1,
            missing=0,
        )
        if kind == "file"
        else Artifact(
            kind="kernel",
            kernel="u/nb",
            version=7,
            output="submission.csv",
            weights=[{"run": "e", "sha256": "c" * 64}],
        )
    )
    return Staged(
        submission_id="S1",
        dataset="t",
        kind="candidate",
        eval_run="e",
        test_run="t1" if kind == "file" else None,
        pairing=Pairing(mode="single" if kind == "file" else "kernel"),
        artifact=artifact,
        gate=Gate(admission="PASS"),
        profile_sha256="p" * 64,
        staged_at=STAMP,
        vcp_version="0",
    )


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        code, out, err = self.responses.pop(0)
        return subprocess.CompletedProcess(args, code, out, err)


def test_redact():
    assert redact(f"KAGGLE_KEY={SECRET} done") == "KAGGLE_KEY=<redacted>"
    assert redact("Authorization: Bearer abc.def") == "Authorization=<redacted>"
    assert redact("token: xyz") == "token=<redacted>"
    assert redact(f"echo {SECRET}") == "echo <redacted>"
    assert redact("short id S1 ok") == "short id S1 ok"


def test_manual_platform_refuses(tmp_path):
    p = get_platform("manual")
    with pytest.raises(ValidationFailed, match="manual_platform"):
        p.upload(_staged(), tmp_path / "s.csv", "S1", _profile(platform="manual"), None)
    with pytest.raises(ValidationFailed, match="manual_platform"):
        p.list_submissions(_profile(platform="manual"), None)


def test_kaggle_upload_commands_and_outcomes(tmp_path):
    p = get_platform("kaggle")
    art = tmp_path / "submission.csv"
    art.write_text("id\n", encoding="utf-8")
    runner = FakeRunner([(0, f"KAGGLE_KEY={SECRET}\nSuccessfully submitted to c1", "")])
    res = p.upload(_staged(), art, "S1 note", _profile(), runner)
    assert res.confirmed and res.platform_ref is None and SECRET not in res.detail
    assert runner.calls == [
        ["fake-kaggle", "competitions", "submit", "-f", str(art), "-m", "S1 note", "-q", "c1"]
    ]
    runner = FakeRunner([(0, "Submission queued", "")])
    res = p.upload(_staged("kernel"), None, "S1", _profile(submission_kind="kernel"), runner)
    assert not res.confirmed
    assert runner.calls[0][:9] == [
        "fake-kaggle",
        "competitions",
        "submit",
        "-k",
        "u/nb",
        "-v",
        "7",
        "-f",
        "submission.csv",
    ]
    runner = FakeRunner([(1, "", f"401 Unauthorized key={SECRET}")])
    with pytest.raises(PlatformError, match="exit 1") as ei:
        p.upload(_staged(), art, "S1", _profile(), runner)
    assert SECRET not in str(ei.value) and ei.value.fields == {"exit_code": 1}
    assert ei.value.status == "FAIL"


def test_kaggle_command_must_exist_without_a_runner(tmp_path):
    with pytest.raises(VcpError, match="kaggle_not_found"):
        get_platform("kaggle").list_submissions(_profile(kaggle_command=["no-such-exe-xyz"]), None)


def test_parse_submissions_shapes_and_paging():
    rows = [
        {
            "ref": 11,
            "fileName": "submission.csv",
            "date": "2026-08-31T21:28:00Z",
            "description": "S1",
            "status": "complete",
            "publicScore": "0.79",
            "privateScore": None,
            "submittedBy": "eric",
        },
        {"fileName": "x.csv", "date": "2026-08-31 22:00:00", "publicScore": ""},
    ]
    subs, token = parse_submissions(json.dumps(rows))
    assert token is None and len(subs) == 2
    assert subs[0].platform_ref == "11" and subs[0].public == 0.79 and subs[0].private is None
    assert subs[0].at == "2026-08-31T21:28:00.000Z" and subs[0].submitted_by == "eric"
    assert subs[1].platform_ref and subs[1].public is None and subs[1].submitted_by is None
    subs, token = parse_submissions(json.dumps({"submissions": rows[:1], "nextPageToken": "p2"}))
    assert token == "p2" and len(subs) == 1
    with pytest.raises(ValidationFailed, match="platform_response") as ei:
        parse_submissions(json.dumps([{"date": "2026-08-31T21:28:00Z"}]))
    assert ei.value.fields == {"key": "fileName"}
    with pytest.raises(ValidationFailed, match="not JSON"):
        parse_submissions("<html>")
    assert parse_score("null", "publicScore") is None and parse_score(0.5, "x") == 0.5
    with pytest.raises(ValidationFailed, match="unparsable score"):
        parse_score("n/a", "publicScore")
    assert parse_date("2026-08-31T21:28:00+02:00") == "2026-08-31T19:28:00.000Z"
    with pytest.raises(ValidationFailed, match="unparsable date"):
        parse_date("yesterday")


def test_kaggle_list_pages_to_the_end():
    p = get_platform("kaggle")
    page1 = json.dumps(
        {
            "submissions": [{"fileName": "a.csv", "date": "2026-09-01T00:00:00Z"}],
            "nextPageToken": "t2",
        }
    )
    page2 = json.dumps([{"fileName": "b.csv", "date": "2026-09-02T00:00:00Z"}])
    runner = FakeRunner([(0, page1, ""), (0, page2, "")])
    subs = p.list_submissions(_profile(), runner)
    assert [s.file_name for s in subs] == ["a.csv", "b.csv"]
    assert runner.calls[1][-2:] == ["--page-token", "t2"]
    assert runner.calls[0][:6] == [
        "fake-kaggle",
        "competitions",
        "submissions",
        "--format",
        "json",
        "--page-size",
    ]
    runner = FakeRunner([(2, "", f"boom {SECRET}")])
    with pytest.raises(PlatformError) as ei:
        p.list_submissions(_profile(), runner)
    assert SECRET not in str(ei.value)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/submit/test_platforms.py -o addopts="" -q`
Expected: FAIL（`ImportError: PlatformError`）

- [ ] **Step 3: `src/vcp/core/errors.py` 加**

```python
class PlatformError(VcpError):
    """A submission platform's CLI ran and failed. The message is already redacted."""

    status = "FAIL"
```

- [ ] **Step 4: 寫 `src/vcp/submit/platforms/base.py`**

```python
"""Platform contract and registry (spec 10), and the redaction every platform byte passes
through before it can reach a ledger, a log or a VERDICT (spec 11)."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vcp.core.errors import RegistryError
from vcp.submit.schema import PlatformProfile, Staged

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


@dataclass(frozen=True)
class UploadResult:
    confirmed: bool
    platform_ref: str | None
    detail: str


@dataclass(frozen=True)
class PlatformSubmission:
    platform_ref: str
    file_name: str
    at: str
    description: str
    public: float | None
    private: float | None
    status: str
    submitted_by: str | None


class Platform(Protocol):
    name: str

    def upload(
        self,
        staged: Staged,
        artifact: Path | None,
        message: str,
        profile: PlatformProfile,
        runner: Runner | None,
    ) -> UploadResult: ...

    def list_submissions(
        self, profile: PlatformProfile, runner: Runner | None
    ) -> list[PlatformSubmission]: ...


PLATFORMS: dict[str, Platform] = {}


def register_platform(platform: Platform) -> None:
    if platform.name in PLATFORMS:
        raise RegistryError(f"platform {platform.name!r} already registered")
    PLATFORMS[platform.name] = platform


def get_platform(name: str) -> Platform:
    try:
        return PLATFORMS[name]
    except KeyError:
        raise RegistryError(f"unknown platform {name!r}; known: {sorted(PLATFORMS)}") from None
```

`_KV` 遮到行尾：`key=` / `token:` 之後整行都是值，`Authorization: Bearer abc.def` 整段變成 `Authorization=<redacted>`；過度遮蔽（`monkey=5` 也被遮）是可接受的代價，它只作用在第三方 CLI 的輸出。

- [ ] **Step 5: 寫 `manual.py`、`kaggle.py`、`__init__.py`**

`manual.py`：

```python
"""A platform without an API: people upload, ``vcp submit record`` writes it down."""

from __future__ import annotations

from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.submit.platforms.base import PlatformSubmission, Runner, UploadResult
from vcp.submit.schema import PlatformProfile, Staged


class ManualPlatform:
    name = "manual"

    def upload(
        self,
        staged: Staged,
        artifact: Path | None,
        message: str,
        profile: PlatformProfile,
        runner: Runner | None,
    ) -> UploadResult:
        raise ValidationFailed(
            "manual_platform: this profile has no upload API; upload by hand, then "
            "`vcp submit record --id ... --at ...`"
        )

    def list_submissions(
        self, profile: PlatformProfile, runner: Runner | None
    ) -> list[PlatformSubmission]:
        raise ValidationFailed("manual_platform: nothing to read back from; use `vcp submit score`")
```

`kaggle.py`：

```python
"""Kaggle through its own CLI (spec 10.2). vcp never sees a credential: the CLI reads its own
config, the subprocess inherits the environment unrecorded, and every byte it prints is
redacted before it can reach a ledger, a log or a VERDICT (spec 11)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vcp.core.errors import PlatformError, ValidationFailed, VcpError
from vcp.core.hashing import sha256_text
from vcp.core.time import stamp
from vcp.submit.platforms.base import (
    PlatformSubmission,
    Runner,
    UploadResult,
    default_runner,
    redact,
)
from vcp.submit.schema import PlatformProfile, Staged

SUCCESS = "successfully submitted"
PAGE_SIZE = "200"
MAX_PAGES = 100
_EMPTY = {"", "none", "null", "nan"}


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


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return redact(lines[-1]) if lines else ""


def _failed(proc: Any) -> PlatformError:
    return PlatformError(
        f"kaggle CLI failed (exit {proc.returncode}): {_last_line(proc.stderr or proc.stdout)}",
        fields={"exit_code": proc.returncode},
    )


def parse_score(value: Any, key: str) -> float | None:
    if value is None or str(value).strip().lower() in _EMPTY:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationFailed(
            f"platform_response: unparsable score {value!r}", fields={"key": key}
        ) from None


def parse_date(value: Any) -> str:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationFailed(
            f"platform_response: unparsable date {value!r}", fields={"key": "date"}
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return stamp(dt)


def parse_submissions(text: str) -> tuple[list[PlatformSubmission], str | None]:
    """One page of ``competitions submissions --format json``: a bare list, or an object holding
    the list plus a ``nextPageToken``."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValidationFailed(
            f"platform_response: not JSON ({redact(str(e))})", fields={"key": "json"}
        ) from None
    token: str | None = None
    if isinstance(data, dict):
        token = str(data["nextPageToken"]) if data.get("nextPageToken") else None
        lists = [v for v in data.values() if isinstance(v, list)]
        data = lists[0] if lists else []
    if not isinstance(data, list):
        raise ValidationFailed(
            "platform_response: expected a list of submissions", fields={"key": "json"}
        )
    out: list[PlatformSubmission] = []
    for item in data:
        for key in ("fileName", "date"):
            if not isinstance(item, dict) or key not in item:
                raise ValidationFailed(
                    f"platform_response: key {key!r} missing", fields={"key": key}
                )
        at = parse_date(item["date"])
        ref = item.get("ref")
        platform_ref = (
            str(ref) if ref not in (None, "") else sha256_text(f"{item['fileName']}|{at}")[:16]
        )
        out.append(
            PlatformSubmission(
                platform_ref=platform_ref,
                file_name=str(item["fileName"]),
                at=at,
                description=redact(str(item.get("description") or "")),
                public=parse_score(item.get("publicScore"), "publicScore"),
                private=parse_score(item.get("privateScore"), "privateScore"),
                status=str(item.get("status") or ""),
                submitted_by=str(item["submittedBy"]) if item.get("submittedBy") else None,
            )
        )
    return out, token


class KagglePlatform:
    name = "kaggle"

    def upload(
        self,
        staged: Staged,
        artifact: Path | None,
        message: str,
        profile: PlatformProfile,
        runner: Runner | None,
    ) -> UploadResult:
        base, run = _command(profile, runner)
        cmd = [*base, "competitions", "submit"]
        if staged.artifact.kind == "kernel":
            cmd += ["-k", str(staged.artifact.kernel), "-v", str(staged.artifact.version)]
            cmd += ["-f", str(staged.artifact.output)]
        else:
            cmd += ["-f", str(artifact)]
        cmd += ["-m", message, "-q", str(profile.competition)]
        proc = run(cmd)
        if proc.returncode != 0:
            raise _failed(proc)
        return UploadResult(
            confirmed=SUCCESS in proc.stdout.lower(),
            platform_ref=None,
            detail=_last_line(proc.stdout),
        )

    def list_submissions(
        self, profile: PlatformProfile, runner: Runner | None
    ) -> list[PlatformSubmission]:
        base, run = _command(profile, runner)
        cmd = [*base, "competitions", "submissions", "--format", "json", "--page-size", PAGE_SIZE]
        cmd.append(str(profile.competition))
        out: list[PlatformSubmission] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            proc = run(cmd + (["--page-token", token] if token else []))
            if proc.returncode != 0:
                raise _failed(proc)
            page, token = parse_submissions(proc.stdout)
            out.extend(page)
            if not token:
                return out
        raise PlatformError(f"kaggle CLI kept paging past {MAX_PAGES} pages")
```

`__init__.py`：

```python
"""Platform registry. Importing this package registers the built-in platforms."""

from vcp.submit.platforms.base import (
    PLATFORMS,
    Platform,
    PlatformSubmission,
    Runner,
    UploadResult,
    default_runner,
    get_platform,
    redact,
    register_platform,
)
from vcp.submit.platforms.kaggle import KagglePlatform
from vcp.submit.platforms.manual import ManualPlatform

register_platform(ManualPlatform())
register_platform(KagglePlatform())

__all__ = [
    "PLATFORMS",
    "KagglePlatform",
    "ManualPlatform",
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

- [ ] **Step 6: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/submit/test_platforms.py tests/unit/core -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/core/errors.py src/vcp/submit/platforms tests/unit/submit/test_platforms.py
git commit -m "feat(submit): 平台登記表與 redact；manual 平台；kaggle 經 CLI 上傳（含 kernel）與 submissions 回讀（翻頁、分數與時間解析）" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---
### Task 9: 守衛、`stage`、`verify` 與其 CLI

**Files:**
- Create: `src/vcp/submit/guards.py`、`src/vcp/submit/stage.py`
- Modify: `src/vcp/cli_submit.py`（加 `stage`、`verify`）
- Test: `tests/unit/submit/test_guards.py`、`tests/unit/submit/test_stage.py`、`tests/unit/test_cli_submit.py`（追加）

**Interfaces:**
- Consumes: Task 1–8 全部；`vcp.measure.plugins.load_plugins`、`vcp.measure.predictions.read_predictions`、`vcp.measure.runs.verify_prediction` / `assert_run_matches`、`vcp.core.hashing.md5_file`、`vcp.__version__`。
- Produces: `guards.assert_unlocked(ledger, *, submission_id=None)`、`guards.assert_before_deadline(profile, at)`、`guards.QuotaState(used, per_day, window)`（`.remaining`、`.fields(tz_name)`）、`guards.quota_state(ledger, profile, at) -> QuotaState | None`、`guards.assert_quota(state, tz_name)`；`stage.StageSpec`、`stage.StageResult(staged, path, warnings)`、`stage.stage(spec)`、`stage.stage_json(paths, id)`、`stage.load_staged(paths, id) -> Staged`、`stage.verify(dataset, submission_id, *, data_root, configs_root) -> list[str]`、`STAGE_FILE`、`MAX_ID_LENGTH = 31`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/submit/test_guards.py`

```python
from datetime import timedelta

import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.submit.guards import assert_before_deadline, assert_quota, assert_unlocked, quota_state
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.schema import LedgerRow, PlatformProfile, Quota

T0 = "2026-09-05T00:00:00.000Z"


def _profile(**over):
    base = dict(
        dataset="t",
        eval_dataset="d",
        plan_id="p",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        quota=Quota(per_day=2, day_tz="UTC"),
        created_at=T0,
    )
    return PlatformProfile(**{**base, **over})


def _uploaded(sid, at):
    return LedgerRow(
        event="uploaded",
        ts=at,
        submission_id=sid,
        at=at,
        source="manual",
        confirmed=True,
        profile_sha256="p" * 64,
    )


def test_lock_allows_only_the_chosen_after_final(tmp_path):
    led = SubmissionLedger(tmp_path / "s.jsonl")
    assert_unlocked(led)
    led.append(LedgerRow(event="lock", ts=T0, reason="r0"))
    with pytest.raises(ValidationFailed, match="locked: since") as ei:
        assert_unlocked(led)
    assert ei.value.fields == {"since": T0, "why": "r0"}
    with pytest.raises(ValidationFailed, match="locked"):
        assert_unlocked(led, submission_id="S1")
    led.append(
        LedgerRow(
            event="final",
            ts=T0,
            rule="best_sealed",
            slots=1,
            chosen=["S1"],
            table=[],
            metric="accuracy",
            params={},
            holdout_unseals=0,
            profile_sha256="p" * 64,
        )
    )
    assert_unlocked(led, submission_id="S1")
    with pytest.raises(ValidationFailed, match="locked"):
        assert_unlocked(led, submission_id="S2")


def test_deadline():
    p = _profile(deadline="2026-09-05T12:00:00Z")
    assert_before_deadline(p, parse_stamp("2026-09-05T11:59:59Z"))
    with pytest.raises(ValidationFailed, match="past_deadline") as ei:
        assert_before_deadline(p, parse_stamp("2026-09-05T12:00:00Z"))
    assert ei.value.fields == {"deadline": "2026-09-05T12:00:00Z"}
    assert_before_deadline(_profile(), parse_stamp("2999-01-01T00:00:00Z"))


def test_quota_counts_uploaded_and_foreign(tmp_path):
    led = SubmissionLedger(tmp_path / "s.jsonl")
    now = utc_now()
    assert quota_state(led, _profile(quota=None), now) is None
    state = quota_state(led, _profile(), now)
    assert state.used == 0 and state.remaining == 2
    led.append(_uploaded("S1", stamp(now)))
    led.append(
        LedgerRow(event="foreign", ts=stamp(now), platform_ref="f", file_name="x", at=stamp(now))
    )
    led.append(_uploaded("S0", stamp(now - timedelta(days=2))))
    state = quota_state(led, _profile(), now)
    assert state.used == 2 and state.remaining == 0
    with pytest.raises(ValidationFailed, match="quota_exhausted") as ei:
        assert_quota(state, "Asia/Taipei")
    assert ei.value.fields["quota"] == "2/2" and ei.value.fields["resets_at"].endswith("Z")
    assert "CST" in ei.value.fields["local"]
    assert_quota(None, "UTC")
```

- [ ] **Step 2: 寫 `src/vcp/submit/guards.py`**

```python
"""Lock, deadline and quota checks shared by stage / upload / record / final (spec 6.2, 6.5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vcp.core.errors import ValidationFailed
from vcp.core.time import parse_stamp, stamp
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.schema import PlatformProfile
from vcp.submit.timewin import Window, local_text, used_in, window_for


def assert_unlocked(ledger: SubmissionLedger, *, submission_id: str | None = None) -> None:
    """Refuse while locked -- except an upload of a submission the final pick chose (plan
    decision 3): after ``final``, re-sending the chosen file is the whole point."""
    lock = ledger.lock_state()
    if lock is None:
        return
    if submission_id is not None:
        final = ledger.latest_final()
        if final is not None and final.chosen and submission_id in final.chosen:
            return
    raise ValidationFailed(
        f"locked: since {lock.ts} ({lock.reason})",
        fields={"since": lock.ts, "why": str(lock.reason)},
    )


def assert_before_deadline(profile: PlatformProfile, at: datetime) -> None:
    if profile.deadline is not None and at >= parse_stamp(profile.deadline):
        raise ValidationFailed(
            f"past_deadline: {profile.deadline}", fields={"deadline": profile.deadline}
        )


@dataclass(frozen=True)
class QuotaState:
    used: int
    per_day: int
    window: Window

    @property
    def remaining(self) -> int:
        return max(self.per_day - self.used, 0)

    def fields(self, tz_name: str) -> dict[str, str]:
        return {
            "quota": f"{self.used}/{self.per_day}",
            "resets_at": stamp(self.window.end),
            "local": local_text(self.window.end, tz_name),
        }


def quota_state(
    ledger: SubmissionLedger, profile: PlatformProfile, at: datetime
) -> QuotaState | None:
    if profile.quota is None:
        return None
    window = window_for(at, profile.quota)
    ats = [parse_stamp(r.at) for r in ledger.arrivals() if r.at]
    return QuotaState(used_in(window, ats), profile.quota.per_day, window)


def assert_quota(state: QuotaState | None, tz_name: str) -> None:
    if state is not None and state.used >= state.per_day:
        raise ValidationFailed(
            f"quota_exhausted: {state.used}/{state.per_day} uploads in the window ending "
            f"{stamp(state.window.end)}",
            fields=state.fields(tz_name),
        )
```

- [ ] **Step 3: 寫失敗的測試** `tests/unit/submit/test_stage.py`

```python
import json

import pytest
from submit_fixtures import (
    EVAL,
    TEST,
    STAMP,
    ingest_run,
    random_scores,
    seed_eval_runs,
    seed_judgements,
    seed_test_runs,
)

from vcp.core.errors import IntegrityError, PlanMismatchError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.measure.runs import load_run, save_run
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import init_profile
from vcp.submit.schema import LedgerRow, PlatformProfile, Quota
from vcp.submit.stage import StageSpec, load_staged, stage, verify


def _profile(**over) -> PlatformProfile:
    base = dict(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        writer_opts={"id_field": "view_stem"},
        quota=Quota(per_day=3, day_tz="Asia/Taipei"),
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


@pytest.fixture
def ready(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(_profile(), data_root=pair.roots.data, configs_root=pair.roots.configs)
    seed_test_runs(pair)
    return pair


def _spec(pair, sid, eval_run, test_run, **over) -> StageSpec:
    return StageSpec(
        dataset=TEST,
        submission_id=sid,
        eval_run=eval_run,
        test_run=test_run,
        data_root=pair.roots.data,
        configs_root=pair.roots.configs,
        **over,
    )


def test_stage_candidate_writes_file_snapshot_and_row(ready):
    res = stage(_spec(ready, "S1", "good", "good.test"))
    st = res.staged
    assert res.warnings == ["config_hash unchecked (missing on one side)"]
    assert st.kind == "candidate" and st.gate.admission == "PASS"
    assert st.gate.judgements == ["p-good"] and st.pairing.mode == "single"
    art = res.path / "submission.csv"
    assert art.is_file() and sha256_file(art) == st.artifact.sha256
    assert st.artifact.rows == 50 and st.artifact.samples == 50 and st.artifact.missing == 0
    assert st.artifact.writer_opts == {"id_field": "view_stem"}
    header = art.read_text(encoding="utf-8").splitlines()[0]
    assert header == "id,cat,dog,bird"
    assert load_staged(ready.test_paths, "S1") == st
    rows = SubmissionLedger(ready.test_paths.submissions_log).rows
    assert len(rows) == 1 and rows[0].event == "staged" and rows[0].sha256 == st.artifact.sha256
    assert rows[0].profile_sha256 == st.profile_sha256
    with pytest.raises(ValidationFailed, match="exists"):
        stage(_spec(ready, "S1", "good", "good.test"))


def test_failed_stage_leaves_nothing_behind(ready):
    with pytest.raises(ValidationFailed, match="not_admitted"):
        stage(_spec(ready, "S2", "bad", "bad.test"))
    with pytest.raises(ValidationFailed, match="identity: weights_hash differs"):
        stage(_spec(ready, "S3", "bad", "bad.mismatch", kind="baseline", reason="x"))
    with pytest.raises(ValidationFailed, match="reason_required"):
        stage(_spec(ready, "S4", "bad", "bad.test", kind="probe"))
    with pytest.raises(PlanMismatchError):
        stage(_spec(ready, "S5", "good.test", "good.test"))
    assert not ready.test_paths.submit_dir.exists()
    assert not ready.test_paths.submissions_log.exists()


def test_probe_and_baseline(ready):
    probe = stage(_spec(ready, "P1", "bad", "bad.mismatch", kind="probe", reason="explore"))
    assert probe.staged.pairing.checks == ["identity=skipped"]
    assert probe.staged.gate.admission == "waived" and probe.staged.gate.reason == "explore"
    base = stage(_spec(ready, "B1", "bad", "bad.test", kind="baseline", reason="first"))
    assert base.staged.gate.admission == "waived" and base.staged.pairing.mode == "single"


def test_trained_on_sealed_is_refused(ready):
    card = load_run(ready.roots.data, "good")
    save_run(ready.roots.data, card.model_copy(update={"trained_on": ["train", "holdout"]}))
    with pytest.raises(ValidationFailed, match="trained_on_sealed"):
        stage(_spec(ready, "S1", "good", "good.test"))
    save_run(ready.roots.data, card)


def test_locked_and_deadline_and_long_id(ready):
    led = SubmissionLedger(ready.test_paths.submissions_log)
    led.append(LedgerRow(event="lock", ts=STAMP, reason="r0"))
    with pytest.raises(ValidationFailed, match="locked"):
        stage(_spec(ready, "S1", "good", "good.test"))
    led.append(LedgerRow(event="unlock", ts=STAMP, reason="go"))
    with pytest.raises(ValidationFailed, match="longer than 31"):
        stage(_spec(ready, "S" * 32, "good", "good.test"))
    ready.test_paths.submit_yaml.unlink()
    init_profile(
        _profile(deadline="2020-01-01T00:00:00Z"),
        data_root=ready.roots.data,
        configs_root=ready.roots.configs,
    )
    with pytest.raises(ValidationFailed, match="past_deadline"):
        stage(_spec(ready, "S1", "good", "good.test"))


def test_kernel_stage(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(
        _profile(platform="kaggle", competition="c1", submission_kind="kernel", writer=None),
        data_root=pair.roots.data,
        configs_root=pair.roots.configs,
    )
    res = stage(_spec(pair, "K1", "good", None, kernel="u/nb", version=3))
    st = res.staged
    assert st.test_run is None and st.pairing.mode == "kernel"
    assert st.artifact.kind == "kernel" and st.artifact.weights[0].run == "good"
    assert (res.path / "stage.json").is_file() and not (res.path / "submission.csv").exists()
    with pytest.raises(ValidationFailed, match="kernel submissions need"):
        stage(_spec(pair, "K2", "good", None))
    assert verify(TEST, "K1", data_root=pair.roots.data, configs_root=pair.roots.configs) == [
        "weights:good=ok"
    ]


def test_verify_rebuilds_and_detects_tampering(ready):
    res = stage(_spec(ready, "S1", "good", "good.test"))
    checks = verify(TEST, "S1", data_root=ready.roots.data, configs_root=ready.roots.configs)
    assert checks[0].startswith("sha256=") and "rebuild=ok" in checks
    art = res.path / "submission.csv"
    art.write_bytes(art.read_bytes() + b"x")
    with pytest.raises(IntegrityError, match="artifact sha256"):
        verify(TEST, "S1", data_root=ready.roots.data, configs_root=ready.roots.configs)
    art.write_bytes(art.read_bytes()[:-1])
    ingest_run(
        ready,
        "good.test",
        ready.test_ds,
        "all-v1",
        "test",
        random_scores(list(ready.test_ds.samples), seed=99),
        weights=ready.weights["good"],
        replace=True,
    )
    with pytest.raises(IntegrityError, match="rebuilt artifact"):
        verify(TEST, "S1", data_root=ready.roots.data, configs_root=ready.roots.configs)
    with pytest.raises(ValidationFailed, match="not_staged"):
        verify(TEST, "S9", data_root=ready.roots.data, configs_root=ready.roots.configs)


def test_stage_json_is_plain_json(ready):
    res = stage(_spec(ready, "S1", "good", "good.test"))
    doc = json.loads((res.path / "stage.json").read_text(encoding="utf-8"))
    assert doc["submission_id"] == "S1" and doc["artifact"]["writer"] == "scores_csv"
```

- [ ] **Step 4: 寫 `src/vcp/submit/stage.py`**

```python
"""``vcp submit stage`` / ``verify`` (spec 6.1): every check, then the file, then the ledger row.

A failed stage leaves nothing: no directory, no row. The three verifications of an artifact's
bytes live here too -- hashed at stage, re-hashed before an upload (actions.py), and re-rendered
from the test-side run by ``verify``.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from vcp import __version__
from vcp.core.errors import IntegrityError, PlanMismatchError, ValidationFailed
from vcp.core.hashing import md5_file, sha256_file
from vcp.core.paths import DatasetPaths, validate_name
from vcp.core.time import stamp, utc_now
from vcp.data.dataset import Dataset
from vcp.data.split import SplitPlan, load_plan
from vcp.fuse.build import load_record
from vcp.measure.predictions import read_predictions
from vcp.measure.runs import assert_run_matches, load_run, verify_prediction
from vcp.measure.schema import RunCard
from vcp.submit.gate import admit
from vcp.submit.guards import assert_before_deadline, assert_unlocked
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.pairing import UNCHECKED, is_fusion, verify_pairing, verify_weights
from vcp.submit.profile import load_profile
from vcp.submit.schema import (
    Artifact,
    CandidateKind,
    LedgerRow,
    Pairing,
    PlatformProfile,
    Staged,
    WeightRef,
)
from vcp.submit.writers import writer_for
from vcp.submit.writers.base import WriteContext

STAGE_FILE = "stage.json"
MAX_ID_LENGTH = 31


class StageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    submission_id: str
    eval_run: str
    test_run: str | None = None
    kind: CandidateKind = "candidate"
    reason: str | None = None
    kernel: str | None = None
    version: int | None = None
    output: str = "submission.csv"
    weights: list[str] = Field(default_factory=list)
    writer_opts: dict[str, str] = Field(default_factory=dict)
    data_root: Path | None = None
    configs_root: Path | None = None


@dataclass(frozen=True)
class StageResult:
    staged: Staged
    path: Path
    warnings: list[str]


def stage_json(paths: DatasetPaths, submission_id: str) -> Path:
    return paths.submission_dir(submission_id) / STAGE_FILE


def load_staged(paths: DatasetPaths, submission_id: str) -> Staged:
    path = stage_json(paths, submission_id)
    if not path.is_file():
        raise ValidationFailed(f"not_staged: {path} does not exist", fields={"id": submission_id})
    try:
        return Staged.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValidationFailed(f"bad stage.json: {e}", location=str(path)) from e


def _run_on(data_root: Path, run_id: str, dataset: Dataset, plan_id: str, side: str) -> RunCard:
    card = load_run(data_root, run_id)
    assert_run_matches(card, dataset)
    if card.plan_id != plan_id:
        raise PlanMismatchError(
            f"run {run_id!r} uses plan {card.plan_id!r}; the profile's {side} plan is {plan_id!r}",
            fields={"side": side, "run": run_id},
        )
    return card


def _file_pairing(
    spec: StageSpec,
    profile: PlatformProfile,
    data_root: Path,
    eval_card: RunCard,
    test_card: RunCard,
) -> Pairing:
    if spec.kind == "probe":
        mode = "fusion" if is_fusion(data_root, test_card.run_id) else "single"
        return Pairing(mode=mode, checks=["identity=skipped"])
    return verify_pairing(data_root, eval_card, test_card, test_subset=profile.test_subset)


def _render(
    spec: StageSpec,
    profile: PlatformProfile,
    paths: DatasetPaths,
    test_ds: Dataset,
    test_plan: SplitPlan,
    test_card: RunCard,
    warnings: list[str],
) -> tuple[Artifact, Path]:
    """The submission file, written into a temporary directory that becomes the submission
    directory only once everything else has succeeded."""
    writer = writer_for(profile.writer or "", test_ds.card.task)
    options = {**profile.writer_opts, **spec.writer_opts}
    preds = read_predictions(verify_prediction(paths.data_root, test_card, profile.test_subset))
    samples = test_ds.subset(profile.test_subset, test_plan, paths=paths)
    tmp = paths.submit_dir / f".tmp-{spec.submission_id}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    out = tmp / writer.file_name
    try:
        res = writer.write(preds, WriteContext(test_ds, samples, options, out))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if res.missing:
        warnings.append(f"{len(res.missing)} samples without a prediction (allow_missing)")
    artifact = Artifact(
        kind="file",
        path=writer.file_name,
        bytes=out.stat().st_size,
        sha256=sha256_file(out),
        md5=md5_file(out),
        writer=writer.name,
        writer_version=writer.version,
        writer_opts=options,
        rows=res.rows,
        samples=res.samples,
        missing=len(res.missing),
    )
    return artifact, tmp


def stage(spec: StageSpec) -> StageResult:
    paths = DatasetPaths.resolve(
        spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    profile, profile_sha = load_profile(paths)
    ledger = SubmissionLedger(paths.submissions_log)
    assert_unlocked(ledger)
    assert_before_deadline(profile, utc_now())
    validate_name(spec.submission_id)
    if len(spec.submission_id) > MAX_ID_LENGTH:
        raise ValidationFailed(
            f"submission id {spec.submission_id!r} is longer than {MAX_ID_LENGTH} characters "
            "(sync matches ids inside platform descriptions, and redaction hides anything longer)"
        )
    if paths.submission_dir(spec.submission_id).exists() or ledger.staged(spec.submission_id):
        raise ValidationFailed(
            f"exists: submission {spec.submission_id!r} is already staged",
            fields={"id": spec.submission_id},
        )
    eval_paths = DatasetPaths.resolve(
        profile.eval_dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    eval_ds = Dataset.load(
        profile.eval_dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    eval_plan = load_plan(eval_paths, profile.plan_id)
    if eval_plan.subset(profile.sealed_subset).role != "sealed":
        raise ValidationFailed(
            f"sealed_subset: {profile.sealed_subset!r} is not sealed in plan {profile.plan_id!r}"
        )
    eval_card = _run_on(paths.data_root, spec.eval_run, eval_ds, profile.plan_id, "eval")
    if spec.kind == "candidate" and profile.sealed_subset in eval_card.trained_on:
        raise ValidationFailed(
            f"trained_on_sealed: {spec.eval_run!r} trained on {profile.sealed_subset!r}; it can "
            "never have a clean sealed reading",
            fields={"run": spec.eval_run},
        )
    warnings: list[str] = []
    test_ds = Dataset.load(
        profile.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    test_plan = load_plan(paths, profile.test_plan)
    test_card: RunCard | None = None
    weights: list[WeightRef] = []
    if profile.submission_kind == "file":
        if spec.test_run is None:
            raise ValidationFailed(
                "test_run required: file submissions are rendered from a test-side run"
            )
        test_card = _run_on(paths.data_root, spec.test_run, test_ds, profile.test_plan, "test")
        verify_prediction(paths.data_root, test_card, profile.test_subset)
        pairing = _file_pairing(spec, profile, paths.data_root, eval_card, test_card)
    else:
        if spec.kernel is None or spec.version is None:
            raise ValidationFailed("kernel submissions need --kernel and --version")
        pairing, weights = verify_weights(paths.data_root, spec.eval_run, spec.weights)
    if UNCHECKED in pairing.checks:
        warnings.append("config_hash unchecked (missing on one side)")
    fuse = (
        load_record(paths.data_root, spec.eval_run)
        if is_fusion(paths.data_root, spec.eval_run)
        else None
    )
    gate = admit(paths.data_root, eval_paths, eval_card, fuse, spec.kind, spec.reason)
    final_dir = paths.submission_dir(spec.submission_id)
    if test_card is not None:
        artifact, tmp = _render(spec, profile, paths, test_ds, test_plan, test_card, warnings)
        tmp.rename(final_dir)
    else:
        artifact = Artifact(
            kind="kernel",
            kernel=spec.kernel,
            version=spec.version,
            output=spec.output,
            weights=weights,
        )
        final_dir.mkdir(parents=True)
    staged = Staged(
        submission_id=spec.submission_id,
        dataset=spec.dataset,
        kind=spec.kind,
        eval_run=spec.eval_run,
        test_run=spec.test_run if test_card is not None else None,
        pairing=pairing,
        artifact=artifact,
        gate=gate,
        profile_sha256=profile_sha,
        staged_at=stamp(),
        vcp_version=__version__,
    )
    try:
        text = json.dumps(staged.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        (final_dir / STAGE_FILE).write_text(text, encoding="utf-8", newline="\n")
        ledger.append(
            LedgerRow(
                event="staged",
                ts=stamp(),
                submission_id=spec.submission_id,
                kind=spec.kind,
                eval_run=spec.eval_run,
                test_run=staged.test_run,
                sha256=artifact.sha256,
                md5=artifact.md5,
                gate=gate,
                profile_sha256=profile_sha,
            )
        )
    except BaseException:
        shutil.rmtree(final_dir, ignore_errors=True)
        raise
    return StageResult(staged, final_dir, warnings)


def verify(
    dataset: str, submission_id: str, *, data_root: Path | None, configs_root: Path | None
) -> list[str]:
    """The third verification: the bytes on disk, re-rendered from the test-side run."""
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    staged = load_staged(paths, submission_id)
    checks: list[str] = []
    if staged.artifact.kind == "kernel":
        for ref in staged.artifact.weights:
            have = load_run(paths.data_root, ref.run).source.weights_hash
            if have != ref.sha256:
                raise IntegrityError(
                    f"weights_hash of {ref.run!r} is now {(have or 'none')[:12]}, staged "
                    f"{ref.sha256[:12]}",
                    fields={"run": ref.run},
                )
            checks.append(f"weights:{ref.run}=ok")
        return checks
    path = paths.submission_dir(submission_id) / str(staged.artifact.path)
    if not path.is_file():
        raise IntegrityError(f"artifact missing: {path}")
    actual = sha256_file(path)
    if actual != staged.artifact.sha256:
        raise IntegrityError(
            f"artifact sha256 {actual[:12]} != staged {str(staged.artifact.sha256)[:12]}",
            location=str(path),
        )
    checks.append(f"sha256={actual[:12]}")
    test_ds = Dataset.load(profile.dataset, data_root=data_root, configs_root=configs_root)
    test_plan = load_plan(paths, profile.test_plan)
    test_card = load_run(paths.data_root, str(staged.test_run))
    writer = writer_for(str(staged.artifact.writer), test_ds.card.task)
    if writer.version != staged.artifact.writer_version:
        raise ValidationFailed(
            f"writer {writer.name!r} is now version {writer.version}; the artifact was written "
            f"by version {staged.artifact.writer_version}, so its bytes cannot be reproduced"
        )
    preds = read_predictions(verify_prediction(paths.data_root, test_card, profile.test_subset))
    samples = test_ds.subset(profile.test_subset, test_plan, paths=paths)
    with tempfile.TemporaryDirectory(dir=paths.submit_dir) as tmp:
        out = Path(tmp) / writer.file_name
        writer.write(preds, WriteContext(test_ds, samples, dict(staged.artifact.writer_opts), out))
        rebuilt = sha256_file(out)
    if rebuilt != staged.artifact.sha256:
        raise IntegrityError(
            f"rebuilt artifact sha256 {rebuilt[:12]} != staged {str(staged.artifact.sha256)[:12]}"
        )
    checks.append("rebuild=ok")
    if is_fusion(paths.data_root, test_card.run_id):
        build = load_record(paths.data_root, test_card.run_id).subsets.get(profile.test_subset)
        entry = test_card.predictions.get(profile.test_subset)
        if build is None or entry is None or build.output_sha256 != entry.sha256:
            raise IntegrityError(
                f"fuse.json output sha != run.yaml predictions sha for {test_card.run_id!r}",
                fields={"run": test_card.run_id},
            )
        checks.append("fusion_output=ok")
    return checks
```

- [ ] **Step 5: CLI `stage` 與 `verify`**（追加到 `src/vcp/cli_submit.py`；import 加 `from vcp.measure.plugins import load_plugins`、`from vcp.submit.stage import StageSpec, stage, verify`、`from vcp.core.log import Status`）

```python
@submit_app.command("stage")
def stage_cmd(
    dataset: DatasetOpt,
    submission_id: IdOpt,
    eval_run: Annotated[str, typer.Option("--eval-run", help="eval-side run (judged)")],
    test_run: Annotated[
        str | None, typer.Option("--test-run", help="test-side run the file is rendered from")
    ] = None,
    kind: Annotated[str, typer.Option("--kind", help="candidate | baseline | probe")] = "candidate",
    reason: Annotated[
        str | None, typer.Option("--reason", help="required for baseline / probe")
    ] = None,
    kernel: Annotated[
        str | None, typer.Option("--kernel", help="kernel submissions: user/notebook")
    ] = None,
    version: Annotated[int | None, typer.Option("--version", help="kernel version")] = None,
    output: Annotated[
        str, typer.Option("--output", help="kernel output file name")
    ] = "submission.csv",
    weights: Annotated[
        list[str] | None,
        typer.Option("--weights", help="RUN[:sha] the kernel loads (repeatable)"),
    ] = None,
    writer_opt: Annotated[
        list[str] | None, typer.Option("--writer-opt", help="writer option key=value")
    ] = None,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Pair, gate, render and record a candidate -- every check before any write."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        try:
            spec = StageSpec(
                dataset=dataset,
                submission_id=submission_id,
                eval_run=eval_run,
                test_run=test_run,
                kind=kind,  # type: ignore[arg-type]
                reason=reason,
                kernel=kernel,
                version=version,
                output=output,
                weights=list(weights or []),
                writer_opts=parse_opts(writer_opt, "--writer-opt"),
                data_root=data_root,
                configs_root=configs_root,
            )
        except ValidationError as e:
            raise ValidationFailed(str(e), location="vcp submit stage") from e
        res = stage(spec)
        st = res.staged
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "id": submission_id,
            "kind": st.kind,
            "eval_run": st.eval_run,
            "pairing": st.pairing.mode,
            "admission": st.gate.admission,
        }
        if st.test_run:
            fields["test_run"] = st.test_run
        if st.artifact.sha256:
            fields["sha256"] = st.artifact.sha256[:12]
        if st.artifact.writer:
            fields["writer"] = st.artifact.writer
            fields["rows"] = st.artifact.rows or 0
        human = [f"staged {submission_id} -> {res.path}"]
        human += [f"check: {c}" for c in st.pairing.checks]
        human += [f"warning: {w}" for w in res.warnings]
        status: Status = "WARN" if res.warnings else "OK"
        return status, fields, st.model_dump(mode="json"), human

    run_command("submit.stage", json_mode, data_root, fn)


@submit_app.command("verify")
def verify_cmd(
    dataset: DatasetOpt,
    submission_id: IdOpt,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Re-hash the artifact and re-render it from the test-side run (bit-level reproduction)."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        checks = verify(dataset, submission_id, data_root=data_root, configs_root=configs_root)
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "id": submission_id,
            "checks": len(checks),
        }
        return "OK", fields, {"checks": checks}, [f"ok: {c}" for c in checks]

    run_command("submit.verify", json_mode, data_root, fn)
```

- [ ] **Step 6: 追加 CLI 測試到 `tests/unit/test_cli_submit.py`**

```python
def _ready(pair):
    from submit_fixtures import seed_eval_runs, seed_judgements, seed_test_runs

    seed_eval_runs(pair)
    seed_judgements(pair)
    r = runner.invoke(app, _init_args(**{"--writer-opt": "id_field=view_stem"}))
    assert r.exit_code == 0, r.output
    seed_test_runs(pair)


def _stage(sid, eval_run, test_run, *extra):
    return runner.invoke(
        app,
        [
            "submit",
            "stage",
            "--dataset",
            "beach-test",
            "--id",
            sid,
            "--eval-run",
            eval_run,
            "--test-run",
            test_run,
            *extra,
        ],
    )


def test_stage_and_verify_cli(pair):
    _ready(pair)
    r = _stage("S1", "good", "good.test")
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "status=WARN" in v and "admission=PASS" in v and "pairing=single" in v
    assert "rows=50" in v and "writer=scores_csv" in v
    r = _stage("S2", "bad", "bad.test")
    assert r.exit_code == 1 and "not_admitted" in _verdict(r.output)
    r = _stage("S2", "bad", "bad.test", "--kind", "probe", "--reason", "look")
    assert r.exit_code == 0 and "admission=waived" in _verdict(r.output)
    r = _stage("S3", "bad", "bad.test", "--kind", "royal")
    assert r.exit_code == 1 and "status=FAIL" in _verdict(r.output)
    r = runner.invoke(app, ["submit", "verify", "--dataset", "beach-test", "--id", "S1", "--json"])
    assert r.exit_code == 0, r.output
    assert _json(r)["result"]["checks"][1] == "rebuild=ok"
```

- [ ] **Step 7: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/submit/guards.py src/vcp/submit/stage.py src/vcp/cli_submit.py tests/unit/submit/test_guards.py tests/unit/submit/test_stage.py tests/unit/test_cli_submit.py
git commit -m "feat(submit): 守衛（封槍、截止、配額）與 vcp submit stage / verify（四道門、產檔、stage.json、台帳列、位元級重現）" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: `upload`、`record`、`score`、`sync` 與其 CLI

**Files:**
- Create: `src/vcp/submit/actions.py`、`src/vcp/submit/sync.py`
- Modify: `src/vcp/cli_submit.py`
- Test: `tests/unit/submit/test_actions.py`、`tests/unit/submit/test_sync.py`、`tests/unit/test_cli_submit.py`（追加）

**Interfaces:**
- Consumes: Task 8 的 `get_platform` / `Runner` / `UploadResult` / `PlatformSubmission`、Task 9 的 `load_staged` / guards、Task 2 的 `parse_at`。
- Produces: `actions.upload(dataset, submission_id, *, message=None, runner=None, data_root, configs_root) -> UploadOutcome(row, result, quota)`、`actions.record(dataset, submission_id, at_text, *, tz="platform", platform_ref=None, message=None, data_root, configs_root) -> RecordOutcome(row, quota, warnings)`、`actions.score(dataset, submission_id, *, public=None, private=None, data_root, configs_root) -> LedgerRow`；`sync.sync(dataset, *, runner=None, data_root, configs_root) -> SyncResult(platform_rows, scored, foreign, unconfirmed, matched)`、`sync.match_submission(p, ledger, file_names) -> str | None`、`MATCH_WINDOW`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/submit/test_actions.py`

```python
import subprocess
from datetime import timedelta

import pytest
from submit_fixtures import EVAL, TEST, STAMP, seed_eval_runs, seed_judgements, seed_test_runs

from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.time import parse_stamp, utc_now
from vcp.submit.actions import record, score, upload
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile, Quota
from vcp.submit.stage import StageSpec, stage

SECRET = "fakesecretfakesecretfakesecret1234"


def _profile(**over) -> PlatformProfile:
    base = dict(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        quota=Quota(per_day=1, day_tz="UTC"),
        display_tz="Asia/Taipei",
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        code, out, err = self.responses.pop(0)
        return subprocess.CompletedProcess(args, code, out, err)


def _staged(pair, profile) -> None:
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(profile, data_root=pair.roots.data, configs_root=pair.roots.configs)
    seed_test_runs(pair)
    for sid, eval_run, test_run, kind in (
        ("S1", "good", "good.test", "candidate"),
        ("S2", "bad", "bad.test", "baseline"),
    ):
        stage(
            StageSpec(
                dataset=TEST,
                submission_id=sid,
                eval_run=eval_run,
                test_run=test_run,
                kind=kind,
                reason=None if kind == "candidate" else "anchor",
                data_root=pair.roots.data,
                configs_root=pair.roots.configs,
            )
        )


def _kw(pair):
    return {"data_root": pair.roots.data, "configs_root": pair.roots.configs}


def _now_text(offset=timedelta(0)) -> str:
    return (utc_now() + offset).strftime("%Y-%m-%d %H:%M:%S")


def test_record_then_score(pair):
    _staged(pair, _profile())
    out = record(TEST, "S1", _now_text(), tz="utc", platform_ref="web-7", **_kw(pair))
    assert out.row.source == "manual" and out.row.platform_ref == "web-7" and out.row.confirmed
    assert out.quota.used == 1 and out.warnings == []
    at = parse_stamp(out.row.at)
    assert abs((utc_now() - at).total_seconds()) < 5
    out = record(TEST, "S2", _now_text(), tz="utc", **_kw(pair))
    assert out.warnings and out.warnings[0].startswith("quota_overflow: 1/1")
    row = score(TEST, "S1", public=0.79, **_kw(pair))
    assert row.event == "scored" and row.source == "manual" and row.public == 0.79
    with pytest.raises(ValidationFailed, match="not_uploaded"):
        score(TEST, "S9", public=0.1, **_kw(pair))
    with pytest.raises(ValidationFailed, match="public or private"):
        score(TEST, "S1", **_kw(pair))
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert [r.event for r in led.rows] == ["staged", "staged", "uploaded", "uploaded", "scored"]


def test_record_time_rules(pair):
    _staged(pair, _profile())
    with pytest.raises(ValidationFailed, match="in the future"):
        record(TEST, "S1", _now_text(timedelta(hours=2)), tz="utc", **_kw(pair))
    with pytest.raises(ValidationFailed, match="before the submission was staged"):
        record(TEST, "S1", "2020-01-01 00:00", tz="utc", **_kw(pair))
    with pytest.raises(ValidationFailed, match="--tz must be"):
        record(TEST, "S1", _now_text(), tz="local", **_kw(pair))
    with pytest.raises(ValidationFailed, match="--at must be"):
        record(TEST, "S1", "now", tz="utc", **_kw(pair))
    taipei = (utc_now() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    out = record(TEST, "S1", taipei, **_kw(pair))
    assert abs((utc_now() - parse_stamp(out.row.at)).total_seconds()) < 5


def test_record_refuses_changed_artifact(pair):
    _staged(pair, _profile())
    art = pair.test_paths.submission_dir("S1") / "submission.csv"
    art.write_bytes(art.read_bytes() + b"x")
    with pytest.raises(IntegrityError, match="artifact sha256"):
        record(TEST, "S1", _now_text(), tz="utc", **_kw(pair))
    assert [r.event for r in SubmissionLedger(pair.test_paths.submissions_log).rows] == [
        "staged",
        "staged",
    ]


def test_upload_kaggle_with_quota(pair):
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
    runner = FakeRunner([(0, f"KAGGLE_KEY={SECRET}\nSuccessfully submitted to c1", "")])
    out = upload(TEST, "S1", message="first", runner=runner, **_kw(pair))
    assert out.row.source == "vcp" and out.row.confirmed and out.row.message == "S1 first"
    assert out.result.confirmed and SECRET not in out.result.detail
    assert runner.calls[0][-4:] == ["-m", "S1 first", "-q", "c1"]
    assert out.quota.used == 1
    with pytest.raises(ValidationFailed, match="quota_exhausted") as ei:
        upload(TEST, "S2", runner=FakeRunner([]), **_kw(pair))
    assert ei.value.fields["quota"] == "1/1"
    text = pair.test_paths.submissions_log.read_text(encoding="utf-8")
    assert SECRET not in text and text.count("uploaded") == 1


def test_upload_failures_write_no_row(pair):
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
    runner = FakeRunner([(1, "", f"denied key={SECRET}")])
    with pytest.raises(Exception, match="exit 1") as ei:
        upload(TEST, "S1", runner=runner, **_kw(pair))
    assert SECRET not in str(ei.value)
    runner = FakeRunner([(0, "queued", "")])
    out = upload(TEST, "S1", runner=runner, **_kw(pair))
    assert not out.row.confirmed
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert [r.event for r in led.rows] == ["staged", "staged", "uploaded"]


def test_upload_on_manual_platform_is_refused(pair):
    _staged(pair, _profile())
    with pytest.raises(ValidationFailed, match="manual_platform"):
        upload(TEST, "S1", runner=FakeRunner([]), **_kw(pair))
```

- [ ] **Step 2: 寫失敗的測試** `tests/unit/submit/test_sync.py`

```python
import json
import subprocess
from datetime import timedelta

import pytest
from submit_fixtures import EVAL, TEST, STAMP, seed_eval_runs, seed_judgements, seed_test_runs

from vcp.core.errors import ValidationFailed
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.submit.actions import record
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile
from vcp.submit.stage import StageSpec, stage
from vcp.submit.sync import sync


def _profile(**over) -> PlatformProfile:
    base = dict(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="kaggle",
        competition="c1",
        board_rule="best",
        metric="accuracy",
        writer="scores_csv",
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


class FakeRunner:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(self.rows), "")


def _kw(pair):
    return {"data_root": pair.roots.data, "configs_root": pair.roots.configs}


@pytest.fixture
def staged(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(_profile(), **_kw(pair))
    seed_test_runs(pair)
    for sid, kind in (
        ("S1", "candidate"),
        ("S2", "baseline"),
        ("S3", "baseline"),
        ("S4", "baseline"),
    ):
        stage(
            StageSpec(
                dataset=TEST,
                submission_id=sid,
                eval_run="good" if sid == "S1" else "bad",
                test_run="good.test" if sid == "S1" else "bad.test",
                kind=kind,
                reason=None if kind == "candidate" else "anchor",
                **_kw(pair),
            )
        )
    now = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    record(TEST, "S1", now, tz="utc", **_kw(pair))
    record(TEST, "S2", now, tz="utc", **_kw(pair))
    record(TEST, "S3", now, tz="utc", platform_ref="k33", **_kw(pair))
    record(TEST, "S4", now, tz="utc", **_kw(pair))
    return pair


def test_sync_matches_scores_and_records_foreign(staged):
    at = stamp(utc_now())
    far = stamp(utc_now() - timedelta(hours=3))
    rows = [
        {"ref": 1, "fileName": "x.csv", "date": at, "description": "S1 note", "publicScore": "0.7"},
        {
            "ref": 2,
            "fileName": "submission.csv",
            "date": at,
            "description": "",
            "publicScore": "0.6",
        },
        {"ref": "k33", "fileName": "y.csv", "date": far, "description": "", "publicScore": "0.5"},
        {
            "ref": 4,
            "fileName": "z.csv",
            "date": far,
            "description": "teammate",
            "publicScore": "0.4",
            "submittedBy": "mate",
        },
    ]
    res = sync(TEST, runner=FakeRunner(rows), **_kw(staged))
    assert res.platform_rows == 4 and res.scored == 3 and res.foreign == 1
    assert res.matched == {"1": "S1", "2": "S2", "k33": "S3"} and res.unconfirmed == ["S4"]
    led = SubmissionLedger(staged.test_paths.submissions_log)
    assert led.latest_score("S1").public == 0.7 and led.latest_score("S1").source == "platform"
    assert led.latest_score("S2").public == 0.6 and led.latest_score("S3").public == 0.5
    foreign = led.of("foreign")
    assert len(foreign) == 1 and foreign[0].submitted_by == "mate" and foreign[0].public == 0.4
    again = sync(TEST, runner=FakeRunner(rows), **_kw(staged))
    assert again.scored == 0 and again.foreign == 0
    assert len(SubmissionLedger(staged.test_paths.submissions_log).rows) == len(led.rows)
    rows[0]["privateScore"] = "0.65"
    third = sync(TEST, runner=FakeRunner(rows), **_kw(staged))
    assert third.scored == 1
    assert SubmissionLedger(staged.test_paths.submissions_log).latest_score("S1").private == 0.65


def test_sync_refuses_manual(pair):
    seed_eval_runs(pair)
    init_profile(_profile(platform="manual", competition=None, board_rule="last"), **_kw(pair))
    with pytest.raises(ValidationFailed, match="manual_platform"):
        sync(TEST, runner=FakeRunner([]), **_kw(pair))


def test_match_prefers_ref_then_id_then_file_and_time(staged):
    from vcp.submit.platforms.base import PlatformSubmission
    from vcp.submit.sync import match_submission

    led = SubmissionLedger(staged.test_paths.submissions_log)
    names = {sid: "submission.csv" for sid in led.ids()}
    at = stamp(utc_now())

    def p(ref, desc, file_name="q.csv", when=at):
        return PlatformSubmission(ref, file_name, when, desc, None, None, "complete", None)

    assert match_submission(p("k33", "S1 in text"), led, names) == "S3"
    assert match_submission(p("9", "resend of S1"), led, names) == "S1"
    assert match_submission(p("9", "S10 is not S1"), led, names) is None
    assert match_submission(p("9", "", "submission.csv"), led, names) == "S1"
    assert match_submission(p("9", "", "submission.csv"), led, names, taken={"S1"}) == "S2"
    late = stamp(utc_now() + timedelta(minutes=11))
    assert match_submission(p("9", "", "submission.csv", late), led, names) is None
```

四個候選都以同名檔在同一分鐘內 record：規則 3 依台帳 `ids()` 順序取第一個還沒被本次 sync 配走的（`taken`）。

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/unit/submit/test_actions.py tests/unit/submit/test_sync.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 4: 寫 `src/vcp/submit/actions.py`**

```python
"""``upload`` / ``record`` / ``score`` (spec 6.2): the ledger row is written by the same
command that performs -- or attests to -- the action."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.submit.guards import (
    QuotaState,
    assert_before_deadline,
    assert_quota,
    assert_unlocked,
    quota_state,
)
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.platforms import Runner, UploadResult, get_platform
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow, PlatformProfile, Staged
from vcp.submit.stage import load_staged
from vcp.submit.timewin import parse_at

FUTURE_TOLERANCE = timedelta(seconds=60)


@dataclass(frozen=True)
class Prepared:
    paths: DatasetPaths
    profile: PlatformProfile
    profile_sha: str
    ledger: SubmissionLedger
    staged: Staged
    artifact: Path | None


def _prepare(
    dataset: str, submission_id: str, data_root: Path | None, configs_root: Path | None
) -> Prepared:
    """The staged submission with its artifact re-hashed (the second of the three checks)."""
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, sha = load_profile(paths)
    ledger = SubmissionLedger(paths.submissions_log)
    if ledger.staged(submission_id) is None:
        raise ValidationFailed(
            f"not_staged: {submission_id!r} has no staged row", fields={"id": submission_id}
        )
    staged = load_staged(paths, submission_id)
    artifact: Path | None = None
    if staged.artifact.kind == "file":
        artifact = paths.submission_dir(submission_id) / str(staged.artifact.path)
        if not artifact.is_file():
            raise IntegrityError(f"artifact missing: {artifact}")
        actual = sha256_file(artifact)
        if actual != staged.artifact.sha256:
            raise IntegrityError(
                f"artifact sha256 {actual[:12]} != staged {str(staged.artifact.sha256)[:12]}",
                location=str(artifact),
            )
    return Prepared(paths, profile, sha, ledger, staged, artifact)


@dataclass(frozen=True)
class UploadOutcome:
    row: LedgerRow
    result: UploadResult
    quota: QuotaState | None


def upload(
    dataset: str,
    submission_id: str,
    *,
    message: str | None = None,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> UploadOutcome:
    p = _prepare(dataset, submission_id, data_root, configs_root)
    if p.profile.platform == "manual":
        raise ValidationFailed(
            "manual_platform: this profile has no upload API; upload by hand, then "
            "`vcp submit record`"
        )
    now = utc_now()
    assert_unlocked(p.ledger, submission_id=submission_id)
    assert_before_deadline(p.profile, now)
    tz = p.profile.effective_display_tz()
    assert_quota(quota_state(p.ledger, p.profile, now), tz)
    msg = f"{submission_id} {message}".strip() if message else submission_id
    result = get_platform(p.profile.platform).upload(p.staged, p.artifact, msg, p.profile, runner)
    row = LedgerRow(
        event="uploaded",
        ts=stamp(),
        submission_id=submission_id,
        at=stamp(utc_now()),
        source="vcp",
        platform_ref=result.platform_ref,
        message=msg,
        confirmed=result.confirmed,
        sha256=p.staged.artifact.sha256,
        profile_sha256=p.profile_sha,
    )
    p.ledger.append(row)
    return UploadOutcome(row, result, quota_state(p.ledger, p.profile, parse_stamp(str(row.at))))


@dataclass(frozen=True)
class RecordOutcome:
    row: LedgerRow
    quota: QuotaState | None
    warnings: list[str]


def record(
    dataset: str,
    submission_id: str,
    at_text: str,
    *,
    tz: str = "platform",
    platform_ref: str | None = None,
    message: str | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> RecordOutcome:
    if tz not in ("platform", "utc"):
        raise ValidationFailed(f"--tz must be platform or utc, got {tz!r}")
    p = _prepare(dataset, submission_id, data_root, configs_root)
    tz_name = p.profile.effective_display_tz() if tz == "platform" else "UTC"
    at = parse_at(at_text, tz_name)
    now = utc_now()
    if at > now + FUTURE_TOLERANCE:
        raise ValidationFailed(f"at: {stamp(at)} is in the future (now {stamp(now)})")
    floor = parse_stamp(p.staged.staged_at).replace(microsecond=0)
    if at < floor:
        raise ValidationFailed(
            f"at: {stamp(at)} is before the submission was staged ({p.staged.staged_at})"
        )
    assert_unlocked(p.ledger, submission_id=submission_id)
    assert_before_deadline(p.profile, at)
    warnings: list[str] = []
    state = quota_state(p.ledger, p.profile, at)
    if state is not None and state.used >= state.per_day:
        warnings.append(
            f"quota_overflow: {state.used}/{state.per_day} already in the window ending "
            f"{stamp(state.window.end)}"
        )
    row = LedgerRow(
        event="uploaded",
        ts=stamp(),
        submission_id=submission_id,
        at=stamp(at),
        source="manual",
        platform_ref=platform_ref,
        message=message,
        confirmed=True,
        sha256=p.staged.artifact.sha256,
        profile_sha256=p.profile_sha,
    )
    p.ledger.append(row)
    return RecordOutcome(row, quota_state(p.ledger, p.profile, at), warnings)


def score(
    dataset: str,
    submission_id: str,
    *,
    public: float | None = None,
    private: float | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> LedgerRow:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    load_profile(paths)
    ledger = SubmissionLedger(paths.submissions_log)
    if ledger.staged(submission_id) is None or not ledger.uploads(submission_id):
        raise ValidationFailed(
            f"not_uploaded: {submission_id!r} has no uploaded row to score",
            fields={"id": submission_id},
        )
    if public is None and private is None:
        raise ValidationFailed("a --public or --private score is required")
    try:
        row = LedgerRow(
            event="scored",
            ts=stamp(),
            submission_id=submission_id,
            public=public,
            private=private,
            source="manual",
        )
    except ValidationError as e:
        raise ValidationFailed(str(e)) from e
    ledger.append(row)
    return row
```

- [ ] **Step 5: 寫 `src/vcp/submit/sync.py`**

```python
"""``vcp submit sync`` (spec 6.3): what the platform says happened, reconciled with the ledger.
Uploads the ledger never saw become ``foreign`` rows -- they spent quota too."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, stamp
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.platforms import PlatformSubmission, Runner, get_platform
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow
from vcp.submit.stage import load_staged

MATCH_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True)
class SyncResult:
    platform_rows: int
    scored: int
    foreign: int
    unconfirmed: list[str]
    matched: dict[str, str] = field(default_factory=dict)


def _mentions(description: str, submission_id: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9._-]){re.escape(submission_id)}(?![A-Za-z0-9._-])"
    return re.search(pattern, description) is not None


def match_submission(
    p: PlatformSubmission,
    ledger: SubmissionLedger,
    file_names: dict[str, str],
    taken: set[str] | frozenset[str] = frozenset(),
) -> str | None:
    """Plan decision 8 (a recorded platform ref) first, then spec 6.3's two rules. ``taken``
    holds ids already matched in this sync, so the file-and-time rule moves on to the next
    submission that uploaded the same file name."""
    for r in ledger.of("uploaded"):
        if r.platform_ref and r.platform_ref == p.platform_ref:
            return r.submission_id
    for sid in sorted(ledger.ids(), key=len, reverse=True):
        if _mentions(p.description, sid):
            return sid
    at = parse_stamp(p.at)
    for sid in ledger.ids():
        if sid in taken or file_names.get(sid) != p.file_name:
            continue
        for r in ledger.uploads(sid):
            if r.at and abs(parse_stamp(r.at) - at) <= MATCH_WINDOW:
                return sid
    return None


def sync(
    dataset: str,
    *,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> SyncResult:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    if profile.platform == "manual":
        raise ValidationFailed("manual_platform: nothing to read back from; use `vcp submit score`")
    ledger = SubmissionLedger(paths.submissions_log)
    subs = get_platform(profile.platform).list_submissions(profile, runner)
    file_names: dict[str, str] = {}
    for sid in ledger.ids():
        st = load_staged(paths, sid)
        file_names[sid] = str(
            st.artifact.path if st.artifact.kind == "file" else st.artifact.output
        )
    known_foreign = ledger.foreign_refs()
    matched: dict[str, str] = {}
    scored = foreign = 0
    for p in subs:
        sid = match_submission(p, ledger, file_names, taken=set(matched.values()))
        if sid is None:
            if p.platform_ref in known_foreign:
                continue
            ledger.append(
                LedgerRow(
                    event="foreign",
                    ts=stamp(),
                    platform_ref=p.platform_ref,
                    file_name=p.file_name,
                    at=p.at,
                    public=p.public,
                    private=p.private,
                    platform_status=p.status or None,
                    submitted_by=p.submitted_by,
                )
            )
            known_foreign.add(p.platform_ref)
            foreign += 1
            continue
        matched[p.platform_ref] = sid
        if p.public is None and p.private is None:
            continue
        latest = ledger.latest_score(sid)
        if latest is None or latest.public != p.public or latest.private != p.private:
            ledger.append(
                LedgerRow(
                    event="scored",
                    ts=stamp(),
                    submission_id=sid,
                    public=p.public,
                    private=p.private,
                    source="platform",
                    platform_status=p.status or None,
                )
            )
            scored += 1
    confirmed = set(matched.values())
    unconfirmed = [sid for sid in ledger.ids() if ledger.uploads(sid) and sid not in confirmed]
    return SyncResult(len(subs), scored, foreign, unconfirmed, matched)
```

- [ ] **Step 6: CLI `upload`、`record`、`score`、`sync`**（追加到 `cli_submit.py`；import `from vcp.submit.actions import record, score, upload`、`from vcp.submit.sync import sync`）

```python
@submit_app.command("upload")
def upload_cmd(
    dataset: DatasetOpt,
    submission_id: IdOpt,
    message: Annotated[str | None, typer.Option("--message", help="appended to the id")] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Upload a staged submission through the platform's CLI and record it."""

    def fn() -> CmdResult:
        out = upload(
            dataset, submission_id, message=message, data_root=data_root, configs_root=configs_root
        )
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "id": submission_id,
            "at": str(out.row.at),
            "confirmed": bool(out.row.confirmed),
        }
        if out.quota is not None:
            fields.update(out.quota.fields("UTC"))
        human = [out.result.detail] if out.result.detail else []
        status: Status = "OK" if out.row.confirmed else "WARN"
        return status, fields, out.row.model_dump(mode="json", exclude_none=True), human

    run_command("submit.upload", json_mode, data_root, fn)


@submit_app.command("record")
def record_cmd(
    dataset: DatasetOpt,
    submission_id: IdOpt,
    at: Annotated[str, typer.Option("--at", help="platform time 'YYYY-MM-DD HH:MM[:SS]'")],
    tz: Annotated[str, typer.Option("--tz", help="platform | utc")] = "platform",
    platform_ref: Annotated[str | None, typer.Option("--platform-ref")] = None,
    message: Annotated[str | None, typer.Option("--message")] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Record an upload you made by hand (the ledger row is the upload's receipt)."""

    def fn() -> CmdResult:
        out = record(
            dataset,
            submission_id,
            at,
            tz=tz,
            platform_ref=platform_ref,
            message=message,
            data_root=data_root,
            configs_root=configs_root,
        )
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "id": submission_id,
            "at": str(out.row.at),
        }
        if out.quota is not None:
            fields.update(out.quota.fields("UTC"))
        if out.warnings:
            fields["quota_overflow"] = True
        status: Status = "WARN" if out.warnings else "OK"
        human = [f"warning: {w}" for w in out.warnings]
        return status, fields, out.row.model_dump(mode="json", exclude_none=True), human

    run_command("submit.record", json_mode, data_root, fn)


@submit_app.command("score")
def score_cmd(
    dataset: DatasetOpt,
    submission_id: IdOpt,
    public: Annotated[float | None, typer.Option("--public")] = None,
    private: Annotated[float | None, typer.Option("--private")] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Record a public / private score the platform showed."""

    def fn() -> CmdResult:
        row = score(
            dataset,
            submission_id,
            public=public,
            private=private,
            data_root=data_root,
            configs_root=configs_root,
        )
        fields: dict[str, FieldValue] = {"dataset": dataset, "id": submission_id}
        if row.public is not None:
            fields["public"] = row.public
        if row.private is not None:
            fields["private"] = row.private
        return "OK", fields, row.model_dump(mode="json", exclude_none=True), []

    run_command("submit.score", json_mode, data_root, fn)


@submit_app.command("sync")
def sync_cmd(
    dataset: DatasetOpt,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Read the platform's submission list back into the ledger (scores, foreign uploads)."""

    def fn() -> CmdResult:
        res = sync(dataset, data_root=data_root, configs_root=configs_root)
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "platform_rows": res.platform_rows,
            "scored": res.scored,
            "foreign": res.foreign,
            "unconfirmed": len(res.unconfirmed),
        }
        human = [f"unconfirmed: {sid}" for sid in res.unconfirmed]
        status: Status = "WARN" if res.foreign or res.unconfirmed else "OK"
        payload = {"matched": res.matched, "unconfirmed": res.unconfirmed}
        return status, fields, payload, human

    run_command("submit.sync", json_mode, data_root, fn)
```

- [ ] **Step 7: 追加 CLI 測試到 `tests/unit/test_cli_submit.py`**

```python
def test_record_score_cli(pair):
    from vcp.core.time import utc_now

    _ready(pair)
    assert _stage("S1", "good", "good.test").exit_code == 0
    at = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    base = ["submit", "record", "--dataset", "beach-test", "--id", "S1", "--tz", "utc"]
    r = runner.invoke(app, [*base, "--at", at])
    assert r.exit_code == 0, r.output
    assert "quota=1/3" in _verdict(r.output) and "resets_at=" in _verdict(r.output)
    r = runner.invoke(app, [*base, "--at", "2020-01-01 00:00"])
    assert r.exit_code == 1 and "before the submission" in _verdict(r.output)
    r = runner.invoke(
        app, ["submit", "score", "--dataset", "beach-test", "--id", "S1", "--public", "0.8"]
    )
    assert r.exit_code == 0 and "public=0.8" in _verdict(r.output)
    r = runner.invoke(app, ["submit", "upload", "--dataset", "beach-test", "--id", "S1"])
    assert r.exit_code == 1 and "manual_platform" in _verdict(r.output)
    r = runner.invoke(app, ["submit", "sync", "--dataset", "beach-test"])
    assert r.exit_code == 1 and "manual_platform" in _verdict(r.output)
```

- [ ] **Step 8: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/submit/actions.py src/vcp/submit/sync.py src/vcp/cli_submit.py tests/unit/submit/test_actions.py tests/unit/submit/test_sync.py tests/unit/test_cli_submit.py
git commit -m "feat(submit): upload（Kaggle、配額）、record（手動平台時間換算）、score、sync（配對、foreign、去重）與 CLI" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: `final`、`lock` / `unlock`、`status`、`report` 與其 CLI

**Files:**
- Create: `src/vcp/submit/final.py`、`src/vcp/submit/report.py`
- Modify: `src/vcp/cli_submit.py`
- Test: `tests/unit/submit/test_final.py`、`tests/unit/submit/test_report.py`、`tests/unit/test_cli_submit.py`（追加）

**Interfaces:**
- Consumes: `vcp.measure.ledger.ReadingsLedger`、`vcp.measure.metrics.get_metric` / `effective_params` / `params_key`、`vcp.measure.report.READINGS_LEDGER`、`vcp.measure.measure.measure_run` / `MeasureSpec`（測試）。
- Produces: `final.sealed_reading(readings, card, profile, params_hash, sealed_size) -> tuple[Reading | None, str]`、`final.count_unseals(eval_paths, plan_id, subset) -> int`、`final.final(dataset, *, slots=None, dry_run=False, data_root, configs_root) -> FinalResult(row, chosen, unranked, needs_reupload, warnings, written)`、`final.lock(dataset, reason, ...)` / `final.unlock(...) -> LedgerRow`；`report.status(dataset, ...) -> StatusView(staged, uploaded, foreign, quota, deadline_in_hours, locked, current, unscored)`、`report.report(dataset, ...) -> list[ReportRow(submission_id, at, kind, public, delta, sealed_value, private, shift)]`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/submit/test_final.py`

```python
import pytest
from submit_fixtures import (
    EVAL,
    TEST,
    STAMP,
    ingest_run,
    seed_eval_runs,
    seed_judgements,
    seed_test_runs,
)

from helpers import perfect_predictions
from vcp.core.errors import ValidationFailed
from vcp.core.time import utc_now
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.submit.actions import record, score
from vcp.submit.final import count_unseals, final, lock, unlock
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile
from vcp.submit.stage import StageSpec, stage


def _profile(**over) -> PlatformProfile:
    base = dict(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


def _kw(pair):
    return {"data_root": pair.roots.data, "configs_root": pair.roots.configs}


def _measure_holdout(pair, run):
    measure_run(
        MeasureSpec(
            run_id=run,
            metrics=["accuracy"],
            subsets=["holdout"],
            unseal=True,
            reason="final pick",
            **_kw(pair),
        )
    )


@pytest.fixture
def uploaded(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(_profile(), **_kw(pair))
    seed_test_runs(pair)
    now = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    for sid, eval_run, test_run, kind in (
        ("S1", "good", "good.test", "candidate"),
        ("S2", "bad", "bad.test", "baseline"),
        ("S3", "bad", "bad.test", "probe"),
    ):
        stage(
            StageSpec(
                dataset=TEST,
                submission_id=sid,
                eval_run=eval_run,
                test_run=test_run,
                kind=kind,
                reason=None if kind == "candidate" else "why",
                **_kw(pair),
            )
        )
        record(TEST, sid, now, tz="utc", **_kw(pair))
    score(TEST, "S1", public=0.8, **_kw(pair))
    score(TEST, "S2", public=0.9, **_kw(pair))
    return pair


def test_final_needs_sealed_readings(uploaded):
    with pytest.raises(ValidationFailed, match="no_sealed_readings"):
        final(TEST, **_kw(uploaded))
    assert SubmissionLedger(uploaded.test_paths.submissions_log).of("final") == []


def test_final_picks_by_sealed_not_public(uploaded):
    _measure_holdout(uploaded, "good")
    _measure_holdout(uploaded, "bad")
    dry = final(TEST, dry_run=True, **_kw(uploaded))
    assert dry.chosen == ["S1"] and not dry.written and dry.needs_reupload == "S1"
    led = SubmissionLedger(uploaded.test_paths.submissions_log)
    assert led.of("final") == [] and led.lock_state() is None
    res = final(TEST, **_kw(uploaded))
    assert res.written and res.chosen == ["S1"] and res.unranked == []
    table = {e.submission_id: e for e in res.row.table}
    assert table["S1"].eligible and table["S1"].sealed_value == 1.0 and table["S1"].public == 0.8
    assert table["S2"].eligible and table["S2"].sealed_value < 1.0 and table["S2"].public == 0.9
    assert not table["S3"].eligible and table["S3"].why == "probe"
    assert res.row.holdout_unseals >= 3 and res.row.metric == "accuracy"
    led = SubmissionLedger(uploaded.test_paths.submissions_log)
    assert led.lock_state().reason == "final" and led.latest_final().chosen == ["S1"]
    with pytest.raises(ValidationFailed, match="locked"):
        final(TEST, **_kw(uploaded))
    with pytest.raises(ValidationFailed, match="locked"):
        stage(
            StageSpec(
                dataset=TEST,
                submission_id="S4",
                eval_run="good",
                test_run="good.test",
                **_kw(uploaded),
            )
        )
    now = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    out = record(TEST, "S1", now, tz="utc", **_kw(uploaded))
    assert out.row.submission_id == "S1"
    with pytest.raises(ValidationFailed, match="locked"):
        record(TEST, "S2", now, tz="utc", **_kw(uploaded))


def test_stale_and_slots(uploaded):
    _measure_holdout(uploaded, "good")
    _measure_holdout(uploaded, "bad")
    ds, plan = uploaded.eval_ds, uploaded.eval_plan
    samples = ds.subset("holdout", plan, unseal=True, reason="t", paths=uploaded.eval_paths)
    ingest_run(
        uploaded,
        "good",
        ds,
        "fixed-v1",
        "holdout",
        perfect_predictions(list(reversed(samples))[:-1], ds.card),
        weights=uploaded.weights["good"],
        trained_on=("train",),
        replace=True,
    )
    res = final(TEST, slots=2, dry_run=True, **_kw(uploaded))
    assert res.unranked == ["S1"] and res.chosen == ["S2"]
    table = {e.submission_id: e for e in res.row.table}
    assert table["S1"].why == "stale_reading"
    assert count_unseals(uploaded.eval_paths, "fixed-v1", "holdout") >= 4


def test_lock_and_unlock(uploaded):
    row = lock(TEST, "R0 protocol", **_kw(uploaded))
    assert row.event == "lock" and row.reason == "R0 protocol"
    with pytest.raises(ValidationFailed, match="locked"):
        lock(TEST, "again", **_kw(uploaded))
    assert unlock(TEST, "deadline extended", **_kw(uploaded)).event == "unlock"
    with pytest.raises(ValidationFailed, match="not_locked"):
        unlock(TEST, "again", **_kw(uploaded))
```

`holdout_unseals`：夾具 `seed_eval_runs` 開了一次（產預測用）、兩次 `measure --unseal` 至少各一次，所以 ≥ 3；`test_stale_and_slots` 再開一次 → ≥ 4。用 `>=` 是因為 `measure_run` 內部開幾次是量測層的事。

- [ ] **Step 2: 寫失敗的測試** `tests/unit/submit/test_report.py`

```python
import pytest
from submit_fixtures import EVAL, TEST, STAMP, seed_eval_runs, seed_judgements, seed_test_runs

from vcp.core.time import utc_now
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.submit.actions import record, score
from vcp.submit.final import lock
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import init_profile
from vcp.submit.report import report, status
from vcp.submit.schema import LedgerRow, PlatformProfile, Quota
from vcp.submit.stage import StageSpec, stage


def _profile(**over) -> PlatformProfile:
    base = dict(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        quota=Quota(per_day=5, day_tz="UTC"),
        deadline="2999-01-01T00:00:00Z",
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


def _kw(pair):
    return {"data_root": pair.roots.data, "configs_root": pair.roots.configs}


def _seed(pair, profile):
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(profile, **_kw(pair))
    seed_test_runs(pair)
    now = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    for sid, eval_run, test_run, kind in (
        ("S1", "good", "good.test", "candidate"),
        ("S2", "bad", "bad.test", "baseline"),
    ):
        stage(
            StageSpec(
                dataset=TEST,
                submission_id=sid,
                eval_run=eval_run,
                test_run=test_run,
                kind=kind,
                reason=None if kind == "candidate" else "anchor",
                **_kw(pair),
            )
        )
        record(TEST, sid, now, tz="utc", **_kw(pair))
    score(TEST, "S1", public=0.8, **_kw(pair))


def test_status_view(pair):
    _seed(pair, _profile())
    st = status(TEST, **_kw(pair))
    assert st.staged == 2 and st.uploaded == 2 and st.foreign == 0
    assert st.quota.used == 2 and st.quota.per_day == 5
    assert st.deadline_in_hours > 24 and st.locked is None
    assert st.current == "S2" and st.unscored == ["S2"]
    led = SubmissionLedger(pair.test_paths.submissions_log)
    led.append(
        LedgerRow(
            event="foreign",
            ts=led.rows[-1].ts,
            platform_ref="f1",
            file_name="x.csv",
            at="2999-01-01T00:00:00.000Z",
            public=0.95,
        )
    )
    lock(TEST, "r0", **_kw(pair))
    st = status(TEST, **_kw(pair))
    assert st.current == "foreign:f1" and st.locked.reason == "r0" and st.foreign == 1


def test_status_best_board(pair):
    _seed(pair, _profile(board_rule="best"))
    assert status(TEST, **_kw(pair)).current == "S1"
    score(TEST, "S2", public=0.85, **_kw(pair))
    assert status(TEST, **_kw(pair)).current == "S2"


def test_report_is_last_vs_last(pair):
    _seed(pair, _profile())
    score(TEST, "S2", public=0.9, private=0.7, **_kw(pair))
    for run in ("good", "bad"):
        measure_run(
            MeasureSpec(
                run_id=run,
                metrics=["accuracy"],
                subsets=["holdout"],
                unseal=True,
                reason="report",
                **_kw(pair),
            )
        )
    rows = report(TEST, **_kw(pair))
    assert [r.submission_id for r in rows] == ["S1", "S2"]
    assert rows[0].delta is None and rows[0].public == 0.8 and rows[0].sealed_value == 1.0
    assert rows[1].delta == pytest.approx(0.1) and rows[1].kind == "baseline"
    assert rows[1].private == 0.7 and rows[1].shift == pytest.approx(-0.2)
    assert rows[1].sealed_value < 1.0
```

- [ ] **Step 3: 寫 `src/vcp/submit/final.py`**

```python
"""``final`` / ``lock`` / ``unlock`` (spec 6.4): the last shot is chosen by the sealed holdout,
never by the public board."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.data.split import load_plan
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.metrics import effective_params, get_metric, params_key
from vcp.measure.report import READINGS_LEDGER
from vcp.measure.runs import load_run
from vcp.measure.schema import Reading, RunCard
from vcp.submit.guards import assert_unlocked
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import load_profile
from vcp.submit.schema import FinalEntry, LedgerRow, PlatformProfile

NOT_RANKED = ("probe", "not_uploaded")


def count_unseals(eval_paths: DatasetPaths, plan_id: str, subset: str) -> int:
    """How many times the sealed subset has been opened, per the data layer's unseal log."""
    path = eval_paths.unseal_jsonl(plan_id)
    if not path.is_file():
        return 0
    n = 0
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValidationFailed(
                    f"bad unseal log: {e}", location=f"{path.name}:{lineno}"
                ) from e
            if row.get("subset") == subset:
                n += 1
    return n


def sealed_reading(
    readings: ReadingsLedger,
    card: RunCard,
    profile: PlatformProfile,
    params_hash: str,
    sealed_size: int,
) -> tuple[Reading | None, str]:
    """The newest sealed reading of this run, and why it is unusable when it is ("" = usable)."""
    rows = [
        r
        for r in readings.rows
        if r.run_id == card.run_id
        and r.plan_id == profile.plan_id
        and r.subset == profile.sealed_subset
        and r.metric == profile.metric
        and params_key(r.params) == params_hash
    ]
    if not rows:
        return None, "no_sealed_reading"
    r = rows[-1]
    entry = card.predictions.get(profile.sealed_subset)
    if entry is None or entry.sha256 != r.prediction_sha:
        return r, "stale_reading"
    if r.n_samples != sealed_size:
        return r, "partial_reading"
    return r, ""


@dataclass(frozen=True)
class FinalResult:
    row: LedgerRow
    chosen: list[str]
    unranked: list[str]
    needs_reupload: str | None
    warnings: list[str]
    written: bool


def _open(
    dataset: str, data_root: Path | None, configs_root: Path | None
) -> tuple[DatasetPaths, PlatformProfile, str, SubmissionLedger]:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, sha = load_profile(paths)
    return paths, profile, sha, SubmissionLedger(paths.submissions_log)


def final(
    dataset: str,
    *,
    slots: int | None = None,
    dry_run: bool = False,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> FinalResult:
    paths, profile, profile_sha, ledger = _open(dataset, data_root, configs_root)
    assert_unlocked(ledger)
    now = utc_now()
    warnings: list[str] = []
    if profile.deadline is not None and now >= parse_stamp(profile.deadline):
        warnings.append(f"past_deadline: {profile.deadline}")
    eval_paths = DatasetPaths.resolve(
        profile.eval_dataset, data_root=data_root, configs_root=configs_root
    )
    eval_plan = load_plan(eval_paths, profile.plan_id)
    sealed_size = len(eval_plan.ids_in(profile.sealed_subset))
    metric = get_metric(profile.metric)
    params = effective_params(metric, profile.metric_params)
    params_hash = params_key(params)
    sign = 1.0 if metric.higher_is_better else -1.0
    readings = ReadingsLedger(eval_paths.measure_dir / READINGS_LEDGER)
    entries: list[FinalEntry] = []
    for sid in ledger.ids():
        st = ledger.staged(sid)
        assert st is not None
        latest = ledger.latest_score(sid)
        public = latest.public if latest is not None else None
        reading: Reading | None = None
        if st.kind == "probe":
            why = "probe"
        elif not ledger.uploads(sid):
            why = "not_uploaded"
        else:
            card = load_run(paths.data_root, str(st.eval_run))
            reading, why = sealed_reading(readings, card, profile, params_hash, sealed_size)
        entries.append(
            FinalEntry(
                submission_id=sid,
                eligible=why == "",
                why=why,
                sealed_value=reading.value if reading is not None else None,
                sealed_reading_id=reading.reading_id if reading is not None else None,
                public=public,
                staged_at=st.ts,
            )
        )
    ranked = sorted(
        (e for e in entries if e.eligible),
        key=lambda e: (
            -sign * float(e.sealed_value if e.sealed_value is not None else 0.0),
            -(e.public if e.public is not None else float("-inf")),
            e.staged_at,
        ),
    )
    if not ranked:
        raise ValidationFailed(
            "no_sealed_readings: no uploaded candidate or baseline has a usable sealed reading; "
            "run `vcp eval measure --run R --subsets <sealed> --unseal --reason ...` first"
        )
    n = slots or profile.final_slots
    chosen = [e.submission_id for e in ranked[:n]]
    unranked = [e.submission_id for e in entries if not e.eligible and e.why not in NOT_RANKED]
    needs_reupload: str | None = None
    if profile.board_rule == "last":
        last = ledger.last_uploaded()
        if last is None or last.submission_id != chosen[0]:
            needs_reupload = chosen[0]
    row = LedgerRow(
        event="final",
        ts=stamp(),
        rule=profile.final_rule,
        slots=n,
        chosen=chosen,
        table=entries,
        metric=profile.metric,
        params=params,
        holdout_unseals=count_unseals(eval_paths, profile.plan_id, profile.sealed_subset),
        profile_sha256=profile_sha,
    )
    if not dry_run:
        ledger.append(row)
        ledger.append(LedgerRow(event="lock", ts=stamp(), reason="final"))
    return FinalResult(row, chosen, unranked, needs_reupload, warnings, not dry_run)


def lock(
    dataset: str, reason: str, *, data_root: Path | None = None, configs_root: Path | None = None
) -> LedgerRow:
    _, _, _, ledger = _open(dataset, data_root, configs_root)
    assert_unlocked(ledger)
    row = LedgerRow(event="lock", ts=stamp(), reason=reason)
    ledger.append(row)
    return row


def unlock(
    dataset: str, reason: str, *, data_root: Path | None = None, configs_root: Path | None = None
) -> LedgerRow:
    _, _, _, ledger = _open(dataset, data_root, configs_root)
    if ledger.lock_state() is None:
        raise ValidationFailed("not_locked: nothing to unlock")
    row = LedgerRow(event="unlock", ts=stamp(), reason=reason)
    ledger.append(row)
    return row
```

- [ ] **Step 4: 寫 `src/vcp/submit/report.py`**

```python
"""``vcp submit status`` / ``report``: read-only views over the ledger. ``report`` is
last-vs-last (spec 2): every upload against the one before it, never against the best."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, utc_now
from vcp.data.split import load_plan
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.metrics import effective_params, get_metric, params_key
from vcp.measure.report import READINGS_LEDGER
from vcp.measure.runs import load_run
from vcp.submit.final import sealed_reading
from vcp.submit.guards import QuotaState, quota_state
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow


@dataclass(frozen=True)
class StatusView:
    staged: int
    uploaded: int
    foreign: int
    quota: QuotaState | None
    deadline_in_hours: float | None
    locked: LedgerRow | None
    current: str | None
    unscored: list[str]


def _label(r: LedgerRow) -> str:
    return r.submission_id if r.submission_id else f"foreign:{r.platform_ref}"


def _public(ledger: SubmissionLedger, r: LedgerRow) -> float | None:
    if r.event == "foreign":
        return r.public
    latest = ledger.latest_score(str(r.submission_id))
    return latest.public if latest is not None else None


def status(
    dataset: str, *, data_root: Path | None = None, configs_root: Path | None = None
) -> StatusView:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    ledger = SubmissionLedger(paths.submissions_log)
    now = utc_now()
    deadline_in = None
    if profile.deadline is not None:
        deadline_in = (parse_stamp(profile.deadline) - now).total_seconds() / 3600
    arrivals = ledger.arrivals()
    current: str | None = None
    if profile.board_rule == "last":
        current = _label(arrivals[-1]) if arrivals else None
    else:
        best: float | None = None
        for r in arrivals:
            public = _public(ledger, r)
            if public is not None and (best is None or public > best):
                best, current = public, _label(r)
    unscored = [
        sid for sid in ledger.ids() if ledger.uploads(sid) and ledger.latest_score(sid) is None
    ]
    return StatusView(
        staged=len(ledger.ids()),
        uploaded=len(ledger.of("uploaded")),
        foreign=len(ledger.of("foreign")),
        quota=quota_state(ledger, profile, now),
        deadline_in_hours=deadline_in,
        locked=ledger.lock_state(),
        current=current,
        unscored=unscored,
    )


@dataclass(frozen=True)
class ReportRow:
    submission_id: str
    at: str
    kind: str
    public: float | None
    delta: float | None
    sealed_value: float | None
    private: float | None
    shift: float | None


def report(
    dataset: str, *, data_root: Path | None = None, configs_root: Path | None = None
) -> list[ReportRow]:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    ledger = SubmissionLedger(paths.submissions_log)
    eval_paths = DatasetPaths.resolve(
        profile.eval_dataset, data_root=data_root, configs_root=configs_root
    )
    sealed_size = len(load_plan(eval_paths, profile.plan_id).ids_in(profile.sealed_subset))
    params_hash = params_key(effective_params(get_metric(profile.metric), profile.metric_params))
    readings = ReadingsLedger(eval_paths.measure_dir / READINGS_LEDGER)
    rows: list[ReportRow] = []
    prev_public: float | None = None
    for r in ledger.arrivals():
        if r.event == "foreign":
            kind, public, private, sealed = "foreign", r.public, r.private, None
        else:
            st = ledger.staged(str(r.submission_id))
            kind = str(st.kind) if st is not None else "?"
            latest = ledger.latest_score(str(r.submission_id))
            public = latest.public if latest is not None else None
            private = latest.private if latest is not None else None
            sealed = None
            if st is not None:
                card = load_run(paths.data_root, str(st.eval_run))
                reading, why = sealed_reading(readings, card, profile, params_hash, sealed_size)
                sealed = reading.value if reading is not None and why == "" else None
        delta = None if public is None or prev_public is None else public - prev_public
        shift = None if public is None or private is None else private - public
        rows.append(ReportRow(_label(r), str(r.at), kind, public, delta, sealed, private, shift))
        prev_public = public
    return rows
```

- [ ] **Step 5: CLI `final`、`lock`、`unlock`、`status`、`report`**（追加到 `cli_submit.py`；import `from vcp.submit.final import final, lock, unlock`、`from vcp.submit.report import report, status as status_view`）

```python
@submit_app.command("final")
def final_cmd(
    dataset: DatasetOpt,
    slots: Annotated[int | None, typer.Option("--slots", help="override final_slots")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="print the table, write nothing")
    ] = False,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Pick the final submission(s) by the sealed holdout and lock the ledger."""

    def fn() -> CmdResult:
        res = final(
            dataset, slots=slots, dry_run=dry_run, data_root=data_root, configs_root=configs_root
        )
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "chosen": ",".join(res.chosen),
            "ranked": sum(1 for e in (res.row.table or []) if e.eligible),
            "unranked": len(res.unranked),
            "holdout_unseals": res.row.holdout_unseals or 0,
            "dry_run": dry_run,
        }
        if res.needs_reupload:
            fields["needs_reupload"] = res.needs_reupload
        human = [
            f"{e.submission_id:>12}  eligible={e.eligible!s:5} sealed={e.sealed_value!r} "
            f"public={e.public!r} {e.why}"
            for e in (res.row.table or [])
        ]
        human += [f"warning: {w}" for w in res.warnings]
        warn = bool(res.unranked or res.needs_reupload or res.warnings)
        status: Status = "WARN" if warn else "OK"
        return status, fields, res.row.model_dump(mode="json", exclude_none=True), human

    run_command("submit.final", json_mode, data_root, fn)


@submit_app.command("lock")
def lock_cmd(
    dataset: DatasetOpt,
    reason: Annotated[str, typer.Option("--reason")],
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Stop all staging and uploading (the R0 protocol)."""

    def fn() -> CmdResult:
        row = lock(dataset, reason, data_root=data_root, configs_root=configs_root)
        return (
            "OK",
            {"dataset": dataset, "locked": True},
            row.model_dump(mode="json", exclude_none=True),
            [],
        )

    run_command("submit.lock", json_mode, data_root, fn)


@submit_app.command("unlock")
def unlock_cmd(
    dataset: DatasetOpt,
    reason: Annotated[str, typer.Option("--reason")],
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Lift a lock (recorded with its reason)."""

    def fn() -> CmdResult:
        row = unlock(dataset, reason, data_root=data_root, configs_root=configs_root)
        return (
            "OK",
            {"dataset": dataset, "locked": False},
            row.model_dump(mode="json", exclude_none=True),
            [],
        )

    run_command("submit.unlock", json_mode, data_root, fn)


@submit_app.command("status")
def status_cmd(
    dataset: DatasetOpt,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Quota, deadline, lock, what is on the board, what is unscored. Reads, never writes."""

    def fn() -> CmdResult:
        st = status_view(dataset, data_root=data_root, configs_root=configs_root)
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "staged": st.staged,
            "uploaded": st.uploaded,
            "foreign": st.foreign,
        }
        warn = False
        if st.quota is None:
            fields["quota"] = "none"
            warn = True
        else:
            fields.update(st.quota.fields("UTC"))
            warn = warn or st.quota.remaining == 0
        if st.deadline_in_hours is not None:
            fields["deadline_in"] = round(st.deadline_in_hours, 1)
            warn = warn or st.deadline_in_hours < 24
        fields["locked"] = st.locked is not None
        fields["current"] = st.current or "none"
        fields["unscored"] = len(st.unscored)
        warn = warn or st.locked is not None or bool(st.unscored)
        human = [f"unscored: {sid}" for sid in st.unscored]
        payload = {
            "quota": None if st.quota is None else st.quota.fields("UTC"),
            "current": st.current,
            "unscored": st.unscored,
            "locked": None
            if st.locked is None
            else st.locked.model_dump(mode="json", exclude_none=True),
        }
        return ("WARN" if warn else "OK"), fields, payload, human

    run_command("submit.status", json_mode, data_root, fn)


@submit_app.command("report")
def report_cmd(
    dataset: DatasetOpt,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Every upload in order with its last-vs-last delta, sealed reading and private score."""

    def fn() -> CmdResult:
        rows = report(dataset, data_root=data_root, configs_root=configs_root)
        human = [
            f"{r.at}  {r.submission_id:>16}  {r.kind:>9}  public={r.public!r} delta={r.delta!r} "
            f"sealed={r.sealed_value!r} private={r.private!r} shift={r.shift!r}"
            for r in rows
        ]
        payload = {"rows": [r.__dict__ for r in rows]}
        return "OK", {"dataset": dataset, "rows": len(rows)}, payload, human

    run_command("submit.report", json_mode, data_root, fn)
```

- [ ] **Step 6: 追加 CLI 測試到 `tests/unit/test_cli_submit.py`**

```python
def test_final_status_report_cli(pair):
    from vcp.core.time import utc_now

    _ready(pair)
    assert _stage("S1", "good", "good.test").exit_code == 0
    assert (
        _stage("S2", "bad", "bad.test", "--kind", "baseline", "--reason", "anchor").exit_code == 0
    )
    at = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    for sid in ("S1", "S2"):
        r = runner.invoke(
            app,
            ["submit", "record", "--dataset", "beach-test", "--id", sid, "--tz", "utc", "--at", at],
        )
        assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["submit", "status", "--dataset", "beach-test"])
    assert (
        r.exit_code == 0
        and "current=S2" in _verdict(r.output)
        and "unscored=2" in _verdict(r.output)
    )
    r = runner.invoke(app, ["submit", "final", "--dataset", "beach-test"])
    assert r.exit_code == 1 and "no_sealed_readings" in _verdict(r.output)
    for run in ("good", "bad"):
        r = runner.invoke(
            app,
            [
                "eval",
                "measure",
                "--run",
                run,
                "--metrics",
                "accuracy",
                "--subsets",
                "holdout",
                "--unseal",
                "--reason",
                "final pick",
            ],
        )
        assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["submit", "final", "--dataset", "beach-test", "--dry-run"])
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "chosen=S1" in v and "needs_reupload=S1" in v and "dry_run=true" in v
    r = runner.invoke(app, ["submit", "final", "--dataset", "beach-test", "--json"])
    assert r.exit_code == 0, r.output
    assert _json(r)["result"]["chosen"] == ["S1"]
    r = runner.invoke(app, ["submit", "report", "--dataset", "beach-test"])
    assert r.exit_code == 0 and "rows=2" in _verdict(r.output)
    r = runner.invoke(app, ["submit", "lock", "--dataset", "beach-test", "--reason", "x"])
    assert r.exit_code == 1 and "locked" in _verdict(r.output)
    r = runner.invoke(app, ["submit", "unlock", "--dataset", "beach-test", "--reason", "extended"])
    assert r.exit_code == 0 and "locked=false" in _verdict(r.output)
```

- [ ] **Step 7: 跑測試、ruff、commit**

Run: `uv run pytest tests/unit/submit tests/unit/test_cli_submit.py -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS、乾淨

```bash
git add src/vcp/submit/final.py src/vcp/submit/report.py src/vcp/cli_submit.py tests/unit/submit/test_final.py tests/unit/submit/test_report.py tests/unit/test_cli_submit.py
git commit -m "feat(submit): final（best_sealed 決選、決選表、自動封槍）、lock / unlock、status、report（last-vs-last）與 CLI" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: 端到端、真資料、文件

**Files:**
- Create: `tests/unit/test_e2e_submit.py`、`tests/integration/test_rsna_knee_submit.py`
- Modify: `README.md`（新一節 + ingest 選項）、`CLAUDE.md`（路徑、常用命令、變異軸）

**Interfaces:**
- Consumes: 全部；`submit_fixtures`；`tests/integration/conftest.py` 的 `load_real` / `real_roots`。
- Produces: 無新程式碼。

- [ ] **Step 1: 寫端到端測試** `tests/unit/test_e2e_submit.py`

```python
"""The whole submission layer through the CLI (spec 15): manual platform first (stage / record /
score / report / measure --unseal / final / lock), then the same story on a fake Kaggle CLI (a
real subprocess) with quota exhaustion, sync, a failed upload, and a privacy scan."""

import json
import sys

import pytest
from submit_fixtures import make_pair, seed_eval_runs, seed_judgements, seed_test_runs
from typer.testing import CliRunner

from vcp.cli import app
from vcp.core.time import utc_now
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile, Quota

runner = CliRunner()
SECRET = "fakesecretfakesecretfakesecret1234"
STAMP = "2026-09-05T00:00:00.000Z"

FAKE_KAGGLE = """
import json, os, sys
state_path = os.environ["FAKE_KAGGLE_STATE"]
state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {"submissions": []}
args = sys.argv[1:]
print("KAGGLE_KEY=" + os.environ.get("KAGGLE_KEY", ""), file=sys.stderr)
if args[:2] == ["competitions", "submit"]:
    if os.environ.get("FAKE_KAGGLE_FAIL"):
        print("401 Unauthorized key=" + os.environ.get("KAGGLE_KEY", ""), file=sys.stderr)
        sys.exit(1)
    f = args[args.index("-f") + 1]
    m = args[args.index("-m") + 1]
    n = len(state["submissions"]) + 1
    state["submissions"].insert(0, {"ref": n, "fileName": os.path.basename(f), "date": os.environ["FAKE_KAGGLE_NOW"], "description": m, "status": "complete", "publicScore": str(round(0.5 + 0.1 * n, 3))})
    json.dump(state, open(state_path, "w", encoding="utf-8"))
    print("Successfully submitted to c1")
elif args[:2] == ["competitions", "submissions"]:
    print(json.dumps(state["submissions"]))
else:
    sys.exit(2)
"""


@pytest.fixture
def pair(roots, tmp_path):
    return make_pair(roots, tmp_path)


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _run(*args):
    return runner.invoke(app, ["submit", *args])


def _measure_holdout(run):
    r = runner.invoke(
        app,
        [
            "eval",
            "measure",
            "--run",
            run,
            "--metrics",
            "accuracy",
            "--subsets",
            "holdout",
            "--unseal",
            "--reason",
            "final pick",
        ],
    )
    assert r.exit_code == 0, r.output


def _ready(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)


def test_manual_platform_story(pair):
    _ready(pair)
    r = _run(
        "init",
        "--dataset",
        "beach-test",
        "--eval-dataset",
        "beach",
        "--plan",
        "fixed-v1",
        "--sealed",
        "holdout",
        "--platform",
        "manual",
        "--metric",
        "accuracy",
        "--writer",
        "scores_csv",
        "--writer-opt",
        "id_field=view_stem",
        "--quota",
        "3",
        "--day-tz",
        "Asia/Taipei",
        "--deadline",
        "2999-01-01T00:00:00Z",
    )
    assert r.exit_code == 0, r.output
    seed_test_runs(pair)
    stage = ["stage", "--dataset", "beach-test"]
    r = _run(*stage, "--id", "S1", "--eval-run", "good", "--test-run", "good.test")
    assert r.exit_code == 0 and "admission=PASS" in _verdict(r.output)
    r = _run(*stage, "--id", "S2", "--eval-run", "bad", "--test-run", "bad.test")
    assert r.exit_code == 1 and "not_admitted" in _verdict(r.output)  # j7cas: blocked
    r = _run(
        *stage,
        "--id",
        "S2",
        "--eval-run",
        "bad",
        "--test-run",
        "bad.test",
        "--kind",
        "probe",
        "--reason",
        "curious",
    )
    assert r.exit_code == 0 and "admission=waived" in _verdict(r.output)
    r = _run(
        *stage,
        "--id",
        "S3",
        "--eval-run",
        "bad",
        "--test-run",
        "bad.test",
        "--kind",
        "baseline",
        "--reason",
        "anchor",
    )
    assert r.exit_code == 0, r.output
    r = _run(*stage, "--id", "S4", "--eval-run", "good", "--test-run", "bad.mismatch")
    assert r.exit_code == 1 and "identity" in _verdict(r.output)
    r = _run("verify", "--dataset", "beach-test", "--id", "S1")
    assert r.exit_code == 0 and "checks=2" in _verdict(r.output)
    at = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    for sid in ("S1", "S2", "S3"):
        r = _run("record", "--dataset", "beach-test", "--id", sid, "--tz", "utc", "--at", at)
        assert r.exit_code == 0, r.output
    assert "quota=3/3" in _verdict(r.output)
    r = _run("score", "--dataset", "beach-test", "--id", "S1", "--public", "0.8")
    assert r.exit_code == 0
    r = _run("score", "--dataset", "beach-test", "--id", "S3", "--public", "0.9")
    assert r.exit_code == 0
    r = _run("report", "--dataset", "beach-test")
    assert r.exit_code == 0 and "rows=3" in _verdict(r.output)
    r = _run("status", "--dataset", "beach-test")
    assert "current=S3" in _verdict(r.output) and "quota=3/3" in _verdict(r.output)
    r = _run("final", "--dataset", "beach-test")
    assert r.exit_code == 1 and "no_sealed_readings" in _verdict(r.output)
    _measure_holdout("good")
    _measure_holdout("bad")
    r = _run("final", "--dataset", "beach-test")
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "chosen=S1" in v and "needs_reupload=S1" in v  # sealed beats public
    r = _run(*stage, "--id", "S5", "--eval-run", "good", "--test-run", "good.test")
    assert r.exit_code == 1 and "locked" in _verdict(r.output)
    r = _run("record", "--dataset", "beach-test", "--id", "S1", "--tz", "utc", "--at", at)
    assert r.exit_code == 0 and "quota_overflow=true" in _verdict(r.output)
    r = _run("record", "--dataset", "beach-test", "--id", "S3", "--tz", "utc", "--at", at)
    assert r.exit_code == 1 and "locked" in _verdict(r.output)
    r = _run("status", "--dataset", "beach-test")
    assert "current=S1" in _verdict(r.output) and "locked=true" in _verdict(r.output)
    led = SubmissionLedger(pair.test_paths.submissions_log)
    events = [row.event for row in led.rows]
    assert events.count("staged") == 3 and events.count("uploaded") == 4
    assert events[-3:] == ["final", "lock", "uploaded"]


def _scan(pair, outputs: list[str]) -> None:
    for root in (pair.roots.data, pair.roots.configs):
        for p in root.rglob("*"):
            if p.is_file():
                assert SECRET.encode() not in p.read_bytes(), p
    for text in outputs:
        assert SECRET not in text


def test_kaggle_platform_story(pair, tmp_path, monkeypatch):
    _ready(pair)
    script = tmp_path / "fake_kaggle.py"
    script.write_text(FAKE_KAGGLE, encoding="utf-8")
    state = tmp_path / "state.json"
    monkeypatch.setenv("FAKE_KAGGLE_STATE", str(state))
    monkeypatch.setenv("FAKE_KAGGLE_NOW", utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"))
    monkeypatch.setenv("KAGGLE_KEY", SECRET)
    init_profile(
        PlatformProfile(
            dataset="beach-test",
            eval_dataset="beach",
            plan_id="fixed-v1",
            sealed_subset="holdout",
            platform="kaggle",
            competition="c1",
            board_rule="best",
            final_slots=2,
            quota=Quota(per_day=2, day_tz="UTC"),
            metric="accuracy",
            writer="scores_csv",
            kaggle_command=[sys.executable, str(script)],
            created_at=STAMP,
        ),
        data_root=pair.roots.data,
        configs_root=pair.roots.configs,
    )
    seed_test_runs(pair)
    outputs: list[str] = []
    stage = ["stage", "--dataset", "beach-test"]
    for sid, ev, tr, extra in (
        ("S1", "good", "good.test", []),
        ("S2", "bad", "bad.test", ["--kind", "baseline", "--reason", "anchor"]),
        ("S3", "bad", "bad.test", ["--kind", "probe", "--reason", "look"]),
    ):
        r = _run(*stage, "--id", sid, "--eval-run", ev, "--test-run", tr, *extra)
        assert r.exit_code == 0, r.output
    monkeypatch.setenv("FAKE_KAGGLE_FAIL", "1")
    r = _run("upload", "--dataset", "beach-test", "--id", "S1")
    outputs.append(r.output)
    assert r.exit_code == 1 and "exit 1" in _verdict(r.output)
    monkeypatch.delenv("FAKE_KAGGLE_FAIL")
    for sid in ("S1", "S2"):
        r = _run("upload", "--dataset", "beach-test", "--id", sid, "--message", "hello")
        outputs.append(r.output)
        assert r.exit_code == 0, r.output
        assert "confirmed=true" in _verdict(r.output)
    r = _run("upload", "--dataset", "beach-test", "--id", "S3")
    outputs.append(r.output)
    assert r.exit_code == 1 and "quota_exhausted" in _verdict(r.output)
    doc = json.loads(state.read_text(encoding="utf-8"))
    doc["submissions"].append(
        {
            "ref": 99,
            "fileName": "mate.csv",
            "date": "2026-09-01T00:00:00Z",
            "description": "teammate",
            "publicScore": "0.4",
        }
    )
    state.write_text(json.dumps(doc), encoding="utf-8")
    r = _run("sync", "--dataset", "beach-test")
    outputs.append(r.output)
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "scored=2" in v and "foreign=1" in v and "status=WARN" in v
    r = _run("sync", "--dataset", "beach-test")
    assert "scored=0" in _verdict(r.output) and "foreign=0" in _verdict(r.output)
    r = _run("status", "--dataset", "beach-test")
    outputs.append(r.output)
    assert "current=S2" in _verdict(r.output)  # board_rule=best: S2 scored higher by the fake
    _measure_holdout("good")
    _measure_holdout("bad")
    r = _run("final", "--dataset", "beach-test")
    outputs.append(r.output)
    assert r.exit_code == 0, r.output
    assert "chosen=S1,S2" in _verdict(r.output) and "needs_reupload" not in _verdict(r.output)
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert len(led.of("uploaded")) == 2 and len(led.of("foreign")) == 1
    assert led.latest_score("S1").source == "platform"
    _scan(pair, outputs)
```

假 CLI 每次都把 `KAGGLE_KEY=...` 印到 stderr（成功時 vcp 不讀 stderr；失敗時它是最後一行，必須被遮掉）；時間由環境變數 `FAKE_KAGGLE_NOW` 給，不在字串裡呼叫時鐘。`_scan` 會抓任何漏網的秘密。

- [ ] **Step 2: 跑端到端測試**

Run: `uv run pytest tests/unit/test_e2e_submit.py -o addopts="" -q`
Expected: PASS（2 passed）

- [ ] **Step 3: 寫真資料測試** `tests/integration/test_rsna_knee_submit.py`

```python
"""scores_csv renders a complete submission for three real RSNA Knee studies (one row per study,
one column per label), deterministically; nothing under the real data root is written."""

from __future__ import annotations

import pytest

from conftest import load_real
from vcp.data.dataset import Dataset
from vcp.measure.schema import Prediction, payload_field
from vcp.submit.writers import get_writer
from vcp.submit.writers.base import WriteContext

pytestmark = pytest.mark.realdata
NAME = "rsna-knee"


@pytest.fixture(scope="module")
def knee(real_roots):
    return load_real(NAME, real_roots)


def test_scores_csv_on_three_studies(knee, tmp_path):
    three = list(knee.samples[:3])
    ds = Dataset.from_parts(knee.card.model_copy(update={"name": "knee3"}), three)
    names = [c.name for c in ds.card.categories]
    payload = payload_field(ds.card.task)
    preds = [Prediction(sample_id=s.sample_id, **{payload: {n: 0.5 for n in names}}) for s in three]
    out = tmp_path / "submission.csv"
    res = get_writer("scores_csv").write(preds, WriteContext(ds, three, {}, out))
    assert res.rows == 3 and res.missing == []
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(["id", *names]) and len(lines) == 4
    again = tmp_path / "again.csv"
    get_writer("scores_csv").write(preds, WriteContext(ds, three, {}, again))
    assert again.read_bytes() == out.read_bytes()
```

Run: `uv run pytest tests/integration/test_rsna_knee_submit.py -o addopts="" -q -m realdata`
Expected: 1 passed（無真資料時 1 skipped）

- [ ] **Step 4: README**

在 `## 匯入器與 `rows_read` 的語意` 那一節之前插入：

````markdown
## 提交治理命令 `vcp submit`

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp submit init` | 寫 `configs/datasets/<test>/submit.yaml`（平台、配額與時區、截止、決選指標、輸出格式），並替 test dataset 建單子集 plan `all-v1` | `--dataset`（test dataset）、`--eval-dataset`、`--plan`、`--sealed`、`--platform manual\|kaggle`、`--competition`、`--kind file\|kernel`、`--board-rule last\|best`、`--quota N --day-tz TZ`、`--display-tz`、`--deadline`、`--metric`、`--writer`、`--writer-opt k=v`、`--kaggle-command` |
| `vcp submit stage` | 四道門（封槍 / 截止、eval-test 配對核對、準入判決、產檔）全過才寫 `submit/<test>/<id>/` 與台帳 `staged` 列 | `--id`、`--eval-run`、`--test-run`、`--kind candidate\|baseline\|probe`、`--reason`、kernel 類 `--kernel --version --weights RUN[:sha]`、`--writer-opt`、`--plugin` |
| `vcp submit upload` | Kaggle：再驗 sha → 配額 → `kaggle competitions submit` → `uploaded` 列 | `--id`、`--message` |
| `vcp submit record` | 手動平台：你在網頁上傳後回填，平台顯示時間換成 UTC | `--id`、`--at "YYYY-MM-DD HH:MM"`、`--tz platform\|utc`、`--platform-ref` |
| `vcp submit score` / `sync` | 回填 public / private；Kaggle 以 `competitions submissions` 回讀、配對、把別人的發記成 `foreign`（照數配額） | `--public`、`--private` |
| `vcp submit final` | 已準入且已上傳的候選依 sealed 讀數（同分看 public、再看 staged 時間）選出 `final_slots` 個，寫決選表並封槍 | `--slots`、`--dry-run` |
| `vcp submit lock` / `unlock` | 封槍 / 解封（留理由） | `--reason` |
| `vcp submit status` / `report` | 配額剩餘與重置時間、截止倒數、榜面現任、未回填；每發 last-vs-last、sealed 讀數、public→private 位移（唯讀） | |
| `vcp submit verify` | 三驗第三驗：重讀檔 sha、從 test run 重產比對（位元級重現） | `--id` |

候選 = (eval 側 run, test 側 run) 一對：單模比 `weights_hash`（用 `vcp eval ingest --weights PATH` 給 run 身分），融合比 method / params / 成員權重並逐成員遞迴。`candidate` 需要 `vcp eval judge` 的 PASS 判決（融合 run 每個成員各一份）；`baseline` / `probe` 要 `--reason`，probe 永不進決選。沒有 `--override`。vcp 不碰 Kaggle 憑證：只呼叫 kaggle CLI，輸出經 redact 才落地。

### 一次提交

```bash
uv run vcp eval ingest --run good.test --dataset D-test --plan all-v1 --subset test --format scores_csv --src preds.csv --weights runs-ultra/good/weights/best.pt
uv run vcp submit stage --dataset D-test --id SUB34 --eval-run good --test-run good.test
uv run vcp submit upload --dataset D-test --id SUB34            # 手動平台改 record --at "2026-08-31 21:28"
uv run vcp submit sync --dataset D-test                          # 手動平台改 score --public 0.7916
uv run vcp eval measure --run good --subsets holdout --unseal --reason "final pick"
uv run vcp submit final --dataset D-test                        # 自動封槍；board_rule=last 時照 needs_reupload 重傳
```
````

量測層命令表的 `vcp eval ingest` 那一列，選項欄加 `--weights PATH`、`--config PATH`（檔案 sha256 寫進 run 的 `weights_hash` / `config_hash`）。

- [ ] **Step 5: CLAUDE.md**

「通用性原則」的「十個變異軸（…、融合器）」改為「十二個變異軸（任務、匯入器、匯出器、解碼器、切分策略、稽核、轉換器、指標、σ_p 方法、融合器、輸出格式、平台）」。

「路徑」加一條：

```markdown
- 提交治理：`configs/datasets/<test>/submit.yaml`（平台設定，改就改 git）與 `submissions.jsonl`（只 append 的台帳：staged / uploaded / scored / foreign / final / lock / unlock）進 git；輸出檔與 `stage.json` 在 `submit/<test>/<id>/`（寫一次不改）。候選 = (eval run, test run) 配對，身分靠 `weights_hash`；`final` 只看 sealed 讀數。vcp 不碰平台憑證。
```

「常用命令」加一條：

```markdown
- `uv run vcp submit stage --dataset T --id S --eval-run E --test-run R` / `uv run vcp submit upload --dataset T --id S`（Kaggle）或 `record --at "…"`（手動）/ `uv run vcp submit final --dataset T`（決選 + 封槍）/ `uv run vcp submit status --dataset T`（唯讀）
```

- [ ] **Step 6: 全套測試、ruff、commit**

Run: `uv run pytest --cov=vcp -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: 全部 PASS、覆蓋率 ≥ 80%、乾淨

```bash
git add tests/unit/test_e2e_submit.py tests/integration/test_rsna_knee_submit.py README.md CLAUDE.md
git commit -m "test(submit): 端到端（手動平台與假 Kaggle CLI：配額、sync、失敗上傳、隱私掃描）、RSNA 真資料 writer；README 與 CLAUDE.md 加 vcp submit" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## 自我審查

- **Spec 覆蓋**：§4.1–4.3 → Task 1；§5 → Task 1；§6 十二個命令 → Task 4（init）、9（stage、verify）、10（upload、record、score、sync）、11（final、lock、unlock、status、report）；§6.1–6.5 → Task 9、10、11、2；§7 → Task 7；§8 → Task 7；§9 → Task 5、6；§10 → Task 8；§11 → Task 8（redact、零憑證）+ Task 12（掃描）；§12 → 各任務的 `reason=` 字彙；§13 資料層 / 量測層 / 融合層介面 → Task 1（paths）、3（ingest）、7（fuse.json）；§14 → 各任務測試 + Task 12；§15 驗收 1–8 → Task 12 的兩個劇本 + Task 9 的 verify 測試 + 覆蓋率門檻。
- **計畫層決定**：1（Task 3）、2（Task 9 `_file_pairing`）、3（Task 9 `assert_unlocked`）、4（Task 10 `record`）、5（Task 8）、6（Task 8、12）、7（Task 4）、8（Task 10 `match_submission`）、9（Task 11 `report`）、10（Task 1 `append_ledger_row`）。
- **型別一致性**：`Pairing.checks` 是 `list[str]`（Task 1、7、9）；`Artifact.weights: list[WeightRef]`（Task 1、7、9）；`QuotaState.fields(tz_name) -> dict[str, str]`（Task 9、10、11 CLI 都以 `fields.update(...)` 用）；`Platform.upload(staged, artifact, message, profile, runner)` 五個位置參數（Task 8 定義、Task 10 呼叫）；`sealed_reading(readings, card, profile, params_hash, sealed_size)`（Task 11 兩處）；`SubmissionLedger.arrivals()` 回 `uploaded` + `foreign`（Task 1 定義、Task 9 配額、Task 11 report / status 用）。
