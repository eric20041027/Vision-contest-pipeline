"""Canonical det / seg subset -> COCO instances.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.exporters.base import ExportOutput, select_view
from vcp.data.schema import Mask, Sample


def segmentation_from_mask(mask: Mask) -> Any:
    if mask.polygon is not None:
        return mask.polygon
    size = list(mask.meta.get("size", []))
    if mask.meta.get("rle_encoding") == "uncompressed":
        return {"counts": [int(c) for c in mask.rle.split(",") if c], "size": size}
    return {"counts": mask.rle, "size": size}


def polygon_area(polygon: list[list[float]]) -> float:
    """Sum of shoelace areas of the rings (absolute value)."""
    total = 0.0
    for ring in polygon:
        xs, ys = ring[0::2], ring[1::2]
        n = len(xs)
        total += abs(sum(xs[i] * ys[(i + 1) % n] - xs[(i + 1) % n] * ys[i] for i in range(n))) / 2
    return total


def mask_area(mask: Mask) -> float:
    if "area" in mask.meta:
        return float(mask.meta["area"])
    if mask.polygon is not None:
        return polygon_area(mask.polygon)
    return 0.0


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
    ) -> ExportOutput:
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
            images.append(
                {
                    "id": image_id,
                    "file_name": view.path,
                    "width": view.width,
                    "height": view.height,
                    "sample_id": s.sample_id,
                }
            )
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
                    ann["area"] = mask_area(it)
                annotations.append(ann)
        categories = [
            {
                "id": c.id,
                "name": c.name,
                **({"supercategory": c.meta["supercategory"]} if "supercategory" in c.meta else {}),
            }
            for c in dataset.card.categories
        ]
        target = out / "instances.json"
        with target.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(
                {"images": images, "annotations": annotations, "categories": categories},
                f,
                ensure_ascii=False,
            )
            f.write("\n")
        warnings = []
        if dropped_views:
            warnings.append(f"{dropped_views} annotations on non-exported views dropped")
        if png_masks:
            warnings.append(f"{png_masks} PNG-path masks cannot be expressed in COCO; skipped")
        return ExportOutput([target], warnings)
