import json

import pytest
from typer.testing import CliRunner

from helpers import det_samples, make_card, perfect_predictions, write_images
from vcp.cli import app
from vcp.cli_eval import load_plugins
from vcp.core.errors import VcpError
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.predictions import write_predictions

runner = CliRunner()


def _last_verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def seed_det(roots, name="tiny", n=40, seed=0):
    """Saved det dataset + fixed-v1 plan under the isolated roots; returns (ds, plan, paths)."""
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=seed)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def ingest_perfect(roots, tmp_path, ds, plan, run_id, subset, *, drop=0, extra=()):
    preds = perfect_predictions(ds.subset(subset, plan), ds.card)
    src = tmp_path / f"{run_id}-{subset}.jsonl"
    write_predictions(src, preds[: len(preds) - drop] if drop else preds)
    return runner.invoke(
        app,
        [
            "eval",
            "ingest",
            "--run",
            run_id,
            "--dataset",
            ds.card.name,
            "--plan",
            plan.plan_id,
            "--subset",
            subset,
            "--format",
            "jsonl",
            "--src",
            str(src),
            "--trained-on",
            "train",
            *extra,
        ],
    )


def test_eval_ingest_cli(roots, tmp_path):
    ds, plan, _ = seed_det(roots)
    r = ingest_perfect(roots, tmp_path, ds, plan, "m1", "valA", drop=1)
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert v.startswith("VERDICT cmd=eval.ingest status=OK") and "run=m1" in v and "empty=1" in v
    r = ingest_perfect(roots, tmp_path, ds, plan, "m1", "valA")
    assert r.exit_code == 1 and "replace" in _last_verdict(r.output)
    r = ingest_perfect(roots, tmp_path, ds, plan, "m1", "valA", extra=["--replace", "--json"])
    assert r.exit_code == 0
    doc = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert doc["fields"]["replaced"] is True and doc["result"]["run"]["run_id"] == "m1"
    assert "VERDICT" not in r.stdout and "VERDICT cmd=eval.ingest" in r.stderr
    r = runner.invoke(
        app,
        [
            "eval",
            "ingest",
            "--run",
            "m1",
            "--dataset",
            "tiny",
            "--plan",
            "fixed-v1",
            "--subset",
            "valB",
            "--format",
            "nope",
            "--src",
            "x",
        ],
    )
    assert r.exit_code == 2 and "RegistryError" in _last_verdict(r.output)


def test_eval_ingest_missing_source_file_fails(roots, tmp_path):
    """A missing --src file is a user-actionable FAIL, not a crash."""
    ds, plan, _ = seed_det(roots)
    missing = tmp_path / "nope.jsonl"
    r = runner.invoke(
        app,
        [
            "eval",
            "ingest",
            "--run",
            "m1",
            "--dataset",
            ds.card.name,
            "--plan",
            plan.plan_id,
            "--subset",
            "valA",
            "--format",
            "jsonl",
            "--src",
            str(missing),
        ],
    )
    assert r.exit_code == 1
    v = _last_verdict(r.output)
    assert "status=FAIL" in v and "ValidationFailed" in v and "not found" in v


def test_eval_ingest_malformed_source_fails(roots, tmp_path):
    """A source file in the wrong shape (not even valid JSON per line) is a located FAIL."""
    ds, plan, _ = seed_det(roots)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not-json\n", encoding="utf-8", newline="\n")
    r = runner.invoke(
        app,
        [
            "eval",
            "ingest",
            "--run",
            "m1",
            "--dataset",
            ds.card.name,
            "--plan",
            plan.plan_id,
            "--subset",
            "valA",
            "--format",
            "jsonl",
            "--src",
            str(bad),
        ],
    )
    assert r.exit_code == 1
    v = _last_verdict(r.output)
    assert "status=FAIL" in v and "bad.jsonl:1" in v


def test_eval_ingest_unknown_subset_aborts(roots, tmp_path):
    """A subset name the plan does not have is a structural mismatch, not a data-validation FAIL."""
    ds, plan, _ = seed_det(roots)
    src = tmp_path / "p.jsonl"
    write_predictions(src, perfect_predictions(ds.subset("valA", plan), ds.card))
    r = runner.invoke(
        app,
        [
            "eval",
            "ingest",
            "--run",
            "m1",
            "--dataset",
            ds.card.name,
            "--plan",
            plan.plan_id,
            "--subset",
            "ghost",
            "--format",
            "jsonl",
            "--src",
            str(src),
        ],
    )
    assert r.exit_code == 2
    v = _last_verdict(r.output)
    assert "status=ABORT" in v and "PlanMismatchError" in v


def test_eval_ingest_unknown_sample_id_fails_then_warns_with_opt(roots, tmp_path):
    """A prediction for a sample outside the subset FAILs by default, WARNs opted in."""
    ds, plan, _ = seed_det(roots)
    val = ds.subset("valA", plan)
    other = next(s for s in ds.samples if plan.assignment[s.sample_id] != "valA")
    preds = perfect_predictions(val, ds.card) + perfect_predictions([other], ds.card)
    src = tmp_path / "p.jsonl"
    write_predictions(src, preds)
    base_args = [
        "eval",
        "ingest",
        "--run",
        "m1",
        "--dataset",
        ds.card.name,
        "--plan",
        plan.plan_id,
        "--subset",
        "valA",
        "--format",
        "jsonl",
        "--src",
        str(src),
    ]
    r = runner.invoke(app, base_args)
    assert r.exit_code == 1
    v = _last_verdict(r.output)
    assert "status=FAIL" in v and "unknown" in v

    r = runner.invoke(app, [*base_args, "--opt", "allow_unknown=true"])
    assert r.exit_code == 0
    v = _last_verdict(r.output)
    assert "status=WARN" in v and "unknown=1" in v


def test_eval_ingest_export_manifest_wrong_directory_fails(roots, tmp_path):
    """--export-manifest pointing at a directory without the converter's manifest is a FAIL."""
    ds, plan, _ = seed_det(roots)
    pred_dir = tmp_path / "yolo_preds" / "labels"
    pred_dir.mkdir(parents=True)
    (pred_dir / "whatever.txt").write_text(
        "0 0.5 0.5 0.5 0.5 0.9\n", encoding="utf-8", newline="\n"
    )
    wrong_export = tmp_path / "not_an_export_dir"
    wrong_export.mkdir()
    r = runner.invoke(
        app,
        [
            "eval",
            "ingest",
            "--run",
            "m1",
            "--dataset",
            ds.card.name,
            "--plan",
            plan.plan_id,
            "--subset",
            "valA",
            "--format",
            "yolo_txt",
            "--src",
            str(pred_dir.parent),
            "--export-manifest",
            str(wrong_export),
        ],
    )
    assert r.exit_code == 1
    v = _last_verdict(r.output)
    assert "status=FAIL" in v and "manifest.json not found" in v


def test_load_plugins_returns_loaded_names_and_raises_on_bad_module():
    """Ruling Task5#1: load_plugins returns the loaded module names (final signature)."""
    assert load_plugins(None) == []
    assert load_plugins(["json"]) == ["json"]
    with pytest.raises(VcpError, match="cannot import plugin"):
        load_plugins(["definitely_not_a_real_module_xyz"])
