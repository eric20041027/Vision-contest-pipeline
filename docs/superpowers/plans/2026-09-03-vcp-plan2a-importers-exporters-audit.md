# vcp Plan 2a：影像匯入器、匯出器、稽核與整合測試 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Plan 1 的資料核心上補齊「海廢形態」的資料層：五個影像匯入器（`csv_boxes`、`coco`、`yolo`、`imagefolder`、`image_csv`）、兩個匯出器（`coco`、`yolo`）、三項進場稽核（座標、近重複 / test 重疊、來源揭露）、`vcp data export|audit` 兩個命令、真實資料整合測試，以及 spec §14 指定的 Plan 1 遺留修正。

**Architecture:** 匯入器共用 `importers/common.py`（影像探索、header 尺寸、CSV 讀取），各自實作 `Importer` 協定並以 `finalize_import` 收尾；匯出器與稽核各自是登記表（`EXPORTERS`、`AUDITS`），CLI 只做參數轉換與 VERDICT。所有進 git 的路徑改以 `store_path` / `resolve_stored_path` 相對化；`finalize_import` 先驗證再算 manifest；`Dataset.subset()` 多跑一次不變量。

**Tech Stack:** Python 3.12、uv、pydantic v2、typer、Pillow（header-only 尺寸、灰階縮圖）、numpy（dHash 配對）、PyYAML（YOLO data.yaml）、pytest。不新增依賴。

**Spec:** `docs/superpowers/specs/2026-09-02-vcp-skeleton-and-data-layer-design.md`——本計畫實作 §6.1（`jsonl` 以外的五個匯入器）、§6.2、§8、§11（整合測試）、§14.1–14.4；§14.5 的 Plan 2b（`dicom`、materialize）不在本計畫。

## Global Constraints

- 全 repo 只能透過 `vcp.core.time.utc_now()` / `stamp()` 取時；ruff TID251 禁 `datetime.now` 等；ruff `line-length = 100`；`uv run ruff check .` 與 `uv run ruff format --check .` 必須乾淨（`docs/` 與 `projects/` 已排除）。
- 每個 CLI 命令結尾必輸出 `VERDICT cmd=<名> status=OK|WARN|FAIL|ABORT key=value ...`；exit OK/WARN → 0、FAIL → 1、ABORT → 2；`--json` 時結果 JSON 到 stdout、VERDICT 到 stderr；永不互動提問。
- 會被 hash 或留痕的文字檔（`samples.jsonl`、`raw_manifest.txt`、plan JSON、manifest.json、audit 輸出）一律 `newline="\n"` 寫出。
- 壞資料報位置後中止，不跳過、不猜；唯一的「跳過」是匯入器明確記錄到 `cache/import_skipped.jsonl` 並讓命令回 `WARN`。
- 路徑一律 `pathlib`，不呼叫 shell；Windows 與 Linux 皆可跑。進 git 的 card 內路徑依 §14.1.1：位於 `data_root` 之下存相對 posix 路徑，否則絕對 posix 路徑。
- 通用性：`src/vcp` 內不得出現任何比賽名稱或比賽專屬假設；所有比賽形態的差異只能落在匯入器 `--opt` 選項。
- sample_id 慣例（§14.1.4）：影像匯入器一律 `sample_id = view.path`（相對 `image_root`、含副檔名的 posix 路徑）。影像尺寸一律 Pillow 讀 header（§14.1.5）；影像缺檔 → `ValidationFailed` 列出檔名。
- 測試永不碰真實資料根目錄（`tests/conftest.py` 的 autouse fixture 已隔離）；整合測試改用 `VCP_REALDATA_ROOT` 環境變數並在資料缺席時 skip。
- 每個任務結尾 commit；conventional commits、中文描述、結尾加 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`；永不 `git add -A`（`.superpowers/` 是 scratch）。
- 執行環境 Windows 原生 PowerShell 或 Git Bash；所有命令以 `uv run ...` 開頭。若 ruff 對計畫原文報 E501，換行到 ≤100 字元不改語意；I001 用 `uv run ruff check --fix <file>`；其他規則 → 回報，不自行改邏輯。

## 檔案結構

```
src/vcp/core/paths.py                 修改：configs root 找不到即報錯；store_path / resolve_stored_path；DatasetPaths.resolve_image_root
src/vcp/data/dataset.py               修改：samples_digest；subset() 跑不變量；verify_hash 警語
src/vcp/data/split.py                 修改：SEED_STRIDE、超額訂閱訊息、normalize_keys docstring
src/vcp/data/tasks.py                 修改：regression targets 非空
src/vcp/data/importers/base.py        修改：finalize_import 先驗證再 manifest、相對路徑、plans_invalidated
src/vcp/data/importers/common.py      新增：IMAGE_EXTS、rel_posix、iter_images、image_size、make_view、read_csv、load_categories
src/vcp/data/importers/jsonl.py       修改：load_categories 改自 common 匯入；image_root 相對 src 解析
src/vcp/data/importers/csv_boxes.py   新增
src/vcp/data/importers/coco.py        新增
src/vcp/data/importers/yolo.py        新增
src/vcp/data/importers/imagefolder.py 新增
src/vcp/data/importers/image_csv.py   新增
src/vcp/data/importers/__init__.py    修改：登記六個匯入器
src/vcp/data/exporters/__init__.py    新增：登記表
src/vcp/data/exporters/base.py        新增：ExportSpec / ExportResult / Exporter / select_view / export_subset
src/vcp/data/exporters/coco.py        新增
src/vcp/data/exporters/yolo.py        新增
src/vcp/data/audit/__init__.py        新增：登記表
src/vcp/data/audit/base.py            新增：AuditOptions / CheckResult / AuditCheck / run_audit
src/vcp/data/audit/coords.py          新增
src/vcp/data/audit/dhash.py           新增：dhash64 / gray64 / pearson / 快取 / near_pairs / cross_pairs
src/vcp/data/audit/dedup.py           新增
src/vcp/data/audit/provenance.py      新增
src/vcp/cli.py                        修改：import 的 plans_invalidated WARN；新增 export、audit 命令
tests/unit/core/test_paths.py         修改
tests/unit/data/test_dataset.py       修改
tests/unit/data/test_split_generate.py 修改
tests/unit/data/test_tasks.py         修改
tests/unit/data/importers/test_jsonl.py 修改
tests/unit/data/importers/test_common.py      新增
tests/unit/data/importers/test_csv_boxes.py   新增
tests/unit/data/importers/test_coco.py        新增
tests/unit/data/importers/test_yolo.py        新增
tests/unit/data/importers/test_imagefolder_csv.py 新增
tests/unit/data/exporters/test_exporters.py   新增
tests/unit/data/audit/test_dhash.py           新增
tests/unit/data/audit/test_checks.py          新增
tests/unit/test_cli.py                修改：import WARN、export、audit 測試
tests/unit/test_e2e_flow.py           新增：import → audit → split --group-from-audit → export
tests/integration/conftest.py         新增：VCP_REALDATA_ROOT fixture
tests/integration/test_marine_debris.py 新增（realdata，資料缺席即 skip）
tests/integration/README.md           新增：真實資料準備步驟
CLAUDE.md                             修改：新命令與 VCP_REALDATA_ROOT
```

每個任務只看自己的區塊也能做：**Interfaces** 列出它消費與產出的確切名稱與簽名。Plan 1 已存在且本計畫會用到的名稱：`vcp.core.errors.{ValidationFailed, VcpError, RegistryError, InvariantError, SealedSubsetError, PlanMismatchError}`（每個都有 `.status`）、`vcp.core.hashing.{sha256_file, sha256_text, dir_manifest, write_manifest}`、`vcp.core.log.{Status, FieldValue, Verdict, worst}`、`vcp.core.time.stamp`、`vcp.core.config.{load_yaml_model, dump_yaml_model}`、`vcp.data.schema.{View, Box, Mask, Labels, Sample, Category, SourceInfo, DatasetCard, sample_json_line}`、`vcp.data.tasks.get_task`（回 `TaskSpec`，含 `label_field`）、`vcp.data.dataset.{Dataset, write_samples_jsonl, read_samples_jsonl}`、`vcp.data.split.{DEFAULT_SUBSETS, parse_subsets, build_plan, save_plan, load_plan, assert_plan_invariants}`、`vcp.data.importers.{ImportSpec, ImportResult, get_importer, register_importer, finalize_import}`、`vcp.cli.{app, run_command, parse_opts, CmdResult, JsonOpt, DataRootOpt, ConfigsRootOpt, NameOpt, data_app}`；測試側 `tests/helpers.py` 的 `CATS, det_samples, make_card, write_images` 與 `tests/conftest.py` 的 `roots` fixture（`roots.data`、`roots.configs`）。

---

### Task 1: 路徑可攜性與 Plan 1 遺留修正（paths / dataset / split / tasks）

**Files:**
- Modify: `src/vcp/core/paths.py`（整檔替換）
- Modify: `src/vcp/data/dataset.py`（整檔替換）
- Modify: `src/vcp/data/split.py`（三處片段）
- Modify: `src/vcp/data/tasks.py`（一處片段）
- Test: `tests/unit/core/test_paths.py`、`tests/unit/data/test_dataset.py`、`tests/unit/data/test_split_generate.py`、`tests/unit/data/test_tasks.py`（各追加）

**Interfaces:**
- Produces（paths）：`store_path(path: Path, data_root: Path) -> str`；`resolve_stored_path(stored: str, data_root: Path) -> Path`；`DatasetPaths.resolve_image_root(card: DatasetCard) -> Path`；`resolve_configs_root()` 找不到 repo 時拋 `ValidationFailed`（訊息含 `VCP_CONFIGS_ROOT`）。
- Produces（dataset）：`samples_digest(samples: Iterable[Sample]) -> str`（與 `write_samples_jsonl` 寫出的檔案 sha256 相同）；`Dataset.subset()` 在 hash 核對後呼叫 `assert_plan_invariants(plan, self)`。
- Produces（split）：常數 `SEED_STRIDE = 1000`；超額訂閱時 `InvariantError` 訊息含 `"exceed the eligible pool"`。
- Produces（tasks）：`regression` 的空 `targets` → `ValidationFailed`（訊息含 `"at least one"`）。

- [ ] **Step 1: 追加失敗測試**

`tests/unit/core/test_paths.py` 檔尾追加（檔頭已有 `import sys`、`from pathlib import Path`、`import pytest`、`from vcp.core import paths`、`from vcp.core.errors import ValidationFailed`；再加 `from helpers import make_card`）：

```python
def test_configs_root_errors_when_not_found(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.ENV_CONFIGS_ROOT, raising=False)
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    monkeypatch.chdir(lonely)
    with pytest.raises(ValidationFailed, match="VCP_CONFIGS_ROOT"):
        paths.resolve_configs_root()


def test_store_and_resolve_paths(tmp_path):
    root = tmp_path / "data"
    inside = root / "raw" / "ds"
    inside.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert paths.store_path(inside, root) == "raw/ds"
    assert paths.store_path(outside, root) == outside.resolve().as_posix()
    assert paths.resolve_stored_path("raw/ds", root) == root / "raw" / "ds"
    assert paths.resolve_stored_path(outside.resolve().as_posix(), root) == outside.resolve()


def test_dataset_paths_resolve_image_root(tmp_path):
    p = paths.DatasetPaths.resolve("ds1", data_root=tmp_path / "d", configs_root=tmp_path / "c")
    card = make_card("det", name="ds1", image_root="raw/ds1")
    assert p.resolve_image_root(card) == (tmp_path / "d").resolve() / "raw" / "ds1"
    absolute = (tmp_path / "abs").resolve().as_posix()
    assert p.resolve_image_root(make_card("det", name="ds1", image_root=absolute)) == Path(absolute)
```

`tests/unit/data/test_dataset.py` 檔尾追加（檔頭已 import `Dataset, read_samples_jsonl, write_samples_jsonl`、`det_samples, make_card`、`pytest`；再加 `from vcp.core.errors import InvariantError`、`from vcp.data.dataset import samples_digest`、`from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets`）：

```python
def test_samples_digest_matches_written_file(tmp_path):
    samples = det_samples(4, seed=3)
    assert samples_digest(samples) == write_samples_jsonl(tmp_path / "s.jsonl", samples)


