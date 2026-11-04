"""The import trace of a training child (spec 2026-10-31 §4.4, §5.2, controller ruling C2).

Under ``vcp train run``, the first ``Session.current()`` of a process arms an ``atexit``: when the
process ends, every module in ``sys.modules`` is put in one class by where its file is, the first
that fits --

- ``generated``: under a location of the generated-code registry (not hashed, not a gap);
- ``root`` / ``changed`` / ``unlisted``: under the innermost code root that has a snapshot -- in
  its index with the same sha, in its index with other bytes (or gone), not in its index. A path
  the snapshot rules skip (a hidden or cache segment, a directory holding ``pyvenv.cfg``) is not
  "in the root": a virtualenv inside the project holds distributions, not the project's code;
- ``stdlib`` / ``distribution``: under the standard library or a site directory, the most specific
  directory deciding (a site-packages inside a standard-library directory holds distributions);
- ``outside``: anything else, a module inside a zip included --

and the result is a ``code_trace`` artifact bound to the attempt as ``code_trace.a<n>.<seq>``.
Only files under a root are hashed. Tracing never changes what the process does or how it exits:
nothing is armed with ``VCP_CODE_TRACE=0``, in a ``multiprocessing`` worker or on a rank other than
0; at exit nothing is imported, and any failure is one stderr line and no artifact.
"""

from __future__ import annotations

import atexit
import multiprocessing
import os
import site
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Literal

from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.code import rules
from vcp.code.generated import generated_locations
from vcp.code.schema import TraceDoc
from vcp.code.snapshot import load_index, snapshot_json_path
from vcp.core.build import build_string
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import key_within, resolve_stored_path
from vcp.core.time import stamp
from vcp.data.evidence import artifact_ref
from vcp.data.evidence_ref import CODE_TRACE_ROLE, current
from vcp.measure.runs import run_dir
from vcp.train.env import read_sections
from vcp.train.records import bind_ref, load_record
from vcp.train.schema import TrainRecord

if TYPE_CHECKING:
    from vcp.train.session import Session

__all__ = [
    "COUNT_KEYS",
    "GAP_CLASSES",
    "KIND",
    "TRACE_FILE",
    "Classified",
    "ModuleClass",
    "TraceResult",
    "TracedRoot",
    "arm",
    "classify_file",
    "classify_modules",
    "site_dirs",
    "stdlib_dirs",
    "write_trace",
]

KIND = "code_trace"
TRACE_FILE = "trace.json"
ModuleClass = Literal[
    "generated", "root", "changed", "unlisted", "stdlib", "distribution", "outside"
]
COUNT_KEYS: tuple[ModuleClass, ...] = (
    "root",
    "generated",
    "stdlib",
    "distribution",
    "outside",
    "unlisted",
    "changed",
)
GAP_CLASSES: tuple[ModuleClass, ...] = ("outside", "unlisted", "changed")
MAX_SEQ_TRIES = 100  # trace ids tried, from the attempt's next seq, before giving up
# The standard library's extension modules live in `<prefix>/DLLs` on Windows. Tests patch this
# name, never `os.name` (a POSIX `Path` cannot be made while `os.name` says "nt").
_WINDOWS = os.name == "nt"
_EXTENDED = "\\\\?\\"  # the prefix of a Windows extended-length path
_UNC = "UNC\\"
_ARMED = False  # one atexit per process


@dataclass(frozen=True)
class TracedRoot:
    """A code root a trace is held against: its snapshot and that snapshot's index (root-relative
    posix path -> sha256)."""

    label: str
    path: Path
    snapshot: str
    index: Mapping[str, str]


@dataclass(frozen=True)
class Classified:
    """One file's class; ``shown`` is how a list names it (``<label>:<path>`` under a root, the
    absolute posix path otherwise) and ``generated_by`` the registry entry of a generated one."""

    cls: ModuleClass
    shown: str
    generated_by: str | None = None


@dataclass(frozen=True)
class TraceResult:
    """What a set of modules came to: exact counts per class, generated modules per registry
    entry, the first ``rules.LISTED_PATHS`` of each gap list (sorted), and whether there is no
    gap."""

    counts: dict[str, int]
    generated_by: dict[str, int]
    outside: list[str]
    unlisted: list[str]
    changed: list[str]
    complete: bool


# -- where a file is -------------------------------------------------------------------------------


