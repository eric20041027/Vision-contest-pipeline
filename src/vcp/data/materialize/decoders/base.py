"""Decoder contract and registry. One decoder per file family, chosen by suffix."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from vcp.core.errors import RegistryError


@dataclass(frozen=True)
class Decoded:
    """Decoded pixels plus what a consumer needs to display or window them."""

    array: np.ndarray
    info: dict[str, Any] = field(default_factory=dict)


class Decoder(Protocol):
    name: str
    version: str

    def decode(self, path: Path, *, exif_policy: str = "stored") -> Decoded: ...

    def decode_series(self, paths: list[Path], *, exif_policy: str = "stored") -> Decoded: ...


DECODERS: dict[str, Decoder] = {}
_BY_SUFFIX: dict[str, str] = {".dcm": "dicom"}
DEFAULT_DECODER = "image"


def register_decoder(decoder: Decoder) -> None:
    if decoder.name in DECODERS:
        raise RegistryError(f"decoder {decoder.name!r} already registered")
    DECODERS[decoder.name] = decoder


def get_decoder(name: str) -> Decoder:
    try:
        return DECODERS[name]
    except KeyError:
        raise RegistryError(f"unknown decoder {name!r}; known: {sorted(DECODERS)}") from None


def decoder_for(path: Path, override: str | None = None) -> Decoder:
    """Registry lookup by suffix (``.dcm`` -> dicom, else image) unless ``override`` names one."""
    if override:
        return get_decoder(override)
    return get_decoder(_BY_SUFFIX.get(path.suffix.lower(), DEFAULT_DECODER))
