import json
import subprocess
from datetime import timedelta

import pytest
from typer.testing import CliRunner

from submit_fixtures import EVAL, STAMP, TEST, seed_eval_runs, seed_judgements, seed_test_runs
from vcp.cli import app
from vcp.core.config import dump_yaml_model
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.submit.actions import record, score, upload
from vcp.submit.adopt import adopt
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import shared_ledger
from vcp.submit.platforms import kaggle
from vcp.submit.profile import init_profile
from vcp.submit.schema import LedgerRow, PlatformProfile, Quota
from vcp.submit.stage import StageSpec, stage

SECRET = "fakesecretfakesecretfakesecret1234"
EMPTY = (0, "No submissions found", "")  # the list upload reads first, when it is empty


def _listing(*entries):
    return 0, json.dumps(list(entries)), ""


@pytest.fixture
def no_wait(monkeypatch):
    """Kaggle's read-back looks at once instead of waiting between looks."""
    monkeypatch.setattr(kaggle, "READBACK_DELAYS", tuple(0.0 for _ in kaggle.READBACK_DELAYS))


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
    if profile.ledger == "shared":  # only adopt creates the shared ledger (spec 2026-09-28 §4.1)
        adopt(TEST, data_root=pair.roots.data, configs_root=pair.roots.configs)
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
    runner = FakeRunner([EMPTY, (0, f"KAGGLE_KEY={SECRET}\nSuccessfully submitted to c1", "")])
    out = upload(TEST, "S1", message="first", runner=runner, **_kw(pair))
    assert out.row.source == "vcp" and out.row.confirmed and out.row.message == "S1 first"
    assert out.result.confirmed and SECRET not in out.result.detail
    assert runner.calls[0][1:3] == ["competitions", "submissions"]  # the list comes first
    assert runner.calls[1][-4:] == ["-m", "S1 first", "-q", "c1"]
    assert out.quota.used == 1 and (out.sync, out.bound) == ("ok", 0)
    with pytest.raises(ValidationFailed, match="quota_exhausted") as ei:
        upload(TEST, "S2", runner=FakeRunner([EMPTY]), **_kw(pair))
    assert ei.value.fields["quota"] == "1/1"
    text = pair.test_paths.submissions_log.read_text(encoding="utf-8")
    assert SECRET not in text and text.count("uploaded") == 1


def test_upload_failures_write_no_row(pair, no_wait):
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
    runner = FakeRunner([EMPTY, (1, "", f"denied key={SECRET}")])
    with pytest.raises(Exception, match="exit 1") as ei:
        upload(TEST, "S1", runner=runner, **_kw(pair))
    assert SECRET not in str(ei.value)
    assert ei.value.fields == {"exit_code": 1, "sync": "ok", "bound": 0}
    runner = FakeRunner([EMPTY, (0, "Could not submit to competition", "")])  # 2.2.4, exit 0
    with pytest.raises(Exception, match="upload_failed"):
        upload(TEST, "S1", runner=runner, **_kw(pair))
    runner = FakeRunner([EMPTY, (0, "queued", ""), *[EMPTY] * len(kaggle.READBACK_DELAYS)])
    out = upload(TEST, "S1", runner=runner, **_kw(pair))
    assert not out.row.confirmed and out.row.platform_ref is None
    assert out.result.readback == "not_listed"
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert [r.event for r in led.rows] == ["staged", "staged", "uploaded"]


def test_a_read_back_ref_is_written_into_the_uploaded_row(pair, no_wait):
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
    listed = {"ref": 777, "fileName": "submission.csv", "date": stamp(), "description": "S1 x"}
    runner = FakeRunner([EMPTY, (0, "queued", ""), _listing(listed)])
    out = upload(TEST, "S1", message="x", runner=runner, **_kw(pair))
    assert out.row.confirmed and out.row.platform_ref == "777"
    row = SubmissionLedger(pair.test_paths.submissions_log).of("uploaded")[0]
    assert row.confirmed and row.platform_ref == "777"  # sync's first rule matches it by ref


def test_a_read_back_that_meets_a_ref_the_ledger_holds_confirms_nothing(pair, no_wait):
    quota = Quota(per_day=5, day_tz="UTC")
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best", quota=quota))
    listed = {"ref": 777, "fileName": "submission.csv", "date": stamp(), "description": "S1"}
    answers = [(0, "queued", ""), _listing(listed)]
    first = upload(TEST, "S1", runner=FakeRunner([EMPTY, *answers]), **_kw(pair))
    assert first.row.platform_ref == "777"
    # S1 once more (a re-upload needs --force), while the list still shows only the first
    # one: 777 is not this upload
    runner = FakeRunner([_listing(listed), *answers])
    again = upload(TEST, "S1", force="the first looked lost", runner=runner, **_kw(pair))
    assert (again.row.confirmed, again.row.platform_ref) == (False, None)
    assert again.result.readback == "known_ref" and again.row.reason == "the first looked lost"


