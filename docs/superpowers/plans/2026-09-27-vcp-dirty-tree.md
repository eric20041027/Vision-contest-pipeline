# VCP-041 Dirty Working Tree Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `vcp train run` records what makes the training tree dirty (paths, two sha256s, the tracked diff as a patch). It WARNs on tracked changes, FAILs before the first write under `--require-clean`, and WARNs when HEAD or the tracked diff changed during the run.

**Architecture:**
- A new module `vcp.train.gitstate` owns every git call: status, diff streaming, the `--require-clean` check, the end-of-run comparison and the WARN line.
- `GitInfo` in `train/env.<n>.json` gains the new fields.
- `vcp.train.env.snapshot` passes a patch destination through.
- `vcp.train.run.train_run` wires the preflight, the start record and the end check.
- `vcp train run` adds `--require-clean` and the VERDICT fields.

**Tech Stack:** Python 3.12, pydantic v2, typer, pytest, the `git` CLI.

**Spec:** `docs/superpowers/specs/2026-09-27-vcp-dirty-tree-design.md` (commit 7e2e21c).

## Global Constraints

- **Time.** Only through `vcp.core.time.utc_now()` / `stamp()`; ruff TID251 bans the rest.
- **VERDICT and exit codes.**
  - Every CLI command ends with `VERDICT cmd=... status=OK|WARN|FAIL|ABORT ...`. Exit codes are 0 / 0 / 1 / 2.
  - `--json` sends the JSON to stdout and the VERDICT to stderr. Commands never prompt.
- **`reason=` words** (spec §6):
  - `not_found:` (existing) — `--require-clean` without git, outside a repository, or when a git command fails;
  - `dirty_tree:` (new) — `--require-clean` with tracked changes; carries `modified=<n>`.
- **New `vcp train run` VERDICT fields** (spec §5):
  - `commit=<first 12 hex>` always, or `commit=none` outside a repository;
  - `modified=<n>` and `untracked=<n>`, only inside a repository;
  - `git_changed=<commit,diff subset | unavailable>`, only when something changed. It is a WARN.
  - `modified > 0` is a WARN.
- **Only tracked files count as a change.** Untracked files are listed by path and never read. The end-of-run comparison looks at HEAD and the tracked diff only (spec §4.3).
- **Limits:** paths kept per list `PATH_LIMIT = 50`; patch size `PATCH_LIMIT = 10 MiB`; paths named in a `dirty_tree:` message `SHOWN_PATHS = 5`.
- **Git commands** run at the repository's top level (`rev-parse --show-toplevel`):
  - `git status --porcelain=v1 -z --untracked-files=normal`
  - `git diff HEAD --binary --no-color --no-ext-diff --no-textconv`
- **Tests.**
  - Tests never touch the real data root; use the `roots` fixture. Git repositories in tests are built only with `tests/helpers.py`'s `git` / `git_repo` (throwaway identity, `core.autocrlf=false`, `--no-verify` commits). Without git the test skips.
  - Run tests with `uv run pytest -o addopts="" -q ...`.
- **Files and encoding.**
  - Files are UTF-8 with LF line endings.
  - Do not write repo files with Python `Path.write_text` or text-mode `open`; that produces CRLF on this Windows machine. Use the Edit / Write tools, or `write_bytes`.
  - Never type a backslash-u escape into a tool input.
- **Git workflow.**
  - Commits are `type(scope): 繁中說明` exactly as each task gives them, with no co-author trailer. Write a message that holds CJK text to a file and use `git commit -F`.
  - Never `git add -A`; list files. Never a bare `git stash`.
  - `uv run ruff check . && uv run ruff format --check .` before each commit. `uv run ruff format <file.py>` is fine; never run it on markdown.
- **Environment.**
  - Shell is Git Bash on Windows 11. Bare `python` is not on PATH; use `uv run python`.
  - Work only in `C:/Users/smallfire123123/Desktop/Vision-contest-pipeline/.claude/worktrees/vcp-040` (branch `feat/vcp-041-dirty-tree`). Do not push. Do not open PRs.

## File Structure

- **Create** `src/vcp/train/gitstate.py`: git probing, porcelain parsing, diff streaming into a patch, `require_clean`, `changed_since`, `warning`.
- **Modify** `src/vcp/train/schema.py`: `GitInfo` gains eight defaulted fields.
- **Modify** `src/vcp/train/env.py`: `git_info` / `snapshot` take `patch=(file, run-relative name)` and delegate to `gitstate.record`.
- **Modify** `src/vcp/train/run.py`:
  - `RunSpec.require_clean`;
  - `RunResult.git` / `git_changed`;
  - the preflight, the patch destination, the end check and the warnings.
- **Modify** `src/vcp/cli_train.py`: `--require-clean` and the VERDICT fields.
- **Tests:**
  - **Modify** `tests/helpers.py`: `git`, `git_repo`.
  - **Create** `tests/unit/train/test_gitstate.py`.
  - **Modify** `tests/unit/train/test_env.py`, `tests/unit/train/test_run.py`, `tests/unit/test_cli_train.py`.
- **Docs:**
  - `docs/reference/cli.md`;
  - `docs/superpowers/specs/2026-09-05-vcp-training-layer-design.md`;
  - `CLAUDE.md` and `AGENTS.md`;
  - `.claude/skills/vcp-train-submit-backup/SKILL.md` and its `.agents/skills` mirror;
  - `docs/audits/2026-09-11-vcp-improvement-audit.md`.
