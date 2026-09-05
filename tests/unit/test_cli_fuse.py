import json

import yaml
from typer.testing import CliRunner

from helpers import det_with_runs
from vcp.cli import app
from vcp.fuse.recipes import recipe_path

runner = CliRunner()


def _last_verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _recipe(*extra, members=("perfect", "noisy:0.5"), method="wbf", rid="r1"):
    args = [
        "fuse",
        "recipe",
        "--dataset",
        "tiny",
        "--id",
        rid,
        "--plan",
        "fixed-v1",
        "--method",
        method,
    ]
    for m in members:
        args += ["--member", m]
    return runner.invoke(app, [*args, *extra])


def test_fuse_recipe_cli(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    r = _recipe("--params", "iou=0.6", "--notes", "golden")
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert (
        v.startswith("VERDICT cmd=fuse.recipe status=OK") and "recipe=r1" in v and "members=2" in v
    )
    doc = yaml.safe_load(recipe_path(paths, "r1").read_text(encoding="utf-8"))
    assert doc["params"] == {
        "iou": "0.6",
        "skip": "0",
        "min_score": "0",
        "max_per_image": "0",
        "conf_type": "avg",
    }
    assert doc["members"] == [{"run": "perfect", "weight": 1.0}, {"run": "noisy", "weight": 0.5}]
    assert doc["notes"] == "golden" and doc["created_at"].endswith("Z")
    r = _recipe("--json", "--params", "iou=0.6")
    assert (
        r.exit_code == 1
        and "recipe_exists" in _last_verdict(r.output)
        and "recipe=r1" in _last_verdict(r.output)
    )
    r = _recipe("--json", rid="r2")
    assert r.exit_code == 0
    payload = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert payload["result"]["recipe"]["recipe_id"] == "r2"


def test_fuse_recipe_cli_failures_write_nothing(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    r = _recipe("--params", "iou=2")
    assert r.exit_code == 1 and "param=iou" in _last_verdict(r.output)
    r = _recipe("--params", "zz=1")
    assert r.exit_code == 1 and "param=zz" in _last_verdict(r.output)
    r = _recipe(members=("perfect", "ghost"))
    assert r.exit_code == 1 and "member=ghost" in _last_verdict(r.output)
    r = _recipe(method="nope")
    assert r.exit_code == 2 and "method=nope" in _last_verdict(r.output)
    r = _recipe(method="mean")
    assert r.exit_code == 1 and "payload=boxes" in _last_verdict(r.output)
    r = _recipe(members=("perfect:0",))
    assert r.exit_code == 1 and "weight" in _last_verdict(r.output)
    r = _recipe(members=("perfect", "perfect"))
    assert r.exit_code == 1 and "duplicate" in _last_verdict(r.output)
    assert not (paths.config_dir / "fuse").exists()


def test_fuse_group_is_listed():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0 and "fuse" in r.output
    r = runner.invoke(app, ["fuse", "--help"])
    assert r.exit_code == 0 and "recipe" in r.output


def test_fuse_build_cli(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    assert _recipe().exit_code == 0
    r = runner.invoke(app, ["fuse", "build", "--dataset", "tiny", "--recipe", "r1"])
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert v.startswith("VERDICT cmd=fuse.build status=OK") and "run=fuse-r1" in v
    assert "subsets=valA,valB" in v and "built=2" in v and "cached=0" in v and "members=2" in v
    r = runner.invoke(app, ["fuse", "build", "--dataset", "tiny", "--recipe", "r1", "--json"])
    assert r.exit_code == 0
    payload = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert payload["fields"]["cached"] == 2 and payload["result"]["run_id"] == "fuse-r1"
    assert set(payload["result"]["subsets"]) == {"valA", "valB"}
    assert payload["result"]["subsets"]["valA"]["cached"] is True
    r = runner.invoke(
        app, ["fuse", "build", "--dataset", "tiny", "--recipe", "r1", "--subsets", "holdout"]
    )
    assert (
        r.exit_code == 1
        and "member=noisy" in _last_verdict(r.output)
        and "subset=holdout" in _last_verdict(r.output)
    )
    r = runner.invoke(app, ["fuse", "build", "--dataset", "tiny", "--recipe", "ghost"])
    assert r.exit_code == 1 and "recipe=ghost" in _last_verdict(r.output)


def test_fuse_ablate_cli(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)
    assert _recipe().exit_code == 0
    r = runner.invoke(
        app,
        [
            "fuse",
            "ablate",
            "--dataset",
            "tiny",
            "--recipe",
            "r1",
            "--preregister",
            "--metric",
            "coco_map",
            "--metric-params",
            "max_dets=50",
            "--t-min",
            "1.5",
        ],
    )
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert v.startswith("VERDICT cmd=fuse.ablate status=OK") and "variants=2" in v and "runs=3" in v
    assert "built=6" in v and "cached=0" in v and "preregs=2" in v
    prereg = yaml.safe_load((paths.prereg_dir / "r1-admit-noisy.yaml").read_text(encoding="utf-8"))
    assert prereg["params"]["max_dets"] == "50" and prereg["t_min"] == 1.5
    r = runner.invoke(
        app, ["fuse", "ablate", "--dataset", "tiny", "--recipe", "r1", "--no-build", "--json"]
    )
    assert r.exit_code == 0
    payload = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert payload["fields"]["runs"] == 0 and payload["result"]["variants"] == [
        "r1-minus-perfect",
        "r1-minus-noisy",
    ]
    r = runner.invoke(
        app, ["fuse", "ablate", "--dataset", "tiny", "--recipe", "r1", "--preregister"]
    )
    assert r.exit_code == 1 and "--metric" in _last_verdict(r.output)
