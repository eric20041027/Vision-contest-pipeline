"""``final`` / ``lock`` / ``unlock`` (spec 6.4): the last shot is chosen by the sealed holdout,
never by the public board."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from vcp.code.grade import lowest
from vcp.code.schema import CODE_RANK
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.data.access.schema import GRADE_RANK, Grade
from vcp.data.evidence import IntactMemo
from vcp.data.split import load_plan
from vcp.measure.ledger import READINGS_LEDGER, ReadingsLedger
from vcp.measure.metrics import effective_params, get_metric, params_key
from vcp.measure.provenance import provenance
from vcp.measure.runs import load_run
from vcp.measure.schema import Reading, RunCard
from vcp.submit.guards import assert_unlocked
from vcp.submit.kernel import (
    code_grades,
    kernel_provenance,
    notebook_code_holds,
    submission_runs,
)
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import transaction
from vcp.submit.profile import load_profile
from vcp.submit.schema import FinalEntry, LedgerRow, PlatformProfile, Staged
from vcp.submit.select import resolve
from vcp.submit.stage import load_staged

NOT_RANKED = ("probe", "not_uploaded")
RESEND_REASON = "final re-send"


def count_unseals(eval_paths: DatasetPaths, plan_id: str, subset: str) -> int:
    """How many times the sealed subset has been opened, per the data layer's unseal log."""
    path = eval_paths.unseal_jsonl(plan_id)
    if not path.is_file():
        return 0
    n = 0
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValidationFailed(
                    f"bad unseal log: {e}", location=f"{path.name}:{lineno}"
                ) from e
            if row.get("subset") == subset:
                n += 1
    return n


def sealed_reading(
    readings: ReadingsLedger,
    card: RunCard,
    profile: PlatformProfile,
    params_hash: str,
    sealed_size: int,
) -> tuple[Reading | None, str]:
    """The newest sealed reading of this run, and why it is unusable when it is ("" = usable)."""
    rows = [
        r
        for r in readings.rows
        if r.run_id == card.run_id
        and r.plan_id == profile.plan_id
        and r.subset == profile.sealed_subset
        and r.metric == profile.metric
        and params_key(r.params) == params_hash
    ]
    if not rows:
        return None, "no_sealed_reading"
    r = rows[-1]
    entry = card.predictions.get(profile.sealed_subset)
    if entry is None or entry.sha256 != r.prediction_sha:
        return r, "stale_reading"
    if r.n_samples != sealed_size:
        return r, "partial_reading"
    return r, ""


@dataclass(frozen=True)
class FinalResult:
    row: LedgerRow
    chosen: list[str]
    unranked: list[str]
    needs_reupload: str | None
    warnings: list[str]
    written: bool
    resend: str | None = None  # how to send needs_reupload again (resend_hint)
    # board_rule=best only (spec 2026-10-30 §3.5): "differs" when a recorded selection is not
    # this choice, with that selection's entries as recorded (``[]``: nothing ticked); the next
    # step to take on the platform
    selection: str | None = None
    recorded: list[str] | None = None
    next_step: str | None = None
    # with "differs" (spec 2026-11-01 §3.5): the labels of ``recorded``, same order, and the
    # moment it was recorded
    resolved: list[str] | None = None
    recorded_at: str | None = None


def resend_hint(profile: PlatformProfile, submission_id: str) -> str:
    """What sends the chosen submission again when ``board_rule=last`` scores another upload
    (final review I4). The id went up before, so ``upload`` needs ``--force`` like any re-send
    (spec 2026-09-28 §4.5); final's lock lets the chosen id through. A manual platform has no
    upload: the file goes up by hand and ``record`` writes it down (it only WARNs)."""
    sid, test = submission_id, profile.dataset
    if profile.platform == "manual":
        return (
            f"upload {sid} again by hand, then: vcp submit record --dataset {test} --id {sid} "
            '--at "YYYY-MM-DD HH:MM"'
        )
    return f'send it again: vcp submit upload --dataset {test} --id {sid} --force "{RESEND_REASON}"'


def next_step_hint(profile: PlatformProfile, chosen: list[str]) -> str:
    """What follows ``final`` when the board counts the submissions ticked on the platform
    (``best``): tick them there, then write down what was ticked (spec 2026-10-30 §3.5)."""
    c = ",".join(chosen)
    record = f"vcp submit select --dataset {profile.dataset} --record {c}"
    return f"select {c} on the platform, then: {record}"


