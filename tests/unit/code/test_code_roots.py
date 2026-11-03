import os
import shutil
import subprocess
from pathlib import Path

import pytest

import helpers
from vcp.code import generated
from vcp.code.generated import register_generated
from vcp.code.roots import (
    Editable,
    PlannedRoot,
    canonical_name,
    checkpoint_exclusions,
    clean_url,
    distribution_record,
    editable_dir,
    editables_of,
    generated_exclusions,
    plan_roots,
    repo_top,
    script_of,
    skip_dirs_for,
)
from vcp.code.scan import root_kind
from vcp.code.schema import DirectUrl, Distribution

pytestmark = pytest.mark.usefixtures("isolated_git")

GENERATED_VARS = ("TORCHINDUCTOR_CACHE_DIR", "TORCH_EXTENSIONS_DIR", "TRITON_CACHE_DIR")


def _dir(path: Path, files: tuple[str, ...] = ()) -> Path:
    """A plain directory (tmp_path is under no repository) holding empty ``files``."""
    path.mkdir(parents=True, exist_ok=True)
    for name in files:
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x = 1\n")
    return path.resolve()


def _head(repo: Path) -> str:
    return helpers.git(repo, "rev-parse", "HEAD").strip()


def _labels(planned) -> list[str]:
    return [r.label for r in planned]


def _plan(cwd, command=(), editables=(), extra=()):
    return plan_roots(cwd=cwd, command=list(command), editables=list(editables), extra=list(extra))


def _git_unavailable(monkeypatch, how):
    """git is not on PATH, cannot be started, or refuses the repository (dubious ownership on a
    mount)."""
    if how == "missing":
        monkeypatch.setattr(shutil, "which", lambda name: None)
        return

    def refuses(command, **kwargs):
        err = b"fatal: detected dubious ownership in repository"
        return subprocess.CompletedProcess(command, 128, stdout=b"", stderr=err)

    def cannot_start(command, **kwargs):
        raise PermissionError("git cannot be started")

    monkeypatch.setattr(subprocess, "run", refuses if how == "refuses" else cannot_start)


# -- the root of the working directory, the script and the other roots ---------------------------


def test_cwd_root_is_the_repository_top(tmp_path):
    repo = helpers.git_repo(tmp_path / "repo")
    (repo / "sub").mkdir()
    assert plan_roots(cwd=repo / "sub", command=[], editables=[], extra=[]) == [
        PlannedRoot("cwd", "cwd", repo.resolve(), "git", _head(repo))
    ]


def test_cwd_outside_git_is_itself(tmp_path):
    work = _dir(tmp_path / "work")
    assert _plan(work) == [PlannedRoot("cwd", "cwd", work, "dir", None)]


def test_a_repository_without_a_commit_has_a_root_and_no_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    helpers.git(repo, "init", "-q")
    assert _plan(repo) == [PlannedRoot("cwd", "cwd", repo.resolve(), "git", None)]


@pytest.mark.parametrize("how", ["missing", "refuses", "unstartable"])
def test_a_repository_git_cannot_read_still_gives_its_top(how, tmp_path, monkeypatch):
    """The scan fails such a tree closed (``unreadable``); the plan must name the same top and
    the same kind, not fall back to the directory it was asked about."""
    repo = helpers.git_repo(tmp_path / "repo")
    (repo / "sub" / "deep").mkdir(parents=True)
    plain = _dir(tmp_path / "plain", ("a.py",))
    _git_unavailable(monkeypatch, how)
    assert repo_top(repo / "sub" / "deep") == (repo.resolve(), None)
    assert _plan(repo / "sub" / "deep") == [PlannedRoot("cwd", "cwd", repo.resolve(), "git", None)]
    assert repo_top(plain) is None
    assert _plan(plain) == [PlannedRoot("cwd", "cwd", plain, "dir", None)]


def test_a_dot_git_file_above_cwd_is_the_top_when_git_cannot_run(tmp_path, monkeypatch):
    """A linked worktree or a submodule checkout has a ``.git`` file, not a directory."""
    top = _dir(tmp_path / "wt", (".git",))
    (top / "sub").mkdir()
    _git_unavailable(monkeypatch, "missing")
    assert repo_top(top / "sub") == (top, None)
    assert _plan(top / "sub") == [PlannedRoot("cwd", "cwd", top, "git", None)]


