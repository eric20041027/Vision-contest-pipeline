"""Importer contract and registry. Every importer ends by calling ``finalize_import``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.core.hashing import dir_manifest, write_manifest
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.schema import Category, DatasetCard, Sample, SourceInfo


class ImportSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    importer: str
    src: Path
    name: str
    options: dict[str, str] = Field(default_factory=dict)
    license: str
    url: str
    downloaded_at: str
    notes: str = ""
    data_root: Path | None = None
    configs_root: Path | None = None

    def paths(self) -> DatasetPaths:
        return DatasetPaths.resolve(
            self.name, data_root=self.data_root, configs_root=self.configs_root
        )


class ImportResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    dataset: Dataset
    rows_read: int
    samples_written: int
    rows_skipped: int
    skipped_reasons_path: Path | None


class Importer(Protocol):
    name: str
    version: str

    def run(self, spec: ImportSpec) -> ImportResult: ...


IMPORTERS: dict[str, Importer] = {}


def register_importer(importer: Importer) -> None:
    if importer.name in IMPORTERS:
        raise RegistryError(f"importer {importer.name!r} already registered")
    IMPORTERS[importer.name] = importer


def get_importer(name: str) -> Importer:
    try:
        return IMPORTERS[name]
    except KeyError:
        raise RegistryError(f"unknown importer {name!r}; known: {sorted(IMPORTERS)}") from None


def finalize_import(
    *,
    spec: ImportSpec,
    importer: Importer,
    task: str,
    categories: list[Category],
    image_root: str,
    samples: list[Sample],
    rows_read: int,
    skipped: list[dict[str, Any]],
) -> ImportResult:
    """Common tail of every importer: provenance, card, validation, save, skip report."""
    paths = spec.paths()
    if not spec.src.is_dir():
        raise ValidationFailed(f"source directory not found: {spec.src}")
    raw_hash = write_manifest(dir_manifest(spec.src), paths.raw_manifest)
    source = SourceInfo(
        importer=importer.name,
        importer_version=importer.version,
        raw_path=str(spec.src),
        raw_hash=raw_hash,
        license=spec.license,
        url=spec.url,
        downloaded_at=spec.downloaded_at,
        notes=spec.notes,
    )
    card = DatasetCard(
        name=spec.name,
        task=task,
        categories=categories,
        image_root=image_root,
        source=source,
        created_at=stamp(),
        sample_count=len(samples),
        samples_hash="",
    )
    dataset = Dataset.from_parts(card, samples)
    dataset.save(paths)
    skipped_path: Path | None = None
    if skipped:
        paths.cache_dir.mkdir(parents=True, exist_ok=True)
        skipped_path = paths.cache_dir / "import_skipped.jsonl"
        with skipped_path.open("w", encoding="utf-8", newline="\n") as f:
            for row in skipped:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return ImportResult(
        dataset=dataset,
        rows_read=rows_read,
        samples_written=len(dataset.samples),
        rows_skipped=len(skipped),
        skipped_reasons_path=skipped_path,
    )
