# Run evidence and label sets (VCP-040 + VCP-042) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A run can declare what it read besides the dataset: label sets checked against the split plan (no eval / sealed leakage), and copied evidence files. The declaration is recorded in `run.yaml` / `train.yaml`, and backup, status and the provenance graph all see it.

**Architecture:**
- Two immutable artifact kinds on the existing artifact layer:
  - `label_set/<id>`, created by the new `vcp data labels` after checking every row against the plan.
  - `evidence/<run>-<name>-<sha12>`, a copy of any file.
- A run references them through `EvidenceRef` rows in a new `evidence` list on `RunCard` and `TrainRecord`, merged at the end of `train run` the way `access` receipts are.
- Three entry points attach: `vcp train run --evidence / --labels`, `Session.attach_evidence / attach_labels` inside the loop, and `vcp eval ingest --evidence / --labels`.
- Downstream, the backup walk, the backup consistency layer, `train status --verify`, `eval status` and the provenance graph read the list.

**Tech Stack:** Python 3.12, pydantic v2, typer, pytest, uv, ruff (line length 100).

**Spec:** `docs/superpowers/specs/2026-09-26-vcp-run-evidence-design.md` (commit `1c6e2a7`). Read it first; this plan argues from it.

## Global Constraints

- Time only through `vcp.core.time.utc_now()` / `stamp()`; ruff TID251 bans the rest.
- **VERDICTs**
  - Every CLI command ends with `VERDICT cmd=... status=OK|WARN|FAIL|ABORT ...`; exit codes 0 / 0 / 1 / 2.
  - `--json` sends the JSON to stdout and the VERDICT to stderr.
  - Commands never prompt.
