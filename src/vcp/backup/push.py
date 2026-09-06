"""``vcp backup push`` (spec 6.1): the manifest's present files, tier by tier, only those the
destination does not already hold; every copy verified against the manifest; the ledger row
written whatever happened; and only when nothing failed, the credential wiped.

The manifest is a snapshot: what travels is the bytes the manifest describes. An append-only
ledger that gained rows between ``manifest`` and ``push`` therefore goes out truncated to its
recorded length, not as it stands now -- otherwise the very log this command writes would make
every evacuation fail."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from vcp.backup.dest import Destination, RcloneDest, open_dest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest, local_path
from vcp.backup.schema import LEDGER_ROLES, BackupRow, FileEntry, Manifest
from vcp.core.errors import IntegrityError, PlatformError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_prefix
from vcp.core.paths import DatasetPaths
from vcp.core.proc import Runner
from vcp.core.time import stamp

TIERS = (1, 2, 3)
_CHUNK = 1 << 20


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


def _snapshot(src: Path, entry: FileEntry, tmpdir: Path, index: int) -> Path:
    """The first ``entry.bytes`` bytes of a ledger that grew since the manifest, in a scratch
    file. One directory per entry, so two ledgers of the same file name cannot collide."""
    out = tmpdir / str(index) / Path(entry.path).name
    out.parent.mkdir(parents=True)
    left = entry.bytes
    with src.open("rb") as f, out.open("wb") as g:
        while left > 0:
            chunk = f.read(min(_CHUNK, left))
            if not chunk:
                break
            g.write(chunk)
            left -= len(chunk)
    return out


def _sources(entries: list[FileEntry], paths: DatasetPaths, tmpdir: Path) -> dict[str, Path]:
    """Every file about to be pushed, holding exactly the bytes the manifest recorded -- an
    append-only ledger that only grew contributes a snapshot of its first ``bytes`` bytes. All
    checks happen before any byte moves: an evacuation must not half-run on a stale manifest."""
    out: dict[str, Path] = {}
    for index, e in enumerate(entries):
        src = local_path(e, paths.data_root, paths.configs_root)
        if not src.is_file():
            raise ValidationFailed(
                f"not_found: {e.key} is listed as present but is gone locally; "
                "write a new manifest",
                fields={"file": e.key},
            )
        size = src.stat().st_size
        if size == e.bytes and sha256_file(src) == e.sha256:
            out[e.key] = src
        elif e.role in LEDGER_ROLES and size > e.bytes and sha256_prefix(src, e.bytes) == e.sha256:
            out[e.key] = _snapshot(src, e, tmpdir, index)
        else:
            raise IntegrityError(
                f"drift: {e.key} changed since the manifest was written; write a new manifest",
                fields={"file": e.key},
            )
    return out


@dataclass(frozen=True)
class _Transfer:
    pushed: int
    skipped: int
    verified: int
    bytes: int
    failed: list[str]
    failure: PlatformError | None


def _send(target: Destination, chosen: list[FileEntry], sources: dict[str, Path]) -> _Transfer:
    """Copy what the destination does not already hold, then read every copy's hash back."""
    roots = sorted({e.root for e in chosen})
    rels = {root: [e.path for e in chosen if e.root == root] for root in roots}
    before = {root: target.hashes(root, rels[root]) for root in roots}
    pushed = skipped = sent = 0
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
    except PlatformError as exc:
        failure = exc
        failed = [e.key for e in chosen if e.key not in done]
    return _Transfer(pushed, skipped, len(chosen) - len(failed), sent, failed, failure)


def _unverified(manifest: Manifest, chosen: list[FileEntry], target: Destination) -> list[str]:
    """Every ``kind=file`` entry this push did not send -- a higher tier, or one the manifest
    already recorded as gone -- whose copy at the destination is absent or different. Forgetting
    the credential is the last act before the machine goes: the whole manifest must be there."""
    sent = {e.key for e in chosen}
    rest = [f for f in manifest.files if f.kind == "file" and f.key not in sent]
    roots = sorted({e.root for e in rest})
    have = {root: target.hashes(root, [e.path for e in rest if e.root == root]) for root in roots}
    return [e.key for e in rest if have[e.root].get(e.path) != e.sha256]


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
    ledger = BackupLedger(paths.backup_log)  # a malformed row fails before any byte moves
    target = open_dest(dest, runner)
    if forget_remote and not isinstance(target, RcloneDest):
        raise ValidationFailed(
            "forget_refused: --forget-remote needs an rclone destination; a local directory "
            "holds no credential",
            fields={"dest": dest},
        )
    chosen = [f for f in manifest.files if f.kind == "file" and f.present and f.tier <= tier]
    with tempfile.TemporaryDirectory(prefix="vcp-push-") as tmp:
        sent = _send(target, chosen, _sources(chosen, paths, Path(tmp)))
    ledger.append(
        BackupRow(
            event="push",
            ts=stamp(),
            manifest_id=manifest_id,
            dest=dest,
            tier=tier,
            pushed=sent.pushed,
            skipped=sent.skipped,
            verified=sent.verified,
            failed=sent.failed,
            bytes=sent.bytes,
        )
    )
    counts = {
        "pushed": sent.pushed,
        "skipped": sent.skipped,
        "verified": sent.verified,
        "failed": len(sent.failed),
    }
    if sent.failure is not None:
        sent.failure.fields.update(counts)
        raise sent.failure
    if sent.failed:
        raise IntegrityError(
            f"mismatch: {len(sent.failed)} file(s) not verified at {dest}: "
            f"{', '.join(sent.failed[:5])}",
            fields=counts,
        )
    forgotten: str | None = None
    if forget_remote and isinstance(target, RcloneDest):
        left = _unverified(manifest, chosen, target)
        listed = sum(1 for f in manifest.files if f.kind == "file")
        if left or sent.verified == 0 or listed == 0:
            raise ValidationFailed(
                f"forget_refused: {len(left)} file(s) of the manifest are not verified at "
                f"{dest}; push every tier first",
                fields={**counts, "unverified": len(left)},
            )
        forgotten = target.forget()
        ledger.append(
            BackupRow(
                event="remote_forgotten", ts=stamp(), manifest_id=manifest_id, remote=forgotten
            )
        )
    return PushResult(
        manifest_id,
        dest,
        tier,
        sent.pushed,
        sent.skipped,
        sent.verified,
        sent.failed,
        sent.bytes,
        forgotten,
    )
