"""Attaching what a run read (spec 2026-09-26 §3.2, §4.2): an evidence file becomes an immutable
``evidence`` artifact, a label set is checked against the run, and either way the caller gets an
``EvidenceRef``. ``vcp train run``, ``Session`` and ``vcp eval ingest`` share this; writing the
reference into ``train.yaml`` / ``run.yaml`` is theirs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import validate_name
from vcp.core.time import stamp
from vcp.data.evidence_ref import (
    LABELS_ROLE,
    Binding,
    EvidenceKind,
    EvidenceRef,
    check_name,
    current,
)
from vcp.data.labels import LABEL_SET_KIND, load_label_set

EVIDENCE_KIND = "evidence"


@dataclass(frozen=True)
class RunScope:
    """What an attachment must agree with: the run, and the dataset version, plan and subsets it
    trains on (spec §4.2)."""

    run_id: str
    dataset: str
    samples_hash: str
    plan_id: str
    trained_on: tuple[str, ...]


def parse_evidence_args(items: list[str]) -> list[tuple[str, Path]]:
    """``NAME=PATH`` options in order; a malformed item or a name given twice is refused."""
    out: list[tuple[str, Path]] = []
    for item in items:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            raise ValidationFailed(
                f"--evidence expects NAME=PATH, got {item!r}", fields={"evidence": item}
            )
        if any(n == name for n, _ in out):
            raise ValidationFailed(
                f"evidence_conflict: --evidence {name!r} is given twice", fields={"evidence": name}
            )
        out.append((name, Path(path)))
    return out


def check_evidence_file(name: str, path: Path, role: str) -> None:
    """Everything an evidence attachment needs before a byte is copied."""
    validate_name(name)
    validate_name(role)
    if role == LABELS_ROLE:
        raise ValidationFailed(
            f"role_reserved: role {LABELS_ROLE!r} is for label sets; attach them with --labels "
            "or Session.attach_labels",
            fields={"evidence": name},
        )
    if not path.exists():
        raise ValidationFailed(f"not_found: evidence {name!r} ({path})", fields={"evidence": name})
    if not path.is_file():
        raise ValidationFailed(
            f"not_a_file: evidence {name!r} ({path}) is not a file", fields={"evidence": name}
        )


def check_names(
    existing: list[EvidenceRef], evidence: list[tuple[str, Path]], labels: list[str]
) -> None:
    """spec 2026-09-26 §5.2 / §5.4 "names do not conflict", before anything is written: no
    ``--evidence`` name is also a ``--labels`` id, and no name is already bound to the run under
    another role or kind. Shared by ``train run`` (§5.2) and ``eval ingest`` (§5.4)."""
    names = {name for name, _ in evidence}
    for label_set_id in labels:
        if label_set_id in names:
            raise ValidationFailed(
                f"evidence_conflict: {label_set_id!r} is both an --evidence name and a --labels id",
                fields={"evidence": label_set_id},
            )
    for name, _ in evidence:
        check_name(existing, name, name, "evidence")
    for label_set_id in labels:
        check_name(existing, label_set_id, LABELS_ROLE, "label_set")


def _ref(
    data_root: Path,
    kind: EvidenceKind,
    artifact_id: str,
    *,
    name: str,
    role: str,
    attempt: int | None,
    binding: Binding,
) -> EvidenceRef:
    return EvidenceRef(
        name=name,
        role=role,
        kind=kind,
        artifact_id=artifact_id,
        manifest_sha256=sha256_file(store.manifest_path(data_root, kind, artifact_id)),
        attempt=attempt,
        attached_at=stamp(),
        binding=binding,
    )


def attach_evidence(
    data_root: Path,
    scope: RunScope,
    name: str,
    path: Path,
    *,
    role: str,
    attempt: int | None,
    binding: Binding,
) -> EvidenceRef:
    """Copy ``path`` into ``evidence/<run>-<name>-<sha12>``, or reuse it when those bytes are
    already there (a ``--resume``), and return the reference."""
    check_evidence_file(name, path, role)
    digest = sha256_file(path)
    artifact_id = f"{scope.run_id}-{name}-{digest[:12]}"
    spec = ArtifactSpec(
        kind=EVIDENCE_KIND,
        id=artifact_id,
        dataset=scope.dataset,
        plan_id=scope.plan_id,
        params={"run": scope.run_id, "name": name, "role": role},
        inputs=[InputRef(name="source", sha256=digest)],
    )
    if store.reuse(spec, data_root, check_files=True) is None:
        with ArtifactWriter.create(spec, data_root=data_root) as writer:
            entry = writer.add_file(path.name, path)
            if entry.sha256 != digest:
                raise IntegrityError(
                    f"drift: evidence {name!r} changed while it was being copied",
                    fields={"evidence": name},
                )
            writer.commit()
    return _ref(
        data_root,
        EVIDENCE_KIND,
        artifact_id,
        name=name,
        role=role,
        attempt=attempt,
        binding=binding,
    )


def label_ref(
    data_root: Path,
    scope: RunScope,
    label_set_id: str,
    *,
    attempt: int | None,
    binding: Binding,
) -> EvidenceRef:
    """The reference to a label set that fits the run: same dataset version and plan, subsets
    within ``trained_on`` (``labels_mismatch:`` otherwise). Writes nothing."""
    s = load_label_set(data_root, label_set_id)
    problems: list[str] = []
    if s.dataset != scope.dataset:
        problems.append(f"dataset {s.dataset} != {scope.dataset}")
    if s.samples_hash != scope.samples_hash:
        problems.append(f"samples_hash {s.samples_hash[:12]} != {scope.samples_hash[:12]}")
    if s.plan_id != scope.plan_id:
        problems.append(f"plan {s.plan_id} != {scope.plan_id}")
    beyond = sorted(set(s.subsets) - set(scope.trained_on))
    if beyond:
        problems.append(f"subsets {beyond} are not in trained_on {sorted(scope.trained_on)}")
    if problems:
        raise ValidationFailed(
            f"labels_mismatch: label_set/{label_set_id}: {'; '.join(problems)}",
            fields={"label_set": label_set_id},
        )
    return _ref(
        data_root,
        LABEL_SET_KIND,
        label_set_id,
        name=label_set_id,
        role=LABELS_ROLE,
        attempt=attempt,
        binding=binding,
    )


def source_sha(data_root: Path, ref: EvidenceRef) -> str:
    """The sha256 of the one file an evidence artifact holds: its source's bytes when attached."""
    return store.load_manifest(data_root, ref.kind, ref.artifact_id).files[0].sha256


def ref_intact(data_root: Path, ref: EvidenceRef) -> bool:
    """The artifact exists, verifies, and still has the manifest the run pinned."""
    try:
        if store.verify(data_root, ref.kind, ref.artifact_id).failed:
            return False
    except (ValidationFailed, IntegrityError, OSError):
        return False
    return (
        sha256_file(store.manifest_path(data_root, ref.kind, ref.artifact_id))
        == ref.manifest_sha256
    )


def broken_refs(data_root: Path, refs: list[EvidenceRef]) -> list[str]:
    """Names of current references that are no longer intact (``train status --verify``)."""
    return [r.name for r in current(refs) if not ref_intact(data_root, r)]