def test_repo_top_agrees_with_root_kind(tmp_path, monkeypatch):
    repo = helpers.git_repo(tmp_path / "repo")
    (repo / "sub").mkdir()
    plain = _dir(tmp_path / "plain")
    wt = _dir(tmp_path / "wt", (".git",))
    (wt / "sub").mkdir()
    for how in (None, "missing", "refuses", "unstartable"):
        if how:
            _git_unavailable(monkeypatch, how)
        for path in (repo, repo / "sub", plain, wt, wt / "sub"):
            assert (repo_top(path) is not None) == (root_kind(path) == "git"), (how, path)


def test_a_script_in_another_repository_is_its_own_root(tmp_path):
    work = helpers.git_repo(tmp_path / "work")
    other = helpers.git_repo(tmp_path / "other", {"t.py": b"x = 1\n"})
    planned = _plan(work, ["python", str(other / "t.py")])
    assert _labels(planned) == ["cwd", "script"]
    assert planned[1] == PlannedRoot("script", "script", other.resolve(), "git", _head(other))


def test_a_script_in_a_subdirectory_of_another_repository_has_that_repository_top(tmp_path):
    work = _dir(tmp_path / "work")
    other = helpers.git_repo(tmp_path / "other", {"tools/t.py": b"x = 1\n"})
    planned = _plan(work, ["python", str(other / "tools" / "t.py")])
    assert planned[1].path == other.resolve()


def test_a_relative_script_is_found_from_cwd(tmp_path, monkeypatch):
    elsewhere = _dir(tmp_path / "deeper" / "elsewhere")
    monkeypatch.chdir(elsewhere)  # the wrapper's own directory is not where the script is
    work = helpers.git_repo(tmp_path / "work", {"tools/run.py": b"x = 1\n"})
    assert _labels(_plan(work, ["python", "tools/run.py"])) == ["cwd"]
    a = _dir(tmp_path / "a")
    b = _dir(tmp_path / "b", ("train.py",))
    planned = _plan(a, ["python", "../b/train.py"])
    assert _labels(planned) == ["cwd", "script"]
    assert planned[1] == PlannedRoot("script", "script", b, "dir", None)


def test_the_first_script_outside_every_planned_root_is_the_script_root(tmp_path):
    work = _dir(tmp_path / "work", ("run.py",))
    other = _dir(tmp_path / "other", ("train.py",))
    planned = _plan(work, ["python", "run.py", "../other/train.py"])
    assert _labels(planned) == ["cwd", "script"]
    assert planned[1].path == other


def test_a_script_under_an_editable_root_adds_no_root(tmp_path):
    work = _dir(tmp_path / "work")
    pkg = _dir(tmp_path / "pkg", ("tools/run.py",))
    planned = _plan(work, ["python", str(pkg / "tools" / "run.py")], [Editable("pkg", pkg)])
    assert _labels(planned) == ["cwd", "dist-pkg"]


def test_the_script_root_comes_right_after_cwd(tmp_path):
    work = _dir(tmp_path / "work")
    pkg = _dir(tmp_path / "pkg")
    extra = _dir(tmp_path / "extra")
    script = _dir(tmp_path / "s", ("go.py",)) / "go.py"
    planned = _plan(work, ["python", str(script)], [Editable("pkg", pkg)], [extra])
    assert _labels(planned) == ["cwd", "script", "dist-pkg", "root-1"]
    assert [r.source for r in planned] == ["cwd", "script", "editable", "extra"]


def test_an_editable_root_is_its_own_directory_not_its_repository_top(tmp_path):
    repo = helpers.git_repo(tmp_path / "repo", {"src/pkg/__init__.py": b"x = 1\n"})
    work = _dir(tmp_path / "work")
    planned = _plan(work, editables=[Editable("pkg", repo / "src" / "pkg")])
    assert planned[1] == PlannedRoot(
        "dist-pkg", "editable", (repo / "src" / "pkg").resolve(), "git", _head(repo)
    )


def test_an_editable_at_the_working_directory_is_one_root(tmp_path):
    work = _dir(tmp_path / "work")
    assert _labels(_plan(work, editables=[Editable("vcp", work)])) == ["cwd"]


