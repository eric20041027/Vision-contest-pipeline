"""The whole fusion layer through the CLI alone (spec 12): three ingested members -> recipe ->
ablate --preregister -> measure x4 -> judge x3 (admission PASS for the member that matters,
FAIL for pure false positives) -> report; mean / rank_mean on a multilabel dataset; a fuser
registered by --plugin. Every step is asserted on its VERDICT line."""

import json
import random

from typer.testing import CliRunner

from helpers import (
    ML_CATS,
    dataset_with_perfect_run,
    det_samples,
    make_card,
    multilabel_samples,
    noisy_predictions,
    perfect_predictions,
    write_images,
)
from vcp.cli import app
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample, View
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.predictions import write_predictions
from vcp.measure.schema import PredBox, Prediction

runner = CliRunner()


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _json(r) -> dict:
    return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))


def _samples64(n: int, seed: int) -> list[Sample]:
    """det samples whose gold boxes sit inside [0, 8] of a 64x64 view: room for far false
    positives that can never overlap a gold box, and never get clipped."""
    return [
        s.model_copy(update={"views": [View(path=v.path, width=64, height=64) for v in s.views]})
        for s in det_samples(n, seed=seed)
    ]


def _far_fps(samples: list[Sample], *, seed: int) -> list[Prediction]:
    rng = random.Random(seed)
    out = []
    for s in samples:
        boxes = [
            PredBox(
                x=rng.uniform(40, 50),
                y=rng.uniform(40, 50),
                w=rng.uniform(1, 6),
                h=rng.uniform(1, 6),
                category_id=rng.choice([0, 1, 2]),
                score=rng.uniform(0.3, 0.98),
            )
            for _ in range(2)
        ]
        out.append(Prediction(sample_id=s.sample_id, boxes=boxes))
    return out


