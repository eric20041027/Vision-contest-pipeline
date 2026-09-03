from pathlib import Path

import pytest
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.importers.yolo import load_names


def _img(path: Path, size=(100, 50)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (7, 7, 7)).save(path)


def _spec(roots, src, **opts):
    return ImportSpec(
        importer="yolo",
        src=src,
        name="yolo",
        options=opts,
        license="CC0",
        url="https://example.org",
        downloaded_at="2026-09-03",
        data_root=roots.data,
        configs_root=roots.configs,
    )


def _src(tmp_path) -> Path:
    src = tmp_path / "src"
    _img(src / "images" / "a.jpg")
    _img(src / "images" / "sub" / "b.jpg")
    _img(src / "images" / "c.jpg")
    (src / "labels").mkdir()
    (src / "labels" / "a.txt").write_text(
        "0 0.5 0.5 0.2 0.4\n1 0.1 0.1 0.2 0.2\n", encoding="utf-8"
    )
    (src / "labels" / "sub").mkdir()
    (src / "labels" / "sub" / "b.txt").write_text("\n", encoding="utf-8")
    (src / "classes.txt").write_text("cat\ndog\n", encoding="utf-8")
    return src


def test_import_yolo(roots, tmp_path):
    res = get_importer("yolo").run(_spec(roots, _src(tmp_path)))
    ds = res.dataset
    assert [s.sample_id for s in ds.samples] == ["a.jpg", "c.jpg", "sub/b.jpg"]
    a = ds.by_id["a.jpg"].labels.boxes
    assert [(b.category_id, b.x, b.y, b.w, b.h) for b in a] == [
        (0, 40.0, 15.0, 20.0, 20.0),
        (1, 0.0, 0.0, 20.0, 10.0),
    ]
    assert ds.by_id["c.jpg"].labels.boxes == []
    assert ds.by_id["sub/b.jpg"].labels.boxes == []
    assert [(c.id, c.name) for c in ds.card.categories] == [(0, "cat"), (1, "dog")]
    assert res.rows_read == 3 and res.rows_skipped == 0
    assert res.unlabeled == 1


def test_names_from_yaml_and_errors(roots, tmp_path):
    src = _src(tmp_path)
    (src / "data.yaml").write_text("names:\n  0: cat\n  1: dog\n", encoding="utf-8")
    res = get_importer("yolo").run(_spec(roots, src, names="data.yaml"))
    assert [c.name for c in res.dataset.card.categories] == ["cat", "dog"]
    (src / "list.yaml").write_text("names: [x, y, z]\n", encoding="utf-8")
    assert [c.name for c in load_names(src / "list.yaml")] == ["x", "y", "z"]
    (src / "bad.yaml").write_text("- cat\n- dog\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="YAML mapping"):
        load_names(src / "bad.yaml")
    (src / "nonames.yaml").write_text("path: x\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="no usable 'names'"):
        load_names(src / "nonames.yaml")
    with pytest.raises(ValidationFailed, match="names file not found"):
        load_names(src / "nope.txt")
    (src / "labels" / "a.txt").write_text("5 0.5 0.5 0.2 0.4\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="unknown class 5"):
        get_importer("yolo").run(_spec(roots, src))
    (src / "labels" / "a.txt").write_text("0 0.5 0.5 0.2\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match=r"a\.txt:1"):
        get_importer("yolo").run(_spec(roots, src))


def test_missing_labels_dir_fails(roots, tmp_path):
    src = _src(tmp_path)
    with pytest.raises(ValidationFailed, match="labels directory not found"):
        get_importer("yolo").run(_spec(roots, src, labels="lbls"))
