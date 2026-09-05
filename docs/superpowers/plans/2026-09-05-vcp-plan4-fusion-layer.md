# vcp 融合層（子專案 4）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `vcp fuse` 融合層：進 git 的配方、融合器登記表（`wbf` / `mean` / `rank_mean`）、把成員 run 的預測檔融合成普通 run 的 `build`（位元級重現）、以及一個命令產出「少一名成員」變體與準入預登記的 `ablate`，準入判決全交給既有 `vcp eval judge`。

**Architecture:** 新套件 `src/vcp/fuse/`（`schema`、`recipes`、`members`、`build`、`ablate`、`fusers/{base,wbf,scores}`），CLI 新增 `src/vcp/cli_fuse.py` 掛成 `vcp fuse`。融合結果落在 `runs/<run_id>/`，與 ingest 產的 run 同形，另加 `fuse.json` 記每個成員預測檔的 sha 與輸出 sha。量測層唯一改動：`prereg._measured_subsets` 公開為 `measured_subsets`。資料層零改動。

**Tech Stack:** Python 3.12（uv）、pydantic v2、typer、numpy（既有）、pytest / ruff。不新增任何執行期依賴；ensemble-boxes 只是可選的測試 oracle（`pytest.importorskip`）。

**Spec:** `docs/superpowers/specs/2026-09-05-vcp-fusion-layer-design.md`（v1，全部章節已核可）。量測層介面見 `docs/superpowers/specs/2026-09-04-vcp-measurement-layer-design.md`（含 §15）。

## Global Constraints

- 每個專案命令前綴 `uv run`；pytest 的 `addopts=-q` 會藏摘要，看細節時用 `uv run pytest -o addopts="" -q ...`。
- 三條機械鐵則（`CLAUDE.md`）：取時只用 `vcp.core.time.utc_now()` / `stamp()`（ruff TID251 會擋 `datetime.now`、`time.time`）；每個 CLI 命令以 `VERDICT cmd=... status=OK|WARN|FAIL|ABORT` 收尾、exit 0 / 0 / 1 / 2、永不互動、`--json` 時 JSON 到 stdout、VERDICT 到 stderr；venv 隔離。
- 通用性：`src/vcp` 不得出現比賽名稱或比賽專屬欄名；融合器依 payload 登記；比賽專屬後處理（rescorer 類）經 `--plugin` 從 `projects/` 登記，核心不內建。
- 不可變性：配方檔與變體配方檔寫後不改（要改就換 id）；`run.yaml` 與 `fuse.json` 換寫留痕；預測檔取代必經 `--replace` 並在 `history.jsonl` 記舊 sha；預登記走量測層的 `create_prereg`，不另寫。
- 全有或全無：`recipe` / `build` / `ablate` 三個命令都先做完所有檢查再寫第一個檔（spec §6.2、§6.3）。
- 寫入會被 hash 或被 git 紀錄的文字檔一律 `encoding="utf-8", newline="\n"`；讀檔一律指定 `encoding="utf-8"`；JSON 用 `json.dumps(..., ensure_ascii=False)`。
- 錯誤語意：使用者資料或選項問題 → `ValidationFailed`（FAIL）；成員預測檔 sha 不符 → `IntegrityError`（FAIL）；成員 dataset / hash / plan 不符、subset 不在 plan → `PlanMismatchError`（ABORT）；未知 method、重複登記 → `RegistryError`（ABORT）。`reason=` 的固定字彙（`recipe_exists`、`no_common_subset`、`single_member`、`candidate_measured`、`output_exists`、`run_bound_elsewhere`、`variant_conflict`）以**訊息前綴**呈現（同量測層的 `already_measured`：`run_command` 把例外文字放進 `reason=`），不放進 `VcpError.fields`；`fields` 只放 `member=` / `subset=` / `run=` / `recipe=` / `prereg=` / `param=` / `method=` / `payload=`。
- 決定性：同成員 sha + 同配方 ⇒ 同輸出 sha。所有排序穩定、沒有隨機、tie 依成員順序再依檔內順序。
- 測試：`tests/conftest.py` 的 autouse fixture 已把兩個根目錄指到 tmp；需要真實路徑物件時用 `roots` fixture（`roots.data`、`roots.configs`）；夾具 helper 在 `tests/helpers.py`（`from helpers import ...`，`det_with_runs` 給 det 資料集 + `perfect`（valA/valB/holdout）與 `noisy`（valA/valB）兩個 run，`dataset_with_perfect_run` 給任意任務的一個 run）；真資料測試放 `tests/integration/`、標記 `realdata`、資料缺席即 skip。
- ruff：line-length 100、select `E F I UP B TID`；`uv run ruff format --check .` 也要過。覆蓋率門檻 80%（`uv run pytest --cov=vcp`）。
- 檔案上限 800 行；函式盡量 < 50 行。
- 每個 commit 訊息結尾加空行與 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`；永不 `git add -A`（`.superpowers/`、`.claude/worktrees/` 為 scratch）。

### 計畫層決定（spec 沒寫死、本計畫定案的小事）

1. `Fuser` 協定多一個 `check_params(params) -> None`（spec §7.1 列的五項之外）：`recipe` 命令要在寫檔前拒絕解析不了的參數值（spec §6「params 可解析」），而登記表無法知道各融合器的型別。`mean` / `rank_mean` 的實作是空的。
2. `build` 的快取判斷需要「不寫檔就知道輸出 sha」：`build.content_sha` 重複 `write_predictions` 的序列化規則（依 `sample_id` 排序、`exclude_none`、LF），並以一個測試把兩者釘在一起（Task 6）。
3. 端到端測試的「噪音」成員 = 落在 64×64 view 遠處、與任何 gold 框 IoU 為 0 的純假陽性（`_far_fps`）；「半對」成員的判決只斷言「有判決且非 INVALID」（合成資料上 Δ 的符號不可靠）。
4. `ablate` 對「存在但沒有對應配方檔」的 `fuse-<R>-minus-<X>` run 一律 FAIL `run_bound_elsewhere`（一個沒有配方的融合 run 不可能是本命令建的）。

---

## File Structure

| 檔案 | 責任 | 任務 |
|---|---|---|
| `src/vcp/fuse/__init__.py`、`src/vcp/fuse/schema.py` | 套件說明；`Member`、`Recipe`、`MemberRecord`、`SubsetBuild`、`FuseRecord` | 1 |
| `src/vcp/fuse/recipes.py` | 配方檔路徑、讀寫（寫了不改）、sha、內容比對 | 1 |
| `src/vcp/fuse/fusers/__init__.py`、`src/vcp/fuse/fusers/base.py` | `MemberPredictions`、`FuseContext`、`Fuser` 協定、`FUSERS` 登記表、參數解析 helper、payload 適用性 | 2 |
| `src/vcp/fuse/fusers/wbf.py` | `wbf`（boxes） | 3 |
| `src/vcp/fuse/fusers/scores.py` | `mean`（scores / targets）、`rank_mean`（scores） | 4 |
| `src/vcp/fuse/members.py`、`src/vcp/cli_fuse.py`（新）、`src/vcp/cli.py` | 成員驗證、`--member` 解析、`trained_on` 聯集、共同 subset；`vcp fuse recipe`；掛 `fuse_app` | 5 |
| `src/vcp/fuse/build.py`、`cli_fuse.py` | `BuildSpec` / `build_run`、`fuse.json` 讀寫、`content_sha`、子集選擇、快取 / 取代、`vcp fuse build` | 6 |
| `src/vcp/fuse/ablate.py`、`src/vcp/measure/prereg.py`、`cli_fuse.py` | 變體配方、準入預登記、全有或全無；`measured_subsets` 公開；`vcp fuse ablate` | 7 |
| `tests/unit/test_e2e_fuse.py`、`tests/integration/test_rsna_knee_fuse.py`、`README.md`、`CLAUDE.md` | 端到端（det 三成員準入、multilabel 兩種分數融合、插件融合器）、真資料、文件 | 8 |
| `tests/unit/fuse/...`、`tests/unit/test_cli_fuse.py` | 每任務的單元測試 | 各任務 |

任務順序 1 → 8。Task 1 的 schema 被所有後續任務使用；Task 2 的登記表被 3、4、5、6 使用；Task 5 的 `members` 被 6、7 使用；Task 6 的 `build_run` 被 7 使用。

---

### Task 1: 配方與融合紀錄的資料模型、配方檔讀寫（spec §4.1、§4.2、§8 的 schema 驗證）

**Files:**
- Create: `src/vcp/fuse/__init__.py`
- Create: `src/vcp/fuse/schema.py`
- Create: `src/vcp/fuse/recipes.py`
- Create: `tests/unit/fuse/__init__.py`（空檔）
- Test: `tests/unit/fuse/test_schema_recipes.py`

**Interfaces:**
- Consumes: `vcp.core.config.load_yaml_model / dump_yaml_model`、`vcp.core.hashing.sha256_file`、`vcp.core.paths.DatasetPaths / validate_name`、`vcp.core.errors.ValidationFailed`。
- Produces:
  - `Member(run: str, weight: float = 1.0)`（weight 有限且 > 0）
  - `Recipe(recipe_id, dataset, plan_id, method, params: dict[str, str], members: list[Member]（≥ 1、run 不重複）, notes: str = "", created_at: str)`
  - `MemberRecord(run, weight, trained_on: list[str])`、`SubsetBuild(member_sha256: dict[str, str], output_sha256, samples: int, empty: int, built_at)`、`FuseRecord(run_id, recipe_id, recipe_sha256, method, method_version, params, members: list[MemberRecord], subsets: dict[str, SubsetBuild] = {}, vcp_version)`
  - `recipes.RECIPE_EXISTS = "recipe_exists"`、`fuse_dir(paths) -> Path`、`recipe_path(paths, recipe_id) -> Path`、`load_recipe(paths, recipe_id) -> Recipe`、`save_recipe(paths, recipe) -> Path`（已存在 → `ValidationFailed`）、`recipe_sha(paths, recipe_id) -> str`、`same_recipe(a, b) -> bool`（比 dataset / plan_id / method / params / members，不比 notes 與 created_at）

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/fuse/__init__.py`：空檔。

`tests/unit/fuse/test_schema_recipes.py`：

```python
import pytest
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.fuse.recipes import (
    RECIPE_EXISTS,
    fuse_dir,
    load_recipe,
    recipe_path,
    recipe_sha,
    same_recipe,
    save_recipe,
)
from vcp.fuse.schema import FuseRecord, Member, MemberRecord, Recipe, SubsetBuild

STAMP = "2026-09-05T00:00:00.000Z"


def _recipe(**kw) -> Recipe:
    base = dict(
        recipe_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        method="wbf",
        params={"iou": "0.6"},
        members=[Member(run="a"), Member(run="b", weight=0.5)],
        created_at=STAMP,
    )
    return Recipe(**{**base, **kw})


@pytest.mark.parametrize("weight", [0.0, -1.0, float("nan"), float("inf")])
def test_member_weight_must_be_finite_positive(weight):
    with pytest.raises(ValidationError):
        Member(run="a", weight=weight)


def test_member_defaults_to_weight_one():
    assert Member(run="a").weight == 1.0


def test_recipe_rejects_duplicate_and_empty_members():
    with pytest.raises(ValidationError, match="duplicate"):
        _recipe(members=[Member(run="a"), Member(run="a", weight=0.5)])
    with pytest.raises(ValidationError):
        _recipe(members=[])


def test_recipe_keeps_member_order():
    r = _recipe(members=[Member(run="z"), Member(run="a")])
    assert [m.run for m in r.members] == ["z", "a"]


def test_save_and_load_roundtrip(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    r = _recipe()
    path = save_recipe(paths, r)
    assert path == recipe_path(paths, "r1") == fuse_dir(paths) / "r1.yaml"
    assert path.read_bytes().count(b"\r") == 0
    assert load_recipe(paths, "r1") == r
    assert recipe_sha(paths, "r1") == sha256_file(path)


def test_save_refuses_existing(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    save_recipe(paths, _recipe())
    before = recipe_path(paths, "r1").read_bytes()
    with pytest.raises(ValidationFailed, match=RECIPE_EXISTS) as ei:
        save_recipe(paths, _recipe(params={"iou": "0.7"}))
    assert ei.value.fields == {"recipe": "r1"}
    assert recipe_path(paths, "r1").read_bytes() == before


def test_load_checks_id_and_dataset(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    with pytest.raises(ValidationFailed, match="not found") as ei:
        load_recipe(paths, "nope")
    assert ei.value.fields == {"recipe": "nope"}
    other = DatasetPaths.resolve("other", data_root=roots.data, configs_root=roots.configs)
    save_recipe(other, _recipe(dataset="tiny"))  # a file whose content names another dataset
    with pytest.raises(ValidationFailed, match="belongs to dataset"):
        load_recipe(other, "r1")
    with pytest.raises(ValidationFailed, match="invalid name"):
        recipe_path(paths, "../r1")


def test_same_recipe_ignores_notes_and_stamp():
    a = _recipe()
    assert same_recipe(a, _recipe(notes="x", created_at="2030-01-01T00:00:00.000Z"))
    assert not same_recipe(a, _recipe(params={"iou": "0.7"}))
    assert not same_recipe(a, _recipe(members=[Member(run="a"), Member(run="b", weight=1.0)]))
    assert not same_recipe(a, _recipe(members=[Member(run="b", weight=0.5), Member(run="a")]))


def test_fuse_record_model():
    rec = FuseRecord(
        run_id="fuse-r1",
        recipe_id="r1",
        recipe_sha256="ab" * 32,
        method="wbf",
        method_version="1",
        params={"iou": "0.6"},
        members=[MemberRecord(run="a", weight=1.0, trained_on=["train"])],
        vcp_version="0.1.0",
    )
    assert rec.subsets == {}
    rec2 = rec.model_copy(
        update={
            "subsets": {
                "valA": SubsetBuild(
                    member_sha256={"a": "cd" * 32},
                    output_sha256="ef" * 32,
                    samples=3,
                    empty=1,
                    built_at=STAMP,
                )
            }
        }
    )
    assert FuseRecord.model_validate(rec2.model_dump(mode="json")) == rec2
    with pytest.raises(ValidationError):
        FuseRecord.model_validate({**rec.model_dump(mode="json"), "extra": 1})
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/fuse/test_schema_recipes.py -o addopts="" -q`
Expected: 收集階段 `ModuleNotFoundError: No module named 'vcp.fuse'`。

- [ ] **Step 3: 寫 schema 與 recipes**

`src/vcp/fuse/__init__.py`：

```python
"""Fusion layer: recipes in git, fusers in a registry, fused predictions as ordinary runs."""
```

`src/vcp/fuse/schema.py`：

```python
"""Pydantic models of the fusion layer: a recipe (git) and the record of a build (runs/)."""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Member(_Strict):
    """One run in a recipe and its weight. Order inside ``Recipe.members`` is significant."""

    run: str = Field(min_length=1)
    weight: float = 1.0

    @field_validator("weight")
    @classmethod
    def _finite_positive(cls, v: float) -> float:
        # A zero or negative weight silently removes or inverts a member; nan/inf would poison
        # every score-weighted average downstream.
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"member weight must be a finite positive number, got {v!r}")
        return v


class Recipe(_Strict):
    """``configs/datasets/<name>/fuse/<recipe_id>.yaml`` (spec 4.1). ``params`` are the fuser's
    EFFECTIVE params (defaults filled in), all strings, like a pre-registration's."""

    recipe_id: str
    dataset: str
    plan_id: str
    method: str
    params: dict[str, str] = Field(default_factory=dict)
    members: list[Member] = Field(min_length=1)
    notes: str = ""
    created_at: str

    @model_validator(mode="after")
    def _unique_members(self) -> Recipe:
        runs = [m.run for m in self.members]
        dupes = sorted({r for r in runs if runs.count(r) > 1})
        if dupes:
            raise ValueError(f"duplicate members {dupes}")
        return self


class MemberRecord(_Strict):
    run: str
    weight: float
    trained_on: list[str]


class SubsetBuild(_Strict):
    """One subset of one build: which member bytes went in, which bytes came out."""

    member_sha256: dict[str, str]
    output_sha256: str
    samples: int
    empty: int
    built_at: str


class FuseRecord(_Strict):
    """``runs/<run_id>/fuse.json`` (spec 4.2): the bit-level provenance of a fused run."""

    run_id: str
    recipe_id: str
    recipe_sha256: str
    method: str
    method_version: str
    params: dict[str, str]
    members: list[MemberRecord]
    subsets: dict[str, SubsetBuild] = Field(default_factory=dict)
    vcp_version: str
```

`src/vcp/fuse/recipes.py`：

```python
"""Recipe files under ``configs/datasets/<name>/fuse/``: written once, never rewritten.

A recipe is git-tracked configuration like a split plan or a pre-registration, and follows the
same rule: a changed recipe is a new id. ``save_recipe`` refuses an existing file, and nothing
in this package ever opens one for writing again.
"""

from __future__ import annotations

from pathlib import Path

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, validate_name
from vcp.fuse.schema import Recipe

RECIPE_EXISTS = "recipe_exists"


def fuse_dir(paths: DatasetPaths) -> Path:
    return paths.config_dir / "fuse"


def recipe_path(paths: DatasetPaths, recipe_id: str) -> Path:
    validate_name(recipe_id)
    return fuse_dir(paths) / f"{recipe_id}.yaml"


def load_recipe(paths: DatasetPaths, recipe_id: str) -> Recipe:
    path = recipe_path(paths, recipe_id)
    if not path.is_file():
        raise ValidationFailed(f"recipe not found: {path}", fields={"recipe": recipe_id})
    recipe = load_yaml_model(path, Recipe)
    if recipe.recipe_id != recipe_id:
        raise ValidationFailed(
            f"recipe file names {recipe.recipe_id!r}, not {recipe_id!r}", location=str(path)
        )
    if recipe.dataset != paths.name:
        raise ValidationFailed(
            f"recipe {recipe_id!r} belongs to dataset {recipe.dataset!r}, not {paths.name!r}",
            location=str(path),
        )
    return recipe


def save_recipe(paths: DatasetPaths, recipe: Recipe) -> Path:
    """Write a recipe that is not there yet. An existing file is never rewritten (spec 4.1)."""
    path = recipe_path(paths, recipe.recipe_id)
    if path.exists():
        raise ValidationFailed(
            f"{RECIPE_EXISTS}: recipe {recipe.recipe_id!r} already exists: {path}; "
            "a changed recipe is a new id",
            fields={"recipe": recipe.recipe_id},
        )
    dump_yaml_model(recipe, path)
    return path


def recipe_sha(paths: DatasetPaths, recipe_id: str) -> str:
    """The file's sha256: what a fused run's ``source.config_hash`` binds to."""
    return sha256_file(recipe_path(paths, recipe_id))


def same_recipe(a: Recipe, b: Recipe) -> bool:
    """Equal in everything that decides an output -- not ``notes``, not ``created_at``."""

    def key(r: Recipe) -> tuple:
        return (r.dataset, r.plan_id, r.method, r.params, [(m.run, m.weight) for m in r.members])

    return key(a) == key(b)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/fuse/test_schema_recipes.py -o addopts="" -q`
