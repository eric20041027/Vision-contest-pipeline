# PostgreSQL adaptive provenance policy v2 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 實作 policy v2（相對帶或按規模分層的信心帶）、它的預先登記候選帶比較、能讓 v1 與 v2 同場評估的 runner，並發 0.15.0；量測本身在發版後另跑（附錄 A）。

**Architecture:** 帶的數學放在新的純函式模組 `src/vcp/provenance/policy_bands.py`；`strategy.py` 加 `AdaptivePolicyV2`、`fit_policy_v2`，以 `policy_version` 分派載入、寫入與選擇，v1 的模型與決策逐位元不變。runner（`tests/performance/provenance/`）多一個比較方法 `postgres_adaptive_v1`，所有新參數都是「沒給就跟現在一樣」，既有的簽名、checkpoint 契約與 mock 不受影響。

**Tech Stack:** Python 3.12、pydantic v2、scikit-learn（只在擬合時 import）、pytest、ruff、uv。

**Spec:** `docs/superpowers/specs/2026-10-09-vcp-postgres-adaptive-policy-v2-design.md`（commit `5cfa059`）。計畫與 spec 衝突時以 spec 為準。

## Global Constraints

- policy v1 `postgres-adaptive-v1-9f4e58346529`（`policy.json` SHA-256 `a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78`）與五份 v1 證據不改、不重新發布、不重新擬合；`AdaptivePolicy` 類別與 `_derived_policy_id` 一字不改。
- 不改 PostgreSQL DDL，`POSTGRES_SCHEMA_VERSION` 維持 1；不加 reason code（v2 沿用 `calibrated_incremental_lower_confident_cost` / `calibrated_full_lower_or_uncertain_cost`）。
- `BENCHMARK_SCHEMA_VERSION` 維持 1；`adaptive_benchmark.METHODS` 維持現在的六個。
- held-out v2 seeds 是 `(20261101, 20261102)`；比較方法名稱是 `postgres_adaptive_v1`；v2 的 `policy_version` 是 `postgres-adaptive-v2`，id 是 `postgres-adaptive-v2-<calibration sha 前 12 碼>`。
- 候選帶只有 `relative` 與 `stratified_edges`；§5 的平手門檻是 0.1（`|score_A − score_B| < 0.1` 選 `relative`）。
- 每個新參數（函式關鍵字、CLI 選項、checkpoint 契約鍵）都是可選的；沒給時行為、輸出與呼叫形狀跟現在逐位元相同。
- 取時只用 `vcp.core.time.utc_now()` / `stamp()`（ruff TID251）；證據檔用 `vcp.core.atomic.write_once_text`，寫一次不改，失敗的嘗試也保存。
- PostgreSQL 只用 libpq service name；不讀、不複製 `pg_service.conf` / `pgpass.conf`；`src/vcp` 不出現比賽名稱。
- 測試不碰真實資料根（用 `roots` fixture 或 `tmp_path`）；覆蓋率 ≥ 80%；commit 前 `uv run ruff check . && uv run ruff format --check .`。
- git：明列檔案、不用 `git add -A`、不用 `git stash`、commit 不加任何 co-author trailer；markdown 不跑 `ruff format`；檔案用 LF。
- `CLAUDE.md` 與 `AGENTS.md` 逐位元相同；`.claude/skills/` 改了就整份 `cp -r` 到 `.agents/skills/`。

## 檔案地圖

| 檔案 | 責任 | Task |
|---|---|---|
| `src/vcp/provenance/policy_bands.py`（新） | 兩種帶的模型、擬合與選擇規則，純函式 | 1 |
| `tests/unit/provenance/test_policy_bands.py`（新） | 帶的單元測試 | 1 |
| `tests/unit/provenance/test_policy_v1_regression.py`（新） | 三份 v1 證據的每個決策都能重現 | 2 |
| `src/vcp/provenance/strategy.py` | `fit_cost_models`、`AdaptivePolicyV2`、`fit_policy_v2`、`policy_from_payload`、分派 | 3 |
| `tests/unit/provenance/test_strategy_v2.py`（新） | v2 的模型、擬合、選擇與 artifact | 3 |
| `tests/performance/provenance/compare_bands.py`（新） | §5 的預先登記比較 | 4 |
| `tests/unit/provenance/test_compare_bands.py`（新） | 比較腳本的單元測試 | 4 |
| `docs/benchmarks/postgres-provenance-band-comparison-v1.json`（新） | 比較的證據 | 5 |
| `docs/superpowers/plans/2026-10-09-vcp-postgres-adaptive-policy-v2-followups.md`（新） | 裁決、限制、待辦 | 5、10 |
| `tests/performance/provenance/publish_policy_v2.py`（新） | 從 calibration v2 發布 v2 policy 文件與 artifact | 6 |
| `tests/performance/provenance/evaluate_adaptive.py` | `load_calibration` 讀 v2 文件；多 policy 的 `prepare_workload` / `collect_rows`；held-out v2 | 6、7、8 |
| `docs/benchmarks/postgres-provenance-policy-v2.json` 與 `-artifacts/`（新） | 正式的 v2 policy | 6 |
| `tests/performance/provenance/adaptive_benchmark.py` | 比較方法與第二個 policy 的管線 | 7 |
| `tests/performance/provenance/real_validation.py` | real 加 v1 比較方法 | 9 |
| `tests/unit/provenance/test_adaptive_benchmark.py`、`test_adaptive_evaluation.py` | runner 的新測試 | 6–9 |
| README ×2、操作指南、skill、`cli.md`、交接、舊後記、回歸門檻 | 文件與門檻 | 10 |
| 版本三處、CHANGELOG、交接、README | 發版 | 11 |

---

### Task 1: 信心帶模組 `policy_bands.py`

**Files:**
- Create: `src/vcp/provenance/policy_bands.py`
- Test: `tests/unit/provenance/test_policy_bands.py`

**Interfaces:**
- Consumes: 無。
- Produces（後面的 task 都用這些名字）:
  - `BandKind = Literal["relative", "stratified_edges"]`、`BAND_KINDS: tuple[BandKind, ...]`
  - `RelativeBand(kind="relative", incremental_relative_rmse: float, full_relative_rmse: float)`
  - `EdgesStratum(decade: int, incremental_rmse_ms: float, full_rmse_ms: float, observations: int)`
  - `StratifiedEdgesBand(kind="stratified_edges", strata: tuple[EdgesStratum, ...])`，方法 `stratum_for(total_edges: int) -> EdgesStratum`
  - `Band`（以 `kind` 分辨的聯集，給 pydantic 欄位用）
  - `edges_decade(total_edges: int) -> int`
  - `width(band, model: Literal["incremental", "full"], predicted_ms: float, total_edges: int) -> float`
  - `band_allows_incremental(band, incremental_ms: float, full_ms: float, total_edges: int) -> bool`
  - `rmse(predicted, actual) -> float`、`relative_rmse(predicted, actual) -> float`
  - `fit_band(kind, *, total_edges, incremental_predicted, incremental_actual, full_predicted, full_actual) -> RelativeBand | StratifiedEdgesBand`（輸入有誤時 `ValueError`）

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/unit/provenance/test_policy_bands.py`：

```python
"""Policy v2's uncertainty bands (spec 2026-10-09 §4.2)."""

import math

import pytest
from pydantic import ValidationError

from vcp.provenance.policy_bands import (
    BAND_KINDS,
    EdgesStratum,
    RelativeBand,
    StratifiedEdgesBand,
    band_allows_incremental,
    edges_decade,
    fit_band,
    relative_rmse,
    rmse,
    width,
)


@pytest.mark.parametrize(
    ("edges", "decade"),
    [(0, 0), (1, 0), (9, 0), (10, 1), (999, 2), (1000, 3), (1336, 3), (19982, 4), (199982, 5)],
)
def test_edges_decade_is_the_digit_count_minus_one(edges, decade):
    assert edges_decade(edges) == decade


def test_relative_band_scales_with_the_estimate():
    band = RelativeBand(incremental_relative_rmse=0.2, full_relative_rmse=0.1)
    # 100*1.2 = 120 < 150*0.9 = 135: apart even at the edges
    assert band_allows_incremental(band, 100.0, 150.0, total_edges=1500)
    # the same gap at 100x the size is still apart: v1's fixed band would not see that
    assert band_allows_incremental(band, 10_000.0, 15_000.0, total_edges=150_000)


def test_relative_band_tie_selects_full():
    band = RelativeBand(incremental_relative_rmse=0.5, full_relative_rmse=0.0)
    assert not band_allows_incremental(band, 100.0, 150.0, total_edges=1500)  # 150 < 150 is false


def test_full_relative_rmse_of_one_or_more_never_lets_incremental_win():
    band = RelativeBand(incremental_relative_rmse=0.0, full_relative_rmse=1.0)
    assert not band_allows_incremental(band, 1.0, 1_000_000.0, total_edges=1500)


def test_stratified_band_uses_the_size_decade_and_the_nearest_stratum():
    band = StratifiedEdgesBand(
        strata=(
            EdgesStratum(decade=3, incremental_rmse_ms=10.0, full_rmse_ms=20.0, observations=4),
            EdgesStratum(decade=5, incremental_rmse_ms=1000.0, full_rmse_ms=2000.0, observations=4),
        )
    )
    assert band.stratum_for(1500).decade == 3
    assert band.stratum_for(150_000).decade == 5
    assert band.stratum_for(5).decade == 3  # below the lowest: nearest
    assert band.stratum_for(10**7).decade == 5  # above the highest: nearest
    assert band.stratum_for(15_000).decade == 5  # decade 4 is one from each: the larger wins
    assert band_allows_incremental(band, 100.0, 150.0, total_edges=1500)  # 110 < 130
    assert not band_allows_incremental(band, 100.0, 150.0, total_edges=150_000)  # 1100 < -1850


def test_width_matches_each_kind():
    relative = RelativeBand(incremental_relative_rmse=0.25, full_relative_rmse=0.5)
    assert width(relative, "incremental", 200.0, 1500) == 50.0
    assert width(relative, "full", 200.0, 1500) == 100.0
    stratified = StratifiedEdgesBand(
        strata=(EdgesStratum(decade=3, incremental_rmse_ms=7.0, full_rmse_ms=9.0, observations=1),)
    )
    assert width(stratified, "incremental", 1e9, 1500) == 7.0
    assert width(stratified, "full", 1e9, 1500) == 9.0


def test_rmse_and_relative_rmse():
    assert rmse([10.0, 10.0], [13.0, 6.0]) == math.sqrt((9 + 16) / 2)
    assert relative_rmse([10.0, 20.0], [11.0, 18.0]) == math.sqrt((0.01 + 0.01) / 2)
    with pytest.raises(ValueError, match="positive"):
        relative_rmse([0.0], [1.0])
    with pytest.raises(ValueError):
        rmse([], [])
    with pytest.raises(ValueError):
        rmse([1.0], [1.0, 2.0])


def test_fit_band_relative_and_stratified():
    kwargs = dict(
        total_edges=[1500, 1600, 150_000, 160_000],
        incremental_predicted=[10.0, 10.0, 1000.0, 1000.0],
        incremental_actual=[11.0, 9.0, 1100.0, 900.0],
        full_predicted=[20.0, 20.0, 2000.0, 2000.0],
        full_actual=[22.0, 18.0, 2200.0, 1800.0],
    )
    relative = fit_band("relative", **kwargs)
    assert relative == RelativeBand(incremental_relative_rmse=0.1, full_relative_rmse=0.1)
    stratified = fit_band("stratified_edges", **kwargs)
    assert [s.decade for s in stratified.strata] == [3, 5]
    assert [s.incremental_rmse_ms for s in stratified.strata] == [1.0, 100.0]
    assert [s.full_rmse_ms for s in stratified.strata] == [2.0, 200.0]
    assert [s.observations for s in stratified.strata] == [2, 2]
    with pytest.raises(ValueError, match="band kind"):
        fit_band("absolute", **kwargs)
    with pytest.raises(ValueError):
        fit_band("relative", **{**kwargs, "total_edges": [1500]})


def test_band_models_are_strict():
    assert BAND_KINDS == ("relative", "stratified_edges")
    with pytest.raises(ValidationError):
        RelativeBand(incremental_relative_rmse=-0.1, full_relative_rmse=0.0)
    with pytest.raises(ValidationError):
        RelativeBand(incremental_relative_rmse=math.inf, full_relative_rmse=0.0)
    with pytest.raises(ValidationError, match="extra"):
        RelativeBand(incremental_relative_rmse=0.1, full_relative_rmse=0.1, surprise=1)
    duplicate = EdgesStratum(decade=3, incremental_rmse_ms=1.0, full_rmse_ms=1.0, observations=1)
    with pytest.raises(ValidationError, match="increasing"):
        StratifiedEdgesBand(strata=(duplicate, duplicate))
    with pytest.raises(ValidationError):
        StratifiedEdgesBand(strata=())
```

- [ ] **Step 2: 跑測試，確認失敗**

Run: `uv run pytest tests/unit/provenance/test_policy_bands.py -q -o addopts="" -p no:cacheprovider`
Expected: FAIL，`ModuleNotFoundError: No module named 'vcp.provenance.policy_bands'`

- [ ] **Step 3: 寫實作**

建立 `src/vcp/provenance/policy_bands.py`：

```python
"""Uncertainty bands of adaptive provenance policy v2 (spec 2026-10-09 §4.2).

A band says how sure the policy must be before it trusts the cheaper incremental estimate.
Every width comes from calibration residuals; nothing here is a tuned threshold. The decade
rule of the stratified band is a fixed grouping rule, not a fitted boundary.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BandKind = Literal["relative", "stratified_edges"]
BAND_KINDS: tuple[BandKind, ...] = ("relative", "stratified_edges")
CostSide = Literal["incremental", "full"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class RelativeBand(_Strict):
    """Candidate A: each estimate is trusted to within its calibrated relative RMSE."""

    kind: Literal["relative"] = "relative"
    incremental_relative_rmse: float = Field(ge=0.0)
    full_relative_rmse: float = Field(ge=0.0)


class EdgesStratum(_Strict):
    decade: int = Field(ge=0)
    incremental_rmse_ms: float = Field(ge=0.0)
    full_rmse_ms: float = Field(ge=0.0)
    observations: int = Field(ge=1)


class StratifiedEdgesBand(_Strict):
    """Candidate B: an absolute RMSE per order of magnitude of ``total_edges``."""

    kind: Literal["stratified_edges"] = "stratified_edges"
    strata: tuple[EdgesStratum, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _increasing_decades(self) -> StratifiedEdgesBand:
        decades = [stratum.decade for stratum in self.strata]
        if decades != sorted(set(decades)):
            raise ValueError("strata decades must be unique and increasing")
        return self

    def stratum_for(self, total_edges: int) -> EdgesStratum:
        """This size's decade, else the nearest one; a tie takes the larger decade, whose
        RMSE is the larger and so the more cautious."""
        decade = edges_decade(total_edges)
        return min(self.strata, key=lambda stratum: (abs(stratum.decade - decade), -stratum.decade))


Band = Annotated[RelativeBand | StratifiedEdgesBand, Field(discriminator="kind")]


def edges_decade(total_edges: int) -> int:
    """``floor(log10(total_edges))`` from the digit count, so no float rounding; fewer than one
    edge is decade 0."""
    if total_edges < 1:
        return 0
    return len(str(int(total_edges))) - 1


def width(
    band: RelativeBand | StratifiedEdgesBand,
    side: CostSide,
    predicted_ms: float,
    total_edges: int,
) -> float:
    """The band's half-width around one estimate, in milliseconds."""
    if isinstance(band, RelativeBand):
        ratio = band.incremental_relative_rmse if side == "incremental" else band.full_relative_rmse
        return ratio * predicted_ms
    stratum = band.stratum_for(total_edges)
    return stratum.incremental_rmse_ms if side == "incremental" else stratum.full_rmse_ms


def band_allows_incremental(
    band: RelativeBand | StratifiedEdgesBand,
    incremental_ms: float,
    full_ms: float,
    total_edges: int,
) -> bool:
    """INCREMENTAL only when the two estimates stay apart at the band's edges; a tie is FULL.
    A relative full band of 1 or more never lets incremental win -- the safe outcome."""
    if isinstance(band, RelativeBand):
        return incremental_ms * (1.0 + band.incremental_relative_rmse) < full_ms * (
            1.0 - band.full_relative_rmse
        )
    stratum = band.stratum_for(total_edges)
    return incremental_ms + stratum.incremental_rmse_ms < full_ms - stratum.full_rmse_ms


def _pairs(predicted: Sequence[float], actual: Sequence[float]) -> None:
    if not predicted or len(predicted) != len(actual):
        raise ValueError("residuals need equally many, and some, estimates and measurements")


def rmse(predicted: Sequence[float], actual: Sequence[float]) -> float:
    _pairs(predicted, actual)
    total = sum((a - p) ** 2 for p, a in zip(predicted, actual, strict=True))
    return math.sqrt(total / len(predicted))


def relative_rmse(predicted: Sequence[float], actual: Sequence[float]) -> float:
    """RMSE of ``(actual - estimate) / estimate``: the band multiplies the estimate."""
    _pairs(predicted, actual)
    if any(p <= 0 for p in predicted):
        raise ValueError("relative residuals need positive estimates")
    total = sum(((a - p) / p) ** 2 for p, a in zip(predicted, actual, strict=True))
    return math.sqrt(total / len(predicted))


def fit_band(
    kind: str,
    *,
    total_edges: Sequence[int],
    incremental_predicted: Sequence[float],
    incremental_actual: Sequence[float],
    full_predicted: Sequence[float],
    full_actual: Sequence[float],
) -> RelativeBand | StratifiedEdgesBand:
    """One band of ``kind`` from paired estimates and measurements of the same observations."""
    sizes = {len(total_edges), len(incremental_predicted), len(full_predicted)}
    if len(sizes) != 1:
        raise ValueError("every series must cover the same observations")
    if kind == "relative":
        return RelativeBand(
            incremental_relative_rmse=relative_rmse(incremental_predicted, incremental_actual),
            full_relative_rmse=relative_rmse(full_predicted, full_actual),
        )
    if kind == "stratified_edges":
        groups: dict[int, list[int]] = {}
        for index, edges in enumerate(total_edges):
            groups.setdefault(edges_decade(edges), []).append(index)
        return StratifiedEdgesBand(
            strata=tuple(
                EdgesStratum(
                    decade=decade,
                    incremental_rmse_ms=rmse(
                        [incremental_predicted[i] for i in indices],
                        [incremental_actual[i] for i in indices],
                    ),
                    full_rmse_ms=rmse(
                        [full_predicted[i] for i in indices], [full_actual[i] for i in indices]
                    ),
                    observations=len(indices),
                )
                for decade, indices in sorted(groups.items())
            )
        )
    raise ValueError(f"unknown band kind {kind!r}")
