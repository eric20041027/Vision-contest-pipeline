"""``backup.log.jsonl``: every manifest, push, verify, pull and credential wipe, appended and
never edited."""

from __future__ import annotations

from pathlib import Path

from vcp.backup.schema import BackupRow
from vcp.measure.ledger import read_rows


class BackupLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[BackupRow] = read_rows(path, BackupRow)

    def append(self, row: BackupRow) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(row.model_dump_json(exclude_none=True) + "\n")
        self.rows.append(row)

    def of(self, event: str, manifest_id: str | None = None) -> list[BackupRow]:
        return [
            r
            for r in self.rows
            if r.event == event and (manifest_id is None or r.manifest_id == manifest_id)
        ]

    def latest(self, event: str, manifest_id: str) -> BackupRow | None:
        rows = self.of(event, manifest_id)
        return rows[-1] if rows else None

    def manifest_ids(self) -> list[str]:
        return [r.manifest_id for r in self.of("manifest") if r.manifest_id]
