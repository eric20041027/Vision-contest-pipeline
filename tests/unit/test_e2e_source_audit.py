"""VCP-002 through the CLI: `validate` creates the source audit once (reused after); a
training loop under `vcp train run` reads through it (no `source_audit=missing`); `measure`
reports identity=source_audit; remove the audit -> measure warns; `validate` recreates it."""

import shutil
import sys

from typer.testing import CliRunner

from helpers import det_samples, make_card, perfect_predictions, write_images
from vcp.cli import app
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.data.source_audit import KIND as AUDIT_KIND
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.predictions import write_predictions
from vcp.measure.runs import load_run

runner = CliRunner()

ACCESS_FAKE = """
from pathlib import Path
from vcp.train import MaterializedReader

with MaterializedReader("flow", "npy", plan_id="fixed-v1", subset="train") as reader:
    n = sum(1 for _ in reader)
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best-%d" % n)
"""


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _run(*args):
    return runner.invoke(app, list(args))


def _field(verdict: str, name: str) -> str:
    return verdict.split(f" {name}=", 1)[1].split()[0]


def test_source_audit_flow(roots, tmp_path):
    paths = DatasetPaths.resolve("flow", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(60, seed=5)
    write_images(roots.data / "raw" / "flow", samples)
    ds = Dataset.from_parts(make_card("det", name="flow", image_root="raw/flow"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=3)
    save_plan(plan, paths)
    assert (
        materialize(
            MaterializeSpec(
                name="flow", mode="npy", data_root=roots.data, configs_root=roots.configs
            )
        ).failed
        == 0
    )
    # 1. validate creates the audit; a second validate reuses it
    r = _run("data", "validate", "--name", "flow")
    v = _verdict(r.output)
    assert r.exit_code == 0 and _field(v, "source_audit_state") == "created"
    aid = _field(v, "source_audit")
    assert aid.startswith("src-flow-")
    r = _run("data", "validate", "--name", "flow")
    assert _field(_verdict(r.output), "source_audit_state") == "reused"
    # 2. a training loop reads through the audit: no warning, the ref says so
    work = tmp_path / "work"
    work.mkdir()
    (work / "access.py").write_text(ACCESS_FAKE, encoding="utf-8")
    r = _run(
        "train",
        "run",
        "--run",
        "good",
        "--dataset",
        "flow",
        "--plan",
        "fixed-v1",
        "--trained-on",
        "train",
        "--seed",
        "1",
        "--cwd",
        str(work),
        "--checkpoints",
        "weights/*.pt",
        "--final",
        "weights/best.pt",
        "--",
        sys.executable,
        "access.py",
    )
    v = _verdict(r.output)
    assert r.exit_code == 0, r.output
    assert "receipts=1" in v and "source_audit=missing" not in v
    ref = load_run(roots.data, "good").access[0]
    assert ref.identity == "source_audit" and ref.source_audit == aid
    # 3. measure through the audit
    for subset in ("valA", "valB"):
        src = tmp_path / f"good-{subset}.jsonl"
        write_predictions(src, perfect_predictions(ds.subset(subset, plan), ds.card))
        r = _run(
            "eval",
            "ingest",
            "--run",
            "good",
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
        )
        assert r.exit_code == 0, r.output
    r = _run("eval", "measure", "--run", "good")
    v = _verdict(r.output)
    assert r.exit_code == 0 and _field(v, "identity") == "source_audit"
    assert "source_audit=missing" not in v
    # 4. without the audit the same measure falls back and warns
    shutil.rmtree(roots.data / "artifacts" / AUDIT_KIND)
    r = _run("eval", "measure", "--run", "good")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v
    # `measure_cmd` (unlike `export_cmd`) folds `res.warnings` into `human` only, not into the
    # VERDICT `fields`, so "source_audit=missing" prints as its own line above the VERDICT
    # rather than inside it; check the full captured output for the warning instead.
    assert _field(v, "identity") == "full_hash" and "source_audit=missing" in r.output
    # 5. validate brings it back under the same id, and the artifact layer can verify it
    r = _run("data", "validate", "--name", "flow")
    v = _verdict(r.output)
    assert _field(v, "source_audit_state") == "created" and _field(v, "source_audit") == aid
    r = _run("artifact", "verify", "--kind", AUDIT_KIND, "--id", aid)
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output)
