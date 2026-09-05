import pytest

from helpers import SEG_CATS, make_card, perfect_predictions, seg_samples
from vcp.core.errors import ValidationFailed
from vcp.data.schema import Labels, Mask, Sample, View
from vcp.measure.metrics import applicable_metrics, get_metric
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import Prediction, PredMask


def _run(metric, samples, card, preds, params=None):
    return get_metric(metric).compute(samples, predictions_by_id(preds), card, params or {})


def test_registered_and_perfect():
    assert applicable_metrics("seg") == ["dice", "miou"]
    samples = seg_samples(20, seed=0)
    card = make_card("seg", categories=SEG_CATS)
    perfect = perfect_predictions(samples, card)
    for name in ("dice", "miou"):
        res = _run(name, samples, card, perfect)
        assert res.value == pytest.approx(1.0) and res.n == 20
        assert res.per_class == {"road": pytest.approx(1.0), "water": pytest.approx(1.0)}


def test_empty_shifted_and_threshold():
    samples = seg_samples(20, seed=1)
    card = make_card("seg", categories=SEG_CATS)
    perfect = perfect_predictions(samples, card)
    empty = [Prediction(sample_id=p.sample_id, masks=[]) for p in perfect]
    assert _run("dice", samples, card, empty).value == 0.0
    assert _run("miou", samples, card, empty).value == 0.0
    shifted = [
        Prediction(
            sample_id=p.sample_id,
            masks=[
                PredMask(
                    category_id=m.category_id,
                    score=1.0,
                    polygon=[
                        [v + (1 if i % 2 == 0 else 0) for i, v in enumerate(ring)]
                        for ring in m.polygon
                    ],
                )
                for m in p.masks
            ],
        )
        for p in perfect
    ]
    d = _run("dice", samples, card, shifted).value
    assert 0.0 < d < 1.0
    assert _run("miou", samples, card, shifted).value < d
    low = [
        Prediction(
            sample_id=p.sample_id, masks=[m.model_copy(update={"score": 0.2}) for m in p.masks]
        )
        for p in perfect
    ]
    assert _run("dice", samples, card, low).value == 0.0
    assert _run("dice", samples, card, low, {"threshold": "0.1"}).value == pytest.approx(1.0)


# --- registration details beyond the brief's own applicable_metrics() check.


def test_registered_declares_defaults_and_direction():
    for name in ("dice", "miou"):
        m = get_metric(name)
        assert m.defaults == {"threshold": "0.5"}
        assert m.higher_is_better is True
        assert m.version == "1"


# --- ruling 3: a mask on a view other than 0 is a located ValidationFailed, on both the gold
# and the prediction side (mirrors coco_map's box-side check, added for the same reason).


def _two_view_sample(masks: list[Mask]) -> Sample:
    return Sample(
        sample_id="s0000",
        views=[
            View(path="s0000_v0.jpg", width=8, height=8),
            View(path="s0000_v1.jpg", width=8, height=8),
        ],
        labels=Labels(masks=masks),
        label_source="gold",
    )


def test_gold_mask_on_nonzero_view_is_validation_failed():
    card = make_card("seg", categories=SEG_CATS)
    sample = _two_view_sample([Mask(category_id=0, view=1, polygon=[[1, 1, 4, 1, 4, 3, 1, 3]])])
    with pytest.raises(ValidationFailed, match="view"):
        _run("dice", [sample], card, [Prediction(sample_id="s0000", masks=[])])


def test_prediction_mask_on_nonzero_view_is_validation_failed():
    card = make_card("seg", categories=SEG_CATS)
    sample = _two_view_sample([Mask(category_id=0, polygon=[[1, 1, 4, 1, 4, 3, 1, 3]])])
    preds = [
        Prediction(
            sample_id="s0000",
            masks=[PredMask(category_id=0, score=1.0, view=1, polygon=[[1, 1, 4, 1, 4, 3, 1, 3]])],
        )
    ]
    with pytest.raises(ValidationFailed, match="view"):
        _run("dice", [sample], card, preds)


# --- a category id the card does not declare must not crash with a bare KeyError.


def test_gold_mask_with_unknown_category_id_is_validation_failed():
    card = make_card("seg", categories=SEG_CATS)  # road=0, water=1
    sample = Sample(
        sample_id="s0000",
        views=[View(path="s0000.jpg", width=8, height=8)],
        labels=Labels(masks=[Mask(category_id=99, polygon=[[1, 1, 4, 1, 4, 3, 1, 3]])]),
        label_source="gold",
    )
    with pytest.raises(ValidationFailed, match="unknown category id"):
        _run("dice", [sample], card, [Prediction(sample_id="s0000", masks=[])])


