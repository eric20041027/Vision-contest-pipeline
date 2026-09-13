"""Where a receipt lives and how it is claimed (spec 5, 6.5, 6.6): an ``access_receipt``
artifact of the immutable-artifact layer, claimed when the access opens and committed when it
closes. ``ReceiptBinding`` is how an execution environment (the training layer's session) names
the receipt and learns of it; the data layer defines the protocol and never imports the
training layer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple, Protocol

from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import utc_now
from vcp.data.access.schema import AccessReceipt, AccessRef, Purpose

KIND = "access_receipt"
RECEIPT_FILE = "receipt.json"
MAX_CLAIM_RETRIES = 100


class ReceiptBinding(Protocol):
    """Implemented by whoever runs the job (``vcp.train.session.SessionBinding``)."""

    run_id: str
    attempt: int

    def receipt_id(self, seq: int) -> str: ...

    def next_seq(self) -> int: ...

    def on_commit(self, ref: AccessRef) -> None: ...


def standalone_receipt_id(purpose: str, dataset: str, plan_id: str) -> str:
    """A receipt nobody bound: purpose, dataset, plan, a UTC stamp and a nonce."""
    return f"{purpose}-{dataset}-{plan_id}-{utc_now():%Y%m%dT%H%M%S}-{os.urandom(2).hex()}"


def receipt_spec(
    *,
    receipt_id: str,
    purpose: Purpose,
    run_id: str | None,
    attempt: int | None,
    paths: DatasetPaths,
    plan_id: str,
    samples_hash: str,
    plan_sha256: str,
    card_sha256: str,
) -> ArtifactSpec:
    # Only samples.jsonl lives under data_root; card.yaml and the plan json live under
    # configs_root, so recording them as `inputs` would store absolute filesystem paths in
    # spec.json / manifest.json (the privacy envelope is data-root-relative paths only).
    # Both shas are already carried by receipt.json's own card_sha256/plan_sha256 fields, so
    # nothing is lost by keeping them here as opaque params instead of artifact inputs.
    params = {"purpose": purpose, "card_sha256": card_sha256, "plan_sha256": plan_sha256}
    if run_id is not None:
        params["run"] = run_id
    if attempt is not None:
        params["attempt"] = str(attempt)
    return ArtifactSpec(
        kind=KIND,
        id=receipt_id,
        dataset=paths.name,
        plan_id=plan_id,
        params=params,
        inputs=[
            InputRef(name="samples", path=str(paths.samples_jsonl), sha256=samples_hash),
        ],
    )


def claim_receipt(
    spec: ArtifactSpec, data_root: Path, binding: ReceiptBinding | None
) -> ArtifactWriter:
    """Claim the receipt's directory. With a binding the id is ``binding.receipt_id(seq)`` and a
    taken seq (another worker of the same attempt) is retried with the next one."""
    if binding is None:
        return ArtifactWriter.create(spec, data_root=data_root)
    seq = binding.next_seq()
    for _ in range(MAX_CLAIM_RETRIES):
        candidate = spec.model_copy(update={"id": binding.receipt_id(seq)})
        try:
            return ArtifactWriter.create(candidate, data_root=data_root)
        except ValidationFailed as e:
            if not str(e).startswith("exists:"):
                raise
            seq += 1
    raise ValidationFailed(
        f"exists: no free receipt id for run {binding.run_id!r} attempt {binding.attempt} "
        f"after {MAX_CLAIM_RETRIES} tries"
    )


class ReceiptFile(NamedTuple):
    receipt: AccessReceipt
    sha256: str


def read_receipt(data_root: Path, artifact_id: str) -> ReceiptFile:
    """A committed, verified receipt and the sha of its ``receipt.json``."""
    manifest = store.load_manifest(data_root, KIND, artifact_id)
    res = store.verify(data_root, KIND, artifact_id)
    if res.failed:
        raise IntegrityError(
            f"mismatch: receipt {artifact_id!r} no longer matches its manifest "
            f"(mismatch={len(res.mismatch)} missing={len(res.missing)} extra={len(res.extra)})",
            fields={"receipt": artifact_id},
        )
    entry = manifest.file(RECEIPT_FILE)
    if entry is None:
        raise ValidationFailed(
            f"not_found: receipt {artifact_id!r} has no {RECEIPT_FILE}",
            fields={"receipt": artifact_id},
        )
    path = store.manifest_path(data_root, KIND, artifact_id).parent / RECEIPT_FILE
    try:
        receipt = AccessReceipt.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValidationFailed(f"bad receipt: {e}", location=str(path)) from e
    return ReceiptFile(receipt, entry.sha256)
