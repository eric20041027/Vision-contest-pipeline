"""Coordinate sanity beyond what load-time validation already guarantees (spec §15.2).

Validation rejects out-of-bounds boxes on views that carry a size, so this check reports
(a) legal-but-suspicious boxes on sized views, (b) out-of-bounds boxes / polygon vertices where
validation could not see them (unsized views; polygons are never bounds-checked at load) and
(c) the rows the importer refused, merged from ``cache/import_skipped.jsonl``.
"""

from __future__ import annotations

import json
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.data.audit.base import AuditContext, AuditOptions, CheckResult, write_jsonl
from vcp.data.dataset import Dataset
from vcp.data.importers.common import SWAPPED_ORIENTATIONS, image_header
from vcp.data.schema import Box, Sample
from vcp.data.tasks import get_task

TOLERANCE_PX = 1.0
Row = dict[str, object]


def box_problems(box: Box, width: int, height: int) -> list[str]:
    problems: list[str] = []
    if box.w <= 0 or box.h <= 0:
        problems.append("non-positive size")
    if box.x < -TOLERANCE_PX or box.y < -TOLERANCE_PX:
        problems.append("negative origin")
    if box.x + box.w > width + TOLERANCE_PX or box.y + box.h > height + TOLERANCE_PX:
        problems.append(f"exceeds {width}x{height}")
    return problems


def polygon_problems(polygon: list[list[float]], width: int, height: int) -> list[str]:
    problems: list[str] = []
    for ring in polygon:
        if len(ring) < 6 or len(ring) % 2:
            problems.append("degenerate ring")
            continue
        xs, ys = ring[0::2], ring[1::2]
        if (
            min(xs) < -TOLERANCE_PX
            or min(ys) < -TOLERANCE_PX
            or max(xs) > width + TOLERANCE_PX
            or max(ys) > height + TOLERANCE_PX
        ):
            problems.append(f"vertex outside {width}x{height}")
    return problems


def suspicious_problems(box: Box, width: int, height: int, opts: AuditOptions) -> list[str]:
    """Legal boxes that usually mean a labelling or unit mistake."""
    problems: list[str] = []
    if box.w < opts.min_box_px or box.h < opts.min_box_px:
        problems.append("tiny")
    if box.w > 0 and box.h > 0 and max(box.w / box.h, box.h / box.w) > opts.max_aspect:
        problems.append("aspect")
    if box.w * box.h >= opts.max_cover * width * height:
        problems.append("cover")
    return problems


def read_import_skipped(path: Path) -> list[Row]:
    """Rows refused at import time, tagged ``kind=import_skipped``; [] when the file is absent."""
    if not path.is_file():
        return []
    rows: list[Row] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValidationFailed(
                    f"bad import_skipped row: {e}", location=f"{path}:{lineno}"
                ) from e
            rows.append({"kind": "import_skipped", **row})
    return rows


def _view_size(
    sample: Sample,
    index: int,
    image_root: Path,
    exif_policy: str,
    cache: dict[int, tuple[int, int] | None],
) -> tuple[int, int] | None:
    """Declared size, header size for unsized views, or None when the file cannot be read.

    An unsized view is sized the same way ``common.make_view`` sizes it at import time: under
    ``exif_policy == "oriented"`` a header orientation in ``SWAPPED_ORIENTATIONS`` swaps
    width/height, so the audit judges bounds in whatever frame the dataset's policy uses.
    """
    if index not in cache:
        v = sample.views[index]
        if v.width is not None and v.height is not None:
            cache[index] = (v.width, v.height)
        else:
            try:
                width, height, orientation = image_header(image_root / v.path)
            except ValidationFailed:
                cache[index] = None
            else:
                if exif_policy == "oriented" and orientation in SWAPPED_ORIENTATIONS:
                    width, height = height, width
                cache[index] = (width, height)
    return cache[index]


