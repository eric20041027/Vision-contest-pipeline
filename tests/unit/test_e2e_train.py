"""The whole training layer through the CLI alone (spec 12): export yolo -> train run (fake
command, checkpoints, final, local upload) -> status -> predictions -> eval ingest (no
--trained-on: the card already knows) -> eval measure -> train run --resume -> train upload to a
second destination -> status. Every step is asserted on its VERDICT line."""

import json
import sys

from typer.testing import CliRunner

from helpers import det_samples, make_card, perfect_predictions, write_images
from vcp.cli import app
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.predictions import write_predictions
from vcp.measure.runs import load_run

runner = CliRunner()

FAKE = """
import os, sys
from pathlib import Path
print("training with seed", os.environ.get("VCP_SEED"), "run", os.environ.get("VCP_RUN_ID"))
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best-" + os.environ.get("VCP_SEED", "").encode())
Path("weights/last.pt").write_bytes(b"last")
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
"""


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _json(r) -> dict:
    return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))


def test_training_flow(roots, tmp_path):
    paths = DatasetPaths.resolve("flow", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(60, seed=5)
    write_images(roots.data / "raw" / "flow", samples)
    ds = Dataset.from_parts(make_card("det", name="flow", image_root="raw/flow"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=3)
    save_plan(plan, paths)
    export = tmp_path / "yolo-train"
    r = runner.invoke(
        app,
        [
            "data",
            "export",
            "--name",
            "flow",
            "--plan",
            "fixed-v1",
            "--subset",
            "train",
            "--format",
            "yolo",
            "--out",
            str(export),
        ],
    )
    assert r.exit_code == 0, r.output
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_text(FAKE, encoding="utf-8")
    vault = tmp_path / "vault"

    r = runner.invoke(
        app,
        [
            "train",
            "run",
            "--run",
            "m1",
            "--dataset",
            "flow",
            "--plan",
            "fixed-v1",
            "--export",
            str(export),
            "--seed",
            "7",
            "--framework",
            "fake 1.0",
            "--cwd",
            str(work),
            "--checkpoints",
            "weights/*.pt",
            "--final",
            "weights/best.pt",
            "--upload",
            str(vault),
            "--",
            sys.executable,
            "fake_train.py",
            "0",
        ],
    )
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "status=WARN" in v and "venv=inherited" in v and "seed=7" in v
    assert "exit_code=0" in v and "checkpoints=2" in v and "uploaded=2" in v and "verified=2" in v
    assert "training with seed 7 run m1" in r.output
    card = load_run(roots.data, "m1")
    assert card.trained_on == ["train"] and card.source.framework == "fake 1.0"
    assert card.source.export_manifest_sha == sha256_file(export / "manifest.json")
    assert card.source.weights_hash == sha256_file(work / "weights" / "best.pt")
    assert sha256_file(vault / "m1" / "best.pt") == card.source.weights_hash

    r = runner.invoke(app, ["train", "status", "--run", "m1"])
    assert (
        r.exit_code == 0
        and "status=OK" in _verdict(r.output)
        and "unbacked=0" in _verdict(r.output)
    )

    for subset in ("valA", "valB"):
        src = tmp_path / f"pred-{subset}.jsonl"
        write_predictions(src, perfect_predictions(ds.subset(subset, plan), ds.card))
        r = runner.invoke(
            app,
            [
                "eval",
                "ingest",
                "--run",
                "m1",
                "--dataset",
                "flow",
                "--plan",
                "fixed-v1",
                "--subset",
                subset,
                "--format",
                "jsonl",
                "--src",
                str(src),
            ],
        )
        assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["eval", "measure", "--run", "m1", "--metrics", "coco_map"])
    assert r.exit_code == 0, r.output
    assert "readings=2" in _verdict(r.output)
    assert load_run(roots.data, "m1").source.framework == "fake 1.0"  # ingest kept the card's facts

    r = runner.invoke(
        app,
        [
            "train",
            "run",
            "--run",
            "m1",
            "--dataset",
            "flow",
            "--plan",
            "fixed-v1",
            "--export",
            str(export),
            "--seed",
            "7",
            "--framework",
            "fake 1.0",
            "--cwd",
            str(work),
            "--checkpoints",
            "weights/*.pt",
            "--final",
            "weights/best.pt",
            "--resume",
            "--json",
            "--",
            sys.executable,
            "fake_train.py",
            "0",
        ],
    )
    assert r.exit_code == 0, r.output
    doc = _json(r)
    assert doc["fields"]["attempt"] == 2 and len(doc["result"]["attempts"]) == 2
    assert doc["fields"]["checkpoints"] == 2  # same bytes as attempt 1: nothing new registered

    r = runner.invoke(app, ["train", "upload", "--run", "m1", "--dest", str(tmp_path / "vault2")])
    assert (
        r.exit_code == 0
        and "uploaded=2" in _verdict(r.output)
        and "verified=2" in _verdict(r.output)
    )
    r = runner.invoke(app, ["train", "upload", "--run", "m1", "--dest", str(tmp_path / "vault2")])
    assert r.exit_code == 0 and "skipped=2" in _verdict(r.output)
    r = runner.invoke(app, ["train", "status", "--run", "m1", "--json"])
    assert r.exit_code == 0
    assert len(_json(r)["result"]["uploads"]) == 4  # two destinations x two files

    r = runner.invoke(
        app,
        [
            "train",
            "run",
            "--run",
            "m2",
            "--dataset",
            "flow",
            "--plan",
            "fixed-v1",
            "--trained-on",
            "train",
            "--cwd",
            str(work),
            "--checkpoints",
            "weights/*.pt",
            "--final",
            "weights/best.pt",
            "--upload",
            str(vault),
            "--",
            sys.executable,
            "fake_train.py",
            "1",
        ],
    )
    assert r.exit_code == 1, r.output
    v = _verdict(r.output)
    assert "status=FAIL" in v and "exit_code=1" in v and "checkpoints=2" in v and "uploaded=0" in v
    assert not (vault / "m2").exists()
