"""Who names which path (0.17.0 spec 5.3): every file and directory the canonical records
reference, read from the records themselves and never from the provenance index.

The walk is backup's: ``vcp.backup.evidence.Collector`` with a ``ReferenceRecorder`` in place of
the hashing one, so a reference is the path, the role, the conclusion it serves and whatever sha
and size a record already vouches for -- nothing is hashed. Each walk unit (a run, a judgement,
a submission, a dataset's own files, an image root, a backup manifest, an artifact) is a
transaction: a unit that cannot be read is taken back alone, named in ``skipped``, and the files
it would have referenced may then be reported as unreferenced -- which is why ``skipped`` makes
a scan WARN.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from vcp.artifact import store
from vcp.backup.dest import dest_kind
from vcp.backup.evidence import Collector
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest, local_path
from vcp.backup.schema import RemoteCopy
from vcp.core.errors import VcpError
from vcp.core.paths import DatasetPaths, artifacts_root, resolve_stored_path, runs_root, store_path
from vcp.data.dataset import Dataset
from vcp.train.records import events_path, has_record
from vcp.train.records import load_record as load_train_record
from vcp.train.schema import TrainRecord
from vcp.train.status import checkpoint_dirs

COUNTED = ("runs", "datasets", "backup_manifests", "artifacts")


def path_key(path: Path) -> str:
    """How two spellings of one path are told to be one: resolved, in the platform's case."""
    return os.path.normcase(str(Path(path).resolve()))


@dataclass(frozen=True)
class Reference:
    """One record naming one path. ``referrer`` is ``<conclusion>#<role>``; ``copy`` is the
    destination of a ``train upload`` copy the record vouches for."""

    path: Path
    referrer: str
    is_dir: bool
    sha256: str | None
    size: int | None
    copy: str | None


class ReferenceRecorder:
    """The recording end gc hands the evidence walk: every file is kept as it comes, and a walk
    unit that fails is taken back by truncation. What a unit may raise and be stepped over is
    wider than backup's: unreadable (``OSError``), malformed (``ValueError``, which holds
    ``UnicodeDecodeError`` and pydantic's errors) and vcp's own refusals (``VcpError``); anything
    else is a bug and stops the scan. Paths are resolved here, so a path that cannot be resolved
    fails the unit that named it, not the whole scan."""

    tolerates: tuple[type[Exception], ...] = (VcpError, OSError, ValueError)

    def __init__(self) -> None:
        self.refs: list[Reference] = []
        self._resolved: dict[Path, Path] = {}

    def _resolve(self, path: Path) -> Path:
        key = Path(path)
        resolved = self._resolved.get(key)
        if resolved is None:
            resolved = self._resolved[key] = key.resolve()
        return resolved

    def add(
        self,
        path: Path,
        role: str,
        conclusion: str,
        *,
        sha256: str | None = None,
        size: int | None = None,
        remote: RemoteCopy | None = None,
    ) -> None:
        copy = None if remote is None else remote.dest
        self.refs.append(
            Reference(self._resolve(path), f"{conclusion}#{role}", False, sha256, size, copy)
        )

    def add_dir(self, path: Path, role: str, conclusion: str) -> None:
        """A directory reference covers every file under it."""
        self.refs.append(
            Reference(self._resolve(path), f"{conclusion}#{role}", True, None, None, None)
        )

    def mark(self) -> int:
        return len(self.refs)

    def rollback(self, mark: int) -> None:
        del self.refs[mark:]


@dataclass(frozen=True)
class FileRef:
    """Everything that names one file. ``sha256`` / ``size`` / ``copy`` are the first the
    records gave, in walk order; ``None`` when no record has one."""

    path: Path
    referrers: frozenset[str]
    sha256: str | None
    size: int | None
    copy: str | None


@dataclass(frozen=True)
class References:
    """``files`` and ``dirs`` are keyed by ``path_key``. ``skipped`` holds ``"<label>: <error>"``
    once per label; ``skipped_runs`` the ids of the runs among them. ``running`` are the runs
    whose newest attempt is still ``running``; ``checkpoint_dirs`` the directories
    ``vcp.train.status.checkpoint_dirs`` finds, resolved and sorted by text. ``counts`` is the
    walk units that succeeded (``COUNTED``); ``missing`` the distinct referenced paths, files and
    directories, that name nothing on disk. ``unreadable`` are the paths, as ``store_path``
    writes them, whose examination failed while the checkpoint directories were being found."""

    files: dict[str, FileRef]
    dirs: dict[str, frozenset[str]]
    skipped: list[str]
    skipped_runs: frozenset[str]
    running: list[str]
    checkpoint_dirs: list[Path]
    counts: dict[str, int]
    missing: int
    unreadable: list[str] = field(default_factory=list)


