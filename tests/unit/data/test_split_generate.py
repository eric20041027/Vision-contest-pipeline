import numpy as np
import pytest

from helpers import (
    ML_CATS,
    REG_CATS,
    cls_samples,
    det_samples,
    make_card,
    multilabel_samples,
    regression_samples,
)
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.schema import Labels
from vcp.data.split import (
    DEFAULT_SUBSETS,
    STRATEGIES,
    assert_plan_invariants,
    build_plan,
    distribution_table,
    generate_fixed,
    normalize_keys,
    parse_subsets,
    stratified_take,
)


def _counts(plan):
    out: dict[str, int] = {}
    for sub in plan.assignment.values():
        out[sub] = out.get(sub, 0) + 1
    return out


def test_stratified_take_categorical_largest_remainder():
    pool = [f"a{i}" for i in range(6)] + [f"b{i}" for i in range(3)] + ["c0"]
    keys = {sid: sid[0] for sid in pool}
    taken = stratified_take(pool, keys, 5, seed=0)
    assert len(taken) == 5 and taken == sorted(taken)
    by_key = {k: sum(1 for t in taken if t[0] == k) for k in "abc"}
    assert by_key == {"a": 3, "b": 2, "c": 0}
    assert stratified_take(pool, keys, 0, seed=0) == []
    assert stratified_take(pool, keys, 99, seed=0) == sorted(pool)
    assert stratified_take(pool, keys, 5, seed=0) == taken


def test_stratified_take_vector_path():
    rng = np.random.default_rng(0)
    pool = [f"s{i:03d}" for i in range(100)]
    keys = {sid: tuple(int(x) for x in rng.random(3) < [0.5, 0.2, 0.1]) for sid in pool}
    taken = stratified_take(pool, keys, 50, seed=0)
    assert len(taken) == 50 and taken == sorted(taken)
    assert stratified_take(pool, keys, 50, seed=0) == taken


def test_normalize_keys_modes():
    assert normalize_keys({"a": (1, 0), "b": None}) == {"a": (1, 0), "b": (0, 0)}
    binned = normalize_keys({f"s{i}": float(i) for i in range(100)} | {"n": None})
    assert binned["s0"] == "q0" and binned["s99"] == "q9" and binned["n"] == "None"
    assert normalize_keys({"a": 1, "b": "x", "c": None}) == {"a": "1", "b": "x", "c": "None"}


@pytest.mark.parametrize("seed", [0, 1, 7])
@pytest.mark.parametrize(
    "subsets",
    [
        DEFAULT_SUBSETS,
        "train:train:0.8,val:eval:0.2",
        "train:train:0.6,v1:eval:0.1,v2:eval:0.1,v3:eval:0.1,hold:sealed:0.1",
    ],
)
def test_fixed_split_invariants_and_ratios(seed, subsets):
    ds = Dataset.from_parts(make_card("det"), det_samples(200, seed=seed, gold_frac=0.8))
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets(subsets), seed=seed)
    assert_plan_invariants(plan, ds)
    assert plan.strategy == "fixed" and plan.dataset_hash == ds.card.samples_hash
    n_gold = sum(s.label_source == "gold" for s in ds.samples)
    counts = _counts(plan)
    for sub in plan.subsets:
        if sub.role != "train":
            assert abs(counts.get(sub.name, 0) - round(sub.ratio * n_gold)) <= 1
    assert all(
        plan.assignment[s.sample_id] == "train" for s in ds.samples if s.label_source != "gold"
    )
    assert plan.params["seed"] == seed and plan.params["eval_gold_only"] is True


def test_determinism_and_seed_sensitivity():
    ds = Dataset.from_parts(make_card("det"), det_samples(100, seed=3))
    a = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=42)
    b = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=42)
    c = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=43)
    assert a.assignment == b.assignment
    assert a.assignment != c.assignment


def test_groups_kept_together_and_audit_groups():
    ds = Dataset.from_parts(make_card("det"), det_samples(120, seed=0, group_every=3))
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    assert_plan_invariants(plan, ds)
    assert plan.params["units"] == 40
    ds2 = Dataset.from_parts(make_card("det"), det_samples(60, seed=0))
    audit = {"s0000": "dup1", "s0001": "dup1", "s0002": "dup1"}
    plan2 = build_plan(
        ds2, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0, audit_groups=audit
    )
    assert len({plan2.assignment[k] for k in audit}) == 1
    assert plan2.params["group_from_audit"] is True
    assert plan2.params["audit_group_conflicts"] == 0


def test_audit_group_conflict_counted_but_explicit_group_wins():
    samples = det_samples(10, seed=0)
    samples[0] = samples[0].model_copy(update={"group": "explicit"})
    ds = Dataset.from_parts(make_card("det"), samples)
    plan = build_plan(
        ds,
        plan_id="p",
        subsets=parse_subsets("train:train:0.5,val:eval:0.5"),
        seed=0,
        audit_groups={"s0000": "dup", "s0001": "dup"},
    )
    assert plan.params["audit_group_conflicts"] == 1


