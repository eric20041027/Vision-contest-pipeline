"""The claims of a training run (spec 2026-10-31 §4.6, right column): what a ``train`` consumer can
check, and what a finished run -- the basis of an authorization -- gives.

``train_claims`` is the one place the vocabulary is spelled out: ``run_claims`` calls it with the
values it reads from a run's records, and ``vcp train run --authorization`` calls it with the same
values computed from its own inputs before its first write, so a name or a format cannot differ
between the two sides. ``run_claims`` takes the last ``finished`` attempt (else the last one) and
reads everything from that attempt's point of view: the command and seed it ran with, the code
snapshots its ``env.<n>.json`` records, the environment identity written there, and the evidence
and label sets ``train run`` attached up to it. What a loop attached through ``Session`` is not a
claim: a consumer cannot know it before it starts.

This is the training layer's part of ``vcp artifact authorize``; ``vcp.artifact.authorization``
never imports it (the CLI passes ``run_claims`` in).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from vcp.code.schema import CodeSection, EnvIdentity
from vcp.code.snapshot import snapshot_json_path
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file, sha256_json
from vcp.core.paths import DatasetPaths
from vcp.data.evidence import source_sha
from vcp.data.evidence_ref import LABELS_ROLE, RESERVED_ROLES, EvidenceRef
from vcp.measure.runs import load_run, run_dir
from vcp.train.env import EnvSections, read_sections
from vcp.train.records import has_record, load_record
from vcp.train.schema import Attempt, TrainRecord

__all__ = ["plan_file_sha", "run_claims", "train_claims"]


def train_claims(
    *,
    dataset: str,
    plan_id: str,
    seed: int | None,
    samples_hash: str,
    trained_on: Sequence[str],
    config_sha256: str,
    command: Sequence[str],
    plan_sha256: str,
    code: Mapping[str, str],
    evidence: Mapping[str, str],
    labels: Mapping[str, str],
    identity: EnvIdentity | None,
) -> dict[str, str]:
    """Every claim of the ``train`` vocabulary for these values, sorted by name. ``seed`` is a
    claim only when there is one; ``code`` maps a root label to the sha of its ``snapshot.json``,
    ``evidence`` a name to its source's sha and ``labels`` a label-set id to its manifest's sha;
    the three ``env:`` claims are there when the environment's ``identity`` is."""
    claims = {
        "dataset": dataset,
        "plan_id": plan_id,
        "param:samples_hash": samples_hash,
        "param:trained_on": ",".join(sorted(trained_on)),
        "param:config_sha256": config_sha256,
        "param:command_sha256": sha256_json(list(command)),
        "input:plan": plan_sha256,
    }
    if seed is not None:
        claims["seed"] = str(seed)
    claims.update({f"input:code_snapshot.{label}": sha for label, sha in code.items()})
    claims.update({f"input:evidence.{name}": sha for name, sha in evidence.items()})
    claims.update({f"input:labels.{label_set_id}": sha for label_set_id, sha in labels.items()})
    if identity is not None:
        claims["env:python"] = identity.python
        claims["env:packages"] = identity.packages_sha256
        claims["env:gpus"] = identity.gpus
    return dict(sorted(claims.items()))


def plan_file_sha(data_root: Path, configs_root: Path, dataset: str, plan_id: str) -> str:
    """The sha256 of the plan's file (``input:plan``): plans are written once and never edited
    (a different split is a different plan id), so the file's bytes are the plan."""
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    path = paths.plan_json(plan_id)
    if not path.is_file():
        raise ValidationFailed(f"not_found: plan {plan_id} of dataset {dataset} ({path})")
    return sha256_file(path)


# -- reading a run -----------------------------------------------------------------------------


def _record(data_root: Path, run_id: str) -> TrainRecord:
    if not has_record(data_root, run_id):
        raise ValidationFailed(
            f"invalid: basis run {run_id} has no training record (train.yaml): only a run that "
            "`vcp train run` made can be a basis for --for train"
        )
    return load_record(data_root, run_id)


