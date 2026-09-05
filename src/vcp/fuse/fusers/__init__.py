"""Fuser registry. Importing this package registers the built-in fusers in a fixed order."""

from vcp.fuse.fusers.base import (
    FUSERS,
    FuseContext,
    Fuser,
    MemberPredictions,
    choice_param,
    effective_params,
    float_param,
    get_fuser,
    int_param,
    register_fuser,
    require_payload,
    resolve_params,
)
from vcp.fuse.fusers.scores import Mean, RankMean
from vcp.fuse.fusers.wbf import Wbf

for _fuser in (Wbf(), Mean(), RankMean()):
    register_fuser(_fuser)

__all__ = [
    "FUSERS",
    "FuseContext",
    "Fuser",
    "MemberPredictions",
    "choice_param",
    "effective_params",
    "float_param",
    "get_fuser",
    "int_param",
    "register_fuser",
    "require_payload",
    "resolve_params",
]
