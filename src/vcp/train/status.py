"""``vcp train status`` (read-only) and ``vcp train upload`` (a later or second backup)."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath

from vcp.core.paths import resolve_stored_path, store_path
from vcp.data.evidence import broken_refs
from vcp.measure.runs import run_dir
from vcp.train.checkpoints import drift as _drift
from vcp.train.checkpoints import missing as _missing
from vcp.train.disk import glob_prefix, nearest_dir
from vcp.train.records import append_event, events_path, load_record, save_record
from vcp.train.schema import TrainRecord
from vcp.train.upload import Runner, UploadOutcome, merge_uploads, upload

TMP_SUFFIX = ".tmp"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TmpFile:
    """A leftover ``*.tmp``; ``path`` is ``store_path``: data-root-relative, else absolute."""

    path: str
    bytes: int


@dataclass(frozen=True)
class TmpSkip:
    """A directory or entry the ``.tmp`` scan could not read; ``path`` is ``store_path``,
    ``error`` the exception's class name."""

    path: str
    error: str


@dataclass(frozen=True)
class TmpScan:
    files: list[TmpFile]
    skipped: list[TmpSkip]


@dataclass(frozen=True)
class StatusResult:
    record: TrainRecord
    backed: int
    unbacked: list[str]
    superseded: list[str]
    missing: list[str]
    drift: list[str]
    running: int
    tmp: list[TmpFile]
    tmp_skipped: list[TmpSkip]


def _is_plain_dir(entry: os.DirEntry[str]) -> bool:
    """A directory the walk may enter: not a symlink and not a junction (``is_symlink()`` is
    False for one, and a junction that points at an ancestor loops to the reparse limit)."""
    return not (entry.is_symlink() or entry.is_junction()) and entry.is_dir(follow_symlinks=False)


def _tmp_in(
    directory: Path, *, recursive: bool, on_error: Callable[[Path, OSError], None]
) -> list[Path]:
    """Leftovers directly in (or, if ``recursive``, under) ``directory``: entries named
    ``*.tmp``, any case, that are files or symlinks (a link is a finding of its own, even a
    dangling one). A symlink or junction is never entered. A directory that cannot be listed,
    or whose first unreadable entry stops the listing, goes to ``on_error`` once and the walk
    goes on with the rest; one that vanished mid-scan, or is no directory, is nothing to
    report."""
    found: list[Path] = []
    pending = [directory]
    while pending:
        here = pending.pop()
        try:
            with os.scandir(here) as entries:
                for entry in entries:
                    if entry.name.lower().endswith(TMP_SUFFIX) and (
                        entry.is_symlink() or entry.is_file(follow_symlinks=False)
                    ):
                        found.append(Path(entry.path))
                    elif recursive and _is_plain_dir(entry):
                        pending.append(Path(entry.path))
        except (FileNotFoundError, NotADirectoryError):  # gone since its parent was listed
            continue
        except OSError as e:
            on_error(here, e)
    return found


def _started_globs(data_root: Path, run_id: str) -> list[tuple[str, str | None]]:
    """Every ``--checkpoints`` / ``--final`` pattern named by a ``started`` event of the run
    (spec 2026-10-30 §12), each with the ``cwd`` that event recorded, or ``None`` when it has no
    usable one (the caller then falls back to the record's). Read tolerantly: a line that does
    not parse (a torn last line, a half-written character), a row that is not an object, a
    ``started`` event from a vcp that wrote no patterns, a value of the wrong type -- each adds
    nothing and none stops the read."""
    path = events_path(data_root, run_id)
    if not path.is_file():
        return []
    globs: list[tuple[str, str | None]] = []
    for raw in path.read_bytes().split(b"\n"):
        try:
            row = json.loads(raw)
        except (ValueError, RecursionError):  # ValueError includes UnicodeDecodeError
            continue
        if not isinstance(row, dict) or row.get("event") != "started":
            continue
        cwd = row.get("cwd")
        own_cwd = cwd if isinstance(cwd, str) and cwd else None
        patterns = row.get("checkpoints")
        patterns = [p for p in patterns if isinstance(p, str)] if isinstance(patterns, list) else []
        if isinstance(row.get("final"), str):
            patterns.append(row["final"])
        globs += [(pattern, own_cwd) for pattern in patterns]
    return globs


def _glob_dir(pattern: str, cwd: Path, on_error: Callable[[Path, OSError], None]) -> Path | None:
    """The directory a pattern writes into: the nearest existing directory of ``cwd`` plus the
    pattern's fixed prefix -- the same reading the disk check uses. An absolute pattern points
    wherever it says. A relative one is bound to its ``cwd``: with the ``cwd`` gone (a stale
    one in an old event) or the nearest directory an ancestor of it (``../not-yet/*.pt``), the
    walk up would end in a directory that is nobody's checkpoint directory, so it says nothing.
    A ``cwd`` that cannot be examined goes to ``on_error`` and says nothing either."""
    target = nearest_dir(cwd / glob_prefix(pattern))
    if PurePath(pattern).anchor:
        return target
    try:
        if not cwd.is_dir() or target.resolve() in cwd.resolve().parents:
            return None
    except OSError as e:
        on_error(cwd, e)
        return None
    return target


