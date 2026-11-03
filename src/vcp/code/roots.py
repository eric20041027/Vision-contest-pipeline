"""Which code roots a training run has, and what each of them leaves out (spec 2026-10-31 §5.1
step 4, §4.2, §4.3).

A run's code is the working directory's repository, the script the command runs, every editable
distribution of the training environment, and any ``--code-root``. This module only plans: it
reads the process environment, asks git, and never reads a file's content. A file belongs to the
innermost root that holds it (``skip_dirs_for``), so a root nested in another is skipped by the
outer one.

``editables_of`` is the seam tests replace: callers reach it as ``roots.editables_of(...)``, never
by an imported name, so a test can give a run an editable root without installing anything.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import url2pathname

from vcp.code.generated import generated_locations
from vcp.code.scan import RootKind, nearest_dot_git, root_kind
from vcp.code.schema import DirectUrl, Distribution, RootSource
from vcp.core.paths import is_under, key_within, path_key
from vcp.core.proc import git_environment, redact

__all__ = [
    "Editable",
    "PlannedRoot",
    "canonical_name",
    "checkpoint_exclusions",
    "clean_url",
    "distribution_record",
    "editable_dir",
    "editables_of",
    "generated_exclusions",
    "plan_roots",
    "repo_top",
    "script_of",
    "skip_dirs_for",
]

_WILDCARDS = frozenset("*?[")
_UNREADABLE_URL = "<redacted>"  # what a URL that cannot be taken apart is recorded as


@dataclass(frozen=True)
class PlannedRoot:
    """One root of a run. ``path`` is resolved; ``commit`` is the HEAD of the repository that
    holds it (a git root only, and None for a repository without a commit or one git cannot
    read)."""

    label: str
    source: RootSource
    path: Path
    kind: RootKind
    commit: str | None


@dataclass(frozen=True)
class Editable:
    """An editable distribution and the directory its ``direct_url.json`` points at."""

    name: str
    path: Path


# -- distributions ------------------------------------------------------------------------------


def canonical_name(name: str) -> str:
    """The PEP 503 normalised name: runs of ``-``, ``_`` and ``.`` become one ``-``, lowercase."""
    return re.sub(r"[-_.]+", "-", name).lower()


def clean_url(url: str) -> str:
    """``url`` as it may be recorded: no userinfo, query or fragment, then ``redact``. A URL that
    cannot be taken apart is not recorded at all."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return _UNREADABLE_URL
    host = parts.netloc.rpartition("@")[2]
    return redact(urlunsplit((parts.scheme, host, parts.path, "", "")))


def editable_dir(url: str) -> Path | None:
    """The directory a ``file:`` URL names; None for any other scheme or a URL that cannot be read.
    A host other than ``localhost`` makes it a UNC path (on Windows)."""
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() != "file":
            return None
        path = parts.path
        if parts.netloc and parts.netloc.lower() != "localhost":
            path = f"//{parts.netloc}{path}"
        return Path(url2pathname(path))  # url2pathname decodes the %XX escapes itself
    except (ValueError, OSError):  # an invalid host, a malformed drive letter
        return None


def _direct_url(raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The row's ``direct_url`` when it is a mapping with a string ``url``."""
    direct = raw.get("direct_url")
    if isinstance(direct, Mapping) and isinstance(direct.get("url"), str):
        return direct
    return None


def _is_editable(direct: Mapping[str, Any]) -> bool:
    info = direct.get("dir_info")
    return isinstance(info, Mapping) and info.get("editable") is True


def distribution_record(raw: Mapping[str, Any]) -> Distribution:
    """The record of one PROBE row (``name``, ``version``, ``record_sha256`` and ``direct_url`` as
    ``direct_url.json`` holds it, or None). The URL is cleaned here, so what is recorded is never
    what a root is planned from (``editables_of``)."""
    direct = _direct_url(raw)
    recorded = None
    if direct is not None:
        vcs = direct.get("vcs_info")
        commit = vcs.get("commit_id") if isinstance(vcs, Mapping) else None
        recorded = DirectUrl(
            url=clean_url(direct["url"]),
            editable=_is_editable(direct),
            commit_id=commit if isinstance(commit, str) else None,
        )
    return Distribution(
        name=raw.get("name"),  # a missing name or version is pydantic's to refuse
        version=raw.get("version"),
        record_sha256=raw.get("record_sha256"),
        direct_url=recorded,
    )


def editables_of(raw: Sequence[Mapping[str, Any]]) -> list[Editable]:
    """The editable distributions of PROBE rows, in order, each with its directory. The directory
    comes from the raw URL: the recorded one passed ``redact``, which turns a long path into
    ``<redacted>``."""
    found = []
    for row in raw:
        direct = _direct_url(row)
        name = row.get("name")
        if direct is None or not isinstance(name, str) or not _is_editable(direct):
            continue
        path = editable_dir(direct["url"])
        if path is not None:
            found.append(Editable(name, path))
    return found


# -- finding the roots ----------------------------------------------------------------------------


def _git_line(path: Path, *args: str) -> str | None:
    """The first line ``git -C path <args>`` prints; None when git is missing, refuses or fails."""
    exe = shutil.which("git")
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, "-C", str(path), *args], capture_output=True, env=git_environment()
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return os.fsdecode(proc.stdout).rstrip("\r\n") or None


