"""Models of the access layer (spec 4): what an accessor was allowed, what it actually read,
and how a run refers to that receipt. Only ``vcp.core`` and pydantic may be imported here."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Purpose = Literal["train", "export", "measure", "submit", "custom"]
Binding = Literal["session", "manual"]
Grade = Literal["receipt", "export", "declared"]
Role = Literal["train", "eval", "sealed"]
Outcome = Literal["completed", "failed"]
GRADE_RANK: dict[str, int] = {"declared": 0, "export": 1, "receipt": 2}
MAX_DENIED_FIRST = 5
_SHA = re.compile(r"^[0-9a-f]{64}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccessedSubset(_Strict):
    """One subset the accessor actually iterated: how many distinct ids, their identity, and
    how many rows were parsed (a row read twice is parsed once)."""

    role: Role
    ids_count: int = Field(ge=0)
    ids_sha256: str
    records_parsed: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_hashes(self) -> AccessedSubset:
        if not _SHA.fullmatch(self.ids_sha256):
            raise ValueError("ids_sha256 must be 64 hex characters")
        return self


class AccessReceipt(_Strict):
    """``receipt.json`` of an ``access_receipt`` artifact. Built by the accessor at close; the
    caller's only field is ``notes``."""

    schema_version: int = 1
    dataset: str
    samples_hash: str
    card_sha256: str
    plan_id: str
    plan_sha256: str
    authorization_sha256: str
    purpose: Purpose
    run_id: str | None = None
    attempt: int | None = None
    allowed: list[str]
    roles: dict[str, str]
    accessed: dict[str, AccessedSubset]
    fields: list[str] = Field(default_factory=lambda: ["all"])
    denied: int = Field(default=0, ge=0)
    denied_first: list[str] = Field(default_factory=list)
    sealed_accessed: bool = False
    unseal_event_sha256: str | None = None
    outcome: Outcome
    exception: str | None = None
    started_at: str
    finished_at: str
    vcp_version: str
    notes: str = ""

    @model_validator(mode="after")
    def _shape(self) -> AccessReceipt:
        if self.allowed != sorted(self.allowed) or len(set(self.allowed)) != len(self.allowed):
            raise ValueError("allowed must be sorted and unique")
        if set(self.roles) != set(self.allowed):
            raise ValueError("roles must name exactly the allowed subsets")
        stray = sorted(set(self.accessed) - set(self.allowed))
        if stray:
            raise ValueError(f"accessed subsets must be allowed: {stray}")
        if len(self.denied_first) > MAX_DENIED_FIRST:
            raise ValueError(f"denied_first keeps at most {MAX_DENIED_FIRST} entries")
        if self.outcome == "failed" and not self.exception:
            raise ValueError("outcome=failed needs the exception class name")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        for name, value in (
            ("samples_hash", self.samples_hash),
            ("card_sha256", self.card_sha256),
            ("plan_sha256", self.plan_sha256),
            ("authorization_sha256", self.authorization_sha256),
        ):
            if not _SHA.fullmatch(value):
                raise ValueError(f"{name} must be 64 hex characters")
        if self.unseal_event_sha256 is not None:
            if not _SHA.fullmatch(self.unseal_event_sha256):
                raise ValueError("unseal_event_sha256 must be 64 hex characters")
        return self


class AccessRef(_Strict):
    """How ``train.yaml`` / ``run.yaml`` refer to one receipt (spec 4.1)."""

    artifact_id: str
    purpose: Purpose
    subsets: list[str]
    sealed_accessed: bool
    denied: int = Field(ge=0)
    receipt_sha256: str
    binding: Binding

    @model_validator(mode="after")
    def _validate_hashes(self) -> AccessRef:
        if not _SHA.fullmatch(self.receipt_sha256):
            raise ValueError("receipt_sha256 must be 64 hex characters")
        return self
