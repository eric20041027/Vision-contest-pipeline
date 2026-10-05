"""``vcp backup status`` (read-only): each manifest's last push and verify, the tiers never
pushed, whether it is complete, and whether an rclone config file is still on this machine.

Completeness is recomputed from the manifest every time (spec 2026-10-04 §4.2): a verify row
written before 0.13.0 never asked, so a passing one cannot vouch for a manifest that lists fewer
files than its runs registered. Without the run records on this machine the rows' own
``incomplete`` decides; with none, a manifest written before 0.10.0 stays ``unchecked``.

A push or verify row about a destination that does not cover the manifest's local copies
vouches only if it handled them (``local_copies``, written since 0.13.0, spec §5.2)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.backup.completeness import checkable, manifest_gaps
from vcp.backup.dest import covers, rclone_conf_state
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest
from vcp.backup.push import TIERS
from vcp.backup.schema import BackupRow, Manifest
from vcp.core.build import parse_build_string
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.proc import Runner

# The first release whose manifests list every checkpoint path (VCP-035).
COMPLETE_SINCE = (0, 10, 0)


@dataclass(frozen=True)
class ManifestStatus:
    manifest_id: str
    conclusion: str
    files: int
    created: str
    last_push: BackupRow | None
    last_verify: BackupRow | None
    pushed_tiers: list[int]
    verified: bool
    local_ok: bool
    incomplete: int | None = 0  # files its runs registered that it does not list; None: unknown

    @property
    def unpushed_tiers(self) -> list[int]:
        return [t for t in TIERS if t not in self.pushed_tiers]

    @property
    def completeness(self) -> str:
        if self.incomplete is None:
            return "unchecked"
        return "incomplete" if self.incomplete else "complete"


@dataclass(frozen=True)
class StatusView:
    dataset: str
    manifests: list[ManifestStatus]
    rclone_conf: str

    @property
    def unverified(self) -> list[str]:
        return [m.manifest_id for m in self.manifests if not m.verified]

    @property
    def incomplete(self) -> list[str]:
        return [m.manifest_id for m in self.manifests if m.incomplete]

    @property
    def unchecked(self) -> list[str]:
        return [m.manifest_id for m in self.manifests if m.incomplete is None]


def local_ok(row: BackupRow) -> bool:
    """The two layers that need no destination: local consistency -- completeness included
    (spec 2026-10-04 §4.3) -- and timestamps."""
    return not row.drift and not row.bad_stamps and not row.incomplete


def passed(row: BackupRow) -> bool:
    """A verify row with nothing wrong in any layer -- copies included, over every tier. A row
    that checked no destination, or only the lower tiers, says nothing about the copies: calling
    that "verified" is exactly the claim a machine about to be wiped must not be given."""
    copies = row.copies
    return (
        copies is not None
        and row.tier == 3
        and copies.get("missing", 0) == 0
        and copies.get("mismatch", 0) == 0
        and local_ok(row)
    )


def _load(paths: DatasetPaths, manifest_id: str) -> Manifest | None:
    try:
        return load_manifest(paths, manifest_id)
    except ValidationFailed:
        return None  # gone or unreadable: nothing about it can be verified


def _older_than_complete(build: str) -> bool:
    try:
        version = parse_build_string(build).version
    except ValueError:
        return True
    return tuple(int(part) for part in version.split(".")) < COMPLETE_SINCE


def gaps_of(
    manifest: Manifest | None, paths: DatasetPaths, verifies: list[BackupRow]
) -> int | None:
    """How many files the manifest's runs registered that it does not list -- or None when this
    machine cannot tell (spec 2026-10-04 §4.2)."""
    if manifest is None:
        return None
    if checkable(manifest, paths):
        return len(manifest_gaps(manifest, paths))
    recorded = max((v.incomplete or 0 for v in verifies), default=0)
    if recorded:
        return recorded
    return None if _older_than_complete(manifest.vcp_version) else 0


def _vouches(row: BackupRow, manifest: Manifest | None) -> bool:
    """Whether a push or verify row about destination D speaks for the manifest's remote_copies
    there: D covers every one, or the row handled them (``local_copies``, written since 0.13.0).
    An older row never looked for a local copy at D (spec 2026-10-04 §5.2)."""
    if manifest is None or row.dest is None or row.local_copies is not None:
        return True
    return all(covers(row.dest, f.remote) for f in manifest.files if f.remote is not None)


def _pushed_tier(row: BackupRow, manifest: Manifest | None) -> int:
    """The tiers a push row covers: a tier-3 push that left the local copies out covers two."""
    tier = int(row.tier or 0)
    return 2 if tier == 3 and not _vouches(row, manifest) else tier


def status(
    dataset: str,
    *,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> StatusView:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    ledger = BackupLedger(paths.backup_log)
    out: list[ManifestStatus] = []
    for row in ledger.of("manifest"):
        mid = str(row.manifest_id)
        pushes = ledger.of("push", mid)
        verifies = ledger.of("verify", mid)
        manifest = _load(paths, mid)
        gaps = gaps_of(manifest, paths, verifies)
        complete = gaps == 0
        covered = max((_pushed_tier(p, manifest) for p in pushes if p.failed == []), default=0)
        out.append(
            ManifestStatus(
                manifest_id=mid,
                conclusion=str(row.conclusion),
                files=row.files or 0,
                created=row.ts,
                last_push=pushes[-1] if pushes else None,
                last_verify=verifies[-1] if verifies else None,
                pushed_tiers=[t for t in TIERS if t <= covered],
                verified=complete and any(passed(v) and _vouches(v, manifest) for v in verifies),
                local_ok=complete and any(local_ok(v) for v in verifies),
                incomplete=gaps,
            )
        )
    return StatusView(dataset, out, rclone_conf_state(runner))