```

- [ ] **Step 4: 跑測試，確認通過**

Run: `uv run pytest tests/unit/provenance/test_policy_bands.py -q -o addopts="" -p no:cacheprovider`
Expected: 全部 PASS。

- [ ] **Step 5: lint 並 commit**

```bash
uv run ruff check src/vcp/provenance/policy_bands.py tests/unit/provenance/test_policy_bands.py
uv run ruff format --check src/vcp/provenance/policy_bands.py tests/unit/provenance/test_policy_bands.py
git add src/vcp/provenance/policy_bands.py tests/unit/provenance/test_policy_bands.py
git commit -m "feat(provenance): uncertainty bands of adaptive policy v2"
```

---

### Task 2: 釘住 v1——三份已發布證據的每個決策都要重現

這個測試在動 `strategy.py` 之前就要先綠，之後每個 task 都靠它保證 v1 沒被改到。

**Files:**
- Create: `tests/unit/provenance/test_policy_v1_regression.py`

**Interfaces:**
- Consumes: 現有的 `load_policy_artifact`、`select_strategy`、`MaintenanceFeatures`、`POLICY_VERSION`。
- Produces: 測試函式 `test_v1_policy_reproduces_every_published_decision`（Task 10 的回歸門檻會列它）。

- [ ] **Step 1: 寫測試**

```python
"""Policy v1 stays frozen (spec 2026-10-09 §10): the committed v1 artifact reproduces every
adaptive decision of the three published v1 evidence files, field for field."""

import json
from pathlib import Path

import pytest

from vcp.core.hashing import sha256_file
from vcp.provenance.strategy import (
    POLICY_VERSION,
    MaintenanceFeatures,
    load_policy_artifact,
    select_strategy,
)

BENCHMARKS = Path(__file__).resolve().parents[3] / "docs" / "benchmarks"
ARTIFACTS = BENCHMARKS / "postgres-provenance-calibration-v2-artifacts"
V1_ID = "postgres-adaptive-v1-9f4e58346529"
V1_SHA256 = "a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78"


@pytest.fixture(scope="module")
def v1_policy():
    policy_file = ARTIFACTS / "artifacts" / "provenance_policy" / V1_ID / "policy.json"
    assert sha256_file(policy_file) == V1_SHA256
    policy = load_policy_artifact(ARTIFACTS, V1_ID)
    assert policy.policy_version == POLICY_VERSION
    return policy


@pytest.mark.parametrize(
    ("name", "key", "count"),
    [
        ("postgres-provenance-six-method-v1.json", "results", 108),
        ("postgres-provenance-heldout-v1.json", "results", 108),
        ("postgres-provenance-real-rsna-v1.json", "six_method_benchmark", 3),
    ],
)
def test_v1_policy_reproduces_every_published_decision(v1_policy, name, key, count):
    document = json.loads((BENCHMARKS / name).read_text(encoding="utf-8"))
    rows = [row for row in document[key] if row["method"] == "postgres_adaptive"]
    assert len(rows) == count
    for row in rows:
        assert (row["policy_id"], row["policy_sha256"]) == (V1_ID, V1_SHA256)
        features = MaintenanceFeatures.model_validate(
            {field: row[field] for field in MaintenanceFeatures.model_fields}
        )
        decision = select_strategy("auto", features, v1_policy)
        assert (
            decision.selected_strategy.value,
            decision.reason,
            decision.policy_version,
            decision.estimated_incremental_ms,
            decision.estimated_full_ms,
        ) == (
            row["selected_strategy"],
            row["strategy_reason"],
            row["policy_version"],
            row["estimated_incremental_ms"],
            row["estimated_full_ms"],
        ), row["scenario_hash"]
```

- [ ] **Step 2: 跑測試，確認現在就通過**

Run: `uv run pytest tests/unit/provenance/test_policy_v1_regression.py -q -o addopts="" -p no:cacheprovider`
Expected: 3 passed。這是回歸釘，**必須在任何 strategy 改動之前就綠**。若失敗，先停下來查（可能是 artifact 路徑或 JSON 結構與預期不同），不要往下做。

- [ ] **Step 3: commit**

```bash
uv run ruff check tests/unit/provenance/test_policy_v1_regression.py
uv run ruff format --check tests/unit/provenance/test_policy_v1_regression.py
git add tests/unit/provenance/test_policy_v1_regression.py
git commit -m "test(provenance): pin every published policy v1 decision"
```

---

### Task 3: `strategy.py` 的 v2——模型、擬合、載入、寫入與選擇

**Files:**
- Modify: `src/vcp/provenance/strategy.py`
- Test: `tests/unit/provenance/test_strategy_v2.py`（新）

**Interfaces:**
- Consumes: Task 1 的 `Band`、`BandKind`、`BAND_KINDS`、`band_allows_incremental`、`fit_band`。
- Produces:
  - 常數 `POLICY_VERSION_V2 = "postgres-adaptive-v2"`、`SUPPORTED_POLICY_VERSIONS = (POLICY_VERSION, POLICY_VERSION_V2)`、`HELDOUT_V2_SEEDS = (20261101, 20261102)`
  - `FittedCostModel`（frozen dataclass：`model: CostModel`、`rmse_ms: float`、`predicted: tuple[float, ...]`、`actual: tuple[float, ...]`）
  - `fit_cost_models(rows: Sequence[CalibrationObservation]) -> tuple[FittedCostModel, FittedCostModel]`（incremental, full；不驗證 evidence，給交叉驗證的單一 fold 用）
  - `AdaptivePolicyV2`（欄位見 spec §4.1；`.id` 為 `postgres-adaptive-v2-<sha12>`）
  - `ProvenancePolicy = AdaptivePolicy | AdaptivePolicyV2`
  - `policy_from_payload(payload) -> ProvenancePolicy`
  - `fit_policy_v2(calibration_rows, *, band: str) -> AdaptivePolicyV2`
  - `select_strategy`、`load_policy_artifact`、`write_policy_artifact` 接受兩版。

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/unit/provenance/test_strategy_v2.py`：

```python
"""Adaptive policy v2 in strategy.py (spec 2026-10-09 §4)."""

import json

import pytest
from pydantic import ValidationError

from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.provenance.policy_bands import EdgesStratum, RelativeBand, StratifiedEdgesBand
from vcp.provenance.strategy import (
    FULL_FEATURE_ORDER,
    HELDOUT_SEEDS,
    HELDOUT_V2_SEEDS,
    INCREMENTAL_FEATURE_ORDER,
    POLICY_VERSION_V2,
    AdaptivePolicy,
    AdaptivePolicyV2,
    CalibrationObservation,
    CostModel,
    MaintenanceFeatures,
    _policy_spec,
    calibration_evidence,
    fit_cost_models,
    fit_policy,
    fit_policy_v2,
    load_policy_artifact,
    policy_from_payload,
    select_strategy,
    write_policy_artifact,
)


def _features(total_edges=1500):
    return MaintenanceFeatures(
        changed_samples=12,
        dirty_entities=40,
        total_entities=200,
        dirty_ratio=0.2,
        total_edges=total_edges,
        historical_changes=80,
        head_count=3,
    )


def _models(incremental_ms, full_ms):
    return (
        CostModel(
            feature_order=INCREMENTAL_FEATURE_ORDER,
            coefficients={name: 0.0 for name in INCREMENTAL_FEATURE_ORDER},
            intercept_ms=incremental_ms,
        ),
        CostModel(
            feature_order=FULL_FEATURE_ORDER,
            coefficients={name: 0.0 for name in FULL_FEATURE_ORDER},
            intercept_ms=full_ms,
        ),
    )


def _v2(band, calibration_sha256="c" * 64, incremental_ms=100.0, full_ms=150.0):
    incremental, full = _models(incremental_ms, full_ms)
    return AdaptivePolicyV2(
        backend_schema_version=1,
        postgresql_major=17,
        benchmark_schema_version=1,
        environment_fingerprint="env-fixture-v1",
        calibration_sha256=calibration_sha256,
        incremental_model=incremental,
        full_model=full,
        band=band,
        training_row_count=18,
    )


def _observations():
    """Synthetic paired rows over three size decades, both calibration seeds; multiplicative
    noise so both candidate bands fit."""
    rows = []
    for seed in (20260913, 20260914):
        for decade, edges in ((3, 1500), (4, 15_000), (5, 150_000)):
            for i in range(1, 7):
                total = edges + 7 * i
                changed = 10 * i
                noise = 1.1 if (i + seed) % 2 else 0.9
                rows.append(
                    CalibrationObservation(
                        scenario_id=f"unit-{seed}-{decade}-{i}",
                        scenario_hash=f"{seed}{decade}{i:02d}".rjust(64, "0"),
                        workload_hash=f"{seed}{decade}{i:02d}".rjust(64, "1"),
                        seed=seed,
                        changed_samples=changed,
                        dirty_entities=changed,
                        total_entities=total,
                        dirty_ratio=changed / total,
                        total_edges=total,
                        historical_changes=total // 2,
                        head_count=1,
                        incremental_p50_ms=(1.2 * changed + 0.12 * total) * noise,
                        full_p50_ms=(0.7 * total + 0.1 * (total // 2)) * noise,
                        environment_fingerprint="a" * 64,
                        backend_schema_version=1,
                        postgresql_major=17,
                        benchmark_schema_version=1,
                    )
                )
    return rows


def test_heldout_v2_seeds_are_new():
    assert HELDOUT_V2_SEEDS == (20261101, 20261102)
    assert not set(HELDOUT_V2_SEEDS) & {20260908, 20260909, 20260913, 20260914, *HELDOUT_SEEDS}


def test_v2_relative_band_decides_and_keeps_the_v1_reasons():
    policy = _v2(RelativeBand(incremental_relative_rmse=0.2, full_relative_rmse=0.1))
    decision = select_strategy("auto", _features(), policy)
    assert decision.selected_strategy.value == "INCREMENTAL"  # 120 < 135
    assert decision.reason == "calibrated_incremental_lower_confident_cost"
    assert decision.policy_version == POLICY_VERSION_V2
    assert (decision.estimated_incremental_ms, decision.estimated_full_ms) == (100.0, 150.0)
    wide = _v2(RelativeBand(incremental_relative_rmse=0.5, full_relative_rmse=0.0))
    decision = select_strategy("auto", _features(), wide)
    assert decision.selected_strategy.value == "FULL"  # 150 < 150 is false: a tie is FULL
    assert decision.reason == "calibrated_full_lower_or_uncertain_cost"


def test_v2_stratified_band_uses_the_features_size_decade():
    band = StratifiedEdgesBand(
        strata=(
            EdgesStratum(decade=3, incremental_rmse_ms=10.0, full_rmse_ms=20.0, observations=6),
            EdgesStratum(decade=5, incremental_rmse_ms=1000.0, full_rmse_ms=2000.0, observations=6),
        )
    )
    policy = _v2(band)
    assert select_strategy("auto", _features(1500), policy).selected_strategy.value == "INCREMENTAL"
    assert select_strategy("auto", _features(150_000), policy).selected_strategy.value == "FULL"


def test_fixed_requests_no_op_and_fallback_are_the_same_for_v2():
    policy = _v2(RelativeBand(incremental_relative_rmse=0.0, full_relative_rmse=0.0))
    assert select_strategy("full", _features(), policy).reason == "requested_full"
    assert select_strategy("incremental", _features(), policy).reason == "requested_incremental"
    zero = _features().model_copy(update={"changed_samples": 0, "dirty_entities": 0})
    assert select_strategy("auto", zero, policy).reason == "verified_zero_semantic_changes"


def test_v1_and_v2_payloads_do_not_mix():
    v2 = _v2(RelativeBand(incremental_relative_rmse=0.1, full_relative_rmse=0.1))
    payload = v2.model_dump(mode="json")
    with pytest.raises(ValidationError, match="extra"):
        AdaptivePolicyV2.model_validate({**payload, "incremental_rmse_ms": 1.0})
    v1_payload = {key: value for key, value in payload.items() if key != "band"}
    v1_payload.update(policy_version="postgres-adaptive-v1", incremental_rmse_ms=1.0, full_rmse_ms=1.0)
    with pytest.raises(ValidationError, match="extra"):
        AdaptivePolicy.model_validate({**v1_payload, "band": payload["band"]})
    assert isinstance(policy_from_payload(payload), AdaptivePolicyV2)
    assert isinstance(policy_from_payload(v1_payload), AdaptivePolicy)
    assert v2.id == "postgres-adaptive-v2-" + "c" * 12


def test_fit_cost_models_and_fit_policy_v2_keep_the_v1_cost_models():
    rows = _observations()
    v1 = fit_policy(rows)
    # fit_policy fits the evidence's sorted rows; float sums depend on order, so fit the same
    incremental, full = fit_cost_models(calibration_evidence(rows).observations)
    assert (incremental.model, full.model) == (v1.incremental_model, v1.full_model)
    assert (incremental.rmse_ms, full.rmse_ms) == (v1.incremental_rmse_ms, v1.full_rmse_ms)
    for band in ("relative", "stratified_edges"):
        v2 = fit_policy_v2(rows, band=band)
        assert v2.band.kind == band
        assert (v2.incremental_model, v2.full_model) == (v1.incremental_model, v1.full_model)
        assert v2.calibration_sha256 == v1.calibration_sha256
        assert v2.training_row_count == len(rows)
    stratified = fit_policy_v2(rows, band="stratified_edges")
    assert [s.decade for s in stratified.band.strata] == [3, 4, 5]


def test_fit_policy_v2_rejects_an_unknown_band():
    with pytest.raises(ValidationFailed, match="unsupported_band"):
        fit_policy_v2(_observations(), band="absolute")


def _calibration(roots):
    path = roots.data / "inputs" / "calibration-result.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"scenario_ids": ["cal-1"], "scenario_hashes": ["a" * 64]}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _compatibility():
    return dict(
        backend_schema_version=1,
        postgresql_major=17,
        benchmark_schema_version=1,
        environment_fingerprint="env-fixture-v1",
    )


def test_v2_policy_artifact_round_trips(roots):
    calibration = _calibration(roots)
    policy = _v2(
        RelativeBand(incremental_relative_rmse=0.1, full_relative_rmse=0.2),
        calibration_sha256=sha256_file(calibration),
    )
    policy_id = write_policy_artifact(roots.data, policy, calibration)
    assert policy_id.startswith("postgres-adaptive-v2-")
    assert load_policy_artifact(roots.data, policy_id, **_compatibility()) == policy
    assert write_policy_artifact(roots.data, policy, calibration) == policy_id


def test_an_unknown_policy_version_is_incompatible_on_load(roots):
    calibration = _calibration(roots)
    incremental, full = _models(1.0, 2.0)
    unknown = AdaptivePolicy(
        policy_version="postgres-adaptive-v9",
        backend_schema_version=1,
        postgresql_major=17,
        benchmark_schema_version=1,
        environment_fingerprint="env-fixture-v1",
        calibration_sha256=sha256_file(calibration),
        incremental_model=incremental,
        full_model=full,
        incremental_rmse_ms=1.0,
        full_rmse_ms=1.0,
        training_row_count=18,
    )
    with pytest.raises(ValidationFailed, match="incompatible_policy"):
        write_policy_artifact(roots.data, unknown, calibration)
    # Publish it by hand, as only a foreign writer could, to reach the loader's own check.
    with ArtifactWriter.create(_policy_spec(unknown, calibration), data_root=roots.data) as writer:
        writer.write_json("policy.json", unknown.model_dump(mode="json"))
        writer.add_file("calibration.json", calibration)
        writer.commit()
    with pytest.raises(ValidationFailed, match="incompatible_policy: provenance policy policy version"):
        load_policy_artifact(roots.data, unknown.id, **_compatibility())
```

- [ ] **Step 2: 跑測試，確認失敗**

Run: `uv run pytest tests/unit/provenance/test_strategy_v2.py -q -o addopts="" -p no:cacheprovider`
Expected: FAIL，`ImportError: cannot import name 'HELDOUT_V2_SEEDS'`。

- [ ] **Step 3: 實作——常數與 import**

`src/vcp/provenance/strategy.py` 頂端：

把

```python
import json
import math
import re
from pathlib import Path
from typing import Literal
```

換成

```python
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
```

在 `from vcp.provenance.backend import BackendName, RequestedStrategy, SelectedStrategy` 之後加：

```python
from vcp.provenance.policy_bands import BAND_KINDS, Band, band_allows_incremental, fit_band
```

把

```python
POLICY_VERSION = "postgres-adaptive-v1"
```

換成

```python
POLICY_VERSION = "postgres-adaptive-v1"
POLICY_VERSION_V2 = "postgres-adaptive-v2"
SUPPORTED_POLICY_VERSIONS = (POLICY_VERSION, POLICY_VERSION_V2)
```

把

```python
HELDOUT_SEEDS = (20261001, 20261002)
```

換成

```python
HELDOUT_SEEDS = (20261001, 20261002)
# Policy v2's held-out seeds (spec 2026-10-09 §6.3): never measured before; the v1 held-out
# seeds above shaped v2's design and can no longer stand for unseen data.
HELDOUT_V2_SEEDS = (20261101, 20261102)
```

在 `_derived_policy_id` 之後加：

```python
def _derived_policy_v2_id(calibration_sha256: str) -> str:
    return f"postgres-adaptive-v2-{calibration_sha256[:12]}"
```

- [ ] **Step 4: 實作——`AdaptivePolicyV2` 與 `policy_from_payload`**