def _plain(path: str) -> str:
    """Windows' extended-length spelling (``\\\\?\\C:\\...``, ``\\\\?\\UNC\\host\\...``) as the
    plain path it names: ``Path.resolve`` gives a file reached through a junction (uv's Python
    directories are junctions) in that form and the directory holding it in the plain one."""
    if not _WINDOWS or not path.startswith(_EXTENDED):
        return path
    rest = path[len(_EXTENDED) :]
    return "\\\\" + rest[len(_UNC) :] if rest.upper().startswith(_UNC) else rest


def _resolved(file: str | Path) -> Path:
    """The file's path with links resolved; as given (made absolute) when it cannot be resolved."""
    try:
        return Path(_plain(str(Path(file).resolve())))
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(file))


def _key(path: Path) -> str:
    return os.path.normcase(str(path))


def _in_a_file(path: Path) -> bool:
    """A path below something that is a file: a module inside a zip (or an egg)."""
    return not os.path.isfile(path) and any(os.path.isfile(parent) for parent in path.parents)


class _Root:
    """A ``TracedRoot`` ready to be asked: resolved once, its index keyed the platform's way."""

    def __init__(self, root: TracedRoot) -> None:
        self.label = root.label
        self.path = _resolved(root.path)
        self.key = _key(self.path)
        self.depth = len(self.path.parts)
        self.index = {os.path.normcase(path): (path, sha) for path, sha in root.index.items()}


class _Classifier:
    """The classes in order (module docstring), with every directory resolved once."""

    def __init__(
        self,
        *,
        roots: Sequence[TracedRoot],
        generated: Mapping[str, Path],
        stdlib: Sequence[Path],
        site: Sequence[Path],
    ) -> None:
        self.generated = [(name, _key(_resolved(place))) for name, place in generated.items()]
        # Innermost root first; then the most specific place, the stdlib first on a tie.
        self.roots = sorted((_Root(root) for root in roots), key=lambda r: -len(r.key))
        places = [(_key(_resolved(d)), "stdlib") for d in stdlib]
        places += [(_key(_resolved(d)), "distribution") for d in site]
        self.places: list[tuple[str, ModuleClass]] = sorted(places, key=lambda p: -len(p[0]))
        self._venv: dict[str, bool] = {}

    def classify(self, path: Path, key: str | None = None) -> Classified:
        """The class of a resolved path; ``key`` is its ``_key`` when the caller has it."""
        key = _key(path) if key is None else key
        if _in_a_file(path):
            return Classified("outside", path.as_posix())
        for name, place in self.generated:
            if key_within(key, place):
                return Classified("generated", path.as_posix(), name)
        root = next((r for r in self.roots if key_within(key, r.key)), None)
        if root is not None:
            found = self._in_root(root, path)
            if found is not None:
                return found
        for place, cls in self.places:
            if key_within(key, place):
                return Classified(cls, path.as_posix())
        return Classified("outside", path.as_posix())

    def _is_venv(self, directory: Path) -> bool:
        key = _key(directory)
        if key not in self._venv:
            self._venv[key] = os.path.exists(directory / "pyvenv.cfg")
        return self._venv[key]

    def _in_root(self, root: _Root, path: Path) -> Classified | None:
        """The file's class in ``root``; None for a path the snapshot rules skip, which is not
        in the root at all (Review Focus 1)."""
        parts = path.parts[root.depth :]
        if any(rules.skipped_segment(part) for part in parts):
            return None
        if any(self._is_venv(root.path.joinpath(*parts[:depth])) for depth in range(1, len(parts))):
            return None
        relative = "/".join(parts)
        listed = root.index.get(os.path.normcase(relative))
        if listed is None:
            return Classified("unlisted", f"{root.label}:{relative}")
        indexed, sha = listed
        try:
            same = sha256_file(path) == sha
        except OSError:  # gone, or no longer readable: nothing says it is what was snapshotted
            same = False
        return Classified("root" if same else "changed", f"{root.label}:{indexed}")


def classify_file(
    path: Path,
    *,
    roots: Sequence[TracedRoot],
    generated: Mapping[str, Path],
    stdlib: Sequence[Path],
    site: Sequence[Path],
) -> Classified:
    """The class of one file (module docstring). Only a file listed in a root's index is read."""
    classifier = _Classifier(roots=roots, generated=generated, stdlib=stdlib, site=site)
    return classifier.classify(_resolved(path))


def _file_of(module: object) -> str | None:
    """A module's ``__file__`` from its own namespace: never through a module ``__getattr__``,
    which may import."""
    try:
        namespace = object.__getattribute__(module, "__dict__")
        file = namespace.get("__file__")
    except Exception:
        return None
    return file if isinstance(file, str) else None


