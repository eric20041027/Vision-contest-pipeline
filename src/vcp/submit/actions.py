"""``upload`` / ``record`` / ``score`` (spec 6.2): the ledger row is written by the same
command that performs -- or attests to -- the action."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.submit.guards import (
    QuotaState,
    assert_before_deadline,
    assert_quota,
    assert_unlocked,
    quota_state,
)
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.platforms import Runner, UploadResult, get_platform
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow, PlatformProfile, Staged
from vcp.submit.stage import load_staged
from vcp.submit.timewin import parse_at

FUTURE_TOLERANCE = timedelta(seconds=60)


@dataclass(frozen=True)
class Prepared:
    paths: DatasetPaths
    profile: PlatformProfile
    profile_sha: str
    ledger: SubmissionLedger
    staged: Staged
    artifact: Path | None


def _prepare(
    dataset: str, submission_id: str, data_root: Path | None, configs_root: Path | None
) -> Prepared:
    """The staged submission with its artifact re-hashed (the second of the three checks)."""
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, sha = load_profile(paths)
    ledger = SubmissionLedger(paths.submissions_log)
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
    return Prepared(paths, profile, sha, ledger, staged, artifact)


@dataclass(frozen=True)
class UploadOutcome:
    row: LedgerRow
    result: UploadResult
    quota: QuotaState | None


def upload(
    dataset: str,
    submission_id: str,
    *,
    message: str | None = None,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> UploadOutcome:
    p = _prepare(dataset, submission_id, data_root, configs_root)
    if p.profile.platform == "manual":
        raise ValidationFailed(
            "manual_platform: this profile has no upload API; upload by hand, then "
            "`vcp submit record`"
        )
    now = utc_now()
    assert_unlocked(p.ledger, submission_id=submission_id)
    assert_before_deadline(p.profile, now)
    assert_quota(quota_state(p.ledger, p.profile, now))
    msg = f"{submission_id} {message}".strip() if message else submission_id
    result = get_platform(p.profile.platform).upload(p.staged, p.artifact, msg, p.profile, runner)
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
    )
    p.ledger.append(row)
    return UploadOutcome(row, result, quota_state(p.ledger, p.profile, parse_stamp(str(row.at))))


@dataclass(frozen=True)
class RecordOutcome:
    row: LedgerRow
    quota: QuotaState | None
    warnings: list[str]


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
    p = _prepare(dataset, submission_id, data_root, configs_root)
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
    warnings: list[str] = []
    state = quota_state(p.ledger, p.profile, at)
    if state is not None and state.used >= state.per_day:
        warnings.append(
            f"quota_overflow: {state.used}/{state.per_day} already in the window ending "
            f"{stamp(state.window.end)}"
        )
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
    return RecordOutcome(row, quota_state(p.ledger, p.profile, at), warnings)


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
    load_profile(paths)
    ledger = SubmissionLedger(paths.submissions_log)
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
