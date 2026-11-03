"""Snapshot rules v1 (spec 2026-10-31 §4.2): which files make a code root's identity, which names
are never opened, which path segments are never entered, and every limit.

The principle is that a training run's output is not code: only ``CODE`` files and, where git
tracks them, ``CONFIG`` files count. The limits are plain module attributes that the scan reads
when it runs, so a test can lower one.
"""

from __future__ import annotations

import fnmatch
import os
from typing import Literal

RULES_VERSION = 1
IDENTITY_PREFIX = "vcp-code/1\n"

# Python sources, and the Cython / C++ / CUDA sources a run's own code compiles into modules.
CODE_SUFFIXES = frozenset(
    {".py", ".pyi", ".pyx", ".pxd", ".c", ".cc", ".cpp", ".cu", ".cuh", ".h", ".hpp"}
)
# Settings and build files; taken only where git tracks them (the framework writes its own
# args.yaml / config.json / metrics.json into --cwd, and those are not the code).
CONFIG_SUFFIXES = frozenset(
    {
        ".pyd",
        ".so",
        ".ipynb",
        ".sh",
        ".ps1",
        ".bat",
        ".cmd",
        ".toml",
        ".cfg",
        ".ini",
        ".yaml",
        ".yml",
        ".json",
    }
)
CONFIG_GLOBS = ("requirements*.txt", "constraints*.txt", "*.lock", "Makefile", "Dockerfile")
# A file with one of these names is neither opened nor hashed; its path is only reported.
SECRET_GLOBS = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.kdbx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "kaggle.json",
    "rclone.conf",
    "pg_service.conf",
    "pgpass.conf",
    "_netrc",
    "credentials*",
    "*secret*",
    "*service-account*",
    "*service_account*",
)

KEEP_FILE_LIMIT = 1024**2  # a file larger than this keeps its sha, not its bytes (capped)
KEEP_ROOT_LIMIT = 64 * 1024**2  # bytes kept per root (capped)
MAX_FILES = 20_000  # files taken into one root; more is too_large
MAX_HASH_BYTES = 512 * 1024**2  # bytes hashed per root; more is too_large
MAX_WALK_ENTRIES = 200_000  # entries a plain directory root may list; more is too_large
LISTED_PATHS = 50  # paths a record lists per list; the counts stay exact

_SKIPPED_SEGMENTS = frozenset({"__pycache__", "node_modules", "site-packages"})
_CONFIG_GLOBS_FOLDED = tuple(pattern.casefold() for pattern in CONFIG_GLOBS)
_SECRET_GLOBS_FOLDED = tuple(pattern.casefold() for pattern in SECRET_GLOBS)


def _matches(folded: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(folded, pattern) for pattern in patterns)


def file_class(name: str) -> Literal["code", "config"] | None:
    """The group a file name belongs to, ignoring case; None for a file the snapshot never reads."""
    folded = name.casefold()
    suffix = os.path.splitext(folded)[1]
    if suffix in CODE_SUFFIXES:
        return "code"
    if suffix in CONFIG_SUFFIXES or _matches(folded, _CONFIG_GLOBS_FOLDED):
        return "config"
    return None


def is_secret(name: str) -> bool:
    """A file name that may hold a credential (ignoring case): never opened, never hashed."""
    return _matches(name.casefold(), _SECRET_GLOBS_FOLDED)


def skipped_segment(segment: str) -> bool:
    """A path segment the snapshot neither enters nor lists: anything that starts with ``.``
    (``.git``, ``.venv``, ``.env*``, ``.claude``, ``.hydra``), a cache or an installed-package
    directory, an ``*.egg-info``."""
    folded = segment.casefold()
    return segment.startswith(".") or folded in _SKIPPED_SEGMENTS or folded.endswith(".egg-info")
