"""The ``code_snapshot`` artifact (spec 2026-10-31 §4.1, controller ruling C3): one scanned code
root, kept as an immutable artifact whose id is its content (``code-<16 hex of the identity>``).

One content is one directory, wherever it was scanned and whoever wrote it. The writers of one id
queue on ``<data_root>/locks/code_snapshot-<id>.lock``; the one that gets the lock finds the
snapshot already committed (reuse) or writes it. A directory without ``manifest.json`` -- an
earlier writer was interrupted -- blocks its id until ``vcp artifact clean`` removes it: there is
no reopening. A snapshot that is there but does not match its id is ``mismatch:``, as a source
audit is (``vcp.data.source_audit``).

A snapshot that cannot be written is an outcome, not an exception: the caller decides whether a
missing snapshot is a downgrade (``train run`` without an authorization) or a refusal (with one,
or for the commands that ask for the snapshot). A full disk is the exception to that: it is
raised as it came, so ``own_writes`` in ``train run`` turns it into ``disk_full:``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vcp.artifact import store
from vcp.artifact.schema import RESERVED_NAMES, ArtifactManifest, ArtifactSpec, check_file_name
from vcp.artifact.writer import ArtifactWriter
from vcp.code import rules
from vcp.code.scan import (
    IndexEntry,
    RootScan,
    identity_sha256,
    index_jsonl_bytes,
    snapshot_json_bytes,
)
from vcp.core import atomic
from vcp.core.errors import IntegrityError, ValidationFailed, VcpError
from vcp.core.hashing import sha256_file
from vcp.core.lock import LOCKS_DIR, file_lock
from vcp.core.paths import artifact_dir, check_relative_path

__all__ = [
    "FILES_DIR",
    "INDEX_FILE",
    "KIND",
    "MAX_PATH",
    "SNAPSHOT_FILE",
    "SNAPSHOT_PATH",
    "SnapshotCheck",
    "SnapshotOutcome",
    "WriteReason",
    "check_snapshot",
    "load_index",
    "lock_file",
    "snapshot_json_path",
    "snapshot_spec",
    "write_snapshot",
]

KIND = "code_snapshot"
SNAPSHOT_FILE = "snapshot.json"
INDEX_FILE = "index.jsonl"
FILES_DIR = "files"
# What a stored path to a snapshot's ``snapshot.json`` looks like (a ref's ``path``).
SNAPSHOT_PATH = re.compile(r"^artifacts/code_snapshot/(code-[0-9a-f]{16})/snapshot\.json$")
# A Windows path of more than this many characters (260 with the terminating NUL) is not safe to
# write, or to restore on another machine. Tests patch both names, never ``os.name``.
MAX_PATH = 259
_WINDOWS = os.name == "nt"
# What `write_once_stream` adds to a name for the temp file it opens beside the target
# (`.<name>.<8 hex>.tmp`), measured on atomic's own naming so the two cannot drift apart. It is the
# temp path that has to fit, not the final one.
_TMP_EXTRA = len(atomic._tmp_path(Path("x")).name) - len("x")
# The longest name the writer creates directly in the artifact directory (`manifest.json`).
_TOP_LEVEL_NAME = max(len(name) for name in (*RESERVED_NAMES, SNAPSHOT_FILE, INDEX_FILE))

WriteReason = Literal["locked", "partial", "unwritable", "drift"]

_SHA = re.compile(r"^[0-9a-f]{64}$")
_INDEX_KEYS = frozenset({"path", "sha256", "bytes", "kept"})
_REPAIR = "move the directory aside and snapshot again"


@dataclass(frozen=True)
class SnapshotOutcome:
    """What ``write_snapshot`` did. ``state`` says whether the snapshot was written now
    (``created``) or was already there (``reused``); with a ``reason`` it could not be written and
    ``state`` is None. ``kept`` / ``not_kept`` count the files whose bytes the snapshot holds and
    those it records by sha only -- of the snapshot that is there on ``reused``, of the scan
    otherwise."""

    snapshot_id: str
    state: Literal["created", "reused"] | None
    reason: WriteReason | None
    kept: int
    not_kept: int


@dataclass(frozen=True)
class SnapshotCheck:
    """What ``check_snapshot`` found: ``mismatch`` names the files that differ (or that nobody
    listed), ``missing`` the kept files that are gone; ``files`` and ``kept`` count the index."""

    ok: bool
    mismatch: list[str]
    missing: list[str]
    files: int
    kept: int


class _Drift(Exception):
    """A file the scan listed is not what the scan hashed any more (or is gone)."""


def lock_file(data_root: Path, snapshot_id: str) -> Path:
    """``<data_root>/locks/code_snapshot-<id>.lock``. Not ``lock.lock_path``: that hashes a path,
    and the id already is the name of the thing being locked."""
    return Path(data_root) / LOCKS_DIR / f"{KIND}-{snapshot_id}.lock"


def snapshot_json_path(data_root: Path, snapshot_id: str) -> Path:
    return artifact_dir(Path(data_root), KIND, snapshot_id) / SNAPSHOT_FILE


def snapshot_spec(scan: RootScan) -> ArtifactSpec:
    """The job: the id and the identity it was made from. No inputs -- the files are copied from a
    work tree that has no business being named in an artifact that is the same on every machine."""
    identity = scan.identity_sha256
    if identity is None:
        raise ValueError(f"a root that was not scanned has no snapshot (reason={scan.reason})")
    return ArtifactSpec(
        kind=KIND,
        id=f"code-{identity[:16]}",
        params={"rules": str(rules.RULES_VERSION), "identity_sha256": identity},
    )


def _ident(snapshot_id: str) -> dict[str, str]:
    return {"kind": KIND, "id": snapshot_id}


def _mismatch(snapshot_id: str, what: str) -> IntegrityError:
    return IntegrityError(
        f"mismatch: {KIND}/{snapshot_id} {what}; {_REPAIR}", fields=_ident(snapshot_id)
    )


def _manifest(data_root: Path, snapshot_id: str) -> ArtifactManifest:
    """``store.load_manifest`` (``not_found:`` / ``partial:`` as it raises them); a manifest that
    does not parse is a snapshot that does not hold, which is ``mismatch:`` like any other."""
    try:
        return store.load_manifest(data_root, KIND, snapshot_id)
    except ValidationFailed as e:
        if not str(e).startswith("bad manifest:"):
            raise
        raise _mismatch(snapshot_id, f"has a manifest that cannot be read ({e})") from e


# -- the index ----------------------------------------------------------------------------------


def _index_entry(line: str) -> IndexEntry:
    """One ``index.jsonl`` line; ``ValueError`` says what is wrong with it."""
    doc = json.loads(line)
    if not isinstance(doc, dict) or set(doc) != _INDEX_KEYS:
        raise ValueError(f"expected exactly the keys {sorted(_INDEX_KEYS)}")
    path, digest, size, kept = doc["path"], doc["sha256"], doc["bytes"], doc["kept"]
    if not isinstance(path, str):
        raise ValueError("path is not a string")
    check_relative_path(path)
    if not isinstance(digest, str) or not _SHA.fullmatch(digest):
        raise ValueError("sha256 is not 64 lowercase hex characters")
    if type(size) is not int or size < 0:
        raise ValueError("bytes is not a non-negative integer")
    if type(kept) is not bool:
        raise ValueError("kept is not a boolean")
    return IndexEntry(path, digest, size, kept)


def _read_index(path: Path, snapshot_id: str) -> list[IndexEntry]:
    def bad(why: str) -> IntegrityError:
        return _mismatch(snapshot_id, f"{INDEX_FILE} {why}")

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise bad("is missing") from None
    try:
        # Split on "\n" alone: a path may hold U+2028 or U+0085, which `splitlines` cuts at.
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise bad(f"is not UTF-8 ({e})") from e
    if text and not text.endswith("\n"):
        raise bad("ends in the middle of a line")
    entries: list[IndexEntry] = []
    for number, line in enumerate(text.split("\n")[:-1], start=1):
        try:
            entry = _index_entry(line)
        except ValueError as e:
            raise bad(f"line {number} is not an index row ({e})") from e
        if entries and entry.path <= entries[-1].path:
            twice = entry.path == entries[-1].path
            raise bad(
                f"line {number}: {entry.path!r} is {'listed twice' if twice else 'out of order'}"
            )
        entries.append(entry)
    return entries


def load_index(data_root: Path, snapshot_id: str) -> list[IndexEntry]:
    """The entries of a committed snapshot, in path order. ``not_found:`` / ``partial:`` as
    ``store.load_manifest`` raises them; an index that is missing, unsorted, lists a path twice or
    has a row it would not have written is ``mismatch:``."""
    data_root = Path(data_root)
    _manifest(data_root, snapshot_id)
    return _read_index(artifact_dir(data_root, KIND, snapshot_id) / INDEX_FILE, snapshot_id)


# -- writing ------------------------------------------------------------------------------------


def _outcome(
    scan: RootScan, state: Literal["created", "reused"] | None, reason: WriteReason | None
) -> SnapshotOutcome:
    return SnapshotOutcome(str(scan.snapshot_id), state, reason, scan.kept, scan.not_kept)


def _reuse(data_root: Path, scan: RootScan, snapshot_id: str) -> SnapshotOutcome:
    """The snapshot is committed: it must be this one. Cheap by design -- the identity, the
    ``snapshot.json`` pinned to it and the index; the kept files are ``check_snapshot``'s."""
    directory = artifact_dir(data_root, KIND, snapshot_id)
    manifest = _manifest(data_root, snapshot_id)
    identity = scan.identity_sha256
    wanted = hashlib.sha256(snapshot_json_bytes(scan.entries)).hexdigest()
    pinned = manifest.file(SNAPSHOT_FILE)
    try:
        on_disk: str | None = sha256_file(directory / SNAPSHOT_FILE)
    except FileNotFoundError:
        on_disk = None
    if manifest.spec.params.get("identity_sha256") != identity:
        raise _mismatch(snapshot_id, "was committed for another identity")
    if pinned is None or pinned.sha256 != wanted or on_disk != wanted:
        raise _mismatch(snapshot_id, f"has a {SNAPSHOT_FILE} that is not this content's")
    index = _read_index(directory / INDEX_FILE, snapshot_id)
    if identity_sha256(index) != identity:
        raise _mismatch(snapshot_id, f"has an {INDEX_FILE} that is not this content's")
    kept = sum(1 for entry in index if entry.kept)
    return SnapshotOutcome(snapshot_id, "reused", None, kept, len(index) - kept)


