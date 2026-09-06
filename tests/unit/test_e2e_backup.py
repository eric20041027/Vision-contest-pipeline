# ruff: noqa: E501
"""The whole backup layer through the CLI (spec 11): the manual-platform story to `final`, a
manifest of the final submission, push / verify / pull against a local vault, a timestamp
tamper, then the same manifest against a fake rclone (a real subprocess that leaks a secret on
every call) with --forget-remote and a privacy scan."""

import json
import sys

import pytest
from typer.testing import CliRunner

from backup_fixtures import SECRET, make_world
from vcp.backup import dest as destmod
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest
from vcp.cli import app
from vcp.core.paths import DatasetPaths
from vcp.core.time import utc_now
from vcp.measure.report import READINGS_LEDGER
from vcp.measure.runs import load_run, run_dir

runner = CliRunner()

FAKE_RCLONE = """
import hashlib, os, re, shutil, sys
from pathlib import Path
store = Path(os.environ["FAKE_RCLONE_STORE"])
args = sys.argv[1:]
print("RCLONE_CONFIG_PASS=" + os.environ.get("RCLONE_CONFIG_PASS", ""), file=sys.stderr)


def is_remote(s):
    return re.match(r"^[A-Za-z0-9_-]+:", s) and not re.match(r"^[A-Za-z]:[\\\\/]", s)


def local(spec):
    remote, _, path = spec.partition(":")
    return store / remote / path


if args[:2] == ["hashsum", "sha256"]:
    base = local(args[2])
    if not base.is_dir():
        print("directory not found", file=sys.stderr)
        sys.exit(3)
    for p in sorted(base.rglob("*")):
        if p.is_file():
            corrupt = p.name == os.environ.get("FAKE_RCLONE_CORRUPT")
            digest = "0" * 64 if corrupt else hashlib.sha256(p.read_bytes()).hexdigest()
            print(f"{digest}  {p.relative_to(base).as_posix()}")
elif args[0] == "copyto":
    src = local(args[1]) if is_remote(args[1]) else Path(args[1])
    dst = local(args[2]) if is_remote(args[2]) else Path(args[2])
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
elif args[:2] == ["config", "delete"]:
    (store / f"deleted-{args[2]}").write_text("", encoding="utf-8")
elif args[:2] == ["config", "file"]:
    print("Configuration file is stored at:")
    print(os.environ["FAKE_RCLONE_CONF"])
else:
    sys.exit(2)
"""


@pytest.fixture
def world(roots, tmp_path):
    return make_world(roots, tmp_path)


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _backup(*args):
    return runner.invoke(app, ["backup", *args])


def _paths(world, name):
    return DatasetPaths.resolve(name, data_root=world.roots.data, configs_root=world.roots.configs)


def _to_final(world) -> None:
    """record S1, unseal + measure holdout, final: the submission story's tail (Plan 6)."""
    at = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    r = runner.invoke(
        app,
        ["submit", "record", "--dataset", "beach-test", "--id", "S1", "--tz", "utc", "--at", at],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app,
        [
            "eval",
            "measure",
            "--run",
            "good",
            "--metrics",
            "accuracy",
            "--subsets",
            "holdout",
            "--unseal",
            "--reason",
            "final pick",
        ],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["submit", "final", "--dataset", "beach-test"])
    assert r.exit_code == 0 and "chosen=S1" in _verdict(r.output), r.output


def _scan(world, outputs: list[str]) -> None:
    scanned = 0
    for root in (world.roots.data, world.roots.configs):
        for p in root.rglob("*"):
            if p.is_file():
                scanned += 1
                assert SECRET.encode() not in p.read_bytes(), p
    assert scanned > 0, "privacy scan found no files to inspect"
    for text in outputs:
        assert SECRET not in text


