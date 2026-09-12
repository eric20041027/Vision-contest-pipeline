"""What is under ``artifacts/`` (spec 6 ``status``) and the only code that removes any of it
(spec 8 ``clean``): partial directories past a grace period, and temp files. One rule classifies
a directory: ``manifest.json`` → complete; else ``spec.json`` → partial; else foreign, never
touched."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vcp.artifact.ledger import read_supersession
from vcp.artifact.lineage import list_manifests, successors_of
from vcp.artifact.schema import FailureRecord, SpecRecord
from vcp.artifact.store import FAILURE, MANIFEST, SPEC
from vcp.core.atomic import is_tmp_name
from vcp.core.errors import ValidationFailed
from vcp.core.paths import artifacts_root, validate_name
from vcp.core.time import parse_stamp, utc_now

_AGE = re.compile(r"^(?P<n>\d+)(?P<unit>[mhd])?$")
_UNITS = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}


def parse_age(text: str) -> timedelta:
    """``--older-than``: ``<N>m`` / ``<N>h`` / ``<N>d`` (``24h``), or ``0``."""
    m = _AGE.match(text.strip())
    if m is None or (m.group("unit") is None and m.group("n") != "0"):
        raise ValidationFailed(f"--older-than expects <N>m|h|d or 0, got {text!r}")
    if m.group("unit") is None:
        return timedelta(0)
    return int(m.group("n")) * _UNITS[m.group("unit")]


@dataclass(frozen=True)
class PartialInfo:
    id: str
    opened_at: str | None  # None when spec.json does not parse
    failure: str | None  # exception class from failure.json, when the job raised


@dataclass(frozen=True)
class KindStatus:
    kind: str
    complete: list[str]
    partial: list[PartialInfo]
    unlinked: list[str]
    forks: int
    foreign: list[str]


def _kind_dirs(data_root: Path, kind: str | None) -> list[Path]:
    root = artifacts_root(data_root)
    if kind is not None:
        validate_name(kind)
        d = root / kind
        return [d] if d.is_dir() else []
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _partial_info(d: Path) -> PartialInfo:
    opened_at: str | None = None
    try:
        opened_at = SpecRecord.model_validate_json((d / SPEC).read_text(encoding="utf-8")).opened_at
    except (OSError, ValueError):
        pass
    failure: str | None = None
    if (d / FAILURE).is_file():
        try:
            failure = FailureRecord.model_validate_json(
                (d / FAILURE).read_text(encoding="utf-8")
            ).exception
        except (OSError, ValueError):
            failure = "unreadable"
    return PartialInfo(d.name, opened_at, failure)


def _partial_dirs(kind_dir: Path) -> list[Path]:
    return [
        d
        for d in sorted(p for p in kind_dir.iterdir() if p.is_dir())
        if not (d / MANIFEST).is_file() and (d / SPEC).is_file()
    ]


def scan(data_root: Path, kind: str | None = None) -> list[KindStatus]:
    out: list[KindStatus] = []
    for kind_dir in _kind_dirs(data_root, kind):
        manifests = list_manifests(data_root, kind_dir.name)
        partial = [_partial_info(d) for d in _partial_dirs(kind_dir)]
        foreign = [
            d.name
            for d in sorted(p for p in kind_dir.iterdir() if p.is_dir())
            if not (d / MANIFEST).is_file() and not (d / SPEC).is_file()
        ]
        linked = {r.id for r in read_supersession(data_root, kind_dir.name)}
        unlinked = [
            i for i, m in manifests.items() if m.spec.supersedes is not None and i not in linked
        ]
        forks = sum(1 for ids in successors_of(manifests).values() if len(ids) > 1)
        out.append(KindStatus(kind_dir.name, list(manifests), partial, unlinked, forks, foreign))
    return out


@dataclass(frozen=True)
class CleanResult:
    candidates: list[
        str
    ]  # "<kind>/<id>" for a partial directory, "<kind>/…/.x.<nonce>.tmp" for a temp
    removed: list[str]


def _opened_before(d: Path, threshold: datetime) -> bool:
    opened_at = _partial_info(d).opened_at
    if opened_at is None:
        return False  # unreadable: never a candidate
    try:
        return parse_stamp(opened_at) <= threshold
    except ValueError:
        return False


def _modified_before(p: Path, threshold: datetime) -> bool:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=UTC) <= threshold


def clean(
    data_root: Path,
    *,
    kind: str | None = None,
    older_than: timedelta = timedelta(hours=24),
    apply: bool = False,
) -> CleanResult:
    """Candidates: partial directories whose ``opened_at`` is older than ``older_than`` (a
    ``spec.json`` that does not parse is never one: nothing unreadable is deleted), and temp files
    under the kind at least that old by mtime. Committed artifacts, ledgers and foreign
    directories are never touched; without ``apply`` the candidates are only listed."""
    threshold = utc_now() - older_than
    root = artifacts_root(data_root)
    dirs: list[Path] = []
    tmps: list[Path] = []
    for kind_dir in _kind_dirs(data_root, kind):
        dirs += [d for d in _partial_dirs(kind_dir) if _opened_before(d, threshold)]
        tmps += [
            p
            for p in sorted(kind_dir.rglob("*"))
            if p.is_file()
            and is_tmp_name(p.name)
            and not any(p.is_relative_to(d) for d in dirs)
            and _modified_before(p, threshold)
        ]
    candidates = [p.relative_to(root).as_posix() for p in dirs + tmps]
    removed: list[str] = []
    if apply:
        for d in dirs:
            if (d / MANIFEST).exists():  # re-checked at the moment of removal
                continue
            shutil.rmtree(d)
            removed.append(d.relative_to(root).as_posix())
        for p in tmps:
            p.unlink(missing_ok=True)
            removed.append(p.relative_to(root).as_posix())
    return CleanResult(candidates, removed)
