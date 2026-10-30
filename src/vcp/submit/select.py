"""What was ticked as the final submissions on the platform, and whether the record can be
trusted (spec 2026-10-30 §3.4). ``selection_state`` is the one judgement ``status``, ``report``,
``final`` and ``select`` all read; ``selection_lines`` and ``differs_line`` are the sentences
that say it. Only a ``best`` board has a selection to check: a ``last`` board takes the final
submission by itself, so its ``words`` are always ``none`` (controller ruling C1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from vcp.core.time import parse_stamp, stamp
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.schema import LedgerRow, PlatformProfile

NO_WARNING = ("none", "ok")  # the ``words`` that need nothing said


def label(row: LedgerRow) -> str:
    """What an arrival is called in ``chosen``: our submission id, or ``foreign:<platform_ref>``
    for an upload someone else (or the web page) made."""
    return row.submission_id if row.submission_id else f"foreign:{row.platform_ref}"


@dataclass(frozen=True)
class SelectionState:
    chosen: list[str] | None  # the newest ``selected`` row's labels
    recorded_at: str | None  # that row's ``ts``
    words: str  # none | ok | missing | slots | differs, the last three joined by commas
    expected: int  # how many labels the platform takes of the entries there are
    final: list[str] | None  # the newest ``final`` row's choice
    due_at: str | None  # from when a missing selection warns
    slots: int  # the final slots in force
    deadline: str | None

    @property
    def warns(self) -> bool:
        return self.words not in NO_WARNING

    def to_json(self) -> dict[str, Any]:
        """The ``selection`` object of ``--json`` (spec §3.5)."""
        return {
            "chosen": self.chosen,
            "recorded_at": self.recorded_at,
            "words": self.words,
            "expected": self.expected,
            "final": self.final,
            "due_at": self.due_at,
        }


def _labels(row: LedgerRow | None) -> list[str] | None:
    return None if row is None or row.chosen is None else list(row.chosen)


def _due(
    profile: PlatformProfile, final: LedgerRow | None, now: datetime
) -> tuple[str | None, bool]:
    """From when a missing selection warns, and whether that moment has come. With a deadline
    it is ``selection_warn_hours`` before it; without one, the ``final`` row is the one moment
    vcp knows the next step is to tick; ``null`` hours never warn."""
    hours = profile.selection_warn_hours
    if hours is None:
        return None, False
    if profile.deadline is not None:
        due_at = stamp(parse_stamp(profile.deadline) - timedelta(hours=hours))
        return due_at, now >= parse_stamp(due_at)
    if final is None:
        return None, False
    return final.ts, True


def _words(
    profile: PlatformProfile,
    chosen: list[str] | None,
    final: list[str] | None,
    expected: int,
    due: bool,
) -> str:
    if profile.board_rule != "best":
        return "none"
    if chosen is None:
        return "missing" if due else "none"
    words: list[str] = []
    if len(chosen) != expected:
        words.append("slots")
    if final is not None and set(chosen) != set(final):
        words.append("differs")
    return ",".join(words) or "ok"


def selection_state(
    ledger: SubmissionLedger, profile: PlatformProfile, now: datetime
) -> SelectionState:
    selected, final_row = ledger.latest_selected(), ledger.latest_final()
    chosen, final = _labels(selected), _labels(final_row)
    slots = final_row.slots if final_row and final_row.slots is not None else profile.final_slots
    entries = len({label(r) for r in ledger.arrivals()})
    expected = max(1, min(slots, entries))
    due_at, due = _due(profile, final_row, now)
    return SelectionState(
        chosen=chosen,
        recorded_at=selected.ts if selected else None,
        words=_words(profile, chosen, final, expected, due),
        expected=expected,
        final=final,
        due_at=due_at,
        slots=slots,
        deadline=profile.deadline,
    )


def _join(labels: list[str]) -> str:
    return ",".join(labels) or "-"


def differs_line(final: list[str], recorded: list[str]) -> str:
    """The two sides of a selection that is not what ``final`` chose; an empty side is ``-``."""
    left_out = [x for x in final if x not in recorded]
    extra = [x for x in recorded if x not in final]
    return (
        f"selection: final chose {_join(final)}; recorded {_join(recorded)} "
        f"(not selected: {_join(left_out)}; selected, not final: {_join(extra)})"
    )


def selection_lines(state: SelectionState) -> list[str]:
    """One line per word of a state that warns, in the order of ``words``; none for a state that
    needs nothing said."""
    lines: list[str] = []
    for word in state.words.split(",") if state.warns else ():
        if word == "missing":
            since = f"warning since {state.due_at}"
            when = f"deadline {state.deadline}" if state.deadline else "no deadline"
            lines.append(f"selection: missing ({when}, {since})")
        elif word == "slots":
            lines.append(
                f"selection: {len(state.chosen or [])} recorded, {state.expected} expected "
                f"(slots={state.slots})"
            )
        elif word == "differs":
            lines.append(differs_line(state.final or [], state.chosen or []))
    return lines
