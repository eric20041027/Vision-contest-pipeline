import pytest

from helpers import noisy_predictions
from submit_fixtures import (
    EVAL,
    STAMP,
    TEST,
    ingest_run,
    seed_eval_runs,
    seed_judgements,
    seed_test_runs,
)
from vcp.core.errors import ValidationFailed
from vcp.core.time import utc_now
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.submit.actions import record, score
from vcp.submit.final import count_unseals, final, lock, rank_key, unlock
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import init_profile
from vcp.submit.schema import FinalEntry, PlatformProfile
from vcp.submit.stage import StageSpec, load_staged, stage


def _profile(**over) -> PlatformProfile:
    base = dict(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


def _kw(pair):
    return {"data_root": pair.roots.data, "configs_root": pair.roots.configs}


def _measure_holdout(pair, run):
    measure_run(
        MeasureSpec(
            run_id=run,
            metrics=["accuracy"],
            subsets=["holdout"],
            unseal=True,
            reason="final pick",
            **_kw(pair),
        )
    )


@pytest.fixture
def uploaded(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(_profile(), **_kw(pair))
    seed_test_runs(pair)
    for sid, eval_run, test_run, kind in (
        ("S1", "good", "good.test", "candidate"),
        ("S2", "bad", "bad.test", "baseline"),
        ("S3", "bad", "bad.test", "probe"),
    ):
        stage(
            StageSpec(
                dataset=TEST,
                submission_id=sid,
                eval_run=eval_run,
                test_run=test_run,
                kind=kind,
                reason=None if kind == "candidate" else "why",
                **_kw(pair),
            )
        )
        now = utc_now().strftime("%Y-%m-%d %H:%M:%S")
        record(TEST, sid, now, tz="utc", **_kw(pair))
    score(TEST, "S1", public=0.8, **_kw(pair))
    score(TEST, "S2", public=0.9, **_kw(pair))
    return pair


def test_final_needs_sealed_readings(uploaded):
    with pytest.raises(ValidationFailed, match="no_sealed_readings"):
        final(TEST, **_kw(uploaded))
    assert SubmissionLedger(uploaded.test_paths.submissions_log).of("final") == []


def test_final_picks_by_sealed_not_public(uploaded):
    _measure_holdout(uploaded, "good")
    _measure_holdout(uploaded, "bad")
    dry = final(TEST, dry_run=True, **_kw(uploaded))
    assert dry.chosen == ["S1"] and not dry.written and dry.needs_reupload == "S1"
    led = SubmissionLedger(uploaded.test_paths.submissions_log)
    assert led.of("final") == [] and led.lock_state() is None
    res = final(TEST, **_kw(uploaded))
    assert res.written and res.chosen == ["S1"] and res.unranked == []
    table = {e.submission_id: e for e in res.row.table}
    assert table["S1"].eligible and table["S1"].sealed_value == 1.0 and table["S1"].public == 0.8
    assert table["S2"].eligible and table["S2"].sealed_value < 1.0 and table["S2"].public == 0.9
    assert not table["S3"].eligible and table["S3"].why == "probe"
    assert res.row.holdout_unseals >= 3 and res.row.metric == "accuracy"
    for sid in ("S1", "S2", "S3"):
        assert table[sid].staged_at == load_staged(uploaded.test_paths, sid).staged_at
    led = SubmissionLedger(uploaded.test_paths.submissions_log)
    assert led.lock_state().reason == "final" and led.latest_final().chosen == ["S1"]
    with pytest.raises(ValidationFailed, match="locked"):
        final(TEST, **_kw(uploaded))
    with pytest.raises(ValidationFailed, match="locked"):
        stage(
            StageSpec(
                dataset=TEST,
                submission_id="S4",
                eval_run="good",
                test_run="good.test",
                **_kw(uploaded),
            )
        )
    now = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    out = record(TEST, "S1", now, tz="utc", **_kw(uploaded))
    assert out.row.submission_id == "S1"
    with pytest.raises(ValidationFailed, match="locked"):
        record(TEST, "S2", now, tz="utc", **_kw(uploaded))


def test_stale_and_slots(uploaded):
    _measure_holdout(uploaded, "good")
    _measure_holdout(uploaded, "bad")
    ds, plan = uploaded.eval_ds, uploaded.eval_plan
    samples = ds.subset("holdout", plan, unseal=True, reason="t", paths=uploaded.eval_paths)
    ingest_run(
        uploaded,
        "good",
        ds,
        "fixed-v1",
        "holdout",
        noisy_predictions(samples, ds.card, seed=42, flip=1.0),
        weights=uploaded.weights["good"],
        trained_on=("train",),
        replace=True,
    )
    res = final(TEST, slots=2, dry_run=True, **_kw(uploaded))
    assert res.unranked == ["S1"] and res.chosen == ["S2"]
    table = {e.submission_id: e for e in res.row.table}
    assert table["S1"].why == "stale_reading"
    assert count_unseals(uploaded.eval_paths, "fixed-v1", "holdout") >= 4


def test_lock_and_unlock(uploaded):
    row = lock(TEST, "R0 protocol", **_kw(uploaded))
    assert row.event == "lock" and row.reason == "R0 protocol"
    with pytest.raises(ValidationFailed, match="locked"):
        lock(TEST, "again", **_kw(uploaded))
    assert unlock(TEST, "deadline extended", **_kw(uploaded)).event == "unlock"
    with pytest.raises(ValidationFailed, match="not_locked"):
        unlock(TEST, "again", **_kw(uploaded))


def test_final_rejects_slots_below_one(uploaded):
    with pytest.raises(ValidationFailed, match="slots: must be >= 1"):
        final(TEST, slots=0, dry_run=True, **_kw(uploaded))
    with pytest.raises(ValidationFailed, match="slots: must be >= 1"):
        final(TEST, slots=-1, dry_run=True, **_kw(uploaded))
    assert SubmissionLedger(uploaded.test_paths.submissions_log).of("final") == []


def _entry(sid: str, sealed: float | None, public: float | None, staged_at: str) -> FinalEntry:
    return FinalEntry(
        submission_id=sid,
        eligible=True,
        why="",
        sealed_value=sealed,
        public=public,
        staged_at=staged_at,
    )


def test_rank_key():
    t1, t2 = "2026-01-01T00:00:00.000Z", "2026-01-02T00:00:00.000Z"

    # sign +1: higher sealed first, then higher public, then earlier staged_at.
    entries = [
        _entry("a", 0.5, 0.1, t1),
        _entry("b", 0.9, 0.2, t2),
        _entry("c", 0.9, 0.2, t1),
        _entry("d", 0.9, 0.4, t1),
    ]
    ranked = sorted(entries, key=rank_key(1.0))
    assert [e.submission_id for e in ranked] == ["d", "c", "b", "a"]

    # sign -1: lower sealed first, then LOWER public.
    entries2 = [
        _entry("a", 0.9, 0.1, t1),
        _entry("b", 0.5, 0.4, t1),
        _entry("c", 0.5, 0.2, t1),
    ]
    ranked2 = sorted(entries2, key=rank_key(-1.0))
    assert [e.submission_id for e in ranked2] == ["c", "b", "a"]

    # public=None sorts after any number, whichever direction the metric goes.
    tied = [_entry("has_public", 0.5, 0.3, t1), _entry("no_public", 0.5, None, t1)]
    assert [e.submission_id for e in sorted(tied, key=rank_key(1.0))] == [
        "has_public",
        "no_public",
    ]
    assert [e.submission_id for e in sorted(tied, key=rank_key(-1.0))] == [
        "has_public",
        "no_public",
    ]