@dataclass
class _Accumulator:
    path: Path
    referrers: set[str] = field(default_factory=set)
    sha256: str | None = None
    size: int | None = None
    copy: str | None = None


def _collapse(
    refs: list[Reference],
) -> tuple[dict[str, FileRef], dict[str, frozenset[str]], int]:
    """Group the references by path key; also how many keys name nothing on disk."""
    files: dict[str, _Accumulator] = {}
    dirs: dict[str, set[str]] = {}
    paths: dict[str, Path] = {}
    for ref in refs:
        key = path_key(ref.path)
        paths.setdefault(key, ref.path)
        if ref.is_dir:
            dirs.setdefault(key, set()).add(ref.referrer)
            continue
        acc = files.setdefault(key, _Accumulator(ref.path))
        acc.referrers.add(ref.referrer)
        acc.sha256 = ref.sha256 if acc.sha256 is None else acc.sha256
        acc.size = ref.size if acc.size is None else acc.size
        acc.copy = ref.copy if acc.copy is None else acc.copy
    missing = sum(1 for path in paths.values() if not os.path.lexists(path))
    return (
        {
            key: FileRef(a.path, frozenset(a.referrers), a.sha256, a.size, a.copy)
            for key, a in files.items()
        },
        {key: frozenset(referrers) for key, referrers in dirs.items()},
        missing,
    )


def _shown(path: Path, data_root: Path) -> str:
    try:
        return store_path(path, data_root)
    except (OSError, ValueError):
        return path.as_posix()


def _parent_name(path: Path) -> str:
    return path.parent.name


@dataclass(frozen=True)
class _RunDirs:
    checkpoint_dirs: list[Path]
    running: list[str]
    unreadable: list[str]


def _read_run_dirs(data_root: Path) -> _RunDirs:
    """Outside any unit: the checkpoint directories and the runs still running, from every
    ``train.yaml``. A record that cannot be read adds nothing here; its run's own unit reports
    it. What stops the directories from being found -- a ``cwd`` that cannot be examined, an
    event log that cannot be read -- is named in ``unreadable``, since the directories it would
    have given are then not looked at."""
    dirs: set[Path] = set()
    running: list[str] = []
    failed: list[Path] = []

    def examine_failed(path: Path, _error: OSError) -> None:
        failed.append(path)

    for card in sorted(runs_root(data_root).glob("*/train.yaml"), key=_parent_name):
        run_id = card.parent.name
        try:
            record = load_train_record(data_root, run_id)
        except (VcpError, OSError, ValueError):
            continue
        if record.attempts and record.attempts[-1].status == "running":
            running.append(run_id)
        try:
            dirs |= {d.resolve() for d in checkpoint_dirs(data_root, record, examine_failed)}
        except (OSError, ValueError):
            failed.append(events_path(data_root, run_id))
    unreadable = sorted({_shown(path, data_root) for path in failed})
    return _RunDirs(sorted(dirs, key=str), running, unreadable)


def _train_record_refs(
    col: Collector, recorder: ReferenceRecorder, record: TrainRecord, conclusion: str
) -> None:
    """What ``train.yaml`` names beyond what ``walk_run`` lists: the export directories, the
    local copies ``train upload`` made, the venvs and the config the run read."""
    root = col.data_root
    for export in record.exports:
        recorder.add_dir(resolve_stored_path(export.dir, root), "export", conclusion)
    for upload in record.uploads:
        if upload.kind == "local":
            where = Path(upload.dest) / record.run_id / upload.name
            col.add(where, "upload_copy", conclusion, sha256=upload.sha256)
    for venv in (record.venv, *(a.venv for a in record.attempts)):
        if venv:
            recorder.add_dir(resolve_stored_path(venv, root), "venv", conclusion)
    if record.config is not None:
        config = resolve_stored_path(record.config.path, root)
        col.add(config, "config", conclusion, sha256=record.config.sha256)


def _walk_run(col: Collector, recorder: ReferenceRecorder, run_id: str) -> None:
    conclusion = f"run:{run_id}"
    col.walk_run(run_id, conclusion)
    if has_record(col.data_root, run_id):
        _train_record_refs(col, recorder, load_train_record(col.data_root, run_id), conclusion)


def _dataset_paths(col: Collector, name: str) -> DatasetPaths:
    return DatasetPaths.resolve(name, data_root=col.data_root, configs_root=col.configs_root)


def _walk_dataset(col: Collector, recorder: ReferenceRecorder, name: str) -> bool:
    """The dataset's runs, judgements, submissions and own files, each a unit of
    ``Collector.walk_all`` that is taken back alone when it fails. Nothing is taken back here:
    what escapes ``walk_all`` comes from the code between its units (listing the runs and
    preregs, looking for ``submit.yaml``), so every reference already recorded belongs to a
    unit that completed. Returns whether the dataset was read whole."""
    before = len(col.skipped)
    try:
        col.walk_all(_dataset_paths(col, name), split=True)
    except recorder.tolerates as e:
        col.skipped.append(f"datasets:{name}: {e}")
        return False
    own_files = f"dataset:{name}: "
    return not any(entry.startswith(own_files) for entry in col.skipped[before:])


