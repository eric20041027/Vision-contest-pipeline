"""A code snapshot as a plain directory (spec 2026-10-31 §5.7, controller ruling C4): ``export``
writes the kept bytes of a snapshot and its index out, ``verify_dir`` checks such a directory
without git and without the repository it came from.

An export is ``<out>/<path>`` for every file whose bytes the snapshot kept, and ``<out>/.vcp-code/``
with the snapshot's ``index.jsonl`` and ``snapshot.json``. The directory is a copy for reading,
shipping to a kernel or hashing again, never a working tree: nothing here writes into one.

A snapshot taken by ``train run`` keeps no file past 1 MiB, so its export has holes. They are not
failures: ``verify_dir`` reports them (``not_kept``) and, once somebody puts the file in, checks
it by the sha the index holds. A file that is in the directory must match; a file the snapshot
kept and the directory lost is ``missing``; a file the rules would take that the index does not
list is ``extra``. The identity is recomputed from the index (``snapshot_json_bytes``), so the
directory vouches for itself.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from vcp.code import rules
from vcp.code.scan import IndexEntry, snapshot_json_bytes
from vcp.code.snapshot import (
    FILES_DIR,
    INDEX_FILE,
    KIND,
    SNAPSHOT_FILE,
    check_snapshot,
    load_index,
    parse_index,
)
from vcp.core import atomic
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir, is_under

__all__ = ["VCP_CODE_DIR", "ExportResult", "VerifyDirResult", "export", "verify_dir"]

VCP_CODE_DIR = ".vcp-code"
SHOWN_PATHS = 5  # names an error message gives; its counts stay exact
_CHUNK = 1 << 20
_INDEX_PATH = f"{VCP_CODE_DIR}/{INDEX_FILE}"
_SNAPSHOT_PATH = f"{VCP_CODE_DIR}/{SNAPSHOT_FILE}"


@dataclass(frozen=True)
class ExportResult:
    """``files`` is what the snapshot lists, ``exported`` the files written (those whose bytes it
    kept) and ``not_kept`` the rest."""

    files: int
    exported: int
    not_kept: int


@dataclass(frozen=True)
class VerifyDirResult:
    """What ``verify_dir`` found, as paths relative to the directory. ``files`` and ``kept`` count
    the index. ``mismatch`` is a file that is there and is not what the index says (or the
    ``.vcp-code`` file that disagrees with the index); ``missing`` a kept file that is gone;
    ``not_kept`` a file the snapshot never held the bytes of and the directory lacks; ``extra`` a
    file the rules take that the index does not list. Only ``mismatch`` and ``missing`` fail."""

    files: int
    kept: int
    mismatch: list[str]
    missing: list[str]
    not_kept: list[str]
    extra: list[str]


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            yield chunk


# -- export -------------------------------------------------------------------------------------


def _check_destination(data_root: Path, out: Path) -> None:
    """``invalid:`` inside the data root (an export is not an artifact); ``exists:`` when there is
    anything at ``out`` but an empty directory. Asked before anything is read or written."""
    if is_under(out, data_root):
        raise ValidationFailed(
            f"invalid: --out {out} is inside the data root {data_root}; export to a directory "
            "of your own"
        )
    if os.path.lexists(out) and (not out.is_dir() or any(out.iterdir())):
        raise ValidationFailed(
            f"exists: {out} is not an empty directory; export into a new one, or empty it first"
        )


def export(data_root: Path, snapshot_id: str, out: Path) -> ExportResult:
    """Write the kept files of ``snapshot_id`` to ``out/<path>`` and its ``index.jsonl`` and
    ``snapshot.json`` to ``out/.vcp-code/`` (the last thing written, so a directory that has
    them was written through). ``out`` must not exist or be empty (``exists:``) and must not be
    in the data root (``invalid:``); a snapshot that does not hold (``check_snapshot``) is
    ``mismatch:`` and nothing is written; ``not_found:`` / ``partial:`` as ``store.load_manifest``
    raises them."""
    data_root, out = Path(data_root), Path(out)
    _check_destination(data_root, out)
    check = check_snapshot(data_root, snapshot_id)
    if not check.ok:
        named = ", ".join([*check.mismatch, *check.missing][:SHOWN_PATHS])
        raise IntegrityError(
            f"mismatch: {KIND}/{snapshot_id} does not hold ({named}); nothing was exported -- "
            "`vcp code verify` names every file",
            fields={
                "id": snapshot_id,
                "mismatch": len(check.mismatch),
                "missing": len(check.missing),
            },
        )
    index = load_index(data_root, snapshot_id)
    source = artifact_dir(data_root, KIND, snapshot_id)
    exported = 0
    for entry in index:
        if entry.kept:
            atomic.write_once_stream(out / entry.path, _chunks(source / FILES_DIR / entry.path))
            exported += 1
    for name in (INDEX_FILE, SNAPSHOT_FILE):
        atomic.write_once_stream(out / VCP_CODE_DIR / name, _chunks(source / name))
    return ExportResult(files=len(index), exported=exported, not_kept=len(index) - exported)


# -- verify_dir ---------------------------------------------------------------------------------


def _extras(directory: Path, listed: set[str]) -> list[str]:
    """The files below ``directory`` that the rules take (``file_class``) and ``listed`` does not
    hold (``os.path.normcase``-ed relative paths). A hidden or cache segment, a virtualenv, a link
    and a secret name are left alone, as a scan leaves them; nothing is opened. More than
    ``rules.MAX_WALK_ENTRIES`` entries is ``too_large:``."""
    limit = rules.MAX_WALK_ENTRIES
    seen = 0
    found: list[str] = []
    pending = [("", directory)]
    while pending:
        prefix, current = pending.pop()
        with os.scandir(current) as entries:
            children = list(entries)
        for child in children:
            if rules.skipped_segment(child.name):
                continue
            linked = child.is_symlink() or child.is_junction()
            is_dir = not linked and child.is_dir(follow_symlinks=False)
            if is_dir and os.path.exists(os.path.join(child.path, "pyvenv.cfg")):
                continue
            seen += 1
            if seen > limit:
                raise ValidationFailed(
                    f"too_large: {directory} holds more than {limit} entries to look through"
                )
            relative = prefix + child.name
            if is_dir:
                pending.append((relative + "/", Path(child.path)))
            elif linked or rules.is_secret(child.name) or rules.file_class(child.name) is None:
                continue
            elif os.path.normcase(relative) not in listed:
                found.append(relative)
    return sorted(found)


def _own_file_is_wrong(directory: Path, index: list[IndexEntry]) -> bool:
    """``.vcp-code/snapshot.json`` is not what the index makes of it (or is not there)."""
    try:
        on_disk = (directory / VCP_CODE_DIR / SNAPSHOT_FILE).read_bytes()
    except FileNotFoundError:
        return True
    return on_disk != snapshot_json_bytes(index)


def verify_dir(directory: Path) -> VerifyDirResult:
    """Check a directory ``export`` wrote (or one somebody completed by hand) against its own
    ``.vcp-code/index.jsonl``: no git, no data root, no original repository. A directory with no
    index is ``not_found:`` -- it is no export; an index that is unsorted, lists a path twice or
    has a row it would not have written is a ``mismatch`` of the index file, and nothing else is
    looked at (there is nothing to hold it against)."""
    directory = Path(directory)
    index_file = directory / VCP_CODE_DIR / INDEX_FILE
    if not index_file.is_file():
        raise ValidationFailed(
            f"not_found: {directory} has no {VCP_CODE_DIR}/{INDEX_FILE}: it is not a directory "
            "`vcp code export` wrote"
        )
    try:
        index = parse_index(index_file.read_bytes())
    except ValueError:
        return VerifyDirResult(0, 0, [_INDEX_PATH], [], [], [])
    mismatch = [_SNAPSHOT_PATH] if _own_file_is_wrong(directory, index) else []
    missing: list[str] = []
    not_kept: list[str] = []
    for entry in index:
        path = directory / entry.path
        if path.is_file():
            if sha256_file(path) != entry.sha256:
                mismatch.append(entry.path)
        elif path.exists():  # a directory, or something else, where the file should be
            mismatch.append(entry.path)
        elif entry.kept:
            missing.append(entry.path)
        else:
            not_kept.append(entry.path)
    listed = {os.path.normcase(entry.path) for entry in index}
    return VerifyDirResult(
        files=len(index),
        kept=sum(1 for entry in index if entry.kept),
        mismatch=mismatch,
        missing=missing,
        not_kept=not_kept,
        extra=_extras(directory, listed),
    )
