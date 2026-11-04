"""The in-memory scan of one code root (spec 2026-10-31 §4.1-§4.2): the files that make its
identity, their shas, the identity itself, and which of them a snapshot keeps the bytes of.

A scan reads the disk and writes nothing. Files that rules v1 does not take are never opened:
secrets are not even stat-ed, an untracked ``CONFIG`` file is not read, a symlink or junction is
not followed, and a path under an ignored, hidden, virtualenv or ``skip_dirs`` directory is not
listed, nor (in a plain directory) one inside another repository's work tree. The limits are
read from ``vcp.code.rules`` when the scan runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from vcp.code import rules
from vcp.code.schema import RootKind
from vcp.core.hashing import sha256_file
from vcp.core.paths import is_under, key_within, path_key
from vcp.core.proc import git_environment

__all__ = [
    "IndexEntry",
    "RootKind",
    "RootScan",
    "ScanReason",
    "ScanStopped",
    "identity_sha256",
    "index_jsonl_bytes",
    "is_linked",
    "is_under",
    "nearest_dot_git",
    "path_key",
    "root_kind",
    "scan_root",
    "select_kept",
    "snapshot_json_bytes",
    "walk_files",
]

ScanReason = Literal["too_large", "unreadable", "not_found"]
Keep = Literal["capped", "all"]
_SNAPSHOT_SCHEMA = 1
_KEEP_MODES = ("capped", "all")
# IO_REPARSE_TAG_MOUNT_POINT, the tag of a junction; `stat` has no name for it off Windows.
_MOUNT_POINT_REPARSE_TAG = 0xA0000003


@dataclass(frozen=True)
class IndexEntry:
    """One file of a root: its root-relative posix path, content sha256 and size, and whether the
    snapshot keeps its bytes (``kept`` is not part of the identity)."""

    path: str
    sha256: str
    bytes: int
    kept: bool = False


def _by_path(entry: IndexEntry) -> str:
    return entry.path


def identity_sha256(entries: Iterable[IndexEntry]) -> str:
    """``sha256("vcp-code/1\\n" + "<sha256> <bytes> <path>\\n" per file)``, ordered by path."""
    digest = hashlib.sha256(rules.IDENTITY_PREFIX.encode("utf-8"))
    for entry in sorted(entries, key=_by_path):
        digest.update(f"{entry.sha256} {entry.bytes} {entry.path}\n".encode())
    return digest.hexdigest()


def select_kept(entries: Sequence[IndexEntry], keep: Keep) -> tuple[IndexEntry, ...]:
    """The entries in path order with ``kept`` set. ``all`` keeps every file. ``capped`` walks the
    paths in order and keeps a file of at most ``KEEP_FILE_LIMIT`` bytes while the kept total stays
    within ``KEEP_ROOT_LIMIT``; a file that is not kept adds nothing to the total."""
    if keep not in _KEEP_MODES:
        raise ValueError(f"keep must be one of {_KEEP_MODES}, got {keep!r}")
    ordered = sorted(entries, key=_by_path)
    if keep == "all":
        return tuple(replace(entry, kept=True) for entry in ordered)
    total = 0
    selected = []
    for entry in ordered:
        fits = entry.bytes <= rules.KEEP_FILE_LIMIT and total + entry.bytes <= rules.KEEP_ROOT_LIMIT
        if fits:
            total += entry.bytes
        selected.append(replace(entry, kept=fits))
    return tuple(selected)


def snapshot_json_bytes(entries: Sequence[IndexEntry]) -> bytes:
    """The bytes of ``snapshot.json`` -- what ``ArtifactWriter.write_json`` writes for the same
    object. Nothing in it depends on the machine, the time or ``kept``, so one content has one
    ``snapshot.json`` wherever it was scanned."""
    doc = {
        "schema_version": _SNAPSHOT_SCHEMA,
        "rules": rules.RULES_VERSION,
        "identity_sha256": identity_sha256(entries),
        "files": len(entries),
        "bytes": sum(entry.bytes for entry in entries),
    }
    return (json.dumps(doc, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def index_jsonl_bytes(entries: Iterable[IndexEntry]) -> bytes:
    """The bytes of ``index.jsonl``: one line per file, ordered by path."""
    lines = (
        json.dumps(
            {"path": e.path, "sha256": e.sha256, "bytes": e.bytes, "kept": e.kept},
            ensure_ascii=False,
        )
        + "\n"
        for e in sorted(entries, key=_by_path)
    )
    return "".join(lines).encode("utf-8")


@dataclass(frozen=True)
class RootScan:
    """What one scan found. With a ``reason`` the root could not be scanned: ``entries``,
    ``untracked_code`` and ``excluded_paths`` are empty and there is no identity."""

    root: Path
    kind: RootKind
    entries: tuple[IndexEntry, ...]
    untracked_code: tuple[str, ...]  # included untracked CODE files of a git root
    # Secret names, symlinks and junctions, and (a plain-directory root only) directories that hold
    # a ``.git`` entry: not opened, not entered, not listed.
    excluded_paths: tuple[str, ...]
    reason: ScanReason | None

    @property
    def identity_sha256(self) -> str | None:
        return None if self.reason else identity_sha256(self.entries)

    @property
    def snapshot_id(self) -> str | None:
        identity = self.identity_sha256
        return None if identity is None else f"code-{identity[:16]}"

    @property
    def files(self) -> int:
        return len(self.entries)

    @property
    def kept(self) -> int:
        return sum(1 for entry in self.entries if entry.kept)

    @property
    def not_kept(self) -> int:
        return self.files - self.kept

    @property
    def bytes(self) -> int:
        return sum(entry.bytes for entry in self.entries)


class ScanStopped(Exception):
    """The root cannot be scanned; ``reason`` says why."""

    def __init__(self, reason: ScanReason) -> None:
        super().__init__(reason)
        self.reason = reason


def _failed(root: Path, kind: RootKind, reason: ScanReason) -> RootScan:
    return RootScan(
        root=root, kind=kind, entries=(), untracked_code=(), excluded_paths=(), reason=reason
    )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    """``git -C root <args>`` in the git environment; None when there is no git to run."""
    exe = shutil.which("git")
    if exe is None:
        return None
    return subprocess.run([exe, "-C", str(root), *args], capture_output=True, env=git_environment())


def nearest_dot_git(path: Path) -> Path | None:
    """The nearest directory at or above ``path`` that holds a ``.git`` file or directory -- a
    work tree, a linked worktree and a submodule checkout all have one -- resolved; None when no
    directory above ``path`` does."""
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):  # a path that cannot be resolved is no work tree we can name
        return None
    for directory in (resolved, *resolved.parents):
        if os.path.lexists(directory / ".git"):
            return directory
    return None


def root_kind(path: Path) -> RootKind:
    """``git`` when ``path`` is inside a git work tree, else ``dir``.

    A tree that carries a ``.git`` entry stays ``git`` when git cannot be asked -- no git on PATH,
    or git refuses the repository (dubious ownership on a WSL mount or a bind mount). Taken as a
    plain directory it would be walked without ``.gitignore``, so ignored files would be read and
    kept (spec §7); as ``git`` its scan fails closed (``unreadable``) instead."""
    try:
        proc = _git(path, "rev-parse", "--show-toplevel")
    except OSError:
        proc = None
    if proc is not None and proc.returncode == 0:
        return "git"
    return "git" if nearest_dot_git(path) is not None else "dir"


def _ls_files(root: Path, *args: str) -> list[str]:
    """The paths ``git ls-files`` lists under ``root``, relative to it, as UTF-8 text."""
    try:
        proc = _git(root, "ls-files", "-z", *args)
    except OSError as e:
        raise ScanStopped("unreadable") from e
    if proc is None or proc.returncode != 0:
        raise ScanStopped("unreadable")
    try:
        text = proc.stdout.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ScanStopped("unreadable") from e
    return [path for path in text.split("\0") if path]


def _is_link(st: os.stat_result) -> bool:
    """A symlink, or a Windows junction (a mount-point reparse point; only Windows stat results
    have ``st_reparse_tag``)."""
    return (
        stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", None) == _MOUNT_POINT_REPARSE_TAG
    )


def _lstat(path: str) -> os.stat_result | None:
    """``os.lstat``; None when the path is gone (or a parent is no longer a directory)."""
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def is_linked(path: Path) -> bool:
    """``path`` is a symlink or a Windows junction (``lstat``: nothing is followed). A path that is
    gone is not."""
    st = _lstat(str(path))
    return st is not None and _is_link(st)


@dataclass(frozen=True)
class _Taken:
    path: str
    absolute: str
    size: int
    tracked: bool


class _Selector:
    """Rules v1 for the paths of one root: which are dropped, which are only reported, which are
    taken. A path is judged from its name first; the disk is asked only about directories above
    it (once each) and about the file itself, and never about a secret."""

    def __init__(self, root: Path, skip_dirs: Sequence[Path]) -> None:
        self.root = str(root)
        # Entries are keyed lexically below the resolved root, which is right for every entry
        # that is not behind a link, and links are not followed. This avoids resolving each path.
        self.base = path_key(root).rstrip(os.sep) + os.sep
        self.skip_keys = tuple(path_key(Path(directory)) for directory in skip_dirs)
        self.excluded: set[str] = set()
        self._ancestors: dict[str, Literal["ok", "venv", "link"]] = {}

    def key(self, parts: Sequence[str]) -> str:
        return self.base + os.path.normcase(os.sep.join(parts))

    def in_skip_dir(self, key: str) -> bool:
        return any(key_within(key, skip) for skip in self.skip_keys)

    def _ancestor(self, rel_dir: str) -> Literal["ok", "venv", "link"]:
        state = self._ancestors.get(rel_dir)
        if state is None:
            absolute = os.path.join(self.root, *rel_dir.split("/"))
            st = _lstat(absolute)
            if st is not None and _is_link(st):
                state = "link"
            elif os.path.exists(os.path.join(absolute, "pyvenv.cfg")):
                state = "venv"
            else:
                state = "ok"
            self._ancestors[rel_dir] = state
        return state

    def behind_a_boundary(self, parts: Sequence[str]) -> bool:
        """The path lies below a virtualenv (dropped) or a linked directory (not entered: the
        link is reported instead)."""
        for depth in range(1, len(parts)):
            rel_dir = "/".join(parts[:depth])
            state = self._ancestor(rel_dir)
            if state == "link":
                self.excluded.add(rel_dir)
            if state != "ok":
                return True
        return False

    def take(self, candidates: Iterable[tuple[str, bool]]) -> list[_Taken]:
        taken = []
        for path, tracked in candidates:
            item = self._judge(path, tracked)
            if item is not None:
                taken.append(item)
        return taken

    def _judge(self, path: str, tracked: bool) -> _Taken | None:
        if path.endswith("/"):  # an untracked repository inside the root is listed as a directory
            return None
        parts = path.split("/")
        if any(rules.skipped_segment(part) for part in parts):
            return None
        if self.in_skip_dir(self.key(parts)) or self.behind_a_boundary(parts):
            return None
        name = parts[-1]
        if rules.is_secret(name):
            self.excluded.add(path)
            return None
        absolute = os.path.join(self.root, *parts)
        st = _lstat(absolute)
        if st is None:  # tracked but deleted
            return None
        if _is_link(st):
            self.excluded.add(path)
            return None
        group = rules.file_class(name)
        if group is None or (group == "config" and not tracked):
            return None
        if not stat.S_ISREG(st.st_mode):  # a submodule is a directory
            return None
        return _Taken(path, absolute, st.st_size, tracked)


def _git_candidates(root: Path) -> list[tuple[str, bool]]:
    tracked = list(dict.fromkeys(_ls_files(root, "--cached")))
    known = set(tracked)
    loose = [p for p in _ls_files(root, "--others", "--exclude-standard") if p not in known]
    return [*((path, True) for path in tracked), *((path, False) for path in loose)]


def _holds_dot_git(directory: str) -> bool:
    """The directory holds a ``.git`` file or directory (or a link of that name): it is the top
    of another work tree -- a clone, a linked worktree, a submodule checkout."""
    return os.path.lexists(os.path.join(directory, ".git"))


def _walk_candidates(root: Path, selector: _Selector) -> list[tuple[str, bool]]:
    """Every file below ``root`` that is neither skipped nor behind a link. Skipped entries (a
    hidden or cache segment, a virtualenv, a ``skip_dirs`` directory) are not entered and do not
    count toward ``MAX_WALK_ENTRIES``. A directory below ``root`` that holds a ``.git`` entry is
    another repository: walked as a plain directory its ignored files would be read, so it is not
    entered and is listed in ``excluded_paths`` instead, as a link is (spec §7)."""
    limit = rules.MAX_WALK_ENTRIES
    seen = 0
    files: list[tuple[str, bool]] = []
    pending = [("", str(root), selector.base)]  # (relative prefix, directory, key prefix)
    try:
        while pending:
            prefix, directory, key_prefix = pending.pop()
            try:
                with os.scandir(directory) as it:
                    entries = list(it)
            except (FileNotFoundError, NotADirectoryError):
                continue
            for entry in entries:
                if rules.skipped_segment(entry.name):
                    continue
                key = key_prefix + os.path.normcase(entry.name)
                if selector.in_skip_dir(key):
                    continue
                linked = entry.is_symlink() or entry.is_junction()
                is_dir = not linked and entry.is_dir(follow_symlinks=False)
                if is_dir and os.path.exists(os.path.join(entry.path, "pyvenv.cfg")):
                    continue
                seen += 1
                if seen > limit:
                    raise ScanStopped("too_large")
                path = prefix + entry.name
                if linked or (is_dir and _holds_dot_git(entry.path)):
                    selector.excluded.add(path)
                elif is_dir:
                    pending.append((path + "/", entry.path, key + os.sep))
                else:
                    files.append((path, False))
    except OSError as e:
        raise ScanStopped("unreadable") from e
    return files


def walk_files(root: Path, skip_dirs: Sequence[Path] = ()) -> list[str]:
    """The root-relative posix path of every file below ``root`` that is neither skipped nor
    behind a link nor inside a nested work tree, in path order: the walk a plain-directory scan
    takes, under the same limits. Too many entries or a directory that cannot be read stops it as
    it stops a scan (``ScanStopped``, ``too_large`` / ``unreadable``). Nothing is opened; which of
    these files the rules take (``file_class``, secrets) is for the caller."""
    root = Path(root)
    walked = _walk_candidates(root, _Selector(root, skip_dirs))
    return sorted(path for path, _ in walked)


def _hash(taken: Sequence[_Taken]) -> list[IndexEntry]:
    entries = []
    for item in sorted(taken, key=lambda t: t.path):
        try:
            digest = sha256_file(Path(item.absolute))
        except (FileNotFoundError, NotADirectoryError):  # it went away since it was listed
            continue
        except OSError as e:
            raise ScanStopped("unreadable") from e
        entries.append(IndexEntry(item.path, digest, item.size))
    return entries


def _scan(root: Path, kind: RootKind, skip_dirs: Sequence[Path], keep: Keep) -> RootScan:
    selector = _Selector(root, skip_dirs)
    candidates = _git_candidates(root) if kind == "git" else _walk_candidates(root, selector)
    taken = selector.take(candidates)
    if len(taken) > rules.MAX_FILES or sum(t.size for t in taken) > rules.MAX_HASH_BYTES:
        raise ScanStopped("too_large")  # nothing has been hashed yet
    entries = select_kept(_hash(taken), keep)
    untracked = {t.path for t in taken if not t.tracked} if kind == "git" else set()
    return RootScan(
        root=root,
        kind=kind,
        entries=entries,
        untracked_code=tuple(entry.path for entry in entries if entry.path in untracked),
        excluded_paths=tuple(sorted(selector.excluded)),
        reason=None,
    )


def scan_root(
    root: Path,
    *,
    kind: RootKind,
    skip_dirs: Sequence[Path] = (),
    keep: Keep = "capped",
) -> RootScan:
    """Scan one code root. ``kind="git"`` takes the paths ``git ls-files`` gives for the root's
    subtree (tracked CODE and CONFIG files, untracked non-ignored CODE files) and reads the work
    tree's bytes; ``kind="dir"`` walks the directory and takes CODE files only, never entering a
    directory that holds a ``.git`` entry (listed in ``excluded_paths``). Paths under any of
    ``skip_dirs`` are neither listed nor entered. ``keep`` chooses which files keep their bytes
    (``select_kept``). A root that cannot be scanned comes back with a ``reason`` and no entries."""
    if kind not in ("git", "dir"):
        raise ValueError(f"kind must be 'git' or 'dir', got {kind!r}")
    if keep not in _KEEP_MODES:
        raise ValueError(f"keep must be one of {_KEEP_MODES}, got {keep!r}")
    root = Path(root)
    try:
        if not root.is_dir():
            return _failed(root, kind, "not_found")
        return _scan(root, kind, skip_dirs, keep)
    except ScanStopped as stop:
        return _failed(root, kind, stop.reason)
    except OSError:
        return _failed(root, kind, "unreadable")
