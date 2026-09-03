"""Importer contract and registry. Every importer ends by calling ``finalize_import``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.config import load_yaml_model
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.core.hashing import dir_manifest, write_manifest
from vcp.core.paths import DatasetPaths, store_path
from vcp.core.time import stamp
from vcp.data.dataset import Dataset, samples_digest
from vcp.data.schema import Category, DatasetCard, RawManifestMode, Sample, SourceInfo


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
    raw_manifest: RawManifestMode = "full"
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
    plans_invalidated: int = 0
    unlabeled: int = 0
    exif_rotated: int = 0
    extra_fields: dict[str, int] = Field(default_factory=dict)


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


def count_invalidated_plans(paths: DatasetPaths, new_digest: str) -> int:
    """Existing split plans that a re-import with a different samples_hash would orphan."""
    if not paths.card_yaml.is_file() or not paths.splits_dir.is_dir():
        return 0
    try:
        old = load_yaml_model(paths.card_yaml, DatasetCard)
    except ValidationFailed:
        old = None  # unreadable old card: cannot prove the plans still match, so count them
    if old is not None and old.samples_hash == new_digest:
        return 0
    return len(list(paths.splits_dir.glob("*.json")))


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
    unlabeled: int = 0,
    exif_policy: str = "stored",
    exif_rotated: int = 0,
    extra_fields: dict[str, int] | None = None,
) -> ImportResult:
    """Common tail of every importer: validate, then provenance, save, skip report.

    Validation runs before the raw manifest so a bad dataset never pays for hashing every raw
    file. Paths inside the data root are stored relative to it (portable cards).
    ``exif_policy`` / ``exif_rotated`` are supplied by image importers (see
    ``importers/common.py``).
    """
    paths = spec.paths()
    if not spec.src.is_dir():
        raise ValidationFailed(f"source directory not found: {spec.src}")
    source = SourceInfo(
        importer=importer.name,
        importer_version=importer.version,
        raw_path=store_path(spec.src, paths.data_root),
        raw_hash="",
        license=spec.license,
        url=spec.url,
        downloaded_at=spec.downloaded_at,
        notes=spec.notes,
        raw_manifest_mode=spec.raw_manifest,
    )
    card = DatasetCard(
        name=spec.name,
        task=task,
        categories=categories,
        image_root=store_path(Path(image_root), paths.data_root),
        source=source,
        created_at=stamp(),
        sample_count=len(samples),
        samples_hash="",
        exif_policy=exif_policy,
    )
    dataset = Dataset.from_parts(card, samples)
    plans_invalidated = count_invalidated_plans(paths, samples_digest(dataset.samples))
    raw_hash = write_manifest(dir_manifest(spec.src, mode=spec.raw_manifest), paths.raw_manifest)
    dataset.card = dataset.card.model_copy(
        update={"source": source.model_copy(update={"raw_hash": raw_hash})}
    )
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
        plans_invalidated=plans_invalidated,
        unlabeled=unlabeled,
        exif_rotated=exif_rotated,
        extra_fields=dict(extra_fields or {}),
    )
