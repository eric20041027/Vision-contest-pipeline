import math

import numpy as np
import pytest
from sklearn.metrics import (
    accuracy_score,
    f1_score,
    log_loss,
    mean_absolute_error,
    mean_squared_error,
    roc_auc_score,
)

from helpers import (
    ML_CATS,
    REG_CATS,
    cls_samples,
    make_card,
    multilabel_samples,
    noisy_predictions,
    perfect_predictions,
    regression_samples,
)
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.measure.metrics import METRICS, applicable_metrics, get_metric, register_metric
from vcp.measure.metrics.base import effective_params, params_key
from vcp.measure.metrics.tabular import EPS
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import Prediction


def _run(metric, samples, card, preds, params=None):
    return get_metric(metric).compute(samples, predictions_by_id(preds), card, params or {})


def test_registry_and_params():
    assert list(METRICS)[:6] == ["accuracy", "macro_f1", "log_loss", "macro_auc", "rmse", "mae"]
    assert applicable_metrics("cls") == ["accuracy", "macro_f1", "log_loss"]
    assert applicable_metrics("multilabel") == ["macro_auc"]
    assert applicable_metrics("regression") == ["rmse", "mae"]
    assert applicable_metrics("det") == []
    with pytest.raises(RegistryError):
        get_metric("bleu")
    with pytest.raises(RegistryError, match="already registered"):
        register_metric(METRICS["accuracy"])  # a plugin must not shadow a built-in
    assert params_key({"b": "2", "a": "1"}) == "a=1,b=2" and params_key({}) == ""
    assert effective_params(get_metric("accuracy"), {}) == {}
    with pytest.raises(ValidationFailed, match="params"):
        effective_params(get_metric("accuracy"), {"iou": "50"})


def test_cls_metrics_perfect_and_noisy():
    samples = cls_samples(40, seed=1)
    card = make_card("cls")
    perfect = perfect_predictions(samples, card)
    assert _run("accuracy", samples, card, perfect).value == 1.0
    f1 = _run("macro_f1", samples, card, perfect)
    assert f1.value == 1.0 and set(f1.per_class) == {"cat", "dog", "bird"}
    assert _run("log_loss", samples, card, perfect).value < 1e-6
    noisy = noisy_predictions(samples, card, seed=3, flip=0.4)
    assert _run("accuracy", samples, card, noisy).value < 1.0
    assert _run("log_loss", samples, card, noisy).value > 0.05
    res = _run("accuracy", samples, card, perfect)
    assert res.n == 40


def test_multilabel_auc_perfect_and_undefined_class():
    samples = multilabel_samples(60, seed=2, probs=(0.5, 0.3, 0.0))  # third class never positive
    card = make_card("multilabel", categories=ML_CATS)
    res = _run("macro_auc", samples, card, perfect_predictions(samples, card))
    assert res.value == 1.0
    assert res.per_class["acl"] == 1.0 and res.per_class["effusion"] is None
    noisy = noisy_predictions(samples, card, seed=5, flip=0.4)
    assert 0.0 <= _run("macro_auc", samples, card, noisy).value < 1.0
    constant = [
        Prediction(sample_id=s.sample_id, scores={"acl": 0.5, "mcl": 0.5, "effusion": 0.5})
        for s in samples
    ]
    assert abs(_run("macro_auc", samples, card, constant).value - 0.5) < 1e-9


def test_regression_metrics_and_gold_only():
    samples = regression_samples(30, seed=0)
    card = make_card("regression", categories=REG_CATS)
    perfect = perfect_predictions(samples, card)
    assert _run("rmse", samples, card, perfect).value == 0.0
    assert _run("mae", samples, card, perfect).value == 0.0
    shifted = [
        Prediction(sample_id=p.sample_id, targets={"age": p.targets["age"] + 2.0}) for p in perfect
    ]
    assert math.isclose(_run("rmse", samples, card, shifted).value, 2.0)
    assert math.isclose(_run("mae", samples, card, shifted).value, 2.0)
    missing = [Prediction(sample_id=p.sample_id, targets={}) for p in perfect]
    with pytest.raises(ValidationFailed, match="missing target"):
        _run("rmse", samples, card, missing)
    unlabeled = cls_samples(5, seed=0, gold_frac=0.0)
    with pytest.raises(ValidationFailed, match="gold labels"):
        _run(
            "accuracy",
            unlabeled,
            make_card("cls"),
            perfect_predictions(unlabeled, make_card("cls")),
        )
    with pytest.raises(ValidationFailed, match="missing prediction"):
        _run("accuracy", cls_samples(3), make_card("cls"), [])