def test_two_spellings_of_one_root_are_one_root(tmp_path):
    work = _dir(tmp_path / "work")
    (work / "sub").mkdir()
    assert _labels(_plan(work, extra=[work / ".", work / "sub" / ".."])) == ["cwd"]
    other = _dir(tmp_path / "other")
    planned = _plan(work, extra=[work, other])
    assert _labels(planned) == ["cwd", "root-2"]  # a dropped first entry does not renumber


# -- distributions and editable installs ----------------------------------------------------------


def _raw(name="My_Pkg", url=None, *, editable=True, commit=None, record="a" * 64):
    direct = None
    if url is not None:
        direct = {"url": url, "dir_info": {"editable": editable}}
        if commit is not None:
            direct = {"url": url, "vcs_info": {"vcs": "git", "commit_id": commit}}
    return {"name": name, "version": "1.0", "record_sha256": record, "direct_url": direct}


def test_editable_roots_come_from_the_raw_url(tmp_path):
    work = _dir(tmp_path / "work")
    deep = tmp_path / ("x" * 40) / "pkg"
    deep.mkdir(parents=True)
    raw = _raw(url=deep.as_uri(), record=None)
    assert editables_of([raw]) == [Editable("My_Pkg", deep)]
    assert "<redacted>" in distribution_record(raw).direct_url.url  # what is recorded is redacted
    planned = _plan(work, editables=editables_of([raw]))
    assert _labels(planned) == ["cwd", "dist-my-pkg"]
    assert planned[1].path == deep.resolve()


def test_a_percent_sign_in_a_directory_name_is_decoded_once(tmp_path):
    odd = tmp_path / "a%20b" / "pkg"
    odd.mkdir(parents=True)
    assert odd.as_uri().count("%2520") == 1
    assert editable_dir(odd.as_uri()) == odd
    spaced = tmp_path / "with space"
    spaced.mkdir()
    assert editable_dir(spaced.as_uri()) == spaced


def test_a_localhost_file_url_is_a_local_directory(tmp_path):
    pkg = _dir(tmp_path / "pkg")
    local = "file://localhost/" + pkg.as_uri().removeprefix("file:///")
    assert editable_dir(local) == pkg


@pytest.mark.skipif(os.name != "nt", reason="a file URL with a host is a UNC path on Windows")
def test_a_file_url_with_a_host_is_a_unc_path():
    assert editable_dir("file://server/share/pkg") == Path(r"\\server\share\pkg")


@pytest.mark.parametrize("url", ["https://host.example/pkg", "git+https://host/x.git", "pkg"])
def test_only_file_urls_are_directories(url):
    assert editable_dir(url) is None


def test_a_url_that_cannot_be_read_is_no_directory_and_no_leak():
    assert editable_dir("file://[bad/x") is None
    assert clean_url("https://user:tok@[bad/x") == "<redacted>"


def test_clean_url_drops_userinfo_query_and_fragment():
    assert (
        clean_url("https://user:tok@host.example/x.git?token=abc#frag")
        == "https://host.example/x.git"
    )
    assert clean_url("https://host.example:8443/x") == "https://host.example:8443/x"
    assert clean_url("file:///C:/work/pkg") == "file:///C:/work/pkg"


def test_non_editable_or_remote_urls_are_no_roots(tmp_path):
    pkg = _dir(tmp_path / "pkg")
    rows = [
        _raw("plain", pkg.as_uri(), editable=False),
        _raw("remote", "https://host.example/pkg", editable=True),
        _raw("none", None),
        {"name": "bare", "version": "1", "record_sha256": None},  # no direct_url key at all
        {"name": "odd", "version": "1", "record_sha256": None, "direct_url": {"url": 5}},
        {
            "name": "flag",
            "version": "1",
            "record_sha256": None,
            "direct_url": {"url": pkg.as_uri(), "dir_info": {"editable": "yes"}},
        },
        {
            "version": "1",
            "record_sha256": None,
            "direct_url": {"url": pkg.as_uri(), "dir_info": {"editable": True}},
        },  # nameless
        _raw("real", pkg.as_uri()),
    ]
    assert editables_of(rows) == [Editable("real", pkg)]


def test_editables_keep_the_order_they_were_reported_in(tmp_path):
    first, second = _dir(tmp_path / "one"), _dir(tmp_path / "two")
    rows = [_raw("b", second.as_uri()), _raw("a", first.as_uri())]
    assert editables_of(rows) == [Editable("b", second), Editable("a", first)]


