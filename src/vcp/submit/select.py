"""What was ticked as the final submissions on the platform, and whether the record can be
trusted (spec 2026-10-30 §3.4). ``selection_state`` is the one judgement ``status`` and ``select``
read (``report`` and ``final`` read the newest ``selected`` row themselves); ``selection_lines``
and ``differs_line`` are the sentences that say it. Only a ``best`` board has a selection to
check: a ``last`` board takes the final submission by itself, so its ``words`` are always
``none`` (controller ruling C1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import transaction
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow, PlatformProfile

NO_WARNING = ("none", "ok")  # the ``words`` that need nothing said
# ``selection_warn_hours`` only has to be finite and >= 0, but a deadline minus 1e10 hours is out
# of range for ``datetime``; a hundred years before the deadline is "always" for any contest.
MAX_SELECTION_WARN_HOURS = 24 * 365 * 100


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
        window = timedelta(hours=min(hours, MAX_SELECTION_WARN_HOURS))
        due_at = stamp(parse_stamp(profile.deadline) - window)
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


@dataclass(frozen=True)
class SelectOutcome:
    row: LedgerRow  # the newest ``selected`` row: the one written, or the one already there
    written: bool
    state: SelectionState


FOREIGN_PREFIX = "foreign:"


def _check_entries(ledger: SubmissionLedger, entries: list[str]) -> None:
    """Refuse the first thing the platform could not have been asked to select: nothing, an
    entry twice, an id vcp never staged or never uploaded, a foreign ref the ledger never saw.
    Nothing is written before every entry has passed."""
    if not entries:
        raise ValidationFailed("invalid: --record needs at least one id or foreign:<ref>")
    seen: set[str] = set()
    for entry in entries:
        if entry in seen:
            raise ValidationFailed(f"invalid: --record names {entry} twice")
        seen.add(entry)
    foreign = ledger.foreign_refs()
    for entry in entries:
        fields = {"id": entry}
        if entry.startswith(FOREIGN_PREFIX):
            if entry.removeprefix(FOREIGN_PREFIX) not in foreign:
                raise ValidationFailed(
                    f"not_found: {entry} is not a foreign upload in the ledger", fields=fields
                )
        elif ledger.staged(entry) is None:
            raise ValidationFailed(f"not_staged: '{entry}' has no staged row", fields=fields)
        elif not ledger.uploads(entry):
            raise ValidationFailed(
                f"not_uploaded: '{entry}' has no uploaded row; the platform cannot select it",
                fields=fields,
            )


def select(
    dataset: str,
    entries: list[str],
    *,
    message: str | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> SelectOutcome:
    """Write down what was ticked on the platform (spec 2026-10-30 §3.2): one ``selected`` row,
    unless the newest one already names the same set. It records a fact about the platform, as
    ``sync`` and ``score`` do: it is not stopped by ``lock`` or by the deadline, never calls the
    platform, and a count or a choice that looks wrong is written and then warned about, because
    the ledger must say what the platform holds even when that is a mistake."""
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    with transaction(paths, profile, command="submit.select") as ledger:
        _check_entries(ledger, entries)
        latest = ledger.latest_selected()
        if latest is not None and set(latest.chosen or ()) == set(entries):
            row, written = latest, False
        else:
            row = LedgerRow(
                event="selected",
                ts=stamp(),
                source="manual",
                chosen=list(entries),
                message=message,
            )
            ledger.append(row)
            written = True
        return SelectOutcome(row, written, selection_state(ledger, profile, utc_now()))
