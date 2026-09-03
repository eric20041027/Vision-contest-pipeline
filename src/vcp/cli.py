"""``vcp`` command line. Every command ends with a VERDICT line and never prompts."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import TypeAdapter, ValidationError

from vcp import __version__
from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.log import FieldValue, Status, Verdict, exit_code, setup_logging
from vcp.core.paths import DatasetPaths, logs_dir, resolve_data_root
from vcp.data.dataset import Dataset
from vcp.data.importers import ImportSpec, get_importer
from vcp.data.lineage import clean_eval_subsets
from vcp.data.split import (
    DEFAULT_SUBSETS,
    build_plan,
    distribution_table,
    load_plan,
    parse_subsets,
    save_plan,
)

app = typer.Typer(no_args_is_help=True, add_completion=False, help="vision contest pipeline")
data_app = typer.Typer(no_args_is_help=True, help="dataset commands")
app.add_typer(data_app, name="data")

CmdResult = tuple[Status, dict[str, FieldValue], Any, list[str]]

JsonOpt = Annotated[bool, typer.Option("--json", help="JSON result to stdout, VERDICT to stderr")]
DataRootOpt = Annotated[Path | None, typer.Option("--data-root", help="override VCP_DATA_ROOT")]
ConfigsRootOpt = Annotated[
    Path | None, typer.Option("--configs-root", help="override VCP_CONFIGS_ROOT")
]
NameOpt = Annotated[str, typer.Option("--name", help="dataset name")]


@app.callback()
def _root() -> None:
    """vcp: vision contest pipeline."""


@app.command("version")
def version_cmd(json_mode: JsonOpt = False, data_root: DataRootOpt = None) -> None:
    """Print the vcp version."""

    def fn() -> CmdResult:
        return "OK", {"version": __version__}, {"version": __version__}, [__version__]

    run_command("version", json_mode, data_root, fn)


def parse_opts(opts: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in opts or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValidationFailed(f"--opt expects key=value, got {item!r}")
        out[key] = value
    return out


def render_table(table: dict[str, dict[str, int]], counts: dict[str, int]) -> str:
    subsets = list(table)
    labels = sorted({label for row in table.values() for label in row})
    header = ["label", *subsets]
    rows = [["(total)", *[str(counts.get(s, 0)) for s in subsets]]]
    rows += [[label, *[str(table[s].get(label, 0)) for s in subsets]] for label in labels]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]

    def fmt(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    return "\n".join([fmt(header), *(fmt(r) for r in rows)])


def _logger(data_root: Path | None) -> logging.Logger:
    try:
        return setup_logging(logs_dir(resolve_data_root(data_root)))
    except Exception:
        return logging.getLogger("vcp")


def run_command(
    cmd: str, json_mode: bool, data_root: Path | None, fn: Callable[[], CmdResult]
) -> None:
    logger = _logger(data_root)
    payload: Any = None
    human: list[str] = []
    try:
        status, fields, payload, human = fn()
    except VcpError as e:
        status, fields = e.status, {"reason": f"{type(e).__name__}: {e}"}  # type: ignore[assignment]
        logger.error("command failed", exc_info=True, extra={"vcp": {"cmd": cmd}})
    except Exception as e:
        status, fields = "ABORT", {"reason": f"{type(e).__name__}: {e}"}
        logger.error("command aborted", exc_info=True, extra={"vcp": {"cmd": cmd}})
    verdict = Verdict(cmd=cmd, status=status, fields=fields)
    logger.info(verdict.line(), extra={"vcp": {"cmd": cmd, "status": status}})
    if json_mode:
        doc = {"cmd": cmd, "status": status, "fields": fields, "result": payload}
        typer.echo(json.dumps(doc, ensure_ascii=False, default=str))
        typer.echo(verdict.line(), err=True)
    else:
        for line in human:
            typer.echo(line)
        typer.echo(verdict.line())
    raise typer.Exit(code=exit_code(status))


@data_app.command("import")
def import_cmd(
    importer: Annotated[str, typer.Option("--importer", help="registered importer name")],
    src: Annotated[Path, typer.Option("--src", help="source directory")],
    name: NameOpt,
    license_: Annotated[str, typer.Option("--license", help="license of the raw data")],
    url: Annotated[str, typer.Option("--url", help="where the raw data came from")],
    downloaded_at: Annotated[str, typer.Option("--downloaded-at", help="UTC date of download")],
    notes: Annotated[str, typer.Option("--notes")] = "",
    opt: Annotated[
        list[str] | None, typer.Option("--opt", help="importer option key=value (repeatable)")
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Raw data -> canonical dataset (dataset.yaml + samples.jsonl)."""

    def fn() -> CmdResult:
        spec = ImportSpec(
            importer=importer,
            src=src,
            name=name,
            options=parse_opts(opt),
            license=license_,
            url=url,
            downloaded_at=downloaded_at,
            notes=notes,
            data_root=data_root,
            configs_root=configs_root,
        )
        res = get_importer(importer).run(spec)
        status: Status = "WARN" if res.rows_skipped else "OK"
        fields: dict[str, FieldValue] = {
            "name": name,
            "task": res.dataset.card.task,
            "samples": res.samples_written,
            "rows_read": res.rows_read,
            "rows_skipped": res.rows_skipped,
        }
        if res.skipped_reasons_path is not None:
            fields["skipped_reasons"] = str(res.skipped_reasons_path)
        human = [
            f"imported {res.samples_written} samples into dataset {name!r} "
            f"(task={res.dataset.card.task})"
        ]
        return status, fields, {"card": res.dataset.card.model_dump(mode="json")}, human

    run_command("import", json_mode, data_root, fn)