def _has_a_path_the_artifact_cannot_hold(scan: RootScan) -> bool:
    """A path ``index.jsonl`` could not be read back with (a file named ``a\\b.py`` on POSIX), or
    that ``ArtifactWriter`` refuses as a file name. Asked for every entry before the id is claimed:
    written anyway, the snapshot would be committed and then never load."""
    for entry in scan.entries:
        try:
            check_relative_path(entry.path)
            if entry.kept:
                check_file_name(f"{FILES_DIR}/{entry.path}")
        except ValueError:
            return True
    return False


def _has_a_case_clash(scan: RootScan) -> bool:
    """Two kept paths that differ only in case are one file on a case-insensitive disk (macOS,
    Windows, a share): the writer would raise ``exists:`` halfway, and the snapshot could not be
    restored there. Asked on every platform, before the id is claimed."""
    folded = [entry.path.casefold() for entry in scan.entries if entry.kept]
    return len(set(folded)) != len(folded)


def _too_long_for_windows(data_root: Path, scan: RootScan, snapshot_id: str) -> bool:
    """A temp path the writer would open that is longer than ``MAX_PATH``: for a kept file, beside
    its place under ``files/``; for the writer's own files, in the artifact directory. Asked before
    the id is claimed, so a root that cannot be written leaves no directory behind to block its
    own content."""
    directory = os.path.abspath(artifact_dir(data_root, KIND, snapshot_id))
    if len(directory) + 1 + _TOP_LEVEL_NAME + _TMP_EXTRA > MAX_PATH:
        return True
    base = os.path.abspath(os.path.join(directory, FILES_DIR))
    return any(
        len(base) + 1 + len(entry.path) + _TMP_EXTRA > MAX_PATH
        for entry in scan.entries
        if entry.kept
    )


