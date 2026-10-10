"""Uncertainty bands of adaptive provenance policy v2 (spec 2026-10-09 §4.2).

A band says how sure the policy must be before it trusts the cheaper incremental estimate.
Every width comes from calibration residuals; nothing here is a tuned threshold. The decade
rule of the stratified band is a fixed grouping rule, not a fitted boundary.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BandKind = Literal["relative", "stratified_edges"]
BAND_KINDS: tuple[BandKind, ...] = ("relative", "stratified_edges")
CostSide = Literal["incremental", "full"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class RelativeBand(_Strict):
    """Candidate A: each estimate is trusted to within its calibrated relative RMSE."""

    kind: Literal["relative"] = "relative"
    incremental_relative_rmse: float = Field(ge=0.0)
    full_relative_rmse: float = Field(ge=0.0)


class EdgesStratum(_Strict):
    decade: int = Field(ge=0)
    incremental_rmse_ms: float = Field(ge=0.0)
    full_rmse_ms: float = Field(ge=0.0)
    observations: int = Field(ge=1)


class StratifiedEdgesBand(_Strict):
    """Candidate B: an absolute RMSE per order of magnitude of ``total_edges``."""

    kind: Literal["stratified_edges"] = "stratified_edges"
    strata: tuple[EdgesStratum, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _increasing_decades(self) -> StratifiedEdgesBand:
        decades = [stratum.decade for stratum in self.strata]
        if decades != sorted(set(decades)):
            raise ValueError("strata decades must be unique and increasing")
        return self

    def stratum_for(self, total_edges: int) -> EdgesStratum:
        """This size's decade, else the nearest one; a tie takes the larger decade, whose
        RMSE is the larger and so the more cautious."""
        decade = edges_decade(total_edges)
        return min(self.strata, key=lambda stratum: (abs(stratum.decade - decade), -stratum.decade))


Band = Annotated[RelativeBand | StratifiedEdgesBand, Field(discriminator="kind")]


def edges_decade(total_edges: int) -> int:
    """``floor(log10(total_edges))`` from the digit count, so no float rounding; fewer than one
    edge is decade 0."""
    if total_edges < 1:
        return 0
    return len(str(int(total_edges))) - 1


def width(
    band: RelativeBand | StratifiedEdgesBand,
    side: CostSide,
    predicted_ms: float,
    total_edges: int,
) -> float:
    """The band's half-width around one estimate, in milliseconds."""
    if isinstance(band, RelativeBand):
        ratio = band.incremental_relative_rmse if side == "incremental" else band.full_relative_rmse
        return ratio * predicted_ms
    stratum = band.stratum_for(total_edges)
    return stratum.incremental_rmse_ms if side == "incremental" else stratum.full_rmse_ms


def band_allows_incremental(
    band: RelativeBand | StratifiedEdgesBand,
    incremental_ms: float,
    full_ms: float,
    total_edges: int,
) -> bool:
    """INCREMENTAL only when the two estimates stay apart at the band's edges; a tie is FULL.
    A relative full band of 1 or more never lets incremental win -- the safe outcome."""
    if isinstance(band, RelativeBand):
        return incremental_ms * (1.0 + band.incremental_relative_rmse) < full_ms * (
            1.0 - band.full_relative_rmse
        )
    stratum = band.stratum_for(total_edges)
    return incremental_ms + stratum.incremental_rmse_ms < full_ms - stratum.full_rmse_ms


def _pairs(predicted: Sequence[float], actual: Sequence[float]) -> None:
    if not predicted or len(predicted) != len(actual):
        raise ValueError("residuals need equally many, and some, estimates and measurements")


def rmse(predicted: Sequence[float], actual: Sequence[float]) -> float:
    _pairs(predicted, actual)
    total = sum((a - p) ** 2 for p, a in zip(predicted, actual, strict=True))
    return math.sqrt(total / len(predicted))


def relative_rmse(predicted: Sequence[float], actual: Sequence[float]) -> float:
    """RMSE of ``(actual - estimate) / estimate``: the band multiplies the estimate."""
    _pairs(predicted, actual)
    if any(p <= 0 for p in predicted):
        raise ValueError("relative residuals need positive estimates")
    total = sum(((a - p) / p) ** 2 for p, a in zip(predicted, actual, strict=True))
    return math.sqrt(total / len(predicted))


def fit_band(
    kind: str,
    *,
    total_edges: Sequence[int],
    incremental_predicted: Sequence[float],
    incremental_actual: Sequence[float],
    full_predicted: Sequence[float],
    full_actual: Sequence[float],
) -> RelativeBand | StratifiedEdgesBand:
    """One band of ``kind`` from paired estimates and measurements of the same observations."""
    sizes = {
        len(total_edges),
        len(incremental_predicted),
        len(incremental_actual),
        len(full_predicted),
        len(full_actual),
    }
    if len(sizes) != 1:
        raise ValueError("every series must cover the same observations")
    if kind == "relative":
        return RelativeBand(
            incremental_relative_rmse=relative_rmse(incremental_predicted, incremental_actual),
            full_relative_rmse=relative_rmse(full_predicted, full_actual),
        )
    if kind == "stratified_edges":
        groups: dict[int, list[int]] = {}
        for index, edges in enumerate(total_edges):
            groups.setdefault(edges_decade(edges), []).append(index)
        return StratifiedEdgesBand(
            strata=tuple(
                EdgesStratum(
                    decade=decade,
                    incremental_rmse_ms=rmse(
                        [incremental_predicted[i] for i in indices],
                        [incremental_actual[i] for i in indices],
                    ),
                    full_rmse_ms=rmse(
                        [full_predicted[i] for i in indices], [full_actual[i] for i in indices]
                    ),
                    observations=len(indices),
                )
                for decade, indices in sorted(groups.items())
            )
        )
    raise ValueError(f"unknown band kind {kind!r}")
