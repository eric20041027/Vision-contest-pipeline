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
from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.log import FieldValue, Status
from vcp.core.time import stamp
from vcp.measure.anchors import anchor_key, set_anchor
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.measure import MeasureSpec, load_context, measure_run
from vcp.measure.metrics import effective_params, get_metric, params_key
from vcp.measure.schema import Anchor

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


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


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
        list[str] | None,
        typer.Option("--opt", help="converter or ingest option key=value, e.g. allow_unknown=true"),
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
            trained_on=_csv(trained_on),
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


@eval_app.command("measure")
def measure_cmd(
    run: RunOpt,
    metrics: Annotated[
        str | None, typer.Option("--metrics", help="comma-separated; default: all applicable")
    ] = None,
    subsets: Annotated[
        str | None, typer.Option("--subsets", help="comma-separated; default: clean eval subsets")
    ] = None,
    params: Annotated[
        list[str] | None, typer.Option("--params", help="metric param key=value (repeatable)")
    ] = None,
    unseal: Annotated[
        bool, typer.Option("--unseal", help="open a sealed subset (recorded)")
    ] = False,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Guardrail, then one reading per clean eval subset and applicable metric."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        res = measure_run(
            MeasureSpec(
                run_id=run,
                metrics=_csv(metrics),
                subsets=_csv(subsets),
                params=parse_opts(params),
                unseal=unseal,
                reason=reason,
                data_root=data_root,
                configs_root=configs_root,
            )
        )
        # Every cell without an anchor adds a warning, so `warnings` already covers
        # guardrail in ("none", "partial"); a fully anchored measure warns about nothing.
        status: Status = "WARN" if res.warnings else "OK"
        fields: dict[str, FieldValue] = {
            "run": run,
            "dataset": res.dataset,
            "subsets": ",".join(sorted({r.subset for r in res.readings})),
            "metrics": ",".join(sorted({r.metric for r in res.readings})),
            "readings": res.new,
            "cached": res.cached,
            "guardrail": res.guardrail,
        }
        human = [
            f"{r.subset:>10}  {r.metric:<12} {r.value!r}  n={r.n_samples}" for r in res.readings
        ]
        human += res.warnings
        payload = {
            "readings": [r.model_dump(mode="json") for r in res.readings],
            "warnings": res.warnings,
        }
        return status, fields, payload, human

    run_command("eval.measure", json_mode, data_root, fn)


@eval_app.command("anchor")
def anchor_cmd(
    run: RunOpt,
    subset: Annotated[str, typer.Option("--subset")],
    metric: Annotated[str, typer.Option("--metric")],
    params: Annotated[list[str] | None, typer.Option("--params")] = None,
    tolerance: Annotated[float, typer.Option("--tolerance")] = 1e-6,
    replace: Annotated[bool, typer.Option("--replace")] = False,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Make an existing reading the guardrail for its plan/subset/metric."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        card, _dataset, plan, paths = load_context(run, data_root, configs_root)
        pk = params_key(effective_params(get_metric(metric), parse_opts(params)))
        entry = card.predictions.get(subset)
        if entry is None:
            raise ValidationFailed(f"run {run!r} has no predictions for subset {subset!r}")
        ledger = ReadingsLedger(paths.measure_dir / "readings.jsonl")
        match = [
            r
            for r in ledger.rows
            if r.run_id == run
            and r.subset == subset
            and r.metric == metric
            and params_key(r.params) == pk
            and r.prediction_sha == entry.sha256
        ]
        if not match:
            raise ValidationFailed(
                f"no reading for run {run!r} {subset}/{metric}/{pk}; run `vcp eval measure` first"
            )
        reading = match[-1]
        key = anchor_key(plan.plan_id, subset, metric, pk)
        anchor = Anchor(
            run_id=run,
            reading_id=reading.reading_id,
            value=reading.value,
            tolerance=tolerance,
            set_at=stamp(),
        )
        set_anchor(paths, key, anchor, replace=replace)
        fields: dict[str, FieldValue] = {
            "key": key,
            "value": reading.value,
            "tolerance": tolerance,
            "reading": reading.reading_id[:12],
        }
        payload = {"key": key, "anchor": anchor.model_dump(mode="json")}
        return "OK", fields, payload, [f"anchor {key} = {reading.value!r}"]

    run_command("eval.anchor", json_mode, data_root, fn)
