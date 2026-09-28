import json
import subprocess
from datetime import timedelta

import pytest

from submit_fixtures import EVAL, STAMP, TEST, seed_eval_runs, seed_judgements, seed_test_runs
from vcp.core.config import dump_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.submit.actions import record
from vcp.submit.adopt import adopt
from vcp.submit.guards import quota_state
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import shared_ledger
from vcp.submit.profile import init_profile, load_profile
from vcp.submit.report import report, status
from vcp.submit.schema import Gate, LedgerRow, PlatformProfile, Quota
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


def test_a_description_that_opens_with_an_id_is_that_ids_before_any_id_it_mentions(roots):
    """The final review's probe (spec 2026-09-28 §4.4): vcp writes ``<id> <message>``, so
    ``S2 same as S1`` is S2's upload. S2's own row carries no ref (CLI 2.2.4 answers a file
    upload with "Successfully submitted" alone), so rule 0 cannot claim the entry; matching it
    to the id it merely mentions made it a binding -- an extra upload of S1 the append-only
    ledger never drops -- and left S2 without its score."""
    paths = DatasetPaths.resolve(TEST, data_root=roots.data, configs_root=roots.configs)
    profile = _profile(quota=Quota(per_day=5, day_tz="UTC"))
    dump_yaml_model(profile, paths.submit_yaml)
    led = SubmissionLedger(paths.submissions_log)
    for sid in ("S1", "S2"):
        led.append(
            LedgerRow(
                event="staged",
                ts="2026-09-28T00:00:00.000Z",
                submission_id=sid,
                kind="candidate",
                eval_run="e",
                gate=Gate(admission="PASS"),
                profile_sha256="p" * 64,
            )
        )
    for sid, at, message in (
        ("S1", "2026-09-28T01:00:00.000Z", "S1"),
        ("S2", "2026-09-28T02:00:00.000Z", "S2 same as S1"),
    ):  # what `upload` writes when the CLI says "Successfully submitted" and no ref
        led.append(
            LedgerRow(
                event="uploaded",
                ts=at,
                submission_id=sid,
                at=at,
                source="vcp",
                message=message,
                confirmed=True,
                profile_sha256="p" * 64,
            )
        )
    listed = [
        {
            "ref": 101,
            "fileName": "submission.csv",
            "date": "2026-09-28T01:00:03Z",
            "description": "S1",
            "publicScore": "0.80",
        },
        {
            "ref": 102,
            "fileName": "submission.csv",
            "date": "2026-09-28T02:00:03Z",
            "description": "S2 same as S1",
            "publicScore": "0.85",
        },
    ]
    res = sync(TEST, runner=FakeRunner(listed), data_root=roots.data, configs_root=roots.configs)
    assert res.matched == {"101": "S1", "102": "S2"} and res.bound == 0
    after = SubmissionLedger(paths.submissions_log)
    assert quota_state(after, profile, parse_stamp("2026-09-28T03:00:00Z")).used == 2
    assert [r.platform_ref for r in after.of("scored", "S1")] == ["101"]
    assert [r.platform_ref for r in after.of("scored", "S2")] == ["102"]


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


def test_a_re_upload_that_scores_the_same_still_gets_its_own_scored_row(staged):
    """VCP-038: re-sending the same file (board_rule=last's needs_reupload) scores the same; its
    ref still needs a scored row, the row that ties it to the id."""
    now = utc_now()
    first = {"ref": 1, "fileName": "x.csv", "description": "S1 first", "publicScore": "0.9"}
    rows = [
        {**first, "date": stamp(now - timedelta(hours=2))},
        {**first, "ref": 2, "date": stamp(now), "description": "S1 again"},
    ]
    assert sync(TEST, runner=FakeRunner(rows), **_kw(staged)).scored == 2
    led = SubmissionLedger(staged.test_paths.submissions_log)
    assert [r.platform_ref for r in led.of("scored", "S1")] == ["1", "2"]
    assert sync(TEST, runner=FakeRunner(rows), **_kw(staged)).scored == 0  # still idempotent


