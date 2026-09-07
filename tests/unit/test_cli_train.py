import json
import sys

from typer.testing import CliRunner

from helpers import det_samples, make_card, write_images
from vcp.cli import app
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan

runner = CliRunner()

FAKE = """
import sys
from pathlib import Path
print("hello from fake")
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best")
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
"""


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def seed_det(roots, name="tiny", n=40):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def _run(work, *extra, code="0", run="r1"):
    return runner.invoke(
        app,
        [
            "train",
            "run",
            "--run",
            run,
            "--dataset",
            "tiny",
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
            *extra,
            "--",
            sys.executable,
            "fake_train.py",
            code,
        ],
    )


def test_train_run_cli(roots, tmp_path):
    seed_det(roots)
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_text(FAKE, encoding="utf-8")
    r = _run(work, "--seed", "3", "--framework", "fake", "--upload", str(tmp_path / "vault"))
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert v.startswith("VERDICT cmd=train.run status=WARN") and "run=r1" in v and "attempt=1" in v
    assert "exit_code=0" in v and "checkpoints=1" in v and "uploaded=1" in v and "verified=1" in v
    assert "seed=3" in v and "venv=inherited" in v and "final=none" not in v
    assert "hello from fake" in r.output
    r = _run(work, "--resume", "--seed", "3", "--framework", "fake", "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert doc["fields"]["attempt"] == 2 and doc["result"]["run_id"] == "r1"
    assert "hello from fake" in r.output  # still echoed (to stderr under --json; CliRunner merges)
    r = _run(work, "--seed", "3", "--framework", "fake", code="1", run="r2")
    assert r.exit_code == 1, r.output
    v = _verdict(r.output)
    assert "status=FAIL" in v and "exit_code=1" in v and "run=r2" in v and "checkpoints=1" in v


def test_train_run_cli_failures(roots, tmp_path):
    seed_det(roots)
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_text(FAKE, encoding="utf-8")
    r = runner.invoke(
        app,
        [
            "train",
            "run",
            "--run",
            "r1",
            "--dataset",
            "tiny",
            "--plan",
            "fixed-v1",
            "--trained-on",
            "train",
            "--cwd",
            str(work),
        ],
    )
    assert r.exit_code == 1 and "training command is required" in _verdict(r.output)
    r = runner.invoke(
        app,
        [
            "train",
            "run",
            "--run",
            "r1",
            "--dataset",
            "tiny",
            "--plan",
            "fixed-v1",
            "--trained-on",
            "nope",
            "--cwd",
            str(work),
            "--",
            sys.executable,
            "fake_train.py",
        ],
    )
    assert r.exit_code == 2
    r = runner.invoke(app, ["train", "--help"])
    assert r.exit_code == 0 and "run" in r.output


def test_train_upload_and_status_cli(roots, tmp_path):
    seed_det(roots)
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_text(FAKE, encoding="utf-8")
    assert _run(work, "--seed", "1").exit_code == 0
    r = runner.invoke(app, ["train", "status", "--run", "r1"])
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert (
        "status=WARN" in v
        and "checkpoints=1" in v
        and "unbacked=1" in v
        and "backed=0" in v
        and "running=0" in v
    )
    # 5-2: the LAST attempt's command (a --resume may have changed it), not the record's
    assert f"attempt 1 command: {sys.executable} fake_train.py 0" in r.output
    r = runner.invoke(app, ["train", "upload", "--run", "r1", "--dest", str(tmp_path / "vault")])
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "status=OK" in v and "uploaded=1" in v and "verified=1" in v and "skipped=0" in v
    r = runner.invoke(
        app, ["train", "upload", "--run", "r1", "--dest", str(tmp_path / "vault"), "--json"]
    )
    assert r.exit_code == 0 and "skipped=1" in _verdict(r.output)
    doc = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert len(doc["result"]["uploads"]) == 1
    r = runner.invoke(app, ["train", "status", "--run", "r1", "--verify"])
    assert (
        r.exit_code == 0
        and "status=OK" in _verdict(r.output)
        and "unbacked=0" in _verdict(r.output)
    )
    (work / "weights" / "best.pt").write_bytes(b"tampered")
    r = runner.invoke(app, ["train", "status", "--run", "r1", "--verify"])
    assert (
        r.exit_code == 0 and "status=WARN" in _verdict(r.output) and "drift=1" in _verdict(r.output)
    )
    r = runner.invoke(app, ["train", "upload", "--run", "r1", "--dest", str(tmp_path / "v2")])
    assert r.exit_code == 1 and "changed since" in _verdict(r.output)
    r = runner.invoke(app, ["train", "status", "--run", "ghost"])
    assert r.exit_code == 1 and "run=ghost" in _verdict(r.output)
