import builtins
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import helpers
from vcp.artifact.writer import _json_bytes
from vcp.code import rules, scan
from vcp.code.scan import (
    IndexEntry,
    RootScan,
    identity_sha256,
    index_jsonl_bytes,
    root_kind,
    scan_root,
    select_kept,
    snapshot_json_bytes,
)
from vcp.core.hashing import sha256_file

SA = hashlib.sha256(b"a").hexdigest()
SB = hashlib.sha256(b"b").hexdigest()

pytestmark = pytest.mark.usefixtures("isolated_git")
KINDS = pytest.mark.parametrize("kind", ["dir", "git"])


def _put(root: Path, files: dict[str, bytes | str]) -> Path:
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return root


def _root(kind: str, path: Path, tracked: dict, loose: dict | None = None) -> Path:
    """A root of ``kind``: a git repository whose commit holds ``tracked`` (plus ``loose`` files
    left untracked), or a plain directory holding both."""
    if kind == "git":
        files = {k: v if isinstance(v, bytes) else v.encode() for k, v in tracked.items()}
        helpers.git_repo(path, files)
        return _put(path, loose or {})
    return _put(path, {**tracked, **(loose or {})})


def _never_open(monkeypatch, names, *, stat=False):
    """``open`` / ``Path.open`` (and with ``stat`` also ``os.stat`` / ``os.lstat``) raise
    ``AssertionError`` for a file whose name is in ``names``."""
    banned = frozenset(names)
    real_path_open, real_open = Path.open, builtins.open
    real_stat, real_lstat = os.stat, os.lstat

    def check(file):
        if isinstance(file, int):
            return
        if os.path.basename(os.fsdecode(file)) in banned:
            raise AssertionError(f"touched {file}")

    def path_open(self, *args, **kwargs):
        check(self)
        return real_path_open(self, *args, **kwargs)

    def builtin_open(file, *args, **kwargs):
        check(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(builtins, "open", builtin_open)
    if stat:

        def guarded(real):
            def call(path, *args, **kwargs):
                check(path)
                return real(path, *args, **kwargs)

            return call

        monkeypatch.setattr(os, "stat", guarded(real_stat))
        monkeypatch.setattr(os, "lstat", guarded(real_lstat))


def _paths(s: RootScan) -> list[str]:
    return [e.path for e in s.entries]


# -- the walk and the skip rules ---------------------------------------------------------------


def test_skipped_paths_are_neither_read_nor_listed(tmp_path, monkeypatch):
    r = _put(
        tmp_path / "r",
        {
            "ok.py": "x = 1\n",
            ".venv/x.py": "",
            ".git/h.py": "",
            ".claude/c.py": "",
            "__pycache__/m.py": "",
            "node_modules/n.py": "",
            "pkg.egg-info/p.py": "",
            "env/pyvenv.cfg": "home = x\n",
            "env/lib/q.py": "",
            "data/d.py": "",
            "configs/c.py": "",
        },
    )
    _never_open(monkeypatch, {"x.py", "h.py", "c.py", "m.py", "n.py", "p.py", "q.py", "d.py"})
    scanned = []
    real_scandir = os.scandir

    def spy(path="."):
        scanned.append(os.path.basename(os.fspath(path)))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", spy)
    s = scan_root(r, kind="dir", skip_dirs=[r / "data", r / "configs"])
    assert _paths(s) == ["ok.py"] and s.excluded_paths == () and s.reason is None
    assert scanned == ["r"]  # nothing skipped was even listed


def test_skipped_paths_in_a_git_root_are_neither_read_nor_listed(tmp_path, monkeypatch):
    r = helpers.git_repo(
        tmp_path / "r",
        {
            "ok.py": b"x = 1\n",
            ".github/w.yaml": b"a: 1\n",
            "pkg.egg-info/p.py": b"",
            "data/d.py": b"",
            "env/pyvenv.cfg": b"home = x\n",
        },
    )
    _put(r, {".venv/x.py": "", "__pycache__/m.py": "", "env/lib/q.py": "", "configs/c.py": ""})
    _never_open(monkeypatch, {"w.yaml", "p.py", "d.py", "x.py", "m.py", "q.py", "c.py"})
    s = scan_root(r, kind="git", skip_dirs=[r / "data", r / "configs"])
    assert _paths(s) == ["ok.py"] and s.excluded_paths == () and s.untracked_code == ()


def test_the_walk_does_not_descend_into_skip_dirs_or_count_them(tmp_path, monkeypatch):
    """A plain root that holds the data root: its entries are not walked, so a huge data root
    cannot make the code root too_large."""
    r = _put(tmp_path / "r", {"ok.py": "x = 1\n"})
    _put(r / "data", {f"f{i}.py": "" for i in range(10)})
    monkeypatch.setattr(rules, "MAX_WALK_ENTRIES", 3)
    assert scan_root(r, kind="dir").reason == "too_large"  # the control: walked, it is too many
    s = scan_root(r, kind="dir", skip_dirs=[r / "data"])
    assert s.reason is None and _paths(s) == ["ok.py"]


def test_a_walk_past_the_entry_limit_is_too_large(tmp_path, monkeypatch):
    r = _put(tmp_path / "r", {f"{n}.py": "" for n in "abcde"})
    monkeypatch.setattr(rules, "MAX_WALK_ENTRIES", 3)
    s = scan_root(r, kind="dir")
    assert (s.reason, s.entries) == ("too_large", ())
    assert s.identity_sha256 is None and s.snapshot_id is None
    monkeypatch.setattr(rules, "MAX_WALK_ENTRIES", 5)
    assert scan_root(r, kind="dir").reason is None  # exactly the limit is still within it


def test_a_root_that_is_not_a_directory_is_not_found(tmp_path):
    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
    for missing in (tmp_path / "nope", tmp_path / "f.py"):
        for kind in ("dir", "git"):
            s = scan_root(missing, kind=kind)
            assert (s.reason, s.entries, s.untracked_code, s.excluded_paths) == (
                "not_found",
                (),
                (),
                (),
            )
            assert s.root == missing and s.kind == kind
            assert (s.identity_sha256, s.snapshot_id) == (None, None)


def test_nested_paths_are_relative_posix(tmp_path):
    r = _put(tmp_path / "r", {"a/b/c.py": "x = 1\n", "a/d.py": "", "top.py": ""})
    assert _paths(scan_root(r, kind="dir")) == ["a/b/c.py", "a/d.py", "top.py"]


def test_a_plain_directory_takes_code_only(tmp_path):
    r = _put(tmp_path / "r", {n: "x" for n in ["a.py", "b.cu", "c.yaml", "d.json", "e.ipynb"]})
    s = scan_root(r, kind="dir")
    assert _paths(s) == ["a.py", "b.cu"] and s.untracked_code == ()


@KINDS
def test_secret_files_are_never_opened(kind, tmp_path, monkeypatch):
    r = _root(
        kind,
        tmp_path / "r",
        {"train.py": "x = 1\n", "kaggle.json": "{}"},
        {"deploy.pem": "KEY", "my_secret.py": "y = 2\n"},
    )
    _never_open(monkeypatch, {"deploy.pem", "kaggle.json", "my_secret.py"}, stat=True)
    s = scan_root(r, kind=kind)
    assert _paths(s) == ["train.py"]
    assert sorted(s.excluded_paths) == ["deploy.pem", "kaggle.json", "my_secret.py"]


@KINDS
def test_symlinks_are_not_followed(kind, tmp_path):
    outside = _put(tmp_path / "outside", {"o.py": "y = 2\n"})
    r = _root(kind, tmp_path / "r", {"real.py": "x = 1\n"})
    try:
        os.symlink(r / "real.py", r / "link.py")
        os.symlink(outside, r / "linkdir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this OS cannot create symlinks here")
    s = scan_root(r, kind=kind)
    assert _paths(s) == ["real.py"]
    assert {"link.py", "linkdir"} <= set(s.excluded_paths)


@KINDS
@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows thing")
def test_junctions_are_not_followed(kind, tmp_path):
    import _winapi

    outside = _put(tmp_path / "outside", {"o.py": "y = 2\n"})
    r = _root(kind, tmp_path / "r", {"real.py": "x = 1\n"})
    _winapi.CreateJunction(str(outside), str(r / "junc"))
    s = scan_root(r, kind=kind)
    assert _paths(s) == ["real.py"]
    assert "junc" in s.excluded_paths


# -- a git root ---------------------------------------------------------------------------------


def test_tracked_code_and_config_come_from_the_worktree(tmp_path):
    r = helpers.git_repo(tmp_path / "r", {"train.py": b"v = 1\n", "cfg.yaml": b"a: 1\n"})
    (r / "train.py").write_bytes(b"v = 2  # dirty\n")  # an uncommitted edit: the file as it is now
    s = scan_root(r, kind="git")
    assert s.kind == "git" and {e.path: e.sha256 for e in s.entries} == {
        "cfg.yaml": sha256_file(r / "cfg.yaml"),
        "train.py": sha256_file(r / "train.py"),
    }
    assert s.untracked_code == ()
    assert {e.path: e.bytes for e in s.entries} == {
        "cfg.yaml": len(b"a: 1\n"),
        "train.py": len(b"v = 2  # dirty\n"),
    }


def test_every_tracked_config_kind_is_taken(tmp_path):
    names = ["requirements.txt", "Makefile", "nb.ipynb", "run.sh", "uv.lock", "x.toml", "ext.so"]
    r = helpers.git_repo(tmp_path / "r", {n: b"x" for n in [*names, "README.md", "d.csv"]})
    assert _paths(scan_root(r, kind="git")) == sorted(names)


def test_untracked_code_is_read_and_untracked_config_never_opened(tmp_path, monkeypatch):
    never = [
        "args.yaml",
        "hparams.yaml",
        "config.json",
        "metrics.json",
        "notes.txt",
        "x.csv",
        "run.ipynb",
        "ext.so",
        "run.sh",
    ]
    r = helpers.git_repo(tmp_path / "r", {"train.py": b"v = 1\n"})
    _put(r, {"helper.py": "h = 1\n", "k.cu": "__global__ void k() {}\n", **{n: "x" for n in never}})
    _never_open(monkeypatch, set(never))
    s = scan_root(r, kind="git")
    assert _paths(s) == ["helper.py", "k.cu", "train.py"]
    assert s.untracked_code == ("helper.py", "k.cu")


def test_ignored_files_are_never_opened(tmp_path, monkeypatch):
    r = helpers.git_repo(tmp_path / "r", {"train.py": b"v = 1\n", ".gitignore": b"gen.py\nout/\n"})
    _put(r, {"gen.py": "g = 1\n", "out/o.py": "o = 1\n", "kept.py": "k = 1\n"})
    _never_open(monkeypatch, {"gen.py", "o.py"})
    s = scan_root(r, kind="git")
    assert _paths(s) == ["kept.py", "train.py"]


def test_a_subdirectory_root_lists_only_its_subtree(tmp_path):
    repo = helpers.git_repo(tmp_path / "repo", {"a/x.py": b"x = 1\n", "b/y.py": b"y = 1\n"})
    _put(repo, {"a/new.py": "n = 1\n", "b/other.py": "o = 1\n"})
    s = scan_root(repo / "a", kind="git")
    assert _paths(s) == ["new.py", "x.py"] and s.untracked_code == ("new.py",)
    assert s.root == repo / "a"


def test_nested_roots_split_innermost(tmp_path):
    repo = helpers.git_repo(
        tmp_path / "repo",
        {"main.py": b"m = 1\n", "vendor/lib.py": b"l = 1\n", "vendor/v/w.py": b"w = 1\n"},
    )
    outer = scan_root(repo, kind="git", skip_dirs=[repo / "vendor"])
    assert _paths(outer) == ["main.py"]
    assert _paths(scan_root(repo / "vendor", kind="git")) == ["lib.py", "v/w.py"]


def test_tracked_but_deleted_and_submodules_are_not_listed(tmp_path):
    other = helpers.git_repo(tmp_path / "other", {"inner.py": b"i = 1\n"})
    r = helpers.git_repo(tmp_path / "r", {"keep.py": b"k = 1\n", "gone.py": b"g = 1\n"})
    helpers.git(r, "-c", "protocol.file.allow=always", "submodule", "add", str(other), "lib.py")
    (r / "gone.py").unlink()
    s = scan_root(r, kind="git")
    assert _paths(s) == ["keep.py"]  # no gone.py (deleted), no lib.py (a submodule, not a file)


def test_a_nested_repository_is_not_listed(tmp_path):
    r = helpers.git_repo(tmp_path / "r", {"keep.py": b"k = 1\n"})
    helpers.git_repo(r / "inner", {"i.py": b"i = 1\n"})  # an untracked repository inside it
    assert _paths(scan_root(r, kind="git")) == ["keep.py"]


def test_a_non_ascii_tracked_name_is_listed_by_its_real_name(tmp_path):
    r = helpers.git_repo(tmp_path / "r", {"資料處理.py": b"d = 1\n"})
    s = scan_root(r, kind="git")
    assert _paths(s) == ["資料處理.py"]
    assert s.entries[0].sha256 == sha256_file(r / "資料處理.py")


def test_git_variables_of_another_repository_are_ignored(tmp_path, monkeypatch):
    other = helpers.git_repo(tmp_path / "other", {"o.py": b"o = 1\n"})
    r = helpers.git_repo(tmp_path / "r", {"mine.py": b"m = 1\n"})
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert _paths(scan_root(r, kind="git")) == ["mine.py"]
    assert root_kind(r) == "git"


def test_a_pyvenv_directory_inside_a_git_root_is_skipped(tmp_path):
    r = helpers.git_repo(tmp_path / "r", {"ok.py": b"x = 1\n"})
    _put(r, {"env/pyvenv.cfg": "home = x\n", "env/lib/q.py": "q = 1\n", "env/lib/deep/z.py": ""})
    s = scan_root(r, kind="git")
    assert _paths(s) == ["ok.py"] and s.excluded_paths == () and s.untracked_code == ()


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows thing")
def test_files_behind_a_linked_directory_are_never_taken(tmp_path):
    """Whether git descends into a junction or lists it as one path, nothing behind it is read."""
    import _winapi

    outside = _put(tmp_path / "outside", {"o.py": "y = 2\n"})
    r = helpers.git_repo(tmp_path / "r", {"real.py": b"x = 1\n"})
    _winapi.CreateJunction(str(outside), str(r / "lib"))
    s = scan_root(r, kind="git")
    assert _paths(s) == ["real.py"] and "lib" in s.excluded_paths


def test_git_that_fails_or_speaks_non_utf8_marks_the_root_unreadable(tmp_path, monkeypatch):
    r = helpers.git_repo(tmp_path / "r", {"ok.py": b"x = 1\n"})

    def fake(stdout, code=0):
        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, code, stdout=stdout, stderr=b"boom")

        return run

    monkeypatch.setattr(scan.subprocess, "run", fake(b"\xff\xfe.py\0"))
    assert scan_root(r, kind="git").reason == "unreadable"
    monkeypatch.setattr(scan.subprocess, "run", fake(b"", 128))
    assert scan_root(r, kind="git").reason == "unreadable"

    def cannot_start(command, **kwargs):
        raise OSError("no exec")

    monkeypatch.setattr(scan.subprocess, "run", cannot_start)
    assert scan_root(r, kind="git").reason == "unreadable"
    monkeypatch.setattr(scan.shutil, "which", lambda name: None)
    assert scan_root(r, kind="git").reason == "unreadable"


