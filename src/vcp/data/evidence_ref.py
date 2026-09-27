"""``EvidenceRef``: a run's reference to an immutable artifact it read -- a label set or a
copied evidence file (spec 2026-09-26 §3.3). The model and the list rules only; making the
artifacts is ``vcp.data.evidence``'s job, writing the list is the caller's."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from vcp.core.errors import ValidationFailed
from vcp.core.paths import validate_name

EvidenceKind = Literal["evidence", "label_set"]
Binding = Literal["cli", "session", "manual"]
LABELS_ROLE = "labels"


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    role: str
    kind: EvidenceKind
    artifact_id: str
    manifest_sha256: str
    attempt: int | None = None
    attached_at: str
    binding: Binding

    @field_validator("name", "role", "artifact_id")
    @classmethod
    def _path_safe(cls, v: str) -> str:
        try:
            validate_name(v)
        except ValidationFailed as e:
            raise ValueError(str(e)) from e
        return v


def check_name(refs: list[EvidenceRef], name: str, role: str, kind: str) -> None:
    """The list rule that can fail (spec §3.3): a name keeps its role and kind."""
    same = [r for r in refs if r.name == name]
    if any(r.role != role or r.kind != kind for r in same):
        raise ValidationFailed(
            f"evidence_conflict: {name!r} is attached as {same[-1].kind}/{same[-1].role}, "
            f"not {kind}/{role}",
            fields={"evidence_name": name},
        )


def add_ref(refs: list[EvidenceRef], ref: EvidenceRef) -> list[EvidenceRef]:
    """The list only grows (spec §3.3): the same name and manifest again is no new row, the same
    name with another manifest is a new row that becomes current, and the same name under
    another role or kind is ``evidence_conflict:``."""
    check_name(refs, ref.name, ref.role, ref.kind)
    same = [r for r in refs if r.name == ref.name]
    if same and same[-1].manifest_sha256 == ref.manifest_sha256:
        return list(refs)
    return [*refs, ref]


def add_refs(refs: list[EvidenceRef], new: list[EvidenceRef]) -> list[EvidenceRef]:
    out = list(refs)
    for ref in new:
        out = add_ref(out, ref)
    return out


def current(refs: list[EvidenceRef]) -> list[EvidenceRef]:
    """The newest row of every name, names in the order they were first attached."""
    newest: dict[str, EvidenceRef] = {}
    for r in refs:
        newest[r.name] = r
    return list(newest.values())


def merge_refs(first: list[EvidenceRef], second: list[EvidenceRef]) -> list[EvidenceRef]:
    """``first``'s rows, then those of ``second`` it lacks by (kind, artifact_id): how
    ``train.yaml``'s list is merged into ``run.yaml``'s, as access receipts are."""
    seen = {(r.kind, r.artifact_id) for r in first}
    return [*first, *(r for r in second if (r.kind, r.artifact_id) not in seen)]


def labels_field(refs: list[EvidenceRef]) -> str:
    """``dataset`` when no label set is attached, else the current label-set ids."""
    ids = [r.artifact_id for r in current(refs) if r.kind == "label_set"]
    return ",".join(ids) if ids else "dataset"
