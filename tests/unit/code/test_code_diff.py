"""``vcp.code.diff`` (spec 2026-10-31 §5.7): a snapshot held against the files that are there now,
by the rules the snapshot was taken with. It reads the working tree and writes nothing; whether the
snapshot itself still holds is ``vcp code verify``'s question, not this one's."""

import sys
from pathlib import Path

import pytest

from code_fixtures import write_file
from helpers import FAKE, git_repo, seed_train
from vcp.code import diff, process, rules
from vcp.code.diff import DiffResult, diff_root, diff_run
from vcp.code.scan import RootScan, scan_root
from vcp.code.snapshot import write_snapshot
from vcp.core.errors import ValidationFailed, VcpError
from vcp.train.records import load_record, save_record
from vcp.train.run import RunSpec, train_run

FILES = {"a.py": b"a = 1\n", "b.py": b"b = 2\n", "pkg/c.py": b"c = 3\n"}


def _project(tmp_path: Path, name: str = "proj") -> Path:
    root = tmp_path / name
    for rel, data in FILES.items():
        write_file(root / rel, data)
    return root


def _snapshot(roots, root: Path, *, skip_dirs=()) -> str:
    scan = scan_root(root, kind="dir", skip_dirs=skip_dirs, keep="all")
    outcome = write_snapshot(roots.data, scan, command="test.diff")
    assert outcome.reason is None
    return outcome.snapshot_id


def _empty() -> DiffResult:
    return DiffResult(changed=[], added=[], removed=[])


# -- one root -------------------------------------------------------------------------------------


def test_a_root_nobody_touched_has_no_difference(roots, tmp_path):
    root = _project(tmp_path)
    sid = _snapshot(roots, root)
    assert diff_root(roots.data, sid, root) == _empty()


def test_changed_added_and_removed_files_are_listed_in_path_order(roots, tmp_path):
    root = _project(tmp_path)
    sid = _snapshot(roots, root)
    (root / "a.py").write_bytes(b"a = 2\n")
    (root / "pkg" / "c.py").write_bytes(b"c = 4\n")
    write_file(root / "z.py")
    write_file(root / "pkg" / "new.py")
    (root / "b.py").unlink()
    assert diff_root(roots.data, sid, root) == DiffResult(
        changed=["a.py", "pkg/c.py"], added=["pkg/new.py", "z.py"], removed=["b.py"]
    )


def test_files_the_rules_never_take_are_no_difference(roots, tmp_path):
    root = _project(tmp_path)
    sid = _snapshot(roots, root)
    for rel in ("notes.txt", ".hidden/x.py", "__pycache__/a.pyc.py", "credentials.py", "k.pem"):
        write_file(root / rel)
    assert diff_root(roots.data, sid, root) == _empty()


def test_the_snapshot_is_not_looked_at_only_its_index_is_read(roots, tmp_path):
    root = _project(tmp_path)
    sid = _snapshot(roots, root)
    from vcp.core.paths import artifact_dir

    # the stored copy was damaged: that is `verify`'s to find; the diff compares the index
    (artifact_dir(roots.data, "code_snapshot", sid) / "files" / "a.py").write_bytes(b"damaged\n")
    assert diff_root(roots.data, sid, root) == _empty()


def test_the_data_root_inside_the_root_is_left_out(tmp_path):
    root = _project(tmp_path)
    data = root / "vcp-data"  # the snapshot's own copies are .py files below the root
    ids = [w.outcome.snapshot_id for w in process.snapshot_roots(data, None, [root])]
    assert any(p.suffix == ".py" for p in (data / "artifacts").rglob("*"))
    assert diff_root(data, ids[0], root) == _empty()


def test_skip_dirs_the_caller_gives_are_left_out(roots, tmp_path):
    root = _project(tmp_path)
    sid = _snapshot(roots, root, skip_dirs=[root / "pkg"])
    assert diff_root(roots.data, sid, root) == DiffResult([], ["pkg/c.py"], [])
    assert diff_root(roots.data, sid, root, skip_dirs=[root / "pkg"]) == _empty()


def test_generated_code_an_environment_variable_names_is_left_out(roots, tmp_path, monkeypatch):
    root = _project(tmp_path)
    cache = tmp_path / "proj" / "inductor"
    monkeypatch.setenv("TORCHINDUCTOR_CACHE_DIR", str(cache))
    sid = _snapshot(roots, root, skip_dirs=[cache])
    write_file(cache / "abc" / "kernel.py")
    assert diff_root(roots.data, sid, root) == _empty()
    monkeypatch.delenv("TORCHINDUCTOR_CACHE_DIR")
    assert diff_root(roots.data, sid, root).added == ["inductor/abc/kernel.py"]


def test_a_git_root_is_read_the_way_a_snapshot_reads_it(roots, tmp_path, isolated_git):
    repo = git_repo(tmp_path / "repo", {"train.py": b"v = 1\n", "cfg.yaml": b"lr: 1\n"})
    scan = scan_root(repo, kind="git", keep="all")
    sid = write_snapshot(roots.data, scan, command="test.diff").snapshot_id
    assert diff_root(roots.data, sid, repo) == _empty()
    (repo / "cfg.yaml").write_bytes(b"lr: 2\n")  # a tracked config counts
    write_file(repo / "loose.yaml")  # an untracked one does not
    write_file(repo / "loose.py")  # an untracked code file does
    assert diff_root(roots.data, sid, repo) == DiffResult(["cfg.yaml"], ["loose.py"], [])
    assert diff_root(roots.data, sid, repo, kind="dir") == DiffResult(
        [], ["loose.py"], ["cfg.yaml"]
    )  # the kind is the caller's to give


