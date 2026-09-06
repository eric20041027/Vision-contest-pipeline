"""``vcp backup status`` (read-only): each manifest's last push and verify, the tiers never
pushed, and whether an rclone config file is still on this machine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.backup.dest import rclone_conf_state
from vcp.backup.ledger import BackupLedger
from vcp.backup.push import TIERS
from vcp.backup.schema import BackupRow
from vcp.core.paths import DatasetPaths
from vcp.core.proc import Runner


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

    @property
    def unpushed_tiers(self) -> list[int]:
        return [t for t in TIERS if t not in self.pushed_tiers]


@dataclass(frozen=True)
class StatusView:
    dataset: str
    manifests: list[ManifestStatus]
    rclone_conf: str

    @property
    def unverified(self) -> list[str]:
        return [m.manifest_id for m in self.manifests if not m.verified]


def local_ok(row: BackupRow) -> bool:
    """The two layers that need no destination: local consistency and timestamps."""
    return not row.drift and not row.bad_stamps


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
        covered = max((int(p.tier or 0) for p in pushes if p.failed == []), default=0)
        out.append(
            ManifestStatus(
                manifest_id=mid,
                conclusion=str(row.conclusion),
                files=row.files or 0,
                created=row.ts,
                last_push=pushes[-1] if pushes else None,
                last_verify=verifies[-1] if verifies else None,
                pushed_tiers=[t for t in TIERS if t <= covered],
                verified=any(passed(v) for v in verifies),
                local_ok=any(local_ok(v) for v in verifies),
            )
        )
    return StatusView(dataset, out, rclone_conf_state(runner))
