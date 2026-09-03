import json

import pytest

from helpers import CATS, det_samples
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.core.hashing import dir_manifest, manifest_hash
from vcp.data.dataset import Dataset, write_samples_jsonl
from vcp.data.importers import IMPORTERS, get_importer, register_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.importers.jsonl import load_categories
from vcp.data.schema import Box, Labels


def _spec(roots, src, **opts):
    return ImportSpec(
        importer="jsonl",
        src=src,
        name="ds",
        options=opts,
        license="CC0",
        url="https://example.org",
        downloaded_at="2026-09-02T00:00:00.000Z",
        data_root=roots.data,
        configs_root=roots.configs,
    )


def _src(tmp_path, samples):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    write_samples_jsonl(src / "samples.jsonl", samples)
    return src


def test_jsonl_import_writes_dataset(roots, tmp_path):
    samples = det_samples(4)
    src = _src(tmp_path, samples)
    (src / "categories.json").write_text(
        json.dumps([c.model_dump() for c in CATS]), encoding="utf-8"
    )
    res = get_importer("jsonl").run(
        _spec(roots, src, task="det", categories="categories.json", image_root=str(src))
    )
    assert (res.rows_read, res.samples_written, res.rows_skipped) == (4, 4, 0)
    assert res.skipped_reasons_path is None
    ds = Dataset.load("ds", data_root=roots.data, configs_root=roots.configs)
    assert ds.samples == samples
    assert ds.card.task == "det"
    assert [c.name for c in ds.card.categories] == ["cat", "dog", "bird"]
    assert ds.card.source.importer == "jsonl" and ds.card.source.license == "CC0"
    assert ds.card.source.raw_path == str(src)
    manifest = roots.data / "datasets" / "ds" / "raw_manifest.txt"
    assert manifest.is_file()
    assert ds.card.source.raw_hash == manifest_hash(dir_manifest(src))
    assert ds.card.created_at.endswith("Z")


def test_jsonl_inline_categories_and_default_image_root(roots, tmp_path):
    src = _src(tmp_path, det_samples(2))
    cat_list = [
        {"id": 0, "name": "cat"},
        {"id": 1, "name": "dog"},
        {"id": 2, "name": "bird"},
    ]
    cats = json.dumps(cat_list)
    res = get_importer("jsonl").run(_spec(roots, src, task="det", categories=cats))
    assert res.dataset.card.image_root == str(src)
    assert len(res.dataset.card.categories) == 3


def test_jsonl_requires_task_and_validates_samples(roots, tmp_path):
    src = _src(tmp_path, det_samples(2))
    with pytest.raises(ValidationFailed, match="task"):
        get_importer("jsonl").run(_spec(roots, src))
    with pytest.raises(RegistryError):
        get_importer("jsonl").run(_spec(roots, src, task="pose"))
    bad = det_samples(1)
    bad[0] = bad[0].model_copy(
        update={"labels": Labels(boxes=[Box(x=0, y=0, w=1, h=1, category_id=9)])}
    )
    write_samples_jsonl(src / "samples.jsonl", bad)
    with pytest.raises(ValidationFailed, match="unknown category id"):
        get_importer("jsonl").run(_spec(roots, src, task="det", categories="[]"))


def test_jsonl_missing_files_reported(roots, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(ValidationFailed, match="samples file not found"):
        get_importer("jsonl").run(_spec(roots, src, task="det"))
    with pytest.raises(ValidationFailed, match="categories file not found"):
        load_categories("nope.json", src)
    with pytest.raises(ValidationFailed, match="bad categories"):
        load_categories('[{"id": "x"}]', src)
    assert load_categories(None, src) == []


def test_registry():
    assert "jsonl" in IMPORTERS
    with pytest.raises(RegistryError):
        get_importer("nope")
    with pytest.raises(RegistryError):
        register_importer(IMPORTERS["jsonl"])
