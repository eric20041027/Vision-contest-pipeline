"""COCO results JSON ([{image_id, category_id, bbox, score, segmentation?}]) -> Predictions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.measure.converters.base import ConvertContext
from vcp.measure.schema import PredBox, Prediction, PredMask, payload_field


def image_id_map(ctx: ConvertContext) -> dict[int, str]:
    """image_id -> sample_id from the COCO export's instances.json, or --opt id_map=<json>."""
    if ctx.options.get("id_map"):
        path = Path(ctx.options["id_map"])
        if not path.is_file():
            raise ValidationFailed(f"id_map file not found: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {int(k): str(v) for k, v in raw.items()}
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as e:
            raise ValidationFailed(f"bad id_map {path}: {e}") from e
    if ctx.export_dir is None:
        raise ValidationFailed(
            "coco_results needs --export-manifest <coco export dir> (its instances.json maps "
            "image_id to sample_id) or --opt id_map=<json file>"
        )
    instances = ctx.export_dir / "instances.json"
    if not instances.is_file():
        raise ValidationFailed(f"instances.json not found in export dir {ctx.export_dir}")
    try:
        doc = json.loads(instances.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValidationFailed(f"{instances}: {e}") from e
    images = doc.get("images") if isinstance(doc, dict) else None
    valid = isinstance(images, list) and all(
        isinstance(im, dict) and "id" in im and "sample_id" in im for im in images
    )
    if not valid:
        raise ValidationFailed(
            f"{instances}: expected the 'images' list from a vcp COCO export (each entry with "
            "'id' and 'sample_id'); point --export-manifest at the vcp export directory, not "
            "the original annotations (re-export with the current vcp)"
        )
    return {int(im["id"]): str(im["sample_id"]) for im in images}


def _mask(seg: Any, category_id: int, score: float, *, index: int, location: str) -> PredMask:
    """Parse a COCO ``segmentation`` value: a list of polygon rings (each a flat list of
    numbers), or an RLE dict carrying ``counts`` (a compressed string, or an uncompressed list
    of ints) and a two-element ``size``."""
    shape = (
        f"result {index}: segmentation must be a list of polygon rings (each a flat list of "
        "numbers), or an RLE dict with 'counts' and a two-element 'size'"
    )
    try:
        if isinstance(seg, list) and seg:
            return PredMask(
                category_id=category_id,
                score=score,
                polygon=[[float(v) for v in poly] for poly in seg],
            )
        if isinstance(seg, dict) and "counts" in seg:
            counts = seg["counts"]
            size = list(seg.get("size", []))
            if len(size) != 2:
                raise ValidationFailed(shape, location=location)
            if isinstance(counts, str):
                return PredMask(
                    category_id=category_id, score=score, rle=counts, meta={"size": size}
                )
            return PredMask(
                category_id=category_id,
                score=score,
                rle=",".join(str(c) for c in counts),
                meta={"size": size, "rle_encoding": "uncompressed"},
            )
    except ValidationError as e:
        raise ValidationFailed(f"result {index}: {e}", location=location) from e
    except (TypeError, ValueError) as e:
        raise ValidationFailed(f"{shape} ({type(e).__name__}: {e})", location=location) from e
    raise ValidationFailed(shape, location=location)


class CocoResultsConverter:
    name = "coco_results"
    version = "1"

    def convert(self, src: Path, ctx: ConvertContext) -> list[Prediction]:
        task = ctx.dataset.card.task
        # Which tasks this converter serves is derived from the task registry, not a
        # hardcoded ("det", "seg") tuple: a COCO results file carries per-instance boxes or
        # masks. Registering a new box/mask-payload task must not require editing this
        # converter.
        field = payload_field(task)
        if field not in ("boxes", "masks"):
            raise ValidationFailed(
                f"coco_results converts tasks whose predictions are boxes or masks, but task "
                f"{task!r} predicts {field!r}"
            )
        if not src.is_file():
            raise ValidationFailed(f"results file not found: {src}")
        try:
            results = json.loads(src.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValidationFailed(f"{src.name}: {e}") from e
        if not isinstance(results, list):
            raise ValidationFailed(f"{src.name}: expected a JSON list of results")
        ids = image_id_map(ctx)
        cat_ids = {c.id for c in ctx.dataset.card.categories}
        boxes: dict[str, list[PredBox]] = {}
        masks: dict[str, list[PredMask]] = {}
        for i, r in enumerate(results):
            try:
                image_id = int(r["image_id"])
                category_id = int(r["category_id"])
                score = float(r["score"])
            except (KeyError, TypeError, ValueError) as e:
                raise ValidationFailed(
                    f"result {i}: bad field ({type(e).__name__}: {e})", location=src.name
                ) from e
            if image_id not in ids:
                raise ValidationFailed(
                    f"result {i}: unknown image_id {image_id}", location=src.name
                )
            if category_id not in cat_ids:
                raise ValidationFailed(
                    f"result {i}: unknown category_id {category_id}", location=src.name
                )
            sid = ids[image_id]
            if field == "boxes":
                bbox = r.get("bbox")
                if not isinstance(bbox, list) or len(bbox) != 4:
                    raise ValidationFailed(
                        f"result {i}: bbox must have 4 numbers", location=src.name
                    )
                try:
                    x, y, w, h = (float(v) for v in bbox)
                except (TypeError, ValueError) as e:
                    raise ValidationFailed(
                        f"result {i}: bad bbox value ({type(e).__name__}: {e})",
                        location=src.name,
                    ) from e
                try:
                    boxes.setdefault(sid, []).append(
                        PredBox(x=x, y=y, w=w, h=h, category_id=category_id, score=score)
                    )
                except ValidationError as e:
                    raise ValidationFailed(f"result {i}: {e}", location=src.name) from e
            else:
                masks.setdefault(sid, []).append(
                    _mask(r.get("segmentation"), category_id, score, index=i, location=src.name)
                )
        try:
            if field == "boxes":
                return [Prediction(sample_id=s, boxes=b) for s, b in sorted(boxes.items())]
            return [Prediction(sample_id=s, masks=m) for s, m in sorted(masks.items())]
        except ValidationError as e:
            raise ValidationFailed(str(e), location=src.name) from e
