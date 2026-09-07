import random

import numpy as np
import pytest

from helpers import (
    SEG_CATS,
    cls_samples,
    make_card,
    noisy_predictions,
    perfect_predictions,
    seg_samples,
)
from vcp.core.errors import ValidationFailed, VcpError
from vcp.data.schema import Labels, Sample, View
from vcp.measure.metrics import METRICS, get_metric, register_metric
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import MetricResult, Prediction
from vcp.measure.stats import bootstrap_sd, paired_bootstrap, resample_indexes

# Pinned from a fresh process (see the task report). Any change to how the resample indexes are
# drawn -- another RNG, another draw order, an unsorted sample list -- moves these. The two
# coincide because the paired baseline below scores 1.0 on every resample, so the delta is the
# noisy run's own value shifted by a constant.
GOLDEN_SD = 0.06232117273791124
GOLDEN_SE = 0.06232117273791124


def _cls_case(n=80, *, seed=0, noise_seed=1, flip=0.3):
    samples = cls_samples(n, seed=seed)
    card = make_card("cls")
    perfect = predictions_by_id(perfect_predictions(samples, card))
    noisy = predictions_by_id(noisy_predictions(samples, card, seed=noise_seed, flip=flip))
    return samples, card, perfect, noisy


def test_paired_bootstrap_is_deterministic_and_signed():
    samples, card, perfect, noisy = _cls_case()
    metric = get_metric("accuracy")
    delta, se, deltas = paired_bootstrap(
        samples, perfect, noisy, metric, card, {}, resamples=50, seed=0
    )
    assert delta < 0 and se > 0 and len(deltas) == 50
    delta2, se2, deltas2 = paired_bootstrap(
        samples, perfect, noisy, metric, card, {}, resamples=50, seed=0
    )
    assert (delta2, se2, deltas2) == (delta, se, deltas)
    _, _, deltas3 = paired_bootstrap(
        samples, perfect, noisy, metric, card, {}, resamples=50, seed=1
    )
    assert deltas3 != deltas
    d_same, se_same, _ = paired_bootstrap(
        samples, perfect, perfect, metric, card, {}, resamples=20, seed=0
    )
    assert d_same == 0.0 and se_same == 0.0


def test_bootstrap_sd():
    samples = cls_samples(60, seed=2)
    card = make_card("cls")
    metric = get_metric("accuracy")
    perfect = predictions_by_id(perfect_predictions(samples, card))
    flat = bootstrap_sd(samples, perfect, metric, card, {}, resamples=30, seed=0)
    assert flat.value == 0.0
    assert (flat.resamples, flat.used, flat.skipped) == (30, 30, 0)
    noisy = predictions_by_id(noisy_predictions(samples, card, seed=3, flip=0.3))
    sd = bootstrap_sd(samples, noisy, metric, card, {}, resamples=30, seed=0).value
    assert 0.0 < sd < 0.2
    # Ruling 1: one draw has no spread to measure. That is a user-facing ValidationFailed (the
    # CLI's --resamples reaches straight here), not a bare ValueError, and it is raised before
    # a single metric runs.
    with pytest.raises(ValidationFailed, match="resamples must be >= 2"):
        bootstrap_sd(samples, noisy, metric, card, {}, resamples=1, seed=0)


def test_resample_indexes_rejects_a_negative_seed():
    """I1: ``numpy.random.default_rng`` raises a bare ``ValueError`` on a negative seed, which
    would reach a CLI ``--seed -1`` unguarded and ABORT instead of FAIL."""
    with pytest.raises(ValidationFailed, match="seed"):
        resample_indexes(10, 5, -1)


def test_bootstrap_is_bit_identical_across_calls_and_sample_order():
    """Same seed, same resamples, same inputs -> the same sigma_p to the last bit, whatever
    order the caller collected the samples in."""
    samples, card, perfect, noisy = _cls_case()
    metric = get_metric("accuracy")
    shuffled = random.Random(11).sample(samples, len(samples))
    assert [s.sample_id for s in shuffled] != [s.sample_id for s in samples]

    sd = bootstrap_sd(samples, noisy, metric, card, {}, resamples=64, seed=7).value
    assert sd == GOLDEN_SD
    assert bootstrap_sd(samples, noisy, metric, card, {}, resamples=64, seed=7).value == sd
    assert bootstrap_sd(shuffled, noisy, metric, card, {}, resamples=64, seed=7).value == sd

    _, se, deltas = paired_bootstrap(
        samples, perfect, noisy, metric, card, {}, resamples=64, seed=7
    )
    assert se == GOLDEN_SE
    _, se_shuffled, deltas_shuffled = paired_bootstrap(
        shuffled, perfect, noisy, metric, card, {}, resamples=64, seed=7
    )
    assert (se_shuffled, deltas_shuffled) == (se, deltas)