def _check_boxes(
    s: Sample,
    image_root: Path,
    exif_policy: str,
    opts: AuditOptions,
    rows: list[Row],
    counts: dict[str, int],
    sizes: dict[int, tuple[int, int] | None],
) -> None:
    """Bounds before duplicates: a duplicated out-of-bounds box is two bad boxes, not one."""
    boxes = (s.labels.boxes if s.labels else None) or []
    seen: dict[tuple[int, int, float, float, float, float], int] = {}
    for i, b in enumerate(boxes):
        size = _view_size(s, b.view, image_root, exif_policy, sizes)
        if size is None:
            counts["unsized"] += 1
            rows.append({"sample_id": s.sample_id, "kind": "unsized", "index": i, "view": b.view})
            continue
        view = s.views[b.view]
        if view.width is None or view.height is None:  # validation never saw these bounds
            problems = box_problems(b, *size)
            if problems:
                counts["out_of_bounds"] += 1
                rows.append(
                    {
                        "sample_id": s.sample_id,
                        "kind": "out_of_bounds",
                        "index": i,
                        "problems": problems,
                    }
                )
                continue
        key = (b.view, b.category_id, b.x, b.y, b.w, b.h)
        if key in seen:
            counts["suspicious"] += 1
            rows.append(
                {
                    "sample_id": s.sample_id,
                    "kind": "suspicious",
                    "index": i,
                    "problems": [f"duplicate of box {seen[key]}"],
                }
            )
            continue
        seen[key] = i
        problems = suspicious_problems(b, *size, opts)
        if problems:
            counts["suspicious"] += 1
            rows.append(
                {"sample_id": s.sample_id, "kind": "suspicious", "index": i, "problems": problems}
            )


def _check_polygons(
    s: Sample,
    image_root: Path,
    exif_policy: str,
    rows: list[Row],
    counts: dict[str, int],
    sizes: dict[int, tuple[int, int] | None],
) -> None:
    masks = (s.labels.masks if s.labels else None) or []
    for i, m in enumerate(masks):
        if m.polygon is None:
            continue
        size = _view_size(s, m.view, image_root, exif_policy, sizes)
        if size is None:
            counts["unsized"] += 1
            rows.append({"sample_id": s.sample_id, "kind": "unsized", "index": i, "view": m.view})
            continue
        problems = polygon_problems(m.polygon, *size)
        if problems:
            counts["out_of_bounds"] += 1
            rows.append(
                {
                    "sample_id": s.sample_id,
                    "kind": "out_of_bounds",
                    "index": i,
                    "mask": True,
                    "problems": problems,
                }
            )


class CoordsCheck:
    name = "coords"

    def applies(self, dataset: Dataset) -> bool:
        return get_task(dataset.card.task).label_field in ("boxes", "masks")

    def run(self, ctx: AuditContext) -> CheckResult:
        image_root = ctx.paths.resolve_image_root(ctx.dataset.card)
        exif_policy = ctx.dataset.card.exif_policy
        rows: list[Row] = []
        counts = {"suspicious": 0, "out_of_bounds": 0, "unsized": 0}
        for s in ctx.dataset.samples:
            if s.labels is None:
                continue
            sizes: dict[int, tuple[int, int] | None] = {}
            _check_boxes(s, image_root, exif_policy, ctx.opts, rows, counts, sizes)
            _check_polygons(s, image_root, exif_policy, rows, counts, sizes)
        skipped = read_import_skipped(ctx.paths.cache_dir / "import_skipped.jsonl")
        rows.extend(skipped)
        report = write_jsonl(ctx.out_dir / "coords_bad.jsonl", rows)
        bad = counts["out_of_bounds"] + len(skipped)
        if bad > ctx.opts.max_bad_boxes:
            status = "FAIL"
        elif counts["suspicious"] or counts["unsized"]:
            status = "WARN"
        else:
            status = "OK"
        fields = {
            "suspicious": counts["suspicious"],
            "out_of_bounds": counts["out_of_bounds"],
            "import_skipped": len(skipped),
            "unsized": counts["unsized"],
            "max_bad": ctx.opts.max_bad_boxes,
            "report": str(report),
        }
        human = [
            f"coords: {counts['suspicious']} suspicious, {counts['out_of_bounds']} out of bounds, "
            f"{len(skipped)} rows refused at import, {counts['unsized']} unsized"
        ]
        return CheckResult(status, fields, human)
