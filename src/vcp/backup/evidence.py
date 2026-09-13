"""The evidence graph (spec 7): from a conclusion to every file that supports it.

A conclusion is a final submission, a judgement, a run, or a whole dataset. Walking it yields the
files a reader would need to reproduce or re-verify that conclusion, each tagged with a role (and
so a tier) and with the conclusions it serves. Nothing here copies anything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import default_manifest_id, write_manifest
from vcp.backup.schema import ROLES, TIER_OF, BackupRow, FileEntry, Manifest, RemoteCopy
from vcp.core.build import build_string
from vcp.core.config import load_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import (
    DatasetPaths,
    artifact_dir,
    logs_dir,
    resolve_stored_path,
    validate_name,
)
from vcp.core.time import stamp
from vcp.fuse.build import load_record, record_path
from vcp.fuse.recipes import recipe_path
from vcp.measure.ledger import JUDGEMENTS_LEDGER, READINGS_LEDGER, SIGMA_LEDGER
from vcp.measure.prereg import list_preregs, load_prereg, prereg_path
from vcp.measure.runs import load_run, run_dir
from vcp.measure.schema import RunCard
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import load_profile
from vcp.submit.stage import load_staged, stage_json
from vcp.train.records import events_path, has_record, train_dir, train_yaml
from vcp.train.records import load_record as load_train_record
from vcp.train.schema import CheckpointRecord, TrainRecord

CONCLUSIONS = ("submission", "judgement", "run", "all")
HISTORY = "history.jsonl"
ANCHORS = "anchors.json"
ANCHORS_LOG = "anchors.log.jsonl"
MEASURE_LEDGERS = (
    (READINGS_LEDGER, "readings"),
    (JUDGEMENTS_LEDGER, "judgements"),
    (SIGMA_LEDGER, "sigma"),
    (ANCHORS, "anchors"),
    (ANCHORS_LOG, "anchors_log"),
)


def parse_conclusion(text: str) -> tuple[str, str]:
    """``all`` -> ("all", ""); ``run:<id>`` -> ("run", "<id>"); anything else is a FAIL."""
    kind, sep, ident = text.partition(":")
    if kind == "all" and not sep:
        return "all", ""
    if kind not in CONCLUSIONS or kind == "all" or not ident:
        raise ValidationFailed(
            f"bad conclusion {text!r}: expected submission:<id>, judgement:<prereg>, "
            "run:<id> or all"
        )
    validate_name(ident)
    return kind, ident


def external_path(path: Path) -> str:
    """``C:/x/y`` -> ``C/x/y``, ``/mnt/x`` -> ``mnt/x``: a relative posix path that keeps the
    origin."""
    return path.resolve().as_posix().replace(":", "").lstrip("/")


@dataclass
class Collector:
    data_root: Path
    configs_root: Path
    entries: dict[str, FileEntry] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    _seen: set[tuple[str, str]] = field(default_factory=set)

    def locate(self, path: Path) -> tuple[str, str, str | None]:
        """(root, relative posix path, source): data / configs by containment, else external."""
        resolved = path.resolve()
        for root, base in (("data", self.data_root), ("configs", self.configs_root)):
            try:
                return root, resolved.relative_to(base.resolve()).as_posix(), None
            except ValueError:
                continue
        return "external", external_path(resolved), resolved.as_posix()

    def add(
        self,
        path: Path,
        role: str,
        conclusion: str,
        *,
        sha256: str | None = None,
        size: int | None = None,
        remote: RemoteCopy | None = None,
    ) -> None:
        """Record one file. A present file is hashed now; an absent one is listed with the sha a
        record vouches for (so verify / pull can still act on it), or dropped as `unlisted`."""
        root, rel, source = self.locate(path)
        key = f"{root}/{rel}"
        entry = self.entries.get(key)
        if entry is not None:
            if conclusion not in entry.for_:
                entry.for_.append(conclusion)
            return
        present = path.is_file()
        if present:
            sha256 = sha256_file(path)
            size = path.stat().st_size
        elif sha256 is None:
            if key not in self.unlisted:
                self.unlisted.append(key)
            return
        else:
            self.missing.append(key)
            size = size or 0
        self.entries[key] = FileEntry(
            root=root,  # type: ignore[arg-type]
            path=rel,
            sha256=sha256,
            bytes=size,
            role=role,
            tier=TIER_OF[role],
            kind="remote_copy" if remote is not None else "file",
            present=present,
            remote=remote,
            source=source,
            for_=[conclusion],
        )

    def files_of(self) -> list[FileEntry]:
        return sorted(
            self.entries.values(), key=lambda e: (e.tier, ROLES.index(e.role), e.root, e.path)
        )

    def measure_ledgers(self, dpaths: DatasetPaths, conclusion: str) -> None:
        for name, role in MEASURE_LEDGERS:
            p = dpaths.measure_dir / name
            if p.is_file():
                self.add(p, role, conclusion)

    def dataset_basics(self, dpaths: DatasetPaths, plan_id: str | None, conclusion: str) -> None:
        if dpaths.card_yaml.is_file():
            self.add(dpaths.card_yaml, "dataset_card", conclusion)
        if plan_id is not None:
            self.add(dpaths.plan_json(plan_id), "plan", conclusion)
            unseal = dpaths.unseal_jsonl(plan_id)
            if unseal.is_file():
                self.add(unseal, "unseal_log", conclusion)

    def _dataset_paths(self, name: str) -> DatasetPaths:
        return DatasetPaths.resolve(name, data_root=self.data_root, configs_root=self.configs_root)

    def walk_run(self, run_id: str, conclusion: str) -> None:
        if (run_id, conclusion) in self._seen:
            return
        rdir = run_dir(self.data_root, run_id)
        if not (rdir / "run.yaml").is_file():  # marked seen only once it is really there, so a
            raise ValidationFailed(  # dangling run is reported for every conclusion that names it
                f"not_found: run {run_id!r} has no run.yaml under {rdir}", fields={"run": run_id}
            )
        self._seen.add((run_id, conclusion))
        card = load_run(self.data_root, run_id)
        self.add(rdir / "run.yaml", "run_card", conclusion)
        for ref in card.access:  # spec 8: the receipts are the evidence of what it trained on
            adir = artifact_dir(self.data_root, "access_receipt", ref.artifact_id)
            self.add(adir / "manifest.json", "access_receipt", conclusion)
            self.add(adir / "receipt.json", "access_receipt", conclusion, sha256=ref.receipt_sha256)
        if (rdir / HISTORY).is_file():
            self.add(rdir / HISTORY, "history", conclusion)
        for entry in card.predictions.values():
            self.add(rdir / entry.path, "prediction", conclusion, sha256=entry.sha256)
        if record_path(self.data_root, run_id).is_file():
            self.add(record_path(self.data_root, run_id), "fuse_record", conclusion)
            rec = load_record(self.data_root, run_id)
            rpath = recipe_path(self._dataset_paths(card.dataset), rec.recipe_id)
            if rpath.is_file():
                self.add(rpath, "recipe", conclusion)
            for m in rec.members:
                self.walk_run(m.run, conclusion)
        if has_record(self.data_root, run_id):
            self.add(train_yaml(self.data_root, run_id), "train_record", conclusion)
            if events_path(self.data_root, run_id).is_file():
                self.add(events_path(self.data_root, run_id), "train_log", conclusion)
            tdir = train_dir(self.data_root, run_id)
            if tdir.is_dir():
                for p in sorted(tdir.rglob("*")):
                    if p.is_file():
                        self.add(p, "train_dir", conclusion)
            self._checkpoints(load_train_record(self.data_root, run_id), conclusion)
        dpaths = self._dataset_paths(card.dataset)
        self.dataset_basics(dpaths, card.plan_id, conclusion)
        self.measure_ledgers(dpaths, conclusion)

    def _checkpoints(self, record: TrainRecord, conclusion: str) -> None:
        """The newest record per checkpoint file name (a --resume that changed the bytes is
        history); a copy `train upload` verified becomes a remote_copy instead of a file."""
        newest: dict[str, CheckpointRecord] = {}
        for c in record.checkpoints:
            newest[Path(c.path).name] = c
        for name, c in newest.items():
            remote = None
            for u in reversed(record.uploads):
                if u.name == name and u.sha256 == c.sha256 and u.verified:
                    remote = RemoteCopy(dest=u.dest, run=record.run_id, name=name)
                    break
            self.add(
                resolve_stored_path(c.path, self.data_root),
                "checkpoint_final" if c.final else "checkpoint",
                conclusion,
                sha256=c.sha256,
                size=c.bytes,
                remote=remote,
            )

    def walk_judgement(self, dpaths: DatasetPaths, prereg_id: str, conclusion: str) -> None:
        path = prereg_path(dpaths, prereg_id)
        if not path.is_file():
            raise ValidationFailed(
                f"not_found: pre-registration {prereg_id!r} ({path})", fields={"prereg": prereg_id}
            )
        pr = load_prereg(dpaths, prereg_id)
        self.add(path, "prereg", conclusion)
        if dpaths.prereg_log.is_file():
            self.add(dpaths.prereg_log, "prereg_log", conclusion)
        self.measure_ledgers(dpaths, conclusion)
        self.walk_run(pr.baseline_run, conclusion)
        self.walk_run(pr.candidate_run, conclusion)

    def walk_submission(self, tpaths: DatasetPaths, submission_id: str, conclusion: str) -> None:
        profile, _ = load_profile(tpaths)
        if not stage_json(tpaths, submission_id).is_file():
            raise ValidationFailed(
                f"not_found: submission {submission_id!r} has no stage.json under "
                f"{tpaths.submission_dir(submission_id)}",
                fields={"id": submission_id},
            )
        staged = load_staged(tpaths, submission_id)
        self.add(tpaths.submit_yaml, "submit_profile", conclusion)
        if tpaths.submissions_log.is_file():
            self.add(tpaths.submissions_log, "submissions_log", conclusion)
        self.add(stage_json(tpaths, submission_id), "stage", conclusion)
        if staged.artifact.kind == "file":
            self.add(
                tpaths.submission_dir(submission_id) / str(staged.artifact.path),
                "artifact",
                conclusion,
                sha256=staged.artifact.sha256,
                size=staged.artifact.bytes,
            )
        self.walk_run(staged.eval_run, conclusion)
        if staged.test_run:
            self.walk_run(staged.test_run, conclusion)
        for w in staged.artifact.weights:
            self.walk_run(w.run, conclusion)
        epaths = self._dataset_paths(profile.eval_dataset)
        for pid in staged.gate.judgements:
            self.walk_judgement(epaths, pid, conclusion)
        self.dataset_basics(tpaths, profile.test_plan, conclusion)

    def _try(self, label: str, walk: Callable[..., None], *args: Any) -> None:
        """``all`` means "everything this dataset still has": one conclusion whose evidence has
        gone missing is recorded and stepped over, never a reason to lose all the others."""
        try:
            walk(*args)
        except ValidationFailed as e:
            self.skipped.append(f"{label}: {e}")

    def walk_all(self, dpaths: DatasetPaths) -> None:
        conclusion = "all"
        if dpaths.runs_dir.is_dir():
            for p in sorted(dpaths.runs_dir.glob("*/run.yaml")):
                try:
                    card = load_yaml_model(p, RunCard)
                except (ValidationFailed, OSError, UnicodeDecodeError):
                    continue  # another project's or a half-written card is not this dataset's
                if card.dataset == dpaths.name:
                    self._try(f"run:{card.run_id}", self.walk_run, card.run_id, conclusion)
        for pid in list_preregs(dpaths):
            self._try(f"judgement:{pid}", self.walk_judgement, dpaths, pid, conclusion)
        if dpaths.submit_yaml.is_file():
            for sid in SubmissionLedger(dpaths.submissions_log).ids():
                if stage_json(dpaths, sid).is_file():
                    self._try(f"submission:{sid}", self.walk_submission, dpaths, sid, conclusion)
        self.dataset_basics(dpaths, None, conclusion)
        if dpaths.splits_dir.is_dir():
            for p in sorted(dpaths.splits_dir.glob("*.json")):
                self.add(p, "plan", conclusion)
            for p in sorted(dpaths.splits_dir.glob("*.unseal.jsonl")):
                self.add(p, "unseal_log", conclusion)
        self.measure_ledgers(dpaths, conclusion)
        if dpaths.samples_jsonl.is_file():
            self.add(dpaths.samples_jsonl, "samples", conclusion)
        if dpaths.raw_manifest.is_file():
            self.add(dpaths.raw_manifest, "raw_manifest", conclusion)
        for p in sorted(logs_dir(self.data_root).glob("vcp-*.jsonl")):
            self.add(p, "logs", conclusion)


@dataclass(frozen=True)
class ManifestResult:
    manifest: Manifest
    path: Path
    missing: list[str]
    unlisted: list[str]
    skipped: list[str]


def build_manifest(
    dataset: str,
    conclusion: str,
    *,
    manifest_id: str | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> ManifestResult:
    """Walk the conclusion, write the manifest (once) and its ledger row."""
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    kind, ident = parse_conclusion(conclusion)
    if not paths.card_yaml.is_file():
        raise ValidationFailed(f"not_found: dataset {dataset!r} ({paths.card_yaml})")
    mid = manifest_id or default_manifest_id(conclusion)
    validate_name(mid)
    if paths.backup_manifest(mid).exists():
        raise ValidationFailed(f"exists: manifest {mid!r}", fields={"manifest": mid})
    col = Collector(paths.data_root, paths.configs_root)
    if kind == "run":
        if not (run_dir(paths.data_root, ident) / "run.yaml").is_file():
            raise ValidationFailed(f"not_found: run {ident!r}", fields={"run": ident})
        if load_run(paths.data_root, ident).dataset != dataset:
            raise ValidationFailed(
                f"run {ident!r} belongs to dataset {load_run(paths.data_root, ident).dataset!r}, "
                f"not {dataset!r}",
                fields={"run": ident},
            )
        col.walk_run(ident, conclusion)
    elif kind == "judgement":
        col.walk_judgement(paths, ident, conclusion)
    elif kind == "submission":
        col.walk_submission(paths, ident, conclusion)
    else:
        col.walk_all(paths)
    manifest = Manifest(
        manifest_id=mid,
        dataset=dataset,
        conclusion=conclusion,
        created_at=stamp(),
        vcp_version=build_string(),
        data_root=paths.data_root.as_posix(),
        files=col.files_of(),
    )
    path = write_manifest(paths, manifest)
    BackupLedger(paths.backup_log).append(
        BackupRow(
            event="manifest",
            ts=stamp(),
            manifest_id=mid,
            conclusion=conclusion,
            files=len(manifest.files),
            bytes_by_tier=manifest.bytes_by_tier(),
            missing=len(col.missing),
            remote_copies=sum(1 for f in manifest.files if f.kind == "remote_copy"),
        )
    )
    return ManifestResult(manifest, path, col.missing, col.unlisted, col.skipped)
