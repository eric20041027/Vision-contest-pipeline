import pytest

from vcp.core.errors import ValidationFailed
from vcp.submit.ledger import SubmissionLedger, append_ledger_row
from vcp.submit.schema import Gate, LedgerRow

T0 = "2026-09-05T00:00:00.000Z"
T1 = "2026-09-05T01:00:00.000Z"
T2 = "2026-09-05T02:00:00.000Z"


def _staged(sid, kind="candidate"):
    return LedgerRow(
        event="staged",
        ts=T0,
        submission_id=sid,
        kind=kind,
        eval_run="e",
        gate=Gate(admission="PASS"),
        profile_sha256="p" * 64,
    )


def _uploaded(sid, at, ref=None):
    return LedgerRow(
        event="uploaded",
        ts=at,
        submission_id=sid,
        at=at,
        source="manual",
        platform_ref=ref,
        confirmed=True,
        profile_sha256="p" * 64,
    )


def test_rows_round_trip_without_nulls(tmp_path):
    path = tmp_path / "submissions.jsonl"
    led = SubmissionLedger(path)
    assert led.rows == []
    led.append(_staged("S1"))
    led.append(_uploaded("S1", T1, ref="k1"))
    led.append(LedgerRow(event="scored", ts=T2, submission_id="S1", source="manual", public=0.5))
    text = path.read_text(encoding="utf-8")
    assert "null" not in text and text.count("\n") == 3
    again = SubmissionLedger(path)
    assert again.rows == led.rows
    assert again.ids() == ["S1"]
    assert again.staged("S1").kind == "candidate" and again.staged("S9") is None
    assert [r.at for r in again.uploads("S1")] == [T1]
    assert again.latest_score("S1").public == 0.5 and again.latest_score("S2") is None


def test_arrivals_last_uploaded_and_foreign_refs(tmp_path):
    led = SubmissionLedger(tmp_path / "s.jsonl")
    led.append(_staged("S1"))
    led.append(_staged("S2"))
    led.append(_uploaded("S2", T2))
    led.append(
        LedgerRow(event="foreign", ts=T2, platform_ref="f1", file_name="x.csv", at=T1, public=0.1)
    )
    led.append(_uploaded("S1", T0))
    assert [r.at for r in led.arrivals()] == [T0, T1, T2]
    assert led.last_uploaded().submission_id == "S2"
    assert led.foreign_refs() == {"f1"}


def test_arrivals_use_the_latest_snapshot_of_a_foreign_ref(tmp_path):
    led = SubmissionLedger(tmp_path / "s.jsonl")
    led.append(
        LedgerRow(
            event="foreign",
            ts=T1,
            platform_ref="f1",
            file_name="x.csv",
            at=T0,
            platform_status="pending",
        )
    )
    led.append(
        LedgerRow(
            event="foreign",
            ts=T2,
            platform_ref="f1",
            file_name="x.csv",
            at=T0,
            platform_status="complete",
            public=0.935,
        )
    )
    arrivals = led.arrivals()
    assert len(arrivals) == 1
    assert arrivals[0].public == 0.935 and arrivals[0].platform_status == "complete"
    assert led.latest_foreign("f1") is arrivals[0]


def test_lock_state_and_latest_final(tmp_path):
    led = SubmissionLedger(tmp_path / "s.jsonl")
    assert led.lock_state() is None and led.latest_final() is None
    led.append(LedgerRow(event="lock", ts=T0, reason="r0"))
    assert led.lock_state().reason == "r0"
    led.append(LedgerRow(event="unlock", ts=T1, reason="extended"))
    assert led.lock_state() is None
    led.append(
        LedgerRow(
            event="final",
            ts=T2,
            rule="best_sealed",
            slots=1,
            chosen=["S1"],
            table=[],
            metric="accuracy",
            params={},
            holdout_unseals=2,
            profile_sha256="p" * 64,
        )
    )
    led.append(LedgerRow(event="lock", ts=T2, reason="final"))
    assert led.latest_final().chosen == ["S1"] and led.lock_state().reason == "final"


def test_bad_row_is_located(tmp_path):
    path = tmp_path / "s.jsonl"
    append_ledger_row(path, LedgerRow(event="note", ts=T0, text="hi"))
    with path.open("a", encoding="utf-8") as f:
        f.write('{"event": "lock", "ts": "x"}\n')
    with pytest.raises(ValidationFailed, match="s.jsonl:2"):
        SubmissionLedger(path)
