"""Kernel submissions: which runs' weights a notebook may load (VCP-036).

A kernel candidate is admitted on its eval run's judgement and ranked by that run's sealed
reading, so the weights the notebook declares must be the weights that were judged: the eval run
itself or, for a fusion, its members all the way down. Every weights run of a candidate also
passes the checks its eval run passes -- never trained on or read the sealed subset, provenance
at least what the profile requires. A baseline is held to membership only (its eval run is not
sealed-checked either); a probe is exempt, but everything it would have failed is written into
its pairing checks as ``weights:<run>=<finding>``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.data.access.schema import GRADE_RANK, Grade
from vcp.fuse.build import load_record
from vcp.measure.provenance import provenance
from vcp.measure.runs import load_run
from vcp.submit.pairing import is_fusion
from vcp.submit.schema import CandidateKind, WeightRef


@dataclass(frozen=True)
class WeightsCheck:
    notes: list[str]  # pairing checks for findings that did not stop the stage
    grade: Grade  # the lowest provenance grade among the weights runs


def candidate_runs(data_root: Path, eval_run: str) -> set[str]:
    """The eval run and, for a fusion, every member all the way down."""
    seen: set[str] = set()
    pending = [eval_run]
    while pending:
        run = pending.pop()
        if run in seen:
            continue
        seen.add(run)
        if is_fusion(data_root, run):
            pending.extend(member.run for member in load_record(data_root, run).members)
    return seen


def _findings(
    ref: WeightRef,
    *,
    eval_run: str,
    allowed: set[str],
    trained_on: list[str],
    observed: list[str],
    grade: Grade,
    sealed_subset: str,
    require_provenance: Grade,
) -> list[tuple[str, str]]:
    """``(code, message)`` for every rule this weights run breaks, in a fixed order."""
    run = ref.run
    found: list[tuple[str, str]] = []
    if run not in allowed:
        found.append(
            (
                "not_in_candidate",
                f"weights_not_in_candidate: run {run!r} is neither {eval_run!r} nor one of its "
                "fusion members; a candidate's notebook may load only the weights that were judged",
            )
        )
    if sealed_subset in trained_on:
        found.append(
            (
                "trained_on_sealed",
                f"trained_on_sealed: weights run {run!r} trained on {sealed_subset!r}; it can "
                "never have a clean sealed reading",
            )
        )
    if sealed_subset in observed:
        found.append(
            (
                "observed_sealed",
                f"observed_sealed: weights run {run!r} read {sealed_subset!r} (access receipt); "
                "it can never have a clean sealed reading",
            )
        )
    if GRADE_RANK[grade] < GRADE_RANK[require_provenance]:
        found.append(
            (
                f"provenance_{grade}",
                f"provenance_required: weights run {run!r} is {grade}, profile requires "
                f"{require_provenance}",
            )
        )
    return found


def check_weights(
    *,
    data_root: Path,
    configs_root: Path | None,
    eval_run: str,
    weights: list[WeightRef],
    kind: CandidateKind,
    sealed_subset: str,
    require_provenance: Grade,
) -> WeightsCheck:
    """Stop a candidate on any finding and a baseline on membership; note the rest."""
    allowed = candidate_runs(data_root, eval_run)
    notes: list[str] = []
    grades: list[Grade] = []
    for ref in weights:
        card = load_run(data_root, ref.run)
        info = provenance(card, data_root=data_root, configs_root=configs_root)
        grades.append(info.grade)
        findings = _findings(
            ref,
            eval_run=eval_run,
            allowed=allowed,
            trained_on=card.trained_on,
            observed=info.observed,
            grade=info.grade,
            sealed_subset=sealed_subset,
            require_provenance=require_provenance,
        )
        for code, message in findings:
            if kind == "candidate" or (kind == "baseline" and code == "not_in_candidate"):
                fields = {"run": ref.run}
                if code.startswith("provenance_"):  # the eval-run check names the grade too
                    fields["provenance"] = info.grade
                raise ValidationFailed(message, fields=fields)
            notes.append(f"weights:{ref.run}={code}")
    lowest = min(grades, key=lambda grade: GRADE_RANK[grade]) if grades else "declared"
    return WeightsCheck(notes=notes, grade=lowest)


def kernel_provenance(
    *,
    data_root: Path,
    configs_root: Path | None,
    grade: Grade,
    observed: Iterable[str],
    weights: list[WeightRef],
) -> tuple[Grade, set[str]]:
    """Fold the weights runs into an eval run's provenance: the lowest grade and every subset
    read. `submit final` re-reads a kernel submission this way, as `stage` recorded it."""
    lowest, seen = grade, set(observed)
    for ref in weights:
        info = provenance(
            load_run(data_root, ref.run), data_root=data_root, configs_root=configs_root
        )
        lowest = min(lowest, info.grade, key=lambda g: GRADE_RANK[g])
        seen |= set(info.observed)
    return lowest, seen
