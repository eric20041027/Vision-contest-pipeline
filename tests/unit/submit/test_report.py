import pytest

from submit_fixtures import EVAL, STAMP, TEST, seed_eval_runs, seed_judgements, seed_test_runs
from vcp.core.time import utc_now
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.submit.actions import record, score
from vcp.submit.final import lock
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import init_profile
from vcp.submit.report import report, status
from vcp.submit.schema import LedgerRow, PlatformProfile, Quota
from vcp.submit.stage import StageSpec, stage


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
        quota=Quota(per_day=5, day_tz="UTC"),
        deadline="2999-01-01T00:00:00Z",
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


def _kw(pair):
    return {"data_root": pair.roots.data, "configs_root": pair.roots.configs}


def _seed(pair, profile):
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(profile, **_kw(pair))
    seed_test_runs(pair)
    for sid, eval_run, test_run, kind in (
        ("S1", "good", "good.test", "candidate"),
        ("S2", "bad", "bad.test", "baseline"),
    ):
        stage(
            StageSpec(
                dataset=TEST,
                submission_id=sid,
                eval_run=eval_run,
                test_run=test_run,
                kind=kind,
                reason=None if kind == "candidate" else "anchor",
                **_kw(pair),
            )
        )
        now = utc_now().strftime("%Y-%m-%d %H:%M:%S")
        record(TEST, sid, now, tz="utc", **_kw(pair))
    score(TEST, "S1", public=0.8, **_kw(pair))


def test_status_view(pair):
    _seed(pair, _profile())
    st = status(TEST, **_kw(pair))
    assert st.staged == 2 and st.uploaded == 2 and st.foreign == 0
    assert st.quota.used == 2 and st.quota.per_day == 5
    assert st.deadline_in_hours > 24 and st.locked is None
    assert st.current == "S2" and st.unscored == ["S2"]
    led = SubmissionLedger(pair.test_paths.submissions_log)
    led.append(
        LedgerRow(
            event="foreign",
            ts=led.rows[-1].ts,
            platform_ref="f1",
            file_name="x.csv",
            at="2999-01-01T00:00:00.000Z",
            public=0.95,
        )
    )
    lock(TEST, "r0", **_kw(pair))
    st = status(TEST, **_kw(pair))
    assert st.current == "foreign:f1" and st.locked.reason == "r0" and st.foreign == 1


def test_status_best_board(pair):
    _seed(pair, _profile(board_rule="best"))
    assert status(TEST, **_kw(pair)).current == "S1"
    score(TEST, "S2", public=0.85, **_kw(pair))
    assert status(TEST, **_kw(pair)).current == "S2"


def test_report_is_last_vs_last(pair):
    _seed(pair, _profile())
    score(TEST, "S2", public=0.9, private=0.7, **_kw(pair))
    for run in ("good", "bad"):
        measure_run(
            MeasureSpec(
                run_id=run,
                metrics=["accuracy"],
                subsets=["holdout"],
                unseal=True,
                reason="report",
                **_kw(pair),
            )
        )
    rows = report(TEST, **_kw(pair))
    assert [r.submission_id for r in rows] == ["S1", "S2"]
    assert rows[0].delta is None and rows[0].public == 0.8 and rows[0].sealed_value == 1.0
    assert rows[1].delta == pytest.approx(0.1) and rows[1].kind == "baseline"
    assert rows[1].private == 0.7 and rows[1].shift == pytest.approx(-0.2)
    assert rows[1].sealed_value < 1.0
