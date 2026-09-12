import json

import pytest

from helpers import det_samples, make_card
from vcp.core.errors import IntegrityError, PlanMismatchError, ValidationFailed
from vcp.data.dataset import Dataset
from vcp.measure.predictions import write_predictions
from vcp.measure.runs import (
    append_history,
    assert_run_matches,
    load_run,
    prediction_path,
    run_dir,
    save_run,
    verify_prediction,
)
from vcp.measure.schema import Prediction, PredictionFile, RunCard, RunSource


def _card(**kw):
    base = dict(
        run_id="r1",
        dataset="ds",
        samples_hash="h",
        plan_id="p",
        trained_on=["train"],
        source=RunSource(framework="t"),
        created_at="2026-09-04T00:00:00.000Z",
    )
    return RunCard(**{**base, **kw})


def test_save_load_and_verify(roots):
    card = _card()
    save_run(roots.data, card)
    assert run_dir(roots.data, "r1") == roots.data / "runs" / "r1"
    assert load_run(roots.data, "r1") == card
    with pytest.raises(ValidationFailed, match="run not found"):
        load_run(roots.data, "nope")
    path = prediction_path(roots.data, "r1", "valA")
    sha = write_predictions(path, [Prediction(sample_id="a", boxes=[])])
    card = card.model_copy(
        update={
            "predictions": {
                "valA": PredictionFile(
                    path="predictions/valA.jsonl",
                    sha256=sha,
                    samples=1,
                    empty=0,
                    format_in="jsonl",
                    ingested_at="2026-09-04T00:00:00.000Z",
                )
            }
        }
    )
    save_run(roots.data, card)
    assert verify_prediction(roots.data, card, "valA") == path
    with pytest.raises(ValidationFailed, match="no predictions for subset"):
        verify_prediction(roots.data, card, "valB")
    path.write_text('{"sample_id": "a", "boxes": []}\n\n', encoding="utf-8", newline="\n")
    with pytest.raises(IntegrityError):
        verify_prediction(roots.data, card, "valA")
    # The clock wins over a caller-supplied ts: this log's timestamps prove ordering.
    append_history(
        roots.data,
        "r1",
        {"event": "replace", "subset": "valA", "old_sha": sha, "ts": "1999-01-01T00:00:00.000Z"},
    )
    lines = (roots.data / "runs" / "r1" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "replace" and row["old_sha"] == sha
    assert row["ts"].endswith("Z") and row["ts"] != "1999-01-01T00:00:00.000Z"


def test_assert_run_matches_dataset_name_and_hash(roots):
    """Task 5 ruling 4: the run-vs-dataset check lives here once so Task 9 can reuse it."""
    ds = Dataset.from_parts(make_card("det", name="tiny"), det_samples(5, seed=0))
    card = _card(run_id="r1", dataset="tiny", samples_hash=ds.card.samples_hash)
    assert_run_matches(card, ds.card)  # no error: name and hash both agree

    wrong_name = _card(run_id="r1", dataset="other", samples_hash=ds.card.samples_hash)
    with pytest.raises(PlanMismatchError, match="dataset"):
        assert_run_matches(wrong_name, ds.card)

    wrong_hash = _card(run_id="r1", dataset="tiny", samples_hash="stale-hash")
    with pytest.raises(PlanMismatchError, match="samples_hash"):
        assert_run_matches(wrong_hash, ds.card)
