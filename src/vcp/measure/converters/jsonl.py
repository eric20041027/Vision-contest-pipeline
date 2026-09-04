"""Canonical jsonl pass-through (validated on read)."""

from __future__ import annotations

from pathlib import Path

from vcp.measure.converters.base import ConvertContext
from vcp.measure.predictions import read_predictions
from vcp.measure.schema import Prediction


class JsonlConverter:
    name = "jsonl"
    version = "1"

    def convert(self, src: Path, ctx: ConvertContext) -> list[Prediction]:
        return read_predictions(src)
