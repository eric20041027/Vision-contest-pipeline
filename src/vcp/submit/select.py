"""What was ticked as the final submissions on the platform, and whether the record can be
trusted (spec 2026-10-30 §3.4). ``selection_state`` is the one judgement ``status`` and ``select``
read (``report`` and ``final`` read the newest ``selected`` row themselves); ``selection_lines``,
``differs_line`` and ``unticked_line`` are the sentences that say it. Only a ``best`` board has a
selection to check: a ``last`` board takes the final submission by itself, so its ``words`` are
always ``none`` (controller ruling C1)."""

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

NO_WARNING = ("none", "ok", "unticked")  # the ``words`` that need nothing said
# ``selection_warn_hours`` only has to be finite and >= 0, but a deadline minus 1e10 hours is out
# of range for ``datetime``; a hundred years before the deadline is "always" for any contest.
MAX_SELECTION_WARN_HOURS = 24 * 365 * 100
FOREIGN_PREFIX = "foreign:"
REF_SEPARATOR = "@"  # ``<id>@<ref>``; an id never holds one (``validate_name``)


def label(row: LedgerRow) -> str:
    """What an arrival is called in ``chosen``: our submission id, or ``foreign:<platform_ref>``
    for an upload someone else (or the web page) made."""
    return row.submission_id if row.submission_id else f"{FOREIGN_PREFIX}{row.platform_ref}"


def parse_entry(entry: str) -> tuple[str | None, str | None]:
    """Split one ``chosen`` entry (spec 2026-11-01 §3.1) into ``(submission_id, ref)``:
    ``<id>`` -> ``(id, None)``; ``<id>@<ref>`` -> split at the first ``@``, both sides stripped;
    ``foreign:<ref>`` -> ``(None, ref)``, the prefix judged first so such a ref may hold an
    ``@``. A side left empty (``S3@``, ``@123``) comes back as ``""``: refusing it is the
    caller's check."""
    if entry.startswith(FOREIGN_PREFIX):
        return None, entry.removeprefix(FOREIGN_PREFIX)
    submission_id, at, ref = entry.partition(REF_SEPARATOR)
    return (submission_id.strip(), ref.strip()) if at else (submission_id, None)


@dataclass(frozen=True)
class Resolved:
    """An entry read against the ledger: ``label`` is the unit compared with ``final`` and
    ``arrivals()``; ``submission_id`` is the id it names (None for a teammate's upload); ``ref``
    is the platform entry it points at, when it says which shot."""

    label: str
    submission_id: str | None
    ref: str | None


def resolve(ledger: SubmissionLedger, entry: str) -> Resolved:
    """What an entry means in the ledger as it is now (spec 2026-11-01 §3.1). Read-time only,
    never written back: a later ``sync`` can change who owns a ref, and the ledger keeps what
    the person said. It checks nothing -- an id that was never staged or a ref nobody saw
    resolves like any other; the caller refuses those."""
    submission_id, ref = parse_entry(entry)
    if submission_id is not None:
        return Resolved(submission_id, submission_id, ref)
    owner = ledger.owners().get(ref or "")
    if owner is not None:
        return Resolved(owner, owner, ref)
    return Resolved(f"{FOREIGN_PREFIX}{ref}", None, ref)


@dataclass(frozen=True)
class SelectionState:
    chosen: list[str] | None  # the newest ``selected`` row's entries, as recorded
    recorded_at: str | None  # that row's ``ts``
    # none | ok | missing | unticked | slots | differs; unticked, slots and differs may be joined
    # by commas (``unticked,differs``, ``slots,differs``)
    words: str
    expected: int  # how many labels the platform takes of the entries there are
    final: list[str] | None  # the newest ``final`` row's choice
    due_at: str | None  # from when a missing selection warns
    slots: int  # the final slots in force
    deadline: str | None
    resolved: list[str] | None = None  # ``chosen``'s labels, same order; None without a row

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
            "resolved": self.resolved,
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
    resolved: list[str] | None,
    final: list[str] | None,
    expected: int,
    due: bool,
) -> str:
    """``resolved`` is the newest ``selected`` row's labels (None: no row). Several shots of one
    upload are one submission, so ``slots`` counts distinct labels; an empty selection is a
    decision (the platform picks by its own rule), so it is never ``slots``."""
    if profile.board_rule != "best":
        return "none"
    if resolved is None:
        return "missing" if due else "none"
    if not resolved:
        return "unticked,differs" if final is not None else "unticked"
    words: list[str] = []
    if len(set(resolved)) != expected:
        words.append("slots")
    if final is not None and set(resolved) != set(final):
        words.append("differs")
    return ",".join(words) or "ok"


