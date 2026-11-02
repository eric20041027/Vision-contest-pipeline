"""The disk inventory's walk (0.17.0 spec 5.2 and 5.5): every file under the roots, classified by
its place in the layout, merged into items no wider than their unit, counted once, and the copies
of referenced files found by hashing only the candidates whose size matches one (and the
original they match, once, to be sure it still holds those bytes).

Read-only: nothing is deleted, changed or locked, and nothing but those files is hashed. The
roots are the data root (recursive), each checkpoint directory the train records name (the
files directly in it) and each ``--root`` (recursive). The rules follow the path, not the root:
a ``--root`` walk never enters the data root -- the data root's own walk covers it, with the data
root's classes and units -- and never counts the files directly in a checkpoint directory that
is walked as one, so each file is counted once. Links and junctions are neither entered nor
counted. Whatever a directory holds that was not seen -- one of those, a part another walk
covers, an entry that could not be read -- keeps it from being one item. Memory holds the
summaries on the current path and the items listed, never one object per file of the tree.
"""

from __future__ import annotations

import math
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from vcp.artifact.store import MANIFEST, SPEC
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import store_path
from vcp.core.time import stamp
from vcp.gc.layout import CLASSES, Where, classify, unit_of
from vcp.gc.references import References, collect_references, path_key

MIB = 1024**2
DEFAULT_MIN_MIB = 100.0

Source = Literal["data_root", "checkpoint_dir", "option"]
Kind = Literal["file", "dir"]

_DUPLICATE_CLASSES = frozenset({"record", "unreferenced"})
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_LATEST = datetime.max.replace(tzinfo=UTC)
_EARLIEST = datetime.min.replace(tzinfo=UTC)
# ``References.unreadable`` keeps the path alone: what failed there was examining it.
_REFERENCE_ERROR = "OSError"
# Windows opens no longer path unless long paths are enabled, and says FileNotFoundError.
_MAX_PATH = 260
# The walk's own listing, apart from the one ``collect_references`` and pathlib use.
_scandir = os.scandir


@dataclass(frozen=True)
class Root:
    """A directory the scan was asked to look at; ``path`` is resolved."""

    path: Path
    recursive: bool
    source: Source


@dataclass(frozen=True)
class Item:
    """One listed file, or one directory every file below which has the same class, referrers
    and copy. ``path`` is ``store_path`` (no trailing ``/``); ``mtime`` is the newest file's."""

    path: str
    kind: Kind
    bytes: int
    files: int
    cls: str
    referrers: tuple[str, ...]
    copy: str | None
    duplicate_of: str | None
    duplicate_bytes: int
    mtime: str

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "bytes": self.bytes,
            "files": self.files,
            "class": self.cls,
            "referrers": list(self.referrers),
            "copy": self.copy,
            "duplicate_of": self.duplicate_of,
            "duplicate_bytes": self.duplicate_bytes,
            "mtime": self.mtime,
        }


@dataclass(frozen=True)
class Unreadable:
    """A directory or file the scan could not read; ``error`` is the exception's class name."""

    path: str
    error: str


@dataclass(frozen=True)
class ScanResult:
    """``totals`` holds every class, ``(bytes, files)`` over every file whatever its size;
    ``items`` only those of at least ``min_bytes``, largest first, then by path."""

    scanned_at: str
    finished_at: str
    data_root: Path
    configs_root: Path
    roots: list[Root]
    min_bytes: int
    totals: dict[str, tuple[int, int]]
    duplicates: tuple[int, int]
    items: list[Item]
    references: dict[str, int]
    missing: int
    skipped: list[str]
    unreadable: list[Unreadable]
    running: list[str]


def check_min_mib(value: float) -> int:
    """``--min-mib`` as a byte threshold."""
    if not math.isfinite(value) or value < 0 or not math.isfinite(value * MIB):
        raise ValidationFailed(
            f"invalid: --min-mib must be a finite number >= 0, got {value!r}",
            fields={"min_mib": value},
        )
    return math.floor(value * MIB)


def check_roots(paths: Sequence[Path]) -> list[Path]:
    """Each ``--root`` must be a directory; returned resolved."""
    out: list[Path] = []
    for given in paths:
        path = Path(given).expanduser()
        if not path.is_dir():
            raise ValidationFailed(
                f"not_found: --root {path} is not a directory", fields={"root": str(path)}
            )
        out.append(path.resolve())
    return out


_Sig = tuple[str, tuple[str, ...], str | None]