在 `class AdaptivePolicy` 整個類別之後（`class StrategyDecision` 之前）加。v1 類別本身不動，所以這裡的兩個驗證器是刻意複製的（spec §4.1：v1 一字不改）：

```python
class AdaptivePolicyV2(_Strict):
    """Policy v2 (spec 2026-10-09 §4): v1's two cost models with a band derived from the
    calibration residuals -- relative to each estimate, or per order of magnitude of the
    graph -- chosen by the pre-registered comparison of §5."""

    policy_version: Literal["postgres-adaptive-v2"] = POLICY_VERSION_V2
    backend: BackendName = BackendName.POSTGRESQL
    backend_schema_version: int = Field(ge=1)
    postgresql_major: int = Field(ge=1)
    benchmark_schema_version: int = Field(ge=1)
    environment_fingerprint: str = Field(min_length=1)
    calibration_sha256: str
    incremental_model: CostModel
    full_model: CostModel
    band: Band
    training_row_count: int = Field(ge=1)

    @field_validator("calibration_sha256")
    @classmethod
    def _valid_sha256(cls, value: str) -> str:
        if not _LOWER_SHA256.fullmatch(value):
            raise ValueError("calibration_sha256 must be 64 lowercase hex characters")
        return value

    @model_validator(mode="after")
    def _fixed_feature_orders(self) -> AdaptivePolicyV2:
        if self.incremental_model.feature_order != INCREMENTAL_FEATURE_ORDER:
            raise ValueError("incremental model feature_order is incompatible")
        if self.full_model.feature_order != FULL_FEATURE_ORDER:
            raise ValueError("full model feature_order is incompatible")
        return self

    @property
    def id(self) -> str:
        return _derived_policy_v2_id(self.calibration_sha256)


ProvenancePolicy = AdaptivePolicy | AdaptivePolicyV2


def policy_from_payload(payload: object) -> ProvenancePolicy:
    """Validate a ``policy.json`` payload with the model its ``policy_version`` names (spec
    2026-10-09 §4.4). Any other version validates as v1 and then fails the version check of
    whoever loads it, exactly as before v2 existed."""
    if isinstance(payload, Mapping) and payload.get("policy_version") == POLICY_VERSION_V2:
        return AdaptivePolicyV2.model_validate(payload)
    return AdaptivePolicy.model_validate(payload)
```

- [ ] **Step 5: 實作——擬合**

把整個 `def fit_policy(calibration_rows) -> AdaptivePolicy:` 函式換成下面三段。`fit_cost_models` 的算式與 v1 原本的內部函式逐項相同（同樣的迴歸、同樣的 RMSE 算法），所以 v1 的擬合結果不變：

```python
@dataclass(frozen=True)
class FittedCostModel:
    model: CostModel
    rmse_ms: float
    predicted: tuple[float, ...]
    actual: tuple[float, ...]


def fit_cost_models(
    rows: Sequence[CalibrationObservation],
) -> tuple[FittedCostModel, FittedCostModel]:
    """The incremental and the full cost model, fitted as v1 fits them (nonnegative linear
    regression in frozen feature order). Nothing is validated here: callers pass a whole
    verified calibration or one fold of a cross-validation (spec 2026-10-09 §5.3).

    The numerical dependency is imported only when fitting. Held-out runners use artifact
    loading and selection, and never invoke this API.
    """
    from sklearn.linear_model import LinearRegression

    if not rows:
        raise ValueError("no calibration observations")

    def fit(order, target):
        matrix = [[float(getattr(row, feature)) for feature in order] for row in rows]
        values = [getattr(row, target) for row in rows]
        fitted = LinearRegression(positive=True).fit(matrix, values)
        model = CostModel(
            feature_order=order,
            coefficients=dict(zip(order, fitted.coef_, strict=True)),
            intercept_ms=float(fitted.intercept_),
        )
        predicted = tuple(model.predict(row) for row in rows)
        rmse = math.sqrt(
            sum((p - value) ** 2 for p, value in zip(predicted, values, strict=True)) / len(rows)
        )
        return FittedCostModel(model, rmse, predicted, tuple(values))

    return (
        fit(INCREMENTAL_FEATURE_ORDER, "incremental_p50_ms"),
        fit(FULL_FEATURE_ORDER, "full_p50_ms"),
    )


def fit_policy(calibration_rows) -> AdaptivePolicy:
    """Fit nonnegative models on calibration observations only, in frozen order."""
    evidence = calibration_evidence(calibration_rows)
    rows = evidence.observations
    assert rows
    incremental, full = fit_cost_models(rows)
    first = rows[0]
    return AdaptivePolicy(
        backend_schema_version=first.backend_schema_version,
        postgresql_major=first.postgresql_major,
        benchmark_schema_version=first.benchmark_schema_version,
        environment_fingerprint=first.environment_fingerprint,
        calibration_sha256=sha256_text(calibration_text(evidence)),
        incremental_model=incremental.model,
        full_model=full.model,
        incremental_rmse_ms=incremental.rmse_ms,
        full_rmse_ms=full.rmse_ms,
        training_row_count=len(rows),
    )


def fit_policy_v2(calibration_rows, *, band: str) -> AdaptivePolicyV2:
    """Policy v2 from the same calibration as v1: the same cost models, plus the band the
    pre-registered comparison chose (spec 2026-10-09 §4.3, §13)."""
    if band not in BAND_KINDS:
        raise ValidationFailed(f"unsupported_band: {band}")
    evidence = calibration_evidence(calibration_rows)
    rows = evidence.observations
    assert rows
    incremental, full = fit_cost_models(rows)
    try:
        fitted = fit_band(
            band,
            total_edges=[row.total_edges for row in rows],
            incremental_predicted=incremental.predicted,
            incremental_actual=incremental.actual,
            full_predicted=full.predicted,
            full_actual=full.actual,
        )
    except ValueError:
        raise ValidationFailed("invalid_calibration_rows") from None
    first = rows[0]
    return AdaptivePolicyV2(
        backend_schema_version=first.backend_schema_version,
        postgresql_major=first.postgresql_major,
        benchmark_schema_version=first.benchmark_schema_version,
        environment_fingerprint=first.environment_fingerprint,
        calibration_sha256=sha256_text(calibration_text(evidence)),
        incremental_model=incremental.model,
        full_model=full.model,
        band=fitted,
        training_row_count=len(rows),
    )
```

- [ ] **Step 6: 實作——選擇**

在 `select_strategy` 裡，把簽名的 `policy: AdaptivePolicy | None,` 換成 `policy: ProvenancePolicy | None,`，再把

```python
        assert incremental_ms is not None and full_ms is not None
        incremental_is_confidently_lower = (
            incremental_ms + policy.incremental_rmse_ms < full_ms - policy.full_rmse_ms
        )
```

換成

```python
        assert incremental_ms is not None and full_ms is not None
        if isinstance(policy, AdaptivePolicyV2):
            incremental_is_confidently_lower = band_allows_incremental(
                policy.band, incremental_ms, full_ms, features.total_edges
            )
        else:
            incremental_is_confidently_lower = (
                incremental_ms + policy.incremental_rmse_ms < full_ms - policy.full_rmse_ms
            )
```

- [ ] **Step 7: 實作——artifact 的寫入與載入**

1. `_policy_spec`、`_policy_sha256`、`_check_calibration_policy` 的參數型別 `AdaptivePolicy` 換成 `ProvenancePolicy`（內容不變：它們只用兩版共有的欄位）。
2. `write_policy_artifact`：把

```python
def write_policy_artifact(data_root: Path, policy: AdaptivePolicy, calibration_path: Path) -> str:
    """Publish or idempotently reuse a policy derived from one pinned calibration JSON."""
    try:
        policy = AdaptivePolicy.model_validate(policy.model_dump(mode="json"))
    except (TypeError, ValueError):
        raise ValidationFailed("bad provenance policy payload") from None
    if (
        policy.policy_version != POLICY_VERSION
```

換成

```python
def write_policy_artifact(
    data_root: Path, policy: ProvenancePolicy, calibration_path: Path
) -> str:
    """Publish or idempotently reuse a policy derived from one pinned calibration JSON."""
    model = AdaptivePolicyV2 if isinstance(policy, AdaptivePolicyV2) else AdaptivePolicy
    try:
        policy = model.model_validate(policy.model_dump(mode="json"))
    except (TypeError, ValueError):
        raise ValidationFailed("bad provenance policy payload") from None
    expected_version = POLICY_VERSION_V2 if model is AdaptivePolicyV2 else POLICY_VERSION
    if (
        policy.policy_version != expected_version
```

3. `load_policy_artifact`：回傳型別 `-> AdaptivePolicy:` 換成 `-> ProvenancePolicy:`；把

```python
    try:
        policy = AdaptivePolicy.model_validate_json(
            (directory / POLICY_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError):
        raise IntegrityError("mismatch: provenance policy payload") from None
```

換成

```python
    try:
        policy = policy_from_payload(
            json.loads((directory / POLICY_FILE).read_text(encoding="utf-8"))
        )
    except (OSError, ValidationError, ValueError, TypeError):
        raise IntegrityError("mismatch: provenance policy payload") from None
```

再把相容性檢查裡的

```python
        "policy version": policy.policy_version == POLICY_VERSION,
```

換成

```python
        "policy version": policy.policy_version in SUPPORTED_POLICY_VERSIONS,
```

4. `__all__` 加入：`"HELDOUT_V2_SEEDS"`、`"POLICY_VERSION_V2"`、`"SUPPORTED_POLICY_VERSIONS"`、`"AdaptivePolicyV2"`、`"FittedCostModel"`、`"ProvenancePolicy"`、`"fit_cost_models"`、`"fit_policy_v2"`、`"policy_from_payload"`（照字母順序插入既有清單）。

- [ ] **Step 8: 跑新測試、v1 回歸與所有 provenance 測試**

Run: `uv run pytest tests/unit/provenance -q -o addopts="" -p no:cacheprovider`
Expected: 全部 PASS，包括 Task 2 的 `test_policy_v1_regression.py`（v1 決策逐位元相同）與既有的 `test_strategy.py`、`test_adaptive_evaluation.py`。

- [ ] **Step 9: lint 並 commit**

```bash
uv run ruff check src/vcp/provenance/strategy.py tests/unit/provenance/test_strategy_v2.py
uv run ruff format --check src/vcp/provenance/strategy.py tests/unit/provenance/test_strategy_v2.py
git add src/vcp/provenance/strategy.py tests/unit/provenance/test_strategy_v2.py
git commit -m "feat(provenance): adaptive policy v2 model, fit and dispatch"
```

---

### Task 4: 預先登記的候選帶比較腳本 `compare_bands.py`

這個 task 只寫腳本與測試、commit；**不得**對真正的 calibration 執行（那是 Task 5，必須在本 task 的 commit 之後）。

**Files:**
- Create: `tests/performance/provenance/compare_bands.py`
- Test: `tests/unit/provenance/test_compare_bands.py`

**Interfaces:**
- Consumes: Task 1 的 `edges_decade`、`fit_band`、`width`、`band_allows_incremental`；Task 3 的 `fit_cost_models`、`CALIBRATION_SEEDS`、`CalibrationEvidence`、`CalibrationObservation`。
- Produces:
  - `KIND = "postgres-provenance-band-comparison-v1"`、`TIE = 0.1`
  - `distance(s: float) -> float`、`choose(score_relative: float, score_stratified: float) -> str | None`
  - `seed_crossvalidation(observations, kind) -> list[dict]`、`leave_one_decade_out(observations, kind) -> list[dict]`
  - `compare(observations, *, calibration_sha256: str, commit: str) -> dict`（輸出文件的鍵：`kind`、`calibration_sha256`、`script_commit`、`tie`、`candidates`、`scores`、`winner`、`leave_one_decade_out`、`calibration_decisions`、`full_data_bands`、`v1_rmse_ms`）
  - `main(argv=None) -> int`

- [ ] **Step 1: 寫失敗的測試**

```python
"""The pre-registered band comparison (spec 2026-10-09 §5), on synthetic rows only."""

import json
import math

import pytest

from performance.provenance import compare_bands
from vcp.provenance.strategy import CalibrationObservation


def _rows(noise):
    """Two seeds, three size decades; ``noise(i, seed, predicted)`` gives the measurement."""
    rows = []
    for seed in (20260913, 20260914):
        for decade, edges in ((3, 1500), (4, 15_000), (5, 150_000)):
            for i in range(1, 9):
                total = edges + 7 * i
                changed = 10 * i
                incremental = 1.2 * changed + 0.12 * total
                full = 0.7 * total + 0.1 * (total // 2)
                rows.append(
                    CalibrationObservation(
                        scenario_id=f"unit-{seed}-{decade}-{i}",
                        scenario_hash=f"{seed}{decade}{i:02d}".rjust(64, "0"),
                        workload_hash=f"{seed}{decade}{i:02d}".rjust(64, "1"),
                        seed=seed,
                        changed_samples=changed,
                        dirty_entities=changed,
                        total_entities=total,
                        dirty_ratio=changed / total,
                        total_edges=total,
                        historical_changes=total // 2,
                        head_count=1,
                        incremental_p50_ms=noise(i, seed, incremental),
                        full_p50_ms=noise(i, seed, full),
                        environment_fingerprint="a" * 64,
                        backend_schema_version=1,
                        postgresql_major=17,
                        benchmark_schema_version=1,
                    )
                )
    return rows


def _multiplicative(i, seed, value):
    return value * (1.1 if (i + seed) % 2 else 0.9)


def _additive(i, seed, value):
    return value + (50.0 if (i + seed) % 2 else -50.0)


def test_distance_and_choose():
    assert compare_bands.distance(1.0) == 0.0
    assert compare_bands.distance(math.e) == pytest.approx(1.0)
    assert compare_bands.distance(0.0) == math.inf
    assert compare_bands.distance(math.inf) == math.inf
    assert compare_bands.choose(0.30, 0.25) == "relative"  # within 0.1: the simpler A
    assert compare_bands.choose(0.50, 0.30) == "stratified_edges"
    assert compare_bands.choose(0.20, 0.50) == "relative"
    assert compare_bands.choose(math.inf, 0.4) == "stratified_edges"
    assert compare_bands.choose(math.inf, math.inf) is None


def test_seed_crossvalidation_covers_both_directions_models_and_decades():
    cells = compare_bands.seed_crossvalidation(_rows(_multiplicative), "relative")
    assert {cell["split"] for cell in cells} == {
        "train-20260913-test-20260914",
        "train-20260914-test-20260913",
    }
    assert {cell["model"] for cell in cells} == {"incremental", "full"}
    assert {cell["decade"] for cell in cells} == {3, 4, 5}
    assert len(cells) == 2 * 2 * 3
    assert all(cell["n"] == 8 for cell in cells)


def test_multiplicative_noise_chooses_the_relative_band():
    document = compare_bands.compare(
        _rows(_multiplicative), calibration_sha256="c" * 64, commit="d" * 40
    )
    assert document["winner"] == "relative"


def test_constant_noise_favours_the_stratified_band():
    document = compare_bands.compare(_rows(_additive), calibration_sha256="c" * 64, commit="d" * 40)
    assert document["scores"]["relative"] - document["scores"]["stratified_edges"] >= 0.1
    assert document["winner"] == "stratified_edges"


def test_document_shape_and_determinism():
    rows = _rows(_multiplicative)
    first = compare_bands.compare(rows, calibration_sha256="c" * 64, commit="d" * 40)
    second = compare_bands.compare(rows, calibration_sha256="c" * 64, commit="d" * 40)
    assert first == second
    assert first["kind"] == compare_bands.KIND
    assert (first["calibration_sha256"], first["script_commit"], first["tie"]) == (
        "c" * 64,
        "d" * 40,
        0.1,
    )
    assert set(first) == {
        "kind",
        "calibration_sha256",
        "script_commit",
        "tie",
        "candidates",
        "scores",
        "winner",
        "leave_one_decade_out",
        "calibration_decisions",
        "full_data_bands",
        "v1_rmse_ms",
    }
    assert set(first["calibration_decisions"]) == {"relative", "stratified_edges"}
    assert {"3", "4", "5"} <= set(first["calibration_decisions"]["relative"])
    json.dumps(first, allow_nan=False)  # every infinity is spelled out as a string


def test_main_refuses_an_existing_output_and_an_uncommitted_script(tmp_path, monkeypatch, capsys):
    calibration = tmp_path / "calibration.json"
    calibration.write_text("{}", encoding="utf-8")
    existing = tmp_path / "exists.json"
    existing.write_text("{}", encoding="utf-8")
    assert compare_bands.main(["--calibration", str(calibration), "--output", str(existing)]) == 1

    def dirty():
        raise RuntimeError("uncommitted")

    monkeypatch.setattr(compare_bands, "pinned_commit", dirty)
    output = tmp_path / "out.json"
    assert compare_bands.main(["--calibration", str(calibration), "--output", str(output)]) == 1
    assert not output.exists()
    assert "status=FAIL" in capsys.readouterr().err
```

- [ ] **Step 2: 跑測試，確認失敗**

Run: `uv run pytest tests/unit/provenance/test_compare_bands.py -q -o addopts="" -p no:cacheprovider`
Expected: FAIL，`ImportError: cannot import name 'compare_bands'`。

- [ ] **Step 3: 寫實作**

建立 `tests/performance/provenance/compare_bands.py`：

