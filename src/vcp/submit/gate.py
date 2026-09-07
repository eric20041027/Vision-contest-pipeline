"""The admission gate (spec 8): a candidate is a PASS judgement, never a good-looking number."""

from __future__ import annotations

from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.fuse.schema import FuseRecord
from vcp.measure.ledger import JUDGEMENTS_LEDGER, READINGS_LEDGER, ReadingsLedger, read_rows
from vcp.measure.prereg import load_prereg
from vcp.measure.runs import load_run
from vcp.measure.schema import Judgement, RunCard
from vcp.submit.schema import CandidateKind, Gate


def latest_judgements(eval_paths: DatasetPaths) -> dict[str, Judgement]:
    """The newest judgement of every claim (a re-judged claim's last ledger row wins)."""
    latest: dict[str, Judgement] = {}
    for j in read_rows(eval_paths.measure_dir / JUDGEMENTS_LEDGER, Judgement):
        latest[j.prereg_id] = j
    return latest


def _assert_fresh(data_root: Path, j: Judgement, readings: ReadingsLedger) -> None:
    """Every reading the judgement rests on must still describe the run's current bytes."""
    for rid in j.reading_ids:
        r = readings.by_id.get(rid)
        if r is None:
            raise ValidationFailed(
                f"stale_judgement: {j.prereg_id!r} cites reading {rid[:12]} which is not in "
                "the readings ledger",
                fields={"prereg": j.prereg_id},
            )
        entry = load_run(data_root, r.run_id).predictions.get(r.subset)
        now = entry.sha256 if entry is not None else None
        if now != r.prediction_sha:
            raise ValidationFailed(
                f"stale_judgement: {j.prereg_id!r} judged {r.run_id}/{r.subset} at prediction "
                f"sha {r.prediction_sha[:12]}, the run now has {(now or 'none')[:12]}; "
                "re-judge after the replacement",
                fields={"prereg": j.prereg_id},
            )


def admit(
    data_root: Path,
    eval_paths: DatasetPaths,
    eval_card: RunCard,
    fuse: FuseRecord | None,
    kind: CandidateKind,
    reason: str | None,
) -> Gate:
    if kind != "candidate":
        if not reason:
            raise ValidationFailed(
                f"reason_required: kind={kind} needs --reason", fields={"kind": kind}
            )
        return Gate(admission="waived", judgements=[], reason=reason)
    latest = latest_judgements(eval_paths)
    passing = [
        j for j in latest.values() if j.candidate_run == eval_card.run_id and j.verdict == "PASS"
    ]
    if not passing:
        raise ValidationFailed(
            f"not_admitted: no PASS judgement names {eval_card.run_id!r} as its candidate run",
            fields={"run": eval_card.run_id},
        )
    used: list[Judgement] = []
    if fuse is None:
        used.append(passing[-1])
    else:
        for m in fuse.members:
            match = [j for j in passing if load_prereg(eval_paths, j.prereg_id).component == m.run]
            if not match:
                raise ValidationFailed(
                    f"member_not_admitted: member {m.run!r} of {eval_card.run_id!r} has no PASS "
                    "admission judgement (run `vcp fuse ablate --preregister` and judge it)",
                    fields={"member": m.run},
                )
            used.append(match[-1])
    readings = ReadingsLedger(eval_paths.measure_dir / READINGS_LEDGER)
    for j in used:
        _assert_fresh(data_root, j, readings)
    return Gate(admission="PASS", judgements=[j.prereg_id for j in used], reason=reason or "")