def _chosen_attempt(record: TrainRecord) -> Attempt:
    """The last attempt that finished, else the last one (spec §4.6)."""
    if not record.attempts:
        raise ValidationFailed(f"invalid: basis run {record.run_id} has no attempt to read")
    finished = [a for a in record.attempts if a.status == "finished"]
    return (finished or record.attempts)[-1]


def _sections(data_root: Path, run_id: str, attempt: Attempt) -> EnvSections:
    if attempt.env is None:
        return EnvSections(git=None, code=None, identity=None)
    return read_sections(run_dir(data_root, run_id) / attempt.env)


def _code_claims(
    data_root: Path, run_id: str, attempt: Attempt, code: CodeSection | None
) -> dict[str, str]:
    """Root label -> the sha of its snapshot's ``snapshot.json``. Every root of the attempt must
    have a snapshot: a basis that cannot bind one root would give an authorization with one claim
    fewer than the run it vouches for, so the run is ``invalid:`` as a basis."""
    who = f"basis run {run_id} attempt {attempt.n}"
    if code is None:
        raise ValidationFailed(
            f"invalid: {who} has no code snapshot (its environment file records no code section)"
        )
    snapshots = {root.label: root.snapshot for root in code.roots if root.snapshot is not None}
    missing = [root.label for root in code.roots if root.snapshot is None]
    if missing:
        raise ValidationFailed(f"invalid: {who} has no code snapshot for {','.join(missing)}")
    claims: dict[str, str] = {}
    for label, snapshot in snapshots.items():
        path = snapshot_json_path(data_root, snapshot)
        if not path.is_file():
            raise ValidationFailed(
                f"not_found: {who}: code snapshot {snapshot} of root {label} is gone ({path})"
            )
        claims[label] = sha256_file(path)
    return claims


def _role_fits(kind: str, role: str) -> bool:
    """A label set carries the ``labels`` role; an evidence copy carries the role it was attached
    under, which is never one of vcp's own."""
    return role == LABELS_ROLE if kind == "label_set" else role not in RESERVED_ROLES


def _cli_refs(record: TrainRecord, attempt: Attempt, kind: str) -> dict[str, EvidenceRef]:
    """The newest ref of each name among those of ``kind`` that ``train run`` bound from its own
    options (``binding == "cli"``) to the chosen attempt or an earlier one. Refs are told apart
    by kind and role, never by name: a label set may be called ``code.v1``."""
    newest: dict[str, EvidenceRef] = {}
    for ref in record.evidence:
        if ref.kind != kind or ref.binding != "cli" or not _role_fits(ref.kind, ref.role):
            continue
        if ref.attempt is not None and ref.attempt <= attempt.n:
            newest[ref.name] = ref
    return newest


def run_claims(data_root: Path, configs_root: Path, run_id: str) -> dict[str, str]:
    """The ``train`` claims of run ``run_id``, read from the records of its chosen attempt. A run
    that cannot give all of them is not a basis, and nothing is bound with a claim fewer:
    ``invalid:`` for a run with no training record or no attempt, or with a root that has no
    snapshot (an attempt whose environment file is missing or records no code is one);
    ``not_found:`` for a snapshot, plan or evidence artifact that is gone."""
    record = _record(data_root, run_id)
    attempt = _chosen_attempt(record)
    sections = _sections(data_root, run_id, attempt)
    code = _code_claims(data_root, run_id, attempt, sections.code)
    evidence = {
        name: source_sha(data_root, ref)
        for name, ref in _cli_refs(record, attempt, "evidence").items()
    }
    labels = {
        name: ref.manifest_sha256 for name, ref in _cli_refs(record, attempt, "label_set").items()
    }
    return train_claims(
        dataset=record.dataset,
        plan_id=record.plan_id,
        seed=attempt.seed,
        samples_hash=load_run(data_root, run_id).samples_hash,
        trained_on=record.trained_on,
        config_sha256=record.config_hash,
        command=attempt.command or record.command,
        plan_sha256=plan_file_sha(data_root, configs_root, record.dataset, record.plan_id),
        code=code,
        evidence=evidence,
        labels=labels,
        identity=sections.identity,
    )
