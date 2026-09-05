"""Fuser protocol and registry (spec 7.1): one axis, the same shape as the metric registry.

A fuser declares which prediction payloads it can fuse; the dataset's task decides which payload
its predictions carry (``TaskSpec.pred_payload``), so applicability is derived from two
registries and never from a hand-written table. Params arrive as strings (a recipe is yaml in
git); the typed helpers below turn them into numbers with a located, machine-readable failure.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample
from vcp.measure.schema import PAYLOAD_FIELDS, Prediction, payload_field


@dataclass(frozen=True)
class MemberPredictions:
    """One member's predictions for one subset, keyed by sample id."""

    run_id: str
    weight: float
    predictions: dict[str, Prediction]


@dataclass(frozen=True)
class FuseContext:
    """What every fuser call shares: the subset's ids in dataset order, the samples (for view
    sizes), and the recipe's effective params. Never labels -- a fuser does not read gold."""

    dataset: Dataset
    subset: str
    ids: list[str]
    samples: dict[str, Sample]
    params: dict[str, str]


class Fuser(Protocol):
    name: str
    version: str
    payloads: frozenset[str]
    defaults: dict[str, str]

    def check_params(self, params: dict[str, str]) -> None:
        """Raise ``ValidationFailed`` for a value this fuser cannot use."""
        ...

    def fuse(self, members: list[MemberPredictions], ctx: FuseContext) -> list[Prediction]: ...


FUSERS: dict[str, Fuser] = {}


def register_fuser(fuser: Fuser) -> None:
    if fuser.name in FUSERS:
        raise RegistryError(f"fuser {fuser.name!r} already registered")
    bad = sorted(set(fuser.payloads) - set(PAYLOAD_FIELDS))
    if bad or not fuser.payloads:
        raise RegistryError(
            f"fuser {fuser.name!r} declares payloads {sorted(fuser.payloads)}; "
            f"must be a non-empty subset of {PAYLOAD_FIELDS}"
        )
    FUSERS[fuser.name] = fuser


def get_fuser(name: str) -> Fuser:
    try:
        return FUSERS[name]
    except KeyError:
        raise RegistryError(
            f"unknown fuser {name!r}; known: {sorted(FUSERS)}", fields={"method": name}
        ) from None


def effective_params(fuser: Fuser, params: dict[str, str]) -> dict[str, str]:
    """Fuser defaults overridden by the given params; an unknown key is the user's mistake."""
    unknown = sorted(set(params) - set(fuser.defaults))
    if unknown:
        raise ValidationFailed(
            f"fuser {fuser.name!r} has no params {unknown}; known: {sorted(fuser.defaults)}",
            fields={"param": unknown[0]},
        )
    return {**fuser.defaults, **params}


def resolve_params(fuser: Fuser, params: dict[str, str]) -> dict[str, str]:
    """Effective params that the fuser has also agreed it can parse."""
    effective = effective_params(fuser, params)
    fuser.check_params(effective)
    return effective


def require_payload(fuser: Fuser, task: str) -> str:
    """The payload field this task's predictions carry, if the fuser can fuse it."""
    field = payload_field(task)
    if field not in fuser.payloads:
        raise ValidationFailed(
            f"fuser {fuser.name!r} fuses {sorted(fuser.payloads)} predictions; "
            f"task {task!r} predicts {field!r}",
            fields={"method": fuser.name, "payload": field},
        )
    return field


def _bad(key: str, value: str, what: str) -> ValidationFailed:
    return ValidationFailed(f"param {key}={value!r}: {what}", fields={"param": key})


def float_param(
    params: dict[str, str],
    key: str,
    *,
    lo: float | None = None,
    hi: float | None = None,
    lo_open: bool = False,
) -> float:
    raw = params[key]
    try:
        value = float(raw)
    except ValueError:
        raise _bad(key, raw, "not a number") from None
    if not math.isfinite(value):
        raise _bad(key, raw, "must be finite")
    if lo is not None and (value <= lo if lo_open else value < lo):
        raise _bad(key, raw, f"must be {'>' if lo_open else '>='} {lo}")
    if hi is not None and value > hi:
        raise _bad(key, raw, f"must be <= {hi}")
    return value


def int_param(params: dict[str, str], key: str, *, lo: int = 0) -> int:
    raw = params[key]
    try:
        value = int(raw)
    except ValueError:
        raise _bad(key, raw, "not an integer") from None
    if value < lo:
        raise _bad(key, raw, f"must be >= {lo}")
    return value


def choice_param(params: dict[str, str], key: str, choices: tuple[str, ...]) -> str:
    raw = params[key]
    if raw not in choices:
        raise _bad(key, raw, f"must be one of {list(choices)}")
    return raw
