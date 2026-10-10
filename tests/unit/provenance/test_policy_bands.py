"""Policy v2's uncertainty bands (spec 2026-10-09 §4.2)."""

import math

import pytest
from pydantic import ValidationError

from vcp.provenance.policy_bands import (
    BAND_KINDS,
    EdgesStratum,
    RelativeBand,
    StratifiedEdgesBand,
    band_allows_incremental,
    edges_decade,
    fit_band,
    relative_rmse,
    rmse,
    width,
)


@pytest.mark.parametrize(
    ("edges", "decade"),
    [(0, 0), (1, 0), (9, 0), (10, 1), (999, 2), (1000, 3), (1336, 3), (19982, 4), (199982, 5)],
)
def test_edges_decade_is_the_digit_count_minus_one(edges, decade):
    assert edges_decade(edges) == decade


def test_relative_band_scales_with_the_estimate():
    band = RelativeBand(incremental_relative_rmse=0.2, full_relative_rmse=0.1)
    # 100*1.2 = 120 < 150*0.9 = 135: apart even at the edges
    assert band_allows_incremental(band, 100.0, 150.0, total_edges=1500)
    # the same gap at 100x the size is still apart: v1's fixed band would not see that
    assert band_allows_incremental(band, 10_000.0, 15_000.0, total_edges=150_000)


def test_relative_band_tie_selects_full():
    band = RelativeBand(incremental_relative_rmse=0.5, full_relative_rmse=0.0)
    assert not band_allows_incremental(band, 100.0, 150.0, total_edges=1500)  # 150 < 150 is false


def test_full_relative_rmse_of_one_or_more_never_lets_incremental_win():
    band = RelativeBand(incremental_relative_rmse=0.0, full_relative_rmse=1.0)
    assert not band_allows_incremental(band, 1.0, 1_000_000.0, total_edges=1500)


def test_stratified_band_uses_the_size_decade_and_the_nearest_stratum():
    band = StratifiedEdgesBand(
        strata=(
            EdgesStratum(decade=3, incremental_rmse_ms=10.0, full_rmse_ms=20.0, observations=4),
            EdgesStratum(decade=5, incremental_rmse_ms=1000.0, full_rmse_ms=2000.0, observations=4),
        )
    )
    assert band.stratum_for(1500).decade == 3
    assert band.stratum_for(150_000).decade == 5
    assert band.stratum_for(5).decade == 3  # below the lowest: nearest
    assert band.stratum_for(10**7).decade == 5  # above the highest: nearest
    assert band.stratum_for(15_000).decade == 5  # decade 4 is one from each: the larger wins
    assert band_allows_incremental(band, 100.0, 150.0, total_edges=1500)  # 110 < 130
    assert not band_allows_incremental(band, 100.0, 150.0, total_edges=150_000)  # 1100 < -1850


def test_width_matches_each_kind():
    relative = RelativeBand(incremental_relative_rmse=0.25, full_relative_rmse=0.5)
    assert width(relative, "incremental", 200.0, 1500) == 50.0
    assert width(relative, "full", 200.0, 1500) == 100.0
    stratified = StratifiedEdgesBand(
        strata=(EdgesStratum(decade=3, incremental_rmse_ms=7.0, full_rmse_ms=9.0, observations=1),)
    )
    assert width(stratified, "incremental", 1e9, 1500) == 7.0
    assert width(stratified, "full", 1e9, 1500) == 9.0


def test_rmse_and_relative_rmse():
    assert rmse([10.0, 10.0], [13.0, 6.0]) == math.sqrt((9 + 16) / 2)
    assert relative_rmse([10.0, 20.0], [11.0, 18.0]) == math.sqrt((0.01 + 0.01) / 2)
    with pytest.raises(ValueError, match="positive"):
        relative_rmse([0.0], [1.0])
    with pytest.raises(ValueError):
        rmse([], [])
    with pytest.raises(ValueError):
        rmse([1.0], [1.0, 2.0])


def test_fit_band_relative_and_stratified():
    kwargs = dict(
        total_edges=[1500, 1600, 150_000, 160_000],
        incremental_predicted=[10.0, 10.0, 1000.0, 1000.0],
        incremental_actual=[11.0, 9.0, 1100.0, 900.0],
        full_predicted=[20.0, 20.0, 2000.0, 2000.0],
        full_actual=[22.0, 18.0, 2200.0, 1800.0],
    )
    relative = fit_band("relative", **kwargs)
    assert relative == RelativeBand(incremental_relative_rmse=0.1, full_relative_rmse=0.1)
    stratified = fit_band("stratified_edges", **kwargs)
    assert [s.decade for s in stratified.strata] == [3, 5]
    assert [s.incremental_rmse_ms for s in stratified.strata] == [1.0, 100.0]
    assert [s.full_rmse_ms for s in stratified.strata] == [2.0, 200.0]
    assert [s.observations for s in stratified.strata] == [2, 2]
    with pytest.raises(ValueError, match="band kind"):
        fit_band("absolute", **kwargs)
    with pytest.raises(ValueError):
        fit_band("relative", **{**kwargs, "total_edges": [1500]})


def test_fit_band_rejects_misaligned_measurements():
    kwargs = dict(
        total_edges=[1500, 1600, 150_000, 160_000],
        incremental_predicted=[10.0, 10.0, 1000.0, 1000.0],
        incremental_actual=[11.0, 9.0, 1100.0, 900.0],
        full_predicted=[20.0, 20.0, 2000.0, 2000.0],
        full_actual=[22.0, 18.0, 2200.0, 1800.0],
    )
    misaligned = [
        {"incremental_actual": [11.0, 9.0, 1100.0]},
        {"incremental_actual": [11.0, 9.0, 1100.0, 900.0, 1000.0]},
        {"full_actual": [22.0, 18.0, 2200.0]},
    ]
    for kind in BAND_KINDS:
        for override in misaligned:
            with pytest.raises(ValueError, match="same observations"):
                fit_band(kind, **{**kwargs, **override})


def test_band_models_are_strict():
    assert BAND_KINDS == ("relative", "stratified_edges")
    with pytest.raises(ValidationError):
        RelativeBand(incremental_relative_rmse=-0.1, full_relative_rmse=0.0)
    with pytest.raises(ValidationError):
        RelativeBand(incremental_relative_rmse=math.inf, full_relative_rmse=0.0)
    with pytest.raises(ValidationError, match="extra"):
        RelativeBand(incremental_relative_rmse=0.1, full_relative_rmse=0.1, surprise=1)
    duplicate = EdgesStratum(decade=3, incremental_rmse_ms=1.0, full_rmse_ms=1.0, observations=1)
    with pytest.raises(ValidationError, match="increasing"):
        StratifiedEdgesBand(strata=(duplicate, duplicate))
    with pytest.raises(ValidationError):
        StratifiedEdgesBand(strata=())
