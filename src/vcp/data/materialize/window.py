"""8-bit mapping for png output and long-side resizing."""

from __future__ import annotations

import numpy as np
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.data.materialize.decoders.base import Decoded

WINDOW_MODES = ("dicom", "minmax", "percentile")


def _bounds(decoded: Decoded, mode: str) -> tuple[float, float]:
    arr = decoded.array
    if mode == "dicom":
        c, w = decoded.info.get("window_center"), decoded.info.get("window_width")
        if c is not None and w:
            return c - w / 2, c + w / 2
        mode = "minmax"
    if mode == "percentile":
        lo, hi = np.percentile(arr, [0.5, 99.5])
        return float(lo), float(hi)
    return float(arr.min()), float(arr.max())


def to_uint8(decoded: Decoded, mode: str) -> np.ndarray:
    """Map any decoded array to uint8; uint8 input is returned untouched except for the
    MONOCHROME1 inversion."""
    if mode not in WINDOW_MODES:
        raise ValidationFailed(f"window mode must be one of {WINDOW_MODES}, got {mode!r}")
    arr = decoded.array
    inverted = decoded.info.get("photometric") == "MONOCHROME1"
    if arr.dtype == np.uint8:
        return (255 - arr) if inverted else arr
    lo, hi = _bounds(decoded, mode)
    scaled = np.clip((arr.astype(np.float64) - lo) / max(hi - lo, 1e-9), 0.0, 1.0)
    out = np.round(scaled * 255).astype(np.uint8)
    return (255 - out) if inverted else out


def resize_long_side(arr: np.ndarray, long_side: int) -> np.ndarray:
    """Proportional resize of a uint8 HW or HWC array so max(H, W) == long_side (LANCZOS)."""
    h, w = arr.shape[:2]
    scale = long_side / max(h, w)
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return np.asarray(Image.fromarray(arr).resize(size, Image.LANCZOS))
