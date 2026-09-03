"""manifest.jsonl: one row per materialized output, paths relative to the mode directory."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from vcp.core.errors import ValidationFailed


class ManifestRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str
    view: int | None
    seq_id: str | None
    src: str
    out: str
    shape: list[int]
    dtype: str
    resize: int | None
    window: str | None
    exif_policy: str
    decoder: str
    decoder_version: str
    sha256: str
    bytes: int
    materialized_at: str


def row_key(sample_id: str, view: int | None, seq_id: str | None) -> str:
    return f"{sample_id}\x00{view}\x00{seq_id}"


def read_manifest(path: Path) -> dict[str, ManifestRow]:
    if not path.is_file():
        return {}
    rows: dict[str, ManifestRow] = {}
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = ManifestRow.model_validate_json(line)
            except ValidationError as e:
                raise ValidationFailed(f"bad manifest row: {e}", location=f"{path}:{lineno}") from e
            rows[row_key(row.sample_id, row.view, row.seq_id)] = row
    return rows


def write_manifest(path: Path, rows: Iterable[ManifestRow]) -> None:
    ordered = sorted(
        rows, key=lambda r: (r.sample_id, r.seq_id or "", -1 if r.view is None else r.view)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in ordered:
            f.write(row.model_dump_json() + "\n")
