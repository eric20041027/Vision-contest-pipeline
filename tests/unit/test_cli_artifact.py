"""``vcp artifact`` through the CLI: VERDICT lines, exit codes, --json."""

import json

from typer.testing import CliRunner

from vcp.cli import app
from vcp.core.paths import artifact_dir

runner = CliRunner()


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _json(r) -> dict:
    return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))


def _run(*args):
    return runner.invoke(app, ["artifact", *args])


def _create(tmp_path, artifact_id="r1", *extra):
    src = tmp_path / f"{artifact_id}.txt"
    src.write_text(artifact_id, encoding="utf-8")
    return _run("create", "--kind", "receipt", "--id", artifact_id, "--file", str(src), *extra)


def test_create_show_and_the_second_write(roots, tmp_path):
    plan = roots.data / "plan.json"
    plan.write_bytes(b"plan")
    r = _create(
        tmp_path,
        "r1",
        "--seed",
        "42",
        "--param",
        "fold=1",
        "--input",
        f"plan={plan}",
        "--id-pattern",
        r"r(?P<fold>\d)",
        "--dataset",
        "knee",
        "--json",
    )
    assert r.exit_code == 0, r.output
    doc = _json(r)
    assert doc["status"] == "OK" and doc["fields"]["kind"] == "receipt"
    assert (
        doc["fields"]["id"] == "r1" and doc["fields"]["files"] == 1 and doc["fields"]["bytes"] == 2
    )
    assert doc["result"]["spec"]["params"] == {"fold": "1"} and doc["result"]["spec"]["seed"] == 42
    assert doc["result"]["spec"]["inputs"][0]["path"] == "plan.json"
    assert len(doc["result"]["spec"]["inputs"][0]["sha256"]) == 64
    assert (artifact_dir(roots.data, "receipt", "r1") / "manifest.json").is_file()
    r = _create(tmp_path, "r1")
    v = _verdict(r.output)
    assert (
        r.exit_code == 1 and 'reason="ValidationFailed: exists: ' in v and "kind=receipt id=r1" in v
    )
    r = _run("show", "--kind", "receipt", "--id", "r1")
    assert (
        r.exit_code == 0 and "files=1" in _verdict(r.output) and "file r1.txt: 2 bytes" in r.output
    )
    assert "input plan:" in r.output and "dataset=knee" in r.output
    r = _run("show", "--kind", "receipt", "--id", "nope")
    assert r.exit_code == 1 and "not_found: artifact receipt/nope" in _verdict(r.output)
    r = _run("create", "--kind", "receipt", "--id", "r2", "--file", str(tmp_path / "missing.txt"))
    assert r.exit_code == 1 and "not_found:" in _verdict(r.output)
    assert not artifact_dir(roots.data, "receipt", "r2").exists()
    r = _run("create", "--kind", "receipt", "--id", "r3")
    assert r.exit_code == 1 and "at least one --file" in _verdict(r.output)
    r = _create(tmp_path, "r4", "--seed", "43", "--id-pattern", r"r(?P<seed>\d)")
    assert r.exit_code == 1 and "reads '4' from the id but the spec says '43'" in r.output
    assert not artifact_dir(roots.data, "receipt", "r4").exists()
    r = _run(
        "create",
        "--kind",
        "receipt",
        "--id",
        "r5",
        "--file",
        f"renamed.txt={tmp_path / 'r1.txt'}",
        "--input",
        "noequals",
    )
    assert r.exit_code == 1 and "--input expects name=PATH" in _verdict(r.output)
    r = _run(
        "create",
        "--kind",
        "receipt",
        "--id",
        "r5",
        "--file",
        f"renamed.txt={tmp_path / 'r1.txt'}",
        "--json",
    )
    assert r.exit_code == 0 and [f["name"] for f in _json(r)["result"]["files"]] == ["renamed.txt"]


