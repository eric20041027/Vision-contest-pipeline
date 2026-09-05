import pytest
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.measure.schema import (
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