def test_a_root_that_is_gone_is_not_found(roots, tmp_path):
    root = _project(tmp_path)
    sid = _snapshot(roots, root)
    with pytest.raises(ValidationFailed, match="^not_found:"):
        diff_root(roots.data, sid, tmp_path / "nowhere")


def test_a_root_past_the_limits_is_too_large(roots, tmp_path, monkeypatch):
    root = _project(tmp_path)
    sid = _snapshot(roots, root)
    monkeypatch.setattr(rules, "MAX_FILES", 1)
    with pytest.raises(ValidationFailed, match="^too_large:") as ei:
        diff_root(roots.data, sid, root, label="cwd")
    assert ei.value.fields == {"code_root": "cwd"}


def test_an_unreadable_root_is_code_unavailable(roots, tmp_path, monkeypatch):
    root = _project(tmp_path)
    sid = _snapshot(roots, root)

    def unreadable(path, *, kind, skip_dirs, keep="capped"):
        return RootScan(path, kind, (), (), (), "unreadable")

    monkeypatch.setattr(diff, "scan_root", unreadable)
    with pytest.raises(VcpError, match="^code_unavailable:") as ei:
        diff_root(roots.data, sid, root)
    assert ei.value.status == "ABORT" and ei.value.fields["cause"] == "unreadable"


def test_an_unknown_snapshot_is_not_found(roots, tmp_path):
    with pytest.raises(ValidationFailed, match="^not_found:"):
        diff_root(roots.data, "code-" + "0" * 16, _project(tmp_path))


def test_diff_writes_nothing(roots, tmp_path):
    root = _project(tmp_path)
    sid = _snapshot(roots, root)
    before = sorted(p for p in roots.data.rglob("*"))
    (root / "a.py").write_bytes(b"a = 2\n")
    diff_root(roots.data, sid, root)
    assert sorted(p for p in roots.data.rglob("*")) == before


# -- a training run -------------------------------------------------------------------------------


def _train(roots, cwd: Path, **kw):
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
    return train_run(RunSpec(**{**base, **kw}))


def test_diff_run_holds_each_root_of_the_last_attempt_against_the_disk(roots, work):
    seed_train(roots)
    write_file(work / "helper.py", b"x = 1\n")
    _train(roots, work)
    assert diff_run(roots.data, "r", None) == [("cwd", _empty())]
    (work / "helper.py").write_bytes(b"x = 2\n")
    write_file(work / "extra.py")
    assert diff_run(roots.data, "r", None) == [("cwd", DiffResult(["helper.py"], ["extra.py"], []))]


def test_diff_run_takes_the_attempt_it_is_asked_for(roots, work):
    seed_train(roots)
    write_file(work / "helper.py", b"x = 1\n")
    _train(roots, work)
    (work / "helper.py").write_bytes(b"x = 2\n")
    _train(roots, work, resume=True)  # attempt 2 saw the new helper
    assert diff_run(roots.data, "r", None) == [("cwd", _empty())]
    assert diff_run(roots.data, "r", 2) == [("cwd", _empty())]
    assert diff_run(roots.data, "r", 1) == [("cwd", DiffResult(["helper.py"], [], []))]
    with pytest.raises(ValidationFailed, match="^not_found:.*attempt 3"):
        diff_run(roots.data, "r", 3)


def test_diff_run_leaves_a_root_nested_in_another_to_its_own_snapshot(roots, tmp_path):
    seed_train(roots)
    work = tmp_path / "work"
    write_file(work / "fake_train.py", FAKE.encode())
    inner = write_file(work / "vendored" / "lib.py", b"v = 1\n").parent
    _train(roots, work, code_roots=[inner])
    record = load_record(roots.data, "r")
    assert record.attempts  # both roots are in the attempt
    (inner / "lib.py").write_bytes(b"v = 2\n")
    result = dict(diff_run(roots.data, "r", None))
    assert result["root-1"].changed == ["lib.py"]
    assert result["cwd"] == _empty()  # the outer root does not list the inner one's files


def test_diff_run_leaves_out_the_directories_the_run_excluded(roots, work):
    seed_train(roots)
    _train(roots, work)  # weights/*.pt is a checkpoint glob: weights/ is excluded
    write_file(work / "weights" / "tool.py")
    assert diff_run(roots.data, "r", None) == [("cwd", _empty())]


def test_diff_run_takes_the_skip_dirs_the_caller_gives(roots, work):
    seed_train(roots)
    _train(roots, work)
    write_file(work / "scratch" / "x.py")
    assert diff_run(roots.data, "r", None) == [("cwd", DiffResult([], ["scratch/x.py"], []))]
    assert diff_run(roots.data, "r", None, skip_dirs=[work / "scratch"]) == [("cwd", _empty())]


def test_a_root_without_a_snapshot_is_not_compared(roots, work, monkeypatch):
    seed_train(roots)
    monkeypatch.setattr(rules, "MAX_FILES", 0)  # every root is too large to snapshot: a warning
    _train(roots, work)
    with pytest.raises(ValidationFailed, match="^not_found:.*snapshot"):
        diff_run(roots.data, "r", None)


def test_a_run_without_a_training_record_is_not_found(roots):
    with pytest.raises(ValidationFailed, match="^not_found:") as ei:
        diff_run(roots.data, "never-trained", None)
    assert ei.value.fields == {"run": "never-trained"}


def test_an_attempt_without_a_code_section_is_not_found(roots, work):
    seed_train(roots)
    _train(roots, work)
    record = load_record(roots.data, "r")
    old = record.attempts[0].model_copy(update={"env": None})  # a run from before env.json
    save_record(roots.data, record.model_copy(update={"attempts": [old]}))
    with pytest.raises(ValidationFailed, match="^not_found:.*code"):
        diff_run(roots.data, "r", None)
