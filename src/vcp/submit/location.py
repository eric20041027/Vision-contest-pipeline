"""Where a test dataset's submissions ledger lives (spec 2026-09-28 §3.1), and the transaction a
write command runs in (§4.2). The submit commands, backup and provenance all ask this module;
none of them builds the ledger's path itself.

``configs`` (the default) keeps ``configs/datasets/<test>/submissions.jsonl`` in git, one per
checkout. ``shared`` keeps ``<data_root>/submit/<test>/submissions.jsonl`` beside the submission
directories, one for every checkout that uses the data root. Either way a write command takes
the ledger's lock and only then reads the ledger."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.lock import Holder, file_lock, lock_path
from vcp.core.paths import DatasetPaths
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.schema import PlatformProfile

LEDGER_NAME = "submissions.jsonl"
LOCK_PREFIX = "submissions"


def shared_ledger(paths: DatasetPaths) -> Path:
    """``<data_root>/submit/<test>/submissions.jsonl``."""
    return paths.submit_dir / LEDGER_NAME


def shared_ledgers(data_root: Path) -> list[Path]:
    """Every shared ledger under a data root: what provenance checkpoints besides the configs
    root's ledgers (spec §3.1)."""
    root = Path(data_root) / "submit"
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob(f"*/{LEDGER_NAME}") if p.is_file())


def _has_rows(path: Path) -> bool:
    return path.is_file() and any(line.strip() for line in path.read_bytes().split(b"\n"))


def locate(paths: DatasetPaths, profile: PlatformProfile) -> Path:
    """The ledger ``profile`` names. A ``shared`` ledger that does not exist yet while the
    configs ledger still has rows is ``not_adopted:``: starting an empty one would hide that
    history from the quota and the guards (spec §4.1). When neither has rows, start fresh."""
    if profile.ledger == "configs":
        return paths.submissions_log
    shared = shared_ledger(paths)
    if not shared.exists() and _has_rows(paths.submissions_log):
        raise ValidationFailed(
            f"not_adopted: submit.yaml says ledger: shared, but {shared} does not exist while "
            f"{paths.submissions_log} has rows; run `vcp submit ledger adopt --dataset "
            f"{paths.name}` first",
            fields={"ledger": "shared"},
        )
    return shared


def ledger_lock_file(paths: DatasetPaths, ledger: Path) -> Path:
    """``<data_root>/locks/submissions-<hash16>.lock`` for this ledger (spec §3.3)."""
    return lock_path(paths.data_root, LOCK_PREFIX, ledger)


@contextmanager
def ledger_lock(paths: DatasetPaths, ledger: Path, *, command: str) -> Iterator[Holder]:
    """The ledger's lock: ABORT ``locked:`` after the wait. ``adopt`` takes it without
    reading the ledger."""
    with file_lock(ledger_lock_file(paths, ledger), command=command, label=str(ledger)) as me:
        yield me


@contextmanager
def transaction(
    paths: DatasetPaths, profile: PlatformProfile, *, command: str
) -> Iterator[SubmissionLedger]:
    """A write command's whole body (spec §4.2): locate the ledger, take its lock, and only then
    read it, so every check sees what another process wrote a moment ago and nothing appended
    inside interleaves with another writer's rows."""
    ledger = locate(paths, profile)
    with ledger_lock(paths, ledger, command=command):
        yield SubmissionLedger(ledger)


def read_only(paths: DatasetPaths, profile: PlatformProfile) -> SubmissionLedger:
    """What ``status``, ``report`` and backup read: no lock, and a last line still being written
    is not a row yet (spec §4.2)."""
    return SubmissionLedger(locate(paths, profile), complete_only=True)
