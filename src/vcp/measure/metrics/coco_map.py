"""COCO-style mAP (pycocotools). Ground truth and detections are built in memory from the
canonical samples / predictions, so the metric works on any subset of samples."""

from __future__ import annotations

import contextlib
import io
from typing import Any

import numpy as np

from vcp.core.errors import ValidationFailed, VcpError
from vcp.data.schema import DatasetCard, Sample
from vcp.measure.metrics.base import effective_params, gold_only, require_nonempty
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


def _coerce_iscrowd(value: Any, sample_id: str) -> int:
    """``Box.meta['iscrowd']`` is free-form JSON, so it may arrive as an int, a bool, or a
    numeral string (the framework's own COCO importer writes a plain ``1``); anything else is a
    located ``ValidationFailed`` rather than a value pycocotools silently misinterprets."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    if isinstance(value, str) and value in ("0", "1"):
        return int(value)
    raise ValidationFailed(
        f"box.meta['iscrowd'] must be 0, 1, a bool, or their string form, got {value!r}",
        location=sample_id,
    )


def _gt_dataset(samples: list[Sample], card: DatasetCard) -> dict[str, Any]:
    """Build a pycocotools-shaped dataset dict. ``samples`` is sorted by sample_id by the caller,
    so the image-id assignment below is derived from the dataset, not from incoming call order."""
    images, annotations = [], []
    for i, s in enumerate(samples, start=1):
        v = s.views[0]
        if v.width is None or v.height is None:
            raise ValidationFailed(
                f"sample {s.sample_id!r} view has no size; re-import the dataset"
            )
        images.append({"id": i, "width": v.width, "height": v.height})
        for b in (s.labels.boxes if s.labels else None) or []:  # type: ignore[union-attr]
            annotations.append(
                {
                    "id": len(annotations) + 1,
                    "image_id": i,
                    "category_id": b.category_id,
                    "bbox": [b.x, b.y, b.w, b.h],
                    "area": b.w * b.h,
                    "iscrowd": _coerce_iscrowd(b.meta.get("iscrowd", 0), s.sample_id),
                }
            )
    if not annotations:
        raise ValidationFailed("no ground-truth boxes in this subset; mAP is undefined")
    return {
        "images": images,
        "annotations": annotations,
        "categories": [{"id": c.id, "name": c.name} for c in card.categories],
    }


def _require_view_zero(samples: list[Sample], predictions: dict[str, Prediction]) -> None:
    """coco_map builds one pycocotools image per sample from view 0 alone (mirrors Task 8
    ruling 3, which raises the same way on the mask side). Per-view image ids are the fuller
    fix and are a recorded follow-up; until then, a box on any other view must fail loudly
    instead of being silently folded into view 0's image."""
    for s in samples:
        for b in (s.labels.boxes if s.labels else None) or []:
            if b.view != 0:
                raise ValidationFailed(
                    f"sample {s.sample_id!r} has a gold box on view {b.view}; "
                    "det metrics score view 0 only",
                    location=s.sample_id,
                )
        pred = predictions.get(s.sample_id)
        for b in (pred.boxes if pred else None) or []:
            if b.view != 0:
                raise ValidationFailed(
                    f"sample {s.sample_id!r} has a prediction box on view {b.view}; "
                    "det metrics score view 0 only",
                    location=s.sample_id,
                )


class CocoMap:
    """COCO-style mean average precision over IoU thresholds (pycocotools). Higher is better."""

    name = "coco_map"
    version = "1"
    tasks = frozenset({"det"})
    defaults = {"iou": "50:95", "max_dets": "100"}
    higher_is_better = True

    def compute(
        self,
        samples: list[Sample],
        predictions: dict[str, Prediction],
        card: DatasetCard,
        params: dict[str, str],
    ) -> MetricResult:
        p = effective_params(self, params)
        if p["iou"] not in IOU_PRESETS:
            raise ValidationFailed(f"iou must be one of {sorted(IOU_PRESETS)}, got {p['iou']!r}")
        raw_max_dets = p["max_dets"].strip()
        if not raw_max_dets.isdigit():
            raise ValidationFailed(f"max_dets must be a positive integer, got {p['max_dets']!r}")
        max_dets = int(raw_max_dets)
        if max_dets < 1:
            raise ValidationFailed(f"max_dets must be >= 1, got {max_dets}")
        COCO, COCOeval = require_pycocotools()
        samples = require_nonempty(samples)
        samples = gold_only(samples)
        # Sorted so the image-id mapping is derived from the dataset (sample_id order), never
        # from whatever order the caller happened to pass in.
        samples = sorted(samples, key=lambda s: s.sample_id)
        _require_view_zero(samples, predictions)
        gt = COCO()
        gt.dataset = _gt_dataset(samples, card)
        with contextlib.redirect_stdout(io.StringIO()):
            gt.createIndex()
        dts = [
            {
                "image_id": i,
                "category_id": b.category_id,
                "bbox": [b.x, b.y, b.w, b.h],
                "score": b.score,
            }
            for i, s in enumerate(samples, start=1)
            for b in (
                (predictions[s.sample_id].boxes if s.sample_id in predictions else None) or []
            )
        ]
        names = {c.id: c.name for c in card.categories}
        if not dts:
            # pycocotools' loadRes crashes on an empty results list (indexes anns[0]), so
            # short-circuit here. A category with at least one gold instance in this subset
            # reports 0.0 (every instance is an unrecalled false negative); one with zero gold
            # instances here reports None (undefined), matching what the full pycocotools path
            # below would say for it. The gold -- already in gt.dataset["annotations"] -- is
            # what tells the two cases apart, not the (absent) detections.
            gold_cats = {a["category_id"] for a in gt.dataset["annotations"]}
            per_class = {n: (0.0 if cid in gold_cats else None) for cid, n in names.items()}
            return MetricResult(value=0.0, per_class=per_class, n=len(samples))
        with contextlib.redirect_stdout(io.StringIO()):
            dt = gt.loadRes(dts)
            ev = COCOeval(gt, dt, "bbox")
            ev.params.iouThrs = np.array(IOU_PRESETS[p["iou"]])
            max_dets_list = sorted({1, 10, max_dets})
            ev.params.maxDets = max_dets_list
            ev.evaluate()
            ev.accumulate()
        # ev.params.maxDets can reorder max_dets relative to the fixed 1/10 anchors (e.g. a
        # requested 5 sorts between them), so the slot is looked up by value -- never assumed to
        # be last, which would silently ignore any request below 10 (ruling 2).
        m_idx = max_dets_list.index(max_dets)
        precision = ev.eval["precision"][:, :, :, 0, m_idx]  # thresholds x recall x class
        valid = precision > -1
        value = float(precision[valid].mean()) if valid.any() else 0.0
        per_class: dict[str, float | None] = {}
        for k, cat_id in enumerate(ev.params.catIds):
            pk = precision[:, :, k]
            per_class[names[cat_id]] = float(pk[pk > -1].mean()) if (pk > -1).any() else None
        return MetricResult(value=value, per_class=per_class, n=len(samples))
