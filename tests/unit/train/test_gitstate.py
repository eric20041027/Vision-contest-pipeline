import hashlib

import pytest

from helpers import git, git_repo
from vcp.core.errors import ValidationFailed
from vcp.train import gitstate
from vcp.train.gitstate import (
    changed_since,
    parse_porcelain,
    probe,
    record,
    require_clean,
    warning,
)
from vcp.train.schema import GitInfo

EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def test_an_older_env_json_still_parses():
    old = GitInfo.model_validate({"commit": "a" * 40, "dirty": True})
    assert old.modified == 0 and old.modified_paths == [] and old.patch is None


def test_parse_porcelain_splits_tracked_from_untracked():
    raw = b" M a.py\0M  b.py\0D  gone.py\0R  new.py\0old.py\0?? out/\0?? note with space.txt\0"
    assert parse_porcelain(raw) == (
        ["a.py", "b.py", "gone.py", "new.py"],
        ["out/", "note with space.txt"],
    )
    assert parse_porcelain(b"") == ([], [])


def test_a_clean_repository(tmp_path):
    repo = git_repo(tmp_path / "repo")
    info = record(repo)
    assert info is not None and len(info.commit) == 40 and info.dirty is False
    assert (info.modified, info.untracked) == (0, 0)
    assert info.modified_paths == [] and info.untracked_paths == []
    assert info.status_sha256 == EMPTY_SHA
    assert info.diff_sha256 is None and info.patch_bytes is None and info.patch is None
    assert warning(info) is None
    require_clean(repo)


def test_untracked_files_are_listed_but_do_not_count(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "outputs").mkdir()
    (repo / "outputs" / "a.log").write_bytes(b"a")
    (repo / "outputs" / "b.log").write_bytes(b"b")
    info = record(repo)
    assert info.dirty is True and info.modified == 0 and info.untracked == 1
    assert info.untracked_paths == ["outputs/"] and info.diff_sha256 is None
    assert warning(info) is None
    require_clean(repo)


def test_tracked_changes_are_recorded_with_a_patch_that_applies(tmp_path):
    repo = git_repo(
        tmp_path / "repo",
        {
            "train.py": b"print('v1')\n",
            "old.py": b"x = 1\n",
            "gone.py": b"y = 2\n",
            "données.py": b"z = 3\n",
        },
    )
    (repo / "train.py").write_bytes(b"print('v2')\n")
    (repo / "données.py").write_bytes(b"z = 4\n")
    git(repo, "mv", "old.py", "new name.py")
    git(repo, "rm", "-q", "gone.py")
    patch = tmp_path / "git.1.patch"
    info = record(repo, (patch, "train/git.1.patch"))
    assert info.modified == 4 and info.untracked == 0 and info.dirty is True
    assert sorted(info.modified_paths) == sorted(
        ["train.py", "données.py", "new name.py", "gone.py"]
    )
    assert info.patch == "train/git.1.patch" and patch.is_file()
    assert info.diff_sha256 == hashlib.sha256(patch.read_bytes()).hexdigest()
    assert info.patch_bytes == patch.stat().st_size
    assert warning(info) == "modified=4 tracked path(s); patch train/git.1.patch"
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(repo), str(clone))
    git(clone, "apply", "--check", str(patch))
    with pytest.raises(ValidationFailed, match="dirty_tree: 4 tracked path") as caught:
        require_clean(repo)
    assert caught.value.fields["modified"] == 4


def test_limits_truncate_the_lists_and_drop_a_large_patch(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo", {f"f{i}.py": b"a\n" for i in range(3)})
    for i in range(3):
        (repo / f"f{i}.py").write_bytes(b"b\n")
        (repo / f"u{i}.txt").write_bytes(b"u")
    monkeypatch.setattr(gitstate, "PATH_LIMIT", 2)
    monkeypatch.setattr(gitstate, "PATCH_LIMIT", 10)
    patch = tmp_path / "git.1.patch"
    info = record(repo, (patch, "train/git.1.patch"))
    assert (info.modified, info.untracked) == (3, 3)
    assert len(info.modified_paths) == 2 and len(info.untracked_paths) == 2
    assert info.patch is None and not patch.exists()
    assert not patch.with_name(patch.name + ".tmp").exists()
    assert info.patch_bytes > 10 and info.diff_sha256 is not None
    assert warning(info) == (
        f"modified=3 tracked path(s); patch too large ({info.patch_bytes} bytes), sha256 only"
    )


def test_outside_a_repository_or_without_git(tmp_path, monkeypatch):
    assert probe(tmp_path) is None and record(tmp_path) is None
    with pytest.raises(ValidationFailed, match="not_found: git repository"):
        require_clean(tmp_path)
    repo = git_repo(tmp_path / "repo")
    monkeypatch.setattr(gitstate.shutil, "which", lambda name, *a, **k: None)
    assert record(repo) is None
    with pytest.raises(ValidationFailed, match="not_found: git repository"):
        require_clean(repo)


def test_changed_since_looks_at_head_and_the_tracked_diff_only(tmp_path):
    repo = git_repo(tmp_path / "repo")
    start = record(repo)
    (repo / "out.log").write_bytes(b"an output the run wrote")
    assert changed_since(repo, start).changed == ()
    (repo / "train.py").write_bytes(b"print('edited mid-run')\n")
    delta = changed_since(repo, start)
    assert delta.changed == ("diff",) and delta.commit == start.commit and delta.diff_sha256
    git(repo, "commit", "-q", "--no-verify", "-am", "mid-run commit")
    delta = changed_since(repo, start)
    assert delta.changed == ("commit",) and delta.diff_sha256 is None


def test_changed_since_without_git(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    start = record(repo)
    monkeypatch.setattr(gitstate.shutil, "which", lambda name, *a, **k: None)
    assert changed_since(repo, start).changed == ("unavailable",)


def test_a_patch_applies_whatever_the_users_diff_prefix_settings(tmp_path):
    """spec 2026-09-27 §3.2 (fix round 1): a repo-local ``diff.noprefix`` or
    ``diff.mnemonicPrefix`` must not break the plain ``git apply`` the run-restore promise
    (spec §3.3) depends on."""
    for i, setting in enumerate(("diff.noprefix", "diff.mnemonicPrefix")):
        repo = git_repo(tmp_path / f"repo{i}")
        git(repo, "config", setting, "true")
        (repo / "train.py").write_bytes(b"print('v2')\n")
        patch = tmp_path / f"git{i}.patch"
        info = record(repo, (patch, "train/git.1.patch"))
        assert info.patch == "train/git.1.patch" and patch.is_file()
        clone = tmp_path / f"clone{i}"
        git(tmp_path, "clone", "-q", str(repo), str(clone))
        git(clone, "apply", "--check", str(patch))


def test_diff_sha256_is_the_same_whatever_the_users_quotepath_setting(tmp_path):
    """spec 2026-09-27 §3.2 (fix round 1): ``core.quotepath`` must not change what gets hashed,
    so two machines with different settings agree on the same non-ASCII change."""
    shas = []
    for i, value in enumerate(("true", "false")):
        repo = git_repo(tmp_path / f"repo{i}", {"données.py": b"z = 3\n"})
        git(repo, "config", "core.quotepath", value)
        (repo / "données.py").write_bytes(b"z = 4\n")
        shas.append(record(repo).diff_sha256)
    assert shas[0] is not None and shas[0] == shas[1]


def test_a_repository_with_no_commits_yet(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    assert probe(repo) is None and record(repo) is None
    with pytest.raises(ValidationFailed, match="not_found: git repository"):
        require_clean(repo)
