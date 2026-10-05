"""Is a backup manifest complete (spec 2026-10-04 §4, VCP-045)?

Before 0.10.0 a manifest kept one checkpoint per file name, so a run whose five folds each wrote
``model.pt`` was listed with one of them; 0.10.0 fixed how manifests are built, not how old ones
are judged. The rule here judges any manifest by its own content: every run record it lists
names the checkpoints the run had registered, and the evidence and label sets it had attached,
when the manifest was written -- each of those files must be listed, under any role or kind (a
checkpoint under ``runs/<id>/train/`` is listed as a tier-2 ``train_dir`` file, and that counts).
A record only grows, so what it gained after ``created_at`` makes the manifest stale (the
consistency layer's drift says so), not incomplete. ``verify``, ``status``, a tier-3 ``push``,
``--forget-remote`` and the manifest's own self-check all ask ``manifest_gaps``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vcp.backup.manifest import entry_key, local_path
from vcp.backup.schema import Manifest
from vcp.core.config import load_yaml_model
from vcp.core.paths import DatasetPaths, artifact_dir, resolve_stored_path
from vcp.core.time import parse_stamp
from vcp.data.evidence_ref import EvidenceRef
from vcp.measure.schema import RunCard
from vcp.train.checkpoints import newest_per_path
from vcp.train.schema import TrainRecord

RECORD_ROLES = ("run_card", "train_record")


@dataclass(frozen=True)
class Gap:
    """A file the manifest should list and does not. ``what`` names the record field that
    registered it (the verify problem is ``manifest_incomplete:<what>``); ``key`` is the manifest
    key it would have had."""

    run: str
    what: str
    key: str


def _created(manifest: Manifest) -> datetime | None:
    """When the manifest was written; None (later than everything) if its stamp was edited."""
    try:
        return parse_stamp(manifest.created_at)
    except ValueError:
        return None


def _by(text: str, created: datetime | None) -> bool:
    """Registered or attached no later than the manifest. A stamp that does not parse counts as
    earlier: the timestamp layer reports the stamp itself."""
    if created is None:
        return True
    try:
        return parse_stamp(text) <= created
    except ValueError:
        return True


def checkable(manifest: Manifest, paths: DatasetPaths) -> bool:
    """Whether this machine holds every run record the manifest lists; a new machine before
    ``pull`` does not, and then ``status`` falls back to what the verify rows recorded."""
    return all(
        local_path(e, paths.data_root, paths.configs_root).is_file()
        for e in manifest.files
        if e.role in RECORD_ROLES
    )


def manifest_gaps(manifest: Manifest, paths: DatasetPaths) -> list[Gap]:
    """Every file the manifest's run records had registered or attached by ``created_at`` that
    it does not list, one gap per manifest key, in manifest order. A record that is not on this
    machine is skipped (see ``checkable``); one that does not load is ``ValidationFailed``, as in
    the consistency layer."""
    listed = {e.key for e in manifest.files}
    created = _created(manifest)
    gaps: dict[str, Gap] = {}

    def need(run: str, what: str, path: Path) -> None:
        key = entry_key(path, paths.data_root, paths.configs_root)
        if key not in listed and key not in gaps:
            gaps[key] = Gap(run, what, key)

    for e in manifest.files:
        if e.role not in RECORD_ROLES:
            continue
        local = local_path(e, paths.data_root, paths.configs_root)
        if not local.is_file():
            continue
        refs: list[EvidenceRef]
        if e.role == "train_record":
            record = load_yaml_model(local, TrainRecord)
            run, owner, refs = record.run_id, f"{record.run_id}/train.yaml", record.evidence
            registered = [c for c in record.checkpoints if _by(c.registered_at, created)]
            for path in newest_per_path(registered):
                stored = resolve_stored_path(path, paths.data_root)
                need(run, f"{owner}:checkpoints.{path}", stored)
        else:
            card = load_yaml_model(local, RunCard)
            run, owner, refs = card.run_id, f"{card.run_id}/run.yaml", card.evidence
        for ref in refs:
            if _by(ref.attached_at, created):
                adir = artifact_dir(paths.data_root, ref.kind, ref.artifact_id)
                what = f"{owner}:evidence.{ref.kind}/{ref.artifact_id}"
                need(run, what, adir / "manifest.json")
    return list(gaps.values())
