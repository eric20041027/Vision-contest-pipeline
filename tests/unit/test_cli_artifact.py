"""``vcp artifact`` through the CLI: VERDICT lines, exit codes, --json."""

import json
import sys

from typer.testing import CliRunner

from helpers import seed_train
from vcp.artifact.authorization import AuthorizeSpec, authorize
from vcp.cli import app
from vcp.code.scan import scan_root
from vcp.code.snapshot import snapshot_json_path, write_snapshot
from vcp.core.paths import artifact_dir
from vcp.train.run import RunSpec, train_run

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


def test_create_rejects_duplicate_names_and_bad_names_before_claiming_the_id(roots, tmp_path):
    a = tmp_path / "a" / "same.txt"
    a.parent.mkdir()
    a.write_text("a", encoding="utf-8")
    b = tmp_path / "b" / "same.txt"
    b.parent.mkdir()
    b.write_text("b", encoding="utf-8")
    r = _run("create", "--kind", "receipt", "--id", "dup", "--file", str(a), "--file", str(b))
    v = _verdict(r.output)
    assert r.exit_code == 1 and "exists: --file 'same.txt' given twice" in v
    assert not artifact_dir(roots.data, "receipt", "dup").exists()
    target = tmp_path / "target.txt"
    target.write_text("t", encoding="utf-8")
    r = _run("create", "--kind", "receipt", "--id", "badname", "--file", f"../x={target}")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "unsafe_path:" in v
    assert not artifact_dir(roots.data, "receipt", "badname").exists()


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


def test_cli_create_checks_a_snapshot_input_only_statically(roots, tmp_path):
    """spec 2026-10-31 §5.5: a snapshot another process took is checked for integrity only --
    this process (pytest, CliRunner) has modules everywhere, and that does not matter."""
    code = tmp_path / "code"
    code.mkdir()
    (code / "a.py").write_bytes(b"a = 1\n")
    outcome = write_snapshot(roots.data, scan_root(code, kind="dir"), command="test")
    snapshot = snapshot_json_path(roots.data, outcome.snapshot_id)
    r = _create(tmp_path, "r1", "--input", f"code_snapshot.cwd={snapshot}")
    assert r.exit_code == 0, r.output
    r = _create(tmp_path, "r2", "--input", f"code_snapshot.cwd={snapshot.parent / 'manifest.json'}")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "not_found: input 'code_snapshot.cwd'" in v
    assert "kind=receipt id=r2" in v and "input=code_snapshot.cwd" in v
    (snapshot.parent / "files" / "a.py").write_bytes(b"a = 2\n")
    r = _create(tmp_path, "r3", "--input", f"code_snapshot.cwd={snapshot}")
    v = _verdict(r.output)
    assert r.exit_code == 1 and 'reason="IntegrityError: mismatch: ' in v and "id=r3" in v
    assert not artifact_dir(roots.data, "receipt", "r2").exists()
    assert not artifact_dir(roots.data, "receipt", "r3").exists()


def test_artifact_failure_preserves_command_identity(roots):
    r = _run("verify", "--kind", "receipt", "--id", "nope")
    v = _verdict(r.output)
    assert r.exit_code == 1 and v.startswith("VERDICT cmd=artifact.verify status=FAIL reason=")
    assert "kind=receipt id=nope" in v
    r = _run("lineage", "--kind", "receipt", "--id", "nope")
    assert r.exit_code == 1 and "kind=receipt id=nope" in _verdict(r.output)


def test_cli_create_with_an_authorization_input(roots, tmp_path, bench):
    """VCP-006 through the CLI: the authorization is an ordinary ``--input``; a job that differs
    from what it binds is a FAIL naming the difference, and leaves no directory."""
    authorize(
        AuthorizeSpec(
            "a1",
            "artifact:benchmark/b-50",
            "artifact",
            "features",
            ["param:limit"],
            [],
            "50-sample benchmark passed",
        ),
        data_root=roots.data,
    )
    payload = tmp_path / "features.bin"
    payload.write_bytes(b"f")

    def create(artifact_id: str, batch: str):
        spec = bench.spec(artifact_id, batch=batch, authorization="a1")
        args = ["--kind", "features", "--id", artifact_id, "--dataset", "demo"]
        args += ["--file", str(payload)]
        for key, value in spec.params.items():
            args += ["--param", f"{key}={value}"]
        for ref in spec.inputs:
            args += ["--input", f"{ref.name}={ref.path}"]
        return _run("create", *args)

    r = create("f-1", "8")
    assert r.exit_code == 0, r.output
    assert "status=OK" in _verdict(r.output)
    r = create("f-2", "16")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "unauthorized:" in v and "differs=param:batch" in v
    assert "authorization=a1" in v and "kind=features id=f-2" in v
    assert not artifact_dir(roots.data, "features", "f-2").exists()


