"""``vcp submit``: submission governance commands. Every command ends with a VERDICT line."""

from __future__ import annotations

from typing import Annotated

import typer
from pydantic import ValidationError

from vcp.cli_common import (
    CmdResult,
    ConfigsRootOpt,
    DataRootOpt,
    JsonOpt,
    parse_opts,
    run_command,
)
from vcp.core.errors import ValidationFailed
from vcp.core.log import FieldValue, Status
from vcp.core.time import stamp
from vcp.measure.metrics import effective_params, get_metric
from vcp.measure.plugins import load_plugins
from vcp.submit.actions import record, score, upload
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile, Quota
from vcp.submit.stage import StageSpec, stage, verify
from vcp.submit.sync import sync

submit_app = typer.Typer(no_args_is_help=True, help="submission governance commands")

DatasetOpt = Annotated[str, typer.Option("--dataset", help="test dataset name")]
IdOpt = Annotated[str, typer.Option("--id", help="submission id (path-safe, under 32 chars)")]
PluginOpt = Annotated[
    list[str] | None, typer.Option("--plugin", help="python module to import (registers writers)")
]


@submit_app.command("init")
def init_cmd(
    dataset: DatasetOpt,
    eval_dataset: Annotated[str, typer.Option("--eval-dataset", help="eval-side dataset")],
    plan: Annotated[str, typer.Option("--plan", help="eval-side plan id (has the sealed subset)")],
    sealed: Annotated[str, typer.Option("--sealed", help="sealed subset `final` ranks on")],
    platform: Annotated[str, typer.Option("--platform", help="manual | kaggle")],
    metric: Annotated[str, typer.Option("--metric", help="metric of the sealed reading")],
    competition: Annotated[str | None, typer.Option("--competition")] = None,
    kind: Annotated[str, typer.Option("--kind", help="file | kernel")] = "file",
    board_rule: Annotated[
        str | None, typer.Option("--board-rule", help="last | best (default by platform)")
    ] = None,
    slots: Annotated[int, typer.Option("--slots", help="final picks (Kaggle allows 2)")] = 1,
    quota: Annotated[int | None, typer.Option("--quota", help="uploads per platform day")] = None,
    day_tz: Annotated[str, typer.Option("--day-tz", help="zone of the platform day")] = "UTC",
    day_start: Annotated[str, typer.Option("--day-start", help="HH:MM wall time")] = "00:00",
    display_tz: Annotated[
        str | None, typer.Option("--display-tz", help="zone `record --at` is read in")
    ] = None,
    deadline: Annotated[
        str | None, typer.Option("--deadline", help="UTC stamp, e.g. 2026-10-22T23:59:00Z")
    ] = None,
    params: Annotated[
        list[str] | None, typer.Option("--params", help="metric param key=value (repeatable)")
    ] = None,
    writer: Annotated[str | None, typer.Option("--writer", help="registered writer")] = None,
    writer_opt: Annotated[
        list[str] | None, typer.Option("--writer-opt", help="writer option key=value")
    ] = None,
    kaggle_command: Annotated[
        str, typer.Option("--kaggle-command", help="how to run the kaggle CLI")
    ] = "kaggle",
    test_plan: Annotated[str, typer.Option("--test-plan")] = "all-v1",
    test_subset: Annotated[str, typer.Option("--test-subset")] = "test",
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Write submit.yaml and the test dataset's single-subset plan."""

    def fn() -> CmdResult:
        metric_params = effective_params(get_metric(metric), parse_opts(params, "--params"))
        rule = board_rule or ("best" if platform == "kaggle" else "last")
        try:
            profile = PlatformProfile(
                dataset=dataset,
                eval_dataset=eval_dataset,
                plan_id=plan,
                sealed_subset=sealed,
                test_plan=test_plan,
                test_subset=test_subset,
                platform=platform,  # type: ignore[arg-type]
                competition=competition,
                submission_kind=kind,  # type: ignore[arg-type]
                board_rule=rule,  # type: ignore[arg-type]
                final_slots=slots,
                quota=None
                if quota is None
                else Quota(per_day=quota, day_tz=day_tz, day_start=day_start),
                display_tz=display_tz,
                deadline=deadline,
                metric=metric,
                metric_params=metric_params,
                writer=writer,
                writer_opts=parse_opts(writer_opt, "--writer-opt"),
                kaggle_command=kaggle_command.split(),
                created_at=stamp(),
            )
        except ValidationError as e:
            raise ValidationFailed(str(e), location="vcp submit init") from e
        res = init_profile(profile, data_root=data_root, configs_root=configs_root)
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "platform": platform,
            "profile": str(res.path),
            "plan": test_plan,
            "plan_created": res.plan_created,
        }
        payload = {"profile": profile.model_dump(mode="json"), "path": str(res.path)}
        return "OK", fields, payload, [f"profile written to {res.path}"]

    run_command("submit.init", json_mode, data_root, fn)


