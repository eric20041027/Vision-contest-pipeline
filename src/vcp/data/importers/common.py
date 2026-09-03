"""Helpers shared by image-based importers: image discovery, header-only sizes, CSV reading."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.data.schema import Category, View

IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"})


def rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def iter_images(root: Path) -> list[Path]:
    """Every image file under ``root`` (recursive), sorted by posix relative path."""
    if not root.is_dir():
        raise ValidationFailed(f"image directory not found: {root}")
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return sorted(files, key=lambda p: rel_posix(p, root))


def image_size(path: Path) -> tuple[int, int]:
    """(width, height) read from the file header; pixels are never decoded."""
    try:
        with Image.open(path) as im:
            return im.size
    except FileNotFoundError:
        raise ValidationFailed(f"image not found: {path}") from None
    except UnidentifiedImageError:
        raise ValidationFailed(f"not a readable image: {path}") from None


def make_view(root: Path, rel: str) -> View:
    width, height = image_size(root / rel)
    return View(path=rel, width=width, height=height)


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