def test_a_foreign_row_the_ledger_later_learns_is_ours_stops_counting(pair):
    """VCP-038 end to end: sync meets an upload the ledger does not know yet and writes it as
    foreign; the upload is recorded afterwards; the next sync ties the ref to S1 with a scored
    row, and from then on the submission is one arrival."""
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(_profile(quota=Quota(per_day=5, day_tz="UTC")), **_kw(pair))
    seed_test_runs(pair)
    stage(
        StageSpec(
            dataset=TEST, submission_id="S1", eval_run="good", test_run="good.test", **_kw(pair)
        )
    )
    now = utc_now()
    web = {"ref": 9, "fileName": "submission.csv", "date": stamp(now), "publicScore": "0.8"}
    rows = [{**web, "description": "sent from the browser"}]
    assert sync(TEST, runner=FakeRunner(rows), **_kw(pair)).foreign == 1
    record(TEST, "S1", now.strftime("%Y-%m-%d %H:%M:%S"), tz="utc", **_kw(pair))
    assert len(SubmissionLedger(pair.test_paths.submissions_log).arrivals()) == 2  # not tied yet
    res = sync(TEST, runner=FakeRunner(rows), **_kw(pair))
    assert res.matched == {"9": "S1"} and res.scored == 1
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert [r.event for r in led.arrivals()] == ["uploaded"]
    st = status(TEST, **_kw(pair))
    assert st.foreign == 0 and st.quota.used == 1


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
    before = staged.test_paths.submissions_log.read_bytes()
    with pytest.raises(ValidationFailed, match="platform_response"):
        sync(TEST, runner=FakeRunner(rows), **_kw(staged))
    # every platform value is checked before the first write -- upload's pre-sync relies on it
    assert staged.test_paths.submissions_log.read_bytes() == before


def _only_s1(pair, **over):
    """S1 staged on a Kaggle profile with room for five uploads a day; nothing uploaded."""
    seed_eval_runs(pair)
    seed_judgements(pair)
    profile = _profile(quota=Quota(per_day=5, day_tz="UTC"), **over)
    init_profile(profile, **_kw(pair))
    if profile.ledger == "shared":  # only adopt creates the shared ledger (spec 2026-09-28 §4.1)
        adopt(TEST, **_kw(pair))
    seed_test_runs(pair)
    stage(
        StageSpec(
            dataset=TEST, submission_id="S1", eval_run="good", test_run="good.test", **_kw(pair)
        )
    )


def test_a_pending_upload_the_ledger_never_recorded_is_bound_to_its_id(pair):
    """spec 2026-09-28 §4.4: S1 went up by hand and was never recorded. The entry names S1 and
    has no score yet; it becomes S1's upload anyway (source=platform), counts once against the
    quota, and the next sync binds nothing more -- it only adds the score."""
    _only_s1(pair)
    now = stamp(utc_now())
    web = {
        "ref": 21,
        "fileName": "submission.csv",
        "date": now,
        "description": "S1 by hand",
        "status": "pending",
    }
    first = sync(TEST, runner=FakeRunner([web]), **_kw(pair))
    assert (first.bound, first.scored, first.foreign) == (1, 0, 0)
    assert first.matched == {"21": "S1"} and first.unconfirmed == []
    [row] = SubmissionLedger(pair.test_paths.submissions_log).of("uploaded")
    assert (row.submission_id, row.source, row.platform_ref, row.at, row.confirmed) == (
        "S1",
        "platform",
        "21",
        now,
        True,
    )
    assert row.profile_sha256 == load_profile(pair.test_paths)[1]
    assert status(TEST, **_kw(pair)).quota.used == 1
    scored = {**web, "status": "complete", "publicScore": "0.8"}
    second = sync(TEST, runner=FakeRunner([scored]), **_kw(pair))
    assert (second.bound, second.scored) == (0, 1)
    assert status(TEST, **_kw(pair)).quota.used == 1


def test_record_refuses_a_platform_ref_an_upload_row_already_carries(pair):
    """Final review M3: sync bound entry 25 to S1. Recording it again by hand would be a second
    upload row for one platform entry; it is refused and nothing is written."""
    _only_s1(pair)
    now = utc_now()
    web = {"ref": 25, "fileName": "submission.csv", "date": stamp(now), "description": "S1"}
    assert sync(TEST, runner=FakeRunner([web]), **_kw(pair)).bound == 1
    before = pair.test_paths.submissions_log.read_bytes()
    at = now.strftime("%Y-%m-%d %H:%M:%S")
    with pytest.raises(ValidationFailed) as ei:
        record(TEST, "S1", at, tz="utc", platform_ref="25", **_kw(pair))
    assert str(ei.value) == "exists: platform ref 25 is already recorded for S1"
    assert pair.test_paths.submissions_log.read_bytes() == before
    out = record(TEST, "S1", at, tz="utc", platform_ref="26", **_kw(pair))  # another entry
    assert out.prior_uploads == 1 and out.row.platform_ref == "26"


def test_an_upload_recorded_within_ten_minutes_is_that_entry_and_nothing_is_bound(pair):
    _only_s1(pair)
    now = utc_now()
    record(TEST, "S1", now.strftime("%Y-%m-%d %H:%M:%S"), tz="utc", **_kw(pair))
    near = {
        "ref": 22,
        "fileName": "submission.csv",
        "date": stamp(now + timedelta(minutes=3)),
        "description": "S1",
    }
    res = sync(TEST, runner=FakeRunner([near]), **_kw(pair))
    assert res.bound == 0 and res.matched == {"22": "S1"}
    assert len(SubmissionLedger(pair.test_paths.submissions_log).of("uploaded")) == 1
    assert status(TEST, **_kw(pair)).quota.used == 1


