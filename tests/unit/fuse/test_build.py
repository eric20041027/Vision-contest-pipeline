import json

import pytest

from helpers import (
    ML_CATS,
    dataset_with_perfect_run,
    det_with_runs,
    multilabel_samples,
    noisy_predictions,
    perfect_predictions,
)
from vcp import __version__
from vcp.core.errors import IntegrityError, PlanMismatchError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.fuse.build import (
    FRAMEWORK,
    NO_COMMON_SUBSET,
    OUTPUT_EXISTS,
    RUN_BOUND_ELSEWHERE,
    BuildSpec,
    build_run,
    content_sha,
    load_record,
    record_path,
)
from vcp.fuse.fusers import FUSERS, register_fuser
from vcp.fuse.recipes import recipe_sha, save_recipe
from vcp.fuse.schema import Member, Recipe
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.predictions import read_predictions, write_predictions
from vcp.measure.runs import load_run, run_dir
from vcp.measure.schema import Prediction

STAMP = "2026-09-05T00:00:00.000Z"


def _recipe(
    paths,
    rid="r1",
    members=(("perfect", 1.0), ("noisy", 0.5)),
    method="wbf",
    params=None,
    dataset="tiny",
):
    r = Recipe(
        recipe_id=rid,
        dataset=dataset,
        plan_id="fixed-v1",
        method=method,
        params=params or {},
        members=[Member(run=a, weight=w) for a, w in members],
        created_at=STAMP,
    )
    save_recipe(paths, r)
    return r


def _build(roots, rid="r1", dataset="tiny", **kw):
    return build_run(
        BuildSpec(
            dataset=dataset, recipe_id=rid, data_root=roots.data, configs_root=roots.configs, **kw
        )
    )


def test_build_writes_an_ordinary_run_with_provenance(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths, params={"iou": "0.5"})
    res = _build(roots)
    assert res.created_run and res.built == 2 and res.cached == 0
    assert list(res.subsets) == [
        "valA",
        "valB",
    ]  # common subsets, plan order (noisy has no holdout)
    card = load_run(roots.data, "fuse-r1")
    assert card.source.framework == FRAMEWORK
    assert card.source.config_hash == recipe_sha(paths, "r1")
    assert card.trained_on == ["train"] and card.plan_id == "fixed-v1"
    assert card.samples_hash == ds.card.samples_hash
    for subset in ("valA", "valB"):
        entry = card.predictions[subset]
        assert entry.format_in == "fuse:wbf" and entry.export_manifest_sha is None
        path = run_dir(roots.data, "fuse-r1") / entry.path
        assert sha256_file(path) == entry.sha256 == res.subsets[subset].sha256
        rows = read_predictions(path)
        assert entry.samples == len(rows) and entry.samples + entry.empty == len(
            plan.ids_in(subset)
        )
        assert all(p.boxes for p in rows)
    rec = load_record(roots.data, "fuse-r1")
    assert (
        rec.run_id == "fuse-r1"
        and rec.recipe_id == "r1"
        and rec.recipe_sha256 == card.source.config_hash
    )
    assert rec.method == "wbf" and rec.method_version == "1" and rec.vcp_version == __version__
    assert rec.params["iou"] == "0.5" and rec.params["conf_type"] == "avg"
    assert [(m.run, m.weight, m.trained_on) for m in rec.members] == [
        ("perfect", 1.0, ["train"]),
        ("noisy", 0.5, ["train"]),
    ]
    perfect = load_run(roots.data, "perfect")
    assert rec.subsets["valA"].member_sha256 == {
        "perfect": perfect.predictions["valA"].sha256,
        "noisy": load_run(roots.data, "noisy").predictions["valA"].sha256,
    }
    assert rec.subsets["valA"].output_sha256 == card.predictions["valA"].sha256
    text = record_path(roots.data, "fuse-r1").read_bytes()
    assert b"\r" not in text and text.endswith(b"\n")


