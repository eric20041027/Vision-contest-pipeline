"""Bootstrap over samples: the paired delta between two prediction sets, and the sampling
noise of a single reading.

Everything here is deterministic. The samples are ordered by ``sample_id`` before anything is
drawn, the resample indexes come from ``numpy.random.default_rng(seed)`` alone (never the
global RNG), and the metric is recomputed on the resampled list with its duplicates. Same
seed, same resamples, same inputs -> the same numbers, bit for bit, in any process.
"""

from __future__ import annotations

import numpy as np

from vcp.core.errors import ValidationFailed, VcpError
from vcp.data.schema import DatasetCard, Sample
from vcp.measure.metrics.base import Metric, require_nonempty
from vcp.measure.schema import Prediction

MIN_RESAMPLES = 2


def sample_sd(values: list[float]) -> float:
    """Sample standard deviation (ddof=1): the spread of a bootstrap distribution -- the
    standard error of the statistic -- and, in ``sigma.py``, the spread of the per-run
    split-half differences."""
    return float(np.std(values, ddof=1))


def _ordered(samples: list[Sample]) -> list[Sample]:
    """Sorted by ``sample_id``, so a resample index means the same sample whatever order the
    caller happened to collect the subset in."""
    return sorted(require_nonempty(samples), key=lambda s: s.sample_id)


def resample_indexes(n: int, resamples: int, seed: int) -> list[np.ndarray]:
    """``resamples`` draws of ``n`` positions with replacement, from this seed alone."""
    if resamples < MIN_RESAMPLES:
        raise ValidationFailed(
            f"resamples must be >= {MIN_RESAMPLES} to have any spread to measure, got {resamples}"
        )
    if seed < 0:
        # numpy's default_rng raises a bare ValueError on a negative seed (I1); a CLI --seed -1
        # would otherwise reach it unguarded and ABORT instead of FAIL.
        raise ValidationFailed(f"seed must be a non-negative integer, got {seed}")
    rng = np.random.default_rng(seed)
    return [rng.integers(0, n, n) for _ in range(resamples)]


def _value(
    metric: Metric,
    samples: list[Sample],
    predictions: dict[str, Prediction],
    card: DatasetCard,
    params: dict[str, str],
    *,
    index: int,
    seed: int,
) -> float:
    """One resample's metric value.

    A metric that refuses this particular draw -- every category undefined in it, no gold left
    in it -- fails loudly, naming the draw that produced it, and is never skipped: dropping the
    resamples a metric cannot score would quietly bias sigma_p towards the draws that happen to
    be scoreable (ruling 0).
    """
    try:
        return metric.compute(samples, predictions, card, params).value
    except ValidationFailed as e:
        raise ValidationFailed(
            f"metric {metric.name!r} refused bootstrap resample {index} (seed={seed}): {e}",
            location=f"resample {index}",
        ) from e
    except Exception as e:
        # Anything else is a bug in the metric, not bad input (ABORT, not FAIL) -- but it must
        # still name the draw that triggered it, or a plugin crash is undebuggable from the
        # VERDICT line alone.
        raise VcpError(
            f"metric {metric.name!r} raised {type(e).__name__} on bootstrap resample {index} "
            f"(seed={seed}): {e}",
            location=f"resample {index}",
        ) from e


def paired_bootstrap(
    samples: list[Sample],
    preds_a: dict[str, Prediction],
    preds_b: dict[str, Prediction],
    metric: Metric,
    card: DatasetCard,
    params: dict[str, str],
    *,
    resamples: int = 200,
    seed: int = 0,
) -> tuple[float, float, list[float]]:
    """``(delta, se, deltas)``: ``delta`` = metric(b) - metric(a) over the whole subset, ``se``
    = the standard deviation (ddof=1) of the resampled deltas, ``deltas`` = one per resample.

    Both runs are scored on the SAME resample, so the sampling noise they share cancels and
    ``se`` measures only how much their *difference* moves. Resampling them independently would
    inflate it to sqrt(se_a^2 + se_b^2) and would call a real improvement noise.

    The delta is raw and signed (b - a) in the metric's own units; whether up means better is
    the metric's ``higher_is_better``, and applying it is the judge's job, not this one's.
    """
    ordered = _ordered(samples)
    draws = resample_indexes(len(ordered), resamples, seed)
    base = (
        metric.compute(ordered, preds_b, card, params).value
        - metric.compute(ordered, preds_a, card, params).value
    )
    deltas: list[float] = []
    for i, idx in enumerate(draws):
        sub = [ordered[j] for j in idx]  # the SAME resample scores both runs
        deltas.append(
            _value(metric, sub, preds_b, card, params, index=i, seed=seed)
            - _value(metric, sub, preds_a, card, params, index=i, seed=seed)
        )
    return float(base), sample_sd(deltas), deltas


def bootstrap_sd(
    samples: list[Sample],
    preds: dict[str, Prediction],
    metric: Metric,
    card: DatasetCard,
    params: dict[str, str],
    *,
    resamples: int = 200,
    seed: int = 0,
) -> float:
    """How far one reading moves when the subset is resampled: the sigma_p ``bootstrap``
    method's estimate, and the sampling-noise floor under any claim made from that subset.

    A magnitude, so it is direction-free: a lower-is-better metric needs no special case.
    """
    ordered = _ordered(samples)
    draws = resample_indexes(len(ordered), resamples, seed)
    # A problem with the WHOLE subset (no predictions anywhere, every category undefined) must
    # surface as the metric's own message, not as "refused bootstrap resample 0" -- that framing
    # says the failure is bad luck on one draw, when really every draw would fail the same way.
    metric.compute(ordered, preds, card, params)
    values = [
        _value(metric, [ordered[j] for j in idx], preds, card, params, index=i, seed=seed)
        for i, idx in enumerate(draws)
    ]
    return sample_sd(values)
