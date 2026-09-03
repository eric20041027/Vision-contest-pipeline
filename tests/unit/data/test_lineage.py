import json

import pytest

from helpers import det_samples, make_card
from vcp.core.errors import PlanMismatchError, SealedSubsetError
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.lineage import clean_eval_subsets
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets


@pytest.fixture
def ds_and_plan(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(make_card("det"), det_samples(60, seed=0))
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    return ds, plan, paths


def test_clean_eval_subsets(ds_and_plan):
    _, plan, _ = ds_and_plan
    assert clean_eval_subsets(plan, {"train"}) == ["valA", "valB", "holdout(sealed)"]
    assert clean_eval_subsets(plan, {"train", "valA"}) == ["valB", "holdout(sealed)"]
    assert clean_eval_subsets(plan, {"train", "valA", "valB"}) == ["holdout(sealed)"]
    assert clean_eval_subsets(plan, {"train", "valA", "valB", "holdout"}) == []
    with pytest.raises(PlanMismatchError, match="unknown"):
        clean_eval_subsets(plan, {"train", "nope"})
    with pytest.raises(PlanMismatchError):
        clean_eval_subsets(plan, set())


def test_subset_returns_members_and_checks_plan(ds_and_plan):
    ds, plan, _ = ds_and_plan
    val_a = ds.subset("valA", plan)
    assert {s.sample_id for s in val_a} == plan.ids_in("valA")
    assert len(val_a) == 6
    stale = plan.model_copy(update={"dataset_hash": "0" * 64})
    with pytest.raises(PlanMismatchError, match="samples_hash"):
        ds.subset("valA", stale)
    other = plan.model_copy(update={"dataset": "other"})
    with pytest.raises(PlanMismatchError, match="belongs"):
        ds.subset("valA", other)
    with pytest.raises(PlanMismatchError):
        ds.subset("nope", plan)


def test_sealed_requires_recorded_unseal(ds_and_plan):
    ds, plan, paths = ds_and_plan
    with pytest.raises(SealedSubsetError, match="sealed"):
        ds.subset("holdout", plan)
    with pytest.raises(SealedSubsetError, match="reason"):
        ds.subset("holdout", plan, unseal=True, paths=paths)
    with pytest.raises(SealedSubsetError, match="paths"):
        ds.subset("holdout", plan, unseal=True, reason="final decision")
    assert not paths.unseal_jsonl("fixed-v1").exists()
    hold = ds.subset(
        "holdout", plan, unseal=True, reason="final decision", caller="test", paths=paths
    )
    assert {s.sample_id for s in hold} == plan.ids_in("holdout")
    lines = paths.unseal_jsonl("fixed-v1").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert len(records) == 1
    rec = records[0]
    assert rec["reason"] == "final decision" and rec["caller"] == "test"
    assert rec["subset"] == "holdout" and rec["plan_id"] == "fixed-v1"
    assert rec["dataset_hash"] == ds.card.samples_hash and rec["ts"].endswith("Z")
    ds.subset("holdout", plan, unseal=True, reason="again", paths=paths)
    assert len(paths.unseal_jsonl("fixed-v1").read_text(encoding="utf-8").splitlines()) == 2
