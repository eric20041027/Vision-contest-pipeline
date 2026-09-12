"""Provenance of a run's training reads (spec 4.3, 7.2): which receipts still hold, what they
observed, and the grade ``receipt > export > declared`` the consumers act on. Computed on read,
never stored -- a run written before receipts existed simply grades as ``declared``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import IntegrityError, VcpError
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.access.receipt import read_receipt
from vcp.data.access.schema import GRADE_RANK, AccessRef, Grade
from vcp.measure.runs import FUSE_FRAMEWORK, load_run
from vcp.measure.schema import RunCard


@dataclass(frozen=True)
class ProvenanceInfo:
    grade: Grade
    observed: list[str]
    invalid: list[str]
    receipts: list[AccessRef]


def _fused(card: RunCard, data_root: Path, configs_root: Path | None) -> ProvenanceInfo | None:
    """A fused run has no reads of its own: the weakest member's grade, every member's
    observation (lazy import: the fusion layer imports this layer)."""
    from vcp.fuse.build import load_record, record_path

    if card.source.framework != FUSE_FRAMEWORK or not record_path(data_root, card.run_id).is_file():
        return None
    infos = [
        provenance(load_run(data_root, m.run), data_root=data_root, configs_root=configs_root)
        for m in load_record(data_root, card.run_id).members
    ]
    if not infos:
        return ProvenanceInfo("declared", [], [], [])
    grade = min((i.grade for i in infos), key=lambda g: GRADE_RANK[g])
    observed = sorted(set().union(*(set(i.observed) for i in infos)))
    invalid = sorted(set().union(*(set(i.invalid) for i in infos)))
    return ProvenanceInfo(grade, observed, invalid, [])


def provenance(
    card: RunCard, *, data_root: Path, configs_root: Path | None = None
) -> ProvenanceInfo:
    fused = _fused(card, data_root, configs_root)
    if fused is not None:
        return fused
    paths = DatasetPaths.resolve(card.dataset, data_root=data_root, configs_root=configs_root)
    plan_file = paths.plan_json(card.plan_id)
    plan_sha = sha256_file(plan_file) if plan_file.is_file() else None
    card_sha = sha256_file(paths.card_yaml) if paths.card_yaml.is_file() else None
    valid: list[AccessRef] = []
    invalid: list[str] = []
    observed: set[str] = set()
    for ref in card.access:
        try:
            receipt, sha = read_receipt(data_root, ref.artifact_id)
        except VcpError:
            invalid.append(ref.artifact_id)
            continue
        holds = (
            sha == ref.receipt_sha256
            and receipt.samples_hash == card.samples_hash
            and receipt.plan_id == card.plan_id
            and receipt.plan_sha256 == plan_sha
            and receipt.card_sha256 == card_sha
        )
        if not holds:
            invalid.append(ref.artifact_id)
            continue
        valid.append(ref)
        observed.update(receipt.accessed)
    if any(r.purpose == "train" for r in valid):
        grade: Grade = "receipt"
    elif card.source.export_manifest_sha:
        grade = "export"
    else:
        grade = "declared"
    return ProvenanceInfo(grade, sorted(observed), invalid, valid)


def attach_receipts(card: RunCard, artifact_ids: list[str], *, data_root: Path) -> RunCard:
    """``ingest --receipt`` (spec 7.2): bind receipts made outside ``vcp train run`` to a run.
    Idempotent per artifact id; a receipt of another run, dataset or plan is refused."""
    refs = list(card.access)
    for artifact_id in artifact_ids:
        if any(r.artifact_id == artifact_id for r in refs):
            continue
        receipt, sha = read_receipt(data_root, artifact_id)
        if receipt.dataset != card.dataset or receipt.plan_id != card.plan_id:
            raise IntegrityError(
                f"mismatch: receipt {artifact_id!r} belongs to {receipt.dataset}/{receipt.plan_id}"
                f", not {card.dataset}/{card.plan_id}",
                fields={"receipt": artifact_id, "run": card.run_id},
            )
        if receipt.run_id is not None and receipt.run_id != card.run_id:
            raise IntegrityError(
                f"mismatch: receipt {artifact_id!r} was produced under run {receipt.run_id!r}, "
                f"not {card.run_id!r}",
                fields={"receipt": artifact_id, "run": card.run_id},
            )
        refs.append(
            AccessRef(
                artifact_id=artifact_id,
                purpose=receipt.purpose,
                subsets=sorted(receipt.accessed),
                sealed_accessed=receipt.sealed_accessed,
                denied=receipt.denied,
                receipt_sha256=sha,
                binding="manual" if receipt.run_id is None else "session",
            )
        )
    return card.model_copy(update={"access": refs})
