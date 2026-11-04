"""The code grade of a run (spec 2026-10-31 §5.4): how much of the code it ran can be tied to, from
strongest to weakest ``traced > snapshot > commit > declared``.

Computed when read, never stored -- like ``vcp.measure.provenance`` -- from what is on disk: the
attempt's ``env.<n>.json``, ``train.yaml``'s refs and the artifacts they pin, the event log's notes.
So the grade falls when a snapshot is deleted or changed, when a file a snapshot lists was changed
by the child, or when a trace has a gap. A fused run has no code of its own and takes its weakest
member's grade.

``code_info`` never raises: what cannot be read is ``declared``, because nothing says what it held.
This module may import ``vcp.train`` (the record and the event log it reads); the modules
``vcp.train`` imports from ``vcp.code`` never import this one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vcp.code.schema import CODE_RANK, CodeGrade, CodeSection, TraceDoc
from vcp.code.trace import KIND as TRACE_KIND
from vcp.code.trace import TRACE_FILE
from vcp.core.errors import VcpError
from vcp.core.paths import artifact_dir
from vcp.data.evidence import ref_intact
from vcp.data.evidence_ref import CODE_ROLE, CODE_TRACE_ROLE, EvidenceRef, current
from vcp.measure.runs import FUSE_FRAMEWORK, load_run, run_dir
from vcp.train.env import EnvSections, read_sections
from vcp.train.records import events_path, has_record, load_record
from vcp.train.schema import GitInfo, TrainRecord

__all__ = [
    "AttemptCode",
    "CodeInfo",
    "RootCode",
    "code_info",
    "lowest",
    "notes_by_attempt",
]

SNAPSHOT_KIND = "code_snapshot"
CODE_CHANGED = "code_changed"  # the note ``train run`` leaves when the child changed a listed file
GIT_CHANGED = "git_changed"  # ... and when HEAD or the tracked diff moved
# What reading a record, an artifact or a yaml can raise; none of it leaves ``code_info``.
_UNREADABLE = (VcpError, OSError, ValueError, RecursionError)


@dataclass(frozen=True)
class RootCode:
    """One root of an attempt as ``env.<n>.json`` records it: its snapshot, or why it has none."""

    label: str
    snapshot: str | None
    reason: str | None


@dataclass(frozen=True)
class AttemptCode:
    n: int
    grade: CodeGrade
    roots: list[RootCode]


@dataclass(frozen=True)
class CodeInfo:
    """The run's grade (its lowest attempt's) and each attempt's. A run without attempts (an
    ingested one, a fused one) has none to list."""

    grade: CodeGrade
    attempts: list[AttemptCode]

    def to_json(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "attempts": [
                {
                    "n": a.n,
                    "grade": a.grade,
                    "roots": [
                        {"label": r.label, "snapshot": r.snapshot, "reason": r.reason}
                        for r in a.roots
                    ],
                }
                for a in self.attempts
            ],
        }


_DECLARED = CodeInfo("declared", [])
_NO_SECTIONS = EnvSections(git=None, code=None, identity=None)


def lowest(grades: Iterable[CodeGrade]) -> CodeGrade:
    """The weakest of ``grades``; ``declared`` when there are none."""
    return min(grades, key=CODE_RANK.__getitem__, default="declared")


# -- the event log ---------------------------------------------------------------------------------


def notes_by_attempt(data_root: Path, run_id: str) -> dict[int, set[str]]:
    """The ``key`` of every ``note`` event, by the attempt that wrote it. Read tolerantly, like
    ``vcp.train.status``'s patterns: a line that does not parse (a torn last line, a half-written
    character), a row that is not an object and a note without an integer attempt and a string key
    each add nothing and none stops the read."""
    try:
        raw = events_path(data_root, run_id).read_bytes()
    except OSError:
        return {}
    notes: dict[int, set[str]] = {}
    for line in raw.split(b"\n"):
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):  # ValueError includes UnicodeDecodeError
            continue
        if not isinstance(row, dict) or row.get("event") != "note":
            continue
        attempt, key = row.get("attempt"), row.get("key")
        if isinstance(attempt, int) and not isinstance(attempt, bool) and isinstance(key, str):
            notes.setdefault(attempt, set()).add(key)
    return notes


# -- one attempt -----------------------------------------------------------------------------------


class _Intact:
    """``ref_intact`` once per pinned artifact: verifying a snapshot hashes its files, and the
    attempts of a resumed run share it."""

    def __init__(self, data_root: Path):
        self._data_root = data_root
        self._seen: dict[tuple[str, str, str], bool] = {}

    def __call__(self, ref: EvidenceRef) -> bool:
        key = (ref.kind, ref.artifact_id, ref.manifest_sha256)
        if key not in self._seen:
            self._seen[key] = ref_intact(self._data_root, ref)
        return self._seen[key]


def _snapshot_complete(
    code: CodeSection | None, evidence: list[EvidenceRef], intact: _Intact
) -> bool:
    """Every root of the attempt has a snapshot, and the run holds an intact ``code_snapshot`` ref
    to that id (picked by kind and role; the rows of every name count, so an earlier attempt's
    snapshot is still vouched for after a later one took the name). No roots is never complete."""
    if code is None or not code.roots:
        return False
    refs = [r for r in evidence if r.kind == SNAPSHOT_KIND and r.role == CODE_ROLE]
    return all(
        root.snapshot is not None
        and any(r.artifact_id == root.snapshot and intact(r) for r in refs)
        for root in code.roots
    )


def _trace_complete(data_root: Path, ref: EvidenceRef) -> bool:
    path = artifact_dir(data_root, TRACE_KIND, ref.artifact_id) / TRACE_FILE
    try:
        return TraceDoc.model_validate_json(path.read_bytes()).complete
    except (OSError, ValueError):  # pydantic's ValidationError is a ValueError
        return False


def _traced(data_root: Path, evidence: list[EvidenceRef], n: int, intact: _Intact) -> bool:
    """The attempt has at least one trace, and every one of them is intact and complete."""
    refs = [
        r
        for r in current(evidence)
        if r.kind == TRACE_KIND and r.role == CODE_TRACE_ROLE and r.attempt == n
    ]
    return bool(refs) and all(intact(r) and _trace_complete(data_root, r) for r in refs)


def _commit(git: GitInfo | None, notes: set[str]) -> bool:
    """The attempt started at a commit and the code was not known to move: with ``status_sha256``
    (0.11 and later) no modified tracked file or a patch of the diff, without it a clean tree."""
    if git is None or not git.commit or GIT_CHANGED in notes:
        return False
    if git.status_sha256 is not None:
        return git.modified == 0 or git.diff_sha256 is not None
    return not git.dirty


def _attempt_grade(
    data_root: Path,
    record: TrainRecord,
    n: int,
    sections: EnvSections,
    notes: set[str],
    intact: _Intact,
) -> CodeGrade:
    if _snapshot_complete(sections.code, record.evidence, intact):
        # code_changed does not lower a trace: it compared the files that were imported.
        if _traced(data_root, record.evidence, n, intact):
            return "traced"
        if CODE_CHANGED not in notes:
            return "snapshot"
    return "commit" if _commit(sections.git, notes) else "declared"


def _attempt(
    data_root: Path,
    record: TrainRecord,
    n: int,
    env: str | None,
    notes: set[str],
    intact: _Intact,
) -> AttemptCode:
    sections = read_sections(run_dir(data_root, record.run_id) / env) if env else _NO_SECTIONS
    roots = (
        [RootCode(r.label, r.snapshot, r.reason) for r in sections.code.roots]
        if sections.code is not None
        else []
    )
    try:
        grade = _attempt_grade(data_root, record, n, sections, notes, intact)
    except _UNREADABLE:
        grade = "declared"
    return AttemptCode(n, grade, roots)


# -- a run -----------------------------------------------------------------------------------------


def _train_info(data_root: Path, run_id: str) -> CodeInfo:
    record = load_record(data_root, run_id)
    notes = notes_by_attempt(data_root, run_id)
    intact = _Intact(data_root)
    attempts = [
        _attempt(data_root, record, a.n, a.env, notes.get(a.n, set()), intact)
        for a in record.attempts
    ]
    return CodeInfo(lowest(a.grade for a in attempts), attempts)


def _ingest_info(data_root: Path, run_id: str) -> CodeInfo:
    """A run that did not train here (``eval ingest``): ``snapshot`` when it carries code refs and
    every one is intact, else ``declared`` -- there is no ``env.json`` to fall back on."""
    refs = [
        r
        for r in current(load_run(data_root, run_id).evidence)
        if r.kind == SNAPSHOT_KIND and r.role == CODE_ROLE
    ]
    intact = _Intact(data_root)
    return CodeInfo("snapshot" if refs and all(intact(r) for r in refs) else "declared", [])


def _fused_members(data_root: Path, run_id: str) -> list[str] | None:
    """The member runs of a fused run, None for any other (lazy import: the fusion layer imports
    the measurement layer, as ``vcp.measure.provenance._fused`` does)."""
    from vcp.fuse.build import load_record as load_fusion
    from vcp.fuse.build import record_path

    try:
        card = load_run(data_root, run_id)
    except VcpError:
        return None
    if card.source.framework != FUSE_FRAMEWORK or not record_path(data_root, run_id).is_file():
        return None
    return [m.run for m in load_fusion(data_root, run_id).members]


def _info(data_root: Path, run_id: str, seen: frozenset[str]) -> CodeInfo:
    if run_id in seen:  # a fusion that names itself: nothing says what it ran
        return _DECLARED
    try:
        members = _fused_members(data_root, run_id)
        if members is not None:
            below = seen | {run_id}
            return CodeInfo(lowest(_info(data_root, m, below).grade for m in members), [])
        if has_record(data_root, run_id):
            return _train_info(data_root, run_id)
        return _ingest_info(data_root, run_id)
    except _UNREADABLE:
        return _DECLARED


def code_info(data_root: Path, run_id: str) -> CodeInfo:
    """The code grade of ``run_id`` and of each of its attempts (spec §5.4). A run is as strong as
    its weakest attempt; one without ``train.yaml`` is graded by its ``code.<label>`` refs; a fused
    one by its weakest member. Never raises: what cannot be read is ``declared``."""
    return _info(Path(data_root), run_id, frozenset())
