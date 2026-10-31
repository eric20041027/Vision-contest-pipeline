"""Free disk space for ``vcp train run`` (spec 2026-10-30 §5.1): measured before the first write.

A training run writes to three places -- vcp's own run directory, the child's working directory,
and wherever the checkpoint globs point -- and a disk that fills halfway through ``torch.save``
leaves a truncated ``.tmp`` and a run that cannot be trusted. So the run is measured first, once
per volume, and refused below ``--min-free-gib``.

``free`` is ``shutil.disk_usage().free``: on Windows the free space of the whole volume, not the
user's quota (controller ruling C5); the quota's symptoms are read from the console afterwards.
The probe is the module attribute ``usage`` and every caller goes through it, so a test replaces
the disk without filling one.

After a failed attempt the same volumes are measured again and the tail of the console is read
(§5.3): a disk-full word there is ``disk_full:``, otherwise less free space than the bar is
``low_disk:`` ("may have filled" -- the space is a fact, the cause is not). And when vcp itself
cannot write its own record inside ``train run``, ``own_writes`` turns that into the same
``disk_full:`` as an ABORT.
"""

from __future__ import annotations

import errno
import math
import os
import shutil
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.paths import store_path
from vcp.core.time import stamp
from vcp.train.schema import DiskCheck, DiskRole, DiskVolume

GIB = 1024**3
DEFAULT_MIN_FREE_GIB = 10.0
UNKNOWN = "unknown"
_WILDCARDS = "*?["

# §5.3: how much of the console's end is read for a disk-full word, and which words. The words
# are matched without regard to case, in this order (the first one in the list wins, not the
# earliest in the text), and reported as written here.
DISK_FULL_TAIL = 64 * 1024
DISK_FULL_MARKERS: tuple[str, ...] = (
    "No space left on device",
    "[Errno 28]",
    "[WinError 112]",
    "[WinError 39]",
    "There is not enough space on the disk",
    "Disk quota exceeded",
)
# What an OSError of vcp's own looks like when the disk (or the user's quota) is full.
DISK_FULL_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})
DISK_FULL_WINERRORS = frozenset({112, 39})


def disk_free_total(path: Path) -> tuple[int, int]:
    """``(free, total)`` bytes of the volume holding ``path``."""
    reading = shutil.disk_usage(path)
    return reading.free, reading.total


# The probe. Always called as ``usage(...)`` from this module, never imported by name elsewhere:
# the tests' autouse fixture replaces it, and an alias taken at import time would miss that.
usage: Callable[[Path], tuple[int, int]] = disk_free_total


def device(path: Path) -> int:
    """The id of the volume holding ``path`` (``st_dev``): two paths with the same id share a
    reading."""
    return os.stat(path).st_dev


def glob_prefix(pattern: str) -> str:
    """The fixed text of a glob before its first ``*``, ``?`` or ``[`` -- the directory (or
    stem) the child writes under."""
    cuts = [i for i, ch in enumerate(pattern) if ch in _WILDCARDS]
    return pattern[: min(cuts)] if cuts else pattern


def nearest_dir(path: Path) -> Path:
    """``path`` if it is a directory, else its closest existing ancestor (a checkpoint directory
    the child has not created yet is measured where it will be created)."""
    here = Path(path)
    while not os.path.isdir(here) and here.parent != here:
        here = here.parent
    return here


def targets(run_root: Path, cwd: Path, patterns: Iterable[str]) -> list[tuple[DiskRole, Path]]:
    """What a run writes: its run directory, the working directory, and the fixed prefix of every
    checkpoint glob. Globs are relative to ``cwd`` as ``expand`` reads them, so an absolute
    pattern points wherever it says."""
    out: list[tuple[DiskRole, Path]] = [("run", run_root), ("cwd", cwd)]
    out.extend(("checkpoints", cwd / glob_prefix(pattern)) for pattern in patterns)
    return out


def measure(
    targets: Iterable[tuple[DiskRole, Path]], *, min_free_gib: float, data_root: Path
) -> DiskCheck:
    """One reading per volume, volumes in the order first met. A volume whose reading fails is
    kept with both sizes ``None`` (the caller warns; it does not abort on a broken probe)."""
    found: dict[object, tuple[Path, list[DiskRole]]] = {}
    for role, path in targets:
        here = nearest_dir(path)
        try:
            volume: object = device(here)
        except OSError:
            volume = ("unreadable", str(here))  # a path with no readable device stands alone
        roles = found.setdefault(volume, (here, []))[1]
        if role not in roles:
            roles.append(role)
    volumes: list[DiskVolume] = []
    for here, roles in found.values():
        free: int | None
        total: int | None
        try:
            free, total = usage(here)
        except OSError:
            free = total = None
        volumes.append(
            DiskVolume(
                path=store_path(here, data_root), roles=roles, free_bytes=free, total_bytes=total
            )
        )
    return DiskCheck(min_free_gib=min_free_gib, checked_at=stamp(), volumes=volumes)


