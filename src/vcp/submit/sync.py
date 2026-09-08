"""``vcp submit sync`` (spec 6.3): what the platform says happened, reconciled with the ledger.
Uploads the ledger never saw become ``foreign`` rows -- they spent quota too."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, stamp
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.platforms import PlatformSubmission, Runner, get_platform
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow
from vcp.submit.stage import load_staged, stage_json

MATCH_WINDOW = timedelta(minutes=10)


def _row(**fields: Any) -> LedgerRow:
    """A ledger row from platform data, or a located FAIL when the platform's values are
    unusable."""
    try:
        return LedgerRow(**fields)
    except ValidationError as e:
        raise ValidationFailed(f"platform_response: {e}", fields={"key": "score"}) from e


@dataclass(frozen=True)
class SyncResult:
    platform_rows: int
    scored: int
    foreign: int
    unconfirmed: list[str]
    matched: dict[str, str] = field(default_factory=dict)


def _mentions(description: str, submission_id: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9._-]){re.escape(submission_id)}(?![A-Za-z0-9._-])"
    return re.search(pattern, description) is not None


def match_submission(
    p: PlatformSubmission,
    ledger: SubmissionLedger,
    file_names: dict[str, str],
    taken: set[str] | frozenset[str] = frozenset(),
) -> str | None:
    """Plan decision 8 (a recorded platform ref) first, then spec 6.3's two rules. ``taken``
    holds ids already matched in this sync, so the file-and-time rule moves on to the next
    submission that uploaded the same file name."""
    for r in ledger.of("uploaded"):
        if r.platform_ref and r.platform_ref == p.platform_ref:
            return r.submission_id
    for sid in sorted(ledger.ids(), key=len, reverse=True):
        if _mentions(p.description, sid):
            return sid
    at = parse_stamp(p.at)
    for sid in ledger.ids():
        if sid in taken or file_names.get(sid) != p.file_name:
            continue
        for r in ledger.uploads(sid):
            if r.at and abs(parse_stamp(r.at) - at) <= MATCH_WINDOW:
                return sid
    return None


def sync(
    dataset: str,
    *,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> SyncResult:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    if profile.platform == "manual":
        raise ValidationFailed("manual_platform: nothing to read back from; use `vcp submit score`")
    ledger = SubmissionLedger(paths.submissions_log)
    subs = get_platform(profile.platform).list_submissions(profile, runner)
    file_names: dict[str, str] = {}
    for sid in ledger.ids():
        if not stage_json(paths, sid).is_file():
            continue  # snapshot lives in the data root; the ref and description rules still work
        st = load_staged(paths, sid)
        file_names[sid] = str(
            st.artifact.path if st.artifact.kind == "file" else st.artifact.output
        )
    known_foreign = ledger.foreign_refs()
    matched: dict[str, str] = {}
    scored = foreign = 0
    # Oldest first (stamps sort as strings), so a resubmitted id's scored rows land in time
    # order and its newest platform score is the ledger's latest (ruling R7).
    for p in sorted(subs, key=lambda s: s.at):
        sid = match_submission(p, ledger, file_names, taken=set(matched.values()))
        if sid is None:
            row = _row(
                event="foreign",
                ts=stamp(),
                platform_ref=p.platform_ref,
                file_name=p.file_name,
                at=p.at,
                public=p.public,
                private=p.private,
                platform_status=p.status or None,
                submitted_by=p.submitted_by,
            )
            previous = ledger.latest_foreign(p.platform_ref)
            if previous is not None and all(
                getattr(previous, field) == getattr(row, field)
                for field in (
                    "platform_ref",
                    "file_name",
                    "at",
                    "public",
                    "private",
                    "platform_status",
                    "submitted_by",
                )
            ):
                continue
            ledger.append(row)
            if p.platform_ref in known_foreign:
                continue
            known_foreign.add(p.platform_ref)
            foreign += 1
            continue
        matched[p.platform_ref] = sid
        if p.public is None and p.private is None:
            continue
        latest = ledger.latest_score(sid)
        if latest is None or latest.public != p.public or latest.private != p.private:
            ledger.append(
                _row(
                    event="scored",
                    ts=stamp(),
                    submission_id=sid,
                    public=p.public,
                    private=p.private,
                    source="platform",
                    platform_status=p.status or None,
                    at=p.at,
                    platform_ref=p.platform_ref,
                )
            )
            scored += 1
    confirmed = set(matched.values())
    unconfirmed = [sid for sid in ledger.ids() if ledger.uploads(sid) and sid not in confirmed]
    return SyncResult(len(subs), scored, foreign, unconfirmed, matched)
