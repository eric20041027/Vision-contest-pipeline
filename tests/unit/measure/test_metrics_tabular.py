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
from vcp.data.schema import Category, Labels, Sample, View
from vcp.measure.metrics import METRICS, applicable_metrics, get_metric, register_metric
from vcp.measure.metrics.base import effective_params, params_key
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import Prediction


def _run(metric, samples, card, preds, params=None):
    return get_metric(metric).compute(samples, predictions_by_id(preds), card, params or {})


def test_registry_and_params():
    assert list(METRICS)[:7] == [
        "coco_map",
        "accuracy",
        "macro_f1",
        "log_loss",
        "macro_auc",
        "rmse",
        "mae",
    ]
    assert applicable_metrics("cls") == ["accuracy", "macro_f1", "log_loss"]
    assert applicable_metrics("multilabel") == ["macro_auc"]
    assert applicable_metrics("regression") == ["rmse", "mae"]
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


def test_macro_f1_excludes_a_class_with_no_gold_in_the_subset():
    """A class with no gold instance in THIS subset is undefined here, not zero.

    macro_auc, dice, miou and coco_map all report ``None`` for such a class and leave it out of
    the mean. macro_f1 folded it in as 0.0, so a perfect prediction on a 3-class card scored
    0.667 on any subset that happens not to contain one of the classes -- and every bootstrap
    resample is such a subset, which is exactly where sigma_p and the judge's t come from.
    """
    card = make_card("cls")  # cat / dog / bird
    samples = [s for s in cls_samples(30, seed=1) if s.labels.cls != 2]  # no 'bird' in gold
    assert {s.labels.cls for s in samples} == {0, 1}
    perfect = perfect_predictions(samples, card)
    res = _run("macro_f1", samples, card, perfect)
    assert res.value == 1.0  # "perfect -> 1.0" must hold on ANY subset
    assert res.per_class == {"cat": 1.0, "dog": 1.0, "bird": None}
    # accuracy has no per-class breakdown, so an absent class cannot pull it either way
    assert _run("accuracy", samples, card, perfect).value == 1.0
    # A class that IS in gold but never predicted stays defined and zero: it was there to be
    # found and was missed, which is what an F1 of 0.0 means.
    only_cat = [
        Prediction(sample_id=s.sample_id, scores={"cat": 1.0, "dog": 0.0, "bird": 0.0})
        for s in samples
    ]
    per = _run("macro_f1", samples, card, only_cat).per_class
    assert per["dog"] == 0.0 and per["bird"] is None
    # The guard the mean relies on: nothing reaches np.mean([]). A non-empty subset with gold
    # labels has at least one class in it, and both ways of not having one are refused before
    # any array is built.
    with pytest.raises(ValidationFailed, match="no samples"):
        _run("macro_f1", [], card, [])
    unlabeled = cls_samples(5, seed=0, gold_frac=0.0)
    with pytest.raises(ValidationFailed, match="gold labels"):
        _run("macro_f1", unlabeled, card, perfect_predictions(unlabeled, card))


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

    # Literal, not vcp.measure.metrics.tabular.EPS: this cross-check must move independently of
    # the module's own constant, or a change to EPS would silently drag both sides together.
    probs = np.clip(scores, 1e-15, 1.0)
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


# --- fix round 1 -----------------------------------------------------------------------------
# F1 (Critical): an empty subset (a real reachable state -- SubsetSpec.ratio allows 0.0, so a
# split can round an eval subset down to zero samples) must fail loudly, never a silent nan and
# never a raw numpy/sklearn crash.

_EMPTY_SUBSET_CARDS = {
    "accuracy": make_card("cls"),
    "macro_f1": make_card("cls"),
    "log_loss": make_card("cls"),
    "macro_auc": make_card("multilabel", categories=ML_CATS),
    "rmse": make_card("regression", categories=REG_CATS),
    "mae": make_card("regression", categories=REG_CATS),
}


@pytest.mark.filterwarnings("error::RuntimeWarning", "error::UserWarning")
@pytest.mark.parametrize("metric_name", list(_EMPTY_SUBSET_CARDS))
def test_empty_subset_raises_validation_failed_not_nan_or_crash(metric_name):
    """Previously: rmse/mae returned a silent nan (two leaked RuntimeWarnings, and a nan that
    pydantic serialises as JSON null, breaking readings.jsonl on the next load); accuracy /
    macro_f1 / log_loss raised numpy.exceptions.AxisError; macro_auc raised IndexError. All six
    must now raise a located ValidationFailed instead, before any array is built."""
    card = _EMPTY_SUBSET_CARDS[metric_name]
    with pytest.raises(ValidationFailed, match="no samples"):
        _run(metric_name, [], card, [])