@dataclass(frozen=True)
class _Uniform:
    """Every file below ``parts`` has one ``sig`` (class, referrers, copy) and one unit."""

    sig: _Sig
    unit: tuple[str, ...]
    parts: tuple[str, ...]
    kind: Kind
    bytes: int
    files: int
    mtime: float
    dup_of: str | None
    dup_bytes: int


class _Mixed:
    """A directory whose files cannot be one item; what it could list, it has listed."""


_MIXED = _Mixed()
# None: nothing below.
_Summary = _Uniform | _Mixed | None


@dataclass(frozen=True)
class _Context:
    """What a directory hands down: the referrers of the directories above it that a record
    names whole, whether it is in the configs root, and in the data root whether it is in a
    partial or a committed artifact, or in a run whose walk failed."""

    referrers: frozenset[str]
    in_configs: bool
    partial: bool = False
    committed: bool = False
    skipped_run: bool = False


@dataclass(frozen=True)
class _Walk:
    root: Path
    where: Where
    recursive: bool

    def shown(self, parts: tuple[str, ...]) -> str:
        """``store_path`` of a path below the root, without resolving it again."""
        if self.where == "data":
            return "/".join(parts) if parts else "."
        if not parts:
            return self.root.as_posix()
        return "/".join((self.root.as_posix().rstrip("/"), *parts))


@dataclass
class _Frame:
    """A directory on the current path: the subdirectories still to walk, and the merge so far
    -- blocked, or one ``sig`` and unit with the sums and the children worth listing should it
    block later."""

    path: str
    parts: tuple[str, ...]
    ctx: _Context
    subdirs: list[str] = field(default_factory=list)
    blocked: bool = False
    sig: _Sig | None = None
    unit: tuple[str, ...] = ()
    bytes: int = 0
    files: int = 0
    mtime: float = -math.inf
    dup_bytes: int = 0
    held: list[_Uniform] = field(default_factory=list)


def _within(key: str, base: str) -> bool:
    return key == base or key.startswith(base if base.endswith(os.sep) else base + os.sep)


def _within_unit(parts: tuple[str, ...], unit: tuple[str, ...]) -> bool:
    """A directory may merge only at or below the unit: the unit is a prefix of its parts."""
    return parts[: len(unit)] == unit


def _mtime(seconds: float) -> str:
    """The epoch plus a delta rather than fromtimestamp, which Windows refuses before 1970; a
    time past what a datetime holds (Windows allows year 30828) is clamped to its end."""
    try:
        return stamp(_EPOCH + timedelta(seconds=seconds))
    except OverflowError:
        return stamp(_LATEST if seconds > 0 else _EARLIEST)


def _beyond_max_path(path: str) -> bool:
    return os.name == "nt" and len(path) >= _MAX_PATH


def _name(entry: os.DirEntry[str]) -> str:
    return entry.name


