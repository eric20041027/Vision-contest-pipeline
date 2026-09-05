import pytest

from helpers import det_with_runs
from vcp.core.errors import ValidationFailed
from vcp.fuse.ablate import (
    CANDIDATE_MEASURED,
    SINGLE_MEMBER,
    VARIANT_CONFLICT,
    AblateSpec,
    ablate_recipe,
    admit_id,
    variant_id,
)
from vcp.fuse.build import RUN_BOUND_ELSEWHERE, BuildSpec, build_run
from vcp.fuse.recipes import load_recipe, recipe_path, save_recipe
from vcp.fuse.schema import Member, Recipe
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.measure.prereg import list_preregs, load_prereg, measured_subsets, prereg_time
from vcp.measure.runs import load_run, run_dir
from vcp.measure.schema import PreRegistration

STAMP = "2026-09-05T00:00:00.000Z"


def _recipe(paths, rid="r1", members=(("perfect", 1.0), ("noisy", 0.5)), params=None):
    r = Recipe(
        recipe_id=rid,
        dataset="tiny",
        plan_id="fixed-v1",
        method="wbf",
        params=params or {},
        members=[Member(run=a, weight=w) for a, w in members],
        created_at=STAMP,
    )
    save_recipe(paths, r)
    return r


def _ablate(roots, rid="r1", **kw):
    return ablate_recipe(
        AblateSpec(
            dataset="tiny", recipe_id=rid, data_root=roots.data, configs_root=roots.configs, **kw
        )
    )


def test_ids():
    assert variant_id("r1", "perfect") == "r1-minus-perfect"
    assert admit_id("r1", "perfect") == "r1-admit-perfect"


def test_ablate_writes_variants_runs_and_admission_claims(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths, params={"iou": "0.5"})
    res = _ablate(roots, preregister=True, metric="coco_map")
    assert res.variants == ["r1-minus-perfect", "r1-minus-noisy"]
    assert res.runs == ["fuse-r1", "fuse-r1-minus-perfect", "fuse-r1-minus-noisy"]
    assert res.preregs == ["r1-admit-perfect", "r1-admit-noisy"]
    assert res.built == 6 and res.cached == 0
    minus = load_recipe(paths, "r1-minus-perfect")
    assert [(m.run, m.weight) for m in minus.members] == [("noisy", 0.5)]
    assert minus.params == {"iou": "0.5"} and minus.method == "wbf" and minus.plan_id == "fixed-v1"
    for rid in res.runs:
        card = load_run(roots.data, rid)
        assert set(card.predictions) == {"valA", "valB"} and card.trained_on == ["train"]
    pr = load_prereg(paths, "r1-admit-noisy")
    assert pr.baseline_run == "fuse-r1-minus-noisy" and pr.candidate_run == "fuse-r1"
    assert pr.component == "noisy" and pr.component_class == "model" and pr.metric == "coco_map"
    assert pr.subsets == ["valA", "valB"] and pr.t_min == 2.0 and pr.min_bases == 2
    assert pr.params == {"iou": "50:95", "max_dets": "100"}  # the metric's effective params
    assert "noisy" in pr.claim and "r1" in pr.claim
    assert prereg_time(paths, "r1-admit-noisy") is not None


def test_ablate_reuses_identical_variants_and_is_cached(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    first = _ablate(roots)
    assert first.preregs == [] and first.built == 6
    again = _ablate(roots)
    assert again.built == 0 and again.cached == 6 and again.variants == first.variants
    assert list_preregs(paths) == []


def test_ablate_variant_conflict_writes_nothing(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    _recipe(paths, rid="r1-minus-noisy", members=(("perfect", 0.7),))  # same id, other weight
    with pytest.raises(ValidationFailed, match=VARIANT_CONFLICT) as ei:
        _ablate(roots)
    assert ei.value.fields == {"recipe": "r1-minus-noisy"}
    assert not recipe_path(paths, "r1-minus-perfect").exists()
    assert not run_dir(roots.data, "fuse-r1").exists()


def test_ablate_refuses_single_member_and_orphan_run(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths, rid="solo", members=(("perfect", 1.0),))
    with pytest.raises(ValidationFailed, match=SINGLE_MEMBER):
        _ablate(roots, rid="solo")
    _recipe(paths)
    _recipe(paths, rid="other", members=(("noisy", 1.0),))
    build_run(
        BuildSpec(
            dataset="tiny",
            recipe_id="other",
            run_id="fuse-r1-minus-perfect",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    with pytest.raises(ValidationFailed, match=RUN_BOUND_ELSEWHERE) as ei:
        _ablate(roots)
    assert ei.value.fields == {"run": "fuse-r1-minus-perfect"}
    assert not recipe_path(paths, "r1-minus-perfect").exists()


def test_ablate_refuses_a_measured_candidate_before_writing(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    build_run(
        BuildSpec(dataset="tiny", recipe_id="r1", data_root=roots.data, configs_root=roots.configs)
    )
    measure_run(
        MeasureSpec(
            run_id="fuse-r1", metrics=["coco_map"], data_root=roots.data, configs_root=roots.configs
        )
    )
    with pytest.raises(ValidationFailed, match=CANDIDATE_MEASURED) as ei:
        _ablate(roots, preregister=True, metric="coco_map")
    assert ei.value.fields == {"run": "fuse-r1"}
    assert not recipe_path(paths, "r1-minus-perfect").exists() and list_preregs(paths) == []
    # without --preregister the same ablation is fine: variants are just runs
    res = _ablate(roots)
    assert res.preregs == [] and len(res.runs) == 3


def test_ablate_preregister_checks(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    with pytest.raises(ValidationFailed, match="--metric"):
        _ablate(roots, preregister=True)
    with pytest.raises(ValidationFailed, match="not applicable"):
        _ablate(roots, preregister=True, metric="macro_auc")
    with pytest.raises(ValidationFailed, match="bases"):
        _ablate(roots, preregister=True, metric="coco_map", bases=["valA", "holdout"])
    assert list_preregs(paths) == [] and not run_dir(roots.data, "fuse-r1").exists()
    res = _ablate(roots, preregister=True, metric="coco_map", build=False)
    assert res.runs == [] and res.built == 0 and len(res.preregs) == 2
    assert not run_dir(roots.data, "fuse-r1").exists()
    with pytest.raises(ValidationFailed, match="already exists") as ei:
        _ablate(roots, preregister=True, metric="coco_map")
    assert ei.value.fields == {"prereg": "r1-admit-perfect"}


def test_measured_subsets_is_public(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    measure_run(
        MeasureSpec(
            run_id="noisy", metrics=["coco_map"], data_root=roots.data, configs_root=roots.configs
        )
    )
    pr = PreRegistration(
        prereg_id="x",
        claim="c",
        component="k",
        component_class="model",
        baseline_run="perfect",
        candidate_run="noisy",
        metric="coco_map",
        params={"iou": "50:95", "max_dets": "100"},
        subsets=["valA", "valB"],
        created_at=STAMP,
    )
    ledger = ReadingsLedger(paths.measure_dir / "readings.jsonl")
    assert measured_subsets(ledger, pr, "iou=50:95,max_dets=100") == ["valA", "valB"]
