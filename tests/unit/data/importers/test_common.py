from pathlib import Path

import pytest
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.data.importers.common import (
    IMAGE_EXTS,
    image_size,
    iter_images,
    load_categories,
    make_view,
    read_csv,
    rel_posix,
)


def _img(path: Path, size=(12, 7)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (10, 20, 30)).save(path)


def test_iter_images_recursive_sorted_and_filtered(tmp_path):
    _img(tmp_path / "b.jpg")
    _img(tmp_path / "sub" / "a.png")
    _img(tmp_path / "A.JPG")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    found = [rel_posix(p, tmp_path) for p in iter_images(tmp_path)]
    assert found == ["A.JPG", "b.jpg", "sub/a.png"]
    assert ".jpeg" in IMAGE_EXTS
    with pytest.raises(ValidationFailed, match="image directory not found"):
        iter_images(tmp_path / "missing")


def test_image_size_reads_header_only(tmp_path):
    _img(tmp_path / "x.png", size=(31, 17))
    assert image_size(tmp_path / "x.png") == (31, 17)
    with pytest.raises(ValidationFailed, match="image not found"):
        image_size(tmp_path / "nope.png")
    (tmp_path / "fake.jpg").write_text("not an image", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="not a readable image"):
        image_size(tmp_path / "fake.jpg")


def test_make_view(tmp_path):
    _img(tmp_path / "d" / "v.jpg", size=(9, 4))
    view = make_view(tmp_path, "d/v.jpg")
    assert (view.path, view.width, view.height) == ("d/v.jpg", 9, 4)


def test_read_csv_header_bom_and_required(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("﻿image,label\na.jpg,1\nb.jpg,2\n", encoding="utf-8")
    header, rows = read_csv(p, required=["image", "label"])
    assert header == ["image", "label"]
    assert rows == [{"image": "a.jpg", "label": "1"}, {"image": "b.jpg", "label": "2"}]
    with pytest.raises(ValidationFailed, match="lacks columns"):
        read_csv(p, required=["image", "score"])
    with pytest.raises(ValidationFailed, match="CSV not found"):
        read_csv(tmp_path / "none.csv", required=[])


def test_load_categories_inline_file_and_errors(tmp_path):
    assert load_categories(None, tmp_path) == []
    cats = load_categories('[{"id": 0, "name": "a"}]', tmp_path)
    assert [(c.id, c.name) for c in cats] == [(0, "a")]
    (tmp_path / "c.json").write_text('[{"id": 1, "name": "b"}]', encoding="utf-8")
    assert load_categories("c.json", tmp_path)[0].name == "b"
    with pytest.raises(ValidationFailed, match="categories file not found"):
        load_categories("missing.json", tmp_path)
    with pytest.raises(ValidationFailed, match="bad categories"):
        load_categories('[{"id": "x"}]', tmp_path)