def _top_class(scores: dict[str, float], names: list[str]) -> str:
    return names[int(np.argmax([scores[n] for n in names]))]


def _one_hot(sample: Sample, names: list[str], gold_of: dict[int, str]) -> Prediction:
    gold = gold_of[sample.labels.cls]
    return Prediction(
        sample_id=sample.sample_id, scores={n: (1.0 if n == gold else 0.0) for n in names}
    )


def test_paired_se_is_far_below_the_unpaired_one():
    """Pairing is the whole point: both runs are scored on the SAME resample, so the sampling
    noise they share cancels and the SE measures only how much their *difference* moves.
    Resampling them independently would leave sqrt(sd_a^2 + sd_b^2) in the SE -- five times
    more on this fixture -- and would call a real improvement noise."""
    n = 120
    fixed = 2
    samples = cls_samples(n, seed=5)
    card = make_card("cls")
    metric = get_metric("accuracy")
    names = [c.name for c in card.categories]
    gold_of = {c.id: c.name for c in card.categories}
    a = predictions_by_id(noisy_predictions(samples, card, seed=6, flip=0.3))
    # b is a, with the first few samples a gets wrong corrected: two runs that agree
    # everywhere else, so a paired delta only moves when one of those samples is drawn.
    wrong = [
        s
        for s in samples
        if _top_class(a[s.sample_id].scores or {}, names) != gold_of[s.labels.cls]
    ][:fixed]
    b = {**a, **{s.sample_id: _one_hot(s, names, gold_of) for s in wrong}}
    delta, se, _ = paired_bootstrap(samples, a, b, metric, card, {}, resamples=200, seed=0)
    assert delta == pytest.approx(fixed / n)  # b is better on exactly those samples
    sd_a = bootstrap_sd(samples, a, metric, card, {}, resamples=200, seed=1).value
    sd_b = bootstrap_sd(samples, b, metric, card, {}, resamples=200, seed=2).value
    unpaired = float(np.hypot(sd_a, sd_b))  # what independent resampling would report
    assert se < unpaired / 3
    # 2 corrected samples out of 120: sd(delta) = sqrt(120 * (1/60) * (59/60)) / 120 = 0.0117
    assert se < 0.02


def _mostly_empty_seg(n: int) -> list[Sample]:
    """One sample with gold, the rest with none. A resample that happens to miss the only
    labelled sample leaves every category undefined -- which is what the seg metrics refuse
    to score."""
    return seg_samples(1, seed=0) + [
        Sample(
            sample_id=f"s{i:04d}",
            views=[View(path=f"s{i:04d}.jpg", width=8, height=8)],
            labels=Labels(masks=[]),
            label_source="gold",
        )
        for i in range(1, n)
    ]


def test_a_metric_refusing_a_resample_is_counted_not_fatal():
    """3-3, replacing ruling 0's "never skip": one draw hitting the metric's own guard is bad
    luck, not a broken estimate. It is skipped and counted, so the caller can file how many
    draws the number is actually made of -- and `too_many_skipped` below still refuses an
    estimate built from a minority of the draws."""
    samples = _mostly_empty_seg(8)
    card = make_card("seg", categories=SEG_CATS)
    preds = predictions_by_id(perfect_predictions(samples, card))
    metric = get_metric("dice")
    assert metric.compute(samples, preds, card, {}).value == 1.0  # the full subset scores fine
    res = bootstrap_sd(samples, preds, metric, card, {}, resamples=20, seed=0)
    assert res.resamples == 20 and res.used + res.skipped == 20
    assert 0 < res.skipped <= res.used  # (7/8)^8 of the draws miss the only labelled sample
    assert res.value == 0.0  # every scoreable draw scores a perfect 1.0
    # Same seed, same counts: skipping is as deterministic as the draws it skips.
    assert bootstrap_sd(samples, preds, metric, card, {}, resamples=20, seed=0) == res


