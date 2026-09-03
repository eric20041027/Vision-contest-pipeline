"""YOLO layout importer: images/, labels/<same relpath>.txt with normalised cxcywh rows."""

from __future__ import annotations

from pathlib import Path

import yaml

from vcp.core.errors import ValidationFailed
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.importers.common import (
    count_exif_rotated,
    exif_policy_option,
    iter_images,
    make_view,
    rel_posix,
)
from vcp.data.schema import Box, Category, Labels, Sample, View


def load_names(path: Path) -> list[Category]:
    """classes.txt (one name per line) or a data.yaml whose ``names`` is a list or {id: name}."""
    if not path.is_file():
        raise ValidationFailed(f"names file not found: {path}")
    if path.suffix.lower() in (".yaml", ".yml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(doc, dict):
            raise ValidationFailed(
                f"{path.name}: expected a YAML mapping at top level", location=str(path)
            )
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
            Box(
                x=(cx - w / 2) * width,
                y=(cy - h / 2) * height,
                w=w * width,
                h=h * height,
                category_id=cls,
            )
        )
    return boxes


class YoloImporter:
    name = "yolo"
    version = "1"

    def run(self, spec: ImportSpec) -> ImportResult:
        opts = spec.options
        images_dir = spec.src / opts.get("images", "images")
        labels_dir = spec.src / opts.get("labels", "labels")
        if not labels_dir.is_dir():
            raise ValidationFailed(f"labels directory not found: {labels_dir}")
        categories = load_names(spec.src / opts.get("names", "classes.txt"))
        known = {c.id for c in categories}
        exif_policy = exif_policy_option(opts)
        samples: list[Sample] = []
        unlabeled = 0
        for p in iter_images(images_dir):
            rel = rel_posix(p, images_dir)
            view = make_view(images_dir, rel, exif_policy=exif_policy)
            label_file = labels_dir / Path(rel).with_suffix(".txt")
            if label_file.is_file():
                boxes = _parse_label_file(label_file, view, known)
            else:
                unlabeled += 1
                boxes = []
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
            unlabeled=unlabeled,
            exif_policy=exif_policy,
            exif_rotated=count_exif_rotated(samples),
        )
