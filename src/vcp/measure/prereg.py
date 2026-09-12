"""Pre-registration files (git) plus an append-only log whose timestamps prove ordering.

A claim is written down BEFORE the candidate is measured, or it is not a claim -- it is a
description of an answer already seen. Two mechanisms enforce that: ``create_prereg`` refuses a
candidate that already has a reading for the claimed metric on a claimed subset, and the log
stamps its own clock so ``judge`` can compare the candidate's readings against the moment the
claim became binding. Neither the yaml nor the log line is ever rewritten: a changed claim is a
new id.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from vcp.core.atomic import write_once_text
from vcp.core.config import dump_yaml_text, load_yaml_model
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, validate_name
from vcp.core.time import stamp
from vcp.data.schema import DatasetCard
from vcp.measure.ledger import ReadingsLedger, append_row, read_rows
from vcp.measure.metrics import effective_params, get_metric, params_key
from vcp.measure.schema import PreRegistration

ALREADY_MEASURED = "already_measured"


class PreregLogEntry(BaseModel):
    """One line of ``prereg.log.jsonl`` (spec 4.5): the id, the file's hash, and the moment
    the claim became binding. ``ts`` is this module's own clock, never the caller's."""

    model_config = ConfigDict(extra="forbid")

    prereg_id: str
    sha256: str
    ts: str


def prereg_path(paths: DatasetPaths, prereg_id: str) -> Path:
    validate_name(prereg_id)
    return paths.prereg_dir / f"{prereg_id}.yaml"


def list_preregs(paths: DatasetPaths) -> list[str]:
    if not paths.prereg_dir.is_dir():
        return []
    return sorted(p.stem for p in paths.prereg_dir.glob("*.yaml"))


def load_prereg(paths: DatasetPaths, prereg_id: str) -> PreRegistration:
    """The pre-registration as it was logged: the yaml is trusted only when its bytes still hash
    to the FIRST log row for this id. A yaml with no row was never registered (hand-written, or
    the log append failed and the yaml was not cleaned up); a yaml whose hash moved was edited
    after the claim became binding -- either way the claim cannot be judged."""
    path = prereg_path(paths, prereg_id)
    if not path.is_file():
        raise ValidationFailed(
            f"not_found: pre-registration {prereg_id!r} ({path})", fields={"prereg": prereg_id}
        )
    entry = _first_prereg_entry(paths, prereg_id)
    if entry is None:
        raise ValidationFailed(
            f"not_found: pre-registration {prereg_id!r} has no row in {paths.prereg_log.name}; "
            "it was never registered",
            fields={"prereg": prereg_id},
        )
    digest = sha256_file(path)
    if entry.sha256 != digest:
        raise IntegrityError(
            f"mismatch: pre-registration {prereg_id!r} SHA256 differs from its first log entry "
            f"({digest[:12]} != {entry.sha256[:12]}); the yaml was edited after registration, "
            "register the changed claim under a new id",
            location=str(path),
            fields={"prereg": prereg_id},
        )
    return load_yaml_model(path, PreRegistration)


def _first_prereg_entry(paths: DatasetPaths, prereg_id: str) -> PreregLogEntry | None:
    """The first append-only identity row for this id, or ``None``."""
    for row in read_rows(paths.prereg_log, PreregLogEntry):
        if row.prereg_id == prereg_id:
            return row
    return None


def prereg_time(paths: DatasetPaths, prereg_id: str) -> str | None:
    """When this id was FIRST logged, or ``None`` if it never was.

    The first line wins: the log is append-only, so a second line for the same id could only
    ever move the deadline forwards and make an already-measured candidate look legal.
    """
    entry = _first_prereg_entry(paths, prereg_id)
    return entry.ts if entry is not None else None


def _dataset_task(paths: DatasetPaths) -> str:
    """The task this dataset poses, read from its card alone (the samples are not needed)."""
    if not paths.card_yaml.is_file():
        raise ValidationFailed(f"dataset card not found: {paths.card_yaml}")
    return load_yaml_model(paths.card_yaml, DatasetCard).task


def measured_subsets(readings: ReadingsLedger, pr: PreRegistration, params_hash: str) -> list[str]:
    """The claimed subsets on which the candidate already has a reading for this metric.

    Public because ``vcp fuse ablate`` must ask this BEFORE it writes anything (all-or-nothing),
    while ``create_prereg`` asks it again at the moment of writing; one implementation, two callers.
    """
    return sorted(
        {
            r.subset
            for r in readings.rows
            if r.run_id == pr.candidate_run
            and r.metric == pr.metric
            and params_key(r.params) == params_hash
            and r.subset in pr.subsets
        }
    )


def create_prereg(paths: DatasetPaths, pr: PreRegistration, readings: ReadingsLedger) -> Path:
    """Write the claim down, then log the moment it became binding.

    Refuses a candidate that has already been measured on a claimed subset: a claim written
    after the answer is max-of-N with extra steps. The baseline may be measured -- it is the
    thing being improved on, and knowing its value is what makes a claim worth making.
    """
    path = prereg_path(paths, pr.prereg_id)
    if path.exists():
        raise ValidationFailed(f"pre-registration {pr.prereg_id!r} already exists: {path}")
    if not pr.subsets:
        raise ValidationFailed(f"pre-registration {pr.prereg_id!r} names no subsets to judge on")
    if len(set(pr.subsets)) != len(pr.subsets):
        # Minor 1: an unnoticed duplicate (e.g. a copy-pasted --subsets valA,valA) would count
        # the same subset toward bases_positive as if it were two independent ones, producing a
        # permanent, confusingly-worded FAIL every time the claim is judged.
        raise ValidationFailed(
            f"pre-registration {pr.prereg_id!r} names duplicate subsets: {pr.subsets}"
        )
    metric = get_metric(pr.metric)
    task = _dataset_task(paths)
    if task not in metric.tasks:
        # A claim on a metric this dataset's task cannot produce could never be judged, and it
        # would be committed to git before anyone noticed.
        raise ValidationFailed(f"metric {pr.metric!r} is not applicable to task {task!r}")
    params = effective_params(metric, pr.params)
    measured = measured_subsets(readings, pr, params_key(params))
    if measured:
        raise ValidationFailed(
            f"{ALREADY_MEASURED}: candidate {pr.candidate_run!r} has {pr.metric} readings on "
            f"{measured}; pre-register before measuring the candidate"
        )
    write_once_text(path, dump_yaml_text(pr.model_copy(update={"params": params})))
    try:
        append_row(
            paths.prereg_log,
            # ruling 5: the log reads the clock itself. A log whose timestamps are what make a
            # claim provably earlier than a reading may not record a caller-supplied time.
            PreregLogEntry(prereg_id=pr.prereg_id, sha256=sha256_file(path), ts=stamp()),
        )
    except Exception:
        # 3-9: the two writes are one act. A yaml with no log line is a claim that never became
        # binding, so `judge` refuses it -- and `create_prereg` refuses to write the id again,
        # because the path exists. That leaves an id that can neither be used nor re-used, in a
        # directory that goes to git. The log line is the half that cannot be undone (the log is
        # append-only), so the yaml is the half that goes back.
        path.unlink(missing_ok=True)
        raise
    return path
