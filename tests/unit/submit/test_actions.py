import subprocess
from datetime import timedelta

import pytest

from submit_fixtures import EVAL, STAMP, TEST, seed_eval_runs, seed_judgements, seed_test_runs
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.time import parse_stamp, utc_now
from vcp.submit.actions import record, score, upload
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile, Quota
from vcp.submit.stage import StageSpec, stage

SECRET = "fakesecretfakesecretfakesecret1234"


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
        quota=Quota(per_day=1, day_tz="UTC"),
        display_tz="Asia/Taipei",
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        code, out, err = self.responses.pop(0)
        return subprocess.CompletedProcess(args, code, out, err)


def _staged(pair, profile) -> None:
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(profile, data_root=pair.roots.data, configs_root=pair.roots.configs)
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
                data_root=pair.roots.data,
                configs_root=pair.roots.configs,
            )
        )


def _kw(pair):
    return {"data_root": pair.roots.data, "configs_root": pair.roots.configs}


def _now_text(offset=timedelta(0)) -> str:
    return (utc_now() + offset).strftime("%Y-%m-%d %H:%M:%S")


def test_record_then_score(pair):
    _staged(pair, _profile())
    out = record(TEST, "S1", _now_text(), tz="utc", platform_ref="web-7", **_kw(pair))
    assert out.row.source == "manual" and out.row.platform_ref == "web-7" and out.row.confirmed
    assert out.quota.used == 1 and out.warnings == []
    at = parse_stamp(out.row.at)
    assert abs((utc_now() - at).total_seconds()) < 5
    out = record(TEST, "S2", _now_text(), tz="utc", **_kw(pair))
    assert out.warnings and out.warnings[0].startswith("quota_overflow: 1/1")
    row = score(TEST, "S1", public=0.79, **_kw(pair))
    assert row.event == "scored" and row.source == "manual" and row.public == 0.79
    with pytest.raises(ValidationFailed, match="not_uploaded"):
        score(TEST, "S9", public=0.1, **_kw(pair))
    with pytest.raises(ValidationFailed, match="--public or --private"):
        score(TEST, "S1", **_kw(pair))
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert [r.event for r in led.rows] == ["staged", "staged", "uploaded", "uploaded", "scored"]


def test_record_time_rules(pair):
    _staged(pair, _profile())
    with pytest.raises(ValidationFailed, match="in the future"):
        record(TEST, "S1", _now_text(timedelta(hours=2)), tz="utc", **_kw(pair))
    with pytest.raises(ValidationFailed, match="before the submission was staged"):
        record(TEST, "S1", "2020-01-01 00:00", tz="utc", **_kw(pair))
    with pytest.raises(ValidationFailed, match="--tz must be"):
        record(TEST, "S1", _now_text(), tz="local", **_kw(pair))
    with pytest.raises(ValidationFailed, match="--at must be"):
        record(TEST, "S1", "now", tz="utc", **_kw(pair))
    taipei = (utc_now() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    out = record(TEST, "S1", taipei, **_kw(pair))
    assert abs((utc_now() - parse_stamp(out.row.at)).total_seconds()) < 5


def test_record_refuses_changed_artifact(pair):
    _staged(pair, _profile())
    art = pair.test_paths.submission_dir("S1") / "submission.csv"
    art.write_bytes(art.read_bytes() + b"x")
    with pytest.raises(IntegrityError, match="artifact sha256"):
        record(TEST, "S1", _now_text(), tz="utc", **_kw(pair))
    assert [r.event for r in SubmissionLedger(pair.test_paths.submissions_log).rows] == [
        "staged",
        "staged",
    ]


def test_upload_kaggle_with_quota(pair):
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
    runner = FakeRunner([(0, f"KAGGLE_KEY={SECRET}\nSuccessfully submitted to c1", "")])
    out = upload(TEST, "S1", message="first", runner=runner, **_kw(pair))
    assert out.row.source == "vcp" and out.row.confirmed and out.row.message == "S1 first"
    assert out.result.confirmed and SECRET not in out.result.detail
    assert runner.calls[0][-4:] == ["-m", "S1 first", "-q", "c1"]
    assert out.quota.used == 1
    with pytest.raises(ValidationFailed, match="quota_exhausted") as ei:
        upload(TEST, "S2", runner=FakeRunner([]), **_kw(pair))
    assert ei.value.fields["quota"] == "1/1"
    text = pair.test_paths.submissions_log.read_text(encoding="utf-8")
    assert SECRET not in text and text.count("uploaded") == 1


def test_upload_failures_write_no_row(pair):
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
    runner = FakeRunner([(1, "", f"denied key={SECRET}")])
    with pytest.raises(Exception, match="exit 1") as ei:
        upload(TEST, "S1", runner=runner, **_kw(pair))
    assert SECRET not in str(ei.value)
    runner = FakeRunner([(0, "queued", "")])
    out = upload(TEST, "S1", runner=runner, **_kw(pair))
    assert not out.row.confirmed
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert [r.event for r in led.rows] == ["staged", "staged", "uploaded"]


def test_upload_on_manual_platform_is_refused(pair):
    _staged(pair, _profile())
    with pytest.raises(ValidationFailed, match="manual_platform"):
        upload(TEST, "S1", runner=FakeRunner([]), **_kw(pair))