def test_rebuild_is_cached_and_bytes_identical(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    first = _build(roots)
    path = run_dir(roots.data, "fuse-r1") / "predictions" / "valA.jsonl"
    before = path.read_bytes()
    stamp_before = record_path(roots.data, "fuse-r1").read_text(encoding="utf-8")
    again = _build(roots)
    assert again.built == 0 and again.cached == 2 and not again.created_run
    assert all(o.cached for o in again.subsets.values())
    assert path.read_bytes() == before
    assert record_path(roots.data, "fuse-r1").read_text(encoding="utf-8") == stamp_before
    assert not (run_dir(roots.data, "fuse-r1") / "history.jsonl").exists()
    assert first.subsets["valA"].sha256 == again.subsets["valA"].sha256


def test_content_sha_matches_write_predictions(tmp_path):
    preds = [Prediction(sample_id="b", scores={"x": 0.5}), Prediction(sample_id="a", boxes=[])]
    assert content_sha(preds) == write_predictions(tmp_path / "p.jsonl", preds)


def test_explicit_subsets_and_missing_member_file(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    res = _build(roots, subsets=["valB"])
    assert list(res.subsets) == ["valB"]
    with pytest.raises(ValidationFailed, match="no predictions") as ei:
        _build(roots, subsets=["holdout"])
    assert ei.value.fields == {"member": "noisy", "subset": "holdout"}
    with pytest.raises(PlanMismatchError):
        _build(roots, subsets=["nope"])
    assert "holdout" not in load_run(roots.data, "fuse-r1").predictions


def test_no_common_subset(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    src = tmp_path / "h.jsonl"
    write_predictions(
        src,
        perfect_predictions(
            ds.subset("holdout", plan, unseal=True, reason="t", paths=paths), ds.card
        ),
    )
    ingest(
        IngestSpec(
            run_id="holdout_only",
            dataset="tiny",
            plan_id="fixed-v1",
            subset="holdout",
            format="jsonl",
            src=src,
            trained_on=["train"],
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    _recipe(paths, members=(("noisy", 1.0), ("holdout_only", 1.0)))
    with pytest.raises(ValidationFailed, match=NO_COMMON_SUBSET):
        _build(roots)
    assert not run_dir(roots.data, "fuse-r1").exists()


def test_tampered_member_file_fails_before_any_write(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    path = run_dir(roots.data, "noisy") / "predictions" / "valB.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(IntegrityError) as ei:
        _build(roots)
    assert ei.value.fields == {"member": "noisy", "subset": "valB"}
    assert not run_dir(roots.data, "fuse-r1").exists()


def test_output_exists_replace_and_history(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    first = _build(roots)
    # the member changes (re-ingested with other predictions) -> the fused bytes would change
    src = tmp_path / "noisy2-valA.jsonl"
    write_predictions(src, noisy_predictions(ds.subset("valA", plan), ds.card, seed=99, flip=0.6))
    ingest(
        IngestSpec(
            run_id="noisy",
            dataset="tiny",
            plan_id="fixed-v1",
            subset="valA",
            format="jsonl",
            src=src,
            replace=True,
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    with pytest.raises(ValidationFailed, match=OUTPUT_EXISTS) as ei:
        _build(roots)
    assert ei.value.fields == {"run": "fuse-r1", "subset": "valA"}
    assert (
        load_run(roots.data, "fuse-r1").predictions["valA"].sha256 == first.subsets["valA"].sha256
    )
    res = _build(roots, replace=True)
    assert res.built == 1 and res.cached == 1 and not res.subsets["valA"].cached
    assert res.subsets["valA"].sha256 != first.subsets["valA"].sha256
    lines = [
        json.loads(line)
        for line in (run_dir(roots.data, "fuse-r1") / "history.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert lines == [
        {
            "event": "replace",
            "subset": "valA",
            "old_sha256": first.subsets["valA"].sha256,
            "via": "fuse.build",
            "ts": lines[0]["ts"],
        }
    ]
    rec = load_record(roots.data, "fuse-r1")
    assert (
        rec.subsets["valA"].member_sha256["noisy"]
        == load_run(roots.data, "noisy").predictions["valA"].sha256
    )
    assert rec.subsets["valB"].output_sha256 == first.subsets["valB"].sha256


def test_run_id_is_bound_to_one_recipe(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    _recipe(paths)
    _build(roots)
    _recipe(paths, rid="r2", params={"iou": "0.9"})
    with pytest.raises(ValidationFailed, match=RUN_BOUND_ELSEWHERE) as ei:
        _build(roots, rid="r2", run_id="fuse-r1")
    assert ei.value.fields == {"run": "fuse-r1"}
    with pytest.raises(ValidationFailed, match=RUN_BOUND_ELSEWHERE):
        _build(roots, run_id="perfect")  # an ingested run is not a build of anything
    res = _build(roots, rid="r2", run_id="custom")
    assert res.run.run_id == "custom" and load_record(roots.data, "custom").recipe_id == "r2"


def test_plugin_fuser_output_is_validated_and_nothing_written(roots, tmp_path, monkeypatch):
    ds, plan, paths = det_with_runs(roots, tmp_path)

    class Rogue:
        name = "test_rogue"
        version = "1"
        payloads = frozenset({"boxes"})
        defaults = {}

        def check_params(self, params):
            return None

        def fuse(self, members, ctx):
            return [Prediction(sample_id="not-in-subset", boxes=[])]

    monkeypatch.delitem(FUSERS, "test_rogue", raising=False)
    register_fuser(Rogue())
    try:
        _recipe(paths, method="test_rogue")
        with pytest.raises(ValidationFailed, match="unknown sample_id") as ei:
            _build(roots)
        assert ei.value.fields == {"subset": "valA"}
        assert not run_dir(roots.data, "fuse-r1").exists()
    finally:
        FUSERS.pop("test_rogue", None)


def test_mean_build_on_multilabel(roots, tmp_path):
    ds, plan, paths = dataset_with_perfect_run(
        roots,
        tmp_path,
        name="ml",
        task="multilabel",
        samples=multilabel_samples(40, seed=1),
        categories=ML_CATS,
        run_id="a",
    )
    for subset in ("valA", "valB"):
        src = tmp_path / f"b-{subset}.jsonl"
        write_predictions(src, noisy_predictions(ds.subset(subset, plan), ds.card, seed=3))
        ingest(
            IngestSpec(
                run_id="b",
                dataset="ml",
                plan_id="fixed-v1",
                subset=subset,
                format="jsonl",
                src=src,
                trained_on=["train"],
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )
    _recipe(paths, rid="m", members=(("a", 1.0), ("b", 1.0)), method="mean", dataset="ml")
    res = _build(roots, rid="m", dataset="ml")
    a = {
        p.sample_id: p
        for p in read_predictions(run_dir(roots.data, "a") / "predictions" / "valA.jsonl")
    }
    b = {
        p.sample_id: p
        for p in read_predictions(run_dir(roots.data, "b") / "predictions" / "valA.jsonl")
    }
    fused = read_predictions(run_dir(roots.data, "fuse-m") / "predictions" / "valA.jsonl")
    assert len(fused) == len(a) == res.subsets["valA"].samples and res.subsets["valA"].empty == 0
    for p in fused:
        for k, v in p.scores.items():
            assert v == pytest.approx((a[p.sample_id].scores[k] + b[p.sample_id].scores[k]) / 2)
