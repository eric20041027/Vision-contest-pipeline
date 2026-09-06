import pytest

from helpers import det_samples, make_card
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample, View
from vcp.measure.schema import PredBox, Prediction
from vcp.submit.writers import get_writer
from vcp.submit.writers.base import WriteContext
from vcp.submit.writers.csv_boxes import from_abs_xywh


def _det():
    ds = Dataset.from_parts(make_card("det", name="d"), det_samples(2, gold_frac=0.0))
    preds = [
        Prediction(sample_id="s0000", boxes=[PredBox(x=1, y=2, w=4, h=2, category_id=1, score=0.9)])
    ]
    return ds, preds


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_default_columns_and_formats(tmp_path):
    ds, preds = _det()
    w = get_writer("csv_boxes")
    out = tmp_path / "submission.csv"
    res = w.write(preds, WriteContext(ds, list(ds.samples), {}, out))
    assert res.rows == 1 and res.samples == 1
    assert _lines(out) == ["image_filename,label_id,x,y,w,h,score", "s0000,1,1.0,2.0,4.0,2.0,0.9"]
    assert b"\r" not in out.read_bytes()
    w.write(preds, WriteContext(ds, list(ds.samples), {"box_format": "xyxy"}, out))
    assert _lines(out)[1] == "s0000,1,1.0,2.0,5.0,4.0,0.9"
    w.write(preds, WriteContext(ds, list(ds.samples), {"box_format": "cxcywh"}, out))
    assert _lines(out)[1] == "s0000,1,3.0,3.0,4.0,2.0,0.9"
    w.write(preds, WriteContext(ds, list(ds.samples), {"coords": "norm"}, out))
    assert _lines(out)[1] == "s0000,1,0.125,0.25,0.5,0.25,0.9"
    assert from_abs_xywh(1, 2, 4, 2, box_format="xyxy", coords="abs", width=8, height=8) == (
        1,
        2,
        5,
        4,
    )


def test_labels_columns_and_ids(tmp_path):
    ds, preds = _det()
    w = get_writer("csv_boxes")
    out = tmp_path / "submission.csv"
    opts = {"label": "category_name", "columns": "image=file,score=conf", "id_field": "view_stem"}
    w.write(preds, WriteContext(ds, list(ds.samples), opts, out))
    assert _lines(out) == ["file,label_id,x,y,w,h,conf", "s0000,dog,1.0,2.0,4.0,2.0,0.9"]
    with pytest.raises(ValidationFailed, match="option=columns"):
        w.write(preds, WriteContext(ds, list(ds.samples), {"columns": "area=a"}, out))
    with pytest.raises(ValidationFailed, match="option=box_format"):
        w.write(preds, WriteContext(ds, list(ds.samples), {"box_format": "polar"}, out))


def test_norm_needs_view_size(tmp_path):
    samples = [Sample(sample_id="n0", views=[View(path="n0.png")], label_source="none")]
    ds = Dataset.from_parts(make_card("det", name="n"), samples)
    preds = [
        Prediction(sample_id="n0", boxes=[PredBox(x=1, y=1, w=1, h=1, category_id=0, score=1)])
    ]
    with pytest.raises(ValidationFailed, match="coords=norm needs"):
        get_writer("csv_boxes").write(
            preds, WriteContext(ds, samples, {"coords": "norm"}, tmp_path / "s.csv")
        )