# -- artifact authorize ---------------------------------------------------------------------------


def _authorize_args(authorization_id="a1", *extra, basis="artifact:benchmark/b-50"):
    return ["authorize", "--id", authorization_id, "--basis", basis, *extra]


def test_artifact_authorize_cli(roots, bench):
    r = _run(
        *_authorize_args(
            "a1",
            "--for",
            "artifact",
            "--target-kind",
            "features",
            "--free",
            "param:limit",
            "--reason",
            "50-sample benchmark passed",
        )
    )
    v = _verdict(r.output)
    assert r.exit_code == 0, r.output
    assert v.startswith("VERDICT cmd=artifact.authorize status=OK")
    assert all(
        field in v
        for field in [
            "id=a1",
            "basis=artifact:benchmark/b-50",
            "for=artifact",
            "claims=6",
            "freed=1",
        ]
    )
    lines = r.output.splitlines()
    assert "claim dataset=demo" in lines and "claim param:batch=8" in lines
    assert f"claim input:model={bench.sha('model')[:12]}" in lines
    assert sum(line.startswith("claim ") for line in lines) == 6
    assert artifact_dir(roots.data, "authorization", "a1").is_dir()


def test_artifact_authorize_cli_json_and_set(roots, bench, tmp_path):
    model = tmp_path / "new-model.bin"
    model.write_bytes(b"m3")
    r = _run(
        *_authorize_args(
            "a2",
            "--for",
            "artifact",
            "--target-kind",
            "features",
            "--free",
            "param:limit",
            "--free",
            "input:ids",
            "--set",
            "param:batch=16",
            "--set",
            f"input:model=@{model}",
            "--reason",
            "retuned",
            "--json",
        )
    )
    assert r.exit_code == 0, r.output
    doc = _json(r)
    assert doc["status"] == "OK" and doc["cmd"] == "artifact.authorize"
    assert (doc["fields"]["claims"], doc["fields"]["freed"]) == (5, 2)
    result = doc["result"]
    assert result["authorization_id"] == "a2" and result["consumer"] == "artifact"
    assert result["freed"] == ["param:limit", "input:ids"]
    assert result["claims"]["param:batch"] == "16" and "input:ids" not in result["claims"]
    assert len(result["manifest_sha256"]) == 64
    assert "VERDICT cmd=artifact.authorize" in r.stderr and "claim " not in r.stdout


def test_artifact_authorize_cli_failures_keep_id_and_basis(roots, bench):
    reason = ("--reason", "because")
    r = _run(*_authorize_args("a3", "--for", "artifact", *reason))  # no --target-kind
    v = _verdict(r.output)
    assert r.exit_code == 1 and "invalid:" in v and "id=a3" in v
    assert "basis=artifact:benchmark/b-50" in v

    r = _run(*_authorize_args("a4", "--for", "train", *reason))  # param:* is not a train claim
    v = _verdict(r.output)
    assert r.exit_code == 1 and "unbindable:" in v and "param:batch" in v and "id=a4" in v

    r = _run(*_authorize_args("a5", "--for", "artifact", "--target-kind", "features"))
    v = _verdict(r.output)
    assert r.exit_code == 1 and "invalid:" in v and "--reason" in v and "id=a5" in v

    nope = "artifact:benchmark/nope"
    r = _run(*_authorize_args("a6", "--for", "artifact", "--target-kind", "f", *reason, basis=nope))
    v = _verdict(r.output)  # the basis's own kind/id must not replace id=a6
    assert r.exit_code == 1 and "not_found:" in v and "id=a6" in v
    assert "basis=artifact:benchmark/nope" in v and "kind=benchmark" not in v

    r = _run(
        *_authorize_args("a7", "--for", "artifact", "--target-kind", "f", *reason, basis="run:nope")
    )
    v = _verdict(r.output)
    assert r.exit_code == 1 and "not_found:" in v and "id=a7" in v and "basis=run:nope" in v
    assert not artifact_dir(roots.data, "authorization", "a7").exists()


