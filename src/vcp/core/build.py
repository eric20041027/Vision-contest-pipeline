"""Build identity: the string an artifact records as ``vcp_version`` must resolve to code.

``__version__`` alone names a release only when the running code IS that tagged release. Run
from a checkout -- the editable install this project lives in -- the same string would cover
every commit since the tag, so the commit is appended in PEP 440 local-version form:

- ``0.2.0``                 a release wheel, or any copy git does not track
- ``0.2.0+g<sha>``          that commit of a checkout, clean
- ``0.2.0+g<sha>.dirty``    that commit plus uncommitted changes anywhere in its repository

A dirty tree over-reports on purpose: the commit no longer fully describes what ran, and the
reader must know that. Versioning rules and the release procedure live in ``CHANGELOG.md``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import vcp

_BUILD = re.compile(
    r"^(?P<version>\d+\.\d+\.\d+)(?:\+g(?P<commit>[0-9a-f]{40})(?P<dirty>\.dirty)?)?$"
)


@dataclass(frozen=True)
class BuildInfo:
    """What ``vcp_version`` says about the code that wrote an artifact."""

    version: str
    commit: str | None  # 40-hex HEAD of the checkout the package runs from; None otherwise
    dirty: bool | None  # uncommitted changes in that repository; None when commit is None


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str] | None:
    exe = shutil.which("git")
    if exe is None:
        return None
    return subprocess.run([exe, "-C", str(cwd), *args], capture_output=True, text=True)


def git_head(cwd: Path) -> tuple[str, bool] | None:
    """``(commit, dirty)`` of the repository containing ``cwd``; None without git or outside one."""
    head = _git("rev-parse", "HEAD", cwd=cwd)
    if head is None or head.returncode != 0:
        return None
    status = _git("status", "--porcelain", cwd=cwd)
    return head.stdout.strip(), bool(status is not None and status.stdout.strip())


def probe_build(package_dir: Path) -> BuildInfo:
    """The identity of the vcp package at ``package_dir``: its commit only when git tracks that
    very directory. A wheel unpacked into a venv that lives inside someone's repository is not
    a checkout of vcp, whatever ``rev-parse`` there would answer."""
    tracked = _git("ls-files", "--error-unmatch", "__init__.py", cwd=package_dir)
    if tracked is None or tracked.returncode != 0:
        return BuildInfo(vcp.__version__, None, None)
    head = git_head(package_dir)
    if head is None:
        return BuildInfo(vcp.__version__, None, None)
    return BuildInfo(vcp.__version__, head[0], head[1])


@lru_cache(maxsize=1)
def build_info() -> BuildInfo:
    """This process's vcp, probed once (git is a subprocess) and stable for the process's life."""
    return probe_build(Path(vcp.__file__).resolve().parent)


def build_string(info: BuildInfo | None = None) -> str:
    """PEP 440 local-version form of ``info`` (default: this process's build)."""
    info = build_info() if info is None else info
    if info.commit is None:
        return info.version
    return f"{info.version}+g{info.commit}" + (".dirty" if info.dirty else "")


def parse_build_string(text: str) -> BuildInfo:
    """Inverse of ``build_string``: what an artifact's ``vcp_version`` says about its writer.
    A bare version (every artifact written before 0.2.0) parses to no commit."""
    m = _BUILD.match(text)
    if m is None:
        raise ValueError(f"not a vcp build string: {text!r}")
    commit = m.group("commit")
    dirty = (m.group("dirty") is not None) if commit is not None else None
    return BuildInfo(m.group("version"), commit, dirty)
