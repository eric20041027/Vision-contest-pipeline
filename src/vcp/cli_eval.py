"""``vcp eval``: measurement-layer commands. Every command ends with a VERDICT line."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Annotated

import typer

from vcp.cli_common import (
    CmdResult,
    ConfigsRootOpt,
    DataRootOpt,
    JsonOpt,
    parse_opts,
    run_command,
)
from vcp.core.errors import VcpError
from vcp.core.log import FieldValue, Status
from vcp.measure.ingest import IngestSpec, ingest

eval_app = typer.Typer(no_args_is_help=True, help="measurement commands")

RunOpt = Annotated[str, typer.Option("--run", help="run id (path-safe name)")]
DatasetOpt = Annotated[str, typer.Option("--dataset", help="dataset name")]
PluginOpt = Annotated[
    list[str] | None,
    typer.Option("--plugin", help="python module to import (registers metrics / converters)"),
]


def load_plugins(modules: list[str] | None) -> list[str]:
    """Import user modules so they can register metrics / converters.

    Returns the names actually imported. Task 12 moves this to ``measure/plugins.py``.
    """
    loaded: list[str] = []
    for name in modules or []:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 - surface the plugin's own error text
            raise VcpError(f"cannot import plugin {name!r}: {type(e).__name__}: {e}") from e
        loaded.append(name)
    return loaded


@eval_app.command("ingest")
def ingest_cmd(
    run: RunOpt,
    dataset: DatasetOpt,
    plan: Annotated[str, typer.Option("--plan", help="plan id")],
    subset: Annotated[str, typer.Option("--subset", help="subset the predictions cover")],
    fmt: Annotated[str, typer.Option("--format", help="registered converter name")],
    src: Annotated[Path, typer.Option("--src", help="prediction file or directory")],
    export_manifest: Annotated[
        Path | None, typer.Option("--export-manifest", help="vcp data export directory")
    ] = None,
    trained_on: Annotated[
        str | None, typer.Option("--trained-on", help="comma-separated subsets the run trained on")
    ] = None,
    framework: Annotated[str, typer.Option("--framework")] = "",
    notes: Annotated[str, typer.Option("--notes")] = "",
    keep_input: Annotated[
        bool, typer.Option("--keep-input", help="copy the source into the run")
    ] = False,
    replace: Annotated[
        bool, typer.Option("--replace", help="overwrite existing predictions")
    ] = False,
    opt: Annotated[
        list[str] | None, typer.Option("--opt", help="converter option key=value")
    ] = None,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Convert framework output into a run's canonical predictions."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        spec = IngestSpec(
            run_id=run,
            dataset=dataset,
            plan_id=plan,
            subset=subset,
            format=fmt,
            src=src,
            export_dir=export_manifest,
            trained_on=[t.strip() for t in (trained_on or "").split(",") if t.strip()],
            framework=framework,
            notes=notes,
            keep_input=keep_input,
            replace=replace,
            options=parse_opts(opt),
            data_root=data_root,
            configs_root=configs_root,
        )
        res = ingest(spec)
        nothing_predicted = res.predicted == 0
        status: Status = "WARN" if res.unknown or nothing_predicted else "OK"
        fields: dict[str, FieldValue] = {
            "run": run,
            "subset": subset,
            "format": fmt,
            "samples": res.samples,
            "predicted": res.predicted,
            "empty": res.empty,
            "sha": res.sha256[:12],
            "replaced": res.replaced,
        }
        if res.unknown:
            fields["unknown"] = len(res.unknown)
        human = [
            f"ingested {res.predicted} predictions for {subset!r} into run {run!r} ({res.path})"
        ]
        payload = {
            "run": res.run.model_dump(mode="json"),
            "path": str(res.path),
            "unknown": res.unknown,
        }
        return status, fields, payload, human

    run_command("eval.ingest", json_mode, data_root, fn)
