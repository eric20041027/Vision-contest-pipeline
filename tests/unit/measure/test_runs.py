import pytest

from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.measure.predictions import write_predictions
from vcp.measure.runs import (
    append_history,
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
    append_history(roots.data, "r1", {"event": "replace", "subset": "valA", "old_sha": sha})
    lines = (roots.data / "runs" / "r1" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and '"event": "replace"' in lines[0]
