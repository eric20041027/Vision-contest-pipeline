import pytest

from vcp.core.errors import ValidationFailed
from vcp.submit.ledger import TWIN_WINDOW, SubmissionLedger, append_ledger_row
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


def _foreign(ref, at):
    return LedgerRow(event="foreign", ts=T2, platform_ref=ref, file_name="submission.csv", at=at)


def _scored(sid, ref, at):
    return LedgerRow(
        event="scored",
        ts=T2,
        submission_id=sid,
        source="platform",
        public=0.5,
        at=at,
        platform_ref=ref,
    )


def _arrivals(path, rows):
    led = SubmissionLedger(path)
    for row in rows:
        led.append(row)
    return [(r.event, r.submission_id or r.platform_ref) for r in led.arrivals()]


def test_a_foreign_row_for_our_own_upload_is_one_arrival(tmp_path):
    """VCP-038: a ledger that did not know S1 yet (another worktree, an upload recorded later)
    wrote the platform's S1 as foreign. Once a ref ties that row to S1 -- a sync ``scored`` row
    or the upload's own ref -- the upload and the foreign row are one arrival, not two."""
    by_score = [_staged("S1"), _uploaded("S1", T1), _foreign("k7", T1), _scored("S1", "k7", T1)]
    assert _arrivals(tmp_path / "a.jsonl", by_score) == [("uploaded", "S1")]
    by_ref = [_staged("S1"), _foreign("k7", T1), _uploaded("S1", T1, ref="k7")]
    assert _arrivals(tmp_path / "b.jsonl", by_ref) == [("uploaded", "S1")]


def test_a_claimed_ref_with_no_upload_row_to_absorb_it_still_counts(tmp_path):
    # S1 went up by hand on the web and was never recorded: the foreign row is its only arrival
    rows = [_staged("S1"), _foreign("k7", T1), _scored("S1", "k7", T1)]
    assert _arrivals(tmp_path / "s.jsonl", rows) == [("foreign", "k7")]


def _at(hour, minute=0, day=5):
    return f"2026-09-{day:02d}T{hour:02d}:{minute:02d}:00.000Z"


def test_an_upload_absorbs_its_own_twin_before_a_nearby_web_upload(tmp_path):
    """Closest pairs first: S1 went up through vcp at 10:00 (another ledger wrote its platform
    copy kV as foreign) and by hand at 09:55 (kW, never recorded). Taking refs in time order
    would hand the upload to kW and leave its own twin counted."""
    path = tmp_path / "s.jsonl"
    rows = [
        _staged("S1"),
        _uploaded("S1", _at(10)),
        _foreign("kW", _at(9, 55)),
        _foreign("kV", _at(10)),
        _scored("S1", "kW", _at(9, 55)),
        _scored("S1", "kV", _at(10)),
    ]
    assert _arrivals(path, rows) == [("foreign", "kW"), ("uploaded", "S1")]
    assert SubmissionLedger(path).last_uploaded().event == "uploaded"  # final's needs_reupload


def test_a_tied_ref_far_from_every_upload_still_counts(tmp_path):
    """Only within TWIN_WINDOW: S1's upload on the 5th cannot be the S1 sent by hand on the
    6th, whatever ties that ref to S1 -- absorbing it would free a slot on the 6th."""
    rows = [
        _staged("S1"),
        _uploaded("S1", _at(10, day=5)),
        _foreign("kV", _at(10, day=5)),  # its twin, not tied (no scored row yet)
        _foreign("kW", _at(10, day=6)),
        _scored("S1", "kW", _at(10, day=6)),
    ]
    assert _arrivals(tmp_path / "s.jsonl", rows) == [
        ("uploaded", "S1"),
        ("foreign", "kV"),
        ("foreign", "kW"),
    ]


def test_each_upload_absorbs_the_twin_closest_to_it(tmp_path):
    rows = [
        _staged("S1"),
        _uploaded("S1", _at(12)),  # recorded first, happened last
        _uploaded("S1", _at(10)),
        _foreign("k10", _at(10, 1)),
        _foreign("k12", _at(12, 1)),
        _scored("S1", "k12", _at(12, 1)),
        _scored("S1", "k10", _at(10, 1)),
    ]
    assert _arrivals(tmp_path / "s.jsonl", rows) == [("uploaded", "S1"), ("uploaded", "S1")]


def test_a_ref_tied_to_two_ids_goes_to_the_one_with_an_upload_to_absorb_it(tmp_path):
    rows = [
        _staged("S1"),
        _staged("S2"),
        _uploaded("S2", _at(10)),
        _foreign("k", _at(10, 1)),
        _scored("S2", "k", _at(10, 1)),
        _scored("S1", "k", _at(10, 1)),  # the later tie names an id with no upload
    ]
    assert _arrivals(tmp_path / "s.jsonl", rows) == [("uploaded", "S2")]


def test_the_twin_window_is_syncs_match_window():
    from vcp.submit.sync import MATCH_WINDOW

    assert TWIN_WINDOW == MATCH_WINDOW


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
