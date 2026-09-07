"""``vcp train``: training-layer commands. Every command ends with a VERDICT line."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from vcp.cli_common import (
    CmdResult,
    ConfigsRootOpt,
    DataRootOpt,
    JsonOpt,
    parse_csv,
    run_command,
)
from vcp.core.errors import ValidationFailed
from vcp.core.log import FieldValue, Status
from vcp.core.paths import resolve_data_root
from vcp.train.run import RunSpec, train_run
from vcp.train.status import status as status_view
from vcp.train.status import upload_run

train_app = typer.Typer(no_args_is_help=True, help="training commands")

RunOpt = Annotated[str, typer.Option("--run", help="run id (path-safe name)")]


@train_app.command(
    "run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def run_cmd(
    ctx: typer.Context,
    run: RunOpt,
    dataset: Annotated[str, typer.Option("--dataset", help="dataset name")],
    plan: Annotated[str, typer.Option("--plan", help="plan id")],
    export: Annotated[
        list[Path] | None, typer.Option("--export", help="vcp data export directory (repeatable)")
    ] = None,
    trained_on: Annotated[
        str | None, typer.Option("--trained-on", help="comma-separated subsets (when no --export)")
    ] = None,
    venv: Annotated[
        Path | None, typer.Option("--venv", help="framework virtualenv directory")
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", help="config file to hash and copy")
    ] = None,
    seed: Annotated[int | None, typer.Option("--seed")] = None,
    framework: Annotated[
        str, typer.Option("--framework", help="free text, e.g. 'ultralytics 8.3.0'")
    ] = "",
    cwd: Annotated[
        Path | None, typer.Option("--cwd", help="where the command runs (default: here)")
    ] = None,
    checkpoints: Annotated[
        list[str] | None, typer.Option("--checkpoints", help="glob relative to --cwd (repeatable)")
    ] = None,
    final: Annotated[
        str | None, typer.Option("--final", help="glob of THE checkpoint (exactly one file)")
    ] = None,
    upload: Annotated[
        list[str] | None,
        typer.Option("--upload", help="remote:path (rclone) or a directory (repeatable)"),
    ] = None,
    resume: Annotated[
        bool, typer.Option("--resume", help="add an attempt to an existing training run")
    ] = False,
    notes: Annotated[str, typer.Option("--notes")] = "",
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Run a training command and record it as a run: -- COMMAND... after the options."""

    def fn() -> CmdResult:
        try:
            spec = RunSpec(
                run_id=run,
                dataset=dataset,
                plan_id=plan,
                exports=list(export or []),
                trained_on=parse_csv(trained_on),
                venv=venv,
                config=config,
                seed=seed,
                framework=framework,
                cwd=cwd,
                checkpoints=list(checkpoints or []),
                final=final,
                uploads=list(upload or []),
                resume=resume,
                notes=notes,
                command=list(ctx.args),
                on_line=lambda line: typer.echo(line, nl=False, err=json_mode),
                data_root=data_root,
                configs_root=configs_root,
            )
        except ValidationError as e:
            raise ValidationFailed(str(e), location="vcp train run") from e
        res = train_run(spec)
        fields: dict[str, FieldValue] = {
            "run": run,
            "attempt": res.attempt.n,
            "exit_code": res.attempt.exit_code if res.attempt.exit_code is not None else -1,
            "duration_s": res.attempt.duration_s or 0.0,
            "checkpoints": len(res.record.checkpoints),
            "final": res.final.sha256[:12] if res.final else "none",
            "uploaded": res.uploaded,
            "verified": res.verified,
            "seed": spec.seed if spec.seed is not None else "none",
            "venv": spec.venv.name if spec.venv is not None else "inherited",
        }
        if res.skipped:
            fields["skipped"] = res.skipped
        failed = res.attempt.status != "finished" or res.verified < res.uploaded + res.skipped
        status: Status = "FAIL" if failed else ("WARN" if res.warnings else "OK")
        if res.attempt.status != "finished":
            fields["status_attempt"] = res.attempt.status
        human = [f"attempt {res.attempt.n}: {res.attempt.status} (exit {res.attempt.exit_code})"]
        human += [f"warning: {w}" for w in res.warnings]
        return status, fields, res.record.model_dump(mode="json"), human

    run_command(
        "train.run",
        json_mode,
        data_root,
        fn,
        context={"run": run, "dataset": dataset, "plan": plan},
    )


@train_app.command("upload")
def upload_cmd(
    run: RunOpt,
    dest: Annotated[str, typer.Option("--dest", help="remote:path (rclone) or a directory")],
    only: Annotated[
        str | None, typer.Option("--only", help="'final' to upload only the final checkpoint")
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
) -> None:
    """Upload a run's registered checkpoints and verify them (idempotent)."""

    # 5-8: no configs-root option here -- this command reads a run under the data root and
    # nothing under the configs root; the option was declared and never used.

    def fn() -> CmdResult:
        if only not in (None, "final"):
            raise ValidationFailed(f"--only accepts 'final', got {only!r}")
        root = resolve_data_root(data_root)
        record, out = upload_run(root, run, dest, only_final=only == "final")
        verified = sum(1 for r in out.records if r.verified)
        fields: dict[str, FieldValue] = {
            "run": run,
            "dest": dest,
            "uploaded": out.uploaded,
            "verified": verified,
            "skipped": out.skipped,
        }
        status: Status = "FAIL" if verified < len(out.records) else "OK"
        human = [f"{r.name}: {'verified' if r.verified else 'NOT verified'}" for r in out.records]
        return status, fields, record.model_dump(mode="json"), human

    run_command("train.upload", json_mode, data_root, fn, context={"run": run})


@train_app.command("status")
def status_cmd(
    run: RunOpt,
    verify: Annotated[bool, typer.Option("--verify", help="re-hash every checkpoint")] = False,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
) -> None:
    """Attempts, checkpoints and their backups. Reads, never writes."""

    # 5-8: no configs-root option here -- this command reads a run under the data root and
    # nothing under the configs root; the option was declared and never used.

    def fn() -> CmdResult:
        st = status_view(resolve_data_root(data_root), run, verify=verify)
        fields: dict[str, FieldValue] = {
            "run": run,
            "attempts": len(st.record.attempts),
            "checkpoints": len(st.record.checkpoints),
            "backed": st.backed,
            "unbacked": len(st.unbacked),
            # 5-10: bytes a later registration of the same path replaced and nothing ever backed
            # up. Reported, never WARNed: no copy of them can appear any more.
            "superseded": len(st.superseded),
            "running": st.running,
        }
        if verify:
            fields["drift"] = len(st.drift)
        if st.missing:
            fields["missing"] = len(st.missing)
        # 5-2: the LAST attempt's command. A --resume may have run a different one, and the
        # record-level command is the FIRST attempt's; older records have neither, so fall back.
        last = st.record.attempts[-1] if st.record.attempts else None
        human = []
        if last is not None:
            human.append(f"attempt {last.n} command: {' '.join(last.command or st.record.command)}")
        human += [f"unbacked: {p}" for p in st.unbacked]
        human += [f"superseded: {p}" for p in st.superseded]
        human += [f"missing: {p}" for p in st.missing]
        human += [f"drift: {p}" for p in st.drift]
        warn = bool(st.unbacked or st.missing or st.drift or st.running)
        payload = {
            **st.record.model_dump(mode="json"),
            "drift": st.drift,
            "missing": st.missing,
            "unbacked": st.unbacked,
            "superseded": st.superseded,
        }
        return ("WARN" if warn else "OK"), fields, payload, human

    run_command("train.status", json_mode, data_root, fn, context={"run": run})
