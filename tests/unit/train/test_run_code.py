"""``vcp train run`` snapshots the code it runs (spec 2026-10-31 §5.1 steps 2-5 and 7-9, §10.2):
every root is planned and scanned before the first write, written or reused under ``own_writes``,
recorded in ``env.<n>.json`` and bound to the run as a ``code.<label>`` ref."""

import builtins
import errno
import json
import os
import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import helpers
import vcp
from helpers import FAKE, git_repo, seed_train
from vcp.code import roots as code_roots
from vcp.code import rules
from vcp.code.roots import Editable, canonical_name
from vcp.code.schema import Distribution
from vcp.code.snapshot import SnapshotOutcome, load_index
from vcp.core.errors import IntegrityError, ValidationFailed, VcpError
from vcp.core.paths import artifact_dir, store_path
from vcp.measure.runs import load_run, run_dir
from vcp.train import coderun
from vcp.train import env as envmod
from vcp.train import run as run_module
from vcp.train.records import load_record, read_events, train_yaml
from vcp.train.run import RunSpec, train_run

IGNORE_WEIGHTS = b"weights/\n"

# C1: a framework writes its settings and metrics into --cwd, untracked.
OUTPUT_FAKE = (
    FAKE.replace("sys.exit(code)\n", "")
    + """
for name in ("args.yaml", "hparams.yaml", "config.json", "metrics.json"):
    Path(name).write_text("epoch: " + str(time.time()) + "\\n")
sys.exit(code)
"""
)


def _spec(roots, cwd, **kw) -> RunSpec:
    base = dict(
        run_id="r",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        cwd=cwd,
        command=[sys.executable, "fake_train.py", "0"],
        checkpoints=["weights/*.pt"],
        final="weights/best.pt",
        data_root=roots.data,
        configs_root=roots.configs,
    )
    return RunSpec(**{**base, **kw})


def _env(roots, n=1, run_id="r") -> dict:
    return json.loads((run_dir(roots.data, run_id) / "train" / f"env.{n}.json").read_bytes())


def _by_label(env: dict) -> dict[str, dict]:
    return {root["label"]: root for root in env["code"]["roots"]}


def _code_refs(roots, run_id="r"):
    """The run's code-snapshot refs, picked by kind and role (a label set may be named code.x)."""
    record = load_record(roots.data, run_id)
    return [r for r in record.evidence if r.kind == "code_snapshot" and r.role == "code"]


def _snapshots(roots) -> list[str]:
    base = roots.data / "artifacts" / "code_snapshot"
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


def _never_touch(monkeypatch, names):
    """``open``, ``Path.open``, ``os.stat`` and ``os.lstat`` raise for a file named in ``names``."""
    banned = frozenset(names)
    real_path_open, real_open, real_stat, real_lstat = Path.open, builtins.open, os.stat, os.lstat

    def check(file):
        if not isinstance(file, int) and os.path.basename(os.fsdecode(file)) in banned:
            raise AssertionError(f"touched {file}")

    def guarded(real):
        def call(file, *args, **kwargs):
            check(file)
            return real(file, *args, **kwargs)

        return call

    monkeypatch.setattr(Path, "open", guarded(real_path_open))
    monkeypatch.setattr(builtins, "open", guarded(real_open))
    monkeypatch.setattr(os, "stat", guarded(real_stat))
    monkeypatch.setattr(os, "lstat", guarded(real_lstat))


def _with_vcp(report, version):
    """``report`` whose environment has the vcp distribution at ``version``."""
    others = [d for d in report.distributions if canonical_name(d.name) != "vcp"]
    vcp_row = Distribution(name="vcp", version=version, record_sha256=None)
    return replace(report, distributions=[*others, vcp_row])


# -- a run's code ---------------------------------------------------------------------------------