@data_app.command("validate")
def validate_cmd(
    name: NameOpt,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Re-validate card + samples and verify samples_hash."""

    def fn() -> CmdResult:
        ds = Dataset.load(name, data_root=data_root, configs_root=configs_root)
        short = ds.card.samples_hash[:12]
        fields: dict[str, FieldValue] = {
            "name": name,
            "task": ds.card.task,
            "samples": len(ds.samples),
            "samples_hash": short,
        }
        human = [f"dataset {name!r}: {len(ds.samples)} samples, task={ds.card.task}, hash={short}"]
        return "OK", fields, {"card": ds.card.model_dump(mode="json")}, human

    run_command("validate", json_mode, data_root, fn)


@data_app.command("split")
def split_cmd(
    name: NameOpt,
    plan_id: Annotated[str, typer.Option("--plan-id", help="new plan id (immutable once written)")],
    seed: Annotated[int, typer.Option("--seed")] = 42,
    subsets: Annotated[
        str, typer.Option("--subsets", help="name:role:ratio,... roles: train|eval|sealed")
    ] = DEFAULT_SUBSETS,
    stratify_key: Annotated[
        str, typer.Option("--stratify-key", help="auto | none | meta.<field>")
    ] = "auto",
    group_key: Annotated[str, typer.Option("--group-key", help="auto | meta.<field>")] = "auto",
    group_from_audit: Annotated[
        bool, typer.Option("--group-from-audit", help="also use cache/audit/groups.json")
    ] = False,
    no_eval_gold_only: Annotated[
        bool, typer.Option("--no-eval-gold-only", help="allow non-gold samples in eval/sealed")
    ] = False,
    strategy: Annotated[
        str, typer.Option("--strategy", help="split strategy from the registry")
    ] = "fixed",
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Generate a fixed multi-subset split plan and commit-ready plan file."""

    def fn() -> CmdResult:
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        target = paths.plan_json(plan_id)
        if target.exists():
            raise VcpError(
                f"plan file already exists: {target}; plans are immutable, choose a new plan-id"
            )
        ds = Dataset.load(name, data_root=data_root, configs_root=configs_root)
        audit_groups: dict[str, str] | None = None
        if group_from_audit:
            groups_file = paths.cache_dir / "audit" / "groups.json"
            if not groups_file.is_file():
                raise VcpError(
                    f"--group-from-audit needs {groups_file}; run `vcp data audit` first"
                )
            try:
                audit_groups = TypeAdapter(dict[str, str]).validate_json(
                    groups_file.read_text(encoding="utf-8")
                )
            except ValidationError as e:
                raise ValidationFailed(
                    f"bad audit groups file: {e}", location=str(groups_file)
                ) from e
        plan = build_plan(
            ds,
            plan_id=plan_id,
            subsets=parse_subsets(subsets),
            seed=seed,
            stratify_key=stratify_key,
            group_key=group_key,
            eval_gold_only=not no_eval_gold_only,
            audit_groups=audit_groups,
            strategy=strategy,
        )
        table = distribution_table(plan, ds)
        counts = {sub.name: len(plan.ids_in(sub.name)) for sub in plan.subsets}
        empty = list(plan.params.get("empty_subsets", []))
        status: Status = "WARN" if plan.params.get("audit_group_conflicts") or empty else "OK"
        fields: dict[str, FieldValue] = {"plan": plan_id, **counts, "seed": seed}
        if empty:
            fields["empty_subsets"] = ",".join(empty)
        human = [f"plan {plan_id!r} written to {target}", render_table(table, counts)]
        payload = {
            "plan_path": str(target),
            "counts": counts,
            "distribution": table,
            "params": plan.params,
        }
        save_plan(plan, paths)
        return status, fields, payload, human

    run_command("split", json_mode, data_root, fn)


@data_app.command("lineage")
def lineage_cmd(
    name: NameOpt,
    plan: Annotated[str, typer.Option("--plan", help="plan id")],
    trained_on: Annotated[
        str, typer.Option("--trained-on", help="comma-separated subset names a run trained on")
    ],
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Which subsets are clean evaluation bases for a run trained on the given subsets."""

    def fn() -> CmdResult:
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        split_plan = load_plan(paths, plan)
        trained = {t.strip() for t in trained_on.split(",") if t.strip()}
        clean = clean_eval_subsets(split_plan, trained)
        fields: dict[str, FieldValue] = {
            "plan": plan,
            "trained_on": ",".join(sorted(trained)),
            "clean": ",".join(clean) or "-",
        }
        human = [f"clean=[{', '.join(clean)}]"]
        return "OK", fields, {"clean": clean, "trained_on": sorted(trained)}, human

    run_command("lineage", json_mode, data_root, fn)
