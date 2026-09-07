"""``vcp eval``: measurement-layer commands. Every command ends with a VERDICT line."""

from __future__ import annotations

import math
from dataclasses import asdict
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
    parse_opts,
    run_command,
)
from vcp.core.errors import ValidationFailed
from vcp.core.log import FieldValue, Status
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.measure.anchors import anchor_key, set_anchor
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.judge import JudgeSpec, judge_prereg
from vcp.measure.ledger import READINGS_LEDGER, ReadingsLedger
from vcp.measure.measure import MeasureSpec, load_context, measure_run
from vcp.measure.metrics import effective_params, get_metric, params_key
from vcp.measure.plugins import load_plugins
from vcp.measure.prereg import create_prereg, load_prereg
from vcp.measure.report import last_vs_last, report_rows
from vcp.measure.report import status as status_view  # `status` is a local name in 4 commands
from vcp.measure.schema import COMPONENT_CLASSES, Anchor, PreRegistration
from vcp.measure.sigma import SigmaSpec, estimate_sigma_result

eval_app = typer.Typer(no_args_is_help=True, help="measurement commands")

RunOpt = Annotated[str, typer.Option("--run", help="run id (path-safe name)")]
DatasetOpt = Annotated[str, typer.Option("--dataset", help="dataset name")]
PluginOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--plugin", help="python module to import (registers metrics / converters / sigma methods)"
    ),
]


def _read_only_paths(
    dataset: str, data_root: Path | None, configs_root: Path | None
) -> DatasetPaths:
    """Resolve the dataset ``status`` / ``report`` are asked about, refusing one that is not there.

    Both commands are views over ledgers that need not exist yet, and every read of one is
    ``is_file()``-guarded, so without this a typo'd ``--dataset`` answers ``status=OK ...
    preregs=0 judged=0``: a false all-clear from the very orphan detector the user ran the
    command to consult. Same message as ``prereg._dataset_task``, which is how every other
    command in this group says it.
    """
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    if not paths.card_yaml.is_file():
        raise ValidationFailed(f"dataset card not found: {paths.card_yaml}")
    return paths


def _check_tolerance(tolerance: float) -> None:
    """I1: nan/inf silently disables the guardrail (any drift compares `<= tolerance`, which is
    vacuously true for +inf and always False for nan) and a negative tolerance jams it (nothing
    is ever within tolerance). 0.0 -- exact reproduction -- must stay legal."""
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValidationFailed(f"--tolerance must be finite and >= 0, got {tolerance!r}")


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
    weights: Annotated[
        Path | None,
        typer.Option("--weights", help="weights file the predictions came from (sha256 -> run)"),
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", help="model config file (sha256 -> run)")
    ] = None,
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
            trained_on=parse_csv(trained_on),
            framework=framework,
            notes=notes,
            weights=weights,
            config=config,
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
            # The size of the subset, not a count of anything written. `samples` is already
            # spent: run.yaml's PredictionFile.samples is the number of prediction rows
            # WRITTEN, and one name may not mean two quantities across the machine-readable
            # interface (the same rule that made `report`'s field `rows=`).
            "in_subset": res.samples,
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
                metrics=parse_csv(metrics),
                subsets=parse_csv(subsets),
                params=parse_opts(params, "--params"),
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
        _check_tolerance(tolerance)
        card, _dataset, plan, paths = load_context(run, data_root, configs_root)
        pk = params_key(effective_params(get_metric(metric), parse_opts(params, "--params")))
        entry = card.predictions.get(subset)
        if entry is None:
            raise ValidationFailed(f"run {run!r} has no predictions for subset {subset!r}")
        ledger = ReadingsLedger(paths.measure_dir / READINGS_LEDGER)
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
            # 3-5: this command takes its dataset from the run, not from an option, so without
            # this the VERDICT never said whose anchors.json it just rewrote.
            "dataset": card.dataset,
            "key": key,
            "value": reading.value,
            "tolerance": tolerance,
            "reading": reading.reading_id[:12],
        }
        payload = {"key": key, "anchor": anchor.model_dump(mode="json")}
        return "OK", fields, payload, [f"anchor {key} = {reading.value!r}"]

    run_command("eval.anchor", json_mode, data_root, fn)


