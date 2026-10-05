"""``vcp submit sync`` (spec 6.3; 2026-09-28 §4.4, §4.6): what the platform says happened,
reconciled with the ledger. Uploads the ledger never saw become ``foreign`` rows -- they spent
quota too. An entry matched to an id whose upload the ledger lacks becomes an ``uploaded`` row of
``source=platform`` (a binding), scored or not, so the quota counts it and the re-upload guard
sees it. ``upload`` runs ``reconcile`` inside its own transaction before it counts the quota
(§4.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vcp.core.errors import PlatformTimeout, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, stamp
from vcp.submit.ledger import TWIN_WINDOW, SubmissionLedger
from vcp.submit.location import transaction
from vcp.submit.matching import leads, mentions
from vcp.submit.platforms import PlatformSubmission, Runner, get_platform
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow
from vcp.submit.stage import load_staged, stage_json

MATCH_WINDOW = TWIN_WINDOW  # one ten-minute tolerance for every "near in time" rule
PLATFORM = "platform"  # the source of the rows sync writes from the platform's list
FOREIGN_FIELDS = (
    "platform_ref",
    "file_name",
    "at",
    "public",
    "private",
    "platform_status",
    "submitted_by",
)
SCORE_FIELDS = ("public", "private", "platform_status")


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
    foreign: int  # platform refs seen for the first time (each spent quota once)
    unconfirmed: list[str]
    matched: dict[str, str] = field(default_factory=dict)
    refreshed: int = 0  # already-known foreign refs whose status or score changed (new snapshot)
    bound: int = 0  # uploaded rows of source=platform written (spec 2026-09-28 §4.4)


def match_submission(
    p: PlatformSubmission,
    ledger: SubmissionLedger,
    file_names: dict[str, str],
    taken: set[str] | frozenset[str] = frozenset(),
) -> str | None:
    """Plan decision 8 (a recorded platform ref) first, then spec 6.3's two rules. The
    description rule first looks for the id the description opens with -- vcp writes
    ``<id> <message>``, so ``S2 same as S1`` is S2's -- and only then for an id it merely
    mentions (spec 2026-09-28 §4.4): each pass tries longer ids first, then ledger order.
    ``taken`` holds ids already matched in this sync, so the file-and-time rule moves on to the
    next submission that uploaded the same file name. That rule looks only at uploads vcp or a
    person attested: a binding is the platform's own entry, and letting it vouch for a
    same-named neighbour within ten minutes would bind the neighbour to the id as well."""
    for r in ledger.of("uploaded"):
        if r.platform_ref and r.platform_ref == p.platform_ref:
            return r.submission_id
    ids = sorted(ledger.ids(), key=len, reverse=True)
    for names in (leads, mentions):
        for sid in ids:
            if names(p.description, sid):
                return sid
    at = parse_stamp(p.at)
    for sid in ledger.ids():
        if sid in taken or file_names.get(sid) != p.file_name:
            continue
        for r in ledger.uploads(sid):
            if r.source != PLATFORM and r.at and abs(parse_stamp(r.at) - at) <= MATCH_WINDOW:
                return sid
    return None


def _needs_binding(ledger: SubmissionLedger, sid: str, p: PlatformSubmission) -> bool:
    """spec 2026-09-28 §4.4: the id has no ``uploaded`` row carrying this ref, and none without a
    ref within ``TWIN_WINDOW`` of the platform's time (that one is this entry, unconfirmed)."""
    at = parse_stamp(p.at)
    for r in ledger.uploads(sid):
        if r.platform_ref == p.platform_ref:
            return False
        if r.platform_ref is None and abs(parse_stamp(str(r.at)) - at) <= TWIN_WINDOW:
            return False
    return True


