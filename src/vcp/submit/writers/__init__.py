"""Writer registry. Importing this package registers the built-in writers."""

from vcp.submit.writers.base import (
    WRITERS,
    WriteContext,
    Writer,
    WriteResult,
    get_writer,
    register_writer,
    writer_for,
)
from vcp.submit.writers.coco_results import CocoResultsWriter
from vcp.submit.writers.csv_boxes import CsvBoxesWriter
from vcp.submit.writers.scores_csv import ScoresCsvWriter

register_writer(ScoresCsvWriter())
register_writer(CocoResultsWriter())
register_writer(CsvBoxesWriter())

__all__ = [
    "WRITERS",
    "CocoResultsWriter",
    "CsvBoxesWriter",
    "ScoresCsvWriter",
    "WriteContext",
    "WriteResult",
    "Writer",
    "get_writer",
    "register_writer",
    "writer_for",
]
