import pytest

from helpers import (
    ML_CATS,
    REG_CATS,
    cls_samples,
    det_samples,
    make_card,
    multilabel_samples,
    regression_samples,
)
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.dataset import Dataset
from vcp.measure.converters import CONVERTERS, get_converter, register_converter
from vcp.measure.converters.base import ConvertContext
from vcp.measure.predictions import write_predictions
from vcp.measure.schema import Prediction


def _ctx(ds, **opts):
    return ConvertContext(
        dataset=ds, subset_ids={s.sample_id for s in ds.samples}, export_dir=None, options=opts
    )


HEADER = "id,cat,dog,bird\n"
SHORT_CSV = HEADER + "s0000,0.1,0.2\n"  # one field short: DictReader pads it with None
NAN_CSV = HEADER + "s0000,nan,0,0\n"  # float() accepts it, the schema does not
NOID_CSV = HEADER + " ,0.1,0.2,0.7\n"  # empty id cell
DET_CSV = HEADER + "s0000,1,0,0\n"


def test_registry():
    assert set(CONVERTERS) == {"coco_results", "jsonl", "scores_csv", "yolo_txt"}
    assert get_converter("jsonl").name == "jsonl"
    with pytest.raises(RegistryError):
        get_converter("parquet")
    with pytest.raises(RegistryError, match="already registered"):
        register_converter(CONVERTERS["jsonl"])  # a plugin must not shadow a built-in


def test_jsonl_passthrough(tmp_path):
    samples = cls_samples(3, seed=0)
    ds = Dataset.from_parts(make_card("cls"), samples)
    preds = [
        Prediction(sample_id=s.sample_id, scores={"cat": 1.0, "dog": 0.0, "bird": 0.0})
        for s in samples
    ]
    write_predictions(tmp_path / "p.jsonl", preds)
    assert get_converter("jsonl").convert(tmp_path / "p.jsonl", _ctx(ds)) == preds


def test_scores_csv_cls_multilabel_regression(tmp_path):
    cls_ds = Dataset.from_parts(make_card("cls"), cls_samples(3, seed=0))
    (tmp_path / "cls.csv").write_text(
        "id,cat,dog,bird\ns0000,0.7,0.2,0.1\ns0001,0.1,0.8,0.1\ns0002,0.2,0.2,0.6\n",
        encoding="utf-8",
    )
    preds = get_converter("scores_csv").convert(tmp_path / "cls.csv", _ctx(cls_ds))
    assert [p.sample_id for p in preds] == ["s0000", "s0001", "s0002"]
    assert preds[1].scores == {"cat": 0.1, "dog": 0.8, "bird": 0.1}

    ml_ds = Dataset.from_parts(make_card("multilabel", categories=ML_CATS), multilabel_samples(2))
    (tmp_path / "ml.csv").write_text(
        "StudyID,ACL,MCL,EFF\ns0000,0.9,0.1,0.5\ns0001,0.2,0.2,0.2\n", encoding="utf-8"
    )
    preds = get_converter("scores_csv").convert(
        tmp_path / "ml.csv", _ctx(ml_ds, id_col="StudyID", columns="ACL:acl,MCL:mcl,EFF:effusion")
    )
    assert preds[0].scores == {"acl": 0.9, "mcl": 0.1, "effusion": 0.5}

    reg_ds = Dataset.from_parts(make_card("regression", categories=REG_CATS), regression_samples(2))
    (tmp_path / "reg.csv").write_text("path,age,note\ns0000,31.5,x\ns0001,40,y\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="unexpected columns"):
        get_converter("scores_csv").convert(tmp_path / "reg.csv", _ctx(reg_ds))
    preds = get_converter("scores_csv").convert(
        tmp_path / "reg.csv", _ctx(reg_ds, ignore_extra="true")
    )
    assert preds[1].targets == {"age": 40.0}


def test_scores_csv_errors(tmp_path):
    cls_ds = Dataset.from_parts(make_card("cls"), cls_samples(2, seed=0))
    (tmp_path / "missing.csv").write_text("id,cat,dog\ns0000,1,0\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="missing columns"):
        get_converter("scores_csv").convert(tmp_path / "missing.csv", _ctx(cls_ds))
    (tmp_path / "bad.csv").write_text("id,cat,dog,bird\ns0000,x,0,0\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="bad.csv:2"):
        get_converter("scores_csv").convert(tmp_path / "bad.csv", _ctx(cls_ds))
    with pytest.raises(ValidationFailed, match="columns="):
        get_converter("scores_csv").convert(tmp_path / "bad.csv", _ctx(cls_ds, columns="nocolon"))
    # A truncated row: DictReader pads the missing fields with None, so without the guard
    # float(None) escapes as a bare TypeError -> ABORT for what is plainly bad user data.
    (tmp_path / "short.csv").write_text(SHORT_CSV, encoding="utf-8")
    with pytest.raises(ValidationFailed, match="fewer fields"):
        get_converter("scores_csv").convert(tmp_path / "short.csv", _ctx(cls_ds))
    # float() accepts nan/inf; the schema rejects them. That rejection is a pydantic
    # ValidationError and must reach the caller wrapped, with its location.
    (tmp_path / "nan.csv").write_text(NAN_CSV, encoding="utf-8")
    with pytest.raises(ValidationFailed, match="nan.csv:2"):
        get_converter("scores_csv").convert(tmp_path / "nan.csv", _ctx(cls_ds))
    # An empty id cell is the same shape of escape.
    (tmp_path / "noid.csv").write_text(NOID_CSV, encoding="utf-8")
    with pytest.raises(ValidationFailed, match="noid.csv:2"):
        get_converter("scores_csv").convert(tmp_path / "noid.csv", _ctx(cls_ds))


def test_scores_csv_rejects_a_list_payload_task(tmp_path):
    """Which tasks this converter serves is derived from the task registry, not hardcoded."""
    det_ds = Dataset.from_parts(make_card("det"), det_samples(2, seed=0))
    (tmp_path / "d.csv").write_text(DET_CSV, encoding="utf-8")
    with pytest.raises(ValidationFailed, match="predicts 'boxes'"):
        get_converter("scores_csv").convert(tmp_path / "d.csv", _ctx(det_ds))
