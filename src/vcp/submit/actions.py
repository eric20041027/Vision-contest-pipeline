"""``upload`` / ``record`` / ``score`` (spec 6.2): the ledger row is written by the same
command that performs -- or attests to -- the action, each inside the ledger's transaction (spec
2026-09-28 §4.2). ``upload`` first reads the platform's list into the ledger (§4.3) and refuses
an id that went up before unless ``--force`` gives a reason (§4.5, VCP-014)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from vcp.core.errors import IntegrityError, ValidationFailed, VcpError
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.submit.guards import (
    QuotaState,
    already_uploaded,
    assert_before_deadline,
    assert_not_uploaded,
    assert_quota,
    assert_unlocked,
    quota_state,
)
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import transaction
from vcp.submit.platforms import Runner, UploadResult, get_platform
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow, PlatformProfile, Staged
from vcp.submit.stage import load_staged
from vcp.submit.sync import SyncResult, reconcile
from vcp.submit.timewin import parse_at

FUTURE_TOLERANCE = timedelta(seconds=60)
SyncState = Literal["ok", "skipped"]


@dataclass(frozen=True)
class Prepared:
    paths: DatasetPaths
    profile: PlatformProfile
    profile_sha: str
    ledger: SubmissionLedger
    staged: Staged
    artifact: Path | None


def _prepare(
    paths: DatasetPaths,
    profile: PlatformProfile,
    profile_sha: str,
    ledger: SubmissionLedger,
    submission_id: str,
    *,
    uploading: bool = False,
) -> Prepared:
    """The staged submission with its artifact re-hashed (the second of the three checks),
    against the ledger the transaction just read."""
    if uploading:
        if profile.platform == "manual":
            raise ValidationFailed(
                "manual_platform: this profile has no upload API; upload by hand, then "
                "`vcp submit record`"
            )
        assert_unlocked(ledger, submission_id=submission_id)
        assert_before_deadline(profile, utc_now())
    if ledger.staged(submission_id) is None:
        raise ValidationFailed(
            f"not_staged: {submission_id!r} has no staged row", fields={"id": submission_id}
        )
    staged = load_staged(paths, submission_id)
    artifact: Path | None = None
    if staged.artifact.kind == "file":
        artifact = paths.submission_dir(submission_id) / str(staged.artifact.path)
        if not artifact.is_file():
            raise IntegrityError(f"artifact missing: {artifact}")
        actual = sha256_file(artifact)
        if actual != staged.artifact.sha256:
            raise IntegrityError(
                f"artifact sha256 {actual[:12]} != staged {str(staged.artifact.sha256)[:12]}",
                location=str(artifact),
            )
    return Prepared(paths, profile, profile_sha, ledger, staged, artifact)


@dataclass(frozen=True)
class UploadOutcome:
    row: LedgerRow
    result: UploadResult
    quota: QuotaState | None
    sync: SyncState = "ok"  # "skipped" under --no-sync, a WARN (spec 2026-09-28 §4.3)
    bound: int = 0  # uploaded rows of source=platform the pre-upload sync wrote (§4.4)


def _known_refs(ledger: SubmissionLedger) -> frozenset[str]:
    """Every platform ref the ledger already holds, in any row: earlier submissions."""
    return frozenset(r.platform_ref for r in ledger.rows if r.platform_ref)


def _unclaimed(result: UploadResult, ledger: SubmissionLedger) -> UploadResult:
    """A ref the ledger already holds is an earlier submission's, not this upload's (VCP-037).
    The read-back looks past such refs itself (spec 2026-10-04 §6.1); this guards a ref the
    CLI printed, and a platform that ignores ``known_refs``."""
    known = _known_refs(ledger)
    if result.platform_ref is None or result.platform_ref not in known:
        return result
    return replace(result, confirmed=False, platform_ref=None, readback="known_ref")


def _force_reason(force: str | None) -> str | None:
    """``--force "<reason>"`` (spec 2026-09-28 §4.5): the reason goes into the new row, so an
    empty one is refused before anything else happens."""
    if force is None:
        return None
    reason = force.strip()
    if not reason:
        raise ValidationFailed("invalid: --force needs a reason, got an empty string")
    return reason


def _pre_sync(p: Prepared, runner: Runner | None) -> SyncResult:
    """spec 2026-09-28 §4.3: what ``vcp submit sync`` does, inside this upload's transaction and
    before the quota is counted, so an upload the platform lists and the ledger lacks counts. A
    list vcp cannot read -- or cannot store -- stops the upload before anything is sent."""
    try:
        subs = get_platform(p.profile.platform).list_submissions(p.profile, runner)
        return reconcile(p.paths, p.profile_sha, p.ledger, subs)
    except (VcpError, OSError) as e:
        raise ValidationFailed(f"sync_failed: {e}") from e


def upload(
    dataset: str,
    submission_id: str,
    *,
    message: str | None = None,
    force: str | None = None,
    no_sync: bool = False,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> UploadOutcome:
    """One upload, one transaction (spec 2026-09-28 §4.2): every check that needs no platform,
    then the platform's list read into the ledger, the quota, the re-upload guard, the upload
    and its row. The rows the pre-sync wrote stay even when a later step refuses, so that
    refusal carries ``sync=`` and ``bound=`` like the upload's own VERDICT would."""
    reason = _force_reason(force)
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, profile_sha = load_profile(paths)
    with transaction(paths, profile, command="submit.upload") as ledger:
        p = _prepare(paths, profile, profile_sha, ledger, submission_id, uploading=True)
        synced = None if no_sync else _pre_sync(p, runner)
        state: SyncState = "skipped" if synced is None else "ok"
        bound = 0 if synced is None else synced.bound
        try:
            row, result, quota = _send(p, submission_id, message, reason, runner)
        except VcpError as e:
            e.fields.setdefault("sync", state)
            e.fields.setdefault("bound", bound)
            raise
    return UploadOutcome(row, result, quota, sync=state, bound=bound)