def lowest(check: DiskCheck) -> DiskVolume | None:
    """The readable volume with the least free space (the first of equals); None when none could
    be read."""
    best: DiskVolume | None = None
    for volume in check.volumes:
        if volume.free_bytes is None:
            continue
        if best is None or best.free_bytes is None or volume.free_bytes < best.free_bytes:
            best = volume
    return best


def _gib(size: int) -> float:
    return round(size / GIB, 1)


def free_gib(check: DiskCheck) -> float | str:
    """The VERDICT's ``free_gib=``: the least free space in GiB to one decimal, or ``unknown``
    when no volume could be read."""
    low = lowest(check)
    return UNKNOWN if low is None or low.free_bytes is None else _gib(low.free_bytes)


def unavailable(check: DiskCheck) -> bool:
    """Whether any volume could not be read -- the ``disk_check=unavailable`` warning."""
    return any(volume.free_bytes is None for volume in check.volumes)


def check_min_free_gib(value: float) -> None:
    """FAIL ``invalid:`` unless the bar is a finite number >= 0."""
    if not math.isfinite(value) or value < 0:
        raise ValidationFailed(
            f"invalid: --min-free-gib must be a finite number >= 0, got {value!r}"
        )


def assert_room(check: DiskCheck) -> None:
    """ABORT ``low_disk:`` when the least free space is below the bar. A bar of 0 only records,
    and a volume that could not be read never aborts."""
    low = lowest(check)
    if check.min_free_gib == 0 or low is None or low.free_bytes is None:
        return
    if low.free_bytes >= check.min_free_gib * GIB:
        return
    gib = _gib(low.free_bytes)
    raise VcpError(
        f"low_disk: {gib:.1f} GiB free on {low.path}, below --min-free-gib "
        f"{check.min_free_gib:g}; free space or move the run to another disk, "
        "or lower the bar with --min-free-gib",
        fields={"free_gib": gib, "min_free_gib": check.min_free_gib, "disk_path": low.path},
    )


@dataclass(frozen=True)
class DiskFailure:
    """What a failed attempt says about the disk: ``disk_full`` (the console names it) or
    ``low_disk`` (less room than the bar now). ``evidence`` is ``console`` for the first and
    None for the second; ``free_bytes`` is the least free space measured afterwards."""

    key: Literal["disk_full", "low_disk"]
    reason: str
    marker: str | None
    free_bytes: int | None
    evidence: Literal["console"] | None


def console_marker(console: Path) -> str | None:
    """The first of ``DISK_FULL_MARKERS`` found in the last ``DISK_FULL_TAIL`` bytes of the
    console, as the list writes it; None when none is there or the file is not. A tail that
    starts inside a multi-byte character decodes with a replacement character, not an error."""
    try:
        with console.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - DISK_FULL_TAIL))
            tail = f.read()
    except OSError:
        return None  # nothing to read is no evidence; the diagnosis is a best effort
    text = tail.decode("utf-8", errors="replace").casefold()
    return next((m for m in DISK_FULL_MARKERS if m.casefold() in text), None)


def diagnose(exit_code: int, console: Path, after: DiskCheck) -> DiskFailure | None:
    """Name the disk as the cause of a failed attempt, or say nothing. The console comes first;
    then the free space measured afterwards against the bar (``after.min_free_gib``; 0 turns
    this step off). The caller asks only about attempts whose status is ``failed``."""
    low = lowest(after)
    free = low.free_bytes if low is not None else None
    marker = console_marker(console)
    if marker is not None:
        return DiskFailure(
            key="disk_full",
            reason=f'disk_full: the command exited {exit_code} and its console says "{marker}"',
            marker=marker,
            free_bytes=free,
            evidence="console",
        )
    bar = after.min_free_gib
    if bar > 0 and free is not None and free < bar * GIB:
        return DiskFailure(
            key="low_disk",
            reason=(
                f"low_disk: the command exited {exit_code} with {_gib(free):.1f} GiB free "
                f"(below --min-free-gib {bar:g}); the disk may have filled"
            ),
            marker=None,
            free_bytes=free,
            evidence=None,
        )
    return None


def is_disk_full(error: OSError) -> bool:
    """Whether ``error`` says the disk or the quota is full: ``errno`` ENOSPC or EDQUOT, or a
    Windows error code 112 or 39 (``winerror`` exists on Windows only)."""
    return (
        error.errno in DISK_FULL_ERRNOS or getattr(error, "winerror", None) in DISK_FULL_WINERRORS
    )


@contextmanager
def own_writes() -> Iterator[None]:
    """Around the writes vcp makes to its own record in ``train run``: a full disk becomes an
    ABORT ``disk_full:`` with ``disk_evidence=vcp`` (clear space, then ``--resume``); any other
    ``OSError`` passes as it came. Only ``train run`` uses it -- every other command keeps the
    OSError it always raised."""
    try:
        yield
    except OSError as e:
        if not is_disk_full(e):
            raise
        raise VcpError(
            f"disk_full: vcp could not write its own record ({e}); free space, then --resume",
            fields={"disk_evidence": "vcp"},
        ) from e