def test_verify_relink_and_lineage(roots, tmp_path):
    assert _create(tmp_path, "r1").exit_code == 0
    r = _create(tmp_path, "r2", "--supersedes", "r1", "--reason", "seed fix")
    assert r.exit_code == 0 and "supersedes=r1" in _verdict(r.output)
    assert _create(tmp_path, "r3", "--supersedes", "r1", "--reason", "fork").exit_code == 0
    r = _run(
        "create",
        "--kind",
        "receipt",
        "--id",
        "r4",
        "--file",
        str(tmp_path / "r1.txt"),
        "--supersedes",
        "r1",
    )
    assert r.exit_code == 1 and "go together" in r.output
    r = _run("verify", "--kind", "receipt", "--id", "r2", "--json")
    assert r.exit_code == 0
    assert _json(r)["fields"] == {
        "kind": "receipt",
        "id": "r2",
        "mismatch": 0,
        "missing": 0,
        "extra": 0,
        "unlinked": 0,
    }
    d = artifact_dir(roots.data, "receipt", "r2")
    (d / "extra.bin").write_bytes(b"x")
    r = _run("verify", "--kind", "receipt", "--id", "r2")
    v = _verdict(r.output)
    assert (
        r.exit_code == 1
        and "status=FAIL" in v
        and 'reason="extra: extra.bin"' in v
        and "extra=1" in v
    )
    (d / "extra.bin").unlink()
    (d / "r2.txt").write_text("tampered", encoding="utf-8")
    r = _run("verify", "--kind", "receipt", "--id", "r2")
    assert r.exit_code == 1 and 'reason="mismatch: r2.txt"' in _verdict(r.output)
    (d / "r2.txt").write_text("r2", encoding="utf-8")
    log = roots.data / "artifacts" / "receipt" / "supersession.jsonl"
    log.write_text("", encoding="utf-8", newline="\n")
    r = _run("verify", "--kind", "receipt", "--id", "r2")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "unlinked=1" in v and "relink" in r.output
    r = _run("relink", "--kind", "receipt", "--id", "r2")
    assert r.exit_code == 0 and "appended=1" in _verdict(r.output)
    r = _run("relink", "--kind", "receipt", "--id", "r2")
    assert r.exit_code == 0 and "appended=0" in _verdict(r.output)
    r = _run("verify", "--kind", "receipt", "--id", "r2")
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output)
    r = _run("show", "--kind", "receipt", "--id", "r1")
    assert "superseded_by=r2,r3" in _verdict(r.output)
    r = _run("lineage", "--kind", "receipt", "--id", "r1", "--json")
    assert r.exit_code == 0
    doc = _json(r)
    assert (
        doc["status"] == "WARN"
        and doc["fields"]["forks"] == 1
        and doc["fields"]["heads"] == "r2,r3"
    )
    assert doc["fields"]["root"] == "r1" and doc["fields"]["depth"] == 1
    assert [m["spec"]["id"] for m in doc["result"]["successors"]] == ["r2", "r3"]
    r = _run("lineage", "--kind", "receipt", "--id", "r2")
    assert r.exit_code == 0 and "depth=2" in _verdict(r.output) and "heads=r2" in _verdict(r.output)
    assert "seed fix" in r.output


def test_status_and_clean(roots, tmp_path):
    r = _run("status")
    assert r.exit_code == 0 and "kinds=0" in _verdict(r.output) and "no artifacts" in r.output
    assert _create(tmp_path, "r1").exit_code == 0
    half = artifact_dir(roots.data, "receipt", "half")
    half.mkdir()
    (half / "spec.json").write_text(
        json.dumps(
            {
                "spec": {"kind": "receipt", "id": "half"},
                "opened_at": "2026-09-01T00:00:00.000Z",
                "vcp_version": "t",
            }
        ),
        encoding="utf-8",
    )
    tmp = artifact_dir(roots.data, "receipt", "r1") / ".r1.txt.0a1b2c3d.tmp"
    tmp.write_bytes(b"t")
    r = _run("status", "--json")
    assert r.exit_code == 0
    doc = _json(r)
    assert doc["status"] == "WARN"
    assert doc["fields"] == {
        "kinds": 1,
        "complete": 1,
        "partial": 1,
        "unlinked": 0,
        "forks": 0,
        "foreign": 0,
    }
    assert doc["result"]["kinds"][0]["partial"][0]["id"] == "half"
    r = _run("status")
    assert "partial receipt/half" in r.output and "opened_at=2026-09-01T00:00:00.000Z" in r.output
    r = _run("clean", "--kind", "receipt")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "candidates=1 removed=0" in v
    assert "candidate receipt/half" in r.output and "--apply" in r.output and half.is_dir()
    r = _run("clean", "--older-than", "0", "--apply", "--json")
    doc = _json(r)
    assert r.exit_code == 0 and doc["status"] == "OK" and doc["fields"]["removed"] == 2
    assert doc["result"]["removed"] == ["receipt/half", "receipt/r1/.r1.txt.0a1b2c3d.tmp"]
    assert not half.exists() and not tmp.exists()
    assert (artifact_dir(roots.data, "receipt", "r1") / "manifest.json").is_file()
    r = _run("clean", "--older-than", "1w")
    assert r.exit_code == 1 and "--older-than" in _verdict(r.output)
    r = _run("status", "--kind", "nothing")
    assert r.exit_code == 0 and "kinds=0" in _verdict(r.output)
    r = _run("clean")
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output) and "nothing to clean" in r.output


def test_artifact_failure_preserves_command_identity(roots):
    r = _run("verify", "--kind", "receipt", "--id", "nope")
    v = _verdict(r.output)
    assert r.exit_code == 1 and v.startswith("VERDICT cmd=artifact.verify status=FAIL reason=")
    assert "kind=receipt id=nope" in v
    r = _run("lineage", "--kind", "receipt", "--id", "nope")
    assert r.exit_code == 1 and "kind=receipt id=nope" in _verdict(r.output)
