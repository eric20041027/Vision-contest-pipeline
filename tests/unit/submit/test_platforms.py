import json
import logging
import subprocess
from datetime import timedelta

import pytest

from vcp.core.errors import PlatformError, ValidationFailed, VcpError
from vcp.core.time import parse_stamp, stamp
from vcp.submit.platforms import get_platform, kaggle
from vcp.submit.platforms.base import redact
from vcp.submit.platforms.kaggle import parse_date, parse_score, parse_submissions
from vcp.submit.schema import Artifact, Gate, Pairing, PlatformProfile, Staged

SECRET = "fakesecretfakesecretfakesecret1234"
STAMP = "2026-09-05T00:00:00.000Z"
NOW = parse_stamp("2026-09-25T08:00:00.000Z")
LIST = [
    "fake-kaggle",
    "competitions",
    "submissions",
    "--format",
    "json",
    "--page-size",
    "200",
    "c1",
]
# Kaggle CLI 2.2.4 answers a code submission with the server's message alone
# (competition_submit_code; -q drops the rest): no success phrase, no ref. Its words were
# never recorded -- any text without those two is the real shape.
KERNEL_REPLY = "Your submission was queued"


@pytest.fixture
def no_wait(monkeypatch):
    """Read-backs look at once, at a fixed now."""
    monkeypatch.setattr(kaggle, "READBACK_DELAYS", tuple(0.0 for _ in kaggle.READBACK_DELAYS))
    monkeypatch.setattr(kaggle, "utc_now", lambda: NOW)


def _listing(*rows):
    """One ``competitions submissions --format json`` answer from ``(ref, description,
    minutes before NOW)`` rows."""
    items = [
        {
            "ref": ref,
            "fileName": "submission.csv",
            "date": stamp(NOW - timedelta(minutes=ago)),
            "description": description,
            "status": "pending",
        }
        for ref, description, ago in rows
    ]
    return 0, json.dumps(items) if items else "No submissions found", ""  # 2.2.4's empty list


