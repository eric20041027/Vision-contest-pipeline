# vcp 角色範圍存取與收據實作計畫（稽核 Wave 1b-1，Plan 9）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 「一個 job 讀了哪些資料」從自我宣告變成框架觀測：card-only 載入、按 plan 角色授權的 `DatasetAccess` 存取器、存取器在關閉時產生的 `AccessReceipt` 產物、以及 `train run` / `ingest` / `measure` / `judge` / `submit` 改吃「宣告 ∪ 觀測」；release 為 `0.5.0`。

**Architecture:** 新套件 `src/vcp/data/access/`（`schema.py` 模型、`receipt.py` 產物落地與 `ReceiptBinding` 協定、`access.py` 存取器）；`Dataset.load_card`；訓練層 `Session.access` / `SessionBinding` 實作綁定、`MaterializedReader` 改走存取器並成為 context manager、`train run` 把收據抄進 `run.yaml`；新 `src/vcp/measure/provenance.py` 算三級 provenance；`measure` / `judge` / `ingest --receipt` / `status` / `report`、`submit stage` / `final` / `status`、備份證據圖各接一處。所有收據都是 v0.4.0 的 `ArtifactWriter` 寫的產物（kind `access_receipt`）。

**Tech Stack:** Python 3.12、pydantic v2、typer 0.27、標準庫 `re` / `json` / `hashlib`、numpy（測試）、pytest、ruff（line-length 100）。

**Spec:** `docs/superpowers/specs/2026-09-12-vcp-access-receipts-design.md`（來源稽核 `docs/audits/2026-09-11-vcp-improvement-audit.md` VCP-001 / VCP-003；前置 `docs/superpowers/specs/2026-09-11-vcp-immutable-artifacts-design.md`）

## Global Constraints

