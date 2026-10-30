"""The shared judgement of what was ticked on the platform (spec 2026-10-30 §3.4, controller
rulings C1 and C4): ``selection_state`` and the lines that say it."""

from datetime import timedelta

import pytest

from submit_fixtures import (
    EVAL,
    STAMP,
    TEST,
    final_row,
    foreign_row,
    ledger_of,
    seed_ledger,
    selected_row,
    staged_row,
    uploaded_row,
)
from vcp.core.errors import ValidationFailed
from vcp.core.time import parse_stamp, stamp
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow, PlatformProfile
from vcp.submit.select import differs_line, label, select, selection_lines, selection_state

NOW = parse_stamp("2026-10-20T00:00:00.000Z")


def _at(hours: float) -> str:
    return stamp(NOW + timedelta(hours=hours))


def _profile(**over) -> PlatformProfile:
    base = dict(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="best",
        metric="accuracy",
        writer="scores_csv",
        final_slots=2,
        deadline=None,
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


def up(*sids):
    """Our uploads, one an hour apart, the last five hours before NOW at the earliest."""
    return [uploaded_row(sid, _at(-5 + i)) for i, sid in enumerate(sids)]


@pytest.mark.parametrize(
    ("hours_left", "warn", "words"),
    [
        (72, 48, "none"),
        (47, 48, "missing"),
        (-1, 48, "missing"),
        (71, 72, "missing"),
        (1, None, "none"),
        (-1, None, "none"),
    ],
)
def test_missing_starts_selection_warn_hours_before_the_deadline(tmp_path, hours_left, warn, words):
    st = selection_state(
        ledger_of(tmp_path, *up("S1", "S2")),
        _profile(deadline=_at(hours_left), selection_warn_hours=warn),
        NOW,
    )
    assert st.words == words and st.chosen is None
    assert st.due_at == (None if warn is None else _at(hours_left - warn))


def test_zero_warn_hours_means_from_the_deadline_moment(tmp_path):
    ledger = ledger_of(tmp_path, *up("S1"))
    on_time = _profile(deadline=_at(0), selection_warn_hours=0)
    assert selection_state(ledger, on_time, NOW).words == "missing"
    early = _profile(deadline=_at(1), selection_warn_hours=0)
    assert selection_state(ledger, early, NOW).words == "none"


def test_without_a_deadline_the_final_row_is_the_moment_to_select(tmp_path):
    ledger = ledger_of(tmp_path, *up("S1", "S2"))
    st = selection_state(ledger, _profile(), NOW)
    assert st.words == "none" and st.due_at is None and st.deadline is None
    fin = final_row(["S1"], 1)
    ledger.append(fin)
    st = selection_state(ledger, _profile(), NOW)
    assert st.words == "missing" and st.due_at == fin.ts
    off = selection_state(ledger, _profile(selection_warn_hours=None), NOW)
    assert off.words == "none" and off.due_at is None


def test_a_selection_equal_to_final_with_the_right_count_is_ok(tmp_path):
    fin, sel = final_row(["S1", "S2"], 2), selected_row(["S2", "S1"])
    ledger = ledger_of(tmp_path, *up("S1", "S2"), fin, sel)
    st = selection_state(ledger, _profile(), NOW)
    assert st.to_json() == {
        "chosen": ["S2", "S1"],
        "recorded_at": sel.ts,
        "words": "ok",
        "expected": 2,
        "final": ["S1", "S2"],
        "due_at": fin.ts,
    }
    assert not st.warns


def test_expected_is_never_more_than_the_entries_there_are(tmp_path):
    ledger = ledger_of(tmp_path, *up("S1"), selected_row(["S1"]))
    st = selection_state(ledger, _profile(final_slots=2), NOW)
    assert st.words == "ok" and st.expected == 1


def test_expected_is_at_least_one(tmp_path):
    st = selection_state(ledger_of(tmp_path, selected_row(["S1"])), _profile(), NOW)
    assert st.expected == 1 and st.words == "ok"


def test_the_slots_of_the_newest_final_row_win_over_final_slots(tmp_path):
    ledger = ledger_of(tmp_path, *up("S1", "S2"), final_row(["S1"], 1), selected_row(["S1"]))
    st = selection_state(ledger, _profile(final_slots=2), NOW)
    assert st.words == "ok" and st.expected == 1 and st.slots == 1
    # an older final row does not count once a newer one overrode the slots
    ledger.append(final_row(["S1", "S2"], 2))
    again = selection_state(ledger, _profile(final_slots=1), NOW)
    assert again.slots == 2 and again.expected == 2 and again.words == "slots,differs"


def test_count_and_set_both_wrong_say_slots_then_differs(tmp_path):
    ledger = ledger_of(
        tmp_path, *up("S1", "S2", "S3"), final_row(["S1"], 1), selected_row(["S2", "S3"])
    )
    st = selection_state(ledger, _profile(), NOW)
    assert st.words == "slots,differs" and st.warns


def test_each_word_can_stand_alone(tmp_path):
    base = [*up("S1", "S2", "S3")]
    wrong_count = ledger_of(tmp_path / "a", *base, selected_row(["S1"]))
    assert selection_state(wrong_count, _profile(), NOW).words == "slots"
    other_set = ledger_of(
        tmp_path / "b", *base, final_row(["S1", "S2"], 2), selected_row(["S1", "S3"])
    )
    st = selection_state(other_set, _profile(), NOW)
    assert st.words == "differs" and st.warns


def test_without_a_final_row_nothing_is_compared_with_it(tmp_path):
    ledger = ledger_of(tmp_path, *up("S1", "S2"), selected_row(["S2"]))
    st = selection_state(ledger, _profile(final_slots=1), NOW)
    assert st.words == "ok" and st.final is None


def test_the_newest_selected_row_is_the_selection(tmp_path):
    first, second = selected_row(["S2"], ts=_at(-3)), selected_row(["S1"], ts=_at(-2))
    ledger = ledger_of(tmp_path, *up("S1", "S2"), final_row(["S1"], 1), first, second)
    st = selection_state(ledger, _profile(), NOW)
    assert st.words == "ok" and st.chosen == ["S1"] and st.recorded_at == second.ts


def test_latest_selected_is_the_last_row_in_ledger_order(tmp_path):
    assert ledger_of(tmp_path / "none", *up("S1")).latest_selected() is None
    late, early = selected_row(["S1"], ts=_at(-1)), selected_row(["S2"], ts=_at(-9))
    ledger = ledger_of(tmp_path / "some", *up("S1", "S2"), late, early)
    assert ledger.latest_selected() is early  # file order, not the stamps


def test_foreign_entries_count_and_are_labelled_foreign_ref(tmp_path):
    foreign = foreign_row("k9", _at(-3))
    ledger = ledger_of(tmp_path, *up("S1"), foreign, selected_row(["S1", "foreign:k9"]))
    st = selection_state(ledger, _profile(), NOW)
    assert st.words == "ok" and st.expected == 2
    assert label(foreign) == "foreign:k9" and label(uploaded_row("S1", _at(-1))) == "S1"


def test_board_rule_last_is_never_checked(tmp_path):
    profile = _profile(board_rule="last", deadline=_at(-1))
    ledger = ledger_of(tmp_path, *up("S1", "S2"))
    assert selection_state(ledger, profile, NOW).words == "none"
    ledger.append(final_row(["S1"], 1))
    ledger.append(selected_row(["S2"]))
    st = selection_state(ledger, profile, NOW)
    assert st.words == "none" and st.chosen == ["S2"] and not st.warns
    assert st.final == ["S1"] and st.expected == 1 and st.due_at == _at(-49)


def test_selection_lines_and_differs_line(tmp_path):
    missing = selection_state(ledger_of(tmp_path / "a"), _profile(deadline=_at(24)), NOW)
    assert selection_lines(missing) == [
        "selection: missing (deadline 2026-10-21T00:00:00.000Z, "
        "warning since 2026-10-19T00:00:00.000Z)"
    ]
    both = selection_state(
        ledger_of(
            tmp_path / "b",
            *up("S1", "S2", "S3"),
            final_row(["S1"], 1),
            selected_row(["S2", "S3"]),
        ),
        _profile(),
        NOW,
    )
    assert selection_lines(both) == [
        "selection: 2 recorded, 1 expected (slots=1)",
        "selection: final chose S1; recorded S2,S3 (not selected: S1; selected, not final: S2,S3)",
    ]
    assert differs_line(["S1", "S2"], ["S1"]) == (
        "selection: final chose S1,S2; recorded S1 (not selected: S2; selected, not final: -)"
    )
    assert differs_line([], ["S1"]) == (
        "selection: final chose -; recorded S1 (not selected: -; selected, not final: S1)"
    )


def test_a_missing_line_without_a_deadline_names_the_final_row(tmp_path):
    fin = final_row(["S1"], 1)
    st = selection_state(ledger_of(tmp_path, *up("S1"), fin), _profile(), NOW)
    assert selection_lines(st) == [f"selection: missing (no deadline, warning since {fin.ts})"]


@pytest.mark.parametrize("selected", [False, True])
def test_nothing_to_say_when_none_or_ok(tmp_path, selected):
    rows = [*up("S1", "S2")] + ([selected_row(["S1", "S2"])] if selected else [])
    st = selection_state(ledger_of(tmp_path, *rows), _profile(), NOW)
    assert st.words == ("ok" if selected else "none") and selection_lines(st) == []


def test_seed_ledger_writes_the_profile_and_the_configs_ledger(roots):
    rows = [staged_row("S1"), *up("S1")]
    paths = seed_ledger(roots, _profile(), *rows)
    assert paths.submit_yaml.is_file() and paths.submissions_log.is_file()
    assert load_profile(paths)[0] == _profile()
    assert paths.submissions_log.read_bytes().count(b"\n") == 2


def _kw(roots) -> dict:
    return {"data_root": roots.data, "configs_root": roots.configs}


def _seeded(roots, *extra: LedgerRow, **over):
    """S1 S2 S3 staged, S1 S2 uploaded, a foreign upload k9; ``extra`` rows after them."""
    return seed_ledger(
        roots,
        _profile(**over),
        staged_row("S1"),
        staged_row("S2"),
        staged_row("S3"),
        uploaded_row("S1", stamp()),
        uploaded_row("S2", stamp()),
        foreign_row("k9", stamp()),
        *extra,
    )


def _events(paths) -> list[str]:
    return [r.event for r in SubmissionLedger(paths.submissions_log).rows]


def test_select_records_the_ticked_ids_once(roots):
    paths = _seeded(roots, final_slots=3)
    out = select(TEST, ["S1", "S2", "foreign:k9"], message="web", **_kw(roots))
    assert out.written
    assert (out.row.event, out.row.source, out.row.message) == ("selected", "manual", "web")
    assert out.row.chosen == ["S1", "S2", "foreign:k9"] and out.state.words == "ok"
    again = select(TEST, ["foreign:k9", "S2", "S1"], **_kw(roots))
    assert not again.written and again.row == out.row and again.state.words == "ok"
    assert _events(paths).count("selected") == 1


@pytest.mark.parametrize(
    ("entries", "word"),
    [
        ([], "invalid:"),
        (["S1", "S1"], "invalid:"),
        (["S9"], "not_staged:"),
        (["S3"], "not_uploaded:"),
        (["foreign:zz"], "not_found:"),
    ],
)
def test_select_refuses_and_writes_nothing(roots, entries, word):
    paths = _seeded(roots)
    before = paths.submissions_log.read_bytes()
    with pytest.raises(ValidationFailed, match=f"^{word}") as ei:
        select(TEST, entries, **_kw(roots))
    assert paths.submissions_log.read_bytes() == before
    if word != "invalid:":
        assert ei.value.fields["id"] == entries[0]


def test_select_names_the_first_entry_that_fails_in_input_order(roots):
    _seeded(roots)
    with pytest.raises(ValidationFailed, match="^not_uploaded:") as ei:
        select(TEST, ["S1", "S3", "S9"], **_kw(roots))
    assert ei.value.fields["id"] == "S3"
    with pytest.raises(ValidationFailed, match="^not_staged:") as ei:
        select(TEST, ["S1", "S9", "S3"], **_kw(roots))
    assert ei.value.fields["id"] == "S9"


def test_select_records_after_final_locked_the_ledger(roots):
    lock = LedgerRow(event="lock", ts=stamp(), reason="final")
    paths = _seeded(roots, final_row(["S1"], 1), lock)
    assert SubmissionLedger(paths.submissions_log).lock_state() is not None
    assert select(TEST, ["S1"], **_kw(roots)).written


def test_select_records_a_wrong_count_or_a_choice_unlike_final_and_warns(roots):
    _seeded(roots, final_row(["S1"], 1))
    # two ids where the final slots are one, and the set is not final's: both words, in order
    assert select(TEST, ["S1", "S2"], **_kw(roots)).state.words == "slots,differs"
    out = select(TEST, ["S2"], **_kw(roots))
    assert out.written and out.state.words == "differs" and out.state.warns


def test_select_records_a_wrong_count_alone_and_warns(roots):
    _seeded(roots, final_row(["S1"], 2))  # two slots, two entries: two are expected
    out = select(TEST, ["S1"], **_kw(roots))
    assert out.written and out.state.words == "slots" and out.state.warns


def test_board_rule_last_records_and_never_checks(roots):
    _seeded(roots, final_row(["S1"], 1), board_rule="last")
    out = select(TEST, ["S2"], **_kw(roots))
    assert out.written and out.state.words == "none" and not out.state.warns


def test_select_ignores_a_deadline_long_past(roots):
    paths = _seeded(roots, deadline=stamp(parse_stamp(stamp()) - timedelta(days=30)))
    assert select(TEST, ["S1", "S2"], **_kw(roots)).written
    assert _events(paths).count("selected") == 1
