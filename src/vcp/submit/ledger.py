"""``submissions.jsonl``: every staging, upload, score and decision, appended and never edited."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.core.time import parse_stamp
from vcp.measure.ledger import read_rows
from vcp.submit.schema import LedgerRow

# How far vcp's stamp of one of its uploads and the platform's may sit apart -- the tolerance of
# sync's file-and-time rule (``sync.MATCH_WINDOW``, which cannot be imported here: sync imports
# this module; a test keeps the two equal).
TWIN_WINDOW = timedelta(minutes=10)

_BEFORE_ANY = datetime.min.replace(tzinfo=UTC)  # where a score without a platform time sorts
# The rows that say what became of an upload on the platform (spec 2026-10-09 §4.2).
OUTCOMES = ("scored", "errored")


def _when(row: LedgerRow) -> datetime:
    return parse_stamp(row.at) if row.at else _BEFORE_ANY


def assign_scores(uploads: list[LedgerRow], scores: list[LedgerRow]) -> list[LedgerRow | None]:
    """The score that applies to each upload of one submission id (same order as ``uploads``,
    which may be in any order -- ``record --at`` can append an upload whose platform time
    precedes an earlier row's). A platform-timed score (``at`` present) belongs to the upload
    whose ``at`` is nearest in time to the score's ``at`` (smallest absolute difference; a tie
    goes to the upload with the later ``at``). A manual score (no ``at``) belongs to the newest
    upload (by ``ts``) with ``upload.ts <= score.ts`` (none -> the score is dropped). Per
    upload, the newest assigned score (by ``ts``) wins; an upload nothing was assigned to gets
    None. ``scores`` may hold ``errored`` rows too, placed by the same rule: the result is then
    each upload's outcome (spec 2026-10-09 §4.2).

    Each score is placed independently -- scores never compete to "claim" an upload the way an
    earlier version of this function had them do. Claiming let a corrected score (a later ``ts``
    for the same moment) bump the score it corrects onto a different, wrong upload; placing each
    score on its own nearest/eligible upload and then letting the newest ``ts`` win per upload
    does not have that failure mode.
    """
    assigned: list[LedgerRow | None] = [None] * len(uploads)
    if not uploads:
        return assigned
    ats = [parse_stamp(str(u.at)) for u in uploads]
    for s in scores:
        if s.at is not None:
            score_at = parse_stamp(s.at)
            winner = min(
                range(len(uploads)),
                key=lambda idx: (abs(ats[idx] - score_at), -ats[idx].timestamp()),
            )
        else:
            eligible = [idx for idx, u in enumerate(uploads) if u.ts <= s.ts]
            if not eligible:
                continue
            winner = max(eligible, key=lambda idx: (uploads[idx].ts, idx))
        if assigned[winner] is None or s.ts > assigned[winner].ts:
            assigned[winner] = s
    return assigned


def append_ledger_row(path: Path, row: LedgerRow) -> None:
    """One JSON object per line; ``None`` fields are left out so a row carries only its event's
    fields (plan decision 10)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(row.model_dump_json(exclude_none=True) + "\n")


def complete_length(path: Path) -> int:
    """Bytes up to and including the last newline: the part whose rows are whole. A last line
    without its newline is a row still being written (spec 2026-09-28 §4.2); 0 when absent."""
    if not path.is_file():
        return 0
    return path.read_bytes().rfind(b"\n") + 1


def _complete_rows(path: Path) -> list[LedgerRow]:
    """The rows of whole lines only, for readers that take no lock. The shared ``read_rows``
    stays strict: only this ledger has writers that serialize through a lock, so only here is
    a torn last line a write in progress rather than damage."""
    if not path.is_file():
        return []
    data = path.read_bytes()
    text = data[: data.rfind(b"\n") + 1].decode("utf-8")
    rows: list[LedgerRow] = []
    for lineno, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            rows.append(LedgerRow.model_validate_json(line))
        except ValidationError as e:
            raise ValidationFailed(f"bad ledger row: {e}", location=f"{path.name}:{lineno}") from e
    return rows


def _ours(
    uploads: list[LedgerRow], foreign: dict[str, LedgerRow], outcomes: list[LedgerRow]
) -> set[str]:
    """Foreign refs that are one of our own uploads, written by a ledger that did not know it
    yet -- another worktree's, or one where the upload was recorded later (VCP-038). An upload
    carrying the ref is that submission. Otherwise a ``scored`` or ``errored`` row (spec
    2026-10-09 §4.6) that ties the ref to an id lets a ref-less upload of that id absorb it:
    closest pairs first, one ref per upload, and only within ``TWIN_WINDOW``. A ref left over
    (say, a web upload never recorded) stays an arrival -- the count errs toward too many,
    never too few."""
    ours = {u.platform_ref for u in uploads if u.platform_ref} & foreign.keys()
    ties: dict[str, set[str]] = {}
    for s in outcomes:
        if s.platform_ref and s.submission_id:
            ties.setdefault(s.platform_ref, set()).add(s.submission_id)
    free: dict[str, list[tuple[datetime, int]]] = {}
    for i, u in enumerate(uploads):
        if u.submission_id and not u.platform_ref:
            free.setdefault(u.submission_id, []).append((parse_stamp(str(u.at)), i))
    pairs = sorted(
        (abs(upload_at - parse_stamp(str(row.at))), i, ref)
        for ref, row in foreign.items()
        if ref not in ours
        for sid in ties.get(ref, ())
        for upload_at, i in free.get(sid, ())
    )
    absorbed: set[int] = set()
    for gap, i, ref in pairs:
        if gap > TWIN_WINDOW:
            break
        if i not in absorbed and ref not in ours:
            absorbed.add(i)
            ours.add(ref)
    return ours