def _copy_kept(writer: ArtifactWriter, root: Path, entry: IndexEntry) -> None:
    """Copy one kept file in and compare what was copied with what the scan hashed. A file that is
    gone, or is not the file the scan saw, is ``_Drift`` -- never a bare ``FileNotFoundError``."""
    source = root / entry.path
    try:
        written = writer.add_file(f"{FILES_DIR}/{entry.path}", source)
    except ValidationFailed as e:
        if str(e).startswith("not_found:") and not source.is_file():
            raise _Drift(f"drift: {entry.path} is gone since the scan") from e
        raise
    except (FileNotFoundError, NotADirectoryError) as e:
        if not source.is_file():  # the source went away while it was read, not the target path
            raise _Drift(f"drift: {entry.path} is gone since the scan") from e
        raise
    if written.sha256 != entry.sha256:
        raise _Drift(
            f"drift: {entry.path} changed since the scan "
            f"({entry.sha256[:12]} -> {written.sha256[:12]})"
        )


def _create(data_root: Path, scan: RootScan) -> SnapshotOutcome:
    try:
        with ArtifactWriter.create(snapshot_spec(scan), data_root=data_root) as writer:
            writer.write_bytes(SNAPSHOT_FILE, snapshot_json_bytes(scan.entries))
            writer.write_bytes(INDEX_FILE, index_jsonl_bytes(scan.entries))
            for entry in scan.entries:
                if entry.kept:
                    _copy_kept(writer, scan.root, entry)
            writer.commit()
    except _Drift:
        # The directory stays, as any interrupted job's does (with its failure.json).
        return _outcome(scan, None, "drift")
    except OSError as e:
        from vcp.train.disk import is_disk_full  # not at import time: vcp.train imports this

        if is_disk_full(e):
            raise
        return _outcome(scan, None, "unwritable")
    return _outcome(scan, "created", None)


