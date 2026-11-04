"""A snapshot held against the files that are there now (spec 2026-10-31 §5.7).

``vcp code verify`` asks whether a snapshot still holds and never looks at the working tree; this
module asks the other question -- has the checkout moved since the snapshot was taken -- and never
looks at the snapshot's bytes, only at its index. The current files are scanned by the same rules
and limits as the snapshot was (``vcp.code.scan``), so a file the rules never take is no
difference, and a file counts as ``changed`` only by its sha.

``diff_root`` compares one directory; ``diff_run`` compares each root of a training attempt that
has a snapshot, with what the attempt itself left out: the directories its ``env.<n>.json``
records as excluded and the roots nested in another. Reading only: nothing is written, no lock is
taken. May import ``vcp.train`` for the record it reads (preflight R8); the modules ``vcp.train``
imports from ``vcp.code`` never import this one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from vcp.code.process import raise_for_outcome
from vcp.code.roots import PlannedRoot, generated_exclusions, skip_dirs_for
from vcp.code.scan import RootKind, root_kind, scan_root
from vcp.code.snapshot import load_index
from vcp.core.errors import ValidationFailed
from vcp.core.paths import resolve_stored_path
from vcp.measure.runs import run_dir
from vcp.train.env import read_sections
from vcp.train.records import has_record, load_record
from vcp.train.schema import Attempt

__all__ = ["DiffResult", "diff_root", "diff_run"]


@dataclass(frozen=True)
class DiffResult:
    """Root-relative posix paths, each list in path order: files whose sha is not the snapshot's,
    files the snapshot does not list, and files it lists that are not there."""

    changed: list[str]
    added: list[str]
    removed: list[str]


def _compare(
    data_root: Path,
    snapshot_id: str,
    root: Path,
    kind: RootKind,
    skip_dirs: Sequence[Path],
    label: str,
) -> DiffResult:
    """The index of ``snapshot_id`` (``not_found:`` / ``partial:`` / ``mismatch:`` as
    ``load_index`` raises them) against a fresh scan of ``root``; a root that cannot be scanned is
    ``raise_for_outcome``'s refusal (``too_large:``, ``not_found:``, ``code_unavailable:``)."""
    then = {entry.path: entry.sha256 for entry in load_index(data_root, snapshot_id)}
    scan = scan_root(root, kind=kind, skip_dirs=skip_dirs)
    if scan.reason is not None:
        raise_for_outcome(label, scan.reason)
    now = {entry.path: entry.sha256 for entry in scan.entries}
    return DiffResult(
        changed=sorted(path for path in now.keys() & then.keys() if now[path] != then[path]),
        added=sorted(now.keys() - then.keys()),
        removed=sorted(then.keys() - now.keys()),
    )


def diff_root(
    data_root: Path,
    snapshot_id: str,
    root: Path,
    *,
    kind: RootKind | None = None,
    skip_dirs: Sequence[Path] = (),
    label: str = "root",
) -> DiffResult:
    """``snapshot_id`` against the files of ``root`` now. ``kind`` is what the disk says
    (``root_kind``) unless the caller knows. The data root, ``skip_dirs`` (the caller's: the
    configs root a snapshot also left out) and the generated-code directories an environment
    variable puts inside ``root`` are not looked at (preflight R4). ``label`` names the root in a
    refusal."""
    data_root, root = Path(data_root), Path(root)
    chosen = kind if kind is not None else root_kind(root)
    here = PlannedRoot(label, "extra", root.resolve(), chosen, None)
    skips = [data_root, *skip_dirs, *generated_exclusions([here])]
    return _compare(data_root, snapshot_id, root, chosen, skips, label)


def _attempt(run_id: str, attempts: list[Attempt], wanted: int | None) -> Attempt:
    """The attempt asked for, the last by default."""
    if not attempts:
        raise ValidationFailed(
            f"not_found: run {run_id} has no attempts to compare", fields={"run": run_id}
        )
    if wanted is None:
        return attempts[-1]
    for attempt in attempts:
        if attempt.n == wanted:
            return attempt
    raise ValidationFailed(
        f"not_found: run {run_id} has no attempt {wanted} (it has "
        f"{', '.join(str(a.n) for a in attempts)})",
        fields={"run": run_id},
    )


def diff_run(
    data_root: Path, run_id: str, attempt: int | None = None, *, skip_dirs: Sequence[Path] = ()
) -> list[tuple[str, DiffResult]]:
    """Each root of an attempt of ``run_id`` (the last unless ``attempt`` says) that has a
    snapshot, by label, against the files now. The root's path and kind are the ones
    ``env.<n>.json`` recorded; what the attempt left out -- its recorded ``excluded_dirs`` and the
    roots nested in another -- is left out here, with the data root and ``skip_dirs`` (the configs
    root, which the record does not name). ``not_found:`` for a run without a training record, an
    attempt that is not there, one that recorded no code roots, or one whose roots have no
    snapshot."""
    data_root = Path(data_root)
    if not has_record(data_root, run_id):
        raise ValidationFailed(
            f"not_found: run {run_id} has no training record (train.yaml); only a run trained "
            "here recorded its code roots",
            fields={"run": run_id},
        )
    record = load_record(data_root, run_id)
    chosen = _attempt(run_id, record.attempts, attempt)
    code = read_sections(run_dir(data_root, run_id) / chosen.env).code if chosen.env else None
    if code is None or not code.roots:
        raise ValidationFailed(
            f"not_found: attempt {chosen.n} of run {run_id} recorded no code roots (a run from "
            "before vcp 0.18, or its env.json is gone)",
            fields={"run": run_id},
        )
    planned = [
        PlannedRoot(r.label, r.source, resolve_stored_path(r.path, data_root), r.kind, r.commit)
        for r in code.roots
    ]
    fixed = [
        data_root,
        *(resolve_stored_path(stored, data_root) for stored in code.excluded_dirs),
        *skip_dirs,
    ]
    results = [
        (
            root.label,
            _compare(
                data_root,
                root.snapshot,
                planned[i].path,
                planned[i].kind,
                skip_dirs_for(planned, i, fixed=fixed),
                root.label,
            ),
        )
        for i, root in enumerate(code.roots)
        if root.snapshot is not None
    ]
    if not results:
        raise ValidationFailed(
            f"not_found: no root of attempt {chosen.n} of run {run_id} has a snapshot to compare",
            fields={"run": run_id},
        )
    return results