def repo_top(path: Path) -> tuple[Path, str | None] | None:
    """``(top of the repository holding path, its HEAD)``; the commit is None in a repository
    without one. None when ``path`` is in no repository.

    Where git cannot be asked -- missing, or it refuses the repository -- the nearest directory
    above ``path`` that holds a ``.git`` entry is the top and the commit is unknown: the rule
    ``scan.root_kind`` follows, so a tree is never ``git`` for one and a plain directory for the
    other."""
    top = _git_line(path, "rev-parse", "--show-toplevel")
    if top is None:
        fallback = nearest_dot_git(path)
        return None if fallback is None else (fallback, None)
    top_path = Path(top).resolve()
    return top_path, _git_line(top_path, "rev-parse", "HEAD")


def script_of(command: Sequence[str], cwd: Path) -> list[Path]:
    """Every token of ``command`` that ends in ``.py`` (any case) and names a file, resolved, in
    order. A relative token is read from ``cwd`` -- where the child runs -- not from the
    wrapper's own directory."""
    found = []
    for token in command:
        if not token.casefold().endswith(".py"):
            continue
        candidate = Path(cwd) / token
        try:
            if candidate.is_file():
                found.append(candidate.resolve())
        except (OSError, RuntimeError):  # a token the filesystem cannot hold is not a script
            continue
    return found


def _top_or_self(path: Path) -> Path:
    """The top of the repository holding ``path``; ``path`` itself outside any repository."""
    top = repo_top(path)
    return top[0] if top is not None else Path(path).resolve()


def _planned(label: str, source: RootSource, path: Path) -> PlannedRoot:
    path = Path(path).resolve()
    kind = root_kind(path)
    found = repo_top(path) if kind == "git" else None
    return PlannedRoot(label, source, path, kind, found[1] if found else None)


def plan_roots(
    *,
    cwd: Path,
    command: Sequence[str],
    editables: Sequence[Editable],
    extra: Sequence[Path],
) -> list[PlannedRoot]:
    """The roots of a run, in order: ``cwd`` (the top of ``cwd``'s repository, else ``cwd``),
    ``script``, ``dist-<name>`` for each editable, ``root-<i>`` for each ``extra`` (``i`` counts
    from 1 in ``extra``, whatever is dropped before it). One path is one root: the first spelling
    stays. ``script`` is the first ``.py`` of the command that no other root holds; its root is its
    repository's top, else its directory."""
    cwd = Path(cwd).resolve()
    wanted: list[tuple[str, RootSource, Path]] = [("cwd", "cwd", _top_or_self(cwd))]
    wanted += [(f"dist-{canonical_name(e.name)}", "editable", e.path) for e in editables]
    wanted += [(f"root-{i}", "extra", Path(path)) for i, path in enumerate(extra, start=1)]
    planned: list[PlannedRoot] = []
    seen: set[str] = set()
    for label, source, path in wanted:
        key = path_key(path)
        if key not in seen:
            seen.add(key)
            planned.append(_planned(label, source, path))
    for script in script_of(command, cwd):
        if not any(is_under(script, root.path) for root in planned):
            planned.insert(1, _planned("script", "script", _top_or_self(script.parent)))
            break
    return planned


# -- what a root leaves out ----------------------------------------------------------------------


def _strictly_inside(path: Path, directory: Path) -> bool:
    key, base = path_key(path), path_key(directory)
    return key != base and key_within(key, base)


def skip_dirs_for(roots: Sequence[PlannedRoot], i: int, *, fixed: Sequence[Path]) -> list[Path]:
    """What the scan of ``roots[i]`` does not enter: ``fixed`` (the data root, the configs root,
    the excluded directories) and every other root strictly inside it -- a file belongs to the
    innermost root."""
    here = roots[i].path
    nested = [
        root.path for j, root in enumerate(roots) if j != i and _strictly_inside(root.path, here)
    ]
    return [*fixed, *nested]


def _within_roots(directories: Iterable[Path], roots: Sequence[PlannedRoot]) -> list[Path]:
    """The directories strictly inside some root, resolved, once each, in ``path_key`` order. One
    that is a root, or holds one, is left out: a skip directory at or above a root removes
    everything the scan of that root would find."""
    kept: dict[str, Path] = {}
    for directory in directories:
        resolved = Path(directory).resolve()
        key = path_key(resolved)
        holds_a_root = any(key_within(path_key(root.path), key) for root in roots)
        if not holds_a_root and any(_strictly_inside(resolved, root.path) for root in roots):
            kept[key] = resolved
    return [kept[key] for key in sorted(kept)]


def _glob_base(pattern: str) -> str:
    """The directory part of a checkpoint glob before its first wildcard segment (``/`` or ``\\``
    separate segments); with no wildcard, the file's directory. Empty when there is none."""
    segments = pattern.replace("\\", "/").split("/")
    for depth, segment in enumerate(segments):
        if _WILDCARDS.intersection(segment):
            return "/".join(segments[:depth])
    return "/".join(segments[:-1])


def checkpoint_exclusions(
    patterns: Sequence[str], cwd: Path, roots: Sequence[PlannedRoot]
) -> list[Path]:
    """The base directories of the ``--checkpoints`` / ``--final`` globs that lie strictly inside a
    root and neither are nor hold another one (relative ones read from ``cwd``, as the globs are
    expanded): what the child writes there is output, not code."""
    bases = (_glob_base(pattern) for pattern in patterns)
    return _within_roots((Path(cwd) / base for base in bases if base), roots)


def generated_exclusions(roots: Sequence[PlannedRoot]) -> list[Path]:
    """The generated-code directories (``vcp.code.generated``) that an environment variable puts
    strictly inside a root, unless one is or holds another root."""
    return _within_roots(generated_locations(env_only=True).values(), roots)