def _write_locked(data_root: Path, scan: RootScan, snapshot_id: str) -> SnapshotOutcome:
    if store.is_partial(data_root, KIND, snapshot_id):
        return _outcome(scan, None, "partial")
    if store.manifest_path(data_root, KIND, snapshot_id).is_file():
        return _reuse(data_root, scan, snapshot_id)
    if _has_a_path_the_artifact_cannot_hold(scan):
        return _outcome(scan, None, "unwritable")
    if _has_a_case_clash(scan):
        return _outcome(scan, None, "unwritable")
    if _WINDOWS and _too_long_for_windows(data_root, scan, snapshot_id):
        return _outcome(scan, None, "unwritable")
    return _create(data_root, scan)


def write_snapshot(data_root: Path, scan: RootScan, *, command: str) -> SnapshotOutcome:
    """Write the snapshot of a scanned root, or find it already written (spec §4.1).

    Under ``lock_file`` (a minute's wait; ``locked`` after that): a committed snapshot of this
    content is reused, a half-written directory blocks (``partial``), otherwise the artifact is
    written -- ``snapshot.json``, ``index.jsonl`` and the bytes of every kept file, each compared
    with what the scan hashed (``drift`` when a file changed or went away since). A committed
    snapshot that is not this content is ``IntegrityError`` ``mismatch:``; a full disk is raised
    as the ``OSError`` it is. A ``scan`` with a ``reason`` is the caller's bug (``ValueError``)."""
    snapshot_id = scan.snapshot_id
    if snapshot_id is None:
        raise ValueError(f"a root that was not scanned has no snapshot (reason={scan.reason})")
    data_root = Path(data_root)
    holding = False
    try:
        with file_lock(
            lock_file(data_root, snapshot_id), command=command, label=f"{KIND} {snapshot_id}"
        ):
            holding = True
            return _write_locked(data_root, scan, snapshot_id)
    except VcpError as e:
        if holding or not str(e).startswith("locked:"):
            raise
    return _outcome(scan, None, "locked")


# -- checking -----------------------------------------------------------------------------------


def check_snapshot(data_root: Path, snapshot_id: str) -> SnapshotCheck:
    """Is this snapshot what its id says? ``store.verify`` (every file against the manifest, and
    nothing the manifest does not name), then the files against the *index* -- the manifest alone
    can be rewritten to agree with a rewritten file -- and ``snapshot.json`` and the id against the
    index's identity. ``not_found:`` / ``partial:`` as ``store.load_manifest`` raises them."""
    data_root = Path(data_root)
    manifest = _manifest(data_root, snapshot_id)
    directory = artifact_dir(data_root, KIND, snapshot_id)
    verified = store.verify(data_root, KIND, snapshot_id)
    mismatch = {*verified.mismatch, *verified.extra}
    missing = set(verified.missing)
    index: list[IndexEntry] | None = None
    if not (directory / INDEX_FILE).is_file():  # whether or not the manifest still names it
        missing.add(INDEX_FILE)
    elif INDEX_FILE not in mismatch:
        try:
            index = _read_index(directory / INDEX_FILE, snapshot_id)
        except IntegrityError:
            mismatch.add(INDEX_FILE)
    if index is None:  # nothing to hold the other files against
        return _result(mismatch, missing, 0, 0)
    held = {f.name: f.sha256 for f in manifest.files if f.name.startswith(f"{FILES_DIR}/")}
    for entry in index:
        if not entry.kept:
            continue
        name = f"{FILES_DIR}/{entry.path}"
        if name not in held:
            missing.add(name)
        elif held[name] != entry.sha256:
            mismatch.add(name)
    kept_names = {f"{FILES_DIR}/{entry.path}" for entry in index if entry.kept}
    mismatch.update(name for name in held if name not in kept_names)
    found = _snapshot_json_state(directory, snapshot_id, index)
    if found == "missing":
        missing.add(SNAPSHOT_FILE)
    elif found == "mismatch":
        mismatch.add(SNAPSHOT_FILE)
    return _result(mismatch, missing, len(index), sum(1 for entry in index if entry.kept))


def _snapshot_json_state(
    directory: Path, snapshot_id: str, index: list[IndexEntry]
) -> Literal["ok", "mismatch", "missing"]:
    """``snapshot.json`` is what the index makes of it, and the index's identity is the id. A file
    that is not there is ``missing`` even when the manifest has stopped naming it."""
    try:
        on_disk = (directory / SNAPSHOT_FILE).read_bytes()
    except FileNotFoundError:
        return "missing"
    if f"code-{identity_sha256(index)[:16]}" != snapshot_id or on_disk != snapshot_json_bytes(
        index
    ):
        return "mismatch"
    return "ok"


def _result(mismatch: set[str], missing: set[str], files: int, kept: int) -> SnapshotCheck:
    return SnapshotCheck(not (mismatch or missing), sorted(mismatch), sorted(missing), files, kept)
