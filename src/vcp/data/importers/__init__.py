"""Importer registry. Importing this package registers the built-in importers."""

from vcp.data.importers.base import (
    IMPORTERS,
    Importer,
    ImportResult,
    ImportSpec,
    finalize_import,
    get_importer,
    register_importer,
)
from vcp.data.importers.coco import CocoImporter
from vcp.data.importers.csv_boxes import CsvBoxesImporter
from vcp.data.importers.image_csv import ImageCsvImporter
from vcp.data.importers.imagefolder import ImageFolderImporter
from vcp.data.importers.jsonl import JsonlImporter
from vcp.data.importers.yolo import YoloImporter

register_importer(JsonlImporter())
register_importer(CsvBoxesImporter())
register_importer(CocoImporter())
register_importer(YoloImporter())
register_importer(ImageFolderImporter())
register_importer(ImageCsvImporter())

__all__ = [
    "IMPORTERS",
    "CocoImporter",
    "CsvBoxesImporter",
    "ImageCsvImporter",
    "ImageFolderImporter",
    "Importer",
    "ImportResult",
    "ImportSpec",
    "JsonlImporter",
    "YoloImporter",
    "finalize_import",
    "get_importer",
    "register_importer",
]
