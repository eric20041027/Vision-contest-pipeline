"""MaterializedReader on three RSNA Knee studies materialized into a throwaway root: every array
matches its manifest row, labels come through for gold samples, nothing under the real data
root is written."""

from __future__ import annotations

import pytest

from conftest import load_real
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.train import MaterializedReader

pytestmark = pytest.mark.realdata
NAME = "rsna-knee"


@pytest.fixture(scope="module")
def knee(real_roots):
    return load_real(NAME, real_roots)


def test_reader_matches_manifest_on_three_studies(knee, real_roots, tmp_path):
    real_paths = DatasetPaths.resolve(
        NAME, data_root=real_roots.data, configs_root=real_roots.configs
    )
    image_root = real_paths.resolve_image_root(knee.card)
    data, configs = tmp_path / "data", tmp_path / "configs"
    paths = DatasetPaths.resolve("knee3", data_root=data, configs_root=configs)
    three = knee.samples[:3]
    card = knee.card.model_copy(update={"name": "knee3", "image_root": str(image_root)})
    Dataset.from_parts(card, three).save(paths)
    res = materialize(
        MaterializeSpec(name="knee3", mode="png", resize=256, data_root=data, configs_root=configs)
    )
    assert res.failed == 0
    reader = MaterializedReader(
        "knee3", "png-r256", data_root=data, configs_root=configs, verify=True
    )
    assert reader.ids == [s.sample_id for s in three]
    for rec in reader:
        rows = {
            (str(r.view) if r.view is not None else r.seq_id): r for r in reader.rows(rec.sample_id)
        }
        assert set(rec.arrays) == set(rows)
        for key, arr in rec.arrays.items():
            assert list(arr.shape) == rows[key].shape and max(arr.shape[:2]) == 256
        assert (rec.labels is not None) == (rec.sample.label_source == "gold")
