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
from vcp.data.importers.jsonl import JsonlImporter

register_importer(JsonlImporter())

__all__ = [
    "IMPORTERS",
    "Importer",
    "ImportResult",
    "ImportSpec",
    "JsonlImporter",
    "finalize_import",
    "get_importer",
    "register_importer",
]
