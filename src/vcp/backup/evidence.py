"""The evidence graph (spec 7): from a conclusion to every file that supports it.

A conclusion is a final submission, a judgement, a run, or a whole dataset. Walking it yields the
files a reader would need to reproduce or re-verify that conclusion, each tagged with a role (and
so a tier) and with the conclusions it serves. Nothing here copies anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from vcp.backup.schema import ROLES, TIER_OF, FileEntry, RemoteCopy
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, resolve_stored_path, validate_name
from vcp.fuse.build import load_record, record_path
from vcp.fuse.recipes import recipe_path
from vcp.measure.report import JUDGEMENTS_LEDGER, READINGS_LEDGER, SIGMA_LEDGER
from vcp.measure.runs import load_run, run_dir
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
        self._seen.add((run_id, conclusion))
        rdir = run_dir(self.data_root, run_id)
        if not (rdir / "run.yaml").is_file():
            raise ValidationFailed(
                f"not_found: run {run_id!r} has no run.yaml under {rdir}", fields={"run": run_id}
            )
        card = load_run(self.data_root, run_id)
        self.add(rdir / "run.yaml", "run_card", conclusion)
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