@eval_app.command("sigma")
def sigma_cmd(
    dataset: DatasetOpt,
    plan: Annotated[str, typer.Option("--plan")],
    metric: Annotated[str, typer.Option("--metric")],
    method: Annotated[str, typer.Option("--method", help="splithalf | bootstrap | prior")],
    params: Annotated[list[str] | None, typer.Option("--params")] = None,
    subsets: Annotated[
        str | None,
        typer.Option("--subsets", help="two eval subsets (splithalf) or one (bootstrap)"),
    ] = None,
    run: Annotated[str | None, typer.Option("--run", help="run to resample (bootstrap)")] = None,
    prior: Annotated[float | None, typer.Option("--prior")] = None,
    note: Annotated[str, typer.Option("--note", help="source of a prior")] = "",
    resamples: Annotated[int, typer.Option("--resamples")] = 200,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Estimate sigma_p and append it to the sigma ledger."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        try:
            spec = SigmaSpec(
                dataset=dataset,
                plan_id=plan,
                metric=metric,
                params=parse_opts(params, "--params"),
                method=method,
                subsets=parse_csv(subsets),
                run_id=run,
                prior=prior,
                note=note,
                resamples=resamples,
                seed=seed,
                data_root=data_root,
                configs_root=configs_root,
            )
        except ValidationError as e:
            # An out-of-range option (--resamples 1) is a FAIL the user can act on, not a
            # pydantic error escaping as an ABORT.
            raise ValidationFailed(str(e), location="vcp eval sigma") from e
        res = estimate_sigma_result(spec)
        est = res.estimate
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "plan": plan,
            "metric": metric,
            "method": method,
            "value": est.value,
            "estimate": est.estimate_id[:12],
            # `existing=`, an int, not `cached=`, a bool: `eval measure` already spends
            # `cached=` on a COUNT of rows already in the ledger, and one name may not mean two
            # shapes across the machine-readable interface (3-5). An estimate is one row, so
            # this is 0 or 1.
            "existing": int(res.cached),
        }
        if "runs" in est.inputs:
            fields["runs"] = len(est.inputs["runs"])
        human = [f"sigma_p ({method}) = {est.value!r}"]
        # A sigma_p of zero passes every `mean_delta >= sigma_ratio * sigma_p` the judge can
        # ask, so it must not go by unremarked: it means this estimator found no spread at all.
        status: Status = "OK"
        if est.value == 0.0:
            status = "WARN"
            human.append(f"sigma_p is 0.0: method {method!r} found no spread to measure here")
        # 3-3: draws the metric refused are skipped, not fatal -- but an estimate made of fewer
        # draws than were asked for is a weaker number, so the count is on the VERDICT line.
        skipped = int(est.inputs.get("skipped") or 0)
        if skipped:
            status = "WARN"
            fields["skipped"] = skipped
            human.append(
                f"{skipped} of {est.inputs.get('resamples')} resamples were refused by "
                f"{metric!r} and skipped; sigma_p is the spread of the remaining "
                f"{est.inputs.get('used')}"
            )
        return status, fields, {"estimate": est.model_dump(mode="json")}, human

    run_command("eval.sigma", json_mode, data_root, fn)


@eval_app.command("preregister")
def preregister_cmd(
    dataset: DatasetOpt,
    prereg_id: Annotated[str, typer.Option("--id", help="pre-registration id (path-safe)")],
    claim: Annotated[str, typer.Option("--claim", help="what is being claimed, in words")],
    component: Annotated[str, typer.Option("--component", help="what changed")],
    component_class: Annotated[str, typer.Option("--class", help="model | tuning")],
    baseline_run: Annotated[str, typer.Option("--baseline-run")],
    candidate_run: Annotated[str, typer.Option("--candidate-run")],
    metric: Annotated[str, typer.Option("--metric")],
    params: Annotated[list[str] | None, typer.Option("--params")] = None,
    subsets: Annotated[str, typer.Option("--subsets", help="comma-separated bases")] = "valA,valB",
    t_min: Annotated[float, typer.Option("--t-min")] = 2.0,
    min_bases: Annotated[int, typer.Option("--min-bases")] = 2,
    sigma_method: Annotated[str, typer.Option("--sigma-method")] = "splithalf",
    sigma_ratio: Annotated[float, typer.Option("--sigma-ratio")] = 1.0,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Write the claim down before measuring the candidate."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        if component_class not in COMPONENT_CLASSES:
            raise ValidationFailed(
                f"--class must be one of {COMPONENT_CLASSES}, got {component_class!r}"
            )
        paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
        try:
            pr = PreRegistration(
                prereg_id=prereg_id,
                claim=claim,
                component=component,
                component_class=component_class,
                baseline_run=baseline_run,
                candidate_run=candidate_run,
                metric=metric,
                params=parse_opts(params, "--params"),
                subsets=parse_csv(subsets),
                t_min=t_min,
                min_bases=min_bases,
                sigma_method=sigma_method,
                sigma_ratio=sigma_ratio,
                created_at=stamp(),
            )
        except ValidationError as e:
            # A nan threshold silently un-binds the bar it names; that is a FAIL the user can
            # act on, not a pydantic error escaping as an ABORT.
            raise ValidationFailed(str(e), location="vcp eval preregister") from e
        path = create_prereg(paths, pr, ReadingsLedger(paths.measure_dir / READINGS_LEDGER))
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "prereg": prereg_id,
            "metric": metric,
            "candidate": candidate_run,
            "baseline": baseline_run,
            "subsets": ",".join(pr.subsets),
            "path": str(path),
        }
        # Read back what was committed: the params on disk are the metric's effective ones.
        stored = load_prereg(paths, prereg_id)
        payload = {"prereg": stored.model_dump(mode="json"), "path": str(path)}
        return "OK", fields, payload, [f"pre-registered {prereg_id} -> {path}"]

    run_command("eval.preregister", json_mode, data_root, fn)


