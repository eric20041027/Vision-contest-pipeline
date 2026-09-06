"""COCO ``results.json``: one entry per box or mask (spec 9.4)."""

from __future__ import annotations

import json
from typing import Any

from vcp.core.errors import ValidationFailed
from vcp.data.schema import Sample
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import Prediction, PredMask, payload_field
from vcp.submit.writers.base import (
    WriteContext,
    WriteResult,
    check_options,
    output_ids,
    sorted_samples,
)


def _image_id(value: str) -> int | str:
    """A canonical decimal id becomes an int (COCO image ids are ints); "07" stays a string,
    so a zero-padded id can never alias another sample's int id."""
    return int(value) if value.isdigit() and str(int(value)) == value else value


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
