from datetime import timedelta

import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.submit.guards import assert_before_deadline, assert_quota, assert_unlocked, quota_state
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.schema import LedgerRow, PlatformProfile, Quota

T0 = "2026-09-05T00:00:00.000Z"


def _profile(**over):
    base = dict(
        dataset="t",
        eval_dataset="d",
        plan_id="p",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        quota=Quota(per_day=2, day_tz="UTC"),
        created_at=T0,
    )
    return PlatformProfile(**{**base, **over})


def _uploaded(sid, at):
    return LedgerRow(
        event="uploaded",
        ts=at,
        submission_id=sid,
        at=at,
        source="manual",
        confirmed=True,
        profile_sha256="p" * 64,
    )


def test_lock_allows_only_the_chosen_after_final(tmp_path):
    led = SubmissionLedger(tmp_path / "s.jsonl")
    assert_unlocked(led)
    led.append(LedgerRow(event="lock", ts=T0, reason="r0"))
    with pytest.raises(ValidationFailed, match="locked: since") as ei:
        assert_unlocked(led)
    assert ei.value.fields == {"since": T0, "why": "r0"}
    with pytest.raises(ValidationFailed, match="locked"):
        assert_unlocked(led, submission_id="S1")
    led.append(
        LedgerRow(
            event="final",
            ts=T0,
            rule="best_sealed",
            slots=1,
            chosen=["S1"],
            table=[],
            metric="accuracy",
            params={},
            holdout_unseals=0,
            profile_sha256="p" * 64,
        )
    )
    assert_unlocked(led, submission_id="S1")
    with pytest.raises(ValidationFailed, match="locked"):
        assert_unlocked(led, submission_id="S2")


def test_deadline():
    p = _profile(deadline="2026-09-05T12:00:00Z")
    assert_before_deadline(p, parse_stamp("2026-09-05T11:59:59Z"))
    with pytest.raises(ValidationFailed, match="past_deadline") as ei:
        assert_before_deadline(p, parse_stamp("2026-09-05T12:00:00Z"))
    assert ei.value.fields == {"deadline": "2026-09-05T12:00:00Z"}
    assert_before_deadline(_profile(), parse_stamp("2999-01-01T00:00:00Z"))


def test_quota_counts_uploaded_and_foreign(tmp_path):
    led = SubmissionLedger(tmp_path / "s.jsonl")
    now = utc_now()
    assert quota_state(led, _profile(quota=None), now) is None
    state = quota_state(led, _profile(), now)
    assert state.used == 0 and state.remaining == 2
    led.append(_uploaded("S1", stamp(now)))
    led.append(
        LedgerRow(event="foreign", ts=stamp(now), platform_ref="f", file_name="x", at=stamp(now))
    )
    led.append(_uploaded("S0", stamp(now - timedelta(days=2))))
    state = quota_state(led, _profile(), now)
    assert state.used == 2 and state.remaining == 0
    state = quota_state(led, _profile(display_tz="Asia/Taipei"), now)
    with pytest.raises(ValidationFailed, match="quota_exhausted") as ei:
        assert_quota(state)
    assert ei.value.fields["quota"] == "2/2" and ei.value.fields["resets_at"].endswith("Z")
    assert "CST" in ei.value.fields["local"]
    assert_quota(None)
