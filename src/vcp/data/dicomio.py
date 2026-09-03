"""Header-only DICOM access shared by the ``dicom`` importer and the ``dicom`` decoder.

Grouping and ordering come from the header (StudyInstanceUID / SeriesInstanceUID /
InstanceNumber), never from folder names. Pixel data is never read here.
"""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vcp.core.errors import VcpError

REQUIRED_TAGS = ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID", "Rows", "Columns")
INSTALL_HINT = "DICOM support needs the 'dicom' extra: uv sync --extra dicom"


def require_pydicom() -> Any:
    try:
        import pydicom
    except ImportError as e:
        raise VcpError(INSTALL_HINT) from e
    return pydicom


@dataclass(frozen=True)
class SliceHeader:
    path: Path
    study_uid: str
    series_uid: str
    sop_uid: str
    rows: int
    columns: int
    instance_number: int | None
    position: tuple[float, float, float] | None
    orientation: tuple[float, ...] | None
    tags: dict[str, Any]


def jsonable(value: Any) -> Any:
    """pydicom values (MultiValue, DSfloat, IS, UID, PersonName) -> plain JSON-able Python."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, (list, tuple)) or type(value).__name__ == "MultiValue":
        return [jsonable(v) for v in value]
    return str(value)


def _optional_int(ds: Any, keyword: str) -> int | None:
    value = getattr(ds, keyword, None)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _floats(ds: Any, keyword: str, n: int) -> tuple[float, ...] | None:
    value = getattr(ds, keyword, None)
    if value is None:
        return None
    try:
        length = len(value)
    except TypeError:
        return None  # scalar (e.g. malformed VM=1 tag), not the n-length sequence we need
    if length != n:
        return None
    try:
        return tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return None


def read_header(path: Path, *, extra: Iterable[str] = ()) -> SliceHeader | str:
    """Parsed header, or a skip reason: ``not_dicom`` / ``missing_tag:<kw>`` / ``multiframe``."""
    pydicom = require_pydicom()
    try:
        ds = pydicom.dcmread(path, stop_before_pixels=True)
    except (pydicom.errors.InvalidDicomError, ValueError, OSError):
        return "not_dicom"
    for kw in REQUIRED_TAGS:
        if getattr(ds, kw, None) in (None, ""):
            return f"missing_tag:{kw}"
    if (_optional_int(ds, "NumberOfFrames") or 1) > 1:
        return "multiframe"
    tags: dict[str, Any] = {kw: jsonable(getattr(ds, kw)) for kw in extra if kw in ds}
    meta = getattr(ds, "file_meta", None)
    tags["TransferSyntaxUID"] = (
        str(meta.TransferSyntaxUID) if meta is not None and "TransferSyntaxUID" in meta else None
    )
    position = _floats(ds, "ImagePositionPatient", 3)
    return SliceHeader(
        path=path,
        study_uid=str(ds.StudyInstanceUID),
        series_uid=str(ds.SeriesInstanceUID),
        sop_uid=str(ds.SOPInstanceUID),
        rows=int(ds.Rows),
        columns=int(ds.Columns),
        instance_number=_optional_int(ds, "InstanceNumber"),
        position=position,  # type: ignore[arg-type]
        orientation=_floats(ds, "ImageOrientationPatient", 6),
        tags=tags,
    )


def read_headers(
    paths: list[Path], *, extra: Iterable[str] = (), workers: int = 4
) -> list[SliceHeader | str]:
    wanted = tuple(extra)
    if workers <= 1 or len(paths) < 2:
        return [read_header(p, extra=wanted) for p in paths]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda p: read_header(p, extra=wanted), paths))


def _normal(orientation: tuple[float, ...]) -> tuple[float, float, float]:
    r, c = orientation[:3], orientation[3:]
    return (r[1] * c[2] - r[2] * c[1], r[2] * c[0] - r[0] * c[2], r[0] * c[1] - r[1] * c[0])


def sort_slices(headers: list[SliceHeader]) -> list[SliceHeader]:
    """InstanceNumber when every slice has one; else position projected on the slice normal
    when every slice has one; else file name. Ties always break on file name."""
    if headers and all(h.instance_number is not None for h in headers):
        return sorted(headers, key=lambda h: (h.instance_number, h.path.name))
    if headers and all(h.position is not None for h in headers):

        def projected(h: SliceHeader) -> float:
            normal = _normal(h.orientation) if h.orientation else (0.0, 0.0, 1.0)
            return sum(p * n for p, n in zip(h.position or (), normal, strict=True))

        return sorted(headers, key=lambda h: (projected(h), h.path.name))
    return sorted(headers, key=lambda h: h.path.name)
