"""Canonical dataset format. A *sample* is the unit of prediction; it owns one or more views.

Field names describe data shape, never a source domain: ``seq_id`` covers DICOM series,
video clips and time series alike; ``targets`` covers multi-label, regression and soft labels.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LabelSource = Literal["gold", "derived", "pseudo", "none"]
ExifPolicy = Literal["stored", "oriented"]
RawManifestMode = Literal["full", "sizes"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class View(_Strict):
    path: str = Field(min_length=1)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    role: str | None = None
    seq_id: str | None = None
    seq_index: int | None = Field(default=None, ge=0)
    meta: dict[str, Any] = Field(default_factory=dict)


class Box(_Strict):
    """Absolute pixels, top-left origin, xywh. Rotation parameters, if any, go to meta.rotated."""

    x: float
    y: float
    w: float
    h: float
    category_id: int
    view: int = Field(default=0, ge=0)
    meta: dict[str, Any] = Field(default_factory=dict)


class Mask(_Strict):
    category_id: int
    view: int = Field(default=0, ge=0)
    rle: str | None = None
    polygon: list[list[float]] | None = None
    path: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_representation(self) -> Mask:
        present = sum(x is not None for x in (self.rle, self.polygon, self.path))
        if present != 1:
            raise ValueError("Mask needs exactly one of rle / polygon / path")
        return self


class Labels(_Strict):
    cls: int | None = None
    targets: dict[str, float] | None = None
    boxes: list[Box] | None = None
    masks: list[Mask] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Sample(_Strict):
    sample_id: str = Field(min_length=1)
    views: list[View] = Field(min_length=1)
    labels: Labels | None = None
    label_source: LabelSource
    group: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistency(self) -> Sample:
        if (self.label_source != "none") != (self.labels is not None):
            raise ValueError("label_source must be 'none' exactly when labels is absent")
        seen: set[tuple[str | None, int]] = set()
        for v in self.views:
            if v.seq_index is None:
                continue
            key = (v.seq_id, v.seq_index)
            if key in seen:
                raise ValueError(f"duplicate seq_index {v.seq_index} in seq {v.seq_id!r}")
            seen.add(key)
        return self


class Category(_Strict):
    id: int
    name: str = Field(min_length=1)
    meta: dict[str, Any] = Field(default_factory=dict)


class SourceInfo(_Strict):
    importer: str
    importer_version: str
    raw_path: str
    raw_hash: str
    license: str
    url: str
    downloaded_at: str
    notes: str = ""
    raw_manifest_mode: RawManifestMode = "full"


class DatasetCard(_Strict):
    name: str
    task: str
    categories: list[Category] = Field(default_factory=list)
    image_root: str
    source: SourceInfo
    created_at: str
    sample_count: int = Field(ge=0)
    samples_hash: str
    exif_policy: ExifPolicy = "stored"
    schema_version: int = 1

    @model_validator(mode="after")
    def _unique_categories(self) -> DatasetCard:
        ids = [c.id for c in self.categories]
        names = [c.name for c in self.categories]
        if len(set(ids)) != len(ids):
            raise ValueError("category ids must be unique")
        if len(set(names)) != len(names):
            raise ValueError("category names must be unique")
        return self


def dump_sample(sample: Sample) -> dict[str, Any]:
    """Minimal, stable dict: defaults and None dropped, key order = declaration order."""
    return sample.model_dump(mode="json", exclude_none=True, exclude_defaults=True)


def sample_json_line(sample: Sample) -> str:
    return json.dumps(dump_sample(sample), ensure_ascii=False)
