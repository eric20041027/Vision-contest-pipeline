"""Classification, multi-label and regression metrics on top of scikit-learn."""

from __future__ import annotations

import math

import numpy as np
from sklearn.metrics import accuracy_score, f1_score, log_loss, roc_auc_score

from vcp.core.errors import ValidationFailed
from vcp.data.schema import DatasetCard
from vcp.measure.metrics.base import gold_only, require_predictions
from vcp.measure.schema import MetricResult

EPS = 1e-15


def _names(card: DatasetCard) -> list[str]:
    return [c.name for c in card.categories]


def _cls_arrays(samples, predictions, card) -> tuple[np.ndarray, np.ndarray, list[str]]:
    names = _names(card)
    index_of = {c.id: i for i, c in enumerate(card.categories)}
    preds = require_predictions(gold_only(samples), predictions)
    y_true = np.array([index_of[s.labels.cls] for s in samples], dtype=int)  # type: ignore[union-attr]
    scores = np.array([[p.scores[n] for n in names] for p in preds], dtype=float)  # type: ignore[index]
    return y_true, scores, names


class Accuracy:
    name, version, tasks, defaults = "accuracy", "1", frozenset({"cls"}), {}

    def compute(self, samples, predictions, card, params) -> MetricResult:
        y_true, scores, _ = _cls_arrays(samples, predictions, card)
        return MetricResult(
            value=float(accuracy_score(y_true, scores.argmax(axis=1))), n=len(y_true)
        )


class MacroF1:
    name, version, tasks, defaults = "macro_f1", "1", frozenset({"cls"}), {}

    def compute(self, samples, predictions, card, params) -> MetricResult:
        y_true, scores, names = _cls_arrays(samples, predictions, card)
        labels = list(range(len(names)))
        per = f1_score(y_true, scores.argmax(axis=1), labels=labels, average=None, zero_division=0)
        return MetricResult(
            value=float(np.mean(per)),
            per_class={n: float(v) for n, v in zip(names, per, strict=True)},
            n=len(y_true),
        )


class LogLoss:
    name, version, tasks, defaults = "log_loss", "1", frozenset({"cls"}), {}

    def compute(self, samples, predictions, card, params) -> MetricResult:
        y_true, scores, names = _cls_arrays(samples, predictions, card)
        probs = np.clip(scores, EPS, 1.0)
        probs = probs / probs.sum(axis=1, keepdims=True)
        return MetricResult(
            value=float(log_loss(y_true, probs, labels=list(range(len(names))))), n=len(y_true)
        )


class MacroAuc:
    name, version, tasks, defaults = "macro_auc", "1", frozenset({"multilabel"}), {}

    def compute(self, samples, predictions, card, params) -> MetricResult:
        names = _names(card)
        preds = require_predictions(gold_only(samples), predictions)
        y_true = np.array([[s.labels.targets[n] for n in names] for s in samples], dtype=float)  # type: ignore[index]
        scores = np.array([[p.scores[n] for n in names] for p in preds], dtype=float)  # type: ignore[index]
        per_class: dict[str, float | None] = {}
        for j, n in enumerate(names):
            col = y_true[:, j]
            per_class[n] = (
                float(roc_auc_score(col, scores[:, j])) if 0 < col.sum() < len(col) else None
            )
        defined = [v for v in per_class.values() if v is not None]
        if not defined:
            raise ValidationFailed(
                "macro_auc undefined: every class is all-positive or all-negative"
            )
        return MetricResult(value=float(np.mean(defined)), per_class=per_class, n=len(samples))


def _regression_errors(samples, predictions, card) -> np.ndarray:
    names = _names(card)
    preds = require_predictions(gold_only(samples), predictions)
    errors = []
    for s, p in zip(samples, preds, strict=True):
        for n in names:
            if p.targets is None or n not in p.targets:
                raise ValidationFailed(f"sample {s.sample_id!r}: missing target {n!r}")
            errors.append(p.targets[n] - s.labels.targets[n])  # type: ignore[index]
    return np.array(errors, dtype=float)


class Rmse:
    name, version, tasks, defaults = "rmse", "1", frozenset({"regression"}), {}

    def compute(self, samples, predictions, card, params) -> MetricResult:
        e = _regression_errors(samples, predictions, card)
        return MetricResult(value=float(math.sqrt(np.mean(e**2))), n=len(samples))


class Mae:
    name, version, tasks, defaults = "mae", "1", frozenset({"regression"}), {}

    def compute(self, samples, predictions, card, params) -> MetricResult:
        e = _regression_errors(samples, predictions, card)
        return MetricResult(value=float(np.mean(np.abs(e))), n=len(samples))
