"""Dataset-level Dice and mIoU: intersection / areas accumulated per class over all samples,
then averaged over the classes that have any ground truth or predicted pixels.

A class with neither gold nor predicted pixels anywhere in the subset is undefined (``None``
in ``per_class``) rather than 1.0 or 0.0, and is excluded from the macro mean -- the same way
``macro_auc`` excludes an all-constant class: an empty intersection over an empty union is
"nothing to measure" here, not "perfect" or "total miss". If *every* class in the subset is
undefined this way, the metric itself is undefined and raises ``ValidationFailed`` rather than
silently reporting a misleading 0.0 for "no data" (mirroring how ``macro_auc`` raises when
every class is all-positive or all-negative).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from vcp.core.errors import ValidationFailed
from vcp.data.schema import DatasetCard, Mask, Sample
from vcp.measure.masks import mask_array
from vcp.measure.metrics.base import effective_params, gold_only, require_nonempty
from vcp.measure.schema import MetricResult, Prediction, PredMask

ClassTotals = tuple[dict[int, float], dict[int, float], dict[int, float]]


def _require_view_zero(samples: list[Sample], predictions: dict[str, Prediction]) -> None:
    """seg metrics rasterise every mask against view 0's (width, height) alone (mirrors
    coco_map's ``_require_view_zero`` on the box side). A mask on any other view must fail
    loudly instead of being silently rasterised against the wrong view's size."""
    for s in samples:
        for m in (s.labels.masks if s.labels else None) or []:  # type: ignore[union-attr]
            if m.view != 0:
                raise ValidationFailed(
                    f"sample {s.sample_id!r} has a gold mask on view {m.view}; "
                    "seg metrics score view 0 only",
                    location=s.sample_id,
                )
        pred = predictions.get(s.sample_id)
        for m in (pred.masks if pred else None) or []:
            if m.view != 0:
                raise ValidationFailed(
                    f"sample {s.sample_id!r} has a prediction mask on view {m.view}; "
                    "seg metrics score view 0 only",
                    location=s.sample_id,
                )


def _accumulate(
    masks: Sequence[Mask | PredMask],
    dest: dict[int, np.ndarray],
    *,
    sample_id: str,
    kind: str,
    width: int,
    height: int,
) -> None:
    for m in masks:
        if m.category_id not in dest:
            raise ValidationFailed(
                f"sample {sample_id!r} has a {kind} mask with unknown category id "
                f"{m.category_id}; declared categories: {sorted(dest)}",
                location=sample_id,
            )
        dest[m.category_id] |= mask_array(m, width, height)


def _class_totals(
    samples: list[Sample],
    predictions: dict[str, Prediction],
    card: DatasetCard,
    threshold: float,
) -> ClassTotals:
    samples = require_nonempty(samples)
    samples = gold_only(samples)
    _require_view_zero(samples, predictions)
    inter: dict[int, float] = {c.id: 0.0 for c in card.categories}
    gt_area = dict(inter)
    pr_area = dict(inter)
    for s in samples:
        v = s.views[0]
        if v.width is None or v.height is None:
            raise ValidationFailed(
                f"sample {s.sample_id!r} view has no size; re-import the dataset"
            )
        gt = {cid: np.zeros((v.height, v.width), dtype=bool) for cid in inter}
        gold_masks = (s.labels.masks if s.labels else None) or []  # type: ignore[union-attr]
        _accumulate(
            gold_masks, gt, sample_id=s.sample_id, kind="gold", width=v.width, height=v.height
        )
        pr = {cid: np.zeros((v.height, v.width), dtype=bool) for cid in inter}
        pred = predictions.get(s.sample_id)
        pred_masks = [m for m in ((pred.masks if pred else None) or []) if m.score >= threshold]
        _accumulate(
            pred_masks, pr, sample_id=s.sample_id, kind="prediction", width=v.width, height=v.height
        )
        for cid in inter:
            inter[cid] += float(np.logical_and(gt[cid], pr[cid]).sum())
            gt_area[cid] += float(gt[cid].sum())
            pr_area[cid] += float(pr[cid].sum())
    return inter, gt_area, pr_area


def _parse_threshold(raw: str) -> float:
    try:
        return float(raw)
    except ValueError as e:
        raise ValidationFailed(f"threshold must be a number, got {raw!r}") from e


def _summarise(
    metric_name: str, card: DatasetCard, per_id: dict[int, float | None], n: int
) -> MetricResult:
    """Per-class values keyed by category NAME; raises if every class is undefined (see the
    module docstring for the no-gold/no-prediction decision)."""
    names = {c.id: c.name for c in card.categories}
    per_class = {names[cid]: v for cid, v in per_id.items()}
    defined = [v for v in per_class.values() if v is not None]
    if not defined:
        raise ValidationFailed(
            f"{metric_name} undefined: no category has any gold or predicted pixels in this subset"
        )
    return MetricResult(value=float(np.mean(defined)), per_class=per_class, n=n)


class Dice:
    """Dataset-level Dice coefficient (2 * intersection / (gold area + predicted area)) per
    class, macro-averaged over classes with any gold or predicted pixels. Higher is better."""

    name, version, tasks, defaults = "dice", "1", frozenset({"seg"}), {"threshold": "0.5"}
    higher_is_better = True

    def compute(
        self,
        samples: list[Sample],
        predictions: dict[str, Prediction],
        card: DatasetCard,
        params: dict[str, str],
    ) -> MetricResult:
        threshold = _parse_threshold(effective_params(self, params)["threshold"])
        inter, gt, pr = _class_totals(samples, predictions, card, threshold)
        per = {
            cid: (2 * inter[cid] / (gt[cid] + pr[cid]) if gt[cid] + pr[cid] > 0 else None)
            for cid in inter
        }
        return _summarise("dice", card, per, len(samples))


class MIoU:
    """Dataset-level mean IoU (intersection / union) per class, macro-averaged over classes
    with any gold or predicted pixels. Higher is better."""

    name, version, tasks, defaults = "miou", "1", frozenset({"seg"}), {"threshold": "0.5"}
    higher_is_better = True

    def compute(
        self,
        samples: list[Sample],
        predictions: dict[str, Prediction],
        card: DatasetCard,
        params: dict[str, str],
    ) -> MetricResult:
        threshold = _parse_threshold(effective_params(self, params)["threshold"])
        inter, gt, pr = _class_totals(samples, predictions, card, threshold)
        per: dict[int, float | None] = {}
        for cid in inter:
            union = gt[cid] + pr[cid] - inter[cid]
            per[cid] = inter[cid] / union if union > 0 else None
        return _summarise("miou", card, per, len(samples))