def classify_modules(
    modules: Mapping[str, ModuleType],
    *,
    roots: Sequence[TracedRoot],
    generated: Mapping[str, Path],
    stdlib: Sequence[Path],
    site: Sequence[Path],
) -> TraceResult:
    """Every module with a ``__file__`` (``__main__`` too), one file counted once. Imports
    nothing, and calls no registry resolver: ``generated`` is already resolved."""
    classifier = _Classifier(roots=roots, generated=generated, stdlib=stdlib, site=site)
    counts = dict.fromkeys(COUNT_KEYS, 0)
    generated_by: dict[str, int] = {}
    listed: dict[str, list[str]] = {cls: [] for cls in GAP_CLASSES}
    seen: set[str] = set()
    for module in list(modules.values()):
        file = _file_of(module)
        if file is None:
            continue
        path = _resolved(file)
        key = _key(path)
        if key in seen:
            continue
        seen.add(key)
        found = classifier.classify(path, key)
        counts[found.cls] += 1
        if found.generated_by is not None:
            generated_by[found.generated_by] = generated_by.get(found.generated_by, 0) + 1
        if found.cls in listed:
            listed[found.cls].append(found.shown)
    first = rules.LISTED_PATHS
    return TraceResult(
        counts=counts,
        generated_by=dict(sorted(generated_by.items())),
        outside=sorted(listed["outside"])[:first],
        unlisted=sorted(listed["unlisted"])[:first],
        changed=sorted(listed["changed"])[:first],
        complete=not any(counts[cls] for cls in GAP_CLASSES),
    )


def _sysconfig_dir(name: str) -> Path | None:
    try:
        found = sysconfig.get_path(name)
    except Exception:  # a scheme without that path, or an interpreter whose config is broken
        return None
    return Path(found) if found else None


def _once(paths: Sequence[Path | None]) -> list[Path]:
    return list(dict.fromkeys(path for path in paths if path is not None))


def stdlib_dirs() -> list[Path]:
    """sysconfig's ``stdlib`` and ``platstdlib``; on Windows also the ``DLLs`` directories of
    ``sys.base_prefix`` and ``sys.prefix`` that exist, where the standard library's extension
    modules are (Review Focus 2)."""
    found = [_sysconfig_dir("stdlib"), _sysconfig_dir("platstdlib")]
    if _WINDOWS:
        dlls = (Path(sys.base_prefix) / "DLLs", Path(sys.prefix) / "DLLs")
        found += [directory for directory in dlls if directory.is_dir()]
    return _once(found)


def site_dirs() -> list[Path]:
    """sysconfig's ``purelib`` and ``platlib`` and the ``site`` module's directories, each asked
    on its own (an old virtualenv's ``site`` lacks ``getsitepackages``)."""
    found = [_sysconfig_dir("purelib"), _sysconfig_dir("platlib")]
    try:
        found += [Path(directory) for directory in site.getsitepackages()]
    except Exception:
        pass
    try:
        found.append(Path(site.getusersitepackages()))
    except Exception:
        pass
    return _once(found)


# -- the artifact ----------------------------------------------------------------------------------


def _traced_roots(data_root: Path, record: TrainRecord, attempt: int) -> list[TracedRoot]:
    """The roots of the attempt's ``env.<n>.json`` that have a snapshot, with its index. A root
    without a snapshot holds nothing to compare: its modules fall through to the other classes."""
    entry = next((a for a in record.attempts if a.n == attempt), None)
    if entry is None:
        raise ValidationFailed(f"not_found: attempt {attempt} is not in train.yaml")
    if entry.env is None:
        return []
    code = read_sections(run_dir(data_root, record.run_id) / entry.env).code
    if code is None:
        return []
    return [
        TracedRoot(
            label=root.label,
            path=resolve_stored_path(root.path, data_root),
            snapshot=root.snapshot,
            index={e.path: e.sha256 for e in load_index(data_root, root.snapshot)},
        )
        for root in code.roots
        if root.snapshot is not None
    ]