def test_subset_rejects_tampered_plan(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(make_card("det"), det_samples(30, seed=0))
    ds.save(paths)
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    victim = next(iter(plan.ids_in("valA")))
    tampered = plan.model_copy(
        update={"assignment": {k: v for k, v in plan.assignment.items() if k != victim}}
    )
    with pytest.raises(InvariantError, match="does not cover"):
        ds.subset("valA", tampered)
```

`tests/unit/data/test_split_generate.py` 檔尾追加（檔頭已 import `InvariantError`? 若無則加 `from vcp.core.errors import InvariantError`）：

```python
def test_oversubscribed_ratios_explain_themselves():
    ds = Dataset.from_parts(make_card("det"), det_samples(15, seed=0))
    spec_text = "train:train:0.1," + ",".join(f"v{i}:eval:0.1" for i in range(9))
    with pytest.raises(InvariantError, match="exceed the eligible pool"):
        build_plan(ds, plan_id="p", subsets=parse_subsets(spec_text), seed=0)
```

`tests/unit/data/test_tasks.py` 的 `test_regression_validation_and_float_key` 內、`assert t.stratify_key(sample(Labels(targets={})), c) is None` 之前加：

```python
    with pytest.raises(ValidationFailed, match="at least one"):
        t.validate(sample(Labels(targets={})), c)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/core/test_paths.py tests/unit/data/test_dataset.py tests/unit/data/test_split_generate.py tests/unit/data/test_tasks.py -q`
Expected: 新增的 6 個測試失敗（`AttributeError: store_path`、`ImportError: samples_digest`、`InvariantError` 未拋出、regression 空 targets 未拋出）。

- [ ] **Step 3: 實作**

`src/vcp/core/paths.py` 整檔替換為：

```python
"""Where things live: data root (big, not in git) and configs root (small, in git)."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vcp.core.errors import ValidationFailed

if TYPE_CHECKING:
    from vcp.data.schema import DatasetCard

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
    raise ValidationFailed(
        f"cannot locate configs root: no ancestor of {here} contains pyproject.toml and configs/; "
        f"set {ENV_CONFIGS_ROOT} or pass --configs-root"
    )


def validate_name(name: str) -> None:
    """Dataset / plan ids become path segments; keep them boring."""
    if not _NAME_RE.match(name):
        raise ValidationFailed(
            f"invalid name {name!r}: must match {_NAME_RE.pattern} (letters, digits, . _ -)"
        )


def logs_dir(data_root: Path) -> Path:
    return data_root / "logs"


def store_path(path: Path, data_root: Path) -> str:
    """How a filesystem path is written into a git-tracked card.

    Inside ``data_root`` -> posix path relative to it (portable across machines that follow the
    data-root convention); elsewhere -> absolute posix path.
    """
    resolved = Path(path).expanduser().resolve()
    root = Path(data_root).expanduser().resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def resolve_stored_path(stored: str, data_root: Path) -> Path:
    """Inverse of ``store_path``: relative values are re-anchored at ``data_root``."""
    p = Path(stored)
    return p if p.is_absolute() else Path(data_root) / p


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

    def resolve_image_root(self, card: DatasetCard) -> Path:
        """Absolute image root for this dataset on this machine (see ``store_path``)."""
        return resolve_stored_path(card.image_root, self.data_root)
```

`src/vcp/data/dataset.py` 整檔替換為：

```python
"""Dataset = card + samples sorted by id. Every file boundary is validated and hash-checked."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import IntegrityError, PlanMismatchError, SealedSubsetError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_text
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.schema import DatasetCard, Sample, sample_json_line
from vcp.data.tasks import get_task

if TYPE_CHECKING:
    from vcp.data.split import SplitPlan


def samples_digest(samples: Iterable[Sample]) -> str:
    """sha256 of exactly the bytes ``write_samples_jsonl`` would write (sorted, LF, UTF-8)."""
    ordered = sorted(samples, key=lambda s: s.sample_id)
    return sha256_text("".join(sample_json_line(s) + "\n" for s in ordered))


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
        self.samples: tuple[Sample, ...] = tuple(sorted(samples, key=lambda s: s.sample_id))
        self._by_id: dict[str, Sample] = {}
        for s in self.samples:
            if s.sample_id in self._by_id:
                raise ValidationFailed(f"duplicate sample_id {s.sample_id!r}")
            self._by_id[s.sample_id] = s

    @property
    def by_id(self) -> Mapping[str, Sample]:
        return MappingProxyType(self._by_id)

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
        """Load a saved dataset, verifying the card and the samples file.

        ``verify_hash=False`` exists for tests only: production code must keep the hash chain
        (card.samples_hash -> samples.jsonl -> plan.dataset_hash) intact.
        """
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
        from vcp.data.split import assert_plan_invariants  # local: split imports Dataset lazily

        if plan.dataset != self.card.name:
            raise PlanMismatchError(
                f"plan {plan.plan_id!r} belongs to dataset {plan.dataset!r}, not {self.card.name!r}"
            )
        if plan.dataset_hash != self.card.samples_hash:
            raise PlanMismatchError(
                f"plan {plan.plan_id!r} was built on samples_hash {plan.dataset_hash[:12]}, "
                f"dataset now has {self.card.samples_hash[:12]}"
            )
        assert_plan_invariants(plan, self)
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

`src/vcp/data/split.py` 三處片段：

(a) 在 `QUANTILE_BINS = 10` 之後加一行：

```python
SEED_STRIDE = 1000  # per-subset seeds: seed * SEED_STRIDE + step never collide across seeds
```

(b) 把 `normalize_keys` 的單行 docstring 換成：

```python
    """Normalise raw stratify keys for ``stratified_take``.

    Vectors stay vectors (None -> zeros); a zero-width vector carries no information and falls
    through to string keys (one stratum, i.e. a plain random split); floats become quantile bins
    ``q0..q9`` (None -> "None"); everything else becomes ``str(key)``.
    """
```

(c) 在 `generate_fixed` 內把

```python
        n_take = round(sub.ratio * n_eligible)
        taken = stratified_take(pool, keys, n_take, seed=seed + step)
```

換成

```python
        n_take = round(sub.ratio * n_eligible)
        if n_take > len(pool):
            raise InvariantError(
                f"subset {sub.name!r} needs {n_take} units but only {len(pool)} remain: the rounded "
                f"subset sizes exceed the eligible pool of {n_eligible}; lower the ratios or add data"
            )
        taken = stratified_take(pool, keys, n_take, seed=seed * SEED_STRIDE + step)
```

`src/vcp/data/tasks.py` 的 `_validate_regression` 內，`_fail(sample, "task regression requires labels.targets")` 那個 `if` 之後加：

```python
    if not targets:
        _fail(sample, "task regression requires at least one target value")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest -q`
Expected: 全部通過（原 137 + 新增 6 = 143）。`test_configs_root_env_and_walk_up` 仍通過（repo 內 cwd 找得到 pyproject + configs）。

Run: `uv run ruff check .` / `uv run ruff format --check .`
Expected: 乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/core/paths.py src/vcp/data/dataset.py src/vcp/data/split.py src/vcp/data/tasks.py tests/unit/core/test_paths.py tests/unit/data/test_dataset.py tests/unit/data/test_split_generate.py tests/unit/data/test_tasks.py
git commit -m "fix(core,data): 路徑可攜性（store_path）、configs root 缺失即報錯、subset 重驗不變量、regression 非空、seed stride

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: finalize_import 重排（先驗證、相對路徑、plans_invalidated）與 CLI import WARN

**Files:**
- Modify: `src/vcp/data/importers/base.py`（整檔替換）
- Modify: `src/vcp/data/importers/jsonl.py`（`image_root` 相對 `src` 解析）
- Modify: `src/vcp/cli.py`（`import_cmd` 的 status 與 fields）
- Test: `tests/unit/data/importers/test_jsonl.py`（修改兩個斷言、追加三個測試）、`tests/unit/test_cli.py`（追加一個測試）

**Interfaces:**
- Consumes: `store_path`、`DatasetPaths.resolve_image_root`（Task 1）；`samples_digest`（Task 1）。
- Produces: `ImportResult.plans_invalidated: int = 0`；`count_invalidated_plans(paths: DatasetPaths, new_digest: str) -> int`；`finalize_import` 的行為：驗證 → 計算 `plans_invalidated` → 寫 `raw_manifest.txt` → 補 `raw_hash` → `save`；card 內 `raw_path` / `image_root` 經 `store_path`。CLI `import`：`rows_skipped > 0` 或 `plans_invalidated > 0` → `WARN`，後者帶欄位 `plans_invalidated`。

- [ ] **Step 1: 修改與追加測試**

`tests/unit/data/importers/test_jsonl.py`：

(a) `test_jsonl_import_writes_dataset` 內把 `assert ds.card.source.raw_path == str(src)` 改為 `assert ds.card.source.raw_path == src.resolve().as_posix()`。
(b) `test_jsonl_inline_categories_and_default_image_root` 內把 `assert res.dataset.card.image_root == str(src)` 改為 `assert res.dataset.card.image_root == src.resolve().as_posix()`。
(c) 檔尾追加（需要 `from vcp.core.paths import DatasetPaths`、`from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan`）：

```python
def test_paths_inside_data_root_are_stored_relative(roots):
    src = roots.data / "raw" / "ds"
    src.mkdir(parents=True)
    write_samples_jsonl(src / "samples.jsonl", det_samples(2))
    (src / "categories.json").write_text(
        json.dumps([c.model_dump() for c in CATS]), encoding="utf-8"
    )
    res = get_importer("jsonl").run(_spec(roots, src, task="det", categories="categories.json"))
    card = res.dataset.card
    assert card.source.raw_path == "raw/ds"
    assert card.image_root == "raw/ds"
    paths = DatasetPaths.resolve("ds", data_root=roots.data, configs_root=roots.configs)
    assert paths.resolve_image_root(card) == roots.data / "raw" / "ds"


def test_reimport_with_changed_samples_counts_invalidated_plans(roots, tmp_path):
    src = _src(tmp_path, det_samples(6))
    (src / "categories.json").write_text(
        json.dumps([c.model_dump() for c in CATS]), encoding="utf-8"
    )
    spec = _spec(roots, src, task="det", categories="categories.json")
    first = get_importer("jsonl").run(spec)
    assert first.plans_invalidated == 0
    paths = DatasetPaths.resolve("ds", data_root=roots.data, configs_root=roots.configs)
    plan = build_plan(first.dataset, plan_id="p1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    same = get_importer("jsonl").run(spec)
    assert same.plans_invalidated == 0
    write_samples_jsonl(src / "samples.jsonl", det_samples(7))
    changed = get_importer("jsonl").run(spec)
    assert changed.plans_invalidated == 1


def test_invalid_samples_do_not_write_manifest(roots, tmp_path):
    bad = det_samples(1)
    bad[0] = bad[0].model_copy(
        update={"labels": Labels(boxes=[Box(x=0, y=0, w=1, h=1, category_id=9)])}
    )
    src = _src(tmp_path, bad)
    with pytest.raises(ValidationFailed, match="unknown category id"):
        get_importer("jsonl").run(_spec(roots, src, task="det", categories="[]"))
    assert not (roots.data / "datasets" / "ds" / "raw_manifest.txt").exists()
```

`tests/unit/test_cli.py` 檔尾追加：

```python
def test_reimport_after_split_warns_about_invalidated_plans(roots, tmp_path):
    assert _import_tiny(roots, tmp_path).exit_code == 0
    r = runner.invoke(app, ["data", "split", "--name", "tiny", "--plan-id", "p", "--seed", "0"])
    assert r.exit_code == 0
    r = _import_tiny(roots, tmp_path, n=61)
    assert r.exit_code == 0
    v = _last_verdict(r.output)
    assert "status=WARN" in v and "plans_invalidated=1" in v
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/importers/test_jsonl.py tests/unit/test_cli.py -q`
Expected: 新增與修改的測試失敗（`raw_path` 仍是 `str(src)`、`ImportResult` 無 `plans_invalidated`、manifest 被寫出）。

- [ ] **Step 3: 實作**

`src/vcp/data/importers/base.py` 整檔替換為：

```python
"""Importer contract and registry. Every importer ends by calling ``finalize_import``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.config import load_yaml_model
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.core.hashing import dir_manifest, write_manifest
from vcp.core.paths import DatasetPaths, store_path
from vcp.core.time import stamp
from vcp.data.dataset import Dataset, samples_digest
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
    plans_invalidated: int = 0


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


def count_invalidated_plans(paths: DatasetPaths, new_digest: str) -> int:
    """Existing split plans that a re-import with a different samples_hash would orphan."""
    if not paths.card_yaml.is_file():
        return 0
    old = load_yaml_model(paths.card_yaml, DatasetCard)
    if old.samples_hash == new_digest or not paths.splits_dir.is_dir():
        return 0
    return len(list(paths.splits_dir.glob("*.json")))


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
    """Common tail of every importer: validate, then provenance, save, skip report.

    Validation runs before the raw manifest so a bad dataset never pays for hashing every raw
    file. Paths inside the data root are stored relative to it (portable cards).
    """
    paths = spec.paths()
    if not spec.src.is_dir():
        raise ValidationFailed(f"source directory not found: {spec.src}")
    source = SourceInfo(
        importer=importer.name,
        importer_version=importer.version,
        raw_path=store_path(spec.src, paths.data_root),
        raw_hash="",
        license=spec.license,
        url=spec.url,
        downloaded_at=spec.downloaded_at,
        notes=spec.notes,
    )
    card = DatasetCard(
        name=spec.name,
        task=task,
        categories=categories,
        image_root=store_path(Path(image_root), paths.data_root),
        source=source,
        created_at=stamp(),
        sample_count=len(samples),
        samples_hash="",
    )
    dataset = Dataset.from_parts(card, samples)
    plans_invalidated = count_invalidated_plans(paths, samples_digest(dataset.samples))
    raw_hash = write_manifest(dir_manifest(spec.src), paths.raw_manifest)
    dataset.card = dataset.card.model_copy(
        update={"source": source.model_copy(update={"raw_hash": raw_hash})}
    )
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
        plans_invalidated=plans_invalidated,
    )
```

`src/vcp/data/importers/jsonl.py` 的 `run` 內把 `image_root = spec.options.get("image_root", str(spec.src))` 換成：

```python
        root_opt = spec.options.get("image_root")
        root = spec.src if not root_opt else Path(root_opt)
        if not root.is_absolute():
            root = spec.src / root
        image_root = str(root)
```

`src/vcp/cli.py` 的 `import_cmd.fn` 內把 `status: Status = "WARN" if res.rows_skipped else "OK"` 換成 `status: Status = "WARN" if res.rows_skipped or res.plans_invalidated else "OK"`，並在 `if res.skipped_reasons_path is not None:` 區塊之後加：

```python
        if res.plans_invalidated:
            fields["plans_invalidated"] = res.plans_invalidated
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest -q`
Expected: 全部通過（143 + 4 = 147）。

Run: `uv run ruff check .` / `uv run ruff format --check .`
Expected: 乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/importers/base.py src/vcp/data/importers/jsonl.py src/vcp/cli.py tests/unit/data/importers/test_jsonl.py tests/unit/test_cli.py
git commit -m "feat(data): finalize_import 先驗證再算 manifest、card 路徑相對化、覆寫既有資料集時回報 plans_invalidated

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: 匯入器共用工具（common.py）

**Files:**
- Create: `src/vcp/data/importers/common.py`
- Modify: `src/vcp/data/importers/jsonl.py`（`load_categories` 改由 common 提供）
- Test: `tests/unit/data/importers/test_common.py`

**Interfaces:**
- Produces:
  - `IMAGE_EXTS: frozenset[str]`（`.jpg .jpeg .png .bmp .tif .tiff .webp`）
  - `rel_posix(path: Path, root: Path) -> str`
  - `iter_images(root: Path) -> list[Path]`（遞迴、依 posix 相對路徑排序；`root` 不存在 → `ValidationFailed`）
  - `image_size(path: Path) -> tuple[int, int]`（`(width, height)`，只讀 header；缺檔 → `ValidationFailed("image not found: ...")`；非影像 → `ValidationFailed("not a readable image: ...")`）
  - `make_view(root: Path, rel: str) -> View`
  - `read_csv(path: Path, *, required: Iterable[str]) -> tuple[list[str], list[dict[str, str]]]`（`utf-8-sig`；缺欄 → `ValidationFailed`）
  - `load_categories(value: str | None, base: Path) -> list[Category]`（原 `jsonl.py` 的實作，搬家；`jsonl.py` 改為 `from vcp.data.importers.common import load_categories`，既有測試 `from vcp.data.importers.jsonl import load_categories` 仍可用）

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/importers/test_common.py`：

```python
from pathlib import Path

import pytest
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.data.importers.common import (
    IMAGE_EXTS,
    image_size,
    iter_images,
    load_categories,
    make_view,
    read_csv,
    rel_posix,
)


def _img(path: Path, size=(12, 7)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (10, 20, 30)).save(path)


def test_iter_images_recursive_sorted_and_filtered(tmp_path):
    _img(tmp_path / "b.jpg")
    _img(tmp_path / "sub" / "a.png")
    _img(tmp_path / "A.JPG")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    found = [rel_posix(p, tmp_path) for p in iter_images(tmp_path)]
    assert found == ["A.JPG", "b.jpg", "sub/a.png"]
    assert ".jpeg" in IMAGE_EXTS
    with pytest.raises(ValidationFailed, match="image directory not found"):
        iter_images(tmp_path / "missing")


def test_image_size_reads_header_only(tmp_path):
    _img(tmp_path / "x.png", size=(31, 17))
    assert image_size(tmp_path / "x.png") == (31, 17)
    with pytest.raises(ValidationFailed, match="image not found"):
        image_size(tmp_path / "nope.png")
    (tmp_path / "fake.jpg").write_text("not an image", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="not a readable image"):
        image_size(tmp_path / "fake.jpg")


def test_make_view(tmp_path):
    _img(tmp_path / "d" / "v.jpg", size=(9, 4))
    view = make_view(tmp_path, "d/v.jpg")
    assert (view.path, view.width, view.height) == ("d/v.jpg", 9, 4)


def test_read_csv_header_bom_and_required(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("\ufeffimage,label\na.jpg,1\nb.jpg,2\n", encoding="utf-8")
    header, rows = read_csv(p, required=["image", "label"])
    assert header == ["image", "label"]
    assert rows == [{"image": "a.jpg", "label": "1"}, {"image": "b.jpg", "label": "2"}]
    with pytest.raises(ValidationFailed, match="lacks columns"):
        read_csv(p, required=["image", "score"])
    with pytest.raises(ValidationFailed, match="CSV not found"):
        read_csv(tmp_path / "none.csv", required=[])


def test_load_categories_inline_file_and_errors(tmp_path):
    assert load_categories(None, tmp_path) == []
    cats = load_categories('[{"id": 0, "name": "a"}]', tmp_path)
    assert [(c.id, c.name) for c in cats] == [(0, "a")]
    (tmp_path / "c.json").write_text('[{"id": 1, "name": "b"}]', encoding="utf-8")
    assert load_categories("c.json", tmp_path)[0].name == "b"
    with pytest.raises(ValidationFailed, match="categories file not found"):
        load_categories("missing.json", tmp_path)
    with pytest.raises(ValidationFailed, match="bad categories"):
        load_categories('[{"id": "x"}]', tmp_path)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/importers/test_common.py -q`
Expected: `ImportError`（`vcp.data.importers.common` 不存在）。

- [ ] **Step 3: 實作**

`src/vcp/data/importers/common.py`：

```python
"""Helpers shared by image-based importers: image discovery, header-only sizes, CSV reading."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.data.schema import Category, View

IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"})


def rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def iter_images(root: Path) -> list[Path]:
    """Every image file under ``root`` (recursive), sorted by posix relative path."""
    if not root.is_dir():
        raise ValidationFailed(f"image directory not found: {root}")
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return sorted(files, key=lambda p: rel_posix(p, root))


def image_size(path: Path) -> tuple[int, int]:
    """(width, height) read from the file header; pixels are never decoded."""
    try:
        with Image.open(path) as im:
            return im.size
    except FileNotFoundError:
        raise ValidationFailed(f"image not found: {path}") from None
    except UnidentifiedImageError:
        raise ValidationFailed(f"not a readable image: {path}") from None


def make_view(root: Path, rel: str) -> View:
    width, height = image_size(root / rel)
    return View(path=rel, width=width, height=height)


def read_csv(path: Path, *, required: Iterable[str]) -> tuple[list[str], list[dict[str, str]]]:
    """(header, rows) of a UTF-8 (optionally BOM-prefixed) CSV; missing columns -> error."""
    if not path.is_file():
        raise ValidationFailed(f"CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        missing = [c for c in required if c not in header]
        if missing:
            raise ValidationFailed(f"CSV {path.name} lacks columns {missing}; header = {header}")
        return header, [dict(row) for row in reader]


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
```

`src/vcp/data/importers/jsonl.py`：刪掉檔內的 `load_categories` 函式與其專用匯入（`json`、`ValidationError`、`Category`），改為 `from vcp.data.importers.common import load_categories`；其餘不變（`run` 仍呼叫 `load_categories(spec.options.get("categories"), spec.src)`）。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/data/importers -q`
Expected: 全部通過（含既有 `test_jsonl.py` 對 `load_categories` 的匯入）。

Run: `uv run ruff check .`
Expected: 乾淨（jsonl.py 不得留下未使用匯入）。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/importers/common.py src/vcp/data/importers/jsonl.py tests/unit/data/importers/test_common.py
git commit -m "feat(data): 匯入器共用工具（影像探索、header 尺寸、CSV 讀取、load_categories 搬家）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: csv_boxes 匯入器

**Files:**
- Create: `src/vcp/data/importers/csv_boxes.py`
- Modify: `src/vcp/data/importers/__init__.py`（登記）
- Test: `tests/unit/data/importers/test_csv_boxes.py`

**Interfaces:**
- Consumes: `iter_images`、`make_view`、`read_csv`、`rel_posix`、`load_categories`（Task 3）；`finalize_import`（Task 2）。
- Produces: `CsvBoxesImporter`（`name="csv_boxes"`, `version="1"`），選項：`csv`（預設 `labels.csv`）、`images`（預設 `images`）、`col_image` / `col_label` / `col_x` / `col_y` / `col_w` / `col_h`（預設 `image_filename,label_id,x,y,w,h`）、`box_format=xywh|xyxy|cxcywh`、`coords=abs|norm`、`on_bad_row=abort|skip`、`categories`（inline JSON 或檔案；缺省依出現的 label id 產生 `name=str(id)`）。純函式 `to_abs_xywh(a, b, c, d, *, box_format, coords, width, height) -> tuple[float, float, float, float]`、`box_problem(x, y, w, h, view: View) -> str | None`。壞列在 `abort` 模式 → `ValidationFailed(..., location="<csv 檔名>:<行號>")`；`skip` 模式寫入 skipped（欄位 `line`、`reason`、`row`）。影像目錄裡沒有任何列的影像 → 負樣本（`boxes=[]`）。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/importers/test_csv_boxes.py`：

```python
import json
from pathlib import Path

import pytest
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.importers.csv_boxes import box_problem, to_abs_xywh
from vcp.data.schema import View


def _img(path: Path, size=(20, 10)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (1, 2, 3)).save(path)


def _spec(roots, src, **opts):
    return ImportSpec(
        importer="csv_boxes", src=src, name="boxes", options=opts, license="CC0",
        url="https://example.org", downloaded_at="2026-09-03",
        data_root=roots.data, configs_root=roots.configs,
    )


def _src(tmp_path, csv_text: str) -> Path:
    src = tmp_path / "src"
    for name in ("a.jpg", "b.jpg", "sub/c.png"):
        _img(src / "images" / name)
    (src / "labels.csv").write_text(csv_text, encoding="utf-8")
    return src


GOOD = (
    "image_filename,label_id,x,y,w,h,confidence\n"
    "a.jpg,3,1,1,5,4,1.0\n"
    "a.jpg,7,10,2,9,7,0.9\n"
    "sub/c.png,3,0,0,20,10,1.0\n"
)


def test_import_boxes_negatives_and_derived_categories(roots, tmp_path):
    src = _src(tmp_path, GOOD)
    res = get_importer("csv_boxes").run(_spec(roots, src))
    ds = res.dataset
    assert (res.rows_read, res.rows_skipped, res.samples_written) == (3, 0, 3)
    assert [s.sample_id for s in ds.samples] == ["a.jpg", "b.jpg", "sub/c.png"]
    a = ds.by_id["a.jpg"]
    assert (a.views[0].width, a.views[0].height) == (20, 10)
    assert [(b.category_id, b.x, b.y, b.w, b.h) for b in a.labels.boxes] == [
        (3, 1.0, 1.0, 5.0, 4.0), (7, 10.0, 2.0, 9.0, 7.0),
    ]
    assert ds.by_id["b.jpg"].labels.boxes == []
    assert [(c.id, c.name) for c in ds.card.categories] == [(3, "3"), (7, "7")]
    assert ds.card.task == "det" and ds.card.image_root.endswith("src/images")


def test_explicit_categories_and_column_mapping(roots, tmp_path):
    src = _src(tmp_path, "file,cls,x1,y1,x2,y2\na.jpg,0,2,2,6,5\n")
    cats = json.dumps([{"id": 0, "name": "bottle"}, {"id": 1, "name": "net"}])
    res = get_importer("csv_boxes").run(
        _spec(
            roots, src, col_image="file", col_label="cls", col_x="x1", col_y="y1", col_w="x2",
            col_h="y2", box_format="xyxy", categories=cats,
        )
    )
    box = res.dataset.by_id["a.jpg"].labels.boxes[0]
    assert (box.x, box.y, box.w, box.h) == (2.0, 2.0, 4.0, 3.0)
    assert [c.name for c in res.dataset.card.categories] == ["bottle", "net"]


def test_bad_rows_abort_with_location_or_skip_with_reasons(roots, tmp_path):
    bad = (
        "image_filename,label_id,x,y,w,h\n"
        "a.jpg,3,1,1,5,4\n"
        "zzz.jpg,3,1,1,5,4\n"
        "a.jpg,x,1,1,5,4\n"
        "b.jpg,3,15,1,10,4\n"
        "b.jpg,3,1,1,0,4\n"
    )
    src = _src(tmp_path, bad)
    with pytest.raises(ValidationFailed, match=r"labels\.csv:3"):
        get_importer("csv_boxes").run(_spec(roots, src))
    res = get_importer("csv_boxes").run(_spec(roots, src, on_bad_row="skip"))
    assert (res.rows_read, res.rows_skipped, res.samples_written) == (5, 4, 3)
    reasons = [json.loads(line)["reason"] for line in res.skipped_reasons_path.read_text().splitlines()]
    assert any("unknown image" in r for r in reasons)
    assert any("unparsable" in r for r in reasons)
    assert any("exceeds image bounds" in r for r in reasons)
    assert any("non-positive size" in r for r in reasons)


def test_option_validation_and_missing_inputs(roots, tmp_path):
    src = _src(tmp_path, GOOD)
    with pytest.raises(ValidationFailed, match="box_format"):
        get_importer("csv_boxes").run(_spec(roots, src, box_format="wh"))
    with pytest.raises(ValidationFailed, match="CSV not found"):
        get_importer("csv_boxes").run(_spec(roots, src, csv="other.csv"))
    with pytest.raises(ValidationFailed, match="image directory not found"):
        get_importer("csv_boxes").run(_spec(roots, src, images="imgs"))


def test_to_abs_xywh_and_box_problem():
    assert to_abs_xywh(0.5, 0.5, 0.2, 0.4, box_format="cxcywh", coords="norm", width=100, height=50) == (
        40.0, 15.0, 20.0, 20.0,
    )
    assert to_abs_xywh(2, 3, 6, 9, box_format="xyxy", coords="abs", width=0, height=0) == (2, 3, 4, 6)
    assert to_abs_xywh(1, 2, 3, 4, box_format="xywh", coords="abs", width=0, height=0) == (1, 2, 3, 4)
    view = View(path="v.jpg", width=10, height=10)
    assert box_problem(0, 0, 10.9, 10, view) is None
    assert "exceeds" in box_problem(0, 0, 12, 10, view)
    assert "non-positive" in box_problem(0, 0, 0, 5, view)
    assert box_problem(-5, 0, 3, 3, View(path="v.jpg")) is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/importers/test_csv_boxes.py -q`
Expected: `ImportError`。

- [ ] **Step 3: 實作**

`src/vcp/data/importers/csv_boxes.py`：

```python
"""Generic "one row per box" CSV importer; images live in a directory next to the CSV."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vcp.core.errors import ValidationFailed
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.importers.common import (
    iter_images,
    load_categories,
    make_view,
    read_csv,
    rel_posix,
)
from vcp.data.schema import Box, Category, Labels, Sample, View

DEFAULT_COLUMNS = {
    "image": "image_filename",
    "label": "label_id",
    "x": "x",
    "y": "y",
    "w": "w",
    "h": "h",
}
BOX_FORMATS = ("xywh", "xyxy", "cxcywh")
COORD_MODES = ("abs", "norm")
BAD_ROW_MODES = ("abort", "skip")
BOUNDS_TOLERANCE_PX = 1.0


def _choice(opts: dict[str, str], key: str, allowed: tuple[str, ...], default: str) -> str:
    value = opts.get(key, default)
    if value not in allowed:
        raise ValidationFailed(f"--opt {key}= must be one of {allowed}, got {value!r}")
    return value


def to_abs_xywh(
    a: float,
    b: float,
    c: float,
    d: float,
    *,
    box_format: str,
    coords: str,
    width: int,
    height: int,
) -> tuple[float, float, float, float]:
    """Convert (a, b, c, d) in the given box format / coordinate mode to absolute xywh."""
    if coords == "norm":
        a, c = a * width, c * width
        b, d = b * height, d * height
    if box_format == "xyxy":
        return a, b, c - a, d - b
    if box_format == "cxcywh":
        return a - c / 2, b - d / 2, c, d
    return a, b, c, d


def box_problem(x: float, y: float, w: float, h: float, view: View) -> str | None:
    """Why this box is unusable, or None. Bounds are only checked when the view has a size."""
    if w <= 0 or h <= 0:
        return f"non-positive size w={w} h={h}"
    if view.width is None or view.height is None:
        return None
    tol = BOUNDS_TOLERANCE_PX
    if x < -tol or y < -tol or x + w > view.width + tol or y + h > view.height + tol:
        return f"box exceeds image bounds {view.width}x{view.height}"
    return None


class CsvBoxesImporter:
    name = "csv_boxes"
    version = "1"

    def run(self, spec: ImportSpec) -> ImportResult:
        opts = spec.options
        csv_path = spec.src / opts.get("csv", "labels.csv")
        images_dir = spec.src / opts.get("images", "images")
        cols = {k: opts.get(f"col_{k}", v) for k, v in DEFAULT_COLUMNS.items()}
        box_format = _choice(opts, "box_format", BOX_FORMATS, "xywh")
        coords = _choice(opts, "coords", COORD_MODES, "abs")
        on_bad_row = _choice(opts, "on_bad_row", BAD_ROW_MODES, "abort")
        _, rows = read_csv(csv_path, required=cols.values())
        views = {
            rel_posix(p, images_dir): make_view(images_dir, rel_posix(p, images_dir))
            for p in iter_images(images_dir)
        }
        if not views:
            raise ValidationFailed(f"no images found under {images_dir}")
        boxes: dict[str, list[Box]] = {rel: [] for rel in views}
        label_ids: set[int] = set()
        skipped: list[dict[str, Any]] = []
        for lineno, row in enumerate(rows, start=2):
            problem = _parse_row(row, cols, views, box_format=box_format, coords=coords)
            if isinstance(problem, str):
                if on_bad_row == "abort":
                    raise ValidationFailed(problem, location=f"{csv_path.name}:{lineno}")
                skipped.append({"line": lineno, "reason": problem, "row": row})
                continue
            image, box = problem
            boxes[image].append(box)
            label_ids.add(box.category_id)
        categories = load_categories(opts.get("categories"), spec.src) or [
            Category(id=i, name=str(i)) for i in sorted(label_ids)
        ]
        samples = [
            Sample(
                sample_id=rel,
                views=[view],
                labels=Labels(boxes=boxes[rel]),
                label_source="gold",
            )
            for rel, view in views.items()
        ]
        return finalize_import(
            spec=spec,
            importer=self,
            task="det",
            categories=categories,
            image_root=str(images_dir),
            samples=samples,
            rows_read=len(rows),
            skipped=skipped,
        )


def _parse_row(
    row: dict[str, str],
    cols: dict[str, str],
    views: dict[str, View],
    *,
    box_format: str,
    coords: str,
) -> tuple[str, Box] | str:
    """(image, Box) for a good row, or a problem description for a bad one."""
    image = Path(row[cols["image"]]).as_posix()
    view = views.get(image)
    if view is None:
        return f"unknown image {image!r}"
    try:
        label = int(row[cols["label"]])
        a, b, c, d = (float(row[cols[k]]) for k in ("x", "y", "w", "h"))
    except ValueError as e:
        return f"unparsable number: {e}"
    x, y, w, h = to_abs_xywh(
        a, b, c, d, box_format=box_format, coords=coords,
        width=view.width or 0, height=view.height or 0,
    )
    problem = box_problem(x, y, w, h, view)
    if problem:
        return problem
    return image, Box(x=x, y=y, w=w, h=h, category_id=label)
```

`src/vcp/data/importers/__init__.py`：在 `from vcp.data.importers.jsonl import JsonlImporter` 之後加 `from vcp.data.importers.csv_boxes import CsvBoxesImporter`，並把 `register_importer(JsonlImporter())` 之後加 `register_importer(CsvBoxesImporter())`；`__all__` 加 `"CsvBoxesImporter"`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/data/importers/test_csv_boxes.py -q`
Expected: `5 passed`（`test_bad_rows...` 的 abort 模式在第 3 列 `zzz.jpg` 停止；skip 模式 4 列跳過、3 張影像仍成為樣本）。

Run: `uv run pytest -q` / `uv run ruff check .`
Expected: 全綠、乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/importers/csv_boxes.py src/vcp/data/importers/__init__.py tests/unit/data/importers/test_csv_boxes.py
git commit -m "feat(data): csv_boxes 匯入器（欄位對照、box_format/coords、abort|skip 壞列、負樣本）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: coco 匯入器

**Files:**
- Create: `src/vcp/data/importers/coco.py`
- Modify: `src/vcp/data/importers/__init__.py`（登記）
- Test: `tests/unit/data/importers/test_coco.py`

**Interfaces:**
- Consumes: `image_size`、`rel_posix`（Task 3）；`finalize_import`（Task 2）。
- Produces: `CocoImporter`（`name="coco"`, `version="1"`），選項 `json`（預設 `instances.json`）、`images`（預設 `images`）、`task=det|seg`（預設 det）。`images[].file_name` 相對 `images` 目錄；有 `width`/`height` 就信任，否則讀 header；缺檔 → `ValidationFailed` 列出前五個；`file_name` 重複 → `ValidationFailed`。annotation 的 `image_id` 未知 → skipped（reason `unknown image_id`）；seg 模式無 `segmentation` → skipped。`sample.meta["coco_image_id"]` 保留原 id；`iscrowd=1` 進 box/mask 的 `meta`。純函式 `mask_from_segmentation(seg: Any, category_id: int, meta: dict) -> Mask | None`（polygon → `polygon`；dict 且 `counts` 為字串 → `rle=counts`、`meta["size"]`；`counts` 為 list → `rle=",".join`、`meta["rle_encoding"]="uncompressed"`）。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/importers/test_coco.py`：

```python
import json
from pathlib import Path

import pytest
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.importers.coco import mask_from_segmentation


def _img(path: Path, size=(16, 8)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (5, 5, 5)).save(path)


def _spec(roots, src, **opts):
    return ImportSpec(
        importer="coco", src=src, name="coco", options=opts, license="CC-BY",
        url="https://example.org", downloaded_at="2026-09-03",
        data_root=roots.data, configs_root=roots.configs,
    )


def _doc(seg=False):
    anns = [
        {"id": 1, "image_id": 10, "category_id": 1, "bbox": [1, 1, 4, 3], "area": 12, "iscrowd": 0},
        {"id": 2, "image_id": 10, "category_id": 2, "bbox": [6, 2, 5, 5], "area": 25, "iscrowd": 1},
        {"id": 3, "image_id": 99, "category_id": 1, "bbox": [0, 0, 1, 1], "area": 1, "iscrowd": 0},
    ]
    if seg:
        anns[0]["segmentation"] = [[1, 1, 5, 1, 5, 4]]
        anns[1]["segmentation"] = {"counts": "abc", "size": [8, 16]}
        anns.append({"id": 4, "image_id": 11, "category_id": 2, "bbox": [0, 0, 2, 2], "area": 4,
                     "iscrowd": 0, "segmentation": {"counts": [0, 3, 5], "size": [8, 16]}})
        anns.append({"id": 5, "image_id": 11, "category_id": 1, "bbox": [0, 0, 2, 2], "area": 4,
                     "iscrowd": 0})
    return {
        "images": [
            {"id": 10, "file_name": "a.jpg", "width": 16, "height": 8},
            {"id": 11, "file_name": "sub/b.jpg"},
        ],
        "annotations": anns,
        "categories": [
            {"id": 1, "name": "bottle", "supercategory": "plastic"},
            {"id": 2, "name": "net"},
        ],
    }


def _src(tmp_path, doc) -> Path:
    src = tmp_path / "src"
    _img(src / "images" / "a.jpg")
    _img(src / "images" / "sub" / "b.jpg", size=(10, 10))
    (src / "instances.json").write_text(json.dumps(doc), encoding="utf-8")
    return src


def test_import_det(roots, tmp_path):
    res = get_importer("coco").run(_spec(roots, _src(tmp_path, _doc())))
    ds = res.dataset
    assert (res.rows_read, res.rows_skipped, res.samples_written) == (3, 1, 2)
    assert [s.sample_id for s in ds.samples] == ["a.jpg", "sub/b.jpg"]
    a = ds.by_id["a.jpg"]
    assert a.meta["coco_image_id"] == 10 and (a.views[0].width, a.views[0].height) == (16, 8)
    assert [(b.category_id, b.x, b.y, b.w, b.h) for b in a.labels.boxes] == [
        (1, 1.0, 1.0, 4.0, 3.0), (2, 6.0, 2.0, 5.0, 5.0),
    ]
    assert a.labels.boxes[1].meta == {"iscrowd": 1}
    b = ds.by_id["sub/b.jpg"]
    assert (b.views[0].width, b.views[0].height) == (10, 10) and b.labels.boxes == []
    assert [(c.id, c.name, c.meta) for c in ds.card.categories] == [
        (1, "bottle", {"supercategory": "plastic"}), (2, "net", {}),
    ]
    reason = json.loads(res.skipped_reasons_path.read_text().splitlines()[0])["reason"]
    assert "unknown image_id" in reason


def test_import_seg(roots, tmp_path):
    res = get_importer("coco").run(_spec(roots, _src(tmp_path, _doc(seg=True)), task="seg"))
    ds = res.dataset
    assert ds.card.task == "seg"
    masks = ds.by_id["a.jpg"].labels.masks
    assert masks[0].polygon == [[1.0, 1.0, 5.0, 1.0, 5.0, 4.0]] and masks[0].category_id == 1
    assert masks[1].rle == "abc" and masks[1].meta == {"iscrowd": 1, "size": [8, 16]}
    b = ds.by_id["sub/b.jpg"].labels.masks
    assert b[0].rle == "0,3,5" and b[0].meta["rle_encoding"] == "uncompressed"
    assert res.rows_skipped == 2  # unknown image_id + annotation without segmentation


def test_missing_image_and_bad_task(roots, tmp_path):
    src = _src(tmp_path, _doc())
    (src / "images" / "a.jpg").unlink()
    with pytest.raises(ValidationFailed, match="images missing"):
        get_importer("coco").run(_spec(roots, src))
    with pytest.raises(ValidationFailed, match="task"):
        get_importer("coco").run(_spec(roots, _src(tmp_path / "t2", _doc()), task="cls"))


def test_mask_from_segmentation_variants():
    assert mask_from_segmentation([[0, 0, 1, 0, 1, 1]], 1, {}).polygon == [[0.0, 0.0, 1.0, 0.0, 1.0, 1.0]]
    m = mask_from_segmentation({"counts": "xyz", "size": [4, 4]}, 2, {"iscrowd": 1})
    assert m.rle == "xyz" and m.meta == {"iscrowd": 1, "size": [4, 4]}
    u = mask_from_segmentation({"counts": [1, 2], "size": [4, 4]}, 2, {})
    assert u.rle == "1,2" and u.meta == {"size": [4, 4], "rle_encoding": "uncompressed"}
    assert mask_from_segmentation(None, 1, {}) is None
    assert mask_from_segmentation([], 1, {}) is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/importers/test_coco.py -q`
Expected: `ImportError`。

- [ ] **Step 3: 實作**

`src/vcp/data/importers/coco.py`：

```python
"""COCO instances JSON importer (detection or segmentation)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vcp.core.errors import ValidationFailed
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.importers.common import image_size
from vcp.data.schema import Box, Category, Labels, Mask, Sample, View

TASKS = ("det", "seg")


def mask_from_segmentation(seg: Any, category_id: int, meta: dict[str, Any]) -> Mask | None:
    """Polygons -> ``polygon``; RLE dicts -> ``rle`` (uncompressed counts joined by commas)."""
    if isinstance(seg, list) and seg:
        return Mask(
            category_id=category_id,
            polygon=[[float(v) for v in poly] for poly in seg],
            meta=dict(meta),
        )
    if isinstance(seg, dict) and "counts" in seg:
        counts = seg["counts"]
        extra = {**meta, "size": list(seg.get("size", []))}
        if isinstance(counts, str):
            return Mask(category_id=category_id, rle=counts, meta=extra)
        extra["rle_encoding"] = "uncompressed"
        return Mask(category_id=category_id, rle=",".join(str(c) for c in counts), meta=extra)
    return None


class CocoImporter:
    name = "coco"
    version = "1"

    def run(self, spec: ImportSpec) -> ImportResult:
        opts = spec.options
        task = opts.get("task", "det")
        if task not in TASKS:
            raise ValidationFailed(f"--opt task= must be one of {TASKS}, got {task!r}")
        json_path = spec.src / opts.get("json", "instances.json")
        images_dir = spec.src / opts.get("images", "images")
        if not json_path.is_file():
            raise ValidationFailed(f"COCO json not found: {json_path}")
        doc = json.loads(json_path.read_text(encoding="utf-8"))
        for key in ("images", "annotations", "categories"):
            if key not in doc:
                raise ValidationFailed(f"COCO json lacks {key!r}", location=str(json_path))
        categories = [
            Category(
                id=int(c["id"]),
                name=str(c["name"]),
                meta={"supercategory": c["supercategory"]} if "supercategory" in c else {},
            )
            for c in doc["categories"]
        ]
        views = _load_views(doc["images"], images_dir)
        boxes: dict[int, list[Box]] = {iid: [] for iid in views}
        masks: dict[int, list[Mask]] = {iid: [] for iid in views}
        skipped: list[dict[str, Any]] = []
        for i, ann in enumerate(doc["annotations"]):
            iid = int(ann["image_id"])
            if iid not in views:
                skipped.append({"annotation": ann.get("id", i), "reason": f"unknown image_id {iid}"})
                continue
            cid = int(ann["category_id"])
            meta: dict[str, Any] = {"iscrowd": 1} if ann.get("iscrowd") else {}
            if task == "det":
                x, y, w, h = (float(v) for v in ann["bbox"])
                boxes[iid].append(Box(x=x, y=y, w=w, h=h, category_id=cid, meta=meta))
                continue
            mask = mask_from_segmentation(ann.get("segmentation"), cid, meta)
            if mask is None:
                skipped.append(
                    {"annotation": ann.get("id", i), "reason": "annotation without segmentation"}
                )
                continue
            masks[iid].append(mask)
        samples = [
            Sample(
                sample_id=rel,
                views=[view],
                labels=Labels(boxes=boxes[iid]) if task == "det" else Labels(masks=masks[iid]),
                label_source="gold",
                meta={"coco_image_id": iid},
            )
            for iid, (rel, view) in views.items()
        ]
        return finalize_import(
            spec=spec,
            importer=self,
            task=task,
            categories=categories,
            image_root=str(images_dir),
            samples=samples,
            rows_read=len(doc["annotations"]),
            skipped=skipped,
        )


def _load_views(images: list[dict[str, Any]], images_dir: Path) -> dict[int, tuple[str, View]]:
    views: dict[int, tuple[str, View]] = {}
    seen: set[str] = set()
    missing: list[str] = []
    for im in images:
        rel = Path(str(im["file_name"])).as_posix()
        if rel in seen:
            raise ValidationFailed(f"duplicate file_name {rel!r} in COCO images")
        seen.add(rel)
        path = images_dir / rel
        if not path.is_file():
            missing.append(rel)
            continue
        if "width" in im and "height" in im:
            width, height = int(im["width"]), int(im["height"])
        else:
            width, height = image_size(path)
        views[int(im["id"])] = (rel, View(path=rel, width=width, height=height))
    if missing:
        raise ValidationFailed(
            f"{len(missing)} images missing under {images_dir}: {missing[:5]}"
        )
    return views
```

`src/vcp/data/importers/__init__.py`：加 `from vcp.data.importers.coco import CocoImporter`、`register_importer(CocoImporter())`、`__all__` 加 `"CocoImporter"`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/data/importers/test_coco.py -q`
Expected: `4 passed`。

Run: `uv run pytest -q` / `uv run ruff check .`
Expected: 全綠、乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/importers/coco.py src/vcp/data/importers/__init__.py tests/unit/data/importers/test_coco.py
git commit -m "feat(data): coco 匯入器（det/seg、polygon 與 RLE 原樣、未知 image_id 跳過並 WARN）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: yolo 匯入器

**Files:**
- Create: `src/vcp/data/importers/yolo.py`
- Modify: `src/vcp/data/importers/__init__.py`（登記）
- Test: `tests/unit/data/importers/test_yolo.py`

**Interfaces:**
- Consumes: `iter_images`、`make_view`、`rel_posix`（Task 3）；`finalize_import`（Task 2）。
- Produces: `YoloImporter`（`name="yolo"`, `version="1"`），選項 `images`（預設 `images`）、`labels`（預設 `labels`）、`names`（預設 `classes.txt`；`.yaml`/`.yml` 則讀 `names`，list 或 `{id: name}` dict）。標籤檔 `labels/<影像相對路徑去副檔名>.txt`，每列 `cls cx cy w h`（正規化）；缺標籤檔 → 負樣本；列欄數 ≠ 5 或類別未知或非數字 → `ValidationFailed(location="<label 檔>:<行>")`。純函式 `load_names(path: Path) -> list[Category]`。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/importers/test_yolo.py`：

```python
from pathlib import Path

import pytest
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.importers.yolo import load_names


def _img(path: Path, size=(100, 50)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (7, 7, 7)).save(path)


def _spec(roots, src, **opts):
    return ImportSpec(
        importer="yolo", src=src, name="yolo", options=opts, license="CC0",
        url="https://example.org", downloaded_at="2026-09-03",
        data_root=roots.data, configs_root=roots.configs,
    )


def _src(tmp_path) -> Path:
    src = tmp_path / "src"
    _img(src / "images" / "a.jpg")
    _img(src / "images" / "sub" / "b.jpg")
    _img(src / "images" / "c.jpg")
    (src / "labels").mkdir()
    (src / "labels" / "a.txt").write_text("0 0.5 0.5 0.2 0.4\n1 0.1 0.1 0.2 0.2\n", encoding="utf-8")
    (src / "labels" / "sub").mkdir()
    (src / "labels" / "sub" / "b.txt").write_text("\n", encoding="utf-8")
    (src / "classes.txt").write_text("cat\ndog\n", encoding="utf-8")
    return src


def test_import_yolo(roots, tmp_path):
    res = get_importer("yolo").run(_spec(roots, _src(tmp_path)))
    ds = res.dataset
    assert [s.sample_id for s in ds.samples] == ["a.jpg", "c.jpg", "sub/b.jpg"]
    a = ds.by_id["a.jpg"].labels.boxes
    assert [(b.category_id, b.x, b.y, b.w, b.h) for b in a] == [
        (0, 40.0, 15.0, 20.0, 20.0), (1, 0.0, 0.0, 20.0, 10.0),
    ]
    assert ds.by_id["c.jpg"].labels.boxes == []
    assert ds.by_id["sub/b.jpg"].labels.boxes == []
    assert [(c.id, c.name) for c in ds.card.categories] == [(0, "cat"), (1, "dog")]
    assert res.rows_read == 3 and res.rows_skipped == 0


def test_names_from_yaml_and_errors(roots, tmp_path):
    src = _src(tmp_path)
    (src / "data.yaml").write_text("names:\n  0: cat\n  1: dog\n", encoding="utf-8")
    res = get_importer("yolo").run(_spec(roots, src, names="data.yaml"))
    assert [c.name for c in res.dataset.card.categories] == ["cat", "dog"]
    (src / "list.yaml").write_text("names: [x, y, z]\n", encoding="utf-8")
    assert [c.name for c in load_names(src / "list.yaml")] == ["x", "y", "z"]
    with pytest.raises(ValidationFailed, match="names file not found"):
        load_names(src / "nope.txt")
    (src / "labels" / "a.txt").write_text("5 0.5 0.5 0.2 0.4\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="unknown class 5"):
        get_importer("yolo").run(_spec(roots, src))
    (src / "labels" / "a.txt").write_text("0 0.5 0.5 0.2\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match=r"a\.txt:1"):
        get_importer("yolo").run(_spec(roots, src))
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/importers/test_yolo.py -q`
Expected: `ImportError`。

- [ ] **Step 3: 實作**

`src/vcp/data/importers/yolo.py`：

```python
"""YOLO layout importer: images/, labels/<same relpath>.txt with normalised cxcywh rows."""

from __future__ import annotations

from pathlib import Path

import yaml

from vcp.core.errors import ValidationFailed
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.importers.common import iter_images, make_view, rel_posix
from vcp.data.schema import Box, Category, Labels, Sample, View


def load_names(path: Path) -> list[Category]:
    """classes.txt (one name per line) or a data.yaml whose ``names`` is a list or {id: name}."""
    if not path.is_file():
        raise ValidationFailed(f"names file not found: {path}")
    if path.suffix.lower() in (".yaml", ".yml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        names = doc.get("names")
        if isinstance(names, dict):
            return [Category(id=int(k), name=str(v)) for k, v in sorted(names.items())]
        if isinstance(names, list):
            return [Category(id=i, name=str(n)) for i, n in enumerate(names)]
        raise ValidationFailed(f"{path.name} has no usable 'names' entry")
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    return [Category(id=i, name=n) for i, n in enumerate(ln for ln in lines if ln)]


def _parse_label_file(path: Path, view: View, known: set[int]) -> list[Box]:
    boxes: list[Box] = []
    width, height = view.width or 0, view.height or 0
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 5:
            raise ValidationFailed(
                f"expected 'cls cx cy w h', got {line!r} (YOLO segmentation labels are not boxes)",
                location=f"{path}:{lineno}",
            )
        try:
            cls = int(parts[0])
            cx, cy, w, h = (float(v) for v in parts[1:])
        except ValueError as e:
            raise ValidationFailed(f"unparsable number: {e}", location=f"{path}:{lineno}") from e
        if cls not in known:
            raise ValidationFailed(f"unknown class {cls}", location=f"{path}:{lineno}")
        boxes.append(
            Box(x=(cx - w / 2) * width, y=(cy - h / 2) * height, w=w * width, h=h * height,
                category_id=cls)
        )
    return boxes


class YoloImporter:
    name = "yolo"
    version = "1"

    def run(self, spec: ImportSpec) -> ImportResult:
        opts = spec.options
        images_dir = spec.src / opts.get("images", "images")
        labels_dir = spec.src / opts.get("labels", "labels")
        categories = load_names(spec.src / opts.get("names", "classes.txt"))
        known = {c.id for c in categories}
        samples: list[Sample] = []
        for p in iter_images(images_dir):
            rel = rel_posix(p, images_dir)
            view = make_view(images_dir, rel)
            label_file = labels_dir / Path(rel).with_suffix(".txt")
            boxes = _parse_label_file(label_file, view, known) if label_file.is_file() else []
            samples.append(
                Sample(sample_id=rel, views=[view], labels=Labels(boxes=boxes), label_source="gold")
            )
        return finalize_import(
            spec=spec,
            importer=self,
            task="det",
            categories=categories,
            image_root=str(images_dir),
            samples=samples,
            rows_read=len(samples),
            skipped=[],
        )
```

`src/vcp/data/importers/__init__.py`：加 `from vcp.data.importers.yolo import YoloImporter`、`register_importer(YoloImporter())`、`__all__` 加 `"YoloImporter"`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/data/importers/test_yolo.py -q`
Expected: `2 passed`（`a.jpg` 100×50：`0 0.5 0.5 0.2 0.4` → x=40, y=15, w=20, h=20；`1 0.1 0.1 0.2 0.2` → x=0, y=0, w=20, h=10）。

Run: `uv run pytest -q` / `uv run ruff check .`
Expected: 全綠、乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/importers/yolo.py src/vcp/data/importers/__init__.py tests/unit/data/importers/test_yolo.py
git commit -m "feat(data): yolo 匯入器（classes.txt / data.yaml 類別、正規化 cxcywh → 絕對 xywh、缺標籤即負樣本）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: imagefolder 與 image_csv 匯入器

**Files:**
- Create: `src/vcp/data/importers/imagefolder.py`, `src/vcp/data/importers/image_csv.py`
- Modify: `src/vcp/data/importers/__init__.py`（登記）
- Test: `tests/unit/data/importers/test_imagefolder_csv.py`

**Interfaces:**
- Consumes: `iter_images`、`make_view`、`read_csv`、`rel_posix`（Task 3）；`finalize_import`（Task 2）。
- Produces:
  - `ImageFolderImporter`（`name="imagefolder"`, `version="1"`），選項 `root`（相對 `src`，預設 `.`）；類別 = `root` 下的子目錄名排序，`id` = 序號；`sample_id = "<class>/<file>"`；`task="cls"`。
  - `ImageCsvImporter`（`name="image_csv"`, `version="1"`），選項 `csv`（預設 `labels.csv`）、`images`（預設 `images`）、`path_col`（預設 `path`）、`target_cols`（逗號分隔；缺省 = 除 `path_col`/`gold_col` 外的所有欄）、`task=cls|multilabel|regression`（缺省自動判定）、`gold_col`（0/1 欄；缺省全 gold，`0` → `derived`）。純函式 `infer_task(rows, cols) -> str`（單欄且全整數 → `cls`；全欄值 ∈ {0,1} → `multilabel`；否則 `regression`）。`cls` 類別：欄值去重排序（數值優先數值序，否則字串序），`id` = 序號、`name = str(值)`。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/importers/test_imagefolder_csv.py`：

```python
from pathlib import Path

import pytest
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.importers.image_csv import infer_task


def _img(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (9, 9, 9)).save(path)


def _spec(roots, importer, src, **opts):
    return ImportSpec(
        importer=importer, src=src, name="ds", options=opts, license="CC0",
        url="https://example.org", downloaded_at="2026-09-03",
        data_root=roots.data, configs_root=roots.configs,
    )


def test_imagefolder(roots, tmp_path):
    src = tmp_path / "src"
    for rel in ("dog/1.jpg", "cat/2.png", "cat/3.jpg"):
        _img(src / rel)
    (src / "README.txt").write_text("ignored", encoding="utf-8")
    res = get_importer("imagefolder").run(_spec(roots, "imagefolder", src))
    ds = res.dataset
    assert ds.card.task == "cls"
    assert [(c.id, c.name) for c in ds.card.categories] == [(0, "cat"), (1, "dog")]
    assert {s.sample_id: s.labels.cls for s in ds.samples} == {
        "cat/2.png": 0, "cat/3.jpg": 0, "dog/1.jpg": 1,
    }
    with pytest.raises(ValidationFailed, match="no class directories"):
        get_importer("imagefolder").run(_spec(roots, "imagefolder", src, root="cat"))


def _csv_src(tmp_path, text: str) -> Path:
    src = tmp_path / "src"
    for rel in ("a.jpg", "b.jpg", "c.jpg"):
        _img(src / "images" / rel)
    (src / "labels.csv").write_text(text, encoding="utf-8")
    return src


def test_image_csv_cls_auto(roots, tmp_path):
    src = _csv_src(tmp_path, "path,label,is_gold\na.jpg,2,1\nb.jpg,10,0\nc.jpg,2,1\n")
    res = get_importer("image_csv").run(_spec(roots, "image_csv", src, gold_col="is_gold"))
    ds = res.dataset
    assert ds.card.task == "cls"
    assert [(c.id, c.name) for c in ds.card.categories] == [(0, "2"), (1, "10")]
    assert ds.by_id["a.jpg"].labels.cls == 0 and ds.by_id["b.jpg"].labels.cls == 1
    assert ds.by_id["b.jpg"].label_source == "derived" and ds.by_id["a.jpg"].label_source == "gold"


def test_image_csv_multilabel_and_regression(roots, tmp_path):
    src = _csv_src(tmp_path, "path,acl,mcl\na.jpg,1,0\nb.jpg,0,0\nc.jpg,1,1\n")
    res = get_importer("image_csv").run(_spec(roots, "image_csv", src))
    assert res.dataset.card.task == "multilabel"
    assert res.dataset.by_id["c.jpg"].labels.targets == {"acl": 1.0, "mcl": 1.0}
    assert [c.name for c in res.dataset.card.categories] == ["acl", "mcl"]
    src2 = _csv_src(tmp_path / "r", "path,age,note\na.jpg,37.5,x\nb.jpg,4,y\nc.jpg,0.25,z\n")
    res2 = get_importer("image_csv").run(
        _spec(roots, "image_csv", src2, target_cols="age", task="regression")
    )
    assert res2.dataset.card.task == "regression"
    assert res2.dataset.by_id["a.jpg"].labels.targets == {"age": 37.5}
    assert res2.dataset.by_id["a.jpg"].meta == {"note": "x"}


def test_image_csv_errors(roots, tmp_path):
    src = _csv_src(tmp_path, "path,label\na.jpg,1\nmissing.jpg,2\n")
    with pytest.raises(ValidationFailed, match="image not found"):
        get_importer("image_csv").run(_spec(roots, "image_csv", src))
    src2 = _csv_src(tmp_path / "t", "path,label\na.jpg,1\n")
    with pytest.raises(ValidationFailed, match="task"):
        get_importer("image_csv").run(_spec(roots, "image_csv", src2, task="pose"))
    with pytest.raises(ValidationFailed, match="no target columns"):
        get_importer("image_csv").run(_spec(roots, "image_csv", src2, target_cols=""))
    src3 = _csv_src(tmp_path / "u", "path,score\na.jpg,abc\n")
    with pytest.raises(ValidationFailed, match=r"labels\.csv:2"):
        get_importer("image_csv").run(_spec(roots, "image_csv", src3, task="regression"))


def test_infer_task():
    assert infer_task([{"l": "1"}, {"l": "3"}], ["l"]) == "cls"
    assert infer_task([{"a": "1", "b": "0"}, {"a": "0", "b": "1"}], ["a", "b"]) == "multilabel"
    assert infer_task([{"a": "0.5"}], ["a"]) == "regression"
    assert infer_task([{"a": "1"}, {"a": "0"}], ["a"]) == "cls"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/importers/test_imagefolder_csv.py -q`
Expected: `ImportError`。

- [ ] **Step 3: 實作**

`src/vcp/data/importers/imagefolder.py`：

```python
"""root/<class>/*.jpg -> single-label classification dataset."""

from __future__ import annotations

from vcp.core.errors import ValidationFailed
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.importers.common import iter_images, make_view, rel_posix
from vcp.data.schema import Category, Labels, Sample


class ImageFolderImporter:
    name = "imagefolder"
    version = "1"

    def run(self, spec: ImportSpec) -> ImportResult:
        root = (spec.src / spec.options.get("root", ".")).resolve()
        if not root.is_dir():
            raise ValidationFailed(f"image folder root not found: {root}")
        class_dirs = sorted(p for p in root.iterdir() if p.is_dir())
        if not class_dirs:
            raise ValidationFailed(f"no class directories under {root}")
        categories = [Category(id=i, name=d.name) for i, d in enumerate(class_dirs)]
        samples: list[Sample] = []
        for cid, d in enumerate(class_dirs):
            for p in iter_images(d):
                rel = rel_posix(p, root)
                samples.append(
                    Sample(
                        sample_id=rel,
                        views=[make_view(root, rel)],
                        labels=Labels(cls=cid),
                        label_source="gold",
                    )
                )
        return finalize_import(
            spec=spec,
            importer=self,
            task="cls",
            categories=categories,
            image_root=str(root),
            samples=samples,
            rows_read=len(samples),
            skipped=[],
        )
```

`src/vcp/data/importers/image_csv.py`：

```python
"""CSV of image paths + label / target columns -> cls, multilabel or regression dataset."""

from __future__ import annotations

import re
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.importers.common import make_view, read_csv
from vcp.data.schema import Category, Labels, Sample

TASKS = ("cls", "multilabel", "regression")
_INT = re.compile(r"^-?\d+$")
_TRUE = {"1", "true", "yes"}


def infer_task(rows: list[dict[str, str]], cols: list[str]) -> str:
    values = [row[c].strip() for row in rows for c in cols]
    if len(cols) == 1 and all(_INT.match(v) for v in values):
        return "cls"
    if all(v in ("0", "1", "0.0", "1.0") for v in values):
        return "multilabel"
    return "regression"


def _cls_categories(values: list[str]) -> list[Category]:
    distinct = sorted(set(values), key=lambda v: (0, int(v)) if _INT.match(v) else (1, v))
    return [Category(id=i, name=v) for i, v in enumerate(distinct)]


class ImageCsvImporter:
    name = "image_csv"
    version = "1"

    def run(self, spec: ImportSpec) -> ImportResult:
        opts = spec.options
        csv_path = spec.src / opts.get("csv", "labels.csv")
        images_dir = spec.src / opts.get("images", "images")
        path_col = opts.get("path_col", "path")
        gold_col = opts.get("gold_col")
        header, rows = read_csv(csv_path, required=[path_col, *([gold_col] if gold_col else [])])
        if "target_cols" in opts:
            target_cols = [c.strip() for c in opts["target_cols"].split(",") if c.strip()]
        else:
            target_cols = [c for c in header if c not in (path_col, gold_col)]
        if not target_cols:
            raise ValidationFailed("no target columns: pass --opt target_cols=a,b,...")
        missing = [c for c in target_cols if c not in header]
        if missing:
            raise ValidationFailed(f"target columns {missing} not in CSV header {header}")
        task = opts.get("task") or infer_task(rows, target_cols)
        if task not in TASKS:
            raise ValidationFailed(f"--opt task= must be one of {TASKS}, got {task!r}")
        if task == "cls" and len(target_cols) != 1:
            raise ValidationFailed(f"task cls needs exactly one target column, got {target_cols}")
        categories = (
            _cls_categories([row[target_cols[0]].strip() for row in rows])
            if task == "cls"
            else [Category(id=i, name=c) for i, c in enumerate(target_cols)]
        )
        cls_index = {c.name: c.id for c in categories}
        meta_cols = [c for c in header if c not in (path_col, gold_col, *target_cols)]
        samples: list[Sample] = []
        for lineno, row in enumerate(rows, start=2):
            rel = Path(row[path_col].strip()).as_posix()
            view = make_view(images_dir, rel)
            labels = _labels_for(row, task, target_cols, cls_index, csv_path, lineno)
            gold = gold_col is None or row[gold_col].strip().lower() in _TRUE
            samples.append(
                Sample(
                    sample_id=rel,
                    views=[view],
                    labels=labels,
                    label_source="gold" if gold else "derived",
                    meta={c: row[c] for c in meta_cols},
                )
            )
        return finalize_import(
            spec=spec,
            importer=self,
            task=task,
            categories=categories,
            image_root=str(images_dir),
            samples=samples,
            rows_read=len(rows),
            skipped=[],
        )


def _labels_for(
    row: dict[str, str],
    task: str,
    target_cols: list[str],
    cls_index: dict[str, int],
    csv_path: Path,
    lineno: int,
) -> Labels:
    if task == "cls":
        return Labels(cls=cls_index[row[target_cols[0]].strip()])
    try:
        targets = {c: float(row[c]) for c in target_cols}
    except ValueError as e:
        raise ValidationFailed(f"unparsable target: {e}", location=f"{csv_path.name}:{lineno}") from e
    return Labels(targets=targets)
```

`src/vcp/data/importers/__init__.py`：加 `ImageFolderImporter`、`ImageCsvImporter` 的匯入、登記與 `__all__`。完成後六個匯入器登記順序：jsonl、csv_boxes、coco、yolo、imagefolder、image_csv。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/data/importers/test_imagefolder_csv.py -q`
Expected: `5 passed`。

Run: `uv run pytest -q` / `uv run ruff check .`
Expected: 全綠、乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/importers/imagefolder.py src/vcp/data/importers/image_csv.py src/vcp/data/importers/__init__.py tests/unit/data/importers/test_imagefolder_csv.py
git commit -m "feat(data): imagefolder 與 image_csv 匯入器（cls/multilabel/regression 自動判定、gold_col）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: 匯出器（coco、yolo）與 `vcp data export`

**Files:**
- Create: `src/vcp/data/exporters/__init__.py`, `src/vcp/data/exporters/base.py`, `src/vcp/data/exporters/coco.py`, `src/vcp/data/exporters/yolo.py`
- Modify: `src/vcp/cli.py`（新增 `export` 命令）、`tests/helpers.py`（`write_images` 改為每張圖不同的隨機像素，讓後續稽核的 dHash 有意義）
- Test: `tests/unit/data/exporters/test_exporters.py`、`tests/unit/test_cli.py`（追加）

`tests/helpers.py` 的 `write_images` 整個函式替換為：

```python
def write_images(directory: Path, samples: list[Sample], size: tuple[int, int] = (8, 8)) -> None:
    """One distinct random-pixel image per view (seeded by sample index), so perceptual hashes differ."""
    directory.mkdir(parents=True, exist_ok=True)
    for i, s in enumerate(samples):
        rng = random.Random(i)
        for v in s.views:
            img = Image.new("RGB", size)
            img.putdata(
                [
                    (rng.randrange(256), rng.randrange(256), rng.randrange(256))
                    for _ in range(size[0] * size[1])
                ]
            )
            (directory / v.path).parent.mkdir(parents=True, exist_ok=True)
            img.save(directory / v.path)
```

**Interfaces:**
- Consumes: `Dataset.load` / `Dataset.subset`（Task 1）；`DatasetPaths.resolve_image_root`（Task 1）；`load_plan`；`sha256_file`；`stamp`；`rel_posix`（Task 3）。
- Produces（base）：
  - `ExportSpec(BaseModel)`：`name, plan_id, subset, format, out: Path, options: dict[str, str] = {}, unseal: bool = False, reason: str | None = None, data_root, configs_root`；`paths()`。
  - `ExportResult(BaseModel)`：`out: Path, manifest_path: Path, files: int, warnings: list[str]`。
  - `Exporter(Protocol)`：`name`, `version`, `run(dataset: Dataset, samples: list[Sample], out: Path, image_root: Path, options: dict[str, str]) -> tuple[list[Path], list[str]]`（寫出的檔案、警告）。
  - `EXPORTERS`、`register_exporter`、`get_exporter`（未知 → `RegistryError`）。
  - `select_view(sample: Sample, view_opt: str | None) -> tuple[int, View]`（未指定且多 view → `ValidationFailed`；數字 = 索引；否則比對 `role`）。
  - `export_subset(spec: ExportSpec) -> ExportResult`：載入資料集與 plan → `subset(..., caller="vcp data export", paths=paths)` → 輸出目錄必須不存在或為空 → exporter → `manifest.json`（`dataset, samples_hash, plan_id, subset, format, exporter_version, exported_at, sample_count, files: {相對路徑: sha256}`，indent=1、LF）。
- Produces（coco）：`CocoExporter`（`name="coco"`）：`instances.json`；det → bbox/area；seg → segmentation（polygon 原樣；rle 依 `meta["rle_encoding"]` 還原 dict）；PNG path 遮罩無法匯出 → 警告並略過；非匯出 view 上的標註略過並計入警告。
- Produces（yolo）：`YoloExporter`（`name="yolo"`）：`images/`（巢狀路徑以 `__` 攤平）、`labels/`、`data.yaml`（`path` 絕對、`train: images`、`val: images`、`names: {index: name}`）；影像預設 symlink，失敗退回複製並加警告 `"symlink not permitted; images were copied"`；`--opt copy=true` 強制複製；class index 依 `card.categories` 宣告序。
- CLI：`vcp data export --name --plan --subset --format --out [--opt k=v]* [--unseal --reason ...] [--json] [--data-root] [--configs-root]`；VERDICT 欄位 `name, plan, subset, format, files, out`，有警告 → `WARN` 並帶 `warnings`。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/exporters/test_exporters.py`：

```python
import json
from pathlib import Path

import pytest
import yaml
from helpers import CATS, det_samples, make_card, write_images

from vcp.core.errors import RegistryError, SealedSubsetError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.exporters import get_exporter
from vcp.data.exporters.base import ExportSpec, export_subset, select_view
from vcp.data.schema import Labels, Mask, Sample, View
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan


@pytest.fixture
def det_ds(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(40, seed=0)
    image_root = roots.data / "raw" / "tiny"
    write_images(image_root, samples)
    ds = Dataset.from_parts(make_card("det", image_root="raw/tiny"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def _spec(roots, fmt, out, subset="valA", **kw):
    return ExportSpec(
        name="tiny", plan_id="fixed-v1", subset=subset, format=fmt, out=out,
        data_root=roots.data, configs_root=roots.configs, **kw,
    )


def test_registry_and_select_view():
    assert get_exporter("coco").name == "coco" and get_exporter("yolo").name == "yolo"
    with pytest.raises(RegistryError):
        get_exporter("nope")
    single = det_samples(1)[0]
    assert select_view(single, None) == (0, single.views[0])
    multi = Sample(
        sample_id="m", label_source="none",
        views=[View(path="a.jpg", role="rgb"), View(path="b.jpg", role="nir")],
    )
    assert select_view(multi, "1")[1].path == "b.jpg"
    assert select_view(multi, "nir")[1].path == "b.jpg"
    with pytest.raises(ValidationFailed, match="views"):
        select_view(multi, None)
    with pytest.raises(ValidationFailed):
        select_view(multi, "depth")


def test_export_coco_det(roots, tmp_path, det_ds):
    ds, plan, _ = det_ds
    out = tmp_path / "coco_out"
    res = export_subset(_spec(roots, "coco", out))
    doc = json.loads((out / "instances.json").read_text(encoding="utf-8"))
    ids = sorted(plan.ids_in("valA"))
    assert [im["file_name"] for im in doc["images"]] == ids
    assert [im["sample_id"] for im in doc["images"]] == ids
    assert all(im["width"] == 8 and im["height"] == 8 for im in doc["images"])
    expected_boxes = sum(len(ds.by_id[i].labels.boxes) for i in ids)
    assert len(doc["annotations"]) == expected_boxes
    first = doc["annotations"][0]
    assert set(first) >= {"id", "image_id", "category_id", "bbox", "area", "iscrowd"}
    assert [c["name"] for c in doc["categories"]] == [c.name for c in CATS]
    manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
    assert manifest["subset"] == "valA" and manifest["plan_id"] == "fixed-v1"
    assert manifest["samples_hash"] == ds.card.samples_hash
    assert set(manifest["files"]) == {"instances.json"} and res.files == 1
    assert manifest["exported_at"].endswith("Z") and res.warnings == []


def test_export_coco_seg_rle_roundtrip(roots, tmp_path):
    paths = DatasetPaths.resolve("seg", data_root=roots.data, configs_root=roots.configs)
    samples = [
        Sample(
            sample_id=f"s{i}.jpg", views=[View(path=f"s{i}.jpg", width=8, height=8)],
            label_source="gold",
            labels=Labels(masks=[
                Mask(category_id=0, polygon=[[0, 0, 4, 0, 4, 4]]),
                Mask(category_id=1, rle="1,2,3", meta={"size": [8, 8], "rle_encoding": "uncompressed"}),
                Mask(category_id=1, rle="abc", meta={"size": [8, 8]}),
                Mask(category_id=2, path="m.png"),
            ]),
        )
        for i in range(4)
    ]
    ds = Dataset.from_parts(make_card("seg", name="seg", image_root="raw/seg"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets("train:train:0.5,val:eval:0.5"), seed=0)
    save_plan(plan, paths)
    out = tmp_path / "seg_out"
    res = export_subset(ExportSpec(name="seg", plan_id="p", subset="val", format="coco", out=out,
                                   data_root=roots.data, configs_root=roots.configs))
    doc = json.loads((out / "instances.json").read_text(encoding="utf-8"))
    segs = [a["segmentation"] for a in doc["annotations"] if a["image_id"] == 1]
    assert segs[0] == [[0.0, 0.0, 4.0, 0.0, 4.0, 4.0]]
    assert segs[1] == {"counts": [1, 2, 3], "size": [8, 8]}
    assert segs[2] == {"counts": "abc", "size": [8, 8]}
    assert len(segs) == 3 and any("PNG" in w for w in res.warnings)


def test_export_yolo_copy_and_symlink_fallback(roots, tmp_path, det_ds, monkeypatch):
    ds, plan, _ = det_ds
    out = tmp_path / "yolo_out"
    res = export_subset(_spec(roots, "yolo", out, options={"copy": "true"}))
    ids = sorted(plan.ids_in("valA"))
    assert sorted(p.name for p in (out / "images").iterdir()) == ids
    assert sorted(p.name for p in (out / "labels").iterdir()) == [
        Path(i).with_suffix(".txt").name for i in ids
    ]
    data = yaml.safe_load((out / "data.yaml").read_text(encoding="utf-8"))
    assert data["train"] == "images" and data["names"] == {0: "cat", 1: "dog", 2: "bird"}
    assert Path(data["path"]) == out.resolve()
    sample = ds.by_id[ids[0]]
    lines = (out / "labels" / Path(ids[0]).with_suffix(".txt").name).read_text().splitlines()
    assert len(lines) == len(sample.labels.boxes)
    if lines:
        idx, cx, cy, w, h = lines[0].split()
        b = sample.labels.boxes[0]
        assert int(idx) == b.category_id and abs(float(cx) - (b.x + b.w / 2) / 8) < 1e-6
    assert res.files == 2 * len(ids) + 1 and res.warnings == []

    def refuse(self, target, target_is_directory=False):
        raise OSError("symlink not permitted")

    monkeypatch.setattr(Path, "symlink_to", refuse)
    out2 = tmp_path / "yolo_out2"
    res2 = export_subset(_spec(roots, "yolo", out2))
    assert any("copied" in w for w in res2.warnings)
    assert (out2 / "images" / ids[0]).is_file() and not (out2 / "images" / ids[0]).is_symlink()


def test_export_guards(roots, tmp_path, det_ds):
    _, _, paths = det_ds
    with pytest.raises(SealedSubsetError):
        export_subset(_spec(roots, "coco", tmp_path / "h1", subset="holdout"))
    res = export_subset(_spec(roots, "coco", tmp_path / "h2", subset="holdout", unseal=True, reason="final"))
    assert res.files == 1 and paths.unseal_jsonl("fixed-v1").is_file()
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("y", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="not empty"):
        export_subset(_spec(roots, "coco", busy))
    with pytest.raises(RegistryError):
        export_subset(_spec(roots, "nope", tmp_path / "n"))
```

`tests/unit/test_cli.py` 檔尾追加：

```python
def _split_tiny(roots, tmp_path, name="tiny"):
    assert _import_tiny(roots, tmp_path, name=name).exit_code == 0
    r = runner.invoke(app, ["data", "split", "--name", name, "--plan-id", "fixed-v1", "--seed", "0"])
    assert r.exit_code == 0, r.output


def test_export_cli_flow(roots, tmp_path):
    _split_tiny(roots, tmp_path)
    out = tmp_path / "exp"
    r = runner.invoke(app, ["data", "export", "--name", "tiny", "--plan", "fixed-v1", "--subset",
                            "valA", "--format", "coco", "--out", str(out)])
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "status=OK" in v and "files=1" in v and "format=coco" in v
    assert (out / "manifest.json").is_file()
    r = runner.invoke(app, ["data", "export", "--name", "tiny", "--plan", "fixed-v1", "--subset",
                            "holdout", "--format", "coco", "--out", str(tmp_path / "h")])
    assert r.exit_code == 2 and "SealedSubsetError" in _last_verdict(r.output)
    r = runner.invoke(app, ["data", "export", "--name", "tiny", "--plan", "fixed-v1", "--subset",
                            "holdout", "--format", "coco", "--out", str(tmp_path / "h"),
                            "--unseal", "--reason", "final decision"])
    assert r.exit_code == 0 and "status=OK" in _last_verdict(r.output)
    r = runner.invoke(app, ["data", "export", "--name", "tiny", "--plan", "fixed-v1", "--subset",
                            "valA", "--format", "nope", "--out", str(tmp_path / "n")])
    assert r.exit_code == 2 and "RegistryError" in _last_verdict(r.output)
```

（`_import_tiny` 匯入的 jsonl 資料集沒有真實影像，所以 CLI 測試只用 `coco` 匯出；`yolo` 的複製路徑由 exporters 測試以 `write_images` 覆蓋。）

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/exporters -q tests/unit/test_cli.py -q`
Expected: `ImportError`（`vcp.data.exporters` 不存在）與 CLI `No such command 'export'`。

- [ ] **Step 3: 實作**

`src/vcp/data/exporters/base.py`：

```python
"""Exporter contract, registry and the shared export flow (subset -> files + manifest)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.importers.common import rel_posix
from vcp.data.schema import Sample, View
from vcp.data.split import load_plan


class ExportSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    plan_id: str
    subset: str
    format: str
    out: Path
    options: dict[str, str] = Field(default_factory=dict)
    unseal: bool = False
    reason: str | None = None
    data_root: Path | None = None
    configs_root: Path | None = None

    def paths(self) -> DatasetPaths:
        return DatasetPaths.resolve(
            self.name, data_root=self.data_root, configs_root=self.configs_root
        )


class ExportResult(BaseModel):
    out: Path
    manifest_path: Path
    files: int
    warnings: list[str]


class Exporter(Protocol):
    name: str
    version: str

    def run(
        self,
        dataset: Dataset,
        samples: list[Sample],
        out: Path,
        image_root: Path,
        options: dict[str, str],
    ) -> tuple[list[Path], list[str]]: ...


EXPORTERS: dict[str, Exporter] = {}


def register_exporter(exporter: Exporter) -> None:
    if exporter.name in EXPORTERS:
        raise RegistryError(f"exporter {exporter.name!r} already registered")
    EXPORTERS[exporter.name] = exporter


def get_exporter(name: str) -> Exporter:
    try:
        return EXPORTERS[name]
    except KeyError:
        raise RegistryError(f"unknown exporter {name!r}; known: {sorted(EXPORTERS)}") from None


def select_view(sample: Sample, view_opt: str | None) -> tuple[int, View]:
    """Which view an exporter should use: the only one, an index, or a role name."""
    views = sample.views
    if view_opt is None:
        if len(views) != 1:
            raise ValidationFailed(
                f"sample {sample.sample_id!r} has {len(views)} views; pass --opt view=<index|role>"
            )
        return 0, views[0]
    if view_opt.isdigit():
        idx = int(view_opt)
        if idx >= len(views):
            raise ValidationFailed(f"sample {sample.sample_id!r} has no view index {idx}")
        return idx, views[idx]
    for i, v in enumerate(views):
        if v.role == view_opt:
            return i, v
    raise ValidationFailed(f"sample {sample.sample_id!r} has no view with role {view_opt!r}")


def export_subset(spec: ExportSpec) -> ExportResult:
    paths = spec.paths()
    exporter = get_exporter(spec.format)
    dataset = Dataset.load(spec.name, data_root=spec.data_root, configs_root=spec.configs_root)
    plan = load_plan(paths, spec.plan_id)
    samples = dataset.subset(
        spec.subset, plan, unseal=spec.unseal, reason=spec.reason,
        caller="vcp data export", paths=paths,
    )
    out = spec.out.expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        raise ValidationFailed(f"output directory not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    files, warnings = exporter.run(
        dataset, samples, out, paths.resolve_image_root(dataset.card), spec.options
    )
    manifest = {
        "dataset": dataset.card.name,
        "samples_hash": dataset.card.samples_hash,
        "plan_id": plan.plan_id,
        "subset": spec.subset,
        "format": exporter.name,
        "exporter_version": exporter.version,
        "exported_at": stamp(),
        "sample_count": len(samples),
        "files": {rel_posix(f, out): sha256_file(f) for f in sorted(files)},
    }
    manifest_path = out / "manifest.json"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return ExportResult(out=out, manifest_path=manifest_path, files=len(files), warnings=warnings)
```

`src/vcp/data/exporters/coco.py`：

```python
"""Canonical det / seg subset -> COCO instances.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.exporters.base import select_view
from vcp.data.schema import Mask, Sample


def segmentation_from_mask(mask: Mask) -> Any:
    if mask.polygon is not None:
        return mask.polygon
    size = list(mask.meta.get("size", []))
    if mask.meta.get("rle_encoding") == "uncompressed":
        return {"counts": [int(c) for c in mask.rle.split(",") if c], "size": size}
    return {"counts": mask.rle, "size": size}


class CocoExporter:
    name = "coco"
    version = "1"

    def run(
        self,
        dataset: Dataset,
        samples: list[Sample],
        out: Path,
        image_root: Path,
        options: dict[str, str],
    ) -> tuple[list[Path], list[str]]:
        task = dataset.card.task
        if task not in ("det", "seg"):
            raise ValidationFailed(f"coco export supports det/seg datasets, not {task!r}")
        view_opt = options.get("view")
        images: list[dict[str, Any]] = []
        annotations: list[dict[str, Any]] = []
        dropped_views = 0
        png_masks = 0
        for image_id, s in enumerate(samples, start=1):
            vi, view = select_view(s, view_opt)
            if view.width is None or view.height is None:
                raise ValidationFailed(f"sample {s.sample_id!r} view has no size; re-import")
            images.append({"id": image_id, "file_name": view.path, "width": view.width,
                           "height": view.height, "sample_id": s.sample_id})
            if s.labels is None:
                continue
            items = s.labels.boxes if task == "det" else s.labels.masks
            for it in items or []:
                if it.view != vi:
                    dropped_views += 1
                    continue
                if task == "seg" and it.path is not None:
                    png_masks += 1
                    continue
                ann: dict[str, Any] = {
                    "id": len(annotations) + 1,
                    "image_id": image_id,
                    "category_id": it.category_id,
                    "iscrowd": int(it.meta.get("iscrowd", 0)),
                }
                if task == "det":
                    ann["bbox"] = [it.x, it.y, it.w, it.h]
                    ann["area"] = it.w * it.h
                else:
                    ann["segmentation"] = segmentation_from_mask(it)
                    ann["area"] = it.meta.get("area", 0)
                annotations.append(ann)
        categories = [
            {"id": c.id, "name": c.name, **({"supercategory": c.meta["supercategory"]}
                                           if "supercategory" in c.meta else {})}
            for c in dataset.card.categories
        ]
        target = out / "instances.json"
        with target.open("w", encoding="utf-8", newline="\n") as f:
            json.dump({"images": images, "annotations": annotations, "categories": categories},
                      f, ensure_ascii=False)
            f.write("\n")
        warnings = []
        if dropped_views:
            warnings.append(f"{dropped_views} annotations on non-exported views dropped")
        if png_masks:
            warnings.append(f"{png_masks} PNG-path masks cannot be expressed in COCO; skipped")
        return [target], warnings
```

`src/vcp/data/exporters/yolo.py`：

```python
"""Canonical det subset -> YOLO layout (images/, labels/, data.yaml)."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.exporters.base import select_view
from vcp.data.schema import Sample

_TRUE = {"1", "true", "yes"}


def _place_image(src: Path, dst: Path, *, copy: bool) -> bool:
    """Put ``src`` at ``dst`` by symlink (or copy). Returns True when a copy was forced by OSError."""
    if copy:
        shutil.copy2(src, dst)
        return False
    try:
        dst.symlink_to(src)
        return False
    except OSError:
        shutil.copy2(src, dst)
        return True


class YoloExporter:
    name = "yolo"
    version = "1"

    def run(
        self,
        dataset: Dataset,
        samples: list[Sample],
        out: Path,
        image_root: Path,
        options: dict[str, str],
    ) -> tuple[list[Path], list[str]]:
        if dataset.card.task != "det":
            raise ValidationFailed(f"yolo export supports det datasets, not {dataset.card.task!r}")
        copy = options.get("copy", "false").lower() in _TRUE
        view_opt = options.get("view")
        index = {c.id: i for i, c in enumerate(dataset.card.categories)}
        images_out, labels_out = out / "images", out / "labels"
        images_out.mkdir(parents=True, exist_ok=True)
        labels_out.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        fell_back = False
        dropped_views = 0
        for s in samples:
            vi, view = select_view(s, view_opt)
            if view.width is None or view.height is None:
                raise ValidationFailed(f"sample {s.sample_id!r} view has no size; re-import")
            src = image_root / view.path
            if not src.is_file():
                raise ValidationFailed(f"image missing: {src}")
            flat = view.path.replace("/", "__")
            dst = images_out / flat
            fell_back = _place_image(src, dst, copy=copy) or fell_back
            files.append(dst)
            lines = []
            for b in (s.labels.boxes if s.labels else None) or []:
                if b.view != vi:
                    dropped_views += 1
                    continue
                cx, cy = (b.x + b.w / 2) / view.width, (b.y + b.h / 2) / view.height
                w, h = b.w / view.width, b.h / view.height
                lines.append(f"{index[b.category_id]} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
            label = labels_out / Path(flat).with_suffix(".txt").name
            with label.open("w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
            files.append(label)
        data_yaml = out / "data.yaml"
        with data_yaml.open("w", encoding="utf-8", newline="\n") as f:
            yaml.safe_dump(
                {"path": str(out), "train": "images", "val": "images",
                 "names": {i: c.name for i, c in enumerate(dataset.card.categories)}},
                f, sort_keys=False, allow_unicode=True,
            )
        files.append(data_yaml)
        warnings = []
        if fell_back:
            warnings.append("symlink not permitted; images were copied")
        if dropped_views:
            warnings.append(f"{dropped_views} boxes on non-exported views dropped")
        return files, warnings
```

`src/vcp/data/exporters/__init__.py`：

```python
"""Exporter registry. Importing this package registers the built-in exporters."""

from vcp.data.exporters.base import (
    EXPORTERS,
    Exporter,
    ExportResult,
    ExportSpec,
    export_subset,
    get_exporter,
    register_exporter,
    select_view,
)
from vcp.data.exporters.coco import CocoExporter
from vcp.data.exporters.yolo import YoloExporter

register_exporter(CocoExporter())
register_exporter(YoloExporter())

__all__ = [
    "EXPORTERS",
    "CocoExporter",
    "ExportResult",
    "ExportSpec",
    "Exporter",
    "YoloExporter",
    "export_subset",
    "get_exporter",
    "register_exporter",
    "select_view",
]
```

`src/vcp/cli.py`：加 `from vcp.data.exporters import ExportSpec, export_subset`，並在 `lineage_cmd` 之後加：

```python
@data_app.command("export")
def export_cmd(
    name: NameOpt,
    plan: Annotated[str, typer.Option("--plan", help="plan id")],
    subset: Annotated[str, typer.Option("--subset", help="subset name from the plan")],
    fmt: Annotated[str, typer.Option("--format", help="registered exporter: coco | yolo")],
    out: Annotated[Path, typer.Option("--out", help="output directory (must be empty)")],
    opt: Annotated[
        list[str] | None, typer.Option("--opt", help="exporter option key=value (repeatable)")
    ] = None,
    unseal: Annotated[bool, typer.Option("--unseal", help="open a sealed subset (recorded)")] = False,
    reason: Annotated[str | None, typer.Option("--reason", help="why a sealed subset is opened")] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Export one subset to a training-framework layout, with a hashed manifest."""

    def fn() -> CmdResult:
        spec = ExportSpec(
            name=name, plan_id=plan, subset=subset, format=fmt, out=out, options=parse_opts(opt),
            unseal=unseal, reason=reason, data_root=data_root, configs_root=configs_root,
        )
        res = export_subset(spec)
        status: Status = "WARN" if res.warnings else "OK"
        fields: dict[str, FieldValue] = {
            "name": name, "plan": plan, "subset": subset, "format": fmt,
            "files": res.files, "out": str(res.out),
        }
        if res.warnings:
            fields["warnings"] = "; ".join(res.warnings)
        human = [f"exported {res.files} files to {res.out}", *res.warnings]
        payload = {"manifest": str(res.manifest_path), "warnings": res.warnings}
        return status, fields, payload, human

    run_command("export", json_mode, data_root, fn)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/data/exporters tests/unit/test_cli.py -q`
Expected: 全部通過（exporters 5 個 + CLI 新增 1 個）。

Run: `uv run pytest -q` / `uv run ruff check .` / `uv run ruff format --check .`
Expected: 全綠、乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/exporters src/vcp/cli.py tests/unit/data/exporters tests/unit/test_cli.py
git commit -m "feat(data,cli): coco/yolo 匯出器、manifest.json、vcp data export（含 sealed 開封）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: 稽核（coords、dedup、provenance）與 `vcp data audit`

**Files:**
- Create: `src/vcp/data/audit/__init__.py`, `src/vcp/data/audit/base.py`, `src/vcp/data/audit/coords.py`, `src/vcp/data/audit/dhash.py`, `src/vcp/data/audit/dedup.py`, `src/vcp/data/audit/provenance.py`
- Modify: `src/vcp/cli.py`（新增 `audit` 命令）
- Test: `tests/unit/data/audit/test_dhash.py`、`tests/unit/data/audit/test_checks.py`、`tests/unit/test_cli.py`（追加）

**Interfaces:**
- Consumes: `Dataset`、`DatasetPaths.resolve_image_root`（Task 1）；`image_size`（Task 3）；`get_task`；`worst`、`Verdict`；`stamp`。
- Produces（base）：`AuditOptions(BaseModel)`（`max_bad_boxes=0, hamming=4, corr=0.95, view_hits=1, recompute=False`）；`CheckResult(status, fields, human)`（frozen dataclass）；`AuditContext(dataset, paths, opts, against=None, against_paths=None)` 與屬性 `out_dir = paths.cache_dir / "audit"`；`AuditCheck(Protocol)`：`name`、`applies(dataset) -> bool`、`run(ctx) -> CheckResult`；`AUDITS`、`register_check`、`get_check`；`run_audit(ctx) -> tuple[Status, dict[str, CheckResult]]`（寫 `summary.json`）。
- Produces（dhash）：`dhash64(path) -> int`；`gray64(path) -> np.ndarray`；`pearson(a, b) -> float`；`compute_hashes(image_root, rel_paths, cache_path, *, recompute=False) -> dict[str, int]`（快取 `{"path","size","hash"}` 每列一筆；缺檔 → `ValidationFailed`）；`near_pairs(keys, hashes, max_dist) -> list[tuple[str, str, int]]`（i < j）；`cross_pairs(keys_a, hashes_a, keys_b, hashes_b, max_dist) -> list[tuple[str, str, int]]`。
- Produces（checks）：`CoordsCheck`（`name="coords"`，僅 `label_field ∈ {boxes, masks}`；view 無尺寸時讀影像；寫 `coords_bad.jsonl`；壞標註數 > `max_bad_boxes` → FAIL；欄位 `bad, max_bad, report`）；`DedupCheck`（`name="dedup"`；寫 `groups.json`（`sample_id → dupNNNN`，只含多成員群）與 `overlap.jsonl`；有 `--against` 且 sample 級重疊 ≥ `view_hits` → WARN；欄位 `dup_groups, dup_samples, overlap_pairs, overlap_samples`）；`ProvenanceCheck`（`name="provenance"`；`license/url/downloaded_at/raw_hash` 任一空 → FAIL；欄位 `missing`）。純函式 `box_problems(box, width, height) -> list[str]`、`polygon_problems(polygon, width, height) -> list[str]`。
- CLI：`vcp data audit --name [--against] [--max-bad-boxes 0] [--hamming 4] [--corr 0.95] [--view-hits 1] [--recompute] [--json] ...`；人類輸出每項一行 `VERDICT cmd=audit.<name> ...`，最後 `VERDICT cmd=audit status=<最差者> name=... coords=... dedup=... provenance=... summary=...`。

- [ ] **Step 1: 寫失敗測試**

`tests/unit/data/audit/test_dhash.py`：

```python
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.data.audit import dhash as dh


def _gradient(w=32, h=32, transpose=False) -> np.ndarray:
    x = np.linspace(0, 255, w, dtype=np.float64)
    img = np.tile(x, (h, 1))
    if transpose:
        img = img.T
    return np.stack([img, img, img], axis=-1).astype(np.uint8)


def _save(path: Path, arr: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(path)
    return path


@pytest.fixture
def images(tmp_path):
    a = _gradient()
    near = a.copy()
    near[0:2, 0:2] = 200
    files = {
        "a.png": _save(tmp_path / "a.png", a),
        "dup.png": _save(tmp_path / "dup.png", a),
        "near.png": _save(tmp_path / "near.png", near),
        "other.png": _save(tmp_path / "other.png", _gradient(transpose=True)),
    }
    return tmp_path, files


def test_dhash_identity_and_distance(images):
    root, files = images
    h = {k: dh.dhash64(p) for k, p in files.items()}
    assert 0 <= h["a.png"] < 2**64
    assert h["a.png"] == h["dup.png"]
    assert bin(h["a.png"] ^ h["near.png"]).count("1") <= 4
    assert bin(h["a.png"] ^ h["other.png"]).count("1") > 8


def test_pearson(images):
    root, files = images
    a, near, other = (dh.gray64(files[k]) for k in ("a.png", "near.png", "other.png"))
    assert dh.pearson(a, a) == pytest.approx(1.0)
    assert dh.pearson(a, near) > 0.95
    assert dh.pearson(a, other) < 0.5
    flat = np.zeros(4096)
    assert dh.pearson(flat, flat) == 1.0 and dh.pearson(flat, a) == 0.0


def test_pairs(images):
    root, files = images
    keys = list(files)
    hashes = [dh.dhash64(files[k]) for k in keys]
    pairs = dh.near_pairs(keys, hashes, 4)
    assert {(a, b) for a, b, _ in pairs} == {("a.png", "dup.png"), ("a.png", "near.png"), ("dup.png", "near.png")}
    cross = dh.cross_pairs(["x"], [hashes[0]], keys, hashes, 0)
    assert sorted(b for _, b, _ in cross) == ["a.png", "dup.png"]
    assert dh.near_pairs([], [], 4) == []


def test_cache_reuse_and_recompute(images, monkeypatch):
    root, files = images
    cache = root / "dhash.jsonl"
    first = dh.compute_hashes(root, ["a.png", "near.png"], cache)
    assert cache.is_file() and len(cache.read_text(encoding="utf-8").splitlines()) == 2

    def boom(path):
        raise AssertionError(f"recomputed {path}")

    monkeypatch.setattr(dh, "dhash64", boom)
    assert dh.compute_hashes(root, ["a.png", "near.png"], cache) == first
    with pytest.raises(AssertionError):
        dh.compute_hashes(root, ["a.png"], cache, recompute=True)
    monkeypatch.undo()
    with pytest.raises(ValidationFailed, match="image not found"):
        dh.compute_hashes(root, ["missing.png"], cache)
```

`tests/unit/data/audit/test_checks.py`：

```python
import json
from pathlib import Path

import numpy as np
import pytest
from helpers import CATS, make_card, make_source
from PIL import Image

from vcp.core.paths import DatasetPaths
from vcp.data.audit import AUDITS, get_check
from vcp.data.audit.base import AuditContext, AuditOptions, run_audit
from vcp.data.audit.coords import box_problems, polygon_problems
from vcp.data.dataset import Dataset
from vcp.data.schema import Box, DatasetCard, Labels, Mask, Sample, View


def _write(path: Path, arr: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(path)


def _gradient(seed: int) -> np.ndarray:
    """A distinct 32x32 image per seed: random 8x8 blocks upscaled (perceptual hashes differ)."""
    rng = np.random.default_rng(seed)
    blocks = rng.integers(0, 256, (8, 8), dtype=np.uint8)
    img = np.kron(blocks, np.ones((4, 4), dtype=np.uint8))
    return np.stack([img, img, img], axis=-1)


def _dataset(roots, name, specs, *, task="det", card_kwargs=None):
    """specs: list of (sample_id, image array | None, labels, view kwargs)."""
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    root = roots.data / "raw" / name
    samples = []
    for sid, arr, labels, view_kw in specs:
        if arr is not None:
            _write(root / sid, arr)
        samples.append(Sample(sample_id=sid, views=[View(path=sid, **view_kw)], labels=labels,
                              label_source="gold" if labels is not None else "none"))
    card = make_card(task, name=name, image_root=f"raw/{name}", **(card_kwargs or {}))
    ds = Dataset.from_parts(card, samples)
    ds.save(paths)
    return ds, paths


def test_box_and_polygon_problems():
    assert box_problems(Box(x=0, y=0, w=5, h=5, category_id=0), 10, 10) == []
    assert "non-positive size" in box_problems(Box(x=0, y=0, w=0, h=5, category_id=0), 10, 10)
    assert any("exceeds" in p for p in box_problems(Box(x=8, y=0, w=5, h=5, category_id=0), 10, 10))
    assert "negative origin" in box_problems(Box(x=-3, y=0, w=5, h=5, category_id=0), 10, 10)
    assert polygon_problems([[0, 0, 5, 0, 5, 5]], 10, 10) == []
    assert any("outside" in p for p in polygon_problems([[0, 0, 12, 0, 5, 5]], 10, 10))
    assert any("degenerate" in p for p in polygon_problems([[0, 0, 5, 0]], 10, 10))


def test_coords_check_reads_sizes_and_fails_over_limit(roots):
    img = _gradient(1)
    specs = [
        ("ok.png", img, Labels(boxes=[Box(x=1, y=1, w=5, h=5, category_id=0)]), {"width": 32, "height": 32}),
        ("zero.png", img, Labels(boxes=[Box(x=1, y=1, w=0, h=5, category_id=0)]), {"width": 32, "height": 32}),
        ("unsized.png", img, Labels(boxes=[Box(x=0, y=0, w=100, h=100, category_id=1)]), {}),
        ("poly.png", img, Labels(masks=[Mask(category_id=2, polygon=[[0, 0, 50, 0, 5, 5]])]), {"width": 32, "height": 32}),
    ]
    ds, paths = _dataset(roots, "cc", specs)
    ctx = AuditContext(dataset=ds, paths=paths, opts=AuditOptions())
    res = get_check("coords").run(ctx)
    assert res.status == "FAIL" and res.fields["bad"] == 3
    rows = [json.loads(l) for l in (ctx.out_dir / "coords_bad.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {r["sample_id"] for r in rows} == {"zero.png", "unsized.png", "poly.png"}
    res2 = get_check("coords").run(AuditContext(dataset=ds, paths=paths, opts=AuditOptions(max_bad_boxes=3)))
    assert res2.status == "OK"
    assert not get_check("coords").applies(Dataset.from_parts(make_card("cls"), []))


def test_dedup_groups_and_overlap(roots):
    a, b, c = _gradient(1), _gradient(2), _gradient(3)
    near = a.copy()
    near[0, 0] = 255
    train_specs = [
        ("a.png", a, Labels(boxes=[]), {}), ("a_dup.png", a, Labels(boxes=[]), {}),
        ("a_near.png", near, Labels(boxes=[]), {}), ("b.png", b, Labels(boxes=[]), {}),
        ("c.png", c, Labels(boxes=[]), {}),
    ]
    ds, paths = _dataset(roots, "tr", train_specs)
    test_specs = [("t1.png", b, None, {}), ("t2.png", _gradient(9), None, {})]
    test_ds, test_paths = _dataset(roots, "te", test_specs)
    ctx = AuditContext(dataset=ds, paths=paths, opts=AuditOptions(), against=test_ds, against_paths=test_paths)
    res = get_check("dedup").run(ctx)
    groups = json.loads((ctx.out_dir / "groups.json").read_text(encoding="utf-8"))
    assert set(groups) == {"a.png", "a_dup.png", "a_near.png"} and len(set(groups.values())) == 1
    assert res.fields["dup_groups"] == 1 and res.fields["dup_samples"] == 3
    overlap = [json.loads(l) for l in (ctx.out_dir / "overlap.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["sample_id"], r["other_sample_id"]) for r in overlap] == [("b.png", "t1.png")]
    assert res.status == "WARN" and res.fields["overlap_pairs"] == 1
    assert (paths.cache_dir / "dhash.jsonl").is_file() and (test_paths.cache_dir / "dhash.jsonl").is_file()
    alone = get_check("dedup").run(AuditContext(dataset=ds, paths=paths, opts=AuditOptions()))
    assert alone.status == "OK" and alone.fields["overlap_pairs"] == 0


def test_provenance_and_run_audit(roots):
    ds, paths = _dataset(roots, "pv", [("x.png", _gradient(4), Labels(boxes=[]), {})])
    ok = get_check("provenance").run(AuditContext(dataset=ds, paths=paths, opts=AuditOptions()))
    assert ok.status == "OK" and ok.fields["missing"] == "-"
    bad_card = ds.card.model_copy(update={"source": ds.card.source.model_copy(update={"license": ""})})
    bad_ds = Dataset.from_parts(bad_card, ds.samples)
    res = get_check("provenance").run(AuditContext(dataset=bad_ds, paths=paths, opts=AuditOptions()))
    assert res.status == "FAIL" and res.fields["missing"] == "license"
    status, results = run_audit(AuditContext(dataset=ds, paths=paths, opts=AuditOptions()))
    assert status == "OK" and set(results) == {"coords", "dedup", "provenance"}
    summary = json.loads((paths.cache_dir / "audit" / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "OK" and summary["checks"]["dedup"]["fields"]["dup_groups"] == 0
    assert summary["audited_at"].endswith("Z") and list(AUDITS) == ["coords", "dedup", "provenance"]
```

`tests/unit/test_cli.py`：把 `_import_tiny` 改為

```python
def _import_tiny(roots, tmp_path, name="tiny", n=60, with_images=False):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    samples = det_samples(n, seed=0)
    write_samples_jsonl(src / "samples.jsonl", samples)
    if with_images:
        write_images(src, samples)
    (src / "cats.json").write_text(json.dumps([c.model_dump() for c in CATS]), encoding="utf-8")
    return runner.invoke(
        app,
        [
            "data", "import", "--importer", "jsonl", "--src", str(src), "--name", name,
            "--license", "CC0", "--url", "https://example.org", "--downloaded-at", "2026-09-02",
            "--opt", "task=det", "--opt", "categories=cats.json",
        ],
    )
```

（`from helpers import CATS, det_samples, write_images`），並在檔尾追加：

```python
def test_audit_cli(roots, tmp_path):
    assert _import_tiny(roots, tmp_path, with_images=True).exit_code == 0
    r = runner.invoke(app, ["data", "audit", "--name", "tiny"])
    assert r.exit_code == 0, r.output
    assert "VERDICT cmd=audit.coords status=OK" in r.output
    assert "VERDICT cmd=audit.dedup status=OK" in r.output
    assert "VERDICT cmd=audit.provenance status=OK" in r.output
    v = _last_verdict(r.output)
    assert v.startswith("VERDICT cmd=audit status=OK") and "dedup=OK" in v
    assert (roots.data / "datasets" / "tiny" / "cache" / "audit" / "summary.json").is_file()
    r = runner.invoke(app, ["data", "audit", "--name", "tiny", "--against", "missing"])
    assert r.exit_code == 1 and "status=FAIL" in _last_verdict(r.output)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/audit tests/unit/test_cli.py -q`
Expected: `ImportError`（`vcp.data.audit` 不存在）與 CLI `No such command 'audit'`。

- [ ] **Step 3: 實作**

`src/vcp/data/audit/base.py`：

```python
"""Audit checks: contract, registry and the runner that writes cache/audit/summary.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from vcp.core.errors import RegistryError
from vcp.core.log import FieldValue, Status, worst
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset


class AuditOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_bad_boxes: int = 0
    hamming: int = 4
    corr: float = 0.95
    view_hits: int = 1
    recompute: bool = False


@dataclass(frozen=True)
class CheckResult:
    status: Status
    fields: dict[str, FieldValue] = field(default_factory=dict)
    human: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AuditContext:
    dataset: Dataset
    paths: DatasetPaths
    opts: AuditOptions
    against: Dataset | None = None
    against_paths: DatasetPaths | None = None

    @property
    def out_dir(self) -> Path:
        return self.paths.cache_dir / "audit"


class AuditCheck(Protocol):
    name: str

    def applies(self, dataset: Dataset) -> bool: ...

    def run(self, ctx: AuditContext) -> CheckResult: ...


AUDITS: dict[str, AuditCheck] = {}


def register_check(check: AuditCheck) -> None:
    if check.name in AUDITS:
        raise RegistryError(f"audit check {check.name!r} already registered")
    AUDITS[check.name] = check


def get_check(name: str) -> AuditCheck:
    try:
        return AUDITS[name]
    except KeyError:
        raise RegistryError(f"unknown audit check {name!r}; known: {sorted(AUDITS)}") from None


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def run_audit(ctx: AuditContext) -> tuple[Status, dict[str, CheckResult]]:
    """Run every applicable registered check; write summary.json; return (worst status, results)."""
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    results = {
        name: check.run(ctx) for name, check in AUDITS.items() if check.applies(ctx.dataset)
    }
    status = worst(*(r.status for r in results.values()))
    summary = {
        "dataset": ctx.dataset.card.name,
        "samples_hash": ctx.dataset.card.samples_hash,
        "against": ctx.against.card.name if ctx.against is not None else None,
        "options": ctx.opts.model_dump(),
        "audited_at": stamp(),
        "status": status,
        "checks": {n: {"status": r.status, "fields": r.fields} for n, r in results.items()},
    }
    with (ctx.out_dir / "summary.json").open("w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return status, results
```

`src/vcp/data/audit/coords.py`：

```python
"""Coordinate sanity: every box / polygon lies inside its view (postmortem error #2)."""

from __future__ import annotations

from pathlib import Path

from vcp.data.audit.base import AuditContext, CheckResult, write_jsonl
from vcp.data.dataset import Dataset
from vcp.data.importers.common import image_size
from vcp.data.schema import Box, Sample
from vcp.data.tasks import get_task

TOLERANCE_PX = 1.0


def box_problems(box: Box, width: int, height: int) -> list[str]:
    problems: list[str] = []
    if box.w <= 0 or box.h <= 0:
        problems.append("non-positive size")
    if box.x < -TOLERANCE_PX or box.y < -TOLERANCE_PX:
        problems.append("negative origin")
    if box.x + box.w > width + TOLERANCE_PX or box.y + box.h > height + TOLERANCE_PX:
        problems.append(f"exceeds {width}x{height}")
    return problems


def polygon_problems(polygon: list[list[float]], width: int, height: int) -> list[str]:
    problems: list[str] = []
    for ring in polygon:
        if len(ring) < 6 or len(ring) % 2:
            problems.append("degenerate ring")
            continue
        xs, ys = ring[0::2], ring[1::2]
        if (
            min(xs) < -TOLERANCE_PX
            or min(ys) < -TOLERANCE_PX
            or max(xs) > width + TOLERANCE_PX
            or max(ys) > height + TOLERANCE_PX
        ):
            problems.append(f"vertex outside {width}x{height}")
    return problems


def _view_size(sample: Sample, index: int, image_root: Path, cache: dict[int, tuple[int, int]]) -> tuple[int, int]:
    if index not in cache:
        v = sample.views[index]
        cache[index] = (
            (v.width, v.height)
            if v.width is not None and v.height is not None
            else image_size(image_root / v.path)
        )
    return cache[index]


class CoordsCheck:
    name = "coords"

    def applies(self, dataset: Dataset) -> bool:
        return get_task(dataset.card.task).label_field in ("boxes", "masks")

    def run(self, ctx: AuditContext) -> CheckResult:
        image_root = ctx.paths.resolve_image_root(ctx.dataset.card)
        bad: list[dict[str, object]] = []
        for s in ctx.dataset.samples:
            if s.labels is None:
                continue
            sizes: dict[int, tuple[int, int]] = {}
            for i, b in enumerate(s.labels.boxes or []):
                problems = box_problems(b, *_view_size(s, b.view, image_root, sizes))
                if problems:
                    bad.append({"sample_id": s.sample_id, "kind": "box", "index": i, "problems": problems})
            for i, m in enumerate(s.labels.masks or []):
                if m.polygon is None:
                    continue
                problems = polygon_problems(m.polygon, *_view_size(s, m.view, image_root, sizes))
                if problems:
                    bad.append({"sample_id": s.sample_id, "kind": "mask", "index": i, "problems": problems})
        report = write_jsonl(ctx.out_dir / "coords_bad.jsonl", bad)
        status = "FAIL" if len(bad) > ctx.opts.max_bad_boxes else "OK"
        fields = {"bad": len(bad), "max_bad": ctx.opts.max_bad_boxes, "report": str(report)}
        return CheckResult(status, fields, [f"coords: {len(bad)} bad annotations"])
```

`src/vcp/data/audit/dhash.py`：

```python
"""Perceptual hashing (64-bit dHash) with a jsonl cache, plus numpy Hamming pair search."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from vcp.core.errors import ValidationFailed

_POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def dhash64(path: Path) -> int:
    """Grey 9x8 thumbnail, adjacent-pixel comparison -> 64-bit integer."""
    with Image.open(path) as im:
        px = np.asarray(im.convert("L").resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    bits = (px[:, 1:] > px[:, :-1]).ravel()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def gray64(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(
            im.convert("L").resize((64, 64), Image.Resampling.LANCZOS), dtype=np.float64
        ).ravel()


def pearson(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    if denom == 0.0:
        return 1.0 if np.array_equal(a, b) else 0.0
    return float((a * b).sum() / denom)


def load_cache(path: Path) -> dict[str, tuple[int, int]]:
    """rel path -> (file size, hash)."""
    if not path.is_file():
        return {}
    out: dict[str, tuple[int, int]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["path"]] = (int(row["size"]), int(row["hash"]))
    return out


def save_cache(path: Path, entries: dict[str, tuple[int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for rel in sorted(entries):
            size, value = entries[rel]
            f.write(json.dumps({"path": rel, "size": size, "hash": value}) + "\n")


def compute_hashes(
    image_root: Path, rel_paths: list[str], cache_path: Path, *, recompute: bool = False
) -> dict[str, int]:
    cache = {} if recompute else load_cache(cache_path)
    out: dict[str, int] = {}
    changed = recompute
    for rel in rel_paths:
        file = image_root / rel
        if not file.is_file():
            raise ValidationFailed(f"image not found: {file}")
        size = file.stat().st_size
        hit = cache.get(rel)
        if hit is not None and hit[0] == size:
            out[rel] = hit[1]
            continue
        out[rel] = dhash64(file)
        cache[rel] = (size, out[rel])
        changed = True
    if changed:
        save_cache(cache_path, cache)
    return out


def _popcount64(x: np.ndarray) -> np.ndarray:
    return _POP[np.ascontiguousarray(x).view(np.uint8)].reshape(*x.shape, 8).sum(axis=-1)


def _pairs(
    keys_a: list[str],
    hashes_a: list[int],
    keys_b: list[str],
    hashes_b: list[int],
    max_dist: int,
    *,
    same_set: bool,
    chunk: int = 512,
) -> list[tuple[str, str, int]]:
    if not keys_a or not keys_b:
        return []
    arr_a = np.array(hashes_a, dtype=np.uint64)
    arr_b = np.array(hashes_b, dtype=np.uint64)
    pairs: list[tuple[str, str, int]] = []
    for start in range(0, len(arr_a), chunk):
        block = arr_a[start : start + chunk]
        dist = _popcount64(block[:, None] ^ arr_b[None, :])
        ii, jj = np.nonzero(dist <= max_dist)
        for i, j in zip(ii.tolist(), jj.tolist(), strict=True):
            gi = start + i
            if same_set and gi >= j:
                continue
            pairs.append((keys_a[gi], keys_b[j], int(dist[i, j])))
    return pairs


def near_pairs(keys: list[str], hashes: list[int], max_dist: int) -> list[tuple[str, str, int]]:
    """Within one set: (key_i, key_j, hamming) for i < j with hamming <= max_dist."""
    return _pairs(keys, hashes, keys, hashes, max_dist, same_set=True)


def cross_pairs(
    keys_a: list[str], hashes_a: list[int], keys_b: list[str], hashes_b: list[int], max_dist: int
) -> list[tuple[str, str, int]]:
    """Across two sets: (key_a, key_b, hamming) with hamming <= max_dist."""
    return _pairs(keys_a, hashes_a, keys_b, hashes_b, max_dist, same_set=False)
```

`src/vcp/data/audit/dedup.py`：

```python
"""Near-duplicate groups inside a dataset and overlap against another dataset (e.g. test)."""

from __future__ import annotations

from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.data.audit.base import AuditContext, CheckResult, write_jsonl
from vcp.data.audit.dhash import compute_hashes, cross_pairs, gray64, near_pairs, pearson
from vcp.data.dataset import Dataset


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> list[list[str]]:
        members: dict[str, list[str]] = {}
        for x in self.parent:
            members.setdefault(self.find(x), []).append(x)
        return sorted(sorted(g) for g in members.values() if len(g) > 1)


def _views(dataset: Dataset) -> tuple[list[str], dict[str, str]]:
    """(view paths in dataset order, view path -> sample_id)."""
    keys: list[str] = []
    owner: dict[str, str] = {}
    for s in dataset.samples:
        for v in s.views:
            keys.append(v.path)
            owner[v.path] = s.sample_id
    return keys, owner


class DedupCheck:
    name = "dedup"

    def applies(self, dataset: Dataset) -> bool:
        return True

    def run(self, ctx: AuditContext) -> CheckResult:
        opts = ctx.opts
        root = ctx.paths.resolve_image_root(ctx.dataset.card)
        keys, owner = _views(ctx.dataset)
        hashes = compute_hashes(root, keys, ctx.paths.cache_dir / "dhash.jsonl", recompute=opts.recompute)
        hits: dict[tuple[str, str], int] = {}
        for ka, kb, _ in near_pairs(keys, [hashes[k] for k in keys], opts.hamming):
            sa, sb = owner[ka], owner[kb]
            if sa == sb or pearson(gray64(root / ka), gray64(root / kb)) < opts.corr:
                continue
            pair = (min(sa, sb), max(sa, sb))
            hits[pair] = hits.get(pair, 0) + 1
        uf = _UnionFind()
        for (a, b), n in hits.items():
            if n >= opts.view_hits:
                uf.union(a, b)
        groups = uf.groups()
        mapping = {sid: f"dup{i:04d}" for i, members in enumerate(groups, start=1) for sid in members}
        groups_path = ctx.out_dir / "groups.json"
        groups_path.parent.mkdir(parents=True, exist_ok=True)
        with groups_path.open("w", encoding="utf-8", newline="\n") as f:
            import json

            json.dump(dict(sorted(mapping.items())), f, ensure_ascii=False, indent=1)
            f.write("\n")
        overlap_rows, overlap_pairs = self._overlap(ctx, root, keys, owner, hashes)
        write_jsonl(ctx.out_dir / "overlap.jsonl", overlap_rows)
        fields = {
            "dup_groups": len(groups),
            "dup_samples": len(mapping),
            "overlap_pairs": len(overlap_pairs),
            "overlap_samples": len({a for a, _ in overlap_pairs}),
        }
        status = "WARN" if overlap_pairs else "OK"
        human = [
            f"dedup: {len(groups)} near-duplicate groups ({len(mapping)} samples); "
            f"{len(overlap_pairs)} sample pairs overlap the reference set"
        ]
        return CheckResult(status, fields, human)

    @staticmethod
    def _overlap(
        ctx: AuditContext,
        root: Path,
        keys: list[str],
        owner: dict[str, str],
        hashes: dict[str, int],
    ) -> tuple[list[dict[str, object]], set[tuple[str, str]]]:
        if ctx.against is None:
            return [], set()
        if ctx.against_paths is None:
            raise ValidationFailed("against dataset given without its paths")
        b_root = ctx.against_paths.resolve_image_root(ctx.against.card)
        b_keys, b_owner = _views(ctx.against)
        b_hashes = compute_hashes(
            b_root, b_keys, ctx.against_paths.cache_dir / "dhash.jsonl", recompute=ctx.opts.recompute
        )
        rows: list[dict[str, object]] = []
        hits: dict[tuple[str, str], int] = {}
        for ka, kb, dist in cross_pairs(
            keys, [hashes[k] for k in keys], b_keys, [b_hashes[k] for k in b_keys], ctx.opts.hamming
        ):
            corr = pearson(gray64(root / ka), gray64(b_root / kb))
            if corr < ctx.opts.corr:
                continue
            rows.append({"sample_id": owner[ka], "view": ka, "other_sample_id": b_owner[kb],
                         "other_view": kb, "hamming": dist, "corr": round(corr, 4)})
            pair = (owner[ka], b_owner[kb])
            hits[pair] = hits.get(pair, 0) + 1
        return rows, {p for p, n in hits.items() if n >= ctx.opts.view_hits}
```

（把 `import json` 移到檔頭；上面寫在函式內只是為了強調 groups.json 也是 LF 寫出。）

`src/vcp/data/audit/provenance.py`：

```python
"""Disclosure check: a card without license / source / download date / raw hash is not usable."""

from __future__ import annotations

from vcp.data.audit.base import AuditContext, CheckResult
from vcp.data.dataset import Dataset

REQUIRED = ("license", "url", "downloaded_at", "raw_hash")


class ProvenanceCheck:
    name = "provenance"

    def applies(self, dataset: Dataset) -> bool:
        return True

    def run(self, ctx: AuditContext) -> CheckResult:
        src = ctx.dataset.card.source
        missing = [f for f in REQUIRED if not str(getattr(src, f)).strip()]
        status = "FAIL" if missing else "OK"
        fields = {"missing": ",".join(missing) or "-"}
        human = [f"provenance: missing {missing}" if missing else "provenance: complete"]
        return CheckResult(status, fields, human)
```

`src/vcp/data/audit/__init__.py`：

```python
"""Audit registry. Importing this package registers the built-in checks in run order."""

from vcp.data.audit.base import (
    AUDITS,
    AuditCheck,
    AuditContext,
    AuditOptions,
    CheckResult,
    get_check,
    register_check,
    run_audit,
)
from vcp.data.audit.coords import CoordsCheck
from vcp.data.audit.dedup import DedupCheck
from vcp.data.audit.provenance import ProvenanceCheck

register_check(CoordsCheck())
register_check(DedupCheck())
register_check(ProvenanceCheck())

__all__ = [
    "AUDITS",
    "AuditCheck",
    "AuditContext",
    "AuditOptions",
    "CheckResult",
    "CoordsCheck",
    "DedupCheck",
    "ProvenanceCheck",
    "get_check",
    "register_check",
    "run_audit",
]
```

`src/vcp/cli.py`：加 `from vcp.data.audit import AuditContext, AuditOptions, run_audit`，並在 `export_cmd` 之後加：

```python
@data_app.command("audit")
def audit_cmd(
    name: NameOpt,
    against: Annotated[
        str | None, typer.Option("--against", help="dataset to check overlap against (e.g. test)")
    ] = None,
    max_bad_boxes: Annotated[int, typer.Option("--max-bad-boxes")] = 0,
    hamming: Annotated[int, typer.Option("--hamming", help="max dHash Hamming distance")] = 4,
    corr: Annotated[float, typer.Option("--corr", help="min 64x64 grey Pearson to confirm")] = 0.95,
    view_hits: Annotated[int, typer.Option("--view-hits", help="views that must match")] = 1,
    recompute: Annotated[bool, typer.Option("--recompute", help="ignore the dHash cache")] = False,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Coordinate sanity, near-duplicate / overlap and provenance checks."""

    def fn() -> CmdResult:
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        ds = Dataset.load(name, data_root=data_root, configs_root=configs_root)
        against_ds = against_paths = None
        if against:
            against_paths = DatasetPaths.resolve(against, data_root=data_root, configs_root=configs_root)
            against_ds = Dataset.load(against, data_root=data_root, configs_root=configs_root)
        ctx = AuditContext(
            dataset=ds,
            paths=paths,
            opts=AuditOptions(max_bad_boxes=max_bad_boxes, hamming=hamming, corr=corr,
                              view_hits=view_hits, recompute=recompute),
            against=against_ds,
            against_paths=against_paths,
        )
        status, results = run_audit(ctx)
        human = [Verdict(cmd=f"audit.{n}", status=r.status, fields=r.fields).line() for n, r in results.items()]
        fields: dict[str, FieldValue] = {"name": name}
        fields.update({n: r.status for n, r in results.items()})
        fields["summary"] = str(ctx.out_dir / "summary.json")
        payload = {
            "summary": str(ctx.out_dir / "summary.json"),
            "checks": {n: {"status": r.status, "fields": r.fields} for n, r in results.items()},
        }
        return status, fields, payload, human

    run_command("audit", json_mode, data_root, fn)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/unit/data/audit tests/unit/test_cli.py -q`
Expected: 全部通過（dhash 4 個、checks 4 個、CLI 新增 1 個）。若 `test_dhash_identity_and_distance` 的近重複距離 > 4，回報實際距離而不是放寬門檻（LANCZOS 縮圖對兩個角落像素的變動應只影響 0–2 個 bit）。

Run: `uv run pytest -q` / `uv run ruff check .` / `uv run ruff format --check .`
Expected: 全綠、乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/vcp/data/audit src/vcp/cli.py tests/unit/data/audit tests/unit/test_cli.py
git commit -m "feat(data,cli): 進場稽核（座標、dHash 近重複與 test 重疊、來源揭露）與 vcp data audit

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: 整合測試骨架與海廢真實資料測試

**Files:**
- Create: `tests/integration/conftest.py`, `tests/integration/test_marine_debris.py`, `tests/integration/README.md`
- Modify: `CLAUDE.md`（新命令、`VCP_REALDATA_ROOT`）

**Interfaces:**
- Produces: fixture `real_roots`（讀環境變數 `VCP_REALDATA_ROOT`，缺省 Windows `C:/vcp-data`、Linux `~/vcp-data`；`VCP_REALDATA_CONFIGS` 缺省為 repo `configs/`）；helper `load_real(name, real_roots) -> Dataset`（資料集不存在即 `pytest.skip`）。整合測試以 `@pytest.mark.realdata` 標記，資料缺席時整檔 skip，不會讓 `uv run pytest` 變紅。
- 真實資料的匯入命令寫在 README（由使用者執行一次），測試只驗證已匯入的資料集：`marine-debris`（train，det，15,163 樣本、34 類）與 `marine-val282`（`coco` 匯入 `val_gt_282.json`，282 樣本、1,093 框、33 類出現）。

- [ ] **Step 1: 寫測試與 README**

`tests/integration/conftest.py`：

```python
"""Integration tests run against REAL datasets under VCP_REALDATA_ROOT; they skip when absent.

The unit-test autouse fixture redirects VCP_DATA_ROOT / VCP_CONFIGS_ROOT to tmp dirs, so these
tests pass explicit roots instead of relying on the environment the CLI would see.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from vcp.data.dataset import Dataset


def _default_real_root() -> Path:
    return Path("C:/vcp-data") if sys.platform == "win32" else Path.home() / "vcp-data"


@pytest.fixture(scope="session")
def real_roots():
    data = Path(os.environ.get("VCP_REALDATA_ROOT", _default_real_root()))
    configs = Path(
        os.environ.get("VCP_REALDATA_CONFIGS", Path(__file__).resolve().parents[2] / "configs")
    )
    return SimpleNamespace(data=data, configs=configs)


def load_real(name: str, real_roots) -> Dataset:
    card = real_roots.configs / "datasets" / name / "dataset.yaml"
    samples = real_roots.data / "datasets" / name / "samples.jsonl"
    if not (card.is_file() and samples.is_file()):
        pytest.skip(f"real dataset {name!r} not imported (see tests/integration/README.md)")
    return Dataset.load(name, data_root=real_roots.data, configs_root=real_roots.configs)
```

`tests/integration/test_marine_debris.py`：

```python
"""Real-data checks for the 2026 marine-debris detection dataset (postmortem §1.3, §5.1)."""

from __future__ import annotations

import pytest
from conftest import load_real

from vcp.core.paths import DatasetPaths
from vcp.data.audit import AuditContext, AuditOptions, get_check

pytestmark = pytest.mark.realdata


@pytest.fixture(scope="module")
def marine(real_roots):
    return load_real("marine-debris", real_roots)


@pytest.fixture(scope="module")
def val282(real_roots):
    return load_real("marine-val282", real_roots)


def test_marine_train_shape(marine):
    assert marine.card.task == "det"
    assert len(marine.samples) == 15163
    assert len(marine.card.categories) == 34
    assert all(s.label_source == "gold" for s in marine.samples)


def test_marine_coords_audit_runs(marine, real_roots):
    paths = DatasetPaths.resolve(
        "marine-debris", data_root=real_roots.data, configs_root=real_roots.configs
    )
    ctx = AuditContext(dataset=marine, paths=paths, opts=AuditOptions(max_bad_boxes=10**9))
    res = get_check("coords").run(ctx)
    assert res.status == "OK"
    assert isinstance(res.fields["bad"], int)


def test_val282_matches_postmortem(val282):
    assert len(val282.samples) == 282
    boxes = sum(len(s.labels.boxes) for s in val282.samples if s.labels is not None)
    assert boxes == 1093
    present = {b.category_id for s in val282.samples if s.labels for b in s.labels.boxes}
    assert len(present) == 33
```

`tests/integration/README.md`：

```markdown
# 整合測試（真實資料）

這裡的測試以 `realdata` 標記，資料不在就 skip。它們讀 `VCP_REALDATA_ROOT`（預設
Windows `C:/vcp-data`、Linux `~/vcp-data`）與 `VCP_REALDATA_CONFIGS`（預設 repo `configs/`）。

## 準備海廢資料（一次）

1. 從 Google Drive vault 下載官方 train 影像與標註 CSV 到 `<root>/raw/marine-debris/`，
   `val_gt_282.json` 與對應的 val 影像到 `<root>/raw/marine-val282/`。
2. 匯入（依實際檔名調整 `--opt csv=`、`--opt images=`）：

```bash
uv run vcp data import --importer csv_boxes --src C:/vcp-data/raw/marine-debris --name marine-debris \
  --license "AIdea competition terms" --url "https://aidea-web.tw" --downloaded-at 2026-08-23 \
  --opt csv=train_label.csv --opt images=train --opt on_bad_row=skip
uv run vcp data import --importer coco --src C:/vcp-data/raw/marine-val282 --name marine-val282 \
  --license "AIdea competition terms" --url "https://aidea-web.tw" --downloaded-at 2026-08-23 \
  --opt json=val_gt_282.json --opt images=images
```

3. 跑：`uv run pytest tests/integration -m realdata`。

期望值來自賽後報告 §1.3 / §5.1：train 15,163 張、34 類；val282 282 張、1,093 框、33 類出現。
`on_bad_row=skip` 會把方向錯誤的標註列寫進 `cache/import_skipped.jsonl`，稽核 `coords` 再看一次。
```

`CLAUDE.md` 的「常用命令」下加一行：`uv run vcp data export --name X --plan P --subset S --format coco|yolo --out DIR` 與 `uv run vcp data audit --name X [--against TEST]`；「路徑」下加：「整合測試讀 `VCP_REALDATA_ROOT` / `VCP_REALDATA_CONFIGS`，資料缺席即 skip；`raw/<name>/` 永不修改。」

- [ ] **Step 2: 執行確認 skip 行為**

Run: `uv run pytest tests/integration -q`
Expected: `3 skipped`（本機沒有匯入真實資料時），沒有 error。

Run: `uv run pytest -q`
Expected: 全部通過，skipped 3。

- [ ] **Step 3: Commit**

```bash
git add tests/integration CLAUDE.md
git commit -m "test: 整合測試骨架（VCP_REALDATA_ROOT、缺資料即 skip）與海廢真實資料驗證

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: 端到端流程測試、整體驗證與驗收自查

**Files:**
- Create: `tests/unit/test_e2e_flow.py`
- Modify（僅在需要時）: 補覆蓋率的測試

**Interfaces:** 無新介面。

- [ ] **Step 1: 端到端流程測試**

`tests/unit/test_e2e_flow.py`：

```python
"""import (csv_boxes) -> audit (dedup finds planted duplicate) -> split --group-from-audit
-> export yolo + coco, all through the CLI."""

import json
from pathlib import Path

import numpy as np
from PIL import Image
from typer.testing import CliRunner

from vcp.cli import app

runner = CliRunner()


def _img(path: Path, seed: int) -> None:
    rng = np.random.default_rng(seed)
    blocks = rng.integers(0, 256, (8, 8), dtype=np.uint8)
    arr = np.kron(blocks, np.ones((4, 4), dtype=np.uint8))
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.stack([arr, arr, arr], axis=-1)).save(path)


def _verdicts(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("VERDICT ")]


def test_full_flow(roots, tmp_path):
    src = roots.data / "raw" / "flow"
    for i in range(12):
        _img(src / "images" / f"img{i:02d}.jpg", seed=i)
    _img(src / "images" / "img00_copy.jpg", seed=0)  # planted duplicate of img00
    rows = ["image_filename,label_id,x,y,w,h"]
    for i in range(12):
        rows.append(f"img{i:02d}.jpg,{i % 3},2,2,{10 + i % 5},{8 + i % 4}")
    rows.append("img00_copy.jpg,0,2,2,10,8")
    (src / "labels.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    r = runner.invoke(app, ["data", "import", "--importer", "csv_boxes", "--src", str(src),
                            "--name", "flow", "--license", "CC0", "--url", "https://example.org",
                            "--downloaded-at", "2026-09-03"])
    assert r.exit_code == 0, r.output
    assert "samples=13" in _verdicts(r.output)[-1]
    card = (roots.configs / "datasets" / "flow" / "dataset.yaml").read_text(encoding="utf-8")
    assert "image_root: raw/flow/images" in card

    r = runner.invoke(app, ["data", "audit", "--name", "flow"])
    assert r.exit_code == 0, r.output
    groups = json.loads((roots.data / "datasets" / "flow" / "cache" / "audit" / "groups.json").read_text())
    assert set(groups) == {"img00.jpg", "img00_copy.jpg"}

    r = runner.invoke(app, ["data", "split", "--name", "flow", "--plan-id", "p1", "--seed", "1",
                            "--subsets", "train:train:0.6,val:eval:0.4", "--group-from-audit"])
    assert r.exit_code == 0, r.output
    plan = json.loads((roots.configs / "datasets" / "flow" / "splits" / "p1.json").read_text())
    assert plan["assignment"]["img00.jpg"] == plan["assignment"]["img00_copy.jpg"]
    assert plan["params"]["group_from_audit"] is True

    out_yolo = tmp_path / "yolo"
    r = runner.invoke(app, ["data", "export", "--name", "flow", "--plan", "p1", "--subset", "val",
                            "--format", "yolo", "--out", str(out_yolo), "--opt", "copy=true"])
    assert r.exit_code == 0, r.output
    n_val = sum(1 for v in plan["assignment"].values() if v == "val")
    assert len(list((out_yolo / "images").iterdir())) == n_val
    manifest = json.loads((out_yolo / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["subset"] == "val" and len(manifest["files"]) == 2 * n_val + 1

    out_coco = tmp_path / "coco"
    r = runner.invoke(app, ["data", "export", "--name", "flow", "--plan", "p1", "--subset", "val",
                            "--format", "coco", "--out", str(out_coco)])
    assert r.exit_code == 0, r.output
    doc = json.loads((out_coco / "instances.json").read_text(encoding="utf-8"))
    assert len(doc["images"]) == n_val and len(doc["annotations"]) == n_val
```

Run: `uv run pytest tests/unit/test_e2e_flow.py -q`
Expected: `1 passed`。

- [ ] **Step 2: 覆蓋率、lint、格式**

Run: `uv run pytest --cov=vcp --cov-report=term-missing`
Expected: 全部通過（含 3 skipped 整合測試），TOTAL ≥ 80%。對 `term-missing` 列出的明顯分支補測試（最可能：`yolo.py` 的 `load_names` 缺 `names`、`coco.py` 的 JSON 缺鍵、`dedup.py` 的 `against_paths is None`、`image_csv.py` 的 `cls` 多欄錯誤）；補到 ≥ 90% 即可，不追 100%。

Run: `uv run ruff check .` 與 `uv run ruff format --check .`
Expected: 乾淨；有格式差異就 `uv run ruff format src tests` 後重跑測試，獨立 `style:` commit。

- [ ] **Step 3: 對照 spec 驗收**

逐條寫一行「哪個測試 / 命令證明」：spec §13 的 2（海廢 CSV 匯入 → `tests/integration/test_marine_debris.py::test_marine_train_shape`，資料缺席時由 `test_csv_boxes.py` 的合成夾具代表）、4（`vcp data split` 在真實集 → 整合測試前置條件）、5（audit 在真實集 → `test_marine_coords_audit_runs`；合成 → `test_checks.py`、`test_audit_cli`）、6（export coco/yolo + manifest → `test_exporters.py`、`test_e2e_flow.py`）、10 前半（`image_csv` regression → `test_imagefolder_csv.py::test_image_csv_multilabel_and_regression`）；spec §14.1（`test_paths.py::test_store_and_resolve_paths`、`test_jsonl.py::test_paths_inside_data_root_are_stored_relative`、`test_reimport_with_changed_samples_counts_invalidated_plans`、`test_invalid_samples_do_not_write_manifest`）、§14.2（`test_exporters.py`）、§14.3（`test_dhash.py`、`test_checks.py`）、§14.4（Task 1 的四個測試）。

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_e2e_flow.py
git commit -m "test: 端到端流程（csv_boxes → audit → split --group-from-audit → export）與覆蓋率補強

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

（若有為覆蓋率補的測試檔，一併加入同一 commit。）

---

## 自我檢查紀錄（撰寫計畫時已執行）

- **Spec 覆蓋**：§6.1 五個匯入器 → Task 4–7；§6.2 匯出器 + manifest + sealed 開封 → Task 8；§8 稽核三項 + `groups.json` 給 `split --group-from-audit` → Task 9；§9 新命令 `export`、`audit` → Task 8、9；§11 整合測試 → Task 10；§14.1 → Task 1–2、匯入器共同慣例（Task 3–7）；§14.2 → Task 8；§14.3 → Task 9；§14.4 → Task 1；§14.5 的 2b 項目明確不在本計畫。
- **型別一致性**：`finalize_import(spec, importer, task, categories, image_root: str, samples, rows_read, skipped)` 在 Task 2 定義、Task 4–7 一致呼叫；`Exporter.run(dataset, samples, out, image_root, options) -> (files, warnings)` 在 Task 8 base 與兩個匯出器一致；`AuditCheck.applies/run(ctx)`、`AuditContext.out_dir` 在 Task 9 四個檔案一致；`DatasetPaths.resolve_image_root(card)` 由 Task 1 提供、Task 8/9 使用；`store_path(Path, Path) -> str` 由 Task 1 提供、Task 2 使用。
- **已知取捨**：`csv_boxes` 的越界框在 `on_bad_row=skip` 時只跳過該列、不丟整張影像（海廢的 EXIF 方向問題需在稽核 `coords` 再看；PIL header 尺寸不套用 EXIF 方向，與平台計分一致與否留待真實資料驗證）；YOLO 匯出以 `__` 攤平巢狀路徑以維持 `images/` ↔ `labels/` 對應；`dedup` 的 Pearson 確認對每個候選配對重讀影像，15k 圖規模可接受；整合測試依賴使用者先手動匯入（raw 檔名未知）。
- **佔位符掃描**：無 TBD / TODO；每個程式碼步驟都有完整程式碼。
