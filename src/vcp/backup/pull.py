"""``vcp backup pull``: rebuild the manifest's files on this machine from a destination, each
read back against the manifest's sha. A local file that differs is a conflict, never silently
replaced; a fetched copy that does not match is deleted, never kept.

Two boundaries hold whatever the manifest says: a ``data`` / ``configs`` entry may only land
under its root, and an entry outside the roots is restored only into a directory that already
exists -- vcp never creates one out there. A ledger that only grew past the manifest's snapshot
counts as already here."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.backup.dest import Destination, open_dest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest, local_path
from vcp.backup.push import check_tier
from vcp.backup.schema import LEDGER_ROLES, BackupRow, FileEntry
from vcp.core.errors import IntegrityError, PlatformError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_prefix
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
    external_skipped: list[str]


def _backup_name(local: Path) -> Path:
    return local.with_name(f"{local.name}.bak-{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}")


def _check_targets(chosen: list[FileEntry], paths: DatasetPaths) -> None:
    """A manifest is data: no entry of it may name a target outside the root it claims. Checked
    for the whole list before anything is fetched, so a crafted manifest writes nothing."""
    for e in chosen:
        if e.root == "external":
            continue
        root = paths.data_root if e.root == "data" else paths.configs_root
        target = local_path(e, paths.data_root, paths.configs_root).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            raise ValidationFailed(
                f"unsafe_path: {e.key} would land outside the {e.root} root ({target})",
                fields={"file": e.key},
            ) from None


def _already_here(local: Path, e: FileEntry) -> bool:
    """The local file holds what the manifest describes -- or is an append-only ledger that only
    grew past it, which the manifest's snapshot does not oblige us to undo."""
    if e.role in LEDGER_ROLES and local.stat().st_size >= e.bytes:
        return sha256_prefix(local, e.bytes) == e.sha256
    return sha256_file(local) == e.sha256


def _restore(local: Path, backup: Path | None) -> None:
    """Whatever happened to the fetch, the file that was here comes back."""
    local.unlink(missing_ok=True)
    if backup is not None:
        backup.replace(local)


def _fetch(
    source: Destination, sub: str, rel: str, local: Path, e: FileEntry, backup: Path | None
) -> bool:
    """One file, read back against the manifest. Bytes vcp cannot vouch for never survive, and
    an ``--overwrite`` that failed leaves the old file in place, not only its ``.bak``."""
    try:
        source.get(sub, rel, local)
    except PlatformError:
        _restore(local, backup)
        raise
    if sha256_file(local) != e.sha256:
        _restore(local, backup)
        return False
    return True


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
    ledger = BackupLedger(paths.backup_log)  # a malformed row fails before any byte moves
    chosen = [f for f in manifest.files if f.tier <= tier]
    _check_targets(chosen, paths)
    dests: dict[str, Destination] = {dest: open_dest(dest, runner)}
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
    external_skipped: list[str] = []
    failure: PlatformError | None = None
    try:
        for e in chosen:
            d, sub, rel = sources[e.key]
            local = local_path(e, paths.data_root, paths.configs_root)
            exists = local.is_file()
            if exists and _already_here(local, e):
                skipped += 1
                continue
            if e.root == "external" and not Path(str(e.source)).parent.is_dir():
                external_skipped.append(e.key)  # vcp creates no directory outside its roots
                continue
            got = have[(d, sub)].get(rel)
            if got is None:
                missing.append(e.key)
                continue
            if got != e.sha256:
                mismatch.append(e.key)
                continue
            backup: Path | None = None
            if exists:
                if not overwrite:
                    conflicts.append(e.key)
                    continue
                backup = _backup_name(local)
                local.replace(backup)
            if _fetch(dests[d], sub, rel, local, e, backup):
                pulled += 1
            else:
                mismatch.append(e.key)
    except PlatformError as exc:
        failure = exc
    ledger.append(
        BackupRow(
            event="pull",
            ts=stamp(),
            manifest_id=manifest_id,
            dest=dest,
            tier=tier,
            pulled=pulled,
            skipped=skipped,
            conflicts=conflicts,
            dest_missing=len(missing),  # not the manifest's `missing`, nor a push's `failed`
            mismatch=mismatch,
            external_skipped=len(external_skipped),
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
    return PullResult(
        manifest_id, dest, tier, pulled, skipped, conflicts, missing, mismatch, external_skipped
    )
