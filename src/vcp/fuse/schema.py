"""Pydantic models of the fusion layer: a recipe (git) and the record of a build (runs/)."""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Member(_Strict):
    """One run in a recipe and its weight. Order inside ``Recipe.members`` is significant."""

    run: str = Field(min_length=1)
    weight: float = 1.0

    @field_validator("weight")
    @classmethod
    def _finite_positive(cls, v: float) -> float:
        # A zero or negative weight silently removes or inverts a member; nan/inf would poison
        # every score-weighted average downstream.
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"member weight must be a finite positive number, got {v!r}")
        return v


class Recipe(_Strict):
    """``configs/datasets/<name>/fuse/<recipe_id>.yaml`` (spec 4.1). ``params`` are the fuser's
    EFFECTIVE params (defaults filled in), all strings, like a pre-registration's."""

    recipe_id: str
    dataset: str
    plan_id: str
    method: str
    params: dict[str, str] = Field(default_factory=dict)
    members: list[Member] = Field(min_length=1)
    notes: str = ""
    created_at: str

    @model_validator(mode="after")
    def _unique_members(self) -> Recipe:
        runs = [m.run for m in self.members]
        dupes = sorted({r for r in runs if runs.count(r) > 1})
        if dupes:
            raise ValueError(f"duplicate members {dupes}")
        return self


class MemberRecord(_Strict):
    run: str
    weight: float
    trained_on: list[str]


class SubsetBuild(_Strict):
    """One subset of one build: which member bytes went in, which bytes came out."""

    member_sha256: dict[str, str]
    output_sha256: str
    samples: int
    empty: int
    built_at: str


class FuseRecord(_Strict):
    """``runs/<run_id>/fuse.json`` (spec 4.2): the bit-level provenance of a fused run."""

    run_id: str
    recipe_id: str
    recipe_sha256: str
    method: str
    method_version: str
    params: dict[str, str]
    members: list[MemberRecord]
    subsets: dict[str, SubsetBuild] = Field(default_factory=dict)
    vcp_version: str