def _walk_image_root(col: Collector, recorder: ReferenceRecorder, name: str) -> None:
    """The one thing the dataset card is read for. A card that does not load costs this
    directory and nothing else."""
    card = Dataset.load_card(name, data_root=col.data_root, configs_root=col.configs_root)
    image_root = _dataset_paths(col, name).resolve_image_root(card)
    recorder.add_dir(image_root, "image_root", f"dataset:{name}")


def _walk_manifest(col: Collector, name: str, manifest_id: str) -> None:
    dpaths = _dataset_paths(col, name)
    manifest = load_manifest(dpaths, manifest_id)
    for entry in manifest.files:
        where = local_path(entry, dpaths.data_root, dpaths.configs_root)
        col.add(
            where,
            entry.role,
            f"backup:{name}/{manifest_id}",
            sha256=entry.sha256,
            size=entry.bytes,
            remote=entry.remote,
        )


def _walk_pushes(col: Collector, recorder: ReferenceRecorder, name: str) -> None:
    """A local destination a manifest was pushed to is referenced whole: it is the backup."""
    for row in BackupLedger(_dataset_paths(col, name).backup_log).of("push"):
        if row.dest is not None and row.manifest_id is not None and dest_kind(row.dest) == "local":
            recorder.add_dir(Path(row.dest), "backup_dest", f"backup:{name}/{row.manifest_id}")


def _walk_artifact(col: Collector, kind: str, artifact_id: str) -> None:
    manifest = store.load_manifest(col.data_root, kind, artifact_id)
    for ref in manifest.spec.inputs:
        if ref.path is not None:
            where = resolve_stored_path(ref.path, col.data_root)
            col.add(where, "artifact_input", f"artifact:{kind}/{artifact_id}", sha256=ref.sha256)


def _first_per_label(skipped: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for entry in skipped:
        label = entry.partition(": ")[0]
        if label not in seen:
            seen.add(label)
            out.append(entry)
    return out


def collect_references(data_root: Path, configs_root: Path) -> References:
    """Walk every run, dataset, backup manifest and artifact the records hold -- all of them,
    whichever dataset they belong to, since a part of the references would call a file another
    dataset uses unreferenced. Read-only; hashes nothing."""
    recorder = ReferenceRecorder()
    col = Collector(data_root, configs_root, recorder=recorder)
    counts = dict.fromkeys(COUNTED, 0)

    def unit(label: str, counter: str | None, walk: Callable[..., None], *args: object) -> bool:
        done = False

        def body() -> None:
            nonlocal done
            walk(*args)
            done = True

        col.attempt(label, body)
        if done and counter is not None:
            counts[counter] += 1
        return done

    run_dirs = _read_run_dirs(data_root)
    for card in sorted(runs_root(data_root).glob("*/run.yaml"), key=_parent_name):
        run_id = card.parent.name
        unit(f"run:{run_id}", "runs", _walk_run, col, recorder, run_id)
    for card in sorted((configs_root / "datasets").glob("*/dataset.yaml"), key=_parent_name):
        name = card.parent.name
        walked = _walk_dataset(col, recorder, name)
        rooted = unit(f"image_root:{name}", None, _walk_image_root, col, recorder, name)
        if walked and rooted:
            counts["datasets"] += 1
        for manifest in sorted((card.parent / "backup").glob("*.json"), key=lambda p: p.stem):
            label = f"backup:{name}/{manifest.stem}"
            unit(label, "backup_manifests", _walk_manifest, col, name, manifest.stem)
        unit(f"backup:{name}", None, _walk_pushes, col, recorder, name)
    for manifest in sorted(artifacts_root(data_root).glob("*/*/manifest.json")):
        kind, artifact_id = manifest.parent.parent.name, manifest.parent.name
        unit(f"artifact:{kind}/{artifact_id}", "artifacts", _walk_artifact, col, kind, artifact_id)

    files, dirs, missing = _collapse(recorder.refs)
    skipped = _first_per_label(col.skipped)
    labels = [entry.partition(": ")[0] for entry in skipped]
    skipped_runs = frozenset(
        label.removeprefix("run:") for label in labels if label.startswith("run:")
    )
    return References(
        files=files,
        dirs=dirs,
        skipped=skipped,
        skipped_runs=skipped_runs,
        running=run_dirs.running,
        checkpoint_dirs=run_dirs.checkpoint_dirs,
        counts=counts,
        missing=missing,
        unreadable=run_dirs.unreadable,
    )
