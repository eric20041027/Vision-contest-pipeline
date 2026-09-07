"""Task registry: what a label must look like and how to stratify it, per task type.

Adding a task type = one ``TaskSpec`` + ``register_task``; schema and CLI stay untouched.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, NoReturn

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.schema import Box, DatasetCard, Sample

StratKey = int | str | float | tuple[int, ...]
LabelField = Literal["cls", "targets", "boxes", "masks"]
BOUNDS_TOLERANCE_PX = 1.0


@dataclass(frozen=True)
class TaskSpec:
    name: str
    label_field: LabelField
    validate: Callable[[Sample, DatasetCard], None]
    stratify_key: Callable[[Sample, DatasetCard], StratKey | None]
    pred_payload: str  # which vcp.measure.schema.Prediction field carries this task's predictions


TASKS: dict[str, TaskSpec] = {}


def register_task(spec: TaskSpec) -> None:
    if spec.name in TASKS:
        raise RegistryError(f"task {spec.name!r} already registered")
    TASKS[spec.name] = spec


def get_task(name: str) -> TaskSpec:
    try:
        return TASKS[name]
    except KeyError:
        raise RegistryError(f"unknown task {name!r}; known: {sorted(TASKS)}") from None


def _fail(sample: Sample, msg: str) -> NoReturn:
    raise ValidationFailed(msg, location=f"sample {sample.sample_id}")


def _cat_ids(card: DatasetCard) -> set[int]:
    return {c.id for c in card.categories}


def _cat_names(card: DatasetCard) -> list[str]:
    return [c.name for c in card.categories]


def _validate_cls(sample: Sample, card: DatasetCard) -> None:
    if sample.labels is None:
        return
    if sample.labels.cls is None:
        _fail(sample, "task cls requires labels.cls")
    if sample.labels.cls not in _cat_ids(card):
        _fail(sample, f"unknown category id {sample.labels.cls}")


def _validate_multilabel(sample: Sample, card: DatasetCard) -> None:
    if sample.labels is None:
        return
    targets = sample.labels.targets
    if targets is None:
        _fail(sample, "task multilabel requires labels.targets")
    names = set(_cat_names(card))
    if set(targets) != names:
        _fail(sample, f"targets keys {sorted(targets)} must equal category names {sorted(names)}")
    bad = {k: v for k, v in targets.items() if v not in (0.0, 1.0)}
    if bad:
        _fail(sample, f"multilabel targets must be 0/1, got {bad}")


def _validate_regression(sample: Sample, card: DatasetCard) -> None:
    if sample.labels is None:
        return
    targets = sample.labels.targets
    if targets is None:
        _fail(sample, "task regression requires labels.targets")
    if not targets:
        _fail(sample, "task regression requires at least one target value")
    unknown = sorted(set(targets) - set(_cat_names(card)))
    if unknown:
        _fail(sample, f"unknown target names {unknown}; declared: {_cat_names(card)}")
    bad = {k: v for k, v in targets.items() if not math.isfinite(v)}
    if bad:
        _fail(sample, f"regression targets must be finite, got {bad}")


def _check_annotation_views(sample: Sample) -> None:
    """Every annotation's ``view`` index must name a view this sample owns -- boxes AND masks,
    not just the field the task's ``label_field`` points at (2c-1 / 3-13).

    A ``det`` sample may legally carry masks and a ``seg`` one boxes, and the coords audit reads
    both fields whichever task it runs under. An index past the end reached ``_view_size`` there
    as a bare ``IndexError`` (ABORT) although it is plainly bad input; checking both fields at
    the boundary makes it the located FAIL it always was.
    """
    if sample.labels is None:
        return
    n = len(sample.views)
    for kind, items in (("box", sample.labels.boxes), ("mask", sample.labels.masks)):
        for idx, item in enumerate(items or []):
            if item.view >= n:
                _fail(
                    sample,
                    f"{kind} {idx}: view index {item.view} out of range (sample has {n} views)",
                )


def _check_category(
    sample: Sample, card: DatasetCard, idx: int, kind: str, category_id: int
) -> None:
    if category_id not in _cat_ids(card):
        _fail(sample, f"{kind} {idx}: unknown category id {category_id}")


def _check_bounds(sample: Sample, idx: int, box: Box) -> None:
    v = sample.views[box.view]
    if v.width is None or v.height is None:
        return
    tol = BOUNDS_TOLERANCE_PX
    if (
        box.x < -tol
        or box.y < -tol
        or box.x + box.w > v.width + tol
        or box.y + box.h > v.height + tol
    ):
        _fail(
            sample,
            f"box {idx} exceeds view bounds: xywh=({box.x}, {box.y}, {box.w}, {box.h}) "
            f"view={v.width}x{v.height}",
        )


def _validate_det(sample: Sample, card: DatasetCard) -> None:
    if sample.labels is None:
        return
    boxes = sample.labels.boxes
    if boxes is None:
        _fail(sample, "task det requires labels.boxes (use [] for a negative sample)")
    _check_annotation_views(sample)
    for i, b in enumerate(boxes):
        _check_category(sample, card, i, "box", b.category_id)
        _check_bounds(sample, i, b)


def _validate_seg(sample: Sample, card: DatasetCard) -> None:
    if sample.labels is None:
        return
    masks = sample.labels.masks
    if masks is None:
        _fail(sample, "task seg requires labels.masks")
    _check_annotation_views(sample)
    for i, m in enumerate(masks):
        _check_category(sample, card, i, "mask", m.category_id)


def _key_cls(sample: Sample, card: DatasetCard) -> StratKey | None:
    return None if sample.labels is None else sample.labels.cls


def _key_vector(sample: Sample, card: DatasetCard) -> StratKey | None:
    if sample.labels is None or sample.labels.targets is None:
        return None
    return tuple(int(sample.labels.targets.get(n, 0)) for n in _cat_names(card))


def _key_regression(sample: Sample, card: DatasetCard) -> StratKey | None:
    if sample.labels is None or sample.labels.targets is None or not card.categories:
        return None
    value = sample.labels.targets.get(card.categories[0].name)
    return None if value is None else float(value)


def _key_presence(sample: Sample, card: DatasetCard) -> StratKey | None:
    if sample.labels is None:
        return None
    items = sample.labels.boxes if sample.labels.boxes is not None else (sample.labels.masks or [])
    present = {it.category_id for it in items}
    return tuple(int(c.id in present) for c in card.categories)


for _spec in (
    TaskSpec("cls", "cls", _validate_cls, _key_cls, pred_payload="scores"),
    TaskSpec("multilabel", "targets", _validate_multilabel, _key_vector, pred_payload="scores"),
    TaskSpec(
        "regression", "targets", _validate_regression, _key_regression, pred_payload="targets"
    ),
    TaskSpec("det", "boxes", _validate_det, _key_presence, pred_payload="boxes"),
    TaskSpec("seg", "masks", _validate_seg, _key_presence, pred_payload="masks"),
):
    register_task(_spec)
