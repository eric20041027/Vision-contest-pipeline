import json

import pytest
from typer.testing import CliRunner

from helpers import det_samples, make_card, noisy_predictions, perfect_predictions, write_images
from vcp.cli import app
from vcp.cli_eval import load_plugins
from vcp.core.errors import VcpError
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.ledger import ReadingsLedger
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


def ingest_perfect(roots, tmp_path, ds, plan, run_id, subset, *, drop=0, extra=(), unseal=False):
    paths = DatasetPaths.resolve(ds.card.name, data_root=roots.data, configs_root=roots.configs)
    sub = ds.subset(subset, plan, unseal=unseal, reason="fixture", paths=paths)
    preds = perfect_predictions(sub, ds.card)
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


def test_eval_ingest_plugin_registers_a_contest_converter(roots, tmp_path, monkeypatch):
    """F2: --plugin is the only route by which a projects/<contest>/ converter reaches
    vcp eval ingest, so the wiring is pinned at the CLI level: a throwaway module registers a
    converter under a unique name and the command must find it through the registry."""
    import sys

    from vcp.measure.converters import CONVERTERS

    ds, plan, paths = seed_det(roots)
    module = "vcp_test_plug_ingest"
    (tmp_path / f"{module}.py").write_text(
        "\n".join(
            [
                "from vcp.measure.converters import get_converter, register_converter",
                "",
                "",
                "class PlugJsonl:",
                "    name = 'plug_jsonl'",
                "    version = '1'",
                "",
                "    def convert(self, src, ctx):",
                "        return get_converter('jsonl').convert(src, ctx)",
                "",
                "",
                "register_converter(PlugJsonl())",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        preds = perfect_predictions(ds.subset("valA", plan), ds.card)
        src = tmp_path / "plug-valA.jsonl"
        write_predictions(src, preds)
        args = [
            "eval",
            "ingest",
            "--run",
            "plug",
            "--dataset",
            ds.card.name,
            "--plan",
            plan.plan_id,
            "--subset",
            "valA",
            "--format",
            "plug_jsonl",
            "--src",
            str(src),
            "--trained-on",
            "train",
            "--plugin",
            module,
        ]
        r = runner.invoke(app, args)
        assert r.exit_code == 0, r.output
        assert "VERDICT cmd=eval.ingest status=OK" in _last_verdict(r.output)
        card = (paths.data_root / "runs" / "plug" / "run.yaml").read_text(encoding="utf-8")
        assert "format_in: plug_jsonl" in card
    finally:
        CONVERTERS.pop("plug_jsonl", None)
        sys.modules.pop(module, None)


def test_eval_measure_and_anchor_cli(roots, tmp_path):
    ds, plan, paths = seed_det(roots)
    assert ingest_perfect(roots, tmp_path, ds, plan, "m1", "valA").exit_code == 0
    assert ingest_perfect(roots, tmp_path, ds, plan, "m1", "valB").exit_code == 0
    r = runner.invoke(app, ["eval", "measure", "--run", "m1"])
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "status=WARN" in v and "readings=2" in v and "guardrail=none" in v
    assert "valA" in r.output and "coco_map" in r.output
    anchor_args = ["eval", "anchor", "--run", "m1", "--subset", "valA", "--metric", "coco_map"]
    r = runner.invoke(app, anchor_args)
    assert r.exit_code == 0, r.output
    assert "key=fixed-v1/valA/coco_map/iou=50:95,max_dets=100" in _last_verdict(r.output).replace(
        '"', ""
    )
    r = runner.invoke(app, anchor_args)
    assert r.exit_code == 1 and "replace" in _last_verdict(r.output)
    r = runner.invoke(
        app, ["eval", "measure", "--run", "m1", "--params", "iou=50", "--subsets", "valA"]
    )
    # different params -> a different anchor key, so this cell has no anchor of its own
    assert r.exit_code == 0 and "guardrail=none" in _last_verdict(r.output)
    r = runner.invoke(app, ["eval", "measure", "--run", "m1", "--subsets", "valA", "--json"])
    assert r.exit_code == 0
    doc = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert doc["fields"]["cached"] == 1 and doc["result"]["readings"][0]["subset"] == "valA"
    r = runner.invoke(app, ["eval", "measure", "--run", "m1", "--subsets", "holdout"])
    assert r.exit_code == 1 and "no predictions" in _last_verdict(r.output)
    assert ingest_perfect(roots, tmp_path, ds, plan, "m1", "holdout", unseal=True).exit_code == 0
    r = runner.invoke(app, ["eval", "measure", "--run", "m1", "--subsets", "holdout"])
    assert r.exit_code == 2 and "SealedSubsetError" in _last_verdict(r.output)
    r = runner.invoke(app, ["eval", "measure", "--run", "m1", "--subsets", "holdout", "--unseal"])
    assert r.exit_code == 2 and "reason" in _last_verdict(r.output)
    unseal = ["--unseal", "--reason", "final read"]
    r = runner.invoke(app, ["eval", "measure", "--run", "m1", "--subsets", "holdout", *unseal])
    assert r.exit_code == 0 and "readings=1" in _last_verdict(r.output)
    assert '"caller": "vcp eval measure"' in paths.unseal_jsonl("fixed-v1").read_text(
        encoding="utf-8"
    )
    assert (paths.measure_dir / "anchors.json").is_file()


def test_eval_measure_and_anchor_failures_cli(roots, tmp_path):
    """Each way a user can get these two commands wrong maps to its own status and exit code."""
    ds, plan, _ = seed_det(roots)
    assert ingest_perfect(roots, tmp_path, ds, plan, "m1", "valA").exit_code == 0
    only_valA = ["--subsets", "valA"]
    cases = [
        (["eval", "measure", "--run", "ghost"], 1, "run not found"),
        (["eval", "measure", "--run", "m1"], 1, "no predictions"),  # valB never ingested
        (["eval", "measure", "--run", "m1", *only_valA, "--metrics", "nope"], 2, "RegistryError"),
        (
            ["eval", "measure", "--run", "m1", *only_valA, "--metrics", "accuracy"],
            1,
            "not applicable",
        ),
        (
            ["eval", "measure", "--run", "m1", *only_valA, "--params", "nms=1"],
            1,
            "no registered metric accepts params",
        ),
        (
            # Minor 7: parse_opts must name the option that was actually used, not a hardcoded
            # "--opt" -- measure/anchor expose it as --params.
            ["eval", "measure", "--run", "m1", *only_valA, "--params", "foo"],
            1,
            "--params expects key=value",
        ),
        (
            ["eval", "anchor", "--run", "m1", "--subset", "valA", "--metric", "nope"],
            2,
            "RegistryError",
        ),
        (
            ["eval", "anchor", "--run", "m1", "--subset", "valB", "--metric", "coco_map"],
            1,
            "no predictions",
        ),
        (
            ["eval", "anchor", "--run", "m1", "--subset", "valA", "--metric", "coco_map"],
            1,
            "run `vcp eval measure` first",
        ),
    ]
    for args, code, needle in cases:
        r = runner.invoke(app, args)
        assert r.exit_code == code, (args, r.output)
        assert needle in _last_verdict(r.output), (args, r.output)


def test_eval_anchor_rejects_bad_tolerance_cli(roots, tmp_path):
    """I1: --tolerance nan/inf silently disables the guardrail (any drift compares `<= tolerance`
    as vacuously true for inf, and false for nan, which never trips it) and a negative one jams
    it (nothing is ever within tolerance). 0.0 -- exact reproduction -- must stay legal."""
    ds, plan, _ = seed_det(roots)
    assert ingest_perfect(roots, tmp_path, ds, plan, "m1", "valA").exit_code == 0
    assert (
        runner.invoke(app, ["eval", "measure", "--run", "m1", "--subsets", "valA"]).exit_code == 0
    )
    base = ["eval", "anchor", "--run", "m1", "--subset", "valA", "--metric", "coco_map"]
    for bad in ("nan", "inf", "-1"):
        r = runner.invoke(app, [*base, "--tolerance", bad])
        assert r.exit_code == 1, (bad, r.output)
        v = _last_verdict(r.output)
        assert "ValidationFailed" in v and bad in v, (bad, r.output)
    r = runner.invoke(app, [*base, "--tolerance", "0"])
    assert r.exit_code == 0, r.output


def test_eval_measure_with_corrupt_anchors_json_fails_located(roots, tmp_path):
    """I2: anchors.json is rewritten whole on every `vcp eval anchor`, so a crash partway (or any
    other corruption) is a real failure mode -- it must be a located FAIL, never a bare pydantic
    error escaping as an ABORT (blanket G)."""
    ds, plan, paths = seed_det(roots)
    assert ingest_perfect(roots, tmp_path, ds, plan, "m1", "valA").exit_code == 0
    paths.measure_dir.mkdir(parents=True, exist_ok=True)
    (paths.measure_dir / "anchors.json").write_text(
        '{"fixed-v1/valA/coco_map/iou=50:95,max_dets=100": {"value": 1',
        encoding="utf-8",
        newline="\n",
    )
    r = runner.invoke(app, ["eval", "measure", "--run", "m1", "--subsets", "valA"])
    assert r.exit_code == 1, r.output
    v = _last_verdict(r.output)
    assert "status=FAIL" in v and "ValidationFailed" in v and "anchors.json" in v


def test_eval_measure_guardrail_abort_is_reported_and_writes_nothing_cli(roots, tmp_path):
    """Minor 1 / Adjudication 2: replacing the anchor run's own predictions with drifted ones
    must abort the guardrail with the anchor's reading_id named in the VERDICT reason, and the
    ledger must not grow by even the readings that were already computed before the abort."""
    ds, plan, paths = seed_det(roots)
    assert ingest_perfect(roots, tmp_path, ds, plan, "m1", "valA").exit_code == 0
    r = runner.invoke(app, ["eval", "measure", "--run", "m1", "--subsets", "valA"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app, ["eval", "anchor", "--run", "m1", "--subset", "valA", "--metric", "coco_map"]
    )
    assert r.exit_code == 0, r.output
    rows_before = ReadingsLedger(paths.measure_dir / "readings.jsonl").rows
    anchored = next(row for row in rows_before if row.subset == "valA")
    before = len(rows_before)

    sub = ds.subset("valA", plan)
    src = tmp_path / "m1-valA-drifted.jsonl"
    write_predictions(src, noisy_predictions(sub, ds.card))
    replace_args = [
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
        "--trained-on",
        "train",
        "--replace",
    ]
    assert runner.invoke(app, replace_args).exit_code == 0

    r = runner.invoke(app, ["eval", "measure", "--run", "m1", "--subsets", "valA"])
    assert r.exit_code == 2, r.output
    v = _last_verdict(r.output)
    assert "GuardrailError" in v and anchored.reading_id in v
    assert len(ReadingsLedger(paths.measure_dir / "readings.jsonl").rows) == before


SIGMA_BASE = ["eval", "sigma", "--dataset", "tiny", "--plan", "fixed-v1", "--metric", "coco_map"]
PRIOR_OK = ["--prior", "0.008", "--note", "history"]


def test_eval_sigma_cli(roots, tmp_path):
    _, _, paths = seed_det(roots)
    prior = [*SIGMA_BASE, "--method", "prior", *PRIOR_OK]
    r = runner.invoke(app, prior)
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "status=OK" in v and "method=prior" in v and "value=0.008" in v and "cached=false" in v
    r = runner.invoke(app, prior)
    assert r.exit_code == 0, r.output
    assert "cached=true" in _last_verdict(r.output)
    # append-only: the same estimate is one row, for ever
    sigma_jsonl = paths.measure_dir / "sigma.jsonl"
    assert len(sigma_jsonl.read_text(encoding="utf-8").splitlines()) == 1
    r = runner.invoke(app, [*SIGMA_BASE, "--method", "splithalf"])
    assert r.exit_code == 1 and "at least 3 runs" in _last_verdict(r.output)
    # a sigma_p of zero makes the judge's sigma_p bar vacuous: written, but not silently
    r = runner.invoke(app, [*SIGMA_BASE, "--method", "prior", "--prior", "0", "--note", "flat"])
    assert r.exit_code == 0, r.output
    assert "status=WARN" in _last_verdict(r.output) and "value=0.0" in _last_verdict(r.output)


def test_eval_sigma_failures_cli(roots, tmp_path):
    """Each way a user can get `eval sigma` wrong maps to its own status and exit code."""
    seed_det(roots)
    other = ["eval", "sigma", "--dataset", "tiny", "--plan", "fixed-v1"]
    cases = [
        ([*SIGMA_BASE, "--method", "prior", "--prior", "0.008"], 1, "prior needs --prior"),
        ([*SIGMA_BASE, "--method", "magic"], 1, "--method must be one of"),
        ([*SIGMA_BASE, "--method", "prior", *PRIOR_OK, "--resamples", "1"], 1, "resamples"),
        ([*SIGMA_BASE, "--method", "bootstrap"], 1, "bootstrap needs --run"),
        ([*SIGMA_BASE, "--method", "bootstrap", "--run", "ghost"], 1, "run not found"),
        ([*other, "--metric", "nope", "--method", "prior", *PRIOR_OK], 2, "RegistryError"),
        (
            ["eval", "sigma", "--dataset", "tiny", "--plan", "ghost", "--metric", "coco_map"]
            + ["--method", "prior", *PRIOR_OK],
            2,
            "plan not found",
        ),
    ]
    for args, code, needle in cases:
        r = runner.invoke(app, args)
        assert r.exit_code == code, (args, r.output)
        assert needle in _last_verdict(r.output), (args, r.output)


def test_load_plugins_returns_loaded_names_and_raises_on_bad_module():
    """Ruling Task5#1: load_plugins returns the loaded module names (final signature)."""
    assert load_plugins(None) == []
    assert load_plugins(["json"]) == ["json"]
    with pytest.raises(VcpError, match="cannot import plugin"):
        load_plugins(["definitely_not_a_real_module_xyz"])
