"""``<data_root>/artifacts/<kind>/supersession.jsonl``: append-only, written after a commit, an
index that ``relink`` can rebuild from committed manifests (spec 2). The jsonl helpers are the
measurement layer's, as the backup ledger already does."""

from __future__ import annotations

from pathlib import Path

from vcp.artifact.schema import ArtifactManifest, SupersessionRow
from vcp.core.paths import artifacts_root, validate_name
from vcp.core.time import stamp
from vcp.measure.ledger import append_row, read_rows

SUPERSESSION_LEDGER = "supersession.jsonl"


def supersession_log(data_root: Path, kind: str) -> Path:
    validate_name(kind)
    return artifacts_root(data_root) / kind / SUPERSESSION_LEDGER


def append_supersession(data_root: Path, row: SupersessionRow) -> None:
    append_row(supersession_log(data_root, row.kind), row)


def read_supersession(data_root: Path, kind: str) -> list[SupersessionRow]:
    return read_rows(supersession_log(data_root, kind), SupersessionRow)


def supersession_of(data_root: Path, kind: str, artifact_id: str) -> SupersessionRow | None:
    """The first row recording ``artifact_id`` superseding something (``relink`` is idempotent,
    so there is at most one)."""
    return next((r for r in read_supersession(data_root, kind) if r.id == artifact_id), None)


def row_for(manifest: ArtifactManifest, manifest_sha256: str) -> SupersessionRow:
    """The row a committed manifest implies. The writer appends it right after the commit and
    ``relink`` appends the same one when that append never happened."""
    spec = manifest.spec
    if (
        spec.supersedes is None
        or spec.supersedes_reason is None
        or manifest.supersedes_sha256 is None
    ):
        raise ValueError(f"artifact {spec.kind}/{spec.id} supersedes nothing")
    return SupersessionRow(
        ts=stamp(),
        kind=spec.kind,
        id=spec.id,
        manifest_sha256=manifest_sha256,
        supersedes_id=spec.supersedes,
        supersedes_sha256=manifest.supersedes_sha256,
        reason=spec.supersedes_reason,
    )