def test_prediction_mask_with_unknown_category_id_is_validation_failed():
    card = make_card("seg", categories=SEG_CATS)
    sample = Sample(
        sample_id="s0000",
        views=[View(path="s0000.jpg", width=8, height=8)],
        labels=Labels(masks=[Mask(category_id=0, polygon=[[1, 1, 4, 1, 4, 3, 1, 3]])]),
        label_source="gold",
    )
    preds = [
        Prediction(
            sample_id="s0000",
            masks=[PredMask(category_id=99, score=1.0, polygon=[[1, 1, 4, 1, 4, 3, 1, 3]])],
        )
    ]
    with pytest.raises(ValidationFailed, match="unknown category id"):
        _run("dice", [sample], card, preds)


# --- ruling 0: an empty subset must fail loudly (require_nonempty), for both metrics.


@pytest.mark.parametrize("metric_name", ["dice", "miou"])
def test_empty_subset_raises_validation_failed_not_a_crash(metric_name):
    card = make_card("seg", categories=SEG_CATS)
    with pytest.raises(ValidationFailed, match="no samples"):
        _run(metric_name, [], card, [])


# --- a sample with no prediction row at all is legitimate for seg (a list payload, like det):
# it must score identically to an explicit empty-masks prediction for that sample, not crash.


def test_missing_prediction_row_behaves_like_an_explicit_empty_one():
    samples = seg_samples(6, seed=2)
    card = make_card("seg", categories=SEG_CATS)
    perfect = perfect_predictions(samples, card)
    dropped = {"s0002", "s0003"}
    missing_rows = predictions_by_id([p for p in perfect if p.sample_id not in dropped])
    explicit_empty = predictions_by_id(
        [
            p if p.sample_id not in dropped else Prediction(sample_id=p.sample_id, masks=[])
            for p in perfect
        ]
    )
    from_missing = get_metric("dice").compute(samples, missing_rows, card, {})
    from_explicit = get_metric("dice").compute(samples, explicit_empty, card, {})
    assert from_missing.value == from_explicit.value
    assert from_missing.per_class == from_explicit.per_class
    assert 0.0 < from_missing.value < 1.0  # some but not all samples of each class matched


# --- the no-gold/no-prediction decision (see the module docstring in metrics/seg.py): a single
# undefined category is excluded from the macro mean, not folded in as 1.0 or 0.0; a subset
# where EVERY category is undefined this way has no defined mean at all and must raise.


def test_category_with_no_gold_and_no_prediction_pixels_is_excluded_from_macro_mean():
    card = make_card("seg", categories=SEG_CATS)  # road=0, water=1
    sample = Sample(
        sample_id="s0000",
        views=[View(path="s0000.jpg", width=8, height=8)],
        labels=Labels(masks=[Mask(category_id=0, polygon=[[1, 1, 4, 1, 4, 3, 1, 3]])]),
        label_source="gold",
    )
    perfect = perfect_predictions([sample], card)
    for name in ("dice", "miou"):
        res = _run(name, [sample], card, perfect)
        assert res.per_class["road"] == pytest.approx(1.0)
        assert res.per_class["water"] is None
        assert res.value == pytest.approx(1.0)  # macro mean over {road} alone


def test_all_categories_undefined_is_validation_failed():
    card = make_card("seg", categories=SEG_CATS)
    samples = [
        Sample(
            sample_id=f"s{i:04d}",
            views=[View(path=f"s{i:04d}.jpg", width=8, height=8)],
            labels=Labels(masks=[]),
            label_source="gold",
        )
        for i in range(3)
    ]
    empty_preds = [Prediction(sample_id=s.sample_id, masks=[]) for s in samples]
    for name in ("dice", "miou"):
        with pytest.raises(ValidationFailed, match="undefined"):
            _run(name, samples, card, empty_preds)


# --- threshold parsing mirrors coco_map's max_dets: a bad value is a located ValidationFailed,
# never a bare ValueError from float().


def test_invalid_threshold_is_validation_failed():
    samples = seg_samples(4, seed=0)
    card = make_card("seg", categories=SEG_CATS)
    perfect = perfect_predictions(samples, card)
    with pytest.raises(ValidationFailed, match="threshold"):
        _run("dice", samples, card, perfect, {"threshold": "abc"})


# --- determinism: accumulation is a running sum over samples, so the result must not depend on
# the order samples/predictions are passed in.


def test_dice_and_miou_are_order_independent():
    samples = seg_samples(12, seed=3)
    card = make_card("seg", categories=SEG_CATS)
    perfect = perfect_predictions(samples, card)
    shifted = [
        Prediction(
            sample_id=p.sample_id,
            masks=[
                PredMask(
                    category_id=m.category_id,
                    score=1.0,
                    polygon=[
                        [v + (1 if i % 2 == 0 else 0) for i, v in enumerate(ring)]
                        for ring in m.polygon
                    ],
                )
                for m in p.masks
            ],
        )
        for p in perfect
    ]
    for name in ("dice", "miou"):
        baseline = _run(name, samples, card, shifted)
        reordered = _run(name, list(reversed(samples)), card, list(reversed(shifted)))
        assert reordered.value == baseline.value
        assert reordered.per_class == baseline.per_class
