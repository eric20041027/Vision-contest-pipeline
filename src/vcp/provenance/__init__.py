"""Dataset evolution artifacts and the disposable downstream provenance index."""

from vcp.provenance.diff import (
    DatasetComparison,
    DatasetDiffResult,
    DatasetDiffSpec,
    compare_dataset_versions,
    create_dataset_diff,
)
from vcp.provenance.schema import (
    ChangeDomain,
    ChangeType,
    DatasetDiffSummary,
    SampleChange,
    SemanticEffect,
)

__all__ = [
    "ChangeDomain",
    "ChangeType",
    "DatasetComparison",
    "DatasetDiffResult",
    "DatasetDiffSpec",
    "DatasetDiffSummary",
    "SampleChange",
    "SemanticEffect",
    "compare_dataset_versions",
    "create_dataset_diff",
]
