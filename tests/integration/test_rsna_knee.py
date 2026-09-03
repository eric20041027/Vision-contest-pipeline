"""Real-data checks for the RSNA Knee subset (spec §15.6-27). Expectations are derived from the
competition CSVs on disk, never hard-coded, so any subset size works."""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from conftest import load_real
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.data.materialize.manifest import read_manifest

pytestmark = pytest.mark.realdata
NAME = "rsna-knee"


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def knee(real_roots):
    return load_real(NAME, real_roots)


@pytest.fixture(scope="module")
def raw(real_roots):
    root = real_roots.data / "raw" / NAME
    if not (root / "train.csv").is_file():
        pytest.skip("RSNA raw CSVs not present")
    return root


def test_knee_shape_matches_csvs(knee, raw):
    studies = sorted(p.name for p in (raw / "train_series").iterdir() if p.is_dir())
    assert [s.sample_id for s in knee.samples] == studies
    assert knee.card.task == "multilabel" and len(knee.card.categories) == 12
    labels = {r["StudyInstanceUID"]: r for r in _rows(raw / "train.csv")}
    target_cols = [c.name for c in knee.card.categories]
    gold = [sid for sid in studies if all(labels[sid][c].strip() for c in target_cols)]
    assert sum(s.label_source == "gold" for s in knee.samples) == len(gold)
    assert sum(s.label_source == "none" for s in knee.samples) == len(studies) - len(gold)
    per_study = Counter(r["StudyInstanceUID"] for r in _rows(raw / "train_series.csv"))
    for s in knee.samples:
        assert len(s.meta["series"]) == per_study[s.sample_id]
        assert s.meta["Report"].strip()
        by_seq: dict[str, list[int]] = {}
        for v in s.views:
            assert v.width and v.height and v.seq_id and v.role in ("Sagittal", "Coronal", "Axial")
            by_seq.setdefault(v.seq_id, []).append(v.seq_index)
        assert all(idx == list(range(len(idx))) for idx in by_seq.values())


def test_knee_materialize_three_studies(knee, real_roots, tmp_path):
    real_paths = DatasetPaths.resolve(
        NAME, data_root=real_roots.data, configs_root=real_roots.configs
    )
    image_root = real_paths.resolve_image_root(knee.card)
    data, configs = tmp_path / "data", tmp_path / "configs"
    paths = DatasetPaths.resolve("knee3", data_root=data, configs_root=configs)
    three = knee.samples[:3]
    card = knee.card.model_copy(update={"name": "knee3", "image_root": str(image_root)})
    Dataset.from_parts(card, three).save(paths)
    spec = dict(name="knee3", data_root=data, configs_root=configs)
    png = materialize(MaterializeSpec(mode="png", resize=256, **spec))
    assert png.failed == 0 and png.materialized == sum(len(s.views) for s in three)
    again = materialize(MaterializeSpec(mode="png", resize=256, **spec))
    assert again.skipped == png.materialized and again.materialized == 0
    forced = materialize(MaterializeSpec(mode="png", resize=256, force=True, **spec))
    assert {r.sha256 for r in read_manifest(forced.manifest_path).values()} == {
        r.sha256 for r in read_manifest(png.manifest_path).values()
    }
    vol = materialize(MaterializeSpec(mode="npy", stack_seq=True, **spec))
    assert vol.failed == 0 and vol.materialized == sum(len(s.meta["series"]) for s in three)
    row = next(iter(read_manifest(vol.manifest_path).values()))
    arr = np.load(vol.out_dir / row.out)
    assert arr.ndim == 3 and arr.shape[0] == three[0].meta["series"][row.seq_id]["n_slices"]
    assert arr.dtype in (np.uint16, np.int16)