- **Release (Task 5):** `src/vcp/__init__.py`, `.claude/.claude-plugin/plugin.json`, `CHANGELOG.md`.

---

### Task 1: `GitInfo` fields and `vcp.train.gitstate`

**Files:**
- Modify: `src/vcp/train/schema.py` (`GitInfo`)
- Create: `src/vcp/train/gitstate.py`
- Modify: `tests/helpers.py` (append `git`, `git_repo`)
- Test: `tests/unit/train/test_gitstate.py` (create)

**Interfaces:**
- Consumes: `vcp.core.errors.ValidationFailed(message, fields=...)`; `vcp.train.schema._Strict` (pydantic, `extra="forbid"`).
- Produces:
  - `GitInfo` fields: `commit`, `dirty`, `modified`, `untracked`, `modified_paths`, `untracked_paths`, `status_sha256`, `diff_sha256`, `patch_bytes`, `patch`.
  - `vcp.train.gitstate`:
    - constants `PATH_LIMIT`, `PATCH_LIMIT`, `SHOWN_PATHS`;
    - dataclasses `GitStatus`, `Diff`, `GitDelta(changed: tuple[str, ...], commit: str | None, diff_sha256: str | None)`;
    - `parse_porcelain(raw: bytes) -> tuple[list[str], list[str]]`;
    - `probe(cwd: Path) -> GitStatus | None`;
    - `stream_diff(top: Path, patch: Path | None = None, limit: int | None = None) -> Diff | None`;
    - `record(cwd: Path, patch: tuple[Path, str] | None = None) -> GitInfo | None`;
    - `require_clean(cwd: Path) -> None`;
    - `changed_since(cwd: Path, start: GitInfo) -> GitDelta`;
    - `warning(git: GitInfo) -> str | None`.
  - `tests/helpers.py`: `git(repo: Path, *args: str) -> str` and `git_repo(path: Path, files: dict[str, bytes] | None = None) -> Path`.

- [ ] **Step 1: Add the test helpers** to `tests/helpers.py`

Add `import shutil` and `import subprocess` to the standard-library imports and `import pytest` to the third-party imports at the top, keeping ruff's sorting. Then append:

```python
GIT_CONFIG = (
    "-c",
    "user.name=vcp-test",
    "-c",
    "user.email=vcp-test@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.autocrlf=false",
    "-c",
    "init.defaultBranch=main",
)


def git(repo: Path, *args: str) -> str:
    """git in ``repo`` with a throwaway identity and none of the machine's global surprises
    (autocrlf, signing); commits pass ``--no-verify`` at the call site. Skips without git."""
    exe = shutil.which("git")
    if exe is None:
        pytest.skip("git is not installed")
    proc = subprocess.run([exe, *GIT_CONFIG, "-C", str(repo), *args], capture_output=True, check=True)
    return proc.stdout.decode("utf-8", errors="replace")


def git_repo(path: Path, files: dict[str, bytes] | None = None) -> Path:
    """A repository at ``path`` whose one commit holds ``files`` (default: ``train.py``). Its own
    config pins ``core.autocrlf=false``, so vcp's git calls see the bytes the test wrote."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "core.autocrlf", "false")
    for name, data in (files or {"train.py": b"print('v1')\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    git(path, "add", "-A")
    git(path, "commit", "-q", "--no-verify", "-m", "init")
    return path
```

- [ ] **Step 2: Write the failing tests** — create `tests/unit/train/test_gitstate.py`

```python
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
    assert sorted(info.modified_paths) == sorted(["train.py", "données.py", "new name.py", "gone.py"])
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
```

- [ ] **Step 3: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/train/test_gitstate.py`
Expected: FAIL (`ModuleNotFoundError: No module named 'vcp.train.gitstate'`).

- [ ] **Step 4: Extend `GitInfo`** in `src/vcp/train/schema.py`

Replace:

```python
class GitInfo(_Strict):
    commit: str
    dirty: bool
```

with:

```python
class GitInfo(_Strict):
    commit: str
    dirty: bool  # any porcelain output, untracked files included (meaning unchanged)
    # VCP-041 (spec 2026-09-27 §3.1): what `dirty` is made of. The defaults keep an older
    # env.json valid; vcp itself never reads env.json back.
    modified: int = 0
    untracked: int = 0
    modified_paths: list[str] = Field(default_factory=list)
    untracked_paths: list[str] = Field(default_factory=list)
    status_sha256: str | None = None
    diff_sha256: str | None = None
    patch_bytes: int | None = None
    patch: str | None = None
