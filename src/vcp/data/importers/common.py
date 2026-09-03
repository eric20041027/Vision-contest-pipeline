"""Helpers shared by image-based importers: image discovery, header-only sizes, CSV reading."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.data.schema import Category, Sample, View

IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"})


def rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def iter_images(root: Path) -> list[Path]:
    """Every image file under ``root`` (recursive), sorted by posix relative path."""
    if not root.is_dir():
        raise ValidationFailed(f"image directory not found: {root}")
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return sorted(files, key=lambda p: rel_posix(p, root))


EXIF_ORIENTATION_TAG = 0x0112
SWAPPED_ORIENTATIONS = frozenset({5, 6, 7, 8})
EXIF_POLICIES = ("stored", "oriented")


def choice_option(opts: dict[str, str], key: str, allowed: tuple[str, ...], default: str) -> str:
    """``--opt key=value`` restricted to ``allowed``; a bad value is the user's problem (FAIL)."""
    value = opts.get(key, default)
    if value not in allowed:
        raise ValidationFailed(f"--opt {key}= must be one of {allowed}, got {value!r}")
    return value


def exif_policy_option(opts: dict[str, str]) -> str:
    return choice_option(opts, "exif", EXIF_POLICIES, "stored")


def image_header(path: Path) -> tuple[int, int, int | None]:
    """(width, height, exif_orientation) from the file header; pixels are never decoded.
    Orientation is None when the tag is absent or 1 (normal)."""
    try:
        with Image.open(path) as im:
            width, height = im.size
            orientation = im.getexif().get(EXIF_ORIENTATION_TAG)
    except FileNotFoundError:
        raise ValidationFailed(f"image not found: {path}") from None
    except UnidentifiedImageError:
        raise ValidationFailed(f"not a readable image: {path}") from None
    if not isinstance(orientation, int) or orientation == 1:
        orientation = None
    return width, height, orientation


def image_size(path: Path) -> tuple[int, int]:
    width, height, _ = image_header(path)
    return width, height


def make_view(root: Path, rel: str, *, exif_policy: str = "stored") -> View:
    """View with header size. Under ``oriented`` the size is the EXIF-transposed one; the raw
    orientation tag is always recorded in ``meta`` so audits and exporters can warn."""
    width, height, orientation = image_header(root / rel)
    meta: dict[str, Any] = {}
    if orientation is not None:
        meta["exif_orientation"] = orientation
        if exif_policy == "oriented" and orientation in SWAPPED_ORIENTATIONS:
            width, height = height, width
    return View(path=rel, width=width, height=height, meta=meta)


def count_exif_rotated(samples: Iterable[Sample]) -> int:
    return sum(1 for s in samples for v in s.views if "exif_orientation" in v.meta)


def read_csv(path: Path, *, required: Iterable[str]) -> tuple[list[str], list[dict[str, str]]]:
    """(header, rows) of a UTF-8 (optionally BOM-prefixed) CSV; missing columns -> error."""
    if not path.is_file():
        raise ValidationFailed(f"CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        missing = [c for c in required if c not in header]
        if missing:
            raise ValidationFailed(f"CSV {path.name} lacks columns {missing}; header = {header}")
        return header, [dict(row) for row in reader]


def load_categories(value: str | None, base: Path) -> list[Category]:
    """``value`` is inline JSON (starts with ``[``) or a path to a JSON file (relative to base)."""
    if not value:
        return []
    text = value.strip()
    if not text.startswith("["):
        path = Path(text) if Path(text).is_absolute() else base / text
        if not path.is_file():
            raise ValidationFailed(f"categories file not found: {path}")
        text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
        return [Category.model_validate(item) for item in raw]
    except (json.JSONDecodeError, ValidationError, TypeError) as e:
        raise ValidationFailed(f"bad categories: {e}") from e
