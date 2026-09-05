"""Metric contract and registry.

A metric must work on any subset of samples (bootstrap needs it).
"""

from __future__ import annotations

from typing import Protocol

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.schema import DatasetCard, Sample
from vcp.measure.schema import MetricResult, Prediction


class Metric(Protocol):
    name: str
    version: str
    tasks: frozenset[str]
    defaults: dict[str, str]

    def compute(
        self,
        samples: list[Sample],
        predictions: dict[str, Prediction],
        card: DatasetCard,
        params: dict[str, str],
    ) -> MetricResult: ...


METRICS: dict[str, Metric] = {}


def register_metric(metric: Metric) -> None:
    if metric.name in METRICS:
        raise RegistryError(f"metric {metric.name!r} already registered")
    METRICS[metric.name] = metric


def get_metric(name: str) -> Metric:
    try:
        return METRICS[name]
    except KeyError:
        raise RegistryError(f"unknown metric {name!r}; known: {sorted(METRICS)}") from None


def applicable_metrics(task: str) -> list[str]:
    return [name for name, m in METRICS.items() if task in m.tasks]


def effective_params(metric: Metric, params: dict[str, str]) -> dict[str, str]:
    """Metric defaults overridden by the given params; unknown keys are the user's mistake."""
    unknown = sorted(set(params) - set(metric.defaults))
    if unknown:
        raise ValidationFailed(
            f"metric {metric.name!r} has no params {unknown}; known: {sorted(metric.defaults)}"
        )
    return {**metric.defaults, **params}


def params_key(params: dict[str, str]) -> str:
    return ",".join(f"{k}={params[k]}" for k in sorted(params))


def gold_only(samples: list[Sample]) -> list[Sample]:
    missing = [s.sample_id for s in samples if s.labels is None]
    if missing:
        raise ValidationFailed(
            f"{len(missing)} samples have no gold labels (e.g. {missing[:3]}); "
            "evaluate on gold-only subsets"
        )
    return samples


def require_predictions(
    samples: list[Sample], predictions: dict[str, Prediction]
) -> list[Prediction]:
    missing = [s.sample_id for s in samples if s.sample_id not in predictions]
    if missing:
        raise ValidationFailed(
            f"missing prediction for {len(missing)} samples (e.g. {missing[:3]})"
        )
    return [predictions[s.sample_id] for s in samples]