```

(`Field` is already imported in that module; `EnvSnapshot` uses it.)

- [ ] **Step 5: Create `src/vcp/train/gitstate.py`**

```python
"""The training working tree's git state (VCP-041, spec 2026-09-27): what ``dirty`` is made of,
the tracked diff kept as a patch, the ``--require-clean`` check, and the second look when the
command has finished.

Only tracked files count as a change (spec decision 1): untracked files are listed by path and
never read. Every command runs at the top level of the repository that contains ``--cwd``, so
``diff.relative`` or a subdirectory cannot change what is recorded.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.train.schema import GitInfo

PATH_LIMIT = 50  # paths kept per list; the counts stay exact
PATCH_LIMIT = 10 * 1024 * 1024  # a larger diff keeps its sha256 and size, not the file
SHOWN_PATHS = 5  # paths named in a dirty_tree: message
_CHUNK = 1024 * 1024
STATUS_ARGS = ("status", "--porcelain=v1", "-z", "--untracked-files=normal")
DIFF_ARGS = ("diff", "HEAD", "--binary", "--no-color", "--no-ext-diff", "--no-textconv")


@dataclass(frozen=True)
class GitStatus:
    """One ``git status`` of the repository: its top level, HEAD and the porcelain bytes."""

    top: Path
    commit: str
    raw: bytes
    modified: tuple[str, ...]
    untracked: tuple[str, ...]


@dataclass(frozen=True)
class Diff:
    sha256: str
    size: int
    written: bool  # the patch file was written: not empty, within the limit


@dataclass(frozen=True)
class GitDelta:
    """The second look (spec §4.3): what differs from the start, and HEAD / the diff now."""

    changed: tuple[str, ...]
    commit: str | None
    diff_sha256: str | None


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes] | None:
    exe = shutil.which("git")
    if exe is None:
        return None
    try:
        return subprocess.run([exe, "-C", str(cwd), *args], capture_output=True)
    except OSError:
        return None


def parse_porcelain(raw: bytes) -> tuple[list[str], list[str]]:
    """``git status --porcelain=v1 -z`` -> (tracked paths with changes, untracked paths). A rename
    or copy entry is followed by its original path as one more field, which is skipped."""
    modified: list[str] = []
    untracked: list[str] = []
    fields = raw.split(b"\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:  # the empty field after the last NUL
            continue
        xy, path = entry[:2], entry[3:].decode("utf-8", errors="replace")
        if xy == b"??":
            untracked.append(path)
        elif xy != b"!!":
            modified.append(path)
            if b"R" in xy or b"C" in xy:
                i += 1
    return modified, untracked


def probe(cwd: Path) -> GitStatus | None:
    """The status of the repository containing ``cwd``; None without git, outside a repository,
    before its first commit, or when a git command fails."""
    top = _git(["rev-parse", "--show-toplevel"], cwd)
    if top is None or top.returncode != 0:
        return None
    root = Path(top.stdout.decode("utf-8", errors="replace").strip())
    head = _git(["rev-parse", "HEAD"], root)
    status = _git(list(STATUS_ARGS), root)
    if head is None or head.returncode != 0 or status is None or status.returncode != 0:
        return None
    modified, untracked = parse_porcelain(status.stdout)
    return GitStatus(
        top=root,
        commit=head.stdout.decode("ascii", errors="replace").strip(),
        raw=status.stdout,
        modified=tuple(modified),
        untracked=tuple(untracked),
    )


def stream_diff(top: Path, patch: Path | None = None, limit: int | None = None) -> Diff | None:
    """Hash all of ``git diff HEAD --binary``; also write it to ``patch`` when it is not empty and
    fits in ``limit`` bytes (default ``PATCH_LIMIT``), ``.tmp`` first and then renamed. None when
    git fails."""
    cap = PATCH_LIMIT if limit is None else limit
    exe = shutil.which("git")
    if exe is None:
        return None
    digest = hashlib.sha256()
    size = 0
    kept: list[bytes] = []
    try:
        with subprocess.Popen(
            [exe, "-C", str(top), *DIFF_ARGS], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ) as proc:
            stdout = proc.stdout
            if stdout is None:
                return None
            while chunk := stdout.read(_CHUNK):
                digest.update(chunk)
                size += len(chunk)
                if patch is not None and size <= cap:
                    kept.append(chunk)
            code = proc.wait()
    except OSError:
        return None
    if code != 0:
        return None
    written = False
    if patch is not None and 0 < size <= cap:
        tmp = patch.with_name(patch.name + ".tmp")
        tmp.write_bytes(b"".join(kept))
        os.replace(tmp, patch)
        written = True
    return Diff(digest.hexdigest(), size, written)


def record(cwd: Path, patch: tuple[Path, str] | None = None) -> GitInfo | None:
    """spec 2026-09-27 §3: the record taken when an attempt starts. ``patch`` is (the file to write
    the tracked diff to, the run-relative name recorded for it). None when git, the repository or
    its HEAD is missing -- as before 0.11.0."""
    status = probe(cwd)
    if status is None:
        return None
    diff: Diff | None = None
    if status.modified:
        diff = stream_diff(status.top, patch[0] if patch is not None else None)
        if diff is None:
            return None
    return GitInfo(
        commit=status.commit,
        dirty=bool(status.modified or status.untracked),
        modified=len(status.modified),
        untracked=len(status.untracked),
        modified_paths=list(status.modified[:PATH_LIMIT]),
        untracked_paths=list(status.untracked[:PATH_LIMIT]),
        status_sha256=hashlib.sha256(status.raw).hexdigest(),
        diff_sha256=diff.sha256 if diff is not None else None,
        patch_bytes=diff.size if diff is not None else None,
        patch=patch[1] if patch is not None and diff is not None and diff.written else None,
    )


def require_clean(cwd: Path) -> None:
    """``--require-clean`` (spec 2026-09-27 §4.1), before the first write: a tracked change FAILs,
    untracked files pass, and a tree git cannot see cannot be proven clean."""
    status = probe(cwd)
    if status is None:
        raise ValidationFailed(f"not_found: git repository for --cwd {cwd}")
    if status.modified:
        shown = ", ".join(status.modified[:SHOWN_PATHS])
        raise ValidationFailed(
            f"dirty_tree: {len(status.modified)} tracked path(s) changed: {shown}",
            fields={"modified": len(status.modified)},
        )


def changed_since(cwd: Path, start: GitInfo) -> GitDelta:
    """spec 2026-09-27 §4.3: HEAD and the tracked diff again, no patch written. Untracked files
    are not compared -- a run writing its outputs into the repository is expected."""
    status = probe(cwd)
    if status is None:
        return GitDelta(("unavailable",), None, None)
    diff_sha: str | None = None
    if status.modified:
        diff = stream_diff(status.top)
        if diff is None:
            return GitDelta(("unavailable",), status.commit, None)
        diff_sha = diff.sha256
    changed = tuple(
        name
        for name, now, then in (
            ("commit", status.commit, start.commit),
            ("diff", diff_sha, start.diff_sha256),
        )
        if now != then
    )
    return GitDelta(changed, status.commit, diff_sha)


def warning(git: GitInfo) -> str | None:
    """The WARN line for tracked changes when an attempt starts (spec 2026-09-27 §4.2)."""
    if not git.modified:
        return None
    head = f"modified={git.modified} tracked path(s)"
    if git.patch is not None:
        return f"{head}; patch {git.patch}"
    if git.patch_bytes is not None and git.patch_bytes > PATCH_LIMIT:
        return f"{head}; patch too large ({git.patch_bytes} bytes), sha256 only"
    return head
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `uv run pytest -o addopts="" -q tests/unit/train/test_gitstate.py tests/unit/train/test_env.py tests/unit/train/test_schema_records.py`
Expected: all PASS.

- [ ] **Step 7: Lint, then commit**

```bash
uv run ruff format tests/helpers.py tests/unit/train/test_gitstate.py src/vcp/train/gitstate.py
uv run ruff check . && uv run ruff format --check .
git add src/vcp/train/schema.py src/vcp/train/gitstate.py tests/helpers.py tests/unit/train/test_gitstate.py
git commit -F <message file>
```

Message: `feat(train): 工作樹的 git 狀態：改動與未追蹤路徑、兩個 sha、patch`

---

### Task 2: The environment snapshot writes the patch

**Files:**
- Modify: `src/vcp/train/env.py` (`git_info`, `snapshot`)
- Test: `tests/unit/train/test_env.py` (append; extend one test)

**Interfaces:**
- Consumes: `vcp.train.gitstate.record(cwd, patch)` (Task 1).
- Produces:
  - `git_info(cwd: Path, *, patch: tuple[Path, str] | None = None) -> GitInfo | None`;
  - `snapshot(python: Path | None, cwd: Path, *, patch: tuple[Path, str] | None = None) -> EnvSnapshot`.

  Task 3 calls `snapshot(python, cwd, patch=(run_root / patch_rel, patch_rel))`.

- [ ] **Step 1: Write the failing test** — append to `tests/unit/train/test_env.py`

Add `from helpers import git_repo` to the imports, then append:

```python
def test_snapshot_writes_the_patch_it_was_given(tmp_path, monkeypatch):
    real_which = shutil.which
    monkeypatch.setattr(
        envmod.shutil,
        "which",
        lambda name, *a, **k: None if name == "nvidia-smi" else real_which(name),
    )
    repo = git_repo(tmp_path / "repo")
    (repo / "train.py").write_bytes(b"print('v2')\n")
    patch = tmp_path / "run" / "train" / "git.1.patch"
    patch.parent.mkdir(parents=True)
    snap = snapshot(None, repo, patch=(patch, "train/git.1.patch"))
    assert snap.git is not None and snap.git.modified == 1
    assert snap.git.patch == "train/git.1.patch" and patch.is_file()
    assert snap.git.modified_paths == ["train.py"]
```

In `test_snapshot_uses_current_interpreter_and_repo_git`, after the line that asserts `snap.git is not None and len(snap.git.commit) == 40 ...`, add:

```python
    assert snap.git.status_sha256 is not None and len(snap.git.status_sha256) == 64
    assert snap.git.patch is None  # no destination given, so no file
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest -o addopts="" -q tests/unit/train/test_env.py`
Expected: FAIL (`TypeError: snapshot() got an unexpected keyword argument 'patch'`, and the new `status_sha256` assert fails on `None`).

- [ ] **Step 3: Implement** in `src/vcp/train/env.py`

Change the import `from vcp.core.build import build_string, git_head` to `from vcp.core.build import build_string`, and add `from vcp.train.gitstate import record as git_record` after the `vcp.core.time` import.

Replace `git_info` and `snapshot` with:

```python
def git_info(cwd: Path, *, patch: tuple[Path, str] | None = None) -> GitInfo | None:
    """The git state of the repository containing ``cwd`` (the training working directory, not
    vcp's own; spec 2026-09-27 §3). With ``patch`` the tracked diff is also written there. None
    outside any repo."""
    return git_record(cwd, patch)


def snapshot(
    python: Path | None, cwd: Path, *, patch: tuple[Path, str] | None = None
) -> EnvSnapshot:
    """spec 4.4: the venv's report plus what vcp sees from outside."""
    data = _probe(python or Path(sys.executable))
    names, driver = gpus()
    return EnvSnapshot(
        **data,
        gpus=names,
        nvidia_driver=driver,
        vcp_version=build_string(),
        git=git_info(cwd, patch=patch),
        taken_at=stamp(),
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/train`
Expected: all PASS.

- [ ] **Step 5: Lint, then commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/train/env.py tests/unit/train/test_env.py
git commit -F <message file>
```

Message: `feat(train): 環境快照帶完整 git 狀態並寫 patch`

---

### Task 3: `vcp train run --require-clean`, the WARNs and the VERDICT fields

**Files:**
- Modify: `src/vcp/train/run.py` (`RunSpec`, `RunResult`, `train_run`)
- Modify: `src/vcp/cli_train.py` (`run_cmd`)
- Test: `tests/unit/train/test_run.py` (append), `tests/unit/test_cli_train.py` (append)

**Interfaces:**
- Consumes:
  - `require_clean`, `changed_since`, `warning` and `GitDelta` from Task 1;
  - `snapshot(..., patch=...)` from Task 2;
  - `GitInfo`.
- Produces:
  - `RunSpec.require_clean: bool = False`;
  - `RunResult.git: GitInfo | None`, `RunResult.git_changed: list[str]`;
  - CLI `--require-clean`;
  - VERDICT `commit=` / `modified=` / `untracked=` / `git_changed=`.

- [ ] **Step 1: Append the failing tests** to `tests/unit/train/test_run.py`

Add `git` and `git_repo` to its `from helpers import (...)` line, then append:

```python
# VCP-041: a loop that edits a tracked file while it trains.
EDIT_FAKE = """
from pathlib import Path
model = Path("model.py")
model.write_bytes(model.read_bytes() + b"# edited while training\\n")
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best")
"""


def test_git_state_of_the_training_tree(roots, tmp_path):
    _seed(roots)
    repo = git_repo(tmp_path / "repo", {"fake_train.py": FAKE.encode()})
    clean = train_run(_spec(roots, repo))
    assert clean.git is not None and len(clean.git.commit) == 40 and clean.git.modified == 0
    assert clean.git_changed == []  # weights/ is a new untracked output, not a change
    assert not any(w.startswith("modified=") for w in clean.warnings)
    (repo / "fake_train.py").write_bytes(FAKE.encode() + b"# local tweak\n")
    dirty = train_run(_spec(roots, repo, run_id="r2"))
    assert dirty.git.modified == 1 and dirty.git.modified_paths == ["fake_train.py"]
    assert dirty.git.patch == "train/git.1.patch"
    run = run_dir(roots.data, "r2")
    assert sha256_file(run / "train" / "git.1.patch") == dirty.git.diff_sha256
    assert "modified=1 tracked path(s); patch train/git.1.patch" in dirty.warnings
    env = json.loads((run / "train" / "env.1.json").read_text(encoding="utf-8"))
    assert env["git"]["patch"] == "train/git.1.patch"
    assert env["git"]["untracked_paths"] == ["weights/"]


def test_require_clean_fails_before_the_first_write(roots, tmp_path, work):
    _seed(roots)
    repo = git_repo(tmp_path / "repo", {"fake_train.py": FAKE.encode()})
    (repo / "fake_train.py").write_bytes(FAKE.encode() + b"# local tweak\n")
    with pytest.raises(ValidationFailed, match="dirty_tree: 1 tracked path"):
        train_run(_spec(roots, repo, require_clean=True))
    assert not run_dir(roots.data, "r1").exists()
    with pytest.raises(ValidationFailed, match="not_found: git repository"):
        train_run(_spec(roots, work, require_clean=True))
    assert not run_dir(roots.data, "r1").exists()
    git(repo, "commit", "-q", "--no-verify", "-am", "keep the tweak")
    (repo / "notes.txt").write_bytes(b"untracked is fine")
    ok = train_run(_spec(roots, repo, require_clean=True))
    assert ok.attempt.status == "finished" and ok.git.modified == 0 and ok.git.untracked == 1


def test_a_tracked_edit_during_training_is_a_warning(roots, tmp_path):
    _seed(roots)
    repo = git_repo(
        tmp_path / "repo", {"edit_train.py": EDIT_FAKE.encode(), "model.py": b"W = 1\n"}
    )
    res = train_run(_spec(roots, repo, command=[sys.executable, "edit_train.py"]))
    assert res.attempt.status == "finished"
    assert res.git.modified == 0 and res.git_changed == ["diff"]
    assert "git_changed=diff" in res.warnings
    notes = [e for e in read_events(roots.data, "r1") if e.get("key") == "git_changed"]
    assert len(notes) == 1 and notes[0]["value"] == "diff"
    assert notes[0]["commit"] == res.git.commit and len(notes[0]["diff_sha256"]) == 64


def test_each_attempt_keeps_its_own_git_record(roots, tmp_path):
    _seed(roots)
    repo = git_repo(tmp_path / "repo", {"fake_train.py": FAKE.encode()})
    (repo / "fake_train.py").write_bytes(FAKE.encode() + b"# tweak 1\n")
    first = train_run(_spec(roots, repo))
    (repo / "fake_train.py").write_bytes(FAKE.encode() + b"# tweak 2\n")
    second = train_run(_spec(roots, repo, resume=True))
    train = run_dir(roots.data, "r1") / "train"
    assert (train / "git.1.patch").is_file() and (train / "git.2.patch").is_file()
    assert first.git.diff_sha256 != second.git.diff_sha256
    assert second.git.patch == "train/git.2.patch"
```

Append to `tests/unit/test_cli_train.py`, adding `git_repo` to its `from helpers import ...` line:

```python
def test_train_run_cli_git_fields(roots, tmp_path):
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
    work = tmp_path / "work"
    work.mkdir()
    (work / "fake_train.py").write_bytes(FAKE.encode())
    r = _run(work, "--seed", "3", "--framework", "fake", run="r3")
    v = _verdict(r.output)
    assert "commit=none" in v and "modified=" not in v
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/train/test_run.py tests/unit/test_cli_train.py -k "git or require_clean or tracked_edit"`
Expected: FAIL (`RunSpec` rejects `require_clean` as `extra_forbidden`; `RunResult` has no `git`).

- [ ] **Step 3: Wire `src/vcp/train/run.py`**

1. **Imports.** Add `GitInfo` to the `from vcp.train.schema import (...)` block. After the `from vcp.train.env import snapshot, venv_python` line, add:

   ```python
   from vcp.train.gitstate import changed_since, require_clean
   from vcp.train.gitstate import warning as git_warning
   ```

2. **`RunSpec`.** After `labels: list[str] = Field(default_factory=list)  # label_set ids`, add:

   ```python
       require_clean: bool = False  # VCP-041: FAIL before the first write on tracked changes
   ```

3. **`RunResult`.** After `evidence_changed: list[str] = Field(default_factory=list)`, add:

   ```python
       git: GitInfo | None = None  # VCP-041: the attempt's start record (spec 2026-09-27 §3.1)
       git_changed: list[str] = Field(default_factory=list)
   ```

4. **The preflight.** In `train_run`, right after `evidence = preflight(data_root, scope, spec.evidence, spec.labels)`, add:

   ```python
       if spec.require_clean:
           require_clean(cwd)  # VCP-041 (spec 2026-09-27 §4.1): still before the first write
   ```

5. **The patch destination.** Replace `snap = snapshot(python, cwd)` with:

   ```python
       patch_rel = f"{TRAIN_DIR}/git.{n}.patch"
       snap = snapshot(python, cwd, patch=(run_root / patch_rel, patch_rel))
   ```

6. **The end check.** Right after the block that appends the `evidence_changed` note event (`if changed: append_event(...)`), add:

   ```python
       # VCP-041 (spec 2026-09-27 §4.3): HEAD and the tracked diff again, after the command.
       delta = changed_since(cwd, snap.git) if snap.git is not None else None
       git_changed = list(delta.changed) if delta is not None else []
       if delta is not None and git_changed:
           append_event(
               data_root,
               spec.run_id,
               "note",
               n,
               key="git_changed",
               value=",".join(git_changed),
               commit=delta.commit,
               diff_sha256=delta.diff_sha256,
           )
   ```

7. **The warnings.** Right after `warnings.append(f"evidence_changed={','.join(changed)}")` (inside its `if changed:`), at the same indentation as that `if`, add:

   ```python
       git_line = git_warning(snap.git) if snap.git is not None else None
       if git_line is not None:
           warnings.append(git_line)
       if git_changed:
           warnings.append(f"git_changed={','.join(git_changed)}")
   ```

8. **The result.** In the final `return RunResult(...)`, add after `evidence_changed=changed,`:

   ```python
           git=snap.git,
           git_changed=git_changed,
   ```

- [ ] **Step 4: Wire the CLI** — `src/vcp/cli_train.py` `run_cmd`

1. **The option.** Add it after the `labels` option:

   ```python
       require_clean: Annotated[
           bool,
           typer.Option(
               "--require-clean", help="FAIL before any write when tracked files have changes"
           ),
       ] = False,
   ```

2. **`RunSpec`.** Pass `require_clean=require_clean,` to `RunSpec(...)`, after `labels=list(labels or []),`.

3. **`fields`.** In the `fields` dict, after `"labels": res.labels,`, add:

   ```python
               "commit": res.git.commit[:12] if res.git is not None else "none",
   ```

4. **The optional fields.** After the `if res.evidence_changed:` block, add:

   ```python
           if res.git is not None:
               fields["modified"] = res.git.modified
               fields["untracked"] = res.git.untracked
           if res.git_changed:
               fields["git_changed"] = ",".join(res.git_changed)
   ```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/train tests/unit/test_cli_train.py tests/unit/test_e2e_train.py tests/unit/test_e2e_evidence.py`
Expected: all PASS.

- [ ] **Step 6: Lint, then commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add src/vcp/train/run.py src/vcp/cli_train.py tests/unit/train/test_run.py tests/unit/test_cli_train.py
git commit -F <message file>
```

Message: `feat(train): train run --require-clean、追蹤檔改動與中途變動的 WARN、VERDICT commit/modified/untracked`

---

### Task 4: Documentation

**Files:**
- Modify: `docs/reference/cli.md`
- Modify: `docs/superpowers/specs/2026-09-05-vcp-training-layer-design.md`
- Modify: `CLAUDE.md`, `AGENTS.md`
- Modify: `.claude/skills/vcp-train-submit-backup/SKILL.md`, then mirror to `.agents/skills/vcp-train-submit-backup/SKILL.md`
- Modify: `docs/audits/2026-09-11-vcp-improvement-audit.md`

**Interfaces:**
- Consumes: the behavior of Tasks 1–3.

Make every edit with the Edit tool; the files are UTF-8 and LF. Read each anchor first.

- [ ] **Step 1: `docs/reference/cli.md`**

In the `vcp train run` row, replace `；VERDICT \`evidence=\` \`labels=\`；\`--\` 之後是訓練命令 |` with:

```markdown
；`--require-clean`（`--cwd` 所在 repo 的追蹤檔有改動 → 第一次寫入前 FAIL `dirty_tree:`，不在 repo 裡 → `not_found:`；未追蹤檔不擋）；VERDICT `evidence=` `labels=` `commit=`（前 12 位，不在 repo 裡是 `none`）`modified=` `untracked=`；追蹤檔有改動 → WARN，diff 存成 `train/git.<n>.patch`（10 MiB 以內）；結束時 HEAD 或追蹤檔的 diff 變了 → WARN `git_changed=`；`--` 之後是訓練命令 |
```

- [ ] **Step 2: The training-layer spec** — append at the end of `docs/superpowers/specs/2026-09-05-vcp-training-layer-design.md`, with one blank line before it:

```markdown

19. **dirty 工作樹（VCP-041，2026-09-27）**：`train/env.<n>.json` 的 `git` 除了 `commit` / `dirty`，多記追蹤檔改動與未追蹤的數量和路徑（各最多 50 筆）、status 與 diff 的 sha256；追蹤檔有改動時把 `git diff HEAD --binary` 存成 `train/git.<n>.patch`（10 MiB 以內），在乾淨的 commit 上 `git apply` 就能還原。追蹤檔有改動 → WARN（`modified=`）；`--require-clean` 在第一次寫入前 FAIL `dirty_tree:`；結束時重看 HEAD 與追蹤檔的 diff，變了 → `note` 事件 `git_changed` 與 WARN。未追蹤檔只記路徑，不算改動。細節見 `2026-09-27-vcp-dirty-tree-design.md`。
```

- [ ] **Step 3: `CLAUDE.md` and `AGENTS.md`** — the same edit in both

Replace `` `train/` 放 console、config 副本、環境快照。 `` with:

```markdown
`train/` 放 console、config 副本、環境快照與 `git.<n>.patch`（`--cwd` 所在 repo 追蹤檔的未提交差異；只有追蹤檔，追蹤檔若含機密會跟著進 data root 與備份；有改動 `train run` 就 WARN，`--require-clean` 直接 FAIL）。
```

- [ ] **Step 4: The skill** — `.claude/skills/vcp-train-submit-backup/SKILL.md`

Replace the line `- \`--resume\` 加 attempt；\`train upload --run R --dest …\` 冪等，\`--only final\` 只傳最終權重。` with:

```markdown
- `--resume` 加 attempt；`train upload --run R --dest …` 冪等，`--only final` 只傳最終權重。
- `--cwd` 所在 repo 的追蹤檔有未提交改動時 `train run` WARN（`modified=`），diff 存成 `train/git.<n>.patch`；比賽要求只用 commit 過的程式就加 `--require-clean`（第一次寫入前 FAIL `dirty_tree:`）。未追蹤的輸出檔不算改動；訓練中途改了追蹤檔或 commit → WARN `git_changed=`。
```

Then mirror and check:

```bash
cp -r .claude/skills/vcp-train-submit-backup/. .agents/skills/vcp-train-submit-backup/
diff -r .claude/skills .agents/skills
uv run pytest -o addopts="" -q tests/unit/test_skills_plugin.py
```

- [ ] **Step 5: The audit** — `docs/audits/2026-09-11-vcp-improvement-audit.md`

In the §16 table, change the VCP-041 row's last cell `待 spec（Wave 1c，VCP-004 的一片）` to `已實作（隨 0.11.0）`.

Replace the paragraph under `### VCP-041：dirty 工作樹沒有警告也沒有細節` (the line starting `**狀態：待 spec（Wave 1c）。**`) with:

```markdown
**狀態：已實作，隨 0.11.0 發出（spec `2026-09-27-vcp-dirty-tree-design.md`）。** `train run` 原本只記 `dirty: true`，不 WARN、沒有 `--require-clean`、不記哪些路徑，事後無法證明只是無關的未追蹤檔。現在 `env.<n>.json` 的 `git` 記追蹤檔改動與未追蹤的數量和路徑、status 與 diff 的 sha256，追蹤檔的 diff 存成 `train/git.<n>.patch`；只有追蹤檔的改動才 WARN（`modified=`），`--require-clean` 在第一次寫入前 FAIL `dirty_tree:`；結束時 HEAD 或追蹤檔的 diff 變了 → WARN `git_changed=`。VERDICT 帶 `commit=` / `modified=` / `untracked=`。
```

- [ ] **Step 6: Run everything, check the files, commit**

```bash
uv run pytest -o addopts="" -q
uv run ruff check . && uv run ruff format --check .
git diff --check
git add docs/reference/cli.md docs/superpowers/specs/2026-09-05-vcp-training-layer-design.md CLAUDE.md AGENTS.md .claude/skills/vcp-train-submit-backup/SKILL.md .agents/skills/vcp-train-submit-backup/SKILL.md docs/audits/2026-09-11-vcp-improvement-audit.md
git commit -F <message file>
```

Message: `docs: dirty 工作樹的命令參考、spec 修訂、skill 與稽核狀態`

Expected: the full suite passes (the 1805 of main at ed228ef plus this plan's new tests), with 76 skips.

---

### Task 5: Release 0.11.0 (VCP-040 + 042 and VCP-041)

This task runs only after the VCP-041 feature PR is merged into `main`, and only with the user's approval. It follows `CHANGELOG.md`'s four release steps on a release branch from the updated `main`, like 0.10.0 (PR #29).

**Files:**
- Modify: `src/vcp/__init__.py`, `.claude/.claude-plugin/plugin.json`, `CHANGELOG.md`

- [ ] **Step 1: Version bump**

1. In `src/vcp/__init__.py`, set `__version__ = "0.11.0"`.
2. In `.claude/.claude-plugin/plugin.json`, set `"version": "0.11.0"`.

- [ ] **Step 2: CHANGELOG** — insert above `## [0.10.0] - 2026-09-25`. Use the release day's date, and the number of the VCP-041 PR that `gh pr view` reports.

```markdown
## [0.11.0] - <release date>

RSNA Knee 第二輪回報的 VCP-040 + 042（#30）與 VCP-041（#<VCP-041 PR>）。MINOR 的理由：
- 新命令 `vcp data labels`。
- 新選項：
  - `train run --evidence / --labels / --require-clean`；
  - `eval ingest --evidence / --labels`。
- VERDICT 新欄位：`evidence=`、`labels=`、`evidence_changed=`、`commit=`、`modified=`、`untracked=`、`git_changed=`。
- `reason=` 新字：`labels_outside_subsets:`、`labels_on_sealed:`、`labels_mismatch:`、`evidence_conflict:`、`role_reserved:`、`dirty_tree:`；既有的 `invalid:` 也用在標籤列格式與 `--evidence` 參數上。
- 新產物種類 `label_set`、`evidence`。
- 寫入內容的改變：
  - `run.yaml` / `train.yaml` 的 `evidence` 清單；
  - `train.log.jsonl` 的 `evidence` 事件與 `note` 的 `evidence_changed` / `git_changed`；
  - `env.<n>.json` 的 `git` 新欄位與 `train/git.<n>.patch`；
  - 備份新角色。

### Added
- `vcp data labels`：訓練標籤檔（`.csv` / `.jsonl`，一個 id 一列）對切分 plan 驗過後，存成不可變的 `label_set/<id>`。
  - 落在允許子集以外的 dataset 樣本 → FAIL `labels_outside_subsets:`。
  - sealed 子集不能標。
  - 不在 dataset 裡的列記 `external=`。
  - 一列都沒對到 → WARN。
- 證據檔的不可變副本 `evidence/<run>-<name>-<sha12>`，以及 `run.yaml` / `train.yaml` 的 `evidence` 參照清單（空的時候不寫出）。附上的入口有三個：
  - `train run --evidence NAME=PATH --labels ID`；
  - `Session.attach_evidence / attach_labels`；
  - `eval ingest --evidence / --labels`。
- `train status`：多了 `evidence=` / `labels=`，`--verify` 驗證據產物。`eval status`：多了每個 run 的 `labels`。
- 備份：新角色 `label_set`（tier 1）、`evidence`（tier 2）。
- provenance 圖：「產物 → run」的 `CONSUMED_BY` 邊。
- `train run --require-clean`：`--cwd` 所在 repo 的追蹤檔有改動時，在第一次寫入前 FAIL `dirty_tree:`。

### Changed
- `train run` 在追蹤檔有改動時 WARN（`modified=`），並把 diff 存成 `train/git.<n>.patch`（10 MiB 以內）。`env.<n>.json` 的 `git` 多記改動與未追蹤的數量和路徑、status 與 diff 的 sha256。結束時 HEAD 或追蹤檔的 diff 變了 → WARN `git_changed=`。
- `register_checkpoint` 只給權重用。其他檔改用 `attach_evidence`。

### 相容性
- 新 vcp 照讀舊紀錄。
- 舊 vcp 讀不了的東西：
  - 帶 `evidence` 的 `run.yaml` / `train.yaml`：`extra_forbidden` FAIL；
  - 帶新角色的備份清單。
- 訓練 venv 若裝的是舊版、非 editable 的 vcp，wrapper 附上 `--evidence` / `--labels` 後，`Session.register_checkpoint` 會失敗。訓練 venv 要跟著升級。
- `GitInfo.dirty` 的意思不變。`env.<n>.json` 的新欄位都有預設值。
```

- [ ] **Step 3: Verify**

```bash
uv sync --reinstall-package vcp
uv run pytest --cov=vcp
uv run ruff check . && uv run ruff format --check .
```

Expected: pass, with coverage ≥ 80%. `tests/unit/test_package.py` and `tests/unit/test_skills_plugin.py` agree on 0.11.0.

- [ ] **Step 4: Commit, PR, tag** — only on the user's approval

1. Commit `chore(release): v0.11.0`.
2. Open the release PR and merge it after CI.
3. Tag the merge commit and push the tag:

   ```bash
   git tag -a v0.11.0 -m "vcp 0.11.0"
   git push origin v0.11.0
   ```
