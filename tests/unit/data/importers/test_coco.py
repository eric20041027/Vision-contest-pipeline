import json
from pathlib import Path

import pytest
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.importers.coco import mask_from_segmentation


def _img(path: Path, size=(16, 8)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (5, 5, 5)).save(path)


def _spec(roots, src, **opts):
    return ImportSpec(
        importer="coco",
        src=src,
        name="coco",
        options=opts,
        license="CC-BY",
        url="https://example.org",
        downloaded_at="2026-09-03",
        data_root=roots.data,
        configs_root=roots.configs,
    )


def _doc(seg=False):
    anns = [
        {"id": 1, "image_id": 10, "category_id": 1, "bbox": [1, 1, 4, 3], "area": 12, "iscrowd": 0},
        {"id": 2, "image_id": 10, "category_id": 2, "bbox": [6, 2, 5, 5], "area": 25, "iscrowd": 1},
        {"id": 3, "image_id": 99, "category_id": 1, "bbox": [0, 0, 1, 1], "area": 1, "iscrowd": 0},
    ]
    if seg:
        anns[0]["segmentation"] = [[1, 1, 5, 1, 5, 4]]
        anns[1]["segmentation"] = {"counts": "abc", "size": [8, 16]}
        anns.append(
            {
                "id": 4,
                "image_id": 11,
                "category_id": 2,
                "bbox": [0, 0, 2, 2],
                "area": 4,
                "iscrowd": 0,
                "segmentation": {"counts": [0, 3, 5], "size": [8, 16]},
            }
        )
        anns.append(
            {
                "id": 5,
                "image_id": 11,
                "category_id": 1,
                "bbox": [0, 0, 2, 2],
                "area": 4,
                "iscrowd": 0,
            }
        )
    return {
        "images": [
            {"id": 10, "file_name": "a.jpg", "width": 16, "height": 8},
            {"id": 11, "file_name": "sub/b.jpg"},
        ],
        "annotations": anns,
        "categories": [
            {"id": 1, "name": "bottle", "supercategory": "plastic"},
            {"id": 2, "name": "net"},
        ],
    }


def _src(tmp_path, doc) -> Path:
    src = tmp_path / "src"
    _img(src / "images" / "a.jpg")
    _img(src / "images" / "sub" / "b.jpg", size=(10, 10))
    (src / "instances.json").write_text(json.dumps(doc), encoding="utf-8")
    return src


def test_import_det(roots, tmp_path):
    res = get_importer("coco").run(_spec(roots, _src(tmp_path, _doc())))
    ds = res.dataset
    assert (res.rows_read, res.rows_skipped, res.samples_written) == (3, 1, 2)
    assert [s.sample_id for s in ds.samples] == ["a.jpg", "sub/b.jpg"]
    a = ds.by_id["a.jpg"]
    assert a.meta["coco_image_id"] == 10 and (a.views[0].width, a.views[0].height) == (16, 8)
    assert [(b.category_id, b.x, b.y, b.w, b.h) for b in a.labels.boxes] == [
        (1, 1.0, 1.0, 4.0, 3.0),
        (2, 6.0, 2.0, 5.0, 5.0),
    ]
    assert a.labels.boxes[1].meta == {"iscrowd": 1}
    b = ds.by_id["sub/b.jpg"]
    assert (b.views[0].width, b.views[0].height) == (10, 10) and b.labels.boxes == []
    assert [(c.id, c.name, c.meta) for c in ds.card.categories] == [
        (1, "bottle", {"supercategory": "plastic"}),
        (2, "net", {}),
    ]
    reason = json.loads(res.skipped_reasons_path.read_text().splitlines()[0])["reason"]
    assert "unknown image_id" in reason


def test_import_seg(roots, tmp_path):
    res = get_importer("coco").run(_spec(roots, _src(tmp_path, _doc(seg=True)), task="seg"))
    ds = res.dataset
    assert ds.card.task == "seg"
    masks = ds.by_id["a.jpg"].labels.masks
    assert masks[0].polygon == [[1.0, 1.0, 5.0, 1.0, 5.0, 4.0]] and masks[0].category_id == 1
    assert masks[1].rle == "abc" and masks[1].meta == {"iscrowd": 1, "size": [8, 16]}
    b = ds.by_id["sub/b.jpg"].labels.masks
    assert b[0].rle == "0,3,5" and b[0].meta["rle_encoding"] == "uncompressed"
    assert res.rows_skipped == 2  # unknown image_id + annotation without segmentation


def test_missing_image_and_bad_task(roots, tmp_path):
    src = _src(tmp_path, _doc())
    (src / "images" / "a.jpg").unlink()
    with pytest.raises(ValidationFailed, match="images missing"):
        get_importer("coco").run(_spec(roots, src))
    with pytest.raises(ValidationFailed, match="task"):
        get_importer("coco").run(_spec(roots, _src(tmp_path / "t2", _doc()), task="cls"))


def test_mask_from_segmentation_variants():
    result = mask_from_segmentation([[0, 0, 1, 0, 1, 1]], 1, {})
    assert result.polygon == [[0.0, 0.0, 1.0, 0.0, 1.0, 1.0]]
    m = mask_from_segmentation({"counts": "xyz", "size": [4, 4]}, 2, {"iscrowd": 1})
    assert m.rle == "xyz" and m.meta == {"iscrowd": 1, "size": [4, 4]}
    u = mask_from_segmentation({"counts": [1, 2], "size": [4, 4]}, 2, {})
    assert u.rle == "1,2" and u.meta == {"size": [4, 4], "rle_encoding": "uncompressed"}
    assert mask_from_segmentation(None, 1, {}) is None
    assert mask_from_segmentation([], 1, {}) is None