def test_a_git_scan_of_a_plain_directory_is_unreadable(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    r = _put(tmp_path / "plain", {"a.py": "x = 1\n"})
    s = scan_root(r, kind="git")
    assert (s.reason, s.entries) == ("unreadable", ())


# -- root_kind ----------------------------------------------------------------------------------


def test_root_kind_is_git_inside_a_work_tree_and_dir_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = helpers.git_repo(tmp_path / "repo", {"sub/x.py": b"x = 1\n"})
    plain = _put(tmp_path / "plain", {"a.py": "x = 1\n"})
    assert root_kind(repo) == "git"
    assert root_kind(repo / "sub") == "git"
    assert root_kind(plain) == "dir"
    assert root_kind(tmp_path / "missing") == "dir"


def test_root_kind_ignores_git_variables_of_another_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    other = helpers.git_repo(tmp_path / "other", {"o.py": b"o = 1\n"})
    plain = _put(tmp_path / "plain", {"a.py": "x = 1\n"})
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert root_kind(plain) == "dir"


def _git_unavailable(monkeypatch, how):
    """git is not on PATH, or it refuses the repository (dubious ownership on a WSL mount, a
    Docker bind mount, a volume that cannot hold git's lock files)."""
    if how == "missing":
        monkeypatch.setattr(scan.shutil, "which", lambda name: None)
        return

    def refuses(command, **kwargs):
        err = b"fatal: detected dubious ownership in repository"
        return subprocess.CompletedProcess(command, 128, stdout=b"", stderr=err)

    monkeypatch.setattr(scan.subprocess, "run", refuses)


@pytest.mark.parametrize("how", ["missing", "refuses"])
def test_a_repository_git_cannot_read_is_still_git_and_is_not_scanned(how, tmp_path, monkeypatch):
    """Scanned as a plain directory it would read and keep the gitignored CODE files (spec §7):
    the root stays ``git`` and the scan fails closed."""
    repo = helpers.git_repo(tmp_path / "repo", {"train.py": b"v = 1\n", ".gitignore": b"gen.py\n"})
    _put(repo, {"gen.py": "g = 1\n", "loose.py": "l = 1\n"})
    _never_open(monkeypatch, {"train.py", "gen.py", "loose.py"})
    _git_unavailable(monkeypatch, how)
    kind = root_kind(repo)
    assert kind == "git"
    s = scan_root(repo, kind=kind)
    assert (s.reason, s.entries, s.untracked_code, s.excluded_paths) == ("unreadable", (), (), ())
    assert (s.identity_sha256, s.snapshot_id) == (None, None)
    assert root_kind(repo / "pkg" / "not-yet-created") == "git"  # below a work tree, too


def test_a_dot_git_file_above_the_path_makes_it_git(tmp_path, monkeypatch):
    """A linked worktree or a submodule checkout has a ``.git`` file, not a directory."""
    top = _put(tmp_path / "wt", {".git": "gitdir: ../elsewhere/.git/worktrees/wt\n"})
    deep = _put(top / "sub", {"deep/a.py": "x = 1\n"}) / "deep"
    _git_unavailable(monkeypatch, "missing")
    assert root_kind(top) == "git"
    assert root_kind(deep) == "git"
    assert scan_root(deep, kind=root_kind(deep)).reason == "unreadable"


@pytest.mark.parametrize("how", ["missing", "refuses"])
def test_a_directory_with_no_dot_git_above_it_is_dir_even_without_git(how, tmp_path, monkeypatch):
    plain = _put(tmp_path / "plain", {"a.py": "x = 1\n", "sub/b.py": "y = 2\n"})
    _git_unavailable(monkeypatch, how)
    assert root_kind(plain) == "dir" and root_kind(plain / "sub") == "dir"
    assert _paths(scan_root(plain, kind=root_kind(plain))) == ["a.py", "sub/b.py"]


def test_every_git_call_runs_in_the_git_environment(tmp_path, monkeypatch):
    seen = []
    real_run = subprocess.run

    def spy(command, **kwargs):
        seen.append(kwargs.get("env"))
        return real_run(command, **kwargs)

    repo = helpers.git_repo(tmp_path / "repo", {"x.py": b"x = 1\n"})
    monkeypatch.setattr(scan.subprocess, "run", spy)
    root_kind(repo)
    scan_root(repo, kind="git")
    assert len(seen) == 3  # rev-parse, ls-files --cached, ls-files --others
    assert all(env is not None and env["GIT_OPTIONAL_LOCKS"] == "0" for env in seen)


# -- limits -------------------------------------------------------------------------------------


def test_capped_keep_is_greedy_in_path_order(tmp_path, monkeypatch):
    r = _put(tmp_path / "r", {"big.py": b"x" * (1024**2 + 1), "ok.py": b"y" * 1024**2})
    capped = {e.path: e.kept for e in scan_root(r, kind="dir").entries}
    assert capped == {"big.py": False, "ok.py": True}  # the limit itself is kept
    assert all(e.kept for e in scan_root(r, kind="dir", keep="all").entries)

    r = _put(tmp_path / "g", {"a.py": b"1234", "b.py": b"1234", "c.py": b"1234", "d.py": b"12"})
    monkeypatch.setattr(rules, "KEEP_ROOT_LIMIT", 10)
    first = scan_root(r, kind="dir")
    assert [e.kept for e in first.entries] == [True, True, False, True]
    assert scan_root(r, kind="dir") == first  # reproducible
    assert (first.files, first.bytes, first.kept, first.not_kept) == (4, 14, 3, 1)


def test_too_many_files_or_bytes_is_too_large_and_hashes_nothing(tmp_path, monkeypatch):
    def boom(path):
        raise AssertionError(f"hashed {path}")

    monkeypatch.setattr(scan, "sha256_file", boom)
    r = _put(tmp_path / "r", {"a.py": "1", "b.py": "2", "c.py": "3"})
    with monkeypatch.context() as m:
        m.setattr(rules, "MAX_FILES", 2)
        s = scan_root(r, kind="dir")
    assert (s.reason, s.entries) == ("too_large", ())
    assert s.snapshot_id is None and s.identity_sha256 is None

    r = _put(tmp_path / "big", {"a.py": "123456"})
    monkeypatch.setattr(rules, "MAX_HASH_BYTES", 5)
    assert scan_root(r, kind="dir").reason == "too_large"


def test_the_limits_are_inclusive(tmp_path, monkeypatch):
    r = _put(tmp_path / "r", {"a.py": "123", "b.py": "123"})
    monkeypatch.setattr(rules, "MAX_FILES", 2)
    monkeypatch.setattr(rules, "MAX_HASH_BYTES", 6)
    assert scan_root(r, kind="dir").reason is None
    monkeypatch.setattr(rules, "MAX_HASH_BYTES", 5)
    assert scan_root(r, kind="dir").reason == "too_large"


def test_an_unreadable_file_marks_the_root_unreadable(tmp_path, monkeypatch):
    r = _put(tmp_path / "r", {"a.py": "1", "b.py": "2"})
    real = scan.sha256_file

    def denied(path):
        if Path(path).name == "a.py":
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(scan, "sha256_file", denied)
    s = scan_root(r, kind="dir")
    assert (s.reason, s.entries) == ("unreadable", ())
    assert s.snapshot_id is None


def test_a_file_that_vanishes_before_it_is_hashed_is_dropped(tmp_path, monkeypatch):
    r = _put(tmp_path / "r", {"a.py": "1", "b.py": "2"})
    real = scan.sha256_file

    def vanished(path):
        if Path(path).name == "a.py":
            raise FileNotFoundError(path)
        return real(path)

    monkeypatch.setattr(scan, "sha256_file", vanished)
    s = scan_root(r, kind="dir")
    assert s.reason is None and _paths(s) == ["b.py"]


# -- identity -----------------------------------------------------------------------------------


def test_identity_is_the_documented_sha():
    es = [IndexEntry("b.py", SB, 2, True), IndexEntry("a.py", SA, 1, False)]
    assert (
        identity_sha256(es)
        == hashlib.sha256(f"vcp-code/1\n{SA} 1 a.py\n{SB} 2 b.py\n".encode()).hexdigest()
    )
    assert identity_sha256([replace(e, kept=not e.kept) for e in es]) == identity_sha256(es)
    assert identity_sha256([]) == hashlib.sha256(b"vcp-code/1\n").hexdigest()


def test_identity_orders_by_path_and_counts_the_bytes_the_path_and_the_sha():
    a, b = IndexEntry("a.py", SA, 1), IndexEntry("b.py", SB, 2)
    assert identity_sha256([a, b]) == identity_sha256([b, a])
    assert identity_sha256([a]) != identity_sha256([replace(a, bytes=2)])
    assert identity_sha256([a]) != identity_sha256([replace(a, path="c.py")])
    assert identity_sha256([a]) != identity_sha256([replace(a, sha256=SB)])


def test_one_content_in_two_places_has_one_snapshot_json(tmp_path):
    files = {"a.py": "x = 1\n", "pkg/b.py": "y = 2\n", "big.py": b"z" * (1024**2 + 1)}
    r1, r2 = _put(tmp_path / "r1", files), _put(tmp_path / "deep" / "r2", files)
    four = [scan_root(r, kind="dir", keep=keep) for r in (r1, r2) for keep in ("capped", "all")]
    assert len({snapshot_json_bytes(s.entries) for s in four}) == 1
    assert len({s.snapshot_id for s in four}) == 1
    assert {e.kept for e in four[0].entries} == {True, False}  # capped really dropped one
    doc = json.loads(snapshot_json_bytes(four[0].entries))
    assert list(doc) == ["schema_version", "rules", "identity_sha256", "files", "bytes"]
    assert doc["files"] == 3 and doc["bytes"] == 6 + 6 + 1024**2 + 1
    assert four[0].snapshot_id == f"code-{doc['identity_sha256'][:16]}"


def test_snapshot_json_is_the_bytes_an_artifact_writer_writes():
    es = [IndexEntry("a.py", SA, 1, True), IndexEntry("b.py", SB, 2, False)]
    doc = {
        "schema_version": 1,
        "rules": 1,
        "identity_sha256": identity_sha256(es),
        "files": 2,
        "bytes": 3,
    }
    assert snapshot_json_bytes(es) == _json_bytes(doc)
    assert snapshot_json_bytes(es).endswith(b"}\n") and b"\r" not in snapshot_json_bytes(es)


def test_index_jsonl_is_one_sorted_line_per_file():
    es = [IndexEntry("資料.py", SB, 2, False), IndexEntry("a.py", SA, 1, True)]
    out = index_jsonl_bytes(es)
    assert (
        out
        == (
            f'{{"path": "a.py", "sha256": "{SA}", "bytes": 1, "kept": true}}\n'
            f'{{"path": "資料.py", "sha256": "{SB}", "bytes": 2, "kept": false}}\n'
        ).encode()
    )
    assert index_jsonl_bytes([]) == b""


def test_a_code_edit_moves_the_identity_and_a_readme_does_not(tmp_path):
    r = _put(tmp_path / "r", {"a.py": "x = 1\n", "README.md": "# hi\n", "notes.txt": "n\n"})
    before = scan_root(r, kind="dir")
    (r / "README.md").write_text("# changed\n", encoding="utf-8")
    (r / "notes.txt").write_text("changed\n", encoding="utf-8")
    assert scan_root(r, kind="dir").snapshot_id == before.snapshot_id
    (r / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert scan_root(r, kind="dir").snapshot_id != before.snapshot_id


# -- select_kept and the RootScan counts --------------------------------------------------------


def test_select_kept_all_keeps_everything_and_capped_ignores_the_input_flags(monkeypatch):
    es = [IndexEntry("b.py", SB, 5, True), IndexEntry("a.py", SA, 5, False)]
    assert [(e.path, e.kept) for e in select_kept(es, "all")] == [("a.py", True), ("b.py", True)]
    monkeypatch.setattr(rules, "KEEP_FILE_LIMIT", 4)
    assert [(e.path, e.kept) for e in select_kept(es, "capped")] == [
        ("a.py", False),
        ("b.py", False),
    ]
    assert isinstance(select_kept(es, "all"), tuple)


def test_a_file_that_is_not_kept_does_not_use_up_the_root_budget(monkeypatch):
    monkeypatch.setattr(rules, "KEEP_FILE_LIMIT", 6)
    monkeypatch.setattr(rules, "KEEP_ROOT_LIMIT", 10)
    es = [IndexEntry("a.py", SA, 7), IndexEntry("b.py", SB, 6), IndexEntry("c.py", SA, 4)]
    assert [e.kept for e in select_kept(es, "capped")] == [False, True, True]


def test_a_root_scan_with_a_reason_has_no_identity(tmp_path):
    s = RootScan(tmp_path, "dir", (), (), (), "too_large")
    assert (s.identity_sha256, s.snapshot_id) == (None, None)
    assert (s.files, s.bytes, s.kept, s.not_kept) == (0, 0, 0, 0)
    ok = RootScan(tmp_path, "dir", (IndexEntry("a.py", SA, 1, True),), (), (), None)
    assert ok.identity_sha256 == identity_sha256(ok.entries)
    assert ok.snapshot_id == f"code-{ok.identity_sha256[:16]}"


def test_the_shared_path_helpers_are_reachable_from_scan(tmp_path):
    assert scan.path_key(tmp_path) == os.path.normcase(str(tmp_path.resolve()))
    assert scan.is_under(tmp_path / "a", tmp_path) and not scan.is_under(tmp_path, tmp_path / "a")


def test_link_detection_needs_no_windows_only_names(monkeypatch):
    """``st_reparse_tag`` and ``stat.IO_REPARSE_TAG_MOUNT_POINT`` exist only on Windows."""
    monkeypatch.delattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", raising=False)
    assert scan._is_link(SimpleNamespace(st_mode=stat.S_IFLNK | 0o777))
    assert not scan._is_link(SimpleNamespace(st_mode=stat.S_IFREG | 0o644))
    assert scan._is_link(SimpleNamespace(st_mode=stat.S_IFDIR, st_reparse_tag=0xA0000003))
    assert not scan._is_link(SimpleNamespace(st_mode=stat.S_IFREG, st_reparse_tag=0))


# -- walk_files: the plain-directory walk, for callers that are not a scan ------------------------


def test_walk_files_lists_what_a_directory_scan_walks(tmp_path):
    r = _put(
        tmp_path / "r",
        {"b.py": "", "a.txt": "", "pkg/c.py": "", ".hidden/x.py": "", "__pycache__/y.py": ""},
    )
    _put(r / "venv", {"pyvenv.cfg": "", "lib/z.py": ""})
    _put(r / "data", {"d.py": ""})
    assert scan.walk_files(r, skip_dirs=[r / "data"]) == ["a.txt", "b.py", "pkg/c.py"]
    assert scan.walk_files(r) == ["a.txt", "b.py", "data/d.py", "pkg/c.py"]  # no rule applied


def test_walk_files_does_not_follow_links(tmp_path):
    outside = _put(tmp_path / "outside", {"o.py": "y = 2\n"})
    r = _put(tmp_path / "r", {"real.py": "x = 1\n"})
    try:
        os.symlink(r / "real.py", r / "link.py")
        os.symlink(outside, r / "linkdir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this OS cannot create symlinks here")
    assert scan.walk_files(r) == ["real.py"]


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows thing")
def test_walk_files_does_not_enter_a_junction(tmp_path):
    import _winapi

    outside = _put(tmp_path / "outside", {"o.py": "y = 2\n"})
    r = _put(tmp_path / "r", {"real.py": "x = 1\n"})
    _winapi.CreateJunction(str(outside), str(r / "junc"))
    assert scan.walk_files(r) == ["real.py"]
    assert scan.is_linked(r / "junc") and not scan.is_linked(r / "real.py")


def test_walk_files_stops_as_a_scan_does(tmp_path, monkeypatch):
    r = _put(tmp_path / "r", {f"{n}.py": "" for n in "abcde"})
    monkeypatch.setattr(rules, "MAX_WALK_ENTRIES", 3)
    with pytest.raises(scan.ScanStopped) as ei:
        scan.walk_files(r)
    assert ei.value.reason == "too_large"
    monkeypatch.setattr(rules, "MAX_WALK_ENTRIES", 5)
    assert len(scan.walk_files(r)) == 5

    real = os.scandir

    def denied(path="."):
        if Path(path).name == "locked":
            raise PermissionError(13, "denied")
        return real(path)

    r2 = _put(tmp_path / "r2", {"a.py": "", "locked/x.py": ""})
    monkeypatch.setattr(scan.os, "scandir", denied)
    with pytest.raises(scan.ScanStopped) as ei:
        scan.walk_files(r2)
    assert ei.value.reason == "unreadable"


def test_a_directory_that_vanishes_during_the_walk_is_not_an_error(tmp_path, monkeypatch):
    r = _put(tmp_path / "r", {"a.py": "", "gone/b.py": ""})
    real = os.scandir

    def vanished(path="."):
        if Path(path).name == "gone":
            raise FileNotFoundError(2, "gone")
        return real(path)

    monkeypatch.setattr(scan.os, "scandir", vanished)
    assert scan.walk_files(r) == ["a.py"]


def test_is_linked_is_false_for_a_path_that_is_gone(tmp_path):
    assert not scan.is_linked(tmp_path / "nowhere") and not scan.is_linked(tmp_path)
