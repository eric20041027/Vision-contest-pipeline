"""Converter registry. Importing this package registers the built-in converters."""

from vcp.measure.converters.base import (
    CONVERTERS,
    ConvertContext,
    Converter,
    get_converter,
    parse_mapping,
    register_converter,
)
from vcp.measure.converters.coco_results import CocoResultsConverter
from vcp.measure.converters.jsonl import JsonlConverter
from vcp.measure.converters.scores_csv import ScoresCsvConverter
from vcp.measure.converters.yolo_txt import YoloTxtConverter

register_converter(CocoResultsConverter())
register_converter(JsonlConverter())
register_converter(ScoresCsvConverter())
register_converter(YoloTxtConverter())

__all__ = [
    "CONVERTERS",
    "CocoResultsConverter",
    "ConvertContext",
    "Converter",
    "JsonlConverter",
    "ScoresCsvConverter",
    "YoloTxtConverter",
    "get_converter",
    "parse_mapping",
    "register_converter",
]
