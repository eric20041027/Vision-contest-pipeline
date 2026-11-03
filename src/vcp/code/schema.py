"""Pydantic models of the code layer (spec 2026-10-31 §4.3): what a run records of the code it ran
and of the distributions it ran on.

This module imports no other part of vcp -- not ``vcp.code.roots``, not ``vcp.train`` -- because the
training layer's schema imports it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# Where a code root came from (spec §5.1 step 4): the working directory's repository, the
# command's script, an editable distribution, or ``--code-root``.
RootSource = Literal["cwd", "script", "editable", "extra"]
# What a root is: a directory inside a git work tree (tracked files count), or a plain directory.
RootKind = Literal["git", "dir"]
# Why a root has no snapshot (spec §4.3): ``vcp.code.scan.ScanReason`` (the scan found nothing it
# could keep) plus ``vcp.code.snapshot.WriteReason`` (the snapshot could not be written).
RootReason = Literal[
    "too_large", "unreadable", "unwritable", "not_found", "locked", "partial", "drift"
]
# How much of the code a run (or a submission candidate) can be tied to, weakest first (spec
# §5.4). The ranking lives here so ``vcp.submit.schema`` can read it without importing the
# training layer.
CodeGrade = Literal["declared", "commit", "snapshot", "traced"]
CODE_RANK: dict[str, int] = {"declared": 0, "commit": 1, "snapshot": 2, "traced": 3}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DirectUrl(_Strict):
    """PEP 610 ``direct_url.json`` as recorded: ``url`` has lost its userinfo, query and fragment
    and passed ``redact`` (``vcp.code.roots.clean_url``)."""

    url: str
    editable: bool = False
    commit_id: str | None = None


class Distribution(_Strict):
    """One installed distribution of the training environment. ``record_sha256`` is the sha of its
    ``RECORD`` file, None when it has none."""

    name: str
    version: str
    record_sha256: str | None
    direct_url: DirectUrl | None = None


class EnvIdentity(_Strict):
    """What a run claims about its environment, in three fields (spec §4.3): ``python`` is
    ``<implementation>-<version>-<cache_tag>-<platform>``, ``packages_sha256`` the sha of the
    non-editable distributions, ``gpus`` the nvidia-smi names joined with commas."""

    python: str
    packages_sha256: str
    gpus: str


class CodeRoot(_Strict):
    """One root of a run's code as ``env.<n>.json`` records it. ``snapshot`` is the
    ``code_snapshot`` artifact id, None when ``reason`` says why there is none. The label, path and
    commit are machine- and run-specific, so they live here and not in the artifact."""

    label: str
    source: RootSource
    kind: RootKind
    path: str
    commit: str | None
    snapshot: str | None
    reason: RootReason | None
    files: int
    untracked_code: int
    untracked_code_paths: list[str]
    excluded: int
    excluded_paths: list[str]


class CodeSection(_Strict):
    """The ``code`` section of ``env.<n>.json``: the rules version, the directories the snapshot
    left out (checkpoint globs, generated code) and the roots."""

    rules: int
    excluded_dirs: list[str]
    roots: list[CodeRoot]


class TraceDoc(_Strict):
    """``trace.json`` of a ``code_trace`` artifact (spec §4.4): what one process of a training
    attempt imported, by class. ``roots`` are the ``{label, snapshot}`` it was held against;
    ``counts`` always has the seven classes; ``outside`` lists absolute posix paths and
    ``unlisted`` / ``changed`` ``<label>:<path>``, each sorted and cut to the first 50 (the counts
    stay exact). ``complete`` is no ``outside``, ``unlisted`` or ``changed`` module: a generated
    one is not a gap."""

    schema_version: int = 1
    run_id: str
    attempt: int
    executable: str
    python: str
    roots: list[dict[str, str]]
    counts: dict[str, int]
    generated_by: dict[str, int]
    outside: list[str]
    unlisted: list[str]
    changed: list[str]
    complete: bool
    taken_at: str
    vcp_version: str
