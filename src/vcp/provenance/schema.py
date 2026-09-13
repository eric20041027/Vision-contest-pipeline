"""Strict, stable records for dataset evolution and impact provenance."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vcp.core.hashing import sha256_text

_SHA = re.compile(r"^[0-9a-f]{64}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChangeType(StrEnum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"


class ChangeDomain(StrEnum):
    VIEWS = "VIEWS"
    LABELS = "LABELS"
    LABEL_SOURCE = "LABEL_SOURCE"
    GROUP = "GROUP"
    META = "META"
    UNKNOWN = "UNKNOWN"


class SemanticEffect(StrEnum):
    INPUT_AFFECTING = "INPUT_AFFECTING"
    TRAINING_AFFECTING = "TRAINING_AFFECTING"
    SPLIT_AFFECTING = "SPLIT_AFFECTING"
    EVALUATION_AFFECTING = "EVALUATION_AFFECTING"
    DISPLAY_ONLY = "DISPLAY_ONLY"
    UNKNOWN = "UNKNOWN"


class EntityStatus(StrEnum):
    VALID = "VALID"
    STALE = "STALE"
    REVIEW = "REVIEW"
    BROKEN = "BROKEN"


def _hash(value: str | None, what: str) -> str | None:
    if value is not None and not _SHA.fullmatch(value):
        raise ValueError(f"{what} must be 64 lowercase hex characters")
    return value


def make_change_id(
    from_samples_hash: str,
    to_samples_hash: str,
    sample_id: str,
    change_type: ChangeType,
    before_row_hash: str | None,
    after_row_hash: str | None,
) -> str:
    """The same transition produces the same event id in every process and index."""
    return sha256_text(
        "|".join(
            [
                from_samples_hash,
                to_samples_hash,
                sample_id,
                change_type.value,
                before_row_hash or "-",
                after_row_hash or "-",
            ]
        )
    )


class SampleChange(_Strict):
    schema_version: int = 1
    change_id: str
    from_dataset: str
    from_samples_hash: str
    to_dataset: str
    to_samples_hash: str
    sample_id: str = Field(min_length=1)
    change_type: ChangeType
    changed_domains: list[ChangeDomain]
    changed_fields: list[str]
    semantic_effects: list[SemanticEffect]
    before_row_hash: str | None = None
    after_row_hash: str | None = None

    @model_validator(mode="after")
    def _identity(self) -> SampleChange:
        _hash(self.change_id, "change_id")
        _hash(self.from_samples_hash, "from_samples_hash")
        _hash(self.to_samples_hash, "to_samples_hash")
        _hash(self.before_row_hash, "before_row_hash")
        _hash(self.after_row_hash, "after_row_hash")
        expected = make_change_id(
            self.from_samples_hash,
            self.to_samples_hash,
            self.sample_id,
            self.change_type,
            self.before_row_hash,
            self.after_row_hash,
        )
        if self.change_id != expected:
            raise ValueError("change_id does not match the stable transition fields")
        if self.change_type == ChangeType.ADDED:
            if self.before_row_hash is not None or self.after_row_hash is None:
                raise ValueError("ADDED needs only after_row_hash")
        elif self.change_type == ChangeType.REMOVED:
            if self.before_row_hash is None or self.after_row_hash is not None:
                raise ValueError("REMOVED needs only before_row_hash")
        elif self.before_row_hash is None or self.after_row_hash is None:
            raise ValueError("MODIFIED needs both row hashes")
        if self.changed_domains != sorted(set(self.changed_domains), key=str):
            raise ValueError("changed_domains must be unique and sorted")
        if self.changed_fields != sorted(set(self.changed_fields)):
            raise ValueError("changed_fields must be unique and sorted")
        if self.semantic_effects != sorted(set(self.semantic_effects), key=str):
            raise ValueError("semantic_effects must be unique and sorted")
        return self


class DatasetDiffSummary(_Strict):
    schema_version: int = 1
    artifact_id: str
    from_dataset: str
    from_samples_hash: str
    to_dataset: str
    to_samples_hash: str
    grade: Literal["source_audit", "fallback"]
    policies: list[str] = Field(default_factory=list)
    total_changes: int = Field(ge=0)
    counts: dict[str, int]
    domain_counts: dict[str, int]
    effect_counts: dict[str, int]

    @model_validator(mode="after")
    def _counts_match(self) -> DatasetDiffSummary:
        _hash(self.from_samples_hash, "from_samples_hash")
        _hash(self.to_samples_hash, "to_samples_hash")
        if sum(self.counts.values()) != self.total_changes:
            raise ValueError("change counts do not sum to total_changes")
        return self


class ProvenanceEntity(_Strict):
    entity_id: str
    entity_type: str
    key: str
    dataset_version_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    broken_reason: str | None = None


class ProvenanceEdge(_Strict):
    edge_id: str
    source_id: str
    target_id: str
    edge_type: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class StatusRecord(_Strict):
    entity_id: str
    status: EntityStatus
    reason: str
    predecessor_id: str | None = None


class ImpactResult(_Strict):
    dataset_version_id: str
    sample_id: str | None = None
    head_id: str | None = None
    entity_ids: list[str]
    statuses: dict[str, StatusRecord]


class ExplainResult(_Strict):
    entity_id: str
    ancestor_ids: list[str]
    edges: list[ProvenanceEdge]
