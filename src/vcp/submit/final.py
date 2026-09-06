"""``final`` / ``lock`` / ``unlock`` (spec 6.4): the last shot is chosen by the sealed holdout,
never by the public board."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.data.split import load_plan
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.metrics import effective_params, get_metric, params_key
from vcp.measure.report import READINGS_LEDGER
from vcp.measure.runs import load_run
from vcp.measure.schema import Reading, RunCard
from vcp.submit.guards import assert_unlocked
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import load_profile
from vcp.submit.schema import FinalEntry, LedgerRow, PlatformProfile

NOT_RANKED = ("probe", "not_uploaded")


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


def _open(
    dataset: str, data_root: Path | None, configs_root: Path | None
) -> tuple[DatasetPaths, PlatformProfile, str, SubmissionLedger]:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, sha = load_profile(paths)
    return paths, profile, sha, SubmissionLedger(paths.submissions_log)


def final(
    dataset: str,
    *,
    slots: int | None = None,
    dry_run: bool = False,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> FinalResult:
    paths, profile, profile_sha, ledger = _open(dataset, data_root, configs_root)
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
        if st.kind == "probe":
            why = "probe"
        elif not ledger.uploads(sid):
            why = "not_uploaded"
        else:
            card = load_run(paths.data_root, str(st.eval_run))
            reading, why = sealed_reading(readings, card, profile, params_hash, sealed_size)
        entries.append(
            FinalEntry(
                submission_id=sid,
                eligible=why == "",
                why=why,
                sealed_value=reading.value if reading is not None else None,
                sealed_reading_id=reading.reading_id if reading is not None else None,
                public=public,
                staged_at=st.ts,
            )
        )
    ranked = sorted(
        (e for e in entries if e.eligible),
        key=lambda e: (
            -sign * float(e.sealed_value if e.sealed_value is not None else 0.0),
            -(e.public if e.public is not None else float("-inf")),
            e.staged_at,
        ),
    )
    if not ranked:
        raise ValidationFailed(
            "no_sealed_readings: no uploaded candidate or baseline has a usable sealed reading; "
            "run `vcp eval measure --run R --subsets <sealed> --unseal --reason ...` first"
        )
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
    return FinalResult(row, chosen, unranked, needs_reupload, warnings, not dry_run)


def lock(
    dataset: str, reason: str, *, data_root: Path | None = None, configs_root: Path | None = None
) -> LedgerRow:
    _, _, _, ledger = _open(dataset, data_root, configs_root)
    assert_unlocked(ledger)
    row = LedgerRow(event="lock", ts=stamp(), reason=reason)
    ledger.append(row)
    return row


def unlock(
    dataset: str, reason: str, *, data_root: Path | None = None, configs_root: Path | None = None
) -> LedgerRow:
    _, _, _, ledger = _open(dataset, data_root, configs_root)
    if ledger.lock_state() is None:
        raise ValidationFailed("not_locked: nothing to unlock")
    row = LedgerRow(event="unlock", ts=stamp(), reason=reason)
    ledger.append(row)
    return row
