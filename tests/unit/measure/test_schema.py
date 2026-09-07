import pytest
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.measure.schema import (
    Anchor,
    PredBox,
    Prediction,
    PredMask,
    PreRegistration,
    RunCard,
    RunSource,
    payload_field,
)


def test_payload_field_per_task():
    assert payload_field("det") == "boxes"
    assert payload_field("seg") == "masks"
    assert payload_field("cls") == "scores" and payload_field("multilabel") == "scores"
    assert payload_field("regression") == "targets"
    with pytest.raises(ValidationFailed):
        payload_field("pose")


def test_prediction_payload_rules():
    p = Prediction(sample_id="a", boxes=[PredBox(x=0, y=0, w=1, h=1, category_id=0, score=0.5)])
    assert p.payload_field() == "boxes"
    assert Prediction(sample_id="b", boxes=[]).payload_field() == "boxes"
    with pytest.raises(ValidationError):
        Prediction(sample_id="c")  # no payload at all
    with pytest.raises(ValidationError):
        Prediction(sample_id="d", boxes=[], scores={"x": 0.1})  # two payloads
    with pytest.raises(ValidationError):
        PredBox(x=0, y=0, w=1, h=1, category_id=0, score=1.5)
    with pytest.raises(ValidationError):
        PredMask(category_id=0, score=0.5)  # needs rle or polygon
    with pytest.raises(ValidationError):
        PredMask(category_id=0, score=0.5, polygon=[[0, 0, float("nan"), 0, 2, 2]])
    with pytest.raises(ValidationError):
        Prediction(sample_id="e", scores={"x": float("nan")})


def test_run_card_and_prereg_defaults():
    card = RunCard(
        run_id="r1",
        dataset="ds",
        samples_hash="abc",
        plan_id="p",
        trained_on=["train"],
        source=RunSource(framework="test"),
        created_at="2026-09-04T00:00:00.000Z",
    )
    assert card.predictions == {} and card.source.notes == ""
    pr = PreRegistration(
        prereg_id="p001",
        claim="x",
        component="c",
        component_class="tuning",
        baseline_run="a",
        candidate_run="b",
        metric="accuracy",
        subsets=["valA", "valB"],
        created_at="2026-09-04T00:00:00.000Z",
    )
    assert (pr.t_min, pr.min_bases, pr.sigma_method, pr.sigma_ratio) == (2.0, 2, "splithalf", 1.0)
    with pytest.raises(ValidationError):
        PreRegistration(**{**pr.model_dump(), "component_class": "other"})


def _anchor(**update):
    base = dict(
        run_id="r", reading_id="x", value=0.5, tolerance=1e-6, set_at="2026-09-04T00:00:00.000Z"
    )
    return Anchor(**{**base, **update})


def test_anchor_tolerance_must_be_finite_and_nonnegative():
    """I1: a nan/inf tolerance silently disables the guardrail (any drift is `<= tolerance`
    for inf, and the nan comparison is always False), and a negative one jams it (nothing is
    ever within tolerance). 0.0 -- exact reproduction required -- must stay legal."""
    assert _anchor(tolerance=0.0).tolerance == 0.0
    for bad in (float("nan"), float("inf"), float("-inf"), -1.0):
        with pytest.raises(ValidationError):
            _anchor(tolerance=bad)


PREREG_BASE = dict(
    prereg_id="p001",
    claim="x",
    component="c",
    component_class="model",
    baseline_run="a",
    candidate_run="b",
    metric="coco_map",
    subsets=["valA", "valB"],
    created_at="2026-09-04T00:00:00.000Z",
)


def test_prereg_threshold_must_be_finite():
    """I1-class defect (mirrors Anchor.tolerance): a nan/inf t_min or sigma_ratio silently
    un-binds the bar it names (`t >= nan` is False for every subset, so nothing ever counts as
    a base, and `mean_delta < nan` is False too, i.e. a vacuous PASS with the sigma_p bar
    silently switched off)."""
    for field in ("t_min", "sigma_ratio"):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValidationError):
                PreRegistration(**{**PREREG_BASE, field: bad})


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("min_bases", 0),
        ("min_bases", -1),
        ("sigma_ratio", 0.0),
        ("sigma_ratio", -5.0),
        ("t_min", -0.5),
    ],
)
def test_prereg_thresholds_must_be_bars_at_all(field, bad):
    """3-10: a threshold that cannot be missed is not pre-registration, it is decoration.
    `min_bases 0` passes with no positive base at all, `sigma_ratio <= 0` makes
    `mean_delta >= ratio * sigma_p` true for any improvement (and for none), and a negative
    `t_min` counts a subset the bootstrap says is going the wrong way. Milder than nan, and
    visible in a yaml already committed to git -- which is why it is caught at the boundary."""
    with pytest.raises(ValidationError):
        PreRegistration(**{**PREREG_BASE, field: bad})


def test_prereg_thresholds_keep_their_legal_edges():
    """The boundary values a real claim uses must stay legal: t_min = 0 (any positive delta
    counts), min_bases = 1 (a single-subset claim) and a fractional sigma_ratio."""
    pr = PreRegistration(**{**PREREG_BASE, "t_min": 0.0, "min_bases": 1, "sigma_ratio": 0.5})
    assert (pr.t_min, pr.min_bases, pr.sigma_ratio) == (0.0, 1, 0.5)
