"""The whole measurement layer through the CLI alone (spec 13): import -> split -> export yolo
-> predictions -> ingest (yolo_txt) x4 -> measure -> anchor -> preregister x2 -> measure the
candidate -> judge (PASS and FAIL) -> status -> report.

Every step is asserted on its VERDICT line, because that machine-readable line is the interface
this framework promises -- not the Python API underneath it.
"""

import json

from typer.testing import CliRunner

from helpers import (
    CATS,
    det_samples,
    noisy_predictions,
    perfect_predictions,
    write_images,
    write_yolo_txt,
)
from vcp.cli import app
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset, write_samples_jsonl
from vcp.data.split import load_plan

runner = CliRunner()

PREREG = [
    "eval",
    "preregister",
    "--dataset",
    "flow",
    "--claim",
    "perfect wins",
    "--component",
    "cand",
    "--baseline-run",
    "base",
    "--candidate-run",
    "cand",
    "--metric",
    "coco_map",
]


def _verdicts(output: str) -> list[str]:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines


def test_eval_flow(roots, tmp_path):
    src = roots.data / "raw" / "flow"
    samples = det_samples(80, seed=5)
    write_images(src, samples)
    write_samples_jsonl(src / "samples.jsonl", samples)
    (src / "cats.json").write_text(
        json.dumps([c.model_dump() for c in CATS]), encoding="utf-8", newline="\n"
    )
    common = ["--license", "CC0", "--url", "u", "--downloaded-at", "2026-09-04"]
    r = runner.invoke(
        app,
        [
            "data",
            "import",
            "--importer",
            "jsonl",
            "--src",
            str(src),
            "--name",
            "flow",
            *common,
            "--opt",
            "task=det",
            "--opt",
            "categories=cats.json",
        ],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app, ["data", "split", "--name", "flow", "--plan-id", "fixed-v1", "--seed", "3"]
    )
    assert r.exit_code == 0, r.output
    ds = Dataset.load("flow", data_root=roots.data, configs_root=roots.configs)
    paths = DatasetPaths.resolve("flow", data_root=roots.data, configs_root=roots.configs)
    plan = load_plan(paths, "fixed-v1")

    exports = {}
    for subset in ("valA", "valB"):
        out = tmp_path / f"yolo-{subset}"
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
                subset,
                "--format",
                "yolo",
                "--out",
                str(out),
                "--opt",
                "copy=true",
            ],
        )
        assert r.exit_code == 0, r.output
        exports[subset] = out

    # The framework side of the contract: predictions arrive as a training framework wrote them
    # (ultralytics label files), not as vcp's own jsonl.
    for run_id, maker in (
        ("base", lambda s: noisy_predictions(s, ds.card, seed=7, flip=0.5)),
        ("cand", lambda s: perfect_predictions(s, ds.card)),
    ):
        for subset in ("valA", "valB"):
            manifest = json.loads((exports[subset] / "manifest.json").read_text(encoding="utf-8"))
            pred_dir = tmp_path / f"{run_id}-{subset}"
            write_yolo_txt(pred_dir, ds, maker(ds.subset(subset, plan)), manifest)
            r = runner.invoke(
                app,
                [
                    "eval",
                    "ingest",
                    "--run",
                    run_id,
                    "--dataset",
                    "flow",
                    "--plan",
                    "fixed-v1",
                    "--subset",
                    subset,
                    "--format",
                    "yolo_txt",
                    "--src",
                    str(pred_dir),
                    "--export-manifest",
                    str(exports[subset]),
                    "--trained-on",
                    "train",
                ],
            )
            assert r.exit_code == 0, r.output
            # Each eval subset holds 8 samples; `in_subset=` is that size, and `predicted=` /
            # `empty=` split it (a det run may legitimately find nothing in an image).
            assert "in_subset=8" in _verdicts(r.output)[-1], r.output

    r = runner.invoke(app, ["eval", "measure", "--run", "base"])
    assert r.exit_code == 0, r.output
    assert "readings=2" in _verdicts(r.output)[-1]
    r = runner.invoke(
        app, ["eval", "anchor", "--run", "base", "--subset", "valA", "--metric", "coco_map"]
    )
    assert r.exit_code == 0, r.output

    # Both claims are written down before the candidate is measured -- p1 as a model change
    # (no sigma_p needed), p2 as a tuning change (which cannot pass without one).
    for prereg_id, component_class in (("p1", "model"), ("p2", "tuning")):
        r = runner.invoke(app, [*PREREG, "--id", prereg_id, "--class", component_class])
        assert r.exit_code == 0, r.output

    r = runner.invoke(app, ["eval", "measure", "--run", "cand"])
    # valA is anchored (through the base run), valB is not: one cell checked, one unanchored.
    assert r.exit_code == 0 and "guardrail=partial" in _verdicts(r.output)[-1], r.output

    judge = ["eval", "judge", "--dataset", "flow", "--resamples", "40"]
    r = runner.invoke(app, [*judge, "--prereg", "p1", "--strict"])
    assert r.exit_code == 0, r.output
    assert "verdict=PASS" in _verdicts(r.output)[-1]
    r = runner.invoke(app, [*judge, "--prereg", "p2"])
    assert r.exit_code == 0, r.output
    assert "verdict=FAIL" in _verdicts(r.output)[-1] and "no_sigma" in r.output

    r = runner.invoke(app, ["eval", "status", "--dataset", "flow"])
    assert r.exit_code == 0, r.output
    v = _verdicts(r.output)[-1]
    assert "status=OK" in v and "runs=2" in v and "preregs=2" in v and "judged=2" in v
    assert "anchors=1" in v and "orphans=" not in v

    r = runner.invoke(app, ["eval", "report", "--dataset", "flow", "--json"])
    assert r.exit_code == 0, r.output
    doc = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    rows, lvl = doc["result"]["readings"], doc["result"]["last_vs_last"]
    assert doc["fields"]["rows"] == len(rows) == 4
    assert doc["fields"]["deltas"] == len(lvl) == 4  # two judgements x two subsets
    assert {(r["run_id"], r["subset"]) for r in rows} == {
        ("base", "valA"),
        ("base", "valB"),
        ("cand", "valA"),
        ("cand", "valB"),
    }
    assert [r["verdict"] for r in lvl[:2]] == ["PASS", "PASS"]  # judgements in ledger order
    assert {r["verdict"] for r in lvl} == {"PASS", "FAIL"}
    assert all(r["delta"] > 0 for r in lvl)  # the candidate really is better, on both claims
    assert "VERDICT" not in r.stdout and "VERDICT cmd=eval.report" in r.stderr

    # --metric reaches BOTH halves of the report: a report filtered to a metric this dataset
    # never measured is empty top and bottom, not empty above and every judgement below.
    r = runner.invoke(app, ["eval", "report", "--dataset", "flow", "--metric", "accuracy"])
    assert r.exit_code == 0, r.output
    assert "rows=0" in _verdicts(r.output)[-1] and "deltas=0" in _verdicts(r.output)[-1]
