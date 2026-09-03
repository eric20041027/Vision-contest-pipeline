"""pydicom-backed decoder: rescaled pixels (int16/uint16 when exact) and window/photometric info."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from vcp.core.errors import ValidationFailed
from vcp.data.dicomio import SliceHeader, read_header, require_pydicom, sort_slices
from vcp.data.materialize.decoders.base import Decoded


def _first_float(value: Any) -> float | None:
    if value is None:
        return None
    is_seq = isinstance(value, (list, tuple)) or type(value).__name__ == "MultiValue"
    try:
        return float(value[0] if is_seq else value)
    except (TypeError, ValueError, IndexError):
        return None


def rescale(arr: np.ndarray, slope: float, intercept: float, signed: bool) -> np.ndarray:
    """Integer-exact rescale keeps 16-bit ints; anything else becomes float32."""
    if slope.is_integer() and intercept.is_integer():
        out = arr.astype(np.int64) * int(slope) + int(intercept)
        if out.min() >= 0 and out.max() <= np.iinfo(np.uint16).max and not signed:
            return out.astype(np.uint16)
        if out.min() >= np.iinfo(np.int16).min and out.max() <= np.iinfo(np.int16).max:
            return out.astype(np.int16)
        return out.astype(np.int32)
    return arr.astype(np.float32) * np.float32(slope) + np.float32(intercept)


class DicomDecoder:
    name = "dicom"
    version = "1"

    def decode(self, path: Path, *, exif_policy: str = "stored") -> Decoded:
        pydicom = require_pydicom()
        ds = pydicom.dcmread(path)
        arr = ds.pixel_array
        slope = _first_float(getattr(ds, "RescaleSlope", None)) or 1.0
        intercept = _first_float(getattr(ds, "RescaleIntercept", None)) or 0.0
        signed = int(getattr(ds, "PixelRepresentation", 0) or 0) == 1 or intercept < 0
        info = {
            "window_center": _first_float(getattr(ds, "WindowCenter", None)),
            "window_width": _first_float(getattr(ds, "WindowWidth", None)),
            "photometric": str(getattr(ds, "PhotometricInterpretation", "MONOCHROME2")),
            "transfer_syntax": str(ds.file_meta.TransferSyntaxUID),
            "rescale": [slope, intercept],
        }
        return Decoded(rescale(arr, slope, intercept, signed), info)

    def decode_series(self, paths: list[Path]) -> Decoded:
        headers = [read_header(p) for p in paths]
        bad = [f"{p.name}: {h}" for p, h in zip(paths, headers, strict=True) if isinstance(h, str)]
        if bad:
            raise ValidationFailed(f"series contains unreadable slices: {bad[:3]}")
        ordered = sort_slices([h for h in headers if isinstance(h, SliceHeader)])
        frames = [self.decode(h.path) for h in ordered]
        shapes = {f.array.shape for f in frames}
        if len(shapes) != 1:
            raise ValidationFailed(f"series slices differ in shape: {sorted(shapes)}")
        return Decoded(
            np.stack([f.array for f in frames]), {**frames[0].info, "slices": len(frames)}
        )
