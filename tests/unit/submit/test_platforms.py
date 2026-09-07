import json
import subprocess

import pytest

from vcp.core.errors import PlatformError, ValidationFailed, VcpError
from vcp.submit.platforms import get_platform
from vcp.submit.platforms.base import redact
from vcp.submit.platforms.kaggle import parse_date, parse_score, parse_submissions
from vcp.submit.schema import Artifact, Gate, Pairing, PlatformProfile, Staged

SECRET = "fakesecretfakesecretfakesecret1234"
STAMP = "2026-09-05T00:00:00.000Z"


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
    assert runner.calls == [
        ["fake-kaggle", "competitions", "submit", "-f", str(art), "-m", "S1 note", "-q", "c1"]
    ]
    runner = FakeRunner([(0, "Submission queued", "")])
    res = p.upload(_staged("kernel"), None, "S1", _profile(submission_kind="kernel"), runner)
    assert not res.confirmed
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
    runner = FakeRunner([(1, "", f"401 Unauthorized key={SECRET}")])
    with pytest.raises(PlatformError, match="exit 1") as ei:
        p.upload(_staged(), art, "S1", _profile(), runner)
    assert SECRET not in str(ei.value) and ei.value.fields == {"exit_code": 1}
    assert ei.value.status == "FAIL"


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
