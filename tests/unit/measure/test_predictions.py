import pytest

from helpers import (
    CATS,
    ML_CATS,
    REG_CATS,
    cls_samples,
    det_samples,
    make_card,
    multilabel_samples,
    perfect_predictions,
    regression_samples,
)
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.measure.predictions import (
    check_predictions,
    predictions_by_id,
    read_predictions,
    write_predictions,
)
from vcp.measure.schema import PredBox, Prediction


def test_write_read_roundtrip_sorted_lf(tmp_path):
    preds = [Prediction(sample_id="b", boxes=[]), Prediction(sample_id="a", boxes=[])]
    sha = write_predictions(tmp_path / "p.jsonl", preds)
    raw = (tmp_path / "p.jsonl").read_bytes()
    assert b"\r\n" not in raw and raw.startswith(b'{"sample_id":"a"')
    again = read_predictions(tmp_path / "p.jsonl")
    assert [p.sample_id for p in again] == ["a", "b"]
    assert len(sha) == 64 and write_predictions(tmp_path / "q.jsonl", again) == sha
    (tmp_path / "bad.jsonl").write_text('{"sample_id": "a"}\n', encoding="utf-8")
    with pytest.raises(ValidationFailed, match="bad.jsonl:1"):
        read_predictions(tmp_path / "bad.jsonl")


def test_check_predictions_det_rules():
    samples = det_samples(6, seed=0)
    ds = Dataset.from_parts(make_card("det"), samples)
    ids = {s.sample_id for s in samples}
    preds = perfect_predictions(samples[:4], ds.card)
    kept, stats = check_predictions(preds, ds, ids)
    assert (stats.samples, stats.predicted, stats.empty, stats.unknown) == (6, 4, 2, [])
    assert len(kept) == 4
    wrong = [Prediction(sample_id="s0000", scores={"cat": 1.0, "dog": 0.0, "bird": 0.0})]
    with pytest.raises(ValidationFailed, match="boxes"):
        check_predictions(wrong, ds, ids)
    bad_cat = [
        Prediction(sample_id="s0000", boxes=[PredBox(x=0, y=0, w=1, h=1, category_id=9, score=1)])
    ]
    with pytest.raises(ValidationFailed, match="category"):
        check_predictions(bad_cat, ds, ids)
    ghost = [Prediction(sample_id="ghost", boxes=[])]
    with pytest.raises(ValidationFailed, match="unknown"):
        check_predictions(ghost, ds, ids)
    kept, stats = check_predictions(ghost, ds, ids, allow_unknown=True)
    assert kept == [] and stats.unknown == ["ghost"] and stats.empty == 6
    dup = [Prediction(sample_id="s0000", boxes=[]), Prediction(sample_id="s0000", boxes=[])]
    with pytest.raises(ValidationFailed, match="duplicate"):
        check_predictions(dup, ds, ids)


def test_check_predictions_cls_requires_every_sample():
    samples = cls_samples(5, seed=0)
    ds = Dataset.from_parts(make_card("cls"), samples)
    ids = {s.sample_id for s in samples}
    preds = perfect_predictions(samples, ds.card)
    kept, stats = check_predictions(preds, ds, ids)
    assert stats.predicted == 5 and stats.empty == 0
    with pytest.raises(ValidationFailed, match="missing predictions for 1"):
        check_predictions(preds[:-1], ds, ids)
    wrong_keys = [Prediction(sample_id=s.sample_id, scores={"cat": 1.0}) for s in samples]
    with pytest.raises(ValidationFailed, match="category names"):
        check_predictions(wrong_keys, ds, ids)
    assert set(predictions_by_id(preds)) == ids


@pytest.mark.parametrize("task", ["cls", "multilabel", "regression"])
def test_a_mapping_payload_task_requires_a_row_for_every_subset_sample(task):
    """scores / targets have no legitimate empty value, so a missing row is an error. The rule
    is derived from the task registry, not from a hand-written task set: dropping any one task
    from such a set would silently fold its missing rows into ``empty`` and every later reading
    would be computed over fewer samples than the subset."""
    builder, cats = {
        "cls": (cls_samples, CATS),
        "multilabel": (multilabel_samples, ML_CATS),
        "regression": (regression_samples, REG_CATS),
    }[task]
    samples = builder(4, seed=0)
    ds = Dataset.from_parts(make_card(task, categories=cats), samples)
    preds = perfect_predictions(samples, ds.card)
    ids = {s.sample_id for s in samples}
    assert check_predictions(preds, ds, ids)[1].empty == 0
    with pytest.raises(ValidationFailed, match="missing predictions"):
        check_predictions(preds[:-1], ds, ids)


def test_a_list_payload_task_treats_a_missing_row_as_predicted_nothing():
    samples = det_samples(4, seed=0)
    ds = Dataset.from_parts(make_card("det"), samples)
    preds = perfect_predictions(samples, ds.card)
    kept, stats = check_predictions(preds[:-1], ds, {s.sample_id for s in samples})
    assert len(kept) == 3 and stats.empty == 1 and stats.samples == 4