def _foreign_row(p: PlatformSubmission) -> LedgerRow:
    return _row(
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


def _binding_row(sid: str, p: PlatformSubmission, profile_sha: str) -> LedgerRow:
    """spec 2026-09-28 §3.2: the platform's time and ref, confirmed, and the profile in force."""
    return _row(
        event="uploaded",
        ts=stamp(),
        submission_id=sid,
        at=p.at,
        source=PLATFORM,
        platform_ref=p.platform_ref,
        confirmed=True,
        profile_sha256=profile_sha,
    )


def _scored_row(sid: str, p: PlatformSubmission) -> LedgerRow:
    return _row(
        event="scored",
        ts=stamp(),
        submission_id=sid,
        public=p.public,
        private=p.private,
        source=PLATFORM,
        platform_status=p.status or None,
        at=p.at,
        platform_ref=p.platform_ref,
    )


def _score_changed(ledger: SubmissionLedger, sid: str, row: LedgerRow) -> bool:
    """spec 2026-09-28 §4.6: a ``scored`` row only for a ref that has none yet (VCP-038: that row
    ties the ref to the id), or whose score or status differs from that ref's newest row -- so an
    id scored differently on two uploads is not rewritten by every sync."""
    previous = ledger.score_for_ref(sid, str(row.platform_ref))
    return previous is None or any(getattr(previous, f) != getattr(row, f) for f in SCORE_FIELDS)


def _file_names(paths: DatasetPaths, ledger: SubmissionLedger) -> dict[str, str]:
    """The artifact file name of every staged id whose stage.json is still there (it lives in
    the data root; the ref and description rules work without it)."""
    out: dict[str, str] = {}
    for sid in ledger.ids():
        if not stage_json(paths, sid).is_file():
            continue
        st = load_staged(paths, sid)
        out[sid] = str(st.artifact.path if st.artifact.kind == "file" else st.artifact.output)
    return out


def reconcile(
    paths: DatasetPaths,
    profile_sha: str,
    ledger: SubmissionLedger,
    subs: list[PlatformSubmission],
) -> SyncResult:
    """spec 6.3 with 2026-09-28 §4.4 and §4.6, on a ledger whose lock the caller holds. Every
    platform value is checked before the first row is written: one the ledger cannot store
    (``platform_response:``) leaves the ledger as it was."""
    for p in subs:
        _foreign_row(p)
    file_names = _file_names(paths, ledger)
    known_foreign = ledger.foreign_refs()
    matched: dict[str, str] = {}
    scored = foreign = refreshed = bound = 0
    # Oldest first (stamps sort as strings), so an id's rows land in platform-time order.
    for p in sorted(subs, key=lambda s: s.at):
        sid = match_submission(p, ledger, file_names, taken=set(matched.values()))
        if sid is None:
            row = _foreign_row(p)
            previous = ledger.latest_foreign(p.platform_ref)
            if previous is not None and all(
                getattr(previous, f) == getattr(row, f) for f in FOREIGN_FIELDS
            ):
                continue
            ledger.append(row)
            if p.platform_ref in known_foreign:
                refreshed += 1  # a later snapshot of a ref already counted: no second arrival
                continue
            known_foreign.add(p.platform_ref)
            foreign += 1
            continue
        matched[p.platform_ref] = sid
        if _needs_binding(ledger, sid, p):
            ledger.append(_binding_row(sid, p, profile_sha))
            bound += 1
        if p.public is None and p.private is None:
            continue
        row = _scored_row(sid, p)
        if _score_changed(ledger, sid, row):
            ledger.append(row)
            scored += 1
    confirmed = set(matched.values())
    unconfirmed = [sid for sid in ledger.ids() if ledger.uploads(sid) and sid not in confirmed]
    return SyncResult(
        len(subs), scored, foreign, unconfirmed, matched, refreshed=refreshed, bound=bound
    )


def sync(
    dataset: str,
    *,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> SyncResult:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, profile_sha = load_profile(paths)
    if profile.platform == "manual":
        raise ValidationFailed("manual_platform: nothing to read back from; use `vcp submit score`")
    with transaction(paths, profile, command="submit.sync") as ledger:
        try:
            subs = get_platform(profile.platform).list_submissions(profile, runner)
        except PlatformTimeout as e:  # spec 2026-10-04 §6.3: the word upload's pre-sync uses
            raise ValidationFailed(f"sync_failed: {e}") from e
        return reconcile(paths, profile_sha, ledger, subs)