def _create_with(bench, artifact_id, *, authorization):
    spec = bench.spec(artifact_id, batch="8", authorization=authorization)
    args = ["--kind", "features", "--id", artifact_id, "--dataset", "demo"]
    for key, value in spec.params.items():
        args += ["--param", f"{key}={value}"]
    for ref in spec.inputs:
        args += ["--input", f"{ref.name}={ref.path}"]
    payload = bench.data / "features.bin"
    payload.write_bytes(b"f")
    return _run("create", *args, "--file", str(payload))


def test_artifact_authorize_cli_exists_and_supersedes(roots, bench):
    base = ["--for", "artifact", "--target-kind", "features", "--reason", "ok"]
    assert _run(*_authorize_args("a1", *base)).exit_code == 0
    r = _run(*_authorize_args("a1", *base))
    v = _verdict(r.output)
    assert r.exit_code == 1 and "exists:" in v and "id=a1" in v
    r = _run(*_authorize_args("a2", *base, "--supersedes", "a1"))
    assert r.exit_code == 0, r.output
    assert "supersedes" not in _verdict(r.output)  # the VERDICT says what the spec lists
    r = _create_with(bench, "f-1", authorization="a1")
    assert r.exit_code == 1 and "superseded:" in _verdict(r.output)


def test_artifact_authorize_cli_needs_a_configs_root_only_for_a_run_basis(
    roots, bench, tmp_path, monkeypatch
):
    """An artifact basis reads nothing under the configs root, so a machine without one can
    authorize it; a run basis reads its plan file there, and says so when there is none."""
    monkeypatch.delenv("VCP_CONFIGS_ROOT")
    monkeypatch.chdir(tmp_path)  # no pyproject.toml beside a configs/ above it either
    base = ["--for", "artifact", "--target-kind", "features", "--reason", "b"]
    r = _run(*_authorize_args("a1", *base))
    assert r.exit_code == 0, r.output
    card = roots.data / "runs" / "r" / "run.yaml"  # a run basis gets as far as reading its claims
    card.parent.mkdir(parents=True)
    card.write_bytes(b"run_id: r\n")
    r = _run(*_authorize_args("a2", "--for", "train", "--reason", "b", basis="run:r"))
    v = _verdict(r.output)
    assert r.exit_code == 1 and "cannot locate configs root" in v
    assert "id=a2" in v and "basis=run:r" in v


def test_artifact_authorize_cli_from_a_training_run(roots, work):
    seed_train(roots)
    train_run(
        RunSpec(
            run_id="r",
            dataset="tiny",
            plan_id="fixed-v1",
            trained_on=["train"],
            cwd=work,
            command=[sys.executable, "fake_train.py", "0"],
            seed=1,
            checkpoints=["weights/*.pt"],
            final="weights/best.pt",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    r = _run(*_authorize_args("a-run", "--for", "train", "--reason", "benchmark", basis="run:r"))
    v = _verdict(r.output)
    assert r.exit_code == 0, r.output
    assert "id=a-run" in v and "basis=run:r" in v and "for=train" in v and "freed=0" in v
    assert "claims=12" in v and "claim seed=1" in r.output.splitlines()
    assert any(line.startswith("claim input:code_snapshot.cwd=") for line in r.output.splitlines())
    free = ["--free", "seed", "--free", "param:command_sha256"]
    r = _run(*_authorize_args("a-run2", "--for", "train", "--reason", "b", *free, basis="run:r"))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "claims=10" in v and "freed=2" in v
    r = _run(
        *_authorize_args(
            "a-run3", "--for", "artifact", "--target-kind", "k", "--reason", "b", basis="run:r"
        )
    )
    assert r.exit_code == 1 and "unbindable:" in _verdict(r.output)
