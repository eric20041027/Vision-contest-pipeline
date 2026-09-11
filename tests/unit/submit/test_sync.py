import json
import subprocess
from datetime import timedelta

import pytest

from submit_fixtures import EVAL, STAMP, TEST, seed_eval_runs, seed_judgements, seed_test_runs
from vcp.core.errors import ValidationFailed
from vcp.core.time import stamp, utc_now
from vcp.submit.actions import record
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import init_profile
from vcp.submit.report import report, status
from vcp.submit.schema import PlatformProfile
from vcp.submit.stage import StageSpec, stage
from vcp.submit.sync import sync


def _profile(**over) -> PlatformProfile:
    base = dict(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="kaggle",
        competition="c1",
        board_rule="best",
        metric="accuracy",
        writer="scores_csv",
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


class FakeRunner:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(self.rows), "")


def _kw(pair):
    return {"data_root": pair.roots.data, "configs_root": pair.roots.configs}


@pytest.fixture
def staged(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(_profile(), **_kw(pair))
    seed_test_runs(pair)
    for sid, kind in (
        ("S1", "candidate"),
        ("S2", "baseline"),
        ("S3", "baseline"),
        ("S4", "baseline"),
    ):
        stage(
            StageSpec(
                dataset=TEST,
                submission_id=sid,
                eval_run="good" if sid == "S1" else "bad",
                test_run="good.test" if sid == "S1" else "bad.test",
                kind=kind,
                reason=None if kind == "candidate" else "anchor",
                **_kw(pair),
            )
        )
    now = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    record(TEST, "S1", now, tz="utc", **_kw(pair))
    record(TEST, "S2", now, tz="utc", **_kw(pair))
    record(TEST, "S3", now, tz="utc", platform_ref="k33", **_kw(pair))
    record(TEST, "S4", now, tz="utc", **_kw(pair))
    return pair


def test_sync_matches_scores_and_records_foreign(staged):
    at = stamp(utc_now())
    far = stamp(utc_now() - timedelta(hours=3))
    rows = [
        {"ref": 1, "fileName": "x.csv", "date": at, "description": "S1 note", "publicScore": "0.7"},
        {
            "ref": 2,
            "fileName": "submission.csv",
            "date": at,
            "description": "",
            "publicScore": "0.6",
        },
        {"ref": "k33", "fileName": "y.csv", "date": far, "description": "", "publicScore": "0.5"},
        {
            "ref": 4,
            "fileName": "z.csv",
            "date": far,
            "description": "teammate",
            "publicScore": "0.4",
            "submittedBy": "mate",
        },
    ]
    res = sync(TEST, runner=FakeRunner(rows), **_kw(staged))
    assert res.platform_rows == 4 and res.scored == 3 and res.foreign == 1
    assert res.matched == {"1": "S1", "2": "S2", "k33": "S3"} and res.unconfirmed == ["S4"]
    led = SubmissionLedger(staged.test_paths.submissions_log)
    assert led.latest_score("S1").public == 0.7 and led.latest_score("S1").source == "platform"
    assert led.latest_score("S2").public == 0.6 and led.latest_score("S3").public == 0.5
    assert led.latest_score("S1").at == at and led.latest_score("S1").platform_ref == "1"
    foreign = led.of("foreign")
    assert len(foreign) == 1 and foreign[0].submitted_by == "mate" and foreign[0].public == 0.4
    again = sync(TEST, runner=FakeRunner(rows), **_kw(staged))
    assert again.scored == 0 and again.foreign == 0
    assert len(SubmissionLedger(staged.test_paths.submissions_log).rows) == len(led.rows)
    rows[0]["privateScore"] = "0.65"
    third = sync(TEST, runner=FakeRunner(rows), **_kw(staged))
    assert third.scored == 1
    assert SubmissionLedger(staged.test_paths.submissions_log).latest_score("S1").private == 0.65


def test_sync_refreshes_pending_foreign_score_without_counting_a_second_arrival(staged):
    at = stamp(utc_now() - timedelta(hours=1))
    pending = {
        "ref": "foreign-1",
        "fileName": "submission.csv",
        "date": at,
        "description": "teammate run",
        "status": "SubmissionStatus.PENDING",
    }
    first = sync(TEST, runner=FakeRunner([pending]), **_kw(staged))
    assert first.foreign == 1 and first.refreshed == 0

    complete = {
        **pending,
        "status": "SubmissionStatus.COMPLETE",
        "publicScore": "0.935",
    }
    second = sync(TEST, runner=FakeRunner([complete]), **_kw(staged))
    assert second.foreign == 0 and second.refreshed == 1

    ledger = SubmissionLedger(staged.test_paths.submissions_log)
    snapshots = [r for r in ledger.of("foreign") if r.platform_ref == "foreign-1"]
    assert len(snapshots) == 2
    assert snapshots[0].public is None and snapshots[1].public == 0.935
    assert snapshots[1].platform_status == "SubmissionStatus.COMPLETE"
    assert len([r for r in ledger.arrivals() if r.platform_ref == "foreign-1"]) == 1

    view = status(TEST, **_kw(staged))
    assert view.foreign == 1 and view.current == "foreign:foreign-1"
    rows = report(TEST, **_kw(staged))
    foreign_rows = [r for r in rows if r.submission_id == "foreign:foreign-1"]
    assert len(foreign_rows) == 1 and foreign_rows[0].public == 0.935

    before = len(ledger.rows)
    third = sync(TEST, runner=FakeRunner([complete]), **_kw(staged))
    assert third.foreign == 0 and third.refreshed == 0
    assert len(SubmissionLedger(staged.test_paths.submissions_log).rows) == before


def _foreign(ref, at, status_="SubmissionStatus.PENDING", **more):
    row = {"fileName": "submission.csv", "date": at, "description": "teammate", "status": status_}
    if ref is not None:
        row["ref"] = ref
    return {**row, **more}


def _snapshots(staged, ref=None):
    rows = SubmissionLedger(staged.test_paths.submissions_log).of("foreign")
    return [r for r in rows if ref is None or r.platform_ref == ref]


def _arrivals(staged):
    """What quota is charged on: ``guards.quota_state`` counts ``ledger.arrivals()``."""
    return len(SubmissionLedger(staged.test_paths.submissions_log).arrivals())


def test_foreign_pending_to_error_is_a_snapshot_without_a_score(staged):
    """VCP-009 matrix: a status change alone (no score) is still worth a row -- the report must
    stop showing the ref as pending -- but it is neither an arrival nor a score."""
    at = stamp(utc_now() - timedelta(hours=1))
    sync(TEST, runner=FakeRunner([_foreign("f-err", at)]), **_kw(staged))
    used = _arrivals(staged)
    res = sync(
        TEST, runner=FakeRunner([_foreign("f-err", at, "SubmissionStatus.ERROR")]), **_kw(staged)
    )
    assert (res.foreign, res.refreshed, res.scored) == (0, 1, 0)
    snaps = _snapshots(staged, "f-err")
    assert [s.platform_status for s in snaps] == [
        "SubmissionStatus.PENDING",
        "SubmissionStatus.ERROR",
    ]
    assert all(s.public is None for s in snaps)
    arrival = [
        r
        for r in SubmissionLedger(staged.test_paths.submissions_log).arrivals()
        if r.platform_ref == "f-err"
    ]
    assert len(arrival) == 1 and arrival[0].platform_status == "SubmissionStatus.ERROR"
    assert _arrivals(staged) == used  # the ref is one arrival however many snapshots it has
    row = next(r for r in report(TEST, **_kw(staged)) if r.submission_id == "foreign:f-err")
    assert row.public is None


def test_foreign_complete_score_correction_keeps_the_newest(staged):
    """VCP-009 matrix: a platform that corrects a COMPLETE score yields a third snapshot; the
    arrival, the board and the report all read the newest, quota still counts one."""
    at = stamp(utc_now() - timedelta(hours=1))
    complete = _foreign("f-fix", at, "SubmissionStatus.COMPLETE", publicScore="0.935")
    sync(TEST, runner=FakeRunner([_foreign("f-fix", at)]), **_kw(staged))
    sync(TEST, runner=FakeRunner([complete]), **_kw(staged))
    used = _arrivals(staged)
    res = sync(TEST, runner=FakeRunner([{**complete, "publicScore": "0.940"}]), **_kw(staged))
    assert (res.foreign, res.refreshed) == (0, 1)
    assert [s.public for s in _snapshots(staged, "f-fix")] == [None, 0.935, 0.940]
    led = SubmissionLedger(staged.test_paths.submissions_log)
    assert led.latest_foreign("f-fix").public == 0.940
    assert [r.public for r in led.arrivals() if r.platform_ref == "f-fix"] == [0.940]
    assert _arrivals(staged) == used
    row = next(r for r in report(TEST, **_kw(staged)) if r.submission_id == "foreign:f-fix")
    assert row.public == 0.940


def test_foreign_without_a_platform_ref_still_refreshes_by_its_derived_ref(staged):
    """VCP-009 matrix: a row the platform lists without ``ref`` gets a ref derived from its
    file name and time; that derivation is stable across syncs, so its snapshots chain up."""
    at = stamp(utc_now() - timedelta(hours=1))
    first = sync(TEST, runner=FakeRunner([_foreign(None, at)]), **_kw(staged))
    assert first.foreign == 1
    snaps = _snapshots(staged)
    ref = snaps[-1].platform_ref
    assert ref and "foreign" not in ref
    res = sync(
        TEST,
        runner=FakeRunner([_foreign(None, at, "SubmissionStatus.COMPLETE", publicScore="0.5")]),
        **_kw(staged),
    )
    assert (res.foreign, res.refreshed) == (0, 1)
    assert [s.public for s in _snapshots(staged, ref)] == [None, 0.5]


def test_foreign_duplicate_rows_at_the_same_time(staged):
    """VCP-009 matrix: one page listing the same ref twice yields one snapshot (the second is
    identical to the one just appended); two different refs sharing a timestamp are two arrivals
    in ledger order, and a rerun of the same page appends nothing."""
    at = stamp(utc_now() - timedelta(hours=1))
    page = [_foreign("f-a", at), _foreign("f-a", at), _foreign("f-b", at)]
    res = sync(TEST, runner=FakeRunner(page), **_kw(staged))
    assert (res.foreign, res.refreshed) == (2, 0)
    assert [s.platform_ref for s in _snapshots(staged)] == ["f-a", "f-b"]
    led = SubmissionLedger(staged.test_paths.submissions_log)
    same_time = [r.platform_ref for r in led.arrivals() if r.at == at]
    assert same_time == ["f-a", "f-b"]
    before = len(led.rows)
    again = sync(TEST, runner=FakeRunner(page), **_kw(staged))
    assert (again.foreign, again.refreshed) == (0, 0)
    assert len(SubmissionLedger(staged.test_paths.submissions_log).rows) == before


def test_sync_refuses_manual(pair):
    seed_eval_runs(pair)
    init_profile(_profile(platform="manual", competition=None, board_rule="last"), **_kw(pair))
    with pytest.raises(ValidationFailed, match="manual_platform"):
        sync(TEST, runner=FakeRunner([]), **_kw(pair))


def test_match_prefers_ref_then_id_then_file_and_time(staged):
    from vcp.submit.platforms.base import PlatformSubmission
    from vcp.submit.sync import match_submission

    led = SubmissionLedger(staged.test_paths.submissions_log)
    names = {sid: "submission.csv" for sid in led.ids()}
    at = stamp(utc_now())

    def p(ref, desc, file_name="q.csv", when=at):
        return PlatformSubmission(ref, file_name, when, desc, None, None, "complete", None)

    assert match_submission(p("k33", "S1 in text"), led, names) == "S3"
    assert match_submission(p("9", "resend of S1"), led, names) == "S1"
    assert match_submission(p("9", "S10 only"), led, names) is None
    assert match_submission(p("9", "S10 is not S1"), led, names) == "S1"
    assert match_submission(p("9", "", "submission.csv"), led, names) == "S1"
    assert match_submission(p("9", "", "submission.csv"), led, names, taken={"S1"}) == "S2"
    late = stamp(utc_now() + timedelta(minutes=11))
    assert match_submission(p("9", "", "submission.csv", late), led, names) is None


def test_sync_processes_platform_rows_in_time_order(staged):
    now = utc_now()
    rows = [
        {
            "ref": 2,
            "fileName": "x.csv",
            "date": stamp(now),
            "description": "S1 resend",
            "publicScore": "0.7",
        },
        {
            "ref": 1,
            "fileName": "x.csv",
            "date": stamp(now - timedelta(hours=2)),
            "description": "S1 first",
            "publicScore": "0.9",
        },
    ]
    res = sync(TEST, runner=FakeRunner(rows), **_kw(staged))
    assert res.scored == 2 and res.foreign == 0
    led = SubmissionLedger(staged.test_paths.submissions_log)
    assert [r.public for r in led.of("scored", "S1")] == [0.9, 0.7]
    assert led.latest_score("S1").public == 0.7


def test_sync_survives_a_missing_stage_json(staged):
    (staged.test_paths.submission_dir("S4") / "stage.json").unlink()
    rows = [
        {
            "ref": 5,
            "fileName": "x.csv",
            "date": stamp(utc_now()),
            "description": "S1 ok",
            "publicScore": "0.5",
        }
    ]
    res = sync(TEST, runner=FakeRunner(rows), **_kw(staged))
    assert res.scored == 1 and "S4" in res.unconfirmed


def test_sync_refuses_a_non_finite_score(staged):
    rows = [
        {
            "ref": 6,
            "fileName": "x.csv",
            "date": stamp(utc_now()),
            "description": "S1",
            "publicScore": "Infinity",
        }
    ]
    with pytest.raises(ValidationFailed, match="platform_response"):
        sync(TEST, runner=FakeRunner(rows), **_kw(staged))