def test_a_clean_repository_run_snapshots_its_code(roots, tmp_path, isolated_git):
    seed_train(roots)
    work = git_repo(
        tmp_path / "work", {"fake_train.py": FAKE.encode(), ".gitignore": IGNORE_WEIGHTS}
    )
    res = train_run(_spec(roots, work))
    assert res.attempt.status == "finished"
    assert res.code_snapshot == "ok" and res.untracked_code == 0 and res.code_missing == []
    assert not any(w.startswith(("code_snapshot=", "untracked_code=")) for w in res.warnings)
    env1 = _env(roots)
    root = env1["code"]["roots"][0]
    assert (root["label"], root["source"], root["kind"], root["reason"]) == (
        "cwd",
        "cwd",
        "git",
        None,
    )
    assert root["path"] == store_path(work, roots.data) and re.fullmatch(
        r"[0-9a-f]{40}", root["commit"]
    )
    assert re.fullmatch(r"code-[0-9a-f]{16}", root["snapshot"]) and root["files"] == 1  # no dotfile
    assert env1["code"]["rules"] == 1 and env1["identity"]["python"] and env1["distributions"]
    refs = _code_refs(roots)
    assert [(r.name, r.role, r.binding, r.attempt) for r in refs] == [
        ("code.cwd", "code", "cli", 1)
    ]
    assert refs[0].artifact_id == root["snapshot"]
    events = [e for e in read_events(roots.data, "r") if e["event"] == "evidence"]
    assert [(e["kind"], e["name"], e["artifact_id"]) for e in events] == [
        ("code_snapshot", "code.cwd", root["snapshot"])
    ]
    assert "code.cwd" in [r.name for r in load_run(roots.data, "r").evidence]
    assert res.evidence == 0  # the VERDICT's evidence= counts what a person attached

    again = train_run(_spec(roots, work, resume=True))
    assert (
        again.code_snapshot == "ok"
        and _env(roots, 2)["code"]["roots"][0]["snapshot"] == root["snapshot"]
    )
    assert len(_code_refs(roots)) == 1  # the same snapshot: no second row, no second event
    assert len([e for e in read_events(roots.data, "r") if e["event"] == "evidence"]) == 1
    assert _snapshots(roots) == [root["snapshot"]]


def test_an_untracked_helper_is_kept_and_warned(roots, tmp_path, isolated_git, capsys):
    seed_train(roots)
    train_py = b"import helper\n" + FAKE.encode()
    work = git_repo(tmp_path / "work", {"fake_train.py": train_py, ".gitignore": IGNORE_WEIGHTS})
    (work / "helper.py").write_bytes(b"VALUE = 1\n")  # imported, never added
    before_child: list[str] = []

    def first_line(line):
        if not before_child:
            before_child.append(capsys.readouterr().err)

    res = train_run(_spec(roots, work, on_line=first_line))
    assert res.attempt.status == "finished"
    sid = _env(roots)["code"]["roots"][0]["snapshot"]
    assert ("helper.py", True) in [(e.path, e.kept) for e in load_index(roots.data, sid)]
    adir = artifact_dir(roots.data, "code_snapshot", sid)
    assert (adir / "files" / "helper.py").read_bytes() == (work / "helper.py").read_bytes()
    root = _env(roots)["code"]["roots"][0]
    assert (root["untracked_code"], root["untracked_code_paths"]) == (1, ["helper.py"])
    assert res.untracked_code == 1 and "untracked_code=1" in res.warnings
    line = "untracked_code=1: cwd:helper.py (in the snapshot; git add or ignore them)"
    assert line in before_child[0]  # printed before the child's first line


def test_the_notice_lists_five_paths_and_counts_them_all(roots, tmp_path, isolated_git, capsys):
    seed_train(roots)
    work = git_repo(
        tmp_path / "work", {"fake_train.py": FAKE.encode(), ".gitignore": IGNORE_WEIGHTS}
    )
    for i in range(7):
        (work / f"h{i}.py").write_bytes(b"X = 1\n")
    res = train_run(_spec(roots, work))
    assert res.untracked_code == 7 and "untracked_code=7" in res.warnings
    shown = ", ".join(f"cwd:h{i}.py" for i in range(5))
    assert (
        f"untracked_code=7: {shown} (in the snapshot; git add or ignore them)"
        in capsys.readouterr().err
    )


def test_a_plain_directory_has_no_untracked_count_and_no_notice(roots, work, capsys):
    seed_train(roots)
    res = train_run(_spec(roots, work))
    assert res.code_snapshot == "ok" and res.untracked_code is None
    root = _env(roots)["code"]["roots"][0]
    assert (root["kind"], root["commit"], root["untracked_code"]) == ("dir", None, 0)
    assert "untracked_code" not in capsys.readouterr().err


def test_a_too_large_root_is_partial_and_training_runs(roots, work, tmp_path, monkeypatch):
    seed_train(roots)
    (work / "a.py").write_bytes(b"A = 1\n")
    (work / "b.py").write_bytes(b"B = 1\n")  # three .py with fake_train.py
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "lib.py").write_bytes(b"L = 1\n")
    monkeypatch.setattr(rules, "MAX_FILES", 2)
    res = train_run(_spec(roots, work, code_roots=[extra]))
    assert res.attempt.status == "finished"
    assert (res.code_snapshot, res.code_missing) == ("partial", ["cwd"])
    assert "code_snapshot=partial (code_missing=cwd)" in res.warnings
    found = _by_label(_env(roots))
    assert (found["cwd"]["reason"], found["cwd"]["snapshot"], found["cwd"]["files"]) == (
        "too_large",
        None,
        0,
    )
    assert found["root-1"]["reason"] is None and found["root-1"]["source"] == "extra"
    assert [(r.name, r.artifact_id) for r in _code_refs(roots)] == [
        ("code.root-1", found["root-1"]["snapshot"])
    ]