@eval_app.command("judge")
def judge_cmd(
    dataset: DatasetOpt,
    prereg_id: Annotated[str, typer.Option("--prereg", help="pre-registration id")],
    resamples: Annotated[int, typer.Option("--resamples")] = 200,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    strict: Annotated[bool, typer.Option("--strict", help="exit 1 unless PASS")] = False,
    unseal: Annotated[
        bool, typer.Option("--unseal", help="open a sealed subset (recorded)")
    ] = False,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Judge a pre-registered claim from the readings ledger (never max-of-N)."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        try:
            spec = JudgeSpec(
                dataset=dataset,
                prereg_id=prereg_id,
                resamples=resamples,
                seed=seed,
                unseal=unseal,
                reason=reason,
                data_root=data_root,
                configs_root=configs_root,
            )
        except ValidationError as e:
            raise ValidationFailed(str(e), location="vcp eval judge") from e
        j = judge_prereg(spec)
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "prereg": prereg_id,
            "verdict": j.verdict,
            "bases_positive": j.bases_positive,
        }
        if j.sigma_p is not None:
            fields["sigma_p"] = j.sigma_p.value
        human = [
            f"{name:>10}  baseline={s.baseline!r} candidate={s.candidate!r} "
            f"delta={s.delta!r} t={s.t:.2f}"
            for name, s in j.per_subset.items()
        ]
        human += [f"reason: {r}" for r in j.reasons]
        # The verdict is the answer, not a tool failure: only --strict turns it into one.
        status: Status = "FAIL" if strict and j.verdict != "PASS" else "OK"
        return status, fields, {"judgement": j.model_dump(mode="json")}, human

    run_command("eval.judge", json_mode, data_root, fn)


@eval_app.command("status")
def status_cmd(
    dataset: DatasetOpt,
    max_age_hours: Annotated[
        int, typer.Option("--max-age-hours", help="a claim older than this and never judged")
    ] = 48,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Orphan pre-registrations, anchors, latest sigma_p, run count. Reads, never writes."""

    def fn() -> CmdResult:
        paths = _read_only_paths(dataset, data_root, configs_root)
        st = status_view(paths, max_age_hours=max_age_hours)
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "runs": st.runs,
            "preregs": st.preregs,
            "judged": st.judged,
            "anchors": st.anchors,
        }
        if st.unreadable:
            # `runs=` is then a count of what could be read, so say how much was not.
            fields["unreadable"] = len(st.unreadable)
        if st.orphans:
            fields["orphans"] = ",".join(st.orphans)
        for key, value in st.sigma.items():
            fields[f"sigma[{key}]"] = value
        human = [
            f"orphan pre-registration (> {max_age_hours}h without a judgement): {p}"
            for p in st.orphans
        ]
        human += [f"unreadable run card (not counted in runs=): {p}" for p in st.unreadable]
        # An abandoned claim is the one thing here that wants attention; everything else is a
        # count of what exists -- except a run card nobody can read, which makes that count a
        # partial answer rather than the answer.
        status: Status = "WARN" if st.orphans or st.unreadable else "OK"
        return status, fields, {"status": asdict(st)}, human

    run_command("eval.status", json_mode, data_root, fn)


@eval_app.command("report")
def report_cmd(
    dataset: DatasetOpt,
    metric: Annotated[str | None, typer.Option("--metric", help="only this metric")] = None,
    plan: Annotated[str | None, typer.Option("--plan", help="only this plan id")] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Every run x subset reading at full precision, plus last-vs-last deltas per judgement."""

    def fn() -> CmdResult:
        paths = _read_only_paths(dataset, data_root, configs_root)
        rows = report_rows(paths, plan_id=plan, metric=metric)
        # Both halves answer about the same question, so both take the same filters: a report
        # narrowed to one metric that still listed every judgement would be two reports.
        lvl = last_vs_last(paths, plan_id=plan, metric=metric)
        human = [
            f"{r['run_id']:<20} {r['subset']:>8} {r['metric']:<12} {r['value']!r}" for r in rows
        ]
        human += [
            f"{r['prereg_id']:<12} {r['subset']:>8} delta={r['delta']!r} "
            f"t={r['t']:.2f} {r['verdict']}"
            for r in lvl
        ]
        # `rows=` not `readings=`: `eval measure` already spends `readings=` on the number of
        # rows it WROTE, and one name may not mean two quantities across the interface. Same
        # rule for `deltas=`: this counts judgement x subset rows, while `eval status`'s
        # `judged=` counts claims (3-5).
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "rows": len(rows),
            "deltas": len(lvl),
        }
        return "OK", fields, {"readings": rows, "last_vs_last": lvl}, human

    run_command("eval.report", json_mode, data_root, fn)
