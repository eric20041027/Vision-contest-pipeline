"""``vcp backup`` through the CLI: VERDICT lines, exit codes, --json. Later tasks append."""

import json

import pytest
from typer.testing import CliRunner

from backup_fixtures import make_world
from vcp.cli import app

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
