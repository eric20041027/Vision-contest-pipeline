"""Exporter contract, registry and the shared export flow (subset -> files + manifest)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.importers.common import rel_posix
from vcp.data.schema import Sample, View
from vcp.data.split import load_plan


class ExportSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    plan_id: str
    subset: str
    format: str
    out: Path
    options: dict[str, str] = Field(default_factory=dict)
    unseal: bool = False
    reason: str | None = None
    data_root: Path | None = None
    configs_root: Path | None = None

    def paths(self) -> DatasetPaths:
        return DatasetPaths.resolve(
            self.name, data_root=self.data_root, configs_root=self.configs_root
        )


class ExportResult(BaseModel):
    out: Path
    manifest_path: Path
    files: int
    warnings: list[str]


class Exporter(Protocol):
    name: str
    version: str

    def run(
        self,
        dataset: Dataset,
        samples: list[Sample],
        out: Path,
        image_root: Path,
        options: dict[str, str],
    ) -> tuple[list[Path], list[str]]: ...


EXPORTERS: dict[str, Exporter] = {}


def register_exporter(exporter: Exporter) -> None:
    if exporter.name in EXPORTERS:
        raise RegistryError(f"exporter {exporter.name!r} already registered")
    EXPORTERS[exporter.name] = exporter


def get_exporter(name: str) -> Exporter:
    try:
        return EXPORTERS[name]
    except KeyError:
        raise RegistryError(f"unknown exporter {name!r}; known: {sorted(EXPORTERS)}") from None


def select_view(sample: Sample, view_opt: str | None) -> tuple[int, View]:
    """Which view an exporter should use: the only one, an index, or a role name."""
    views = sample.views
    if view_opt is None:
        if len(views) != 1:
            raise ValidationFailed(
                f"sample {sample.sample_id!r} has {len(views)} views; pass --opt view=<index|role>"
            )
        return 0, views[0]
    if view_opt.isdigit():
        idx = int(view_opt)
        if idx >= len(views):
            raise ValidationFailed(f"sample {sample.sample_id!r} has no view index {idx}")
        return idx, views[idx]
    for i, v in enumerate(views):
        if v.role == view_opt:
            return i, v
    raise ValidationFailed(f"sample {sample.sample_id!r} has no view with role {view_opt!r}")


def export_subset(spec: ExportSpec) -> ExportResult:
    paths = spec.paths()
    exporter = get_exporter(spec.format)
    dataset = Dataset.load(spec.name, data_root=spec.data_root, configs_root=spec.configs_root)
    plan = load_plan(paths, spec.plan_id)
    samples = dataset.subset(
        spec.subset,
        plan,
        unseal=spec.unseal,
        reason=spec.reason,
        caller="vcp data export",
        paths=paths,
    )
    out = spec.out.expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        raise ValidationFailed(f"output directory not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    files, warnings = exporter.run(
        dataset, samples, out, paths.resolve_image_root(dataset.card), spec.options
    )
    manifest = {
        "dataset": dataset.card.name,
        "samples_hash": dataset.card.samples_hash,
        "plan_id": plan.plan_id,
        "subset": spec.subset,
        "format": exporter.name,
        "exporter_version": exporter.version,
        "exported_at": stamp(),
        "sample_count": len(samples),
        "files": {rel_posix(f, out): sha256_file(f) for f in sorted(files)},
    }
    manifest_path = out / "manifest.json"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return ExportResult(out=out, manifest_path=manifest_path, files=len(files), warnings=warnings)