def test_upload_on_manual_platform_is_refused(pair):
    _staged(pair, _profile())
    with pytest.raises(ValidationFailed, match="manual_platform"):
        upload(TEST, "S1", runner=FakeRunner([]), **_kw(pair))


@pytest.mark.parametrize("first", ["manual_platform", "locked", "past_deadline"])
def test_upload_checks_profile_and_guards_before_artifact_hash(pair, first):
    profile = (
        _profile() if first == "manual_platform" else _profile(platform="kaggle", competition="c1")
    )
    _staged(pair, profile)
    ledger = SubmissionLedger(pair.test_paths.submissions_log)
    if first == "locked":
        ledger.append(LedgerRow(event="lock", ts=stamp(), reason="freeze"))
    if first in {"locked", "past_deadline"}:
        profile = profile.model_copy(update={"deadline": "2000-01-01T00:00:00Z"})
        dump_yaml_model(profile, pair.test_paths.submit_yaml)
    artifact = pair.test_paths.submission_dir("S1") / "submission.csv"
    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    before = pair.test_paths.submissions_log.read_bytes()
    runner = FakeRunner([])
    with pytest.raises(ValidationFailed, match=first):
        upload(TEST, "S1", runner=runner, **_kw(pair))
    assert runner.calls == []
    assert pair.test_paths.submissions_log.read_bytes() == before


def test_the_platforms_list_is_read_into_the_ledger_before_the_quota(pair):
    """spec 2026-09-28 §4.3: a teammate's upload the ledger never saw fills today's one slot;
    the upload is refused on the quota, after the ledger learned about it."""
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
    mate = {"ref": 7, "fileName": "mate.csv", "date": stamp(), "description": "teammate"}
    runner = FakeRunner([_listing(mate)])
    with pytest.raises(ValidationFailed, match="quota_exhausted") as ei:
        upload(TEST, "S1", runner=runner, **_kw(pair))
    assert ei.value.fields["quota"] == "1/1"
    assert (ei.value.fields["sync"], ei.value.fields["bound"]) == ("ok", 0)
    assert len(runner.calls) == 1 and runner.calls[0][1:3] == ["competitions", "submissions"]
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert [(r.event, r.platform_ref) for r in led.rows[2:]] == [("foreign", "7")]


def test_an_unreadable_list_stops_the_upload(pair):
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
    runner = FakeRunner([(1, "", f"401 key={SECRET}")])
    before = pair.test_paths.submissions_log.read_bytes()
    with pytest.raises(ValidationFailed, match=r"sync_failed: kaggle CLI failed \(exit 1\)") as ei:
        upload(TEST, "S1", runner=runner, **_kw(pair))
    assert SECRET not in str(ei.value) and len(runner.calls) == 1
    assert "sync" not in ei.value.fields  # it never ran: nothing to report
    assert pair.test_paths.submissions_log.read_bytes() == before


def test_no_sync_skips_the_list_and_says_so(pair):
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
    runner = FakeRunner([(0, "Successfully submitted to c1", "")])
    out = upload(TEST, "S1", no_sync=True, runner=runner, **_kw(pair))
    assert (out.sync, out.bound) == ("skipped", 0) and len(runner.calls) == 1
    assert runner.calls[0][1:3] == ["competitions", "submit"]


def test_an_upload_found_on_the_platform_is_bound_and_blocks_a_second_upload(pair):
    """spec 2026-09-28 §4.4 + §4.5: S1 already went up by hand. The pre-upload sync binds it,
    so the quota counts it and the guard refuses S1 again: nothing reaches the platform."""
    quota = Quota(per_day=5, day_tz="UTC")
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best", quota=quota))
    web = {"ref": 9, "fileName": "submission.csv", "date": stamp(), "description": "S1 by hand"}
    runner = FakeRunner([_listing(web)])
    with pytest.raises(ValidationFailed, match="already_uploaded: S1 was uploaded 1 time") as ei:
        upload(TEST, "S1", runner=runner, **_kw(pair))
    # the FAIL says what the pre-sync wrote: its binding stays in the ledger (final review M1)
    assert ei.value.fields == {"uploads": 1, "sync": "ok", "bound": 1} and len(runner.calls) == 1
    [bound] = SubmissionLedger(pair.test_paths.submissions_log).of("uploaded")
    assert (bound.source, bound.platform_ref) == ("platform", "9")


