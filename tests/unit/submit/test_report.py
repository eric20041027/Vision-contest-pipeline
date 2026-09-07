from datetime import timedelta

import pytest

from submit_fixtures import EVAL, STAMP, TEST, seed_eval_runs, seed_judgements, seed_test_runs
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.submit.actions import record, score
from vcp.submit.final import lock
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import init_profile
from vcp.submit.report import assign_scores, report, status
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


def _upload(minutes: float, *, base) -> LedgerRow:
    return LedgerRow(
        event="uploaded",
        ts=stamp(base + timedelta(minutes=minutes)),
        submission_id="S1",
        at=stamp(base + timedelta(minutes=minutes)),
        source="manual",
        confirmed=True,
        profile_sha256="p" * 64,
    )


def _platform_score(minutes: float, public: float, *, base) -> LedgerRow:
    return LedgerRow(
        event="scored",
        ts=stamp(base + timedelta(minutes=minutes)),
        submission_id="S1",
        public=public,
        source="platform",
        at=stamp(base + timedelta(minutes=minutes)),
    )


def _manual_score(minutes: float, public: float, *, base) -> LedgerRow:
    return LedgerRow(
        event="scored",
        ts=stamp(base + timedelta(minutes=minutes)),
        submission_id="S1",
        public=public,
        source="manual",
    )


def test_assign_scores():
    base = utc_now()
    uploads = [_upload(0, base=base), _upload(5, base=base)]

    # T0/T5 uploads, T1/T6 platform scores: each maps to the upload nearest to it in time.
    scores = [_platform_score(1, 0.6, base=base), _platform_score(6, 0.7, base=base)]
    assigned = assign_scores(uploads, scores)
    assert [a.public if a else None for a in assigned] == [0.6, 0.7]

    # A platform score 20 seconds before the second upload's `at` is 20s away from it and
    # 4m40s away from the first -- nearest wins, so it maps to the second upload.
    near_second = assign_scores(uploads, [_platform_score(5 - 20 / 60, 0.5, base=base)])
    assert [a.public if a else None for a in near_second] == [None, 0.5]

    # Correction: two manual scores both qualify only for the second upload (the newest upload
    # with ts <= their own ts is the same one for both) -- the newer of the two by `ts` wins
    # there, and the first upload, which neither score is eligible for, gets nothing. A
    # claim-based match got this wrong: it handed the superseded score to the first upload
    # instead of dropping it.
    corrected = assign_scores(
        uploads, [_manual_score(6, 0.7, base=base), _manual_score(7, 0.71, base=base)]
    )
    assert [a.public if a else None for a in corrected] == [None, 0.71]

    # A platform score older than every upload still has a nearest upload: the first one.
    oldest_platform = assign_scores(uploads, [_platform_score(-100, 0.1, base=base)])
    assert [a.public if a else None for a in oldest_platform] == [0.1, None]

    # A manual score older than every upload has no upload recorded before it -> dropped.
    oldest_manual = assign_scores(uploads, [_manual_score(-100, 0.1, base=base)])
    assert oldest_manual == [None, None]

    # Uploads appended out of platform-time order (`record --at` allows it): an exact tie goes
    # to the upload with the later `at`, whatever its position in the list.
    out_of_order = [_upload(10, base=base), _upload(0, base=base)]
    tie = assign_scores(out_of_order, [_platform_score(5, 0.5, base=base)])
    assert [a.public if a else None for a in tie] == [0.5, None]


def test_report_scores_each_upload(pair):
    _seed(pair, _profile())
    score(TEST, "S2", public=0.7, **_kw(pair))
    later = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    record(TEST, "S1", later, tz="utc", **_kw(pair))
    score(TEST, "S1", public=0.9, **_kw(pair))
    rows = report(TEST, **_kw(pair))
    assert [(r.submission_id, r.public, r.delta) for r in rows] == [
        ("S1", 0.8, None),
        ("S2", 0.7, pytest.approx(-0.1)),
        ("S1", 0.9, pytest.approx(0.2)),
    ]
    assert status(TEST, **_kw(pair)).unscored == []


def test_report_when_a_foreign_upload_comes_first(pair):
    _seed(pair, _profile())
    led = SubmissionLedger(pair.test_paths.submissions_log)
    s1_at = led.uploads("S1")[0].at
    earlier = stamp(parse_stamp(str(s1_at)) - timedelta(minutes=5))
    led.append(
        LedgerRow(
            event="foreign",
            ts=stamp(),
            platform_ref="f1",
            file_name="x.csv",
            at=earlier,
            public=0.5,
        )
    )
    rows = report(TEST, **_kw(pair))
    assert rows[0].submission_id == "foreign:f1" and rows[0].delta is None
    assert rows[1].submission_id == "S1" and rows[1].delta == pytest.approx(0.8 - 0.5)