Expected: 全部 PASS。`uv run ruff check src/vcp/fuse tests/unit/fuse && uv run ruff format --check src/vcp/fuse tests/unit/fuse` 乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/fuse/__init__.py src/vcp/fuse/schema.py src/vcp/fuse/recipes.py tests/unit/fuse/__init__.py tests/unit/fuse/test_schema_recipes.py
git commit -m "feat(fuse): 配方與融合紀錄的資料模型、配方檔寫了不改

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: 融合器協定、登記表、參數 helper（spec §2.1、§7.1）

**Files:**
- Create: `src/vcp/fuse/fusers/__init__.py`
- Create: `src/vcp/fuse/fusers/base.py`
- Test: `tests/unit/fuse/test_fusers_base.py`

**Interfaces:**
- Consumes: `vcp.measure.schema.PAYLOAD_FIELDS / Prediction / payload_field`、`vcp.core.errors.RegistryError / ValidationFailed`、`vcp.data.dataset.Dataset`、`vcp.data.schema.Sample`。
- Produces（`vcp.fuse.fusers` 匯出全部）:
  - `MemberPredictions(run_id: str, weight: float, predictions: dict[str, Prediction])`（frozen dataclass）
  - `FuseContext(dataset: Dataset, subset: str, ids: list[str], samples: dict[str, Sample], params: dict[str, str])`（frozen dataclass）
  - `Fuser` Protocol：`name`、`version`、`payloads: frozenset[str]`、`defaults: dict[str, str]`、`check_params(params) -> None`、`fuse(members, ctx) -> list[Prediction]`
  - `FUSERS: dict[str, Fuser]`、`register_fuser(fuser)`（重複 / payloads 不合法 → `RegistryError`）、`get_fuser(name)`（未知 → `RegistryError`，訊息列出登記表，`fields={"method": name}`）
  - `effective_params(fuser, params) -> dict[str, str]`（未知鍵 → `ValidationFailed`，`fields={"param": key}`）、`resolve_params(fuser, params)` = effective + `check_params`
  - `require_payload(fuser, task) -> str`（task 的 payload 不在 `fuser.payloads` → `ValidationFailed`，`fields={"method", "payload"}`）
  - `float_param(params, key, *, lo=None, hi=None, lo_open=False) -> float`、`int_param(params, key, *, lo=0) -> int`、`choice_param(params, key, choices) -> str`（值不合法 → `ValidationFailed`，`fields={"param": key}`）
  - `vcp.fuse.fusers` 套件在匯入時登記內建融合器（本任務尚無內建；Task 3、4 各加一行）

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/fuse/test_fusers_base.py`：

```python
import pytest

from helpers import det_samples, make_card
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.dataset import Dataset
from vcp.fuse.fusers import (
    FUSERS,
    FuseContext,
    MemberPredictions,
    choice_param,
    effective_params,
    float_param,
    get_fuser,
    int_param,
    register_fuser,
    require_payload,
    resolve_params,
)
from vcp.measure.schema import Prediction


class Echo:
    name = "test_echo"
    version = "1"
    payloads = frozenset({"boxes"})
    defaults = {"k": "1"}

    def check_params(self, params):
        int_param(params, "k", lo=1)

    def fuse(self, members, ctx):
        return [p for sid, p in members[0].predictions.items() if sid in set(ctx.ids)]


@pytest.fixture
def echo(monkeypatch):
    monkeypatch.delitem(FUSERS, "test_echo", raising=False)
    f = Echo()
    register_fuser(f)
    yield f
    FUSERS.pop("test_echo", None)


def test_register_refuses_duplicate_and_bad_payloads(echo):
    with pytest.raises(RegistryError, match="already registered"):
        register_fuser(Echo())

    class Bad(Echo):
        name = "test_bad"
        payloads = frozenset({"boxes", "labels"})

    with pytest.raises(RegistryError, match="labels"):
        register_fuser(Bad())

    class Empty(Echo):
        name = "test_empty"
        payloads = frozenset()

    with pytest.raises(RegistryError, match="non-empty"):
        register_fuser(Empty())


def test_get_unknown_lists_registry(echo):
    with pytest.raises(RegistryError, match="test_echo") as ei:
        get_fuser("nope")
    assert ei.value.fields == {"method": "nope"}
    assert get_fuser("test_echo") is echo


def test_effective_and_resolve_params(echo):
    assert effective_params(echo, {}) == {"k": "1"}
    assert effective_params(echo, {"k": "3"}) == {"k": "3"}
    with pytest.raises(ValidationFailed, match="no params") as ei:
        effective_params(echo, {"zz": "1"})
    assert ei.value.fields == {"param": "zz"}
    assert resolve_params(echo, {"k": "2"}) == {"k": "2"}
    with pytest.raises(ValidationFailed) as ei:
        resolve_params(echo, {"k": "0"})
    assert ei.value.fields == {"param": "k"}


def test_require_payload(echo):
    assert require_payload(echo, "det") == "boxes"
    with pytest.raises(ValidationFailed, match="scores") as ei:
        require_payload(echo, "multilabel")
    assert ei.value.fields == {"method": "test_echo", "payload": "scores"}
    with pytest.raises(ValidationFailed):
        require_payload(echo, "no_such_task")


@pytest.mark.parametrize(
    "value, kw, ok",
    [
        ("0.5", dict(lo=0.0, hi=1.0, lo_open=True), True),
        ("0", dict(lo=0.0, hi=1.0, lo_open=True), False),
        ("0", dict(lo=0.0), True),
        ("1", dict(lo=0.0, hi=1.0), True),
        ("1.5", dict(lo=0.0, hi=1.0), False),
        ("nan", dict(lo=0.0), False),
        ("inf", dict(lo=0.0), False),
        ("abc", dict(), False),
    ],
)
def test_float_param(value, kw, ok):
    if ok:
        assert float_param({"x": value}, "x", **kw) == float(value)
    else:
        with pytest.raises(ValidationFailed) as ei:
            float_param({"x": value}, "x", **kw)
        assert ei.value.fields == {"param": "x"}


def test_int_and_choice_param():
    assert int_param({"n": "3"}, "n") == 3
    assert int_param({"n": "0"}, "n", lo=0) == 0
    for bad in ("-1", "1.5", "x"):
        with pytest.raises(ValidationFailed) as ei:
            int_param({"n": bad}, "n", lo=0)
        assert ei.value.fields == {"param": "n"}
    assert choice_param({"c": "avg"}, "c", ("avg", "max")) == "avg"
    with pytest.raises(ValidationFailed, match="avg") as ei:
        choice_param({"c": "sum"}, "c", ("avg", "max"))
    assert ei.value.fields == {"param": "c"}