def _glob_dirs(
    data_root: Path, record: TrainRecord, on_error: Callable[[Path, OSError], None]
) -> set[Path]:
    """Where the child writes checkpoints, so a directory that never had a checkpoint registered
    is still looked at. The cwd is the one the attempt's own ``started`` event recorded
    (``--resume`` may run from another directory); the record's only when the event has none."""
    fallback = resolve_stored_path(record.cwd, data_root)
    dirs = (
        _glob_dir(
            pattern, fallback if cwd is None else resolve_stored_path(cwd, data_root), on_error
        )
        for pattern, cwd in _started_globs(data_root, record.run_id)
    )
    return {d for d in dirs if d is not None}


def _as_itself(path: Path, data_root: Path) -> str:
    """``store_path`` of a file's directory plus the file's own name: a symlink is listed as the
    link, where ``store_path(path)`` would follow it to the target."""
    return (PurePosixPath(store_path(path.parent, data_root)) / path.name).as_posix()


def tmp_files(data_root: Path, record: TrainRecord) -> TmpScan:
    """Leftover ``*.tmp`` files a failed write left behind (spec 5.5): anywhere under the run
    directory, and directly beside each registered checkpoint (the framework's own ``.tmp``
    lives there, e.g. a ``torch.save`` that ran out of disk), and directly in the directory of
    every checkpoint pattern the run was started with, registered or not (a first save that
    failed leaves a ``.tmp`` and nothing else). Never recursive below the run directory, even
    for a ``**`` pattern. Read-only; sorted by ``path``.

    What cannot be read (a directory without permission to list or to stat, an entry whose
    type cannot be read, a file that cannot be ``lstat``-ed) is skipped, named once in
    ``skipped`` and in a WARNING, and the scan goes on. A symlink -- dangling or not -- is listed
    by its own path and sized by ``lstat``; a junction is not entered; one file reached by two
    scans appears once (the same directory and the same name)."""
    skipped: dict[str, TmpSkip] = {}

    def skip(path: Path, error: OSError) -> None:
        shown = _as_itself(path, data_root)
        if shown not in skipped:
            skipped[shown] = TmpSkip(shown, type(error).__name__)
            log.warning(
                "train status: the .tmp scan could not read %s (%s) and skipped it",
                shown,
                type(error).__name__,
            )

    found = _tmp_in(run_dir(data_root, record.run_id), recursive=True, on_error=skip)
    beside = {resolve_stored_path(c.path, data_root).parent for c in record.checkpoints}
    for parent in beside | _glob_dirs(data_root, record, skip):
        found += _tmp_in(parent, recursive=False, on_error=skip)
    out: dict[tuple[Path, str], TmpFile] = {}
    for p in found:
        key = (p.parent.resolve(), p.name)
        if key in out:
            continue
        try:
            size = p.lstat().st_size
        except FileNotFoundError:  # gone between the listing and the stat
            continue
        except OSError as e:
            skip(p, e)
            continue
        out[key] = TmpFile(_as_itself(p, data_root), size)
    return TmpScan(
        files=sorted(out.values(), key=lambda t: t.path),
        skipped=sorted(skipped.values(), key=lambda t: t.path),
    )


def status(data_root: Path, run_id: str, *, verify: bool = False) -> StatusResult:
    record = load_record(data_root, run_id)
    # Backed-ness is keyed by bytes (sha256), not path: a --resume that changes a checkpoint's
    # bytes adds a second CheckpointRecord for the same path, and a verified upload of the OLD
    # bytes must not mark the NEW record backed.
    verified = {u.sha256 for u in record.uploads if u.verified}
    # 5-10: registration appends, so the LAST record of a path holds its current bytes. An
    # earlier record with no copy can never gain one -- the file it describes is gone -- so it
    # is reported as ``superseded`` rather than sitting in ``unbacked`` for the life of the run.
    # ``unbacked`` then means what it can be acted on: current bytes with no copy anywhere.
    newest = {c.path: c.sha256 for c in record.checkpoints}
    stale = [c for c in record.checkpoints if c.sha256 not in verified]
    scan = tmp_files(data_root, record)
    return StatusResult(
        record=record,
        backed=sum(1 for c in record.checkpoints if c.sha256 in verified),
        unbacked=[c.path for c in stale if newest[c.path] == c.sha256],
        # One line per path: a path re-registered several times with unverified bytes is one
        # superseded checkpoint, not several.
        superseded=list(dict.fromkeys(c.path for c in stale if newest[c.path] != c.sha256)),
        missing=_missing(record, data_root),
        drift=(
            _drift(record, data_root)
            + [f"evidence:{n}" for n in broken_refs(data_root, record.evidence)]
            if verify
            else []
        ),
        running=sum(1 for a in record.attempts if a.status == "running"),
        tmp=scan.files,
        tmp_skipped=scan.skipped,
    )


def upload_run(
    data_root: Path,
    run_id: str,
    dest: str,
    *,
    only_final: bool = False,
    runner: Runner | None = None,
) -> tuple[TrainRecord, UploadOutcome]:
    """Upload a recorded run's checkpoints now; the record and the event log both learn of it."""
    record = load_record(data_root, run_id)
    before = {(u.dest, u.name, u.sha256) for u in record.uploads}
    outcome = upload(record, dest, data_root=data_root, only_final=only_final, runner=runner)
    record = merge_uploads(record, outcome.records)
    save_record(data_root, record)
    attempt = record.attempts[-1].n if record.attempts else 0
    for r in outcome.records:
        if (r.dest, r.name, r.sha256) not in before:
            append_event(
                data_root,
                run_id,
                "uploaded",
                attempt,
                dest=dest,
                name=r.name,
                sha256=r.sha256,
                verified=r.verified,
            )
    return record, outcome
