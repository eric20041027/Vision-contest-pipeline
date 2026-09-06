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
    foreign = led.of("foreign")
    assert len(foreign) == 1 and foreign[0].submitted_by == "mate" and foreign[0].public == 0.4
    again = sync(TEST, runner=FakeRunner(rows), **_kw(staged))
    assert again.scored == 0 and again.foreign == 0
    assert len(SubmissionLedger(staged.test_paths.submissions_log).rows) == len(led.rows)
    rows[0]["privateScore"] = "0.65"
    third = sync(TEST, runner=FakeRunner(rows), **_kw(staged))
    assert third.scored == 1
    assert SubmissionLedger(staged.test_paths.submissions_log).latest_score("S1").private == 0.65


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
    assert match_submission(p("9", "S10 is not S1"), led, names) is None
    assert match_submission(p("9", "", "submission.csv"), led, names) == "S1"
    assert match_submission(p("9", "", "submission.csv"), led, names, taken={"S1"}) == "S2"
    late = stamp(utc_now() + timedelta(minutes=11))
    assert match_submission(p("9", "", "submission.csv", late), led, names) is None