class SubmissionLedger:
    def __init__(self, path: Path, *, complete_only: bool = False) -> None:
        """``complete_only``: skip a last line still being written -- for readers that take no
        lock (``status``, ``report``, backup). Writers read strictly inside the lock, where a
        torn last line is a crashed writer's and must stop the command, not be appended to."""
        self.path = path
        self.rows: list[LedgerRow] = (
            _complete_rows(path) if complete_only else read_rows(path, LedgerRow)
        )

    def append(self, row: LedgerRow) -> None:
        append_ledger_row(self.path, row)
        self.rows.append(row)

    def of(self, event: str, submission_id: str | None = None) -> list[LedgerRow]:
        return [
            r
            for r in self.rows
            if r.event == event and (submission_id is None or r.submission_id == submission_id)
        ]

    def ids(self) -> list[str]:
        """Staged submission ids in ledger order."""
        return [r.submission_id for r in self.of("staged") if r.submission_id]

    def staged(self, submission_id: str) -> LedgerRow | None:
        rows = self.of("staged", submission_id)
        return rows[0] if rows else None

    def uploads(self, submission_id: str) -> list[LedgerRow]:
        return self.of("uploaded", submission_id)

    def latest_score(self, submission_id: str) -> LedgerRow | None:
        """The newest score by platform time (spec 2026-09-28 §4.6), not by file order: a later
        sync may append an older upload's score. A row without ``at`` (``vcp submit score``)
        counts as older than every platform-timed row; ledger order breaks ties, so a
        corrected score of the same moment wins."""
        rows = self.of("scored", submission_id)
        if not rows:
            return None
        return rows[max(range(len(rows)), key=lambda i: (_when(rows[i]), i))]

    def outcomes(self, submission_id: str) -> list[LedgerRow]:
        """The id's ``scored`` and ``errored`` rows, in ledger order (spec 2026-10-09 §4.2)."""
        return [r for r in self.rows if r.event in OUTCOMES and r.submission_id == submission_id]

    def assigned_outcomes(self, submission_id: str) -> list[LedgerRow | None]:
        """What became of each upload of the id, in ``uploads`` order: ``assign_scores`` over
        the id's ``scored`` and ``errored`` rows together. A ``scored`` row: scored; an
        ``errored`` row: the platform finished it without a score; None: no result yet."""
        return assign_scores(self.uploads(submission_id), self.outcomes(submission_id))

    def latest_outcome(self, submission_id: str) -> LedgerRow | None:
        """The outcome of the id's newest upload -- by platform time, then ``ts`` -- or None
        when that upload has no result yet or the id has no upload."""
        uploads = self.uploads(submission_id)
        if not uploads:
            return None
        newest = max(range(len(uploads)), key=lambda i: (uploads[i].at or "", uploads[i].ts))
        return self.assigned_outcomes(submission_id)[newest]

    def score_for_ref(self, submission_id: str, platform_ref: str) -> LedgerRow | None:
        """The newest ``scored`` row of one platform entry of this id, in ledger order."""
        return self._newest_for_ref("scored", submission_id, platform_ref)

    def errored_for_ref(self, submission_id: str, platform_ref: str) -> LedgerRow | None:
        """The newest ``errored`` row of one platform entry of this id, in ledger order (spec
        2026-10-09 §4.1)."""
        return self._newest_for_ref("errored", submission_id, platform_ref)

    def _newest_for_ref(
        self, event: str, submission_id: str, platform_ref: str
    ) -> LedgerRow | None:
        rows = [r for r in self.of(event, submission_id) if r.platform_ref == platform_ref]
        return rows[-1] if rows else None

    def arrivals(self) -> list[LedgerRow]:
        """Every upload the platform saw -- ours (``uploaded``) and others' (``foreign``) -- in
        platform-time order. A foreign upload may have multiple append-only snapshots while its
        platform status changes; only its newest snapshot is an arrival, and a foreign ref that
        is one of our own uploads is none (``_ours``). Two ``uploaded`` rows carrying one ref
        are one platform entry (adopt can merge a ``record --platform-ref`` from one checkout
        with another's sync binding): the first in ledger order is its arrival. Stamps share one
        format, so string order is time order; at the same stamp our uploads come before
        others', each in ledger order."""
        latest_foreign: dict[str, LedgerRow] = {}
        uploads: list[LedgerRow] = []
        upload_refs: set[str] = set()
        for row in self.rows:
            if row.event == "uploaded":
                if row.platform_ref:
                    if row.platform_ref in upload_refs:
                        continue
                    upload_refs.add(row.platform_ref)
                uploads.append(row)
            elif row.event == "foreign" and row.platform_ref:
                latest_foreign[row.platform_ref] = row
        ours = _ours(uploads, latest_foreign, [r for r in self.rows if r.event in OUTCOMES])
        others = [row for ref, row in latest_foreign.items() if ref not in ours]
        return sorted([*uploads, *others], key=lambda r: r.at or "")

    def last_uploaded(self) -> LedgerRow | None:
        arrivals = self.arrivals()
        return arrivals[-1] if arrivals else None

    def foreign_refs(self) -> set[str]:
        return {r.platform_ref for r in self.of("foreign") if r.platform_ref}

    def latest_foreign(self, platform_ref: str) -> LedgerRow | None:
        rows = [r for r in self.of("foreign") if r.platform_ref == platform_ref]
        return rows[-1] if rows else None

    def lock_state(self) -> LedgerRow | None:
        """The lock row in force, or None: the newest of lock / unlock decides."""
        state: LedgerRow | None = None
        for r in self.rows:
            if r.event == "lock":
                state = r
            elif r.event == "unlock":
                state = None
        return state

    def latest_final(self) -> LedgerRow | None:
        rows = self.of("final")
        return rows[-1] if rows else None
