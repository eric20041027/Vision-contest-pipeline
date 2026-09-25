"""``submissions.jsonl``: every staging, upload, score and decision, appended and never edited."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from vcp.core.time import parse_stamp
from vcp.measure.ledger import read_rows
from vcp.submit.schema import LedgerRow

# How far vcp's stamp of one of its uploads and the platform's may sit apart -- the tolerance of
# sync's file-and-time rule (``sync.MATCH_WINDOW``, which cannot be imported here: sync imports
# this module; a test keeps the two equal).
TWIN_WINDOW = timedelta(minutes=10)


def append_ledger_row(path: Path, row: LedgerRow) -> None:
    """One JSON object per line; ``None`` fields are left out so a row carries only its event's
    fields (plan decision 10)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(row.model_dump_json(exclude_none=True) + "\n")


def _ours(
    uploads: list[LedgerRow], foreign: dict[str, LedgerRow], scored: list[LedgerRow]
) -> set[str]:
    """Foreign refs that are one of our own uploads, written by a ledger that did not know it
    yet -- another worktree's, or one where the upload was recorded later (VCP-038). An upload
    carrying the ref is that submission. Otherwise a ``scored`` row that ties the ref to an id
    lets a ref-less upload of that id absorb it: closest pairs first, one ref per upload, and
    only within ``TWIN_WINDOW``. A ref left over (say, a web upload never recorded) stays an
    arrival -- the count errs toward too many, never too few."""
    ours = {u.platform_ref for u in uploads if u.platform_ref} & foreign.keys()
    ties: dict[str, set[str]] = {}
    for s in scored:
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
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[LedgerRow] = read_rows(path, LedgerRow)

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
        rows = self.of("scored", submission_id)
        return rows[-1] if rows else None

    def arrivals(self) -> list[LedgerRow]:
        """Every upload the platform saw -- ours (``uploaded``) and others' (``foreign``) -- in
        platform-time order. A foreign upload may have multiple append-only snapshots while its
        platform status changes; only its newest snapshot is an arrival, and a foreign ref that
        is one of our own uploads is none (``_ours``). Stamps share one format, so string order
        is time order; at the same stamp our uploads come before others', each in ledger
        order."""
        latest_foreign: dict[str, LedgerRow] = {}
        uploads: list[LedgerRow] = []
        for row in self.rows:
            if row.event == "uploaded":
                uploads.append(row)
            elif row.event == "foreign" and row.platform_ref:
                latest_foreign[row.platform_ref] = row
        ours = _ours(uploads, latest_foreign, self.of("scored"))
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
