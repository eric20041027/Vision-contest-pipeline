from pathlib import Path

import pytest
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.importers.image_csv import infer_task


def _img(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (9, 9, 9)).save(path)


def _spec(roots, importer, src, **opts):
    return ImportSpec(
        importer=importer,
        src=src,
        name="ds",
        options=opts,
        license="CC0",
        url="https://example.org",
        downloaded_at="2026-09-03",
        data_root=roots.data,
        configs_root=roots.configs,
    )


def test_imagefolder(roots, tmp_path):
    src = tmp_path / "src"
    for rel in ("dog/1.jpg", "cat/2.png", "cat/3.jpg"):
        _img(src / rel)
    (src / "README.txt").write_text("ignored", encoding="utf-8")
    res = get_importer("imagefolder").run(_spec(roots, "imagefolder", src))
    ds = res.dataset
    assert ds.card.task == "cls"
    assert [(c.id, c.name) for c in ds.card.categories] == [(0, "cat"), (1, "dog")]
    assert {s.sample_id: s.labels.cls for s in ds.samples} == {
        "cat/2.png": 0,
        "cat/3.jpg": 0,
        "dog/1.jpg": 1,
    }
    with pytest.raises(ValidationFailed, match="no class directories"):
        get_importer("imagefolder").run(_spec(roots, "imagefolder", src, root="cat"))


def _csv_src(tmp_path, text: str) -> Path:
    src = tmp_path / "src"
    for rel in ("a.jpg", "b.jpg", "c.jpg"):
        _img(src / "images" / rel)
    (src / "labels.csv").write_text(text, encoding="utf-8")
    return src


def test_image_csv_cls_auto(roots, tmp_path):
    src = _csv_src(tmp_path, "path,label,is_gold\na.jpg,2,1\nb.jpg,10,0\nc.jpg,2,1\n")
    res = get_importer("image_csv").run(_spec(roots, "image_csv", src, gold_col="is_gold"))
    ds = res.dataset
    assert ds.card.task == "cls"
    assert [(c.id, c.name) for c in ds.card.categories] == [(0, "2"), (1, "10")]
    assert ds.by_id["a.jpg"].labels.cls == 0 and ds.by_id["b.jpg"].labels.cls == 1
    assert ds.by_id["b.jpg"].label_source == "derived" and ds.by_id["a.jpg"].label_source == "gold"


def test_image_csv_multilabel_and_regression(roots, tmp_path):
    src = _csv_src(tmp_path, "path,acl,mcl\na.jpg,1,0\nb.jpg,0,0\nc.jpg,1,1\n")
    res = get_importer("image_csv").run(_spec(roots, "image_csv", src))
    assert res.dataset.card.task == "multilabel"
    assert res.dataset.by_id["c.jpg"].labels.targets == {"acl": 1.0, "mcl": 1.0}
    assert [c.name for c in res.dataset.card.categories] == ["acl", "mcl"]
    src2 = _csv_src(tmp_path / "r", "path,age,note\na.jpg,37.5,x\nb.jpg,4,y\nc.jpg,0.25,z\n")
    res2 = get_importer("image_csv").run(
        _spec(roots, "image_csv", src2, target_cols="age", task="regression")
    )
    assert res2.dataset.card.task == "regression"
    assert res2.dataset.by_id["a.jpg"].labels.targets == {"age": 37.5}
    assert res2.dataset.by_id["a.jpg"].meta == {"note": "x"}


def test_image_csv_errors(roots, tmp_path):
    src = _csv_src(tmp_path, "path,label\na.jpg,1\nmissing.jpg,2\n")
    with pytest.raises(ValidationFailed, match="image not found"):
        get_importer("image_csv").run(_spec(roots, "image_csv", src))
    src2 = _csv_src(tmp_path / "t", "path,label\na.jpg,1\n")
    with pytest.raises(ValidationFailed, match="task"):
        get_importer("image_csv").run(_spec(roots, "image_csv", src2, task="pose"))
    with pytest.raises(ValidationFailed, match="no target columns"):
        get_importer("image_csv").run(_spec(roots, "image_csv", src2, target_cols=""))
    src3 = _csv_src(tmp_path / "u", "path,score\na.jpg,abc\n")
    with pytest.raises(ValidationFailed, match=r"labels\.csv:2"):
        get_importer("image_csv").run(_spec(roots, "image_csv", src3, task="regression"))


def test_infer_task():
    assert infer_task([{"l": "1"}, {"l": "3"}], ["l"]) == "cls"
    assert infer_task([{"a": "1", "b": "0"}, {"a": "0", "b": "1"}], ["a", "b"]) == "multilabel"
    assert infer_task([{"a": "0.5"}], ["a"]) == "regression"
    assert infer_task([{"a": "1"}, {"a": "0"}], ["a"]) == "cls"


def test_cls_categories_are_deterministic_for_equal_ints():
    from vcp.data.importers.image_csv import _cls_categories

    cats = _cls_categories(["1", "01", "b", "a", "1"])
    assert [(c.id, c.name) for c in cats] == [(0, "01"), (1, "1"), (2, "a"), (3, "b")]


def test_imagefolder_without_images_fails(roots, tmp_path):
    src = tmp_path / "src"
    (src / "cat").mkdir(parents=True)
    (src / "cat" / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="no images found"):
        get_importer("imagefolder").run(_spec(roots, "imagefolder", src))


def test_image_csv_empty_fails(roots, tmp_path):
    src = _csv_src(tmp_path, "path,label\n")
    with pytest.raises(ValidationFailed, match="no data rows"):
        get_importer("image_csv").run(_spec(roots, "image_csv", src))
