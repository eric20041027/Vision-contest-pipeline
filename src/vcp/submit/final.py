"""``final`` / ``lock`` / ``unlock`` (spec 6.4): the last shot is chosen by the sealed holdout,
never by the public board."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.data.access.schema import GRADE_RANK, Grade
from vcp.data.split import load_plan
from vcp.measure.ledger import READINGS_LEDGER, ReadingsLedger
from vcp.measure.metrics import effective_params, get_metric, params_key
from vcp.measure.provenance import provenance
from vcp.measure.runs import load_run
from vcp.measure.schema import Reading, RunCard
from vcp.submit.guards import assert_unlocked
from vcp.submit.kernel import kernel_provenance
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import transaction
from vcp.submit.profile import load_profile
from vcp.submit.schema import FinalEntry, LedgerRow, PlatformProfile
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
    now = utc_now()
    warnings: list[str] = []
    if profile.deadline is not None and now >= parse_stamp(profile.deadline):
        warnings.append(f"past_deadline: {profile.deadline}")
    eval_paths = DatasetPaths.resolve(
        profile.eval_dataset, data_root=data_root, configs_root=configs_root
    )
    eval_plan = load_plan(eval_paths, profile.plan_id)
    sealed_size = len(eval_plan.ids_in(profile.sealed_subset))
    metric = get_metric(profile.metric)
    params = effective_params(metric, profile.metric_params)
    params_hash = params_key(params)
    sign = 1.0 if metric.higher_is_better else -1.0
    readings = ReadingsLedger(eval_paths.measure_dir / READINGS_LEDGER)
    entries: list[FinalEntry] = []
    for sid in ledger.ids():
        st = ledger.staged(sid)
        assert st is not None
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
            card = load_run(paths.data_root, str(st.eval_run))
            info = provenance(card, data_root=paths.data_root, configs_root=configs_root)
            grade, observed = info.grade, set(info.observed)
            artifact = load_staged(paths, sid).artifact
            if artifact.kind == "kernel":  # VCP-036: the weights the notebook loads count too
                grade, observed = kernel_provenance(
                    data_root=paths.data_root,
                    configs_root=configs_root,
                    grade=grade,
                    observed=observed,
                    weights=artifact.weights,
                )
            reading, why = sealed_reading(readings, card, profile, params_hash, sealed_size)
            if why == "" and st.kind == "candidate":
                if profile.sealed_subset in observed:
                    why = "observed_sealed"
                elif GRADE_RANK[grade] < GRADE_RANK[profile.require_provenance]:
                    why = "provenance_required"
        entries.append(
            FinalEntry(
                submission_id=sid,
                eligible=why == "",
                why=why,
                sealed_value=reading.value if reading is not None else None,
                sealed_reading_id=reading.reading_id if reading is not None else None,
                public=public,
                staged_at=load_staged(paths, sid).staged_at,
                provenance=grade,
            )
        )
    ranked = sorted((e for e in entries if e.eligible), key=rank_key(sign))
    if not ranked:
        raise _nothing_ranked(entries)
    n = slots if slots is not None else profile.final_slots
    chosen = [e.submission_id for e in ranked[:n]]
    unranked = [e.submission_id for e in entries if not e.eligible and e.why not in NOT_RANKED]
    needs_reupload: str | None = None
    if profile.board_rule == "last":
        last = ledger.last_uploaded()
        if last is None or last.submission_id != chosen[0]:
            needs_reupload = chosen[0]
    row = LedgerRow(
        event="final",
        ts=stamp(),
        rule=profile.final_rule,
        slots=n,
        chosen=chosen,
        table=entries,
        metric=profile.metric,
        params=params,
        holdout_unseals=count_unseals(eval_paths, profile.plan_id, profile.sealed_subset),
        profile_sha256=profile_sha,
    )
    if not dry_run:
        ledger.append(row)
        ledger.append(LedgerRow(event="lock", ts=stamp(), reason="final"))
    resend = None if needs_reupload is None else resend_hint(profile, needs_reupload)
    return FinalResult(row, chosen, unranked, needs_reupload, warnings, not dry_run, resend)


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