def test_distribution_record_cleans_the_url_and_keeps_the_facts():
    raw = _raw(
        "pkg",
        "https://user:tok@host.example/pkg.git?x=1#f",
        commit="c" * 40,
        record="d" * 64,
    )
    assert distribution_record(raw) == Distribution(
        name="pkg",
        version="1.0",
        record_sha256="d" * 64,
        direct_url=DirectUrl(
            url="https://host.example/pkg.git", editable=False, commit_id="c" * 40
        ),
    )


def test_distribution_record_of_an_editable_and_of_a_plain_distribution(tmp_path):
    pkg = _dir(tmp_path / "pkg")
    editable = distribution_record(_raw("pkg", pkg.as_uri()))
    assert editable.direct_url == DirectUrl(url=clean_url(pkg.as_uri()), editable=True)
    plain = distribution_record(_raw("numpy", None, record=None))
    assert plain == Distribution(name="numpy", version="1.0", record_sha256=None, direct_url=None)


def test_the_models_refuse_unknown_fields():
    with pytest.raises(ValueError):
        DirectUrl(url="x", surprise=1)  # type: ignore[call-arg]


def test_canonical_name_follows_pep_503():
    assert canonical_name("My_Pkg.Name--x") == "my-pkg-name-x"
    assert canonical_name("vcp") == "vcp"


# -- script_of ------------------------------------------------------------------------------------


def test_script_of_lists_every_existing_py_token_in_order(tmp_path):
    work = _dir(tmp_path / "work", ("a.py", "sub/B.PY"))
    (work / "dir.py").mkdir()
    absolute = str(work / "a.py")
    found = script_of(
        ["python", "-u", "missing.py", "sub/B.PY", "dir.py", "notes.txt", absolute], work
    )
    assert found == [work / "sub" / "B.PY", work / "a.py"]


def test_script_of_with_no_script_is_empty(tmp_path):
    work = _dir(tmp_path / "work")
    assert script_of([], work) == []
    assert script_of(["python", "-m", "pkg.train", "--epochs", "3"], work) == []


def test_script_of_survives_a_token_the_filesystem_cannot_hold(tmp_path):
    work = _dir(tmp_path / "work", ("a.py",))
    too_long = "x" * 5000 + ".py"
    assert script_of(["python", "-c", too_long, "a.py"], work) == [work / "a.py"]


# -- nested roots, checkpoint globs and generated directories -------------------------------------


def test_skip_dirs_hold_nested_roots_only(tmp_path):
    work = _dir(tmp_path / "work")
    inner = _dir(work / "inner")
    deeper = _dir(inner / "deeper")
    aside = _dir(tmp_path / "aside")
    planned = [
        PlannedRoot("cwd", "cwd", work, "dir", None),
        PlannedRoot("dist-inner", "editable", inner, "dir", None),
        PlannedRoot("root-1", "extra", aside, "dir", None),
        PlannedRoot("root-2", "extra", deeper, "dir", None),
    ]
    data = tmp_path / "data"
    assert skip_dirs_for(planned, 0, fixed=[data]) == [data, inner, deeper]
    assert skip_dirs_for(planned, 1, fixed=[data]) == [data, deeper]
    assert skip_dirs_for(planned, 2, fixed=[]) == []
    assert skip_dirs_for(planned, 3, fixed=[]) == []


@pytest.mark.parametrize(
    ("pattern", "excluded"),
    [
        ("runs/exp/*.pt", "runs/exp"),
        ("runs/exp*/x.pt", "runs"),
        ("weights/best.pt", "weights"),
        ("runs\\exp\\*.pt", "runs/exp"),
        ("./runs/[ab]/x.pt", "runs"),
        ("runs/e?p/x.pt", "runs"),
        ("runs/../out/*.pt", "out"),
        ("*.pt", None),
        ("best.pt", None),
        ("./*.pt", None),
        ("../*.pt", None),
        ("**/*.pt", None),
        ("", None),
    ],
)
def test_checkpoint_globs_exclude_their_base_directory(tmp_path, pattern, excluded):
    work = _dir(tmp_path / "work")
    planned = [PlannedRoot("cwd", "cwd", work, "dir", None)]
    expected = [] if excluded is None else [work / excluded]
    assert checkpoint_exclusions([pattern], work, planned) == expected


