"""``vcp train run``: wrap one training command and make the result a run (spec 6.1).

Every check runs before the first file is written: the dataset and plan agree, the export
manifests say which subsets were trained on, the config has a hash, the venv has a python, the
command can be found, and the run id is free (or resumable). Only then the run card, the
training record, the console log and the environment snapshot appear -- and whatever the
command does next is recorded, exit code and all.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_json
from vcp.core.paths import DatasetPaths, store_path
from vcp.core.time import stamp, utc_now
from vcp.data.dataset import Dataset
from vcp.data.split import SplitPlan, assert_plan_matches, load_plan
from vcp.measure.runs import append_history, assert_run_matches, load_run, run_dir, save_run
from vcp.measure.schema import RunCard, RunSource
from vcp.train.checkpoints import expand, register, resolve_final
from vcp.train.env import snapshot, venv_python
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
    ExportRef,
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
    dataset: Dataset,
    plan: SplitPlan,
    data_root: Path,
) -> tuple[list[str], list[ExportRef]]:
    """spec 6.1 step 2: subsets come from export manifests, which must belong to this dataset,
    version and plan; an explicit --trained-on must agree with them."""
    refs: list[ExportRef] = []
    for export_dir in exports:
        m = read_export(export_dir)
        expected = {
            "dataset": dataset.card.name,
            "samples_hash": dataset.card.samples_hash,
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
    spec: RunSpec, data_root: Path, dataset: Dataset, trained_on: list[str], chash: str
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
    assert_run_matches(card, dataset)
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
    dataset: Dataset,
    trained_on: list[str],
    refs: list[ExportRef],
    chash: str,
    cwd: Path,
) -> tuple[RunCard, TrainRecord]:
    card = RunCard(
        run_id=spec.run_id,
        dataset=dataset.card.name,
        samples_hash=dataset.card.samples_hash,
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
        dataset=dataset.card.name,
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
    patterns = [*spec.checkpoints, *([spec.final] if spec.final else [])]
    record, added = register(record, expand(patterns, cwd), data_root=data_root, attempt=n)
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
        warnings.append("final=skipped (command failed)")
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
        outcome = upload(record, dest, data_root=data_root, runner=spec.rclone_runner)
        record = merge_uploads(record, outcome.records)
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


def train_run(spec: RunSpec) -> RunResult:
    paths = DatasetPaths.resolve(
        spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    data_root, configs_root = paths.data_root, paths.configs_root
    dataset = Dataset.load(spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root)
    plan = load_plan(paths, spec.plan_id)
    assert_plan_matches(plan, dataset.card)
    if not spec.command or spec.command[0].startswith("-"):
        raise ValidationFailed("a training command is required after -- (e.g. -- python train.py)")
    cwd = (spec.cwd or Path.cwd()).resolve()
    trained_on, refs = derive_trained_on(
        spec.exports, spec.trained_on, dataset=dataset, plan=plan, data_root=data_root
    )
    chash = config_hash(spec.config, spec.command)
    python = venv_python(spec.venv) if spec.venv is not None else None
    env = child_env(spec, data_root=data_root, configs_root=configs_root, python=python)
    if not command_found(spec.command[0], cwd, env.get("PATH")):
        raise ValidationFailed(f"command not found: {spec.command[0]!r}")
    card, record = _existing(spec, data_root, dataset, trained_on, chash)
    created = card is None
    if card is None or record is None:
        card, record = _new(
            spec,
            data_root=data_root,
            dataset=dataset,
            trained_on=trained_on,
            refs=refs,
            chash=chash,
            cwd=cwd,
        )
    record = _close_running(record, data_root=data_root, run_id=spec.run_id)  # no-op for a new run
    n = len(record.attempts) + 1
    # spec 6.1 step 5: the first writes.
    run_root = run_dir(data_root, spec.run_id)
    train_dir(data_root, spec.run_id).mkdir(parents=True, exist_ok=True)
    if spec.config is not None:
        copy_rel = f"{TRAIN_DIR}/config.{n}{spec.config.suffix}"
        shutil.copy2(spec.config, run_root / copy_rel)
        record = record.model_copy(
            update={
                "config": ConfigRef(
                    path=store_path(spec.config, data_root), sha256=chash, copied_to=copy_rel
                )
            }
        )
    attempt = Attempt(
        n=n,
        started_at=stamp(),
        console=f"{TRAIN_DIR}/console.{n}.log",
        command=list(spec.command),
        seed=spec.seed,
        venv=store_path(spec.venv, data_root) if spec.venv is not None else None,
    )
    record = record.model_copy(update={"attempts": [*record.attempts, attempt]})
    if created:
        save_run(data_root, card)
    save_record(data_root, record)
    append_event(
        data_root, spec.run_id, "started", n, command=spec.command, cwd=str(cwd), seed=spec.seed
    )
    # step 6: environment snapshot
    snap = snapshot(python, cwd)
    env_rel = f"{TRAIN_DIR}/env.{n}.json"
    with (run_root / env_rel).open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(snap.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
    attempt = attempt.model_copy(update={"env": env_rel})
    record = _replace_attempt(record, attempt)
    save_record(data_root, record)
    append_event(
        data_root, spec.run_id, "env", n, python=snap.python, gpus=snap.gpus, torch=snap.torch
    )
    # step 7: the command
    started = utc_now()
    code, status = execute(
        spec.command, cwd=cwd, env=env, console=run_root / attempt.console, on_line=spec.on_line
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
    record = _replace_attempt(load_record(data_root, spec.run_id), attempt)
    save_record(data_root, record)
    append_event(
        data_root,
        spec.run_id,
        "finished",
        n,
        exit_code=code,
        status=status,
        duration_s=attempt.duration_s,
    )
    # steps 8-9
    record, card, final, registered, warnings = _finish_checkpoints(
        spec, record, card, data_root=data_root, cwd=cwd, n=n, status=status
    )
    uploaded = verified = skipped = 0
    if status == "finished" and spec.uploads:
        record, uploaded, verified, skipped = _upload_all(spec, record, data_root=data_root, n=n)
    if spec.seed is None:
        warnings.append("seed=none")
    if spec.venv is None:
        warnings.append("venv=inherited")
    return RunResult(
        record=record,
        card=card,
        attempt=attempt,
        final=final,
        registered=registered,
        uploaded=uploaded,
        verified=verified,
        skipped=skipped,
        warnings=warnings,
    )
