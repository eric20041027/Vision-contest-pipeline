"""DICOM directory tree -> one sample per study (or series); one view per slice (or per series).

Grouping and order come from headers (StudyInstanceUID / SeriesInstanceUID / InstanceNumber),
never from folder names. Labels join an optional CSV on the sample id; series attributes join an
optional CSV on SeriesInstanceUID. Nothing here knows any particular contest's column names.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vcp.core.errors import ValidationFailed
from vcp.data.dicomio import SliceHeader, read_headers, sort_slices
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.importers.common import choice_option, exif_policy_option, read_csv, rel_posix
from vcp.data.importers.image_csv import infer_task
from vcp.data.schema import Category, Labels, Sample, View

SAMPLE_LEVELS = ("study", "series")
VIEW_LEVELS = ("slice", "series")
TASKS = ("multilabel", "regression")
SUMMARY_TAGS = ("SeriesDescription", "SeriesNumber", "Modality", "PixelSpacing", "SliceThickness")
SLICE_TAGS = ("SliceLocation",)
DEFAULT_ID_COLS = {"study": "StudyInstanceUID", "series": "SeriesInstanceUID"}
DEFAULT_GROUP_TAG = "PatientID"
Skipped = list[dict[str, Any]]


def split_list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def workers_option(opts: dict[str, str]) -> int:
    value = opts.get("workers", "4")
    try:
        workers = int(value)
    except ValueError:
        workers = 0
    if workers < 1:
        raise ValidationFailed(f"--opt workers= must be a positive integer, got {value!r}")
    return workers


def find_files(src: Path, pattern: str) -> list[Path]:
    """Files matching ``pattern`` under ``src``; a ``.dcm`` pattern matches any suffix case."""
    if pattern.lower().endswith(".dcm"):
        found = (p for p in src.glob(pattern[: -len(".dcm")] + ".*") if p.suffix.lower() == ".dcm")
    else:
        found = src.glob(pattern)
    return sorted((p for p in found if p.is_file()), key=lambda p: rel_posix(p, src))


def _csv_path(src: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else src / path


@dataclass(frozen=True)
class LabelTable:
    rows: dict[str, dict[str, str]]
    id_col: str
    target_cols: list[str]
    meta_cols: list[str]
    task: str


def load_labels(src: Path, opts: dict[str, str], sample_level: str) -> LabelTable | None:
    if "labels_csv" not in opts:
        return None
    path = _csv_path(src, opts["labels_csv"])
    id_col = opts.get("id_col", DEFAULT_ID_COLS[sample_level])
    target_cols = split_list(opts.get("target_cols"))
    if not target_cols:
        raise ValidationFailed("labels_csv needs --opt target_cols=a,b,... (the label columns)")
    meta_cols = split_list(opts.get("meta_cols"))
    if "series" in meta_cols:
        raise ValidationFailed(
            "meta_cols cannot include 'series' (reserved for the per-series summary)"
        )
    _, rows = read_csv(path, required=[id_col, *target_cols, *meta_cols])
    table: dict[str, dict[str, str]] = {}
    for lineno, row in enumerate(rows, start=2):
        key = row[id_col].strip()
        if key in table:
            raise ValidationFailed(f"duplicate id {key!r}", location=f"{path.name}:{lineno}")
        table[key] = row
    labeled = [r for r in rows if all(r[c].strip() for c in target_cols)]
    task = opts.get("task")
    if not task:
        guess = infer_task(labeled, target_cols) if labeled else "multilabel"
        binary = all(r[c].strip() in ("0", "1") for r in labeled for c in target_cols)
        task = guess if guess in TASKS else ("multilabel" if binary else "regression")
    if task not in TASKS:
        raise ValidationFailed(f"--opt task= must be one of {TASKS}, got {task!r}")
    return LabelTable(table, id_col, target_cols, meta_cols, task)


def labels_for(row: dict[str, str], table: LabelTable) -> tuple[Labels | None, str, str | None]:
    """(labels, label_source, skip_reason): all targets present -> gold; none -> unlabeled;
    some -> skipped as partial_targets."""
    values = [row[c].strip() for c in table.target_cols]
    if all(values):
        try:
            targets = {c: float(v) for c, v in zip(table.target_cols, values, strict=True)}
        except ValueError as e:
            raise ValidationFailed(f"unparsable target: {e}", location=row[table.id_col]) from e
        return Labels(targets=targets), "gold", None
    if not any(values):
        return None, "none", None
    return None, "none", "partial_targets"


def load_seq_csv(src: Path, opts: dict[str, str]) -> dict[str, dict[str, str]]:
    if "seq_csv" not in opts:
        return {}
    path = _csv_path(src, opts["seq_csv"])
    id_col = opts.get("seq_id_col", "SeriesInstanceUID")
    cols = split_list(opts.get("seq_cols"))
    header, rows = read_csv(path, required=[id_col, *cols])
    use = cols or [c for c in header if c != id_col]
    table: dict[str, dict[str, str]] = {}
    for lineno, row in enumerate(rows, start=2):
        key = row[id_col].strip()
        if key in table:
            raise ValidationFailed(f"duplicate id {key!r}", location=f"{path.name}:{lineno}")
        table[key] = {c: row[c] for c in use}
    return table


def group_units(
    series: dict[str, list[SliceHeader]], sample_level: str
) -> dict[str, list[list[SliceHeader]]]:
    """sample id -> its series (each sorted by slice order), ordered by (SeriesNumber, uid)."""
    ordered = {uid: sort_slices(slices) for uid, slices in series.items()}

    def series_key(uid: str) -> tuple[int, int, str]:
        number = ordered[uid][0].tags.get("SeriesNumber")
        return (0, number, uid) if isinstance(number, int) else (1, 0, uid)

    units: dict[str, list[list[SliceHeader]]] = defaultdict(list)
    for uid in sorted(ordered, key=series_key):
        key = ordered[uid][0].study_uid if sample_level == "study" else uid
        units[key].append(ordered[uid])
    return units


def _series_summary(
    slices: list[SliceHeader], extra_tags: list[str], seq_row: dict[str, str]
) -> dict[str, Any]:
    first = slices[0]
    return {
        "description": first.tags.get("SeriesDescription"),
        "number": first.tags.get("SeriesNumber"),
        "n_slices": len(slices),
        "modality": first.tags.get("Modality"),
        "rows": first.rows,
        "columns": first.columns,
        "pixel_spacing": first.tags.get("PixelSpacing"),
        "slice_thickness": first.tags.get("SliceThickness"),
        "transfer_syntax": first.tags.get("TransferSyntaxUID"),
        **{kw: first.tags[kw] for kw in extra_tags if kw in first.tags},
        **seq_row,
    }


def _role(first: SliceHeader, role_from: str | None, seq_row: dict[str, str]) -> str | None:
    if not role_from:
        return None
    value = seq_row.get(role_from, first.tags.get(role_from))
    return None if value in (None, "") else str(value)


def build_views(
    sample_id: str,
    series_list: list[list[SliceHeader]],
    src: Path,
    *,
    view_level: str,
    role_from: str | None,
    seq_meta: dict[str, dict[str, str]],
    extra_tags: list[str],
    skipped: Skipped,
) -> tuple[list[View], dict[str, Any], int]:
    """(views, series summary, inconsistent_series count)."""
    views: list[View] = []
    summary: dict[str, Any] = {}
    inconsistent = 0
    for slices in series_list:
        first = slices[0]
        uid = first.series_uid
        seq_row = seq_meta.get(uid, {})
        role = _role(first, role_from, seq_row)
        if view_level == "slice":
            summary[uid] = _series_summary(slices, extra_tags, seq_row)
            for i, h in enumerate(slices):
                meta: dict[str, Any] = {}
                if h.instance_number is not None:
                    meta["instance_number"] = h.instance_number
                if h.tags.get("SliceLocation") is not None:
                    meta["slice_location"] = h.tags["SliceLocation"]
                views.append(
                    View(
                        path=rel_posix(h.path, src),
                        width=h.columns,
                        height=h.rows,
                        role=role,
                        seq_id=uid,
                        seq_index=i,
                        meta=meta,
                    )
                )
            continue
        dirs = {h.path.parent for h in slices}
        if len(dirs) != 1:
            skipped.append({"id": sample_id, "series": uid, "reason": "series_split_across_dirs"})
            continue
        summary[uid] = _series_summary(slices, extra_tags, seq_row)
        sizes = {(h.columns, h.rows) for h in slices}
        meta = {"n_slices": len(slices)}
        width, height = (first.columns, first.rows) if len(sizes) == 1 else (None, None)
        if len(sizes) != 1:
            meta["inconsistent_size"] = True
            inconsistent += 1
        views.append(
            View(
                path=rel_posix(dirs.pop(), src),
                width=width,
                height=height,
                role=role,
                seq_id=uid,
                seq_index=None,
                meta=meta,
            )
        )
    return views, summary, inconsistent


class DicomImporter:
    name = "dicom"
    version = "1"

    def run(self, spec: ImportSpec) -> ImportResult:
        opts = spec.options
        sample_level = choice_option(opts, "sample_level", SAMPLE_LEVELS, "study")
        view_level = choice_option(opts, "view_level", VIEW_LEVELS, "slice")
        exif_policy = exif_policy_option(opts)
        pattern = opts.get("glob", "**/*.dcm")
        workers = workers_option(opts)
        extra_tags = split_list(opts.get("tags"))
        group_from = opts.get("group_from", DEFAULT_GROUP_TAG)
        role_from = opts.get("role_from")
        # Validate the CSV-join options before paying for the (threaded, but O(n)) header scan:
        # a typo in --opt target_cols= should fail fast, not after reading every header on disk.
        seq_meta = load_seq_csv(spec.src, opts)
        table = load_labels(spec.src, opts, sample_level)
        files = find_files(spec.src, pattern)
        if not files:
            raise ValidationFailed(f"no files match {pattern!r} under {spec.src}")
        wanted = [*SUMMARY_TAGS, *SLICE_TAGS, *extra_tags]
        if group_from != "none":
            wanted.append(group_from)
        if role_from:
            wanted.append(role_from)
        headers = read_headers(files, extra=wanted, workers=workers)
        skipped: Skipped = []
        series: dict[str, list[SliceHeader]] = defaultdict(list)
        for path, h in zip(files, headers, strict=True):
            if isinstance(h, str):
                skipped.append({"file": rel_posix(path, spec.src), "reason": h})
                continue
            series[h.series_uid].append(h)
        units = group_units(series, sample_level)
        samples: list[Sample] = []
        unlabeled = inconsistent = 0
        for sample_id in sorted(units):
            series_list = units[sample_id]
            views, summary, bad = build_views(
                sample_id,
                series_list,
                spec.src,
                view_level=view_level,
                role_from=role_from,
                seq_meta=seq_meta,
                extra_tags=extra_tags,
                skipped=skipped,
            )
            inconsistent += bad
            if not views:
                continue
            row = table.rows.get(sample_id) if table else None
            labels, source, reason = (
                labels_for(row, table) if table and row is not None else (None, "none", None)
            )
            if reason:
                skipped.append({"id": sample_id, "reason": reason})
                continue
            if source == "none":
                unlabeled += 1
            meta: dict[str, Any] = {"series": summary}
            if table and row is not None:
                meta.update({c: row[c] for c in table.meta_cols})
            first = series_list[0][0]
            group = None
            if group_from != "none" and first.tags.get(group_from) not in (None, ""):
                group = str(first.tags[group_from])
            samples.append(
                Sample(
                    sample_id=sample_id,
                    views=views,
                    labels=labels,
                    label_source=source,
                    group=group,
                    meta=meta,
                )
            )
        if table:
            for key in sorted(set(table.rows) - set(units)):
                skipped.append({"id": key, "reason": "no_files"})
            task = table.task
            categories = [Category(id=i, name=c) for i, c in enumerate(table.target_cols)]
        else:
            task, categories = choice_option(opts, "task", TASKS, "multilabel"), []
        return finalize_import(
            spec=spec,
            importer=self,
            task=task,
            categories=categories,
            image_root=str(spec.src),
            samples=samples,
            rows_read=len(files),
            skipped=skipped,
            unlabeled=unlabeled,
            exif_policy=exif_policy,
            extra_fields={"inconsistent_series": inconsistent} if inconsistent else None,
        )
