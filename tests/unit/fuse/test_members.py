import pytest

from helpers import det_samples, det_with_runs, make_card, perfect_predictions, write_images
from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.fuse.members import (
    check_members,
    check_plan,
    common_subsets,
    parse_member,
    union_trained_on,
)
from vcp.fuse.schema import Member
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.predictions import write_predictions


def test_parse_member():
    assert parse_member("a") == Member(run="a", weight=1.0)
    assert parse_member("a:0.5") == Member(run="a", weight=0.5)
    for bad in ("", ":1", "a:", "a:x", "a:0", "a:-1"):
        with pytest.raises(ValidationFailed):
            parse_member(bad)


def test_check_members_loads_cards_in_order(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    cards = check_members(
        [Member(run="noisy"), Member(run="perfect", weight=0.5)],
        data_root=roots.data,
        dataset=ds,
        plan_id="fixed-v1",
    )
    assert [c.run_id for c in cards] == ["noisy", "perfect"]
    assert union_trained_on(cards) == ["train"]
    assert common_subsets(plan, cards) == ["valA", "valB"]
    assert common_subsets(plan, cards[1:]) == ["valA", "valB", "holdout"]


def test_check_members_failures_name_the_member(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    with pytest.raises(ValidationFailed, match="run not found") as ei:
        check_members(
            [Member(run="perfect"), Member(run="ghost")],
            data_root=roots.data,
            dataset=ds,
            plan_id="fixed-v1",
        )
    assert ei.value.fields["member"] == "ghost"
    with pytest.raises(PlanMismatchError, match="plan") as ei:
        check_members(
            [Member(run="perfect")], data_root=roots.data, dataset=ds, plan_id="other-plan"
        )
    assert ei.value.fields["member"] == "perfect"
    # a run of another dataset: same run id namespace (runs/ is per data root), other card
    other_paths = DatasetPaths.resolve("other", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(20, seed=9)
    write_images(roots.data / "raw" / "other", samples)
    other = Dataset.from_parts(make_card("det", name="other", image_root="raw/other"), samples)
    other.save(other_paths)
    other_plan = build_plan(
        other, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0
    )
    save_plan(other_plan, other_paths)
    src = tmp_path / "other-valA.jsonl"
    write_predictions(src, perfect_predictions(other.subset("valA", other_plan), other.card))
    ingest(
        IngestSpec(
            run_id="stranger",
            dataset="other",
            plan_id="fixed-v1",
            subset="valA",
            format="jsonl",
            src=src,
            trained_on=["train"],
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    with pytest.raises(PlanMismatchError, match="belongs to dataset") as ei:
        check_members(
            [Member(run="stranger")], data_root=roots.data, dataset=ds, plan_id="fixed-v1"
        )
    assert ei.value.fields["member"] == "stranger"


def test_union_trained_on_and_check_plan(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    src = tmp_path / "wide-valB.jsonl"
    write_predictions(src, perfect_predictions(ds.subset("valB", plan), ds.card))
    ingest(
        IngestSpec(
            run_id="wide",
            dataset="tiny",
            plan_id="fixed-v1",
            subset="valB",
            format="jsonl",
            src=src,
            trained_on=["train", "valA"],
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    cards = check_members(
        [Member(run="perfect"), Member(run="wide")],
        data_root=roots.data,
        dataset=ds,
        plan_id="fixed-v1",
    )
    assert union_trained_on(cards) == ["train", "valA"]
    assert common_subsets(plan, cards) == ["valB"]
    check_plan(plan, ds)
    with pytest.raises(PlanMismatchError):
        check_plan(plan.model_copy(update={"dataset_hash": "0" * 64}), ds)
