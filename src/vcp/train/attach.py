"""``vcp train run --evidence / --labels`` (spec 2026-09-26 §4.3, §5.2): the checks before the
first write, the attaching before the child starts, and the look afterwards at whether an
evidence file moved while the command ran."""

from __future__ import annotations

from pathlib import Path

from vcp.core.hashing import sha256_file
from vcp.data.evidence import (
    RunScope,
    attach_evidence,
    check_evidence_file,
    label_ref,
    parse_evidence_args,
    source_sha,
)
from vcp.train.records import bind_ref
from vcp.train.schema import TrainRecord


def preflight(
    data_root: Path, scope: RunScope, evidence: list[str], labels: list[str]
) -> list[tuple[str, Path]]:
    """Everything that must hold before the first write: ``NAME=PATH`` items naming files, and
    label sets that fit the run. Returns the parsed evidence."""
    parsed = parse_evidence_args(evidence)
    for name, path in parsed:
        check_evidence_file(name, path, name)
    for label_set_id in labels:
        label_ref(data_root, scope, label_set_id, attempt=None, binding="cli")
    return parsed


def attach(
    data_root: Path,
    scope: RunScope,
    record: TrainRecord,
    evidence: list[tuple[str, Path]],
    labels: list[str],
    *,
    attempt: int,
) -> tuple[TrainRecord, dict[str, str]]:
    """Copy every ``--evidence`` file and bind it, then every ``--labels`` set, to ``attempt``.
    Returns the saved record and each evidence file's sha as attached."""
    digests: dict[str, str] = {}
    for name, path in evidence:
        ref = attach_evidence(
            data_root, scope, name, path, role=name, attempt=attempt, binding="cli"
        )
        record, _ = bind_ref(data_root, record, ref, attempt)
        digests[name] = source_sha(data_root, ref)
    for label_set_id in labels:
        ref = label_ref(data_root, scope, label_set_id, attempt=attempt, binding="cli")
        record, _ = bind_ref(data_root, record, ref, attempt)
    return record, digests


def moved(evidence: list[tuple[str, Path]], digests: dict[str, str]) -> list[str]:
    """Names whose file is gone, or holds other bytes than when it was attached."""
    return [
        name for name, path in evidence if not path.is_file() or sha256_file(path) != digests[name]
    ]
