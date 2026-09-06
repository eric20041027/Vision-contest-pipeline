"""Read a materialize cache from a training loop (spec 8.1).

Arrays come from ``cache/materialize/<mode_dir>/`` through its manifest -- the data layer's
authoritative map -- and labels from ``samples.jsonl``. Nothing here augments, batches or
depends on a training framework; wrapping a record in a torch Dataset is the caller's ten
lines (see README).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.materialize.manifest import ManifestRow, read_manifest
from vcp.data.schema import Labels, Sample
from vcp.data.split import load_plan


@dataclass(frozen=True)
class Record:
    """One sample: its arrays keyed by view index (``"0"``) or, for stacked rows, by seq_id."""

    sample_id: str
    sample: Sample
    labels: Labels | None
    arrays: dict[str, np.ndarray]


def _key(row: ManifestRow) -> str:
    return str(row.view) if row.view is not None else (row.seq_id or "")


class MaterializedReader:
    def __init__(
        self,
        name: str,
        mode_dir: str,
        *,
        plan_id: str | None = None,
        subset: str | None = None,
        unseal: bool = False,
        reason: str | None = None,
        data_root: Path | None = None,
        configs_root: Path | None = None,
        verify: bool = False,
    ) -> None:
        if (plan_id is None) != (subset is None):
            raise ValidationFailed("plan_id and subset must be given together")
        self.paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        self.dataset = Dataset.load(name, data_root=data_root, configs_root=configs_root)
        self.root = self.paths.cache_dir / "materialize" / mode_dir
        self.verify = verify
        manifest = self.root / "manifest.jsonl"
        if not manifest.is_file():
            raise ValidationFailed(f"materialize cache not found: {manifest}")
        self._rows: dict[str, list[ManifestRow]] = {}
        for row in read_manifest(manifest).values():
            self._rows.setdefault(row.sample_id, []).append(row)
        if plan_id is not None and subset is not None:
            plan = load_plan(self.paths, plan_id)
            samples = self.dataset.subset(
                subset, plan, unseal=unseal, reason=reason, paths=self.paths
            )
        else:
            samples = list(self.dataset.samples)
        absent = [s.sample_id for s in samples if s.sample_id not in self._rows]
        if absent:
            raise ValidationFailed(
                f"{len(absent)} samples have no materialized rows in {mode_dir!r} "
                f"(e.g. {absent[:3]}); run vcp data materialize first",
                location=absent[0],
            )
        self._samples = {s.sample_id: s for s in samples}
        self.ids: list[str] = [s.sample_id for s in samples]

    def __len__(self) -> int:
        return len(self.ids)

    def __iter__(self) -> Iterator[Record]:
        for sid in self.ids:
            yield self[sid]

    def rows(self, sample_id: str) -> list[ManifestRow]:
        return list(self._rows[sample_id])

    def __getitem__(self, sample_id: str) -> Record:
        sample = self._samples[sample_id]
        arrays = {_key(row): self._load(row) for row in self._rows[sample_id]}
        return Record(sample_id=sample_id, sample=sample, labels=sample.labels, arrays=arrays)

    def _load(self, row: ManifestRow) -> np.ndarray:
        path = self.root / row.out
        if self.verify and sha256_file(path) != row.sha256:
            raise IntegrityError(
                f"materialized file {row.out} does not match its manifest sha256",
                location=row.sample_id,
            )
        if path.suffix == ".npy":
            return np.load(path)
        with Image.open(path) as im:
            return np.asarray(im)