@submit_app.command("stage")
def stage_cmd(
    dataset: DatasetOpt,
    submission_id: IdOpt,
    eval_run: Annotated[str, typer.Option("--eval-run", help="eval-side run (judged)")],
    test_run: Annotated[
        str | None, typer.Option("--test-run", help="test-side run the file is rendered from")
    ] = None,
    kind: Annotated[str, typer.Option("--kind", help="candidate | baseline | probe")] = "candidate",
    reason: Annotated[
        str | None, typer.Option("--reason", help="required for baseline / probe")
    ] = None,
    kernel: Annotated[
        str | None, typer.Option("--kernel", help="kernel submissions: user/notebook")
    ] = None,
    version: Annotated[int | None, typer.Option("--version", help="kernel version")] = None,
    output: Annotated[
        str, typer.Option("--output", help="kernel output file name")
    ] = "submission.csv",
    weights: Annotated[
        list[str] | None,
        typer.Option("--weights", help="RUN[:sha] the kernel loads (repeatable)"),
    ] = None,
    writer_opt: Annotated[
        list[str] | None, typer.Option("--writer-opt", help="writer option key=value")
    ] = None,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Pair, gate, render and record a candidate -- every check before any write."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        try:
            spec = StageSpec(
                dataset=dataset,
                submission_id=submission_id,
                eval_run=eval_run,
                test_run=test_run,
                kind=kind,  # type: ignore[arg-type]
                reason=reason,
                kernel=kernel,
                version=version,
                output=output,
                weights=list(weights or []),
                writer_opts=parse_opts(writer_opt, "--writer-opt"),
                data_root=data_root,
                configs_root=configs_root,
            )
        except ValidationError as e:
            raise ValidationFailed(str(e), location="vcp submit stage") from e
        res = stage(spec)
        st = res.staged
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "id": submission_id,
            "kind": st.kind,
            "eval_run": st.eval_run,
            "pairing": st.pairing.mode,
            "admission": st.gate.admission,
        }
        if st.test_run:
            fields["test_run"] = st.test_run
        if st.artifact.sha256:
            fields["sha256"] = st.artifact.sha256[:12]
        if st.artifact.writer:
            fields["writer"] = st.artifact.writer
            fields["rows"] = st.artifact.rows or 0
        human = [f"staged {submission_id} -> {res.path}"]
        human += [f"check: {c}" for c in st.pairing.checks]
        human += [f"warning: {w}" for w in res.warnings]
        status: Status = "WARN" if res.warnings else "OK"
        return status, fields, st.model_dump(mode="json"), human

    run_command("submit.stage", json_mode, data_root, fn)


