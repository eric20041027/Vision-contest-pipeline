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