def _commit(
    data_root: Path, record: TrainRecord, attempt: int, roots: list[TracedRoot], doc: TraceDoc
) -> tuple[str, int]:
    """Write ``trace.json`` under the attempt's next free ``<run>-a<n>-t<seq>``: from the number
    of traces the attempt has, one more on every ``exists:``. Returns the id and the seq."""
    taken = sum(
        1
        for ref in current(record.evidence)
        if ref.kind == KIND and ref.role == CODE_TRACE_ROLE and ref.attempt == attempt
    )
    inputs = [
        InputRef(name=f"snapshot.{r.label}", path=str(snapshot_json_path(data_root, r.snapshot)))
        for r in roots
    ]
    for seq in range(taken + 1, taken + 1 + MAX_SEQ_TRIES):
        spec = ArtifactSpec(
            kind=KIND,
            id=f"{record.run_id}-a{attempt}-t{seq}",
            dataset=record.dataset,
            plan_id=record.plan_id,
            params={"run": record.run_id, "attempt": str(attempt)},
            inputs=inputs,
        )
        try:
            writer = ArtifactWriter.create(spec, data_root=data_root)
        except ValidationFailed as e:
            if str(e).startswith("exists:"):
                continue
            raise
        with writer:
            writer.write_json(TRACE_FILE, doc.model_dump(mode="json"))
            writer.commit()
        return spec.id, seq
    raise ValidationFailed(
        f"exists: {MAX_SEQ_TRIES} trace ids of attempt {attempt} from t{taken + 1} are taken"
    )


def write_trace(
    run_id: str, data_root: Path, attempt: int, *, modules: Mapping[str, ModuleType]
) -> str:
    """Classify ``modules`` against the attempt's snapshotted roots, write the ``code_trace``
    artifact and bind it as ``code_trace.a<n>.<seq>`` (an ``evidence`` event with it). Returns the
    artifact id. Raises whatever stops it; ``_at_exit`` turns that into one stderr line."""
    data_root = Path(data_root)
    record = load_record(data_root, run_id)
    roots = _traced_roots(data_root, record, attempt)
    result = classify_modules(
        modules,
        roots=roots,
        generated=generated_locations(),
        stdlib=stdlib_dirs(),
        site=site_dirs(),
    )
    doc = TraceDoc(
        run_id=run_id,
        attempt=attempt,
        executable=sys.executable,
        python=sys.version,
        roots=[{"label": r.label, "snapshot": r.snapshot} for r in roots],
        counts=result.counts,
        generated_by=result.generated_by,
        outside=result.outside,
        unlisted=result.unlisted,
        changed=result.changed,
        complete=result.complete,
        taken_at=stamp(),
        vcp_version=build_string(),
    )
    trace_id, seq = _commit(data_root, record, attempt, roots, doc)
    ref = artifact_ref(
        data_root,
        KIND,
        trace_id,
        name=f"code_trace.a{attempt}.{seq}",
        role=CODE_TRACE_ROLE,
        attempt=attempt,
        binding="session",
    )
    bind_ref(data_root, load_record(data_root, run_id), ref, attempt)
    return trace_id


# -- arming ----------------------------------------------------------------------------------------


def _skipped(error: BaseException) -> None:
    try:
        sys.stderr.write(f"[vcp] code trace skipped: {type(error).__name__}: {error}\n")
    except Exception:  # stderr itself is gone at exit: there is nobody left to tell
        pass


def _wanted() -> bool:
    """Not with ``VCP_CODE_TRACE=0``, not in a ``multiprocessing`` worker (a DataLoader's), not on
    a rank other than 0 (torchrun's other processes would rewrite ``train.yaml`` at once)."""
    if os.environ.get("VCP_CODE_TRACE") == "0":
        return False
    if multiprocessing.parent_process() is not None:
        return False
    return os.environ.get("RANK", "") in ("", "0")


def _warm() -> None:
    """Ask, now, what the exit asks again, so whatever the asking imports is imported now: on
    POSIX the first ``sysconfig`` path loads ``_sysconfigdata_*``, and ``getpass.getuser`` may
    load ``pwd``. The answers are not kept; the exit's own are the ones recorded. The build string
    is the exception: it is cached for the process (``build_info``), so asking it now means the
    exit runs no git, whose subprocesses have no timeout."""
    stdlib_dirs()
    site_dirs()
    generated_locations()
    build_string()


def arm(session: Session) -> None:
    """Register the exit-time trace of this process, once (spec §5.2). Never raises."""
    global _ARMED
    try:
        if _ARMED or not _wanted():
            return
        _warm()
        atexit.register(_at_exit, session.run_id, session.data_root, session.attempt, os.getpid())
        _ARMED = True
    except Exception as e:
        _skipped(e)


def _at_exit(run_id: str, data_root: Path, attempt: int, pid: int) -> None:
    """The trace, in the process that armed it only (a forked child inherits the handler). Every
    failure is one stderr line; the process's exit code is its own."""
    if os.getpid() != pid:
        return
    try:
        write_trace(run_id, data_root, attempt, modules=dict(sys.modules))
    except BaseException as e:
        _skipped(e)