def _ingest(roots, tmp_path, ds, plan, run_id, maker):
    for subset in ("valA", "valB"):
        src = tmp_path / f"{run_id}-{subset}.jsonl"
        write_predictions(src, maker(ds.subset(subset, plan)))
        ingest(
            IngestSpec(
                run_id=run_id,
                dataset=ds.card.name,
                plan_id=plan.plan_id,
                subset=subset,
                format="jsonl",
                src=src,
                trained_on=["train"],
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )


def test_admission_flow_det(roots, tmp_path):
    paths = DatasetPaths.resolve("flow", data_root=roots.data, configs_root=roots.configs)
    samples = _samples64(120, seed=5)
    write_images(roots.data / "raw" / "flow", samples, size=(64, 64))
    ds = Dataset.from_parts(make_card("det", name="flow", image_root="raw/flow"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=3)
    save_plan(plan, paths)
    _ingest(roots, tmp_path, ds, plan, "good", lambda s: perfect_predictions(s, ds.card))
    _ingest(roots, tmp_path, ds, plan, "noise", lambda s: _far_fps(s, seed=11))
    _ingest(
        roots, tmp_path, ds, plan, "half", lambda s: noisy_predictions(s, ds.card, seed=7, flip=0.5)
    )

    r = runner.invoke(
        app,
        [
            "fuse",
            "recipe",
            "--dataset",
            "flow",
            "--id",
            "r1",
            "--plan",
            "fixed-v1",
            "--method",
            "wbf",
            "--params",
            "iou=0.5",
            "--member",
            "good",
            "--member",
            "noise",
            "--member",
            "half:0.5",
        ],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app,
        [
            "fuse",
            "ablate",
            "--dataset",
            "flow",
            "--recipe",
            "r1",
            "--preregister",
            "--metric",
            "coco_map",
        ],
    )
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "variants=3" in v and "runs=4" in v and "preregs=3" in v

    for run_id in ("fuse-r1", "fuse-r1-minus-good", "fuse-r1-minus-noise", "fuse-r1-minus-half"):
        r = runner.invoke(app, ["eval", "measure", "--run", run_id, "--metrics", "coco_map"])
        assert r.exit_code == 0, r.output
        assert "readings=2" in _verdict(r.output), _verdict(r.output)

    verdicts = {}
    for member in ("good", "noise", "half"):
        r = runner.invoke(
            app, ["eval", "judge", "--dataset", "flow", "--prereg", f"r1-admit-{member}", "--json"]
        )
        assert r.exit_code == 0, r.output
        doc = _json(r)
        verdicts[member] = doc["result"]["judgement"]["verdict"]
        assert set(doc["result"]["judgement"]["per_subset"]) == {"valA", "valB"}
    assert verdicts["good"] == "PASS", verdicts
    assert verdicts["noise"] == "FAIL", verdicts
    assert verdicts["half"] in ("PASS", "FAIL"), verdicts

    r = runner.invoke(app, ["eval", "report", "--dataset", "flow", "--json"])
    assert r.exit_code == 0, r.output
    doc = _json(r)
    assert {row["prereg_id"] for row in doc["result"]["last_vs_last"]} == {
        "r1-admit-good",
        "r1-admit-noise",
        "r1-admit-half",
    }
    assert len([row for row in doc["result"]["readings"] if row["run_id"] == "fuse-r1"]) == 2

    # bit-level: the same recipe builds to the same bytes, and says so
    r = runner.invoke(app, ["fuse", "build", "--dataset", "flow", "--recipe", "r1"])
    assert r.exit_code == 0 and "cached=2" in _verdict(r.output) and "built=0" in _verdict(r.output)


def test_score_fusers_on_multilabel(roots, tmp_path):
    ds, plan, paths = dataset_with_perfect_run(
        roots,
        tmp_path,
        name="ml",
        task="multilabel",
        samples=multilabel_samples(60, seed=1),
        categories=ML_CATS,
        run_id="a",
    )
    _ingest(roots, tmp_path, ds, plan, "b", lambda s: noisy_predictions(s, ds.card, seed=3))
    for rid, method in (("rm", "mean"), ("rr", "rank_mean")):
        r = runner.invoke(
            app,
            [
                "fuse",
                "recipe",
                "--dataset",
                "ml",
                "--id",
                rid,
                "--plan",
                "fixed-v1",
                "--method",
                method,
                "--member",
                "a",
                "--member",
                "b:0.5",
            ],
        )
        assert r.exit_code == 0, r.output
        r = runner.invoke(app, ["fuse", "build", "--dataset", "ml", "--recipe", rid])
        assert r.exit_code == 0, r.output
        assert "built=2" in _verdict(r.output)
        r = runner.invoke(
            app, ["eval", "measure", "--run", f"fuse-{rid}", "--metrics", "macro_auc", "--json"]
        )
        assert r.exit_code == 0, r.output
        readings = _json(r)["result"]["readings"]
        assert len(readings) == 2 and all(0.5 <= x["value"] <= 1.0 for x in readings)
    r = runner.invoke(
        app,
        [
            "fuse",
            "recipe",
            "--dataset",
            "ml",
            "--id",
            "rw",
            "--plan",
            "fixed-v1",
            "--method",
            "wbf",
            "--member",
            "a",
            "--member",
            "b",
        ],
    )
    assert r.exit_code == 1 and "payload=scores" in _verdict(r.output)


def test_plugin_fuser(roots, tmp_path, monkeypatch):
    ds, plan, paths = dataset_with_perfect_run(
        roots, tmp_path, name="pl", task="det", samples=det_samples(30, seed=2), run_id="a"
    )
    _ingest(roots, tmp_path, ds, plan, "b", lambda s: noisy_predictions(s, ds.card, seed=4))
    (tmp_path / "fuse_plug_e2e.py").write_text(
        "from vcp.fuse.fusers import register_fuser\n"
        "class First:\n"
        "    name = 'first'\n"
        "    version = '1'\n"
        "    payloads = frozenset({'boxes'})\n"
        "    defaults = {}\n"
        "    def check_params(self, params):\n"
        "        return None\n"
        "    def fuse(self, members, ctx):\n"
        "        ids = set(ctx.ids)\n"
        "        return [p for sid, p in members[0].predictions.items() if sid in ids]\n"
        "register_fuser(First())\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    r = runner.invoke(
        app,
        [
            "fuse",
            "recipe",
            "--dataset",
            "pl",
            "--id",
            "p1",
            "--plan",
            "fixed-v1",
            "--method",
            "first",
            "--member",
            "a",
            "--member",
            "b",
        ],
    )
    assert r.exit_code == 2 and "method=first" in _verdict(
        r.output
    )  # not registered without --plugin
    r = runner.invoke(
        app,
        [
            "fuse",
            "recipe",
            "--dataset",
            "pl",
            "--id",
            "p1",
            "--plan",
            "fixed-v1",
            "--method",
            "first",
            "--member",
            "a",
            "--member",
            "b",
            "--plugin",
            "fuse_plug_e2e",
        ],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app,
        [
            "fuse",
            "build",
            "--dataset",
            "pl",
            "--recipe",
            "p1",
            "--plugin",
            "fuse_plug_e2e",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    doc = _json(r)
    assert doc["result"]["subsets"]["valA"]["samples"] > 0
