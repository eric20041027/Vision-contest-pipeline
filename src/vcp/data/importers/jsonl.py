"""Pass-through importer for a samples.jsonl already in canonical form.

This is the universal escape hatch: any contest format not covered by a registered importer
can be converted by a small project-level script and enter the framework here, with
validation, hashing and card generation still done by the framework.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.data.dataset import read_samples_jsonl
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.schema import Category
from vcp.data.tasks import get_task


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


class JsonlImporter:
    name = "jsonl"
    version = "1"

    def run(self, spec: ImportSpec) -> ImportResult:
        task = spec.options.get("task")
        if not task:
            raise ValidationFailed("jsonl importer needs --opt task=<name>")
        get_task(task)
        categories = load_categories(spec.options.get("categories"), spec.src)
        image_root = spec.options.get("image_root", str(spec.src))
        samples_file = spec.src / spec.options.get("samples", "samples.jsonl")
        if not samples_file.is_file():
            raise ValidationFailed(f"samples file not found: {samples_file}")
        samples = list(read_samples_jsonl(samples_file))
        return finalize_import(
            spec=spec,
            importer=self,
            task=task,
            categories=categories,
            image_root=image_root,
            samples=samples,
            rows_read=len(samples),
            skipped=[],
        )