def test_a_glob_base_above_or_at_a_root_excludes_nothing(tmp_path):
    work = _dir(tmp_path / "work")
    inner = _dir(work / "inner")
    planned = [PlannedRoot("cwd", "cwd", inner, "dir", None)]  # the root is below cwd
    assert (
        checkpoint_exclusions(["*.pt", "x/y.pt", "inner/*.pt", "inner/a.pt"], work, planned) == []
    )
    assert checkpoint_exclusions(["inner/runs/*.pt"], work, planned) == [inner / "runs"]


def test_an_absolute_glob_outside_every_root_excludes_nothing(tmp_path):
    work = _dir(tmp_path / "work")
    other = _dir(tmp_path / "other")
    planned = [PlannedRoot("cwd", "cwd", work, "dir", None)]
    assert checkpoint_exclusions([str(other / "runs" / "*.pt")], work, planned) == []
    assert checkpoint_exclusions([str(work / "runs" / "*.pt")], work, planned) == [work / "runs"]


def test_checkpoint_exclusions_are_deduplicated_and_sorted(tmp_path):
    work = _dir(tmp_path / "work")
    planned = [PlannedRoot("cwd", "cwd", work, "dir", None)]
    patterns = ["runs/b/*.pt", "runs/a/*.pt", "runs/a/last.pt", "./runs/a/*.pt"]
    assert checkpoint_exclusions(patterns, work, planned) == [
        work / "runs" / "a",
        work / "runs" / "b",
    ]


def test_a_glob_base_inside_a_nested_root_is_excluded_once(tmp_path):
    work = _dir(tmp_path / "work")
    inner = _dir(work / "inner")
    planned = [
        PlannedRoot("cwd", "cwd", work, "dir", None),
        PlannedRoot("dist-inner", "editable", inner, "dir", None),
    ]
    assert checkpoint_exclusions(["inner/out/*.pt"], work, planned) == [inner / "out"]


@pytest.fixture
def no_generated_env(monkeypatch):
    for var in GENERATED_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(generated, "_REGISTRY", dict(generated._REGISTRY))


def test_generated_env_directories_inside_a_root_are_excluded(
    tmp_path, monkeypatch, no_generated_env
):
    work = _dir(tmp_path / "work")
    planned = [PlannedRoot("cwd", "cwd", work, "dir", None)]
    monkeypatch.setenv("TORCHINDUCTOR_CACHE_DIR", str(work / "ic"))
    assert generated_exclusions(planned) == [work / "ic"]


def test_generated_directories_outside_or_at_a_root_are_not_excluded(
    tmp_path, monkeypatch, no_generated_env
):
    work = _dir(tmp_path / "work")
    planned = [PlannedRoot("cwd", "cwd", work, "dir", None)]
    monkeypatch.setenv("TORCHINDUCTOR_CACHE_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("TORCH_EXTENSIONS_DIR", str(work))
    monkeypatch.setenv("TRITON_CACHE_DIR", str(tmp_path))
    assert generated_exclusions(planned) == []


def test_only_variables_that_are_set_exclude_a_directory(tmp_path, no_generated_env):
    work = _dir(tmp_path / "work")
    planned = [PlannedRoot("cwd", "cwd", work, "dir", None)]
    assert generated_exclusions(planned) == []  # defaults live outside the project


def test_a_registered_generated_directory_with_a_variable_is_excluded(
    tmp_path, monkeypatch, no_generated_env
):
    work = _dir(tmp_path / "work")
    planned = [PlannedRoot("cwd", "cwd", work, "dir", None)]
    register_generated("jax", lambda: work / "jax", env="VCP_TEST_JAX_DIR")
    register_generated("pure", lambda: work / "pure")  # no variable: not an exclusion
    monkeypatch.setenv("VCP_TEST_JAX_DIR", str(work / "jax"))
    assert generated_exclusions(planned) == [work / "jax"]


def test_two_variables_naming_one_directory_exclude_it_once(
    tmp_path, monkeypatch, no_generated_env
):
    work = _dir(tmp_path / "work")
    planned = [PlannedRoot("cwd", "cwd", work, "dir", None)]
    monkeypatch.setenv("TORCHINDUCTOR_CACHE_DIR", str(work / "cache"))
    monkeypatch.setenv("TRITON_CACHE_DIR", str(work / "cache"))
    assert generated_exclusions(planned) == [work / "cache"]
