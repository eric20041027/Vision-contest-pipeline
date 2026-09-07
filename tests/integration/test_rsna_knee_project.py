"""Project transform parity on real DICOM, with all derived files under tmp_path."""

import importlib
from pathlib import Path

import numpy as np
import pytest

from conftest import load_real
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.train import MaterializedReader

pytestmark = pytest.mark.realdata


def test_notebook_raw_transform_matches_materialized_reader(real_roots, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "projects" / "rsna-knee"))
    project = importlib.import_module("rsna_knee.data")
    real = load_real("rsna-knee", real_roots)
    source = DatasetPaths.resolve(
        "rsna-knee", data_root=real_roots.data, configs_root=real_roots.configs
    )
    image_root = source.resolve_image_root(real.card)
    paths = DatasetPaths.resolve(
        "knee-parity", data_root=tmp_path / "data", configs_root=tmp_path / "configs"
    )
    card = real.card.model_copy(update={"name": paths.name, "image_root": str(image_root)})
    ds = Dataset.from_parts(card, real.samples[:3])
    ds.save(paths)
    result = materialize(
        MaterializeSpec(
            name=paths.name,
            mode="png",
            resize=256,
            data_root=paths.data_root,
            configs_root=paths.configs_root,
        )
    )
    assert result.failed == 0
    reader = MaterializedReader(
        paths.name,
        "png-r256",
        data_root=paths.data_root,
        configs_root=paths.configs_root,
        verify=True,
    )
    for sample in ds.samples:
        cached = project.study_tensor(reader[sample.sample_id])
        raw = project.raw_tensor(image_root, sample.sample_id, sample.meta["series"])
        assert np.array_equal(raw, cached), sample.sample_id
        assert raw.shape == (9, 256, 256) and np.isfinite(raw).all()
        assert set(view.path.split("/")[0] for view in sample.views) == {sample.sample_id}
