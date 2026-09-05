import builtins

import pytest

from helpers import det_samples, make_card, perfect_predictions
from vcp.core.errors import ValidationFailed, VcpError
from vcp.data.schema import Box, Labels, Sample, View
from vcp.measure.metrics import applicable_metrics, get_metric
from vcp.measure.metrics import coco_map as coco_module
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import PredBox, Prediction


def _run(samples, card, preds, params=None):
    return get_metric("coco_map").compute(samples, predictions_by_id(preds), card, params or {})


def test_registered_for_det():
    assert applicable_metrics("det") == ["coco_map"]
    m = get_metric("coco_map")
    assert m.defaults == {"iou": "50:95", "max_dets": "100"}
    assert m.higher_is_better is True


def test_perfect_shifted_and_empty():
    samples = det_samples(30, seed=4)
    card = make_card("det")
    perfect = perfect_predictions(samples, card)
    res = _run(samples, card, perfect)
    assert res.value == pytest.approx(1.0) and res.n == 30
    assert set(res.per_class) == {"cat", "dog", "bird"}
    assert all(v in (None, pytest.approx(1.0)) for v in res.per_class.values())
    assert _run(samples, card, perfect, {"iou": "50"}).value == pytest.approx(1.0)
    shifted = [
        Prediction(
            sample_id=p.sample_id, boxes=[b.model_copy(update={"x": b.x + 3.0}) for b in p.boxes]
        )
        for p in perfect
    ]
    # Ruling 3: assert the property that is actually true, not a magic magnitude.
    assert _run(samples, card, shifted).value < res.value
    empty = [Prediction(sample_id=p.sample_id, boxes=[]) for p in perfect]
    assert _run(samples, card, empty).value == 0.0
    with pytest.raises(ValidationFailed, match="params"):
        _run(samples, card, perfect, {"nms": "0.5"})


def test_requires_sizes_gold_and_gt_boxes():
    card = make_card("det")
    unsized = [Sample(sample_id="a", views=[View(path="a.jpg")], labels=None, label_source="none")]
    with pytest.raises(ValidationFailed, match="gold"):
        _run(unsized, card, [Prediction(sample_id="a", boxes=[])])
    nosize = [
        Sample(
            sample_id="a",
            views=[View(path="a.jpg")],
            labels={"boxes": [{"x": 0, "y": 0, "w": 1, "h": 1, "category_id": 0}]},
            label_source="gold",
        )
    ]
    with pytest.raises(ValidationFailed, match="size"):
        _run(nosize, card, [Prediction(sample_id="a", boxes=[])])
    negatives = [
        Sample(
            sample_id="a",
            views=[View(path="a.jpg", width=8, height=8)],
            labels={"boxes": []},
            label_source="gold",
        )
    ]
    with pytest.raises(ValidationFailed, match="ground-truth"):
        _run(
            negatives,
            card,
            [
                Prediction(
                    sample_id="a", boxes=[PredBox(x=0, y=0, w=1, h=1, category_id=0, score=1)]
                )
            ],
        )


def test_missing_pycocotools_is_abort(monkeypatch):
    real_import = builtins.__import__

    def fake(name, *args, **kwargs):
        if name.startswith("pycocotools"):
            raise ImportError("no pycocotools")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(VcpError, match="uv sync --extra eval"):
        coco_module.require_pycocotools()


# --- ruling 2: max_dets is validated, and looked up by its own value -- never assumed to be
# the last slot in ev.params.maxDets, which silently ignores any request below 10.


def test_max_dets_must_parse_as_an_integer():
    samples = det_samples(10, seed=4)
    card = make_card("det")
    perfect = perfect_predictions(samples, card)
    with pytest.raises(ValidationFailed, match="max_dets"):
        _run(samples, card, perfect, {"max_dets": "abc"})


def test_max_dets_must_be_positive():
    samples = det_samples(10, seed=4)
    card = make_card("det")
    perfect = perfect_predictions(samples, card)
    with pytest.raises(ValidationFailed, match="max_dets"):
        _run(samples, card, perfect, {"max_dets": "0"})


