"""Build identity: the ``vcp_version`` every artifact records must resolve to code, not to a
label. Run from a checkout it carries the commit (and a dirty marker); installed without git it
is the bare version, which then names a tagged release or nothing."""

import re
import subprocess
from pathlib import Path

import pytest

import vcp
from vcp.core import build as buildmod
from vcp.core.build import (
    BuildInfo,
    build_info,
    build_string,
    git_head,
    parse_build_string,
    probe_build,
)

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SHA = "0123456789abcdef" * 2 + "01234567"


def _git(cwd: Path, *args: str) -> str:
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A throwaway repository whose tracked ``src/vcp`` stands in for the real package."""
    pkg = tmp_path / "src" / "vcp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "src/vcp/__init__.py")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path, pkg


def test_version_is_semver():
    assert SEMVER.match(vcp.__version__), vcp.__version__


def test_probe_from_a_clean_tracked_checkout(repo):
    root, pkg = repo
    info = probe_build(pkg)
    assert info.version == vcp.__version__  # the running package's version, not the stand-in's
    assert info.commit == _git(root, "rev-parse", "HEAD") and len(info.commit) == 40
    assert info.dirty is False
    assert build_string(info) == f"{vcp.__version__}+g{info.commit}"


def test_probe_reports_a_dirty_tree(repo):
    root, pkg = repo
    (root / "notes.txt").write_text("wip\n", encoding="utf-8")  # anywhere in the repo counts
    info = probe_build(pkg)
    assert info.dirty is True and build_string(info).endswith(f"+g{info.commit}.dirty")


def test_probe_of_an_untracked_directory_is_the_bare_version(tmp_path, monkeypatch):
    pkg = tmp_path / "vcp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    assert probe_build(pkg) == BuildInfo(vcp.__version__, None, None)
    assert build_string(probe_build(pkg)) == vcp.__version__
    monkeypatch.setattr(buildmod.shutil, "which", lambda *a, **k: None)  # and without git at all
    assert probe_build(pkg) == BuildInfo(vcp.__version__, None, None)


def test_a_copy_installed_inside_someone_elses_repo_is_not_claimed(repo):
    """A wheel unpacked into a venv that lives inside a git repository must not borrow that
    repository's commit: only a directory git TRACKS is a checkout of vcp."""
    root, _ = repo
    venv_pkg = root / ".venv" / "site-packages" / "vcp"
    venv_pkg.mkdir(parents=True)
    (venv_pkg / "__init__.py").write_text("", encoding="utf-8")
    assert probe_build(venv_pkg) == BuildInfo(vcp.__version__, None, None)


def test_git_head_names_the_enclosing_repository(repo, monkeypatch):
    root, _ = repo
    head = git_head(root / "src")
    assert head == (_git(root, "rev-parse", "HEAD"), False)
    (root / "x").write_text("", encoding="utf-8")
    assert git_head(root / "src") == (head[0], True)
    monkeypatch.setattr(buildmod.shutil, "which", lambda *a, **k: None)
    assert git_head(root) is None


def test_build_info_is_probed_once_and_describes_this_checkout():
    info = build_info()
    assert info is build_info() and info.version == vcp.__version__
    if info.commit is not None:  # the test suite runs from a checkout; a wheel would give None
        assert len(info.commit) == 40 and isinstance(info.dirty, bool)
    assert build_string() == build_string(info)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("0.2.0", BuildInfo("0.2.0", None, None)),
        (f"0.2.0+g{SHA}", BuildInfo("0.2.0", SHA, False)),
        (f"0.2.0+g{SHA}.dirty", BuildInfo("0.2.0", SHA, True)),
    ],
)
def test_build_string_round_trips(text, expected):
    assert parse_build_string(text) == expected and build_string(expected) == text


@pytest.mark.parametrize("bad", ["", "0.2", "0.2.0+banana", f"0.2.0+g{SHA[:7]}", f"0.2.0-g{SHA}"])
def test_parse_rejects_anything_that_is_not_a_build_string(bad):
    with pytest.raises(ValueError, match="build string"):
        parse_build_string(bad)
