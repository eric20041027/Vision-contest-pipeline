"""``vcp backup verify`` (spec 6.2): three layers, none of which needs the machine that wrote
the manifest. Copies: what a destination holds against the manifest. Consistency: the sha chains
between local files (cards -> predictions, records -> checkpoints, ledgers -> stage files, the
manifest -> everything). Timestamps: every ledger row's ``ts`` parses and never runs backwards;
every card's ``*_at`` / ``ts`` parses (``downloaded_at`` is a human date and is skipped)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from vcp.backup.dest import Destination, open_dest
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import load_manifest, local_path
from vcp.backup.push import check_tier
from vcp.backup.schema import CARD_ROLES, LEDGER_ROLES, BackupRow, Manifest
from vcp.core.config import load_yaml_model
from vcp.core.errors import PlatformError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_prefix
from vcp.core.paths import DatasetPaths, resolve_stored_path
from vcp.core.proc import Runner
from vcp.core.time import parse_stamp, stamp
from vcp.data.schema import DatasetCard
from vcp.fuse.build import load_record as load_fuse_record
from vcp.measure.runs import prediction_path
from vcp.measure.schema import RunCard
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.schema import Staged
from vcp.train.schema import TrainRecord

SKIP_KEYS = frozenset({"downloaded_at"})
REASONS = ("mismatch", "missing", "drift", "bad_stamps")
Adder = Callable[[str, str, str], None]


@dataclass(frozen=True)
class Drift:
    what: str
    expected: str
    actual: str


@dataclass(frozen=True)
class VerifyResult:
    manifest_id: str
    dest: str | None
    copies: dict[str, int] | None
    copy_problems: list[str]
    drift: list[Drift]
    bad_stamps: list[str]

    @property
    def first_bad(self) -> str | None:
        return self.bad_stamps[0] if self.bad_stamps else None

    @property
    def problems(self) -> list[str]:
        out = list(self.copy_problems)
        out += [f"drift:{d.what}" for d in self.drift]
        out += [f"bad_stamps:{b}" for b in self.bad_stamps]
        return out

    @property
    def reason(self) -> str | None:
        kinds = {p.split(":", 1)[0] for p in self.problems}
        return next((r for r in REASONS if r in kinds), None)

    @property
    def ok(self) -> bool:
        return not self.problems


def _load_json_model[T: BaseModel](path: Path, model_cls: type[T]) -> T:
    try:
        return model_cls.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError) as e:
        raise ValidationFailed(f"bad {path.name}: {e}", location=str(path)) from e


# --- layer 1: copies -------------------------------------------------------------------------


def _check_copies(
    manifest: Manifest, dest: str, tier: int, runner: Runner | None
) -> tuple[dict[str, int], list[str]]:
    """Only entries with ``tier <= tier``: a destination that holds tiers 1..N is complete for
    them even though the weights were never pushed. An entry the manifest already recorded as
    gone (``present=false``) is ``absent`` at a destination that never got it -- a fact, not a
    failure; a copy of it that is there still has to match."""
    counts = {"ok": 0, "missing": 0, "mismatch": 0, "absent": 0}
    problems: list[str] = []
    dests: dict[str, Destination] = {dest: open_dest(dest, runner)}
    groups: dict[tuple[str, str], list[tuple[str, str, str, bool]]] = {}
    for e in manifest.files:
        if e.tier > tier:
            continue
        if e.remote is None:
            groups.setdefault((dest, e.root), []).append((e.path, e.sha256, e.key, e.present))
        else:
            dests.setdefault(e.remote.dest, open_dest(e.remote.dest, runner))
            groups.setdefault((e.remote.dest, e.remote.run), []).append(
                (e.remote.name, e.sha256, e.key, e.present)
            )
    for (d, sub), items in groups.items():
        have = dests[d].hashes(sub, [rel for rel, _, _, _ in items])
        for rel, sha, key, present in items:
            got = have.get(rel)
            if got is None and not present:
                counts["absent"] += 1
            elif got is None:
                counts["missing"] += 1
                problems.append(f"missing:{key}")
            elif got != sha:
                counts["mismatch"] += 1
                problems.append(f"mismatch:{key}")
            else:
                counts["ok"] += 1
    return counts, problems


# --- layer 2: local consistency --------------------------------------------------------------


def _run_card(local: Path, paths: DatasetPaths, add: Adder) -> None:
    card = load_yaml_model(local, RunCard)
    for subset, entry in card.predictions.items():
        p = local.parent / entry.path
        if p.is_file():
            add(f"{card.run_id}/run.yaml:predictions.{subset}", entry.sha256, sha256_file(p))


def _train_record(local: Path, paths: DatasetPaths, add: Adder) -> None:
    rec = load_yaml_model(local, TrainRecord)
    newest = {Path(c.path).name: c for c in rec.checkpoints}  # a --resume that changed bytes wins
    for name, c in newest.items():
        p = resolve_stored_path(c.path, paths.data_root)
        if p.is_file():
            add(f"{rec.run_id}/train.yaml:checkpoints.{name}", c.sha256, sha256_file(p))


def _fuse_record(local: Path, paths: DatasetPaths, add: Adder) -> None:
    rec = load_fuse_record(paths.data_root, local.parent.name)
    for subset, build in rec.subsets.items():
        out = prediction_path(paths.data_root, rec.run_id, subset)
        if out.is_file():
            add(
                f"{rec.run_id}/fuse.json:subsets.{subset}.output",
                build.output_sha256,
                sha256_file(out),
            )
        for member, sha in build.member_sha256.items():
            p = prediction_path(paths.data_root, member, subset)
            if p.is_file():
                add(
                    f"{rec.run_id}/fuse.json:subsets.{subset}.members.{member}", sha, sha256_file(p)
                )


def _stage(local: Path, paths: DatasetPaths, add: Adder) -> None:
    staged = _load_json_model(local, Staged)
    if staged.artifact.kind == "file" and staged.artifact.sha256 and staged.artifact.path:
        p = local.parent / staged.artifact.path
        if p.is_file():
            what = f"submit/{staged.dataset}/{staged.submission_id}/stage.json:artifact"
            add(what, staged.artifact.sha256, sha256_file(p))


def _dataset_card(local: Path, paths: DatasetPaths, add: Adder) -> None:
    card = load_yaml_model(local, DatasetCard)
    dpaths = DatasetPaths.resolve(
        card.name, data_root=paths.data_root, configs_root=paths.configs_root
    )
    if dpaths.samples_jsonl.is_file():
        add(
            f"datasets/{card.name}/dataset.yaml:samples_hash",
            card.samples_hash,
            sha256_file(dpaths.samples_jsonl),
        )


def _submissions_log(local: Path, paths: DatasetPaths, add: Adder) -> None:
    name = local.parent.name
    tpaths = DatasetPaths.resolve(name, data_root=paths.data_root, configs_root=paths.configs_root)
    for row in SubmissionLedger(local).of("staged"):
        sid = str(row.submission_id)
        sj = tpaths.submission_dir(sid) / "stage.json"
        if row.sha256 and sj.is_file():
            staged = _load_json_model(sj, Staged)
            add(
                f"datasets/{name}/submissions.jsonl:staged.{sid}",
                row.sha256,
                str(staged.artifact.sha256),
            )


_CHECKERS: dict[str, Callable[[Path, DatasetPaths, Adder], None]] = {
    "run_card": _run_card,
    "train_record": _train_record,
    "fuse_record": _fuse_record,
    "stage": _stage,
    "dataset_card": _dataset_card,
    "submissions_log": _submissions_log,
}


def _check_consistency(manifest: Manifest, paths: DatasetPaths) -> list[Drift]:
    drift: list[Drift] = []

    def add(what: str, expected: str, actual: str) -> None:
        if expected != actual:
            drift.append(Drift(what, expected, actual))

    for e in manifest.files:
        if not e.present:
            continue
        local = local_path(e, paths.data_root, paths.configs_root)
        if not local.is_file():
            continue
        if e.role in LEDGER_ROLES and local.stat().st_size >= e.bytes:
            add(e.key, e.sha256, sha256_prefix(local, e.bytes))  # growth is not drift
        else:
            add(e.key, e.sha256, sha256_file(local))
        checker = _CHECKERS.get(e.role)
        if checker is not None:
            checker(local, paths, add)
    return drift


# --- layer 3: timestamps ---------------------------------------------------------------------


def _ledger_stamps(local: Path, label: str, bad: list[str]) -> None:
    prev = None
    with local.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                cur = parse_stamp(json.loads(line)["ts"])
            except (ValueError, KeyError, TypeError):
                bad.append(f"{label}:{lineno}")
                continue
            if prev is not None and cur < prev:
                bad.append(f"{label}:{lineno}")
            prev = cur


def _load_doc(local: Path) -> Any:
    try:
        text = local.read_text(encoding="utf-8")
        return json.loads(text) if local.suffix == ".json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValidationFailed(f"bad card: {e}", location=str(local)) from e


def _walk_stamps(obj: Any, label: str, bad: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k)
            if (key == "ts" or key.endswith("_at")) and key not in SKIP_KEYS and v is not None:
                try:
                    parse_stamp(v) if isinstance(v, str) else parse_stamp("")
                except ValueError:
                    bad.append(f"{label}:{key}")
            _walk_stamps(v, label, bad)
    elif isinstance(obj, list):
        for v in obj:
            _walk_stamps(v, label, bad)


def _check_stamps(manifest: Manifest, paths: DatasetPaths) -> list[str]:
    bad: list[str] = []
    for e in manifest.files:
        if not e.present:
            continue
        local = local_path(e, paths.data_root, paths.configs_root)
        if not local.is_file():
            continue
        if e.role in LEDGER_ROLES:
            _ledger_stamps(local, e.key, bad)
        elif e.role in CARD_ROLES:
            _walk_stamps(_load_doc(local), e.key, bad)
    if paths.backup_log.is_file():  # the audit's own ledger, never listed in a manifest
        _ledger_stamps(paths.backup_log, paths.backup_log.name, bad)
    return bad


def verify(
    dataset: str,
    manifest_id: str,
    *,
    dest: str | None = None,
    tier: int = 3,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> VerifyResult:
    """All three layers; the result is returned, not raised, so a VERDICT can carry every count
    and ``--json`` every detail. ``tier`` bounds the copies layer only. The ledger row is
    written before returning."""
    check_tier(tier)
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    manifest = load_manifest(paths, manifest_id)
    copies: dict[str, int] | None = None
    problems: list[str] = []
    copies_error: PlatformError | None = None
    if dest is not None:
        try:
            copies, problems = _check_copies(manifest, dest, tier, runner)
        except PlatformError as exc:
            # the copies layer dying (rclone flaked) must not swallow what the other two layers
            # found: they still run, and the row still lands -- with `copies=None` and `error=`
            # standing in for what a `--dest` run could not tell us this time.
            copies_error = exc
    drift = _check_consistency(manifest, paths)
    bad = _check_stamps(manifest, paths)
    res = VerifyResult(manifest_id, dest, copies, problems, drift, bad)
    BackupLedger(paths.backup_log).append(
        BackupRow(
            event="verify",
            ts=stamp(),
            manifest_id=manifest_id,
            dest=dest,
            tier=tier if dest is not None else None,
            copies=copies,
            drift=len(drift),
            bad_stamps=len(bad),
            first_bad=res.first_bad,
            error=str(copies_error) if copies_error is not None else None,
        )
    )
    if copies_error is not None:
        raise copies_error
    return res
