"""``vcp backup pull``: rebuild the manifest's files on this machine from a destination, each
read back against the manifest's sha. A local file that differs is a conflict, never silently
replaced; a fetched copy that does not match is deleted, never kept."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.backup.dest import Destination, open_dest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest, local_path
from vcp.backup.push import check_tier
from vcp.backup.schema import BackupRow
from vcp.core.errors import IntegrityError, PlatformError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.proc import Runner
from vcp.core.time import stamp, utc_now


@dataclass(frozen=True)
class PullResult:
    manifest_id: str
    dest: str
    tier: int
    pulled: int
    skipped: int
    conflicts: list[str]
    missing: list[str]
    mismatch: list[str]


def _backup_name(local: Path) -> Path:
    return local.with_name(f"{local.name}.bak-{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}")


def pull(
    dataset: str,
    manifest_id: str,
    dest: str,
    *,
    tier: int = 3,
    overwrite: bool = False,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> PullResult:
    check_tier(tier)
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    manifest = load_manifest(paths, manifest_id)
    dests: dict[str, Destination] = {dest: open_dest(dest, runner)}
    chosen = [f for f in manifest.files if f.tier <= tier]
    sources: dict[str, tuple[str, str, str]] = {}  # key -> (dest, sub, rel)
    for e in chosen:
        if e.remote is None:
            sources[e.key] = (dest, e.root, e.path)
        else:
            dests.setdefault(e.remote.dest, open_dest(e.remote.dest, runner))
            sources[e.key] = (e.remote.dest, e.remote.run, e.remote.name)
    groups: dict[tuple[str, str], list[str]] = {}
    for d, sub, rel in sources.values():
        groups.setdefault((d, sub), []).append(rel)
    have = {(d, sub): dests[d].hashes(sub, rels) for (d, sub), rels in groups.items()}
    pulled = skipped = 0
    conflicts: list[str] = []
    missing: list[str] = []
    mismatch: list[str] = []
    failure: PlatformError | None = None
    try:
        for e in chosen:
            d, sub, rel = sources[e.key]
            local = local_path(e, paths.data_root, paths.configs_root)
            exists = local.is_file()
            if exists and sha256_file(local) == e.sha256:
                skipped += 1
                continue
            got = have[(d, sub)].get(rel)
            if got is None:
                missing.append(e.key)
                continue
            if got != e.sha256:
                mismatch.append(e.key)
                continue
            if exists:
                if not overwrite:
                    conflicts.append(e.key)
                    continue
                local.replace(_backup_name(local))
            dests[d].get(sub, rel, local)
            if sha256_file(local) != e.sha256:
                local.unlink()  # never leave bytes the manifest does not vouch for
                mismatch.append(e.key)
                continue
            pulled += 1
    except PlatformError as exc:
        failure = exc
    BackupLedger(paths.backup_log).append(
        BackupRow(
            event="pull",
            ts=stamp(),
            manifest_id=manifest_id,
            dest=dest,
            tier=tier,
            pulled=pulled,
            skipped=skipped,
            conflicts=conflicts,
            missing=len(missing),
            failed=mismatch,
        )
    )
    counts = {
        "pulled": pulled,
        "skipped": skipped,
        "conflicts": len(conflicts),
        "missing": len(missing),
        "mismatch": len(mismatch),
    }
    if failure is not None:
        failure.fields.update(counts)
        raise failure
    if mismatch:
        raise IntegrityError(
            f"mismatch: {len(mismatch)} copy(ies) at {dest} do not match the manifest: "
            f"{', '.join(mismatch[:5])}",
            fields=counts,
        )
    if missing:
        raise IntegrityError(
            f"missing: {len(missing)} file(s) not at {dest}: {', '.join(missing[:5])}",
            fields=counts,
        )
    if conflicts:
        raise ValidationFailed(
            f"conflict: {len(conflicts)} local file(s) differ from the manifest (use --overwrite "
            f"to replace them, the old bytes are kept as .bak-<stamp>): {', '.join(conflicts[:5])}",
            fields=counts,
        )
    return PullResult(manifest_id, dest, tier, pulled, skipped, conflicts, missing, mismatch)