# --- sklearn cross-checks (spec §7: hand-computed case AND a cross-check against sklearn) ---


def test_cls_metrics_match_sklearn_directly():
    """accuracy / macro_f1 / log_loss reproduce a from-scratch sklearn computation on the same
    (y_true, scores) arrays, independently rebuilt here from the samples/predictions rather than
    by calling the module's own array-building helper."""
    samples = cls_samples(40, seed=1)
    card = make_card("cls")
    noisy = noisy_predictions(samples, card, seed=3, flip=0.4)
    by_id = predictions_by_id(noisy)
    names = [c.name for c in card.categories]
    index_of = {c.id: i for i, c in enumerate(card.categories)}
    y_true = np.array([index_of[s.labels.cls] for s in samples])
    scores = np.array([[by_id[s.sample_id].scores[n] for n in names] for s in samples])
    y_pred = scores.argmax(axis=1)

    assert _run("accuracy", samples, card, noisy).value == pytest.approx(
        accuracy_score(y_true, y_pred)
    )
    expected_f1 = f1_score(y_true, y_pred, labels=[0, 1, 2], average="macro", zero_division=0)
    assert _run("macro_f1", samples, card, noisy).value == pytest.approx(expected_f1)

    probs = np.clip(scores, EPS, 1.0)
    probs = probs / probs.sum(axis=1, keepdims=True)
    expected_log_loss = log_loss(y_true, probs, labels=[0, 1, 2])
    assert _run("log_loss", samples, card, noisy).value == pytest.approx(expected_log_loss)


def test_macro_auc_matches_sklearn_directly():
    samples = multilabel_samples(60, seed=2, probs=(0.5, 0.3, 0.0))
    card = make_card("multilabel", categories=ML_CATS)
    noisy = noisy_predictions(samples, card, seed=5, flip=0.4)
    by_id = predictions_by_id(noisy)
    names = [c.name for c in card.categories]
    y_true = np.array([[s.labels.targets[n] for n in names] for s in samples])
    y_score = np.array([[by_id[s.sample_id].scores[n] for n in names] for s in samples])
    expected = [
        roc_auc_score(y_true[:, j], y_score[:, j])
        for j in range(len(names))
        if 0 < y_true[:, j].sum() < len(y_true)
    ]
    assert _run("macro_auc", samples, card, noisy).value == pytest.approx(float(np.mean(expected)))


def test_regression_metrics_match_sklearn_directly():
    samples = regression_samples(30, seed=0)
    card = make_card("regression", categories=REG_CATS)
    perfect = perfect_predictions(samples, card)
    shifted = [
        Prediction(sample_id=p.sample_id, targets={"age": p.targets["age"] + 2.0}) for p in perfect
    ]
    y_true = np.array([s.labels.targets["age"] for s in samples])
    y_pred = np.array([p.targets["age"] for p in shifted])
    assert _run("rmse", samples, card, shifted).value == pytest.approx(
        math.sqrt(mean_squared_error(y_true, y_pred))
    )
    assert _run("mae", samples, card, shifted).value == pytest.approx(
        mean_absolute_error(y_true, y_pred)
    )


# --- one degenerate-input case per metric, proving the failure is a ValidationFailed (blanket G)
# not a bare exception from sklearn/numpy. accuracy and rmse are already covered above (gold_only
# and missing-prediction via accuracy, missing-target via rmse); the remaining four are below.


def test_macro_f1_missing_prediction_is_validation_failed():
    with pytest.raises(ValidationFailed, match="missing prediction"):
        _run("macro_f1", cls_samples(3), make_card("cls"), [])


def test_log_loss_requires_gold_labels():
    unlabeled = cls_samples(5, seed=0, gold_frac=0.0)
    card = make_card("cls")
    with pytest.raises(ValidationFailed, match="gold labels"):
        _run("log_loss", unlabeled, card, perfect_predictions(unlabeled, card))


def test_macro_auc_undefined_when_every_class_is_constant():
    samples = multilabel_samples(10, seed=7, probs=(0.0, 0.0, 0.0))  # every class always negative
    card = make_card("multilabel", categories=ML_CATS)
    preds = perfect_predictions(samples, card)
    with pytest.raises(ValidationFailed, match="undefined"):
        _run("macro_auc", samples, card, preds)


def test_mae_requires_target_present_in_prediction():
    samples = regression_samples(5, seed=0)
    card = make_card("regression", categories=REG_CATS)
    perfect = perfect_predictions(samples, card)
    missing = [Prediction(sample_id=p.sample_id, targets={}) for p in perfect]
    with pytest.raises(ValidationFailed, match="missing target"):
        _run("mae", samples, card, missing)