```python
"""Pre-registered comparison of policy v2's candidate bands (spec 2026-10-09 §5).

Only calibration evidence goes in. The two seed-crossvalidated directions decide; the
leave-one-decade-out cells and the in-sample decisions are reported, never used to decide.
The script refuses to run while it, the band module or strategy.py has uncommitted changes,
so the commit that holds them predates every result (§5.5).

Run (after the commit that adds this file):
    uv run python tests/performance/provenance/compare_bands.py \
        --calibration docs/benchmarks/postgres-provenance-calibration-v2-artifacts/inputs/calibration.json \
        --output docs/benchmarks/postgres-provenance-band-comparison-v1.json
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

from vcp.core.atomic import write_once_text
from vcp.core.hashing import sha256_file
from vcp.provenance.policy_bands import band_allows_incremental, edges_decade, fit_band, width
from vcp.provenance.strategy import (
    CALIBRATION_SEEDS,
    CalibrationEvidence,
    CalibrationObservation,
    fit_cost_models,
)

KIND = "postgres-provenance-band-comparison-v1"
TIE = 0.1
CANDIDATES = ("relative", "stratified_edges")
_SIDES = (("incremental", "incremental_p50_ms"), ("full", "full_p50_ms"))
REPOSITORY = Path(__file__).resolve().parents[3]
PINNED = (
    "tests/performance/provenance/compare_bands.py",
    "src/vcp/provenance/policy_bands.py",
    "src/vcp/provenance/strategy.py",
)


def load_observations(path: Path) -> tuple[CalibrationObservation, ...]:
    evidence = CalibrationEvidence.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if not evidence.observations:
        raise ValueError("calibration evidence without observations")
    return evidence.observations


def distance(s: float) -> float:
    """``|ln s|``: how far the band's width is from the error it is meant to cover."""
    if s == 0 or math.isinf(s) or math.isnan(s):
        return math.inf
    return abs(math.log(s))


def choose(score_relative: float, score_stratified: float) -> str | None:
    """§5.3 step 6: within ``TIE`` the simpler relative band; else the lower score; None when
    neither band ever had a width."""
    if math.isinf(score_relative) and math.isinf(score_stratified):
        return None
    if abs(score_relative - score_stratified) < TIE:
        return "relative"
    return "relative" if score_relative < score_stratified else "stratified_edges"


def _fit(train, kind):
    incremental, full = fit_cost_models(train)
    band = fit_band(
        kind,
        total_edges=[row.total_edges for row in train],
        incremental_predicted=incremental.predicted,
        incremental_actual=incremental.actual,
        full_predicted=full.predicted,
        full_actual=full.actual,
    )
    return {"incremental": incremental.model, "full": full.model}, band


def _cells(train, test, kind, split):
    """Standardized residuals of ``test`` under models and band fitted on ``train`` only."""
    models, band = _fit(train, kind)
    cells = []
    for side, target in _SIDES:
        by_decade: dict[int, list[float]] = {}
        for row in test:
            predicted = models[side].predict(row)
            half_width = width(band, side, predicted, row.total_edges)
            z = math.inf if half_width <= 0 else (getattr(row, target) - predicted) / half_width
            by_decade.setdefault(edges_decade(row.total_edges), []).append(z)
        for decade, values in sorted(by_decade.items()):
            s = math.sqrt(sum(z * z for z in values) / len(values))
            cells.append({"split": split, "model": side, "decade": decade, "n": len(values), "s": s})
    return cells


def seed_crossvalidation(observations, kind):
    first, second = CALIBRATION_SEEDS
    cells = []
    for train_seed, test_seed in ((first, second), (second, first)):
        train = [row for row in observations if row.seed == train_seed]
        test = [row for row in observations if row.seed == test_seed]
        cells += _cells(train, test, kind, f"train-{train_seed}-test-{test_seed}")
    return cells


def leave_one_decade_out(observations, kind):
    cells = []
    for decade in sorted({edges_decade(row.total_edges) for row in observations}):
        train = [row for row in observations if edges_decade(row.total_edges) != decade]
        test = [row for row in observations if edges_decade(row.total_edges) == decade]
        cells += _cells(train, test, kind, f"without-decade-{decade}")
    return cells


def _score(cells) -> float:
    return max(distance(cell["s"]) for cell in cells)


def _decisions(observations, kind):
    """In-sample: what the band fitted on every observation would choose for each of them."""
    models, band = _fit(observations, kind)
    summary: dict[str, dict[str, float]] = {}
    for row in observations:
        incremental = models["incremental"].predict(row)
        full = models["full"].predict(row)
        chose_incremental = band_allows_incremental(band, incremental, full, row.total_edges)
        chosen = row.incremental_p50_ms if chose_incremental else row.full_p50_ms
        best = min(row.incremental_p50_ms, row.full_p50_ms)
        cell = summary.setdefault(
            str(edges_decade(row.total_edges)),
            {"n": 0, "incremental": 0, "full": 0, "slower_choice": 0, "extra_ms": 0.0},
        )
        cell["n"] += 1
        cell["incremental" if chose_incremental else "full"] += 1
        cell["slower_choice"] += int(chosen > best)
        cell["extra_ms"] += chosen - best
    return summary, band


def _jsonable(value):
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def compare(observations, *, calibration_sha256: str, commit: str) -> dict:
    observations = list(observations)
    candidates = {kind: seed_crossvalidation(observations, kind) for kind in CANDIDATES}
    scores = {kind: _score(cells) for kind, cells in candidates.items()}
    decisions, bands = {}, {}
    for kind in CANDIDATES:
        decisions[kind], band = _decisions(observations, kind)
        bands[kind] = band.model_dump(mode="json")
    incremental, full = fit_cost_models(observations)
    return _jsonable(
        {
            "kind": KIND,
            "calibration_sha256": calibration_sha256,
            "script_commit": commit,
            "tie": TIE,
            "candidates": candidates,
            "scores": scores,
            "winner": choose(scores["relative"], scores["stratified_edges"]),
            "leave_one_decade_out": {
                kind: leave_one_decade_out(observations, kind) for kind in CANDIDATES
            },
            "calibration_decisions": decisions,
            "full_data_bands": bands,
            "v1_rmse_ms": {"incremental": incremental.rmse_ms, "full": full.rmse_ms},
        }
    )


def pinned_commit() -> str:
    """HEAD, after checking that the files this comparison depends on are committed there."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=REPOSITORY, capture_output=True, text=True, check=True
        ).stdout.strip()

    if git("status", "--porcelain", "--", *PINNED):
        raise RuntimeError("commit compare_bands.py, policy_bands.py and strategy.py first")
    return git("rev-parse", "HEAD")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValueError("band comparison output is write-once")
        commit = pinned_commit()
        document = compare(
            load_observations(args.calibration),
            calibration_sha256=sha256_file(args.calibration),
            commit=commit,
        )
        write_once_text(args.output, json.dumps(document, indent=2, sort_keys=True) + "\n")
    except Exception as error:  # a script: say what failed, then a VERDICT
        print(f"band comparison failed: {type(error).__name__}: {error}", file=sys.stderr)
        print("VERDICT cmd=provenance.compare_bands status=FAIL", file=sys.stderr)
        return 1
    winner = document["winner"] or "none"
    status = "OK" if document["winner"] else "FAIL"
    print(f"VERDICT cmd=provenance.compare_bands status={status} winner={winner}", file=sys.stderr)
    return 0 if document["winner"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑測試，確認通過**

Run: `uv run pytest tests/unit/provenance/test_compare_bands.py -q -o addopts="" -p no:cacheprovider`
Expected: 全部 PASS。若 `test_constant_noise_favours_the_stratified_band` 的分差小於 0.1，表示合成資料不夠分明：把 `_additive` 的 ±50 ms 加大到 ±100 ms 重跑；不得改 `TIE` 或判準。

- [ ] **Step 5: lint 並 commit（這個 commit 就是預先登記）**

```bash
uv run ruff check tests/performance/provenance/compare_bands.py tests/unit/provenance/test_compare_bands.py
uv run ruff format --check tests/performance/provenance/compare_bands.py tests/unit/provenance/test_compare_bands.py
git add tests/performance/provenance/compare_bands.py tests/unit/provenance/test_compare_bands.py
git commit -m "feat(provenance): pre-registered comparison of the policy v2 bands"
```

---

### Task 5: 執行候選帶比較並記錄勝者（可由 En Shuo Zhang 執行）

這是執行步驟，沒有程式碼變更。只用 calibration 資料；在這之前不得產生正式的 v2 policy。

**Files:**
- Create: `docs/benchmarks/postgres-provenance-band-comparison-v1.json`（腳本產生，write-once）
- Create: `docs/superpowers/plans/2026-10-09-vcp-postgres-adaptive-policy-v2-followups.md`
- Modify: `docs/superpowers/specs/2026-10-09-vcp-postgres-adaptive-policy-v2-design.md`（只動 §13）

**Interfaces:**
- Consumes: Task 4 的 commit。
- Produces: 勝者 `relative` 或 `stratified_edges`，記在 spec §13 與比較證據的 `winner`；Task 6 讀它。

- [ ] **Step 1: 確認前置條件**

```bash
git status --porcelain -- tests/performance/provenance/compare_bands.py src/vcp/provenance/policy_bands.py src/vcp/provenance/strategy.py
git log --oneline -1 -- tests/performance/provenance/compare_bands.py
```

Expected：第一行沒有輸出；第二行是 Task 4 的 commit。

- [ ] **Step 2: 執行**

```bash
uv run python tests/performance/provenance/compare_bands.py --calibration docs/benchmarks/postgres-provenance-calibration-v2-artifacts/inputs/calibration.json --output docs/benchmarks/postgres-provenance-band-comparison-v1.json
```

Expected：stderr 最後一行 `VERDICT cmd=provenance.compare_bands status=OK winner=<relative|stratified_edges>`，exit 0。若 `status=FAIL`：
- 輸出已存在 → 不要刪；檢查是否已經跑過，有就沿用；
- `winner=none`（兩個分數都是 inf）→ 依 spec §5.3 不發布 v2，停下來回報使用者；
- 其他錯誤 → 若輸出檔已寫出，改名為 `postgres-provenance-band-comparison-v1.failed-1.json` 保存（spec §5.5），修正後重跑。

- [ ] **Step 3: 回讀數字**

```bash
uv run python -c "import json; d=json.load(open('docs/benchmarks/postgres-provenance-band-comparison-v1.json', encoding='utf-8')); print(d['winner'], d['scores'], d['full_data_bands'], d['v1_rmse_ms']); print(json.dumps(d['calibration_decisions'], indent=1))"
sha256sum docs/benchmarks/postgres-provenance-band-comparison-v1.json
```

- [ ] **Step 4: 寫進 spec §13**

把 spec 的

```markdown
（§5 的比較完成後，在此記錄：比較證據的路徑與 SHA-256、兩個分數、勝者，以及 §4.3 的 `band` 值。在那之前不得產生正式的 v2 policy artifact。）
```

換成（用 Step 3 回讀的實際值填入，數字照抄，不四捨五入以外的改動）：

```markdown
- 候選帶比較（YYYY-MM-DD，腳本 commit `<script_commit 前 7 碼>`）：`docs/benchmarks/postgres-provenance-band-comparison-v1.json`，SHA-256 `<64 hex>`。
- 分數（§5.3，越小越好）：A `relative` = <score>、B `stratified_edges` = <score>；勝者 **<winner>**，`fit_policy_v2(..., band="<winner>")`。
- 全資料的帶：<A 的 r_inc、r_full；B 每層的兩個 RMSE>；v1 的 RMSE 為 <incremental> / <full> ms。
- §5.4 只報告的數字：<1K 層選 INCREMENTAL 的比例、選了較慢一方的次數與多花的毫秒數，A 與 B 各一句>。
```

- [ ] **Step 5: 開後記**

建立 `docs/superpowers/plans/2026-10-09-vcp-postgres-adaptive-policy-v2-followups.md`：

```markdown
# PostgreSQL adaptive policy v2：後記

spec：`docs/superpowers/specs/2026-10-09-vcp-postgres-adaptive-policy-v2-design.md`；計畫：`docs/superpowers/plans/2026-10-09-vcp-postgres-adaptive-policy-v2.md`。

## 1. 裁決

1. **候選帶比較**（Task 5）：勝者 <winner>，分數 A <score> / B <score>（spec §13）。

## 2. 已知限制

- calibration v2 量於 commit `b1512ae`，成本模型可能因之後的程式改動略有偏差；six-method v2 與 held-out v2 量的是 0.15.0 的程式碼（spec §12）。

## 3. 開放待辦

