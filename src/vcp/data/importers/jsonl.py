"""Pass-through importer for a samples.jsonl already in canonical form.

This is the universal escape hatch: any contest format not covered by a registered importer
can be converted by a small project-level script and enter the framework here, with
validation, hashing and card generation still done by the framework.
"""

from __future__ import annotations

from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.data.dataset import read_samples_jsonl
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.importers.common import count_exif_rotated, exif_policy_option, load_categories
from vcp.data.tasks import get_task


class JsonlImporter:
    name = "jsonl"
    version = "1"

    def run(self, spec: ImportSpec) -> ImportResult:
        task = spec.options.get("task")
        if not task:
            raise ValidationFailed("jsonl importer needs --opt task=<name>")
        get_task(task)
        categories = load_categories(spec.options.get("categories"), spec.src)
        root_opt = spec.options.get("image_root")
        if not root_opt:
            root = spec.src
        else:
            root = Path(root_opt)
            if not root.is_absolute():
                root = spec.src / root
        image_root = str(root)
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
            exif_policy=exif_policy_option(spec.options),
            exif_rotated=count_exif_rotated(samples),
        )