@pytest.mark.parametrize("reason", ["drift", "locked", "partial", "unwritable"])
def test_a_snapshot_that_cannot_be_written_degrades_without_authorization(
    roots, work, monkeypatch, reason
):
    seed_train(roots)

    def refused(data_root, scan, *, command):
        assert command == "train.run"
        return SnapshotOutcome(str(scan.snapshot_id), None, reason, 0, 0)

    monkeypatch.setattr(coderun, "write_snapshot", refused)
    res = train_run(_spec(roots, work))
    assert res.attempt.status == "finished"
    root = _env(roots)["code"]["roots"][0]
    assert (root["reason"], root["snapshot"]) == (reason, None)
    assert (res.code_snapshot, res.code_missing) == ("none", ["cwd"])
    assert "code_snapshot=none (code_missing=cwd)" in res.warnings
    assert not _code_refs(roots)


def test_an_editable_whose_directory_is_gone_is_not_found(roots, work, tmp_path, monkeypatch):
    """R1: PROBE named an editable whose directory no longer exists."""
    seed_train(roots)
    ghost = tmp_path / "gone"
    monkeypatch.setattr(code_roots, "editables_of", lambda raw: [Editable("Ghost_Pkg", ghost)])
    res = train_run(_spec(roots, work))
    assert res.attempt.status == "finished"
    found = _by_label(_env(roots))
    assert list(found) == ["cwd", "dist-ghost-pkg"]
    ghost_root = found["dist-ghost-pkg"]
    assert (ghost_root["source"], ghost_root["reason"], ghost_root["snapshot"]) == (
        "editable",
        "not_found",
        None,
    )
    assert (res.code_snapshot, res.code_missing) == ("partial", ["dist-ghost-pkg"])
    assert "code_snapshot=partial (code_missing=dist-ghost-pkg)" in res.warnings


def test_a_tampered_snapshot_of_the_same_content_fails_before_any_write(roots, work):
    seed_train(roots)
    train_run(_spec(roots, work))
    sid = _env(roots)["code"]["roots"][0]["snapshot"]
    (artifact_dir(roots.data, "code_snapshot", sid) / "snapshot.json").write_bytes(b"{}\n")
    with pytest.raises(IntegrityError, match="^mismatch:"):
        train_run(_spec(roots, work, run_id="r2"))
    assert not run_dir(roots.data, "r2").exists()


def test_a_full_disk_while_snapshotting_is_disk_full(roots, work, monkeypatch):
    seed_train(roots)

    def full(data_root, scan, *, command):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(coderun, "write_snapshot", full)
    with pytest.raises(VcpError, match="^disk_full:") as ei:
        train_run(_spec(roots, work))
    assert ei.value.status == "ABORT" and not run_dir(roots.data, "r").exists()


# -- before the first write -----------------------------------------------------------------------


def test_the_child_vcp_must_share_the_minor(roots, work, tmp_path, monkeypatch):
    seed_train(roots)
    venv = tmp_path / "venv"
    for python in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        python.parent.mkdir(parents=True)
        python.write_bytes(b"")
    major, minor, *_ = vcp.__version__.split(".")
    other = f"{major}.{int(minor) - 1 if int(minor) else int(minor) + 1}.0"
    real = envmod.probe(None)
    monkeypatch.setattr(run_module, "probe", lambda python: _with_vcp(real, other))
    with pytest.raises(ValidationFailed, match="^vcp_mismatch:") as ei:
        train_run(_spec(roots, work, venv=venv))
    assert ei.value.fields["child_vcp"] == other and f"vcp {other}" in str(ei.value)
    assert not run_dir(roots.data, "r").exists()
    # without --venv the wrapper's own environment runs the command: nothing to compare
    assert train_run(_spec(roots, work)).attempt.status == "finished"
    # the same MINOR, another patch, is the same contract
    same = f"{major}.{minor}.999"
    monkeypatch.setattr(run_module, "probe", lambda python: _with_vcp(real, same))
    assert train_run(_spec(roots, work, run_id="r2", venv=venv)).attempt.status == "finished"


def test_a_failing_probe_aborts_before_any_write(roots, work, monkeypatch):
    seed_train(roots)
    train_run(_spec(roots, work))
    before = train_yaml(roots.data, "r").read_bytes()
    monkeypatch.setattr(envmod, "PROBE", "import sys; sys.exit(3)")
    with pytest.raises(VcpError, match="environment probe failed") as ei:
        train_run(_spec(roots, work, run_id="r2"))
    assert ei.value.status == "ABORT" and not run_dir(roots.data, "r2").exists()
    with pytest.raises(VcpError, match="environment probe failed"):
        train_run(_spec(roots, work, resume=True))
    assert train_yaml(roots.data, "r").read_bytes() == before  # no `running` attempt left behind


