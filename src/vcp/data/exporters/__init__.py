"""Exporter registry. Importing this package registers the built-in exporters."""

from vcp.data.exporters.base import (
    EXPORTERS,
    Exporter,
    ExportOutput,
    ExportResult,
    ExportSpec,
    export_subset,
    get_exporter,
    register_exporter,
    select_view,
)
from vcp.data.exporters.coco import CocoExporter
from vcp.data.exporters.yolo import YoloExporter

register_exporter(CocoExporter())
register_exporter(YoloExporter())

__all__ = [
    "EXPORTERS",
    "CocoExporter",
    "ExportOutput",
    "ExportResult",
    "ExportSpec",
    "Exporter",
    "YoloExporter",
    "export_subset",
    "get_exporter",
    "register_exporter",
    "select_view",
]
