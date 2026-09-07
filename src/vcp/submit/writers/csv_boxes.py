"""One row per box, ``image,label,x,y,w,h,score``: the mirror of the ``csv_boxes`` importer
(spec 9.5)."""

from __future__ import annotations

import csv

from vcp.core.errors import ValidationFailed
from vcp.data.schema import Sample
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import PredBox, Prediction
from vcp.submit.writers.base import (
    WriteContext,
    WriteResult,
    check_header,
    check_options,
    fmt_float,
    output_ids,
    parse_columns,
    sorted_samples,
)

DEFAULT_COLUMNS = {
    "image": "image_filename",
    "label": "label_id",
    "x": "x",
    "y": "y",
    "w": "w",
    "h": "h",
    "score": "score",
}
ORDER = ("image", "label", "x", "y", "w", "h", "score")
BOX_FORMATS = ("xywh", "xyxy", "cxcywh")
COORD_MODES = ("abs", "norm")
LABEL_MODES = ("category_id", "category_name")


def from_abs_xywh(
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    box_format: str,
    coords: str,
    width: float,
    height: float,
) -> tuple[float, float, float, float]:
    """Inverse of the importer's ``to_abs_xywh``: absolute xywh -> the requested convention."""
    if coords == "norm":
        x, w = x / width, w / width
        y, h = y / height, h / height
    if box_format == "xyxy":
        return x, y, x + w, y + h
    if box_format == "cxcywh":
        return x + w / 2, y + h / 2, w, h
    return x, y, w, h


def _choice(options: dict[str, str], key: str, allowed: tuple[str, ...]) -> str:
    value = options.get(key, allowed[0])
    if value not in allowed:
        raise ValidationFailed(
            f"option={key}: must be one of {allowed}, got {value!r}", fields={"option": key}
        )
    return value


class CsvBoxesWriter:
    name = "csv_boxes"
    version = "1"
    payloads = frozenset({"boxes"})
    file_name = "submission.csv"
    options = frozenset({"id_field", "columns", "box_format", "coords", "label"})

    def write(self, preds: list[Prediction], ctx: WriteContext) -> WriteResult:
        check_options(ctx.options, self.options)
        box_format = _choice(ctx.options, "box_format", BOX_FORMATS)
        coords = _choice(ctx.options, "coords", COORD_MODES)
        label = _choice(ctx.options, "label", LABEL_MODES)
        columns = {**DEFAULT_COLUMNS, **parse_columns(ctx.options.get("columns"))}
        unknown = sorted(set(columns) - set(ORDER))
        if unknown:
            raise ValidationFailed(
                f"option=columns: unknown column keys {unknown}; known: {list(ORDER)}",
                fields={"option": "columns"},
            )
        header = [columns[k] for k in ORDER]
        check_header(header)
        ids = output_ids(ctx.samples, ctx.options)
        names = {c.id: c.name for c in ctx.dataset.card.categories}
        by_id = predictions_by_id(preds)
        rows = samples = 0
        ctx.out.parent.mkdir(parents=True, exist_ok=True)
        with ctx.out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(header)
            for s in sorted_samples(ctx.samples):
                p = by_id.get(s.sample_id)
                boxes = (p.boxes or []) if p is not None else []
                if not boxes:
                    continue
                samples += 1
                for b in boxes:
                    writer.writerow(
                        [
                            ids[s.sample_id],
                            _label(b, label, names, s),
                            *_geometry(b, s, box_format, coords),
                            fmt_float(b.score),
                        ]
                    )
                    rows += 1
        return WriteResult(rows=rows, samples=samples, missing=[])


def _label(b: PredBox, mode: str, names: dict[int, str], s: Sample) -> int | str:
    if mode == "category_id":
        return b.category_id
    if b.category_id not in names:
        raise ValidationFailed(
            f"sample {s.sample_id!r}: category id {b.category_id} has no name in the card",
            fields={"sample": s.sample_id},
        )
    return names[b.category_id]


def _geometry(b: PredBox, s: Sample, box_format: str, coords: str) -> list[str]:
    width = height = 1.0
    if coords == "norm":
        if b.view >= len(s.views):
            raise ValidationFailed(
                f"sample {s.sample_id!r} has no view {b.view}", fields={"sample": s.sample_id}
            )
        view = s.views[b.view]
        if view.width is None or view.height is None:
            raise ValidationFailed(
                f"sample {s.sample_id!r}: coords=norm needs the view's width/height",
                fields={"sample": s.sample_id},
            )
        width, height = float(view.width), float(view.height)
    values = from_abs_xywh(
        b.x, b.y, b.w, b.h, box_format=box_format, coords=coords, width=width, height=height
    )
    return [fmt_float(v) for v in values]