def test_a_missing_code_root_fails_before_any_write(roots, work, tmp_path):
    seed_train(roots)
    a_file = tmp_path / "lib.py"
    a_file.write_bytes(b"")
    for bad in (tmp_path / "nowhere", a_file):
        with pytest.raises(ValidationFailed, match="^not_found: --code-root"):
            train_run(_spec(roots, work, code_roots=[bad]))
        assert not run_dir(roots.data, "r").exists()
    assert not (roots.data / "artifacts").exists()


# -- what a root holds ----------------------------------------------------------------------------


def test_an_unignored_secret_is_not_read(roots, tmp_path, isolated_git, monkeypatch):
    seed_train(roots)
    files = {"fake_train.py": FAKE.encode(), ".gitignore": IGNORE_WEIGHTS, "deploy.pem": b"KEY\n"}
    work = git_repo(tmp_path / "work", files)
    (work / "kaggle.json").write_bytes(b'{"key": "secret"}')  # untracked, not ignored
    _never_touch(monkeypatch, {"deploy.pem", "kaggle.json"})
    res = train_run(_spec(roots, work))
    assert res.attempt.status == "finished"
    root = _env(roots)["code"]["roots"][0]
    assert set(root["excluded_paths"]) == {"deploy.pem", "kaggle.json"} and root["excluded"] == 2
    paths = [e.path for e in load_index(roots.data, root["snapshot"])]
    assert paths == ["fake_train.py"]


def test_script_and_extra_roots_are_recorded(roots, tmp_path, isolated_git):
    seed_train(roots)
    work = git_repo(tmp_path / "work", {"notes.py": b"N = 1\n", ".gitignore": IGNORE_WEIGHTS})
    tools = git_repo(tmp_path / "tools", {"fake_train.py": FAKE.encode()})
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "lib.py").write_bytes(b"L = 1\n")
    command = [sys.executable, str(tools / "fake_train.py"), "0"]
    res = train_run(_spec(roots, work, command=command, code_roots=[extra]))
    assert res.attempt.status == "finished" and res.code_snapshot == "ok"
    recorded = _env(roots)["code"]["roots"]
    assert [(r["label"], r["source"], r["kind"]) for r in recorded] == [
        ("cwd", "cwd", "git"),
        ("script", "script", "git"),
        ("root-1", "extra", "dir"),
    ]
    assert [r["path"] for r in recorded] == [
        store_path(p, roots.data) for p in (work, tools, extra)
    ]
    assert all(r["snapshot"] for r in recorded) and recorded[2]["commit"] is None
    assert [r.name for r in _code_refs(roots)] == ["code.cwd", "code.script", "code.root-1"]
    assert res.untracked_code == 0  # both git roots, summed


def test_checkpoint_directories_are_excluded_and_recorded(roots, work):
    seed_train(roots)
    (work / "runs" / "exp").mkdir(parents=True)
    (work / "runs" / "exp" / "gen.py").write_bytes(b"G = 1\n")
    res = train_run(_spec(roots, work, checkpoints=["runs/exp/*.pt"], final=None))
    assert res.attempt.status == "finished"
    env1 = _env(roots)
    index_paths = [e.path for e in load_index(roots.data, env1["code"]["roots"][0]["snapshot"])]
    assert "runs/exp/gen.py" not in index_paths and "fake_train.py" in index_paths
    assert env1["code"]["excluded_dirs"] == [store_path(work / "runs" / "exp", roots.data)]


def test_framework_outputs_do_not_change_the_identity(roots, tmp_path, isolated_git):
    seed_train(roots)
    work = git_repo(
        tmp_path / "work", {"fake_train.py": OUTPUT_FAKE.encode(), ".gitignore": IGNORE_WEIGHTS}
    )
    train_run(_spec(roots, work))
    assert (work / "args.yaml").is_file() and (work / "metrics.json").is_file()
    again = train_run(_spec(roots, work, resume=True))
    first, second = (_env(roots, n)["code"]["roots"][0] for n in (1, 2))
    assert first["snapshot"] == second["snapshot"] and second["untracked_code"] == 0
    assert again.code_snapshot == "ok" and not any(
        w.startswith("untracked") for w in again.warnings
    )


def test_code_tracking_is_off_unless_a_test_turns_it_on(monkeypatch):
    assert os.environ["VCP_CODE_TRACE"] == "0" and code_roots.editables_of([{"name": "x"}]) == []
    helpers.trace_on(monkeypatch)
    assert "VCP_CODE_TRACE" not in os.environ
    assert code_roots.editables_of([]) == [Editable("vcp", helpers.VCP_PACKAGE)]
    assert (helpers.VCP_PACKAGE / "__init__.py").is_file()