def _differing_selection(ledger: SubmissionLedger, chosen: list[str]) -> LedgerRow | None:
    """The newest ``selected`` row when it is not ``chosen``: it ticked nothing, or the labels of
    its entries (``S1@7`` is S1, spec 2026-11-01 §3.1) are not the chosen ones (as sets: the
    platform's selection has no order); None when there is no such row or it is the same."""
    selected = ledger.latest_selected()
    if selected is None:
        return None
    entries = selected.chosen or []
    if entries and {resolve(ledger, e).label for e in entries} == set(chosen):
        return None
    return selected


@contextmanager
def _open(
    dataset: str, data_root: Path | None, configs_root: Path | None, command: str
) -> Iterator[tuple[DatasetPaths, PlatformProfile, str, SubmissionLedger]]:
    """The profile, then the ledger read inside its transaction (spec 2026-09-28 §4.2)."""
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, sha = load_profile(paths)
    with transaction(paths, profile, command=command) as ledger:
        yield paths, profile, sha, ledger


def _errored(ledger: SubmissionLedger, submission_id: str) -> bool:
    """The id's newest upload has no result on the platform: the platform finished it without
    a score (spec 2026-10-09 §4.5). A later upload that scored, or is still pending, clears it."""
    outcome = ledger.latest_outcome(submission_id)
    return outcome is not None and outcome.event == "errored"


UNSEAL_HINT = "run `vcp eval measure --run R --subsets <sealed> --unseal --reason ...` first"


def _nothing_ranked(entries: list[FinalEntry]) -> ValidationFailed:
    """``no_sealed_readings:``, and when uploads errored on the platform, how many: unsealing
    the holdout does not help those (review of spec 2026-10-09, Minor 3). The unseal hint stays
    for the entries that did not error."""
    errored = [e.submission_id for e in entries if e.why == "errored"]
    if not errored:
        return ValidationFailed(
            "no_sealed_readings: no uploaded candidate or baseline has a usable sealed reading; "
            + UNSEAL_HINT
        )
    message = (
        "no_sealed_readings: no uploaded candidate or baseline can be ranked; "
        f"{len(errored)} of them errored on the platform ({', '.join(errored)}): their newest "
        "upload has no score, so look there and send them again"
    )
    if any(e.why not in (*NOT_RANKED, "errored") for e in entries):
        message += f"; for the others, {UNSEAL_HINT}"
    return ValidationFailed(message)


def rank_key(sign: float) -> Callable[[FinalEntry], tuple[float, float, str]]:
    """Spec 6.4: rank by the sealed reading, public breaks ties, ``staged_at`` breaks the rest.
    Both value keys follow ``higher_is_better`` (``sign`` is +1 or -1) because the board reports
    the same metric the sealed reading does; a missing public always sorts last whatever the
    direction."""

    def key(e: FinalEntry) -> tuple[float, float, str]:
        sealed_or_0 = e.sealed_value if e.sealed_value is not None else 0.0
        public = e.public if e.public is not None else -math.inf * sign
        return (-sign * sealed_or_0, -sign * public, e.staged_at)

    return key