def test_force_uploads_again_and_keeps_the_reason(pair):
    quota = Quota(per_day=5, day_tz="UTC")
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best", quota=quota))
    ok = (0, "Successfully submitted to c1", "")
    upload(TEST, "S1", runner=FakeRunner([EMPTY, ok]), **_kw(pair))
    with pytest.raises(ValidationFailed, match="already_uploaded"):
        upload(TEST, "S1", runner=FakeRunner([EMPTY]), **_kw(pair))
    with pytest.raises(ValidationFailed, match="already_uploaded") as ei:
        upload(TEST, "S1", no_sync=True, runner=FakeRunner([]), **_kw(pair))
    assert ei.value.fields == {"uploads": 1, "sync": "skipped", "bound": 0}
    with pytest.raises(ValidationFailed, match="invalid: --force needs a reason") as ei:
        upload(TEST, "S1", force="  ", runner=FakeRunner([]), **_kw(pair))
    assert ei.value.fields == {}  # refused before the pre-sync
    out = upload(TEST, "S1", force="scorer was down", runner=FakeRunner([EMPTY, ok]), **_kw(pair))
    assert out.row.reason == "scorer was down"
    rows = SubmissionLedger(pair.test_paths.submissions_log).uploads("S1")
    assert [r.reason for r in rows] == [None, "scorer was down"]


def test_after_final_the_chosen_id_goes_up_again_only_with_force(pair):
    """Final review I4 (spec 2026-09-28 §4.5 unchanged: every id that went up needs --force).
    board_rule=last scores the last upload; final chose S1 while S2 went up last, so S1 must go
    up again. final prints the exact command, a plain upload is refused with the way round it,
    and --force passes both final's lock (the chosen id is exempt) and the re-upload guard."""
    quota = Quota(per_day=5, day_tz="UTC")
    _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="last", quota=quota))
    ok = (0, "Successfully submitted to c1", "")
    for sid in ("S1", "S2"):
        upload(TEST, sid, runner=FakeRunner([EMPTY, ok]), **_kw(pair))
    for run in ("good", "bad"):
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
    r = CliRunner().invoke(app, ["submit", "final", "--dataset", TEST])
    verdict = [line for line in r.output.splitlines() if line.startswith("VERDICT ")][-1]
    assert r.exit_code == 0 and "chosen=S1" in verdict and "needs_reupload=S1" in verdict
    assert f'vcp submit upload --dataset {TEST} --id S1 --force "final re-send"' in r.output
    with pytest.raises(ValidationFailed, match="already_uploaded: S1 was uploaded 1 time") as ei:
        upload(TEST, "S1", runner=FakeRunner([EMPTY]), **_kw(pair))
    assert str(ei.value).endswith('; pass --force "<reason>" to send it again')
    out = upload(TEST, "S1", force="final re-send", runner=FakeRunner([EMPTY, ok]), **_kw(pair))
    assert (out.row.submission_id, out.row.reason) == ("S1", "final re-send")
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert [row.reason for row in led.uploads("S1")] == [None, "final re-send"]
    assert led.lock_state().reason == "final" and led.last_uploaded().submission_id == "S1"


def test_record_of_an_id_already_uploaded_warns_and_still_records(pair):
    _staged(pair, _profile(quota=Quota(per_day=5, day_tz="UTC")))
    first = record(TEST, "S1", _now_text(), tz="utc", **_kw(pair))
    assert first.warnings == [] and first.prior_uploads == 0
    again = record(TEST, "S1", _now_text(), tz="utc", **_kw(pair))
    assert again.prior_uploads == 1
    assert again.warnings[0].startswith("already_uploaded: S1 was uploaded 1 time(s), last at ")
    assert len(SubmissionLedger(pair.test_paths.submissions_log).uploads("S1")) == 2


def test_record_and_score_write_the_shared_ledger(pair):
    _staged(pair, _profile(ledger="shared", quota=Quota(per_day=5, day_tz="UTC")))
    record(TEST, "S1", _now_text(), tz="utc", **_kw(pair))
    score(TEST, "S1", public=0.5, **_kw(pair))
    rows = SubmissionLedger(shared_ledger(pair.test_paths)).rows
    assert [r.event for r in rows] == ["staged", "staged", "uploaded", "scored"]
    assert not pair.test_paths.submissions_log.exists()