def test_a_ref_written_as_foreign_before_its_id_was_staged_is_one_arrival_once_bound(pair):
    """The entry was listed before S1 was in the ledger, so sync wrote it as foreign. Once S1 is
    staged and the entry names it, the binding and the foreign row are one arrival."""
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(_profile(quota=Quota(per_day=5, day_tz="UTC")), **_kw(pair))
    seed_test_runs(pair)
    early = {
        "ref": 23,
        "fileName": "submission.csv",
        "date": stamp(utc_now()),
        "description": "S1 early",
    }
    assert sync(TEST, runner=FakeRunner([early]), **_kw(pair)).foreign == 1
    stage(
        StageSpec(
            dataset=TEST, submission_id="S1", eval_run="good", test_run="good.test", **_kw(pair)
        )
    )
    res = sync(TEST, runner=FakeRunner([early]), **_kw(pair))
    assert res.bound == 1 and res.matched == {"23": "S1"}
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert [r.event for r in led.arrivals()] == ["uploaded"]
    assert status(TEST, **_kw(pair)).quota.used == 1


def test_a_bound_entry_does_not_pull_a_same_named_neighbour_into_its_id(pair):
    """The file-and-time rule looks only at uploads vcp or a person attested. A binding is the
    platform's own entry; were it to count, the teammate's upload of the same file name two
    minutes earlier would be bound to S1 on the next sync and S1 would read as uploaded twice."""
    _only_s1(pair)
    now = utc_now()
    mine = {"ref": 31, "fileName": "submission.csv", "date": stamp(now), "description": "S1 web"}
    theirs = {
        "ref": 32,
        "fileName": "submission.csv",
        "date": stamp(now - timedelta(minutes=2)),
        "description": "teammate",
    }
    first = sync(TEST, runner=FakeRunner([mine, theirs]), **_kw(pair))
    assert (first.bound, first.foreign) == (1, 1)
    second = sync(TEST, runner=FakeRunner([mine, theirs]), **_kw(pair))
    assert (second.bound, second.foreign, second.refreshed) == (0, 0, 0)
    assert second.matched == {"31": "S1"}
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert [r.platform_ref for r in led.of("uploaded")] == ["31"]


def test_two_scores_of_one_id_are_written_once_and_the_latest_is_by_platform_time(staged):
    """spec 2026-09-28 §4.6: S1 went up twice and scored 0.9, then 0.7. The later upload is
    listed first, so its row lands first. A sync that lists both writes only the other, the
    next writes nothing, and the latest score is the later upload's whatever the file order."""
    now = utc_now()
    later = {
        "ref": 42,
        "fileName": "x.csv",
        "date": stamp(now),
        "description": "S1 again",
        "publicScore": "0.7",
    }
    earlier = {
        "ref": 41,
        "fileName": "x.csv",
        "date": stamp(now - timedelta(hours=1)),
        "description": "S1 first",
        "publicScore": "0.9",
    }
    assert sync(TEST, runner=FakeRunner([later]), **_kw(staged)).scored == 1
    assert sync(TEST, runner=FakeRunner([later, earlier]), **_kw(staged)).scored == 1
    led = SubmissionLedger(staged.test_paths.submissions_log)
    assert [r.public for r in led.of("scored", "S1")] == [0.7, 0.9]
    assert led.latest_score("S1").public == 0.7
    before = len(led.rows)
    assert sync(TEST, runner=FakeRunner([later, earlier]), **_kw(staged)).scored == 0
    assert len(SubmissionLedger(staged.test_paths.submissions_log).rows) == before


def test_a_status_change_of_a_scored_entry_is_a_new_scored_row(staged):
    entry = {
        "ref": 51,
        "fileName": "x.csv",
        "date": stamp(utc_now()),
        "description": "S1",
        "publicScore": "0.7",
        "status": "pending",
    }
    assert sync(TEST, runner=FakeRunner([entry]), **_kw(staged)).scored == 1
    done = {**entry, "status": "complete"}
    assert sync(TEST, runner=FakeRunner([done]), **_kw(staged)).scored == 1
    assert sync(TEST, runner=FakeRunner([done]), **_kw(staged)).scored == 0


def test_sync_writes_the_shared_ledger_when_submit_yaml_says_so(pair):
    _only_s1(pair, ledger="shared")
    web = {"ref": 24, "fileName": "submission.csv", "date": stamp(utc_now()), "description": "S1"}
    assert sync(TEST, runner=FakeRunner([web]), **_kw(pair)).bound == 1
    events = [r.event for r in SubmissionLedger(shared_ledger(pair.test_paths)).rows]
    assert events == ["staged", "uploaded"]
    assert not pair.test_paths.submissions_log.exists()
