"""Coordinate sanity: every box / polygon lies inside its view (postmortem error #2)."""

from __future__ import annotations

from pathlib import Path

from vcp.data.audit.base import AuditContext, CheckResult, write_jsonl
from vcp.data.dataset import Dataset
from vcp.data.importers.common import image_size
from vcp.data.schema import Box, Sample
from vcp.data.tasks import get_task

TOLERANCE_PX = 1.0


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


def _view_size(
    sample: Sample, index: int, image_root: Path, cache: dict[int, tuple[int, int]]
) -> tuple[int, int]:
    if index not in cache:
        v = sample.views[index]
        cache[index] = (
            (v.width, v.height)
            if v.width is not None and v.height is not None
            else image_size(image_root / v.path)
        )
    return cache[index]


class CoordsCheck:
    name = "coords"

    def applies(self, dataset: Dataset) -> bool:
        return get_task(dataset.card.task).label_field in ("boxes", "masks")

    def run(self, ctx: AuditContext) -> CheckResult:
        image_root = ctx.paths.resolve_image_root(ctx.dataset.card)
        bad: list[dict[str, object]] = []
        for s in ctx.dataset.samples:
            if s.labels is None:
                continue
            sizes: dict[int, tuple[int, int]] = {}
            for i, b in enumerate(s.labels.boxes or []):
                problems = box_problems(b, *_view_size(s, b.view, image_root, sizes))
                if problems:
                    bad.append(
                        {"sample_id": s.sample_id, "kind": "box", "index": i, "problems": problems}
                    )
            for i, m in enumerate(s.labels.masks or []):
                if m.polygon is None:
                    continue
                problems = polygon_problems(m.polygon, *_view_size(s, m.view, image_root, sizes))
                if problems:
                    bad.append(
                        {"sample_id": s.sample_id, "kind": "mask", "index": i, "problems": problems}
                    )
        report = write_jsonl(ctx.out_dir / "coords_bad.jsonl", bad)
        status = "FAIL" if len(bad) > ctx.opts.max_bad_boxes else "OK"
        fields = {"bad": len(bad), "max_bad": ctx.opts.max_bad_boxes, "report": str(report)}
        return CheckResult(status, fields, [f"coords: {len(bad)} bad annotations"])
