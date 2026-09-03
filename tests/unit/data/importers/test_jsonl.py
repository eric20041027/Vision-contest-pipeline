import json
from pathlib import Path

import pytest

from helpers import CATS, det_samples
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.core.hashing import dir_manifest, manifest_hash
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset, write_samples_jsonl
from vcp.data.importers import IMPORTERS, get_importer, register_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.importers.jsonl import load_categories
from vcp.data.schema import Box, Labels
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan


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
    assert ds.samples == tuple(samples)
    assert ds.card.task == "det"
    assert [c.name for c in ds.card.categories] == ["cat", "dog", "bird"]
    assert ds.card.source.importer == "jsonl" and ds.card.source.license == "CC0"
    assert ds.card.source.raw_path == src.resolve().as_posix()
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
    assert res.dataset.card.image_root == src.resolve().as_posix()
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


def test_paths_inside_data_root_are_stored_relative(roots):
    src = roots.data / "raw" / "ds"
    src.mkdir(parents=True)
    write_samples_jsonl(src / "samples.jsonl", det_samples(2))
    (src / "categories.json").write_text(
        json.dumps([c.model_dump() for c in CATS]), encoding="utf-8"
    )
    res = get_importer("jsonl").run(_spec(roots, src, task="det", categories="categories.json"))
    card = res.dataset.card
    assert card.source.raw_path == "raw/ds"
    assert card.image_root == "raw/ds"
    paths = DatasetPaths.resolve("ds", data_root=roots.data, configs_root=roots.configs)
    assert paths.resolve_image_root(card) == roots.data / "raw" / "ds"


def test_reimport_with_changed_samples_counts_invalidated_plans(roots, tmp_path):
    src = _src(tmp_path, det_samples(6))
    (src / "categories.json").write_text(
        json.dumps([c.model_dump() for c in CATS]), encoding="utf-8"
    )
    spec = _spec(roots, src, task="det", categories="categories.json")
    first = get_importer("jsonl").run(spec)
    assert first.plans_invalidated == 0
    paths = DatasetPaths.resolve("ds", data_root=roots.data, configs_root=roots.configs)
    plan = build_plan(first.dataset, plan_id="p1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    same = get_importer("jsonl").run(spec)
    assert same.plans_invalidated == 0
    write_samples_jsonl(src / "samples.jsonl", det_samples(7))
    changed = get_importer("jsonl").run(spec)
    assert changed.plans_invalidated == 1


def test_invalid_samples_do_not_write_manifest(roots, tmp_path):
    bad = det_samples(1)
    bad[0] = bad[0].model_copy(
        update={"labels": Labels(boxes=[Box(x=0, y=0, w=1, h=1, category_id=9)])}
    )
    src = _src(tmp_path, bad)
    with pytest.raises(ValidationFailed, match="unknown category id"):
        get_importer("jsonl").run(_spec(roots, src, task="det", categories="[]"))
    assert not (roots.data / "datasets" / "ds" / "raw_manifest.txt").exists()


def test_relative_src_keeps_default_image_root(roots, tmp_path, monkeypatch):
    src = _src(tmp_path, det_samples(2))
    (src / "categories.json").write_text(
        json.dumps([c.model_dump() for c in CATS]), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    res = get_importer("jsonl").run(
        _spec(roots, Path("src"), task="det", categories="categories.json")
    )
    assert res.dataset.card.image_root == src.resolve().as_posix()
    assert res.dataset.card.source.raw_path == src.resolve().as_posix()


@pytest.mark.parametrize("corrupt", ["name: [broken\n", "name: [broken]\n"])
def test_reimport_over_corrupt_card_still_counts_plans(roots, tmp_path, corrupt):
    src = _src(tmp_path, det_samples(6))
    (src / "categories.json").write_text(
        json.dumps([c.model_dump() for c in CATS]), encoding="utf-8"
    )
    spec = _spec(roots, src, task="det", categories="categories.json")
    first = get_importer("jsonl").run(spec)
    paths = DatasetPaths.resolve("ds", data_root=roots.data, configs_root=roots.configs)
    save_plan(
        build_plan(first.dataset, plan_id="p1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0),
        paths,
    )
    paths.card_yaml.write_text(corrupt, encoding="utf-8")
    again = get_importer("jsonl").run(spec)
    assert again.plans_invalidated == 1