def final(
    dataset: str,
    *,
    slots: int | None = None,
    dry_run: bool = False,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> FinalResult:
    with _open(dataset, data_root, configs_root, "submit.final") as (paths, profile, sha, ledger):
        return _final_locked(paths, profile, sha, ledger, slots, dry_run, data_root, configs_root)


def _deadline_warnings(profile: PlatformProfile) -> list[str]:
    """``past_deadline:`` once the platform's deadline has passed; ``final`` still runs."""
    if profile.deadline is not None and utc_now() >= parse_stamp(profile.deadline):
        return [f"past_deadline: {profile.deadline}"]
    return []


def _run_provenance(
    paths: DatasetPaths, configs_root: Path | None, staged: Staged
) -> tuple[RunCard, Grade, set[str]]:
    """The eval run behind a submission, with the grade of its reads and every subset it read
    (a kernel submission re-read as ``stage`` recorded it, weights included)."""
    card = load_run(paths.data_root, staged.eval_run)
    info = provenance(card, data_root=paths.data_root, configs_root=configs_root)
    grade, observed = info.grade, set(info.observed)
    artifact = staged.artifact
    if artifact.kind == "kernel":  # VCP-036: the weights the notebook loads count too
        grade, observed = kernel_provenance(
            data_root=paths.data_root,
            configs_root=configs_root,
            grade=grade,
            observed=observed,
            weights=artifact.weights,
        )
    return card, grade, observed


def _lacks_code(
    paths: DatasetPaths, profile: PlatformProfile, staged: Staged, memo: IntactMemo
) -> bool:
    """Whether the code behind a submission is short of ``require_code``, read again the way
    ``stage`` read it (spec 2026-10-31 §5.8): a run (the eval run, a file's test run, a kernel's
    weights runs) below the requirement, or -- once it asks for ``snapshot`` or more -- a kernel
    whose ``--code`` snapshot was not recorded or no longer holds."""
    require = CODE_RANK[profile.require_code]
    artifact = staged.artifact
    runs = submission_runs(staged.eval_run, staged.test_run, artifact.weights)
    grade = lowest(code_grades(paths.data_root, runs, memo).values())
    if CODE_RANK[grade] < require:
        return True
    if artifact.kind == "kernel" and require >= CODE_RANK["snapshot"]:
        return not notebook_code_holds(paths.data_root, artifact.code_snapshot)
    return False


def _final_entries(
    paths: DatasetPaths,
    profile: PlatformProfile,
    ledger: SubmissionLedger,
    eval_paths: DatasetPaths,
    params: dict[str, str],
    sealed_size: int,
    configs_root: Path | None,
) -> list[FinalEntry]:
    """One table row per staged submission, in ledger order: its sealed reading when it has a
    usable one, and ``why`` it is not eligible when it has not."""
    params_hash = params_key(params)
    readings = ReadingsLedger(eval_paths.measure_dir / READINGS_LEDGER)
    memo = IntactMemo()  # one for the table: a snapshot several runs share is hashed once
    entries: list[FinalEntry] = []
    for sid in ledger.ids():
        st = ledger.staged(sid)
        assert st is not None
        staged = load_staged(paths, sid)
        latest = ledger.latest_score(sid)
        public = latest.public if latest is not None else None
        reading: Reading | None = None
        grade: Grade | None = None
        if st.kind == "probe":
            why = "probe"
        elif not ledger.uploads(sid):
            why = "not_uploaded"
        elif _errored(ledger, sid):
            why = "errored"  # not in NOT_RANKED: listed among the unranked (spec 2026-10-09 §4.5)
        else:
            card, grade, observed = _run_provenance(paths, configs_root, staged)
            reading, why = sealed_reading(readings, card, profile, params_hash, sealed_size)
            if why == "" and st.kind == "candidate":
                if profile.sealed_subset in observed:
                    why = "observed_sealed"
                elif GRADE_RANK[grade] < GRADE_RANK[profile.require_provenance]:
                    why = "provenance_required"
                elif profile.require_code != "declared" and _lacks_code(
                    paths, profile, staged, memo
                ):
                    why = "code_required"
        entries.append(
            FinalEntry(
                submission_id=sid,
                eligible=why == "",
                why=why,
                sealed_value=reading.value if reading is not None else None,
                sealed_reading_id=reading.reading_id if reading is not None else None,
                public=public,
                staged_at=staged.staged_at,
                provenance=grade,
            )
        )
    return entries


def _rank_candidates(
    paths: DatasetPaths,
    profile: PlatformProfile,
    ledger: SubmissionLedger,
    eval_paths: DatasetPaths,
    slots: int,
    configs_root: Path | None,
) -> tuple[list[FinalEntry], dict[str, str], list[str]]:
    """The table, the metric's effective params, and the ids of the best ``slots`` eligible
    submissions (spec 6.4). ``no_sealed_readings:`` when none can be ranked."""
    sealed_size = len(load_plan(eval_paths, profile.plan_id).ids_in(profile.sealed_subset))
    metric = get_metric(profile.metric)
    params = effective_params(metric, profile.metric_params)
    entries = _final_entries(paths, profile, ledger, eval_paths, params, sealed_size, configs_root)
    sign = 1.0 if metric.higher_is_better else -1.0
    ranked = sorted((e for e in entries if e.eligible), key=rank_key(sign))
    if not ranked:
        raise _nothing_ranked(entries)
    return entries, params, [e.submission_id for e in ranked[:slots]]


def _reupload_target(
    profile: PlatformProfile, ledger: SubmissionLedger, chosen: list[str]
) -> str | None:
    """``board_rule=last`` only: the first choice when the last upload is not it, so the board
    would count another submission."""
    if profile.board_rule != "last":
        return None
    last = ledger.last_uploaded()
    return chosen[0] if last is None or last.submission_id != chosen[0] else None


@dataclass(frozen=True)
class _BoardSelection:
    """What ``final`` says about the selection: all None under any board but ``best``."""

    selection: str | None = None
    recorded: list[str] | None = None
    resolved: list[str] | None = None
    recorded_at: str | None = None
    next_step: str | None = None


def _board_selection(
    profile: PlatformProfile, ledger: SubmissionLedger, chosen: list[str]
) -> _BoardSelection:
    """``board_rule=best`` only: the next step, and ``selection="differs"`` with the recorded
    entries, their labels and when they were recorded if the newest ``selected`` row is not this
    choice (spec 2026-10-30 §3.5, 2026-11-01 §3.5)."""
    if profile.board_rule != "best":
        return _BoardSelection()
    # read before the rows below; a recorded selection that is not this choice is only said,
    # never acted on, and the ``final`` row does not depend on it
    row = _differing_selection(ledger, chosen)
    next_step = next_step_hint(profile, chosen)
    if row is None:
        return _BoardSelection(next_step=next_step)
    recorded = list(row.chosen or [])
    return _BoardSelection(
        selection="differs",
        recorded=recorded,
        resolved=[resolve(ledger, e).label for e in recorded],
        recorded_at=row.ts,
        next_step=next_step,
    )


def _final_row(
    profile: PlatformProfile,
    profile_sha: str,
    eval_paths: DatasetPaths,
    entries: list[FinalEntry],
    params: dict[str, str],
    chosen: list[str],
    slots: int,
) -> LedgerRow:
    """The ``final`` row: the table, the choice, and what it was ranked by (the metric, its
    params, the profile and how often the holdout has been opened)."""
    return LedgerRow(
        event="final",
        ts=stamp(),
        rule=profile.final_rule,
        slots=slots,
        chosen=chosen,
        table=entries,
        metric=profile.metric,
        params=params,
        holdout_unseals=count_unseals(eval_paths, profile.plan_id, profile.sealed_subset),
        profile_sha256=profile_sha,
    )


def _final_locked(
    paths: DatasetPaths,
    profile: PlatformProfile,
    profile_sha: str,
    ledger: SubmissionLedger,
    slots: int | None,
    dry_run: bool,
    data_root: Path | None,
    configs_root: Path | None,
) -> FinalResult:
    """``final``'s body, inside the ledger's transaction (spec 2026-09-28 §4.2); ``--dry-run``
    takes the lock too."""
    assert_unlocked(ledger)
    if slots is not None and slots < 1:
        raise ValidationFailed(f"slots: must be >= 1, got {slots}", fields={"slots": slots})
    warnings = _deadline_warnings(profile)
    eval_paths = DatasetPaths.resolve(
        profile.eval_dataset, data_root=data_root, configs_root=configs_root
    )
    n = slots if slots is not None else profile.final_slots
    entries, params, chosen = _rank_candidates(paths, profile, ledger, eval_paths, n, configs_root)
    unranked = [e.submission_id for e in entries if not e.eligible and e.why not in NOT_RANKED]
    needs_reupload = _reupload_target(profile, ledger, chosen)
    board = _board_selection(profile, ledger, chosen)
    row = _final_row(profile, profile_sha, eval_paths, entries, params, chosen, n)
    if not dry_run:
        ledger.append(row)
        ledger.append(LedgerRow(event="lock", ts=stamp(), reason="final"))
    resend = None if needs_reupload is None else resend_hint(profile, needs_reupload)
    return FinalResult(
        row=row,
        chosen=chosen,
        unranked=unranked,
        needs_reupload=needs_reupload,
        warnings=warnings,
        written=not dry_run,
        resend=resend,
        selection=board.selection,
        recorded=board.recorded,
        next_step=board.next_step,
        resolved=board.resolved,
        recorded_at=board.recorded_at,
    )


def lock(
    dataset: str, reason: str, *, data_root: Path | None = None, configs_root: Path | None = None
) -> LedgerRow:
    with _open(dataset, data_root, configs_root, "submit.lock") as (_, _, _, ledger):
        assert_unlocked(ledger)
        row = LedgerRow(event="lock", ts=stamp(), reason=reason)
        ledger.append(row)
        return row


def unlock(
    dataset: str, reason: str, *, data_root: Path | None = None, configs_root: Path | None = None
) -> LedgerRow:
    with _open(dataset, data_root, configs_root, "submit.unlock") as (_, _, _, ledger):
        if ledger.lock_state() is None:
            raise ValidationFailed("not_locked: nothing to unlock")
        row = LedgerRow(event="unlock", ts=stamp(), reason=reason)
        ledger.append(row)
        return row