def test_context_and_member_predictions_are_frozen(echo):
    samples = det_samples(2, seed=0)
    ds = Dataset.from_parts(make_card("det"), samples)
    ctx = FuseContext(
        dataset=ds,
        subset="valA",
        ids=[s.sample_id for s in samples],
        samples={s.sample_id: s for s in samples},
        params={"k": "1"},
    )
    m = MemberPredictions(
        run_id="a", weight=1.0, predictions={"s0000": Prediction(sample_id="s0000", boxes=[])}
    )
    with pytest.raises(AttributeError):
        ctx.subset = "valB"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        m.weight = 2.0  # type: ignore[misc]
    assert [p.sample_id for p in echo.fuse([m], ctx)] == ["s0000"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/fuse/test_fusers_base.py -o addopts="" -q`
Expected: `ModuleNotFoundError: No module named 'vcp.fuse.fusers'`。

- [ ] **Step 3: 寫協定與登記表**

`src/vcp/fuse/fusers/base.py`：

```python
"""Fuser protocol and registry (spec 7.1): one axis, the same shape as the metric registry.

A fuser declares which prediction payloads it can fuse; the dataset's task decides which payload
its predictions carry (``TaskSpec.pred_payload``), so applicability is derived from two
registries and never from a hand-written table. Params arrive as strings (a recipe is yaml in
git); the typed helpers below turn them into numbers with a located, machine-readable failure.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample
from vcp.measure.schema import PAYLOAD_FIELDS, Prediction, payload_field


@dataclass(frozen=True)
class MemberPredictions:
    """One member's predictions for one subset, keyed by sample id."""

    run_id: str
    weight: float
    predictions: dict[str, Prediction]


@dataclass(frozen=True)
class FuseContext:
    """What every fuser call shares: the subset's ids in dataset order, the samples (for view
    sizes), and the recipe's effective params. Never labels -- a fuser does not read gold."""

    dataset: Dataset
    subset: str
    ids: list[str]
    samples: dict[str, Sample]
    params: dict[str, str]


class Fuser(Protocol):
    name: str
    version: str
    payloads: frozenset[str]
    defaults: dict[str, str]

    def check_params(self, params: dict[str, str]) -> None:
        """Raise ``ValidationFailed`` for a value this fuser cannot use."""
        ...

    def fuse(self, members: list[MemberPredictions], ctx: FuseContext) -> list[Prediction]: ...


FUSERS: dict[str, Fuser] = {}


def register_fuser(fuser: Fuser) -> None:
    if fuser.name in FUSERS:
        raise RegistryError(f"fuser {fuser.name!r} already registered")
    bad = sorted(set(fuser.payloads) - set(PAYLOAD_FIELDS))
    if bad or not fuser.payloads:
        raise RegistryError(
            f"fuser {fuser.name!r} declares payloads {sorted(fuser.payloads)}; "
            f"must be a non-empty subset of {PAYLOAD_FIELDS}"
        )
    FUSERS[fuser.name] = fuser


def get_fuser(name: str) -> Fuser:
    try:
        return FUSERS[name]
    except KeyError:
        raise RegistryError(
            f"unknown fuser {name!r}; known: {sorted(FUSERS)}", fields={"method": name}
        ) from None


def effective_params(fuser: Fuser, params: dict[str, str]) -> dict[str, str]:
    """Fuser defaults overridden by the given params; an unknown key is the user's mistake."""
    unknown = sorted(set(params) - set(fuser.defaults))
    if unknown:
        raise ValidationFailed(
            f"fuser {fuser.name!r} has no params {unknown}; known: {sorted(fuser.defaults)}",
            fields={"param": unknown[0]},
        )
    return {**fuser.defaults, **params}


def resolve_params(fuser: Fuser, params: dict[str, str]) -> dict[str, str]:
    """Effective params that the fuser has also agreed it can parse."""
    effective = effective_params(fuser, params)
    fuser.check_params(effective)
    return effective


def require_payload(fuser: Fuser, task: str) -> str:
    """The payload field this task's predictions carry, if the fuser can fuse it."""
    field = payload_field(task)
    if field not in fuser.payloads:
        raise ValidationFailed(
            f"fuser {fuser.name!r} fuses {sorted(fuser.payloads)} predictions; "
            f"task {task!r} predicts {field!r}",
            fields={"method": fuser.name, "payload": field},
        )
    return field


def _bad(key: str, value: str, what: str) -> ValidationFailed:
    return ValidationFailed(f"param {key}={value!r}: {what}", fields={"param": key})


def float_param(
    params: dict[str, str],
    key: str,
    *,
    lo: float | None = None,
    hi: float | None = None,
    lo_open: bool = False,
) -> float:
    raw = params[key]
    try:
        value = float(raw)
    except ValueError:
        raise _bad(key, raw, "not a number") from None
    if not math.isfinite(value):
        raise _bad(key, raw, "must be finite")
    if lo is not None and (value <= lo if lo_open else value < lo):
        raise _bad(key, raw, f"must be {'>' if lo_open else '>='} {lo}")
    if hi is not None and value > hi:
        raise _bad(key, raw, f"must be <= {hi}")
    return value


def int_param(params: dict[str, str], key: str, *, lo: int = 0) -> int:
    raw = params[key]
    try:
        value = int(raw)
    except ValueError:
        raise _bad(key, raw, "not an integer") from None
    if value < lo:
        raise _bad(key, raw, f"must be >= {lo}")
    return value


def choice_param(params: dict[str, str], key: str, choices: tuple[str, ...]) -> str:
    raw = params[key]
    if raw not in choices:
        raise _bad(key, raw, f"must be one of {list(choices)}")
    return raw
```

`src/vcp/fuse/fusers/__init__.py`：

```python
"""Fuser registry. Importing this package registers the built-in fusers in a fixed order."""

from vcp.fuse.fusers.base import (
    FUSERS,
    FuseContext,
    Fuser,
    MemberPredictions,
    choice_param,
    effective_params,
    float_param,
    get_fuser,
    int_param,
    register_fuser,
    require_payload,
    resolve_params,
)

__all__ = [
    "FUSERS",
    "FuseContext",
    "Fuser",
    "MemberPredictions",
    "choice_param",
    "effective_params",
    "float_param",
    "get_fuser",
    "int_param",
    "register_fuser",
    "require_payload",
    "resolve_params",
]
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/fuse -o addopts="" -q`
Expected: 全部 PASS；ruff check / format 乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/fuse/fusers/__init__.py src/vcp/fuse/fusers/base.py tests/unit/fuse/test_fusers_base.py
git commit -m "feat(fuse): 融合器協定、FUSERS 登記表、參數解析 helper、payload 適用性

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `wbf` 融合器（spec §7.2、§7.5）

**Files:**
- Create: `src/vcp/fuse/fusers/wbf.py`
- Modify: `src/vcp/fuse/fusers/__init__.py`（登記 `Wbf()`）
- Test: `tests/unit/fuse/test_wbf.py`

**Interfaces:**
- Consumes: Task 2 的 `FuseContext`、`MemberPredictions`、`float_param`、`int_param`、`choice_param`、`register_fuser`；`vcp.measure.schema.PredBox / Prediction`。
- Produces: `Wbf`（`name="wbf"`、`version="1"`、`payloads={"boxes"}`、`defaults={"iou": "0.55", "skip": "0", "min_score": "0", "max_per_image": "0", "conf_type": "avg"}`）、`CONF_TYPES = ("avg", "max")`；`get_fuser("wbf")` 可用。

演算法（每個 sample、每個 (view, category_id) 獨立；spec §7.2 的七步）：
1. `score < skip` 的輸入框丟棄。
2. q = score × 成員 weight；依 (−q, 全域輸入順序) 穩定排序（輸入順序 = 成員順序 → 成員檔內順序）。
3. 逐框與現有融合框算 IoU（x1y1x2y2），取最大者，若 **>** `iou` 併入該群並重算融合座標 = Σ q·c / Σ q（Σ q = 0 時取算術平均）；否則新群。
4. 群 score：`avg` → mean(q) × min(成員數, 群大小) / Σ weight；`max` → max(q) / max weight；夾到 [0, 1]。成員數 = 配方成員數（含此圖無框者）。
5. 轉回 x, y, w, h；view 的 width 與 height 都已知時裁到 [0, width] × [0, height]。
6. 丟 `score < min_score`。
7. `max_per_image > 0` 時每個 view 依 (−score, 群建立順序) 取前 N；輸出依 view、群建立順序。

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/fuse/test_wbf.py`：

```python
import random

import pytest

from helpers import make_card
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample, View
from vcp.fuse.fusers import FuseContext, MemberPredictions, get_fuser, resolve_params
from vcp.measure.schema import PredBox, Prediction


def _sample(sid: str, size: tuple[int, int] | None = (8, 8)) -> Sample:
    view = View(path=f"{sid}.jpg") if size is None else View(path=f"{sid}.jpg", width=size[0], height=size[1])
    return Sample(sample_id=sid, views=[view], label_source="none")


def _ctx(samples: list[Sample], **params) -> FuseContext:
    ds = Dataset.from_parts(make_card("det"), samples)
    fuser = get_fuser("wbf")
    return FuseContext(
        dataset=ds,
        subset="valA",
        ids=[s.sample_id for s in samples],
        samples={s.sample_id: s for s in samples},
        params=resolve_params(fuser, {k: str(v) for k, v in params.items()}),
    )


def _box(x, y, w, h, score, cat=0, view=0) -> PredBox:
    return PredBox(x=x, y=y, w=w, h=h, category_id=cat, score=score, view=view)


def _member(run: str, weight: float, boxes: dict[str, list[PredBox]]) -> MemberPredictions:
    return MemberPredictions(
        run_id=run,
        weight=weight,
        predictions={sid: Prediction(sample_id=sid, boxes=bs) for sid, bs in boxes.items()},
    )


def _fuse(members, ctx):
    out = get_fuser("wbf").fuse(members, ctx)
    return {p.sample_id: p.boxes for p in out}


def test_two_members_merge_overlapping_boxes_avg_and_max():
    s = [_sample("s1")]
    m1 = _member("a", 1.0, {"s1": [_box(0, 0, 10, 10, 0.8)]})
    m2 = _member("b", 1.0, {"s1": [_box(2, 0, 10, 10, 0.6)]})
    (b,) = _fuse([m1, m2], _ctx(s, iou=0.55))["s1"]
    # IoU = 80 / 120 = 0.667 > 0.55 -> one cluster; corners = score-weighted mean
    assert b.x == pytest.approx(1.2 / 1.4) and b.y == 0.0
    assert b.w == pytest.approx(10.0) and b.h == pytest.approx(10.0)
    assert b.score == pytest.approx(0.7)  # mean(0.8, 0.6) * min(2, 2) / 2
    (b,) = _fuse([m1, m2], _ctx(s, conf_type="max"))["s1"]
    assert b.score == pytest.approx(0.8)


def test_member_weight_scales_scores_and_coordinates():
    s = [_sample("s1")]
    m1 = _member("a", 1.0, {"s1": [_box(0, 0, 10, 10, 0.8)]})
    m2 = _member("b", 0.5, {"s1": [_box(2, 0, 10, 10, 0.6)]})
    (b,) = _fuse([m1, m2], _ctx(s))["s1"]
    assert b.x == pytest.approx(0.6 / 1.1)
    assert b.x + b.w == pytest.approx(11.6 / 1.1)
    assert b.score == pytest.approx(0.55 * 2 / 1.5)  # mean(0.8, 0.3) * min(2, 2) / 1.5
    (b,) = _fuse([m1, m2], _ctx(s, conf_type="max"))["s1"]
    assert b.score == pytest.approx(0.8 / 1.0)


def test_non_overlapping_boxes_stay_apart_and_are_penalised():
    s = [_sample("s1")]
    m1 = _member("a", 1.0, {"s1": [_box(0, 0, 4, 4, 0.8)]})
    m2 = _member("b", 1.0, {"s1": [_box(10, 10, 4, 4, 0.6)]})
    boxes = _fuse([m1, m2], _ctx(s, iou=0.55))["s1"]
    assert [(b.x, b.score) for b in boxes] == [(0.0, pytest.approx(0.4)), (10.0, pytest.approx(0.3))]


def test_categories_and_views_are_never_merged():
    s = [Sample(sample_id="s1", views=[View(path="a.jpg"), View(path="b.jpg")], label_source="none")]
    m1 = _member("a", 1.0, {"s1": [_box(0, 0, 10, 10, 0.8, cat=0)]})
    m2 = _member("b", 1.0, {"s1": [_box(0, 0, 10, 10, 0.8, cat=1)]})
    boxes = _fuse([m1, m2], _ctx(s))["s1"]
    assert sorted((b.category_id, b.score) for b in boxes) == [(0, pytest.approx(0.4)), (1, pytest.approx(0.4))]
    m3 = _member("c", 1.0, {"s1": [_box(0, 0, 10, 10, 0.8, view=1)]})
    boxes = _fuse([m1, m3], _ctx(s))["s1"]
    assert [b.view for b in boxes] == [0, 1] and all(b.score == pytest.approx(0.4) for b in boxes)


def test_single_member_self_merges_and_score_is_clamped():
    s = [_sample("s1")]
    m1 = _member("a", 1.0, {"s1": [_box(0, 0, 10, 10, 1.0), _box(0, 0, 10, 10, 1.0)]})
    m2 = _member("b", 0.5, {"s1": []})
    (b,) = _fuse([m1, m2], _ctx(s))["s1"]
    assert (b.x, b.y, b.w, b.h) == (0.0, 0.0, 10.0, 10.0)
    assert b.score == 1.0  # mean(1, 1) * min(2, 2) / 1.5 = 1.33 -> clamped
    (b,) = _fuse([m1], _ctx(s))["s1"]
    assert b.score == pytest.approx(1.0)  # one member: mean(1, 1) * min(1, 2) / 1


def test_skip_min_score_and_max_per_image():
    s = [_sample("s1")]
    low = _member("a", 1.0, {"s1": [_box(0, 0, 4, 4, 0.1)]})
    assert _fuse([low], _ctx(s, skip=0.2)) == {}  # dropped before clustering -> no row at all
    m1 = _member("a", 1.0, {"s1": [_box(0, 0, 4, 4, 0.8)]})
    m2 = _member("b", 1.0, {"s1": [_box(10, 10, 4, 4, 0.6)]})
    boxes = _fuse([m1, m2], _ctx(s, min_score=0.35))["s1"]
    assert [b.score for b in boxes] == [pytest.approx(0.4)]
    boxes = _fuse([m1, m2], _ctx(s, max_per_image=1))["s1"]
    assert [b.x for b in boxes] == [0.0]
    assert _fuse([m1, m2], _ctx(s, min_score=0.5)) == {}


def test_clipping_only_when_view_size_is_known():
    m = _member("a", 1.0, {"s1": [_box(6, 6, 5, 5, 0.9)]})
    (b,) = _fuse([m], _ctx([_sample("s1", (8, 8))]))["s1"]
    assert (b.x, b.y, b.w, b.h) == (6.0, 6.0, 2.0, 2.0)
    (b,) = _fuse([m], _ctx([_sample("s1", None)]))["s1"]
    assert (b.x, b.y, b.w, b.h) == (6.0, 6.0, 5.0, 5.0)


def test_missing_prediction_means_no_boxes():
    s = [_sample("s1"), _sample("s2")]
    m1 = _member("a", 1.0, {"s1": [_box(0, 0, 4, 4, 0.8)]})
    m2 = _member("b", 1.0, {})
    out = _fuse([m1, m2], _ctx(s))
    assert list(out) == ["s1"] and out["s1"][0].score == pytest.approx(0.4)


def test_deterministic_and_member_order_invariant_without_ties():
    rng = random.Random(3)
    s = [_sample(f"s{i}", (64, 64)) for i in range(5)]

    def member(run, weight, seed):
        r = random.Random(seed)
        return _member(
            run,
            weight,
            {
                x.sample_id: [
                    _box(r.uniform(0, 50), r.uniform(0, 50), r.uniform(2, 12), r.uniform(2, 12), r.uniform(0.05, 0.99), cat=r.choice([0, 1]))
                    for _ in range(r.randint(0, 6))
                ]
                for x in s
            },
        )

    members = [member("a", 1.0, 1), member("b", 0.5, 2), member("c", 0.7, 3)]
    ctx = _ctx(s, iou=0.5)
    first = [p.model_dump() for p in get_fuser("wbf").fuse(members, ctx)]
    again = [p.model_dump() for p in get_fuser("wbf").fuse(members, ctx)]
    assert first == again
    shuffled = members[:]
    rng.shuffle(shuffled)
    permuted = [p.model_dump() for p in get_fuser("wbf").fuse(shuffled, ctx)]

    def key(p):
        boxes = sorted((round(b["score"], 9), b["category_id"], round(b["x"], 6)) for b in p["boxes"])
        return (p["sample_id"], boxes)

    assert sorted(first, key=key) == sorted(permuted, key=key)


def test_bad_params_fail_with_param_field():
    s = [_sample("s1")]
    for params in ({"iou": "0"}, {"iou": "1.5"}, {"skip": "-1"}, {"max_per_image": "1.5"}, {"conf_type": "sum"}):
        with pytest.raises(ValidationFailed) as ei:
            _ctx(s, **params)
        assert ei.value.fields == {"param": next(iter(params))}


def test_matches_ensemble_boxes_when_installed():
    eb = pytest.importorskip("ensemble_boxes")
    size = 100.0
    for seed in range(5):
        r = random.Random(seed)
        n_models = r.randint(1, 4)
        weights = [r.choice([1.0, 0.5, 0.7]) for _ in range(n_models)]
        boxes_list, scores_list, labels_list, members = [], [], [], []
        for m in range(n_models):
            k = r.randint(0, 8)
            raw = [(r.uniform(0, 70), r.uniform(0, 70), r.uniform(2, 25), r.uniform(2, 25), r.uniform(0.05, 0.99), r.choice([0, 1])) for _ in range(k)]
            boxes_list.append([[x / size, y / size, (x + w) / size, (y + h) / size] for x, y, w, h, _, _ in raw])
            scores_list.append([sc for *_, sc, _ in raw])
            labels_list.append([c for *_, c in raw])
            members.append(_member(f"m{m}", weights[m], {"s1": [_box(x, y, w, h, sc, cat=c) for x, y, w, h, sc, c in raw]}))
        eb_boxes, eb_scores, eb_labels = eb.weighted_boxes_fusion(
            boxes_list, scores_list, labels_list, weights=weights, iou_thr=0.5, skip_box_thr=0.0, conf_type="avg", allows_overflow=False
        )
        expected = sorted((int(lab), round(float(sc), 6), round(float(b[0]) * size, 4), round(float(b[1]) * size, 4)) for b, sc, lab in zip(eb_boxes, eb_scores, eb_labels, strict=True))
        ours = _fuse(members, _ctx([_sample("s1", (100, 100))], iou=0.5)).get("s1", [])
        got = sorted((b.category_id, round(min(1.0, b.score), 6), round(b.x, 4), round(b.y, 4)) for b in ours)
        assert got == expected, seed
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/fuse/test_wbf.py -o addopts="" -q`
Expected: 每個測試在 `get_fuser("wbf")` 拋 `RegistryError: unknown fuser 'wbf'`（oracle 測試在沒裝 ensemble-boxes 時 skip）。

- [ ] **Step 3: 寫 `wbf.py` 並登記**

`src/vcp/fuse/fusers/wbf.py`：

```python
"""``wbf``: weighted boxes fusion in pixel space (spec 7.2).

The semantics are ensemble-boxes' ``weighted_boxes_fusion(..., allows_overflow=False)`` -- what
every detection recipe means by "WBF" -- with four deliberate differences: coordinates stay in
pixels (IoU and a score-weighted mean are scale-invariant, so no image size is needed);
clustering runs per (view, category) instead of per label only; a box is clipped to its view
only when the view's size is known; and a fused score above 1.0 is clamped to 1.0 (the library
lets one model's overlapping boxes overflow; ``PredBox`` would reject that). Every ordering is
stable and every tie breaks by member order then file order, so the same members always fuse
to the same bytes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from vcp.data.schema import Sample
from vcp.fuse.fusers.base import (
    FuseContext,
    MemberPredictions,
    choice_param,
    float_param,
    int_param,
)
from vcp.measure.schema import PredBox, Prediction

CONF_TYPES = ("avg", "max")

Cluster = list[tuple[float, np.ndarray]]  # (weighted score, corners x1 y1 x2 y2)


@dataclass(frozen=True)
class _Params:
    iou: float
    skip: float
    min_score: float
    max_per_image: int
    conf_type: str


def _parse(params: dict[str, str]) -> _Params:
    return _Params(
        iou=float_param(params, "iou", lo=0.0, hi=1.0, lo_open=True),
        skip=float_param(params, "skip", lo=0.0),
        min_score=float_param(params, "min_score", lo=0.0),
        max_per_image=int_param(params, "max_per_image", lo=0),
        conf_type=choice_param(params, "conf_type", CONF_TYPES),
    )


def _iou_with(fused: np.ndarray, box: np.ndarray) -> np.ndarray:
    """IoU of ``box`` against each row of ``fused`` (x1, y1, x2, y2). A zero-area union is 0."""
    xa = np.maximum(fused[:, 0], box[0])
    ya = np.maximum(fused[:, 1], box[1])
    xb = np.minimum(fused[:, 2], box[2])
    yb = np.minimum(fused[:, 3], box[3])
    inter = np.clip(xb - xa, 0.0, None) * np.clip(yb - ya, 0.0, None)
    area_f = (fused[:, 2] - fused[:, 0]) * (fused[:, 3] - fused[:, 1])
    area_b = (box[2] - box[0]) * (box[3] - box[1])
    union = area_f + area_b - inter
    safe = np.where(union > 0, union, 1.0)
    return np.where(union > 0, inter / safe, 0.0)


def _weighted_corners(cluster: Cluster) -> np.ndarray:
    total = sum(q for q, _ in cluster)
    if total <= 0:
        # Every member scored these boxes 0 (legal when skip=0): a plain mean, not 0/0.
        return np.mean([c for _, c in cluster], axis=0)
    return sum(q * c for q, c in cluster) / total


def _fuse_group(
    items: Cluster, *, p: _Params, n_members: int, w_sum: float, w_max: float
) -> list[tuple[float, np.ndarray]]:
    """Cluster the boxes of one (view, category) -- already sorted by weighted score, descending
    -- and return (score, corners) per cluster in creation order (spec 7.2 steps 3-4)."""
    clusters: list[Cluster] = []
    fused: list[np.ndarray] = []
    for q, corners in items:
        idx = -1
        if fused:
            ious = _iou_with(np.stack(fused), corners)
            best = int(np.argmax(ious))
            if ious[best] > p.iou:
                idx = best
        if idx < 0:
            clusters.append([(q, corners)])
            fused.append(corners.copy())
        else:
            clusters[idx].append((q, corners))
            fused[idx] = _weighted_corners(clusters[idx])
    out: list[tuple[float, np.ndarray]] = []
    for members, corners in zip(clusters, fused, strict=True):
        qs = [q for q, _ in members]
        if p.conf_type == "max":
            score = max(qs) / w_max
        else:
            score = (sum(qs) / len(qs)) * min(n_members, len(qs)) / w_sum
        out.append((min(1.0, score), corners))
    return out


def _clip(corners: np.ndarray, sample: Sample, view: int) -> np.ndarray:
    if view >= len(sample.views):
        return corners
    v = sample.views[view]
    if v.width is None or v.height is None:
        return corners
    return np.array(
        [
            min(max(corners[0], 0.0), float(v.width)),
            min(max(corners[1], 0.0), float(v.height)),
            min(max(corners[2], 0.0), float(v.width)),
            min(max(corners[3], 0.0), float(v.height)),
        ]
    )


class Wbf:
    name = "wbf"
    version = "1"
    payloads = frozenset({"boxes"})
    defaults = {
        "iou": "0.55",
        "skip": "0",
        "min_score": "0",
        "max_per_image": "0",
        "conf_type": "avg",
    }

    def check_params(self, params: dict[str, str]) -> None:
        _parse(params)

    def fuse(self, members: list[MemberPredictions], ctx: FuseContext) -> list[Prediction]:
        p = _parse(ctx.params)
        n_members = len(members)
        w_sum = sum(m.weight for m in members)
        w_max = max(m.weight for m in members)
        out: list[Prediction] = []
        for sid in ctx.ids:
            boxes = self._fuse_sample(sid, members, ctx, p, n_members, w_sum, w_max)
            if boxes:
                out.append(Prediction(sample_id=sid, boxes=boxes))
        return out

    @staticmethod
    def _fuse_sample(
        sid: str,
        members: list[MemberPredictions],
        ctx: FuseContext,
        p: _Params,
        n_members: int,
        w_sum: float,
        w_max: float,
    ) -> list[PredBox]:
        # Step 1-2: collect per (view, category), weighted score, global input order for ties.
        groups: dict[tuple[int, int], list[tuple[float, np.ndarray, int]]] = {}
        order = 0
        for m in members:
            pred = m.predictions.get(sid)
            boxes_in = pred.boxes if pred is not None and pred.boxes else []
            for b in boxes_in:
                if b.score < p.skip:
                    continue
                corners = np.array([b.x, b.y, b.x + b.w, b.y + b.h], dtype=float)
                groups.setdefault((b.view, b.category_id), []).append(
                    (b.score * m.weight, corners, order)
                )
                order += 1
        # Step 3-6: cluster each group; creation order is the tie-break for step 7.
        per_view: dict[int, list[tuple[float, np.ndarray, int, int]]] = {}
        created = 0
        for (view, category), items in sorted(groups.items()):
            items.sort(key=lambda t: (-t[0], t[2]))
            fused = _fuse_group(
                [(q, c) for q, c, _ in items], p=p, n_members=n_members, w_sum=w_sum, w_max=w_max
            )
            for score, corners in fused:
                if score < p.min_score:
                    continue
                per_view.setdefault(view, []).append((score, corners, category, created))
                created += 1
        # Step 7: top-N per view, then back to creation order.
        boxes: list[PredBox] = []
        for view in sorted(per_view):
            items = sorted(per_view[view], key=lambda t: (-t[0], t[3]))
            if p.max_per_image:
                items = items[: p.max_per_image]
            for score, corners, category, _ in sorted(items, key=lambda t: t[3]):
                x1, y1, x2, y2 = _clip(corners, ctx.samples[sid], view)
                boxes.append(
                    PredBox(
                        x=float(x1),
                        y=float(y1),
                        w=float(x2 - x1),
                        h=float(y2 - y1),
                        category_id=category,
                        score=float(score),
                        view=view,
                    )
                )
        return boxes
```

`src/vcp/fuse/fusers/__init__.py` 在既有 import 後加：

```python
from vcp.fuse.fusers.wbf import Wbf

for _fuser in (Wbf(),):
    register_fuser(_fuser)
```

（`__all__` 不變；`Wbf` 不需要匯出。）

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/fuse -o addopts="" -q`
Expected: 全部 PASS，oracle 測試 SKIPPED（沒裝 ensemble-boxes）。若想真的跑 oracle 一次：`uv run --with ensemble-boxes pytest tests/unit/fuse/test_wbf.py::test_matches_ensemble_boxes_when_installed -o addopts="" -q`（臨時環境，不進 lockfile）；若 `uv run --with` 在此機器裝不起 numba 依賴就作罷，spec 只要求可選。ruff check / format 乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/fuse/fusers/wbf.py src/vcp/fuse/fusers/__init__.py tests/unit/fuse/test_wbf.py
git commit -m "feat(fuse): wbf 融合器——像素座標的 weighted boxes fusion，語意對齊 ensemble-boxes

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `mean` 與 `rank_mean` 融合器（spec §7.3、§7.4、§7.5）

**Files:**
- Create: `src/vcp/fuse/fusers/scores.py`
- Modify: `src/vcp/fuse/fusers/__init__.py`（登記 `Mean()`、`RankMean()`）
- Test: `tests/unit/fuse/test_scores.py`

**Interfaces:**
- Consumes: Task 2 的 `FuseContext`、`MemberPredictions`；`vcp.measure.schema.payload_field`。
- Produces: `Mean`（`name="mean"`、`payloads={"scores", "targets"}`、`defaults={}`）、`RankMean`（`name="rank_mean"`、`payloads={"scores"}`）、`normalised_ranks(values: list[float]) -> list[float]`（同值取平均名次，正規化為 (rank − 1) / (n − 1)，n = 1 → 0.5）；缺 sample / key 不齊 → `ValidationFailed`，`fields={"member", "sample"}`。

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/fuse/test_scores.py`：

```python
import pytest

from helpers import ML_CATS, REG_CATS, make_card, multilabel_samples, regression_samples
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.fuse.fusers import FuseContext, MemberPredictions, get_fuser, require_payload
from vcp.fuse.fusers.scores import normalised_ranks
from vcp.measure.schema import Prediction


def _ctx(task: str, samples, cats) -> FuseContext:
    ds = Dataset.from_parts(make_card(task, categories=cats), samples)
    return FuseContext(
        dataset=ds,
        subset="valA",
        ids=[s.sample_id for s in samples],
        samples={s.sample_id: s for s in samples},
        params={},
    )


def _scores_member(run, weight, rows: dict[str, dict[str, float]]) -> MemberPredictions:
    return MemberPredictions(
        run_id=run,
        weight=weight,
        predictions={sid: Prediction(sample_id=sid, scores=v) for sid, v in rows.items()},
    )


def test_mean_is_weighted_per_key():
    samples = multilabel_samples(2, seed=0)
    ids = [s.sample_id for s in samples]
    a = _scores_member("a", 1.0, {ids[0]: {"acl": 0.2, "mcl": 1.0, "effusion": 0.0}, ids[1]: {"acl": 0.5, "mcl": 0.5, "effusion": 0.5}})
    b = _scores_member("b", 3.0, {ids[0]: {"acl": 0.8, "mcl": 0.0, "effusion": 0.0}, ids[1]: {"acl": 0.5, "mcl": 0.5, "effusion": 0.5}})
    out = get_fuser("mean").fuse([a, b], _ctx("multilabel", samples, ML_CATS))
    assert [p.sample_id for p in out] == ids
    assert out[0].scores == pytest.approx({"acl": 0.65, "mcl": 0.25, "effusion": 0.0})
    assert out[1].scores == pytest.approx({"acl": 0.5, "mcl": 0.5, "effusion": 0.5})


def test_mean_fuses_targets_for_regression():
    samples = regression_samples(2, seed=0)
    ids = [s.sample_id for s in samples]
    a = MemberPredictions("a", 1.0, {i: Prediction(sample_id=i, targets={"age": 40.0}) for i in ids})
    b = MemberPredictions("b", 1.0, {i: Prediction(sample_id=i, targets={"age": 50.0}) for i in ids})
    out = get_fuser("mean").fuse([a, b], _ctx("regression", samples, REG_CATS))
    assert all(p.targets == {"age": 45.0} and p.scores is None for p in out)


def test_missing_sample_or_mismatched_keys_fail_located():
    samples = multilabel_samples(2, seed=0)
    ids = [s.sample_id for s in samples]
    full = {"acl": 0.1, "mcl": 0.2, "effusion": 0.3}
    a = _scores_member("a", 1.0, {ids[0]: full, ids[1]: full})
    b = _scores_member("b", 1.0, {ids[0]: full})
    with pytest.raises(ValidationFailed, match="no prediction") as ei:
        get_fuser("mean").fuse([a, b], _ctx("multilabel", samples, ML_CATS))
    assert ei.value.fields == {"member": "b", "sample": ids[1]}
    c = _scores_member("c", 1.0, {ids[0]: full, ids[1]: {"acl": 0.1, "mcl": 0.2}})
    with pytest.raises(ValidationFailed, match="keys") as ei:
        get_fuser("mean").fuse([a, c], _ctx("multilabel", samples, ML_CATS))
    assert ei.value.fields == {"member": "c", "sample": ids[1]}


def test_normalised_ranks():
    assert normalised_ranks([0.1, 0.5, 0.9]) == [0.0, 0.5, 1.0]
    assert normalised_ranks([0.9, 0.9, 0.1]) == [0.75, 0.75, 0.0]  # tie -> average rank 2.5
    assert normalised_ranks([0.3]) == [0.5]
    assert normalised_ranks([2.0, 2.0]) == [0.5, 0.5]


def test_rank_mean_uses_subset_as_population():
    samples = multilabel_samples(3, seed=0)
    ids = [s.sample_id for s in samples]
    zero = {"mcl": 0.0, "effusion": 0.0}
    a = _scores_member("a", 1.0, {ids[0]: {"acl": 0.1, **zero}, ids[1]: {"acl": 0.5, **zero}, ids[2]: {"acl": 0.9, **zero}})
    b = _scores_member("b", 1.0, {ids[0]: {"acl": 0.9, **zero}, ids[1]: {"acl": 0.9, **zero}, ids[2]: {"acl": 0.1, **zero}})
    out = get_fuser("rank_mean").fuse([a, b], _ctx("multilabel", samples, ML_CATS))
    assert [p.scores["acl"] for p in out] == pytest.approx([0.375, 0.625, 0.5])
    assert all(p.scores["mcl"] == 0.5 and p.scores["effusion"] == 0.5 for p in out)
    # weights: member a x3 -> (3*0 + 0.75) / 4 for the first sample
    out = get_fuser("rank_mean").fuse([MemberPredictions("a", 3.0, a.predictions), b], _ctx("multilabel", samples, ML_CATS))
    assert out[0].scores["acl"] == pytest.approx(0.75 / 4)


def test_rank_mean_refuses_targets_and_wbf_refuses_scores():
    with pytest.raises(ValidationFailed) as ei:
        require_payload(get_fuser("rank_mean"), "regression")
    assert ei.value.fields == {"method": "rank_mean", "payload": "targets"}
    assert require_payload(get_fuser("mean"), "regression") == "targets"
    assert require_payload(get_fuser("mean"), "cls") == "scores"
    with pytest.raises(ValidationFailed):
        require_payload(get_fuser("wbf"), "multilabel")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/fuse/test_scores.py -o addopts="" -q`
Expected: `ModuleNotFoundError: No module named 'vcp.fuse.fusers.scores'`。

- [ ] **Step 3: 寫 `scores.py` 並登記**

`src/vcp/fuse/fusers/scores.py`：

```python
"""Score-level fusers for mapping payloads (spec 7.3, 7.4).

``mean`` is the weighted arithmetic mean per key -- probabilities stay probabilities. ``rank_mean``
replaces each member's values with normalised ranks over the WHOLE subset before averaging, so
a badly calibrated member cannot dominate; the output is a rank in [0, 1], not a probability,
which is why it is registered for ``scores`` only (an rmse on ranks would be meaningless).
"""

from __future__ import annotations

import numpy as np

from vcp.core.errors import ValidationFailed
from vcp.fuse.fusers.base import FuseContext, MemberPredictions
from vcp.measure.schema import Prediction, payload_field


def _values(member: MemberPredictions, sid: str, field: str) -> dict[str, float]:
    pred = member.predictions.get(sid)
    values = getattr(pred, field) if pred is not None else None
    if values is None:
        raise ValidationFailed(
            f"member {member.run_id!r} has no prediction for sample {sid!r}",
            location=sid,
            fields={"member": member.run_id, "sample": sid},
        )
    return values


def _aligned(
    members: list[MemberPredictions], sid: str, field: str
) -> tuple[list[str], list[dict[str, float]]]:
    """Every member's values for one sample, all over the same keys (spec 7.3)."""
    keys: list[str] | None = None
    rows: list[dict[str, float]] = []
    for m in members:
        values = _values(m, sid, field)
        if keys is None:
            keys = sorted(values)
        elif sorted(values) != keys:
            raise ValidationFailed(
                f"member {m.run_id!r} predicts keys {sorted(values)} for sample {sid!r}; "
                f"the first member predicts {keys}",
                location=sid,
                fields={"member": m.run_id, "sample": sid},
            )
        rows.append(values)
    return keys or [], rows


def normalised_ranks(values: list[float]) -> list[float]:
    """1-based ranks with ties averaged, scaled to [0, 1]; a single value ranks 0.5."""
    n = len(values)
    if n == 1:
        return [0.5]
    arr = np.asarray(values, dtype=float)
    order = np.argsort(arr, kind="stable")
    ranks = np.empty(n, dtype=float)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and arr[order[j + 1]] == arr[order[i]]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return [float(v) for v in (ranks - 1.0) / (n - 1.0)]


class Mean:
    name = "mean"
    version = "1"
    payloads = frozenset({"scores", "targets"})
    defaults: dict[str, str] = {}

    def check_params(self, params: dict[str, str]) -> None:
        return None

    def fuse(self, members: list[MemberPredictions], ctx: FuseContext) -> list[Prediction]:
        field = payload_field(ctx.dataset.card.task)
        w_sum = sum(m.weight for m in members)
        out: list[Prediction] = []
        for sid in ctx.ids:
            keys, rows = _aligned(members, sid, field)
            fused = {
                k: sum(m.weight * row[k] for m, row in zip(members, rows, strict=True)) / w_sum
                for k in keys
            }
            out.append(Prediction(sample_id=sid, **{field: fused}))
        return out


class RankMean:
    name = "rank_mean"
    version = "1"
    payloads = frozenset({"scores"})
    defaults: dict[str, str] = {}

    def check_params(self, params: dict[str, str]) -> None:
        return None

    def fuse(self, members: list[MemberPredictions], ctx: FuseContext) -> list[Prediction]:
        field = payload_field(ctx.dataset.card.task)
        w_sum = sum(m.weight for m in members)
        # rows[sid] = one dict per member; all checked to share one key set.
        rows = {sid: _aligned(members, sid, field) for sid in ctx.ids}
        keys = next(iter(rows.values()))[0] if rows else []
        for sid, (sample_keys, _) in rows.items():
            if sample_keys != keys:
                raise ValidationFailed(
                    f"sample {sid!r} predicts keys {sample_keys}; the subset's first sample "
                    f"predicts {keys}",
                    location=sid,
                    fields={"member": members[0].run_id, "sample": sid},
                )
        fused: dict[str, dict[str, float]] = {sid: {} for sid in ctx.ids}
        for k in keys:
            for m_index, m in enumerate(members):
                ranks = normalised_ranks([rows[sid][1][m_index][k] for sid in ctx.ids])
                for sid, r in zip(ctx.ids, ranks, strict=True):
                    fused[sid][k] = fused[sid].get(k, 0.0) + m.weight * r / w_sum
        return [Prediction(sample_id=sid, **{field: fused[sid]}) for sid in ctx.ids]
```

`src/vcp/fuse/fusers/__init__.py` 的登記段改為：

```python
from vcp.fuse.fusers.scores import Mean, RankMean
from vcp.fuse.fusers.wbf import Wbf

for _fuser in (Wbf(), Mean(), RankMean()):
    register_fuser(_fuser)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/fuse -o addopts="" -q`
Expected: 全部 PASS；ruff check / format 乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/fuse/fusers/scores.py src/vcp/fuse/fusers/__init__.py tests/unit/fuse/test_scores.py
git commit -m "feat(fuse): mean 與 rank_mean 分數融合器

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: 成員驗證、`vcp fuse recipe`、掛上 `fuse_app`（spec §6 recipe 列、§8）

**Files:**
- Create: `src/vcp/fuse/members.py`
- Create: `src/vcp/cli_fuse.py`
- Modify: `src/vcp/cli.py:22`（import）、`src/vcp/cli.py:45`（`add_typer`）
- Test: `tests/unit/fuse/test_members.py`、`tests/unit/test_cli_fuse.py`

**Interfaces:**
- Consumes: Task 1 的 `Member` / `Recipe` / `save_recipe`；Task 2 的 `get_fuser` / `require_payload` / `resolve_params`；`vcp.measure.runs.load_run / assert_run_matches`、`vcp.data.split.SplitPlan / load_plan`、`vcp.measure.schema.RunCard`、`vcp.cli_common.*`、`vcp.measure.plugins.load_plugins`。
- Produces:
  - `members.parse_member(item: str) -> Member`（`RUN[:WEIGHT]`；壞格式 → `ValidationFailed`）
  - `members.check_members(members: list[Member], *, data_root: Path, dataset: Dataset, plan_id: str) -> list[RunCard]`（依成員順序；缺 run → `ValidationFailed`、dataset / hash / plan 不符 → `PlanMismatchError`，兩者都帶 `fields["member"]`）
  - `members.union_trained_on(cards: list[RunCard]) -> list[str]`（排序後的聯集）
  - `members.common_subsets(plan: SplitPlan, cards: list[RunCard]) -> list[str]`（每個成員都有預測檔的 subset，依 plan 順序）
  - `build.check_plan` 要到 Task 6 才有，本任務的 CLI 先在 `members.py` 提供 `check_plan(plan: SplitPlan, dataset: Dataset) -> None`（`dataset_hash` 不符 → `PlanMismatchError`），Task 6 沿用它。
  - `cli_fuse.fuse_app`（typer group `vcp fuse`）與 `recipe` 命令；`cmd=fuse.recipe`，VERDICT 欄位 `dataset= recipe= method= members= path=`。

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/fuse/test_members.py`：

```python
import pytest

from helpers import det_with_runs, make_card, perfect_predictions, write_images, det_samples
from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.fuse.members import check_members, check_plan, common_subsets, parse_member, union_trained_on
from vcp.fuse.schema import Member
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.predictions import write_predictions


def test_parse_member():
    assert parse_member("a") == Member(run="a", weight=1.0)
    assert parse_member("a:0.5") == Member(run="a", weight=0.5)
    for bad in ("", ":1", "a:", "a:x", "a:0", "a:-1"):
        with pytest.raises(ValidationFailed):
            parse_member(bad)


def test_check_members_loads_cards_in_order(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    cards = check_members(
        [Member(run="noisy"), Member(run="perfect", weight=0.5)],
        data_root=roots.data,
        dataset=ds,
        plan_id="fixed-v1",
    )
    assert [c.run_id for c in cards] == ["noisy", "perfect"]
    assert union_trained_on(cards) == ["train"]
    assert common_subsets(plan, cards) == ["valA", "valB"]
    assert common_subsets(plan, cards[1:]) == ["valA", "valB", "holdout"]


def test_check_members_failures_name_the_member(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    with pytest.raises(ValidationFailed, match="run not found") as ei:
        check_members([Member(run="perfect"), Member(run="ghost")], data_root=roots.data, dataset=ds, plan_id="fixed-v1")
    assert ei.value.fields["member"] == "ghost"
    with pytest.raises(PlanMismatchError, match="plan") as ei:
        check_members([Member(run="perfect")], data_root=roots.data, dataset=ds, plan_id="other-plan")
    assert ei.value.fields["member"] == "perfect"
    # a run of another dataset: same run id namespace (runs/ is per data root), other card
    other_paths = DatasetPaths.resolve("other", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(20, seed=9)
    write_images(roots.data / "raw" / "other", samples)
    other = Dataset.from_parts(make_card("det", name="other", image_root="raw/other"), samples)
    other.save(other_paths)
    other_plan = build_plan(other, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(other_plan, other_paths)
    src = tmp_path / "other-valA.jsonl"
    write_predictions(src, perfect_predictions(other.subset("valA", other_plan), other.card))
    ingest(IngestSpec(run_id="stranger", dataset="other", plan_id="fixed-v1", subset="valA", format="jsonl", src=src, trained_on=["train"], data_root=roots.data, configs_root=roots.configs))
    with pytest.raises(PlanMismatchError, match="belongs to dataset") as ei:
        check_members([Member(run="stranger")], data_root=roots.data, dataset=ds, plan_id="fixed-v1")
    assert ei.value.fields["member"] == "stranger"


def test_union_trained_on_and_check_plan(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    src = tmp_path / "wide-valB.jsonl"
    write_predictions(src, perfect_predictions(ds.subset("valB", plan), ds.card))
    ingest(IngestSpec(run_id="wide", dataset="tiny", plan_id="fixed-v1", subset="valB", format="jsonl", src=src, trained_on=["train", "valA"], data_root=roots.data, configs_root=roots.configs))
    cards = check_members([Member(run="perfect"), Member(run="wide")], data_root=roots.data, dataset=ds, plan_id="fixed-v1")
    assert union_trained_on(cards) == ["train", "valA"]
    assert common_subsets(plan, cards) == ["valB"]
    check_plan(plan, ds)
    with pytest.raises(PlanMismatchError):
        check_plan(plan.model_copy(update={"dataset_hash": "0" * 64}), ds)
```

`tests/unit/test_cli_fuse.py`：

```python
import json

import yaml
from typer.testing import CliRunner

from helpers import det_with_runs
from vcp.cli import app
from vcp.core.paths import DatasetPaths
from vcp.fuse.recipes import recipe_path

runner = CliRunner()


def _last_verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _recipe(*extra, members=("perfect", "noisy:0.5"), method="wbf", rid="r1"):
    args = ["fuse", "recipe", "--dataset", "tiny", "--id", rid, "--plan", "fixed-v1", "--method", method]
    for m in members:
        args += ["--member", m]
    return runner.invoke(app, [*args, *extra])


def test_fuse_recipe_cli(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    r = _recipe("--params", "iou=0.6", "--notes", "golden")
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert v.startswith("VERDICT cmd=fuse.recipe status=OK") and "recipe=r1" in v and "members=2" in v
    doc = yaml.safe_load(recipe_path(paths, "r1").read_text(encoding="utf-8"))
    assert doc["params"] == {"iou": "0.6", "skip": "0", "min_score": "0", "max_per_image": "0", "conf_type": "avg"}
    assert doc["members"] == [{"run": "perfect", "weight": 1.0}, {"run": "noisy", "weight": 0.5}]
    assert doc["notes"] == "golden" and doc["created_at"].endswith("Z")
    r = _recipe("--json", "--params", "iou=0.6")
    assert r.exit_code == 1 and "recipe_exists" in _last_verdict(r.output) and "recipe=r1" in _last_verdict(r.output)
    r = _recipe("--json", rid="r2")
    assert r.exit_code == 0
    payload = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert payload["result"]["recipe"]["recipe_id"] == "r2"


def test_fuse_recipe_cli_failures_write_nothing(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    r = _recipe("--params", "iou=2")
    assert r.exit_code == 1 and "param=iou" in _last_verdict(r.output)
    r = _recipe("--params", "zz=1")
    assert r.exit_code == 1 and "param=zz" in _last_verdict(r.output)
    r = _recipe(members=("perfect", "ghost"))
    assert r.exit_code == 1 and "member=ghost" in _last_verdict(r.output)
    r = _recipe(method="nope")
    assert r.exit_code == 2 and "method=nope" in _last_verdict(r.output)
    r = _recipe(method="mean")
    assert r.exit_code == 1 and "payload=boxes" in _last_verdict(r.output)
    r = _recipe(members=("perfect:0",))
    assert r.exit_code == 1 and "weight" in _last_verdict(r.output)
    r = _recipe(members=("perfect", "perfect"))
    assert r.exit_code == 1 and "duplicate" in _last_verdict(r.output)
    assert not (paths.config_dir / "fuse").exists()


def test_fuse_group_is_listed():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0 and "fuse" in r.output
    r = runner.invoke(app, ["fuse", "--help"])
    assert r.exit_code == 0 and "recipe" in r.output
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/fuse/test_members.py tests/unit/test_cli_fuse.py -o addopts="" -q`
Expected: `ModuleNotFoundError: No module named 'vcp.fuse.members'`。

- [ ] **Step 3: 寫 `members.py`、`cli_fuse.py`、掛上群組**

`src/vcp/fuse/members.py`：

```python
"""Member runs of a recipe: parsed from the CLI, checked against dataset / plan, summarised.

A recipe names runs; a run belongs to one dataset version and one plan. Every check here
answers "may these runs be fused together at all?" and names the member that says no, so a
twelve-member recipe fails on `member=` rather than on a message the user has to search.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.split import SplitPlan
from vcp.fuse.schema import Member
from vcp.measure.runs import assert_run_matches, load_run
from vcp.measure.schema import RunCard


def parse_member(item: str) -> Member:
    """``RUN[:WEIGHT]`` as typed on the command line."""
    run, sep, weight = item.partition(":")
    if not run:
        raise ValidationFailed(f"--member expects RUN[:WEIGHT], got {item!r}")
    if not sep:
        return Member(run=run)
    try:
        value = float(weight)
    except ValueError:
        raise ValidationFailed(f"--member {item!r}: weight must be a number") from None
    try:
        return Member(run=run, weight=value)
    except ValidationError as e:
        raise ValidationFailed(str(e), location=f"--member {item}") from e


def check_plan(plan: SplitPlan, dataset: Dataset) -> None:
    """The plan must have been built on this dataset version (same check `ingest` makes)."""
    if plan.dataset_hash != dataset.card.samples_hash:
        raise PlanMismatchError(
            f"plan {plan.plan_id!r} was built on samples_hash {plan.dataset_hash[:12]}, "
            f"dataset now has {dataset.card.samples_hash[:12]}"
        )


def check_members(
    members: list[Member], *, data_root: Path, dataset: Dataset, plan_id: str
) -> list[RunCard]:
    """Every member's run card, in recipe order, each checked against the dataset and plan
    (spec 8). A failure carries ``fields["member"]`` so the VERDICT names the culprit."""
    cards: list[RunCard] = []
    for m in members:
        try:
            card = load_run(data_root, m.run)
            assert_run_matches(card, dataset)
            if card.plan_id != plan_id:
                raise PlanMismatchError(
                    f"member {m.run!r} uses plan {card.plan_id!r}, not {plan_id!r}"
                )
        except (ValidationFailed, PlanMismatchError) as e:
            e.fields.setdefault("member", m.run)
            raise
        cards.append(card)
    return cards


def union_trained_on(cards: list[RunCard]) -> list[str]:
    """What the fused run must declare as trained on: everything any member saw."""
    return sorted(set().union(*(set(c.trained_on) for c in cards)))


def common_subsets(plan: SplitPlan, cards: list[RunCard]) -> list[str]:
    """Subsets every member has predictions for, in plan order (spec 6.1's default)."""
    return [s.name for s in plan.subsets if all(s.name in c.predictions for c in cards)]
```

`src/vcp/cli_fuse.py`（本任務只有 `recipe`；Task 6、7 各加一個命令）：

```python
"""``vcp fuse``: fusion-layer commands. Every command ends with a VERDICT line."""

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
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.split import load_plan
from vcp.fuse.fusers import get_fuser, require_payload, resolve_params
from vcp.fuse.members import check_members, check_plan, parse_member
from vcp.fuse.recipes import save_recipe
from vcp.fuse.schema import Recipe
from vcp.measure.plugins import load_plugins

fuse_app = typer.Typer(no_args_is_help=True, help="fusion commands")

DatasetOpt = Annotated[str, typer.Option("--dataset", help="dataset name")]
RecipeOpt = Annotated[str, typer.Option("--recipe", help="recipe id")]
PluginOpt = Annotated[
    list[str] | None,
    typer.Option("--plugin", help="python module to import (registers fusers / metrics)"),
]


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@fuse_app.command("recipe")
def recipe_cmd(
    dataset: DatasetOpt,
    recipe_id: Annotated[str, typer.Option("--id", help="recipe id (path-safe)")],
    plan: Annotated[str, typer.Option("--plan", help="plan every member was ingested under")],
    method: Annotated[str, typer.Option("--method", help="registered fuser name")],
    member: Annotated[
        list[str], typer.Option("--member", help="RUN[:WEIGHT], repeatable; order is kept")
    ],
    params: Annotated[
        list[str] | None, typer.Option("--params", help="fuser param key=value (repeatable)")
    ] = None,
    notes: Annotated[str, typer.Option("--notes")] = "",
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Write a fusion recipe (git) after checking every member against the dataset and plan."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
        ds = Dataset.load(dataset, data_root=data_root, configs_root=configs_root)
        check_plan(load_plan(paths, plan), ds)
        fuser = get_fuser(method)
        require_payload(fuser, ds.card.task)
        effective = resolve_params(fuser, parse_opts(params, "--params"))
        members = [parse_member(item) for item in member]
        try:
            recipe = Recipe(
                recipe_id=recipe_id,
                dataset=dataset,
                plan_id=plan,
                method=method,
                params=effective,
                members=members,
                notes=notes,
                created_at=stamp(),
            )
        except ValidationError as e:
            raise ValidationFailed(str(e), location="vcp fuse recipe") from e
        check_members(recipe.members, data_root=paths.data_root, dataset=ds, plan_id=plan)
        path = save_recipe(paths, recipe)
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "recipe": recipe_id,
            "method": method,
            "members": len(members),
            "path": str(path),
        }
        payload = {"recipe": recipe.model_dump(mode="json"), "path": str(path)}
        return "OK", fields, payload, [f"wrote recipe {recipe_id} -> {path}"]

    run_command("fuse.recipe", json_mode, data_root, fn)
```

`src/vcp/cli.py`：在 `from vcp.cli_eval import eval_app` 下一行加 `from vcp.cli_fuse import fuse_app`（ruff isort 會要求字母序：`cli_eval` 在 `cli_fuse` 前，正好）；在 `app.add_typer(eval_app, name="eval")` 下一行加 `app.add_typer(fuse_app, name="fuse")`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/fuse tests/unit/test_cli_fuse.py tests/unit/test_cli.py -o addopts="" -q`
Expected: 全部 PASS；ruff check / format 乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/fuse/members.py src/vcp/cli_fuse.py src/vcp/cli.py tests/unit/fuse/test_members.py tests/unit/test_cli_fuse.py
git commit -m "feat(fuse): 成員驗證與 vcp fuse recipe——驗成員後把配方寫進 git

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `build`——逐 subset 融合、位元級重現、`vcp fuse build`（spec §4.2、§4.3、§6 build 列、§6.1、§6.2）

**Files:**
- Create: `src/vcp/fuse/build.py`
- Modify: `src/vcp/cli_fuse.py`（加 `build` 命令）
- Test: `tests/unit/fuse/test_build.py`、`tests/unit/test_cli_fuse.py`（加 build 的 CLI 測試）

**Interfaces:**
- Consumes: Task 1 的 `Recipe` / `FuseRecord` / `MemberRecord` / `SubsetBuild` / `load_recipe` / `recipe_sha`；Task 2 的 `FuseContext` / `MemberPredictions` / `get_fuser` / `require_payload` / `resolve_params`；Task 5 的 `check_members` / `check_plan` / `common_subsets` / `union_trained_on`；量測層 `read_predictions` / `write_predictions` / `predictions_by_id` / `check_predictions` / `PredictionStats`、`load_run` / `save_run` / `assert_run_matches` / `verify_prediction` / `append_history` / `prediction_path` / `run_dir`、`RunCard` / `RunSource` / `PredictionFile`；`vcp.core.hashing.sha256_text`；`vcp.__version__`。
- Produces:
  - 常數 `FRAMEWORK = "vcp.fuse"`、`RECORD_FILE = "fuse.json"`、`NO_COMMON_SUBSET`、`OUTPUT_EXISTS`、`RUN_BOUND_ELSEWHERE`
  - `BuildSpec(dataset, recipe_id, run_id: str | None = None, subsets: list[str] = [], replace: bool = False, data_root, configs_root)`
  - `SubsetOutcome(sha256, samples, empty, cached: bool)`、`BuildResult(run: RunCard, recipe: Recipe, record: FuseRecord, subsets: dict[str, SubsetOutcome], built: int, cached: int, created_run: bool)`
  - `default_run_id(recipe_id) -> str`（`fuse-<recipe_id>`）、`record_path(data_root, run_id)`、`load_record(data_root, run_id) -> FuseRecord`、`write_record(data_root, run_id, record) -> Path`
  - `content_sha(preds) -> str`、`resolve_subsets(requested, plan, cards) -> list[str]`、`check_existing_run(data_root, run_id, *, recipe, recipe_sha256, dataset, trained_on) -> RunCard | None`
  - `build_run(spec: BuildSpec) -> BuildResult`
  - CLI `build`：`cmd=fuse.build`，欄位 `dataset= run= recipe= method= members= subsets= built= cached=`

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/fuse/test_build.py`：

```python
import json

import pytest

from helpers import (
    ML_CATS,
    dataset_with_perfect_run,
    det_with_runs,
    multilabel_samples,
    noisy_predictions,
    perfect_predictions,
)
from vcp import __version__
from vcp.core.errors import IntegrityError, PlanMismatchError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.fuse.build import (
    FRAMEWORK,
    NO_COMMON_SUBSET,
    OUTPUT_EXISTS,
    RUN_BOUND_ELSEWHERE,
    BuildSpec,
    build_run,
    content_sha,
    load_record,
    record_path,
)
from vcp.fuse.fusers import FUSERS, register_fuser
from vcp.fuse.recipes import recipe_sha, save_recipe
from vcp.fuse.schema import Member, Recipe
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.predictions import read_predictions, write_predictions
from vcp.measure.runs import load_run, run_dir
from vcp.measure.schema import Prediction

STAMP = "2026-09-05T00:00:00.000Z"


def _recipe(paths, rid="r1", members=(("perfect", 1.0), ("noisy", 0.5)), method="wbf", params=None, dataset="tiny"):
    r = Recipe(
        recipe_id=rid,
        dataset=dataset,
        plan_id="fixed-v1",
        method=method,
        params=params or {},
        members=[Member(run=a, weight=w) for a, w in members],
        created_at=STAMP,
    )
    save_recipe(paths, r)
    return r


def _build(roots, rid="r1", dataset="tiny", **kw):
    return build_run(BuildSpec(dataset=dataset, recipe_id=rid, data_root=roots.data, configs_root=roots.configs, **kw))


def test_build_writes_an_ordinary_run_with_provenance(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths, params={"iou": "0.5"})
    res = _build(roots)
    assert res.created_run and res.built == 2 and res.cached == 0
    assert list(res.subsets) == ["valA", "valB"]  # common subsets, plan order (noisy has no holdout)
    card = load_run(roots.data, "fuse-r1")
    assert card.source.framework == FRAMEWORK
    assert card.source.config_hash == recipe_sha(paths, "r1")
    assert card.trained_on == ["train"] and card.plan_id == "fixed-v1"
    assert card.samples_hash == ds.card.samples_hash
    for subset in ("valA", "valB"):
        entry = card.predictions[subset]
        assert entry.format_in == "fuse:wbf" and entry.export_manifest_sha is None
        path = run_dir(roots.data, "fuse-r1") / entry.path
        assert sha256_file(path) == entry.sha256 == res.subsets[subset].sha256
        rows = read_predictions(path)
        assert entry.samples == len(rows) and entry.samples + entry.empty == len(plan.ids_in(subset))
        assert all(p.boxes for p in rows)
    rec = load_record(roots.data, "fuse-r1")
    assert rec.run_id == "fuse-r1" and rec.recipe_id == "r1" and rec.recipe_sha256 == card.source.config_hash
    assert rec.method == "wbf" and rec.method_version == "1" and rec.vcp_version == __version__
    assert rec.params["iou"] == "0.5" and rec.params["conf_type"] == "avg"
    assert [(m.run, m.weight, m.trained_on) for m in rec.members] == [("perfect", 1.0, ["train"]), ("noisy", 0.5, ["train"])]
    perfect = load_run(roots.data, "perfect")
    assert rec.subsets["valA"].member_sha256 == {"perfect": perfect.predictions["valA"].sha256, "noisy": load_run(roots.data, "noisy").predictions["valA"].sha256}
    assert rec.subsets["valA"].output_sha256 == card.predictions["valA"].sha256
    text = record_path(roots.data, "fuse-r1").read_bytes()
    assert b"\r" not in text and text.endswith(b"\n")


def test_rebuild_is_cached_and_bytes_identical(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    first = _build(roots)
    path = run_dir(roots.data, "fuse-r1") / "predictions" / "valA.jsonl"
    before = path.read_bytes()
    stamp_before = record_path(roots.data, "fuse-r1").read_text(encoding="utf-8")
    again = _build(roots)
    assert again.built == 0 and again.cached == 2 and not again.created_run
    assert all(o.cached for o in again.subsets.values())
    assert path.read_bytes() == before
    assert record_path(roots.data, "fuse-r1").read_text(encoding="utf-8") == stamp_before
    assert not (run_dir(roots.data, "fuse-r1") / "history.jsonl").exists()
    assert first.subsets["valA"].sha256 == again.subsets["valA"].sha256


def test_content_sha_matches_write_predictions(tmp_path):
    preds = [Prediction(sample_id="b", scores={"x": 0.5}), Prediction(sample_id="a", boxes=[])]
    assert content_sha(preds) == write_predictions(tmp_path / "p.jsonl", preds)


def test_explicit_subsets_and_missing_member_file(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    res = _build(roots, subsets=["valB"])
    assert list(res.subsets) == ["valB"]
    with pytest.raises(ValidationFailed, match="no predictions") as ei:
        _build(roots, subsets=["holdout"])
    assert ei.value.fields == {"member": "noisy", "subset": "holdout"}
    with pytest.raises(PlanMismatchError):
        _build(roots, subsets=["nope"])
    assert "holdout" not in load_run(roots.data, "fuse-r1").predictions


def test_no_common_subset(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    src = tmp_path / "h.jsonl"
    write_predictions(src, perfect_predictions(ds.subset("holdout", plan, unseal=True, reason="t", paths=paths), ds.card))
    ingest(IngestSpec(run_id="holdout_only", dataset="tiny", plan_id="fixed-v1", subset="holdout", format="jsonl", src=src, trained_on=["train"], data_root=roots.data, configs_root=roots.configs))
    _recipe(paths, members=(("noisy", 1.0), ("holdout_only", 1.0)))
    with pytest.raises(ValidationFailed, match=NO_COMMON_SUBSET):
        _build(roots)
    assert not run_dir(roots.data, "fuse-r1").exists()


def test_tampered_member_file_fails_before_any_write(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    path = run_dir(roots.data, "noisy") / "predictions" / "valB.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(IntegrityError) as ei:
        _build(roots)
    assert ei.value.fields == {"member": "noisy", "subset": "valB"}
    assert not run_dir(roots.data, "fuse-r1").exists()


def test_output_exists_replace_and_history(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    first = _build(roots)
    # the member changes (re-ingested with other predictions) -> the fused bytes would change
    src = tmp_path / "noisy2-valA.jsonl"
    write_predictions(src, noisy_predictions(ds.subset("valA", plan), ds.card, seed=99, flip=0.6))
    ingest(IngestSpec(run_id="noisy", dataset="tiny", plan_id="fixed-v1", subset="valA", format="jsonl", src=src, replace=True, data_root=roots.data, configs_root=roots.configs))
    with pytest.raises(ValidationFailed, match=OUTPUT_EXISTS) as ei:
        _build(roots)
    assert ei.value.fields == {"run": "fuse-r1", "subset": "valA"}
    assert load_run(roots.data, "fuse-r1").predictions["valA"].sha256 == first.subsets["valA"].sha256
    res = _build(roots, replace=True)
    assert res.built == 1 and res.cached == 1 and not res.subsets["valA"].cached
    assert res.subsets["valA"].sha256 != first.subsets["valA"].sha256
    lines = [json.loads(line) for line in (run_dir(roots.data, "fuse-r1") / "history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines == [{"event": "replace", "subset": "valA", "old_sha256": first.subsets["valA"].sha256, "via": "fuse.build", "ts": lines[0]["ts"]}]
    rec = load_record(roots.data, "fuse-r1")
    assert rec.subsets["valA"].member_sha256["noisy"] == load_run(roots.data, "noisy").predictions["valA"].sha256
    assert rec.subsets["valB"].output_sha256 == first.subsets["valB"].sha256


def test_run_id_is_bound_to_one_recipe(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    _build(roots)
    _recipe(paths, rid="r2", params={"iou": "0.9"})
    with pytest.raises(ValidationFailed, match=RUN_BOUND_ELSEWHERE) as ei:
        _build(roots, rid="r2", run_id="fuse-r1")
    assert ei.value.fields == {"run": "fuse-r1"}
    with pytest.raises(ValidationFailed, match=RUN_BOUND_ELSEWHERE):
        _build(roots, run_id="perfect")  # an ingested run is not a build of anything
    res = _build(roots, rid="r2", run_id="custom")
    assert res.run.run_id == "custom" and load_record(roots.data, "custom").recipe_id == "r2"


def test_plugin_fuser_output_is_validated_and_nothing_written(roots, tmp_path, monkeypatch):
    ds, plan, paths = det_with_runs(roots, tmp_path)

    class Rogue:
        name = "test_rogue"
        version = "1"
        payloads = frozenset({"boxes"})
        defaults = {}

        def check_params(self, params):
            return None

        def fuse(self, members, ctx):
            return [Prediction(sample_id="not-in-subset", boxes=[])]

    monkeypatch.delitem(FUSERS, "test_rogue", raising=False)
    register_fuser(Rogue())
    try:
        _recipe(paths, method="test_rogue")
        with pytest.raises(ValidationFailed, match="unknown sample_id") as ei:
            _build(roots)
        assert ei.value.fields == {"subset": "valA"}
        assert not run_dir(roots.data, "fuse-r1").exists()
    finally:
        FUSERS.pop("test_rogue", None)


def test_mean_build_on_multilabel(roots, tmp_path):
    ds, plan, paths = dataset_with_perfect_run(roots, tmp_path, name="ml", task="multilabel", samples=multilabel_samples(40, seed=1), categories=ML_CATS, run_id="a")
    for subset in ("valA", "valB"):
        src = tmp_path / f"b-{subset}.jsonl"
        write_predictions(src, noisy_predictions(ds.subset(subset, plan), ds.card, seed=3))
        ingest(IngestSpec(run_id="b", dataset="ml", plan_id="fixed-v1", subset=subset, format="jsonl", src=src, trained_on=["train"], data_root=roots.data, configs_root=roots.configs))
    _recipe(paths, rid="m", members=(("a", 1.0), ("b", 1.0)), method="mean", dataset="ml")
    res = _build(roots, rid="m", dataset="ml")
    a = {p.sample_id: p for p in read_predictions(run_dir(roots.data, "a") / "predictions" / "valA.jsonl")}
    b = {p.sample_id: p for p in read_predictions(run_dir(roots.data, "b") / "predictions" / "valA.jsonl")}
    fused = read_predictions(run_dir(roots.data, "fuse-m") / "predictions" / "valA.jsonl")
    assert len(fused) == len(a) == res.subsets["valA"].samples and res.subsets["valA"].empty == 0
    for p in fused:
        for k, v in p.scores.items():
            assert v == pytest.approx((a[p.sample_id].scores[k] + b[p.sample_id].scores[k]) / 2)
```

`tests/unit/test_cli_fuse.py` 加：

```python
def test_fuse_build_cli(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    assert _recipe().exit_code == 0
    r = runner.invoke(app, ["fuse", "build", "--dataset", "tiny", "--recipe", "r1"])
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert v.startswith("VERDICT cmd=fuse.build status=OK") and "run=fuse-r1" in v
    assert "subsets=valA,valB" in v and "built=2" in v and "cached=0" in v and "members=2" in v
    r = runner.invoke(app, ["fuse", "build", "--dataset", "tiny", "--recipe", "r1", "--json"])
    assert r.exit_code == 0
    payload = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert payload["fields"]["cached"] == 2 and payload["result"]["run_id"] == "fuse-r1"
    assert set(payload["result"]["subsets"]) == {"valA", "valB"}
    assert payload["result"]["subsets"]["valA"]["cached"] is True
    r = runner.invoke(app, ["fuse", "build", "--dataset", "tiny", "--recipe", "r1", "--subsets", "holdout"])
    assert r.exit_code == 1 and "member=noisy" in _last_verdict(r.output) and "subset=holdout" in _last_verdict(r.output)
    r = runner.invoke(app, ["fuse", "build", "--dataset", "tiny", "--recipe", "ghost"])
    assert r.exit_code == 1 and "recipe=ghost" in _last_verdict(r.output)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/fuse/test_build.py tests/unit/test_cli_fuse.py -o addopts="" -q`
Expected: `ModuleNotFoundError: No module named 'vcp.fuse.build'`；CLI 測試 `No such command 'build'`。

- [ ] **Step 3: 寫 `build.py` 與 `build` 命令**

`src/vcp/fuse/build.py`：

```python
"""``vcp fuse build``: a recipe's members, fused subset by subset, written as one ordinary run.

Nothing is written until every requested subset has been fused in memory and validated (spec
6.2): a member whose file drifted from its recorded sha, a fuser that emitted a row for a sample
outside the subset, an output that would silently differ from the one already on disk -- each
fails before the first byte, so a failed build leaves the run exactly as it was. What does get
written is bit-reproducible: the same member bytes and the same recipe give the same output
sha, and ``fuse.json`` records both sides so anyone can check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vcp import __version__
from vcp.core.errors import (
    IntegrityError,
    InvariantError,
    PlanMismatchError,
    ValidationFailed,
    VcpError,
)
from vcp.core.hashing import sha256_text
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.split import SplitPlan, load_plan
from vcp.fuse.fusers import (
    FuseContext,
    Fuser,
    MemberPredictions,
    get_fuser,
    require_payload,
    resolve_params,
)
from vcp.fuse.members import check_members, check_plan, common_subsets, union_trained_on
from vcp.fuse.recipes import load_recipe, recipe_sha
from vcp.fuse.schema import FuseRecord, MemberRecord, Recipe, SubsetBuild
from vcp.measure.predictions import (
    PredictionStats,
    check_predictions,
    predictions_by_id,
    read_predictions,
    write_predictions,
)
from vcp.measure.runs import (
    append_history,
    assert_run_matches,
    load_run,
    prediction_path,
    run_dir,
    save_run,
    verify_prediction,
)
from vcp.measure.schema import Prediction, PredictionFile, RunCard, RunSource

FRAMEWORK = "vcp.fuse"
RECORD_FILE = "fuse.json"
NO_COMMON_SUBSET = "no_common_subset"
OUTPUT_EXISTS = "output_exists"
RUN_BOUND_ELSEWHERE = "run_bound_elsewhere"


class BuildSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    recipe_id: str
    run_id: str | None = None
    subsets: list[str] = Field(default_factory=list)
    replace: bool = False
    data_root: Path | None = None
    configs_root: Path | None = None


class SubsetOutcome(BaseModel):
    sha256: str
    samples: int
    empty: int
    cached: bool


class BuildResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    run: RunCard
    recipe: Recipe
    record: FuseRecord
    subsets: dict[str, SubsetOutcome]
    built: int
    cached: int
    created_run: bool


@dataclass(frozen=True)
class _Fused:
    rows: list[Prediction]
    stats: PredictionStats
    sha256: str
    member_sha256: dict[str, str]


def default_run_id(recipe_id: str) -> str:
    return f"fuse-{recipe_id}"


def record_path(data_root: Path, run_id: str) -> Path:
    return run_dir(data_root, run_id) / RECORD_FILE


def load_record(data_root: Path, run_id: str) -> FuseRecord:
    path = record_path(data_root, run_id)
    if not path.is_file():
        raise ValidationFailed(f"fusion record not found: {path}", fields={"run": run_id})
    try:
        return FuseRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValidationFailed(
            f"bad fusion record: {e}", location=str(path), fields={"run": run_id}
        ) from e


def write_record(data_root: Path, run_id: str, record: FuseRecord) -> Path:
    path = record_path(data_root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
    return path


def content_sha(preds: list[Prediction]) -> str:
    """The sha256 ``write_predictions`` would record for these rows, without touching the disk
    (the cache check runs before any write). Serialisation is kept identical to
    ``write_predictions`` -- sorted by sample_id, exclude_none, LF -- and a test pins them."""
    ordered = sorted(preds, key=lambda p: p.sample_id)
    return sha256_text("".join(p.model_dump_json(exclude_none=True) + "\n" for p in ordered))


def resolve_subsets(requested: list[str], plan: SplitPlan, cards: list[RunCard]) -> list[str]:
    """Spec 6.1: the subsets named, each checked for every member; else the common ones."""
    if requested:
        names = list(dict.fromkeys(requested))
        for name in names:
            plan.subset(name)  # PlanMismatchError for an unknown subset
            for card in cards:
                if name not in card.predictions:
                    raise ValidationFailed(
                        f"member {card.run_id!r} has no predictions for subset {name!r}",
                        fields={"member": card.run_id, "subset": name},
                    )
        return names
    names = common_subsets(plan, cards)
    if not names:
        raise ValidationFailed(
            f"{NO_COMMON_SUBSET}: no subset has predictions from every member; "
            "name one with --subsets after ingesting it for each member"
        )
    return names


def check_existing_run(
    data_root: Path,
    run_id: str,
    *,
    recipe: Recipe,
    recipe_sha256: str,
    dataset: Dataset,
    trained_on: list[str],
) -> RunCard | None:
    """The run.yaml already under this id, if it is a build of THIS recipe; None when absent.

    A run id binds to one recipe file (spec 4.1): an ingested run, or a build of some other
    recipe, must never be silently overwritten with this recipe's output.
    """
    if not (run_dir(data_root, run_id) / "run.yaml").is_file():
        return None
    card = load_run(data_root, run_id)
    if card.source.framework != FRAMEWORK or card.source.config_hash != recipe_sha256:
        raise ValidationFailed(
            f"{RUN_BOUND_ELSEWHERE}: run {run_id!r} is not a build of recipe "
            f"{recipe.recipe_id!r} (framework={card.source.framework!r}, "
            f"config_hash={card.source.config_hash!r}); pick another --run",
            fields={"run": run_id},
        )
    assert_run_matches(card, dataset)
    if card.plan_id != recipe.plan_id:
        raise PlanMismatchError(
            f"run {run_id!r} uses plan {card.plan_id!r}, not {recipe.plan_id!r}",
            fields={"run": run_id},
        )
    if card.trained_on != trained_on:
        raise ValidationFailed(
            f"run {run_id!r} declares trained_on={card.trained_on}; the members now give "
            f"{trained_on}",
            fields={"run": run_id},
        )
    return card


def _fuse_subset(
    subset: str,
    *,
    data_root: Path,
    dataset: Dataset,
    plan: SplitPlan,
    recipe: Recipe,
    cards: list[RunCard],
    fuser: Fuser,
    params: dict[str, str],
) -> _Fused:
    ids = plan.ids_in(subset)
    ordered = [s.sample_id for s in dataset.samples if s.sample_id in ids]
    members: list[MemberPredictions] = []
    shas: dict[str, str] = {}
    for m, card in zip(recipe.members, cards, strict=True):
        try:
            path = verify_prediction(data_root, card, subset)
        except (ValidationFailed, IntegrityError) as e:
            e.fields.setdefault("member", m.run)
            e.fields.setdefault("subset", subset)
            raise
        shas[m.run] = card.predictions[subset].sha256
        members.append(
            MemberPredictions(
                run_id=m.run, weight=m.weight, predictions=predictions_by_id(read_predictions(path))
            )
        )
    ctx = FuseContext(
        dataset=dataset,
        subset=subset,
        ids=ordered,
        samples={sid: dataset.by_id[sid] for sid in ordered},
        params=params,
    )
    try:
        rows = fuser.fuse(members, ctx)
        kept, stats = check_predictions(rows, dataset, ids)
    except VcpError as e:
        e.fields.setdefault("subset", subset)
        raise
    return _Fused(rows=kept, stats=stats, sha256=content_sha(kept), member_sha256=shas)


def _new_card(recipe: Recipe, dataset: Dataset, run_id: str, sha: str, trained_on: list[str]) -> RunCard:
    return RunCard(
        run_id=run_id,
        dataset=recipe.dataset,
        samples_hash=dataset.card.samples_hash,
        plan_id=recipe.plan_id,
        trained_on=trained_on,
        source=RunSource(framework=FRAMEWORK, config_hash=sha, notes=recipe.notes),
        created_at=stamp(),
    )


def _new_record(
    recipe: Recipe, run_id: str, sha: str, fuser: Fuser, params: dict[str, str], cards: list[RunCard]
) -> FuseRecord:
    return FuseRecord(
        run_id=run_id,
        recipe_id=recipe.recipe_id,
        recipe_sha256=sha,
        method=fuser.name,
        method_version=fuser.version,
        params=params,
        members=[
            MemberRecord(run=m.run, weight=m.weight, trained_on=list(c.trained_on))
            for m, c in zip(recipe.members, cards, strict=True)
        ],
        vcp_version=__version__,
    )


def _write(
    data_root: Path,
    run_id: str,
    fuser: Fuser,
    card: RunCard,
    record: FuseRecord,
    pending: list[tuple[str, _Fused]],
) -> tuple[RunCard, FuseRecord, dict[str, SubsetOutcome]]:
    """Spec 6.2 step 5: replace events -> prediction files -> run.yaml -> fuse.json."""
    outcomes: dict[str, SubsetOutcome] = {}
    for subset, f in pending:
        prev = card.predictions.get(subset)
        if prev is not None:
            append_history(
                data_root,
                run_id,
                {"event": "replace", "subset": subset, "old_sha256": prev.sha256, "via": "fuse.build"},
            )
        path = prediction_path(data_root, run_id, subset)
        written = write_predictions(path, f.rows)
        if written != f.sha256:
            raise InvariantError(
                f"fused predictions for {subset!r} hashed {written[:12]} on disk but "
                f"{f.sha256[:12]} in memory"
            )
        entry = PredictionFile(
            path=path.relative_to(run_dir(data_root, run_id)).as_posix(),
            sha256=written,
            samples=f.stats.predicted,
            empty=f.stats.empty,
            format_in=f"fuse:{fuser.name}",
            ingested_at=stamp(),
        )
        card = card.model_copy(update={"predictions": {**card.predictions, subset: entry}})
        build = SubsetBuild(
            member_sha256=f.member_sha256,
            output_sha256=written,
            samples=f.stats.predicted,
            empty=f.stats.empty,
            built_at=stamp(),
        )
        record = record.model_copy(update={"subsets": {**record.subsets, subset: build}})
        outcomes[subset] = SubsetOutcome(
            sha256=written, samples=f.stats.predicted, empty=f.stats.empty, cached=False
        )
    save_run(data_root, card)
    write_record(data_root, run_id, record)
    return card, record, outcomes


def build_run(spec: BuildSpec) -> BuildResult:
    paths = DatasetPaths.resolve(
        spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    dataset = Dataset.load(spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root)
    recipe = load_recipe(paths, spec.recipe_id)
    plan = load_plan(paths, recipe.plan_id)
    check_plan(plan, dataset)
    fuser = get_fuser(recipe.method)
    require_payload(fuser, dataset.card.task)
    params = resolve_params(fuser, recipe.params)
    cards = check_members(
        recipe.members, data_root=paths.data_root, dataset=dataset, plan_id=recipe.plan_id
    )
    subsets = resolve_subsets(spec.subsets, plan, cards)
    run_id = spec.run_id or default_run_id(recipe.recipe_id)
    sha = recipe_sha(paths, recipe.recipe_id)
    trained_on = union_trained_on(cards)
    existing = check_existing_run(
        paths.data_root, run_id, recipe=recipe, recipe_sha256=sha, dataset=dataset, trained_on=trained_on
    )
    card = existing or _new_card(recipe, dataset, run_id, sha, trained_on)
    if existing is not None and record_path(paths.data_root, run_id).is_file():
        record = load_record(paths.data_root, run_id)
    else:
        record = _new_record(recipe, run_id, sha, fuser, params, cards)
    # Steps 2-3: every subset fused and validated in memory before anything is compared or written.
    fused = {
        subset: _fuse_subset(
            subset,
            data_root=paths.data_root,
            dataset=dataset,
            plan=plan,
            recipe=recipe,
            cards=cards,
            fuser=fuser,
            params=params,
        )
        for subset in subsets
    }
    # Step 4: cache hits and conflicts, still before the first write.
    outcomes: dict[str, SubsetOutcome] = {}
    pending: list[tuple[str, _Fused]] = []
    for subset, f in fused.items():
        prev = card.predictions.get(subset)
        if prev is not None and prev.sha256 == f.sha256:
            outcomes[subset] = SubsetOutcome(
                sha256=f.sha256, samples=f.stats.predicted, empty=f.stats.empty, cached=True
            )
            continue
        if prev is not None and not spec.replace:
            raise ValidationFailed(
                f"{OUTPUT_EXISTS}: run {run_id!r} already has different predictions for "
                f"{subset!r} ({prev.sha256[:12]} vs {f.sha256[:12]}); pass --replace to "
                "overwrite (the old sha goes to history.jsonl)",
                fields={"run": run_id, "subset": subset},
            )
        pending.append((subset, f))
    if pending:
        card, record, written = _write(paths.data_root, run_id, fuser, card, record, pending)
        outcomes.update(written)
    return BuildResult(
        run=card,
        recipe=recipe,
        record=record,
        subsets={s: outcomes[s] for s in subsets},
        built=len(pending),
        cached=len(subsets) - len(pending),
        created_run=existing is None,
    )
```

`src/vcp/cli_fuse.py` 加（import `from vcp.fuse.build import BuildSpec, build_run`，並在 `recipe_cmd` 之後）：

```python
@fuse_app.command("build")
def build_cmd(
    dataset: DatasetOpt,
    recipe_id: RecipeOpt,
    run: Annotated[str | None, typer.Option("--run", help="run id; default fuse-<recipe>")] = None,
    subsets: Annotated[
        str | None,
        typer.Option("--subsets", help="comma-separated; default: subsets every member has"),
    ] = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="overwrite an existing, different output")
    ] = False,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Fuse the recipe's members into a run (nothing is written unless every subset validates)."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        res = build_run(
            BuildSpec(
                dataset=dataset,
                recipe_id=recipe_id,
                run_id=run,
                subsets=_csv(subsets),
                replace=replace,
                data_root=data_root,
                configs_root=configs_root,
            )
        )
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "run": res.run.run_id,
            "recipe": recipe_id,
            "method": res.recipe.method,
            "members": len(res.recipe.members),
            "subsets": ",".join(res.subsets),
            "built": res.built,
            "cached": res.cached,
        }
        payload = {
            "run_id": res.run.run_id,
            "recipe_id": recipe_id,
            "trained_on": res.run.trained_on,
            "subsets": {s: o.model_dump(mode="json") for s, o in res.subsets.items()},
        }
        human = [
            f"{s}: {'cached' if o.cached else 'built'} {o.samples} rows ({o.empty} empty) "
            f"sha {o.sha256[:12]}"
            for s, o in res.subsets.items()
        ]
        return "OK", fields, payload, human

    run_command("fuse.build", json_mode, data_root, fn)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/fuse tests/unit/test_cli_fuse.py -o addopts="" -q`
Expected: 全部 PASS；ruff check / format 乾淨（`build.py` 的長行以 ruff format 為準）。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/fuse/build.py src/vcp/cli_fuse.py tests/unit/fuse/test_build.py tests/unit/test_cli_fuse.py
git commit -m "feat(fuse): build——逐 subset 融合、全驗證後一次寫入、fuse.json 記成員與輸出 sha

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: `ablate`——變體配方、準入預登記、全有或全無；`measured_subsets` 公開（spec §6 ablate 列、§6.3、§6.4、§10）

**Files:**
- Modify: `src/vcp/measure/prereg.py:78-92`（`_measured_subsets` → `measured_subsets`，`create_prereg` 內的呼叫跟著改）
- Create: `src/vcp/fuse/ablate.py`
- Modify: `src/vcp/cli_fuse.py`（加 `ablate` 命令）
- Test: `tests/unit/fuse/test_ablate.py`、`tests/unit/test_cli_fuse.py`（加 ablate 的 CLI 測試）、`tests/unit/measure/test_prereg_judge.py`（若有引用 `_measured_subsets` 就改名；先 `grep -rn "_measured_subsets" src tests`）

**Interfaces:**
- Consumes: Task 1 的 `Recipe` / `Member` / `load_recipe` / `save_recipe` / `recipe_path` / `recipe_sha` / `same_recipe`；Task 5 的 `check_members` / `check_plan` / `union_trained_on`；Task 6 的 `BuildSpec` / `build_run` / `default_run_id` / `resolve_subsets` / `check_existing_run` / `RUN_BOUND_ELSEWHERE`；量測層 `create_prereg` / `prereg_path` / `measured_subsets`、`ReadingsLedger`、`get_metric` / `effective_params`（指標的，匯入時取別名 `metric_params`）/ `params_key`、`report.READINGS_LEDGER`、`PreRegistration`。
- Produces:
  - `vcp.measure.prereg.measured_subsets(readings: ReadingsLedger, pr: PreRegistration, params_hash: str) -> list[str]`（公開，行為不變）
  - 常數 `SINGLE_MEMBER`、`CANDIDATE_MEASURED`、`VARIANT_CONFLICT`
  - `variant_id(recipe_id, member_run) -> str`（`<R>-minus-<X>`）、`admit_id(recipe_id, member_run) -> str`（`<R>-admit-<X>`）
  - `AblateSpec(dataset, recipe_id, subsets: list[str] = [], build: bool = True, preregister: bool = False, metric: str | None = None, metric_params: dict[str, str] = {}, bases: list[str] = ["valA", "valB"], t_min: float = 2.0, min_bases: int = 2, data_root, configs_root)`
  - `AblateResult(recipe_id, variants: list[str], runs: list[str], preregs: list[str], built: int, cached: int)`
  - `ablate_recipe(spec) -> AblateResult`
  - CLI `ablate`：`cmd=fuse.ablate`，欄位 `dataset= recipe= variants= runs= built= cached= preregs=`

- [ ] **Step 1: 寫失敗的測試**

`tests/unit/fuse/test_ablate.py`：

```python
import pytest

from helpers import det_with_runs
from vcp.core.errors import ValidationFailed
from vcp.fuse.ablate import (
    CANDIDATE_MEASURED,
    SINGLE_MEMBER,
    VARIANT_CONFLICT,
    AblateSpec,
    ablate_recipe,
    admit_id,
    variant_id,
)
from vcp.fuse.build import RUN_BOUND_ELSEWHERE, BuildSpec, build_run
from vcp.fuse.recipes import load_recipe, recipe_path, save_recipe
from vcp.fuse.schema import Member, Recipe
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.measure.prereg import list_preregs, load_prereg, measured_subsets, prereg_time
from vcp.measure.runs import load_run, run_dir
from vcp.measure.schema import PreRegistration

STAMP = "2026-09-05T00:00:00.000Z"


def _recipe(paths, rid="r1", members=(("perfect", 1.0), ("noisy", 0.5)), params=None):
    r = Recipe(recipe_id=rid, dataset="tiny", plan_id="fixed-v1", method="wbf", params=params or {}, members=[Member(run=a, weight=w) for a, w in members], created_at=STAMP)
    save_recipe(paths, r)
    return r


def _ablate(roots, rid="r1", **kw):
    return ablate_recipe(AblateSpec(dataset="tiny", recipe_id=rid, data_root=roots.data, configs_root=roots.configs, **kw))


def test_ids():
    assert variant_id("r1", "perfect") == "r1-minus-perfect"
    assert admit_id("r1", "perfect") == "r1-admit-perfect"


def test_ablate_writes_variants_runs_and_admission_claims(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths, params={"iou": "0.5"})
    res = _ablate(roots, preregister=True, metric="coco_map")
    assert res.variants == ["r1-minus-perfect", "r1-minus-noisy"]
    assert res.runs == ["fuse-r1", "fuse-r1-minus-perfect", "fuse-r1-minus-noisy"]
    assert res.preregs == ["r1-admit-perfect", "r1-admit-noisy"]
    assert res.built == 6 and res.cached == 0
    minus = load_recipe(paths, "r1-minus-perfect")
    assert [(m.run, m.weight) for m in minus.members] == [("noisy", 0.5)]
    assert minus.params == {"iou": "0.5"} and minus.method == "wbf" and minus.plan_id == "fixed-v1"
    for rid in res.runs:
        card = load_run(roots.data, rid)
        assert set(card.predictions) == {"valA", "valB"} and card.trained_on == ["train"]
    pr = load_prereg(paths, "r1-admit-noisy")
    assert pr.baseline_run == "fuse-r1-minus-noisy" and pr.candidate_run == "fuse-r1"
    assert pr.component == "noisy" and pr.component_class == "model" and pr.metric == "coco_map"
    assert pr.subsets == ["valA", "valB"] and pr.t_min == 2.0 and pr.min_bases == 2
    assert pr.params == {"iou": "50:95", "max_dets": "100"}  # the metric's effective params
    assert "noisy" in pr.claim and "r1" in pr.claim
    assert prereg_time(paths, "r1-admit-noisy") is not None


def test_ablate_reuses_identical_variants_and_is_cached(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    first = _ablate(roots)
    assert first.preregs == [] and first.built == 6
    again = _ablate(roots)
    assert again.built == 0 and again.cached == 6 and again.variants == first.variants
    assert list_preregs(paths) == []


def test_ablate_variant_conflict_writes_nothing(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    _recipe(paths, rid="r1-minus-noisy", members=(("perfect", 0.7),))  # same id, other weight
    with pytest.raises(ValidationFailed, match=VARIANT_CONFLICT) as ei:
        _ablate(roots)
    assert ei.value.fields == {"recipe": "r1-minus-noisy"}
    assert not recipe_path(paths, "r1-minus-perfect").exists()
    assert not run_dir(roots.data, "fuse-r1").exists()


def test_ablate_refuses_single_member_and_orphan_run(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths, rid="solo", members=(("perfect", 1.0),))
    with pytest.raises(ValidationFailed, match=SINGLE_MEMBER):
        _ablate(roots, rid="solo")
    _recipe(paths)
    _recipe(paths, rid="other", members=(("noisy", 1.0),))
    build_run(BuildSpec(dataset="tiny", recipe_id="other", run_id="fuse-r1-minus-perfect", data_root=roots.data, configs_root=roots.configs))
    with pytest.raises(ValidationFailed, match=RUN_BOUND_ELSEWHERE) as ei:
        _ablate(roots)
    assert ei.value.fields == {"run": "fuse-r1-minus-perfect"}
    assert not recipe_path(paths, "r1-minus-perfect").exists()


def test_ablate_refuses_a_measured_candidate_before_writing(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    build_run(BuildSpec(dataset="tiny", recipe_id="r1", data_root=roots.data, configs_root=roots.configs))
    measure_run(MeasureSpec(run_id="fuse-r1", metrics=["coco_map"], data_root=roots.data, configs_root=roots.configs))
    with pytest.raises(ValidationFailed, match=CANDIDATE_MEASURED) as ei:
        _ablate(roots, preregister=True, metric="coco_map")
    assert ei.value.fields == {"run": "fuse-r1"}
    assert not recipe_path(paths, "r1-minus-perfect").exists() and list_preregs(paths) == []
    # without --preregister the same ablation is fine: variants are just runs
    res = _ablate(roots)
    assert res.preregs == [] and len(res.runs) == 3


def test_ablate_preregister_checks(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    with pytest.raises(ValidationFailed, match="--metric"):
        _ablate(roots, preregister=True)
    with pytest.raises(ValidationFailed, match="not applicable"):
        _ablate(roots, preregister=True, metric="macro_auc")
    with pytest.raises(ValidationFailed, match="bases"):
        _ablate(roots, preregister=True, metric="coco_map", bases=["valA", "holdout"])
    assert list_preregs(paths) == [] and not run_dir(roots.data, "fuse-r1").exists()
    res = _ablate(roots, preregister=True, metric="coco_map", build=False)
    assert res.runs == [] and res.built == 0 and len(res.preregs) == 2
    assert not run_dir(roots.data, "fuse-r1").exists()
    with pytest.raises(ValidationFailed, match="already exists") as ei:
        _ablate(roots, preregister=True, metric="coco_map")
    assert ei.value.fields == {"prereg": "r1-admit-perfect"}


def test_measured_subsets_is_public(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    measure_run(MeasureSpec(run_id="noisy", metrics=["coco_map"], data_root=roots.data, configs_root=roots.configs))
    pr = PreRegistration(prereg_id="x", claim="c", component="k", component_class="model", baseline_run="perfect", candidate_run="noisy", metric="coco_map", params={"iou": "50:95", "max_dets": "100"}, subsets=["valA", "valB"], created_at=STAMP)
    ledger = ReadingsLedger(paths.measure_dir / "readings.jsonl")
    assert measured_subsets(ledger, pr, "iou=50:95,max_dets=100") == ["valA", "valB"]
```

`tests/unit/test_cli_fuse.py` 加：

```python
def test_fuse_ablate_cli(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    assert _recipe().exit_code == 0
    r = runner.invoke(app, ["fuse", "ablate", "--dataset", "tiny", "--recipe", "r1", "--preregister", "--metric", "coco_map", "--metric-params", "max_dets=50", "--t-min", "1.5"])
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert v.startswith("VERDICT cmd=fuse.ablate status=OK") and "variants=2" in v and "runs=3" in v
    assert "built=6" in v and "cached=0" in v and "preregs=2" in v
    prereg = yaml.safe_load((paths.prereg_dir / "r1-admit-noisy.yaml").read_text(encoding="utf-8"))
    assert prereg["params"]["max_dets"] == "50" and prereg["t_min"] == 1.5
    r = runner.invoke(app, ["fuse", "ablate", "--dataset", "tiny", "--recipe", "r1", "--no-build", "--json"])
    assert r.exit_code == 0
    payload = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert payload["fields"]["runs"] == 0 and payload["result"]["variants"] == ["r1-minus-perfect", "r1-minus-noisy"]
    r = runner.invoke(app, ["fuse", "ablate", "--dataset", "tiny", "--recipe", "r1", "--preregister"])
    assert r.exit_code == 1 and "--metric" in _last_verdict(r.output)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/fuse/test_ablate.py tests/unit/test_cli_fuse.py -o addopts="" -q`
Expected: `ModuleNotFoundError: No module named 'vcp.fuse.ablate'`；`ImportError: cannot import name 'measured_subsets'`。

- [ ] **Step 3: 公開 `measured_subsets`，寫 `ablate.py` 與 `ablate` 命令**

`src/vcp/measure/prereg.py`：把 `def _measured_subsets(` 改名為 `def measured_subsets(`，docstring 改為：

```python
    """The claimed subsets on which the candidate already has a reading for this metric.

    Public because ``vcp fuse ablate`` must ask this BEFORE it writes anything (all-or-nothing),
    while ``create_prereg`` asks it again at the moment of writing; one implementation, two callers.
    """
```

`create_prereg` 內 `measured = _measured_subsets(readings, pr, params_key(params))` 改為 `measured = measured_subsets(...)`。`grep -rn "_measured_subsets" src tests` 必須為空。

`src/vcp/fuse/ablate.py`：

```python
"""``vcp fuse ablate``: one variant per member (the recipe without it), built alongside the full
recipe, and -- with ``--preregister`` -- one admission claim per member for ``vcp eval judge``:
candidate = the full recipe, baseline = the recipe without the member (spec 6.3; postmortem 9.6).

All-or-nothing: every check runs before the first file is written, including "the candidate has
not been measured yet" -- an admission claim written after the answer is known is not a claim.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.split import load_plan
from vcp.fuse.build import (
    RUN_BOUND_ELSEWHERE,
    BuildSpec,
    build_run,
    check_existing_run,
    default_run_id,
    resolve_subsets,
)
from vcp.fuse.fusers import get_fuser, require_payload, resolve_params
from vcp.fuse.members import check_members, check_plan, union_trained_on
from vcp.fuse.recipes import load_recipe, recipe_path, recipe_sha, same_recipe, save_recipe
from vcp.fuse.schema import Member, Recipe
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.metrics import effective_params as metric_params
from vcp.measure.metrics import get_metric, params_key
from vcp.measure.prereg import create_prereg, measured_subsets, prereg_path
from vcp.measure.report import READINGS_LEDGER
from vcp.measure.runs import run_dir
from vcp.measure.schema import PreRegistration, RunCard

SINGLE_MEMBER = "single_member"
CANDIDATE_MEASURED = "candidate_measured"
VARIANT_CONFLICT = "variant_conflict"


class AblateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    recipe_id: str
    subsets: list[str] = Field(default_factory=list)
    build: bool = True
    preregister: bool = False
    metric: str | None = None
    metric_params: dict[str, str] = Field(default_factory=dict)
    bases: list[str] = Field(default_factory=lambda: ["valA", "valB"])
    t_min: float = 2.0
    min_bases: int = 2
    data_root: Path | None = None
    configs_root: Path | None = None


class AblateResult(BaseModel):
    recipe_id: str
    variants: list[str]
    runs: list[str]
    preregs: list[str]
    built: int
    cached: int


def variant_id(recipe_id: str, member_run: str) -> str:
    return f"{recipe_id}-minus-{member_run}"


def admit_id(recipe_id: str, member_run: str) -> str:
    return f"{recipe_id}-admit-{member_run}"


def _variant(recipe: Recipe, member: Member) -> Recipe:
    """The recipe without one member: same method, params, weights, order and notes."""
    return Recipe(
        recipe_id=variant_id(recipe.recipe_id, member.run),
        dataset=recipe.dataset,
        plan_id=recipe.plan_id,
        method=recipe.method,
        params=dict(recipe.params),
        members=[m for m in recipe.members if m.run != member.run],
        notes=recipe.notes,
        created_at=stamp(),
    )


def _admission(
    recipe: Recipe, member: Member, spec: AblateSpec, metric: str, params: dict[str, str]
) -> PreRegistration:
    return PreRegistration(
        prereg_id=admit_id(recipe.recipe_id, member.run),
        claim=(
            f"recipe {recipe.recipe_id}: member {member.run} contributes "
            "(fused with it beats fused without it)"
        ),
        component=member.run,
        component_class="model",
        baseline_run=default_run_id(variant_id(recipe.recipe_id, member.run)),
        candidate_run=default_run_id(recipe.recipe_id),
        metric=metric,
        params=params,
        subsets=list(spec.bases),
        t_min=spec.t_min,
        min_bases=spec.min_bases,
        created_at=stamp(),
    )


def _check_variants(
    paths: DatasetPaths, recipe: Recipe, dataset: Dataset, cards: list[RunCard]
) -> list[tuple[Recipe, bool]]:
    """Each variant's content and whether its file already exists (identical) -- or a conflict."""
    out: list[tuple[Recipe, bool]] = []
    for m in recipe.members:
        content = _variant(recipe, m)
        exists = recipe_path(paths, content.recipe_id).is_file()
        if exists and not same_recipe(load_recipe(paths, content.recipe_id), content):
            raise ValidationFailed(
                f"{VARIANT_CONFLICT}: recipe {content.recipe_id!r} already exists with different "
                "content; remove it or rename the parent recipe",
                fields={"recipe": content.recipe_id},
            )
        rid = default_run_id(content.recipe_id)
        if exists:
            keep = {x.run for x in content.members}
            check_existing_run(
                paths.data_root,
                rid,
                recipe=content,
                recipe_sha256=recipe_sha(paths, content.recipe_id),
                dataset=dataset,
                trained_on=union_trained_on([c for c in cards if c.run_id in keep]),
            )
        elif (run_dir(paths.data_root, rid) / "run.yaml").is_file():
            raise ValidationFailed(
                f"{RUN_BOUND_ELSEWHERE}: run {rid!r} exists but recipe {content.recipe_id!r} does "
                "not; a fused run without its recipe was not built by this command",
                fields={"run": rid},
            )
        out.append((content, exists))
    return out


def _check_claims(
    paths: DatasetPaths, recipe: Recipe, dataset: Dataset, spec: AblateSpec, subsets: list[str]
) -> tuple[list[PreRegistration], ReadingsLedger]:
    if not spec.metric:
        raise ValidationFailed("--preregister needs --metric")
    metric = get_metric(spec.metric)
    if dataset.card.task not in metric.tasks:
        raise ValidationFailed(
            f"metric {spec.metric!r} is not applicable to task {dataset.card.task!r}"
        )
    params = metric_params(metric, spec.metric_params)
    if spec.build:
        missing = [b for b in spec.bases if b not in subsets]
        if missing:
            raise ValidationFailed(
                f"bases {missing} are not among the subsets this ablation builds {subsets}"
            )
    readings = ReadingsLedger(paths.measure_dir / READINGS_LEDGER)
    claims: list[PreRegistration] = []
    for m in recipe.members:
        pr = _admission(recipe, m, spec, spec.metric, params)
        if prereg_path(paths, pr.prereg_id).exists():
            raise ValidationFailed(
                f"pre-registration {pr.prereg_id!r} already exists", fields={"prereg": pr.prereg_id}
            )
        measured = measured_subsets(readings, pr, params_key(params))
        if measured:
            raise ValidationFailed(
                f"{CANDIDATE_MEASURED}: candidate {pr.candidate_run!r} already has {spec.metric} "
                f"readings on {measured}; an admission claim must be written before the full "
                "recipe is measured",
                fields={"run": pr.candidate_run},
            )
        claims.append(pr)
    return claims, readings


def ablate_recipe(spec: AblateSpec) -> AblateResult:
    paths = DatasetPaths.resolve(
        spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    dataset = Dataset.load(spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root)
    recipe = load_recipe(paths, spec.recipe_id)
    if len(recipe.members) < 2:
        raise ValidationFailed(
            f"{SINGLE_MEMBER}: recipe {recipe.recipe_id!r} has one member; nothing to ablate",
            fields={"recipe": recipe.recipe_id},
        )
    plan = load_plan(paths, recipe.plan_id)
    check_plan(plan, dataset)
    fuser = get_fuser(recipe.method)
    require_payload(fuser, dataset.card.task)
    resolve_params(fuser, recipe.params)
    cards = check_members(
        recipe.members, data_root=paths.data_root, dataset=dataset, plan_id=recipe.plan_id
    )
    subsets = resolve_subsets(spec.subsets, plan, cards)
    check_existing_run(
        paths.data_root,
        default_run_id(recipe.recipe_id),
        recipe=recipe,
        recipe_sha256=recipe_sha(paths, recipe.recipe_id),
        dataset=dataset,
        trained_on=union_trained_on(cards),
    )
    variants = _check_variants(paths, recipe, dataset, cards)
    claims: list[PreRegistration] = []
    readings: ReadingsLedger | None = None
    if spec.preregister:
        claims, readings = _check_claims(paths, recipe, dataset, spec, subsets)
    # Every check has passed: variant recipes -> builds -> claims (a claim never names a run
    # that does not exist yet, unless the caller asked for --no-build).
    for content, exists in variants:
        if not exists:
            save_recipe(paths, content)
    runs: list[str] = []
    built = cached = 0
    if spec.build:
        for rid in [recipe.recipe_id, *(c.recipe_id for c, _ in variants)]:
            res = build_run(
                BuildSpec(
                    dataset=spec.dataset,
                    recipe_id=rid,
                    subsets=subsets,
                    data_root=spec.data_root,
                    configs_root=spec.configs_root,
                )
            )
            runs.append(res.run.run_id)
            built += res.built
            cached += res.cached
    for pr in claims:
        create_prereg(paths, pr, readings)  # type: ignore[arg-type]
    return AblateResult(
        recipe_id=recipe.recipe_id,
        variants=[c.recipe_id for c, _ in variants],
        runs=runs,
        preregs=[p.prereg_id for p in claims],
        built=built,
        cached=cached,
    )
```

`src/vcp/cli_fuse.py` 加（import `from vcp.fuse.ablate import AblateSpec, ablate_recipe`）：

```python
@fuse_app.command("ablate")
def ablate_cmd(
    dataset: DatasetOpt,
    recipe_id: RecipeOpt,
    subsets: Annotated[
        str | None, typer.Option("--subsets", help="comma-separated; default: common subsets")
    ] = None,
    build: Annotated[bool, typer.Option("--build/--no-build", help="build the runs")] = True,
    preregister: Annotated[
        bool, typer.Option("--preregister", help="write one admission claim per member")
    ] = False,
    metric: Annotated[str | None, typer.Option("--metric", help="metric the claims are judged on")] = None,
    metric_params: Annotated[
        list[str] | None, typer.Option("--metric-params", help="metric param key=value")
    ] = None,
    bases: Annotated[str, typer.Option("--bases", help="comma-separated bases")] = "valA,valB",
    t_min: Annotated[float, typer.Option("--t-min")] = 2.0,
    min_bases: Annotated[int, typer.Option("--min-bases")] = 2,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """One 'recipe minus member' variant per member; optionally one admission claim each."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        try:
            spec = AblateSpec(
                dataset=dataset,
                recipe_id=recipe_id,
                subsets=_csv(subsets),
                build=build,
                preregister=preregister,
                metric=metric,
                metric_params=parse_opts(metric_params, "--metric-params"),
                bases=_csv(bases),
                t_min=t_min,
                min_bases=min_bases,
                data_root=data_root,
                configs_root=configs_root,
            )
        except ValidationError as e:
            raise ValidationFailed(str(e), location="vcp fuse ablate") from e
        res = ablate_recipe(spec)
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "recipe": recipe_id,
            "variants": len(res.variants),
            "runs": len(res.runs),
            "built": res.built,
            "cached": res.cached,
            "preregs": len(res.preregs),
        }
        human = [f"variant {v}" for v in res.variants] + [f"claim {p}" for p in res.preregs]
        return "OK", fields, res.model_dump(mode="json"), human

    run_command("fuse.ablate", json_mode, data_root, fn)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/fuse tests/unit/test_cli_fuse.py tests/unit/measure/test_prereg_judge.py -o addopts="" -q`
Expected: 全部 PASS；ruff check / format 乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/measure/prereg.py src/vcp/fuse/ablate.py src/vcp/cli_fuse.py tests/unit/fuse/test_ablate.py tests/unit/test_cli_fuse.py
git commit -m "feat(fuse): ablate——每位成員一份 minus 變體與準入預登記，全有或全無；measured_subsets 公開

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: 端到端（det 三成員準入、multilabel 兩種分數融合、插件融合器）、真資料、README 與 CLAUDE.md（spec §11、§12、§10 文件）

**Files:**
- Create: `tests/unit/test_e2e_fuse.py`
- Create: `tests/integration/test_rsna_knee_fuse.py`
- Modify: `README.md:69`（在「比賽官方計分器…」段落之後、`## 匯入器與 \`rows_read\` 的語意` 之前插入一節）
- Modify: `CLAUDE.md:9`、`CLAUDE.md:16`、`CLAUDE.md:23`

**Interfaces:**
- Consumes: 全部前七個任務；`vcp.cli.app`；helpers `det_samples`、`multilabel_samples`、`perfect_predictions`、`noisy_predictions`、`write_images`、`dataset_with_perfect_run`、`ML_CATS`、`make_card`；`tests/integration/conftest.load_real`、`real_roots`。
- Produces: 無新介面；驗收條件 spec §12 全部落地。

- [ ] **Step 1: 寫端到端測試（先跑必失敗的部分只有插件融合器名稱；其餘是驗收，寫好直接跑）**

`tests/unit/test_e2e_fuse.py`：

```python
"""The whole fusion layer through the CLI alone (spec 12): three ingested members -> recipe ->
ablate --preregister -> measure x4 -> judge x3 (admission PASS for the member that matters,
FAIL for pure false positives) -> report; mean / rank_mean on a multilabel dataset; a fuser
registered by --plugin. Every step is asserted on its VERDICT line."""

import json
import random

from typer.testing import CliRunner

from helpers import (
    ML_CATS,
    dataset_with_perfect_run,
    det_samples,
    make_card,
    multilabel_samples,
    noisy_predictions,
    perfect_predictions,
    write_images,
)
from vcp.cli import app
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample, View
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.predictions import write_predictions
from vcp.measure.schema import PredBox, Prediction

runner = CliRunner()


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _json(r) -> dict:
    return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))


def _samples64(n: int, seed: int) -> list[Sample]:
    """det samples whose gold boxes sit inside [0, 8] of a 64x64 view: room for far false
    positives that can never overlap a gold box, and never get clipped."""
    return [
        s.model_copy(update={"views": [View(path=v.path, width=64, height=64) for v in s.views]})
        for s in det_samples(n, seed=seed)
    ]


def _far_fps(samples: list[Sample], *, seed: int) -> list[Prediction]:
    rng = random.Random(seed)
    out = []
    for s in samples:
        boxes = [
            PredBox(
                x=rng.uniform(40, 50),
                y=rng.uniform(40, 50),
                w=rng.uniform(1, 6),
                h=rng.uniform(1, 6),
                category_id=rng.choice([0, 1, 2]),
                score=rng.uniform(0.3, 0.98),
            )
            for _ in range(2)
        ]
        out.append(Prediction(sample_id=s.sample_id, boxes=boxes))
    return out


def _ingest(roots, tmp_path, ds, plan, run_id, maker):
    for subset in ("valA", "valB"):
        src = tmp_path / f"{run_id}-{subset}.jsonl"
        write_predictions(src, maker(ds.subset(subset, plan)))
        ingest(
            IngestSpec(
                run_id=run_id,
                dataset=ds.card.name,
                plan_id=plan.plan_id,
                subset=subset,
                format="jsonl",
                src=src,
                trained_on=["train"],
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )


def test_admission_flow_det(roots, tmp_path):
    paths = DatasetPaths.resolve("flow", data_root=roots.data, configs_root=roots.configs)
    samples = _samples64(120, seed=5)
    write_images(roots.data / "raw" / "flow", samples, size=(64, 64))
    ds = Dataset.from_parts(make_card("det", name="flow", image_root="raw/flow"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=3)
    save_plan(plan, paths)
    _ingest(roots, tmp_path, ds, plan, "good", lambda s: perfect_predictions(s, ds.card))
    _ingest(roots, tmp_path, ds, plan, "noise", lambda s: _far_fps(s, seed=11))
    _ingest(roots, tmp_path, ds, plan, "half", lambda s: noisy_predictions(s, ds.card, seed=7, flip=0.5))

    r = runner.invoke(app, ["fuse", "recipe", "--dataset", "flow", "--id", "r1", "--plan", "fixed-v1", "--method", "wbf", "--params", "iou=0.5", "--member", "good", "--member", "noise", "--member", "half:0.5"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["fuse", "ablate", "--dataset", "flow", "--recipe", "r1", "--preregister", "--metric", "coco_map"])
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "variants=3" in v and "runs=4" in v and "preregs=3" in v

    for run_id in ("fuse-r1", "fuse-r1-minus-good", "fuse-r1-minus-noise", "fuse-r1-minus-half"):
        r = runner.invoke(app, ["eval", "measure", "--run", run_id, "--metrics", "coco_map"])
        assert r.exit_code == 0, r.output
        assert "readings=2" in _verdict(r.output), _verdict(r.output)

    verdicts = {}
    for member in ("good", "noise", "half"):
        r = runner.invoke(app, ["eval", "judge", "--dataset", "flow", "--prereg", f"r1-admit-{member}", "--json"])
        assert r.exit_code == 0, r.output
        doc = _json(r)
        verdicts[member] = doc["result"]["judgement"]["verdict"]
        assert set(doc["result"]["judgement"]["per_subset"]) == {"valA", "valB"}
    assert verdicts["good"] == "PASS", verdicts
    assert verdicts["noise"] == "FAIL", verdicts
    assert verdicts["half"] in ("PASS", "FAIL"), verdicts

    r = runner.invoke(app, ["eval", "report", "--dataset", "flow", "--json"])
    assert r.exit_code == 0, r.output
    doc = _json(r)
    assert {row["prereg_id"] for row in doc["result"]["last_vs_last"]} == {"r1-admit-good", "r1-admit-noise", "r1-admit-half"}
    assert len([row for row in doc["result"]["readings"] if row["run_id"] == "fuse-r1"]) == 2

    # bit-level: the same recipe builds to the same bytes, and says so
    r = runner.invoke(app, ["fuse", "build", "--dataset", "flow", "--recipe", "r1"])
    assert r.exit_code == 0 and "cached=2" in _verdict(r.output) and "built=0" in _verdict(r.output)


def test_score_fusers_on_multilabel(roots, tmp_path):
    ds, plan, paths = dataset_with_perfect_run(roots, tmp_path, name="ml", task="multilabel", samples=multilabel_samples(60, seed=1), categories=ML_CATS, run_id="a")
    _ingest(roots, tmp_path, ds, plan, "b", lambda s: noisy_predictions(s, ds.card, seed=3))
    for rid, method in (("rm", "mean"), ("rr", "rank_mean")):
        r = runner.invoke(app, ["fuse", "recipe", "--dataset", "ml", "--id", rid, "--plan", "fixed-v1", "--method", method, "--member", "a", "--member", "b:0.5"])
        assert r.exit_code == 0, r.output
        r = runner.invoke(app, ["fuse", "build", "--dataset", "ml", "--recipe", rid])
        assert r.exit_code == 0, r.output
        assert "built=2" in _verdict(r.output)
        r = runner.invoke(app, ["eval", "measure", "--run", f"fuse-{rid}", "--metrics", "macro_auc", "--json"])
        assert r.exit_code == 0, r.output
        readings = _json(r)["result"]["readings"]
        assert len(readings) == 2 and all(0.5 <= x["value"] <= 1.0 for x in readings)
    r = runner.invoke(app, ["fuse", "recipe", "--dataset", "ml", "--id", "rw", "--plan", "fixed-v1", "--method", "wbf", "--member", "a", "--member", "b"])
    assert r.exit_code == 1 and "payload=scores" in _verdict(r.output)


def test_plugin_fuser(roots, tmp_path, monkeypatch):
    ds, plan, paths = dataset_with_perfect_run(roots, tmp_path, name="pl", task="det", samples=det_samples(30, seed=2), run_id="a")
    _ingest(roots, tmp_path, ds, plan, "b", lambda s: noisy_predictions(s, ds.card, seed=4))
    (tmp_path / "fuse_plug_e2e.py").write_text(
        "from vcp.fuse.fusers import register_fuser\n"
        "class First:\n"
        "    name = 'first'\n"
        "    version = '1'\n"
        "    payloads = frozenset({'boxes'})\n"
        "    defaults = {}\n"
        "    def check_params(self, params):\n"
        "        return None\n"
        "    def fuse(self, members, ctx):\n"
        "        ids = set(ctx.ids)\n"
        "        return [p for sid, p in members[0].predictions.items() if sid in ids]\n"
        "register_fuser(First())\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    r = runner.invoke(app, ["fuse", "recipe", "--dataset", "pl", "--id", "p1", "--plan", "fixed-v1", "--method", "first", "--member", "a", "--member", "b"])
    assert r.exit_code == 2 and "method=first" in _verdict(r.output)  # not registered without --plugin
    r = runner.invoke(app, ["fuse", "recipe", "--dataset", "pl", "--id", "p1", "--plan", "fixed-v1", "--method", "first", "--member", "a", "--member", "b", "--plugin", "fuse_plug_e2e"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["fuse", "build", "--dataset", "pl", "--recipe", "p1", "--plugin", "fuse_plug_e2e", "--json"])
    assert r.exit_code == 0, r.output
    doc = _json(r)
    assert doc["result"]["subsets"]["valA"]["samples"] > 0
```

`tests/integration/test_rsna_knee_fuse.py`：

```python
"""Fusion on the RSNA Knee subset: two perfect runs fused by rank_mean and mean stay perfect.

Nothing is written under the real data root: the fusers are called directly on in-memory
members built from the gold labels, exactly as `build` would call them.
"""

from __future__ import annotations

import pytest

from conftest import load_real
from vcp.fuse.fusers import FuseContext, MemberPredictions, get_fuser
from vcp.measure.metrics import get_metric
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import Prediction

pytestmark = pytest.mark.realdata


@pytest.fixture(scope="module")
def knee(real_roots):
    return load_real("rsna-knee", real_roots)


@pytest.mark.parametrize("method", ["rank_mean", "mean"])
def test_two_perfect_runs_fuse_to_auc_one(knee, method):
    gold = [s for s in knee.samples if s.label_source == "gold"]
    names = [c.name for c in knee.card.categories]
    perfect = predictions_by_id(
        [
            Prediction(sample_id=s.sample_id, scores={n: float(s.labels.targets[n]) for n in names})
            for s in gold
        ]
    )
    members = [MemberPredictions("a", 1.0, perfect), MemberPredictions("b", 0.5, perfect)]
    ctx = FuseContext(
        dataset=knee,
        subset="gold",
        ids=[s.sample_id for s in gold],
        samples={s.sample_id: s for s in gold},
        params={},
    )
    fused = predictions_by_id(get_fuser(method).fuse(members, ctx))
    assert set(fused) == set(perfect)
    res = get_metric("macro_auc").compute(gold, fused, knee.card, {})
    assert res.value == pytest.approx(1.0) and res.n == len(gold)
    if method == "mean":
        assert all(fused[k].scores == perfect[k].scores for k in perfect)
```

- [ ] **Step 2: 跑端到端**

Run: `uv run pytest tests/unit/test_e2e_fuse.py -o addopts="" -q` 與 `uv run pytest tests/integration/test_rsna_knee_fuse.py -o addopts="" -q -m realdata`
Expected: 前者全部 PASS（若 `verdicts["good"]` 不是 PASS 或 `verdicts["noise"]` 不是 FAIL，先看 `eval judge` 的 `reasons` 與 per_subset 的 t：這是融合或判決的 bug，不是 seed 的問題——`noise` 的框與 gold 永不重疊，Δ 必 ≤ 0）；後者在有 RSNA 資料時 PASS，否則 SKIPPED。

- [ ] **Step 3: 文件**

`README.md`：在第 69 行段落（「比賽官方計分器…`src/vcp` 不出現比賽名稱。」）之後、`## 匯入器與 \`rows_read\` 的語意` 之前插入：

````markdown
## 融合層命令 `vcp fuse`

| 命令 | 作用 | 主要選項 |
|---|---|---|
| `vcp fuse recipe` | 驗成員後把配方寫進 git（`configs/datasets/<name>/fuse/<id>.yaml`，寫了不改） | `--dataset`、`--id`、`--plan`、`--method wbf\|mean\|rank_mean`、`--params k=v`、`--member RUN[:WEIGHT]`（可重複，順序有意義）、`--notes` |
| `vcp fuse build` | 成員預測檔 → 融合 run（`runs/fuse-<id>/`，`fuse.json` 記每個成員的 sha 與輸出 sha） | `--dataset`、`--recipe`、`--run`、`--subsets`、`--replace` |
| `vcp fuse ablate` | 每位成員一份「少了它」的變體配方與 run；`--preregister` 時每位成員一份準入預登記，交給 `vcp eval judge` | `--dataset`、`--recipe`、`--subsets`、`--no-build`、`--preregister --metric M --metric-params k=v --bases valA,valB --t-min --min-bases` |

共用選項：`--json`、`--data-root`、`--configs-root`、`--plugin <module>`（自訂融合器以 `register_fuser` 登記）。融合結果就是普通 run：`trained_on` 取成員聯集、`source.framework=vcp.fuse`、`source.config_hash` = 配方檔 sha，量測與判決全用 `vcp eval`。同配方再 build 是 `cached=`；成員檔動一個位元是 FAIL `IntegrityError`；同一 run id 只綁一份配方。

### 一輪準入

```bash
uv run vcp fuse recipe --dataset D --id r1 --plan fixed-v1 --method wbf --params iou=0.6 --params min_score=0.02 --member a --member b:0.5
uv run vcp fuse ablate --dataset D --recipe r1 --preregister --metric coco_map   # 寫 r1-minus-*、建 fuse-r1 與 fuse-r1-minus-*、寫 r1-admit-*
uv run vcp eval measure --run fuse-r1 && uv run vcp eval measure --run fuse-r1-minus-a && uv run vcp eval measure --run fuse-r1-minus-b
uv run vcp eval judge --dataset D --prereg r1-admit-a    # PASS = a 證明了自己的位置；FAIL = 降權或移除，換新配方 id 再來
```

準入 = 「有它 vs 沒它」：候選是完整配方、基準是少了該成員的變體，主張 class 固定為 model。完整配方量測過就不能再寫準入預登記（`candidate_measured`），所以先 ablate 再 measure。

`wbf`（boxes）：`iou`、`skip`（輸入框門檻）、`min_score`（融合後門檻）、`max_per_image`、`conf_type=avg|max`，語意同 ensemble-boxes 的 `weighted_boxes_fusion(allows_overflow=False)` 但在像素座標運算、依 (view, category) 分群、有尺寸才裁邊。`mean`（scores / targets）加權平均；`rank_mean`（scores）以子集為母體的名次平均，AUC 型指標用、不是機率。
````

`CLAUDE.md`：
- 第 9 行「六個變異軸（任務、匯入器、匯出器、解碼器、切分策略、稽核）」改為「十個變異軸（任務、匯入器、匯出器、解碼器、切分策略、稽核、轉換器、指標、σ_p 方法、融合器）」。
- 第 16 行結尾加一句：「融合配方 `configs/datasets/<name>/fuse/<id>.yaml` 進 git、寫了不改；融合 run 是普通 run，另有 `runs/<id>/fuse.json`（每次 build 整份換寫，記每個成員預測檔的 sha 與輸出 sha）。」
- 第 23 行之後加一行：「- `uv run vcp fuse recipe --dataset D --id R --plan P --method wbf|mean|rank_mean --member RUN[:W]…` / `uv run vcp fuse ablate --dataset D --recipe R --preregister --metric M`（每位成員一份準入預登記，交給 `vcp eval judge`；先 ablate 再 measure）」

- [ ] **Step 4: 全套檢查**

Run:
```bash
uv run pytest -o addopts="" -q
uv run pytest --cov=vcp -o addopts="" -q 2>&1 | tail -n 5
uv run ruff check . && uv run ruff format --check .
uv run pytest -o addopts="" -q -W error::DeprecationWarning -W error::ResourceWarning -W error::RuntimeWarning tests/unit/fuse tests/unit/test_cli_fuse.py tests/unit/test_e2e_fuse.py
```
Expected: 全綠；覆蓋率 ≥ 80%（Plan 3 結束時 96.45%，本層應維持 ≥ 90%）；ruff 乾淨；三類警告視為錯誤下無警告。

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_e2e_fuse.py tests/integration/test_rsna_knee_fuse.py README.md CLAUDE.md
git commit -m "test(fuse): 端到端準入流程、multilabel 分數融合、插件融合器、RSNA 真資料；README 與 CLAUDE.md 加 vcp fuse

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Spec 覆蓋對照

| Spec 章節 | 任務 |
|---|---|
| §2.1 擴充點（FUSERS、`--plugin`） | 2、5–7（`--plugin` 在三個命令）、8（插件測試） |
| §4.1 配方 | 1、5 |
| §4.2 fuse.json | 1、6 |
| §4.3 融合 run 的 run.yaml | 6 |
| §5 目錄佈局 | 1、6、7 |
| §6 CLI 總表、VERDICT、`--json` | 5、6、7 |
| §6.1 子集選擇、§6.2 寫入順序 | 6 |
| §6.3 ablate 流程、§6.4 識別字 | 7 |
| §7.1 介面與登記表 | 2 |
| §7.2 wbf | 3 |
| §7.3 mean、§7.4 rank_mean、§7.5 通用規則 | 4（缺 sample / key）、3（det 缺 sample = 無框）、6（輸出過 `check_predictions`） |
| §8 成員驗證與不可變性 | 1（配方不可變）、5、6（sha、run 綁定） |
| §9 錯誤字彙 | 各任務的 `fields` 與訊息前綴 |
| §10 介面（`measured_subsets`、文件） | 7、8 |
| §11 測試策略、§12 驗收 | 各任務 + 8 |

## 自審紀錄

- 佔位掃描：無佔位字樣；每個程式碼步驟都附完整程式碼。
- 型別一致性：`check_plan` 在 Task 5 的 `members.py` 定義、Task 6 與 7 從 `vcp.fuse.members` 匯入（Task 6 檔案表原寫 `build.check_plan`，以 Task 5 介面段為準）；`resolve_params` / `require_payload` / `get_fuser` 一律從 `vcp.fuse.fusers` 匯入；`READINGS_LEDGER` 從 `vcp.measure.report` 匯入；指標的 `effective_params` 在 `ablate.py` 取別名 `metric_params` 以免與融合器的同名函式混淆。
- 已知取捨：`build_run` 在 `_write` 途中的非預期例外會留下部分檔案（spec §6.2 第 5 步已明說由下一次 build 的 `verify_prediction` 發現）；`ablate --no-build --preregister` 寫的預登記指向尚未存在的 run（spec §6.3 允許，judge 會在缺 run 時 FAIL）。
