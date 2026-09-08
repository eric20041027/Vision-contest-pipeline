"""``submissions.jsonl``: every staging, upload, score and decision, appended and never edited."""

from __future__ import annotations

from pathlib import Path

from vcp.measure.ledger import read_rows
from vcp.submit.schema import LedgerRow


def append_ledger_row(path: Path, row: LedgerRow) -> None:
    """One JSON object per line; ``None`` fields are left out so a row carries only its event's
    fields (plan decision 10)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(row.model_dump_json(exclude_none=True) + "\n")


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
        platform status changes; only its newest snapshot is an arrival. Stamps share one format,
        so string order is time order; ties keep ledger order."""
        latest_foreign: dict[str, LedgerRow] = {}
        uploads: list[LedgerRow] = []
        for row in self.rows:
            if row.event == "uploaded":
                uploads.append(row)
            elif row.event == "foreign" and row.platform_ref:
                latest_foreign[row.platform_ref] = row
        rows = [*uploads, *latest_foreign.values()]
        return sorted(rows, key=lambda r: r.at or "")

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