def _profile(**over):
    base = dict(
        dataset="t",
        eval_dataset="d",
        plan_id="p",
        sealed_subset="holdout",
        platform="kaggle",
        competition="c1",
        board_rule="best",
        metric="accuracy",
        writer="scores_csv",
        kaggle_command=["fake-kaggle"],
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


def _staged(kind="file"):
    artifact = (
        Artifact(
            kind="file",
            path="submission.csv",
            bytes=3,
            sha256="a" * 64,
            md5="b" * 32,
            writer="scores_csv",
            writer_version="1",
            rows=1,
            samples=1,
            missing=0,
        )
        if kind == "file"
        else Artifact(
            kind="kernel",
            kernel="u/nb",
            version=7,
            output="submission.csv",
            weights=[{"run": "e", "sha256": "c" * 64}],
        )
    )
    return Staged(
        submission_id="S1",
        dataset="t",
        kind="candidate",
        eval_run="e",
        test_run="t1" if kind == "file" else None,
        pairing=Pairing(mode="single" if kind == "file" else "kernel"),
        artifact=artifact,
        gate=Gate(admission="PASS"),
        profile_sha256="p" * 64,
        staged_at=STAMP,
        vcp_version="0",
    )


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        code, out, err = self.responses.pop(0)
        return subprocess.CompletedProcess(args, code, out, err)


def test_redact():
    assert redact(f"KAGGLE_KEY={SECRET} done") == "KAGGLE_KEY=<redacted>"
    assert redact("Authorization: Bearer abc.def") == "Authorization=<redacted>"
    assert redact("token: xyz") == "token=<redacted>"
    assert redact(f"echo {SECRET}") == "echo <redacted>"
    assert redact("short id S1 ok") == "short id S1 ok"


def test_manual_platform_refuses(tmp_path):
    p = get_platform("manual")
    with pytest.raises(ValidationFailed, match="manual_platform"):
        p.upload(_staged(), tmp_path / "s.csv", "S1", _profile(platform="manual"), None)
    with pytest.raises(ValidationFailed, match="manual_platform"):
        p.list_submissions(_profile(platform="manual"), None)


def test_kaggle_upload_commands_and_outcomes(tmp_path):
    p = get_platform("kaggle")
    art = tmp_path / "submission.csv"
    art.write_text("id\n", encoding="utf-8")
    runner = FakeRunner([(0, f"KAGGLE_KEY={SECRET}\nSuccessfully submitted to c1", "")])
    res = p.upload(_staged(), art, "S1 note", _profile(), runner)
    assert res.confirmed and res.platform_ref is None and SECRET not in res.detail
    assert runner.calls == [  # the success phrase confirms it: no read-back
        ["fake-kaggle", "competitions", "submit", "-f", str(art), "-m", "S1 note", "-q", "c1"]
    ]
    runner = FakeRunner([(1, "", f"401 Unauthorized key={SECRET}")])
    with pytest.raises(PlatformError, match="exit 1") as ei:
        p.upload(_staged(), art, "S1", _profile(), runner)
    assert SECRET not in str(ei.value) and ei.value.fields == {"exit_code": 1}
    assert ei.value.status == "FAIL"


def _kernel_upload(runner):
    return get_platform("kaggle").upload(
        _staged("kernel"), None, "S1", _profile(submission_kind="kernel"), runner
    )


def test_kaggle_kernel_upload_confirms_by_reading_the_submission_back(no_wait):
    runner = FakeRunner(
        [
            (0, KERNEL_REPLY, ""),
            _listing(
                (51234, "S1", 0.05),
                (51233, "S10 other", 0.5),
                (51232, "S2 same as S1 + tta", 0.5),  # names S1, but it is S2's
                (50000, "S1", 60 * 24),
            ),
        ]
    )
    res = _kernel_upload(runner)
    assert (res.confirmed, res.platform_ref, res.readback) == (True, "51234", "matched")
    assert res.detail == KERNEL_REPLY
    assert runner.calls[0][:9] == [
        "fake-kaggle",
        "competitions",
        "submit",
        "-k",
        "u/nb",
        "-v",
        "7",
        "-f",
        "submission.csv",
    ]
    assert runner.calls[1:] == [LIST]


def test_a_submission_the_list_shows_late_is_confirmed_on_a_later_look(no_wait):
    # CLI 2.2.4 answers an empty list in plain text: a look with nothing in it, not a failure
    runner = FakeRunner([(0, KERNEL_REPLY, ""), _listing(), _listing((51234, "S1 note", 0))])
    res = _kernel_upload(runner)
    assert (res.platform_ref, res.readback) == ("51234", "matched")
    assert runner.calls[1:] == [LIST, LIST]


@pytest.mark.parametrize(
    "listed",
    [
        (50000, "S1", 60 * 24),
        (50000, "S1", 5),  # an earlier upload of S1, listed while this one is not yet
        (50000, "S1", -5),  # stamped after the window closed
        (50000, "S2 same as S1", 0),
    ],
    ids=["a-day-ago", "an-earlier-upload", "after-the-window", "another-ids-description"],
)
def test_an_upload_the_list_never_shows_stays_unconfirmed(no_wait, listed):
    looks = len(kaggle.READBACK_DELAYS)
    runner = FakeRunner([(0, KERNEL_REPLY, ""), *[_listing(listed)] * looks])
    res = _kernel_upload(runner)
    assert (res.confirmed, res.platform_ref, res.readback) == (False, None, "not_listed")
    assert res.detail == KERNEL_REPLY and runner.calls[1:] == [LIST] * looks


def test_the_window_opens_before_the_cli_started_and_closes_after_it_returned(no_wait, monkeypatch):
    """A big file can keep the CLI busy for minutes; the window is its whole run, give or take
    READBACK_SKEW, not a span around either end."""
    looks = len(kaggle.READBACK_DELAYS)
    for ago, outcome in ((-1, "matched"), (-11, "matched"), (3, "not_listed"), (-13, "not_listed")):
        clock = iter([NOW, NOW + timedelta(minutes=10)])  # the CLI started, then returned
        monkeypatch.setattr(kaggle, "utc_now", lambda clock=clock: next(clock))
        runner = FakeRunner([(0, KERNEL_REPLY, ""), *[_listing((7, "S1", ago))] * looks])
        assert _kernel_upload(runner).readback == outcome, ago


def test_two_uploads_of_one_id_in_the_window_confirm_neither(no_wait):
    runner = FakeRunner([(0, KERNEL_REPLY, ""), _listing((51234, "S1", 0), (51230, "S1 b", 1))])
    res = _kernel_upload(runner)
    assert (res.confirmed, res.platform_ref, res.readback) == (False, None, "ambiguous")
    assert len(runner.calls) == 2  # another look cannot make two into one
    # one submission listed twice (a page boundary moved) is still one submission
    runner = FakeRunner([(0, KERNEL_REPLY, ""), _listing((51234, "S1", 0), (51234, "S1", 0))])
    assert _kernel_upload(runner).platform_ref == "51234"


OUT_OF_RANGE = {"ref": 1, "fileName": "s.csv", "date": "0001-01-01T00:00:00+01:00"}


@pytest.mark.parametrize(
    "answer",
    [
        (2, "", f"503 Service Unavailable key={SECRET}"),
        (0, "<html>", ""),
        (0, json.dumps([{"date": "2026-09-25T08:00:00Z"}]), ""),
        (0, json.dumps([{**OUT_OF_RANGE, "description": "S1"}]), ""),  # OverflowError
    ],
    ids=["exit-2", "not-json", "missing-key", "date-out-of-range"],
)
def test_a_failing_read_back_leaves_the_upload_unconfirmed_instead_of_failing(
    no_wait, answer, caplog, monkeypatch
):
    monkeypatch.setattr(logging.getLogger("vcp"), "propagate", True)  # setup_logging turns it off
    runner = FakeRunner([(0, KERNEL_REPLY, ""), answer])
    with caplog.at_level(logging.WARNING, logger="vcp"):
        res = _kernel_upload(runner)
    assert (res.confirmed, res.platform_ref, res.readback) == (False, None, "failed")
    assert res.detail == KERNEL_REPLY
    assert len(runner.calls) == 2  # a list that fails is not asked again
    assert "kaggle read-back failed" in caplog.text and SECRET not in caplog.text


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (OSError("kaggle vanished"), "failed"),
        (RuntimeError("anything at all"), "failed"),
        (KeyboardInterrupt(), "interrupted"),
    ],
)
def test_a_read_back_cut_short_still_returns_the_upload(no_wait, error, outcome):
    """The CLI accepted the upload: a missing CLI, any error, or a Ctrl+C during the read-back
    stops the confirmation only, so the ledger row is still written."""
    calls = []

    def runner(args):
        calls.append(args)
        if len(calls) == 1:
            return subprocess.CompletedProcess(args, 0, KERNEL_REPLY, "")
        raise error

    res = _kernel_upload(runner)
    assert (res.confirmed, res.platform_ref, res.readback) == (False, None, outcome)
    assert len(calls) == 2


