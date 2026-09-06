"""``vcp backup push`` (spec 6.1): the manifest's present files, tier by tier, only those the
destination does not already hold; every copy verified against the manifest; the ledger row
written whatever happened; and only when nothing failed, the credential wiped."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.backup.dest import RcloneDest, open_dest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest, local_path
from vcp.backup.schema import BackupRow, FileEntry
from vcp.core.errors import IntegrityError, PlatformError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.proc import Runner
from vcp.core.time import stamp

TIERS = (1, 2, 3)


@dataclass(frozen=True)
class PushResult:
    manifest_id: str
    dest: str
    tier: int
    pushed: int
    skipped: int
    verified: int
    failed: list[str]
    bytes: int
    forgotten: str | None = None


def check_tier(tier: int) -> None:
    """Range check in the function layer (never Click's), so a bad value still gets a VERDICT."""
    if tier not in TIERS:
        raise ValidationFailed(f"tier: must be 1, 2 or 3, got {tier}", fields={"tier": tier})


def _sources(entries: list[FileEntry], paths: DatasetPaths) -> dict[str, Path]:
    """Every file about to be pushed, still holding the bytes the manifest recorded. All checks
    happen before any byte moves: an evacuation must not half-run on a stale manifest."""
    out: dict[str, Path] = {}
    for e in entries:
        src = local_path(e, paths.data_root, paths.configs_root)
        if not src.is_file():
            raise ValidationFailed(
                f"not_found: {e.key} is listed as present but is gone locally; "
                "write a new manifest",
                fields={"file": e.key},
            )
        if sha256_file(src) != e.sha256:
            raise IntegrityError(
                f"drift: {e.key} changed since the manifest was written; write a new manifest",
                fields={"file": e.key},
            )
        out[e.key] = src
    return out


def push(
    dataset: str,
    manifest_id: str,
    dest: str,
    *,
    tier: int = 1,
    forget_remote: bool = False,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> PushResult:
    check_tier(tier)
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    manifest = load_manifest(paths, manifest_id)
    target = open_dest(dest, runner)
    if forget_remote and not isinstance(target, RcloneDest):
        raise ValidationFailed(
            "forget_refused: --forget-remote needs an rclone destination; a local directory "
            "holds no credential",
            fields={"dest": dest},
        )
    chosen = [f for f in manifest.files if f.kind == "file" and f.present and f.tier <= tier]
    sources = _sources(chosen, paths)
    roots = sorted({e.root for e in chosen})
    rels = {root: [e.path for e in chosen if e.root == root] for root in roots}
    before = {root: target.hashes(root, rels[root]) for root in roots}
    pushed = skipped = sent = verified = 0
    done: list[str] = []
    failed: list[str] = []
    failure: PlatformError | None = None
    try:
        for e in chosen:  # manifest order: tier 1 first
            if before[e.root].get(e.path) == e.sha256:
                skipped += 1
            else:
                target.put(sources[e.key], e.root, e.path)
                pushed += 1
                sent += e.bytes
            done.append(e.key)
        after = {root: target.hashes(root, rels[root]) for root in roots}
        failed = [e.key for e in chosen if after[e.root].get(e.path) != e.sha256]
        verified = len(chosen) - len(failed)
    except PlatformError as exc:
        failure = exc
        failed = [e.key for e in chosen if e.key not in done]
    ledger = BackupLedger(paths.backup_log)
    ledger.append(
        BackupRow(
            event="push",
            ts=stamp(),
            manifest_id=manifest_id,
            dest=dest,
            tier=tier,
            pushed=pushed,
            skipped=skipped,
            verified=verified,
            failed=failed,
            bytes=sent,
        )
    )
    counts = {"pushed": pushed, "skipped": skipped, "verified": verified, "failed": len(failed)}
    if failure is not None:
        failure.fields.update(counts)
        raise failure
    if failed:
        raise IntegrityError(
            f"mismatch: {len(failed)} file(s) not verified at {dest}: {', '.join(failed[:5])}",
            fields=counts,
        )
    forgotten: str | None = None
    if forget_remote and isinstance(target, RcloneDest):
        forgotten = target.forget()
        ledger.append(
            BackupRow(
                event="remote_forgotten", ts=stamp(), manifest_id=manifest_id, remote=forgotten
            )
        )
    return PushResult(manifest_id, dest, tier, pushed, skipped, verified, failed, sent, forgotten)
