"""``vcp train run``: wrap one training command and make the result a run (spec 6.1).

Every check runs before the first file is written: the dataset and plan agree, the export
manifests say which subsets were trained on, the config has a hash, the venv has a python, the
command can be found, the run id is free (or resumable), and every disk the run writes to has
room (spec 2026-10-30 §5.1). Only then the run card, the training record, the console log and
the environment snapshot appear -- and whatever the command does next is recorded, exit code
and all.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_json
from vcp.core.paths import DatasetPaths, store_path
from vcp.core.time import stamp, utc_now
from vcp.data.dataset import Dataset
from vcp.data.evidence import RunScope, check_names
from vcp.data.evidence_ref import current, labels_field, merge_refs
from vcp.data.schema import DatasetCard
from vcp.data.split import SplitPlan, assert_plan_matches, load_plan
from vcp.measure.provenance import provenance
from vcp.measure.runs import append_history, assert_run_matches, load_run, run_dir, save_run
from vcp.measure.schema import RunCard, RunSource
from vcp.train.attach import attach as attach_cli_evidence
from vcp.train.attach import moved, preflight
from vcp.train.checkpoints import expand, register, resolve_final
from vcp.train.disk import (
    DEFAULT_MIN_FREE_GIB,
    DiskFailure,
    assert_room,
    check_min_free_gib,
    diagnose,
    measure,
    own_writes,
    targets,
    unavailable,
)
from vcp.train.env import snapshot, venv_python
from vcp.train.gitstate import changed_since, require_clean
from vcp.train.gitstate import warning as git_warning
from vcp.train.records import (
    TRAIN_DIR,
    append_event,
    has_record,
    load_record,
    save_record,
    train_dir,
)
from vcp.train.schema import (
    Attempt,
    AttemptStatus,
    CheckpointRecord,
    ConfigRef,
    DiskCheck,
    DiskRole,
    EnvSnapshot,
    ExportRef,
    GitInfo,
    TrainRecord,
)
from vcp.train.upload import Runner, merge_uploads, upload

RUN_EXISTS = "run_exists"
RUN_BOUND_ELSEWHERE = "run_bound_elsewhere"
TRAINED_ON_MISMATCH = "trained_on_mismatch"
TERMINATE_TIMEOUT_S = 30
# 5-1: an exception that is on its way out of execute() has already lost the caller; wait far
# less for the child to go quietly than when the user asked for the stop (Ctrl+C) themselves.
ABORT_TIMEOUT_S = 5


class RunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_id: str
    dataset: str
    plan_id: str
    exports: list[Path] = Field(default_factory=list)
    trained_on: list[str] = Field(default_factory=list)
    venv: Path | None = None
    config: Path | None = None
    seed: int | None = None
    framework: str = ""
    cwd: Path | None = None
    checkpoints: list[str] = Field(default_factory=list)
    final: str | None = None
    uploads: list[str] = Field(default_factory=list)
    resume: bool = False
    notes: str = ""
    evidence: list[str] = Field(default_factory=list)  # NAME=PATH (spec 2026-09-26 §5.2)
    labels: list[str] = Field(default_factory=list)  # label_set ids
    require_clean: bool = False  # VCP-041: FAIL before the first write on tracked changes
    min_free_gib: float = DEFAULT_MIN_FREE_GIB  # ABORT below this free space; 0 = only record
    command: list[str]
    on_line: Callable[[str], None] | None = None
    rclone_runner: Runner | None = None
    data_root: Path | None = None
    configs_root: Path | None = None


class RunResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    record: TrainRecord
    card: RunCard
    attempt: Attempt
    final: CheckpointRecord | None
    registered: int
    uploaded: int
    verified: int
    skipped: int
    warnings: list[str]
    receipts: int = 0
    denied: int = 0
    provenance: str = "declared"
    observed_beyond: list[str] = Field(default_factory=list)
    receipt_invalid: int = 0
    source_audit_missing: int = 0
    evidence: int = 0
    labels: str = "dataset"
    evidence_changed: list[str] = Field(default_factory=list)
    git: GitInfo | None = None  # VCP-041: the attempt's start record (spec 2026-09-27 §3.1)
    git_changed: list[str] = Field(default_factory=list)
    disk: DiskCheck | None = None  # the free space measured before the first write (§5.1)
    disk_failure: DiskFailure | None = None  # a failed attempt the disk explains (§5.3)


def read_export(export_dir: Path) -> dict[str, Any]:
    manifest = export_dir / "manifest.json"
    if not manifest.is_file():
        raise ValidationFailed(
            f"--export {export_dir}: manifest.json not found (the directory must be a vcp export)",
            fields={"export": str(export_dir)},
        )
    return json.loads(manifest.read_text(encoding="utf-8"))


def derive_trained_on(
    exports: list[Path],
    explicit: list[str],
    *,
    card: DatasetCard,
    plan: SplitPlan,
    data_root: Path,
) -> tuple[list[str], list[ExportRef]]:
    """spec 6.1 step 2: subsets come from export manifests, which must belong to this dataset,
    version and plan; an explicit --trained-on must agree with them."""
    refs: list[ExportRef] = []
    for export_dir in exports:
        m = read_export(export_dir)
        expected = {
            "dataset": card.name,
            "samples_hash": card.samples_hash,
            "plan_id": plan.plan_id,
        }
        for key, want in expected.items():
            if m.get(key) != want:
                raise PlanMismatchError(
                    f"export {export_dir}: {key} is {m.get(key)!r}, expected {want!r}",
                    fields={"export": str(export_dir)},
                )
        subset = str(m.get("subset", ""))
        try:
            plan.subset(subset)
        except PlanMismatchError as e:
            e.fields.setdefault("export", str(export_dir))
            raise
        refs.append(
            ExportRef(
                dir=store_path(export_dir, data_root),
                subset=subset,
                format=str(m.get("format", "")),
                manifest_sha256=sha256_file(export_dir / "manifest.json"),
                sample_count=int(m.get("sample_count", 0)),
            )
        )
    derived = sorted({r.subset for r in refs})
    if explicit:
        wanted = sorted(set(explicit))
        for name in wanted:
            plan.subset(name)
        if refs and wanted != derived:
            raise ValidationFailed(
                f"{TRAINED_ON_MISMATCH}: --trained-on {wanted} but the exports say {derived}"
            )
        derived = wanted
    if not derived:
        raise ValidationFailed("trained_on is required: give --export DIR or --trained-on a,b")
    return derived, refs


def config_hash(config: Path | None, command: list[str]) -> str:
    """spec 3: the config file's sha256, or the command's canonical JSON sha256 -- never empty."""
    if config is None:
        return sha256_json(command)
    if not config.is_file():
        raise ValidationFailed(f"--config not found: {config}")
    return sha256_file(config)


def child_env(
    spec: RunSpec, *, data_root: Path, configs_root: Path, python: Path | None
) -> dict[str, str]:
    env = dict(os.environ)
    env["VCP_RUN_ID"] = spec.run_id
    env["VCP_DATA_ROOT"] = str(data_root)
    env["VCP_CONFIGS_ROOT"] = str(configs_root)
    if spec.seed is not None:
        env["VCP_SEED"] = str(spec.seed)
        env["PYTHONHASHSEED"] = str(spec.seed)
    if spec.venv is not None and python is not None:
        env["VIRTUAL_ENV"] = str(spec.venv)
        env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    return env


def stop_child(proc: subprocess.Popen[str], timeout: float) -> int:
    """Terminate the child and reap it, killing it if it will not go within ``timeout``.

    Bounded on purpose: ``with Popen`` waits for the child forever on the way out, so a training
    command that ignores SIGTERM must not be able to hold vcp open with it.
    """
    proc.terminate()
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()


def execute(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    console: Path,
    on_line: Callable[[str], None] | None,
) -> tuple[int, AttemptStatus]:
    """Run the command, tee its output to the console file, return (exit code, status).

    A KeyboardInterrupt (Ctrl+C, or a test's ``on_line`` raising it) terminates the child and
    is recorded as ``interrupted`` rather than propagating: the attempt must reach the record.
    Any OTHER exception (5-1) terminates the child too and is re-raised unchanged -- it has no
    status to record, but leaving the child running would hang the wrapper on the way out.
    """
    console.parent.mkdir(parents=True, exist_ok=True)
    status: AttemptStatus = "finished"
    with console.open("w", encoding="utf-8", newline="\n") as log:
        with subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        ) as proc:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    log.write(line)
                    log.flush()
                    if on_line is not None:
                        on_line(line)
                code = proc.wait()
            except KeyboardInterrupt:
                code = stop_child(proc, TERMINATE_TIMEOUT_S)
                status = "interrupted"
                log.write("\n[vcp] interrupted\n")
            except BaseException:
                stop_child(proc, ABORT_TIMEOUT_S)
                raise
    if status == "finished" and code != 0:
        status = "failed"
    return code, status


def command_found(token: str, cwd: Path, path: str | None) -> bool:
    """Whether the child's first token can run: on the child's PATH, or a file under ``cwd``
    (a join with an absolute token yields the token itself, so absolute paths work too)."""
    return shutil.which(token, path=path) is not None or (cwd / token).is_file()


def _existing(
    spec: RunSpec, data_root: Path, dataset_card: DatasetCard, trained_on: list[str], chash: str
) -> tuple[RunCard | None, TrainRecord | None]:
    """spec 6.1 step 4: a free id, a resumable training run, or a refusal."""
    has_card = (run_dir(data_root, spec.run_id) / "run.yaml").is_file()
    if not has_record(data_root, spec.run_id):
        if has_card:
            raise ValidationFailed(
                f"{RUN_BOUND_ELSEWHERE}: run {spec.run_id!r} exists but is not a training run",
                fields={"run": spec.run_id},
            )
        return None, None
    if not spec.resume:
        raise ValidationFailed(
            f"{RUN_EXISTS}: run {spec.run_id!r} already has a training record; "
            "pass --resume to add an attempt",
            fields={"run": spec.run_id},
        )
    record = load_record(data_root, spec.run_id)
    card = load_run(data_root, spec.run_id)
    if record.config_hash != chash:
        raise ValidationFailed(
            f"{RUN_EXISTS}: run {spec.run_id!r} was trained with config_hash "
            f"{record.config_hash[:12]}, this command gives {chash[:12]}; "
            "a different config is a new run",
            fields={"run": spec.run_id},
        )
    assert_run_matches(card, dataset_card)
    if card.plan_id != spec.plan_id or card.trained_on != trained_on:
        raise ValidationFailed(
            f"{RUN_EXISTS}: run {spec.run_id!r} was trained on {card.trained_on} under plan "
            f"{card.plan_id!r}; --resume must keep them",
            fields={"run": spec.run_id},
        )
    return card, record


def _new(
    spec: RunSpec,
    *,
    data_root: Path,
    dataset_card: DatasetCard,
    trained_on: list[str],
    refs: list[ExportRef],
    chash: str,
    cwd: Path,
) -> tuple[RunCard, TrainRecord]:
    card = RunCard(
        run_id=spec.run_id,
        dataset=dataset_card.name,
        samples_hash=dataset_card.samples_hash,
        plan_id=spec.plan_id,
        trained_on=trained_on,
        source=RunSource(
            framework=spec.framework,
            config_hash=chash,
            export_manifest_sha=refs[0].manifest_sha256 if refs else None,
            notes=spec.notes,
        ),
        created_at=stamp(),
    )
    record = TrainRecord(
        run_id=spec.run_id,
        dataset=dataset_card.name,
        plan_id=spec.plan_id,
        trained_on=trained_on,
        exports=refs,
        config_hash=chash,
        seed=spec.seed,
        framework=spec.framework,
        venv=store_path(spec.venv, data_root) if spec.venv is not None else None,
        cwd=store_path(cwd, data_root),
        command=list(spec.command),
        notes=spec.notes,
    )
    return card, record


def _replace_attempt(record: TrainRecord, attempt: Attempt) -> TrainRecord:
    attempts = [attempt if a.n == attempt.n else a for a in record.attempts]
    return record.model_copy(update={"attempts": attempts})


def _close_running(record: TrainRecord, *, data_root: Path, run_id: str) -> TrainRecord:
    """5-3: an attempt still marked ``running`` is one vcp itself did not survive -- nothing came
    back to close it, so it WARNed in ``train status`` forever. A ``--resume`` is the moment it
    is certainly not running any more. No exit code is invented; the event log records the
    reconciliation so the yaml's new status is never the only trace of it.
    """
    for a in [a for a in record.attempts if a.status == "running"]:
        record = _replace_attempt(
            record,
            a.model_copy(
                update={"status": "interrupted", "finished_at": stamp(), "exit_code": None}
            ),
        )
        append_event(
            data_root,
            run_id,
            "note",
            a.n,
            key="resume",
            value=f"attempt {a.n} found running at resume; marked interrupted",
        )
    return record


def _patterns(spec: RunSpec) -> list[str]:
    """Every glob the run registers checkpoints from: ``--checkpoints`` and ``--final``. Also what
    the disk check measures, so the two cannot drift apart."""
    return [*spec.checkpoints, *([spec.final] if spec.final else [])]


def _finish_checkpoints(
    spec: RunSpec,
    record: TrainRecord,
    card: RunCard,
    *,
    data_root: Path,
    cwd: Path,
    n: int,
    status: str,
) -> tuple[TrainRecord, RunCard, CheckpointRecord | None, int, list[str]]:
    """spec 6.1 step 8: register every glob hit; resolve the final one when the command
    succeeded; write weights_hash to the run card."""
    warnings: list[str] = []
    record, added = register(record, expand(_patterns(spec), cwd), data_root=data_root, attempt=n)
    for c in added:
        append_event(
            data_root,
            spec.run_id,
            "checkpoint",
            n,
            path=c.path,
            sha256=c.sha256,
            bytes=c.bytes,
            source=c.source,
        )
    save_record(data_root, record)  # registrations survive a failing --final below
    final: CheckpointRecord | None = None
    if status == "finished":
        record, final = resolve_final(record, spec.final, cwd=cwd, data_root=data_root)
        if final is None:
            warnings.append("final=none")
    elif spec.final:
        # 5-8: the attempt's own status -- "command failed" was wrong for an interrupted one.
        warnings.append(f"final=skipped (attempt {status})")
    if final is not None:
        old_hash = card.source.weights_hash
        if old_hash is not None and old_hash != final.sha256:
            # run directory rule ("換寫留痕"): a --resume that changes the weights rewrites
            # weights_hash, so the old sha must survive somewhere before it is overwritten.
            append_history(
                data_root,
                spec.run_id,
                {
                    "event": "replace",
                    "field": "weights_hash",
                    "old_sha256": old_hash,
                    "via": "train.run",
                },
            )
        source = card.source.model_copy(update={"weights_hash": final.sha256})
        card = card.model_copy(update={"source": source})
        save_run(data_root, card)
    save_record(data_root, record)
    return record, card, final, len(added), warnings


def _upload_all(
    spec: RunSpec, record: TrainRecord, *, data_root: Path, n: int
) -> tuple[TrainRecord, int, int, int]:
    uploaded = verified = skipped = 0
    for dest in spec.uploads:
        before = {(u.dest, u.name, u.sha256) for u in record.uploads}
        # The destination is not vcp's record: a full disk THERE stays the OSError it always was.
        outcome = upload(record, dest, data_root=data_root, runner=spec.rclone_runner)
        record = merge_uploads(record, outcome.records)
        # What vcp writes about the upload is its record (§5.3). The attempt has finished by now,
        # so the recovery is the idempotent `train upload`, not `--resume` (which reruns training).
        hint = f"free space, then: vcp train upload --run {spec.run_id} --dest {dest}"
        with own_writes(hint=hint):
            save_record(data_root, record)
            for r in outcome.records:
                if (r.dest, r.name, r.sha256) not in before:
                    append_event(
                        data_root,
                        spec.run_id,
                        "uploaded",
                        n,
                        dest=dest,
                        name=r.name,
                        sha256=r.sha256,
                        verified=r.verified,
                    )
        uploaded += outcome.uploaded
        skipped += outcome.skipped
        verified += sum(1 for r in outcome.records if r.verified)
    return record, uploaded, verified, skipped


@dataclass(frozen=True)
class _Prepared:
    """What ``train_run`` has checked and measured before its first write."""

    data_root: Path
    configs_root: Path
    cwd: Path
    run_root: Path
    trained_on: list[str]
    chash: str
    python: Path | None
    env: dict[str, str]
    scope: RunScope
    evidence: list[tuple[str, Path]]
    card: RunCard
    record: TrainRecord
    created: bool  # a new run: its card is not on disk yet
    disk_targets: list[tuple[DiskRole, Path]]
    disk_check: DiskCheck


@dataclass(frozen=True)
class _Attempted:
    """What one attempt left behind: the records after it, what it registered, and what is
    worth a warning. ``uploaded``, ``verified`` and ``skipped`` are filled in by the uploads that
    follow a finished attempt."""

    record: TrainRecord
    card: RunCard
    attempt: Attempt
    n: int
    final: CheckpointRecord | None
    registered: int
    warnings: list[str]
    git: GitInfo | None
    git_changed: list[str]
    changed: list[str]
    disk_failure: DiskFailure | None
    uploaded: int = 0
    verified: int = 0
    skipped: int = 0


def _checked_cwd(spec: RunSpec) -> Path:
    """The directory the command runs in, after the checks that need nothing else: there is a
    command, ``--cwd`` is a directory, ``--min-free-gib`` is a number."""
    if not spec.command or spec.command[0].startswith("-"):
        raise ValidationFailed("a training command is required after -- (e.g. -- python train.py)")
    cwd = (spec.cwd or Path.cwd()).resolve()
    # 5-8: Popen would meet this as a bare OSError (ABORT) only after run.yaml, train.yaml and a
    # `running` attempt were on disk. It is bad input, so it is a located FAIL, checked here with
    # everything else that must hold before the first write.
    if not cwd.is_dir():
        raise ValidationFailed(f"not_found: --cwd {cwd} is not a directory")
    check_min_free_gib(spec.min_free_gib)
    return cwd


def _run_scope(spec: RunSpec, dataset_card: DatasetCard, trained_on: list[str]) -> RunScope:
    return RunScope(
        run_id=spec.run_id,
        dataset=dataset_card.name,
        samples_hash=dataset_card.samples_hash,
        plan_id=spec.plan_id,
        trained_on=tuple(trained_on),
    )


def _existing_or_new(
    spec: RunSpec,
    data_root: Path,
    dataset_card: DatasetCard,
    trained_on: list[str],
    refs: list[ExportRef],
    chash: str,
    cwd: Path,
) -> tuple[RunCard, TrainRecord, bool]:
    """The card and record of the run being resumed, or of the new one (``created``)."""
    card, record = _existing(spec, data_root, dataset_card, trained_on, chash)
    created = card is None
    if card is None or record is None:
        card, record = _new(
            spec,
            data_root=data_root,
            dataset_card=dataset_card,
            trained_on=trained_on,
            refs=refs,
            chash=chash,
            cwd=cwd,
        )
    return card, record, created


def _check_disk(
    spec: RunSpec, data_root: Path, cwd: Path
) -> tuple[list[tuple[DiskRole, Path]], DiskCheck]:
    """spec 2026-10-30 §5.1: the free space of every volume the run writes to, measured after the
    last input check and before vcp's first write -- ``_close_running`` appends to
    train.log.jsonl, so an ABORT here leaves the record exactly as it was, ``--resume``
    included. Returns what was measured, for the second look after a failed attempt."""
    disk_targets = targets(run_dir(data_root, spec.run_id), cwd, _patterns(spec))
    disk_check = measure(disk_targets, min_free_gib=spec.min_free_gib, data_root=data_root)
    assert_room(disk_check)
    return disk_targets, disk_check


def _prepare(spec: RunSpec) -> _Prepared:
    """Every check and measurement that comes before vcp's first write (module docstring)."""
    paths = DatasetPaths.resolve(
        spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    data_root, configs_root = paths.data_root, paths.configs_root
    dataset_card = Dataset.load_card(
        spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    plan = load_plan(paths, spec.plan_id)
    assert_plan_matches(plan, dataset_card)
    cwd = _checked_cwd(spec)
    trained_on, refs = derive_trained_on(
        spec.exports, spec.trained_on, card=dataset_card, plan=plan, data_root=data_root
    )
    chash = config_hash(spec.config, spec.command)
    python = venv_python(spec.venv) if spec.venv is not None else None
    env = child_env(spec, data_root=data_root, configs_root=configs_root, python=python)
    if not command_found(spec.command[0], cwd, env.get("PATH")):
        raise ValidationFailed(f"command not found: {spec.command[0]!r}")
    scope = _run_scope(spec, dataset_card, trained_on)
    evidence = preflight(data_root, scope, spec.evidence, spec.labels)
    if spec.require_clean:
        require_clean(cwd)  # VCP-041 (spec 2026-09-27 §4.1): still before the first write
    card, record, created = _existing_or_new(
        spec, data_root, dataset_card, trained_on, refs, chash, cwd
    )
    # spec 2026-09-26 §5.2: names checked against the run's own lists, still before any write.
    check_names([*record.evidence, *card.evidence], evidence, spec.labels)
    disk_targets, disk_check = _check_disk(spec, data_root, cwd)
    return _Prepared(
        data_root=data_root,
        configs_root=configs_root,
        cwd=cwd,
        run_root=run_dir(data_root, spec.run_id),
        trained_on=trained_on,
        chash=chash,
        python=python,
        env=env,
        scope=scope,
        evidence=evidence,
        card=card,
        record=record,
        created=created,
        disk_targets=disk_targets,
        disk_check=disk_check,
    )


def _start_attempt(
    spec: RunSpec, p: _Prepared, record: TrainRecord, n: int
) -> tuple[TrainRecord, Attempt]:
    """spec 6.1 step 5: the first writes -- the config copy, the ``running`` attempt, the run card
    of a new run, the record and the ``started`` event."""
    train_dir(p.data_root, spec.run_id).mkdir(parents=True, exist_ok=True)
    if spec.config is not None:
        copy_rel = f"{TRAIN_DIR}/config.{n}{spec.config.suffix}"
        shutil.copy2(spec.config, p.run_root / copy_rel)
        config_ref = ConfigRef(
            path=store_path(spec.config, p.data_root), sha256=p.chash, copied_to=copy_rel
        )
        record = record.model_copy(update={"config": config_ref})
    attempt = Attempt(
        n=n,
        started_at=stamp(),
        console=f"{TRAIN_DIR}/console.{n}.log",
        command=list(spec.command),
        seed=spec.seed,
        venv=store_path(spec.venv, p.data_root) if spec.venv is not None else None,
    )
    record = record.model_copy(update={"attempts": [*record.attempts, attempt]})
    if p.created:
        save_run(p.data_root, p.card)
    save_record(p.data_root, record)
    # The patterns as given: `train status` scans their directories for leftover `.tmp`
    # files even where no checkpoint was ever registered (spec 2026-10-30 §12). An event's
    # payload is free-form, so an older vcp reads this row unchanged.
    append_event(
        p.data_root,
        spec.run_id,
        "started",
        n,
        command=spec.command,
        cwd=str(p.cwd),
        seed=spec.seed,
        checkpoints=list(spec.checkpoints),
        final=spec.final,
    )
    return record, attempt


def _snapshot_env(
    spec: RunSpec, p: _Prepared, record: TrainRecord, attempt: Attempt, n: int
) -> tuple[TrainRecord, Attempt, EnvSnapshot]:
    """step 6: the environment snapshot, with the tracked diff beside it, named on the attempt."""
    patch_rel = f"{TRAIN_DIR}/git.{n}.patch"
    snap = snapshot(p.python, p.cwd, patch=(p.run_root / patch_rel, patch_rel), disk=p.disk_check)
    env_rel = f"{TRAIN_DIR}/env.{n}.json"
    with (p.run_root / env_rel).open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(snap.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
    attempt = attempt.model_copy(update={"env": env_rel})
    record = _replace_attempt(record, attempt)
    save_record(p.data_root, record)
    append_event(
        p.data_root, spec.run_id, "env", n, python=snap.python, gpus=snap.gpus, torch=snap.torch
    )
    return record, attempt, snap


def _execute_attempt(
    spec: RunSpec, p: _Prepared, attempt: Attempt, n: int
) -> tuple[TrainRecord, Attempt, int]:
    """step 7: the command, then the attempt's end in the record and the event log. Returns the
    exit code too."""
    env = {**p.env, "VCP_ATTEMPT": str(n)}  # VCP-043: the child's own attempt number
    started = utc_now()
    code, status = execute(
        spec.command, cwd=p.cwd, env=env, console=p.run_root / attempt.console, on_line=spec.on_line
    )
    attempt = attempt.model_copy(
        update={
            "finished_at": stamp(),
            "duration_s": round((utc_now() - started).total_seconds(), 3),
            "exit_code": code,
            "status": status,
        }
    )
    # spec 8.2: only Session writes train.yaml while the command runs (registering checkpoints,
    # marking the final one) -- the in-memory snapshot from before execute() missed all of that,
    # so re-read from disk before layering the attempt update on top.
    record = _replace_attempt(load_record(p.data_root, spec.run_id), attempt)
    save_record(p.data_root, record)
    append_event(
        p.data_root,
        spec.run_id,
        "finished",
        n,
        exit_code=code,
        status=status,
        duration_s=attempt.duration_s,
    )
    return record, attempt, code


def _diagnose_disk(
    spec: RunSpec, p: _Prepared, attempt: Attempt, n: int, code: int
) -> DiskFailure | None:
    """spec 2026-10-30 §5.3: a failed attempt only -- measure the same volumes again and read
    the console's tail, so the record can say the disk was the likely cause."""
    if attempt.status != "failed":
        return None
    after = measure(p.disk_targets, min_free_gib=spec.min_free_gib, data_root=p.data_root)
    failure = diagnose(code, p.run_root / attempt.console, after)
    if failure is not None:
        append_event(
            p.data_root,
            spec.run_id,
            "note",
            n,
            key=failure.key,
            marker=failure.marker,
            free_bytes=failure.free_bytes,
        )
    return failure


def _note_changes(
    spec: RunSpec, p: _Prepared, n: int, digests: dict[str, str], git: GitInfo | None
) -> tuple[list[str], list[str]]:
    """What the command changed under its own feet, each a ``note`` event: the evidence files
    that moved, and (VCP-041, spec 2026-09-27 §4.3) HEAD and the tracked diff. Returns the names
    and the paths."""
    changed = moved(p.evidence, digests)
    if changed:
        append_event(
            p.data_root, spec.run_id, "note", n, key="evidence_changed", value=",".join(changed)
        )
    delta = changed_since(p.cwd, git) if git is not None else None
    git_changed = list(delta.changed) if delta is not None else []
    if delta is not None and git_changed:
        append_event(
            p.data_root,
            spec.run_id,
            "note",
            n,
            key="git_changed",
            value=",".join(git_changed),
            commit=delta.commit,
            diff_sha256=delta.diff_sha256,
        )
    return changed, git_changed


def _merge_receipts(p: _Prepared, record: TrainRecord) -> RunCard:
    """spec 7.1: the receipts the child bound to this attempt become the run's access record.
    F5: merged by artifact_id rather than replaced outright -- train.yaml's refs first (in
    their own order), then any ref already on the card (e.g. a manual `ingest --receipt`
    attached between attempts) that train.yaml does not already know about, so --resume can
    no longer make a manually attached receipt vanish from run.yaml."""
    train_ids = {r.artifact_id for r in record.access}
    merged_access = [
        *record.access,
        *(r for r in p.card.access if r.artifact_id not in train_ids),
    ]
    card = p.card.model_copy(
        update={
            "access": merged_access,
            "evidence": merge_refs(record.evidence, p.card.evidence),
        }
    )
    save_run(p.data_root, card)
    return card


def _run_attempt(spec: RunSpec, p: _Prepared) -> _Attempted:
    """Steps 5-9: everything vcp writes around the command, in the order it writes it."""
    # no-op for a new run
    record = _close_running(p.record, data_root=p.data_root, run_id=spec.run_id)
    n = len(record.attempts) + 1
    record, attempt = _start_attempt(spec, p, record, n)
    record, attempt, snap = _snapshot_env(spec, p, record, attempt, n)
    # spec 2026-09-26 §5.2: attached after the first writes, before the child reads anything.
    # (The record it returns is superseded by the re-read after the command.)
    _, digests = attach_cli_evidence(
        p.data_root, p.scope, record, p.evidence, spec.labels, attempt=n
    )
    record, attempt, code = _execute_attempt(spec, p, attempt, n)
    disk_failure = _diagnose_disk(spec, p, attempt, n, code)
    changed, git_changed = _note_changes(spec, p, n, digests, snap.git)
    card = _merge_receipts(p, record)
    # steps 8-9
    record, card, final, registered, warnings = _finish_checkpoints(
        spec, record, card, data_root=p.data_root, cwd=p.cwd, n=n, status=attempt.status
    )
    return _Attempted(
        record=record,
        card=card,
        attempt=attempt,
        n=n,
        final=final,
        registered=registered,
        warnings=warnings,
        git=snap.git,
        git_changed=git_changed,
        changed=changed,
        disk_failure=disk_failure,
    )


def _run_warnings(spec: RunSpec, p: _Prepared, a: _Attempted) -> list[str]:
    """What is worth a WARN about the run itself, before what its receipts say."""
    warnings = list(a.warnings)
    if spec.seed is None:
        warnings.append("seed=none")
    if spec.venv is None:
        warnings.append("venv=inherited")
    if a.changed:
        warnings.append(f"evidence_changed={','.join(a.changed)}")
    git_line = git_warning(a.git) if a.git is not None else None
    if git_line is not None:
        warnings.append(git_line)
    if a.git_changed:
        warnings.append(f"git_changed={','.join(a.git_changed)}")
    if unavailable(p.disk_check):
        warnings.append("disk_check=unavailable")
    return warnings


def _result(spec: RunSpec, p: _Prepared, a: _Attempted) -> RunResult:
    warnings = _run_warnings(spec, p, a)
    info = provenance(a.card, data_root=p.data_root, configs_root=p.configs_root)
    observed_beyond = sorted(set(info.observed) - set(p.trained_on))
    if observed_beyond:
        warnings.append(f"observed_beyond_trained_on={','.join(observed_beyond)}")
    if info.invalid:
        warnings.append(f"receipt_invalid={len(info.invalid)}")
    source_audit_missing = sum(1 for r in a.card.access if r.identity == "full_hash")
    if source_audit_missing:
        warnings.append("source_audit=missing")
    return RunResult(
        record=a.record,
        card=a.card,
        attempt=a.attempt,
        final=a.final,
        registered=a.registered,
        uploaded=a.uploaded,
        verified=a.verified,
        skipped=a.skipped,
        warnings=warnings,
        receipts=len(a.card.access),
        denied=sum(r.denied for r in a.card.access),
        provenance=info.grade,
        observed_beyond=observed_beyond,
        receipt_invalid=len(info.invalid),
        source_audit_missing=source_audit_missing,
        evidence=len(current(a.card.evidence)),
        labels=labels_field(a.card.evidence),
        evidence_changed=a.changed,
        git=a.git,
        git_changed=a.git_changed,
        disk=p.disk_check,
        disk_failure=a.disk_failure,
    )


def train_run(spec: RunSpec) -> RunResult:
    p = _prepare(spec)
    # §5.3: from here on every write is vcp's own record. A full disk under it is an ABORT
    # `disk_full:` (the child is already stopped by execute() when its console write is the one
    # that fails). The upload destination is not vcp's record, so `_upload_all` guards only its own
    # bookkeeping writes, not the `upload(...)` call.
    with own_writes():
        a = _run_attempt(spec, p)
    if a.attempt.status == "finished" and spec.uploads:
        record, uploaded, verified, skipped = _upload_all(
            spec, a.record, data_root=p.data_root, n=a.n
        )
        a = replace(a, record=record, uploaded=uploaded, verified=verified, skipped=skipped)
    return _result(spec, p, a)