# F2 (Important): _validate_regression (vcp.data.tasks) rejects an UNKNOWN target name but does
# NOT require every declared target to be present in gold -- so a gold sample may legitimately
# carry only some of a multi-target regression card's targets. rmse/mae guarded the prediction
# side (`p.targets is None or n not in p.targets`) but indexed the gold side unguarded.


def test_rmse_and_mae_require_target_in_gold_not_just_prediction():
    two_targets = [Category(id=0, name="age"), Category(id=1, name="weight")]
    card = make_card("regression", categories=two_targets)
    sample = Sample(
        sample_id="s0000",
        views=[View(path="s0000.jpg", width=8, height=8)],
        labels=Labels(targets={"age": 30.0}),  # "weight" is declared but absent from gold
        label_source="gold",
    )
    pred = Prediction(sample_id="s0000", targets={"age": 30.0, "weight": 70.0})
    for metric_name in ("rmse", "mae"):
        with pytest.raises(ValidationFailed, match="weight"):
            _run(metric_name, [sample], card, [pred])


# F3 (Important, plan-mandated gap): Task 11's judge assumes higher-is-better; every metric must
# declare its direction, and a plugin metric that forgets to must fail loudly at registration.


def test_all_metrics_declare_higher_is_better():
    # coco_map (Task 7) and dice/miou (Task 8) register outside the tabular metrics, but this
    # asserts on the whole METRICS registry so each must be listed here too, or registering one
    # breaks this untouched Task 6 test on an unrelated dict-equality mismatch.
    assert {name: m.higher_is_better for name, m in METRICS.items()} == {
        "coco_map": True,
        "accuracy": True,
        "macro_f1": True,
        "log_loss": False,
        "macro_auc": True,
        "rmse": False,
        "mae": False,
        "dice": True,
        "miou": True,
    }


def test_register_metric_requires_higher_is_better():
    class _NoDirectionMetric:
        name = "no_direction_metric"
        version = "1"
        tasks = frozenset({"cls"})
        defaults: dict[str, str] = {}

        def compute(self, samples, predictions, card, params):
            raise NotImplementedError

    try:
        with pytest.raises(RegistryError, match="higher_is_better"):
            register_metric(_NoDirectionMetric())
    finally:
        METRICS.pop("no_direction_metric", None)


@pytest.mark.parametrize("bad", ["my metric", "weird=name", "_leading", "", "sigma[x]"])
def test_register_metric_rejects_a_name_that_cannot_go_in_a_verdict(bad):
    """3-5: a metric name reaches the VERDICT line as a field name (`sigma[<metric>/<method>]=`)
    and `Verdict.line()` does not escape field names, so a space or an `=` in one produces a
    line no reader can parse. The registry is where that is caught, once."""

    class _OddlyNamed:
        version = "1"
        tasks = frozenset({"cls"})
        defaults: dict[str, str] = {}
        higher_is_better = True

        def compute(self, samples, predictions, card, params):
            raise NotImplementedError

    metric = _OddlyNamed()
    metric.name = bad
    try:
        with pytest.raises(RegistryError, match="name"):
            register_metric(metric)
    finally:
        METRICS.pop(bad, None)
    assert bad not in METRICS


# --- fix round 1 minors ------------------------------------------------------------------------


def test_log_loss_clip_avoids_infinite_loss_for_confidently_wrong_prediction():
    """The EPS clip in LogLoss.compute is a numerical guard, not input validation: without it, a
    score of exactly 0.0 for the true class makes log_loss blow up to inf. Pin the guard actually
    mattering, not just being exercised at values nowhere near the boundary."""
    samples = cls_samples(20, seed=0)
    card = make_card("cls")
    by_id = {c.id: c.name for c in card.categories}
    names = [c.name for c in card.categories]
    wrong = []
    for s in samples:
        true_name = by_id[s.labels.cls]  # type: ignore[union-attr]
        other = next(n for n in names if n != true_name)
        scores = {n: 0.0 for n in names}
        scores[other] = 1.0
        wrong.append(Prediction(sample_id=s.sample_id, scores=scores))
    value = _run("log_loss", samples, card, wrong).value
    assert math.isfinite(value)
    assert value > 10.0


def test_effective_params_merges_metric_defaults_with_overrides():
    """No Task 6 metric declares non-empty defaults, so the merge branch of effective_params was
    untested; a throwaway stub (never registered) exercises it directly."""

    class _StubMetricWithDefaults:
        name = "stub_metric_with_defaults"
        version = "1"
        tasks = frozenset({"cls"})
        defaults = {"k": "1"}
        higher_is_better = True

        def compute(self, samples, predictions, card, params):
            raise NotImplementedError

    stub = _StubMetricWithDefaults()
    assert effective_params(stub, {}) == {"k": "1"}
    assert effective_params(stub, {"k": "2"}) == {"k": "2"}
    with pytest.raises(ValidationFailed, match="params"):
        effective_params(stub, {"unknown": "x"})