- **`reason=` words** (exactly as the spec's §7): `labels_outside_subsets:`, `duplicate_id:`, `labels_on_sealed:`, `unsupported_format:`, `not_found:`, `not_a_file:`, `labels_mismatch:`, `evidence_conflict:`, `role_reserved:`; reuse `spec_mismatch:` from the artifact store.
- **New VERDICT fields**
  - `vcp data labels`: `id= dataset= plan= subsets= rows= matched= external= reused=`.
  - `train run` / `eval ingest` / `train status`: `evidence=` (the number of current references, one per name) and `labels=` (current label-set ids, comma-joined, or `dataset`).
  - `train run` only: `evidence_changed=` when non-empty (a WARN).
- `evidence` is **omitted from the dump when empty**, on both `RunCard` and `TrainRecord`, so older vcp still reads records that attached nothing.
- Every artifact goes through `vcp.artifact.writer.ArtifactWriter`; reuse through `vcp.artifact.store.reuse`.
- Tests never touch the real data root: use the `roots` fixture (it sets `VCP_DATA_ROOT` / `VCP_CONFIGS_ROOT`).
- Test data is synthetic only (no contest data, ids or paths).
- Files are UTF-8 with LF line endings. Never build a BOM or other special character with a backslash-u escape in source; use `chr(0xFEFF)`.
- **Git**
  - Commits are `type(scope): 繁中說明`.
  - Never `git add -A`, never a bare `git stash`.
  - No co-author trailer.
- `uv run ruff check . && uv run ruff format --check .` must pass after every task. Never `ruff format` markdown.
- All work happens in the worktree `.claude/worktrees/vcp-040`, branch `feat/vcp-040-run-evidence`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/vcp/data/evidence_ref.py` (new) | `EvidenceRef` model and the pure list rules (`add_ref`, `add_refs`, `current`, `merge_refs`, `labels_field`) |
| `src/vcp/data/labels.py` (new) | Label sets: id mapping, reading CSV / JSONL, classification against the plan, `create_label_set`, `load_label_set` |
| `src/vcp/data/evidence.py` (new) | Attaching: `RunScope`, `parse_evidence_args`, `check_evidence_file`, `attach_evidence`, `label_ref`, `source_sha`, `ref_intact`, `broken_refs` |
| `src/vcp/train/attach.py` (new) | `train run`'s preflight, attach-before-the-child, and end-of-run `moved` check |
| `src/vcp/train/records.py` | `append_evidence_event`, `bind_ref` |
| `src/vcp/train/schema.py` / `src/vcp/measure/schema.py` | `evidence` field + omit-when-empty serializer; `evidence` event |
| `src/vcp/train/session.py` | `Session.attach_evidence`, `Session.attach_labels` |
| `src/vcp/train/run.py`, `src/vcp/cli_train.py` | `--evidence` / `--labels`, VERDICT fields |
| `src/vcp/measure/ingest.py`, `src/vcp/cli_eval.py` | `ingest --evidence / --labels`, VERDICT fields; `eval status` labels |
| `src/vcp/train/status.py` | `evidence:<name>` in `--verify` drift |
| `src/vcp/measure/report.py` | `StatusResult.labels` |
| `src/vcp/cli.py` | `vcp data labels` |
| `src/vcp/backup/schema.py`, `src/vcp/backup/evidence.py`, `src/vcp/backup/verify.py` | roles `label_set` (tier 1) / `evidence` (tier 2), walk, consistency |
| `src/vcp/provenance/graph.py` | artifact → run `CONSUMED_BY` edges; a broken reference makes the run broken |
| `tests/helpers.py` | `seed_tiny`, `make_label_set` |
| tests | `tests/unit/data/test_evidence_ref.py`, `test_labels.py`, `test_evidence.py`, plus additions to the existing train / measure / backup / provenance tests and the new `tests/unit/test_e2e_evidence.py` |

---

### Task 1: `EvidenceRef`, the list rules, and the `evidence` field

**Files:**
- Create: `src/vcp/data/evidence_ref.py`
- Modify: `src/vcp/train/schema.py` (imports, `EVENTS`, `TrainRecord`)
- Modify: `src/vcp/measure/schema.py` (imports, `RunCard`)
- Test: `tests/unit/data/test_evidence_ref.py`

**Interfaces:**
- Produces:
  - `EvidenceKind = Literal["evidence", "label_set"]`, `Binding = Literal["cli", "session", "manual"]`, `LABELS_ROLE = "labels"`.
  - `class EvidenceRef(name, role, kind, artifact_id, manifest_sha256, attempt: int | None, attached_at, binding)`.
  - List rules:
    - `add_ref(refs, ref) -> list[EvidenceRef]`
    - `add_refs(refs, new) -> list[EvidenceRef]`
    - `current(refs) -> list[EvidenceRef]`
    - `merge_refs(first, second) -> list[EvidenceRef]`
    - `labels_field(refs) -> str`
  - `TrainRecord.evidence` and `RunCard.evidence`, both `list[EvidenceRef]` with default `[]`.
  - `"evidence"` in `vcp.train.schema.EVENTS`.

- [ ] **Step 1: Write the failing test** — `tests/unit/data/test_evidence_ref.py`

```python
import pytest
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.data.evidence_ref import (
    EvidenceRef,
    add_ref,
    add_refs,
    current,
    labels_field,
    merge_refs,
)
from vcp.measure.schema import RunCard, RunSource
from vcp.train.schema import EVENTS, TrainRecord

STAMP = "2026-09-26T00:00:00.000Z"


def _ref(name="teacher", sha="a", *, kind="evidence", role=None, artifact_id=None):
    return EvidenceRef(
        name=name,
        role=role or ("labels" if kind == "label_set" else name),
        kind=kind,
        artifact_id=artifact_id or f"r1-{name}-{sha * 12}",
        manifest_sha256=sha * 64,
        attempt=1,
        attached_at=STAMP,
        binding="cli",
    )


def test_the_same_name_and_bytes_twice_is_one_row():
    refs = add_ref([], _ref())
    assert add_ref(refs, _ref()) == refs


def test_new_bytes_under_a_name_become_its_current_row():
    refs = add_refs([], [_ref(sha="a"), _ref(sha="b")])
    assert [r.manifest_sha256[0] for r in refs] == ["a", "b"]
    assert [r.manifest_sha256[0] for r in current(refs)] == ["b"]


def test_a_name_cannot_change_its_role_or_kind():
    refs = add_ref([], _ref())
    with pytest.raises(ValidationFailed, match="evidence_conflict") as ei:
        add_ref(refs, _ref(role="corpus", sha="b"))
    assert ei.value.fields == {"evidence": "teacher"}


def test_merge_keeps_the_first_list_then_what_it_lacks():
    a, b, c = _ref("a"), _ref("b"), _ref("c")
    assert merge_refs([a, b], [b, c]) == [a, b, c]


def test_labels_field_names_the_current_label_sets():
    assert labels_field([]) == "dataset"
    assert labels_field([_ref()]) == "dataset"
    refs = [_ref(), _ref("pseudo-v1", kind="label_set", artifact_id="pseudo-v1")]
    assert labels_field(refs) == "pseudo-v1"


def test_names_and_roles_must_be_path_safe():
    with pytest.raises(ValidationError):
        _ref(name="bad name")


def _card(**over):
    base = dict(
        run_id="r1",
        dataset="tiny",
        samples_hash="f" * 64,
        plan_id="fixed-v1",
        trained_on=["train"],
        source=RunSource(),
        created_at=STAMP,
    )
    return RunCard(**{**base, **over})


def _record(**over):
    base = dict(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="work",
        command=["python"],
    )
    return TrainRecord(**{**base, **over})


@pytest.mark.parametrize("make", [_card, _record])
def test_an_empty_evidence_list_is_not_written(make):
    """An older vcp (extra="forbid") still reads a card or record that attached nothing."""
    assert "evidence" not in make().model_dump(mode="json")
    assert '"evidence"' not in make().model_dump_json()
    full = make(evidence=[_ref()])
    assert full.model_dump(mode="json")["evidence"][0]["name"] == "teacher"
    assert type(full).model_validate(full.model_dump(mode="json")) == full


def test_evidence_is_a_training_event():
    assert "evidence" in EVENTS
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest -o addopts="" -q tests/unit/data/test_evidence_ref.py`
Expected: FAIL (`ModuleNotFoundError: No module named 'vcp.data.evidence_ref'`).

- [ ] **Step 3: Create `src/vcp/data/evidence_ref.py`**

```python
"""``EvidenceRef``: a run's reference to an immutable artifact it read -- a label set or a
copied evidence file (spec 2026-09-26 §3.3). The model and the list rules only; making the
artifacts is ``vcp.data.evidence``'s job, writing the list is the caller's."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from vcp.core.errors import ValidationFailed
from vcp.core.paths import validate_name

EvidenceKind = Literal["evidence", "label_set"]
Binding = Literal["cli", "session", "manual"]
LABELS_ROLE = "labels"


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    role: str
    kind: EvidenceKind
    artifact_id: str
    manifest_sha256: str
    attempt: int | None = None
    attached_at: str
    binding: Binding

    @field_validator("name", "role", "artifact_id")
    @classmethod
    def _path_safe(cls, v: str) -> str:
        try:
            validate_name(v)
        except ValidationFailed as e:
            raise ValueError(str(e)) from e
        return v


def add_ref(refs: list[EvidenceRef], ref: EvidenceRef) -> list[EvidenceRef]:
    """The list only grows (spec §3.3): the same name and manifest again is no new row, the same
    name with another manifest is a new row that becomes current, and the same name under
    another role or kind is ``evidence_conflict:``."""
    same = [r for r in refs if r.name == ref.name]
    if any(r.role != ref.role or r.kind != ref.kind for r in same):
        raise ValidationFailed(
            f"evidence_conflict: {ref.name!r} is attached as {same[-1].kind}/{same[-1].role}, "
            f"not {ref.kind}/{ref.role}",
            fields={"evidence": ref.name},
        )
    if same and same[-1].manifest_sha256 == ref.manifest_sha256:
        return list(refs)
    return [*refs, ref]


def add_refs(refs: list[EvidenceRef], new: list[EvidenceRef]) -> list[EvidenceRef]:
    out = list(refs)
    for ref in new:
        out = add_ref(out, ref)
    return out


def current(refs: list[EvidenceRef]) -> list[EvidenceRef]:
    """The newest row of every name, names in the order they were first attached."""
    newest: dict[str, EvidenceRef] = {}
    for r in refs:
        newest[r.name] = r
    return list(newest.values())


def merge_refs(first: list[EvidenceRef], second: list[EvidenceRef]) -> list[EvidenceRef]:
    """``first``'s rows, then those of ``second`` it lacks by (kind, artifact_id): how
    ``train.yaml``'s list is merged into ``run.yaml``'s, as access receipts are."""
    seen = {(r.kind, r.artifact_id) for r in first}
    return [*first, *(r for r in second if (r.kind, r.artifact_id) not in seen)]


def labels_field(refs: list[EvidenceRef]) -> str:
    """``dataset`` when no label set is attached, else the current label-set ids."""
    ids = [r.artifact_id for r in current(refs) if r.kind == "label_set"]
    return ",".join(ids) if ids else "dataset"
```

- [ ] **Step 4: Add the field to `TrainRecord`** in `src/vcp/train/schema.py`

Change the imports and `EVENTS`:

```python
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

from vcp.data.access.schema import AccessRef
from vcp.data.evidence_ref import EvidenceRef

AttemptStatus = Literal["running", "finished", "failed", "interrupted"]
CheckpointSource = Literal["glob", "session"]
UploadKind = Literal["rclone", "local"]
EVENTS = ("started", "env", "checkpoint", "uploaded", "finished", "note", "access", "evidence")
```

In `class TrainRecord`, add after `access: list[AccessRef] = Field(default_factory=list)`:

```python
    evidence: list[EvidenceRef] = Field(default_factory=list)
```

and at the end of the class body (after `notes: str = ""`):

```python
    @model_serializer(mode="wrap")
    def _omit_empty_evidence(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """A record that attached nothing is written exactly as before 0.11.0, so an older vcp
        still reads it (spec 2026-09-26 §3.3)."""
        data: dict[str, Any] = handler(self)
        if not self.evidence:
            data.pop("evidence", None)
        return data
```

- [ ] **Step 5: Add the field to `RunCard`** in `src/vcp/measure/schema.py`

Extend the pydantic import with `SerializerFunctionWrapHandler, model_serializer`, and add after `from vcp.data.access.schema import AccessRef, Grade`:

```python
from vcp.data.evidence_ref import EvidenceRef
```

In `class RunCard`, after `access: list[AccessRef] = Field(default_factory=list)`, add:

```python
    evidence: list[EvidenceRef] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _omit_empty_evidence(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """A card that attached nothing is written exactly as before 0.11.0, so an older vcp
        still reads it (spec 2026-09-26 §3.3)."""
        data: dict[str, Any] = handler(self)
        if not self.evidence:
            data.pop("evidence", None)
        return data
```

(`Any` is already imported in `measure/schema.py`.)

- [ ] **Step 6: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/data/test_evidence_ref.py tests/unit/train tests/unit/measure`
Expected: all PASS. The existing run-card / train-record tests must stay green, because no dump changed for records without evidence.

- [ ] **Step 7: Commit**

```bash
git add src/vcp/data/evidence_ref.py src/vcp/train/schema.py src/vcp/measure/schema.py tests/unit/data/test_evidence_ref.py
git commit -m "feat(data): run 的證據參照 EvidenceRef、清單規則與空清單不寫出"
```

---

### Task 2: Label sets (`vcp.data.labels`) and the shared test helpers

**Files:**
- Create: `src/vcp/data/labels.py`
- Modify: `tests/helpers.py` (add `seed_tiny`, `make_label_set`)
- Test: `tests/unit/data/test_labels.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - Constants and models:
    - `LABEL_SET_KIND = "label_set"`, `SUMMARY = "label_set.json"`
    - `LabelSetSummary(dataset, samples_hash, plan_id, subsets, id_field, id_col, format, rows, matched: dict[str, int], external)`
    - `LabelSetSpec(name, plan_id, subsets, file, id_field, label_set_id, id_col="id", notes="", data_root=None, configs_root=None)`
    - `LabelSetResult(summary, reused)`
  - Functions:
    - `sample_ids_by_key(samples, id_field) -> dict[str, str]`
    - `read_ids(path, id_col) -> tuple[str, list[str]]`
    - `create_label_set(spec) -> LabelSetResult`
    - `load_label_set(data_root, label_set_id) -> LabelSetSummary`
  - Test helpers:
    - `seed_tiny(roots, samples=None) -> tuple[Dataset, SplitPlan]`
    - `make_label_set(roots, tmp_path, plan, *, label_set_id="pseudo-v1", dataset="tiny", subsets=("train",)) -> LabelSetSummary`

- [ ] **Step 1: Add the helpers** to `tests/helpers.py`

Add to the imports:

```python
from vcp.data.labels import LabelSetSpec, LabelSetSummary, create_label_set
```

Append at the end of the file:

```python
def seed_tiny(roots: Any, samples: list[Sample] | None = None) -> tuple[Dataset, SplitPlan]:
    """det dataset ``tiny`` + plan ``fixed-v1`` (train / valA / valB / holdout), no runs."""
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(
        make_card("det", image_root="raw/tiny"), samples or det_samples(40, seed=0)
    )
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan


def make_label_set(
    roots: Any,
    tmp_path: Path,
    plan: SplitPlan,
    *,
    label_set_id: str = "pseudo-v1",
    dataset: str = "tiny",
    subsets: tuple[str, ...] = ("train",),
) -> LabelSetSummary:
    """A label set over every sample of ``subsets`` (keyed by sample id) plus one external row."""
    ids = sorted(sid for s in subsets for sid in plan.ids_in(s))
    f = tmp_path / f"{label_set_id}.csv"
    f.write_text("id,y\n" + "".join(f"{i},1\n" for i in [*ids, "ext-1"]), encoding="utf-8")
    spec = LabelSetSpec(
        name=dataset,
        plan_id=plan.plan_id,
        subsets=list(subsets),
        file=f,
        id_field="sample_id",
        label_set_id=label_set_id,
        data_root=roots.data,
        configs_root=roots.configs,
    )
    return create_label_set(spec).summary
```

- [ ] **Step 2: Write the failing tests** — `tests/unit/data/test_labels.py`

```python
import json

import pytest

from helpers import det_samples, seed_tiny
from vcp.core.errors import IntegrityError, PlanMismatchError, ValidationFailed
from vcp.core.paths import artifact_dir
from vcp.data.labels import LabelSetSpec, create_label_set, load_label_set, sample_ids_by_key
from vcp.data.schema import View


def _two_views(samples):
    """Every sample gets a lateral view and a case number in its meta."""
    return [
        s.model_copy(
            update={
                "views": [*s.views, View(path=f"{s.sample_id}_lat.jpg", width=8, height=8)],
                "meta": {"case": 1000 + i},
            }
        )
        for i, s in enumerate(samples)
    ]


def _csv(path, ids, *, col="id", bom=False):
    text = f"{col},y\n" + "".join(f"{i},1\n" for i in ids)
    path.write_bytes(((chr(0xFEFF) if bom else "") + text).encode("utf-8"))
    return path


def _spec(roots, file, **over):
    base = dict(
        name="tiny",
        plan_id="fixed-v1",
        subsets=["train"],
        file=file,
        id_field="sample_id",
        label_set_id="pseudo-v1",
        data_root=roots.data,
        configs_root=roots.configs,
    )
    return LabelSetSpec(**{**base, **over})


def test_a_label_set_of_train_samples_is_kept_as_an_artifact(roots, tmp_path):
    _, plan = seed_tiny(roots)
    train = sorted(plan.ids_in("train"))
    f = _csv(tmp_path / "pseudo.csv", [*train, "ext-1", "ext-2"])
    res = create_label_set(_spec(roots, f))
    assert not res.reused
    assert res.summary.rows == len(train) + 2 and res.summary.external == 2
    assert res.summary.matched == {"train": len(train)} and res.summary.subsets == ["train"]
    d = artifact_dir(roots.data, "label_set", "pseudo-v1")
    assert (d / "labels.csv").read_bytes() == f.read_bytes()
    assert load_label_set(roots.data, "pseudo-v1") == res.summary
    again = create_label_set(_spec(roots, f))
    assert again.reused and again.summary == res.summary


def test_labels_on_eval_or_sealed_samples_are_refused(roots, tmp_path):
    _, plan = seed_tiny(roots)
    ids = [
        *sorted(plan.ids_in("train"))[:3],
        *sorted(plan.ids_in("valA"))[:2],
        sorted(plan.ids_in("holdout"))[0],
    ]
    with pytest.raises(ValidationFailed, match="labels_outside_subsets") as ei:
        create_label_set(_spec(roots, _csv(tmp_path / "leak.csv", ids)))
    assert "2 in valA (eval)" in str(ei.value) and "1 in holdout (sealed)" in str(ei.value)
    assert ei.value.fields == {"outside_holdout": 1, "outside_valA": 2}
    assert not artifact_dir(roots.data, "label_set", "pseudo-v1").exists()


def test_a_sealed_subset_cannot_be_labelled_and_an_unknown_one_is_a_plan_mismatch(
    roots, tmp_path
):
    _, plan = seed_tiny(roots)
    f = _csv(tmp_path / "h.csv", sorted(plan.ids_in("holdout")))
    with pytest.raises(ValidationFailed, match="labels_on_sealed"):
        create_label_set(_spec(roots, f, subsets=["holdout"]))
    with pytest.raises(PlanMismatchError):
        create_label_set(_spec(roots, f, subsets=["nope"]))


def test_a_row_given_twice_is_refused(roots, tmp_path):
    _, plan = seed_tiny(roots)
    one = sorted(plan.ids_in("train"))[0]
    with pytest.raises(ValidationFailed, match="duplicate_id"):
        create_label_set(_spec(roots, _csv(tmp_path / "d.csv", [one, one])))


def test_an_id_field_that_cannot_tell_samples_apart_is_refused():
    same = [
        s.model_copy(update={"views": [View(path="same.jpg", width=8, height=8)]})
        for s in det_samples(2, seed=0)
    ]
    with pytest.raises(ValidationFailed, match="duplicate_id"):
        sample_ids_by_key(same, "view_stem")
    with pytest.raises(ValidationFailed, match="id_field"):
        sample_ids_by_key(same, "filename")


@pytest.mark.parametrize(
    ("id_field", "key"),
    [
        ("sample_id", lambda s, i: s.sample_id),
        ("view_path", lambda s, i: f"{s.sample_id}_lat.jpg"),
        ("view_stem", lambda s, i: f"{s.sample_id}_lat"),
        ("meta.case", lambda s, i: str(1000 + i)),
    ],
)
def test_every_id_field_finds_its_sample(roots, tmp_path, id_field, key):
    samples = _two_views(det_samples(40, seed=0))
    _, plan = seed_tiny(roots, samples)
    train = plan.ids_in("train")
    ids = [key(s, i) for i, s in enumerate(samples) if s.sample_id in train]
    res = create_label_set(_spec(roots, _csv(tmp_path / "l.csv", ids), id_field=id_field))
    assert res.summary.matched == {"train": len(train)} and res.summary.external == 0


def test_csv_with_a_bom_and_its_own_id_column(roots, tmp_path):
    _, plan = seed_tiny(roots)
    train = sorted(plan.ids_in("train"))
    f = _csv(tmp_path / "bom.csv", train, col="study", bom=True)
    assert create_label_set(_spec(roots, f, id_col="study")).summary.matched == {
        "train": len(train)
    }
    with pytest.raises(ValidationFailed, match="not_found: column"):
        create_label_set(_spec(roots, f, label_set_id="x1"))


def test_jsonl_ids_may_be_numbers(roots, tmp_path):
    samples = _two_views(det_samples(40, seed=0))
    _, plan = seed_tiny(roots, samples)
    train = plan.ids_in("train")
    f = tmp_path / "soft.jsonl"
    f.write_text(
        "".join(
            json.dumps({"case": 1000 + i, "p": 0.5}) + "\n"
            for i, s in enumerate(samples)
            if s.sample_id in train
        ),
        encoding="utf-8",
    )
    res = create_label_set(_spec(roots, f, id_field="meta.case", id_col="case"))
    assert res.summary.format == "jsonl" and res.summary.matched == {"train": len(train)}


def test_unsupported_or_missing_files(roots, tmp_path):
    seed_tiny(roots)
    txt = tmp_path / "l.txt"
    txt.write_text("id\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="unsupported_format"):
        create_label_set(_spec(roots, txt))
    with pytest.raises(ValidationFailed, match="not_found"):
        create_label_set(_spec(roots, tmp_path / "gone.csv"))


def test_the_same_id_with_other_labels_is_a_spec_mismatch(roots, tmp_path):
    _, plan = seed_tiny(roots)
    train = sorted(plan.ids_in("train"))
    create_label_set(_spec(roots, _csv(tmp_path / "a.csv", train)))
    with pytest.raises(IntegrityError, match="spec_mismatch"):
        create_label_set(_spec(roots, _csv(tmp_path / "b.csv", train[:-1])))


def test_a_tampered_or_absent_label_set_does_not_load(roots, tmp_path):
    _, plan = seed_tiny(roots)
    create_label_set(_spec(roots, _csv(tmp_path / "a.csv", sorted(plan.ids_in("train")))))
    labels = artifact_dir(roots.data, "label_set", "pseudo-v1") / "labels.csv"
    labels.write_text("id\n", encoding="utf-8")
    with pytest.raises(IntegrityError, match="mismatch"):
        load_label_set(roots.data, "pseudo-v1")
    with pytest.raises(ValidationFailed, match="not_found"):
        load_label_set(roots.data, "nope")
```

- [ ] **Step 3: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/data/test_labels.py`
Expected: FAIL at import (`No module named 'vcp.data.labels'`; `tests/helpers.py` imports it too, so every test module that imports helpers fails until Step 4).

- [ ] **Step 4: Create `src/vcp/data/labels.py`**

```python
"""``vcp data labels`` (spec 2026-09-26 §3.1, §4.1): a training label file checked against the
split plan -- every row labels a sample of the subsets it is for, or lies outside the dataset
altogether -- and kept as an immutable ``label_set`` artifact that runs attach.

The check reads the whole dataset the way ``vcp data audit`` does: in vcp's own process, through
no accessor, with no receipt and no unseal. It needs every sample's views and meta to map ids,
never a label."""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, artifact_dir, validate_name
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample
from vcp.data.split import SplitPlan, assert_plan_matches, load_plan

LABEL_SET_KIND = "label_set"
SUMMARY = "label_set.json"
FORMATS = {".csv": "csv", ".jsonl": "jsonl"}
ID_FIELDS = ("sample_id", "view_path", "view_stem")  # and meta.<key>
SHOWN_IDS = 5


class LabelSetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    samples_hash: str
    plan_id: str
    subsets: list[str]
    id_field: str
    id_col: str
    format: str
    rows: int
    matched: dict[str, int]
    external: int


@dataclass(frozen=True)
class LabelSetSpec:
    name: str
    plan_id: str
    subsets: list[str]
    file: Path
    id_field: str
    label_set_id: str
    id_col: str = "id"
    notes: str = ""
    data_root: Path | None = None
    configs_root: Path | None = None


@dataclass(frozen=True)
class LabelSetResult:
    summary: LabelSetSummary
    reused: bool


def _keys(sample: Sample, id_field: str) -> list[str]:
    if id_field == "sample_id":
        return [sample.sample_id]
    if id_field == "view_path":
        return [v.path for v in sample.views]
    if id_field == "view_stem":
        return [Path(v.path).stem for v in sample.views]
    key = id_field.removeprefix("meta.")
    raw = sample.meta.get(key)
    return [] if raw is None else [str(raw)]


def _check_id_field(id_field: str) -> None:
    if id_field in ID_FIELDS or (id_field.startswith("meta.") and len(id_field) > len("meta.")):
        return
    raise ValidationFailed(
        f"id_field: {id_field!r} is not one of {list(ID_FIELDS)} or meta.<key>",
        fields={"id_field": id_field},
    )


def sample_ids_by_key(samples: list[Sample], id_field: str) -> dict[str, str]:
    """external id -> sample_id; one id behind two samples is ``duplicate_id:`` (the field
    cannot tell them apart). Every view of a multi-view sample contributes its own id."""
    _check_id_field(id_field)
    out: dict[str, str] = {}
    for s in samples:
        for key in _keys(s, id_field):
            other = out.get(key)
            if other is not None and other != s.sample_id:
                raise ValidationFailed(
                    f"duplicate_id: {id_field} {key!r} names samples {other!r} and "
                    f"{s.sample_id!r}",
                    fields={"id": key},
                )
            out[key] = s.sample_id
    return out


def _csv_ids(path: Path, id_col: str) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or id_col not in reader.fieldnames:
            raise ValidationFailed(
                f"not_found: column {id_col!r} in {path.name} (has {reader.fieldnames or []})",
                fields={"column": id_col},
            )
        return [str(row[id_col]).strip() for row in reader]


def _jsonl_ids(path: Path, id_col: str) -> list[str]:
    out: list[str] = []
    with path.open("r", encoding="utf-8-sig") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValidationFailed(
                    f"bad label row: {e}", location=f"{path.name}:{lineno}"
                ) from e
            if not isinstance(row, dict) or id_col not in row:
                raise ValidationFailed(
                    f"not_found: column {id_col!r} in {path.name}:{lineno}",
                    fields={"column": id_col},
                )
            out.append(str(row[id_col]).strip())
    return out


def read_ids(path: Path, id_col: str) -> tuple[str, list[str]]:
    """``(format, ids)`` of a label file, ids as stripped strings in file order."""
    if not path.is_file():
        raise ValidationFailed(f"not_found: {path}", fields={"file": str(path)})
    fmt = FORMATS.get(path.suffix.lower())
    if fmt is None:
        raise ValidationFailed(
            f"unsupported_format: {path.name} (want one of {sorted(FORMATS)})",
            fields={"file": str(path)},
        )
    return fmt, (_csv_ids(path, id_col) if fmt == "csv" else _jsonl_ids(path, id_col))


def _classify(
    ids: list[str], by_key: dict[str, str], plan: SplitPlan, subsets: list[str]
) -> tuple[dict[str, int], int]:
    """``(matched per allowed subset, external rows)``; a row on any other dataset sample is
    ``labels_outside_subsets:`` naming counts and a few sample ids, never label content."""
    twice = sorted(i for i, n in Counter(ids).items() if n > 1)
    if twice:
        raise ValidationFailed(
            f"duplicate_id: {len(twice)} ids appear more than once, e.g. {twice[:SHOWN_IDS]}",
            fields={"id": twice[0]},
        )
    matched = {s: 0 for s in subsets}
    outside: dict[str, list[str]] = {}
    external = 0
    for key in ids:
        sid = by_key.get(key)
        if sid is None:
            external += 1
            continue
        where = plan.assignment.get(sid, "unassigned")
        if where in matched:
            matched[where] += 1
        else:
            outside.setdefault(where, []).append(sid)
    if outside:
        roles = {s.name: s.role for s in plan.subsets}
        parts = [f"{len(v)} in {k} ({roles.get(k, 'unassigned')})" for k, v in sorted(outside.items())]
        shown = sorted(sid for v in outside.values() for sid in v)[:SHOWN_IDS]
        raise ValidationFailed(
            f"labels_outside_subsets: {', '.join(parts)}; e.g. {shown}",
            fields={f"outside_{k}": len(v) for k, v in sorted(outside.items())},
        )
    return matched, external


def _subsets(plan: SplitPlan, names: list[str]) -> list[str]:
    subsets = sorted(set(names))
    if not subsets:
        raise ValidationFailed("--subset is required: the subsets these labels are for")
    for name in subsets:
        if plan.subset(name).role == "sealed":  # PlanMismatchError for an unknown subset
            raise ValidationFailed(
                f"labels_on_sealed: {name!r} is the sealed subset; no training label set may "
                "cover it",
                fields={"subset": name},
            )
    return subsets


def create_label_set(spec: LabelSetSpec) -> LabelSetResult:
    validate_name(spec.label_set_id)
    paths = DatasetPaths.resolve(spec.name, data_root=spec.data_root, configs_root=spec.configs_root)
    dataset = Dataset.load(spec.name, data_root=spec.data_root, configs_root=spec.configs_root)
    plan = load_plan(paths, spec.plan_id)
    assert_plan_matches(plan, dataset.card)
    subsets = _subsets(plan, spec.subsets)
    fmt, ids = read_ids(spec.file, spec.id_col)
    by_key = sample_ids_by_key(dataset.samples, spec.id_field)
    matched, external = _classify(ids, by_key, plan, subsets)
    summary = LabelSetSummary(
        dataset=spec.name,
        samples_hash=dataset.card.samples_hash,
        plan_id=spec.plan_id,
        subsets=subsets,
        id_field=spec.id_field,
        id_col=spec.id_col,
        format=fmt,
        rows=len(ids),
        matched=matched,
        external=external,
    )
    art = ArtifactSpec(
        kind=LABEL_SET_KIND,
        id=spec.label_set_id,
        dataset=spec.name,
        plan_id=spec.plan_id,
        params={
            "samples_hash": dataset.card.samples_hash,
            "subsets": ",".join(subsets),
            "id_field": spec.id_field,
            "id_col": spec.id_col,
            "format": fmt,
        },
        inputs=[InputRef(name="labels", sha256=sha256_file(spec.file))],
        notes=spec.notes,
    )
    if store.reuse(art, paths.data_root, check_files=True) is not None:
        return LabelSetResult(load_label_set(paths.data_root, spec.label_set_id), reused=True)
    with ArtifactWriter.create(art, data_root=paths.data_root) as writer:
        writer.add_file(f"labels.{fmt}", spec.file)
        writer.write_json(SUMMARY, summary.model_dump(mode="json"))
        writer.commit()
    return LabelSetResult(summary, reused=False)


def load_label_set(data_root: Path, label_set_id: str) -> LabelSetSummary:
    """A committed label set that still verifies (spec §4.2); ``not_found:`` / ``partial:`` /
    ``mismatch:`` otherwise."""
    res = store.verify(data_root, LABEL_SET_KIND, label_set_id)  # not_found / partial raise
    if res.failed:
        raise IntegrityError(
            f"mismatch: label_set/{label_set_id} no longer matches its manifest "
            f"(mismatch={len(res.mismatch)} missing={len(res.missing)} extra={len(res.extra)})",
            fields={"label_set": label_set_id},
        )
    path = artifact_dir(data_root, LABEL_SET_KIND, label_set_id) / SUMMARY
    return LabelSetSummary.model_validate_json(path.read_text(encoding="utf-8"))
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/data/test_labels.py`
Expected: all PASS.

Then check that nothing else broke through `tests/helpers.py`:

Run: `uv run pytest -o addopts="" -q tests/unit/data tests/unit/measure`
Expected: all PASS.

- [ ] **Step 6: Lint, then commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/data/labels.py tests/helpers.py tests/unit/data/test_labels.py
git commit -m "feat(data): 標籤集 label_set：對切分 plan 驗證、擋落在其他子集的列"
```

---

### Task 3: `vcp data labels`

**Files:**
- Modify: `src/vcp/cli.py` (import, new command after `export_cmd`)
- Test: `tests/unit/data/test_labels.py` (append)

**Interfaces:**
- Consumes: `LabelSetSpec`, `create_label_set` (Task 2).
- Produces: the CLI command `vcp data labels`; VERDICT `cmd=labels`.

- [ ] **Step 1: Append the failing CLI test** to `tests/unit/data/test_labels.py`

```python
from typer.testing import CliRunner

from vcp.cli import app

runner = CliRunner()


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def test_cli_labels(roots, tmp_path):
    _, plan = seed_tiny(roots)
    train = sorted(plan.ids_in("train"))
    f = _csv(tmp_path / "a.csv", [*train, "ext"])
    base = ["data", "labels", "--name", "tiny", "--plan", "fixed-v1", "--subset", "train"]
    base += ["--id-field", "sample_id"]
    ok = runner.invoke(app, [*base, "--file", str(f), "--id", "pseudo-v1"])
    assert ok.exit_code == 0, ok.output
    v = _verdict(ok.output)
    assert v.startswith("VERDICT cmd=labels status=OK")
    for part in (f"rows={len(train) + 1}", f"matched={len(train)}", "external=1"):
        assert part in v
    assert "reused=false" in v and "subsets=train" in v and "id=pseudo-v1" in v
    leak = _csv(tmp_path / "b.csv", sorted(plan.ids_in("valA")))
    bad = runner.invoke(app, [*base, "--file", str(leak), "--id", "leak-v1"])
    assert bad.exit_code == 1 and "status=FAIL" in _verdict(bad.output)
    assert "labels_outside_subsets" in _verdict(bad.output) and "id=leak-v1" in _verdict(bad.output)
    again = runner.invoke(app, [*base, "--file", str(f), "--id", "pseudo-v1", "--json"])
    assert again.exit_code == 0 and json.loads(again.stdout)["fields"]["reused"] is True
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest -o addopts="" -q tests/unit/data/test_labels.py::test_cli_labels`
Expected: FAIL (`No such command 'labels'`, exit code 2).

- [ ] **Step 3: Add the command** to `src/vcp/cli.py`

Import next to the other data imports:

```python
from vcp.data.labels import LabelSetSpec, create_label_set
```

Insert after `export_cmd` (right before `@data_app.command("audit")`):

```python
@data_app.command("labels")
def labels_cmd(
    name: NameOpt,
    plan: Annotated[str, typer.Option("--plan", help="plan id")],
    subset: Annotated[
        list[str], typer.Option("--subset", help="subset these labels are for (repeatable)")
    ],
    file: Annotated[Path, typer.Option("--file", help="label file: .csv or .jsonl")],
    id_field: Annotated[
        str, typer.Option("--id-field", help="sample_id | view_path | view_stem | meta.<key>")
    ],
    label_set_id: Annotated[str, typer.Option("--id", help="label_set artifact id")],
    id_col: Annotated[
        str, typer.Option("--id-col", help="csv column / jsonl key holding the id")
    ] = "id",
    notes: Annotated[str, typer.Option("--notes")] = "",
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Check a training label file against the split plan and keep it as a label_set."""

    def fn() -> CmdResult:
        res = create_label_set(
            LabelSetSpec(
                name=name,
                plan_id=plan,
                subsets=list(subset),
                file=file,
                id_field=id_field,
                label_set_id=label_set_id,
                id_col=id_col,
                notes=notes,
                data_root=data_root,
                configs_root=configs_root,
            )
        )
        s = res.summary
        fields: dict[str, FieldValue] = {
            "id": label_set_id,
            "dataset": name,
            "plan": plan,
            "subsets": ",".join(s.subsets),
            "rows": s.rows,
            "matched": sum(s.matched.values()),
            "external": s.external,
            "reused": res.reused,
        }
        human = [f"{k}: {v} rows" for k, v in s.matched.items()]
        if s.external:
            human.append(f"not in {name} (external): {s.external} rows")
        return "OK", fields, s.model_dump(mode="json"), human

    run_command(
        "labels",
        json_mode,
        data_root,
        fn,
        context={"id": label_set_id, "dataset": name, "plan": plan},
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/data/test_labels.py`
Expected: all PASS.

- [ ] **Step 5: Lint, then commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/cli.py tests/unit/data/test_labels.py
git commit -m "feat(cli): vcp data labels"
```

---

### Task 4: Attaching (`vcp.data.evidence`)

**Files:**
- Create: `src/vcp/data/evidence.py`
- Test: `tests/unit/data/test_evidence.py`

**Interfaces:**
- Consumes:
  - From Task 1: `EvidenceRef`, `LABELS_ROLE`, `Binding`, `EvidenceKind`, `current`.
  - From Task 2: `LABEL_SET_KIND`, `load_label_set`.
- Produces:
  - `EVIDENCE_KIND = "evidence"`
  - `RunScope(run_id, dataset, samples_hash, plan_id, trained_on: tuple[str, ...])`
  - Parsing and checks:
    - `parse_evidence_args(items: list[str]) -> list[tuple[str, Path]]`
    - `check_evidence_file(name: str, path: Path, role: str) -> None`
  - Making references:
    - `attach_evidence(data_root, scope, name, path, *, role, attempt, binding) -> EvidenceRef`
    - `label_ref(data_root, scope, label_set_id, *, attempt, binding) -> EvidenceRef`
  - Reading them back:
    - `source_sha(data_root, ref) -> str`
    - `ref_intact(data_root, ref) -> bool`
    - `broken_refs(data_root, refs) -> list[str]`

- [ ] **Step 1: Write the failing tests** — `tests/unit/data/test_evidence.py`

```python
import pytest

from helpers import make_label_set, seed_tiny
from vcp.artifact import store
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir
from vcp.data.evidence import (
    RunScope,
    attach_evidence,
    broken_refs,
    check_evidence_file,
    label_ref,
    parse_evidence_args,
    ref_intact,
    source_sha,
)


def _scope(ds, **over):
    base = dict(
        run_id="r1",
        dataset="tiny",
        samples_hash=ds.card.samples_hash,
        plan_id="fixed-v1",
        trained_on=("train",),
    )
    return RunScope(**{**base, **over})


def test_an_evidence_file_becomes_an_immutable_artifact_once(roots, tmp_path):
    ds, _ = seed_tiny(roots)
    f = tmp_path / "teacher.jsonl"
    f.write_text("t1", encoding="utf-8")
    ref = attach_evidence(
        roots.data, _scope(ds), "teacher", f, role="teacher", attempt=1, binding="cli"
    )
    digest = sha256_file(f)
    assert ref.artifact_id == f"r1-teacher-{digest[:12]}" and ref.kind == "evidence"
    assert (ref.role, ref.attempt, ref.binding) == ("teacher", 1, "cli")
    d = artifact_dir(roots.data, "evidence", ref.artifact_id)
    assert (d / "teacher.jsonl").read_text(encoding="utf-8") == "t1"
    assert ref.manifest_sha256 == sha256_file(store.manifest_path(roots.data, "evidence", ref.artifact_id))
    assert source_sha(roots.data, ref) == digest
    again = attach_evidence(
        roots.data, _scope(ds), "teacher", f, role="teacher", attempt=2, binding="cli"
    )
    assert again.artifact_id == ref.artifact_id and again.manifest_sha256 == ref.manifest_sha256
    f.write_text("t2", encoding="utf-8")
    moved = attach_evidence(
        roots.data, _scope(ds), "teacher", f, role="teacher", attempt=2, binding="cli"
    )
    assert moved.artifact_id != ref.artifact_id


def test_evidence_files_are_checked_before_anything_is_copied(roots, tmp_path):
    f = tmp_path / "x.json"
    f.write_text("{}", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="role_reserved"):
        check_evidence_file("x", f, "labels")
    with pytest.raises(ValidationFailed, match="not_found"):
        check_evidence_file("x", tmp_path / "gone.json", "x")
    with pytest.raises(ValidationFailed, match="not_a_file"):
        check_evidence_file("x", tmp_path, "x")
    with pytest.raises(ValidationFailed, match="invalid name"):
        check_evidence_file("bad name", f, "bad name")


def test_parse_evidence_args():
    assert parse_evidence_args(["a=x.json", "b=y/z.csv"])[1][0] == "b"
    with pytest.raises(ValidationFailed, match="NAME=PATH"):
        parse_evidence_args(["a"])
    with pytest.raises(ValidationFailed, match="evidence_conflict"):
        parse_evidence_args(["a=x", "a=y"])


def test_a_label_set_must_fit_the_run(roots, tmp_path):
    ds, plan = seed_tiny(roots)
    make_label_set(roots, tmp_path, plan)
    ref = label_ref(roots.data, _scope(ds), "pseudo-v1", attempt=None, binding="manual")
    assert (ref.name, ref.role, ref.kind, ref.artifact_id) == (
        "pseudo-v1",
        "labels",
        "label_set",
        "pseudo-v1",
    )
    for over, what in (
        ({"dataset": "other"}, "dataset"),
        ({"samples_hash": "0" * 64}, "samples_hash"),
        ({"plan_id": "other-v1"}, "plan"),
        ({"trained_on": ("valA",)}, "trained_on"),
    ):
        with pytest.raises(ValidationFailed, match="labels_mismatch") as ei:
            label_ref(roots.data, _scope(ds, **over), "pseudo-v1", attempt=None, binding="manual")
        assert what in str(ei.value)
    with pytest.raises(ValidationFailed, match="not_found"):
        label_ref(roots.data, _scope(ds), "nope", attempt=None, binding="manual")


def test_broken_refs_names_what_no_longer_verifies(roots, tmp_path):
    ds, plan = seed_tiny(roots)
    make_label_set(roots, tmp_path, plan)
    f = tmp_path / "teacher.jsonl"
    f.write_text("t1", encoding="utf-8")
    ev = attach_evidence(roots.data, _scope(ds), "teacher", f, role="teacher", attempt=1, binding="cli")
    lab = label_ref(roots.data, _scope(ds), "pseudo-v1", attempt=1, binding="cli")
    assert broken_refs(roots.data, [ev, lab]) == [] and ref_intact(roots.data, ev)
    (artifact_dir(roots.data, "evidence", ev.artifact_id) / "teacher.jsonl").write_text("x", encoding="utf-8")
    stale = lab.model_copy(update={"manifest_sha256": "0" * 64})
    assert broken_refs(roots.data, [ev, stale]) == ["teacher", "pseudo-v1"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/data/test_evidence.py`
Expected: FAIL (`No module named 'vcp.data.evidence'`).

- [ ] **Step 3: Create `src/vcp/data/evidence.py`**

```python
"""Attaching what a run read (spec 2026-09-26 §3.2, §4.2): an evidence file becomes an immutable
``evidence`` artifact, a label set is checked against the run, and either way the caller gets an
``EvidenceRef``. ``vcp train run``, ``Session`` and ``vcp eval ingest`` share this; writing the
reference into ``train.yaml`` / ``run.yaml`` is theirs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import validate_name
from vcp.core.time import stamp
from vcp.data.evidence_ref import LABELS_ROLE, Binding, EvidenceKind, EvidenceRef, current
from vcp.data.labels import LABEL_SET_KIND, load_label_set

EVIDENCE_KIND = "evidence"


@dataclass(frozen=True)
class RunScope:
    """What an attachment must agree with: the run, and the dataset version, plan and subsets it
    trains on (spec §4.2)."""

    run_id: str
    dataset: str
    samples_hash: str
    plan_id: str
    trained_on: tuple[str, ...]


def parse_evidence_args(items: list[str]) -> list[tuple[str, Path]]:
    """``NAME=PATH`` options in order; a malformed item or a name given twice is refused."""
    out: list[tuple[str, Path]] = []
    for item in items:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            raise ValidationFailed(
                f"--evidence expects NAME=PATH, got {item!r}", fields={"evidence": item}
            )
        if any(n == name for n, _ in out):
            raise ValidationFailed(
                f"evidence_conflict: --evidence {name!r} is given twice", fields={"evidence": name}
            )
        out.append((name, Path(path)))
    return out


def check_evidence_file(name: str, path: Path, role: str) -> None:
    """Everything an evidence attachment needs before a byte is copied."""
    validate_name(name)
    validate_name(role)
    if role == LABELS_ROLE:
        raise ValidationFailed(
            f"role_reserved: role {LABELS_ROLE!r} is for label sets; attach them with --labels "
            "or Session.attach_labels",
            fields={"evidence": name},
        )
    if not path.exists():
        raise ValidationFailed(f"not_found: evidence {name!r} ({path})", fields={"evidence": name})
    if not path.is_file():
        raise ValidationFailed(
            f"not_a_file: evidence {name!r} ({path}) is not a file", fields={"evidence": name}
        )


def _ref(
    data_root: Path,
    kind: EvidenceKind,
    artifact_id: str,
    *,
    name: str,
    role: str,
    attempt: int | None,
    binding: Binding,
) -> EvidenceRef:
    return EvidenceRef(
        name=name,
        role=role,
        kind=kind,
        artifact_id=artifact_id,
        manifest_sha256=sha256_file(store.manifest_path(data_root, kind, artifact_id)),
        attempt=attempt,
        attached_at=stamp(),
        binding=binding,
    )


def attach_evidence(
    data_root: Path,
    scope: RunScope,
    name: str,
    path: Path,
    *,
    role: str,
    attempt: int | None,
    binding: Binding,
) -> EvidenceRef:
    """Copy ``path`` into ``evidence/<run>-<name>-<sha12>``, or reuse it when those bytes are
    already there (a ``--resume``), and return the reference."""
    check_evidence_file(name, path, role)
    digest = sha256_file(path)
    artifact_id = f"{scope.run_id}-{name}-{digest[:12]}"
    spec = ArtifactSpec(
        kind=EVIDENCE_KIND,
        id=artifact_id,
        dataset=scope.dataset,
        plan_id=scope.plan_id,
        params={"run": scope.run_id, "name": name, "role": role},
        inputs=[InputRef(name="source", sha256=digest)],
    )
    if store.reuse(spec, data_root, check_files=True) is None:
        with ArtifactWriter.create(spec, data_root=data_root) as writer:
            entry = writer.add_file(path.name, path)
            if entry.sha256 != digest:
                raise IntegrityError(
                    f"drift: evidence {name!r} changed while it was being copied",
                    fields={"evidence": name},
                )
            writer.commit()
    return _ref(
        data_root, EVIDENCE_KIND, artifact_id, name=name, role=role, attempt=attempt, binding=binding
    )


def label_ref(
    data_root: Path,
    scope: RunScope,
    label_set_id: str,
    *,
    attempt: int | None,
    binding: Binding,
) -> EvidenceRef:
    """The reference to a label set that fits the run: same dataset version and plan, subsets
    within ``trained_on`` (``labels_mismatch:`` otherwise). Writes nothing."""
    s = load_label_set(data_root, label_set_id)
    problems: list[str] = []
    if s.dataset != scope.dataset:
        problems.append(f"dataset {s.dataset} != {scope.dataset}")
    if s.samples_hash != scope.samples_hash:
        problems.append(f"samples_hash {s.samples_hash[:12]} != {scope.samples_hash[:12]}")
    if s.plan_id != scope.plan_id:
        problems.append(f"plan {s.plan_id} != {scope.plan_id}")
    beyond = sorted(set(s.subsets) - set(scope.trained_on))
    if beyond:
        problems.append(f"subsets {beyond} are not in trained_on {sorted(scope.trained_on)}")
    if problems:
        raise ValidationFailed(
            f"labels_mismatch: label_set/{label_set_id}: {'; '.join(problems)}",
            fields={"label_set": label_set_id},
        )
    return _ref(
        data_root,
        LABEL_SET_KIND,
        label_set_id,
        name=label_set_id,
        role=LABELS_ROLE,
        attempt=attempt,
        binding=binding,
    )


def source_sha(data_root: Path, ref: EvidenceRef) -> str:
    """The sha256 of the one file an evidence artifact holds: its source's bytes when attached."""
    return store.load_manifest(data_root, ref.kind, ref.artifact_id).files[0].sha256


def ref_intact(data_root: Path, ref: EvidenceRef) -> bool:
    """The artifact exists, verifies, and still has the manifest the run pinned."""
    try:
        if store.verify(data_root, ref.kind, ref.artifact_id).failed:
            return False
    except (ValidationFailed, IntegrityError, OSError):
        return False
    return sha256_file(store.manifest_path(data_root, ref.kind, ref.artifact_id)) == ref.manifest_sha256


def broken_refs(data_root: Path, refs: list[EvidenceRef]) -> list[str]:
    """Names of current references that are no longer intact (``train status --verify``)."""
    return [r.name for r in current(refs) if not ref_intact(data_root, r)]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/data/test_evidence.py`
Expected: all PASS.

- [ ] **Step 5: Lint, then commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/data/evidence.py tests/unit/data/test_evidence.py
git commit -m "feat(data): 附上證據與標籤集：evidence 產物、相容檢查、完整性檢查"
```

---

### Task 5: `Session.attach_evidence` / `attach_labels`

**Files:**
- Modify: `src/vcp/train/records.py` (add `append_evidence_event`, `bind_ref`)
- Modify: `src/vcp/train/session.py`
- Test: `tests/unit/train/test_session.py` (append)

**Interfaces:**
- Consumes:
  - From Task 4: `RunScope`, `attach_evidence`, `label_ref`.
  - From Task 1: `add_ref`, `current`.
- Produces:
  - `append_evidence_event(data_root, run_id, ref, attempt) -> None`
  - `bind_ref(data_root, record, ref, attempt) -> tuple[TrainRecord, EvidenceRef]`
  - `Session.attach_evidence(name, path, *, role=None) -> EvidenceRef`
  - `Session.attach_labels(label_set_id) -> EvidenceRef`

- [ ] **Step 1: Append the failing test** to `tests/unit/train/test_session.py`

Add to its imports:

```python
from helpers import make_label_set, seed_tiny
from vcp.measure.runs import save_run
from vcp.measure.schema import RunCard, RunSource
```

Append:

```python
def test_attach_evidence_and_labels_in_process(roots, monkeypatch, tmp_path):
    """VCP-040 / 042: what a loop read lands in train.yaml and the event log, once."""
    ds, plan = seed_tiny(roots)
    make_label_set(roots, tmp_path, plan)
    _running(roots)  # train.yaml of r1 on tiny / fixed-v1 / train, attempt 2
    save_run(
        roots.data,
        RunCard(
            run_id="r1",
            dataset="tiny",
            samples_hash=ds.card.samples_hash,
            plan_id="fixed-v1",
            trained_on=["train"],
            source=RunSource(),
            created_at=STAMP,
        ),
    )
    monkeypatch.setenv("VCP_RUN_ID", "r1")
    monkeypatch.delenv("VCP_ATTEMPT", raising=False)
    s = Session("r1", roots.data)
    teacher = tmp_path / "teacher.jsonl"
    teacher.write_text("t", encoding="utf-8")
    ref = s.attach_evidence("teacher", teacher)
    assert (ref.kind, ref.role, ref.attempt, ref.binding) == ("evidence", "teacher", 2, "session")
    assert s.attach_evidence("teacher", teacher) == ref  # same bytes: no second row
    labels = s.attach_labels("pseudo-v1")
    assert (labels.kind, labels.role, labels.name) == ("label_set", "labels", "pseudo-v1")
    assert [r.name for r in load_record(roots.data, "r1").evidence] == ["teacher", "pseudo-v1"]
    events = [e for e in read_events(roots.data, "r1") if e["event"] == "evidence"]
    assert [(e["name"], e["attempt"]) for e in events] == [("teacher", 2), ("pseudo-v1", 2)]
    with pytest.raises(ValidationFailed, match="role_reserved"):
        s.attach_evidence("x", teacher, role="labels")
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest -o addopts="" -q tests/unit/train/test_session.py::test_attach_evidence_and_labels_in_process`
Expected: FAIL (`AttributeError: 'Session' object has no attribute 'attach_evidence'`).

- [ ] **Step 3: Add the record helpers** to `src/vcp/train/records.py`

Add to its imports:

```python
from vcp.data.evidence_ref import EvidenceRef, add_ref, current
```

Append:

```python
def append_evidence_event(data_root: Path, run_id: str, ref: EvidenceRef, attempt: int) -> None:
    append_event(
        data_root,
        run_id,
        "evidence",
        attempt,
        name=ref.name,
        role=ref.role,
        kind=ref.kind,
        artifact_id=ref.artifact_id,
        manifest_sha256=ref.manifest_sha256,
        binding=ref.binding,
    )


def bind_ref(
    data_root: Path, record: TrainRecord, ref: EvidenceRef, attempt: int
) -> tuple[TrainRecord, EvidenceRef]:
    """Add ``ref`` to ``train.yaml`` (saved) and the event log unless it is already there (spec
    2026-09-26 §3.3); returns the record and the reference now current under that name."""
    refs = add_ref(record.evidence, ref)
    if len(refs) > len(record.evidence):
        record = record.model_copy(update={"evidence": refs})
        save_record(data_root, record)
        append_evidence_event(data_root, record.run_id, ref, attempt)
    return record, next(r for r in current(refs) if r.name == ref.name)
```

- [ ] **Step 4: Add the Session methods** to `src/vcp/train/session.py`

Add to its imports:

```python
from vcp.data.evidence import RunScope, attach_evidence, label_ref
from vcp.data.evidence_ref import EvidenceRef
from vcp.measure.runs import load_run
from vcp.train.records import bind_ref
from vcp.train.schema import CheckpointRecord, TrainRecord
```

(`TrainRecord` joins the existing `CheckpointRecord` import.) Add the methods to `class Session`, after `note`:

```python
    def attach_evidence(
        self, name: str, path: str | Path, *, role: str | None = None
    ) -> EvidenceRef:
        """Copy a file this loop read into an immutable ``evidence`` artifact and bind it to the
        running attempt (spec 2026-09-26 §5.3). Weights are checkpoints, not evidence."""
        record = load_record(self.data_root, self.run_id)
        ref = attach_evidence(
            self.data_root,
            self._scope(record),
            name,
            Path(path).resolve(),
            role=role or name,
            attempt=self.attempt,
            binding="session",
        )
        return bind_ref(self.data_root, record, ref, self.attempt)[1]

    def attach_labels(self, label_set_id: str) -> EvidenceRef:
        """Bind a ``vcp data labels`` label set this loop trains with (spec §5.3, §4.2)."""
        record = load_record(self.data_root, self.run_id)
        ref = label_ref(
            self.data_root,
            self._scope(record),
            label_set_id,
            attempt=self.attempt,
            binding="session",
        )
        return bind_ref(self.data_root, record, ref, self.attempt)[1]

    def _scope(self, record: TrainRecord) -> RunScope:
        card = load_run(self.data_root, self.run_id)
        return RunScope(
            run_id=self.run_id,
            dataset=record.dataset,
            samples_hash=card.samples_hash,
            plan_id=record.plan_id,
            trained_on=tuple(record.trained_on),
        )
```

Also change the docstring of `register_checkpoint` so its first line reads:

```python
        """Register a WEIGHTS file (spec 6.2); anything else the loop read is evidence
        (``attach_evidence``) or a label set (``attach_labels``)."""
```

- [ ] **Step 5: Run the train tests**

Run: `uv run pytest -o addopts="" -q tests/unit/train`
Expected: all PASS.

- [ ] **Step 6: Lint, then commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/train/records.py src/vcp/train/session.py tests/unit/train/test_session.py
git commit -m "feat(train): Session.attach_evidence / attach_labels"
```

---

### Task 6: `vcp train run --evidence / --labels`

**Files:**
- Create: `src/vcp/train/attach.py`
- Modify: `src/vcp/train/run.py` (imports; `RunSpec`, `RunResult` fields; `train_run` preflight, attach, end check, merge)
- Modify: `src/vcp/cli_train.py` (`run_cmd` options and VERDICT fields)
- Test: `tests/unit/train/test_run.py` (append)

**Interfaces:**
- Consumes:
  - From Task 4: `RunScope`, `parse_evidence_args`, `check_evidence_file`, `attach_evidence`, `label_ref`, `source_sha`.
  - From Task 5: `bind_ref`.
  - From Task 1: `current`, `labels_field`, `merge_refs`.
- Produces:
  - `RunSpec.evidence: list[str]`, `RunSpec.labels: list[str]`.
  - `RunResult.evidence: int`, `RunResult.labels: str`, `RunResult.evidence_changed: list[str]`.
  - `vcp.train.attach`:
    - `preflight(data_root, scope, evidence, labels) -> list[tuple[str, Path]]`
    - `attach(data_root, scope, record, evidence, labels, *, attempt) -> tuple[TrainRecord, dict[str, str]]`
    - `moved(evidence, digests) -> list[str]`
  - VERDICT fields `evidence=`, `labels=`, `evidence_changed=`.

- [ ] **Step 1: Append the failing tests** to `tests/unit/train/test_run.py`

Add to its imports: `from helpers import make_label_set` (join the existing `from helpers import ...` line).

Append:

```python
# VCP-040: a loop that attaches what it read, beside what `train run --evidence` attached.
EVIDENCE_FAKE = """
from pathlib import Path
from vcp.train import Session

Path("teacher.jsonl").write_text("t")
Session.current().attach_evidence("teacher", "teacher.jsonl")
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best")
"""

# ... and one that rewrites the file `--evidence corpus=` attached before it started.
CHANGE_FAKE = """
from pathlib import Path
Path("corpus.json").write_text("changed")
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best")
"""


def test_evidence_and_labels_are_checked_before_the_first_write(roots, work, tmp_path):
    _, plan, _ = _seed(roots)
    make_label_set(roots, tmp_path, plan, subsets=("train", "valA"), label_set_id="wide")
    for kw, reason in (
        ({"evidence": [f"teacher={tmp_path / 'gone.jsonl'}"]}, "not_found"),
        ({"evidence": [f"teacher={tmp_path}"]}, "not_a_file"),
        ({"evidence": ["teacher"]}, "NAME=PATH"),
        ({"labels": ["nope"]}, "not_found"),
        ({"labels": ["wide"]}, "labels_mismatch"),  # valA is not in trained_on
    ):
        with pytest.raises(ValidationFailed, match=reason):
            train_run(_spec(roots, work, **kw))
        assert not (run_dir(roots.data, "r1") / "run.yaml").exists()


def test_train_run_binds_evidence_and_labels_to_the_run(roots, work, tmp_path):
    _, plan, _ = _seed(roots)
    make_label_set(roots, tmp_path, plan)
    corpus = tmp_path / "corpus-receipt.json"
    corpus.write_text('{"source": "external"}', encoding="utf-8")
    (work / "evidence_train.py").write_text(EVIDENCE_FAKE, encoding="utf-8")
    kw = dict(
        command=[sys.executable, "evidence_train.py"],
        evidence=[f"corpus={corpus}"],
        labels=["pseudo-v1"],
    )
    res = train_run(_spec(roots, work, **kw))
    assert res.attempt.status == "finished", res.record
    assert (res.evidence, res.labels, res.evidence_changed) == (3, "pseudo-v1", [])
    card = load_run(roots.data, "r1")
    assert [(r.name, r.kind, r.binding) for r in card.evidence] == [
        ("corpus", "evidence", "cli"),
        ("pseudo-v1", "label_set", "cli"),
        ("teacher", "evidence", "session"),
    ]
    assert card.evidence == load_record(roots.data, "r1").evidence
    again = train_run(_spec(roots, work, resume=True, **kw))  # same bytes: same artifacts
    assert again.evidence == 3 and len(load_run(roots.data, "r1").evidence) == 3
    assert len(list((roots.data / "artifacts" / "evidence").iterdir())) == 2


def test_an_evidence_file_that_changes_during_the_run_is_a_warning(roots, work):
    _seed(roots)
    corpus = work / "corpus.json"
    corpus.write_text("original", encoding="utf-8")
    (work / "change_train.py").write_text(CHANGE_FAKE, encoding="utf-8")
    res = train_run(
        _spec(
            roots,
            work,
            command=[sys.executable, "change_train.py"],
            evidence=[f"corpus={corpus}"],
        )
    )
    assert res.evidence_changed == ["corpus"] and "evidence_changed=corpus" in res.warnings
    art = load_run(roots.data, "r1").evidence[0].artifact_id
    kept = roots.data / "artifacts" / "evidence" / art / "corpus.json"
    assert kept.read_text(encoding="utf-8") == "original"  # the attached copy never moves
    notes = [e for e in read_events(roots.data, "r1") if e["event"] == "note"]
    assert any(e.get("key") == "evidence_changed" and e["value"] == "corpus" for e in notes)
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/train/test_run.py -k "evidence"`
Expected: FAIL (pydantic `extra_forbidden` on `RunSpec(evidence=...)`).

- [ ] **Step 3: Create `src/vcp/train/attach.py`**

```python
"""``vcp train run --evidence / --labels`` (spec 2026-09-26 §4.3, §5.2): the checks before the
first write, the attaching before the child starts, and the look afterwards at whether an
evidence file moved while the command ran."""

from __future__ import annotations

from pathlib import Path

from vcp.core.hashing import sha256_file
from vcp.data.evidence import (
    RunScope,
    attach_evidence,
    check_evidence_file,
    label_ref,
    parse_evidence_args,
    source_sha,
)
from vcp.train.records import bind_ref
from vcp.train.schema import TrainRecord


def preflight(
    data_root: Path, scope: RunScope, evidence: list[str], labels: list[str]
) -> list[tuple[str, Path]]:
    """Everything that must hold before the first write: ``NAME=PATH`` items naming files, and
    label sets that fit the run. Returns the parsed evidence."""
    parsed = parse_evidence_args(evidence)
    for name, path in parsed:
        check_evidence_file(name, path, name)
    for label_set_id in labels:
        label_ref(data_root, scope, label_set_id, attempt=None, binding="cli")
    return parsed


def attach(
    data_root: Path,
    scope: RunScope,
    record: TrainRecord,
    evidence: list[tuple[str, Path]],
    labels: list[str],
    *,
    attempt: int,
) -> tuple[TrainRecord, dict[str, str]]:
    """Copy every ``--evidence`` file and bind it, then every ``--labels`` set, to ``attempt``.
    Returns the saved record and each evidence file's sha as attached."""
    digests: dict[str, str] = {}
    for name, path in evidence:
        ref = attach_evidence(
            data_root, scope, name, path, role=name, attempt=attempt, binding="cli"
        )
        record, _ = bind_ref(data_root, record, ref, attempt)
        digests[name] = source_sha(data_root, ref)
    for label_set_id in labels:
        ref = label_ref(data_root, scope, label_set_id, attempt=attempt, binding="cli")
        record, _ = bind_ref(data_root, record, ref, attempt)
    return record, digests


def moved(evidence: list[tuple[str, Path]], digests: dict[str, str]) -> list[str]:
    """Names whose file is gone, or holds other bytes than when it was attached."""
    return [
        name
        for name, path in evidence
        if not path.is_file() or sha256_file(path) != digests[name]
    ]
```

- [ ] **Step 4: Wire it into `src/vcp/train/run.py`**

Imports (add):

```python
from vcp.data.evidence import RunScope
from vcp.data.evidence_ref import current, labels_field, merge_refs
from vcp.train.attach import attach as attach_cli_evidence
from vcp.train.attach import moved, preflight
```

`RunSpec`: add after `notes: str = ""`:

```python
    evidence: list[str] = Field(default_factory=list)  # NAME=PATH (spec 2026-09-26 §5.2)
    labels: list[str] = Field(default_factory=list)  # label_set ids
```

`RunResult`: add after `source_audit_missing: int = 0`:

```python
    evidence: int = 0
    labels: str = "dataset"
    evidence_changed: list[str] = Field(default_factory=list)
```

In `train_run`, right after the `command_found` check (still before any write), add:

```python
    scope = RunScope(
        run_id=spec.run_id,
        dataset=dataset_card.name,
        samples_hash=dataset_card.samples_hash,
        plan_id=spec.plan_id,
        trained_on=tuple(trained_on),
    )
    evidence = preflight(data_root, scope, spec.evidence, spec.labels)
```

Right before `# step 7: the command`, add:

```python
    # spec 2026-09-26 §5.2: attached after the first writes, before the child reads anything.
    record, digests = attach_cli_evidence(
        data_root, scope, record, evidence, spec.labels, attempt=n
    )
```

Right after the `append_event(... "finished" ...)` call, add:

```python
    changed = moved(evidence, digests)
    if changed:
        append_event(
            data_root, spec.run_id, "note", n, key="evidence_changed", value=",".join(changed)
        )
```

Replace `card = card.model_copy(update={"access": merged_access})` with:

```python
    card = card.model_copy(
        update={"access": merged_access, "evidence": merge_refs(record.evidence, card.evidence)}
    )
```

After the line `warnings.append("venv=inherited")` block and before `info = provenance(...)`, add:

```python
    if changed:
        warnings.append(f"evidence_changed={','.join(changed)}")
```

In the final `return RunResult(...)`, add the arguments:

```python
        evidence=len(current(card.evidence)),
        labels=labels_field(card.evidence),
        evidence_changed=changed,
```

- [ ] **Step 5: Wire the CLI** — `src/vcp/cli_train.py` `run_cmd`

Add the options after `notes`:

```python
    evidence: Annotated[
        list[str] | None,
        typer.Option("--evidence", help="NAME=PATH of a file the run reads (repeatable)"),
    ] = None,
    labels: Annotated[
        list[str] | None,
        typer.Option("--labels", help="label_set id the run trains with (repeatable)"),
    ] = None,
```

Pass them to `RunSpec(...)`:

```python
                evidence=list(evidence or []),
                labels=list(labels or []),
```

Add to `fields` after `"provenance": res.provenance,`:

```python
            "evidence": res.evidence,
            "labels": res.labels,
```

And after the `source_audit_missing` block:

```python
        if res.evidence_changed:
            fields["evidence_changed"] = ",".join(res.evidence_changed)
```

- [ ] **Step 6: Run the train tests**

Run: `uv run pytest -o addopts="" -q tests/unit/train tests/unit/test_cli_train.py`
Expected: all PASS. If `tests/unit/test_cli_train.py` does not exist, run `tests/unit/train` only.

- [ ] **Step 7: Lint, then commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/train/attach.py src/vcp/train/run.py src/vcp/cli_train.py tests/unit/train/test_run.py
git commit -m "feat(train): train run --evidence / --labels，結束時比對證據原檔"
```

---

### Task 7: `vcp eval ingest --evidence / --labels`

**Files:**
- Modify: `src/vcp/measure/ingest.py` (`IngestSpec` fields; `_attach`; call after receipts)
- Modify: `src/vcp/cli_eval.py` (`ingest_cmd` options and VERDICT fields)
- Test: `tests/unit/measure/test_ingest.py` (append)

**Interfaces:**
- Consumes:
  - From Task 4: `RunScope`, `parse_evidence_args`, `check_evidence_file`, `attach_evidence`, `label_ref`.
  - From Task 1: `add_refs`, `current`, `labels_field`.
- Produces:
  - `IngestSpec.evidence: list[str]`, `IngestSpec.labels: list[str]`.
  - VERDICT `evidence=`, `labels=` on `eval.ingest`.

- [ ] **Step 1: Append the failing test** to `tests/unit/measure/test_ingest.py`

Add `make_label_set` and `perfect_predictions` to its `from helpers import (...)` block if they are missing; `det_with_runs` is already there.

```python
def test_ingest_binds_evidence_and_labels(roots, tmp_path):
    ds, plan, _ = det_with_runs(roots, tmp_path, n=20)
    make_label_set(roots, tmp_path, plan)
    teacher = tmp_path / "teacher.jsonl"
    teacher.write_text("t", encoding="utf-8")
    src = tmp_path / "labelled-valA.jsonl"
    write_predictions(src, perfect_predictions(ds.subset("valA", plan), ds.card))

    def spec(run_id, trained_on, **over):
        base = dict(
            run_id=run_id,
            dataset="tiny",
            plan_id="fixed-v1",
            subset="valA",
            format="jsonl",
            src=src,
            trained_on=trained_on,
            data_root=roots.data,
            configs_root=roots.configs,
        )
        return IngestSpec(**{**base, **over})

    res = ingest(
        spec("labelled", ["train"], evidence=[f"teacher={teacher}"], labels=["pseudo-v1"])
    )
    assert [(r.name, r.binding, r.attempt) for r in res.run.evidence] == [
        ("teacher", "manual", None),
        ("pseudo-v1", "manual", None),
    ]
    assert load_run(roots.data, "labelled").evidence == res.run.evidence
    with pytest.raises(ValidationFailed, match="labels_mismatch"):
        ingest(spec("other", ["valB"], labels=["pseudo-v1"]))
    assert not (run_dir(roots.data, "other") / "run.yaml").exists()
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest -o addopts="" -q tests/unit/measure/test_ingest.py::test_ingest_binds_evidence_and_labels`
Expected: FAIL (`extra_forbidden` on `IngestSpec(evidence=...)`).

- [ ] **Step 3: Implement in `src/vcp/measure/ingest.py`**

Imports (add):

```python
from vcp.data.evidence import (
    RunScope,
    attach_evidence,
    check_evidence_file,
    label_ref,
    parse_evidence_args,
)
from vcp.data.evidence_ref import add_refs
```

`IngestSpec`: add after `receipts`:

```python
    # spec 2026-09-26 §5.4: evidence files (NAME=PATH) and label_set ids to bind to the run.
    evidence: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
```

Add this function above `def ingest`:

```python
def _attach(data_root: Path, card: RunCard, evidence: list[str], labels: list[str]) -> RunCard:
    """``ingest --evidence / --labels``: bound by hand, no attempt. Every check runs before any
    evidence is copied."""
    scope = RunScope(
        run_id=card.run_id,
        dataset=card.dataset,
        samples_hash=card.samples_hash,
        plan_id=card.plan_id,
        trained_on=tuple(card.trained_on),
    )
    parsed = parse_evidence_args(evidence)
    for name, path in parsed:
        check_evidence_file(name, path, name)
    label_refs = [
        label_ref(data_root, scope, lid, attempt=None, binding="manual") for lid in labels
    ]
    evidence_refs = [
        attach_evidence(data_root, scope, name, path, role=name, attempt=None, binding="manual")
        for name, path in parsed
    ]
    return card.model_copy(update={"evidence": add_refs(card.evidence, [*evidence_refs, *label_refs])})
```

In `ingest`, right after the `attach_receipts` block:

```python
    if spec.evidence or spec.labels:
        card = _attach(paths.data_root, card, spec.evidence, spec.labels)
```

- [ ] **Step 4: Wire the CLI** — `src/vcp/cli_eval.py` `ingest_cmd`

Add the options after `receipt`:

```python
    evidence: Annotated[
        list[str] | None,
        typer.Option("--evidence", help="NAME=PATH of a file the run read (repeatable)"),
    ] = None,
    labels: Annotated[
        list[str] | None,
        typer.Option("--labels", help="label_set id the run trained with (repeatable)"),
    ] = None,
```

Pass `evidence=list(evidence or []), labels=list(labels or []),` to `IngestSpec(...)`. Import `from vcp.data.evidence_ref import current, labels_field` and add to `fields` after `"provenance": res.provenance,`:

```python
            "evidence": len(current(res.run.evidence)),
            "labels": labels_field(res.run.evidence),
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/measure tests/unit/test_cli_eval.py`
Expected: all PASS. If `tests/unit/test_cli_eval.py` does not exist, run `tests/unit/measure` only.

- [ ] **Step 6: Lint, then commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/measure/ingest.py src/vcp/cli_eval.py tests/unit/measure/test_ingest.py
git commit -m "feat(measure): eval ingest --evidence / --labels"
```

---

### Task 8: Status views (`train status`, `eval status`)

**Files:**
- Modify: `src/vcp/train/status.py`, `src/vcp/cli_train.py` (`status_cmd`)
- Modify: `src/vcp/measure/report.py` (`StatusResult.labels`), `src/vcp/cli_eval.py` (`status_cmd` human lines)
- Test: `tests/unit/train/test_status.py` (append), `tests/unit/measure/test_report.py` (append)

**Interfaces:**
- Consumes:
  - From Task 4: `broken_refs`.
  - From Task 1: `current`, `labels_field`.
  - From Task 5: `bind_ref`.
- Produces:
  - `train status` VERDICT `evidence=` and `labels=`; `--verify` drift includes `evidence:<name>`.
  - `StatusResult.labels: dict[str, str]`.

- [ ] **Step 1: Append the failing tests**

`tests/unit/train/test_status.py`: add the imports `from helpers import seed_tiny`, `from vcp.data.evidence import RunScope, attach_evidence`, `from vcp.core.paths import artifact_dir`, `from vcp.train.records import bind_ref, load_record` (skip any it already has), then append:

```python
def test_status_verify_reports_a_broken_evidence_artifact(roots, tmp_path):
    ds, _ = seed_tiny(roots)
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="w",
        command=["python"],
    )
    save_record(roots.data, rec)
    f = tmp_path / "teacher.jsonl"
    f.write_text("t", encoding="utf-8")
    scope = RunScope("r1", "tiny", ds.card.samples_hash, "fixed-v1", ("train",))
    ref = attach_evidence(roots.data, scope, "teacher", f, role="teacher", attempt=1, binding="cli")
    bind_ref(roots.data, load_record(roots.data, "r1"), ref, 1)
    assert status(roots.data, "r1", verify=True).drift == []
    (artifact_dir(roots.data, "evidence", ref.artifact_id) / "teacher.jsonl").write_text("x", encoding="utf-8")
    assert status(roots.data, "r1", verify=True).drift == ["evidence:teacher"]
```

(`TrainRecord`, `save_record`, `status` are already imported there; add any that are not.)

`tests/unit/measure/test_report.py`: append (add the imports `make_label_set`, `IngestSpec`, `ingest`, `write_predictions`, `perfect_predictions` if missing):

```python
def test_eval_status_names_each_runs_labels(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=20)
    make_label_set(roots, tmp_path, plan)
    src = tmp_path / "labelled.jsonl"
    write_predictions(src, perfect_predictions(ds.subset("valA", plan), ds.card))
    ingest(
        IngestSpec(
            run_id="labelled",
            dataset="tiny",
            plan_id="fixed-v1",
            subset="valA",
            format="jsonl",
            src=src,
            trained_on=["train"],
            labels=["pseudo-v1"],
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    st = status(paths)
    assert st.labels == {"labelled": "pseudo-v1", "noisy": "dataset", "perfect": "dataset"}
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/train/test_status.py tests/unit/measure/test_report.py -k "evidence or labels"`
Expected: FAIL (the drift list lacks `evidence:teacher`; `StatusResult` has no `labels`).

- [ ] **Step 3: Implement**

`src/vcp/train/status.py`: import `from vcp.data.evidence import broken_refs`, then replace the `drift=` argument with:

```python
        drift=(
            _drift(record, data_root) + [f"evidence:{n}" for n in broken_refs(data_root, record.evidence)]
            if verify
            else []
        ),
```

`src/vcp/cli_train.py` `status_cmd`: import `from vcp.data.evidence_ref import current, labels_field` (combine with Task 6's import if present), and add to `fields` after `"running": st.running,`:

```python
            "evidence": len(current(st.record.evidence)),
            "labels": labels_field(st.record.evidence),
```

`src/vcp/measure/report.py`: import `from vcp.data.evidence_ref import labels_field`; add to `StatusResult` (last field):

```python
    labels: dict[str, str] = field(default_factory=dict)  # run id -> label sets or "dataset"
```

In `status()`, inside `for card in sorted(cards, ...)`, as the first statement:

```python
        labels[card.run_id] = labels_field(card.evidence)
```

with `labels: dict[str, str] = {}` declared next to `grades`. Pass `labels=labels` in the `StatusResult(...)` return.

`src/vcp/cli_eval.py` `status_cmd`: change the per-run human line to:

```python
        human += [
            f"{run}: provenance={g} observed={','.join(st.observed[run]) or '-'} "
            f"labels={st.labels.get(run, 'dataset')}"
            for run, g in st.provenance.items()
        ]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/train tests/unit/measure`
Expected: all PASS.

- [ ] **Step 5: Lint, then commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/train/status.py src/vcp/cli_train.py src/vcp/measure/report.py src/vcp/cli_eval.py tests/unit/train/test_status.py tests/unit/measure/test_report.py
git commit -m "feat(status): train status 的證據與 --verify、eval status 的 labels"
```

---

### Task 9: Backup (roles, walk, consistency)

**Files:**
- Modify: `src/vcp/backup/schema.py` (`ROLES`, `_TIER2`)
- Modify: `src/vcp/backup/evidence.py` (walk)
- Modify: `src/vcp/backup/verify.py` (`_run_card`, `_train_record` checkers)
- Test: `tests/unit/backup/test_evidence_run.py`, `tests/unit/backup/test_verify.py` (append)

**Interfaces:**
- Consumes:
  - From Task 4: `RunScope`, `attach_evidence`, `label_ref`.
  - From Task 1: `merge_refs`.
  - From Task 2: `make_label_set` (test helper).
- Produces:
  - Roles `label_set` (tier 1) and `evidence` (tier 2).
  - Consistency labels `<run>/run.yaml:evidence.<kind>/<artifact_id>` and `<run>/train.yaml:evidence.<kind>/<artifact_id>`.

- [ ] **Step 1: Append the failing tests**

`tests/unit/backup/test_evidence_run.py`: add the imports `from helpers import make_label_set`, `from vcp.data.evidence import RunScope, attach_evidence, label_ref`, `from vcp.data.split import load_plan`, then append:

```python
def _with_evidence(world, tmp_path):
    paths = DatasetPaths.resolve(EVAL, data_root=world.roots.data, configs_root=world.roots.configs)
    make_label_set(world.roots, tmp_path, load_plan(paths, "fixed-v1"), dataset=EVAL)
    card = load_run(world.roots.data, "good")
    scope = RunScope("good", EVAL, card.samples_hash, "fixed-v1", tuple(card.trained_on))
    f = tmp_path / "teacher.jsonl"
    f.write_text("t", encoding="utf-8")
    ev = attach_evidence(world.roots.data, scope, "teacher", f, role="teacher", attempt=None, binding="manual")
    lab = label_ref(world.roots.data, scope, "pseudo-v1", attempt=None, binding="manual")
    save_run(world.roots.data, card.model_copy(update={"evidence": [ev, lab]}))
    return ev, lab


def test_walk_run_collects_evidence_and_label_sets(world, tmp_path):
    assert TIER_OF["label_set"] == 1 and TIER_OF["evidence"] == 2
    ev, _ = _with_evidence(world, tmp_path)
    col = _col(world)
    col.walk_run("good", "run:good")
    roles = _roles(col)
    assert roles["label_set"] == [
        "artifacts/label_set/pseudo-v1/label_set.json",
        "artifacts/label_set/pseudo-v1/labels.csv",
        "artifacts/label_set/pseudo-v1/manifest.json",
    ]
    assert roles["evidence"] == [
        f"artifacts/evidence/{ev.artifact_id}/manifest.json",
        f"artifacts/evidence/{ev.artifact_id}/teacher.jsonl",
    ]
    assert col.missing == [] and col.unlisted == []
```

`tests/unit/backup/test_verify.py`: add the imports `from helpers import make_label_set`, `from vcp.data.split import load_plan`, `from vcp.data.evidence import RunScope, label_ref`, `from vcp.measure.runs import save_run` (skip any it already has), then append:

```python
def test_verify_checks_the_manifest_each_evidence_reference_pinned(world, tmp_path):
    paths = DatasetPaths.resolve(EVAL, data_root=world.roots.data, configs_root=world.roots.configs)
    make_label_set(world.roots, tmp_path, load_plan(paths, "fixed-v1"), dataset=EVAL)
    card = load_run(world.roots.data, "good")
    scope = RunScope("good", EVAL, card.samples_hash, "fixed-v1", tuple(card.trained_on))
    lab = label_ref(world.roots.data, scope, "pseudo-v1", attempt=None, binding="manual")
    save_run(world.roots.data, card.model_copy(update={"evidence": [lab]}))
    build_manifest(EVAL, "run:good", manifest_id="ev", **_kw(world))
    assert not any("evidence" in d.what for d in verify(EVAL, "ev", **_kw(world)).drift)
    stale = lab.model_copy(update={"manifest_sha256": "0" * 64})
    save_run(world.roots.data, card.model_copy(update={"evidence": [stale]}))
    whats = {d.what for d in verify(EVAL, "ev", **_kw(world)).drift}
    assert "good/run.yaml:evidence.label_set/pseudo-v1" in whats
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/backup -k "evidence or pinned"`
Expected: FAIL (`KeyError: 'label_set'` in `TIER_OF`).

- [ ] **Step 3: Implement**

`src/vcp/backup/schema.py`: in `ROLES`, insert the two roles right after `"source_audit",`:

```python
    "source_audit",
    "label_set",
    "evidence",
```

and extend `_TIER2`:

```python
_TIER2 = ("source_audit", "prediction", "samples", "raw_manifest", "train_dir", "logs", "evidence")
```

`src/vcp/backup/evidence.py`: add the imports `from vcp.artifact import store` and `from vcp.data.evidence_ref import EvidenceRef, merge_refs` (plus `IntegrityError` from `vcp.core.errors` if missing). In `walk_run`, right after the `for ref in card.access:` block, add:

```python
        for ref in self._evidence_of(run_id, card.evidence):  # spec 2026-09-26 §6.1
            self._artifact(ref, conclusion)
```

and add these two methods to `Collector`:

```python
    def _evidence_of(self, run_id: str, card_refs: list[EvidenceRef]) -> list[EvidenceRef]:
        """Every reference, history included: run.yaml's, then train.yaml's it lacks."""
        if not has_record(self.data_root, run_id):
            return list(card_refs)
        return merge_refs(card_refs, load_train_record(self.data_root, run_id).evidence)

    def _artifact(self, ref: EvidenceRef, conclusion: str) -> None:
        adir = artifact_dir(self.data_root, ref.kind, ref.artifact_id)
        self.add(adir / "manifest.json", ref.kind, conclusion, sha256=ref.manifest_sha256)
        try:
            manifest = store.load_manifest(self.data_root, ref.kind, ref.artifact_id)
        except (ValidationFailed, IntegrityError):
            return  # listed above as missing, with the sha the run pinned
        for f in manifest.files:
            self.add(adir / f.name, ref.kind, conclusion, sha256=f.sha256, size=f.bytes)
```

`src/vcp/backup/verify.py`: import `from vcp.artifact import store` and add the helper:

```python
def _evidence(owner: str, refs: list[EvidenceRef], paths: DatasetPaths, add: Adder) -> None:
    for ref in refs:
        p = store.manifest_path(paths.data_root, ref.kind, ref.artifact_id)
        if p.is_file():
            add(f"{owner}:evidence.{ref.kind}/{ref.artifact_id}", ref.manifest_sha256, sha256_file(p))
```

(import `EvidenceRef` from `vcp.data.evidence_ref`). Call it at the end of `_run_card` as `_evidence(f"{card.run_id}/run.yaml", card.evidence, paths, add)`, and at the end of `_train_record` as `_evidence(f"{rec.run_id}/train.yaml", rec.evidence, paths, add)`.

- [ ] **Step 4: Run the backup tests**

Run: `uv run pytest -o addopts="" -q tests/unit/backup tests/unit/test_e2e_backup.py`
Expected: all PASS. If a test pins the full `ROLES` tuple or the index of `history`, update it to the new order, `access_receipt`, `source_audit`, `label_set`, `evidence`, `history`, and say so in the task report.

- [ ] **Step 5: Lint, then commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/backup/schema.py src/vcp/backup/evidence.py src/vcp/backup/verify.py tests/unit/backup/test_evidence_run.py tests/unit/backup/test_verify.py
git commit -m "feat(backup): 備份清單收證據與標籤集，一致性層比對 run 釘住的 manifest"
```

---

### Task 10: Provenance graph edges

**Files:**
- Modify: `src/vcp/provenance/graph.py` (`_scan_runs`; helper `_evidence_intact`)
- Test: `tests/unit/provenance/test_graph_views.py` (append)

**Interfaces:**
- Consumes: `EvidenceRef` (Task 1); `make_label_set`, `seed_tiny` (Task 2); `RunScope`, `label_ref` (Task 4).
- Produces:
  - Edges `artifact:<kind>/<id>` → `run:<id>`, type `CONSUMED_BY`, no attributes.
  - Run `broken_reason` gains `invalid evidence/<kind>/<id>`.

- [ ] **Step 1: Append the failing tests** to `tests/unit/provenance/test_graph_views.py`

Add the imports `from helpers import make_label_set` and `from vcp.data.evidence import RunScope, label_ref`, then append:

```python
def _labelled_perfect(roots, tmp_path):
    ds, plan, _ = det_with_runs(roots, tmp_path, n=20)
    make_label_set(roots, tmp_path, plan)
    card = load_run(roots.data, "perfect")
    scope = RunScope("perfect", "tiny", card.samples_hash, "fixed-v1", tuple(card.trained_on))
    ref = label_ref(roots.data, scope, "pseudo-v1", attempt=None, binding="manual")
    return ds, card, ref


def test_a_label_set_is_consumed_by_the_run_and_produced_by_the_dataset(roots, tmp_path):
    ds, card, ref = _labelled_perfect(roots, tmp_path)
    save_run(roots.data, card.model_copy(update={"evidence": [ref]}))
    graph = build_graph(roots.data, roots.configs)
    run = entity_id("run", "perfect")
    label_set = entity_id("artifact", "label_set/pseudo-v1")
    edges = {(e.source_id, e.target_id, e.edge_type) for e in graph.edges.values()}
    assert (label_set, run, "CONSUMED_BY") in edges
    assert (dataset_version_id("tiny", ds.card.samples_hash), label_set, "PRODUCED_BY") in edges
    assert graph.entities[run].broken_reason is None


def test_a_missing_or_repinned_evidence_reference_marks_the_run_broken(roots, tmp_path):
    _, card, ref = _labelled_perfect(roots, tmp_path)
    gone = ref.model_copy(update={"name": "gone", "artifact_id": "gone"})
    stale = ref.model_copy(update={"manifest_sha256": "0" * 64})
    for bad, what in ((gone, "label_set/gone"), (stale, "label_set/pseudo-v1")):
        save_run(roots.data, card.model_copy(update={"evidence": [bad]}))
        reason = build_graph(roots.data, roots.configs).entities[entity_id("run", "perfect")].broken_reason
        assert reason is not None and f"invalid evidence/{what}" in reason
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/provenance/test_graph_views.py -k "label_set or evidence"`
Expected: FAIL (no `CONSUMED_BY` edge; `broken_reason` is None).

- [ ] **Step 3: Implement in `src/vcp/provenance/graph.py`**

Import `from vcp.data.evidence_ref import EvidenceRef` (and `sha256_file` from `vcp.core.hashing` if the module does not import it yet). Add the helper next to `_run_entity`:

```python
def _evidence_intact(graph: ProvenanceGraph, data_root: Path, ref: EvidenceRef) -> bool:
    """The artifact a run references was scanned, verified, and still has the manifest the run
    pinned (spec 2026-09-26 §6.2)."""
    entity = graph.entities.get(entity_id("artifact", f"{ref.kind}/{ref.artifact_id}"))
    if entity is None or entity.broken_reason is not None:
        return False
    manifest = store.manifest_path(data_root, ref.kind, ref.artifact_id)
    return manifest.is_file() and sha256_file(manifest) == ref.manifest_sha256
```

In `_scan_runs`, right after the `if info.invalid:` block, add:

```python
        consumed: list[str] = []
        for ref in card.evidence:
            if _evidence_intact(graph, data_root, ref):
                consumed.append(entity_id("artifact", f"{ref.kind}/{ref.artifact_id}"))
            else:
                missing.append(f"invalid evidence/{ref.kind}/{ref.artifact_id}")
```

At the end of the loop body, after the receipt edges, add:

```python
        for artifact in consumed:
            graph.add_edge(artifact, ident, "CONSUMED_BY")
```

- [ ] **Step 4: Run the provenance tests**

Run: `uv run pytest -o addopts="" -q tests/unit/provenance`
Expected: all PASS; PostgreSQL cases skip without a configured service.

- [ ] **Step 5: Lint, then commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/provenance/graph.py tests/unit/provenance/test_graph_views.py
git commit -m "feat(provenance): 證據與標籤集連到 run，壞掉的參照讓 run broken"
```

---

### Task 11: End-to-end test and documentation

**Files:**
- Create: `tests/unit/test_e2e_evidence.py`
- Modify: `docs/reference/cli.md`, four specs (training, measurement, backup, data layer), `CLAUDE.md`, `AGENTS.md`, `.claude/skills/vcp-data-pipeline/SKILL.md`, `.claude/skills/vcp-train-submit-backup/SKILL.md` (then mirror to `.agents/skills/`), `docs/audits/2026-09-11-vcp-improvement-audit.md`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the end-to-end test** — `tests/unit/test_e2e_evidence.py`

```python
"""VCP-040 + 042 end to end through the CLI: a checked label set; a run that trains with it and
attaches evidence on the command line and from inside the loop; then status, backup and the
provenance graph all see them -- and no label content reaches a VERDICT or the log."""

import sys

from typer.testing import CliRunner

from helpers import seed_tiny
from vcp.cli import app
from vcp.provenance.graph import build_graph, entity_id

runner = CliRunner()
LOOP = """
from pathlib import Path
from vcp.train import Session

Path("teacher.jsonl").write_text("t")
Session.current().attach_evidence("teacher", "teacher.jsonl")
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best")
"""


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def test_run_evidence_story(roots, tmp_path):
    _, plan = seed_tiny(roots)
    labels = tmp_path / "pseudo.csv"
    rows = "".join(f"{i},zz-label-content\n" for i in sorted(plan.ids_in("train")))
    labels.write_text("id,y\n" + rows, encoding="utf-8")
    outputs: list[str] = []

    def run(*args: str) -> str:
        r = runner.invoke(app, list(args))
        outputs.append(r.output)
        assert r.exit_code == 0, r.output
        return _verdict(r.output)

    v = run("data", "labels", "--name", "tiny", "--plan", "fixed-v1", "--subset", "train",
            "--file", str(labels), "--id-field", "sample_id", "--id", "pseudo-v1")
    assert "status=OK" in v
    work = tmp_path / "work"
    work.mkdir()
    (work / "loop.py").write_text(LOOP, encoding="utf-8")
    corpus = tmp_path / "corpus.json"
    corpus.write_text("{}", encoding="utf-8")
    v = run("train", "run", "--run", "r1", "--dataset", "tiny", "--plan", "fixed-v1",
            "--trained-on", "train", "--seed", "1", "--cwd", str(work),
            "--checkpoints", "weights/*.pt", "--final", "weights/best.pt",
            "--evidence", f"corpus={corpus}", "--labels", "pseudo-v1",
            "--", sys.executable, "loop.py")
    assert "evidence=3" in v and "labels=pseudo-v1" in v
    v = run("train", "status", "--run", "r1", "--verify")
    assert "evidence=3" in v and "labels=pseudo-v1" in v and "drift=0" in v
    run("backup", "manifest", "--dataset", "tiny", "--conclusion", "run:r1", "--id", "ev1")
    v = run("backup", "verify", "--dataset", "tiny", "--manifest", "ev1")
    assert "status=OK" in v and "drift=0" in v
    graph = build_graph(roots.data, roots.configs)
    run_id = entity_id("run", "r1")
    consumed = {
        e.source_id
        for e in graph.edges.values()
        if e.target_id == run_id and e.edge_type == "CONSUMED_BY"
    }
    assert entity_id("artifact", "label_set/pseudo-v1") in consumed
    assert graph.entities[run_id].broken_reason is None
    logs = "".join(p.read_text(encoding="utf-8") for p in (roots.data / "logs").iterdir())
    assert "zz-label-content" not in logs
    assert all("zz-label-content" not in o for o in outputs)
```

(After writing, run `uv run ruff format tests/unit/test_e2e_evidence.py` so the call arguments are laid out the project's way.)

- [ ] **Step 2: Run it**

Run: `uv run pytest -o addopts="" -q tests/unit/test_e2e_evidence.py`
Expected: PASS. If it fails, that is a real integration bug in Tasks 1–10: fix the code, not the assertions, and name the fix in the task report.

- [ ] **Step 3: Update `docs/reference/cli.md`**

Make the edits below with the Edit tool; the file is UTF-8 and LF.

1. Insert this row right after the `| \`vcp data export\` |` row:

```markdown
| `vcp data labels` | 訓練標籤檔（`.csv` / `.jsonl`）對切分 plan 驗證後存成不可變的 `label_set/<id>`：每列的 id 依 `--id-field` 對到樣本；落在 `--subset` 以外的 dataset 樣本（驗證、sealed、未分配）→ FAIL `labels_outside_subsets:`（只報計數與前幾個 sample id）；不在 dataset 裡的列允許、記 `external=`；sealed 子集不能當 `--subset`（`labels_on_sealed:`）。同 id 同內容重跑 `reused=true`。VERDICT `rows=` `matched=` `external=` `subsets=` | `--name`、`--plan`、`--subset`（可重複）、`--file`、`--id-field sample_id\|view_path\|view_stem\|meta.<key>`、`--id-col`（預設 `id`）、`--id`、`--notes` |
```

2. In the `vcp eval ingest` row, replace `` `--receipt ID`（可重複；把 `vcp train run` 之外產生的收據掛上 run） |`` with:

```markdown
`--receipt ID`（可重複；把 `vcp train run` 之外產生的收據掛上 run）、`--evidence NAME=PATH`、`--labels ID`（皆可重複；參照記進 `run.yaml` 的 `evidence`，VERDICT `evidence=` `labels=`） |
```

3. In the `vcp eval status` row, replace `最新 σ_p | ` with `最新 σ_p；每個 run 另列 \`labels=\`（\`dataset\` 或 label_set id） | `. Only the second cell changes; the options cell stays as it is.

4. In the `vcp train run` row, replace `` `--resume`、`--notes`；`--` 之後是訓練命令 |`` with:

```markdown
`--resume`、`--notes`、`--evidence NAME=PATH`（可重複：子程序啟動前複製成 `evidence` 產物；結束時原檔變了 → WARN `evidence_changed=`）、`--labels ID`（可重複：`vcp data labels` 的標籤集，須與 run 的 dataset 版本、plan、`trained_on` 相容，否則 `labels_mismatch:`）；VERDICT `evidence=` `labels=`；`--` 之後是訓練命令 |
```

5. In the `vcp train status` row, replace `` | `--run`、`--verify`（重算 sha） |`` with:

```markdown
；VERDICT `evidence=` `labels=`，`--verify` 另驗證據產物（壞了列成 `drift` 的 `evidence:<名稱>`） | `--run`、`--verify`（重算 sha） |
```

6. In the training-loop example, after the line `per_attempt = f"evidence.a{s.attempt}.json"  # --resume 後遞增，與收據 id 的 -a<n>- 相同`, add:

```python
    s.attach_evidence("teacher", "preds/teacher.jsonl")  # 讀過的檔：複製成 evidence 產物
    s.attach_labels("pseudo-v3")  # `vcp data labels` 驗過的標籤集
```

- [ ] **Step 4: Append the spec amendments**

Append each block at the end of its file, keeping one blank line between the file's last line and the block.

`docs/superpowers/specs/2026-09-05-vcp-training-layer-design.md`:

```markdown

18. **run 讀的證據檔與標籤集（VCP-040 / 042，2026-09-26）**：`vcp train run --evidence NAME=PATH --labels ID`、`Session.attach_evidence / attach_labels` 與 `train.log.jsonl` 的新事件 `evidence`；`train.yaml` 多一張 `evidence` 清單（空的時候不寫出），結束時照 `access` 的方式合進 `run.yaml`。`register_checkpoint` 只給權重。細節見 `2026-09-26-vcp-run-evidence-design.md`。
```

`docs/superpowers/specs/2026-09-04-vcp-measurement-layer-design.md`:

```markdown

## 18. run 的證據參照（VCP-040 / 042，2026-09-26）

`RunCard.evidence`（空的時候不寫出）列出 run 讀過的 `evidence` 與 `label_set` 產物；`vcp eval ingest --evidence NAME=PATH --labels ID` 以 `binding=manual` 附上；`vcp eval status` 每個 run 顯示 `labels=`。量測、判決、融合不讀這張清單，provenance 等級不變。細節見 `2026-09-26-vcp-run-evidence-design.md`。
```

`docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md`:

```markdown
- **證據與標籤集**（2026-09-26，VCP-040 / 042）：新角色 `label_set`（tier 1）與 `evidence`（tier 2）。`run:` 走法收進 `run.yaml` 與 `train.yaml` 的 `evidence` 清單指到的每個產物（含歷史列）的 `manifest.json` 與檔案；一致性層比對每筆參照釘住的 `manifest_sha256`，標籤 `<run>/run.yaml:evidence.<kind>/<id>`。細節見 `2026-09-26-vcp-run-evidence-design.md`。
```

`docs/superpowers/specs/2026-09-02-vcp-skeleton-and-data-layer-design.md`:

```markdown

## 18. 標籤集（VCP-042，2026-09-26）

`vcp data labels` 把訓練標籤檔對切分 plan 驗證後存成不可變的 `label_set/<id>`（`labels.csv|jsonl` + `label_set.json`）：落在允許子集以外的 dataset 樣本 → `labels_outside_subsets:`，sealed 子集不能被標（`labels_on_sealed:`），不在 dataset 裡的列只記數。驗證在 vcp 自己的程序裡讀全部樣本列（同 `data audit`），不經存取器、不留收據。細節見 `2026-09-26-vcp-run-evidence-design.md`。
```

- [ ] **Step 5: `CLAUDE.md` and `AGENTS.md`** — the same edit in both files

After the bullet that ends with `壞掉的稽核沒有 supersedes：搬走目錄再 \`validate\` 重建。`, insert:

```markdown
- `artifacts/label_set/<id>/`（`labels.csv|jsonl` + `label_set.json`）是 `vcp data labels` 驗過的訓練標籤集：每列的 id 必須落在列出的子集（不能是 sealed），落在別的子集就 `labels_outside_subsets:` FAIL，不在 dataset 裡的列只記數；可跨 run 共用。`artifacts/evidence/<run>-<name>-<sha12>/` 是 run 讀過的證據檔副本。兩者都掛在 `run.yaml` / `train.yaml` 的 `evidence` 清單（空的時候不寫出）：`vcp train run --evidence NAME=PATH --labels ID`、`Session.attach_evidence / attach_labels`、`vcp eval ingest --evidence / --labels`；權重以外的東西不用 `register_checkpoint`。
```

- [ ] **Step 6: The skills** — edit under `.claude/skills/`, then mirror

In `.claude/skills/vcp-data-pipeline/SKILL.md`, insert right before the line `## 無標註資料（test 集）`:

```markdown
- 偽標籤、soft label、蒸餾標籤先 `uv run vcp data labels --name <ds> --plan fixed-v1 --subset train --file <labels.csv> --id-field sample_id --id <id>`：落在驗證或 sealed 樣本的列會 FAIL（`labels_outside_subsets:`），不在 dataset 裡的列只記數；訓練時用 `--labels <id>` 掛上，`run.yaml` 才說得出實際用的是哪份標籤。

```

In `.claude/skills/vcp-train-submit-backup/SKILL.md`, replace `別呼叫私有的 \`_attempt()\`）在 \`train run\` 底下才可用。` with:

```markdown
別呼叫私有的 `_attempt()`）在 `train run` 底下才可用。run 讀的其他檔（teacher 預測、外部語料收據）用 `--evidence NAME=PATH` 或 `s.attach_evidence(name, path)`，標籤集用 `--labels ID` 或 `s.attach_labels(id)`；`register_checkpoint` 只給權重（拿它登記 JSON 會讓 `weights_hash` 變成那份 JSON 的 sha）。
```

Then mirror and check:

```bash
cp -r .claude/skills/vcp-data-pipeline/. .agents/skills/vcp-data-pipeline/
cp -r .claude/skills/vcp-train-submit-backup/. .agents/skills/vcp-train-submit-backup/
diff -r .claude/skills .agents/skills
```

- [ ] **Step 7: The audit** — `docs/audits/2026-09-11-vcp-improvement-audit.md`

In the §16 table, change the VCP-040 row's last cell to `已實作（隨 0.11.0）`, and the VCP-042 row's last cell to `已實作（隨 0.11.0，與 VCP-040 同一件）`.

Replace the status line under `### VCP-040 + VCP-042：run 實際讀的輸入沒有正式紀錄` with:

```markdown
**狀態：已實作，隨 0.11.0 發出（spec `2026-09-26-vcp-run-evidence-design.md`）。**
```

- [ ] **Step 8: Run everything, check the files, commit**

```bash
uv run pytest -o addopts="" -q
uv run ruff check . && uv run ruff format --check .
git diff --check
git add tests/unit/test_e2e_evidence.py docs/reference/cli.md docs/superpowers/specs/2026-09-05-vcp-training-layer-design.md docs/superpowers/specs/2026-09-04-vcp-measurement-layer-design.md docs/superpowers/specs/2026-09-06-vcp-backup-audit-design.md docs/superpowers/specs/2026-09-02-vcp-skeleton-and-data-layer-design.md CLAUDE.md AGENTS.md .claude/skills/vcp-data-pipeline/SKILL.md .claude/skills/vcp-train-submit-backup/SKILL.md .agents/skills/vcp-data-pipeline/SKILL.md .agents/skills/vcp-train-submit-backup/SKILL.md docs/audits/2026-09-11-vcp-improvement-audit.md
git commit -m "docs: 證據與標籤集的命令參考、spec 修訂、skill 與端到端測試"
```

Expected: the full suite passes (the 0.10.0 count of 1748 plus this plan's new tests), with 76 skips.

---

### Task 12: Release 0.11.0 (after the feature PR is merged)

Run this task only after the user approves merging the feature PR and it is merged. It follows the 0.10.0 flow in `vcp-release-and-environments`: a separate branch and PR, then an annotated tag on the merge commit.

**Files:**
- Modify:
  - `src/vcp/__init__.py`, `.claude/.claude-plugin/plugin.json`, `tests/unit/test_package.py` (version `0.11.0`)
  - `CHANGELOG.md`, `tests/unit/test_regression_gate.py`
  - `docs/handover/HANDOVER.md`, `README.md`, `README.zh-TW.md`

- [ ] **Step 1: Branch from the merged main**

```bash
git fetch origin
git worktree add .claude/worktrees/vcp-release-0110 -b chore/release-0.11.0 origin/main
```

- [ ] **Step 2: Bump the three version strings**

- `src/vcp/__init__.py`: `__version__ = "0.11.0"`.
- `.claude/.claude-plugin/plugin.json`: `"version": "0.11.0",`.
- `tests/unit/test_package.py`: `EXPECTED_CANDIDATE_VERSION = "0.11.0"`.

- [ ] **Step 3: Add the CHANGELOG entry** above `## [0.10.0] - 2026-09-25`:

```markdown
## [0.11.0] - YYYY-MM-DD

run 讀的證據檔與標籤集（VCP-040 + VCP-042）；tag `v0.11.0` 打在發版 PR 的合併 commit 上。MINOR 的
理由：新命令 `vcp data labels`、`train run` / `eval ingest` 的新選項、新 VERDICT 欄位（`evidence=`
`labels=` `evidence_changed=` `rows=` `matched=` `external=`）與 `reason=` 字彙、新產物種類
`label_set` / `evidence`、`run.yaml` / `train.yaml` 的 `evidence` 清單與 `train.log.jsonl` 的
`evidence` 事件、備份角色 `label_set` / `evidence`。

### Added
- `vcp data labels`：訓練標籤檔對切分 plan 驗證後存成不可變的 `label_set/<id>`；落在允許子集以外
  的 dataset 樣本 FAIL `labels_outside_subsets:`，sealed 子集不能被標，不在 dataset 裡的列只記數。
- `vcp train run --evidence NAME=PATH --labels ID`、`Session.attach_evidence / attach_labels`、
  `vcp eval ingest --evidence / --labels`：參照記進 `run.yaml` / `train.yaml` 的 `evidence` 清單。
  清單空的時候不寫出，舊版仍讀得動沒用到證據的 run。
- `train status` 的 `evidence=` / `labels=`，`--verify` 另驗證據產物；`eval status` 每個 run 的
  `labels=`。
- 備份清單收證據與標籤集，一致性層比對 run 釘住的 manifest；provenance 圖把它們連到 run。

### Changed
- `Session.register_checkpoint` 的說明明寫只給權重。
```

Replace `YYYY-MM-DD` with the release day, taken from `uv run python -c "from vcp.core.time import stamp; print(stamp()[:10])"`.

- [ ] **Step 4: Add the regression gate row** — append to `GATE` in `tests/unit/test_regression_gate.py`

```python
    (
        "VCP-040/042 (0.11.0)",
        "data / train: a label set that labels eval or sealed samples is refused; evidence and "
        "label sets reach run.yaml, backup and the provenance graph; an empty list is not "
        "written",
        {
            "tests/unit/data/test_labels.py": [
                "test_labels_on_eval_or_sealed_samples_are_refused",
                "test_a_sealed_subset_cannot_be_labelled_and_an_unknown_one_is_a_plan_mismatch",
            ],
            "tests/unit/data/test_evidence_ref.py": ["test_an_empty_evidence_list_is_not_written"],
            "tests/unit/data/test_evidence.py": ["test_a_label_set_must_fit_the_run"],
            "tests/unit/train/test_run.py": [
                "test_evidence_and_labels_are_checked_before_the_first_write",
                "test_train_run_binds_evidence_and_labels_to_the_run",
            ],
            "tests/unit/test_e2e_evidence.py": ["test_run_evidence_story"],
        },
    ),
```

- [ ] **Step 5: Reinstall, run the suite with coverage, fill in the numbers**

```bash
uv sync -q --reinstall-package vcp
uv run pytest --cov=vcp -o addopts="" -p no:cacheprovider
```

Take `P passed`, `S skipped` and the total coverage `C%` from the output. Then:

- `docs/handover/HANDOVER.md` §2:
  - Prepend a version entry to the `- 版本：` line: `` `0.11.0`（tag `v0.11.0`，<day>，MINOR：run 讀的證據檔與標籤集，VCP-040 + 042） ``.
  - Update the `- 測試：` line to `0.11.0 = P passed / S skipped，覆蓋率 C%`, and keep the 0.10.0 numbers as the previous ones.
  - In §6 item 9, delete `VCP-040 + 042（run 讀的證據檔與標籤集綁進紀錄）、`.
- `README.md` and `README.zh-TW.md`:
  - The version badge becomes `0.11.0`.
  - The tests badge becomes `P`.
  - The status line's version becomes `` `0.11.0` ``.

- [ ] **Step 6: Check, commit, open the PR**

```bash
uv run ruff check . && uv run ruff format --check .
git diff --check
git add src/vcp/__init__.py .claude/.claude-plugin/plugin.json tests/unit/test_package.py CHANGELOG.md tests/unit/test_regression_gate.py docs/handover/HANDOVER.md README.md README.zh-TW.md
git commit -m "chore(release): v0.11.0"
git push -u origin chore/release-0.11.0
gh pr create --base main --head chore/release-0.11.0 --title "chore(release): v0.11.0" --body-file <body file>
```

- [ ] **Step 7: After CI is green and the user approves the merge** — merge, tag the merge commit, and push the tag:

```bash
gh pr merge <PR> --merge
git fetch origin
git tag -a v0.11.0 <merge commit> -m "vcp 0.11.0"
git push origin v0.11.0
```

---

## Self-review (done while writing)

**1. Spec coverage**

| Spec section | Tasks |
|---|---|
| §3.1 label_set | 2 |
| §3.2 evidence | 4 |
| §3.3 EvidenceRef, lists, omit-empty, event, merge | 1, 5, 6 |
| §4.1 classification | 2 |
| §4.2 compatibility | 4, with the preflight in 6 and the ingest in 7 |
| §4.3 source changed | 6 |
| §5.1 `data labels` | 3 |
| §5.2 `train run` | 6 |
| §5.3 Session | 5 |
| §5.4 ingest | 7 |
| §5.5 views | 8 |
| §6.1 backup | 9 |
| §6.2 graph | 10 |
| §7 vocabulary | 2, 4, 6 |
| §8 compatibility | 1 |
| §9 tests | every task, plus 11 |
| §10 docs | 11 |
| Release | 12 |

**2. Placeholder scan.** The only fill-ins are the release day and the numbers in Task 12. Both have an exact command that produces them.

**3. Type consistency.**
- `RunScope(run_id, dataset, samples_hash, plan_id, trained_on: tuple)` is built the same way in Tasks 4, 5, 6, 7, 8, 9 and 10.
- `attach_evidence(data_root, scope, name, path, *, role, attempt, binding)` and `label_ref(data_root, scope, label_set_id, *, attempt, binding)` are called with those keywords everywhere.
- `bind_ref(data_root, record, ref, attempt) -> (record, ref)` is used by Tasks 5, 6 and 8.
- `labels_field` / `current` are used the same way in Tasks 6, 7 and 8.
