"""Code snapshots a job asks for in its own process (spec 2026-10-31 §5.5; VCP-004 acceptance 1-2).

An artifact job that wants its code pinned calls ``snapshot_inputs(data_root)``: the roots are
planned as ``train run`` plans them (§5.1 step 4) -- ``cwd`` is the repository of ``Path.cwd()``,
``script`` the file ``__main__`` runs, every editable distribution of this environment, then
``extra_roots`` -- scanned with the same rules and limits, and each snapshot is written or reused
under its lock. The job asked for these snapshots, so a root that cannot be snapshotted is a
refusal (``strict``), never the downgrade a ``train run`` without an authorization makes.

The snapshots come back as ``code_snapshot.<label>`` inputs, and this process remembers which
directory each was taken of (``_TRACKED``, per data root). ``ArtifactWriter`` calls
``check_imports`` at ``create`` and again at ``commit``: every module the process has loaded is
classified against those roots (``vcp.code.trace``). A module outside them, or under one but not
in its snapshot, is ``code_outside:``; an imported file whose bytes changed since its snapshot is
``drift:``; a file nobody imported may change. A snapshot another process took is in no
``_TRACKED`` here: the writer checks it statically, nothing more.

``snapshot_roots`` is the same for the roots ``vcp code snapshot`` is given by hand (spec §5.7):
each is taken as given, keeps every byte, and a root that cannot be snapshotted is a refusal.
Nothing is tracked for ``check_imports``; no job is running.

``loaded_modules`` and ``main_file`` are the seams tests replace: they are reached as module
attributes, as ``write_snapshot`` and ``scan_root`` are.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import NoReturn

from vcp.artifact.schema import CODE_INPUT_PREFIX, ArtifactSpec, InputRef
from vcp.code import roots
from vcp.code.generated import generated_locations
from vcp.code.roots import (
    PlannedRoot,
    generated_exclusions,
    plan_roots,
    repo_top,
    skip_dirs_for,
)
from vcp.code.scan import RootScan, root_kind, scan_root
from vcp.code.schema import RootReason
from vcp.code.snapshot import SNAPSHOT_PATH, SnapshotOutcome, snapshot_json_path, write_snapshot
from vcp.code.trace import TracedRoot, classify_modules, site_dirs, stdlib_dirs
from vcp.core.errors import IntegrityError, ValidationFailed, VcpError
from vcp.core.hashing import sha256_file
from vcp.core.log import FieldValue
from vcp.core.paths import path_key, resolve_configs_root, resolve_stored_path, store_path

__all__ = [
    "COMMAND",
    "ROOTS_COMMAND",
    "SHOWN_PATHS",
    "Written",
    "check_imports",
    "loaded_modules",
    "main_file",
    "raise_for_outcome",
    "snapshot_inputs",
    "snapshot_roots",
    "strict",
]

COMMAND = "snapshot_inputs"  # who holds a snapshot's lock, as the lock file says
ROOTS_COMMAND = "code.snapshot"  # ... when `vcp code snapshot` holds it
SHOWN_PATHS = 5  # paths a code_outside: / drift: message names; the count stays exact
# The environment's reasons a root has no snapshot (ABORT `code_unavailable:`), and what to do.
_UNAVAILABLE: dict[str, str] = {
    "locked": "another writer still holds its lock; run again when that one is done",
    "partial": "a half-written snapshot of this content blocks its id (`vcp artifact clean`)",
    "unwritable": "it cannot be written into this data root (a path too long, or names that "
    "differ only in case)",
    "unreadable": "the root could not be read (git refused it, or a file could not be opened)",
}

# (path_key of the data root, snapshot id) -> the root this process took that snapshot of.
_TRACKED: dict[tuple[str, str], TracedRoot] = {}


def _sys_modules() -> Mapping[str, ModuleType]:
    return sys.modules


def _main_file() -> Path | None:
    """The file ``__main__`` runs when it is one that exists: none for a REPL, ``-c`` or a
    notebook kernel."""
    file = getattr(sys.modules.get("__main__"), "__file__", None)
    if not isinstance(file, str) or not file:
        return None
    path = Path(file)
    try:
        return path if path.is_file() else None
    except OSError:  # a name the filesystem cannot hold is no file
        return None


loaded_modules: Callable[[], Mapping[str, ModuleType]] = _sys_modules
main_file: Callable[[], Path | None] = _main_file


# -- refusing ---------------------------------------------------------------------------------


def raise_for_outcome(label: str, reason: RootReason) -> NoReturn:
    """The refusal for a code root whose snapshot was asked for and not obtained (spec §5.1 step
    8, §5.5, §5.7; plan open point 13): ``too_large:`` and ``not_found:`` are FAIL (what was
    given), ``drift:`` is FAIL (the code changed after its scan), and what the environment causes
    -- ``locked``, ``partial``, ``unwritable``, ``unreadable`` -- is ABORT ``code_unavailable:``
    with ``cause=<reason>``. Every one carries ``code_root=<label>``."""
    fields: dict[str, FieldValue] = {"code_root": label}
    if reason == "too_large":
        raise ValidationFailed(
            f"too_large: code root {label} is past the snapshot limits (files taken, bytes "
            "hashed or directory entries walked); leave the rest out with .gitignore, or "
            "snapshot a smaller root",
            fields=fields,
        )
    if reason == "not_found":
        raise ValidationFailed(f"not_found: code root {label} is not a directory", fields=fields)
    if reason == "drift":
        raise IntegrityError(
            f"drift: code root {label} changed after it was scanned; run again once its files "
            "stop changing",
            fields=fields,
        )
    cause = _UNAVAILABLE.get(reason)
    if cause is None:
        raise ValueError(f"no refusal for root reason {reason!r}")
    raise VcpError(
        f"code_unavailable: no snapshot of code root {label}: {cause}",
        fields={**fields, "cause": reason},
    )


def strict(root: PlannedRoot, scan: RootScan, outcome: SnapshotOutcome | None) -> str:
    """The snapshot id of a root asked for explicitly, or its refusal (``raise_for_outcome``):
    the scan's reason first, then the write's. ``outcome`` is None only for a scan with a
    reason."""
    if scan.reason is not None:
        raise_for_outcome(root.label, scan.reason)
    if outcome is None:
        raise ValueError(f"code root {root.label} was scanned but never written")
    if outcome.reason is not None:
        raise_for_outcome(root.label, outcome.reason)
    return outcome.snapshot_id


# -- taking the snapshots ---------------------------------------------------------------------


def _configs_root() -> list[Path]:
    """The configs root when one can be found from here; a job without one is no error."""
    try:
        return [resolve_configs_root()]
    except ValidationFailed:
        return []


def _plan(data_root: Path, extra_roots: Sequence[Path]) -> tuple[list[PlannedRoot], list[RootScan]]:
    """spec §5.5: the roots of §5.1 step 4 from this process, scanned (read-only). Each scan
    leaves out the data and configs roots, every root nested in it and the generated-code
    directories an environment variable puts inside one (R4); bytes are kept ``capped``."""
    for path in extra_roots:
        if not Path(path).is_dir():
            raise ValidationFailed(f"not_found: extra root {path} is not a directory")
    script = main_file()
    planned = plan_roots(
        cwd=Path.cwd(),
        command=[str(script)] if script is not None else [],
        editables=roots.editables_of(roots.local_distributions()),
        extra=list(extra_roots),
    )
    fixed = [data_root, *_configs_root(), *generated_exclusions(planned)]
    scans = [
        scan_root(
            root.path,
            kind=root.kind,
            skip_dirs=skip_dirs_for(planned, i, fixed=fixed),
            keep="capped",
        )
        for i, root in enumerate(planned)
    ]
    return planned, scans


def snapshot_inputs(data_root: Path, extra_roots: Sequence[Path] = ()) -> list[InputRef]:
    """Snapshot every code root of this process and return them as ``code_snapshot.<label>``
    inputs (the path of each ``snapshot.json`` and its sha), in root order.

    A root that cannot be scanned stops everything before anything is written; then each
    snapshot is written or reused, and one that cannot be is refused (``strict``). A committed
    snapshot of the same content that does not hold is ``mismatch:``, raised by
    ``write_snapshot``. Only when every root has its snapshot does this process track them for
    ``check_imports``."""
    data_root = Path(data_root)
    planned, scans = _plan(data_root, extra_roots)
    for root, scan in zip(planned, scans, strict=True):
        if scan.reason is not None:
            raise_for_outcome(root.label, scan.reason)
    ids = [
        strict(root, scan, write_snapshot(data_root, scan, command=COMMAND))
        for root, scan in zip(planned, scans, strict=True)
    ]
    base = path_key(data_root)
    inputs = []
    for root, scan, snapshot_id in zip(planned, scans, ids, strict=True):
        index = MappingProxyType({entry.path: entry.sha256 for entry in scan.entries})
        _TRACKED[(base, snapshot_id)] = TracedRoot(root.label, root.path, snapshot_id, index)
        path = snapshot_json_path(data_root, snapshot_id)
        inputs.append(
            InputRef(
                name=f"{CODE_INPUT_PREFIX}{root.label}", path=str(path), sha256=sha256_file(path)
            )
        )
    return inputs


# -- the roots `vcp code snapshot` is given ---------------------------------------------------


@dataclass(frozen=True)
class Written:
    """One root of ``snapshot_roots`` with its scan and the write that gave it a snapshot:
    ``outcome.state`` is ``created`` or ``reused``, never a write with a ``reason``."""

    root: PlannedRoot
    scan: RootScan
    outcome: SnapshotOutcome


def _given_roots(paths: Sequence[Path]) -> list[PlannedRoot]:
    """Each ``--root`` as it was given: ``root-<n>`` (``n`` counts from 1 in what was given,
    whatever is dropped before it), the kind the disk says (``root_kind``), a second spelling of
    one directory dropped. A path that is no directory is ``not_found:``, before anything is
    scanned or written."""
    planned: list[PlannedRoot] = []
    seen: set[str] = set()
    for n, path in enumerate(paths, start=1):
        label = f"root-{n}"
        if not Path(path).is_dir():
            raise ValidationFailed(
                f"not_found: --root {path} is not a directory", fields={"code_root": label}
            )
        resolved = Path(path).resolve()
        key = path_key(resolved)
        if key in seen:
            continue
        seen.add(key)
        kind = root_kind(resolved)
        top = repo_top(resolved) if kind == "git" else None
        planned.append(PlannedRoot(label, "extra", resolved, kind, top[1] if top else None))
    return planned


def snapshot_roots(
    data_root: Path, configs_root: Path | None, roots: Sequence[Path]
) -> list[Written]:
    """spec §5.7: snapshot the directories ``vcp code snapshot`` was given, in order. Each scan
    leaves out the data root, the configs root (when there is one), every root nested in it and
    the generated-code directories an environment variable puts inside one (R4); every file keeps
    its bytes (``keep="all"``: the limits that stay are the file and hash totals). A root that
    cannot be scanned stops everything before anything is written; then each snapshot is written
    or reused, and one that cannot be is a refusal (``strict``). A reused snapshot is whatever
    was written first: a capped one comes back with ``outcome.not_kept`` > 0."""
    data_root = Path(data_root)
    planned = _given_roots(roots)
    fixed = [
        data_root,
        *([Path(configs_root)] if configs_root is not None else []),
        *generated_exclusions(planned),
    ]
    scans = [
        scan_root(
            root.path,
            kind=root.kind,
            skip_dirs=skip_dirs_for(planned, i, fixed=fixed),
            keep="all",
        )
        for i, root in enumerate(planned)
    ]
    for root, scan in zip(planned, scans, strict=True):
        if scan.reason is not None:
            raise_for_outcome(root.label, scan.reason)
    written = []
    for root, scan in zip(planned, scans, strict=True):
        outcome = write_snapshot(data_root, scan, command=ROOTS_COMMAND)
        strict(root, scan, outcome)  # a write with a reason is refused here
        written.append(Written(root, scan, outcome))
    return written


# -- checking a job's imports -----------------------------------------------------------------


def _tracked_roots(spec: ArtifactSpec, data_root: Path) -> list[TracedRoot]:
    """The roots behind the spec's ``code_snapshot.*`` inputs that this process snapshotted
    for this data root, in input order."""
    base = path_key(data_root)
    found = []
    for ref in spec.inputs:
        if not ref.name.startswith(CODE_INPUT_PREFIX) or ref.path is None:
            continue
        stored = store_path(resolve_stored_path(ref.path, data_root), data_root)
        match = SNAPSHOT_PATH.fullmatch(stored)
        root = _TRACKED.get((base, match[1])) if match is not None else None
        if root is not None:
            found.append(root)
    return found


def check_imports(spec: ArtifactSpec, data_root: Path) -> None:
    """spec §5.5: the modules this process has loaded, held against the roots of the spec's
    snapshots taken here (none: nothing to do). Outside them, or under one and not in its
    snapshot -> ``code_outside:``; an imported file with other bytes than its snapshot holds ->
    ``drift:``. Generated modules (``vcp.code.generated``) are no gap, and a file nobody imported
    is not looked at."""
    data_root = Path(data_root)
    tracked = _tracked_roots(spec, data_root)
    if not tracked:
        return
    result = classify_modules(
        loaded_modules(),
        roots=tracked,
        generated=generated_locations(),
        stdlib=stdlib_dirs(),
        site=site_dirs(),
    )
    fields: dict[str, FieldValue] = {"kind": spec.kind, "id": spec.id}
    gaps = result.counts["outside"] + result.counts["unlisted"]
    if gaps:
        shown = ", ".join([*result.outside, *result.unlisted][:SHOWN_PATHS])
        raise ValidationFailed(
            f"code_outside: {gaps} imported module(s) lie outside the snapshot roots: {shown}; "
            "pass their directories as extra_roots (a file under a root that its snapshot does "
            "not list -- ignored, or new since -- needs a new snapshot)",
            fields=fields,
        )
    changed = result.counts["changed"]
    if changed:
        shown = ", ".join(result.changed[:SHOWN_PATHS])
        raise IntegrityError(
            f"drift: imported {shown} changed since its snapshot ({changed} file(s)); the job "
            "ran code its inputs do not hold",
            fields=fields,
        )