1. 發 0.15.0 之後依附錄 A 跑 six-method v2、held-out v2、real RSNA v2。
```

- [ ] **Step 6: commit**

```bash
git add docs/benchmarks/postgres-provenance-band-comparison-v1.json docs/superpowers/specs/2026-10-09-vcp-postgres-adaptive-policy-v2-design.md docs/superpowers/plans/2026-10-09-vcp-postgres-adaptive-policy-v2-followups.md
git commit -m "docs(benchmarks): policy v2 band comparison and its winner"
```

---

### Task 6: 發布正式的 v2 policy（`publish_policy_v2.py`，`load_calibration` 讀 v2 文件）

**Files:**
- Create: `tests/performance/provenance/publish_policy_v2.py`
- Modify: `tests/performance/provenance/evaluate_adaptive.py`（`load_calibration`）
- Test: `tests/unit/provenance/test_adaptive_evaluation.py`（加 helper 與測試）
- Create（執行產生）: `docs/benchmarks/postgres-provenance-policy-v2.json`、`docs/benchmarks/postgres-provenance-policy-v2-artifacts/`

**Interfaces:**
- Consumes: Task 3 的 `fit_policy_v2`、`policy_from_payload`、`POLICY_VERSION_V2`、`BAND_KINDS`；Task 5 的比較證據。
- Produces:
  - `publish_policy_v2.publish_policy_v2(calibration_from: Path, band_comparison: Path, output: Path) -> AdaptivePolicyV2`
  - v2 policy 文件：`kind = "postgres-provenance-policy-v2"`，鍵 `{"kind", "policy", "calibration", "empirical_crossover", "band_comparison"}`，`band_comparison = {"path": <同目錄的檔名>, "sha256": ..., "winner": ...}`；artifact 在 `<output stem>-artifacts/`
  - `evaluate_adaptive.load_calibration(path)` 接受 v1 calibration 文件與 v2 policy 文件，回傳 `(policy, evidence)`
  - 測試 helper `publish_v1_and_v2(tmp_path, band="relative") -> (v1_path, v1_policy, v2_path, v2_policy)`（Task 8 也用）

- [ ] **Step 1: 寫失敗的測試**

在 `tests/unit/provenance/test_adaptive_evaluation.py` 的 import 區加

```python
from performance.provenance import publish_policy_v2 as publish_v2
```

並在檔尾加：

```python
def publish_v1_and_v2(tmp_path, band="relative"):
    """A unit v1 calibration document, a unit band comparison naming ``band``, and the v2
    policy document published from them; all synthetic."""
    v1_path = tmp_path / "unit-calibration.json"
    v1 = calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), v1_path)
    comparison = tmp_path / "unit-band-comparison.json"
    comparison.write_text(
        json.dumps(
            {
                "kind": "postgres-provenance-band-comparison-v1",
                "winner": band,
                "calibration_sha256": v1.calibration_sha256,
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    v2_path = tmp_path / "unit-policy-v2.json"
    v2 = publish_v2.publish_policy_v2(v1_path, comparison, v2_path)
    return v1_path, v1, v2_path, v2


@pytest.mark.parametrize("band", ["relative", "stratified_edges"])
def test_policy_v2_is_published_from_the_v1_calibration_and_loads(tmp_path, band):
    v1_path, v1, v2_path, v2 = publish_v1_and_v2(tmp_path, band)
    assert v2.policy_version == strategy.POLICY_VERSION_V2
    assert v2.band.kind == band
    assert (v2.incremental_model, v2.full_model) == (v1.incremental_model, v1.full_model)
    assert v2.id == "postgres-adaptive-v2-" + v1.calibration_sha256[:12]
    loaded, evidence = evaluation.load_calibration(v2_path)
    assert loaded == v2
    assert strategy.calibration_text(evidence) == strategy.calibration_text(
        evaluation.load_calibration(v1_path)[1]
    )
    document = json.loads(v2_path.read_text(encoding="utf-8"))
    assert document["kind"] == "postgres-provenance-policy-v2"
    assert document["band_comparison"]["path"] == "unit-band-comparison.json"
    assert evaluation.policy_file_sha256(v2_path, v2) == strategy._policy_sha256(v2)


def test_policy_v2_document_must_match_its_band_comparison(tmp_path):
    _, _, v2_path, _ = publish_v1_and_v2(tmp_path)
    comparison = tmp_path / "unit-band-comparison.json"
    comparison.write_text(comparison.read_text().replace("relative", "stratified_edges"))
    with pytest.raises(ValidationFailed, match="invalid_calibration_artifact"):
        evaluation.load_calibration(v2_path)


def test_publish_policy_v2_refuses_a_comparison_of_another_calibration(tmp_path):
    v1_path = tmp_path / "unit-calibration.json"
    calibration.publish_calibration(benchmark_rows(strategy.CALIBRATION_SEEDS), v1_path)
    comparison = tmp_path / "unit-band-comparison.json"
    comparison.write_text(
        json.dumps(
            {
                "kind": "postgres-provenance-band-comparison-v1",
                "winner": "relative",
                "calibration_sha256": "e" * 64,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="invalid_band_comparison"):
        publish_v2.publish_policy_v2(v1_path, comparison, tmp_path / "unit-policy-v2.json")


def test_a_v1_calibration_document_cannot_carry_a_v2_policy(tmp_path):
    v1_path, _, _, v2 = publish_v1_and_v2(tmp_path)
    document = json.loads(v1_path.read_text(encoding="utf-8"))
    document["policy"] = v2.model_dump(mode="json")
    v1_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValidationFailed, match="invalid_calibration_artifact"):
        evaluation.load_calibration(v1_path)
```

- [ ] **Step 2: 跑測試，確認失敗**

Run: `uv run pytest tests/unit/provenance/test_adaptive_evaluation.py -q -o addopts="" -p no:cacheprovider -k "policy_v2 or v2_policy"`
Expected: FAIL，`ImportError: cannot import name 'publish_policy_v2'`。

- [ ] **Step 3: 實作——`load_calibration` 讀兩種文件**

`evaluate_adaptive.py` 的 strategy import 清單加入 `POLICY_VERSION_V2`、`policy_from_payload`（照字母順序），並在 `load_calibration` 之前加：

```python
# The wrapper documents a policy can come in (spec 2026-10-09 §4.4): v1 inside the
# calibration it was fitted from, v2 published from that calibration plus the band comparison.
_POLICY_DOCUMENTS = {
    "postgres-provenance-calibration-v1": (
        frozenset({"kind", "policy", "calibration", "empirical_crossover"}),
        POLICY_VERSION,
    ),
    "postgres-provenance-policy-v2": (
        frozenset({"kind", "policy", "calibration", "empirical_crossover", "band_comparison"}),
        POLICY_VERSION_V2,
    ),
}


def _check_band_comparison(directory: Path, record, policy) -> None:
    """The v2 document names the comparison that chose its band; it must still say so."""
    if set(record) != {"path", "sha256", "winner"} or Path(record["path"]).name != record["path"]:
        raise ValueError
    path = directory / record["path"]
    comparison = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_object_without_duplicate_keys
    )
    if (
        sha256_file(path) != record["sha256"]
        or comparison.get("kind") != "postgres-provenance-band-comparison-v1"
        or comparison.get("winner") != record["winner"]
        or record["winner"] != policy.band.kind
        or comparison.get("calibration_sha256") != policy.calibration_sha256
    ):
        raise ValueError
```

（`sha256_file` 若尚未 import，從 `vcp.core.hashing` 加入。）再把 `load_calibration` 的

```python
        if set(document) != {"kind", "policy", "calibration", "empirical_crossover"}:
            raise ValueError
        if document["kind"] != "postgres-provenance-calibration-v1":
            raise ValueError
        policy = AdaptivePolicy.model_validate(document["policy"])
```

換成

```python
        keys, version = _POLICY_DOCUMENTS[document["kind"]]
        if set(document) != keys:
            raise ValueError
        policy = policy_from_payload(document["policy"])
        if policy.policy_version != version:
            raise ValueError
```

在同一個函式 `return verified, evidence` 之前加：

```python
        if "band_comparison" in document:
            _check_band_comparison(path.parent, document["band_comparison"], verified)
```

並把它的 `except (OSError, TypeError, ValueError, VcpError):` 換成 `except (OSError, TypeError, ValueError, KeyError, VcpError):`。`AdaptivePolicy` 若已不再被這個檔使用，從 import 移除（ruff 會提示）。

- [ ] **Step 4: 實作——`publish_policy_v2.py`**

```python
"""Publish policy v2 from the frozen calibration v2 and the band comparison's winner.

No measurement here (spec 2026-10-09 §4.3): v2's cost models are refitted from the very
calibration v1 was fitted from, and must equal v1's; only the band is new.

    uv run python tests/performance/provenance/publish_policy_v2.py \
        --calibration-from docs/benchmarks/postgres-provenance-calibration-v2.json \
        --band-comparison docs/benchmarks/postgres-provenance-band-comparison-v1.json \
        --output docs/benchmarks/postgres-provenance-policy-v2.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from vcp.core.atomic import write_once_text
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file, sha256_text
from vcp.provenance.policy_bands import BAND_KINDS
from vcp.provenance.strategy import (
    AdaptivePolicyV2,
    CostModel,
    calibration_text,
    fit_policy_v2,
    write_policy_artifact,
)

if __package__:
    from .evaluate_adaptive import empirical_crossover, load_calibration
else:
    from evaluate_adaptive import empirical_crossover, load_calibration

DRIFT = 1e-9  # spec §4.3: v2's coefficients equal v1's within this relative error


def _band_record(band_comparison: Path, output: Path, calibration_sha256: str) -> dict:
    band_comparison = Path(band_comparison)
    if band_comparison.resolve().parent != Path(output).resolve().parent:
        raise ValidationFailed("invalid_band_comparison: keep it beside the policy document")
    try:
        comparison = json.loads(band_comparison.read_text(encoding="utf-8"))
        valid = (
            comparison["kind"] == "postgres-provenance-band-comparison-v1"
            and comparison["winner"] in BAND_KINDS
            and comparison["calibration_sha256"] == calibration_sha256
        )
    except (OSError, ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise ValidationFailed("invalid_band_comparison")
    return {
        "path": band_comparison.name,
        "sha256": sha256_file(band_comparison),
        "winner": comparison["winner"],
    }


def _same_model(first: CostModel, second: CostModel) -> bool:
    if first.feature_order != second.feature_order:
        return False
    pairs = [(first.intercept_ms, second.intercept_ms)]
    pairs += [(first.coefficients[n], second.coefficients[n]) for n in first.feature_order]
    return all(math.isclose(a, b, rel_tol=DRIFT, abs_tol=0.0) for a, b in pairs)


def publish_policy_v2(calibration_from: Path, band_comparison: Path, output: Path) -> AdaptivePolicyV2:
    output = Path(output)
    v1, evidence = load_calibration(calibration_from)
    record = _band_record(band_comparison, output, sha256_text(calibration_text(evidence)))
    policy = fit_policy_v2(evidence, band=record["winner"])
    if not (
        _same_model(policy.incremental_model, v1.incremental_model)
        and _same_model(policy.full_model, v1.full_model)
    ):
        raise ValidationFailed("cost_model_drift: v2 refit differs from v1; stop and investigate")
    root = output.parent / (output.stem + "-artifacts")
    source = root / "inputs" / "calibration.json"
    write_once_text(source, calibration_text(evidence))
    write_policy_artifact(root, policy, source)
    document = {
        "kind": "postgres-provenance-policy-v2",
        "policy": policy.model_dump(mode="json"),
        "calibration": evidence.model_dump(mode="json"),
        "empirical_crossover": empirical_crossover(evidence),
        "band_comparison": record,
    }
    write_once_text(output, json.dumps(document, indent=2) + "\n")
    return policy


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-from", type=Path, required=True)
    parser.add_argument("--band-comparison", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValidationFailed("policy_output_exists")
        policy = publish_policy_v2(args.calibration_from, args.band_comparison, args.output)
    except Exception as error:  # a script: say what failed, then a VERDICT
        print(f"policy v2 publication failed: {type(error).__name__}: {error}", file=sys.stderr)
        print("VERDICT cmd=provenance.publish_policy_v2 status=FAIL", file=sys.stderr)
        return 1
    print(
        f"VERDICT cmd=provenance.publish_policy_v2 status=OK policy={policy.id} "
        f"band={policy.band.kind}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: 跑測試，確認通過，並跑整個 provenance 測試目錄**

Run: `uv run pytest tests/unit/provenance -q -o addopts="" -p no:cacheprovider`
Expected: 全部 PASS（`test_task10_rows_fit_and_heldout_never_refits` 仍通過：`evaluate_adaptive` 沒有 import 擬合相關的名字）。

- [ ] **Step 6: commit 程式**

```bash
uv run ruff check tests/performance/provenance/publish_policy_v2.py tests/performance/provenance/evaluate_adaptive.py tests/unit/provenance/test_adaptive_evaluation.py
uv run ruff format --check tests/performance/provenance/publish_policy_v2.py tests/performance/provenance/evaluate_adaptive.py tests/unit/provenance/test_adaptive_evaluation.py
git add tests/performance/provenance/publish_policy_v2.py tests/performance/provenance/evaluate_adaptive.py tests/unit/provenance/test_adaptive_evaluation.py
git commit -m "feat(provenance): publish policy v2 from the calibration and the band comparison"
```

- [ ] **Step 7: 執行，產生正式的 v2 policy**（Task 5 已把勝者寫進 spec §13 才能做）

```bash
uv run python tests/performance/provenance/publish_policy_v2.py --calibration-from docs/benchmarks/postgres-provenance-calibration-v2.json --band-comparison docs/benchmarks/postgres-provenance-band-comparison-v1.json --output docs/benchmarks/postgres-provenance-policy-v2.json
```

Expected：`VERDICT cmd=provenance.publish_policy_v2 status=OK policy=postgres-adaptive-v2-9f4e58346529 band=<winner>`。若是 `cost_model_drift`：停下來回報（spec §4.3），不得繞過。

- [ ] **Step 8: 驗證並 commit 產物**

```bash
uv run python -c "from pathlib import Path; from performance.provenance.evaluate_adaptive import load_calibration, policy_file_sha256; p=Path('docs/benchmarks/postgres-provenance-policy-v2.json'); pol,_=load_calibration(p); print(pol.id, pol.band.kind, policy_file_sha256(p, pol))"
git diff --check
git add docs/benchmarks/postgres-provenance-policy-v2.json docs/benchmarks/postgres-provenance-policy-v2-artifacts
git commit -m "docs(benchmarks): frozen policy postgres-adaptive-v2"
```

（`uv run python -c` 需要 `tests` 在路徑上：在 repo 根目錄用 `uv run python -c "import sys; sys.path.insert(0, 'tests'); ..."` 執行。）把印出的 policy id 與 `policy.json` 的 SHA-256 補進後記 §1 第 2 條。

---

### Task 7: runner 的比較方法與第二個 policy

**Files:**
- Modify: `tests/performance/provenance/adaptive_benchmark.py`
- Modify: `tests/performance/provenance/evaluate_adaptive.py`（`prepare_workload`、`collect_rows`）
- Test: `tests/unit/provenance/test_adaptive_benchmark.py`、`tests/unit/provenance/test_adaptive_evaluation.py`

**Interfaces:**
- Consumes: Task 6 的 `publish_v1_and_v2` 測試 helper。
- Produces:
  - `adaptive_benchmark.COMPARISON_METHOD = "postgres_adaptive_v1"`、`ADAPTIVE_METHODS`、`KNOWN_METHODS`、`REQUESTED_STRATEGY`
  - `adaptive_benchmark.policy_for(method, policy, policy_sha256, comparison=None, comparison_sha256=None) -> tuple[str | None, str | None]`
  - `_checkpoint_contract(..., comparison_policy_sha256=None)`、`_run_scenario_child(..., comparison_from=None)`、`_child_run_scenario(work_dir, scenario_hash, policy_from, comparison_from=None)`、`run_matrix_isolated(..., comparison_from=None, comparison_sha256=None)`、隱藏 CLI `--_child-comparison-policy-from`
  - `adaptive_benchmark.prepare_policy_workload(workload, policy, evidence, *comparisons)`
  - `evaluate_adaptive.prepare_workload(workload, policy, evidence, comparisons=())`
  - `evaluate_adaptive.collect_rows(..., comparison=None, comparison_from=None)`

- [ ] **Step 1: 寫失敗的測試**

在 `tests/unit/provenance/test_adaptive_benchmark.py` 檔尾加：

```python
def test_the_v1_comparison_is_known_but_not_a_seventh_six_method_method():
    assert bench.METHODS == (
        "canonical_full",
        "sqlite_full",
        "sqlite_incremental",
        "postgres_full",
        "postgres_incremental",
        "postgres_adaptive",
    )
    assert bench.COMPARISON_METHOD == "postgres_adaptive_v1"
    assert bench.KNOWN_METHODS == (*bench.METHODS, "postgres_adaptive_v1")
    assert bench.ADAPTIVE_METHODS == ("postgres_adaptive", "postgres_adaptive_v1")


def test_the_v1_comparison_runs_auto_under_its_own_policy():
    calls = []
    state = SimpleNamespace(
        backend=SimpleNamespace(ingest_diff=lambda *args, **kwargs: calls.append((args, kwargs)))
    )
    workload = SimpleNamespace(artifact_id="delta")
    bench._maintain(state, "postgres_adaptive_v1", workload, "data", "configs", "frozen-v1")
    assert calls == [
        (("delta", "data", "configs"), {"requested_strategy": "auto", "policy_id": "frozen-v1"})
    ]


def test_policy_for_maps_each_adaptive_method_to_its_policy():
    v2 = SimpleNamespace(id="v2-id")
    v1 = SimpleNamespace(id="v1-id")
    assert bench.policy_for("postgres_adaptive", v2, "a" * 64, v1, "b" * 64) == ("v2-id", "a" * 64)
    assert bench.policy_for("postgres_adaptive_v1", v2, "a" * 64, v1, "b" * 64) == (
        "v1-id",
        "b" * 64,
    )
    assert bench.policy_for("postgres_full", v2, "a" * 64, v1, "b" * 64) == (None, None)
    assert bench.policy_for("postgres_adaptive_v1", v2, "a" * 64) == (None, None)


def test_run_method_requires_a_policy_for_the_v1_comparison(tmp_path):
    workload = SimpleNamespace(
        scenario=SimpleNamespace(repetitions=1), data=tmp_path, configs=tmp_path
    )
    with pytest.raises(ValueError, match="postgres_adaptive_v1 requires a frozen policy"):
        bench.run_method(workload, "postgres_adaptive_v1")


def test_the_comparison_policy_is_pinned_only_when_given(tmp_path):
    without = bench._checkpoint_contract(tmp_path, [], bench.METHODS, 1, "a" * 64, _postgres_identity())
    assert "comparison_policy_sha256" not in without
    with_comparison = bench._checkpoint_contract(
        tmp_path,
        [],
        bench.KNOWN_METHODS,
        1,
        "a" * 64,
        _postgres_identity(),
        comparison_policy_sha256="b" * 64,
    )
    assert with_comparison["comparison_policy_sha256"] == "b" * 64


def test_the_child_command_carries_the_comparison_only_when_given(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(
        bench.subprocess,
        "run",
        lambda command, **kwargs: commands.append(command) or SimpleNamespace(returncode=0),
    )
    store = SimpleNamespace(root=tmp_path)
    bench._run_scenario_child(store, "h" * 64, policy_from=tmp_path / "v2.json")
    bench._run_scenario_child(
        store, "h" * 64, policy_from=tmp_path / "v2.json", comparison_from=tmp_path / "v1.json"
    )
    assert "--_child-comparison-policy-from" not in commands[0]
    assert commands[1][-2:] == [
        "--_child-comparison-policy-from",
        str((tmp_path / "v1.json").resolve()),
    ]
```

（`_postgres_identity`、`SimpleNamespace`、`pytest` 在這個測試檔已有；若 `SimpleNamespace` 未 import，從 `types` 加入。）

在 `tests/unit/provenance/test_adaptive_evaluation.py` 檔尾加：

```python
def test_prepare_workload_installs_the_comparison_policy_beside_the_primary(tmp_path):
    _, v1, _, v2 = publish_v1_and_v2(tmp_path)
    evidence = evaluation.load_calibration(tmp_path / "unit-policy-v2.json")[1]
    workload = build_scenario(tmp_path / "fixture", Scenario(40, 0.5, "chain", 20261101))
    prepared = evaluation.prepare_workload(workload, v2, evidence, (v1,))
    assert strategy.load_policy_artifact(prepared.data, v2.id) == v2
    assert strategy.load_policy_artifact(prepared.data, v1.id) == v1
    data, configs = prepared.clone(tmp_path / "clone")
    prepared.publish(data)
    assert build_graph(data, configs).normalized() == prepared.expected.normalized()


def test_prepare_workload_refuses_a_comparison_of_another_calibration(tmp_path):
    _, v1, _, v2 = publish_v1_and_v2(tmp_path)
    evidence = evaluation.load_calibration(tmp_path / "unit-policy-v2.json")[1]
    other = v1.model_copy(update={"calibration_sha256": "e" * 64})
    workload = build_scenario(tmp_path / "fixture", Scenario(40, 0.5, "chain", 20261101))
    with pytest.raises(ValidationFailed, match="invalid_policy_workload_oracle"):
        evaluation.prepare_workload(workload, v2, evidence, (other,))


def test_collect_rows_passes_the_comparison_to_the_isolated_runner(tmp_path, monkeypatch):
    observed = {}
    monkeypatch.setattr(evaluation, "policy_file_sha256", lambda path, value: value.sha)

    def isolated(root, scenarios, **kwargs):
        observed.update(kwargs)
        return []

    monkeypatch.setattr(evaluation.benchmark, "run_matrix_isolated", isolated)
    primary = SimpleNamespace(sha="a" * 64)
    comparison = SimpleNamespace(sha="b" * 64)
    evaluation.collect_rows(
        tmp_path,
        [],
        methods=evaluation.HELDOUT_V2_METHODS,
        pg_runtime="pg",
        policy=primary,
        policy_from=tmp_path / "v2.json",
        comparison=comparison,
        comparison_from=tmp_path / "v1.json",
        isolated=True,
    )
    assert observed["comparison_from"] == tmp_path / "v1.json"
    assert observed["comparison_sha256"] == "b" * 64
    assert observed["policy_sha256"] == "a" * 64
```

（`SimpleNamespace` 若未 import，從 `types` 加入。`evaluation.HELDOUT_V2_METHODS` 在 Task 8 定義；本 task 先在 `evaluate_adaptive.py` 定義它——見 Step 4。）

- [ ] **Step 2: 跑測試，確認失敗**

Run: `uv run pytest tests/unit/provenance/test_adaptive_benchmark.py tests/unit/provenance/test_adaptive_evaluation.py -q -o addopts="" -p no:cacheprovider -k "comparison or v1_comparison or policy_for"`
Expected: FAIL，`AttributeError: module ... has no attribute 'COMPARISON_METHOD'`。

- [ ] **Step 3: 實作 `adaptive_benchmark.py`**

1. 在 `METHODS = (...)` 之後加：

```python
# Held-out v2 and real v2 also run the frozen v1 policy beside v2 (spec 2026-10-09 §6.3,
# §6.4). It is not a seventh six-method method: METHODS stays the published six.
COMPARISON_METHOD = "postgres_adaptive_v1"
ADAPTIVE_METHODS = ("postgres_adaptive", COMPARISON_METHOD)
KNOWN_METHODS = (*METHODS, COMPARISON_METHOD)
REQUESTED_STRATEGY = {
    "postgres_full": "full",
    "postgres_incremental": "incremental",
    "postgres_adaptive": "auto",
    COMPARISON_METHOD: "auto",
}
```

2. 把四處成員檢查（`SpoolKey.__post_init__`、`fresh_backend`、`capture_explain_rollback`、`run_method`；用 `grep -n "in METHODS" tests/performance/provenance/adaptive_benchmark.py` 找）中的 `METHODS` 換成 `KNOWN_METHODS`。`run_matrix_isolated` 的預設參數 `methods=METHODS`、文件的 `"methods": list(METHODS)` 與 `run_matrix` 的 `for method in METHODS` **不改**。
3. `_maintain` 裡的字典換成常數：

```python
    if method.startswith("postgres_"):
        kwargs = {"requested_strategy": REQUESTED_STRATEGY[method], "policy_id": policy_id}
```

4. `run_method` 的

```python
    if method == "postgres_adaptive" and not policy_id:
        raise ValueError("postgres_adaptive requires a frozen policy")
    result = ScenarioResult(
        method,
        workload,
        runtime_environment(workload.data),
        policy_id=policy_id if method == "postgres_adaptive" else None,
        policy_sha256=policy_sha256 if method == "postgres_adaptive" else None,
    )
```

換成

```python
    if method in ADAPTIVE_METHODS and not policy_id:
        raise ValueError(f"{method} requires a frozen policy")
    result = ScenarioResult(
        method,
        workload,
        runtime_environment(workload.data),
        policy_id=policy_id if method in ADAPTIVE_METHODS else None,
        policy_sha256=policy_sha256 if method in ADAPTIVE_METHODS else None,
    )
```

5. 在 `prepare_policy_workload` 之前加 `policy_for`，並把 `prepare_policy_workload` 換成：

```python
def policy_for(method, policy, policy_sha256, comparison=None, comparison_sha256=None):
    """(policy id, policy.json sha) a method runs under: ``postgres_adaptive`` the primary
    policy, ``postgres_adaptive_v1`` the frozen v1 comparison, every fixed method none."""
    if method == "postgres_adaptive" and policy is not None:
        return policy.id, policy_sha256
    if method == COMPARISON_METHOD and comparison is not None:
        return comparison.id, comparison_sha256
    return None, None


def prepare_policy_workload(workload, policy, evidence, *comparisons):
    if __package__:
        from .evaluate_adaptive import prepare_workload
    else:
        from evaluate_adaptive import prepare_workload
    if comparisons:
        return prepare_workload(workload, policy, evidence, comparisons)
    return prepare_workload(workload, policy, evidence)
```

6. `_checkpoint_contract`：簽名加 `*, comparison_policy_sha256=None`（放在 `environment` 之後）；把 `return {...}` 改成先存進 `contract = {...}`，再

```python
    if comparison_policy_sha256 is not None:
        contract["comparison_policy_sha256"] = comparison_policy_sha256
    return contract
```

7. `_run_scenario_child`：簽名加 `comparison_from: Path | None = None`（在 `policy_from` 之後，keyword-only）；在既有的 `if policy_from is not None:` 之後加：

```python
    if comparison_from is not None:
        command.extend(
            ["--_child-comparison-policy-from", str(Path(comparison_from).resolve())]
        )
```

8. `_child_run_scenario(work_dir, scenario_hash, policy_from, comparison_from=None)`：
   - 在 `if policy_sha256 != contract["policy_sha256"]: raise ...` 之後加：

```python
    comparison = comparison_sha256 = None
    if comparison_from is not None:
        comparison, _comparison_evidence, comparison_sha256 = load_frozen_policy(comparison_from)
    if comparison_sha256 != contract.get("comparison_policy_sha256"):
        raise ValueError("checkpoint policy mismatch")
```

   - `workload = prepare_policy_workload(workload, policy, evidence)` 換成
     `workload = prepare_policy_workload(workload, policy, evidence, *((comparison,) if comparison else ()))`
   - 樣本迴圈裡的 `policy_id=...`、`policy_sha256=(...)` 兩個參數換成：

```python
                method_policy, method_sha256 = policy_for(
                    method, policy, policy_sha256, comparison, comparison_sha256
                )
                row = run_method(
                    workload,
                    method,
                    repetitions=1,
                    pg_runtime=pg_runtime,
                    policy_id=method_policy,
                    policy_sha256=method_sha256,
                    capture_explain=False,
                    owned_database=database,
                ).to_dict()
```

   - explain 迴圈的 `policy_id=(policy.id if policy and method == "postgres_adaptive" else None),` 換成
     `policy_id=policy_for(method, policy, policy_sha256, comparison, comparison_sha256)[0],`

9. `run_matrix_isolated`：簽名加 `comparison_from=None, comparison_sha256=None`；把建契約與開子程序兩處換成「只有給了才傳」：

```python
    extra = {} if comparison_sha256 is None else {"comparison_policy_sha256": comparison_sha256}
    contract = _checkpoint_contract(
        work_dir, scenarios, methods, repetitions, policy_sha256, environment, **extra
    )
```

```python
            spawn = {"policy_from": policy_from}
            if comparison_from is not None:
                spawn["comparison_from"] = comparison_from
            process = _run_scenario_child(store, scenario.scenario_hash, **spawn)
```

10. `main`：加 `parser.add_argument("--_child-comparison-policy-from", type=Path, help=argparse.SUPPRESS)`；子程序分支改成：

```python
            child_args = [args._child_work_dir, args._child_scenario_hash, args._child_policy_from]
            if args._child_comparison_policy_from is not None:
                child_args.append(args._child_comparison_policy_from)
            return _child_run_scenario(*child_args)
```

- [ ] **Step 4: 實作 `evaluate_adaptive.py` 的 `prepare_workload`、`collect_rows`**

1. 在 `EVALUATION_METHODS = ...` 之後加：

```python
HELDOUT_V2_METHODS = (*EVALUATION_METHODS, "postgres_adaptive_v1")
```

2. `prepare_workload` 換成：

```python
def prepare_workload(workload, policy, evidence, comparisons=()):
    """Install the policies before the baseline; rebuild the candidate oracle outside measured
    runs. A comparison policy (spec 2026-10-09 §6.3) must share the primary's calibration, so
    one pinned calibration copy serves both."""
    evidence_path = workload.data / "policy-inputs" / "calibration.json"
    write_once_text(evidence_path, calibration_text(evidence))
    write_policy_artifact(workload.data, policy, evidence_path)
    for comparison in comparisons:
        if comparison.calibration_sha256 != policy.calibration_sha256:
            raise ValidationFailed("invalid_policy_workload_oracle")
        write_policy_artifact(workload.data, comparison, evidence_path)
    baseline = build_graph(workload.data, workload.configs)
```

（`baseline = ...` 之後的內容不變。）

3. `collect_rows`：簽名在 `policy_from=None,` 之後加 `comparison=None, comparison_from=None,`；isolated 分支換成：

```python
    if isolated:
        policy_sha256 = policy_file_sha256(policy_from, policy) if policy is not None else None
        kwargs = {
            "methods": methods,
            "pg_runtime": pg_runtime,
            "policy_from": policy_from,
            "policy_sha256": policy_sha256,
        }
        if comparison is not None:
            kwargs["comparison_from"] = comparison_from
            kwargs["comparison_sha256"] = policy_file_sha256(comparison_from, comparison)
        return benchmark.run_matrix_isolated(root, scenarios, **kwargs)
```

非 isolated 分支的

```python
            if policy is not None:
                workload = prepare_workload(workload, policy, evidence)
            for method in methods:
                rows.append(
                    benchmark.run_method(
                        workload,
                        method,
                        pg_runtime=pg_runtime,
                        policy_id=policy.id if policy and method == "postgres_adaptive" else None,
                        policy_sha256=(
                            _policy_sha256(policy)
                            if policy and method == "postgres_adaptive"
                            else None
                        ),
                    ).to_dict()
                )
```

換成

```python
            if policy is not None:
                comparisons = () if comparison is None else (comparison,)
                workload = prepare_workload(workload, policy, evidence, comparisons)
            for method in methods:
                method_policy, method_sha256 = benchmark.policy_for(
                    method,
                    policy,
                    _policy_sha256(policy) if policy else None,
                    comparison,
                    _policy_sha256(comparison) if comparison else None,
                )
                rows.append(
                    benchmark.run_method(
                        workload,
                        method,
                        pg_runtime=pg_runtime,
                        policy_id=method_policy,
                        policy_sha256=method_sha256,
                    ).to_dict()
                )
```

- [ ] **Step 5: 跑新測試與兩個測試檔全部**

Run: `uv run pytest tests/unit/provenance/test_adaptive_benchmark.py tests/unit/provenance/test_adaptive_evaluation.py -q -o addopts="" -p no:cacheprovider`
Expected: 全部 PASS（既有的 mock 簽名、契約鍵、`test_six_exact_methods` 都不變）。

- [ ] **Step 6: lint 並 commit**

```bash
uv run ruff check tests/performance/provenance tests/unit/provenance
uv run ruff format --check tests/performance/provenance tests/unit/provenance
git add tests/performance/provenance/adaptive_benchmark.py tests/performance/provenance/evaluate_adaptive.py tests/unit/provenance/test_adaptive_benchmark.py tests/unit/provenance/test_adaptive_evaluation.py
git commit -m "feat(provenance): run the frozen v1 policy beside v2 in the benchmark runners"
```

---

### Task 8: held-out v2 的驗證與評估（`evaluate_adaptive.py`）

**Files:**
- Modify: `tests/performance/provenance/evaluate_adaptive.py`
- Test: `tests/unit/provenance/test_adaptive_evaluation.py`

**Interfaces:**
- Consumes: Task 3 的 `HELDOUT_V2_SEEDS`、`POLICY_VERSION_V2`；Task 1 的 `edges_decade`；Task 6 的 `publish_v1_and_v2`；Task 7 的 `HELDOUT_V2_METHODS` 與 `collect_rows(..., comparison=, comparison_from=)`。
- Produces:
  - `PolicyEvaluation`、`HeldoutV2Result`（pydantic 模型）
  - `evaluate_policies(policy_from, comparison_from, heldout_rows) -> HeldoutV2Result`
  - CLI：`--comparison-policy-from PATH` 時跑 held-out v2，輸出 `kind = "postgres-provenance-heldout-v2"`；VERDICT `status=OK|FAIL h1=<bool> h1a=<bool>`；exit 0 只在 v2 的 `performance_pass`。
  - `benchmark_rows(seeds, policy=None, comparison=None)`（測試 helper，多一個參數）。

- [ ] **Step 1: 擴充測試 helper `benchmark_rows`**

`tests/unit/provenance/test_adaptive_evaluation.py` 的 `benchmark_rows`：

1. 簽名 `def benchmark_rows(seeds, policy=None):` 換成 `def benchmark_rows(seeds, policy=None, comparison=None):`
2. 方法清單 `for method in evaluation.EVALUATION_METHODS if policy else evaluation.FIXED_METHODS:` 換成：

```python
        methods = (
            evaluation.HELDOUT_V2_METHODS
            if comparison
            else evaluation.EVALUATION_METHODS
            if policy
            else evaluation.FIXED_METHODS
        )
        for method in methods:
```

3. requested 字典加一項 `"postgres_adaptive_v1": "auto",`。
4. decision 換成：

```python
            method_policy = comparison if method == "postgres_adaptive_v1" else policy
            decision = strategy.select_strategy(
                requested, features, method_policy if requested == "auto" else None
            )
```

5. `policy_id=...`、`policy_sha256=(...)` 兩行換成：

```python
                policy_id=method_policy.id if requested == "auto" else None,
                policy_sha256=(
                    strategy._policy_sha256(method_policy) if requested == "auto" else None
                ),
```

（`method_policy` 對固定方法也有值，但 `requested == "auto"` 才用；兩個 adaptive 方法各拿自己的 policy。）

- [ ] **Step 2: 寫失敗的測試**

檔尾加：

```python
def _slow_v1(rows):
    """Make every non-NO_OP v1 adaptive row three times slower than the fixed methods."""
    for row in rows:
        if row["method"] == "postgres_adaptive_v1" and row["selected_strategy"] != "NO_OP":
            row["maintenance_p50_ms"] = row["maintenance_p95_ms"] = 30.0
            for sample in row["samples"]:
                sample["maintenance_ms"] = 30.0
            row["throughput_samples_per_second"] = row["changed_samples"] / 0.03
    return rows


def test_heldout_v2_rows_validate_with_four_methods_on_the_new_seeds(tmp_path):
    _, v1, _, v2 = publish_v1_and_v2(tmp_path)
    rows = benchmark_rows(strategy.HELDOUT_V2_SEEDS, v2, v1)
    groups = evaluation.validate_rows(
        rows, seeds=strategy.HELDOUT_V2_SEEDS, methods=evaluation.HELDOUT_V2_METHODS
    )
    assert len(groups) == 108
    assert all(set(group) == set(evaluation.HELDOUT_V2_METHODS) for group in groups.values())


def test_evaluate_policies_reports_both_and_tests_h1(tmp_path):
    v1_path, v1, v2_path, v2 = publish_v1_and_v2(tmp_path)
    rows = _slow_v1(benchmark_rows(strategy.HELDOUT_V2_SEEDS, v2, v1))
    result = evaluation.evaluate_policies(v2_path, v1_path, rows)
    assert (result.v2.policy_id, result.v1.policy_id) == (v2.id, v1.id)
    assert result.v2.performance_pass is True
    assert result.v1.performance_pass is False
    assert result.v2.scenarios_over == 0
    assert result.v1.scenarios_over == result.v1.non_no_op_scenarios > 0
    assert result.h1 is True and result.h1a is True
    assert set(result.v2.by_decade) == {"3"}
    assert result.seeds == list(strategy.HELDOUT_V2_SEEDS)


def test_evaluate_policies_with_equal_policies_does_not_claim_h1(tmp_path):
    v1_path, v1, v2_path, v2 = publish_v1_and_v2(tmp_path)
    result = evaluation.evaluate_policies(
        v2_path, v1_path, benchmark_rows(strategy.HELDOUT_V2_SEEDS, v2, v1)
    )
    assert result.v2.scenarios_over == result.v1.scenarios_over == 0
    assert result.h1 is False and result.h1a is False


def test_evaluate_policies_refuses_a_swapped_pair(tmp_path):
    v1_path, v1, v2_path, v2 = publish_v1_and_v2(tmp_path)
    rows = benchmark_rows(strategy.HELDOUT_V2_SEEDS, v2, v1)
    with pytest.raises(ValidationFailed, match="invalid_policy_pair"):
        evaluation.evaluate_policies(v1_path, v2_path, rows)


def test_evaluate_policies_rejects_a_v1_row_decided_by_v2(tmp_path):
    v1_path, v1, v2_path, v2 = publish_v1_and_v2(tmp_path)
    rows = benchmark_rows(strategy.HELDOUT_V2_SEEDS, v2, v1)
    for row in rows:
        if row["method"] == "postgres_adaptive_v1":
            row["policy_id"] = v2.id
    with pytest.raises(ValidationFailed, match="policy_decision_mismatch"):
        evaluation.evaluate_policies(v2_path, v1_path, rows)


def test_heldout_v2_cli_publishes_both_policies(tmp_path, monkeypatch, capsys):
    v1_path, v1, v2_path, v2 = publish_v1_and_v2(tmp_path)
    rows = _slow_v1(benchmark_rows(strategy.HELDOUT_V2_SEEDS, v2, v1))
    monkeypatch.setattr(evaluation.benchmark, "postgres_preflight", lambda: object())

    def collect(root, scenarios, **kwargs):
        assert {s.seed for s in scenarios} == set(strategy.HELDOUT_V2_SEEDS)
        assert kwargs["methods"] == evaluation.HELDOUT_V2_METHODS
        assert (kwargs["policy"], kwargs["comparison"]) == (v2, v1)
        return rows

    monkeypatch.setattr(evaluation, "collect_rows", collect)
    output = tmp_path / "unit-heldout-v2.json"
    argv = ["--policy-from", str(v2_path), "--comparison-policy-from", str(v1_path)]
    assert evaluation.main([*argv, "--output", str(output)]) == 0
    document = json.loads(output.read_text())
    assert document["kind"] == "postgres-provenance-heldout-v2"
    assert document["evaluation"]["h1"] is True
    assert "status=OK h1=true h1a=true" in capsys.readouterr().err
```

- [ ] **Step 3: 跑測試，確認失敗**

Run: `uv run pytest tests/unit/provenance/test_adaptive_evaluation.py -q -o addopts="" -p no:cacheprovider -k "heldout_v2 or evaluate_policies"`
Expected: FAIL（`validate_rows` 不認得 `postgres_adaptive_v1`，或 `evaluate_policies` 不存在）。

- [ ] **Step 4: 實作——列的驗證**

`evaluate_adaptive.py`：

1. strategy import 加 `HELDOUT_V2_SEEDS`；新增 `from vcp.provenance.policy_bands import edges_decade`。
2. `_Sample.policy_version` 的 Literal 加 `"postgres-adaptive-v2"`：`Literal["safe-fallback-v1", "postgres-adaptive-v1", "postgres-adaptive-v2"]`。
3. `BenchmarkRow.method` 的 Literal 加 `"postgres_adaptive_v1"`。
4. 在 `HELDOUT_V2_METHODS` 之後加：

```python
_ADAPTIVE_METHODS = ("postgres_adaptive", "postgres_adaptive_v1")
_REQUESTED = {
    "postgres_full": "full",
    "postgres_incremental": "incremental",
    "postgres_adaptive": "auto",
    "postgres_adaptive_v1": "auto",
}
```

5. `validate_rows` 裡的 `expected_requested = {...}[row.method]` 換成 `expected_requested = _REQUESTED[row.method]`；`if row.method == "postgres_adaptive":` 換成 `if row.method in _ADAPTIVE_METHODS:`。
6. `expected_scenarios` 的 `if tuple(seeds) not in (CALIBRATION_SEEDS, HELDOUT_SEEDS):` 換成 `if tuple(seeds) not in (CALIBRATION_SEEDS, HELDOUT_SEEDS, HELDOUT_V2_SEEDS):`。

- [ ] **Step 5: 實作——把 `evaluate_policy` 共用的部分抽出來（行為不變）**

在 `evaluate_policy` 之前加三個 helper，並讓 `evaluate_policy` 用它們：

```python
def _leakage(evidence, heldout_rows) -> int:
    """Distinct calibration scenarios a held-out row names by id, scenario hash or workload."""
    identities = {
        "scenario_id": dict(zip(evidence.scenario_ids, evidence.scenario_hashes, strict=True)),
        "scenario_hash": {value: value for value in evidence.scenario_hashes},
        "workload_hash": dict(
            zip(evidence.scenario_workload_hashes, evidence.scenario_hashes, strict=True)
        ),
    }
    overlaps = set()
    for row in heldout_rows:
        if isinstance(row, dict):
            for field, lookup in identities.items():
                value = row.get(field)
                if isinstance(value, str) and value in lookup:
                    overlaps.add(lookup[value])
    return len(overlaps)


def _check_adaptive_row(adaptive, policy, policy_sha256) -> None:
    """The row ran under this policy and its decision is the one the policy makes."""
    env = adaptive.environment
    if (
        env.environment_fingerprint != policy.environment_fingerprint
        or env.postgresql_major != policy.postgresql_major
        or env.backend_schema_version != policy.backend_schema_version
        or adaptive.schema_version != policy.benchmark_schema_version
    ):
        raise ValidationFailed("incompatible_policy")
    decision = select_strategy("auto", adaptive.features(), policy)
    if (
        adaptive.policy_id != policy.id
        or adaptive.policy_sha256 != policy_sha256
        or adaptive.selected_strategy != decision.selected_strategy.value
        or adaptive.strategy_reason != decision.reason
        or adaptive.policy_version != policy.policy_version
        or adaptive.estimated_incremental_ms != decision.estimated_incremental_ms
        or adaptive.estimated_full_ms != decision.estimated_full_ms
    ):
        raise ValidationFailed("policy_decision_mismatch")


def _aggregates(pooled) -> dict[str, dict[str, float]]:
    return {
        method: {
            "p50": statistics.median(values),
            "p95": sorted(values)[round(0.95 * (len(values) - 1))],
        }
        for method, values in pooled.items()
    }


def _gate_ratios(aggregates, method) -> tuple[float, float]:
    median_ratio = aggregates[method]["p50"] / min(aggregates[m]["p50"] for m in FIXED_METHODS)
    p95_ratio = aggregates[method]["p95"] / min(aggregates[m]["p95"] for m in FIXED_METHODS)
    if not all(math.isfinite(value) for value in (median_ratio, p95_ratio)):
        raise ValidationFailed("invalid_evaluation_aggregate")
    return median_ratio, p95_ratio
```

再把 `evaluate_policy` 的主體換成（輸出、例外、順序都與原本相同；唯一改變是 `policy_version` 跟 policy 自己的版本比，對 v1 等價）：

```python
def evaluate_policy(policy_from, heldout_rows) -> EvaluationResult:
    policy, evidence = load_calibration(policy_from)
    policy_from = Path(policy_from)
    policy_sha256 = policy_file_sha256(policy_from, policy)
    # Use complete verified coverage, including NO_OP scenarios excluded from fit.
    # Count distinct calibration scenarios, not repeated methods or identity aliases.
    overlaps = _leakage(evidence, heldout_rows)
    if overlaps:
        raise ValidationFailed("workload_leakage", fields={"overlap_count": overlaps})
    groups = validate_rows(heldout_rows, seeds=HELDOUT_SEEDS, methods=EVALUATION_METHODS)
    ratios50, ratios95, reports = [], [], []
    pooled = {method: [] for method in EVALUATION_METHODS}
    for scenario_hash, group in groups.items():
        adaptive = group["postgres_adaptive"]
        _check_adaptive_row(adaptive, policy, policy_sha256)
        fixed = [group[method] for method in FIXED_METHODS]
        ratio50 = adaptive.maintenance_p50_ms / min(r.maintenance_p50_ms for r in fixed)
        ratio95 = adaptive.maintenance_p95_ms / min(r.maintenance_p95_ms for r in fixed)
        ratios50.append(ratio50)
        ratios95.append(ratio95)
        for method, row in group.items():
            pooled[method].extend(sample.maintenance_ms for sample in row.samples)
        reports.append(
            {
                "scenario_hash": scenario_hash,
                "median_ratio": ratio50,
                "p95_ratio": ratio95,
                "selected_strategy": adaptive.selected_strategy,
            }
        )
    # Normative gates use each method's pooled held-out latency distribution.
    # Retain per-scenario regressions as a separate conservative diagnostic.
    aggregates = _aggregates(pooled)
    median_ratio, p95_ratio = _gate_ratios(aggregates, "postgres_adaptive")
    return EvaluationResult(
        policy_id=policy.id,
        policy_sha256=policy_sha256,
        parity_rate=1.0,
        overlap_count=0,
        scenario_count=len(groups),
        median_ratio=median_ratio,
        p95_ratio=p95_ratio,
        median_gate_pass=median_ratio <= 1.05,
        p95_gate_pass=p95_ratio <= 1.10,
        performance_pass=median_ratio <= 1.05 and p95_ratio <= 1.10,
        every_scenario_pass=all(ratio <= 1.05 for ratio in ratios50)
        and all(ratio <= 1.10 for ratio in ratios95),
        aggregate_maintenance_ms=aggregates,
        scenarios=reports,
        empirical_crossover=empirical_crossover(evidence),
    )
```

（先用 `git show HEAD:tests/performance/provenance/evaluate_adaptive.py` 對照原本 `evaluate_policy` 的最後幾行，確認 `EvaluationResult(...)` 的每個參數都保留，特別是 `empirical_crossover=`。）

- [ ] **Step 6: 實作——`evaluate_policies` 與結果模型**

在 `evaluate_policy` 之後加：

```python
DIAGNOSTIC_RATIO = 1.05  # spec 2026-10-09 §7.3: a scenario is "slow" above this p50 ratio


class PolicyEvaluation(_Strict):
    method: str
    policy_id: str
    policy_sha256: str
    policy_version: str
    median_ratio: float
    p95_ratio: float
    median_gate_pass: bool
    p95_gate_pass: bool
    performance_pass: bool
    non_no_op_scenarios: int
    scenarios_over: int
    worst_ratio: float | None
    by_decade: dict[str, dict[str, object]]
    scenarios: list[dict]


class HeldoutV2Result(_Strict):
    seeds: list[int]
    parity_rate: float
    overlap_count: int
    scenario_count: int
    v2: PolicyEvaluation
    v1: PolicyEvaluation
    h1: bool
    h1a: bool
    aggregate_maintenance_ms: dict[str, dict[str, float]]
    empirical_crossover: dict[str, object]


def _policy_evaluation(method, policy, policy_sha256, aggregates, reports) -> PolicyEvaluation:
    median_ratio, p95_ratio = _gate_ratios(aggregates, method)
    active = [report for report in reports if report["selected_strategy"] != "NO_OP"]
    by_decade: dict[str, dict[str, object]] = {}
    for report in reports:
        cell = by_decade.setdefault(
            str(report["decade"]),
            {"scenarios": 0, "over": 0, "worst_ratio": None, "selected": {}},
        )
        selected = cell["selected"]
        selected[report["selected_strategy"]] = selected.get(report["selected_strategy"], 0) + 1
        if report["selected_strategy"] == "NO_OP":
            continue
        cell["scenarios"] += 1
        cell["over"] += int(report["median_ratio"] > DIAGNOSTIC_RATIO)
        worst = cell["worst_ratio"]
        cell["worst_ratio"] = (
            report["median_ratio"] if worst is None else max(worst, report["median_ratio"])
        )
    return PolicyEvaluation(
        method=method,
        policy_id=policy.id,
        policy_sha256=policy_sha256,
        policy_version=policy.policy_version,
        median_ratio=median_ratio,
        p95_ratio=p95_ratio,
        median_gate_pass=median_ratio <= 1.05,
        p95_gate_pass=p95_ratio <= 1.10,
        performance_pass=median_ratio <= 1.05 and p95_ratio <= 1.10,
        non_no_op_scenarios=len(active),
        scenarios_over=sum(report["median_ratio"] > DIAGNOSTIC_RATIO for report in active),
        worst_ratio=max((report["median_ratio"] for report in active), default=None),
        by_decade=by_decade,
        scenarios=reports,
    )


def evaluate_policies(policy_from, comparison_from, heldout_rows) -> HeldoutV2Result:
    """Held-out v2 (spec 2026-10-09 §6.3, §7): policy v2 and the frozen v1 on the same unseen
    rows. The gate is v2's; v1's numbers stand beside it; H1 and H1a compare the two."""
    policy, evidence = load_calibration(policy_from)
    comparison, comparison_evidence = load_calibration(comparison_from)
    if (
        policy.policy_version != POLICY_VERSION_V2
        or comparison.policy_version != POLICY_VERSION
        or calibration_text(evidence) != calibration_text(comparison_evidence)
    ):
        raise ValidationFailed("invalid_policy_pair")
    overlaps = _leakage(evidence, heldout_rows)
    if overlaps:
        raise ValidationFailed("workload_leakage", fields={"overlap_count": overlaps})
    groups = validate_rows(heldout_rows, seeds=HELDOUT_V2_SEEDS, methods=HELDOUT_V2_METHODS)
    policies = {"postgres_adaptive": policy, "postgres_adaptive_v1": comparison}
    shas = {
        "postgres_adaptive": policy_file_sha256(Path(policy_from), policy),
        "postgres_adaptive_v1": policy_file_sha256(Path(comparison_from), comparison),
    }
    pooled = {method: [] for method in HELDOUT_V2_METHODS}
    reports = {method: [] for method in _ADAPTIVE_METHODS}
    for scenario_hash, group in groups.items():
        best = min(group[method].maintenance_p50_ms for method in FIXED_METHODS)
        for method in _ADAPTIVE_METHODS:
            row = group[method]
            _check_adaptive_row(row, policies[method], shas[method])
            reports[method].append(
                {
                    "scenario_hash": scenario_hash,
                    "decade": edges_decade(row.total_edges),
                    "median_ratio": row.maintenance_p50_ms / best,
                    "selected_strategy": row.selected_strategy,
                }
            )
        for method, row in group.items():
            pooled[method].extend(sample.maintenance_ms for sample in row.samples)
    aggregates = _aggregates(pooled)
    v2, v1 = (
        _policy_evaluation(method, policies[method], shas[method], aggregates, reports[method])
        for method in _ADAPTIVE_METHODS
    )

    def over(evaluation: PolicyEvaluation, decade: str) -> int:
        return int(evaluation.by_decade.get(decade, {}).get("over", 0))

    return HeldoutV2Result(
        seeds=list(HELDOUT_V2_SEEDS),
        parity_rate=1.0,
        overlap_count=0,
        scenario_count=len(groups),
        v2=v2,
        v1=v1,
        h1=v2.scenarios_over < v1.scenarios_over,
        h1a=over(v2, "3") < over(v1, "3"),
        aggregate_maintenance_ms=aggregates,
        empirical_crossover=empirical_crossover(evidence),
    )
```

- [ ] **Step 7: 實作——CLI**

把 `main` 的 `try:` 區塊內容抽成兩個函式，`main` 只做分派：

```python
def _heldout_v1(args) -> int:
    policy, evidence = load_calibration(args.policy_from)
    pg_runtime = benchmark.postgres_preflight()
    work_dir = args.work_dir or args.output.with_name(args.output.stem + "-work")
    rows = collect_rows(
        work_dir,
        scenario_matrix(seeds=HELDOUT_SEEDS),
        methods=EVALUATION_METHODS,
        pg_runtime=pg_runtime,
        policy=policy,
        evidence=evidence,
        policy_from=args.policy_from,
        isolated=True,
    )
    result = evaluate_policy(args.policy_from, rows)
    document = {
        "kind": "postgres-provenance-heldout-v1",
        "evaluation": result.model_dump(),
        "results": rows,
    }
    benchmark.validate_publication_explain(document)
    write_once_text(args.output, json.dumps(document, indent=2) + "\n")
    status = "OK" if result.performance_pass else "FAIL"
    print(f"VERDICT cmd=provenance.evaluate status={status}", file=sys.stderr)
    return 0 if result.performance_pass else 1


def _heldout_v2(args) -> int:
    policy, evidence = load_calibration(args.policy_from)
    comparison, _ = load_calibration(args.comparison_policy_from)
    pg_runtime = benchmark.postgres_preflight()
    work_dir = args.work_dir or args.output.with_name(args.output.stem + "-work")
    rows = collect_rows(
        work_dir,
        scenario_matrix(seeds=HELDOUT_V2_SEEDS),
        methods=HELDOUT_V2_METHODS,
        pg_runtime=pg_runtime,
        policy=policy,
        evidence=evidence,
        policy_from=args.policy_from,
        comparison=comparison,
        comparison_from=args.comparison_policy_from,
        isolated=True,
    )
    result = evaluate_policies(args.policy_from, args.comparison_policy_from, rows)
    document = {
        "kind": "postgres-provenance-heldout-v2",
        "evaluation": result.model_dump(),
        "results": rows,
    }
    benchmark.validate_publication_explain(document)
    write_once_text(args.output, json.dumps(document, indent=2) + "\n")
    status = "OK" if result.v2.performance_pass else "FAIL"
    print(
        f"VERDICT cmd=provenance.evaluate status={status} "
        f"h1={str(result.h1).lower()} h1a={str(result.h1a).lower()}",
        file=sys.stderr,
    )
    return 0 if result.v2.performance_pass else 1


def main(argv=None) -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--policy-from", type=Path, required=True)
    parser.add_argument("--comparison-policy-from", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    try:
        args = parser.parse_args(argv)
        if args.output.exists():
            raise ValidationFailed("heldout_output_exists")
        if args.comparison_policy_from is None:
            return _heldout_v1(args)
        return _heldout_v2(args)
    except Exception:
        print(
            "Held-out evaluation failed (live service or verified evidence required)",
            file=sys.stderr,
        )
        print("VERDICT cmd=provenance.evaluate status=FAIL", file=sys.stderr)
        return 1
```

- [ ] **Step 8: 跑兩個測試檔全部**

Run: `uv run pytest tests/unit/provenance/test_adaptive_evaluation.py tests/unit/provenance/test_adaptive_benchmark.py tests/unit/provenance/test_policy_v1_regression.py -q -o addopts="" -p no:cacheprovider`
Expected: 全部 PASS，包括原本的 `test_heldout_cli_publishes_honest_gates_on_unit_data`（v1 路徑的輸出不變）。

- [ ] **Step 9: lint 並 commit**

```bash
uv run ruff check tests/performance/provenance/evaluate_adaptive.py tests/unit/provenance/test_adaptive_evaluation.py
uv run ruff format --check tests/performance/provenance/evaluate_adaptive.py tests/unit/provenance/test_adaptive_evaluation.py
git add tests/performance/provenance/evaluate_adaptive.py tests/unit/provenance/test_adaptive_evaluation.py
git commit -m "feat(provenance): held-out v2 evaluates policy v2 and the frozen v1 together"
```

---

### Task 9: real 驗證加 v1 比較方法（`real_validation.py`）

**Files:**
- Modify: `tests/performance/provenance/real_validation.py`
- Test: `tests/unit/provenance/test_adaptive_benchmark.py`

**Interfaces:**
- Consumes: Task 7 的 `COMPARISON_METHOD`、`policy_for`、`prepare_policy_workload(..., *comparisons)`。
- Produces:
  - `real_validation._six_method_rows(root, source_data, source_configs, *, runtime, policy, evidence, policy_sha256, comparison=None, comparison_sha256=None) -> list[dict]`
  - `validate(..., comparison_from=None)`；CLI `--comparison-policy-from`；文件在有比較時多 `comparison_policy_id`、`comparison_policy_sha256`。

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/provenance/test_adaptive_benchmark.py` 檔尾加（`real_validation` 若尚未 import：`from performance.provenance import real_validation`）：

```python
def test_real_six_method_rows_add_the_v1_comparison_only_when_given(tmp_path, monkeypatch):
    calls = []
    installs = []
    monkeypatch.setattr(real_validation, "TRANSITIONS", (("a", "b"),))
    monkeypatch.setattr(
        real_validation, "build_real_scenario", lambda root, data, configs, index: "workload"
    )
    monkeypatch.setattr(
        bench,
        "prepare_policy_workload",
        lambda workload, policy, evidence, *comparisons: installs.append(comparisons) or workload,
    )

    def run(workload, method, *, pg_runtime, policy_id=None, policy_sha256=None):
        calls.append((method, policy_id, policy_sha256))
        return SimpleNamespace(to_dict=lambda: {"method": method, "status": "ok"})

    monkeypatch.setattr(bench, "run_method", run)
    v2 = SimpleNamespace(id="v2-id")
    v1 = SimpleNamespace(id="v1-id")
    real_validation._six_method_rows(
        tmp_path, "data", "configs", runtime="pg", policy=v2, evidence="e", policy_sha256="a" * 64
    )
    assert [call[0] for call in calls] == list(bench.METHODS)
    assert installs == [()]
    calls.clear()
    installs.clear()
    real_validation._six_method_rows(
        tmp_path,
        "data",
        "configs",
        runtime="pg",
        policy=v2,
        evidence="e",
        policy_sha256="a" * 64,
        comparison=v1,
        comparison_sha256="b" * 64,
    )
    assert [call[0] for call in calls] == [*bench.METHODS, "postgres_adaptive_v1"]
    assert ("postgres_adaptive", "v2-id", "a" * 64) in calls
    assert ("postgres_adaptive_v1", "v1-id", "b" * 64) in calls
    assert installs == [(v1,)]


def test_real_validation_refuses_a_comparison_without_six_method(tmp_path):
    with pytest.raises(ValueError, match="six-method"):
        real_validation.validate(tmp_path, tmp_path, comparison_from=tmp_path / "v1.json")
```

- [ ] **Step 2: 跑測試，確認失敗**

Run: `uv run pytest tests/unit/provenance/test_adaptive_benchmark.py -q -o addopts="" -p no:cacheprovider -k real_`
Expected: FAIL，`AttributeError: ... has no attribute '_six_method_rows'`。

- [ ] **Step 3: 實作**

1. 在 `validate` 之前加：

```python
def _six_method_rows(
    root: Path,
    source_data: Path,
    source_configs: Path,
    *,
    runtime,
    policy,
    evidence,
    policy_sha256,
    comparison=None,
    comparison_sha256=None,
) -> list[dict]:
    """Every transition under the six methods, plus the frozen v1 policy when given (spec
    2026-10-09 §6.4: a sanity check of the case that motivated v2, never held-out evidence)."""
    if __package__:
        from . import adaptive_benchmark as benchmark
    else:
        import adaptive_benchmark as benchmark
    methods = benchmark.METHODS
    comparisons = ()
    if comparison is not None:
        methods = (*methods, benchmark.COMPARISON_METHOD)
        comparisons = (comparison,)
    rows = []
    for transition in range(len(TRANSITIONS)):
        workload = build_real_scenario(
            root / f"real-{transition}", source_data, source_configs, transition
        )
        workload = benchmark.prepare_policy_workload(workload, policy, evidence, *comparisons)
        for method in methods:
            method_policy, method_sha256 = benchmark.policy_for(
                method, policy, policy_sha256, comparison, comparison_sha256
            )
            rows.append(
                benchmark.run_method(
                    workload,
                    method,
                    pg_runtime=runtime,
                    policy_id=method_policy,
                    policy_sha256=method_sha256,
                ).to_dict()
            )
    return rows
```

2. `validate` 的簽名在 `policy_from: Path | None = None,` 之後加 `comparison_from: Path | None = None,`；函式開頭（`if six_method:` 之前）加：

```python
    if comparison_from is not None and not six_method:
        raise ValueError("a comparison policy needs the six-method validation")
```

在 `policy, evidence, policy_sha256 = load_frozen_policy(policy_from)` 之後加：

```python
        comparison = comparison_sha256 = None
        if comparison_from is not None:
            comparison, _comparison_evidence, comparison_sha256 = load_frozen_policy(
                comparison_from
            )
```

3. 把 `if six_method:` 區塊裡從 `rows = []` 到 `document["six_method_benchmark"] = rows` 為止（含 for 迴圈與迴圈內的 `prepare_policy_workload` import）換成：

```python
            rows = _six_method_rows(
                root,
                source_data,
                source_configs,
                runtime=runtime,
                policy=policy,
                evidence=evidence,
                policy_sha256=policy_sha256,
                comparison=comparison,
                comparison_sha256=comparison_sha256,
            )
            document["six_method_benchmark"] = rows
```

並在 `document["policy_sha256"] = policy_sha256` 之後加：

```python
            if comparison is not None:
                document["comparison_policy_id"] = comparison.id
                document["comparison_policy_sha256"] = comparison_sha256
```

頂端 `from .adaptive_benchmark import (...)` 兩處清單中的 `METHODS`、`run_method` 若不再使用，移除（ruff 會提示）。

4. `main`：加 `parser.add_argument("--comparison-policy-from", type=Path)`，並把它傳給 `validate(..., comparison_from=args.comparison_policy_from)`。

- [ ] **Step 4: 跑測試**

Run: `uv run pytest tests/unit/provenance -q -o addopts="" -p no:cacheprovider`
Expected: 全部 PASS（`test_real_validation_retains_legacy_fields...` 等既有測試不變）。

- [ ] **Step 5: lint 並 commit**

```bash
uv run ruff check tests/performance/provenance/real_validation.py tests/unit/provenance/test_adaptive_benchmark.py
uv run ruff format --check tests/performance/provenance/real_validation.py tests/unit/provenance/test_adaptive_benchmark.py
git add tests/performance/provenance/real_validation.py tests/unit/provenance/test_adaptive_benchmark.py
git commit -m "feat(provenance): real validation runs the frozen v1 policy beside v2"
```

---

### Task 10: 文件與回歸門檻

**Files:**
- Modify: `README.md`、`README.zh-TW.md`、`docs/guides/POSTGRESQL_PROVENANCE.md`、`.claude/skills/vcp-provenance/SKILL.md`（再整份複製到 `.agents/skills/vcp-provenance/`）、`docs/reference/cli.md`、`docs/handover/HANDOVER.md`、`docs/handover/CODEX_PROMPT.md`、`docs/superpowers/plans/2026-09-13-vcp-postgresql-adaptive-provenance-followups.md`、`docs/superpowers/plans/2026-10-09-vcp-postgres-adaptive-policy-v2-followups.md`、`tests/unit/test_regression_gate.py`
- 不改：`docs/benchmarks/postgres-provenance-v1.md`（spec §8）。

- [ ] **Step 1: README 的路線圖**

`README.md` 第 127 行：把

```markdown
- **Next**: audit wave 1c (code snapshot and artifact authorisation, VCP-004/006).
```

換成

```markdown
- **Now**: adaptive policy v2 for the PostgreSQL backend — a band that scales with each estimate (or with the graph's size decade) in place of v1's fixed 7.3 s band, evaluated against v1 on new held-out seeds ([spec](docs/superpowers/specs/2026-10-09-vcp-postgres-adaptive-policy-v2-design.md)).
- **Next**: audit wave 1c (code snapshot and artifact authorisation, VCP-004/006).
```

並把同一行末尾的 `come after 1.0, and so does adaptive policy v2.` 換成 `come after 1.0.`。

`README.zh-TW.md` 第 127 行：把

```markdown
- **下一步**：稽核 Wave 1c（程式碼快照與產物授權，VCP-004／006）。
```

換成

```markdown
- **進行中**：PostgreSQL 後端的 adaptive policy v2——以跟著每個預估值（或圖的規模層）縮放的信心帶，取代 v1 固定 7.3 秒的帶，並在新的 held-out seeds 上與 v1 同場比較（[spec](docs/superpowers/specs/2026-10-09-vcp-postgres-adaptive-policy-v2-design.md)）。
- **下一步**：稽核 Wave 1c（程式碼快照與產物授權，VCP-004／006）。
```

並把同一行末尾的 `排在 1.0 之後，adaptive policy v2 也是。` 換成 `排在 1.0 之後。`

- [ ] **Step 2: 操作指南**

`docs/guides/POSTGRESQL_PROVENANCE.md`：在

```markdown
  §Real RSNA six-method，Plan 12 後記 §5、§7）。
```

之後插入：

```markdown
- policy v2（`postgres-adaptive-v2-9f4e58346529`，0.15.0 起）用同樣的成本模型，但信心帶跟著預估值縮放
  （或依圖的 `total_edges` 數量級分層，見 spec `2026-10-09-vcp-postgres-adaptive-policy-v2-design.md` §13
  記的勝者）。`--policy` 傳 v2 的 id 就走 v2；決策紀錄的 `policy_version` 欄位分辨 v1 與 v2，reason 字彙不變。
  v2 在 held-out v2 上的結果出來之前，上面對 v1 的建議照舊適用。
```

再把

```markdown
- full rebuild 以新 generation 原子發布後刪除舊 generation，但 PostgreSQL 在 VACUUM 前不回收那些 dead
  tuples：重建後 relation/index 約為 incremental 維護的 2 倍，`status` 也變慢，直到 autovacuum 追上。
```

換成

```markdown
- full rebuild 以新 generation 原子發布後刪除舊 generation，但 PostgreSQL 在 VACUUM 前不回收那些 dead
  tuples：重建後 relation/index 約為 incremental 維護的 2 倍，`status` 也變慢，直到 autovacuum 追上。
  vcp 不在 rebuild 收尾自動 VACUUM（2026-10-09 裁決：它會改變 FULL 的量測成本）。想讓 `status` 立刻變快，
  rebuild 之後在同一個 service 上手動跑 `VACUUM (ANALYZE)`：它不鎖讀者，但幾乎不縮小檔案，所以 storage
  仍約 2 倍。不建議 `VACUUM FULL`：它鎖住整張表、擋住所有讀者。
```

- [ ] **Step 3: skill 與鏡射**

`.claude/skills/vcp-provenance/SKILL.md`：把

```markdown
- **已知限制**：policy v1 的信心帶是絕對毫秒，預估差距 < 7.3 s 時一律 FULL——小圖（~1K）與接近全量變更的 transition 會選錯、慢 2–4 倍。這兩種情況直接 `--strategy incremental`。
```

換成

```markdown
- **已知限制**：policy v1 的信心帶是絕對毫秒，預估差距 < 7.3 s 時一律 FULL——小圖（~1K）與接近全量變更的 transition 會選錯、慢 2–4 倍。這兩種情況直接 `--strategy incremental`。
- **policy v2**（0.15.0 起，`--policy postgres-adaptive-v2-9f4e58346529`）：同樣的成本模型，信心帶由 calibration 推導、跟著預估值或規模層縮放；決策的 `policy_version` 分辨 v1／v2。held-out v2 的結果出來前，v1 的建議照舊。
- full rebuild 後要 `status` 立刻變快：手動 `VACUUM (ANALYZE)`（不鎖讀者，storage 不縮小）；不要 `VACUUM FULL`。
```

然後：

```bash
cp -r .claude/skills/vcp-provenance/. .agents/skills/vcp-provenance/
diff -r .claude/skills .agents/skills
```

Expected：`diff` 沒有輸出。

- [ ] **Step 4: `cli.md`、交接、舊後記**

1. `docs/reference/cli.md`：用 `grep -n "\-\-policy" docs/reference/cli.md` 找到 `provenance ingest` 那一列，在它對 `--policy` 的說明後加「（接受 v1 `postgres-adaptive-v1-…` 與 0.15.0 起的 v2 `postgres-adaptive-v2-…`）」。
2. `docs/handover/HANDOVER.md` 與 `docs/handover/CODEX_PROMPT.md`：用 `grep -n "policy v2" docs/handover/*.md` 找到開放待辦裡 policy v2 那一項，把它的說明換成：「**PostgreSQL adaptive policy v2**：進行中（spec `docs/superpowers/specs/2026-10-09-vcp-postgres-adaptive-policy-v2-design.md`、計畫同日期、後記 `…-followups.md`），排在稽核 Wave 1c 之前；發 0.15.0 後依計畫附錄 A 量測。」不動其他項的編號。
3. `docs/superpowers/plans/2026-09-13-vcp-postgresql-adaptive-provenance-followups.md` 的 §3 第 6、7 項各在句尾加：「（2026-10-09：見 `docs/superpowers/specs/2026-10-09-vcp-postgres-adaptive-policy-v2-design.md`；第 7 項裁決為不加 VACUUM，寫進操作指南。）」

- [ ] **Step 5: 回歸門檻列**

`tests/unit/test_regression_gate.py` 的 `GATE` 清單最後一列之後（`]` 之前）加：

```python
    (
        "policy v2 (0.15.0)",
        "provenance: policy v1 reproduces every published decision; policy v2 keeps v1's cost "
        "models, scales its band with the estimate or the size decade, and is told apart by "
        "policy_version; the band comparison picks the relative band within 0.1; held-out v2 "
        "evaluates v2 and the frozen v1 on the same new seeds",
        {
            "tests/unit/provenance/test_policy_v1_regression.py": [
                "test_v1_policy_reproduces_every_published_decision",
            ],
            "tests/unit/provenance/test_policy_bands.py": [
                "test_relative_band_scales_with_the_estimate",
                "test_stratified_band_uses_the_size_decade_and_the_nearest_stratum",
            ],
            "tests/unit/provenance/test_strategy_v2.py": [
                "test_fit_cost_models_and_fit_policy_v2_keep_the_v1_cost_models",
                "test_v2_relative_band_decides_and_keeps_the_v1_reasons",
                "test_an_unknown_policy_version_is_incompatible_on_load",
            ],
            "tests/unit/provenance/test_compare_bands.py": ["test_distance_and_choose"],
            "tests/unit/provenance/test_adaptive_evaluation.py": [
                "test_evaluate_policies_reports_both_and_tests_h1",
                "test_evaluate_policies_rejects_a_v1_row_decided_by_v2",
            ],
        },
    ),
```

- [ ] **Step 6: 後記補裁決**

在 `docs/superpowers/plans/2026-10-09-vcp-postgres-adaptive-policy-v2-followups.md` 的 §1 補上：

```markdown
2. **v2 policy**：`postgres-adaptive-v2-9f4e58346529`，`policy.json` SHA-256 `<Task 6 Step 8 印出的值>`；成本模型與 v1 相同（相對誤差 ≤ 1e-9 的檢查通過）。
3. **telemetry 不加 reason code**（spec §4.6）：v1 與 v2 以 `policy_version` 分辨，DDL 與 `POSTGRES_SCHEMA_VERSION` 不變。
4. **runner 的相容性**：比較方法 `postgres_adaptive_v1` 不進 `METHODS`；所有新參數沒給時與 0.14.0 逐位元相同，既有的 mock 簽名與 checkpoint 契約不變。
```

- [ ] **Step 7: 跑門檻測試與全套**

```bash
uv run pytest tests/unit/test_regression_gate.py -q -o addopts="" -p no:cacheprovider
uv run pytest --cov=vcp -o addopts="" -p no:cacheprovider
uv run ruff check . && uv run ruff format --check .
git diff --check
```

Expected：全部 PASS、覆蓋率 ≥ 80%、ruff 乾淨、`git diff --check` 沒有輸出。

- [ ] **Step 8: commit**

```bash
git add README.md README.zh-TW.md docs/guides/POSTGRESQL_PROVENANCE.md .claude/skills/vcp-provenance/SKILL.md .agents/skills/vcp-provenance/SKILL.md docs/reference/cli.md docs/handover/HANDOVER.md docs/handover/CODEX_PROMPT.md docs/superpowers/plans/2026-09-13-vcp-postgresql-adaptive-provenance-followups.md docs/superpowers/plans/2026-10-09-vcp-postgres-adaptive-policy-v2-followups.md tests/unit/test_regression_gate.py
git commit -m "docs: adaptive policy v2 in the roadmap, guide, skill and regression gate"
```

---

### Task 11: 發版 0.15.0（功能 PR 合併、使用者核可之後）

照 `.claude/skills/vcp-release-and-environments/SKILL.md` 的發版四步，跟 0.13.0（PR #36）、0.14.0（PR #38）同一組 8 個檔案。

- [ ] **Step 1：開發版分支**：`git fetch origin` 後從 `origin/main` 開 `chore/release-0.15.0`。
- [ ] **Step 2：版本字串三處**：`src/vcp/__init__.py` 的 `__version__ = "0.14.0"` → `"0.15.0"`；`.claude/.claude-plugin/plugin.json` 的 `"version": "0.14.0",` → `"0.15.0",`；`tests/unit/test_package.py` 的 `EXPECTED_CANDIDATE_VERSION = "0.14.0"` → `"0.15.0"`。
- [ ] **Step 3：CHANGELOG**：在 `## [0.14.0] - ...` 之上加 `## [0.15.0] - <stamp()[:10]>`，內容：
  - MINOR 的理由：policy artifact 的新內容（`policy_version: postgres-adaptive-v2` 與 `band`）；telemetry 與 VERDICT 的 `policy_version` 多一個值；benchmark runner 的新 seeds 常數 `HELDOUT_V2_SEEDS`、新方法 `postgres_adaptive_v1`、新選項 `--comparison-policy-from`。
  - Added：`policy_bands.py`、`AdaptivePolicyV2`、`fit_policy_v2`、`fit_cost_models`、`policy_from_payload`；`compare_bands.py`、`publish_policy_v2.py`；held-out v2 評估；比較證據與 v2 policy 產物。
  - Changed：`load_policy_artifact` / `write_policy_artifact` 依 `policy_version` 分派；操作指南的 VACUUM 建議；README 路線圖。
  - 相容性：DDL、`POSTGRES_SCHEMA_VERSION`（1）、`BENCHMARK_SCHEMA_VERSION`（1）、SQLite 索引與台帳不變，不需要遷移；0.14.0 讀不了 v2 的 `policy.json`（`incompatible_policy`），v1 policy 在 0.15.0 照舊可用。
- [ ] **Step 4：重裝、全套**：`uv sync --frozen --reinstall-package vcp`，`uv run pytest --cov=vcp -o addopts="" -p no:cacheprovider`，取 P passed / S skipped / C%。
- [ ] **Step 5：交接與 README 的版本、日期、數字**（HANDOVER 的標題日期、版本行、測試行；CODEX_PROMPT 的標題日期、版本行、覆蓋率；兩份 README 的版本與測試數 badge、狀態行），照 0.14.0 那次的位置改。
- [ ] **Step 6：檢查、commit、PR**：`uv run ruff check . && uv run ruff format --check .`、`git diff --check`；`git commit -m "chore(release): v0.15.0"`（不加署名）；push、`gh pr create`，PR 描述寫摘要與測試計畫，不加署名行。
- [ ] **Step 7：CI 綠、使用者核可後**：merge（merge commit），確認 `gh pr view <PR> --json state` 為 `MERGED` 再做清理；在合併 commit 上 `git tag -a v0.15.0 <合併 commit> -m "vcp 0.15.0"`、`git push origin v0.15.0`。

---

## 附錄 A：發版後的量測（不在本計畫的 task 內）

全部在 `v0.15.0` 的 detached worktree 執行，由 `C:/vcp-data/bench/supervise.py` 看管，10/26 之後開始；PostgreSQL 只用 service name（`VCP_TEST_PG_SERVICE`、`PGSERVICEFILE`、`PGPASSFILE` 以路徑指定，不讀內容）。

```bash
git worktree add --detach ../Vision-contest-pipeline-v0150-bench v0.15.0
cd ../Vision-contest-pipeline-v0150-bench && uv sync --frozen --extra postgres
```

1. six-method v2（calibration seeds，約 2 天）：

```bash
uv run python tests/performance/provenance/adaptive_benchmark.py --policy-from docs/benchmarks/postgres-provenance-policy-v2.json --output docs/benchmarks/postgres-provenance-six-method-v2.json
```

2. held-out v2（新 seeds、四方法，約 1–1.5 天）：

```bash
uv run python tests/performance/provenance/evaluate_adaptive.py --policy-from docs/benchmarks/postgres-provenance-policy-v2.json --comparison-policy-from docs/benchmarks/postgres-provenance-calibration-v2.json --output docs/benchmarks/postgres-provenance-heldout-v2.json
```

3. real RSNA v2（唯讀複本，約 20 分鐘）：

```bash
uv run python tests/performance/provenance/real_validation.py --data-root <唯讀複本的 data root> --configs-root <唯讀複本的 configs root> --six-method --policy-from docs/benchmarks/postgres-provenance-policy-v2.json --comparison-policy-from docs/benchmarks/postgres-provenance-calibration-v2.json --output docs/benchmarks/postgres-provenance-real-rsna-v2.json
```

結果只從這三個 JSON 回讀後才寫進 `docs/benchmarks/postgres-provenance-v2.md` 與 course brief v2；§7 的驗收條件一字不改地套用；中止與汙染照 spec §6.1 的規則處理。
