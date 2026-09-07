import pytest

from helpers import REG_CATS, cls_samples, make_card, regression_samples
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.measure.converters import ConvertContext, ScoresCsvConverter
from vcp.measure.schema import Prediction
from vcp.submit.writers import get_writer
from vcp.submit.writers.base import WriteContext


def _cls(n=4):
    ds = Dataset.from_parts(make_card("cls", name="t"), cls_samples(n, gold_frac=0.0))
    preds = [
        Prediction(sample_id=s.sample_id, scores={"cat": 0.5, "dog": 0.25, "bird": 0.25})
        for s in ds.samples
    ]
    return ds, preds


def _ctx(ds, tmp_path, **options):
    return WriteContext(ds, list(ds.samples), options, tmp_path / "submission.csv")


def test_header_rows_and_determinism(tmp_path):
    ds, preds = _cls()
    w = get_writer("scores_csv")
    res = w.write(list(reversed(preds)), _ctx(ds, tmp_path))
    text = (tmp_path / "submission.csv").read_bytes()
    assert res.rows == 4 and res.samples == 4 and res.missing == []
    assert text.startswith(b"id,cat,dog,bird\ns0000,0.5,0.25,0.25\n") and b"\r" not in text
    again = tmp_path / "again.csv"
    w.write(preds, WriteContext(ds, list(ds.samples), {}, again))
    assert again.read_bytes() == text


def test_missing_rows_fail_unless_allowed(tmp_path):
    ds, preds = _cls()
    w = get_writer("scores_csv")
    with pytest.raises(ValidationFailed, match="missing: 1 samples") as ei:
        w.write(preds[:-1], _ctx(ds, tmp_path))
    assert ei.value.fields == {"missing": 1}
    res = w.write(preds[:-1], _ctx(ds, tmp_path, allow_missing="true"))
    assert res.rows == 3 and res.missing == ["s0003"]


def test_rename_and_id_options(tmp_path):
    ds, preds = _cls(2)
    w = get_writer("scores_csv")
    w.write(preds, _ctx(ds, tmp_path, id_col="image", columns="cat=Cat", id_field="view_stem"))
    lines = (tmp_path / "submission.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "image,Cat,dog,bird" and lines[1].startswith("s0000,")
    with pytest.raises(ValidationFailed, match="missing_key"):
        bad = [Prediction(sample_id="s0000", scores={"cat": 1.0}), preds[1]]
        w.write(bad, _ctx(ds, tmp_path))
    with pytest.raises(ValidationFailed, match="option=colour"):
        w.write(preds, _ctx(ds, tmp_path, colour="red"))


def test_round_trip_through_the_converter(tmp_path):
    ds, preds = _cls(3)
    get_writer("scores_csv").write(preds, _ctx(ds, tmp_path))
    ctx = ConvertContext(ds, {s.sample_id for s in ds.samples})
    back = ScoresCsvConverter().convert(tmp_path / "submission.csv", ctx)
    assert sorted(back, key=lambda p: p.sample_id) == preds


def test_regression_targets(tmp_path):
    card = make_card("regression", name="r", categories=REG_CATS)
    ds = Dataset.from_parts(card, regression_samples(2))
    preds = [Prediction(sample_id=s.sample_id, targets={"age": 30.5}) for s in ds.samples]
    get_writer("scores_csv").write(preds, _ctx(ds, tmp_path))
    lines = (tmp_path / "submission.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "id,age" and lines[1] == "s0000,30.5"


def test_scores_csv_needs_card_categories(tmp_path):
    samples = [
        s.model_copy(update={"labels": None, "label_source": "none"}) for s in regression_samples(2)
    ]
    ds = Dataset.from_parts(make_card("regression", name="r0", categories=[]), samples)
    preds = [Prediction(sample_id=s.sample_id, targets={"age": 30.5}) for s in ds.samples]
    with pytest.raises(ValidationFailed, match="needs the dataset card's categories") as ei:
        get_writer("scores_csv").write(preds, _ctx(ds, tmp_path))
    assert ei.value.fields == {"dataset": "r0"}


@pytest.mark.parametrize("columns", ["cat=dog", "cat=id", "cat=same,dog=same"])
def test_duplicate_headers_are_rejected_before_writing(tmp_path, columns):
    ds, preds = _cls()
    ctx = _ctx(ds, tmp_path, columns=columns)
    with pytest.raises(ValidationFailed, match="duplicate_column:") as ei:
        get_writer("scores_csv").write(preds, ctx)
    assert ei.value.fields["column"] == columns.split(",")[0].split("=")[1]
    assert not ctx.out.exists()
