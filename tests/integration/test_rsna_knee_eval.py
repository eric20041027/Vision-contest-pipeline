"""Measurement layer on the RSNA Knee subset: perfect and random predictions bracket macro AUC.

The synthetic fixtures prove the metric's arithmetic; this proves it survives a real card --
twelve multilabel targets, names with spaces and apostrophes, and studies whose labels are
incomplete (those are not gold and are not scored).
"""

from __future__ import annotations

import random

import pytest

from conftest import load_real
from vcp.measure.metrics import get_metric
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import Prediction

pytestmark = pytest.mark.realdata


@pytest.fixture(scope="module")
def knee(real_roots):
    return load_real("rsna-knee", real_roots)


def test_macro_auc_brackets(knee):
    gold = [s for s in knee.samples if s.label_source == "gold"]
    names = [c.name for c in knee.card.categories]
    perfect = predictions_by_id(
        [
            Prediction(sample_id=s.sample_id, scores={n: float(s.labels.targets[n]) for n in names})
            for s in gold
        ]
    )
    res = get_metric("macro_auc").compute(gold, perfect, knee.card, {})
    assert res.value == pytest.approx(1.0) and res.n == len(gold)
    rng = random.Random(0)
    noise = predictions_by_id(
        [Prediction(sample_id=s.sample_id, scores={n: rng.random() for n in names}) for s in gold]
    )
    assert 0.25 < get_metric("macro_auc").compute(gold, noise, knee.card, {}).value < 0.75
