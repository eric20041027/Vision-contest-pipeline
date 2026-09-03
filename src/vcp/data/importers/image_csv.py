"""CSV of image paths + label / target columns -> cls, multilabel or regression dataset."""

from __future__ import annotations

import re
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.importers.common import count_exif_rotated, exif_policy_option, make_view, read_csv
from vcp.data.schema import Category, Labels, Sample

TASKS = ("cls", "multilabel", "regression")
_INT = re.compile(r"^-?\d+$")
_TRUE = {"1", "true", "yes"}


def infer_task(rows: list[dict[str, str]], cols: list[str]) -> str:
    values = [row[c].strip() for row in rows for c in cols]
    if len(cols) == 1 and all(_INT.match(v) for v in values):
        return "cls"
    if all(v in ("0", "1", "0.0", "1.0") for v in values):
        return "multilabel"
    return "regression"


def _cls_categories(values: list[str]) -> list[Category]:
    distinct = sorted(set(values), key=lambda v: (0, int(v), v) if _INT.match(v) else (1, v))
    return [Category(id=i, name=v) for i, v in enumerate(distinct)]


class ImageCsvImporter:
    name = "image_csv"
    version = "1"

    def run(self, spec: ImportSpec) -> ImportResult:
        opts = spec.options
        csv_path = spec.src / opts.get("csv", "labels.csv")
        images_dir = spec.src / opts.get("images", "images")
        path_col = opts.get("path_col", "path")
        gold_col = opts.get("gold_col")
        header, rows = read_csv(csv_path, required=[path_col, *([gold_col] if gold_col else [])])
        if not rows:
            raise ValidationFailed(f"{csv_path.name} has no data rows")
        if "target_cols" in opts:
            target_cols = [c.strip() for c in opts["target_cols"].split(",") if c.strip()]
        else:
            target_cols = [c for c in header if c not in (path_col, gold_col)]
        if not target_cols:
            raise ValidationFailed("no target columns: pass --opt target_cols=a,b,...")
        missing = [c for c in target_cols if c not in header]
        if missing:
            raise ValidationFailed(f"target columns {missing} not in CSV header {header}")
        task = opts.get("task") or infer_task(rows, target_cols)
        if task not in TASKS:
            raise ValidationFailed(f"--opt task= must be one of {TASKS}, got {task!r}")
        if task == "cls" and len(target_cols) != 1:
            raise ValidationFailed(f"task cls needs exactly one target column, got {target_cols}")
        categories = (
            _cls_categories([row[target_cols[0]].strip() for row in rows])
            if task == "cls"
            else [Category(id=i, name=c) for i, c in enumerate(target_cols)]
        )
        cls_index = {c.name: c.id for c in categories}
        meta_cols = [c for c in header if c not in (path_col, gold_col, *target_cols)]
        exif_policy = exif_policy_option(opts)
        samples: list[Sample] = []
        for lineno, row in enumerate(rows, start=2):
            rel = Path(row[path_col].strip()).as_posix()
            view = make_view(images_dir, rel, exif_policy=exif_policy)
            labels = _labels_for(row, task, target_cols, cls_index, csv_path, lineno)
            gold = gold_col is None or row[gold_col].strip().lower() in _TRUE
            samples.append(
                Sample(
                    sample_id=rel,
                    views=[view],
                    labels=labels,
                    label_source="gold" if gold else "derived",
                    meta={c: row[c] for c in meta_cols},
                )
            )
        return finalize_import(
            spec=spec,
            importer=self,
            task=task,
            categories=categories,
            image_root=str(images_dir),
            samples=samples,
            rows_read=len(rows),
            skipped=[],
            exif_policy=exif_policy,
            exif_rotated=count_exif_rotated(samples),
        )


def _labels_for(
    row: dict[str, str],
    task: str,
    target_cols: list[str],
    cls_index: dict[str, int],
    csv_path: Path,
    lineno: int,
) -> Labels:
    if task == "cls":
        return Labels(cls=cls_index[row[target_cols[0]].strip()])
    try:
        targets = {c: float(row[c]) for c in target_cols}
    except ValueError as e:
        raise ValidationFailed(
            f"unparsable target: {e}", location=f"{csv_path.name}:{lineno}"
        ) from e
    return Labels(targets=targets)
