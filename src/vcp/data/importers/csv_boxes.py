"""Generic "one row per box" CSV importer; images live in a directory next to the CSV."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vcp.core.errors import ValidationFailed
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.importers.common import (
    choice_option as _choice,
)
from vcp.data.importers.common import (
    count_exif_rotated,
    exif_policy_option,
    iter_images,
    load_categories,
    make_view,
    read_csv,
    rel_posix,
)
from vcp.data.schema import Box, Category, Labels, Sample, View

DEFAULT_COLUMNS = {
    "image": "image_filename",
    "label": "label_id",
    "x": "x",
    "y": "y",
    "w": "w",
    "h": "h",
}
BOX_FORMATS = ("xywh", "xyxy", "cxcywh")
COORD_MODES = ("abs", "norm")
BAD_ROW_MODES = ("abort", "skip")
BOUNDS_TOLERANCE_PX = 1.0


def to_abs_xywh(
    a: float,
    b: float,
    c: float,
    d: float,
    *,
    box_format: str,
    coords: str,
    width: int,
    height: int,
) -> tuple[float, float, float, float]:
    """Convert (a, b, c, d) in the given box format / coordinate mode to absolute xywh."""
    if coords == "norm":
        a, c = a * width, c * width
        b, d = b * height, d * height
    if box_format == "xyxy":
        return a, b, c - a, d - b
    if box_format == "cxcywh":
        return a - c / 2, b - d / 2, c, d
    return a, b, c, d


def box_problem(x: float, y: float, w: float, h: float, view: View) -> str | None:
    """Why this box is unusable, or None. Bounds are only checked when the view has a size."""
    if w <= 0 or h <= 0:
        return f"non-positive size w={w} h={h}"
    if view.width is None or view.height is None:
        return None
    tol = BOUNDS_TOLERANCE_PX
    if x < -tol or y < -tol or x + w > view.width + tol or y + h > view.height + tol:
        return f"box exceeds image bounds {view.width}x{view.height}"
    return None


class CsvBoxesImporter:
    name = "csv_boxes"
    version = "1"

    def run(self, spec: ImportSpec) -> ImportResult:
        opts = spec.options
        csv_path = spec.src / opts.get("csv", "labels.csv")
        images_dir = spec.src / opts.get("images", "images")
        cols = {k: opts.get(f"col_{k}", v) for k, v in DEFAULT_COLUMNS.items()}
        box_format = _choice(opts, "box_format", BOX_FORMATS, "xywh")
        coords = _choice(opts, "coords", COORD_MODES, "abs")
        on_bad_row = _choice(opts, "on_bad_row", BAD_ROW_MODES, "abort")
        exif_policy = exif_policy_option(opts)
        _, rows = read_csv(csv_path, required=cols.values())
        views = {
            rel: make_view(images_dir, rel, exif_policy=exif_policy)
            for rel in (rel_posix(p, images_dir) for p in iter_images(images_dir))
        }
        if not views:
            raise ValidationFailed(f"no images found under {images_dir}")
        boxes: dict[str, list[Box]] = {rel: [] for rel in views}
        label_ids: set[int] = set()
        skipped: list[dict[str, Any]] = []
        for lineno, row in enumerate(rows, start=2):
            problem = _parse_row(row, cols, views, box_format=box_format, coords=coords)
            if isinstance(problem, str):
                if on_bad_row == "abort":
                    raise ValidationFailed(problem, location=f"{csv_path.name}:{lineno}")
                skipped.append({"line": lineno, "reason": problem, "row": row})
                continue
            image, box = problem
            boxes[image].append(box)
            label_ids.add(box.category_id)
        categories = load_categories(opts.get("categories"), spec.src) or [
            Category(id=i, name=str(i)) for i in sorted(label_ids)
        ]
        samples = [
            Sample(
                sample_id=rel,
                views=[view],
                labels=Labels(boxes=boxes[rel]),
                label_source="gold",
            )
            for rel, view in views.items()
        ]
        return finalize_import(
            spec=spec,
            importer=self,
            task="det",
            categories=categories,
            image_root=str(images_dir),
            samples=samples,
            rows_read=len(rows),
            skipped=skipped,
            exif_policy=exif_policy,
            exif_rotated=count_exif_rotated(samples),
        )


def _parse_row(
    row: dict[str, str],
    cols: dict[str, str],
    views: dict[str, View],
    *,
    box_format: str,
    coords: str,
) -> tuple[str, Box] | str:
    """(image, Box) for a good row, or a problem description for a bad one."""
    image = Path(row[cols["image"]]).as_posix()
    view = views.get(image)
    if view is None:
        return f"unknown image {image!r}"
    try:
        label = int(row[cols["label"]])
        a, b, c, d = (float(row[cols[k]]) for k in ("x", "y", "w", "h"))
    except ValueError as e:
        return f"unparsable number: {e}"
    x, y, w, h = to_abs_xywh(
        a,
        b,
        c,
        d,
        box_format=box_format,
        coords=coords,
        width=view.width or 0,
        height=view.height or 0,
    )
    problem = box_problem(x, y, w, h, view)
    if problem:
        return problem
    return image, Box(x=x, y=y, w=w, h=h, category_id=label)
