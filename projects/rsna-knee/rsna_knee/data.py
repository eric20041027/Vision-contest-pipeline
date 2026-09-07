"""One deterministic image transform for materialized training and raw notebook inference."""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import numpy as np

from vcp.core.errors import ValidationFailed
from vcp.data.dicomio import SliceHeader, read_headers, sort_slices
from vcp.data.importers.common import read_csv
from vcp.data.materialize.decoders.dicom import DicomDecoder
from vcp.data.materialize.window import resize_long_side, to_uint8
from vcp.data.schema import Sample, View
from vcp.train.reader import Record

NAMES = (
    "ACL",
    "MCL",
    "Medial Meniscus",
    "Lateral Meniscus",
    "Medial OA",
    "Lateral OA",
    "PF OA",
    "Effusion",
    "Synovitis",
    "Baker's",
    "Contusion",
    "Fracture",
)
PLANES = ("Sagittal", "Coronal", "Axial")
MODE_DIR = "png-r256"


def choose_views(sample: Sample) -> list[int | None]:
    groups: dict[str, list[int]] = defaultdict(list)
    for i, view in enumerate(sample.views):
        if view.seq_id is not None:
            groups[view.seq_id].append(i)
    meta = sample.meta.get("series", {})
    chosen: list[int | None] = []
    for plane in PLANES:
        candidates = [
            uid
            for uid in groups
            if str(meta.get(uid, {}).get("Anatomical_Plane", "")).casefold() == plane.casefold()
        ]
        if not candidates:
            chosen.extend([None] * 3)
            continue
        uid = min(
            candidates,
            key=lambda uid: (
                meta[uid].get("Fluid_Sensitive") != "1",
                meta[uid].get("Fat_Suppression") != "1",
                uid,
            ),
        )
        indices = sorted(
            groups[uid],
            key=lambda i: (
                sample.views[i].seq_index if sample.views[i].seq_index is not None else i,
                sample.views[i].path,
            ),
        )
        chosen.extend(indices[round((len(indices) - 1) * q)] for q in (0.25, 0.5, 0.75))
    if all(i is None for i in chosen):
        raise ValidationFailed(
            "missing_planes: no recognized sequence plane", fields={"sample": sample.sample_id}
        )
    return chosen


def study_tensor(record: Record, *, size: int = 256) -> np.ndarray:
    if size < 8:
        raise ValidationFailed("size: must be at least 8")
    out = np.zeros((9, size, size), dtype=np.float32)
    for channel, index in enumerate(choose_views(record.sample)):
        if index is None:
            continue
        key = str(index)
        if key not in record.arrays:
            raise ValidationFailed(
                "not_found: selected slice missing from cache",
                fields={"sample": record.sample_id, "view": index},
            )
        arr = record.arrays[key]
        if arr.ndim != 2 or arr.dtype != np.uint8:
            raise ValidationFailed("cache_format: expected uint8 grayscale PNG slices")
        if max(arr.shape) != size:
            arr = resize_long_side(arr, size)
        h, w = arr.shape
        top, left = (size - h) // 2, (size - w) // 2
        out[channel, top : top + h, left : left + w] = arr.astype(np.float32) / 255.0
    return out


def targets(sample: Sample) -> np.ndarray | None:
    if sample.label_source != "gold":
        return None
    values = sample.labels.targets if sample.labels is not None else None
    if values is None or set(values) != set(NAMES):
        raise ValidationFailed("targets: expected exactly the twelve competition labels")
    row = np.asarray([values[name] for name in NAMES], dtype=np.float32)
    if not np.isin(row, [0.0, 1.0]).all():
        raise ValidationFailed("targets: gold labels must be binary")
    return row


def validate_ids(ids: list[str]) -> None:
    if not ids or len(ids) != len(set(ids)) or any(not sid.strip() for sid in ids):
        raise ValidationFailed("ids: expected nonempty unique study IDs")


def test_ids(path: Path) -> list[str]:
    _, rows = read_csv(path, required=["StudyInstanceUID"])
    ids = [row["StudyInstanceUID"].strip() for row in rows]
    validate_ids(ids)
    return ids


def write_scores(out: Path, ids: list[str], scores: np.ndarray) -> None:
    validate_ids(ids)
    if (
        scores.shape != (len(ids), len(NAMES))
        or not np.isfinite(scores).all()
        or ((scores < 0) | (scores > 1)).any()
    ):
        raise ValidationFailed("scores: expected one finite twelve-probability row per study")
    if out.exists():
        raise ValidationFailed(f"exists: predictions {out}; choose a new output path")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["StudyInstanceUID", *NAMES])
        writer.writerows(
            [sid, *[float(x) for x in row]] for sid, row in zip(ids, scores, strict=True)
        )


def sequence_table(path: Path) -> dict[str, dict[str, str]]:
    _, rows = read_csv(
        path,
        required=["SeriesInstanceUID", "Anatomical_Plane", "Fluid_Sensitive", "Fat_Suppression"],
    )
    ids = [row["SeriesInstanceUID"] for row in rows]
    validate_ids(ids)
    return {row["SeriesInstanceUID"]: row for row in rows}


def raw_tensor(
    image_root: Path, study_id: str, series: dict[str, dict[str, str]], *, size: int = 256
) -> np.ndarray:
    root = image_root.resolve()
    folder = (root / study_id).resolve()
    if folder.parent != root or not folder.is_dir():
        raise ValidationFailed("not_found: study directory", fields={"sample": study_id})
    files = sorted(folder.rglob("*.dcm"))
    headers = read_headers(files, workers=1)
    if not headers or any(isinstance(h, str) for h in headers):
        raise ValidationFailed("dicom: missing or unreadable slices", fields={"sample": study_id})
    groups: dict[str, list[SliceHeader]] = defaultdict(list)
    for h in headers:
        if isinstance(h, SliceHeader):
            if h.study_uid != study_id:
                raise ValidationFailed("study_id: folder and DICOM UID differ")
            groups[h.series_uid].append(h)
    views = []
    for uid in sorted(groups):
        for i, h in enumerate(sort_slices(groups[uid])):
            views.append(View(path=h.path.relative_to(root).as_posix(), seq_id=uid, seq_index=i))
    sample = Sample(sample_id=study_id, views=views, label_source="none", meta={"series": series})
    decoder = DicomDecoder()
    arrays = {}
    for i in set(choose_views(sample)) - {None}:
        decoded = decoder.decode(root / sample.views[i].path)
        arrays[str(i)] = resize_long_side(to_uint8(decoded, "dicom"), 256)
    return study_tensor(Record(study_id, sample, None, arrays), size=size)
