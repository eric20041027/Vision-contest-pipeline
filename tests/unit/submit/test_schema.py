from typing import get_args
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.submit.schema import (
    _REQUIRED,
    EVENTS,
    Artifact,
    Event,
    Gate,
    LedgerRow,
    PlatformProfile,
    Quota,
)

STAMP = "2026-09-05T00:00:00.000Z"


def test_event_requirements_cover_exactly_the_event_type():
    assert EVENTS == get_args(Event)
    assert set(_REQUIRED) == set(EVENTS)


def _profile(**over):
    base = dict(
        dataset="t",
        eval_dataset="d",
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


def test_tzdata_is_available():
    assert ZoneInfo("Asia/Taipei") is not None


def test_profile_defaults_and_display_tz():
    p = _profile()
    assert p.test_plan == "all-v1" and p.test_subset == "test" and p.final_slots == 1
    assert p.kaggle_command == ["kaggle"] and p.quota is None
    assert p.effective_display_tz() == "UTC"
    q = _profile(quota={"per_day": 3, "day_tz": "Asia/Taipei"})
    assert q.quota == Quota(per_day=3, day_tz="Asia/Taipei", day_start="00:00")
    assert q.effective_display_tz() == "Asia/Taipei"
    assert _profile(display_tz="America/New_York").effective_display_tz() == "America/New_York"


@pytest.mark.parametrize(
    "bad",
    [
        {"kaggle_key": "abc"},
        {"platform": "kaggle"},
        {"writer": None},
        {"display_tz": "Mars/Olympus"},
        {"deadline": "2026-09-05 00:00"},
        {"quota": {"per_day": 0}},
        {"quota": {"per_day": 3, "day_start": "24:00"}},
        {"quota": {"per_day": 3, "day_tz": "Nowhere/City"}},
        {"final_slots": 0},
        {"kaggle_command": []},
        {"board_rule": "median"},
    ],
)
def test_profile_rejects(bad):
    with pytest.raises(ValidationError):
        _profile(**bad)


def test_kernel_profile_needs_no_writer():
    p = _profile(platform="kaggle", competition="c1", submission_kind="kernel", writer=None)
    assert p.writer is None and p.competition == "c1"


def test_artifact_shapes():
    Artifact(
        kind="file",
        path="submission.csv",
        bytes=1,
        sha256="a" * 64,
        md5="b" * 32,
        writer="scores_csv",
        writer_version="1",
        rows=1,
        samples=1,
        missing=0,
    )
    with pytest.raises(ValidationError, match="file artifact needs"):
        Artifact(kind="file", path="submission.csv")
    Artifact(
        kind="kernel",
        kernel="u/nb",
        version=3,
        output="submission.csv",
        weights=[{"run": "r", "sha256": "c" * 64}],
    )
    with pytest.raises(ValidationError, match="kernel artifact needs at least one"):
        Artifact(kind="kernel", kernel="u/nb", version=3, output="submission.csv")


def test_ledger_rows_require_their_event_fields():
    assert EVENTS == (
        "staged",
        "uploaded",
        "scored",
        "errored",
        "foreign",
        "final",
        "lock",
        "unlock",
        "note",
    )
    gate = Gate(admission="PASS", judgements=["p1"])
    LedgerRow(
        event="staged",
        ts=STAMP,
        submission_id="S1",
        kind="candidate",
        eval_run="e",
        gate=gate,
        profile_sha256="p" * 64,
    )
    with pytest.raises(ValidationError, match="staged needs"):
        LedgerRow(event="staged", ts=STAMP, submission_id="S1")
    LedgerRow(
        event="uploaded",
        ts=STAMP,
        submission_id="S1",
        at=STAMP,
        source="vcp",
        confirmed=True,
        profile_sha256="p" * 64,
    )
    with pytest.raises(ValidationError, match="scored needs a public or private"):
        LedgerRow(event="scored", ts=STAMP, submission_id="S1", source="manual")
    with pytest.raises(ValidationError, match="finite"):
        LedgerRow(
            event="scored", ts=STAMP, submission_id="S1", source="manual", public=float("nan")
        )
    LedgerRow(event="lock", ts=STAMP, reason="final")
    with pytest.raises(ValidationError, match="lock needs"):
        LedgerRow(event="lock", ts=STAMP)
    LedgerRow(event="foreign", ts=STAMP, platform_ref="x", file_name="f.csv", at=STAMP)
    with pytest.raises(ValidationError):
        LedgerRow(event="party", ts=STAMP)


ERRORED = dict(
    event="errored",
    ts=STAMP,
    submission_id="S1",
    source="platform",
    at=STAMP,
    platform_ref="90000001",
    platform_status="SubmissionStatus.COMPLETE",
)


def test_an_errored_row_carries_the_platform_entry_and_no_score():
    """spec 2026-10-09 §3.2: the id, where it came from, the platform's time, ref and status."""
    row = LedgerRow(**ERRORED)
    assert row.event == "errored" and "errored" in EVENTS
    assert LedgerRow.model_validate_json(row.model_dump_json(exclude_none=True)) == row


@pytest.mark.parametrize(
    "field", ["submission_id", "source", "at", "platform_ref", "platform_status"]
)
def test_an_errored_row_without_a_required_field_fails(field):
    with pytest.raises(ValidationError, match=rf"errored needs \['{field}'\]"):
        LedgerRow(**{**ERRORED, field: None})


@pytest.mark.parametrize("score", ["public", "private"])
def test_an_errored_row_with_a_score_fails(score):
    with pytest.raises(ValidationError, match="errored needs no score"):
        LedgerRow(**{**ERRORED, score: 0.8})


def test_ledger_row_ts_and_at_must_be_stamps():
    with pytest.raises(ValidationError):
        LedgerRow(event="lock", ts="2026-09-05 00:00", reason="x")
    LedgerRow(
        event="foreign",
        ts=STAMP,
        platform_ref="x",
        file_name="f.csv",
        at="2026-09-05T00:00:00Z",
    )


def test_paths(roots):
    paths = DatasetPaths.resolve("t", data_root=roots.data, configs_root=roots.configs)
    assert paths.submit_yaml == roots.configs / "datasets" / "t" / "submit.yaml"
    assert paths.submissions_log == roots.configs / "datasets" / "t" / "submissions.jsonl"
    assert paths.submit_dir == roots.data / "submit" / "t"
    assert paths.submission_dir("S1") == roots.data / "submit" / "t" / "S1"
    with pytest.raises(ValidationFailed):
        paths.submission_dir("bad name")


def test_the_ledger_field_defaults_to_configs_and_is_written_only_when_shared():
    """spec 2026-09-28 §3.1, §7: ``ledger: configs`` stays out of the file, so a profile that
    keeps the configs ledger is what 0.11 wrote and reads."""
    p = _profile()
    assert p.ledger == "configs" and "ledger" not in p.model_dump(mode="json")
    shared = _profile(ledger="shared")
    assert shared.model_dump(mode="json")["ledger"] == "shared"
    assert PlatformProfile.model_validate(shared.model_dump(mode="json")) == shared
    with pytest.raises(ValidationError):
        _profile(ledger="git")
