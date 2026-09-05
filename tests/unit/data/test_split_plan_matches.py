import pytest

from helpers import det_samples, make_card
from vcp.core.errors import PlanMismatchError
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, assert_plan_matches, build_plan, parse_subsets


def test_assert_plan_matches():
    ds = Dataset.from_parts(make_card("det"), det_samples(20, seed=0))
    ds.card = ds.card.model_copy(update={"samples_hash": "a" * 64})
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    assert_plan_matches(plan, ds.card)
    with pytest.raises(PlanMismatchError, match="samples_hash"):
        assert_plan_matches(plan, ds.card.model_copy(update={"samples_hash": "b" * 64}))
    with pytest.raises(PlanMismatchError, match="belongs to dataset"):
        assert_plan_matches(plan, ds.card.model_copy(update={"name": "other"}))