def _is_file(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_file()
    except OSError:
        return False


def _artifact_context(ctx: _Context, entries: list[os.DirEntry[str]]) -> _Context:
    """``artifacts/<kind>/<id>/``: committed with ``manifest.json``, partial with ``spec.json``
    alone -- the rule ``vcp artifact status`` and ``clean`` use; neither is a foreign dir."""
    files = {entry.name for entry in entries if _is_file(entry)}
    committed = MANIFEST in files
    return replace(ctx, committed=committed, partial=not committed and SPEC in files)


@dataclass(frozen=True)
class _Original:
    """A referenced file a copy may be of: ``shown`` is its ``store_path``."""

    shown: str
    path: Path


def _sized(refs: References, data: Path) -> dict[int, dict[str, list[_Original]]]:
    """The referenced files a record vouches a sha for, by size, then sha, in ``store_path``
    order: the only sizes worth hashing a candidate for. A file no longer on disk is no
    original, and neither is one whose size is no longer the recorded one (it changed)."""
    table: dict[int, dict[str, list[_Original]]] = {}
    for ref in refs.files.values():
        if ref.sha256 is None:
            continue
        try:
            st = os.stat(ref.path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or (ref.size is not None and ref.size != st.st_size):
            continue
        original = _Original(store_path(ref.path, data), ref.path)
        table.setdefault(st.st_size, {}).setdefault(ref.sha256, []).append(original)
    for by_sha in table.values():
        for originals in by_sha.values():
            originals.sort(key=lambda o: o.shown)
    return table


def _may_be_one_file(a: Path | str, b: Path) -> bool:
    """A hard link or another spelling of the original is no copy of it. ``os.stat``, not
    ``DirEntry.stat``: the latter has no file id on Windows. When either cannot be examined,
    the answer is yes -- no duplicate is claimed on a guess."""
    try:
        return os.path.samestat(os.stat(a), os.stat(b))
    except OSError:
        return True


class _Scanner:
    """Walks one root at a time into shared totals, duplicates, items and unreadables."""

    def __init__(
        self,
        refs: References,
        data: Path,
        configs: Path,
        *,
        min_bytes: int,
        walked_checkpoints: frozenset[str],
    ) -> None:
        self.refs = refs
        self.data_key = path_key(data)
        self.configs_key = path_key(configs)
        self.min_bytes = min_bytes
        self.walked_checkpoints = walked_checkpoints
        self.dup_floor = max(MIB, min_bytes)
        self.sized = _sized(refs, data)
        self.totals: dict[str, list[int]] = {cls: [0, 0] for cls in CLASSES}
        self.duplicates = [0, 0]
        self.items: list[Item] = []
        self.unreadable: dict[str, Unreadable] = {}
        self.original_sha: dict[str, str | None] = {}

    def run(self, w: _Walk) -> None:
        """Post-order over the root's directories; the root itself is above every unit, so it
        lists its children and is never an item."""
        top = self._open(w, None, str(w.root), (), self._root_context(w.root))
        stack = [] if top is None else [top]
        while stack:
            frame = stack[-1]
            if frame.subdirs:
                child = self._enter(w, frame, frame.subdirs.pop())
                if child is not None:
                    stack.append(child)
                continue
            stack.pop()
            summary = self._close(frame)
            if stack:
                self._add(w, stack[-1], summary)

    def _root_context(self, root: Path) -> _Context:
        referrers: set[str] = set()
        for path in (root, *root.parents):
            referrers |= self.refs.dirs.get(path_key(path), frozenset())
        return _Context(frozenset(referrers), _within(path_key(root), self.configs_key))

    def _enter(self, w: _Walk, frame: _Frame, name: str) -> _Frame | None:
        path = os.path.join(frame.path, name)
        key = os.path.normcase(path)
        parts = (*frame.parts, name)
        up = frame.ctx
        named = self.refs.dirs.get(key)
        failed_run = (
            w.where == "data"
            and len(parts) == 2
            and parts[0] == "runs"
            and name in self.refs.skipped_runs
        )
        ctx = replace(
            up,
            referrers=(up.referrers | named) if named else up.referrers,
            in_configs=up.in_configs or key == self.configs_key,
            skipped_run=up.skipped_run or failed_run,
        )
        return self._open(w, frame, path, parts, ctx)

    def _open(
        self, w: _Walk, parent: _Frame | None, path: str, parts: tuple[str, ...], ctx: _Context
    ) -> _Frame | None:
        """List a directory in name order: its files are summarized now, its subdirectories
        kept to walk. One that cannot be listed keeps ``parent`` from being one item."""
        try:
            with _scandir(path) as listing:
                entries = sorted(listing, key=_name)
        except OSError as e:
            self._fail(w, parent, path, parts, e)
            return None
        if w.where == "data" and len(parts) == 3 and parts[0] == "artifacts":
            ctx = _artifact_context(ctx, entries)
        frame = _Frame(path, parts, ctx)
        files_elsewhere = w.recursive and os.path.normcase(path) in self.walked_checkpoints
        for entry in entries:
            try:
                self._entry(w, frame, entry, files_elsewhere=files_elsewhere)
            except OSError as e:
                self._fail(w, frame, entry.path, (*parts, entry.name), e)
        frame.subdirs.reverse()  # popped from the end: walked in name order
        return frame

    def _entry(
        self, w: _Walk, frame: _Frame, entry: os.DirEntry[str], *, files_elsewhere: bool
    ) -> None:
        if entry.is_symlink() or entry.is_junction():
            # not followed; a record may name a path through it, so its directory is no item
            self._block(w, frame)
            return
        if entry.is_dir(follow_symlinks=False):
            if not w.recursive:
                return
            if os.path.normcase(entry.path) == self.data_key:
                self._block(w, frame)  # a root of its own, walked with its own rules
            else:
                frame.subdirs.append(entry.name)
            return
        if not entry.is_file(follow_symlinks=False):
            return
        if files_elsewhere:
            self._block(w, frame)  # the checkpoint directory's own walk lists it, file by file
            return
        self._add(w, frame, self._file(w, frame, entry))

    def _file(self, w: _Walk, frame: _Frame, entry: os.DirEntry[str]) -> _Uniform | None:
        parts = (*frame.parts, entry.name)
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError as e:
            self._fail(w, frame, entry.path, parts, e)
            return None
        ctx = frame.ctx
        ref = self.refs.files.get(os.path.normcase(entry.path))
        referrers = ctx.referrers if ref is None else (ctx.referrers | ref.referrers)
        cls = classify(
            parts,
            w.where,
            referenced=bool(referrers),
            partial_artifact=ctx.partial,
            committed_artifact=ctx.committed,
            skipped_run=ctx.skipped_run,
            in_configs=ctx.in_configs,
        )
        size = st.st_size
        total = self.totals[cls]
        total[0] += size
        total[1] += 1
        dup_of = self._duplicate_of(w, cls, size, entry.path, parts)
        return _Uniform(
            sig=(cls, tuple(sorted(referrers)), None if ref is None else ref.copy),
            unit=unit_of(parts, w.where),
            parts=parts,
            kind="file",
            bytes=size,
            files=1,
            mtime=st.st_mtime,
            dup_of=dup_of,
            dup_bytes=0 if dup_of is None else size,
        )

    def _duplicate_of(
        self, w: _Walk, cls: str, size: int, path: str, parts: tuple[str, ...]
    ) -> str | None:
        """The referenced file this one is a bit-for-bit copy of. Only a ``record`` or
        ``unreferenced`` file of at least ``max(1 MiB, threshold)`` whose size a referenced
        file with a recorded sha has is hashed; a match is claimed only when the original is
        another file and, hashed too, still holds those bytes."""
        if cls not in _DUPLICATE_CLASSES or size < self.dup_floor:
            return None
        by_sha = self.sized.get(size)
        if not by_sha:
            return None
        try:
            digest = sha256_file(Path(path))
        except OSError as e:
            self._report(w.shown(parts), path, e)
            return None
        for original in by_sha.get(digest, ()):
            if not _may_be_one_file(path, original.path) and self._sha_of(original) == digest:
                self.duplicates[0] += size
                self.duplicates[1] += 1
                return original.shown
        return None

    def _sha_of(self, original: _Original) -> str | None:
        """The original's sha as it is on disk now, hashed once per scan; None when it cannot
        be read."""
        key = path_key(original.path)
        if key not in self.original_sha:
            try:
                self.original_sha[key] = sha256_file(original.path)
            except OSError as e:
                self._report(original.shown, str(original.path), e)
                self.original_sha[key] = None
        return self.original_sha[key]

    def _add(self, w: _Walk, frame: _Frame, summary: _Summary) -> None:
        if summary is None:
            return
        if isinstance(summary, _Mixed):
            self._block(w, frame)
            return
        if not frame.blocked:
            if frame.sig is None and _within_unit(frame.parts, summary.unit):
                frame.sig, frame.unit = summary.sig, summary.unit
            elif frame.sig is None or (summary.sig, summary.unit) != (frame.sig, frame.unit):
                self._block(w, frame)
        if frame.blocked:
            self._list(w, summary)
            return
        frame.bytes += summary.bytes
        frame.files += summary.files
        frame.mtime = max(frame.mtime, summary.mtime)
        frame.dup_bytes += summary.dup_bytes
        if summary.bytes >= self.min_bytes:
            frame.held.append(summary)

    def _block(self, w: _Walk, frame: _Frame) -> None:
        """The directory will not be one item: what it held to list, it lists now."""
        if frame.blocked:
            return
        frame.blocked = True
        for held in frame.held:
            self._list(w, held)
        frame.held = []

    def _list(self, w: _Walk, summary: _Uniform) -> None:
        if summary.bytes < self.min_bytes:
            return
        cls, referrers, copy = summary.sig
        self.items.append(
            Item(
                path=w.shown(summary.parts),
                kind=summary.kind,
                bytes=summary.bytes,
                files=summary.files,
                cls=cls,
                referrers=referrers,
                copy=copy,
                duplicate_of=summary.dup_of,
                duplicate_bytes=summary.dup_bytes,
                mtime=_mtime(summary.mtime),
            )
        )

    @staticmethod
    def _close(frame: _Frame) -> _Summary:
        if frame.blocked:
            return _MIXED
        if frame.sig is None:
            return None
        return _Uniform(
            sig=frame.sig,
            unit=frame.unit,
            parts=frame.parts,
            kind="dir",
            bytes=frame.bytes,
            files=frame.files,
            mtime=frame.mtime,
            dup_of=None,
            dup_bytes=frame.dup_bytes,
        )

    def _fail(
        self, w: _Walk, parent: _Frame | None, path: str, parts: tuple[str, ...], error: OSError
    ) -> None:
        """Something below ``parent`` was not seen, so ``parent`` is no item: what is not seen
        may be anything, a registered checkpoint included."""
        if parent is not None:
            self._block(w, parent)
        self._report(w.shown(parts), path, error)

    def _report(self, shown: str, path: str, error: OSError) -> None:
        """Name what could not be read. A vanished entry is no finding -- unless Windows said
        FileNotFoundError because the path is too long for it to open."""
        if not isinstance(error, FileNotFoundError) or _beyond_max_path(path):
            self._unreadable(shown, error)

    def _unreadable(self, shown: str, error: OSError) -> None:
        self.unreadable.setdefault(shown, Unreadable(shown, type(error).__name__))


def _scannable_checkpoint_dir(path: Path) -> bool:
    """A checkpoint directory that is gone is nothing to scan, and an anchor (a drive root,
    ``/``) -- where an absolute glob's walk up may end -- is no checkpoint directory. One that
    cannot be examined is kept, so its walk reports it unreadable."""
    if path.parent == path:
        return False
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True


def _unique(paths: Sequence[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        if path_key(path) not in seen:
            seen.add(path_key(path))
            out.append(path)
    return out


def _roots(data: Path, checkpoints: list[Path], options: list[Path]) -> list[Root]:
    """One entry per distinct path; the first source wins (data root, checkpoint directory,
    ``--root``). A root inside another is listed too."""
    candidates = [
        Root(data, True, "data_root"),
        *(Root(path, False, "checkpoint_dir") for path in checkpoints),
        *(Root(path, True, "option") for path in options),
    ]
    seen: set[str] = set()
    out: list[Root] = []
    for root in candidates:
        if path_key(root.path) not in seen:
            seen.add(path_key(root.path))
            out.append(root)
    return out


def _walks(data: Path, checkpoints: list[Path], options: list[Path]) -> list[_Walk]:
    """What is actually walked: the data root whole; a checkpoint directory outside it, its
    own files; a ``--root`` outside it and not inside another ``--root``, whole."""
    data_key = path_key(data)
    outside = [path for path in _unique(options) if not _within(path_key(path), data_key)]
    outermost = [
        path
        for path in outside
        if not any(
            path_key(other) != path_key(path) and _within(path_key(path), path_key(other))
            for other in outside
        )
    ]
    return [
        _Walk(data, "data", recursive=True),
        *(
            _Walk(path, "checkpoint", recursive=False)
            for path in _unique(checkpoints)
            if not _within(path_key(path), data_key)
        ),
        *(_Walk(path, "option", recursive=True) for path in outermost),
    ]


def scan(
    data_root: Path, configs_root: Path, *, options: Sequence[Path] = (), min_bytes: int
) -> ScanResult:
    """Read every reference, then walk the roots (``options`` have passed ``check_roots``)."""
    scanned_at = stamp()
    refs = collect_references(data_root, configs_root)
    data = Path(data_root).resolve()
    configs = Path(configs_root).resolve()
    checkpoints = [path for path in refs.checkpoint_dirs if _scannable_checkpoint_dir(path)]
    resolved = [Path(path).resolve() for path in options]
    walks = _walks(data, checkpoints, resolved)
    scanner = _Scanner(
        refs,
        data,
        configs,
        min_bytes=min_bytes,
        walked_checkpoints=frozenset(path_key(w.root) for w in walks if w.where == "checkpoint"),
    )
    for walk in walks:
        scanner.run(walk)
    finished_at = stamp()
    unreadable = dict(scanner.unreadable)
    for shown in refs.unreadable:
        unreadable.setdefault(shown, Unreadable(shown, _REFERENCE_ERROR))
    return ScanResult(
        scanned_at=scanned_at,
        finished_at=finished_at,
        data_root=data,
        configs_root=configs,
        roots=_roots(data, checkpoints, resolved),
        min_bytes=min_bytes,
        totals={cls: (b, f) for cls, (b, f) in scanner.totals.items()},
        duplicates=(scanner.duplicates[0], scanner.duplicates[1]),
        items=sorted(scanner.items, key=lambda item: (-item.bytes, item.path)),
        references=dict(refs.counts),
        missing=refs.missing,
        skipped=list(refs.skipped),
        unreadable=sorted(unreadable.values(), key=lambda u: u.path),
        running=list(refs.running),
    )
