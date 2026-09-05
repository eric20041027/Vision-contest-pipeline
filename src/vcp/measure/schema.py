"""Pydantic models of the measurement layer. Field names describe data shape, never a contest."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.tasks import get_task

PAYLOAD_FIELDS = ("boxes", "masks", "scores", "targets")


def payload_field(task: str) -> str:
    """Which ``Prediction`` field a task's predictions live in.

    Derived from the task registry (``TaskSpec.pred_payload``) so adding a task type stays a
    one-place change instead of a second closed-world mapping here.
    """
    try:
        return get_task(task).pred_payload
    except RegistryError as e:
        raise ValidationFailed(str(e)) from e


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _finite(values: list[float], what: str) -> None:
    bad = [v for v in values if not math.isfinite(v)]
    if bad:
        raise ValueError(f"{what} must be finite, got {bad[:3]}")


class PredBox(_Strict):
    x: float
    y: float
    w: float
    h: float
    category_id: int
    score: float = Field(ge=0.0, le=1.0)
    view: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _finite_geometry(self) -> PredBox:
        _finite([self.x, self.y, self.w, self.h], "box geometry")
        return self


class PredMask(_Strict):
    category_id: int
    score: float = Field(ge=0.0, le=1.0)
    view: int = Field(default=0, ge=0)
    rle: str | None = None
    polygon: list[list[float]] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one(self) -> PredMask:
        if (self.rle is None) == (self.polygon is None):
            raise ValueError("PredMask needs exactly one of rle / polygon")
        return self

    @model_validator(mode="after")
    def _finite_polygon(self) -> PredMask:
        if self.polygon is not None:
            _finite([v for ring in self.polygon for v in ring], "mask polygon")
        return self


class Prediction(_Strict):
    sample_id: str = Field(min_length=1)
    boxes: list[PredBox] | None = None
    masks: list[PredMask] | None = None
    scores: dict[str, float] | None = None
    targets: dict[str, float] | None = None

    @field_validator("scores", "targets")
    @classmethod
    def _finite_values(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is not None:
            _finite(list(v.values()), "prediction values")
        return v

    @model_validator(mode="after")
    def _one_payload(self) -> Prediction:
        present = [f for f in PAYLOAD_FIELDS if getattr(self, f) is not None]
        if len(present) != 1:
            raise ValueError(f"Prediction needs exactly one payload field, got {present}")
        return self

    def payload_field(self) -> str:
        return next(f for f in PAYLOAD_FIELDS if getattr(self, f) is not None)


class RunSource(_Strict):
    framework: str = ""
    config_hash: str | None = None
    weights_hash: str | None = None
    export_manifest_sha: str | None = None
    notes: str = ""


class PredictionFile(_Strict):
    """One subset's canonical prediction file, as recorded on a ``RunCard``.

    ``samples`` is the number of prediction rows actually written; ``empty`` is the number of
    subset samples that have no row (det/seg may omit a sample to mean "no predictions").
    """

    path: str
    sha256: str
    samples: int
    empty: int
    format_in: str
    ingested_at: str


class RunCard(_Strict):
    run_id: str
    dataset: str
    samples_hash: str
    plan_id: str
    trained_on: list[str]
    source: RunSource
    created_at: str
    predictions: dict[str, PredictionFile] = Field(default_factory=dict)


class MetricResult(_Strict):
    value: float
    per_class: dict[str, float | None] | None = None
    n: int


class GuardrailInfo(_Strict):
    anchor_reading_id: str
    ok: bool


class Reading(_Strict):
    reading_id: str
    ts: str
    run_id: str
    dataset: str
    samples_hash: str
    plan_id: str
    subset: str
    metric: str
    metric_version: str
    params: dict[str, str]
    value: float
    per_class: dict[str, float | None] | None
    n_samples: int
    prediction_sha: str
    guardrail: GuardrailInfo | None = None


class Anchor(_Strict):
    run_id: str
    reading_id: str
    value: float
    tolerance: float
    set_at: str


class PreRegistration(_Strict):
    prereg_id: str
    claim: str
    component: str
    component_class: Literal["model", "tuning"]
    baseline_run: str
    candidate_run: str
    metric: str
    params: dict[str, str] = Field(default_factory=dict)
    subsets: list[str]
    t_min: float = 2.0
    min_bases: int = 2
    sigma_method: str = "splithalf"
    sigma_ratio: float = 1.0
    created_at: str


class SubsetJudgement(_Strict):
    baseline: float
    candidate: float
    delta: float
    se: float
    t: float
    n: int


class SigmaRef(_Strict):
    method: str
    value: float
    estimate_id: str


class Judgement(_Strict):
    prereg_id: str
    ts: str
    baseline_run: str
    candidate_run: str
    metric: str
    params: dict[str, str]
    per_subset: dict[str, SubsetJudgement]
    bases_positive: int
    sigma_p: SigmaRef | None
    verdict: Literal["PASS", "FAIL", "INVALID"]
    reasons: list[str]
    reading_ids: list[str]
    bootstrap: dict[str, int]


class SigmaEstimate(_Strict):
    estimate_id: str
    ts: str
    plan_id: str
    metric: str
    params: dict[str, str]
    method: str
    value: float
    inputs: dict[str, Any]
    note: str = ""
