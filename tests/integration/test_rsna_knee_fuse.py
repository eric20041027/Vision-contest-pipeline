"""Fusion on the RSNA Knee subset: two perfect runs fused by rank_mean and mean stay perfect.

Nothing is written under the real data root: the fusers are called directly on in-memory
members built from the gold labels, exactly as `build` would call them.
"""

from __future__ import annotations

import pytest

from conftest import load_real
from vcp.fuse.fusers import FuseContext, MemberPredictions, get_fuser
from vcp.measure.metrics import get_metric
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import Prediction

pytestmark = pytest.mark.realdata


@pytest.fixture(scope="module")
def knee(real_roots):
    return load_real("rsna-knee", real_roots)


@pytest.mark.parametrize("method", ["rank_mean", "mean"])
def test_two_perfect_runs_fuse_to_auc_one(knee, method):
    gold = [s for s in knee.samples if s.label_source == "gold"]
    names = [c.name for c in knee.card.categories]
    perfect = predictions_by_id(
        [
            Prediction(sample_id=s.sample_id, scores={n: float(s.labels.targets[n]) for n in names})
            for s in gold
        ]
    )
    members = [MemberPredictions("a", 1.0, perfect), MemberPredictions("b", 0.5, perfect)]
    ctx = FuseContext(
        dataset=knee,
        subset="gold",
        ids=[s.sample_id for s in gold],
        samples={s.sample_id: s for s in gold},
        params={},
    )
    fused = predictions_by_id(get_fuser(method).fuse(members, ctx))
    assert set(fused) == set(perfect)
    res = get_metric("macro_auc").compute(gold, fused, knee.card, {})
    assert res.value == pytest.approx(1.0) and res.n == len(gold)
    if method == "mean":
        assert all(fused[k].scores == perfect[k].scores for k in perfect)