def test_a_printed_submission_ref_confirms_without_reading_back(no_wait):
    # Kaggle CLIs after 2.2.4 print the ref ahead of the server's message.
    runner = FakeRunner([(0, f"Submission ref: 51234\n{KERNEL_REPLY}", "")])
    res = _kernel_upload(runner)
    assert (res.confirmed, res.platform_ref, res.readback) == (True, "51234", None)
    assert res.detail == KERNEL_REPLY and len(runner.calls) == 1


def test_a_file_the_cli_could_not_submit_fails_although_the_cli_exited_0(tmp_path, no_wait):
    """CLI 2.2.4 exits 0 when the file never reached Kaggle: nothing was submitted, so there is
    nothing to read back and no row to write."""
    art = tmp_path / "submission.csv"
    art.write_text("id\n", encoding="utf-8")
    runner = FakeRunner([(0, "Could not submit to competition", "")])
    with pytest.raises(PlatformError, match="upload_failed") as ei:
        get_platform("kaggle").upload(_staged(), art, "S1", _profile(), runner)
    assert ei.value.status == "FAIL" and len(runner.calls) == 1


def test_kaggle_command_must_exist_without_a_runner(tmp_path):
    with pytest.raises(VcpError, match="kaggle_not_found"):
        get_platform("kaggle").list_submissions(_profile(kaggle_command=["no-such-exe-xyz"]), None)