def test_max_dets_below_ten_limits_recall_not_silently_ignored():
    """A max_dets request between the fixed 1/10 anchors (e.g. 5) must be looked up by its own
    value in ev.params.maxDets, never assumed to sit at [-1]. With 8 true-positive boxes in one
    image, max_dets=5 can only recall 5 of them while max_dets=100 recalls all 8, so the two
    results must differ. Under the old `[-1]` indexing, a max_dets=5 request silently read the
    maxDet=10 slot instead (identical to the max_dets=100 result here), and this test would
    fail."""
    boxes = [Box(x=float(i), y=0.0, w=0.9, h=0.9, category_id=0) for i in range(8)]
    samples = [
        Sample(
            sample_id="s0000",
            views=[View(path="s0000.jpg", width=8, height=8)],
            labels=Labels(boxes=boxes),
            label_source="gold",
        )
    ]
    card = make_card("det")
    preds = perfect_predictions(samples, card)
    low = _run(samples, card, preds, {"max_dets": "5"}).value
    high = _run(samples, card, preds, {"max_dets": "100"}).value
    assert low < high


def test_unsupported_iou_value_is_validation_failed():
    samples = det_samples(10, seed=4)
    card = make_card("det")
    perfect = perfect_predictions(samples, card)
    with pytest.raises(ValidationFailed, match="iou"):
        _run(samples, card, perfect, {"iou": "90"})


# --- failure modes beyond the brief's own tests: an empty subset, an undefined per-class
# category, an all-empty prediction set, and a stray category id must never surface -1/nan.


def test_empty_subset_raises_validation_failed_not_a_crash():
    card = make_card("det")
    with pytest.raises(ValidationFailed, match="no samples"):
        _run([], card, [])


def test_category_with_no_gold_instances_reports_none_not_negative_one():
    """pycocotools reports -1 (undefined) for a class with zero ground-truth instances; that must
    never reach the caller as a fabricated number, only as per_class[name] = None."""
    card = make_card("det")  # CATS = cat(0), dog(1), bird(2); bird never appears below
    samples = [
        Sample(
            sample_id=f"s{i:04d}",
            views=[View(path=f"s{i:04d}.jpg", width=8, height=8)],
            labels=Labels(boxes=[Box(x=0.0, y=0.0, w=2.0, h=2.0, category_id=i % 2)]),
            label_source="gold",
        )
        for i in range(6)
    ]
    preds = perfect_predictions(samples, card)
    res = _run(samples, card, preds)
    assert res.value == pytest.approx(1.0)
    assert res.per_class["cat"] == pytest.approx(1.0)
    assert res.per_class["dog"] == pytest.approx(1.0)
    assert res.per_class["bird"] is None


def test_all_predictions_empty_reports_zero_for_every_category_including_one_without_gold():
    """Documents a deliberate simplification: with zero detections the metric short-circuits
    before calling pycocotools at all (loadRes crashes on an empty results list), so every
    category reports 0.0 -- even "bird", which has no ground-truth instances in this subset and
    would read as None (undefined) via the full pycocotools path exercised just above. Never
    -1, never nan, either way."""
    card = make_card("det")
    samples = [
        Sample(
            sample_id=f"s{i:04d}",
            views=[View(path=f"s{i:04d}.jpg", width=8, height=8)],
            labels=Labels(boxes=[Box(x=0.0, y=0.0, w=2.0, h=2.0, category_id=i % 2)]),
            label_source="gold",
        )
        for i in range(6)
    ]
    empty = [Prediction(sample_id=s.sample_id, boxes=[]) for s in samples]
    res = _run(samples, card, empty)
    assert res.value == 0.0
    assert res.per_class == {"cat": 0.0, "dog": 0.0, "bird": 0.0}


def test_unknown_category_id_in_prediction_does_not_crash():
    """check_predictions should make this unreachable via normal ingest, but the metric itself
    must not crash if a stray category id (not declared on the card) shows up in a prediction --
    pycocotools' own getAnnIds filtering drops it silently, so it simply does not affect any
    real category's score."""
    samples = det_samples(10, seed=4)
    card = make_card("det")
    perfect = perfect_predictions(samples, card)
    tampered = [
        perfect[0].model_copy(
            update={
                "boxes": [
                    *perfect[0].boxes,
                    PredBox(x=0, y=0, w=1, h=1, category_id=999, score=1.0),
                ]
            }
        ),
        *perfect[1:],
    ]
    assert _run(samples, card, tampered).value == pytest.approx(1.0)


def test_perfect_is_deterministic_across_two_runs():
    samples = det_samples(30, seed=4)
    card = make_card("det")
    perfect = perfect_predictions(samples, card)
    first = _run(samples, card, perfect)
    second = _run(samples, card, perfect)
    assert first.value == second.value
    assert first.per_class == second.per_class


def test_pycocotools_output_is_silenced(capsys):
    samples = det_samples(20, seed=4)
    card = make_card("det")
    perfect = perfect_predictions(samples, card)
    _run(samples, card, perfect)
    assert capsys.readouterr().out == ""