def test_local_vault_story(world):
    _to_final(world)
    r = _backup(
        "manifest", "--dataset", "beach-test", "--conclusion", "submission:S1", "--id", "m1"
    )
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and "remote_copies=1" in v and "missing=0" in v
    manifest = load_manifest(_paths(world, "beach-test"), "m1")
    files = {f.role: f for f in manifest.files}
    assert {
        "submit_profile",
        "submissions_log",
        "stage",
        "artifact",
        "run_card",
        "prediction",
        "judgements",
        "prereg",
        "train_record",
        "checkpoint_final",
        "unseal_log",
    } <= set(files)
    assert sum(f.bytes for f in manifest.files if f.tier == 1) < 16 << 20
    # the `all` manifest lists logs/vcp-<date>.jsonl, which this very command grew after hashing
    # it: push sends the snapshot the manifest describes, so the evacuation recipe still works.
    r = _backup("manifest", "--dataset", "beach", "--conclusion", "all", "--id", "all1")
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output), r.output
    vault_all = world.tmp / "vault-all"
    every = ["--dataset", "beach", "--manifest", "all1", "--dest", str(vault_all), "--tier", "2"]
    r = _backup("push", *every)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "failed=0" in v and "pushed=0" not in v, r.output
    r = _backup("verify", *every)
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output), r.output
    vault = world.tmp / "vault"
    common = ["--dataset", "beach-test", "--manifest", "m1", "--dest", str(vault)]
    r = _backup("push", *common, "--tier", "1")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "failed=0" in v and "skipped=0" in v
    assert not (vault / "data" / "work").exists() and not list(vault.rglob("*.pt"))
    assert not (vault / "data" / "runs" / "good" / "predictions").exists()
    r = _backup("push", *common, "--tier", "2")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "failed=0" in v
    tier2 = sum(1 for f in manifest.files if f.tier == 2 and f.kind == "file")
    assert f"pushed={tier2}" in v
    r = _backup("push", *common, "--tier", "2")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "pushed=0" in v and "failed=0" in v
    ledger = BackupLedger(_paths(world, "beach-test").backup_log)
    assert [row.event for row in ledger.rows] == ["manifest", "push", "push", "push"]
    r = _backup("verify", *common, "--tier", "2")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and "missing=0" in v and "mismatch=0" in v
    assert "tier=2" in v
    assert "drift=0" in v and "bad_stamps=0" in v
    card = load_run(world.roots.data, "good")
    pred = run_dir(world.roots.data, "good") / card.predictions["valB"].path
    original = pred.read_bytes()
    pred.unlink()
    r = _backup("pull", *common, "--tier", "2")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "pulled=1" in v and pred.read_bytes() == original
    (vault / "data" / "runs" / "good" / "predictions" / "valA.jsonl").unlink()
    r = _backup("verify", *common, "--tier", "2")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "reason=missing" in v and "missing=1" in v
    readings = _paths(world, "beach").measure_dir / READINGS_LEDGER
    lines = readings.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    lines[-1] = json.dumps({**last, "ts": "2000-01-01T00:00:00.000Z"})
    readings.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    r = _backup("verify", "--dataset", "beach-test", "--manifest", "m1")
    v = _verdict(r.output)
    assert (
        r.exit_code == 1
        and "bad_stamps=1" in v
        and f"first_bad=measure/beach/{READINGS_LEDGER}:{len(lines)}" in v
    )
    assert "drift=1" in v  # the edited row sits inside the bytes the manifest hashed
    r = _backup("status", "--dataset", "beach-test")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "manifests=1" in v and "unverified=0" in v  # one verify did pass


def test_fake_rclone_story(world, tmp_path, monkeypatch):
    script = tmp_path / "fake_rclone.py"
    script.write_text(FAKE_RCLONE, encoding="utf-8")
    store = tmp_path / "remote-store"
    conf = tmp_path / "rclone.conf"
    conf.write_text("[fake]\ntype = local\n", encoding="utf-8")
    monkeypatch.setattr(destmod, "RCLONE", [sys.executable, str(script)])
    monkeypatch.setenv("FAKE_RCLONE_STORE", str(store))
    monkeypatch.setenv("FAKE_RCLONE_CONF", str(conf))
    monkeypatch.setenv("RCLONE_CONFIG_PASS", SECRET)
    outputs: list[str] = []
    r = _backup(
        "manifest", "--dataset", "beach-test", "--conclusion", "submission:S1", "--id", "r1"
    )
    assert r.exit_code == 0, r.output
    r = _backup("status", "--dataset", "beach-test")
    outputs.append(r.output)
    v = _verdict(r.output)
    assert (
        r.exit_code == 0
        and "status=WARN" in v
        and "rclone_conf=present" in v
        and "unverified=1" in v
    )
    common = ["--dataset", "beach-test", "--manifest", "r1", "--dest", "fake:vault"]
    monkeypatch.setenv("FAKE_RCLONE_CORRUPT", "submit.yaml")
    r = _backup("push", *common, "--tier", "1", "--forget-remote")
    outputs.append(r.output)
    v = _verdict(r.output)
    assert r.exit_code == 1 and "mismatch" in v and "failed=1" in v
    assert not (store / "deleted-fake").exists()
    monkeypatch.delenv("FAKE_RCLONE_CORRUPT")
    r = _backup("push", *common, "--tier", "1", "--forget-remote")
    outputs.append(r.output)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "failed=0" in v and "forgotten=fake" in v
    assert (store / "deleted-fake").is_file()
    assert (
        store / "fake" / "vault" / "configs" / "datasets" / "beach-test" / "submit.yaml"
    ).is_file()
    assert not (store / "fake" / "vault" / "data" / "runs" / "good" / "predictions").exists()
    ledger = BackupLedger(_paths(world, "beach-test").backup_log)
    assert [row.event for row in ledger.rows[-3:]] == ["push", "push", "remote_forgotten"]
    assert ledger.rows[-1].remote == "fake" and ledger.rows[-3].failed == [
        "configs/datasets/beach-test/submit.yaml"
    ]
    r = _backup("verify", *common)
    outputs.append(r.output)
    v = _verdict(r.output)
    manifest = load_manifest(_paths(world, "beach-test"), "r1")
    tier1 = sum(1 for f in manifest.files if f.tier == 1 and f.kind == "file")
    rest = sum(1 for f in manifest.files if f.tier > 1 and f.kind == "file")
    assert (
        r.exit_code == 1 and f"ok={tier1 + 1}" in v and f"missing={rest}" in v
    )  # +1: the remote_copy, verified in place
    profile = _paths(world, "beach-test").submit_yaml
    original = profile.read_bytes()
    profile.unlink()
    r = _backup("pull", *common, "--tier", "1")
    outputs.append(r.output)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "pulled=1" in v and profile.read_bytes() == original
    _scan(world, outputs)
