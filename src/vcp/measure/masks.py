"""Rasterise canonical masks (polygon / RLE) into boolean arrays of a view's size.

A polygon vertex outside the view is not an error: Pillow clips the fill to the canvas (the
same as any other drawing call), so a mask that legitimately extends past its declared
width/height still rasterises to whatever portion overlaps the view instead of crashing.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from vcp.core.errors import ValidationFailed
from vcp.data.schema import Mask
from vcp.measure.schema import PredMask


def rasterize_polygon(polygons: list[list[float]], width: int, height: int) -> np.ndarray:
    """Fill one or more polygon rings into a ``(height, width)`` boolean array.

    Pillow's polygon fill is boundary-inclusive, so a ring's own extreme pixels are always
    part of the mask (e.g. a 2x2 axis-aligned square covers a 3x3 pixel block, not 2x2).
    """
    img = Image.new("1", (width, height), 0)
    draw = ImageDraw.Draw(img)
    for ring in polygons:
        if len(ring) < 6 or len(ring) % 2:
            raise ValidationFailed(f"degenerate polygon ring with {len(ring)} numbers")
        draw.polygon([(ring[i], ring[i + 1]) for i in range(0, len(ring), 2)], fill=1)
    return np.asarray(img, dtype=bool)


def _rle_size(meta: dict[str, Any], width: int, height: int) -> tuple[int, int]:
    size = meta.get("size") or [height, width]
    if len(size) != 2:
        raise ValidationFailed(f"RLE meta['size'] must have two elements, got {size!r}")
    try:
        h, w = int(size[0]), int(size[1])
    except (TypeError, ValueError) as e:
        raise ValidationFailed(f"RLE meta['size'] must be two integers, got {size!r}") from e
    if (h, w) != (height, width):
        raise ValidationFailed(f"RLE size {[h, w]} does not match the view {[height, width]}")
    return h, w


def _decode_uncompressed(rle: str, h: int, w: int) -> np.ndarray:
    try:
        counts = [int(c) for c in rle.split(",") if c.strip()]
    except ValueError as e:
        raise ValidationFailed(f"bad uncompressed RLE counts: {e}") from e
    flat = np.zeros(h * w, dtype=bool)
    pos, value = 0, False
    for run in counts:
        if run < 0:
            raise ValidationFailed(f"uncompressed RLE counts must be non-negative, got {run}")
        if value:
            flat[pos : pos + run] = True
        pos += run
        value = not value
    if pos != h * w:
        raise ValidationFailed(f"RLE counts cover {pos} pixels, view has {h * w}")
    return flat.reshape((h, w), order="F")


def _decode_compressed(rle: str, h: int, w: int) -> np.ndarray:
    if not rle.strip():
        raise ValidationFailed("compressed RLE counts string is empty")
    from vcp.measure.metrics.coco_map import require_pycocotools

    require_pycocotools()
    from pycocotools import mask as mask_util

    try:
        decoded = mask_util.decode({"size": [h, w], "counts": rle.encode("ascii")})
    except ValueError as e:
        raise ValidationFailed(f"pycocotools could not decode RLE counts: {e}") from e
    return np.asarray(decoded, dtype=bool)


def decode_rle(rle: str, meta: dict[str, Any], width: int, height: int) -> np.ndarray:
    """Decode an RLE mask into a ``(height, width)`` boolean array.

    ``meta["rle_encoding"] == "uncompressed"`` means ``rle`` is comma-separated, column-major
    run lengths (the vcp convention for a hand-rolled/JSON-friendly RLE); anything else is
    treated as a COCO-compressed counts string, decoded lazily via pycocotools.
    """
    h, w = _rle_size(meta, width, height)
    if meta.get("rle_encoding") == "uncompressed":
        return _decode_uncompressed(rle, h, w)
    return _decode_compressed(rle, h, w)


def mask_array(mask: Mask | PredMask, width: int, height: int) -> np.ndarray:
    """Rasterise any supported mask representation to a ``(height, width)`` boolean array.

    The canonical ``Mask`` has three representations (rle / polygon / path); ``PredMask`` only
    ever carries rle / polygon (a prediction cannot point at an image file on disk). A
    path-form mask is therefore a located ``ValidationFailed`` naming the two forms this
    function does support, not a crash from a missing ``.polygon`` / ``.rle`` value.
    """
    if mask.polygon is not None:
        return rasterize_polygon(mask.polygon, width, height)
    if mask.rle is not None:
        return decode_rle(mask.rle, mask.meta, width, height)
    raise ValidationFailed(
        "mask_array does not support path-form masks; only polygon or RLE are supported"
    )