@submit_app.command("verify")
def verify_cmd(
    dataset: DatasetOpt,
    submission_id: IdOpt,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Re-hash the artifact and re-render it from the test-side run (bit-level reproduction)."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        checks = verify(dataset, submission_id, data_root=data_root, configs_root=configs_root)
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "id": submission_id,
            "checks": len(checks),
        }
        return "OK", fields, {"checks": checks}, [f"ok: {c}" for c in checks]

    run_command("submit.verify", json_mode, data_root, fn)


@submit_app.command("upload")
def upload_cmd(
    dataset: DatasetOpt,
    submission_id: IdOpt,
    message: Annotated[str | None, typer.Option("--message", help="appended to the id")] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Upload a staged submission through the platform's CLI and record it."""

    def fn() -> CmdResult:
        out = upload(
            dataset, submission_id, message=message, data_root=data_root, configs_root=configs_root
        )
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "id": submission_id,
            "at": str(out.row.at),
            "confirmed": bool(out.row.confirmed),
        }
        if out.quota is not None:
            fields.update(out.quota.fields())
        human = [out.result.detail] if out.result.detail else []
        status: Status = "OK" if out.row.confirmed else "WARN"
        return status, fields, out.row.model_dump(mode="json", exclude_none=True), human

    run_command("submit.upload", json_mode, data_root, fn)


@submit_app.command("record")
def record_cmd(
    dataset: DatasetOpt,
    submission_id: IdOpt,
    at: Annotated[str, typer.Option("--at", help="platform time 'YYYY-MM-DD HH:MM[:SS]'")],
    tz: Annotated[str, typer.Option("--tz", help="platform | utc")] = "platform",
    platform_ref: Annotated[str | None, typer.Option("--platform-ref")] = None,
    message: Annotated[str | None, typer.Option("--message")] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Record an upload you made by hand (the ledger row is the upload's receipt)."""

    def fn() -> CmdResult:
        out = record(
            dataset,
            submission_id,
            at,
            tz=tz,
            platform_ref=platform_ref,
            message=message,
            data_root=data_root,
            configs_root=configs_root,
        )
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "id": submission_id,
            "at": str(out.row.at),
        }
        if out.quota is not None:
            fields.update(out.quota.fields())
        if out.warnings:
            fields["quota_overflow"] = True
        status: Status = "WARN" if out.warnings else "OK"
        human = [f"warning: {w}" for w in out.warnings]
        return status, fields, out.row.model_dump(mode="json", exclude_none=True), human

    run_command("submit.record", json_mode, data_root, fn)


@submit_app.command("score")
def score_cmd(
    dataset: DatasetOpt,
    submission_id: IdOpt,
    public: Annotated[float | None, typer.Option("--public")] = None,
    private: Annotated[float | None, typer.Option("--private")] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Record a public / private score the platform showed."""

    def fn() -> CmdResult:
        row = score(
            dataset,
            submission_id,
            public=public,
            private=private,
            data_root=data_root,
            configs_root=configs_root,
        )
        fields: dict[str, FieldValue] = {"dataset": dataset, "id": submission_id}
        if row.public is not None:
            fields["public"] = row.public
        if row.private is not None:
            fields["private"] = row.private
        return "OK", fields, row.model_dump(mode="json", exclude_none=True), []

    run_command("submit.score", json_mode, data_root, fn)


@submit_app.command("sync")
def sync_cmd(
    dataset: DatasetOpt,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Read the platform's submission list back into the ledger (scores, foreign uploads)."""

    def fn() -> CmdResult:
        res = sync(dataset, data_root=data_root, configs_root=configs_root)
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "platform_rows": res.platform_rows,
            "scored": res.scored,
            "foreign": res.foreign,
            "unconfirmed": len(res.unconfirmed),
        }
        human = [f"unconfirmed: {sid}" for sid in res.unconfirmed]
        status: Status = "WARN" if res.foreign or res.unconfirmed else "OK"
        payload = {"matched": res.matched, "unconfirmed": res.unconfirmed}
        return status, fields, payload, human

    run_command("submit.sync", json_mode, data_root, fn)
