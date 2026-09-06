"""Training layer: wrap any training command, record its identity, keep checkpoints backed up."""

from vcp.train.reader import MaterializedReader, Record
from vcp.train.session import Session

__all__ = ["MaterializedReader", "Record", "Session"]
