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
from vcp.submit.writers.scores_csv import ScoresCsvWriter

register_writer(ScoresCsvWriter())

__all__ = [
    "WRITERS",
    "ScoresCsvWriter",
    "WriteContext",
    "WriteResult",
    "Writer",
    "get_writer",
    "register_writer",
    "writer_for",
]
