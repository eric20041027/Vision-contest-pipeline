"""Prediction converters: framework / contest output -> canonical Prediction list."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.dataset import Dataset
from vcp.measure.schema import Prediction


@dataclass(frozen=True)
class ConvertContext:
    dataset: Dataset
    subset_ids: set[str]
    export_dir: Path | None = None  # the `vcp data export` directory the predictions came from
    options: dict[str, str] = field(default_factory=dict)


class Converter(Protocol):
    name: str
    version: str

    def convert(self, src: Path, ctx: ConvertContext) -> list[Prediction]: ...


CONVERTERS: dict[str, Converter] = {}


def register_converter(converter: Converter) -> None:
    if converter.name in CONVERTERS:
        raise RegistryError(f"converter {converter.name!r} already registered")
    CONVERTERS[converter.name] = converter


def get_converter(name: str) -> Converter:
    try:
        return CONVERTERS[name]
    except KeyError:
        raise RegistryError(f"unknown converter {name!r}; known: {sorted(CONVERTERS)}") from None


def parse_mapping(value: str | None, option: str) -> dict[str, str]:
    """``a:b,c:d`` -> {"a": "b", "c": "d"}; malformed -> ValidationFailed."""
    out: dict[str, str] = {}
    for item in (value or "").split(","):
        if not item.strip():
            continue
        src, sep, dst = item.partition(":")
        if not sep or not src.strip() or not dst.strip():
            raise ValidationFailed(f"--opt {option}= expects a:b,c:d pairs, got {item!r}")
        out[src.strip()] = dst.strip()
    return out
