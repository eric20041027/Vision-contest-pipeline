import errno
import json
import os
import sys
from pathlib import Path

from typer.testing import CliRunner

from helpers import det_samples, git_repo, make_card, write_images
from vcp.cli import app
from vcp.core import atomic
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.train import disk
from vcp.train.disk import GIB

runner = CliRunner()


def test_train_failure_preserves_command_identity(roots, tmp_path):
    result = runner.invoke(
        app,
        [
            "train",
            "upload",
            "--run",
            "trial",
            "--dest",
            str(tmp_path / "vault"),
            "--only",
            "typo",
            "--json",
        ],
    )
    assert result.exit_code == 1, result.output
    verdict = result.stderr.strip().splitlines()[-1]
    assert verdict.startswith("VERDICT cmd=train.upload status=FAIL")
    assert "run=trial" in verdict
    assert json.loads(result.stdout)["fields"]["run"] == "trial"


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
        and "superseded=0" in v  # 5-10: always reported, so 0 is a statement, not a silence
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


def test_train_status_cli_prints_tmp(roots, tmp_path):
    """5.5: a leftover `.tmp` beside a checkpoint is a WARN with its path and size, in the human
    lines and in --json; the field is always printed, so `tmp=0` is a statement, not a silence."""
    seed_det(roots)
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_text(FAKE, encoding="utf-8")
    assert _run(work, "--seed", "1", "--upload", str(tmp_path / "vault")).exit_code == 0
    r = runner.invoke(app, ["train", "status", "--run", "r1"])
    assert r.exit_code == 0, r.output
    assert "tmp=0" in _verdict(r.output) and "status=OK" in _verdict(r.output)

    leftover = work / "weights" / "resume.pth.tmp"
    leftover.write_bytes(b"x" * 10)
    r = runner.invoke(app, ["train", "status", "--run", "r1"])
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "tmp=1" in v and "status=WARN" in v
    assert "tmp: " in r.output and "(10 bytes)" in r.output

    r = runner.invoke(app, ["train", "status", "--run", "r1", "--json"])
    assert r.exit_code == 0, r.output
    doc = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert doc["fields"]["tmp"] == 1 and doc["result"]["tmp"][0]["bytes"] == 10
    assert doc["result"]["tmp"][0]["path"].endswith("weights/resume.pth.tmp")
    assert leftover.is_file()  # read-only: listing a leftover never deletes it

    leftover.unlink()
    r = runner.invoke(app, ["train", "status", "--run", "r1"])
    assert "tmp=0" in _verdict(r.output) and "status=OK" in _verdict(r.output)


def test_train_status_cli_says_what_the_tmp_scan_could_not_read(roots, tmp_path, monkeypatch):
    """A directory the scan cannot list no longer aborts the command: one `tmp_scan: skipped`
    line names it, the VERDICT is what it would have been, and `--json` is unchanged."""
    seed_det(roots)
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_text(FAKE, encoding="utf-8")
    assert _run(work, "--seed", "1", "--upload", str(tmp_path / "vault")).exit_code == 0
    r = runner.invoke(app, ["train", "status", "--run", "r1", "--json"])
    assert r.exit_code == 0, r.output
    before = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))

    real = Path.iterdir

    def iterdir(self):
        if self.name == "weights":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    r = runner.invoke(app, ["train", "status", "--run", "r1"])
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "status=OK" in v and "tmp=0" in v and "tmp_scan" not in v
    assert f"tmp_scan: skipped {(work / 'weights').resolve().as_posix()} (PermissionError)" in (
        r.output
    )

    r = runner.invoke(app, ["train", "status", "--run", "r1", "--json"])
    assert r.exit_code == 0, r.output
    after = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert after == before
    assert "tmp_scan" not in r.stdout


def test_upload_and_status_have_no_configs_root_option(roots, tmp_path):
    """5-8: both declared `--configs-root` and never read it -- they take a run id and a data
    root, nothing that lives under configs/. An option that does nothing is worse than absent."""
    for cmd in (
        ["train", "status", "--run", "r1", "--configs-root", str(tmp_path)],
        [
            "train",
            "upload",
            "--run",
            "r1",
            "--dest",
            str(tmp_path),
            "--configs-root",
            str(tmp_path),
        ],
    ):
        r = runner.invoke(app, cmd)
        assert r.exit_code != 0, r.output
        assert "configs-root" in (r.output + (r.stderr if r.stderr_bytes else ""))
    assert "--configs-root" not in runner.invoke(app, ["train", "status", "--help"]).output
    assert "--configs-root" not in runner.invoke(app, ["train", "upload", "--help"]).output
    assert "--configs-root" in runner.invoke(app, ["train", "run", "--help"]).output


def test_upload_only_accepts_final(roots, tmp_path):
    """5-9: `--only` is a two-valued flag spelled as a string; anything else must say what the
    one accepted value is rather than silently uploading everything."""
    r = runner.invoke(
        app, ["train", "upload", "--run", "r1", "--dest", str(tmp_path), "--only", "garbage"]
    )
    assert r.exit_code == 1, r.output
    v = _verdict(r.output)
    assert "status=FAIL" in v and "'final'" in v and "garbage" in v


