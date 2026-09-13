"""Pydantic models of the submission layer (spec 4): the platform profile, a staged candidate's
snapshot, and the rows of the submissions ledger."""

from __future__ import annotations

import math
import re
from typing import Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vcp.core.time import parse_stamp
from vcp.data.access.schema import Grade

PlatformName = Literal["manual", "kaggle"]
SubmissionKind = Literal["file", "kernel"]
BoardRule = Literal["last", "best"]
FinalRule = Literal["best_sealed"]
CandidateKind = Literal["candidate", "baseline", "probe"]
Admission = Literal["PASS", "waived"]
PairingMode = Literal["single", "fusion", "kernel"]
Event = Literal["staged", "uploaded", "scored", "foreign", "final", "lock", "unlock", "note"]
EVENTS = get_args(Event)
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_REQUIRED: dict[str, tuple[str, ...]] = {
    "staged": ("submission_id", "kind", "eval_run", "gate", "profile_sha256"),
    "uploaded": ("submission_id", "at", "source", "confirmed", "profile_sha256"),
    "scored": ("submission_id", "source"),
    "foreign": ("platform_ref", "file_name", "at"),
    "final": (
        "rule",
        "slots",
        "chosen",
        "table",
        "metric",
        "params",
        "holdout_unseals",
        "profile_sha256",
    ),
    "lock": ("reason",),
    "unlock": ("reason",),
    "note": ("text",),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def check_tz(name: str) -> str:
    """A zoneinfo name, or ValueError (pydantic turns it into a validation error)."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown time zone {name!r}") from e
    return name


def _finite(name: str, v: float | None) -> None:
    if v is not None and not math.isfinite(v):
        raise ValueError(f"{name} must be finite, got {v!r}")


class Quota(_Strict):
    """Uploads allowed per platform day: the day starts at ``day_start`` wall time in ``day_tz``."""

    per_day: int = Field(ge=1)
    day_tz: str = "UTC"
    day_start: str = "00:00"

    @field_validator("day_tz")
    @classmethod
    def _tz(cls, v: str) -> str:
        return check_tz(v)

    @field_validator("day_start")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError(f"day_start must be HH:MM, got {v!r}")
        return v


class PlatformProfile(_Strict):
    """``configs/datasets/<test>/submit.yaml`` (spec 4.1). No credential field exists, and
    ``extra="forbid"`` refuses one that is added by hand."""

    dataset: str
    eval_dataset: str
    plan_id: str
    sealed_subset: str
    test_plan: str = "all-v1"
    test_subset: str = "test"
    platform: PlatformName
    competition: str | None = None
    submission_kind: SubmissionKind = "file"
    board_rule: BoardRule
    final_rule: FinalRule = "best_sealed"
    final_slots: int = Field(default=1, ge=1)
    quota: Quota | None = None
    display_tz: str | None = None
    deadline: str | None = None
    metric: str
    metric_params: dict[str, str] = Field(default_factory=dict)
    writer: str | None = None
    writer_opts: dict[str, str] = Field(default_factory=dict)
    kaggle_command: list[str] = Field(default_factory=lambda: ["kaggle"])
    require_provenance: Grade = "declared"
    created_at: str

    @field_validator("display_tz")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        return None if v is None else check_tz(v)

    @field_validator("deadline")
    @classmethod
    def _stamp(cls, v: str | None) -> str | None:
        if v is not None:
            parse_stamp(v)
        return v

    @model_validator(mode="after")
    def _requirements(self) -> PlatformProfile:
        if self.platform == "kaggle" and not self.competition:
            raise ValueError("platform=kaggle needs competition")
        if self.submission_kind == "file" and not self.writer:
            raise ValueError("submission_kind=file needs writer")
        if not self.kaggle_command:
            raise ValueError("kaggle_command must not be empty")
        return self

    def effective_display_tz(self) -> str:
        """The zone ``record --at`` is read in: display_tz, else the quota day's zone, else UTC."""
        if self.display_tz:
            return self.display_tz
        if self.quota is not None:
            return self.quota.day_tz
        return "UTC"


class PairMember(_Strict):
    eval: str
    test: str
    mode: PairingMode


class Pairing(_Strict):
    """How the eval and test sides were shown to be the same model (spec 7)."""

    mode: PairingMode
    members: list[PairMember] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)


