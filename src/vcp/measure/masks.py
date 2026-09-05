"""Rasterise canonical masks (polygon / RLE) into boolean arrays of a view's size.

Polygon, uncompressed RLE and compressed RLE all rasterise through pycocotools' own C
implementation (``frPyObjects`` / ``decode``) -- ONE convention for all three representations.
This is also the convention the framework's own COCO exporter already assumes (its shoelace-
formula ``polygon_area`` in ``vcp.data.exporters.coco`` matches pycocotools exactly) and the
convention every pycocotools-scored leaderboard uses.

This module used to fill polygons with Pillow instead. Pillow's polygon fill is
boundary-inclusive (a ring's own extreme pixels are always part of the mask), which disagrees
with pycocotools by up to 2x on small masks -- e.g. a 2x2 axis-aligned square covers a 3x3 pixel
block (9px) under Pillow but 4px under pycocotools (== its own area). Since the COCO importer
stores gold labels as polygons and detectors typically emit RLE, scoring a polygon gold against
an RLE prediction of the exact same region silently produced dice/miou far below 1.0. Pillow is
no longer used anywhere in this module (it remains a project dependency for other code).

Every ``ValidationFailed`` raised here accepts a ``location`` (typically the sample id) so a
caller scoring many samples can say which one broke, instead of reporting e.g. "degenerate
polygon ring with 5 numbers" with nothing identifying which of 50k samples it came from.
"""

from __future__ import annotations

import warnings
from typing import Any

import numpy as np

from vcp.core.errors import ValidationFailed
from vcp.data.schema import Mask
from vcp.measure.schema import PredMask

# pycocotools 2.0.x's compiled decode() trips NumPy 2's "__array__ doesn't accept a copy
# keyword" DeprecationWarning on every call. It is the library's to fix, not ours; a warning per
# mask on a 50k-sample dataset is unacceptable output, so the ONE place that calls decode()
# filters exactly that message -- never a blanket filter in pyproject.
_PYCOCOTOOLS_ARRAY_COPY_WARNING = r"__array__ implementation doesn't accept a copy keyword"


def _decode(rle: Any) -> np.ndarray:
    """``pycocotools.mask.decode`` as a boolean array, with the library's own NumPy-2
    deprecation noise filtered at the call site."""
    from pycocotools import mask as mask_util

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message=_PYCOCOTOOLS_ARRAY_COPY_WARNING, category=DeprecationWarning
        )
        return np.asarray(mask_util.decode(rle), dtype=bool)


def rasterize_polygon(
    polygons: list[list[float]],
    width: int,
    height: int,
    *,
    location: str | None = None,
) -> np.ndarray:
    """Fill one or more polygon rings into a ``(height, width)`` boolean array via pycocotools
    (``frPyObjects`` + ``merge`` + ``decode``) -- the same convention ``decode_rle`` uses for RLE,
    so a polygon and an RLE encoding the same region always agree exactly. Multiple rings are
    unioned (a multi-part instance), not treated as holes. A vertex outside the view is not an
    error: pycocotools clips the fill to the canvas, same as it would for any drawing call.
    """
    if not polygons:
        raise ValidationFailed("polygon has no rings", location=location)
    for ring in polygons:
        if len(ring) < 6 or len(ring) % 2:
            raise ValidationFailed(
                f"degenerate polygon ring with {len(ring)} numbers", location=location
            )
    from vcp.measure.metrics.coco_map import require_pycocotools

    require_pycocotools()
    from pycocotools import mask as mask_util

    rles = mask_util.frPyObjects(polygons, height, width)
    merged = mask_util.merge(rles)
    return _decode(merged)


def _rle_size(
    meta: dict[str, Any],
    width: int,
    height: int,
    *,
    location: str | None = None,
) -> tuple[int, int]:
    size = meta.get("size") or [height, width]
    # The isinstance check must run BEFORE len()/indexing: meta is a user file's unconstrained
    # dict, so size may arrive as e.g. an int (len() raises TypeError) or a two-key dict (len()
    # == 2 passes, but size[0] then raises KeyError) -- both must become the same located
    # ValidationFailed, never a raw crash (F2).
    if not isinstance(size, (list, tuple)) or len(size) != 2:
        raise ValidationFailed(
            f"RLE meta['size'] must have two elements, got {size!r}", location=location
        )
    try:
        h, w = int(size[0]), int(size[1])
    except (TypeError, ValueError) as e:
        raise ValidationFailed(
            f"RLE meta['size'] must be two integers, got {size!r}", location=location
        ) from e
    if (h, w) != (height, width):
        raise ValidationFailed(
            f"RLE size {[h, w]} does not match the view {[height, width]}", location=location
        )
    return h, w


def _decode_uncompressed(
    rle: str,
    h: int,
    w: int,
    *,
    location: str | None = None,
) -> np.ndarray:
    try:
        counts = [int(c) for c in rle.split(",") if c.strip()]
    except ValueError as e:
        raise ValidationFailed(f"bad uncompressed RLE counts: {e}", location=location) from e
    flat = np.zeros(h * w, dtype=bool)
    pos, value = 0, False
    for run in counts:
        if run < 0:
            raise ValidationFailed(
                f"uncompressed RLE counts must be non-negative, got {run}", location=location
            )
        if value:
            flat[pos : pos + run] = True
        pos += run
        value = not value
    if pos != h * w:
        raise ValidationFailed(
            f"RLE counts cover {pos} pixels, view has {h * w}", location=location
        )
    return flat.reshape((h, w), order="F")


def _decode_compressed(
    rle: str,
    h: int,
    w: int,
    *,
    location: str | None = None,
) -> np.ndarray:
    if not rle.strip():
        raise ValidationFailed("compressed RLE counts string is empty", location=location)
    from vcp.measure.metrics.coco_map import require_pycocotools

    require_pycocotools()
    try:
        return _decode({"size": [h, w], "counts": rle.encode("ascii")})
    except ValueError as e:
        raise ValidationFailed(
            f"pycocotools could not decode RLE counts: {e}", location=location
        ) from e


def decode_rle(
    rle: str,
    meta: dict[str, Any],
    width: int,
    height: int,
    *,
    location: str | None = None,
) -> np.ndarray:
    """Decode an RLE mask into a ``(height, width)`` boolean array.

    ``meta["rle_encoding"] == "uncompressed"`` means ``rle`` is comma-separated, column-major
    run lengths (the vcp convention for a hand-rolled/JSON-friendly RLE); anything else is
    treated as a COCO-compressed counts string, decoded lazily via pycocotools.
    """
    h, w = _rle_size(meta, width, height, location=location)
    if meta.get("rle_encoding") == "uncompressed":
        return _decode_uncompressed(rle, h, w, location=location)
    return _decode_compressed(rle, h, w, location=location)


def mask_array(
    mask: Mask | PredMask,
    width: int,
    height: int,
    *,
    location: str | None = None,
) -> np.ndarray:
    """Rasterise any supported mask representation to a ``(height, width)`` boolean array.

    The canonical ``Mask`` has three representations (rle / polygon / path); ``PredMask`` only
    ever carries rle / polygon (a prediction cannot point at an image file on disk). A
    path-form mask is therefore a located ``ValidationFailed`` naming the two forms this
    function does support, not a crash from a missing ``.polygon`` / ``.rle`` value.
    """
    if mask.polygon is not None:
        return rasterize_polygon(mask.polygon, width, height, location=location)
    if mask.rle is not None:
        return decode_rle(mask.rle, mask.meta, width, height, location=location)
    raise ValidationFailed(
        "mask_array does not support path-form masks; only polygon or RLE are supported",
        location=location,
    )
