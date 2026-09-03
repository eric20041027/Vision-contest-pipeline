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
from vcp.data.importers.csv_boxes import CsvBoxesImporter
from vcp.data.importers.jsonl import JsonlImporter

register_importer(JsonlImporter())
register_importer(CsvBoxesImporter())

__all__ = [
    "IMPORTERS",
    "CsvBoxesImporter",
    "Importer",
    "ImportResult",
    "ImportSpec",
    "JsonlImporter",
    "finalize_import",
    "get_importer",
    "register_importer",
]