class WeightRef(_Strict):
    run: str
    sha256: str


class Artifact(_Strict):
    """What was (or will be) uploaded: a file with hashes, or a kernel reference."""

    kind: SubmissionKind
    path: str | None = None
    bytes: int | None = None
    sha256: str | None = None
    md5: str | None = None
    writer: str | None = None
    writer_version: str | None = None
    writer_opts: dict[str, str] = Field(default_factory=dict)
    rows: int | None = None
    samples: int | None = None
    missing: int | None = None
    kernel: str | None = None
    version: int | None = None
    output: str | None = None
    weights: list[WeightRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape(self) -> Artifact:
        if self.kind == "file":
            needed = {
                "path": self.path,
                "bytes": self.bytes,
                "sha256": self.sha256,
                "md5": self.md5,
                "writer": self.writer,
                "writer_version": self.writer_version,
                "rows": self.rows,
                "samples": self.samples,
                "missing": self.missing,
            }
            missing = [k for k, v in needed.items() if v is None]
            if missing:
                raise ValueError(f"file artifact needs {missing}")
        else:
            if self.kernel is None or self.version is None or self.output is None:
                raise ValueError("kernel artifact needs kernel, version and output")
            if not self.weights:
                raise ValueError("kernel artifact needs at least one weights reference")
        return self


class Gate(_Strict):
    admission: Admission
    judgements: list[str] = Field(default_factory=list)
    reason: str = ""


class Staged(_Strict):
    """``submit/<test>/<submission_id>/stage.json`` (spec 4.2): written once, never edited."""

    submission_id: str
    dataset: str
    kind: CandidateKind
    eval_run: str
    test_run: str | None
    pairing: Pairing
    artifact: Artifact
    gate: Gate
    profile_sha256: str
    staged_at: str
    vcp_version: str
    provenance: Grade | None = None


class FinalEntry(_Strict):
    submission_id: str
    eligible: bool
    why: str
    sealed_value: float | None = None
    sealed_reading_id: str | None = None
    public: float | None = None
    staged_at: str
    provenance: Grade | None = None

    @model_validator(mode="after")
    def _finite_values(self) -> FinalEntry:
        _finite("sealed_value", self.sealed_value)
        _finite("public", self.public)
        return self


class LedgerRow(_Strict):
    """One line of ``submissions.jsonl`` (spec 4.3). Every event shares the model; which fields
    it must carry is decided per event so a row can never be silently half-written."""

    event: Event
    ts: str
    submission_id: str | None = None
    kind: CandidateKind | None = None
    eval_run: str | None = None
    test_run: str | None = None
    sha256: str | None = None
    md5: str | None = None
    gate: Gate | None = None
    profile_sha256: str | None = None
    at: str | None = None
    source: str | None = None
    platform_ref: str | None = None
    message: str | None = None
    confirmed: bool | None = None
    public: float | None = None
    private: float | None = None
    platform_status: str | None = None
    file_name: str | None = None
    submitted_by: str | None = None
    rule: str | None = None
    slots: int | None = None
    chosen: list[str] | None = None
    table: list[FinalEntry] | None = None
    metric: str | None = None
    params: dict[str, str] | None = None
    holdout_unseals: int | None = None
    reason: str | None = None
    text: str | None = None

    @field_validator("ts", "at")
    @classmethod
    def _is_stamp(cls, v: str | None) -> str | None:
        if v is not None:
            parse_stamp(v)
        return v

    @model_validator(mode="after")
    def _shape(self) -> LedgerRow:
        missing = [f for f in _REQUIRED[self.event] if getattr(self, f) is None]
        if missing:
            raise ValueError(f"{self.event} needs {missing}")
        if self.event == "scored" and self.public is None and self.private is None:
            raise ValueError("scored needs a public or private score")
        _finite("public", self.public)
        _finite("private", self.private)
        return self