def test_no_eval_gold_only_allows_unlabeled_in_eval():
    ds = Dataset.from_parts(make_card("det"), det_samples(100, seed=0, gold_frac=0.0))
    plan = build_plan(
        ds,
        plan_id="p",
        subsets=parse_subsets("train:train:0.5,val:eval:0.5"),
        seed=0,
        eval_gold_only=False,
    )
    assert _counts(plan) == {"train": 50, "val": 50}


def test_cls_stratification_preserves_class_proportions():
    ds = Dataset.from_parts(make_card("cls"), cls_samples(300, seed=0))
    plan = build_plan(
        ds, plan_id="p", subsets=parse_subsets("train:train:0.5,val:eval:0.5"), seed=0
    )
    table = distribution_table(plan, ds)
    for name in ("cat", "dog", "bird"):
        assert abs(table["train"].get(name, 0) - table["val"].get(name, 0)) <= 2


def test_multilabel_uses_iterative_stratification():
    ds = Dataset.from_parts(
        make_card("multilabel", categories=ML_CATS), multilabel_samples(200, seed=0)
    )
    plan = build_plan(
        ds, plan_id="p", subsets=parse_subsets("train:train:0.5,val:eval:0.5"), seed=0
    )
    table = distribution_table(plan, ds)
    for name in [c.name for c in ML_CATS]:
        assert abs(table["train"].get(name, 0) - table["val"].get(name, 0)) <= 4


def test_regression_bins_by_quantile():
    ds = Dataset.from_parts(
        make_card("regression", categories=REG_CATS), regression_samples(200, seed=0)
    )
    plan = build_plan(
        ds, plan_id="p", subsets=parse_subsets("train:train:0.5,val:eval:0.5"), seed=0
    )
    medians = {}
    for sub in ("train", "val"):
        vals = [
            ds.by_id[sid].labels.targets["age"] for sid, s in plan.assignment.items() if s == sub
        ]
        medians[sub] = float(np.median(vals))
    assert abs(medians["train"] - medians["val"]) < 15
    table = distribution_table(plan, ds)
    assert set(table["train"]) <= {f"q{i}" for i in range(10)}


def test_meta_stratify_and_group_keys():
    samples = [
        s.model_copy(update={"meta": {"site": f"S{i % 4}", "patient": f"P{i // 2}"}})
        for i, s in enumerate(det_samples(80, seed=0))
    ]
    ds = Dataset.from_parts(make_card("det"), samples)
    plan = build_plan(
        ds,
        plan_id="p",
        subsets=parse_subsets("train:train:0.5,val:eval:0.5"),
        seed=0,
        stratify_key="meta.site",
        group_key="meta.patient",
    )
    assert_plan_invariants(plan, ds, group_of=lambda s: s.meta["patient"])
    for i in range(0, 80, 2):
        assert plan.assignment[f"s{i:04d}"] == plan.assignment[f"s{i + 1:04d}"]
    table = distribution_table(plan, ds)
    assert set(table["train"]) <= {"S0", "S1", "S2", "S3"}
    assert set(table["val"]) <= {"S0", "S1", "S2", "S3"}
    with pytest.raises(ValidationFailed):
        build_plan(
            ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0, stratify_key="site"
        )


def test_strategy_registry_and_plan_id_validation():
    assert STRATEGIES["fixed"] is generate_fixed
    ds = Dataset.from_parts(make_card("det"), det_samples(4))
    with pytest.raises(ValidationFailed):
        build_plan(ds, plan_id="../x", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    with pytest.raises(RegistryError):
        build_plan(
            ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0, strategy="nope"
        )


@pytest.mark.parametrize("seed", [0, 1, 2])
@pytest.mark.parametrize("n", [1, 6, 17, 50, 99])
def test_stratified_take_vector_returns_exactly_n(seed, n):
    rng = np.random.default_rng(seed)
    pool = [f"s{i:03d}" for i in range(100)]
    keys = {sid: tuple(int(x) for x in rng.random(3) < [0.5, 0.2, 0.1]) for sid in pool}
    taken = stratified_take(pool, keys, n, seed=seed)
    assert len(taken) == n and len(set(taken)) == n and taken == sorted(taken)
    assert set(taken) <= set(pool)
    assert stratified_take(pool, keys, n, seed=seed) == taken


def test_vector_tasks_honour_ratios_exactly_on_small_pools():
    ds = Dataset.from_parts(make_card("det"), det_samples(62, seed=0))
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    assert _counts(plan) == {"train": 44, "valA": 6, "valB": 6, "holdout": 6}


def test_empty_categories_falls_back_to_random_split():
    samples = [s.model_copy(update={"labels": Labels(boxes=[])}) for s in det_samples(20, seed=0)]
    ds = Dataset.from_parts(make_card("det", categories=[]), samples)
    plan = build_plan(
        ds, plan_id="p", subsets=parse_subsets("train:train:0.5,val:eval:0.5"), seed=0
    )
    assert _counts(plan) == {"train": 10, "val": 10}


def test_tiny_pool_reports_empty_subsets():
    ds = Dataset.from_parts(make_card("det"), det_samples(5, seed=0))
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    assert plan.params["empty_subsets"] == ["valA", "valB", "holdout"]
    assert _counts(plan) == {"train": 5}
