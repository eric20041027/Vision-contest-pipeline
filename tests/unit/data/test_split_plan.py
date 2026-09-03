import pytest

from helpers import det_samples, make_card
from vcp.core.errors import InvariantError, PlanMismatchError, ValidationFailed, VcpError
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import (
    DEFAULT_SUBSETS,
    SplitPlan,
    assert_plan_invariants,
    load_plan,
    parse_subsets,
    resolve_group_fn,
    save_plan,
)


def test_parse_default_subsets():
    subs = parse_subsets(DEFAULT_SUBSETS)
    assert [(s.name, s.role, s.ratio) for s in subs] == [
        ("train", "train", 0.7),
        ("valA", "eval", 0.1),
        ("valB", "eval", 0.1),
        ("holdout", "sealed", 0.1),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        "train:train:0.5,val:eval:0.4",
        "a:eval:0.5,b:eval:0.5",
        "a:train:0.5,b:train:0.5",
        "train:train:0.5,val:magic:0.5",
        "train:train:0.5,train:eval:0.5",
        "train:train",
        "train:train:abc",
        "tr ain:train:1.0",
    ],
)
def test_parse_rejects(bad):
    with pytest.raises(ValidationFailed):
        parse_subsets(bad)


def _plan(ds, assignment, subsets=None, eval_gold_only=True):
    return SplitPlan(
        plan_id="p1", dataset=ds.card.name, dataset_hash=ds.card.samples_hash, strategy="fixed",
        params={"eval_gold_only": eval_gold_only, "group_key": "auto"},
        subsets=subsets or parse_subsets("train:train:0.5,val:eval:0.5"),
        assignment=assignment, created_at="2026-09-02T00:00:00.000Z",
    )


def test_plan_helpers_and_model_validation():
    ds = Dataset.from_parts(make_card("det"), det_samples(2))
    ids = [s.sample_id for s in ds.samples]
    plan = _plan(ds, {ids[0]: "train", ids[1]: "val"})
    assert plan.subset("val").role == "eval"
    assert plan.ids_in("val") == {ids[1]}
    with pytest.raises(PlanMismatchError):
        plan.subset("nope")
    with pytest.raises(ValueError, match="unknown subsets"):
        _plan(ds, {ids[0]: "ghost"})


def test_invariants_pass_and_fail():
    samples = det_samples(6, seed=0, group_every=2)
    ds = Dataset.from_parts(make_card("det"), samples)
    ids = [s.sample_id for s in samples]
    good = {sid: ("train" if i < 4 else "val") for i, sid in enumerate(ids)}
    assert_plan_invariants(_plan(ds, good), ds)
    missing = dict(good)
    missing.pop(ids[0])
    with pytest.raises(InvariantError, match="does not cover"):
        assert_plan_invariants(_plan(ds, missing), ds)
    extra = dict(good)
    extra["ghost"] = "train"
    with pytest.raises(InvariantError, match="unknown sample"):
        assert_plan_invariants(_plan(ds, extra), ds)
    split_group = dict(good)
    split_group[ids[4]] = "train"
    with pytest.raises(InvariantError, match="is split across"):
        assert_plan_invariants(_plan(ds, split_group), ds)


def test_invariant_eval_gold_only():
    samples = det_samples(4, seed=0, gold_frac=0.0)
    ds = Dataset.from_parts(make_card("det"), samples)
    ids = [s.sample_id for s in samples]
    bad = {sid: ("val" if i == 0 else "train") for i, sid in enumerate(ids)}
    with pytest.raises(InvariantError, match="non-gold"):
        assert_plan_invariants(_plan(ds, bad), ds)
    assert_plan_invariants(_plan(ds, bad, eval_gold_only=False), ds)


def test_resolve_group_fn():
    s = det_samples(1)[0].model_copy(update={"group": "g", "meta": {"patient": "p7"}})
    assert resolve_group_fn("auto")(s) == "g"
    assert resolve_group_fn("meta.patient")(s) == "p7"
    assert resolve_group_fn("meta.missing")(s) is None
    assert resolve_group_fn("auto", {"s0000": "dup"})(s) == "g"
    ungrouped = s.model_copy(update={"group": None})
    assert resolve_group_fn("auto", {"s0000": "dup"})(ungrouped) == "dup"
    with pytest.raises(ValidationFailed):
        resolve_group_fn("patient")


def test_save_and_load_plan(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(make_card("det"), det_samples(2))
    plan = _plan(ds, {s.sample_id: "train" for s in ds.samples})
    target = save_plan(plan, paths)
    assert target == paths.plan_json("p1") and target.is_file()
    assert b"\r\n" not in target.read_bytes()
    assert load_plan(paths, "p1") == plan
    with pytest.raises(VcpError, match="already exists"):
        save_plan(plan, paths)
    with pytest.raises(PlanMismatchError, match="not found"):
        load_plan(paths, "p2")
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        load_plan(paths, "p1")