- 取時只能用 `vcp.core.time.utc_now()` / `stamp()` / `parse_stamp()`（ruff TID251）。
- 每個 CLI 命令以 `VERDICT cmd=… status=OK|WARN|FAIL|ABORT …` 收尾，exit 0 / 0 / 1 / 2；`--json` 時結果 JSON 到 stdout、VERDICT 到 stderr；永不互動提問；不用 Click 層的參數驗證——所有檢查在函式層。
- 錯誤字彙：`denied:`（`AccessDeniedError`，FAIL）、`contaminated:`、`observed_sealed:`、`provenance_required:`（`ValidationFailed`，FAIL）、`mismatch:`（`IntegrityError`，FAIL）；sealed 無 reason → `SealedSubsetError`（沿用，ABORT）；WARN 欄位 `receipt_invalid=`、`observed_beyond_trained_on=`。`VcpError.fields` 只放機器可讀鍵。pydantic `ValidationError` 在 CLI 包成 `ValidationFailed(str(e))`。
- **未授權的列從不被解析**：存取器只 peek 行首 `sample_id`，未授權角色的行只過 hasher。**收據由存取器累計**，呼叫端只能給 `notes`。**產物在 open 時 claim**、close（含例外）時 commit。**宣告蓋不掉觀測**：乾淨基底 = `clean_eval_subsets(plan, trained_on ∪ observed)`。**provenance 算出來不存**：`receipt > export > declared`。
- `src/vcp/data/access/__init__.py` 只有 docstring，`schema.py` 只 import `vcp.core.*` 與 pydantic——`vcp.measure.schema` 會 import 它，而 `access.py` 透過 artifact → measure.ledger → measure.schema 回到它；`__init__` 一有 import 就成環。
- 資料準備命令（`data validate/split/audit/materialize/import`）、`ingest`、`fuse`、`profile` 維持 `Dataset.load`（見計畫層決定 3）。
- 隱私：收據只含 sha、計數、子集名、data root 相對路徑、時戳、build string、`notes`；sample id 只在 `denied_first`（≤ 5）。
- pydantic 模型 `extra="forbid"`；新欄位一律有預設（舊 `run.yaml` / `train.yaml` / 台帳 / `stage.json` 照讀）；檔案 utf-8、LF；測試永不碰真資料根（`roots` fixture）；覆蓋率 ≥ 80%；`uv run ruff check .` 與 `uv run ruff format --check .` 乾淨。
- 一件事一個分支一個 commit（`type(scope): 說明`），不用 `git add -A`；commit 訊息結尾加一行 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`（第二個 `-m`）；commit 後 `git log -1 --format=%B` 確認尾行，不符就 amend。
- **不要對 markdown 跑 `ruff format`**（它會格式化 README 裡的 Python 區塊；要改就手改成 ruff 的樣子）。

## 計畫層決定（spec 未明說之處；Task 12 寫進 spec §16）

1. **`read_receipt` 放 `data/access/receipt.py`**（收據格式是資料層的），`measure/provenance.py` import 它；spec §4.3 寫在 provenance.py 只是位置不同。
2. **`DatasetAccess.open` 多一個 `run_id: str | None = None`**：無 binding 的收據（measure、stage）用它填 `AccessReceipt.run_id`；有 binding 時 binding 的 `run_id` 優先。
3. **哪些呼叫點走存取器 / card-only / 維持 `Dataset.load`**：`train run` 父程序 → `load_card`；`MaterializedReader`（有 plan / subset）、`export_subset`、`measure_run`、`stage` 的 test 資料集讀取 → 存取器；`ingest`（converter 與 `check_predictions` 要 eval 子集的 rows；那是 eval 側的讀）、`fuse build/ablate`（`check_predictions`）、`submit profile`（`ensure_test_plan` 要全部樣本）、`stage` 的 eval 資料集（只要 card）→ `ingest` / `fuse` / `profile` 維持 `Dataset.load`，`stage` eval 側改 `load_card`；`submit verify`（從 test run 重渲染比對位元組）也維持 `Dataset.load`——它驗的是檔案，不是訓練讀，不留收據。`judge` / `sigma` / `anchor` 繼續用 `load_context`（回 `Dataset`）；`measure_run` 改用新的 `load_card_context`。
4. **`assert_run_matches(card, dataset_card: DatasetCard)`**：第二個參數改收 card；六個呼叫點傳 `dataset.card` 或 card。
5. **`Dataset.subset` 的 unseal 留痕抽成 `append_unseal(paths, plan, subset, reason, caller) -> str`**（回該行 sha），存取器與 `subset()` 共用。
6. **`MaterializedReader` 建構時就迭代授權子集**（與今天 `subset()` 一樣把 `Sample` 讀進記憶體），陣列仍是逐筆 lazy。`reader.dataset` 移除，改 `reader.card` / `reader.access`；不在 run 下、無 plan / subset 的用法維持 `Dataset.load`（無收據）。
7. **`train run` 的 provenance 用 `measure.provenance.provenance()` 算**（不是從 refs 湊）；`RunResult` 多 `receipts` / `denied` / `provenance` / `observed_beyond` / `receipt_invalid`，WARN 走既有 `warnings` 機制加 VERDICT 欄位。
8. **fusion run 的 provenance 遞迴**在 `provenance()` 內用 lazy import（`from vcp.fuse.build import load_record, record_path`）避免 measure ↔ fuse 循環。
9. **`FinalEntry.provenance: Grade | None = None`**（spec 沒列；final 的表要能印等級）；`submit status` 只在 human 行與 payload 印每筆 staged 的等級，`eval status` 多 `receipt_runs=` `export_runs=` `declared_runs=` 三個計數欄位。
10. **judge 的 contaminated 檢查在 step 1 之前**：一旦相交就 `INVALID`，reason `contaminated:<run>/<subset>`，不再做 bootstrap。
11. **stage 的 test 資料集讀取**：`DatasetAccess.open(profile.dataset, profile.test_plan, subsets={profile.test_subset}, purpose="submit", run_id=spec.eval_run)`，樣本組 `Dataset(access.card, samples)` 視圖交給 writer（`WriteContext.dataset` 型別不變）。
12. **索引時 `json.loads` 只解碼 `sample_id` 那個 JSON 字串**（`json.loads(b'"' + m.group(1) + b'"')`），整行不解析。

## 檔案結構

| 檔案 | 責任 |
|---|---|
| `src/vcp/core/errors.py` | `AccessDeniedError` |
| `src/vcp/data/dataset.py` | `Dataset.load_card`、`append_unseal`；`load` 改用 `load_card` |
| `src/vcp/measure/runs.py` + 六個呼叫點 | `assert_run_matches(card, dataset_card)` |
| `src/vcp/data/access/__init__.py` | docstring |
| `src/vcp/data/access/schema.py` | `Purpose` / `Binding` / `Grade` / `GRADE_RANK`、`AccessedSubset`、`AccessReceipt`、`AccessRef` |
| `src/vcp/data/access/receipt.py` | `ReceiptBinding`、`standalone_receipt_id`、`receipt_spec`、`claim_receipt`、`read_receipt` / `ReceiptFile` |
| `src/vcp/data/access/access.py` | `DatasetAccess` |
| `src/vcp/data/exporters/base.py`、`src/vcp/cli.py` | export 走存取器、manifest `receipt`、VERDICT `receipt=` |
| `src/vcp/train/schema.py`、`session.py`、`reader.py`、`run.py`、`cli_train.py`、`__init__.py` | `TrainRecord.access`、`access` 事件、`SessionBinding` / `Session.access`、reader context manager、`train run` 綁定與 WARN |
| `src/vcp/measure/schema.py`、`provenance.py`、`measure.py`、`judge.py`、`ingest.py`、`report.py`、`cli_eval.py` | `RunCard.access`、`Reading.provenance`、`Judgement.provenance`、三級 provenance、`contaminated:`、`--receipt`、status / report |
| `src/vcp/submit/schema.py`、`stage.py`、`final.py`、`report.py`、`cli_submit.py` | `require_provenance`、`Staged.provenance`、`observed_sealed:` / `provenance_required:`、final 重算、status |
| `src/vcp/backup/schema.py`、`evidence.py` | `access_receipt` 角色、`walk_run` |
| `projects/rsna-knee/rsna_knee/training.py` | `with MaterializedReader(...)` |
| `tests/unit/data/test_access.py`、`test_access_schema.py`、`test_dataset.py`、`exporters/test_exporters.py`、`tests/unit/train/test_reader.py`、`test_session.py`、`test_run.py`、`tests/unit/measure/test_provenance.py`、`test_measure.py`、`test_prereg_judge.py`、`tests/unit/test_cli_eval.py`、`tests/unit/submit/test_stage.py`、`test_final.py`、`tests/unit/backup/test_evidence_run.py`、`tests/unit/test_e2e_access.py`、`tests/unit/test_regression_gate.py`、`tests/integration/test_access_receipts.py` | 測試 |
| `README.md`、`CLAUDE.md`、`AGENTS.md`、`docs/handover/HANDOVER.md`、`CHANGELOG.md`、`src/vcp/__init__.py`、spec §16 | 文件與發版 |

## 給實作者的共用約定

- 測試用 `roots` fixture（`tests/conftest.py`）、`tests/helpers.py`（`det_samples`、`make_card`、`write_images`、`det_with_runs`、`perfect_predictions`、`noisy_predictions`）、`tests/submit_fixtures.py`（`make_pair`、`seed_eval_runs`、`seed_judgements`、`seed_test_runs`、`ingest_run`、`EVAL`、`TEST`、`STAMP`）；CLI 測試用 `typer.testing.CliRunner` 對 `vcp.cli.app`，VERDICT 取 `r.output` 最後一行 `VERDICT `。
- 寫完 `.py` 先 `uv run ruff format <檔案>` 再跑測試；pytest 用 `uv run pytest <路徑> -o addopts="" -q`；每個任務結尾 `uv run ruff check . && uv run ruff format --check .` 乾淨才 commit。
- `ruff` 的 isort 把 `helpers` / `submit_fixtures` / `backup_fixtures` 當第一方，接受它排出來的順序；新 import 用 `uv run ruff check --fix <檔案>` 排。
- 本計畫的程式碼片段是要照抄的實作；型別、函式名、`reason=` 字彙以片段為準。

---

### Task 1: `AccessDeniedError`、`Dataset.load_card`、`append_unseal`、`assert_run_matches` 收 card

**Files:**
- Modify: `src/vcp/core/errors.py`（`PlatformError` 之後）、`src/vcp/data/dataset.py:86-172`、`src/vcp/measure/runs.py:47-62`、`src/vcp/measure/ingest.py:141`、`src/vcp/measure/measure.py:64,176`、`src/vcp/fuse/members.py:55`、`src/vcp/submit/stage.py:88`、`src/vcp/train/run.py:282`
- Test: `tests/unit/data/test_dataset.py`、`tests/unit/core/test_errors.py`

**Interfaces:**
- Consumes: 既有 `DatasetPaths`、`load_yaml_model`、`SealedSubsetError`。
- Produces: `vcp.core.errors.AccessDeniedError(VcpError)`（`status="FAIL"`）；`Dataset.load_card(name, *, data_root=None, configs_root=None) -> DatasetCard`；`vcp.data.dataset.append_unseal(paths, plan, subset, reason, caller) -> str`（回追加那一行的 sha256）；`assert_run_matches(card: RunCard, dataset_card: DatasetCard)`。

- [ ] **Step 1: 寫失敗的測試**——追加到 `tests/unit/data/test_dataset.py` 檔尾（import 區加 `from vcp.data.dataset import append_unseal` 併進既有那行、`from vcp.core.hashing import sha256_text`）：

```python
def test_load_card_reads_only_the_card(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(make_card("det", name="tiny", image_root="raw/tiny"), det_samples(4))
    ds.save(paths)
    paths.samples_jsonl.unlink()  # a card-only load must not need the samples file
    card = Dataset.load_card("tiny", data_root=roots.data, configs_root=roots.configs)
    assert card.name == "tiny" and card.samples_hash == ds.card.samples_hash
    with pytest.raises(ValidationFailed, match="dataset card not found"):
        Dataset.load_card("nope", data_root=roots.data, configs_root=roots.configs)
    with pytest.raises(ValidationFailed, match="samples file not found"):
        Dataset.load("tiny", data_root=roots.data, configs_root=roots.configs)


def test_append_unseal_writes_one_line_and_returns_its_sha(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(make_card("det", name="tiny", image_root="raw/tiny"), det_samples(40))
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    sha = append_unseal(paths, plan, "holdout", "final eval", "test")
    lines = paths.unseal_jsonl("fixed-v1").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and sha == sha256_text(lines[0] + "\n")
    row = json.loads(lines[0])
    assert row["subset"] == "holdout" and row["reason"] == "final eval" and row["caller"] == "test"
    assert row["plan_id"] == "fixed-v1" and row["dataset_hash"] == plan.dataset_hash
    assert row["ts"].endswith("Z")
    ds.subset("holdout", plan, unseal=True, reason="again", paths=paths)
    assert len(paths.unseal_jsonl("fixed-v1").read_text(encoding="utf-8").splitlines()) == 2
```

追加到 `tests/unit/core/test_errors.py` 檔尾（import 區加 `AccessDeniedError`）：

```python
def test_access_denied_is_a_fail():
    e = AccessDeniedError("denied: subset 'valA' is not authorized", fields={"subset": "valA"})
    assert e.status == "FAIL" and e.fields == {"subset": "valA"}
    assert str(e).startswith("denied: ")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/test_dataset.py tests/unit/core/test_errors.py -o addopts="" -q`
Expected: FAIL（`ImportError: cannot import name 'append_unseal'` / `AccessDeniedError`）

- [ ] **Step 3: 改四處**

`src/vcp/core/errors.py`——`PlatformError` 之後加：

```python
class AccessDeniedError(VcpError):
    """A role-scoped accessor refused a read the caller was not authorised for. Actionable:
    open the access with the subset in its allowed set, or stop reading it."""

    status = "FAIL"
```

`src/vcp/data/dataset.py`——`Dataset.load` 前面加 `load_card`，`load` 改用它；`subset()` 的留痕改呼叫模組函式 `append_unseal`（放在 `read_samples_jsonl` 之後、`class Dataset` 之前）：

```python
def append_unseal(paths: DatasetPaths, plan: SplitPlan, subset: str, reason: str, caller: str) -> str:
    """Record one opening of a sealed subset (spec 7.4) and return the sha256 of the line."""
    record = {
        "ts": stamp(),
        "plan_id": plan.plan_id,
        "dataset_hash": plan.dataset_hash,
        "subset": subset,
        "reason": reason,
        "caller": caller,
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    target = paths.unseal_jsonl(plan.plan_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line)
    return sha256_text(line)
```

（`SplitPlan` 只在 `TYPE_CHECKING` 下 import，函式簽名用字串註解即可——檔案已有 `from __future__ import annotations`。）

```python
    @classmethod
    def load_card(
        cls, name: str, *, data_root: Path | None = None, configs_root: Path | None = None
    ) -> DatasetCard:
        """The card alone: no samples file is opened, hashed or parsed (spec 6.1)."""
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        if not paths.card_yaml.is_file():
            raise ValidationFailed(f"dataset card not found: {paths.card_yaml}")
        card = load_yaml_model(paths.card_yaml, DatasetCard)
        if card.name != name:
            raise ValidationFailed(
                f"card name {card.name!r} != {name!r}", location=str(paths.card_yaml)
            )
        return card

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
        card = cls.load_card(name, data_root=data_root, configs_root=configs_root)
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
```

`subset()` 裡從 `record = {` 到 `f.write(...)` 的九行換成一行 `append_unseal(paths, plan, name, reason, caller or "unknown")`。

`src/vcp/measure/runs.py`——`assert_run_matches` 改成：

```python
def assert_run_matches(card: RunCard, dataset_card: DatasetCard) -> None:
    """A run must keep pointing at the dataset (by name and content) it was created on.

    Takes the card alone so a caller that never parsed the samples file (spec 6.1) can still
    make the check; one implementation for ingest, measure, fuse, stage and train run.
    """
    if card.dataset != dataset_card.name:
        raise PlanMismatchError(
            f"run {card.run_id!r} belongs to dataset {card.dataset!r}, not {dataset_card.name!r}"
        )
    if card.samples_hash != dataset_card.samples_hash:
        raise PlanMismatchError(
            f"run {card.run_id!r} was created on samples_hash {card.samples_hash[:12]}, "
            f"dataset now has {dataset_card.samples_hash[:12]}"
        )
```

（import 改 `from vcp.data.schema import DatasetCard`，拿掉 `from vcp.data.dataset import Dataset` 若不再用。）六個呼叫點各把第二個引數改成 card：`ingest.py:141` `assert_run_matches(card, dataset.card)`；`measure.py:64` `assert_run_matches(card, dataset.card)`、`measure.py:176` `assert_run_matches(anchor_run, ctx.dataset.card)`；`fuse/members.py:55` `assert_run_matches(card, dataset.card)`；`submit/stage.py:88` `assert_run_matches(card, dataset.card)`；`train/run.py:282` `assert_run_matches(card, dataset.card)`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/core/errors.py src/vcp/data/dataset.py src/vcp/measure/runs.py tests/unit/data/test_dataset.py tests/unit/core/test_errors.py && uv run pytest tests/unit/data tests/unit/core tests/unit/measure tests/unit/fuse tests/unit/submit tests/unit/train -o addopts="" -q`
Expected: 全部通過（呼叫點只換引數，行為不變）

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/core/errors.py src/vcp/data/dataset.py src/vcp/measure/runs.py src/vcp/measure/ingest.py src/vcp/measure/measure.py src/vcp/fuse/members.py src/vcp/submit/stage.py src/vcp/train/run.py tests/unit/data/test_dataset.py tests/unit/core/test_errors.py
git commit -m "feat(data): Dataset.load_card、append_unseal、AccessDeniedError；assert_run_matches 收 card" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `data/access/schema.py`——收據模型

**Files:**
- Create: `src/vcp/data/access/__init__.py`、`src/vcp/data/access/schema.py`
- Test: `tests/unit/data/test_access_schema.py`

**Interfaces:**
- Consumes: pydantic、`vcp.core.paths.validate_name`（只透過 ValueError 包裝）。
- Produces: `Purpose = Literal["train","export","measure","submit","custom"]`、`Binding = Literal["session","manual"]`、`Grade = Literal["receipt","export","declared"]`、`GRADE_RANK`、`AccessedSubset(role, ids_count, ids_sha256, records_parsed)`、`AccessReceipt(...)`（spec §4.1）、`AccessRef(artifact_id, purpose, subsets, sealed_accessed, denied, receipt_sha256, binding)`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/data/test_access_schema.py`

```python
import pytest
from pydantic import ValidationError

from vcp.data.access.schema import (
    GRADE_RANK,
    AccessedSubset,
    AccessReceipt,
    AccessRef,
)

SHA = "a" * 64
T0 = "2026-09-12T00:00:00.000Z"
T1 = "2026-09-12T00:00:01.000Z"


def _receipt(**over) -> AccessReceipt:
    base = {
        "dataset": "tiny",
        "samples_hash": SHA,
        "card_sha256": "b" * 64,
        "plan_id": "fixed-v1",
        "plan_sha256": "c" * 64,
        "authorization_sha256": "d" * 64,
        "purpose": "train",
        "allowed": ["train"],
        "roles": {"train": "train"},
        "accessed": {},
        "outcome": "completed",
        "started_at": T0,
        "finished_at": T1,
        "vcp_version": "0.5.0",
    }
    return AccessReceipt.model_validate({**base, **over})


def test_grade_rank_orders_the_three_grades():
    assert GRADE_RANK["declared"] < GRADE_RANK["export"] < GRADE_RANK["receipt"]


def test_receipt_defaults_and_round_trip():
    r = _receipt()
    assert r.schema_version == 1 and r.fields == ["all"] and r.denied == 0
    assert r.denied_first == [] and not r.sealed_accessed and r.run_id is None
    assert r.exception is None and r.notes == ""
    assert AccessReceipt.model_validate_json(r.model_dump_json()) == r
    with pytest.raises(ValidationError, match="Extra inputs"):
        _receipt(eval_accessed=False)


def test_receipt_validators():
    with pytest.raises(ValidationError, match="allowed must be sorted"):
        _receipt(allowed=["valA", "train"], roles={"valA": "eval", "train": "train"})
    with pytest.raises(ValidationError, match="roles must name exactly the allowed subsets"):
        _receipt(roles={})
    with pytest.raises(ValidationError, match="accessed subsets must be allowed"):
        _receipt(
            accessed={
                "valA": AccessedSubset(role="eval", ids_count=1, ids_sha256=SHA, records_parsed=1)
            }
        )
    with pytest.raises(ValidationError, match="at most 5"):
        _receipt(denied=6, denied_first=["valA"] * 6)
    with pytest.raises(ValidationError, match="failed needs the exception"):
        _receipt(outcome="failed")
    with pytest.raises(ValidationError, match="finished_at"):
        _receipt(started_at=T1, finished_at=T0)
    with pytest.raises(ValidationError, match="Input should be"):
        _receipt(purpose="materialize")
    ok = _receipt(
        outcome="failed",
        exception="RuntimeError",
        accessed={
            "train": AccessedSubset(role="train", ids_count=3, ids_sha256=SHA, records_parsed=3)
        },
    )
    assert ok.accessed["train"].role == "train"


def test_access_ref_round_trip():
    ref = AccessRef(
        artifact_id="r1-a1-1",
        purpose="train",
        subsets=["train"],
        sealed_accessed=False,
        denied=0,
        receipt_sha256=SHA,
        binding="session",
    )
    assert AccessRef.model_validate_json(ref.model_dump_json()) == ref
    with pytest.raises(ValidationError):
        AccessRef.model_validate({**ref.model_dump(), "binding": "auto"})
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/test_access_schema.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'vcp.data.access'`）

- [ ] **Step 3: 寫 `src/vcp/data/access/__init__.py`**

```python
"""Role-scoped dataset access and the receipts it leaves behind (spec 2026-09-12). Nothing is
imported here on purpose: ``schema`` is reached from the measurement layer's schema, and
``access`` reaches the artifact layer, which reaches the measurement layer -- an import in this
file would close that loop."""
```

**寫 `src/vcp/data/access/schema.py`**

```python
"""Models of the access layer (spec 4): what an accessor was allowed, what it actually read,
and how a run refers to that receipt. Only ``vcp.core`` and pydantic may be imported here."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Purpose = Literal["train", "export", "measure", "submit", "custom"]
Binding = Literal["session", "manual"]
Grade = Literal["receipt", "export", "declared"]
Role = Literal["train", "eval", "sealed"]
Outcome = Literal["completed", "failed"]
GRADE_RANK: dict[str, int] = {"declared": 0, "export": 1, "receipt": 2}
MAX_DENIED_FIRST = 5
_SHA = re.compile(r"^[0-9a-f]{64}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccessedSubset(_Strict):
    """One subset the accessor actually iterated: how many distinct ids, their identity, and
    how many rows were parsed (a row read twice is parsed once)."""

    role: Role
    ids_count: int = Field(ge=0)
    ids_sha256: str
    records_parsed: int = Field(ge=0)


class AccessReceipt(_Strict):
    """``receipt.json`` of an ``access_receipt`` artifact. Built by the accessor at close; the
    caller's only field is ``notes``."""

    schema_version: int = 1
    dataset: str
    samples_hash: str
    card_sha256: str
    plan_id: str
    plan_sha256: str
    authorization_sha256: str
    purpose: Purpose
    run_id: str | None = None
    attempt: int | None = None
    allowed: list[str]
    roles: dict[str, str]
    accessed: dict[str, AccessedSubset]
    fields: list[str] = Field(default_factory=lambda: ["all"])
    denied: int = Field(default=0, ge=0)
    denied_first: list[str] = Field(default_factory=list)
    sealed_accessed: bool = False
    unseal_event_sha256: str | None = None
    outcome: Outcome
    exception: str | None = None
    started_at: str
    finished_at: str
    vcp_version: str
    notes: str = ""

    @model_validator(mode="after")
    def _shape(self) -> AccessReceipt:
        if self.allowed != sorted(self.allowed) or len(set(self.allowed)) != len(self.allowed):
            raise ValueError("allowed must be sorted and unique")
        if set(self.roles) != set(self.allowed):
            raise ValueError("roles must name exactly the allowed subsets")
        stray = sorted(set(self.accessed) - set(self.allowed))
        if stray:
            raise ValueError(f"accessed subsets must be allowed: {stray}")
        if len(self.denied_first) > MAX_DENIED_FIRST:
            raise ValueError(f"denied_first keeps at most {MAX_DENIED_FIRST} entries")
        if self.outcome == "failed" and not self.exception:
            raise ValueError("outcome=failed needs the exception class name")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        for name, value in (
            ("samples_hash", self.samples_hash),
            ("card_sha256", self.card_sha256),
            ("plan_sha256", self.plan_sha256),
            ("authorization_sha256", self.authorization_sha256),
        ):
            if not _SHA.fullmatch(value):
                raise ValueError(f"{name} must be 64 hex characters")
        return self


class AccessRef(_Strict):
    """How ``train.yaml`` / ``run.yaml`` refer to one receipt (spec 4.1)."""

    artifact_id: str
    purpose: Purpose
    subsets: list[str]
    sealed_accessed: bool
    denied: int = Field(ge=0)
    receipt_sha256: str
    binding: Binding
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/data/access tests/unit/data/test_access_schema.py && uv run pytest tests/unit/data/test_access_schema.py -o addopts="" -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/data/access/__init__.py src/vcp/data/access/schema.py tests/unit/data/test_access_schema.py
git commit -m "feat(access): AccessReceipt / AccessRef 模型與 provenance 等級" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `data/access/receipt.py` 與 `access.py`——存取器與收據落地

**Files:**
- Create: `src/vcp/data/access/receipt.py`、`src/vcp/data/access/access.py`
- Test: `tests/unit/data/test_access.py`

**Interfaces:**
- Consumes: Task 1 `Dataset.load_card` / `append_unseal` / `AccessDeniedError`；Task 2 模型；`vcp.artifact.writer.ArtifactWriter`、`vcp.artifact.store.load_manifest` / `verify`、`vcp.artifact.schema.ArtifactSpec` / `InputRef`；`vcp.data.split.load_plan` / `assert_plan_matches`；`vcp.data.tasks.get_task`；`vcp.core.hashing.sha256_file` / `sha256_text` / `sha256_json`；`vcp.core.build.build_string`；`vcp.core.time.stamp` / `utc_now`。
- Produces（`receipt.py`）：`ReceiptBinding`（Protocol：`run_id: str`、`attempt: int`、`receipt_id(seq) -> str`、`next_seq() -> int`、`on_commit(ref: AccessRef) -> None`）；`standalone_receipt_id(purpose, dataset, plan_id) -> str`；`receipt_spec(...) -> ArtifactSpec`；`claim_receipt(spec, data_root, binding) -> ArtifactWriter`（binding 給了就重試 seq）；`ReceiptFile(receipt: AccessReceipt, sha256: str)`；`read_receipt(data_root, artifact_id) -> ReceiptFile`。
- Produces（`access.py`）：`DatasetAccess.open(name, plan_id, *, subsets=None, roles=None, purpose="custom", unseal_reason=None, caller=None, binding=None, run_id=None, notes="", data_root=None, configs_root=None) -> DatasetAccess`；屬性 `card`、`plan`、`paths`、`allowed: frozenset[str]`、`roles: dict[str, str]`、`receipt: AccessReceipt | None`、`receipt_id: str`；方法 `ids(subset) -> list[str]`、`iter(subset) -> Iterator[Sample]`、`records(subset) -> dict[str, Sample]`、`by_id(sample_id) -> Sample`、`subset_of(sample_id) -> str`、`close() -> AccessReceipt`；context manager。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/data/test_access.py`

```python
import json

import pytest

from helpers import det_samples, make_card, write_images
from vcp.artifact import store
from vcp.core.config import dump_yaml_model
from vcp.core.errors import (
    AccessDeniedError,
    IntegrityError,
    InvariantError,
    SealedSubsetError,
    ValidationFailed,
)
from vcp.core.hashing import sha256_file, sha256_json, sha256_text
from vcp.core.paths import DatasetPaths, artifact_dir
from vcp.data.access.access import DatasetAccess
from vcp.data.access.receipt import read_receipt, standalone_receipt_id
from vcp.data.access.schema import AccessRef
from vcp.data.dataset import Dataset
from vcp.data.schema import sample_json_line
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan

SECRET = "fakesecretfakesecretfakesecret1234"


def _seed(roots, name="tiny", n=40):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def _open(roots, **kw):
    kw.setdefault("subsets", {"train"})
    kw.setdefault("purpose", "custom")
    return DatasetAccess.open(
        "tiny", "fixed-v1", data_root=roots.data, configs_root=roots.configs, **kw
    )


def _sabotage(ds, plan, paths, *, keep=("train",)):
    """Corrupt the JSON of every row outside ``keep`` (the ``sample_id`` prefix stays), then
    re-point the card and the plan at the new bytes so identity still verifies."""
    lines = []
    for s in ds.samples:
        line = sample_json_line(s)
        if plan.assignment[s.sample_id] not in keep:
            line = line[:-1] + ", BROKEN}"
        lines.append(line)
    paths.samples_jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    digest = sha256_file(paths.samples_jsonl)
    dump_yaml_model(ds.card.model_copy(update={"samples_hash": digest}), paths.card_yaml)
    forged = plan.model_copy(update={"dataset_hash": digest})
    paths.plan_json("fixed-v1").write_text(
        json.dumps(forged.model_dump(mode="json"), ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return digest


def test_sample_json_line_starts_with_the_sample_id():
    line = sample_json_line(det_samples(1, seed=0)[0])
    assert line.startswith('{"sample_id": "s0000"')


def test_train_only_access_never_parses_other_rows(roots):
    ds, plan, paths = _seed(roots)
    digest = _sabotage(ds, plan, paths, keep=("train",))
    with _open(roots, purpose="train") as access:
        assert access.allowed == frozenset({"train"}) and access.roles == {"train": "train"}
        got = list(access.iter("train"))
        assert [s.sample_id for s in got] == sorted(plan.ids_in("train"))
        assert access.records("train")[got[0].sample_id] == got[0]
        assert access.by_id(got[0].sample_id) == got[0]
        assert access.subset_of(sorted(plan.ids_in("valA"))[0]) == "valA"
    r = access.receipt
    assert r is not None and r.outcome == "completed" and r.samples_hash == digest
    assert set(r.accessed) == {"train"}
    train = r.accessed["train"]
    assert train.role == "train" and train.ids_count == len(plan.ids_in("train"))
    assert train.records_parsed == train.ids_count
    assert train.ids_sha256 == sha256_text("\n".join(sorted(plan.ids_in("train"))))
    assert r.denied == 0 and not r.sealed_accessed and r.purpose == "train"
    # the same corrupted rows fail as soon as someone is authorised to read them
    with pytest.raises(ValidationFailed) as ei:
        with _open(roots, subsets={"valA"}) as bad:
            list(bad.iter("valA"))
    assert "BROKEN" not in str(ei.value) and paths.samples_jsonl.name in str(ei.value)


def test_unauthorized_subsets_fail_closed_and_are_counted(roots):
    ds, plan, paths = _seed(roots)
    valid = sorted(plan.ids_in("valA"))[0]
    with _open(roots) as access:
        with pytest.raises(AccessDeniedError, match="^denied: subset 'valA' is not authorized") as ei:
            access.iter("valA")
        assert ei.value.fields == {"subset": "valA"}
        with pytest.raises(AccessDeniedError, match="^denied: "):
            access.ids("valB")
        with pytest.raises(AccessDeniedError, match="^denied: "):
            access.records("holdout")
        with pytest.raises(AccessDeniedError, match="^denied: "):
            access.by_id(valid)
        with pytest.raises(ValidationFailed, match="^not_found: sample 'nope'"):
            access.by_id("nope")
        assert access.ids("train") == sorted(plan.ids_in("train"))
    r = access.receipt
    assert r.denied == 4 and r.denied_first == ["valA", "valB", "holdout", f"valA:{valid}"]
    assert set(r.accessed) == set()  # ids() alone is not a read


def test_roles_expand_to_subsets_and_sealed_needs_a_reason(roots):
    ds, plan, paths = _seed(roots)
    with _open(roots, roles={"eval"}) as access:
        assert access.allowed == frozenset({"valA", "valB"})
    with pytest.raises(SealedSubsetError, match="sealed"):
        _open(roots, subsets={"holdout"})
    assert not paths.unseal_jsonl("fixed-v1").is_file()
    with _open(roots, subsets={"train", "holdout"}, unseal_reason="final eval", caller="t") as a:
        rows = list(a.iter("holdout"))
        assert len(rows) == len(plan.ids_in("holdout"))
    lines = paths.unseal_jsonl("fixed-v1").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["caller"] == "t"
    r = a.receipt
    assert r.sealed_accessed and r.unseal_event_sha256 == sha256_text(lines[0] + "\n")
    assert r.roles == {"holdout": "sealed", "train": "train"}
    with pytest.raises(ValidationFailed, match="exactly one of subsets or roles"):
        _open(roots, subsets={"train"}, roles={"train"})
    with pytest.raises(ValidationFailed, match="exactly one of subsets or roles"):
        DatasetAccess.open("tiny", "fixed-v1", data_root=roots.data, configs_root=roots.configs)


def test_identity_and_index_checks(roots):
    ds, plan, paths = _seed(roots)
    original = paths.samples_jsonl.read_bytes()
    paths.samples_jsonl.write_bytes(original.replace(b'"gold"', b'"none"', 1))
    with pytest.raises(IntegrityError, match="^mismatch: samples.jsonl"):
        _open(roots)
    paths.samples_jsonl.write_bytes(b"not a line\n" + original)
    with pytest.raises(ValidationFailed, match="not a samples.jsonl line") as ei:
        _open(roots)
    assert ei.value.location.endswith(":1")
    paths.samples_jsonl.write_bytes(original)
    forged = plan.model_copy(update={"assignment": {**plan.assignment, "ghost": "train"}})
    paths.plan_json("fixed-v1").write_text(
        json.dumps(forged.model_dump(mode="json"), ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(InvariantError, match="ghost"):
        _open(roots)


def test_receipt_is_an_artifact_claimed_at_open(roots):
    ds, plan, paths = _seed(roots)
    access = _open(roots, notes="hand-written loop")
    rid = access.receipt_id
    assert rid.startswith("custom-tiny-fixed-v1-") and len(rid) == len(
        standalone_receipt_id("custom", "tiny", "fixed-v1")
    )
    assert store.is_partial(roots.data, "access_receipt", rid)  # claimed, not yet committed
    list(access.iter("train"))
    receipt = access.close()
    assert access.close() is receipt  # idempotent
    loaded = read_receipt(roots.data, rid)
    assert loaded.receipt == receipt and loaded.receipt.notes == "hand-written loop"
    manifest = store.load_manifest(roots.data, "access_receipt", rid)
    assert [f.name for f in manifest.files] == ["receipt.json"]
    assert loaded.sha256 == manifest.files[0].sha256
    assert manifest.spec.dataset == "tiny" and manifest.spec.plan_id == "fixed-v1"
    assert manifest.spec.params == {"purpose": "custom"}
    inputs = {i.name: i for i in manifest.spec.inputs}
    assert inputs["samples"].sha256 == ds.card.samples_hash
    assert inputs["plan"].sha256 == sha256_file(paths.plan_json("fixed-v1"))
    assert inputs["card"].sha256 == sha256_file(paths.card_yaml) == receipt.card_sha256
    assert receipt.authorization_sha256 == sha256_json(
        {
            "card_sha256": receipt.card_sha256,
            "samples_hash": receipt.samples_hash,
            "plan_sha256": receipt.plan_sha256,
            "allowed": ["train"],
        }
    )
    assert store.verify(roots.data, "access_receipt", rid) == store.VerifyResult([], [], [], False)
    # a second open gets a different id
    other = _open(roots)
    assert other.receipt_id != rid
    other.close()


def test_receipt_is_written_even_when_the_job_fails(roots):
    ds, plan, paths = _seed(roots)
    with pytest.raises(RuntimeError, match="upload"):
        with _open(roots, purpose="train") as access:
            list(access.iter("train"))
            raise RuntimeError(f"upload failed key={SECRET}")
    r = access.receipt
    assert r.outcome == "failed" and r.exception == "RuntimeError" and "train" in r.accessed
    d = artifact_dir(roots.data, "access_receipt", access.receipt_id)
    assert (d / "manifest.json").is_file()
    for p in d.rglob("*"):
        if p.is_file():
            assert SECRET.encode() not in p.read_bytes(), p


def test_unclosed_access_leaves_a_partial_and_drift_is_refused(roots):
    ds, plan, paths = _seed(roots)
    access = _open(roots)
    assert store.is_partial(roots.data, "access_receipt", access.receipt_id)
    del access
    access = _open(roots)
    list(access.iter("train"))
    paths.samples_jsonl.write_bytes(paths.samples_jsonl.read_bytes() + b"\n")
    with pytest.raises(IntegrityError, match="^drift: input 'samples'"):
        access.close()
    assert access.receipt is None
    assert store.is_partial(roots.data, "access_receipt", access.receipt_id)


def test_binding_names_the_receipt_and_is_told_on_commit(roots):
    ds, plan, paths = _seed(roots)
    seen: list[AccessRef] = []

    class Binding:
        run_id = "r1"
        attempt = 2

        def receipt_id(self, seq: int) -> str:
            return f"r1-a2-{seq}"

        def next_seq(self) -> int:
            return 1

        def on_commit(self, ref: AccessRef) -> None:
            seen.append(ref)

    with _open(roots, purpose="train", binding=Binding()) as first:
        list(first.iter("train"))
    assert first.receipt_id == "r1-a2-1" and first.receipt.run_id == "r1"
    assert first.receipt.attempt == 2
    with _open(roots, purpose="train", binding=Binding()) as second:  # seq 1 is taken -> 2
        pass
    assert second.receipt_id == "r1-a2-2"
    assert [r.artifact_id for r in seen] == ["r1-a2-1", "r1-a2-2"]
    assert seen[0].subsets == ["train"] and seen[0].binding == "session" and seen[0].denied == 0
    assert seen[1].subsets == [] and seen[0].purpose == "train"
    assert seen[0].receipt_sha256 == read_receipt(roots.data, "r1-a2-1").sha256
    manifest = store.load_manifest(roots.data, "access_receipt", "r1-a2-1")
    assert manifest.spec.params == {"purpose": "train", "run": "r1", "attempt": "2"}
    with _open(roots, purpose="measure", run_id="perfect") as m:
        pass
    assert m.receipt.run_id == "perfect" and m.receipt.attempt is None


def test_read_receipt_rejects_a_tampered_or_missing_artifact(roots):
    ds, plan, paths = _seed(roots)
    with _open(roots) as access:
        pass
    rid = access.receipt_id
    (artifact_dir(roots.data, "access_receipt", rid) / "receipt.json").write_text(
        "{}", encoding="utf-8"
    )
    with pytest.raises(IntegrityError, match="^mismatch: receipt"):
        read_receipt(roots.data, rid)
    with pytest.raises(ValidationFailed, match="^not_found: "):
        read_receipt(roots.data, "nope")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/test_access.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'vcp.data.access.access'`）

- [ ] **Step 3: 寫 `src/vcp/data/access/receipt.py`**

```python
"""Where a receipt lives and how it is claimed (spec 5, 6.5, 6.6): an ``access_receipt``
artifact of the immutable-artifact layer, claimed when the access opens and committed when it
closes. ``ReceiptBinding`` is how an execution environment (the training layer's session) names
the receipt and learns of it; the data layer defines the protocol and never imports the
training layer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple, Protocol

from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import utc_now
from vcp.data.access.schema import AccessReceipt, AccessRef, Purpose

KIND = "access_receipt"
RECEIPT_FILE = "receipt.json"
MAX_CLAIM_RETRIES = 100


class ReceiptBinding(Protocol):
    """Implemented by whoever runs the job (``vcp.train.session.SessionBinding``)."""

    run_id: str
    attempt: int

    def receipt_id(self, seq: int) -> str: ...

    def next_seq(self) -> int: ...

    def on_commit(self, ref: AccessRef) -> None: ...


def standalone_receipt_id(purpose: str, dataset: str, plan_id: str) -> str:
    """A receipt nobody bound: purpose, dataset, plan, a UTC stamp and a nonce."""
    return f"{purpose}-{dataset}-{plan_id}-{utc_now():%Y%m%dT%H%M%S}-{os.urandom(2).hex()}"


def receipt_spec(
    *,
    receipt_id: str,
    purpose: Purpose,
    run_id: str | None,
    attempt: int | None,
    paths: DatasetPaths,
    plan_id: str,
    samples_hash: str,
    plan_sha256: str,
) -> ArtifactSpec:
    params = {"purpose": purpose}
    if run_id is not None:
        params["run"] = run_id
    if attempt is not None:
        params["attempt"] = str(attempt)
    return ArtifactSpec(
        kind=KIND,
        id=receipt_id,
        dataset=paths.name,
        plan_id=plan_id,
        params=params,
        inputs=[
            InputRef(name="card", path=str(paths.card_yaml)),
            InputRef(name="samples", path=str(paths.samples_jsonl), sha256=samples_hash),
            InputRef(name="plan", path=str(paths.plan_json(plan_id)), sha256=plan_sha256),
        ],
    )


def claim_receipt(
    spec: ArtifactSpec, data_root: Path, binding: ReceiptBinding | None
) -> ArtifactWriter:
    """Claim the receipt's directory. With a binding the id is ``binding.receipt_id(seq)`` and a
    taken seq (another worker of the same attempt) is retried with the next one."""
    if binding is None:
        return ArtifactWriter.create(spec, data_root=data_root)
    seq = binding.next_seq()
    for _ in range(MAX_CLAIM_RETRIES):
        candidate = spec.model_copy(update={"id": binding.receipt_id(seq)})
        try:
            return ArtifactWriter.create(candidate, data_root=data_root)
        except ValidationFailed as e:
            if not str(e).startswith("exists:"):
                raise
            seq += 1
    raise ValidationFailed(
        f"exists: no free receipt id for run {binding.run_id!r} attempt {binding.attempt} "
        f"after {MAX_CLAIM_RETRIES} tries"
    )


class ReceiptFile(NamedTuple):
    receipt: AccessReceipt
    sha256: str


def read_receipt(data_root: Path, artifact_id: str) -> ReceiptFile:
    """A committed, verified receipt and the sha of its ``receipt.json``."""
    manifest = store.load_manifest(data_root, KIND, artifact_id)
    res = store.verify(data_root, KIND, artifact_id)
    if res.failed:
        raise IntegrityError(
            f"mismatch: receipt {artifact_id!r} no longer matches its manifest "
            f"(mismatch={len(res.mismatch)} missing={len(res.missing)} extra={len(res.extra)})",
            fields={"receipt": artifact_id},
        )
    entry = manifest.file(RECEIPT_FILE)
    if entry is None:
        raise ValidationFailed(
            f"not_found: receipt {artifact_id!r} has no {RECEIPT_FILE}",
            fields={"receipt": artifact_id},
        )
    path = store.manifest_path(data_root, KIND, artifact_id).parent / RECEIPT_FILE
    try:
        receipt = AccessReceipt.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValidationFailed(f"bad receipt: {e}", location=str(path)) from e
    return ReceiptFile(receipt, entry.sha256)
```

**寫 `src/vcp/data/access/access.py`**

```python
"""``DatasetAccess`` (spec 6): a role-scoped view of one dataset under one plan. Rows outside
the allowed subsets are never parsed -- the open pass hashes every byte for identity and only
peeks the leading ``sample_id`` of each line to build an offset index. What was read is
accumulated here and becomes the receipt at close."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import BinaryIO

from vcp.core.build import build_string
from vcp.core.errors import (
    AccessDeniedError,
    IntegrityError,
    InvariantError,
    SealedSubsetError,
    ValidationFailed,
)
from vcp.core.hashing import sha256_file, sha256_json, sha256_text
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.access.receipt import (
    RECEIPT_FILE,
    ReceiptBinding,
    claim_receipt,
    receipt_spec,
    standalone_receipt_id,
)
from vcp.data.access.schema import (
    MAX_DENIED_FIRST,
    AccessedSubset,
    AccessReceipt,
    AccessRef,
    Purpose,
)
from vcp.data.dataset import Dataset, append_unseal
from vcp.data.schema import DatasetCard, Sample
from vcp.data.split import SplitPlan, assert_plan_matches, load_plan
from vcp.data.tasks import get_task

_LINE = re.compile(rb'^\{"sample_id":\s*"((?:[^"\\]|\\.)*)"')


def index_samples(path: Path) -> tuple[str, dict[str, tuple[int, int]]]:
    """One binary pass: sha256 of every byte, and ``sample_id -> (offset, length)`` from the
    leading key of each line. No line is parsed as JSON."""
    digest = hashlib.sha256()
    index: dict[str, tuple[int, int]] = {}
    offset = 0
    with path.open("rb") as f:
        for lineno, raw in enumerate(iter(f.readline, b""), start=1):
            digest.update(raw)
            m = _LINE.match(raw)
            if m is None:
                raise ValidationFailed("not a samples.jsonl line", location=f"{path}:{lineno}")
            sample_id = json.loads(b'"' + m.group(1) + b'"')
            if sample_id in index:
                raise ValidationFailed(
                    f"duplicate sample_id {sample_id!r}", location=f"{path}:{lineno}"
                )
            index[sample_id] = (offset, len(raw))
            offset += len(raw)
    return digest.hexdigest(), index


class DatasetAccess:
    def __init__(
        self,
        *,
        card: DatasetCard,
        plan: SplitPlan,
        paths: DatasetPaths,
        allowed: frozenset[str],
        purpose: Purpose,
        run_id: str | None,
        attempt: int | None,
        index: dict[str, tuple[int, int]],
        card_sha256: str,
        plan_sha256: str,
        unseal_event_sha256: str | None,
        binding: ReceiptBinding | None,
        notes: str,
    ) -> None:
        self.card = card
        self.plan = plan
        self.paths = paths
        self.allowed = allowed
        self.roles = {s.name: s.role for s in plan.subsets if s.name in allowed}
        self.purpose = purpose
        self.receipt: AccessReceipt | None = None
        self._run_id = run_id
        self._attempt = attempt
        self._index = index
        self._card_sha256 = card_sha256
        self._plan_sha256 = plan_sha256
        self._unseal_event_sha256 = unseal_event_sha256
        self._binding = binding
        self._notes = notes
        self._started_at = stamp()
        self._authorization = sha256_json(
            {
                "card_sha256": card_sha256,
                "samples_hash": card.samples_hash,
                "plan_sha256": plan_sha256,
                "allowed": sorted(allowed),
            }
        )
        self._task = get_task(card.task)
        self._accessed: dict[str, set[str]] = {}
        self._parsed: dict[str, int] = {}
        self._cache: dict[str, Sample] = {}
        self._denied = 0
        self._denied_first: list[str] = []
        self._closed = False
        self._writer = claim_receipt(
            receipt_spec(
                receipt_id=standalone_receipt_id(purpose, card.name, plan.plan_id),
                purpose=purpose,
                run_id=run_id,
                attempt=attempt,
                paths=paths,
                plan_id=plan.plan_id,
                samples_hash=card.samples_hash,
                plan_sha256=plan_sha256,
            ),
            paths.data_root,
            binding,
        )
        self.receipt_id: str = self._writer.spec.id

    # -- open ---------------------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        name: str,
        plan_id: str,
        *,
        subsets: Iterable[str] | None = None,
        roles: Iterable[str] | None = None,
        purpose: Purpose = "custom",
        unseal_reason: str | None = None,
        caller: str | None = None,
        binding: ReceiptBinding | None = None,
        run_id: str | None = None,
        notes: str = "",
        data_root: Path | None = None,
        configs_root: Path | None = None,
    ) -> DatasetAccess:
        if (subsets is None) == (roles is None):
            raise ValidationFailed("DatasetAccess.open takes exactly one of subsets or roles")
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        card = Dataset.load_card(name, data_root=data_root, configs_root=configs_root)
        plan = load_plan(paths, plan_id)
        assert_plan_matches(plan, card)
        if subsets is not None:
            allowed = frozenset(subsets)
            for s in allowed:
                plan.subset(s)  # PlanMismatchError for an unknown subset
        else:
            wanted = set(roles or ())
            allowed = frozenset(s.name for s in plan.subsets if s.role in wanted)
        unseal_sha: str | None = None
        for s in sorted(allowed):
            if plan.subset(s).role != "sealed":
                continue
            if not unseal_reason:
                raise SealedSubsetError(
                    f"subset {s!r} is sealed; pass unseal_reason to open it (recorded)"
                )
            unseal_sha = append_unseal(paths, plan, s, unseal_reason, caller or purpose)
        if not paths.samples_jsonl.is_file():
            raise ValidationFailed(f"samples file not found: {paths.samples_jsonl}")
        digest, index = index_samples(paths.samples_jsonl)
        if digest != card.samples_hash:
            raise IntegrityError(
                f"mismatch: samples.jsonl sha256 {digest[:12]} != card samples_hash "
                f"{card.samples_hash[:12]}",
                location=str(paths.samples_jsonl),
            )
        unknown = sorted(set(index) - set(plan.assignment))
        if unknown:
            raise InvariantError(f"assignment does not cover all samples: missing {unknown[:5]}")
        absent = sorted(set(plan.assignment) - set(index))
        if absent:
            raise InvariantError(f"assignment has unknown sample ids: {absent[:5]}")
        return cls(
            card=card,
            plan=plan,
            paths=paths,
            allowed=allowed,
            purpose=purpose,
            run_id=binding.run_id if binding is not None else run_id,
            attempt=binding.attempt if binding is not None else None,
            index=index,
            card_sha256=sha256_file(paths.card_yaml),
            plan_sha256=sha256_file(paths.plan_json(plan_id)),
            unseal_event_sha256=unseal_sha,
            binding=binding,
            notes=notes,
        )

    # -- reads --------------------------------------------------------------------------------

    def _deny(self, subset: str, token: str) -> None:
        self._denied += 1
        if len(self._denied_first) < MAX_DENIED_FIRST:
            self._denied_first.append(token)
        raise AccessDeniedError(
            f"denied: subset {subset!r} is not authorized (allowed: {sorted(self.allowed)})",
            fields={"subset": subset},
        )

    def _authorize(self, subset: str, sample_id: str | None = None) -> None:
        if subset not in self.allowed:
            self._deny(subset, subset if sample_id is None else f"{subset}:{sample_id}")

    def subset_of(self, sample_id: str) -> str:
        """Which subset the plan assigns an id to. Plan knowledge, not a read."""
        try:
            return self.plan.assignment[sample_id]
        except KeyError:
            raise ValidationFailed(
                f"not_found: sample {sample_id!r} is not in plan {self.plan.plan_id!r}",
                fields={"sample": sample_id},
            ) from None

    def ids(self, subset: str) -> list[str]:
        self._authorize(subset)
        return sorted(self.plan.ids_in(subset))

    def _read(self, f: BinaryIO, sample_id: str, subset: str) -> Sample:
        cached = self._cache.get(sample_id)
        if cached is None:
            offset, length = self._index[sample_id]
            f.seek(offset)
            raw = f.read(length).rstrip(b"\r\n")
            try:
                cached = Sample.model_validate_json(raw.decode("utf-8"))
            except ValueError as e:
                raise ValidationFailed(
                    f"bad sample row for {sample_id!r}: {type(e).__name__}",
                    location=f"{self.paths.samples_jsonl}:{sample_id}",
                ) from e
            self._task.validate(cached, self.card)
            self._cache[sample_id] = cached
            self._parsed[subset] = self._parsed.get(subset, 0) + 1
        self._accessed.setdefault(subset, set()).add(sample_id)
        return cached

    def iter(self, subset: str) -> Iterator[Sample]:
        """Authorises eagerly (a generator would defer the check to the first ``next``)."""
        self._authorize(subset)
        return self._rows(subset)

    def _rows(self, subset: str) -> Iterator[Sample]:
        with self.paths.samples_jsonl.open("rb") as f:
            for sample_id in sorted(self.plan.ids_in(subset)):
                yield self._read(f, sample_id, subset)

    def records(self, subset: str) -> dict[str, Sample]:
        return {s.sample_id: s for s in self.iter(subset)}

    def by_id(self, sample_id: str) -> Sample:
        subset = self.subset_of(sample_id)
        self._authorize(subset, sample_id)
        with self.paths.samples_jsonl.open("rb") as f:
            return self._read(f, sample_id, subset)

    # -- close --------------------------------------------------------------------------------

    def _build_receipt(self, exc: BaseException | None) -> AccessReceipt:
        accessed = {
            s: AccessedSubset(
                role=self.roles[s],
                ids_count=len(ids),
                ids_sha256=sha256_text("\n".join(sorted(ids))),
                records_parsed=self._parsed.get(s, 0),
            )
            for s, ids in sorted(self._accessed.items())
        }
        return AccessReceipt(
            dataset=self.card.name,
            samples_hash=self.card.samples_hash,
            card_sha256=self._card_sha256,
            plan_id=self.plan.plan_id,
            plan_sha256=self._plan_sha256,
            authorization_sha256=self._authorization,
            purpose=self.purpose,
            run_id=self._run_id,
            attempt=self._attempt,
            allowed=sorted(self.allowed),
            roles=dict(sorted(self.roles.items())),
            accessed=accessed,
            denied=self._denied,
            denied_first=list(self._denied_first),
            sealed_accessed=any(a.role == "sealed" for a in accessed.values()),
            unseal_event_sha256=self._unseal_event_sha256,
            outcome="failed" if exc is not None else "completed",
            exception=type(exc).__name__ if exc is not None else None,
            started_at=self._started_at,
            finished_at=stamp(),
            vcp_version=build_string(),
            notes=self._notes,
        )

    def _close(self, exc: BaseException | None) -> AccessReceipt:
        if self.receipt is not None:
            return self.receipt
        if self._closed:
            raise ValidationFailed(
                f"closed: access receipt {self.receipt_id!r} was not committed; open a new access"
            )
        self._closed = True
        receipt = self._build_receipt(exc)
        with self._writer as writer:  # a failing commit leaves the artifact partial + failure.json
            writer.write_json(RECEIPT_FILE, receipt.model_dump(mode="json"))
            manifest = writer.commit()
        self.receipt = receipt
        if self._binding is not None:
            entry = manifest.file(RECEIPT_FILE)
            assert entry is not None  # the file was just written
            self._binding.on_commit(
                AccessRef(
                    artifact_id=self.receipt_id,
                    purpose=self.purpose,
                    subsets=sorted(receipt.accessed),
                    sealed_accessed=receipt.sealed_accessed,
                    denied=receipt.denied,
                    receipt_sha256=entry.sha256,
                    binding="session",
                )
            )
        return receipt

    def close(self) -> AccessReceipt:
        return self._close(None)

    def __enter__(self) -> DatasetAccess:
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        self._close(exc)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/data/access tests/unit/data/test_access.py && uv run pytest tests/unit/data/test_access.py tests/unit/data/test_access_schema.py -o addopts="" -q`
Expected: 全部通過（10 個測試）。

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/data/access/receipt.py src/vcp/data/access/access.py tests/unit/data/test_access.py
git commit -m "feat(access): DatasetAccess——行首 peek 索引、角色授權、拒絕計數、收據產物在 open claim / close commit" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `export_subset` 走存取器，manifest 記 `receipt`

**Files:**
- Modify: `src/vcp/data/exporters/base.py:113-162`（`export_subset`）、`src/vcp/cli.py:330-348`（export 的 fields）
- Test: `tests/unit/data/exporters/test_exporters.py`、`tests/unit/test_cli.py`

**Interfaces:**
- Consumes: Task 3 `DatasetAccess`。
- Produces: `ExportResult.receipt: str`（收據產物 id）；export `manifest.json` 多 `"receipt": <id>`；`vcp data export` VERDICT 多 `receipt=`。exporter 登記表簽名不變（`run(dataset, samples, out, image_root, options)` 收到的是 `Dataset(card, samples)` 視圖）。

- [ ] **Step 1: 寫失敗的測試**——追加到 `tests/unit/data/exporters/test_exporters.py` 檔尾（import 區加 `from vcp.artifact import store`、`from vcp.data.access.receipt import read_receipt`；檔案已有 `ExportSpec` / `export_subset` / `DatasetPaths` / 建資料集的 helper——用它既有的建法，下面以 `_seed_det(roots)` 代表「建一個 det 資料集 + `fixed-v1` plan 並回 `(ds, plan, paths)`」，若檔內沒有同型 helper就照 `tests/unit/train/test_run.py::_seed` 抄一個）：

```python
def test_export_leaves_a_receipt_for_the_subset_it_read(roots, tmp_path):
    ds, plan, paths = _seed_det(roots)
    res = export_subset(
        ExportSpec(
            name="tiny",
            plan_id="fixed-v1",
            subset="train",
            format="coco",
            out=tmp_path / "coco-train",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
    assert manifest["receipt"] == res.receipt and res.receipt.startswith("export-tiny-fixed-v1-")
    receipt = read_receipt(roots.data, res.receipt).receipt
    assert receipt.purpose == "export" and receipt.allowed == ["train"]
    assert set(receipt.accessed) == {"train"}
    assert receipt.accessed["train"].ids_count == manifest["sample_count"]
    assert not store.is_partial(roots.data, "access_receipt", res.receipt)


def test_export_of_a_sealed_subset_records_the_unseal_and_the_receipt(roots, tmp_path):
    ds, plan, paths = _seed_det(roots)
    with pytest.raises(SealedSubsetError):
        export_subset(
            ExportSpec(
                name="tiny",
                plan_id="fixed-v1",
                subset="holdout",
                format="coco",
                out=tmp_path / "h1",
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )
    res = export_subset(
        ExportSpec(
            name="tiny",
            plan_id="fixed-v1",
            subset="holdout",
            format="coco",
            out=tmp_path / "h2",
            unseal=True,
            reason="final",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    receipt = read_receipt(roots.data, res.receipt).receipt
    assert receipt.sealed_accessed and receipt.unseal_event_sha256 is not None
    rows = paths.unseal_jsonl("fixed-v1").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["caller"] == "vcp data export"
```

追加到 `tests/unit/test_cli.py` 檔尾（檔內已有 `_import_tiny` / `runner` / `_last_verdict`；`_split(roots, tmp_path)` 若不存在就用 `runner.invoke(app, ["data", "split", "--name", "tiny", "--plan-id", "fixed-v1"])`）：

```python
def test_export_verdict_names_its_receipt(roots, tmp_path):
    assert _import_tiny(roots, tmp_path, with_images=True).exit_code == 0
    assert runner.invoke(app, ["data", "split", "--name", "tiny", "--plan-id", "fixed-v1"]).exit_code == 0
    r = runner.invoke(
        app,
        ["data", "export", "--name", "tiny", "--plan", "fixed-v1", "--subset", "train",
         "--format", "coco", "--out", str(tmp_path / "out"), "--json"],
    )
    assert r.exit_code == 0, r.output
    doc = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert doc["fields"]["receipt"].startswith("export-tiny-fixed-v1-")
    assert doc["result"]["receipt"] == doc["fields"]["receipt"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/data/exporters/test_exporters.py tests/unit/test_cli.py -o addopts="" -q -k "receipt"`
Expected: FAIL（`KeyError: 'receipt'` / `AttributeError: 'ExportResult' object has no attribute 'receipt'`）

- [ ] **Step 3: 改 `export_subset`**——import 區加 `from vcp.data.access.access import DatasetAccess`；`ExportResult` 加 `receipt: str`；函式改成：

```python
def export_subset(spec: ExportSpec) -> ExportResult:
    paths = spec.paths()
    exporter = get_exporter(spec.format)
    with DatasetAccess.open(
        spec.name,
        spec.plan_id,
        subsets={spec.subset},
        purpose="export",
        unseal_reason=spec.reason if spec.unseal else None,
        caller="vcp data export",
        data_root=spec.data_root,
        configs_root=spec.configs_root,
    ) as access:
        samples = list(access.iter(spec.subset))
        # The exporter registry keeps its signature: it sees a Dataset holding exactly the
        # authorised samples, never the registry the accessor guards.
        dataset = Dataset(access.card, samples)
        out = spec.out.expanduser().resolve()
        if out.exists() and any(out.iterdir()):
            raise ValidationFailed(f"output directory not empty: {out}")
        out.mkdir(parents=True, exist_ok=True)
        image_root = paths.resolve_image_root(dataset.card)
        output = exporter.run(dataset, samples, out, image_root, spec.options)
    receipt_id = access.receipt_id
    files, warnings, fields = list(output.files), list(output.warnings), dict(output.fields)
    if not samples:
        warnings.append("subset is empty")
    rotated = count_exif_rotated(samples)
    if rotated:
        fields["exif_rotated"] = rotated
        warnings.append(
            f"{rotated} views carry an EXIF orientation != 1 (policy {dataset.card.exif_policy}); "
            "verify the label space before training"
        )
    manifest = {
        **output.manifest,
        "dataset": dataset.card.name,
        "samples_hash": dataset.card.samples_hash,
        "plan_id": access.plan.plan_id,
        "subset": spec.subset,
        "format": exporter.name,
        "exporter_version": exporter.version,
        "exported_at": stamp(),
        "sample_count": len(samples),
        "exif_policy": dataset.card.exif_policy,
        "exif_rotated": rotated,
        "receipt": receipt_id,
        "files": {rel_posix(f, out): sha256_file(f) for f in sorted(files)},
    }
    manifest_path = out / "manifest.json"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return ExportResult(
        out=out,
        manifest_path=manifest_path,
        files=len(files),
        warnings=warnings,
        fields=fields,
        receipt=receipt_id,
    )
```

（`spec.unseal=True` 而 `reason` 為空時，存取器的 `SealedSubsetError` 訊息與從前 `Dataset.subset` 的不同但類型相同；`load_plan` 不再需要直接 import——若 ruff 報 F401 就拿掉。）

`src/vcp/cli.py` export 的 `fields` dict 加 `"receipt": res.receipt`（放在 `"out"` 之後），payload 加 `"receipt": res.receipt`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/data/exporters/base.py src/vcp/cli.py tests/unit/data/exporters/test_exporters.py tests/unit/test_cli.py && uv run pytest tests/unit/data tests/unit/test_cli.py tests/unit/train/test_run.py -o addopts="" -q`
Expected: 全部通過（既有 export 測試的 manifest 多一個鍵，斷言逐鍵者不受影響；`test_run.py` 的 `_export` 仍可用）

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/data/exporters/base.py src/vcp/cli.py tests/unit/data/exporters/test_exporters.py tests/unit/test_cli.py
git commit -m "feat(data): export 走 DatasetAccess，manifest 與 VERDICT 記收據" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: 量測層 schema 與 `measure/provenance.py`

**Files:**
- Modify: `src/vcp/measure/schema.py:141-150,163-180,244-265`
- Create: `src/vcp/measure/provenance.py`
- Test: `tests/unit/measure/test_provenance.py`、`tests/unit/measure/test_schema.py`

**Interfaces:**
- Consumes: Task 2 `AccessRef` / `Grade` / `GRADE_RANK`；Task 3 `read_receipt` / `DatasetAccess`（測試用）；`vcp.fuse.build.load_record` / `record_path`（lazy）。
- Produces: `RunCard.access: list[AccessRef] = []`；`Reading.provenance: Grade | None = None`；`Judgement.provenance: Grade | None = None`；`ProvenanceInfo(grade, observed, invalid, receipts)`；`provenance(card, *, data_root, configs_root=None) -> ProvenanceInfo`；`attach_receipts(card, artifact_ids, *, data_root) -> RunCard`。

- [ ] **Step 1: 寫失敗的測試** `tests/unit/measure/test_provenance.py`

```python
import json

import pytest

from helpers import det_with_runs
from vcp.core.config import dump_yaml_model
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.paths import artifact_dir
from vcp.data.access.access import DatasetAccess
from vcp.data.access.schema import AccessRef
from vcp.measure.provenance import ProvenanceInfo, attach_receipts, provenance
from vcp.measure.runs import load_run, save_run


def _receipt(roots, subsets, *, purpose="train", run_id=None):
    with DatasetAccess.open(
        "tiny",
        "fixed-v1",
        subsets=set(subsets),
        purpose=purpose,
        run_id=run_id,
        unseal_reason="test" if "holdout" in subsets else None,
        data_root=roots.data,
        configs_root=roots.configs,
    ) as access:
        for s in subsets:
            list(access.iter(s))
    return access.receipt_id


def _kw(roots):
    return {"data_root": roots.data, "configs_root": roots.configs}


def test_grades_declared_export_receipt(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    card = load_run(roots.data, "perfect")
    info = provenance(card, **_kw(roots))
    assert info == ProvenanceInfo(grade="declared", observed=[], invalid=[], receipts=[])
    source = card.source.model_copy(update={"export_manifest_sha": "e" * 64})
    assert provenance(card.model_copy(update={"source": source}), **_kw(roots)).grade == "export"
    rid = _receipt(roots, ["train"], run_id="perfect")
    card = attach_receipts(card, [rid], data_root=roots.data)
    assert [r.artifact_id for r in card.access] == [rid] and card.access[0].binding == "session"
    info = provenance(card, **_kw(roots))
    assert info.grade == "receipt" and info.observed == ["train"] and info.invalid == []
    assert info.receipts == card.access
    # attaching twice is idempotent; a custom-purpose receipt adds observation, not grade
    card = attach_receipts(card, [rid], data_root=roots.data)
    assert len(card.access) == 1
    peek = _receipt(roots, ["valA"], purpose="custom")
    card = attach_receipts(card, [peek], data_root=roots.data)
    assert card.access[1].binding == "manual"
    info = provenance(card, **_kw(roots))
    assert info.grade == "receipt" and info.observed == ["train", "valA"]
    only_custom = card.model_copy(update={"access": [card.access[1]]})
    assert provenance(only_custom, **_kw(roots)).grade == "declared"


def test_attach_receipts_refuses_the_wrong_run_dataset_or_plan(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    card = load_run(roots.data, "perfect")
    rid = _receipt(roots, ["train"], run_id="noisy")
    with pytest.raises(IntegrityError, match="^mismatch: receipt .* was produced under run 'noisy'"):
        attach_receipts(card, [rid], data_root=roots.data)
    with pytest.raises(ValidationFailed, match="^not_found: "):
        attach_receipts(card, ["nope"], data_root=roots.data)
    other = card.model_copy(update={"plan_id": "other"})
    rid = _receipt(roots, ["train"])
    with pytest.raises(IntegrityError, match="^mismatch: receipt .* belongs to tiny/fixed-v1"):
        attach_receipts(other, [rid], data_root=roots.data)


def test_a_changed_card_plan_or_receipt_invalidates(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=40)
    card = attach_receipts(
        load_run(roots.data, "perfect"), [_receipt(roots, ["train"])], data_root=roots.data
    )
    assert provenance(card, **_kw(roots)).grade == "receipt"
    rid = card.access[0].artifact_id
    # plan file rewritten (same content, different bytes)
    pj = paths.plan_json("fixed-v1")
    original = pj.read_bytes()
    pj.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    info = provenance(card, **_kw(roots))
    assert info.grade == "declared" and info.invalid == [rid] and info.observed == []
    pj.write_bytes(original)
    assert provenance(card, **_kw(roots)).grade == "receipt"
    # card rewritten with a cosmetic change
    dump_yaml_model(ds.card.model_copy(update={"exif_policy": "corrected"}), paths.card_yaml)
    assert provenance(card, **_kw(roots)).invalid == [rid]
    dump_yaml_model(ds.card, paths.card_yaml)
    # receipt bytes tampered on disk
    (artifact_dir(roots.data, "access_receipt", rid) / "receipt.json").write_text(
        "{}", encoding="utf-8"
    )
    assert provenance(card, **_kw(roots)).invalid == [rid]
    # a ref whose recorded sha does not match a clean receipt
    fresh = attach_receipts(
        load_run(roots.data, "perfect"), [_receipt(roots, ["train"])], data_root=roots.data
    )
    forged = fresh.model_copy(
        update={"access": [fresh.access[0].model_copy(update={"receipt_sha256": "0" * 64})]}
    )
    assert provenance(forged, **_kw(roots)).invalid == [fresh.access[0].artifact_id]


def test_sealed_and_multiple_receipts_union_their_observations(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    card = load_run(roots.data, "noisy")
    a = _receipt(roots, ["train"], run_id="noisy")
    b = _receipt(roots, ["train", "holdout"], run_id="noisy")
    card = attach_receipts(card, [a, b], data_root=roots.data)
    assert card.access[1].sealed_accessed and not card.access[0].sealed_accessed
    info = provenance(card, **_kw(roots))
    assert info.observed == ["holdout", "train"] and info.grade == "receipt"
    save_run(roots.data, card)
    assert load_run(roots.data, "noisy").access == card.access  # round-trips through run.yaml
```

追加到 `tests/unit/measure/test_schema.py` 檔尾（import 區加 `from vcp.measure.schema import Reading, RunCard, RunSource`——已有的就不重複）：

```python
def test_run_card_and_reading_carry_optional_provenance_fields():
    card = RunCard(
        run_id="r",
        dataset="d",
        samples_hash="a" * 64,
        plan_id="p",
        trained_on=[],
        source=RunSource(),
        created_at="2026-09-12T00:00:00.000Z",
    )
    assert card.access == []
    assert RunCard.model_validate(card.model_dump(mode="json")).access == []
    assert "provenance" in Reading.model_fields and Reading.model_fields["provenance"].default is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/measure/test_provenance.py tests/unit/measure/test_schema.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'vcp.measure.provenance'`；`AttributeError: 'RunCard' object has no attribute 'access'`）

- [ ] **Step 3: 改 `src/vcp/measure/schema.py`**——import 區加 `from vcp.data.access.schema import AccessRef, Grade`；`RunCard` 最後加 `access: list[AccessRef] = Field(default_factory=list)`；`Reading` 的 `guardrail` 之後加 `provenance: Grade | None = None`；`Judgement` 的 `bootstrap` 之後加 `provenance: Grade | None = None`。

**寫 `src/vcp/measure/provenance.py`**

```python
"""Provenance of a run's training reads (spec 4.3, 7.2): which receipts still hold, what they
observed, and the grade ``receipt > export > declared`` the consumers act on. Computed on read,
never stored -- a run written before receipts existed simply grades as ``declared``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import IntegrityError, VcpError
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.access.receipt import read_receipt
from vcp.data.access.schema import GRADE_RANK, AccessRef, Grade
from vcp.measure.runs import FUSE_FRAMEWORK, load_run
from vcp.measure.schema import RunCard


@dataclass(frozen=True)
class ProvenanceInfo:
    grade: Grade
    observed: list[str]
    invalid: list[str]
    receipts: list[AccessRef]


def _fused(card: RunCard, data_root: Path, configs_root: Path | None) -> ProvenanceInfo | None:
    """A fused run has no reads of its own: the weakest member's grade, every member's
    observation (lazy import: the fusion layer imports this layer)."""
    from vcp.fuse.build import load_record, record_path

    if card.source.framework != FUSE_FRAMEWORK or not record_path(data_root, card.run_id).is_file():
        return None
    infos = [
        provenance(load_run(data_root, m.run), data_root=data_root, configs_root=configs_root)
        for m in load_record(data_root, card.run_id).members
    ]
    if not infos:
        return ProvenanceInfo("declared", [], [], [])
    grade = min((i.grade for i in infos), key=lambda g: GRADE_RANK[g])
    observed = sorted(set().union(*(set(i.observed) for i in infos)))
    invalid = sorted(set().union(*(set(i.invalid) for i in infos)))
    return ProvenanceInfo(grade, observed, invalid, [])


def provenance(
    card: RunCard, *, data_root: Path, configs_root: Path | None = None
) -> ProvenanceInfo:
    fused = _fused(card, data_root, configs_root)
    if fused is not None:
        return fused
    paths = DatasetPaths.resolve(card.dataset, data_root=data_root, configs_root=configs_root)
    plan_file = paths.plan_json(card.plan_id)
    plan_sha = sha256_file(plan_file) if plan_file.is_file() else None
    card_sha = sha256_file(paths.card_yaml) if paths.card_yaml.is_file() else None
    valid: list[AccessRef] = []
    invalid: list[str] = []
    observed: set[str] = set()
    for ref in card.access:
        try:
            receipt, sha = read_receipt(data_root, ref.artifact_id)
        except VcpError:
            invalid.append(ref.artifact_id)
            continue
        holds = (
            sha == ref.receipt_sha256
            and receipt.samples_hash == card.samples_hash
            and receipt.plan_id == card.plan_id
            and receipt.plan_sha256 == plan_sha
            and receipt.card_sha256 == card_sha
        )
        if not holds:
            invalid.append(ref.artifact_id)
            continue
        valid.append(ref)
        observed.update(receipt.accessed)
    if any(r.purpose == "train" for r in valid):
        grade: Grade = "receipt"
    elif card.source.export_manifest_sha:
        grade = "export"
    else:
        grade = "declared"
    return ProvenanceInfo(grade, sorted(observed), invalid, valid)


def attach_receipts(card: RunCard, artifact_ids: list[str], *, data_root: Path) -> RunCard:
    """``ingest --receipt`` (spec 7.2): bind receipts made outside ``vcp train run`` to a run.
    Idempotent per artifact id; a receipt of another run, dataset or plan is refused."""
    refs = list(card.access)
    for artifact_id in artifact_ids:
        if any(r.artifact_id == artifact_id for r in refs):
            continue
        receipt, sha = read_receipt(data_root, artifact_id)
        if receipt.dataset != card.dataset or receipt.plan_id != card.plan_id:
            raise IntegrityError(
                f"mismatch: receipt {artifact_id!r} belongs to {receipt.dataset}/{receipt.plan_id}"
                f", not {card.dataset}/{card.plan_id}",
                fields={"receipt": artifact_id, "run": card.run_id},
            )
        if receipt.run_id is not None and receipt.run_id != card.run_id:
            raise IntegrityError(
                f"mismatch: receipt {artifact_id!r} was produced under run {receipt.run_id!r}, "
                f"not {card.run_id!r}",
                fields={"receipt": artifact_id, "run": card.run_id},
            )
        refs.append(
            AccessRef(
                artifact_id=artifact_id,
                purpose=receipt.purpose,
                subsets=sorted(receipt.accessed),
                sealed_accessed=receipt.sealed_accessed,
                denied=receipt.denied,
                receipt_sha256=sha,
                binding="manual" if receipt.run_id is None else "session",
            )
        )
    return card.model_copy(update={"access": refs})
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/measure/schema.py src/vcp/measure/provenance.py tests/unit/measure/test_provenance.py tests/unit/measure/test_schema.py && uv run pytest tests/unit/measure -o addopts="" -q`
Expected: 全部通過

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/measure/schema.py src/vcp/measure/provenance.py tests/unit/measure/test_provenance.py tests/unit/measure/test_schema.py
git commit -m "feat(measure): RunCard.access、Reading / Judgement.provenance、三級 provenance 與 attach_receipts" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: 訓練層——`TrainRecord.access`、`access` 事件、`Session.access`、`MaterializedReader` 走存取器

**Files:**
- Modify: `src/vcp/train/schema.py:12,78-97`、`src/vcp/train/session.py`、`src/vcp/train/reader.py`
- Test: `tests/unit/train/test_session.py`、`tests/unit/train/test_reader.py`

**Interfaces:**
- Consumes: Task 2 `AccessRef`；Task 3 `DatasetAccess` / `ReceiptBinding`。
- Produces: `EVENTS` 含 `"access"`；`TrainRecord.access: list[AccessRef] = []`；`vcp.train.session.SessionBinding(session)`（`run_id`、`attempt`、`receipt_id(seq)`、`next_seq()`、`on_commit(ref)`）；`Session.access(*, subsets=None, roles=None, purpose="train", unseal_reason=None, notes="") -> DatasetAccess`；`MaterializedReader(name, mode_dir, *, plan_id=None, subset=None, unseal=False, reason=None, purpose=None, access=None, data_root=None, configs_root=None, verify=False)` 屬性 `card`、`access`、`ids`，方法 `close()`，context manager；`reader.dataset` 移除。

- [ ] **Step 1: 寫失敗的測試**——追加到 `tests/unit/train/test_session.py` 檔尾（import 區加 `from helpers import det_samples, make_card, write_images`、`from vcp.core.paths import DatasetPaths`、`from vcp.data.dataset import Dataset`、`from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan`、`from vcp.data.access.receipt import read_receipt`、`from vcp.train.session import SessionBinding`）：

```python
def _dataset(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(40, seed=0)
    write_images(roots.data / "raw" / "tiny", samples)
    ds = Dataset.from_parts(make_card("det", name="tiny", image_root="raw/tiny"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return plan


def test_session_access_binds_receipts_to_the_current_attempt(roots, monkeypatch):
    plan = _dataset(roots)
    _running(roots)  # attempt 2 is running
    monkeypatch.setenv("VCP_RUN_ID", "r1")
    session = Session.current(roots.data)
    binding = SessionBinding(session)
    assert (binding.run_id, binding.attempt, binding.next_seq()) == ("r1", 2, 1)
    assert binding.receipt_id(3) == "r1-a2-3"
    with session.access(subsets={"train"}) as access:
        assert access.receipt_id == "r1-a2-1" and access.purpose == "train"
        list(access.iter("train"))
    with session.access(roles={"train"}, notes="second") as again:
        pass
    assert again.receipt_id == "r1-a2-2"
    record = load_record(roots.data, "r1")
    assert [r.artifact_id for r in record.access] == ["r1-a2-1", "r1-a2-2"]
    assert record.access[0].subsets == ["train"] and record.access[0].binding == "session"
    assert record.access[0].receipt_sha256 == read_receipt(roots.data, "r1-a2-1").sha256
    events = [e for e in read_events(roots.data, "r1") if e["event"] == "access"]
    assert [e["artifact_id"] for e in events] == ["r1-a2-1", "r1-a2-2"]
    assert events[0]["attempt"] == 2 and events[0]["subsets"] == ["train"]
    assert events[0]["denied"] == 0 and events[0]["sealed_accessed"] is False
    receipt = read_receipt(roots.data, "r1-a2-1").receipt
    assert receipt.run_id == "r1" and receipt.attempt == 2 and receipt.plan_id == plan.plan_id
```

追加到 `tests/unit/train/test_reader.py` 檔尾（import 區加 `from vcp.core.errors import AccessDeniedError`、`from vcp.data.access.access import DatasetAccess`、`from vcp.data.access.receipt import read_receipt`、`from vcp.train.records import load_record, save_record`、`from vcp.train.schema import Attempt, TrainRecord`、`from vcp.artifact import store`）：

```python
def test_reader_with_a_subset_reads_only_that_subset_and_leaves_a_receipt(roots):
    ds, plan, paths = _image_ds(roots, n=40)
    assert _mat(roots, "tiny", mode="npy").failed == 0
    with MaterializedReader(
        "tiny", "npy", plan_id="fixed-v1", subset="train", purpose="custom",
        data_root=roots.data, configs_root=roots.configs,
    ) as reader:
        assert reader.ids == sorted(plan.ids_in("train")) and reader.card.name == "tiny"
        assert reader.access is not None and reader.access.allowed == frozenset({"train"})
        with pytest.raises(KeyError):
            reader[sorted(plan.ids_in("valA"))[0]]  # not even in the reader's index
        first = reader[reader.ids[0]]
        assert first.labels is not None and set(first.arrays) == {"0"}
        rid = reader.access.receipt_id
        assert store.is_partial(roots.data, "access_receipt", rid)
    receipt = read_receipt(roots.data, rid).receipt
    assert receipt.purpose == "custom" and set(receipt.accessed) == {"train"}
    assert receipt.accessed["train"].ids_count == len(plan.ids_in("train"))


def test_reader_under_a_training_session_registers_its_receipt(roots, monkeypatch):
    ds, plan, paths = _image_ds(roots, n=40)
    assert _mat(roots, "tiny", mode="npy").failed == 0
    save_record(
        roots.data,
        TrainRecord(
            run_id="r1",
            dataset="tiny",
            plan_id="fixed-v1",
            trained_on=["train"],
            config_hash="ab" * 32,
            cwd="work",
            command=["python"],
            attempts=[Attempt(n=1, started_at="2026-09-12T00:00:00.000Z", console="c")],
        ),
    )
    monkeypatch.setenv("VCP_RUN_ID", "r1")
    monkeypatch.setenv("VCP_DATA_ROOT", str(roots.data))
    monkeypatch.setenv("VCP_CONFIGS_ROOT", str(roots.configs))
    with pytest.raises(AccessDeniedError, match="^denied: a training reader must name plan_id"):
        MaterializedReader("tiny", "npy")
    with MaterializedReader("tiny", "npy", plan_id="fixed-v1", subset="train") as reader:
        assert reader.access.receipt_id == "r1-a1-1"
        for rec in reader:
            assert rec.sample_id in plan.ids_in("train")
    record = load_record(roots.data, "r1")
    assert [r.artifact_id for r in record.access] == ["r1-a1-1"]
    assert record.access[0].purpose == "train" and record.access[0].subsets == ["train"]
    # an injected, already-open access is used as-is and NOT closed by the reader
    with DatasetAccess.open(
        "tiny", "fixed-v1", subsets={"valA"}, purpose="custom",
        data_root=roots.data, configs_root=roots.configs,
    ) as access:
        reader = MaterializedReader(
            "tiny", "npy", plan_id="fixed-v1", subset="valA", access=access,
            data_root=roots.data, configs_root=roots.configs,
        )
        reader.close()
        assert access.receipt is None
    assert access.receipt is not None and set(access.receipt.accessed) == {"valA"}


def test_reader_without_a_plan_keeps_the_full_dataset_outside_a_run(roots):
    ds, plan, paths = _image_ds(roots)
    assert _mat(roots, "tiny", mode="npy").failed == 0
    reader = MaterializedReader("tiny", "npy", data_root=roots.data, configs_root=roots.configs)
    assert reader.access is None and len(reader) == 8 and reader.card.name == "tiny"
    reader.close()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/train/test_session.py tests/unit/train/test_reader.py -o addopts="" -q`
Expected: FAIL（`ImportError: cannot import name 'SessionBinding'`；reader 測試 `TypeError: __init__() got an unexpected keyword argument 'purpose'`）

- [ ] **Step 3: 改三個檔**

`src/vcp/train/schema.py`：`EVENTS = ("started", "env", "checkpoint", "uploaded", "finished", "note", "access")`；import 區加 `from vcp.data.access.schema import AccessRef`；`TrainRecord` 的 `notes: str = ""` 之前加 `access: list[AccessRef] = Field(default_factory=list)`。

`src/vcp/train/session.py`——import 區加 `from collections.abc import Iterable`、`from vcp.data.access.access import DatasetAccess`、`from vcp.data.access.schema import AccessRef, Purpose`；檔尾加 `SessionBinding` 與 `Session.access`：

```python
class SessionBinding:
    """The training layer's ``ReceiptBinding``: receipts of the running attempt are numbered
    ``<run_id>-a<attempt>-<seq>`` and land in ``train.yaml`` plus an ``access`` event."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.run_id = session.run_id
        self.attempt = current_attempt(load_record(session.data_root, session.run_id))

    def receipt_id(self, seq: int) -> str:
        return f"{self.run_id}-a{self.attempt}-{seq}"

    def next_seq(self) -> int:
        prefix = f"{self.run_id}-a{self.attempt}-"
        record = load_record(self.session.data_root, self.run_id)
        return sum(1 for r in record.access if r.artifact_id.startswith(prefix)) + 1

    def on_commit(self, ref: AccessRef) -> None:
        record = load_record(self.session.data_root, self.run_id)
        save_record(self.session.data_root, record.model_copy(update={"access": [*record.access, ref]}))
        append_event(
            self.session.data_root,
            self.run_id,
            "access",
            self.attempt,
            artifact_id=ref.artifact_id,
            purpose=ref.purpose,
            subsets=ref.subsets,
            denied=ref.denied,
            sealed_accessed=ref.sealed_accessed,
        )
```

在 `Session` 類別的 `note` 之後加：

```python
    def access(
        self,
        *,
        subsets: Iterable[str] | None = None,
        roles: Iterable[str] | None = None,
        purpose: Purpose = "train",
        unseal_reason: str | None = None,
        notes: str = "",
    ) -> DatasetAccess:
        """A role-scoped access of this run's dataset and plan whose receipt is bound to the
        running attempt (spec 7.1). ``VCP_CONFIGS_ROOT`` (exported by ``vcp train run``) says
        where the card and plan are."""
        record = load_record(self.data_root, self.run_id)
        return DatasetAccess.open(
            record.dataset,
            record.plan_id,
            subsets=subsets,
            roles=roles,
            purpose=purpose,
            unseal_reason=unseal_reason,
            caller=f"vcp train run {self.run_id}",
            binding=SessionBinding(self),
            notes=notes,
            data_root=self.data_root,
        )
```

`src/vcp/train/reader.py`——整檔改成：

```python
"""Read a materialize cache from a training loop (spec 8.1) through a role-scoped access.

Arrays come from ``cache/materialize/<mode_dir>/`` through its manifest -- the data layer's
authoritative map -- and labels from ``samples.jsonl`` via ``DatasetAccess``: with a plan and a
subset only that subset's rows are ever parsed, and the receipt is committed when the reader is
closed (``with MaterializedReader(...) as reader:``). Under ``vcp train run`` (``VCP_RUN_ID``)
the receipt is bound to the running attempt and a reader must name its subset. Nothing here
augments, batches or depends on a training framework.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from vcp.core.errors import AccessDeniedError, IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.access.access import DatasetAccess
from vcp.data.access.schema import Purpose
from vcp.data.dataset import Dataset
from vcp.data.materialize.manifest import ManifestRow, read_manifest
from vcp.data.schema import DatasetCard, Labels, Sample
from vcp.train.session import Session


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
        purpose: Purpose | None = None,
        access: DatasetAccess | None = None,
        data_root: Path | None = None,
        configs_root: Path | None = None,
        verify: bool = False,
    ) -> None:
        if (plan_id is None) != (subset is None):
            raise ValidationFailed("plan_id and subset must be given together")
        self.paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        self.root = self.paths.cache_dir / "materialize" / mode_dir
        self.verify = verify
        manifest = self.root / "manifest.jsonl"
        if not manifest.is_file():
            raise ValidationFailed(f"materialize cache not found: {manifest}")
        rows: dict[str, list[ManifestRow]] = {}
        for row in read_manifest(manifest).values():
            rows.setdefault(row.sample_id, []).append(row)
        under_run = bool(os.environ.get("VCP_RUN_ID"))
        self.access: DatasetAccess | None = None
        self._owns_access = False
        self.card: DatasetCard
        samples: list[Sample]
        if plan_id is not None and subset is not None:
            if access is not None:
                self.access = access
            elif under_run:
                self.access = Session.current(data_root).access(
                    subsets={subset}, purpose="train", unseal_reason=reason if unseal else None
                )
                self._owns_access = True
            else:
                self.access = DatasetAccess.open(
                    name,
                    plan_id,
                    subsets={subset},
                    purpose=purpose or "custom",
                    unseal_reason=reason if unseal else None,
                    caller="MaterializedReader",
                    data_root=data_root,
                    configs_root=configs_root,
                )
                self._owns_access = True
            self.card = self.access.card
            samples = list(self.access.iter(subset))
        else:
            if under_run:
                raise AccessDeniedError(
                    "denied: a training reader must name plan_id and subset under vcp train run"
                )
            dataset = Dataset.load(name, data_root=data_root, configs_root=configs_root)
            self.card = dataset.card
            samples = list(dataset.samples)
        absent = [s.sample_id for s in samples if s.sample_id not in rows]
        if absent:
            raise ValidationFailed(
                f"{len(absent)} samples have no materialized rows in {mode_dir!r} "
                f"(e.g. {absent[:3]}); run vcp data materialize first",
                location=absent[0],
            )
        self._rows = {s.sample_id: rows[s.sample_id] for s in samples}
        self._samples = {s.sample_id: s for s in samples}
        self.ids: list[str] = [s.sample_id for s in samples]

    def close(self) -> None:
        """Commit the receipt (only for an access this reader opened itself)."""
        if self._owns_access and self.access is not None:
            self.access.close()

    def __enter__(self) -> MaterializedReader:
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        if self._owns_access and self.access is not None:
            self.access.__exit__(exc_type, exc, tb)

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

既有 `tests/unit/train/test_reader.py` 若有測試用了 `reader.dataset`（`grep -n "reader.dataset" tests/unit/train/test_reader.py`），改成 `reader.card`（`reader.dataset.card` → `reader.card`）。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/train tests/unit/train && uv run pytest tests/unit/train tests/unit/data -o addopts="" -q`
Expected: 全部通過

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/train/schema.py src/vcp/train/session.py src/vcp/train/reader.py tests/unit/train/test_session.py tests/unit/train/test_reader.py
git commit -m "feat(train): Session.access 綁定收據到 attempt、MaterializedReader 走存取器並成為 context manager" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: `vcp train run` 父程序 card-only、收據抄進 `run.yaml`、WARN

**Files:**
- Modify: `src/vcp/train/run.py`（import、`RunResult`、`derive_trained_on`、`_existing`、`_new`、`train_run`）、`src/vcp/cli_train.py:99-118`
- Test: `tests/unit/train/test_run.py`

**Interfaces:**
- Consumes: Task 1 `Dataset.load_card` / `assert_run_matches(card, dataset_card)`；Task 5 `provenance`；Task 6 `Session.access` / `MaterializedReader`（子程序用）。
- Produces: `derive_trained_on(exports, explicit, *, card: DatasetCard, plan, data_root)`；`RunResult` 多 `receipts: int`、`denied: int`、`provenance: str`、`observed_beyond: list[str]`、`receipt_invalid: int`；`train run` VERDICT 多 `receipts=` `denied=` `provenance=`，WARN 時多 `observed_beyond_trained_on=` / `receipt_invalid=`。

- [ ] **Step 1: 寫失敗的測試**——`tests/unit/train/test_run.py`：import 區加 `from vcp.data.materialize import MaterializeSpec, materialize`；`test_derive_trained_on_from_exports` 裡七處 `derive_trained_on(..., dataset=ds, ...)` 改成 `card=ds.card`（`grep -n "dataset=ds" tests/unit/train/test_run.py`）。在 `RESUME_FAKE` 之後加兩個腳本：

```python
# Wave 1b-1: a loop that reads its subset through the reader under the run's session.
ACCESS_FAKE = """
from pathlib import Path
from vcp.train import MaterializedReader

with MaterializedReader("tiny", "npy", plan_id="fixed-v1", subset="train") as reader:
    n = sum(1 for _ in reader)
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best-%d" % n)
"""

# ... and one that also peeks at valA: the receipt says so, whatever trained_on claims.
PEEK_FAKE = ACCESS_FAKE + """
from vcp.train import Session

with Session.current().access(subsets={"valA"}) as peek:
    list(peek.iter("valA"))
"""
```

檔尾加：

```python
def test_train_run_binds_the_childs_receipts_and_warns_on_observed_beyond(roots, work):
    _seed(roots)
    res_mat = materialize(
        MaterializeSpec(name="tiny", mode="npy", data_root=roots.data, configs_root=roots.configs)
    )
    assert res_mat.failed == 0
    (work / "access_train.py").write_text(ACCESS_FAKE, encoding="utf-8")
    res = train_run(_spec(roots, work, command=[sys.executable, "access_train.py"]))
    assert res.attempt.status == "finished", res.record
    assert (res.receipts, res.denied, res.provenance) == (1, 0, "receipt")
    assert res.observed_beyond == [] and res.receipt_invalid == 0
    card = load_run(roots.data, "r1")
    assert [r.artifact_id for r in card.access] == ["r1-a1-1"]
    assert card.access[0].subsets == ["train"] and card.access == res.record.access
    assert any(e["event"] == "access" for e in read_events(roots.data, "r1"))
    (work / "peek_train.py").write_text(PEEK_FAKE, encoding="utf-8")
    res = train_run(_spec(roots, work, run_id="r2", command=[sys.executable, "peek_train.py"]))
    assert res.attempt.status == "finished", res.record
    assert res.receipts == 2 and res.observed_beyond == ["valA"] and res.provenance == "receipt"
    assert "observed_beyond_trained_on=valA" in res.warnings
    assert [r.artifact_id for r in load_run(roots.data, "r2").access] == ["r2-a1-1", "r2-a1-2"]
    # a resume adds attempt-2 receipts next to the attempt-1 ones
    res = train_run(
        _spec(roots, work, run_id="r2", command=[sys.executable, "peek_train.py"], resume=True)
    )
    ids = [r.artifact_id for r in load_run(roots.data, "r2").access]
    assert ids == ["r2-a1-1", "r2-a1-2", "r2-a2-1", "r2-a2-2"] and res.receipts == 4


def test_train_run_without_receipts_grades_export_or_declared(roots, work, tmp_path):
    _seed(roots)
    res = train_run(_spec(roots, work))
    assert res.receipts == 0 and res.provenance == "declared"
    export = _export(roots, "train", tmp_path / "yolo-train")
    res = train_run(_spec(roots, work, run_id="r3", exports=[export], trained_on=[]))
    assert res.provenance == "export"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/train/test_run.py -o addopts="" -q -k "receipts or grades_export"`
Expected: FAIL（`AttributeError: 'RunResult' object has no attribute 'receipts'`）

- [ ] **Step 3: 改 `src/vcp/train/run.py`**

import 區：加 `from vcp.data.schema import DatasetCard`、`from vcp.measure.provenance import provenance`（`Dataset` 仍要，`load_card` 是它的 classmethod）。`RunResult` 加五個欄位：

```python
    receipts: int = 0
    denied: int = 0
    provenance: str = "declared"
    observed_beyond: list[str] = Field(default_factory=list)
    receipt_invalid: int = 0
```

`derive_trained_on` 的簽名與兩處用法改成 card：

```python
def derive_trained_on(
    exports: list[Path],
    explicit: list[str],
    *,
    card: DatasetCard,
    plan: SplitPlan,
    data_root: Path,
) -> tuple[list[str], list[ExportRef]]:
    ...
        expected = {
            "dataset": card.name,
            "samples_hash": card.samples_hash,
            "plan_id": plan.plan_id,
        }
```

`_existing(spec, data_root, dataset_card: DatasetCard, trained_on, chash)`：內文 `assert_run_matches(card, dataset)` → `assert_run_matches(card, dataset_card)`。`_new(spec, *, data_root, dataset_card: DatasetCard, trained_on, refs, chash, cwd)`：`dataset.card.name` → `dataset_card.name`、`dataset.card.samples_hash` → `dataset_card.samples_hash`（`RunCard` 兩處、`TrainRecord` 一處）。

`train_run` 裡的

```python
    dataset = Dataset.load(spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root)
    plan = load_plan(paths, spec.plan_id)
    assert_plan_matches(plan, dataset.card)
```

改成

```python
    dataset_card = Dataset.load_card(
        spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    plan = load_plan(paths, spec.plan_id)
    assert_plan_matches(plan, dataset_card)
```

其後 `derive_trained_on(spec.exports, spec.trained_on, card=dataset_card, plan=plan, data_root=data_root)`、`_existing(spec, data_root, dataset_card, trained_on, chash)`、`_new(..., dataset_card=dataset_card, ...)`。在 `append_event(data_root, spec.run_id, "finished", n, ...)` 之後、`_finish_checkpoints` 之前加：

```python
    # spec 7.1: the receipts the child bound to this attempt become the run's access record.
    card = card.model_copy(update={"access": list(record.access)})
    save_run(data_root, card)
```

回傳前（`if spec.venv is None:` 之後）加：

```python
    info = provenance(card, data_root=data_root, configs_root=configs_root)
    observed_beyond = sorted(set(info.observed) - set(trained_on))
    if observed_beyond:
        warnings.append(f"observed_beyond_trained_on={','.join(observed_beyond)}")
    if info.invalid:
        warnings.append(f"receipt_invalid={len(info.invalid)}")
```

`RunResult(...)` 加 `receipts=len(card.access)`、`denied=sum(r.denied for r in card.access)`、`provenance=info.grade`、`observed_beyond=observed_beyond`、`receipt_invalid=len(info.invalid)`。

`src/vcp/cli_train.py` 的 `fields` dict 在 `"venv"` 之後加 `"receipts": res.receipts, "denied": res.denied, "provenance": res.provenance`；`if res.skipped:` 之後加：

```python
        if res.observed_beyond:
            fields["observed_beyond_trained_on"] = ",".join(res.observed_beyond)
        if res.receipt_invalid:
            fields["receipt_invalid"] = res.receipt_invalid
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/train/run.py src/vcp/cli_train.py tests/unit/train/test_run.py && uv run pytest tests/unit/train tests/unit/test_cli_train.py tests/unit/test_e2e_train.py -o addopts="" -q`
Expected: 全部通過

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/train/run.py src/vcp/cli_train.py tests/unit/train/test_run.py
git commit -m "feat(train): train run 父程序 card-only，收據抄進 run.yaml，觀測超出宣告則 WARN" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: `ingest --receipt`、`measure` 吃「宣告 ∪ 觀測」、`status` / `report` 印 provenance

**Files:**
- Modify: `src/vcp/measure/ingest.py`、`src/vcp/measure/measure.py`、`src/vcp/measure/report.py`、`src/vcp/cli_eval.py`（ingest / measure / status / report）
- Test: `tests/unit/measure/test_measure.py`、`tests/unit/measure/test_report.py`、`tests/unit/test_cli_eval.py`

**Interfaces:**
- Consumes: Task 3 `DatasetAccess`；Task 5 `provenance` / `attach_receipts` / `Reading.provenance`。
- Produces: `IngestSpec.receipts: list[str]`、`IngestResult.provenance: str`；`load_card_context(run_id, data_root, configs_root) -> (RunCard, DatasetCard, SplitPlan, DatasetPaths)`；`default_subsets(plan, card, *, unseal, observed=())`；`MeasureResult` 多 `provenance: str`、`observed: list[str]`、`receipt_invalid: int`；`StatusResult` 多 `provenance: dict[str, str]`、`observed: dict[str, list[str]]`；`report_rows` 每列多 `provenance`；CLI 欄位如 spec §9。

- [ ] **Step 1: 寫失敗的測試**

追加到 `tests/unit/measure/test_measure.py` 檔尾（import 區加 `from vcp.data.access.access import DatasetAccess`、`from vcp.measure.provenance import attach_receipts`）：

```python
def _receipt(roots, subsets, *, purpose, run_id):
    with DatasetAccess.open(
        "tiny",
        "fixed-v1",
        subsets=set(subsets),
        purpose=purpose,
        run_id=run_id,
        data_root=roots.data,
        configs_root=roots.configs,
    ) as access:
        for s in subsets:
            list(access.iter(s))
    return access.receipt_id


def test_observed_subsets_are_not_clean_bases(roots, tmp_path):
    _, plan, paths = det_with_runs(roots, tmp_path, n=40)
    noisy = load_run(roots.data, "noisy")
    save_run(
        roots.data,
        attach_receipts(
            noisy, [_receipt(roots, ["valA"], purpose="custom", run_id="noisy")], data_root=roots.data
        ),
    )
    card = load_run(roots.data, "noisy")
    assert default_subsets(plan, card, unseal=False, observed=["valA"]) == ["valB"]
    res = measure_run(_spec(roots, "noisy"))
    assert {r.subset for r in res.readings} == {"valB"}
    assert res.provenance == "declared" and res.observed == ["valA"]
    assert all(r.provenance == "declared" for r in res.readings)
    with pytest.raises(ValidationFailed, match="^contaminated: subset 'valA' was read by the run") as ei:
        measure_run(_spec(roots, "noisy", subsets=["valA"]))
    assert ei.value.fields == {"subset": "valA"}


def test_readings_carry_the_runs_grade_and_measure_leaves_its_own_receipt(roots, tmp_path):
    _, plan, paths = det_with_runs(roots, tmp_path, n=40)
    perfect = load_run(roots.data, "perfect")
    save_run(
        roots.data,
        attach_receipts(
            perfect, [_receipt(roots, ["train"], purpose="train", run_id="perfect")], data_root=roots.data
        ),
    )
    res = measure_run(_spec(roots, "perfect"))
    assert res.provenance == "receipt" and res.observed == ["train"]
    assert {r.provenance for r in res.readings} == {"receipt"}
    receipts = sorted(
        p.name for p in (roots.data / "artifacts" / "access_receipt").iterdir() if p.is_dir()
    )
    measure_receipts = [r for r in receipts if r.startswith("measure-tiny-fixed-v1-")]
    assert len(measure_receipts) == 1
    from vcp.data.access.receipt import read_receipt

    receipt = read_receipt(roots.data, measure_receipts[0]).receipt
    assert receipt.run_id == "perfect" and set(receipt.accessed) == {"valA", "valB"}
    # a stale receipt warns and the grade falls back
    pj = paths.plan_json("fixed-v1")
    pj.write_bytes(pj.read_bytes() + b"\n")
    res = measure_run(_spec(roots, "perfect"))
    assert res.receipt_invalid == 1 and res.provenance == "declared"
    assert "receipt_invalid=1" in res.warnings
```

追加到 `tests/unit/measure/test_report.py` 檔尾（`det_with_runs`、`MeasureSpec`、`measure_run`、`report_rows`、`status` 已 import；import 區加 `from vcp.data.access.access import DatasetAccess`、`from vcp.measure.provenance import attach_receipts`、`from vcp.measure.runs import load_run, save_run`）：

```python
def test_status_and_report_show_provenance(roots, tmp_path):
    _, plan, paths = det_with_runs(roots, tmp_path, n=40)
    with DatasetAccess.open(
        "tiny", "fixed-v1", subsets={"train"}, purpose="train", run_id="perfect",
        data_root=roots.data, configs_root=roots.configs,
    ) as access:
        list(access.iter("train"))
    card = attach_receipts(load_run(roots.data, "perfect"), [access.receipt_id], data_root=roots.data)
    save_run(roots.data, card)
    measure_run(MeasureSpec(run_id="perfect", data_root=roots.data, configs_root=roots.configs))
    st = status(paths)
    assert st.provenance == {"noisy": "declared", "perfect": "receipt"}
    assert st.observed == {"noisy": [], "perfect": ["train"]}
    rows = report_rows(paths)
    assert {r["run_id"]: r["provenance"] for r in rows} == {"perfect": "receipt"}
```

追加到 `tests/unit/test_cli_eval.py` 檔尾（`runner`、`perfect_predictions`、`write_predictions`、`Dataset`、`load_run` 已 import；`from helpers import (...)` 加 `det_with_runs`，import 區加 `from vcp.data.access.access import DatasetAccess`）：

```python
def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def test_ingest_receipt_binds_and_refuses_the_wrong_run(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=40)

    def receipt(run_id):
        with DatasetAccess.open(
            "tiny", "fixed-v1", subsets={"train"}, purpose="train", run_id=run_id,
            data_root=roots.data, configs_root=roots.configs,
        ) as access:
            list(access.iter("train"))
        return access.receipt_id

    src = tmp_path / "again.jsonl"
    write_predictions(src, perfect_predictions(ds.subset("valA", plan), ds.card))
    base = ["eval", "ingest", "--run", "perfect", "--dataset", "tiny", "--plan", "fixed-v1",
            "--subset", "valA", "--format", "jsonl", "--src", str(src), "--replace"]
    r = runner.invoke(app, [*base, "--receipt", receipt("perfect")])
    v = _verdict(r.output)
    assert r.exit_code == 0 and "receipts=1" in v and "provenance=receipt" in v
    r = runner.invoke(app, [*base, "--receipt", receipt("noisy")])
    assert r.exit_code == 1 and "mismatch: receipt" in _verdict(r.output)
    r = runner.invoke(app, ["eval", "measure", "--run", "perfect"])
    assert r.exit_code == 0 and "provenance=receipt" in _verdict(r.output) and "observed=train" in _verdict(r.output)
    r = runner.invoke(app, ["eval", "status", "--dataset", "tiny"])
    v = _verdict(r.output)
    assert r.exit_code == 0 and "receipt_runs=1" in v and "declared_runs=1" in v
    assert "perfect: provenance=receipt observed=train" in r.output
    r = runner.invoke(app, ["eval", "report", "--dataset", "tiny"])
    assert r.exit_code == 0 and "receipt" in r.output
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/measure/test_measure.py tests/unit/measure/test_report.py tests/unit/test_cli_eval.py -o addopts="" -q -k "provenance or observed or receipt"`
Expected: FAIL（`TypeError: default_subsets() got an unexpected keyword argument 'observed'` 等）

- [ ] **Step 3: 改四個檔**

`src/vcp/measure/ingest.py`：`IngestSpec` 加 `receipts: list[str] = Field(default_factory=list)`；`IngestResult` 加 `provenance: str`；import 區加 `from vcp.measure.provenance import attach_receipts, provenance`；`ingest()` 裡 `card, created = _run_card(...)` 之後加

```python
    if spec.receipts:
        card = attach_receipts(card, spec.receipts, data_root=paths.data_root)
```

`save_run(paths.data_root, card)` 之後加 `info = provenance(card, data_root=paths.data_root, configs_root=paths.configs_root)`，`IngestResult(...)` 加 `provenance=info.grade`。

`src/vcp/measure/measure.py`：

import 區加 `from vcp.data.access.access import DatasetAccess`、`from vcp.data.schema import DatasetCard`、`from vcp.measure.provenance import ProvenanceInfo, provenance`。`MeasureResult` 加 `provenance: str`、`observed: list[str] = Field(default_factory=list)`、`receipt_invalid: int = 0`。`load_context` 之後加：

```python
def load_card_context(
    run_id: str, data_root: Path | None, configs_root: Path | None
) -> tuple[RunCard, DatasetCard, SplitPlan, DatasetPaths]:
    """``load_context`` without parsing the samples file: what ``measure_run`` needs before it
    opens a role-scoped access (spec 6.7). judge / sigma / anchor keep ``load_context``."""
    card = load_run(resolve_data_root(data_root), run_id)
    paths = DatasetPaths.resolve(card.dataset, data_root=data_root, configs_root=configs_root)
    dataset_card = Dataset.load_card(card.dataset, data_root=data_root, configs_root=configs_root)
    assert_run_matches(card, dataset_card)
    plan = load_plan(paths, card.plan_id)
    return card, dataset_card, plan, paths
```

`default_subsets` 改成：

```python
def default_subsets(
    plan: SplitPlan, card: RunCard, *, unseal: bool, observed: list[str] | tuple[str, ...] = ()
) -> list[str]:
    """Clean eval subsets for this run; sealed ones only when unsealing.

    A subset the run's receipts show it read is not clean whatever ``trained_on`` says (spec 8);
    with nothing trained on and nothing observed, every eval subset is clean (spec 6.1).
    """
    touched = set(card.trained_on) | set(observed)
    if touched:
        names = [n.removesuffix(SEALED_SUFFIX) for n in clean_eval_subsets(plan, touched)]
    else:
        names = [s.name for s in plan.subsets if s.role in ("eval", "sealed")]
    roles = {s.name: s.role for s in plan.subsets}
    return [n for n in names if roles[n] != "sealed" or unseal]
```

`_check_subsets(card, subsets, info: ProvenanceInfo)`：在 `for name in subsets:` 迴圈裡、既有的 `trained_on` 檢查之後、`predictions` 檢查之前加（宣告過的訓練子集仍報舊訊息，只有收據才知道的才報 `contaminated:`）

```python
        if name in info.observed:
            receipt = next((r.artifact_id for r in info.receipts if name in r.subsets), "?")
            raise ValidationFailed(
                f"contaminated: subset {name!r} was read by the run (receipt {receipt})",
                fields={"subset": name},
            )
```

`_Context`：`dataset: Dataset` → `dataset_card: DatasetCard`，加 `provenance: str`；`_guardrail` 的兩處 `ctx.dataset` → `ctx.dataset_card`（`assert_run_matches(anchor_run, ctx.dataset_card)`、`metric.compute(..., ctx.dataset_card, params)`）；`_measure_one` 的 `metric.compute(samples, preds, ctx.dataset_card, params)`，`Reading(...)` 加 `provenance=ctx.provenance`。

`measure_run` 改成：

```python
def measure_run(spec: MeasureSpec) -> MeasureResult:
    card, dataset_card, plan, paths = load_card_context(
        spec.run_id, spec.data_root, spec.configs_root
    )
    info = provenance(card, data_root=paths.data_root, configs_root=paths.configs_root)
    subsets = _unique(
        spec.subsets or default_subsets(plan, card, unseal=spec.unseal, observed=info.observed)
    )
    _check_subsets(card, subsets, info)
    metric_names = _unique(_resolve_metrics(dataset_card.task, spec.metrics))
    _check_params(metric_names, spec.params)
    ctx = _Context(
        spec=spec,
        card=card,
        dataset_card=dataset_card,
        plan=plan,
        paths=paths,
        ledger=ReadingsLedger(paths.measure_dir / READINGS_LEDGER),
        anchors=load_anchors(paths),
        provenance=info.grade,
    )
    readings: list[Reading] = []
    pending: list[Reading] = []
    states: list[str] = []
    warnings: list[str] = []
    if info.invalid:
        warnings.append(f"receipt_invalid={len(info.invalid)}")
    cached = 0
    if spec.unseal and not spec.reason:  # the message Dataset.subset gave before the accessor
        raise SealedSubsetError("unseal requires a non-empty reason")
    with DatasetAccess.open(
        card.dataset,
        card.plan_id,
        subsets=set(subsets),
        purpose="measure",
        unseal_reason=spec.reason if spec.unseal else None,
        caller=CALLER,
        run_id=card.run_id,
        data_root=paths.data_root,
        configs_root=paths.configs_root,
    ) as access:
        for subset in subsets:
            samples = list(access.records(subset).values())
            path = verify_prediction(paths.data_root, card, subset)
            preds = predictions_by_id(read_predictions(path))
            for name in metric_names:
                cell = _measure_one(ctx, subset, samples, preds, get_metric(name))
                readings.append(cell.reading)
                states.append("OK" if cell.anchored else "none")
                if not cell.anchored:
                    warnings.append(
                        f"no anchor for {cell.key}; run `vcp eval anchor` once a reference run "
                        "exists"
                    )
                if cell.is_new:
                    pending.append(cell.reading)
                else:
                    cached += 1
    for reading in pending:  # only now that every guardrail has passed (spec 9)
        ctx.ledger.append(reading)
    return MeasureResult(
        run_id=card.run_id,
        dataset=card.dataset,
        readings=readings,
        new=len(pending),
        cached=cached,
        guardrail=_guardrail_state(states, wrote=bool(pending)),
        warnings=warnings,
        provenance=info.grade,
        observed=info.observed,
        receipt_invalid=len(info.invalid),
    )
```

（import 區的 `from vcp.core.errors import GuardrailError, ValidationFailed` 加上 `SealedSubsetError`；`Sample` import 仍被 `_guardrail` / `_measure_one` 的型別用到；`Dataset` 仍被 `load_context` 用到。存取器對「`--subsets` 點名 sealed 子集但沒 `--unseal`」一樣丟 `SealedSubsetError`，既有測試不用改。）

`src/vcp/measure/report.py`：import 區加 `from vcp.measure.provenance import provenance`；`StatusResult` 加 `provenance: dict[str, str] = field(default_factory=dict)`、`observed: dict[str, list[str]] = field(default_factory=dict)`；`_runs_for` 回傳 `tuple[list[RunCard], list[str]]`（`cards.append(card)` 取代 `runs += 1`）；`status()` 改成 `cards, unreadable = _runs_for(paths)`，並在 `sigma` 之前加

```python
    grades: dict[str, str] = {}
    observed: dict[str, list[str]] = {}
    for card in sorted(cards, key=lambda c: c.run_id):
        info = provenance(card, data_root=paths.data_root, configs_root=paths.configs_root)
        grades[card.run_id] = info.grade
        observed[card.run_id] = info.observed
```

`StatusResult(...)` 加 `runs=len(cards)`（取代原本的 `runs`）、`provenance=grades`、`observed=observed`。`report_rows` 的每列 dict 加 `"provenance": r.provenance or "-"`。

`src/vcp/cli_eval.py`：

- `ingest_cmd`：選項 `receipt: Annotated[list[str] | None, typer.Option("--receipt", help="access receipt artifact id to bind to the run (repeatable)")] = None`（放 `plugin` 之前）；`IngestSpec(..., receipts=list(receipt or []))`；`fields` 加 `"receipts": len(res.run.access), "provenance": res.provenance`。
- `measure_cmd`：`fields` 加 `"provenance": res.provenance`；`if res.observed: fields["observed"] = ",".join(res.observed)`；`if res.receipt_invalid: fields["receipt_invalid"] = res.receipt_invalid`。
- `status_cmd`：`fields` 加 `"receipt_runs": sum(1 for g in st.provenance.values() if g == "receipt")`、`"export_runs": …== "export"`、`"declared_runs": …== "declared"`；`human` 加 `[f"{run}: provenance={g} observed={','.join(st.observed[run]) or '-'}" for run, g in st.provenance.items()]`。
- `report_cmd`：human 行尾加 `  {r['provenance']}`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/measure src/vcp/cli_eval.py tests/unit/measure tests/unit/test_cli_eval.py && uv run pytest tests/unit/measure tests/unit/test_cli_eval.py tests/unit/test_e2e_eval.py tests/unit/fuse tests/unit/submit -o addopts="" -q`
Expected: 全部通過（融合層與提交層經 `measure_run` 的路徑改走存取器；它們的 fixture 讀 eval 子集是正當的）

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/measure/ingest.py src/vcp/measure/measure.py src/vcp/measure/report.py src/vcp/cli_eval.py tests/unit/measure/test_measure.py tests/unit/measure/test_report.py tests/unit/test_cli_eval.py
git commit -m "feat(measure): ingest --receipt、measure 以宣告 ∪ 觀測選乾淨基底並走存取器、status / report 印 provenance" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: `judge`——讀過主張子集的 run 使判決 `INVALID`

**Files:**
- Modify: `src/vcp/measure/judge.py:53-60,322-385`、`src/vcp/cli_eval.py`（judge fields）
- Test: `tests/unit/measure/test_prereg_judge.py`

**Interfaces:**
- Consumes: Task 5 `provenance`；`vcp.measure.runs.load_run`；`vcp.core.paths.resolve_data_root`。
- Produces: `CONTAMINATED = "contaminated"`；`Judgement.provenance` = 候選等級；verdict `INVALID` + reasons `contaminated:<run>/<subset>`；`judge` VERDICT `provenance=`。

- [ ] **Step 1: 寫失敗的測試**——追加到 `tests/unit/measure/test_prereg_judge.py` 檔尾（import 區加 `from vcp.data.access.access import DatasetAccess`、`from vcp.measure.provenance import attach_receipts`、`from vcp.measure.runs import load_run, save_run`）：

```python
def _attach(roots, run_id, subsets, *, purpose="custom"):
    with DatasetAccess.open(
        "tiny", "fixed-v1", subsets=set(subsets), purpose=purpose, run_id=run_id,
        data_root=roots.data, configs_root=roots.configs,
    ) as access:
        for s in subsets:
            list(access.iter(s))
    card = attach_receipts(load_run(roots.data, run_id), [access.receipt_id], data_root=roots.data)
    save_run(roots.data, card)


def test_a_run_that_read_a_claimed_subset_makes_the_judgement_invalid(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    _measure(roots, "perfect")
    create_prereg(paths, _pr(), _ledger(paths))
    _measure(roots, "noisy")
    _attach(roots, "noisy", ["valA"])  # the candidate peeked at valA after being measured
    j = _judge(roots)
    assert j.verdict == "INVALID" and "contaminated:noisy/valA" in j.reasons
    assert j.provenance == "declared" and j.per_subset == {}
    _attach(roots, "perfect", ["valB"])  # the baseline too
    j = _judge(roots)
    assert j.reasons[:2] == ["contaminated:noisy/valA", "contaminated:perfect/valB"]


def test_a_train_receipt_grades_the_judgement(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    _attach(roots, "noisy", ["train"], purpose="train")
    _measure(roots, "perfect")
    create_prereg(paths, _pr(), _ledger(paths))
    _measure(roots, "noisy")
    j = _judge(roots)
    assert j.verdict in ("PASS", "FAIL") and j.provenance == "receipt"
    assert not any(r.startswith("contaminated") for r in j.reasons)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/measure/test_prereg_judge.py -o addopts="" -q -k "contaminated or grades_the_judgement"`
Expected: FAIL（`assert j.verdict == "INVALID"`——現在是 PASS / FAIL；`Judgement` 沒有 `provenance` 值）

- [ ] **Step 3: 改 `src/vcp/measure/judge.py`**——字彙區加 `CONTAMINATED = "contaminated"`；import 區加 `from vcp.core.paths import resolve_data_root`、`from vcp.measure.provenance import provenance`、`from vcp.measure.runs import load_run`；`judge_prereg` 裡 `reasons = _Reasons()` 之後加

```python
    root = resolve_data_root(spec.data_root)
    info_b = provenance(
        load_run(root, pr.candidate_run), data_root=root, configs_root=spec.configs_root
    )
    info_a = provenance(
        load_run(root, pr.baseline_run), data_root=root, configs_root=spec.configs_root
    )
    # spec 8: a number measured on a subset the run read is not evidence, on either side.
    contaminated = [
        f"{run}/{s}"
        for run, info in ((pr.candidate_run, info_b), (pr.baseline_run, info_a))
        for s in sorted(set(pr.subsets) & set(info.observed))
    ]
```

verdict 的分支改成三段：

```python
    if contaminated:
        verdict = "INVALID"
        for c in contaminated:
            reasons.block(f"{CONTAMINATED}:{c}")
    elif any(b.ts < logged_at for _, b in pairs.values()):
        verdict = "INVALID"
        reasons.block(MEASURED_BEFORE_PREREG)
    elif not reasons.blocking:  # step 2
        per_subset = _per_subset(spec, pr, pairs, metric, params)
```

`Judgement(...)` 加 `provenance=info_b.grade`。`src/vcp/cli_eval.py` 的 `judge_cmd` `fields` 加 `"provenance": j.provenance or "-"`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/measure/judge.py src/vcp/cli_eval.py tests/unit/measure/test_prereg_judge.py && uv run pytest tests/unit/measure tests/unit/test_cli_eval.py tests/unit/submit tests/unit/fuse -o addopts="" -q`
Expected: 全部通過

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/measure/judge.py src/vcp/cli_eval.py tests/unit/measure/test_prereg_judge.py
git commit -m "feat(measure): judge——候選或基準讀過主張子集即 INVALID contaminated，記 provenance" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: 提交層——`require_provenance`、`observed_sealed:`、`Staged.provenance`、final 重算、status

**Files:**
- Modify: `src/vcp/submit/schema.py:84-108,210-235`、`src/vcp/submit/stage.py`、`src/vcp/submit/final.py`、`src/vcp/submit/report.py:23-33,90-131`、`src/vcp/cli_submit.py`（stage / final / status）
- Test: `tests/unit/submit/test_stage.py`、`tests/unit/submit/test_final.py`、`tests/unit/submit/test_report.py`

**Interfaces:**
- Consumes: Task 5 `provenance` / `attach_receipts`；Task 3 `DatasetAccess`；Task 2 `Grade` / `GRADE_RANK`。
- Produces: `PlatformProfile.require_provenance: Grade = "declared"`；`Staged.provenance: Grade | None = None`；`FinalEntry.provenance: Grade | None = None`；`StatusView.provenance: dict[str, str]`；`stage` 的兩個新拒絕；`final` 的兩個新 `why`；CLI 欄位 `provenance=`。

- [ ] **Step 1: 寫失敗的測試**

追加到 `tests/unit/submit/test_stage.py` 檔尾（import 區加 `from vcp.data.access.access import DatasetAccess`、`from vcp.measure.provenance import attach_receipts`、`from vcp.data.access.receipt import read_receipt`）：

```python
def _bind(pair, run_id, subsets, *, purpose="train"):
    with DatasetAccess.open(
        EVAL, "fixed-v1", subsets=set(subsets), purpose=purpose,
        unseal_reason="test" if "holdout" in subsets else None,
        data_root=pair.roots.data, configs_root=pair.roots.configs,
    ) as access:
        for s in subsets:
            list(access.iter(s))
    card = attach_receipts(
        load_run(pair.roots.data, run_id), [access.receipt_id], data_root=pair.roots.data
    )
    save_run(pair.roots.data, card)
    return access.receipt_id


def test_stage_records_provenance_and_the_profile_can_require_it(ready):
    res = stage(_spec(ready, "S1", "good", "good.test"))
    assert res.staged.provenance == "declared"
    dump_yaml_model(_profile(require_provenance="receipt"), ready.test_paths.submit_yaml)
    with pytest.raises(ValidationFailed, match="^provenance_required: run 'good' is declared") as ei:
        stage(_spec(ready, "S2", "good", "good.test"))
    assert ei.value.fields == {"run": "good", "provenance": "declared"}
    assert not ready.test_paths.submission_dir("S2").exists()
    _bind(ready, "good", ["train"])
    res = stage(_spec(ready, "S2", "good", "good.test"))
    assert res.staged.provenance == "receipt"
    assert load_staged(ready.test_paths, "S2").provenance == "receipt"
    # a baseline is waived from the requirement; a candidate that read the sealed subset never passes
    res = stage(_spec(ready, "S3", "bad", "bad.test", kind="baseline", reason="ref"))
    assert res.staged.provenance == "declared"
    _bind(ready, "bad", ["train", "holdout"], purpose="custom")
    with pytest.raises(ValidationFailed, match="^observed_sealed: 'bad' read 'holdout'"):
        stage(_spec(ready, "S4", "bad", "bad.test"))


def test_stage_reads_the_test_subset_through_a_receipt(ready):
    res = stage(_spec(ready, "S1", "good", "good.test"))
    receipts = [
        p.name
        for p in (ready.roots.data / "artifacts" / "access_receipt").iterdir()
        if p.name.startswith(f"submit-{TEST}-all-v1-")
    ]
    assert len(receipts) == 1
    receipt = read_receipt(ready.roots.data, receipts[0]).receipt
    assert receipt.run_id == "good" and set(receipt.accessed) == {"test"}
    assert res.staged.artifact.rows == receipt.accessed["test"].ids_count
```

追加到 `tests/unit/submit/test_final.py` 檔尾（import 區加 `from vcp.core.config import dump_yaml_model`、`from vcp.data.access.access import DatasetAccess`、`from vcp.measure.provenance import attach_receipts`、`from vcp.measure.runs import load_run, save_run`）：

```python
def test_final_recomputes_provenance_and_drops_candidates_below_the_bar(uploaded):
    _measure_holdout(uploaded, "good")
    _measure_holdout(uploaded, "bad")
    dump_yaml_model(_profile(require_provenance="receipt"), uploaded.test_paths.submit_yaml)
    res = final(TEST, dry_run=True, **_kw(uploaded))
    table = {e.submission_id: e for e in res.row.table}
    assert table["S1"].why == "provenance_required" and table["S1"].provenance == "declared"
    assert table["S2"].eligible and table["S2"].provenance == "declared"  # baseline: waived
    assert res.chosen == ["S2"]
    with DatasetAccess.open(
        EVAL, "fixed-v1", subsets={"train"}, purpose="train", run_id="good",
        data_root=uploaded.roots.data, configs_root=uploaded.roots.configs,
    ) as access:
        list(access.iter("train"))
    card = attach_receipts(
        load_run(uploaded.roots.data, "good"), [access.receipt_id], data_root=uploaded.roots.data
    )
    save_run(uploaded.roots.data, card)
    res = final(TEST, dry_run=True, **_kw(uploaded))
    table = {e.submission_id: e for e in res.row.table}
    assert table["S1"].eligible and table["S1"].provenance == "receipt"
    assert res.chosen == ["S1"]
```

追加到 `tests/unit/submit/test_report.py` 檔尾（檔內已有 `_profile`、`_kw`、`_seed(pair, profile)`——後者 stage S1 候選與 S2 基準——與 `status`）：

```python
def test_status_lists_each_staged_provenance(pair):
    _seed(pair, _profile())
    st = status(TEST, **_kw(pair))
    assert st.provenance == {"S1": "declared", "S2": "declared"}
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/unit/submit -o addopts="" -q -k "provenance or receipt"`
Expected: FAIL（`ValidationError: … require_provenance … Extra inputs are not permitted`）

- [ ] **Step 3: 改五個檔**

`src/vcp/submit/schema.py`：import 區加 `from vcp.data.access.schema import Grade`；`PlatformProfile` 的 `created_at` 之前加 `require_provenance: Grade = "declared"`；`Staged` 的 `vcp_version` 之後加 `provenance: Grade | None = None`；`FinalEntry` 的 `staged_at` 之後加 `provenance: Grade | None = None`。

`src/vcp/submit/stage.py`：import 區加 `from vcp.data.access.access import DatasetAccess`、`from vcp.data.access.schema import GRADE_RANK`、`from vcp.data.schema import DatasetCard`、`from vcp.measure.provenance import provenance`；`_run_on(data_root, run_id, dataset_card: DatasetCard, plan_id, side)`（內文 `assert_run_matches(card, dataset_card)`）。`_render` 改簽名與前段：

```python
def _render(
    spec: StageSpec,
    profile: PlatformProfile,
    paths: DatasetPaths,
    test_card: RunCard,
    warnings: list[str],
) -> tuple[Artifact, Path]:
    """The submission file, written into a temporary directory that becomes the submission
    directory only once everything else has succeeded. The test subset is read through a
    role-scoped access whose receipt names the candidate's eval run (spec 6.7)."""
    with DatasetAccess.open(
        profile.dataset,
        profile.test_plan,
        subsets={profile.test_subset},
        purpose="submit",
        caller="vcp submit stage",
        run_id=spec.eval_run,
        data_root=paths.data_root,
        configs_root=paths.configs_root,
    ) as access:
        samples = list(access.iter(profile.test_subset))
        test_ds = Dataset(access.card, samples)
    writer = writer_for(profile.writer or "", test_ds.card.task)
    options = {**profile.writer_opts, **spec.writer_opts}
    preds = read_predictions(verify_prediction(paths.data_root, test_card, profile.test_subset))
```

（其後從 `tmp = paths.submit_dir / …` 起不變；舊的 `samples = test_ds.subset(...)` 行刪掉。）`stage()` 裡：`eval_ds = Dataset.load(...)` → `eval_dataset_card = Dataset.load_card(profile.eval_dataset, data_root=spec.data_root, configs_root=spec.configs_root)`；`_run_on(paths.data_root, spec.eval_run, eval_dataset_card, profile.plan_id, "eval")`；`test_ds = Dataset.load(...)` → `test_dataset_card = Dataset.load_card(profile.dataset, ...)`，`test_plan = load_plan(paths, profile.test_plan)` 這行刪掉；`_run_on(paths.data_root, spec.test_run, test_dataset_card, profile.test_plan, "test")`；`_render(spec, profile, paths, test_card, warnings)`。檔尾的 `verify()`（從 test run 重渲染比對位元組）維持 `Dataset.load` + `test_ds.subset(...)`——它是驗證檔案，不是訓練讀，不留收據；`SplitPlan` 若因此不再被引用就從 `from vcp.data.split import SplitPlan, load_plan` 拿掉。在 `trained_on_sealed` 檢查之後加：

```python
    info = provenance(eval_card, data_root=paths.data_root, configs_root=paths.configs_root)
    if spec.kind == "candidate":
        if profile.sealed_subset in info.observed:
            raise ValidationFailed(
                f"observed_sealed: {spec.eval_run!r} read {profile.sealed_subset!r} (access "
                "receipt); it can never have a clean sealed reading",
                fields={"run": spec.eval_run},
            )
        if GRADE_RANK[info.grade] < GRADE_RANK[profile.require_provenance]:
            raise ValidationFailed(
                f"provenance_required: run {spec.eval_run!r} is {info.grade}, profile requires "
                f"{profile.require_provenance}",
                fields={"run": spec.eval_run, "provenance": info.grade},
            )
```

`Staged(...)` 加 `provenance=info.grade`。

`src/vcp/submit/final.py`：import 區加 `from vcp.data.access.schema import GRADE_RANK, Grade`、`from vcp.measure.provenance import provenance`；迴圈裡從 `if st.kind == "probe":` 到 `reading, why = sealed_reading(...)` 改成：

```python
        grade: Grade | None = None
        if st.kind == "probe":
            why = "probe"
        elif not ledger.uploads(sid):
            why = "not_uploaded"
        else:
            card = load_run(paths.data_root, str(st.eval_run))
            info = provenance(card, data_root=paths.data_root, configs_root=configs_root)
            grade = info.grade
            reading, why = sealed_reading(readings, card, profile, params_hash, sealed_size)
            if why == "" and st.kind == "candidate":
                if profile.sealed_subset in info.observed:
                    why = "observed_sealed"
                elif GRADE_RANK[info.grade] < GRADE_RANK[profile.require_provenance]:
                    why = "provenance_required"
```

`FinalEntry(...)` 加 `provenance=grade`。

`src/vcp/submit/report.py`：`StatusView` 加 `provenance: dict[str, str]`；`status()` 在 `unscored` 之後加

```python
    grades = {sid: (load_staged(paths, sid).provenance or "-") for sid in ledger.ids()}
```

（import `from vcp.submit.stage import load_staged`；若已 import `stage` 模組別名則沿用），`StatusView(..., provenance=grades)`。

`src/vcp/cli_submit.py`：`stage_cmd` 的 `fields` 加 `if st.provenance: fields["provenance"] = st.provenance`；`final_cmd` 的 human 行尾加 ` provenance={e.provenance or '-'}`；`status_cmd` 的 `human` 加 `[f"provenance: {sid}={g}" for sid, g in st.provenance.items()]`，payload 加 `"provenance": st.provenance`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run ruff format src/vcp/submit src/vcp/cli_submit.py tests/unit/submit && uv run pytest tests/unit/submit tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py tests/unit/backup -o addopts="" -q`
Expected: 全部通過

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/submit/schema.py src/vcp/submit/stage.py src/vcp/submit/final.py src/vcp/submit/report.py src/vcp/cli_submit.py tests/unit/submit/test_stage.py tests/unit/submit/test_final.py tests/unit/submit/test_report.py
git commit -m "feat(submit): require_provenance、observed_sealed、Staged / final 的 provenance，test 子集經存取器" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: 備份證據圖、RSNA 專案 `with`、端到端、回歸門檻列、真資料唯讀測試

**Files:**
- Modify: `src/vcp/backup/schema.py:16-58`、`src/vcp/backup/evidence.py:157-170`、`projects/rsna-knee/rsna_knee/training.py:44-47,70-74,118`、`tests/unit/test_regression_gate.py`
- Create: `tests/unit/test_e2e_access.py`、`tests/integration/test_access_receipts.py`
- Test: `tests/unit/backup/test_evidence_run.py`

**Interfaces:**
- Consumes: 全部前面的任務。
- Produces: `ROLES` 含 `"access_receipt"`（`"run_card"` 之後，tier 1）、`CARD_ROLES` 含它；`walk_run` 收每份收據的 `manifest.json` + `receipt.json`；門檻列 `wave 1b-1 (VCP-001, VCP-003)`。

- [ ] **Step 1: 備份層測試**——追加到 `tests/unit/backup/test_evidence_run.py` 檔尾（import 區加 `from vcp.data.access.access import DatasetAccess`、`from vcp.measure.provenance import attach_receipts`、`from vcp.measure.runs import save_run`、`from vcp.backup.schema import CARD_ROLES, ROLES, TIER_OF`）：

```python
def test_walk_run_collects_access_receipts(world):
    assert ROLES.index("access_receipt") == ROLES.index("run_card") + 1
    assert TIER_OF["access_receipt"] == 1 and "access_receipt" in CARD_ROLES
    with DatasetAccess.open(
        EVAL, "fixed-v1", subsets={"train"}, purpose="train", run_id="good",
        data_root=world.roots.data, configs_root=world.roots.configs,
    ) as access:
        list(access.iter("train"))
    rid = access.receipt_id
    card = attach_receipts(load_run(world.roots.data, "good"), [rid], data_root=world.roots.data)
    save_run(world.roots.data, card)
    col = _col(world)
    col.walk_run("good", "run:good")
    roles = _roles(col)
    assert roles["access_receipt"] == [
        f"artifacts/access_receipt/{rid}/manifest.json",
        f"artifacts/access_receipt/{rid}/receipt.json",
    ]
    by_key = {e.key: e for e in col.files_of()}
    entry = by_key[f"data/artifacts/access_receipt/{rid}/receipt.json"]
    assert entry.sha256 == card.access[0].receipt_sha256 and entry.tier == 1 and entry.present
    assert col.missing == [] and col.unlisted == []
```

Run: `uv run pytest tests/unit/backup/test_evidence_run.py -o addopts="" -q -k receipts` → FAIL（`ValueError: 'access_receipt' is not in tuple`）。改 `src/vcp/backup/schema.py`：`ROLES` 在 `"run_card",` 之後插入 `"access_receipt",`；`CARD_ROLES` 加 `"access_receipt"`。改 `src/vcp/backup/evidence.py`：import 區加 `from vcp.core.paths import artifact_dir`（併進既有 `vcp.core.paths` 那行）；`walk_run` 在 `self.add(rdir / "run.yaml", "run_card", conclusion)` 之後加

```python
        for ref in card.access:  # spec 8: the receipts are the evidence of what it trained on
            adir = artifact_dir(self.data_root, "access_receipt", ref.artifact_id)
            self.add(adir / "manifest.json", "access_receipt", conclusion)
            self.add(adir / "receipt.json", "access_receipt", conclusion, sha256=ref.receipt_sha256)
```

Run 同上 → PASS；再 `uv run pytest tests/unit/backup -o addopts="" -q` 全綠。

- [ ] **Step 2: RSNA 專案改 `with`**——`projects/rsna-knee/rsna_knee/training.py`：`training_context` 的最後三行改成

```python
    reader = MaterializedReader(name, MODE_DIR, plan_id=plan, subset=subset, verify=True)
    if tuple(c.name for c in reader.card.categories) != NAMES:
        reader.close()
        raise ValidationFailed("categories: expected official RSNA label order")
    return session, reader
```

`train()` 裡 `session, reader = training_context(...)` 之後、`if out.exists():` 之前加 `with reader:`，把從 `if out.exists():` 到 `return {…}` 的整段縮排進去（結尾的 `return` 也在 `with` 內：收據在 checkpoint 登記後 commit）；`targets(reader.dataset.by_id[sid])` 兩處改成 `targets(reader.sample(sid))`；`reader.dataset.card.samples_hash` 改成 `reader.card.samples_hash`。在 `src/vcp/train/reader.py` 的 `rows()` 之前加

```python
    def sample(self, sample_id: str) -> Sample:
        """The sample record without loading its arrays."""
        return self._samples[sample_id]
```

Run: `uv run pytest tests/unit/test_rsna_knee_glue.py tests/unit/train/test_reader.py -o addopts="" -q` → 全綠。

- [ ] **Step 3: 端到端** `tests/unit/test_e2e_access.py`

```python
"""Audit release-gate scenarios 1 and 2 through the CLI (spec 13): a training loop that reads
its subset through the reader under `vcp train run` leaves a receipt that grades the run; a
loop that also peeks at valA is caught -- `train run` warns, `measure` drops valA, `judge`
on valA is INVALID; a profile that requires receipts refuses a declared run and admits a
receipt-backed one; the sealed subset cannot be read without an unseal, id or no id."""

import json
import sys

import pytest
from typer.testing import CliRunner

from helpers import det_samples, make_card, noisy_predictions, perfect_predictions, write_images
from submit_fixtures import EVAL, STAMP, TEST, seed_eval_runs, seed_judgements, seed_test_runs
from vcp.cli import app
from vcp.core.errors import AccessDeniedError, SealedSubsetError
from vcp.core.paths import DatasetPaths
from vcp.data.access.access import DatasetAccess
from vcp.data.dataset import Dataset
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.predictions import write_predictions
from vcp.measure.provenance import attach_receipts
from vcp.measure.runs import load_run, save_run
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile

runner = CliRunner()

ACCESS_FAKE = """
from pathlib import Path
from vcp.train import MaterializedReader

with MaterializedReader("flow", "npy", plan_id="fixed-v1", subset="train") as reader:
    n = sum(1 for _ in reader)
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best-%d" % n)
"""

PEEK_FAKE = ACCESS_FAKE + """
from vcp.train import Session

with Session.current().access(subsets={"valA"}) as peek:
    list(peek.iter("valA"))
"""


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _run(*args):
    return runner.invoke(app, list(args))


def _train(work, run_id, script):
    return _run(
        "train", "run", "--run", run_id, "--dataset", "flow", "--plan", "fixed-v1",
        "--trained-on", "train", "--seed", "1", "--cwd", str(work),
        "--checkpoints", "weights/*.pt", "--final", "weights/best.pt",
        "--", sys.executable, script,
    )


def _ingest(tmp_path, ds, plan, run_id, subset, maker):
    src = tmp_path / f"{run_id}-{subset}.jsonl"
    write_predictions(src, maker(ds.subset(subset, plan), ds.card))
    return _run(
        "eval", "ingest", "--run", run_id, "--dataset", "flow", "--plan", "fixed-v1",
        "--subset", subset, "--format", "jsonl", "--src", str(src),
    )


def test_training_receipts_grade_measure_and_judge(roots, tmp_path):
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
    work = tmp_path / "work"
    work.mkdir()
    (work / "access.py").write_text(ACCESS_FAKE, encoding="utf-8")
    (work / "peek.py").write_text(PEEK_FAKE, encoding="utf-8")
    # 1. a clean train-only loop
    r = _train(work, "good", "access.py")
    v = _verdict(r.output)
    assert r.exit_code == 0, r.output
    assert "receipts=1" in v and "denied=0" in v and "provenance=receipt" in v
    assert "observed_beyond_trained_on" not in v
    # 2. a loop that also reads valA: the run exists, the VERDICT says what it did
    r = _train(work, "peek", "peek.py")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "receipts=2" in v
    assert "observed_beyond_trained_on=valA" in v
    for run_id, maker in (("good", perfect_predictions), ("peek", noisy_predictions)):
        for subset in ("valA", "valB"):
            r = _ingest(tmp_path, ds, plan, run_id, subset, maker)
            assert r.exit_code == 0, r.output
            assert "provenance=receipt" in _verdict(r.output)
    # 3. measure the baseline: the peeked subset is not a clean base any more
    r = _run("eval", "measure", "--run", "peek")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "subsets=valB" in v and "observed=train,valA" in v
    r = _run("eval", "measure", "--run", "peek", "--subsets", "valA")
    assert r.exit_code == 1 and "contaminated: subset 'valA'" in _verdict(r.output)
    # 4. the claims go down BEFORE the candidate is measured (create_prereg refuses otherwise);
    #    one on valA against the peeking baseline is INVALID, one on valB alone is judged
    for pid, subsets in (("p-valA", "valA,valB"), ("p-valB", "valB")):
        r = _run(
            "eval", "preregister", "--dataset", "flow", "--id", pid, "--claim", "good beats peek",
            "--component", "good", "--class", "model", "--baseline-run", "peek",
            "--candidate-run", "good", "--metric", "coco_map", "--subsets", subsets,
            "--min-bases", "1",
        )
        assert r.exit_code == 0, r.output
    r = _run("eval", "measure", "--run", "good")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "subsets=valA,valB" in v and "observed=train" in v
    r = _run("eval", "judge", "--dataset", "flow", "--prereg", "p-valA")
    v = _verdict(r.output)
    assert "verdict=INVALID" in v and "provenance=receipt" in v
    assert "reason: contaminated:peek/valA" in r.output
    r = _run("eval", "judge", "--dataset", "flow", "--prereg", "p-valB")
    v = _verdict(r.output)
    assert "verdict=" in v and "INVALID" not in v and "provenance=receipt" in v
    doc = json.loads(next(l for l in _run("eval", "status", "--dataset", "flow", "--json").stdout.splitlines() if l.startswith("{")))
    assert doc["fields"]["receipt_runs"] == 2
    # 5. sealed: with an id or without, no read without an unseal; the unseal is recorded
    holdout_id = sorted(plan.ids_in("holdout"))[0]
    with DatasetAccess.open("flow", "fixed-v1", subsets={"train"}, data_root=roots.data, configs_root=roots.configs) as access:
        with pytest.raises(AccessDeniedError):
            access.by_id(holdout_id)
        with pytest.raises(AccessDeniedError):
            access.iter("holdout")
    with pytest.raises(SealedSubsetError):
        DatasetAccess.open("flow", "fixed-v1", subsets={"holdout"}, data_root=roots.data, configs_root=roots.configs)
    with DatasetAccess.open("flow", "fixed-v1", subsets={"holdout"}, unseal_reason="final", data_root=roots.data, configs_root=roots.configs) as sealed:
        assert sealed.by_id(holdout_id).sample_id == holdout_id
    assert sealed.receipt.sealed_accessed
    assert len(paths.unseal_jsonl("fixed-v1").read_text(encoding="utf-8").splitlines()) == 1


def test_a_profile_can_require_receipts_at_the_gate(roots, tmp_path):
    from submit_fixtures import make_pair

    pair = make_pair(roots, tmp_path)
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(
        PlatformProfile(
            dataset=TEST, eval_dataset=EVAL, plan_id="fixed-v1", sealed_subset="holdout",
            platform="manual", board_rule="last", metric="accuracy", writer="scores_csv",
            require_provenance="receipt", created_at=STAMP,
        ),
        data_root=roots.data, configs_root=roots.configs,
    )
    seed_test_runs(pair)
    common = ["submit", "stage", "--dataset", TEST, "--eval-run", "good", "--test-run", "good.test"]
    r = _run(*common, "--id", "S1")
    assert r.exit_code == 1 and "provenance_required: run 'good' is declared" in _verdict(r.output)
    with DatasetAccess.open(
        EVAL, "fixed-v1", subsets={"train"}, purpose="train",
        data_root=roots.data, configs_root=roots.configs,
    ) as access:
        list(access.iter("train"))
    save_run(roots.data, attach_receipts(load_run(roots.data, "good"), [access.receipt_id], data_root=roots.data))
    r = _run(*common, "--id", "S1")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "provenance=receipt" in v and "admission=PASS" in v
    r = _run("submit", "status", "--dataset", TEST)
    assert "provenance: S1=receipt" in r.output
```

Run: `uv run pytest tests/unit/test_e2e_access.py -o addopts="" -q`。（`--json` 的 stdout 是 `{"cmd", "status", "fields", "result"}` 一行；`train run` 沒給 `--venv` 本來就 WARN `venv=inherited`，所以 `status=WARN` 兩個 run 都會有——真正的斷言是 `observed_beyond_trained_on=`。）

- [ ] **Step 4: 門檻列**——`tests/unit/test_regression_gate.py` 的 `GATE` 最後加

```python
    (
        "wave 1b-1 (VCP-001, VCP-003)",
        "access: rows outside the allowed subsets are never parsed; a denied read fails closed "
        "and is counted; the receipt is the accessor's, not the caller's; a run that read a "
        "subset loses it as a clean base in measure and judge; a profile can require receipts",
        {
            "tests/unit/data/test_access.py": [
                "test_train_only_access_never_parses_other_rows",
                "test_unauthorized_subsets_fail_closed_and_are_counted",
                "test_receipt_is_written_even_when_the_job_fails",
            ],
            "tests/unit/train/test_run.py": [
                "test_train_run_binds_the_childs_receipts_and_warns_on_observed_beyond",
            ],
            "tests/unit/measure/test_measure.py": ["test_observed_subsets_are_not_clean_bases"],
            "tests/unit/measure/test_prereg_judge.py": [
                "test_a_run_that_read_a_claimed_subset_makes_the_judgement_invalid",
            ],
            "tests/unit/submit/test_stage.py": [
                "test_stage_records_provenance_and_the_profile_can_require_it",
            ],
            "tests/unit/test_e2e_access.py": [
                "test_training_receipts_grade_measure_and_judge",
                "test_a_profile_can_require_receipts_at_the_gate",
            ],
        },
    ),
```

- [ ] **Step 5: 真資料唯讀測試** `tests/integration/test_access_receipts.py`

```python
"""A role-scoped access over the real dataset's metadata, run on a COPY of the card, the plan
and samples.jsonl under a temporary root: the real roots are read once and never written to
(receipts land under the temporary data root)."""

from __future__ import annotations

import shutil

import pytest

from conftest import load_real
from vcp.core.errors import AccessDeniedError
from vcp.core.hashing import sha256_text
from vcp.core.paths import DatasetPaths
from vcp.data.access.access import DatasetAccess
from vcp.data.split import load_plan

pytestmark = pytest.mark.realdata
NAME = "rsna-knee"
PLAN = "fixed-v1"


def _tree(root):
    if not root.is_dir():
        return None
    return sorted((p.relative_to(root).as_posix(), p.stat().st_size) for p in root.rglob("*") if p.is_file())


def test_train_only_access_over_a_copy_of_the_real_metadata(real_roots, tmp_path):
    load_real(NAME, real_roots)  # skips when the dataset is not imported
    src = DatasetPaths.resolve(NAME, data_root=real_roots.data, configs_root=real_roots.configs)
    if not src.plan_json(PLAN).is_file():
        pytest.skip(f"real plan {PLAN!r} absent")
    before = _tree(real_roots.data / "artifacts"), _tree(src.config_dir)
    dst = DatasetPaths.resolve(NAME, data_root=tmp_path / "data", configs_root=tmp_path / "configs")
    for a, b in ((src.card_yaml, dst.card_yaml), (src.samples_jsonl, dst.samples_jsonl), (src.plan_json(PLAN), dst.plan_json(PLAN))):
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(a, b)
    plan = load_plan(dst, PLAN)
    names = [s.name for s in plan.subsets]
    if "train" not in names or len(names) < 2:
        pytest.skip(f"real plan {PLAN!r} has no train subset to scope to (subsets: {names})")
    other = next(n for n in names if n != "train")
    with DatasetAccess.open(NAME, PLAN, subsets={"train"}, purpose="custom", data_root=dst.data_root, configs_root=dst.configs_root) as access:
        train = list(access.iter("train"))
        assert [s.sample_id for s in train] == sorted(plan.ids_in("train"))
        with pytest.raises(AccessDeniedError):
            access.ids(other)
    receipt = access.receipt
    assert receipt.accessed["train"].ids_sha256 == sha256_text("\n".join(sorted(plan.ids_in("train"))))
    assert receipt.denied == 1
    assert (_tree(real_roots.data / "artifacts"), _tree(src.config_dir)) == before
```

Run: `uv run pytest tests/integration/test_access_receipts.py -o addopts="" -q -m realdata`（本機有 `C:/vcp-data` 的 rsna-knee 就跑，否則 skip）。

- [ ] **Step 6: 全套與 commit**

Run: `uv run ruff format src/vcp/backup src/vcp/train/reader.py tests/unit/backup/test_evidence_run.py tests/unit/test_e2e_access.py tests/unit/test_regression_gate.py tests/integration/test_access_receipts.py && uv run pytest tests/unit -o addopts="" -q && uv run ruff check . && uv run ruff format --check .`
Expected: 全綠

```bash
git add src/vcp/backup/schema.py src/vcp/backup/evidence.py src/vcp/train/reader.py projects/rsna-knee/rsna_knee/training.py tests/unit/backup/test_evidence_run.py tests/unit/test_e2e_access.py tests/unit/test_regression_gate.py tests/integration/test_access_receipts.py
git commit -m "feat(backup,rsna,test): 證據圖收收據、RSNA 訓練走 with reader、端到端劇本、門檻列、真資料唯讀測試" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: 文件、spec §16、release 0.5.0

**Files:**
- Modify: `README.md`（資料層表的 `vcp data export` 列、量測層表的 `vcp eval ingest` / `measure` / `judge` 列、訓練層 `vcp train run` 列與 §「一次訓練到量測」之後、提交層 `vcp submit stage` 列與段落）、`CLAUDE.md` 與 `AGENTS.md`（「路徑」與「常用命令」）、`docs/handover/HANDOVER.md:23`、`docs/superpowers/specs/2026-09-12-vcp-access-receipts-design.md`（檔尾 §16）、`CHANGELOG.md`、`src/vcp/__init__.py`

- [ ] **Step 1: README**

`vcp data export` 列的「主要選項」欄尾加 `；每次匯出留一份 `access_receipt`（manifest 的 `receipt`、VERDICT `receipt=`）`。`vcp eval ingest` 列的選項加 `、--receipt ID（可重複；把 `vcp train run` 之外產生的收據掛上 run）`；`vcp eval measure` 列作用改成「護欄 → 每個乾淨 eval 子集 × 適用指標一列讀數；乾淨 = `trained_on ∪` 收據觀測到的子集都不含；`--subsets` 點到被讀過的子集 → `contaminated:`」；`vcp eval judge` 列作用尾加「；候選或基準讀過主張的子集 → `INVALID contaminated:<run>/<subset>`；判決記候選的 provenance」。`vcp train run` 列作用尾加「；子程序用 `MaterializedReader` / `Session.access` 留的收據結束時抄進 `run.yaml`（`receipts=` `denied=` `provenance=`），讀到 `trained_on` 以外的子集 → WARN `observed_beyond_trained_on=`」。在「一次訓練到量測」程式區塊之後加一段：

```markdown
### 收據與 provenance

訓練迴圈用 `with MaterializedReader(name, mode, plan_id=P, subset="train") as reader:`（或 `Session.current().access(subsets={"train"})`）讀資料：只有授權子集的列會被解析，關閉時存取器把它實際讀到的子集、ID 集合 sha、拒絕次數寫成 `artifacts/access_receipt/<run>-a<attempt>-<n>/receipt.json`——呼叫端只能加 `notes`。run 的等級由收據算出、不存：`receipt`（有 `purpose=train` 的有效收據）> `export`（無收據但從 `vcp data export` 目錄訓練）> `declared`（舊 run 或手填）；`vcp eval status` / `report`、`vcp submit status` 都印它。`submit.yaml` 的 `require_provenance: receipt|export|declared`（預設 `declared`）決定 `stage` / `final` 擋不擋候選；讀過 sealed 子集的候選一律 `observed_sealed:`。收據證明的是經 vcp 存取器的讀取；process 自己 `open()` 檔案不在證明範圍。
```

`vcp submit stage` 列作用尾加「；`Staged.provenance` 記候選等級，低於 `require_provenance` → `provenance_required:`」。

- [ ] **Step 2: CLAUDE.md 與 AGENTS.md**（兩檔同樣）——「路徑」最後加

```markdown
- `artifacts/access_receipt/<id>/receipt.json` 是存取器留下的收據（train 下 `<run>-a<attempt>-<n>`，其他 `<purpose>-<dataset>-<plan>-<stamp>-<nonce>`）：只有授權子集的列會被解析，未授權 → `denied:` 並計數；`run.yaml` / `train.yaml` 的 `access` 列出掛上的收據，等級 `receipt > export > declared` 讀取時算出。measure 的乾淨基底 = `trained_on ∪ 觀測`；judge 讀過主張子集 → `INVALID contaminated`；`submit.yaml` 的 `require_provenance` 決定 gate。
```

「常用命令」最後加

```markdown
- `uv run vcp eval ingest --run R … --receipt ID` 掛外部收據；訓練迴圈 `with MaterializedReader(…, plan_id=P, subset="train") as reader:` 或 `Session.current().access(subsets={"train"})` 才會有收據；`vcp eval status --dataset D` 看每個 run 的 provenance
```

`docs/handover/HANDOVER.md` 第 23 行（`- 版本：` 開頭那一整行）換成：

```markdown
- 版本：`0.5.0`（tag `v0.5.0`，2026-09-12）= 稽核 Wave 1b-1（角色範圍存取 `DatasetAccess`、`access_receipt` 產物、provenance 三級與四個強制點，VCP-001/003）；`0.4.0` = Wave 1a（不可變產物層 `vcp artifact` + `core/atomic.write_once`）；`0.3.0` = Wave 0（prereg SHA 綁定、foreign 狀態刷新、回歸門檻）；`0.2.0` 是第一個有 tag 的 release。規則與發版步驟在 `CHANGELOG.md` 表頭。`0.2.0` 之前 240 個 commit 都宣告 `0.1.0` 且無 tag——RSNA 早期產物裡的 `"vcp_version": "0.1.0"` 回推不到單一 commit；0.2.0 起產物記 `版本+g<commit>[.dirty]`。下一步是 Wave 1b-2（SourceAudit 與選取列存取器，VCP-002）、1c（程式碼快照與授權，VCP-004/006）；稽核文件副本在 `docs/audits/`。
```

- [ ] **Step 3: spec §16**——在 `docs/superpowers/specs/2026-09-12-vcp-access-receipts-design.md` 檔尾加

```markdown
## 16. 補充決定（實作期，Plan 9）

1. `read_receipt` 放 `data/access/receipt.py`（收據格式是資料層的），`measure/provenance.py` import 它。
2. `DatasetAccess.open` 多 `run_id=None`：無 binding 的收據（measure、stage）用它填 `run_id`；有 binding 時 binding 優先。
3. `ingest`、`fuse build/ablate`、`submit profile`、`submit verify` 維持 `Dataset.load`（各自要 eval / 全部 rows 或只是重渲染驗檔，不是訓練讀）；`stage` 的 eval 側 `load_card`、test 側走存取器（`purpose=submit`，`run_id` 填候選 eval run）；`judge` / `sigma` / `anchor` 沿用 `load_context`，`measure_run` 用 `load_card_context`。
4. `assert_run_matches(card, dataset_card)` 第二個參數改收 card。
5. unseal 留痕抽成 `data/dataset.append_unseal`，存取器與 `Dataset.subset` 共用。
6. `MaterializedReader` 建構時迭代授權子集；`reader.dataset` 移除，改 `card` / `access` / `sample(id)`；不在 run 下且無 plan / subset 維持 `Dataset.load`。
7. `train run` 用 `provenance()` 算等級，`RunResult` 多 `receipts / denied / provenance / observed_beyond / receipt_invalid`。
8. 融合 run 的 provenance 遞迴用 lazy import 避免 measure ↔ fuse 循環。
9. `FinalEntry.provenance`；`submit status` 只在 human / payload 印每筆等級；`eval status` 多 `receipt_runs=` `export_runs=` `declared_runs=`。
10. judge 的 contaminated 檢查在 measured-before-prereg 之前，INVALID 後不跑 bootstrap。
11. 索引時只 `json.loads` `sample_id` 那個字串，整行不解析；`iter()` 立刻授權（不是 generator）。
12. `data/access/__init__.py` 不 import 任何東西、`schema.py` 只 import core——避免 measure.schema ↔ access 的 import 環。
```

- [ ] **Step 4: 發版**——`src/vcp/__init__.py` 改 `__version__ = "0.5.0"`；`CHANGELOG.md` 在 `## [0.4.0]` 之前插入：

```markdown
## [0.5.0] - 2026-09-12

稽核 **Wave 1b-1**：角色範圍存取與收據——VCP-001（`Dataset.load()` 無法證明 train-only 存取）、VCP-003（access flags 是自我宣告）。MINOR 的理由：新產物 kind `access_receipt`、`run.yaml` / `train.yaml` 的 `access`、三本台帳與 `stage.json` 的 `provenance`、`submit.yaml` 的 `require_provenance`、新 VERDICT 欄位與字彙（`denied:`、`contaminated:`、`observed_sealed:`、`provenance_required:`）、`MaterializedReader.dataset` 移除。

### Added
- **`DatasetAccess`**（`vcp.data.access`）：card-only 載入 + 按 plan 角色授權的列讀取；open 時整檔 hash 身分並只 peek 行首 `sample_id` 建索引，未授權的列永不解析；未授權存取 `AccessDeniedError`（`denied:`）並計數；sealed 沿用 unseal 留痕。關閉時（含例外）存取器把 `AccessReceipt` 寫成 `artifacts/access_receipt/<id>/receipt.json`（v0.4.0 的 `ArtifactWriter`），呼叫端只能加 `notes`。
- **收據綁定**：`Session.access()` / `MaterializedReader`（現在是 context manager）在 `vcp train run` 下把收據登記進 `train.yaml`（`access` 事件），`train run` 結束抄進 `run.yaml`；`vcp eval ingest --receipt` 掛外部收據。
- **provenance**（`vcp.measure.provenance`）：`receipt > export > declared` 讀取時算出；`Reading` / `Judgement` / `Staged` / `FinalEntry` 記等級；`vcp eval status` / `report`、`vcp submit status` 印它。
- **強制點**：`measure` 的乾淨基底 = `trained_on ∪ 收據觀測`（點名被讀過的子集 → `contaminated:`）；`judge` 候選或基準讀過主張子集 → `INVALID contaminated:<run>/<subset>`；`submit stage` / `final` 的 `observed_sealed:` 與 `submit.yaml` `require_provenance`（預設 `declared`）；`train run` WARN `observed_beyond_trained_on=` / `receipt_invalid=`。
- 備份證據圖收 run 的收據（角色 `access_receipt`）；`vcp data export` 每次留收據並記進 manifest；回歸門檻多一列；真資料唯讀整合測試。

### Changed
- `Dataset.load_card`、`append_unseal`；`assert_run_matches` 收 card；`train run` 父程序不再解析 `samples.jsonl`。
- `MaterializedReader`：`reader.dataset` 移除（改 `card` / `access` / `sample()`），在 `VCP_RUN_ID` 下必須給 `plan_id` / `subset`。
- README、AGENTS.md / CLAUDE.md、交接文件；spec §16 補充決定。
```

然後：

```bash
uv sync --reinstall-package vcp
uv run pytest --cov=vcp
uv run ruff check . && uv run ruff format --check .
```

Expected: 全部通過、覆蓋率 ≥ 80%、`tests/unit/test_package.py` 三方一致。

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md AGENTS.md docs/handover/HANDOVER.md docs/superpowers/specs/2026-09-12-vcp-access-receipts-design.md CHANGELOG.md src/vcp/__init__.py
git commit -m "chore(release): v0.5.0——稽核 Wave 1b-1（角色範圍存取與收據）" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

發版第四步（fast-forward `main`、tag `v0.5.0`、push）由收尾流程在合併後執行（`gh pr create` 已可用）。
