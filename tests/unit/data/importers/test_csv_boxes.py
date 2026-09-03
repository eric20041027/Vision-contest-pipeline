import json
from pathlib import Path

import pytest
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.importers.csv_boxes import box_problem, to_abs_xywh
from vcp.data.schema import View


def _img(path: Path, size=(20, 10)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (1, 2, 3)).save(path)


def _spec(roots, src, **opts):
    return ImportSpec(
        importer="csv_boxes",
        src=src,
        name="boxes",
        options=opts,
        license="CC0",
        url="https://example.org",
        downloaded_at="2026-09-03",
        data_root=roots.data,
        configs_root=roots.configs,
    )


def _src(tmp_path, csv_text: str) -> Path:
    src = tmp_path / "src"
    for name in ("a.jpg", "b.jpg", "sub/c.png"):
        _img(src / "images" / name)
    (src / "labels.csv").write_text(csv_text, encoding="utf-8")
    return src


GOOD = (
    "image_filename,label_id,x,y,w,h,confidence\n"
    "a.jpg,3,1,1,5,4,1.0\n"
    "a.jpg,7,10,2,9,7,0.9\n"
    "sub/c.png,3,0,0,20,10,1.0\n"
)


def test_import_boxes_negatives_and_derived_categories(roots, tmp_path):
    src = _src(tmp_path, GOOD)
    res = get_importer("csv_boxes").run(_spec(roots, src))
    ds = res.dataset
    assert (res.rows_read, res.rows_skipped, res.samples_written) == (3, 0, 3)
    assert [s.sample_id for s in ds.samples] == ["a.jpg", "b.jpg", "sub/c.png"]
    a = ds.by_id["a.jpg"]
    assert (a.views[0].width, a.views[0].height) == (20, 10)
    assert [(b.category_id, b.x, b.y, b.w, b.h) for b in a.labels.boxes] == [
        (3, 1.0, 1.0, 5.0, 4.0),
        (7, 10.0, 2.0, 9.0, 7.0),
    ]
    assert ds.by_id["b.jpg"].labels.boxes == []
    assert [(c.id, c.name) for c in ds.card.categories] == [(3, "3"), (7, "7")]
    assert ds.card.task == "det" and ds.card.image_root.endswith("src/images")


def test_explicit_categories_and_column_mapping(roots, tmp_path):
    src = _src(tmp_path, "file,cls,x1,y1,x2,y2\na.jpg,0,2,2,6,5\n")
    cats = json.dumps([{"id": 0, "name": "bottle"}, {"id": 1, "name": "net"}])
    res = get_importer("csv_boxes").run(
        _spec(
            roots,
            src,
            col_image="file",
            col_label="cls",
            col_x="x1",
            col_y="y1",
            col_w="x2",
            col_h="y2",
            box_format="xyxy",
            categories=cats,
        )
    )
    box = res.dataset.by_id["a.jpg"].labels.boxes[0]
    assert (box.x, box.y, box.w, box.h) == (2.0, 2.0, 4.0, 3.0)
    assert [c.name for c in res.dataset.card.categories] == ["bottle", "net"]


def test_bad_rows_abort_with_location_or_skip_with_reasons(roots, tmp_path):
    bad = (
        "image_filename,label_id,x,y,w,h\n"
        "a.jpg,3,1,1,5,4\n"
        "zzz.jpg,3,1,1,5,4\n"
        "a.jpg,x,1,1,5,4\n"
        "b.jpg,3,15,1,10,4\n"
        "b.jpg,3,1,1,0,4\n"
    )
    src = _src(tmp_path, bad)
    with pytest.raises(ValidationFailed, match=r"labels\.csv:3"):
        get_importer("csv_boxes").run(_spec(roots, src))
    res = get_importer("csv_boxes").run(_spec(roots, src, on_bad_row="skip"))
    assert (res.rows_read, res.rows_skipped, res.samples_written) == (5, 4, 3)
    txt = res.skipped_reasons_path.read_text(encoding="utf-8").splitlines()
    reasons = [json.loads(line)["reason"] for line in txt]
    assert any("unknown image" in r for r in reasons)
    assert any("unparsable" in r for r in reasons)
    assert any("exceeds image bounds" in r for r in reasons)
    assert any("non-positive size" in r for r in reasons)


def test_clean_reimport_removes_stale_skipped_reasons_file(roots, tmp_path):
    """F3: a re-import that has nothing to skip must clear a stale cache/import_skipped.jsonl
    left behind by an earlier, dirtier import of the same dataset name."""
    bad = "image_filename,label_id,x,y,w,h,confidence\na.jpg,3,1,1,5,4,1.0\nzzz.jpg,3,1,1,5,4,1.0\n"
    src = _src(tmp_path, bad)
    res = get_importer("csv_boxes").run(_spec(roots, src, on_bad_row="skip"))
    assert res.skipped_reasons_path is not None
    skipped_path = res.skipped_reasons_path
    assert skipped_path.is_file()

    (src / "labels.csv").write_text(GOOD, encoding="utf-8")
    res2 = get_importer("csv_boxes").run(_spec(roots, src, on_bad_row="skip"))
    assert res2.skipped_reasons_path is None
    assert not skipped_path.is_file()


def test_option_validation_and_missing_inputs(roots, tmp_path):
    src = _src(tmp_path, GOOD)
    with pytest.raises(ValidationFailed, match="box_format"):
        get_importer("csv_boxes").run(_spec(roots, src, box_format="wh"))
    with pytest.raises(ValidationFailed, match="CSV not found"):
        get_importer("csv_boxes").run(_spec(roots, src, csv="other.csv"))
    with pytest.raises(ValidationFailed, match="image directory not found"):
        get_importer("csv_boxes").run(_spec(roots, src, images="imgs"))


def test_to_abs_xywh_and_box_problem():
    assert to_abs_xywh(
        0.5,
        0.5,
        0.2,
        0.4,
        box_format="cxcywh",
        coords="norm",
        width=100,
        height=50,
    ) == (40.0, 15.0, 20.0, 20.0)
    assert to_abs_xywh(2, 3, 6, 9, box_format="xyxy", coords="abs", width=0, height=0) == (
        2,
        3,
        4,
        6,
    )
    assert to_abs_xywh(1, 2, 3, 4, box_format="xywh", coords="abs", width=0, height=0) == (
        1,
        2,
        3,
        4,
    )
    view = View(path="v.jpg", width=10, height=10)
    assert box_problem(0, 0, 10.9, 10, view) is None
    assert "exceeds" in box_problem(0, 0, 12, 10, view)
    assert "non-positive" in box_problem(0, 0, 0, 5, view)
    assert box_problem(-5, 0, 3, 3, View(path="v.jpg")) is None