def _send(
    p: Prepared,
    submission_id: str,
    message: str | None,
    reason: str | None,
    runner: Runner | None,
) -> tuple[LedgerRow, UploadResult, QuotaState | None]:
    """What follows the pre-sync, on the ledger it just wrote: the quota, the re-upload guard
    (unless ``--force`` gave ``reason``), the upload and its row."""
    assert_quota(quota_state(p.ledger, p.profile, utc_now()))
    if reason is None:
        assert_not_uploaded(p.ledger, submission_id)
    msg = f"{submission_id} {message}".strip() if message else submission_id
    platform = get_platform(p.profile.platform)
    known = _known_refs(p.ledger)
    result = platform.upload(p.staged, p.artifact, msg, p.profile, runner, known_refs=known)
    result = _unclaimed(result, p.ledger)
    row = LedgerRow(
        event="uploaded",
        ts=stamp(),
        submission_id=submission_id,
        at=stamp(utc_now()),
        source="vcp",
        platform_ref=result.platform_ref,
        message=msg,
        confirmed=result.confirmed,
        sha256=p.staged.artifact.sha256,
        profile_sha256=p.profile_sha,
        reason=reason,
    )
    p.ledger.append(row)
    return row, result, quota_state(p.ledger, p.profile, parse_stamp(str(row.at)))


@dataclass(frozen=True)
class RecordOutcome:
    row: LedgerRow
    quota: QuotaState | None
    warnings: list[str]
    prior_uploads: int = 0  # uploaded rows of this id before this one (spec 2026-09-28 §4.5)


def record(
    dataset: str,
    submission_id: str,
    at_text: str,
    *,
    tz: str = "platform",
    platform_ref: str | None = None,
    message: str | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> RecordOutcome:
    if tz not in ("platform", "utc"):
        raise ValidationFailed(f"--tz must be platform or utc, got {tz!r}")
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, profile_sha = load_profile(paths)
    with transaction(paths, profile, command="submit.record") as ledger:
        p = _prepare(paths, profile, profile_sha, ledger, submission_id)
        return _record_locked(p, submission_id, at_text, tz, platform_ref, message)


def _assert_new_ref(ledger: SubmissionLedger, platform_ref: str | None) -> None:
    """A platform ref an ``uploaded`` row already carries -- any id, any source -- is that
    upload, recorded already: a second row would count one platform entry twice (final review
    M3). ``exists:``, and nothing is written."""
    if not platform_ref:
        return
    for r in ledger.of("uploaded"):
        if r.platform_ref == platform_ref:
            raise ValidationFailed(
                f"exists: platform ref {platform_ref} is already recorded for {r.submission_id}"
            )


def _record_locked(
    p: Prepared,
    submission_id: str,
    at_text: str,
    tz: str,
    platform_ref: str | None,
    message: str | None,
) -> RecordOutcome:
    """``record`` does no sync -- a platform without a list is why it exists (spec 2026-09-28
    §4.3) -- and an upload that happened is written down even when the id went up before."""
    tz_name = p.profile.effective_display_tz() if tz == "platform" else "UTC"
    at = parse_at(at_text, tz_name)
    now = utc_now()
    if at > now + FUTURE_TOLERANCE:
        raise ValidationFailed(f"at: {stamp(at)} is in the future (now {stamp(now)})")
    floor = parse_stamp(p.staged.staged_at).replace(microsecond=0)
    if at < floor:
        raise ValidationFailed(
            f"at: {stamp(at)} is before the submission was staged ({p.staged.staged_at})"
        )
    assert_unlocked(p.ledger, submission_id=submission_id)
    assert_before_deadline(p.profile, at)
    _assert_new_ref(p.ledger, platform_ref)
    warnings: list[str] = []
    state = quota_state(p.ledger, p.profile, at)
    if state is not None and state.used >= state.per_day:
        warnings.append(
            f"quota_overflow: {state.used}/{state.per_day} already in the window ending "
            f"{stamp(state.window.end)}"
        )
    prior = len(p.ledger.uploads(submission_id))
    again = already_uploaded(p.ledger, submission_id)
    if again is not None:
        warnings.append(f"{again}; recorded anyway")
    row = LedgerRow(
        event="uploaded",
        ts=stamp(),
        submission_id=submission_id,
        at=stamp(at),
        source="manual",
        platform_ref=platform_ref,
        message=message,
        confirmed=True,
        sha256=p.staged.artifact.sha256,
        profile_sha256=p.profile_sha,
    )
    p.ledger.append(row)
    return RecordOutcome(row, quota_state(p.ledger, p.profile, at), warnings, prior_uploads=prior)


def score(
    dataset: str,
    submission_id: str,
    *,
    public: float | None = None,
    private: float | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> LedgerRow:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    with transaction(paths, profile, command="submit.score") as ledger:
        if ledger.staged(submission_id) is None or not ledger.uploads(submission_id):
            raise ValidationFailed(
                f"not_uploaded: {submission_id!r} has no uploaded row to score",
                fields={"id": submission_id},
            )
        if public is None and private is None:
            raise ValidationFailed("a --public or --private score is required")
        try:
            row = LedgerRow(
                event="scored",
                ts=stamp(),
                submission_id=submission_id,
                public=public,
                private=private,
                source="manual",
            )
        except ValidationError as e:
            raise ValidationFailed(str(e)) from e
        ledger.append(row)
        return row