def test_parse_submissions_shapes_and_paging():
    rows = [
        {
            "ref": 11,
            "fileName": "submission.csv",
            "date": "2026-08-31T21:28:00Z",
            "description": "S1",
            "status": "complete",
            "publicScore": "0.79",
            "privateScore": None,
            "submittedBy": "eric",
        },
        {"fileName": "x.csv", "date": "2026-08-31 22:00:00", "publicScore": ""},
    ]
    subs, token = parse_submissions(json.dumps(rows))
    assert token is None and len(subs) == 2
    assert subs[0].platform_ref == "11" and subs[0].public == 0.79 and subs[0].private is None
    assert subs[0].at == "2026-08-31T21:28:00.000Z" and subs[0].submitted_by == "eric"
    assert subs[1].platform_ref and subs[1].public is None and subs[1].submitted_by is None
    subs, token = parse_submissions(json.dumps({"submissions": rows[:1], "nextPageToken": "p2"}))
    assert token == "p2" and len(subs) == 1
    with pytest.raises(ValidationFailed, match="platform_response") as ei:
        parse_submissions(json.dumps([{"date": "2026-08-31T21:28:00Z"}]))
    assert ei.value.fields == {"key": "fileName"}
    with pytest.raises(ValidationFailed, match="not JSON"):
        parse_submissions("<html>")
    assert parse_submissions("No submissions found\n") == ([], None)  # CLI 2.2.4, no JSON
    assert parse_score("null", "publicScore") is None and parse_score(0.5, "x") == 0.5
    with pytest.raises(ValidationFailed, match="unparsable score"):
        parse_score("n/a", "publicScore")
    assert parse_date("2026-08-31T21:28:00+02:00") == "2026-08-31T19:28:00.000Z"
    with pytest.raises(ValidationFailed, match="unparsable date"):
        parse_date("yesterday")


def test_parse_submissions_redacts_platform_strings():
    rows = [
        {
            "ref": 7,
            "fileName": SECRET,
            "date": "2026-08-31T21:28:00Z",
            "status": SECRET,
            "submittedBy": SECRET,
        }
    ]
    subs, _ = parse_submissions(json.dumps(rows))
    sub = subs[0]
    assert SECRET not in sub.file_name and "<redacted>" in sub.file_name
    assert SECRET not in sub.status and "<redacted>" in sub.status
    assert sub.submitted_by is not None
    assert SECRET not in sub.submitted_by and "<redacted>" in sub.submitted_by


def test_parse_submissions_ref_zero_is_not_the_sha_fallback():
    rows = [{"ref": 0, "fileName": "x.csv", "date": "2026-08-31T21:28:00Z"}]
    subs, _ = parse_submissions(json.dumps(rows))
    assert subs[0].platform_ref == "0"


def test_kaggle_list_pages_to_the_end():
    p = get_platform("kaggle")
    page1 = json.dumps(
        {
            "submissions": [{"fileName": "a.csv", "date": "2026-09-01T00:00:00Z"}],
            "nextPageToken": "t2",
        }
    )
    page2 = json.dumps([{"fileName": "b.csv", "date": "2026-09-02T00:00:00Z"}])
    runner = FakeRunner([(0, page1, ""), (0, page2, "")])
    subs = p.list_submissions(_profile(), runner)
    assert [s.file_name for s in subs] == ["a.csv", "b.csv"]
    assert runner.calls[1][-2:] == ["--page-token", "t2"]
    assert runner.calls[0][:6] == [
        "fake-kaggle",
        "competitions",
        "submissions",
        "--format",
        "json",
        "--page-size",
    ]
    runner = FakeRunner([(2, "", f"boom {SECRET}")])
    with pytest.raises(PlatformError) as ei:
        p.list_submissions(_profile(), runner)
    assert SECRET not in str(ei.value)


def test_parse_errors_redact_the_raw_value():
    with pytest.raises(ValidationFailed, match="unparsable score") as ei:
        parse_score(f"key={SECRET}", "publicScore")
    assert SECRET not in str(ei.value)
    with pytest.raises(ValidationFailed, match="unparsable date") as ei:
        parse_date(f"token: {SECRET}")
    assert SECRET not in str(ei.value)


def test_failure_message_falls_back_to_stdout(tmp_path):
    p = get_platform("kaggle")
    art = tmp_path / "submission.csv"
    art.write_text("id\n", encoding="utf-8")
    runner = FakeRunner([(1, f"denied on stdout key={SECRET}", " \n")])
    with pytest.raises(PlatformError, match="denied on stdout") as ei:
        p.upload(_staged(), art, "S1", _profile(), runner)
    assert SECRET not in str(ei.value)