def selection_state(
    ledger: SubmissionLedger, profile: PlatformProfile, now: datetime
) -> SelectionState:
    selected, final_row = ledger.latest_selected(), ledger.latest_final()
    chosen, final = _labels(selected), _labels(final_row)
    resolved = None if chosen is None else [resolve(ledger, e).label for e in chosen]
    slots = final_row.slots if final_row and final_row.slots is not None else profile.final_slots
    entries = len({label(r) for r in ledger.arrivals()})
    expected = max(1, min(slots, entries))
    due_at, due = _due(profile, final_row, now)
    return SelectionState(
        chosen=chosen,
        recorded_at=selected.ts if selected else None,
        words=_words(profile, resolved, final, expected, due),
        expected=expected,
        final=final,
        due_at=due_at,
        slots=slots,
        deadline=profile.deadline,
        resolved=resolved,
    )


def _join(labels: list[str]) -> str:
    return ",".join(labels) or "-"


def _distinct(labels: list[str]) -> list[str]:
    return list(dict.fromkeys(labels))


def differs_line(final: list[str], recorded: list[str], resolved: list[str] | None = None) -> str:
    """The two sides of a selection that is not what ``final`` chose; an empty side is ``-``.
    The sides are compared as labels (``resolved``, the labels of ``recorded`` in the same
    order; ``recorded`` itself when not given), each listed once; ``recorded`` is printed as the
    ledger holds it."""
    labels = recorded if resolved is None else resolved
    left_out = [x for x in _distinct(final) if x not in labels]
    extra = [x for x in _distinct(labels) if x not in final]
    return (
        f"selection: final chose {_join(final)}; recorded {_join(recorded)} "
        f"(not selected: {_join(left_out)}; selected, not final: {_join(extra)})"
    )


def unticked_line(final: list[str], recorded_at: str) -> str:
    """What ``final`` chose against a selection that ticked nothing."""
    return (
        f"selection: final chose {_join(final)}; nothing ticked (recorded {recorded_at}): "
        "the platform picks by its own rule"
    )


def selection_lines(state: SelectionState) -> list[str]:
    """One line per word of a state that warns, in the order of ``words``; none for a state that
    needs nothing said. ``unticked,differs`` is one line: with nothing ticked there is no set to
    take apart."""
    lines: list[str] = []
    for word in state.words.split(",") if state.warns else ():
        if word == "missing":
            since = f"warning since {state.due_at}"
            when = f"deadline {state.deadline}" if state.deadline else "no deadline"
            lines.append(f"selection: missing ({when}, {since})")
        elif word == "unticked":
            lines.append(unticked_line(state.final or [], state.recorded_at or ""))
        elif word == "slots":
            chosen = state.chosen or []
            distinct = len(set(chosen if state.resolved is None else state.resolved))
            count = f"{len(chosen)} recorded" + (
                f" ({distinct} distinct)" if distinct != len(chosen) else ""
            )
            lines.append(f"selection: {count}, {state.expected} expected (slots={state.slots})")
        elif word == "differs" and state.chosen:
            lines.append(differs_line(state.final or [], state.chosen, state.resolved))
    return lines


def owned_note(entry: str, submission_id: str) -> str:
    """The sentence for a ``foreign:<ref>`` entry whose ref is one of our own uploads: recorded
    as said, read as the id (spec 2026-11-01 §3.2)."""
    return f"note: {entry} is {submission_id}'s upload; it is read as {submission_id}"


@dataclass(frozen=True)
class SelectOutcome:
    row: LedgerRow  # the newest ``selected`` row: the one written, or the one already there
    written: bool
    state: SelectionState
    notes: tuple[str, ...] = ()  # ``owned_note`` for each foreign entry of ``row`` that is ours


def _normal(entry: str) -> str:
    """One ``--record`` item as it is compared and written: outer spaces gone and, for
    ``<id>@<ref>``, the spaces around the ``@``. A bare id and a ``foreign:`` ref are left as
    they are (``parse_entry`` strips neither), so the CLI split and this agree."""
    entry = entry.strip()
    submission_id, ref = parse_entry(entry)
    if submission_id is None or ref is None:
        return entry
    return f"{submission_id}{REF_SEPARATOR}{ref}"


def _is_ref_entry(entry: str) -> bool:
    """An ``<id>@<ref>`` entry: a ``foreign:`` ref may hold an ``@``, an id never does."""
    return not entry.startswith(FOREIGN_PREFIX) and REF_SEPARATOR in entry


def _check_shape(entries: list[str], *, none: bool) -> None:
    """The ``invalid:`` checks that read nothing but the text, in the order the person meets
    them: nothing given, an ``@`` with an empty side, an entry twice, an id both alone and with
    a ref, a platform ref named twice (spec 2026-11-01 §3.2)."""
    if not none and not entries:
        raise ValidationFailed("invalid: --record needs at least one id or foreign:<ref>")
    for entry in entries:
        submission_id, ref = parse_entry(entry)
        if _is_ref_entry(entry) and not (submission_id and ref):
            raise ValidationFailed(
                f"invalid: --record entry '{entry}' needs an id before @ and a platform ref "
                "after it"
            )
    seen: set[str] = set()
    for entry in entries:
        if entry in seen:
            raise ValidationFailed(f"invalid: --record names {entry} twice")
        seen.add(entry)
    alone = {e for e in entries if not e.startswith(FOREIGN_PREFIX) and not _is_ref_entry(e)}
    for entry in entries:
        submission_id, _ = parse_entry(entry)
        if _is_ref_entry(entry) and submission_id in alone:
            raise ValidationFailed(
                f"invalid: --record names {submission_id} both alone and as "
                f"{submission_id}{REF_SEPARATOR}<ref>: record the id, or each of its uploads"
            )
    refs: set[str] = set()
    for entry in entries:
        ref = parse_entry(entry)[1]
        if ref is None:
            continue
        if ref in refs:
            raise ValidationFailed(f"invalid: --record names the platform ref {ref} twice")
        refs.add(ref)


