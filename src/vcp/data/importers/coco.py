"""COCO instances JSON importer (detection or segmentation)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vcp.core.errors import ValidationFailed
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.importers.common import (
    SWAPPED_ORIENTATIONS,
    count_exif_rotated,
    exif_policy_option,
    image_header,
)
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
        exif_policy = exif_policy_option(opts)
        views = _load_views(doc["images"], images_dir, exif_policy)
        boxes: dict[int, list[Box]] = {iid: [] for iid in views}
        masks: dict[int, list[Mask]] = {iid: [] for iid in views}
        skipped: list[dict[str, Any]] = []
        for i, ann in enumerate(doc["annotations"]):
            ann_id = ann.get("id", i)
            try:
                iid = int(ann["image_id"])
                cid = int(ann["category_id"])
                bbox = [float(v) for v in ann["bbox"]] if task == "det" else None
            except (KeyError, TypeError, ValueError) as e:
                raise ValidationFailed(
                    f"annotation {ann_id!r}: bad field ({type(e).__name__}: {e})",
                    location=str(json_path),
                ) from e
            if bbox is not None and len(bbox) != 4:
                raise ValidationFailed(
                    f"annotation {ann_id!r}: bbox must have 4 numbers, got {len(bbox)}",
                    location=str(json_path),
                )
            if iid not in views:
                skipped.append({"annotation": ann_id, "reason": f"unknown image_id {iid}"})
                continue
            meta: dict[str, Any] = {"iscrowd": 1} if ann.get("iscrowd") else {}
            if task == "det":
                x, y, w, h = bbox
                boxes[iid].append(Box(x=x, y=y, w=w, h=h, category_id=cid, meta=meta))
                continue
            if task == "seg" and "area" in ann:
                meta["area"] = float(ann["area"])
            mask = mask_from_segmentation(ann.get("segmentation"), cid, meta)
            if mask is None:
                skipped.append({"annotation": ann_id, "reason": "annotation without segmentation"})
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
            exif_policy=exif_policy,
            exif_rotated=count_exif_rotated(samples),
        )


def _load_views(
    images: list[dict[str, Any]], images_dir: Path, exif_policy: str
) -> dict[int, tuple[str, View]]:
    views: dict[int, tuple[str, View]] = {}
    seen: set[str] = set()
    seen_ids: set[int] = set()
    missing: list[str] = []
    for im in images:
        rel = Path(str(im["file_name"])).as_posix()
        if rel in seen:
            raise ValidationFailed(f"duplicate file_name {rel!r} in COCO images")
        seen.add(rel)
        image_id = int(im["id"])
        if image_id in seen_ids:
            raise ValidationFailed(f"duplicate image id {image_id} in COCO images")
        seen_ids.add(image_id)
        path = images_dir / rel
        if not path.is_file():
            missing.append(rel)
            continue
        width, height, orientation = image_header(path)
        from_json = "width" in im and "height" in im
        if from_json:
            width, height = int(im["width"]), int(im["height"])
        meta: dict[str, Any] = {}
        if orientation is not None:
            meta["exif_orientation"] = orientation
            if exif_policy == "oriented" and not from_json and orientation in SWAPPED_ORIENTATIONS:
                width, height = height, width
        views[image_id] = (rel, View(path=rel, width=width, height=height, meta=meta))
    if missing:
        raise ValidationFailed(f"{len(missing)} images missing under {images_dir}: {missing[:5]}")
    return views
