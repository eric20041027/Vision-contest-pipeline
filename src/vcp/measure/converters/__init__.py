"""Converter registry. Importing this package registers the built-in converters."""

from vcp.measure.converters.base import (
    CONVERTERS,
    ConvertContext,
    Converter,
    get_converter,
    parse_mapping,
    register_converter,
)
from vcp.measure.converters.jsonl import JsonlConverter
from vcp.measure.converters.scores_csv import ScoresCsvConverter

register_converter(JsonlConverter())
register_converter(ScoresCsvConverter())

__all__ = [
    "CONVERTERS",
    "ConvertContext",
    "Converter",
    "JsonlConverter",
    "ScoresCsvConverter",
    "get_converter",
    "parse_mapping",
    "register_converter",
]