def _first_owner(
    ledger: SubmissionLedger, ref: str, *, other_than: str | None = None
) -> str | None:
    """The first id, in sorted order, with an upload carrying ``ref``; None if none does."""
    ids = {
        r.submission_id for r in ledger.rows if r.submission_id and r.submission_id != other_than
    }
    return next((i for i in sorted(ids) if ref in ledger.refs_of(i)), None)


def _check_foreign(ledger: SubmissionLedger, entry: str, ref: str) -> None:
    if ref in ledger.foreign_refs():
        return
    said = f"not_found: {entry} is not a foreign upload in the ledger"
    owner = _first_owner(ledger, ref)
    if owner is not None:
        said += f"; it is {owner}'s upload: record it as {owner}{REF_SEPARATOR}{ref}"
    raise ValidationFailed(said, fields={"id": entry})


def _check_ours(ledger: SubmissionLedger, entry: str, submission_id: str, ref: str | None) -> None:
    fields = {"id": entry}
    if ledger.staged(submission_id) is None:
        raise ValidationFailed(f"not_staged: '{submission_id}' has no staged row", fields=fields)
    if not ledger.uploads(submission_id):
        raise ValidationFailed(
            f"not_uploaded: '{submission_id}' has no uploaded row; the platform cannot select it",
            fields=fields,
        )
    if ref is None or ref in ledger.refs_of(submission_id):
        return
    said = f"not_found: {entry}: no upload of {submission_id} carries the platform ref {ref}"
    other = _first_owner(ledger, ref, other_than=submission_id)
    if other is not None:
        said += f"; {ref} is {other}'s upload"
    raise ValidationFailed(said, fields=fields)


def _check_entries(ledger: SubmissionLedger, entries: list[str], *, none: bool) -> None:
    """Refuse the first thing the platform could not have been asked to select: nothing, an
    entry twice, an id vcp never staged or never uploaded, a ref that is not that id's upload,
    a foreign ref the ledger never saw. Nothing is written before every entry has passed."""
    _check_shape(entries, none=none)
    for entry in entries:
        submission_id, ref = parse_entry(entry)
        if submission_id is None:
            _check_foreign(ledger, entry, ref or "")
        else:
            _check_ours(ledger, entry, submission_id, ref)


def _owned_notes(ledger: SubmissionLedger, row: LedgerRow) -> tuple[str, ...]:
    """A ``note:`` for each ``foreign:<ref>`` entry of ``row`` whose ref is one of our own
    uploads, in the row's order."""
    owners = ledger.owners()
    notes = []
    for entry in row.chosen or ():
        submission_id, ref = parse_entry(entry)
        if submission_id is None and ref in owners:
            notes.append(owned_note(entry, owners[ref or ""]))
    return tuple(notes)


def select(
    dataset: str,
    entries: list[str] | None,
    *,
    none: bool = False,
    message: str | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> SelectOutcome:
    """Write down what was ticked on the platform (spec 2026-10-30 §3.2): one ``selected`` row,
    unless the newest one already names the same set. ``entries`` is ``--record`` (``None``:
    not given) and ``none`` is ``--none``: exactly one of them (spec 2026-11-01 §3.2, §3.3);
    ``none`` writes the empty set, nothing ticked. It records a fact about the platform, as
    ``sync`` and ``score`` do: it is not stopped by ``lock`` or by the deadline, never calls the
    platform, and a count or a choice that looks wrong is written and then warned about, because
    the ledger must say what the platform holds even when that is a mistake. Every check runs
    inside the transaction, after the lock is taken and the ledger read."""
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    with transaction(paths, profile, command="submit.select") as ledger:
        if (entries is None) == (not none):
            raise ValidationFailed("invalid: give exactly one of --record and --none")
        chosen = [] if entries is None else [_normal(e) for e in entries]
        _check_entries(ledger, chosen, none=none)
        latest = ledger.latest_selected()
        if latest is not None and set(latest.chosen or ()) == set(chosen):
            row, written = latest, False
        else:
            row = LedgerRow(
                event="selected", ts=stamp(), source="manual", chosen=chosen, message=message
            )
            ledger.append(row)
            written = True
        state = selection_state(ledger, profile, utc_now())
        return SelectOutcome(row, written, state, _owned_notes(ledger, row))
