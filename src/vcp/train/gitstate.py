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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.train.schema import GitInfo

PATH_LIMIT = 50  # paths kept per list; the counts stay exact
PATCH_LIMIT = 10 * 1024 * 1024  # a larger diff keeps its sha256 and size, not the file
SHOWN_PATHS = 5  # paths named in a dirty_tree: message
_CHUNK = 1024 * 1024
# Each pin keeps the record a function of the repository, not of the user's git config:
# - --untracked-files=normal: status.showUntrackedFiles cannot hide untracked files.
# - --ignore-submodules=untracked, on status and diff alike: a file a run writes inside a
#   submodule is not a change (decision 1); a moved submodule pointer, or an edited tracked
#   file inside the submodule, still is.
# - core.quotepath=false: a non-ASCII path gives the same diff_sha256 on every machine.
# - --binary, --no-color, --no-ext-diff, --no-textconv: git's own diff bytes, which git apply
#   takes back, whatever colour, external-diff or textconv settings are around.
# - --src-prefix=a/ --dst-prefix=b/: diff.noprefix / diff.mnemonicPrefix cannot break git apply.
# - --submodule=short: diff.submodule=log|diff cannot turn a pointer change into a summary git
#   apply rejects, or inline the submodule's own files.
# - the closing --: a top-level path named HEAD cannot make the revision ambiguous.
STATUS_ARGS = (
    "status",
    "--porcelain=v1",
    "-z",
    "--untracked-files=normal",
    "--ignore-submodules=untracked",
)
DIFF_ARGS = (
    "-c",
    "core.quotepath=false",
    "diff",
    "HEAD",
    "--binary",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--submodule=short",
    "--ignore-submodules=untracked",
    "--",
)
# git's repository-local variables (`git rev-parse --local-env-vars`) and the GIT_CONFIG_COUNT
# pairs. Inherited from a git hook, `git submodule foreach` or a shell, they would point every
# call at another repository and copy its content into the patch.
_LOCAL_ENV = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_OBJECT_DIRECTORY",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_SHALLOW_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_PREFIX",
    }
)
_CONFIG_PAIRS = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


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


class _GitFailed(Exception):
    """git gave no answer. The message says why: git's first stderr line, or no git at all."""


def _environment() -> dict[str, str]:
    """The environment of every git call: without git's repository-local variables, so only the
    repository containing ``--cwd`` is looked at; with ``GIT_OPTIONAL_LOCKS=0``, so a look never
    rewrites that repository's index."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _LOCAL_ENV and not key.startswith(_CONFIG_PAIRS)
    }
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _command(cwd: Path, args: Sequence[str]) -> list[str] | None:
    """``git -C <cwd> <args>`` with the git on PATH; None when there is none."""
    exe = shutil.which("git")
    return None if exe is None else [exe, "-C", str(cwd), *args]


def _git(args: Sequence[str], cwd: Path) -> bytes:
    """The stdout of one git command; raises ``_GitFailed`` when git fails or is missing."""
    command = _command(cwd, args)
    if command is None:
        raise _GitFailed("git not found on PATH")
    try:
        proc = subprocess.run(command, capture_output=True, env=_environment())
    except OSError as e:
        raise _GitFailed(f"git could not start: {e}") from e
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise _GitFailed(lines[0].strip() if lines else f"git exited {proc.returncode}")
    return proc.stdout


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


def _inspect(cwd: Path) -> GitStatus:
    """``probe``, raising ``_GitFailed`` with the reason instead of returning None."""
    top = _git(["rev-parse", "--show-toplevel"], cwd)
    root = Path(top.decode("utf-8", errors="replace").strip())
    head = _git(["rev-parse", "HEAD"], root)
    raw = _git(STATUS_ARGS, root)
    modified, untracked = parse_porcelain(raw)
    return GitStatus(
        top=root,
        commit=head.decode("ascii", errors="replace").strip(),
        raw=raw,
        modified=tuple(modified),
        untracked=tuple(untracked),
    )


def probe(cwd: Path) -> GitStatus | None:
    """The status of the repository containing ``cwd``; None without git, outside a repository,
    before its first commit, or when a git command fails."""
    try:
        return _inspect(cwd)
    except _GitFailed:
        return None


def stream_diff(top: Path, patch: Path | None = None, limit: int | None = None) -> Diff | None:
    """Hash all of ``git diff HEAD --binary``; also write it to ``patch`` when it is not empty and
    fits in ``limit`` bytes (default ``PATCH_LIMIT``), ``.tmp`` first and then renamed. None when
    git fails."""
    cap = PATCH_LIMIT if limit is None else limit
    command = _command(top, DIFF_ARGS)
    if command is None:
        return None
    digest = hashlib.sha256()
    size = 0
    kept: list[bytes] = []
    try:
        with subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_environment()
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
        try:
            tmp.write_bytes(b"".join(kept))
            os.replace(tmp, patch)
        except OSError:
            tmp.unlink(missing_ok=True)  # no stray .tmp; the attempt still ABORTs
            raise
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
    untracked files pass, and a tree git cannot see cannot be proven clean. The message says
    why git gave no answer; both FAILs carry ``commit=`` (``none`` when there is no HEAD)."""
    try:
        status = _inspect(cwd)
    except _GitFailed as e:
        raise ValidationFailed(
            f"not_found: git repository for --cwd {cwd} ({e})", fields={"commit": "none"}
        ) from e
    if status.modified:
        shown = ", ".join(status.modified[:SHOWN_PATHS])
        raise ValidationFailed(
            f"dirty_tree: {len(status.modified)} tracked path(s) changed: {shown}",
            fields={"modified": len(status.modified), "commit": status.commit[:12]},
        )


def changed_since(cwd: Path, start: GitInfo) -> GitDelta:
    """spec 2026-09-27 §4.3: HEAD and the tracked diff again, no patch written. Untracked files
    are not compared -- a run writing its outputs into the repository is expected. An empty
    diff counts as no diff, at the start and now alike."""
    status = probe(cwd)
    if status is None:
        return GitDelta(("unavailable",), None, None)
    diff_sha: str | None = None
    if status.modified:
        diff = stream_diff(status.top)
        if diff is None:
            return GitDelta(("unavailable",), status.commit, None)
        diff_sha = diff.sha256 if diff.size > 0 else None
    start_sha = start.diff_sha256 if (start.patch_bytes or 0) > 0 else None
    changed = tuple(
        name
        for name, now, then in (
            ("commit", status.commit, start.commit),
            ("diff", diff_sha, start_sha),
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