class _PickyMetric:
    """Refuses every resample after the first ``allow`` of them. The whole-subset call
    ``bootstrap_sd`` makes before any draw is the first call and is always allowed."""

    name = "picky"
    version = "1"
    tasks = frozenset({"cls"})
    defaults: dict[str, str] = {}
    higher_is_better = True

    def __init__(self, allow: int) -> None:
        self.allow = allow
        self.calls = 0

    def compute(self, samples, predictions, card, params) -> MetricResult:
        self.calls += 1
        if self.calls > self.allow + 1:
            raise ValidationFailed("no gold in this draw")
        return MetricResult(value=float(len({s.sample_id for s in samples})), n=len(samples))


def test_bootstrap_refuses_an_estimate_made_of_a_minority_of_the_draws():
    """3-3: skipping is only honest while most draws survive. Past half, the spread being
    reported is the spread of whatever happened to be scoreable, so it is a FAIL that names
    the counts rather than a quietly-biased sigma_p."""
    samples = cls_samples(10, seed=0)
    card = make_card("cls")
    with pytest.raises(ValidationFailed) as excinfo:
        bootstrap_sd(samples, {}, _PickyMetric(allow=4), card, {}, resamples=10, seed=0)
    message = str(excinfo.value)
    assert message.startswith("too_many_skipped:")
    assert "6" in message and "4" in message and "10" in message
    # One more usable draw than skipped ones and the estimate stands.
    res = bootstrap_sd(samples, {}, _PickyMetric(allow=6), card, {}, resamples=10, seed=0)
    assert (res.resamples, res.used, res.skipped) == (10, 6, 4) and res.value > 0.0
    # A single usable draw has no spread: refused with the same reason, never a nan.
    with pytest.raises(ValidationFailed, match="too_many_skipped") as single:
        bootstrap_sd(samples, {}, _PickyMetric(allow=1), card, {}, resamples=2, seed=0)
    assert "leaving 1 usable" in str(single.value)


def _all_empty_seg(n: int) -> list[Sample]:
    """Every sample gold-empty: unlike ``_mostly_empty_seg`` the FULL subset is unscoreable,
    not just some resamples of it."""
    return [
        Sample(
            sample_id=f"s{i:04d}",
            views=[View(path=f"s{i:04d}.jpg", width=8, height=8)],
            labels=Labels(masks=[]),
            label_source="gold",
        )
        for i in range(n)
    ]


def test_bootstrap_sd_reports_a_subset_wide_failure_without_a_resample_label():
    """A problem with the WHOLE subset (no predictions anywhere, so every category is
    undefined) must surface as the metric's own message. 'refused bootstrap resample 0' would
    say the failure was bad luck on one draw, when really every draw fails the same way."""
    samples = _all_empty_seg(4)
    card = make_card("seg", categories=SEG_CATS)
    metric = get_metric("dice")
    with pytest.raises(ValidationFailed) as excinfo:
        bootstrap_sd(samples, {}, metric, card, {}, resamples=5, seed=0)
    message = str(excinfo.value)
    assert "no category has any gold or predicted pixels" in message
    assert "resample" not in message and "too_many_skipped" not in message


class _FlakyMetric:
    """A plugin bug -- a plain exception, not ``ValidationFailed`` -- on its third call.
    Registered only for the duration of one test (Task 12 ruling 2's leak concern)."""

    name = "flaky"
    version = "1"
    tasks = frozenset({"cls"})
    defaults: dict[str, str] = {}
    higher_is_better = True

    def __init__(self) -> None:
        self.calls = 0

    def compute(self, samples, predictions, card, params) -> MetricResult:
        self.calls += 1
        if self.calls == 3:
            raise ZeroDivisionError("boom")
        return MetricResult(value=1.0, per_class=None, n=len(samples))


def test_a_plugin_exception_inside_a_resample_is_wrapped_with_its_location():
    """Ruling 0 covers a metric's own ValidationFailed; a plain bug (ZeroDivisionError, ...) in
    a plugin metric must not escape bare either. It is a programming error (ABORT, not FAIL),
    but still needs the resample index and seed to be diagnosable from the VERDICT line alone."""
    samples = cls_samples(20, seed=0)
    card = make_card("cls")
    metric = _FlakyMetric()
    register_metric(metric)
    try:
        with pytest.raises(VcpError) as excinfo:
            # base = 2 calls (preds_b, preds_a), then resample 0's preds_b is the 3rd call.
            paired_bootstrap(samples, {}, {}, get_metric("flaky"), card, {}, resamples=5, seed=0)
    finally:
        METRICS.pop("flaky", None)
    assert not isinstance(excinfo.value, ValidationFailed)
    message = str(excinfo.value)
    assert "resample 0" in message and "seed=0" in message
    assert "ZeroDivisionError" in message and "boom" in message