def test_train_run_cli_git_fields(roots, tmp_path, isolated_git):
    seed_det(roots)
    repo = git_repo(tmp_path / "repo", {"fake_train.py": FAKE.encode()})
    r = _run(repo, "--seed", "3", "--framework", "fake")
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "commit=" in v and "commit=none" not in v
    assert "modified=0" in v and "untracked=0" in v
    (repo / "fake_train.py").write_bytes(FAKE.encode() + b"# tweak\n")
    r = _run(repo, "--seed", "3", "--framework", "fake", "--require-clean", run="r2")
    assert r.exit_code == 1, r.output
    v = _verdict(r.output)
    assert "status=FAIL" in v and "dirty_tree" in v and "modified=1" in v and "run=r2" in v
    assert "commit=" in v and "commit=none" not in v
    r = _run(repo, "--seed", "3", "--framework", "fake", run="r4")
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "status=WARN" in v and "modified=1" in v
    assert "warning: modified=1 tracked path(s)" in r.output
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_bytes(FAKE.encode())
    r = _run(work, "--seed", "3", "--framework", "fake", run="r3")
    v = _verdict(r.output)
    assert "commit=none" in v and "modified=" not in v


def _work(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_text(FAKE, encoding="utf-8")
    return work


def test_train_run_cli_free_gib_and_min_free_gib(roots, tmp_path, monkeypatch):
    seed_det(roots)
    work = _work(tmp_path)
    assert "free_gib=1024.0" in _verdict(_run(work).output)  # the conftest's roomy probe
    monkeypatch.setattr(disk, "usage", lambda path: (2 * GIB, 100 * GIB))
    r = _run(work, run="r2")
    v = _verdict(r.output)
    assert r.exit_code == 2 and "status=ABORT" in v and "low_disk:" in v and "run=r2" in v
    assert "free_gib=2.0" in v and "min_free_gib=10.0" in v and "disk_path=" in v
    assert not (roots.data / "runs" / "r2").exists()
    r = _run(work, "--min-free-gib", "0", run="r2")
    assert r.exit_code == 0, r.output
    assert "free_gib=2.0" in _verdict(r.output)
    r = _run(work, "--min-free-gib=-1", run="r3")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "invalid:" in v and "run=r3" in v
    assert not (roots.data / "runs" / "r3").exists()

    def broken(path):
        raise OSError("probe failed")

    monkeypatch.setattr(disk, "usage", broken)
    r = _run(work, run="r4")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v
    assert "disk_check=unavailable" in v and "free_gib=unknown" in v


def test_train_run_cli_json_carries_the_disk_check(roots, tmp_path):
    seed_det(roots)
    r = _run(_work(tmp_path), "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert doc["fields"]["free_gib"] == 1024.0
    assert "disk_check" not in doc["fields"]


def test_train_run_help_documents_min_free_gib(roots):
    out = runner.invoke(app, ["train", "run", "--help"]).output
    assert "--min-free-gib" in out and "0" in out


def test_train_run_cli_names_disk_full(roots, tmp_path):
    seed_det(roots)
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_text(
        'import sys\nprint("OSError: [Errno 28] No space left on device")\n'
        "sys.exit(int(sys.argv[1]))\n",
        encoding="utf-8",
    )
    r = _run(work, code="1")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "disk_evidence=console" in v
    assert "free_gib=" in v
    assert 'reason="disk_full: the command exited 1 and its console says' in v
    assert "; free space, then --resume" in r.output


def test_train_run_cli_names_low_disk_after_a_failed_command(roots, tmp_path, monkeypatch):
    """A child that fails without a disk word, on a disk that dropped below the bar after it
    started: the VERDICT stays FAIL and says ``low_disk:`` with no ``disk_evidence=``."""
    seed_det(roots)
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        'Path("started").write_text("x")\n'
        'print("terminate called after throwing std::ios_base::failure: ios_base::badbit set")\n'
        "sys.exit(int(sys.argv[1]))\n",
        encoding="utf-8",
    )
    started = work / "started"
    monkeypatch.setattr(
        disk, "usage", lambda path: (2 * GIB if started.exists() else 1024 * GIB, 1024 * GIB)
    )
    r = _run(work, code="1")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "run=r1" in v and "exit_code=1" in v
    assert 'reason="low_disk: the command exited 1 with 2.0 GiB free' in v
    assert "(below --min-free-gib 10); the disk may have filled" in v
    assert "disk_evidence=" not in v and "disk_full" not in v


def test_train_run_cli_aborts_when_vcp_itself_cannot_write(roots, tmp_path, monkeypatch):
    """vcp's own record failing with ENOSPC is an ABORT ``disk_full:`` with ``disk_evidence=vcp``
    that still names the run."""
    seed_det(roots)
    work = _work(tmp_path)
    assert _run(work).exit_code == 0
    real_replace = os.replace

    def full_disk(src, dst):
        if Path(dst).name == "train.yaml":
            raise OSError(errno.ENOSPC, "No space left on device")
        real_replace(src, dst)

    monkeypatch.setattr(atomic.os, "replace", full_disk)
    r = _run(work, "--resume")
    v = _verdict(r.output)
    assert r.exit_code == 2 and "status=ABORT" in v and "disk_evidence=vcp" in v
    assert "run=r1" in v and "disk_full: vcp could not write its own record" in v
