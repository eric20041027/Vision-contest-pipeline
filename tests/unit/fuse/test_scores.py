import pytest

from helpers import ML_CATS, REG_CATS, make_card, multilabel_samples, regression_samples
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.fuse.fusers import FuseContext, MemberPredictions, get_fuser, require_payload
from vcp.fuse.fusers.scores import normalised_ranks
from vcp.measure.schema import Prediction


def _ctx(task: str, samples, cats) -> FuseContext:
    ds = Dataset.from_parts(make_card(task, categories=cats), samples)
    return FuseContext(
        dataset=ds,
        subset="valA",
        ids=[s.sample_id for s in samples],
        samples={s.sample_id: s for s in samples},
        params={},
    )


def _scores_member(run, weight, rows: dict[str, dict[str, float]]) -> MemberPredictions:
    return MemberPredictions(
        run_id=run,
        weight=weight,
        predictions={sid: Prediction(sample_id=sid, scores=v) for sid, v in rows.items()},
    )


def test_mean_is_weighted_per_key():
    samples = multilabel_samples(2, seed=0)
    ids = [s.sample_id for s in samples]
    a = _scores_member(
        "a",
        1.0,
        {
            ids[0]: {"acl": 0.2, "mcl": 1.0, "effusion": 0.0},
            ids[1]: {"acl": 0.5, "mcl": 0.5, "effusion": 0.5},
        },
    )
    b = _scores_member(
        "b",
        3.0,
        {
            ids[0]: {"acl": 0.8, "mcl": 0.0, "effusion": 0.0},
            ids[1]: {"acl": 0.5, "mcl": 0.5, "effusion": 0.5},
        },
    )
    out = get_fuser("mean").fuse([a, b], _ctx("multilabel", samples, ML_CATS))
    assert [p.sample_id for p in out] == ids
    assert out[0].scores == pytest.approx({"acl": 0.65, "mcl": 0.25, "effusion": 0.0})
    assert out[1].scores == pytest.approx({"acl": 0.5, "mcl": 0.5, "effusion": 0.5})


def test_mean_fuses_targets_for_regression():
    samples = regression_samples(2, seed=0)
    ids = [s.sample_id for s in samples]
    a = MemberPredictions(
        "a", 1.0, {i: Prediction(sample_id=i, targets={"age": 40.0}) for i in ids}
    )
    b = MemberPredictions(
        "b", 1.0, {i: Prediction(sample_id=i, targets={"age": 50.0}) for i in ids}
    )
    out = get_fuser("mean").fuse([a, b], _ctx("regression", samples, REG_CATS))
    assert all(p.targets == {"age": 45.0} and p.scores is None for p in out)


def test_missing_sample_or_mismatched_keys_fail_located():
    samples = multilabel_samples(2, seed=0)
    ids = [s.sample_id for s in samples]
    full = {"acl": 0.1, "mcl": 0.2, "effusion": 0.3}
    a = _scores_member("a", 1.0, {ids[0]: full, ids[1]: full})
    b = _scores_member("b", 1.0, {ids[0]: full})
    with pytest.raises(ValidationFailed, match="no prediction") as ei:
        get_fuser("mean").fuse([a, b], _ctx("multilabel", samples, ML_CATS))
    assert ei.value.fields == {"member": "b", "sample": ids[1]}
    c = _scores_member("c", 1.0, {ids[0]: full, ids[1]: {"acl": 0.1, "mcl": 0.2}})
    with pytest.raises(ValidationFailed, match="keys") as ei:
        get_fuser("mean").fuse([a, c], _ctx("multilabel", samples, ML_CATS))
    assert ei.value.fields == {"member": "c", "sample": ids[1]}


def test_normalised_ranks():
    assert normalised_ranks([0.1, 0.5, 0.9]) == [0.0, 0.5, 1.0]
    assert normalised_ranks([0.9, 0.9, 0.1]) == [0.75, 0.75, 0.0]  # tie -> average rank 2.5
    assert normalised_ranks([0.3]) == [0.5]
    assert normalised_ranks([2.0, 2.0]) == [0.5, 0.5]


def test_rank_mean_uses_subset_as_population():
    samples = multilabel_samples(3, seed=0)
    ids = [s.sample_id for s in samples]
    zero = {"mcl": 0.0, "effusion": 0.0}
    a = _scores_member(
        "a",
        1.0,
        {
            ids[0]: {"acl": 0.1, **zero},
            ids[1]: {"acl": 0.5, **zero},
            ids[2]: {"acl": 0.9, **zero},
        },
    )
    b = _scores_member(
        "b",
        1.0,
        {
            ids[0]: {"acl": 0.9, **zero},
            ids[1]: {"acl": 0.9, **zero},
            ids[2]: {"acl": 0.1, **zero},
        },
    )
    out = get_fuser("rank_mean").fuse([a, b], _ctx("multilabel", samples, ML_CATS))
    assert [p.scores["acl"] for p in out] == pytest.approx([0.375, 0.625, 0.5])
    assert all(p.scores["mcl"] == 0.5 and p.scores["effusion"] == 0.5 for p in out)
    # weights: member a x3 -> (3*0 + 0.75) / 4 for the first sample
    out = get_fuser("rank_mean").fuse(
        [MemberPredictions("a", 3.0, a.predictions), b], _ctx("multilabel", samples, ML_CATS)
    )
    assert out[0].scores["acl"] == pytest.approx(0.75 / 4)


def test_rank_mean_refuses_targets_and_wbf_refuses_scores():
    with pytest.raises(ValidationFailed) as ei:
        require_payload(get_fuser("rank_mean"), "regression")
    assert ei.value.fields == {"method": "rank_mean", "payload": "targets"}
    assert require_payload(get_fuser("mean"), "regression") == "targets"
    assert require_payload(get_fuser("mean"), "cls") == "scores"
    with pytest.raises(ValidationFailed):
        require_payload(get_fuser("wbf"), "multilabel")
