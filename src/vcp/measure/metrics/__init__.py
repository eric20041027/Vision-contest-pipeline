"""Metric registry. Importing this package registers the built-in metrics in a fixed order."""

from vcp.measure.metrics.base import (
    METRICS,
    Metric,
    applicable_metrics,
    effective_params,
    get_metric,
    params_key,
    register_metric,
)
from vcp.measure.metrics.coco_map import CocoMap
from vcp.measure.metrics.tabular import Accuracy, LogLoss, MacroAuc, MacroF1, Mae, Rmse

for _metric in (CocoMap(), Accuracy(), MacroF1(), LogLoss(), MacroAuc(), Rmse(), Mae()):
    register_metric(_metric)

__all__ = [
    "METRICS",
    "Metric",
    "applicable_metrics",
    "effective_params",
    "get_metric",
    "params_key",
    "register_metric",
]
