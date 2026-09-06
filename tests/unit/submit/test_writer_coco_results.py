import json

import pytest

from helpers import SEG_CATS, det_samples, make_card
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample, View
from vcp.measure.schema import PredBox, Prediction, PredMask
from vcp.submit.writers import get_writer
from vcp.submit.writers.base import WriteContext


def _det():
    samples = [
        s.model_copy(update={"meta": {"image_id": str(10 + i)}})
        for i, s in enumerate(det_samples(3, gold_frac=0.0))
    ]
    ds = Dataset.from_parts(make_card("det", name="d"), samples)
    preds = [
        Prediction(
            sample_id="s0000",
            boxes=[
                PredBox(x=1, y=2, w=3, h=4, category_id=1, score=0.9),
                PredBox(x=0.5, y=0.5, w=1, h=1, category_id=0, score=0.25),
            ],
        ),
        Prediction(sample_id="s0002", boxes=[PredBox(x=2, y=2, w=2, h=2, category_id=2, score=1)]),
    ]
    return ds, preds


def test_entries_are_sorted_compact_and_deterministic(tmp_path):
    ds, preds = _det()
    w = get_writer("coco_results")
    out = tmp_path / "results.json"
    res = w.write(list(reversed(preds)), WriteContext(ds, list(ds.samples), {}, out))
    assert res.rows == 3 and res.samples == 2 and res.missing == []
    text = out.read_bytes()
    assert b"\r" not in text
    assert text.endswith(b"\n") and b" " not in text.split(b"\n")[0]
    entries = json.loads(text)
    assert entries[0] == {
        "bbox": [1.0, 2.0, 3.0, 4.0],
        "category_id": 1,
        "image_id": "s0000",
        "score": 0.9,
    }
    assert [e["image_id"] for e in entries] == ["s0000", "s0000", "s0002"]
    again = tmp_path / "b.json"
    w.write(preds, WriteContext(ds, list(ds.samples), {}, again))
    assert again.read_bytes() == text


def test_numeric_ids_become_ints(tmp_path):
    ds, preds = _det()
    out = tmp_path / "results.json"
    get_writer("coco_results").write(
        preds, WriteContext(ds, list(ds.samples), {"id_field": "meta.image_id"}, out)
    )
    assert [e["image_id"] for e in json.loads(out.read_text(encoding="utf-8"))] == [10, 10, 12]


def test_masks_polygon_and_rle(tmp_path):
    samples = [
        Sample(sample_id="m0", views=[View(path="m0.png", width=8, height=6)], label_source="none"),
        Sample(sample_id="m1", views=[View(path="m1.png")], label_source="none"),
    ]
    ds = Dataset.from_parts(make_card("seg", name="s", categories=SEG_CATS), samples)
    w = get_writer("coco_results")
    preds = [
        Prediction(
            sample_id="m0",
            masks=[
                PredMask(category_id=0, score=0.5, polygon=[[0, 0, 4, 0, 4, 4]]),
                PredMask(category_id=1, score=0.5, rle="abc"),
            ],
        )
    ]
    out = tmp_path / "results.json"
    w.write(preds, WriteContext(ds, samples, {}, out))
    entries = json.loads(out.read_text(encoding="utf-8"))
    assert entries[0]["segmentation"] == [[0.0, 0.0, 4.0, 0.0, 4.0, 4.0]]
    assert entries[1]["segmentation"] == {"size": [6, 8], "counts": "abc"}
    bad = [Prediction(sample_id="m1", masks=[PredMask(category_id=0, score=0.5, rle="abc")])]
    with pytest.raises(ValidationFailed, match="width/height"):
        w.write(bad, WriteContext(ds, samples, {}, out))


def test_zero_padded_ids_stay_strings(tmp_path):
    from vcp.submit.writers.coco_results import _image_id

    assert _image_id("7") == 7 and _image_id("07") == "07" and _image_id("s7") == "s7"
    samples = [
        Sample(sample_id="a", views=[View(path="a.png")], label_source="none", meta={"k": "07"}),
        Sample(sample_id="b", views=[View(path="b.png")], label_source="none", meta={"k": "7"}),
    ]
    ds = Dataset.from_parts(make_card("det", name="z"), samples)
    preds = [
        Prediction(sample_id=sid, boxes=[PredBox(x=0, y=0, w=1, h=1, category_id=0, score=0.5)])
        for sid in ("a", "b")
    ]
    out = tmp_path / "results.json"
    get_writer("coco_results").write(preds, WriteContext(ds, samples, {"id_field": "meta.k"}, out))
    ids = [e["image_id"] for e in json.loads(out.read_text(encoding="utf-8"))]
    assert ids == ["07", 7]
