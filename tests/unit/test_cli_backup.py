"""``vcp backup`` through the CLI: VERDICT lines, exit codes, --json. Later tasks append."""

import json

import pytest
from typer.testing import CliRunner

from backup_fixtures import make_world
from vcp.backup import dest as destmod
from vcp.cli import app
from vcp.measure.runs import load_run, run_dir

runner = CliRunner()


@pytest.fixture
def world(roots, tmp_path):
    return make_world(roots, tmp_path)


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _json(r) -> dict:
    return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))


def _run(*args):
    return runner.invoke(app, ["backup", *args])


def test_manifest_cli(world):
    r = _run(
        "manifest",
        "--dataset",
        "beach-test",
        "--conclusion",
        "submission:S1",
        "--id",
        "m-s1",
        "--json",
    )
    assert r.exit_code == 0, r.output
    doc = _json(r)
    assert doc["status"] == "OK" and doc["fields"]["manifest"] == "m-s1"
    assert doc["fields"]["remote_copies"] == 1 and doc["fields"]["missing"] == 0
    assert doc["result"]["files"] > 10 and doc["result"]["path"].endswith("m-s1.json")
    r = _run("manifest", "--dataset", "beach-test", "--conclusion", "submission:S1", "--id", "m-s1")
    assert r.exit_code == 1 and "exists" in _verdict(r.output)
    r = _run("manifest", "--dataset", "beach-test", "--conclusion", "photo:1")
    assert r.exit_code == 1 and "bad conclusion" in _verdict(r.output)
    (world.weights / "last.pt").unlink()
    r = _run("manifest", "--dataset", "beach", "--conclusion", "run:good", "--id", "m-good")
    assert (
        r.exit_code == 0
        and "status=WARN" in _verdict(r.output)
        and "missing=1" in _verdict(r.output)
    )


def test_push_cli(world, monkeypatch):
    r = _run("manifest", "--dataset", "beach-test", "--conclusion", "submission:S1", "--id", "m1")
    assert r.exit_code == 0, r.output
    vault = world.tmp / "vault"
    common = ["push", "--dataset", "beach-test", "--manifest", "m1", "--dest", str(vault)]
    r = _run(*common, "--tier", "2", "--json")
    assert r.exit_code == 0, r.output
    doc = _json(r)
    assert doc["status"] == "OK" and doc["fields"]["failed"] == 0 and doc["fields"]["pushed"] > 0
    assert doc["fields"]["tier"] == 2 and "forgotten" not in doc["fields"]
    assert doc["result"]["failed"] == [] and doc["result"]["forgotten"] is None
    r = _run(*common, "--tier", "2")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "pushed=0" in v and f"skipped={doc['fields']['pushed']}" in v
    r = _run(*common, "--tier", "9")
    assert r.exit_code == 1 and "tier" in _verdict(r.output)
    r = _run(*common, "--forget-remote")
    assert r.exit_code == 1 and "forget_refused" in _verdict(r.output)
    monkeypatch.setattr(destmod.shutil, "which", lambda name, *a, **k: None)
    r = _run("push", "--dataset", "beach-test", "--manifest", "m1", "--dest", "gdrive:x")
    assert r.exit_code == 2 and "rclone_not_found" in _verdict(r.output)


def test_verify_cli(world):
    r = _run("manifest", "--dataset", "beach-test", "--conclusion", "submission:S1", "--id", "m1")
    assert r.exit_code == 0, r.output
    vault = world.tmp / "vault"
    r = _run(
        "push", "--dataset", "beach-test", "--manifest", "m1", "--dest", str(vault), "--tier", "3"
    )
    assert r.exit_code == 0, r.output
    common = ["verify", "--dataset", "beach-test", "--manifest", "m1"]
    r = _run(*common, "--dest", str(vault))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v
    assert "missing=0" in v and "mismatch=0" in v and "drift=0" in v and "bad_stamps=0" in v
    r = _run(*common)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "ok=" not in v and "drift=0" in v
    (vault / "data" / "runs" / "good" / "run.yaml").unlink()
    r = _run(*common, "--dest", str(vault), "--json")
    assert r.exit_code == 1
    doc = _json(r)
    assert doc["status"] == "FAIL" and doc["fields"]["reason"] == "missing"
    assert doc["fields"]["missing"] == 1 and doc["result"]["copy_problems"] == [
        "missing:data/runs/good/run.yaml"
    ]
    assert doc["result"]["drift"] == [] and doc["result"]["bad_stamps"] == []
    r = _run("verify", "--dataset", "beach-test", "--manifest", "nope")
    assert r.exit_code == 1 and "not_found" in _verdict(r.output)


def test_pull_and_status_cli(world, monkeypatch):
    monkeypatch.setattr(destmod.shutil, "which", lambda name, *a, **k: None)  # no rclone here
    r = _run("status", "--dataset", "beach-test")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "manifests=0" in v
    assert "rclone_conf=unknown" in v
    r = _run("manifest", "--dataset", "beach-test", "--conclusion", "submission:S1", "--id", "m1")
    assert r.exit_code == 0, r.output
    vault = world.tmp / "vault"
    r = _run(
        "push", "--dataset", "beach-test", "--manifest", "m1", "--dest", str(vault), "--tier", "2"
    )
    assert r.exit_code == 0, r.output
    r = _run("status", "--dataset", "beach-test", "--json")
    doc = _json(r)
    assert doc["status"] == "WARN" and doc["fields"]["unverified"] == 1
    assert doc["result"]["manifests"][0]["unpushed_tiers"] == [3]
    assert doc["result"]["manifests"][0]["last_push"]["tier"] == 2
    r = _run(
        "verify", "--dataset", "beach-test", "--manifest", "m1", "--dest", str(vault), "--tier", "2"
    )
    assert r.exit_code == 0, r.output
    r = _run("status", "--dataset", "beach-test")
    v = _verdict(r.output)  # tier 2 verified: the weights' copies were never checked
    assert r.exit_code == 0 and "status=WARN" in v and "unverified=1" in v and "m1" in r.output
    assert "local_ok=True" in r.output
    card = load_run(world.roots.data, "good")
    pred = run_dir(world.roots.data, "good") / card.predictions["valB"].path
    pred.unlink()
    common = ["pull", "--dataset", "beach-test", "--manifest", "m1", "--dest", str(vault)]
    r = _run(*common, "--tier", "2")
    assert r.exit_code == 0 and "pulled=1" in _verdict(r.output) and pred.is_file()
    pred.write_bytes(b"edited\n")
    r = _run(*common, "--tier", "2")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "conflict" in v and "conflicts=1" in v
    r = _run(*common, "--tier", "2", "--overwrite", "--json")
    assert r.exit_code == 0
    doc = _json(r)
    assert doc["fields"]["pulled"] == 1 and doc["result"]["conflicts"] == []
    (world.weights / "last.pt").unlink()  # gone locally, and tier 3 was never pushed
    r = _run(*common, "--tier", "3")
    assert (
        r.exit_code == 1 and "missing" in _verdict(r.output) and "missing=1" in _verdict(r.output)
    )
