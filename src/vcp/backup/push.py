"""``vcp backup push`` (spec 6.1): the manifest's present files, tier by tier, only those the
destination does not already hold; every copy verified against the manifest; the ledger row
written whatever happened; and only once the whole manifest -- every tier, not just this
push's -- is verified at that destination, the credential wiped.

The manifest is a snapshot: what travels is the bytes the manifest describes. An append-only
ledger that gained rows between ``manifest`` and ``push`` therefore goes out truncated to its
recorded length, not as it stands now -- otherwise the very log this command writes would make
every evacuation fail."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from vcp.backup.completeness import manifest_gaps
from vcp.backup.dest import Destination, RcloneDest, copy_path, open_dest, travels
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest, local_path
from vcp.backup.schema import LEDGER_ROLES, BackupRow, FileEntry, Manifest, RemoteCopy
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
    local_copies: int = 0  # remote_copies this push sent like files (spec 2026-10-04 §5.2)


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
    append-only ledger that only grew contributes a snapshot of its first ``bytes`` bytes, and a
    local remote_copy its checkpoint or that copy. All checks happen before any byte moves: an
    evacuation must not half-run on a stale manifest."""
    out: dict[str, Path] = {}
    for index, e in enumerate(entries):
        if e.remote is not None:
            out[e.key] = _copy_source(e, e.remote, paths)
            continue
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


def _copy_source(e: FileEntry, copy: RemoteCopy, paths: DatasetPaths) -> Path:
    """A local remote_copy's bytes (spec 2026-10-04 §5.2): the checkpoint itself while it holds
    what the manifest recorded, else the copy ``train upload`` verified. Neither: nothing moves."""
    original = local_path(e, paths.data_root, paths.configs_root)
    kept = copy_path(copy)
    for candidate in (original, kept):
        if candidate.is_file() and sha256_file(candidate) == e.sha256:
            return candidate
    if original.is_file() or kept.is_file():
        raise IntegrityError(
            f"drift: neither {e.key} nor its copy {kept.as_posix()} holds the bytes the manifest "
            "recorded; write a new manifest",
            fields={"file": e.key},
        )
    raise ValidationFailed(
        f"not_found: {e.key} and its copy {kept.as_posix()} are both gone; write a new manifest",
        fields={"file": e.key},
    )


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
        after = {root: target.hashes(root, rels[root]) for root in roots}
        failed = [e.key for e in chosen if after[e.root].get(e.path) != e.sha256]
    except PlatformError as exc:
        # A copy or the read-back died: nothing was read back, so nothing counts as verified
        # (a copy that did land is found as `skipped` by the next push).
        failure = exc
        failed = [e.key for e in chosen]
    verified = 0 if failure is not None else len(chosen) - len(failed)
    return _Transfer(pushed, skipped, verified, sent, failed, failure)


def _unverified(manifest: Manifest, chosen: list[FileEntry], target: Destination) -> list[str]:
    """Every entry that belongs at the destination -- every file, and every remote_copy it does
    not cover (spec 2026-10-04 §5.2) -- this push did not send (a higher tier, or one the
    manifest recorded as gone) and whose copy there is absent or different. Forgetting the
    credential is the last act before the machine goes: the whole manifest must be there."""
    sent = {e.key for e in chosen}
    rest = [f for f in manifest.files if travels(f, target.dest) and f.key not in sent]
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
    gaps = manifest_gaps(manifest, paths) if tier == 3 or forget_remote else []
    if tier == 3 and gaps:  # spec 2026-10-04 §4.2: before any byte moves, and no push row
        raise ValidationFailed(
            f"manifest_incomplete: {len(gaps)} file(s) its runs registered are not in manifest "
            f"{manifest_id!r} (first {gaps[0].what}); write a new manifest under a new id, then "
            "push and verify that one",
            fields={"incomplete": len(gaps)},
        )
    # spec 2026-10-04 §5.2: the present files of tiers 1..N, and every remote_copy this
    # destination does not cover -- present or not: the copy `train upload` made holds the bytes
    chosen = [
        f
        for f in manifest.files
        if f.tier <= tier and travels(f, dest) and (f.present or f.remote is not None)
    ]
    local = sum(1 for f in chosen if f.remote is not None)
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
            local_copies=local or None,
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
        listed = sum(1 for f in manifest.files if travels(f, dest))
        if gaps:  # spec 2026-10-04 §4.2: what was never listed was never pushed either
            raise ValidationFailed(
                f"forget_refused: manifest {manifest_id!r} lacks {len(gaps)} file(s) its runs "
                "registered; write a new manifest under a new id and push every tier of it",
                fields={**counts, "unverified": len(left), "incomplete": len(gaps)},
            )
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
        local,
    )
