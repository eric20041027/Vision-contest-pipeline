"""``vcp`` command line. Every command ends with a VERDICT line and never prompts."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import TypeAdapter, ValidationError

from vcp.cli_artifact import artifact_app
from vcp.cli_backup import backup_app
from vcp.cli_common import (
    CmdResult,
    ConfigsRootOpt,
    DataRootOpt,
    JsonOpt,
    NameOpt,
    parse_opts,
    render_table,
    run_command,
)
from vcp.cli_eval import eval_app
from vcp.cli_fuse import fuse_app
from vcp.cli_submit import submit_app
from vcp.cli_train import train_app
from vcp.core.build import build_info, build_string
from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.hashing import MANIFEST_MODES
from vcp.core.log import FieldValue, Status, Verdict
from vcp.core.paths import DatasetPaths
from vcp.data.audit import AUDITS, AuditContext, AuditOptions, run_audit
from vcp.data.dataset import Dataset
from vcp.data.exporters import ExportSpec, export_subset
from vcp.data.importers import ImportSpec, get_importer
from vcp.data.lineage import clean_eval_subsets
from vcp.data.materialize import MaterializeSpec, materialize
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
app.add_typer(artifact_app, name="artifact")
app.add_typer(backup_app, name="backup")
app.add_typer(eval_app, name="eval")
app.add_typer(fuse_app, name="fuse")
app.add_typer(submit_app, name="submit")
app.add_typer(train_app, name="train")


@app.callback()
def _root() -> None:
    """vcp: vision contest pipeline."""


@app.command("version")
def version_cmd(json_mode: JsonOpt = False, data_root: DataRootOpt = None) -> None:
    """Print the vcp version and, when run from a checkout, the commit it runs from."""

    def fn() -> CmdResult:
        info = build_info()
        build = build_string(info)
        fields: dict[str, FieldValue] = {"version": info.version, "build": build}
        if info.commit is not None:
            fields["commit"] = info.commit
            fields["dirty"] = bool(info.dirty)
        payload = {
            "version": info.version,
            "build": build,
            "commit": info.commit,
            "dirty": info.dirty,
        }
        return "OK", fields, payload, [build]

    run_command("version", json_mode, data_root, fn)


@data_app.command("import")
def import_cmd(
    importer: Annotated[str, typer.Option("--importer", help="registered importer name")],
    src: Annotated[Path, typer.Option("--src", help="source directory")],
    name: NameOpt,
    license_: Annotated[str, typer.Option("--license", help="license of the raw data")],
    url: Annotated[str, typer.Option("--url", help="where the raw data came from")],
    downloaded_at: Annotated[str, typer.Option("--downloaded-at", help="UTC date of download")],
    notes: Annotated[str, typer.Option("--notes")] = "",
    raw_manifest: Annotated[
        str,
        typer.Option(
            "--raw-manifest",
            help="full: md5 of every raw file (default) | sizes: names and sizes only (huge trees)",
        ),
    ] = "full",
    opt: Annotated[
        list[str] | None, typer.Option("--opt", help="importer option key=value (repeatable)")
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Raw data -> canonical dataset (dataset.yaml + samples.jsonl)."""

    def fn() -> CmdResult:
        if raw_manifest not in MANIFEST_MODES:
            raise ValidationFailed(
                f"--raw-manifest must be one of {MANIFEST_MODES}, got {raw_manifest!r}"
            )
        spec = ImportSpec(
            importer=importer,
            src=src,
            name=name,
            options=parse_opts(opt),
            license=license_,
            url=url,
            downloaded_at=downloaded_at,
            notes=notes,
            raw_manifest=raw_manifest,
            data_root=data_root,
            configs_root=configs_root,
        )
        res = get_importer(importer).run(spec)
        status: Status = (
            "WARN"
            if res.rows_skipped or res.plans_invalidated or res.exif_rotated or res.extra_fields
            else "OK"
        )
        fields: dict[str, FieldValue] = {
            "name": name,
            "task": res.dataset.card.task,
            "samples": res.samples_written,
            "rows_read": res.rows_read,
            "rows_skipped": res.rows_skipped,
        }
        if res.skipped_reasons_path is not None:
            fields["skipped_reasons"] = str(res.skipped_reasons_path)
        if res.plans_invalidated:
            fields["plans_invalidated"] = res.plans_invalidated
            if res.old_card_unreadable:
                fields["old_card"] = "unreadable"
        if res.unlabeled:
            fields["unlabeled"] = res.unlabeled
        if res.exif_rotated:
            fields["exif_rotated"] = res.exif_rotated
        for k, v in res.extra_fields.items():
            fields.setdefault(k, v)
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


@data_app.command("export")
def export_cmd(
    name: NameOpt,
    plan: Annotated[str, typer.Option("--plan", help="plan id")],
    subset: Annotated[str, typer.Option("--subset", help="subset name from the plan")],
    fmt: Annotated[str, typer.Option("--format", help="registered exporter: coco | yolo")],
    out: Annotated[Path, typer.Option("--out", help="output directory (must be empty)")],
    opt: Annotated[
        list[str] | None, typer.Option("--opt", help="exporter option key=value (repeatable)")
    ] = None,
    unseal: Annotated[
        bool, typer.Option("--unseal", help="open a sealed subset (recorded)")
    ] = False,
    reason: Annotated[
        str | None, typer.Option("--reason", help="why a sealed subset is opened")
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Export one subset to a training-framework layout, with a hashed manifest."""

    def fn() -> CmdResult:
        spec = ExportSpec(
            name=name,
            plan_id=plan,
            subset=subset,
            format=fmt,
            out=out,
            options=parse_opts(opt),
            unseal=unseal,
            reason=reason,
            data_root=data_root,
            configs_root=configs_root,
        )
        res = export_subset(spec)
        status: Status = "WARN" if res.warnings else "OK"
        fields: dict[str, FieldValue] = {
            "name": name,
            "plan": plan,
            "subset": subset,
            "format": fmt,
            "files": res.files,
            "out": str(res.out),
            "receipt": res.receipt,
        }
        for k, v in res.fields.items():
            fields.setdefault(k, v)
        if res.warnings:
            fields["warnings"] = "; ".join(res.warnings)
        human = [f"exported {res.files} files to {res.out}", *res.warnings]
        payload = {
            "manifest": str(res.manifest_path),
            "warnings": res.warnings,
            "receipt": res.receipt,
        }
        return status, fields, payload, human

    run_command("export", json_mode, data_root, fn)


@data_app.command("audit")
def audit_cmd(
    name: NameOpt,
    against: Annotated[
        str | None, typer.Option("--against", help="dataset to check overlap against (e.g. test)")
    ] = None,
    max_bad_boxes: Annotated[int, typer.Option("--max-bad-boxes")] = 0,
    min_box_px: Annotated[
        float, typer.Option("--min-box-px", help="boxes thinner than this are suspicious")
    ] = 2.0,
    max_aspect: Annotated[
        float, typer.Option("--max-aspect", help="max w/h or h/w before suspicious")
    ] = 20.0,
    max_cover: Annotated[
        float, typer.Option("--max-cover", help="box area / view area that is suspicious")
    ] = 0.98,
    hamming: Annotated[int, typer.Option("--hamming", help="max dHash Hamming distance")] = 4,
    corr: Annotated[float, typer.Option("--corr", help="min 64x64 grey Pearson to confirm")] = 0.95,
    view_hits: Annotated[int, typer.Option("--view-hits", help="views that must match")] = 1,
    recompute: Annotated[bool, typer.Option("--recompute", help="ignore the dHash cache")] = False,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Coordinate sanity, near-duplicate / overlap and provenance checks."""

    def fn() -> CmdResult:
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        ds = Dataset.load(name, data_root=data_root, configs_root=configs_root)
        against_ds = against_paths = None
        if against:
            against_paths = DatasetPaths.resolve(
                against, data_root=data_root, configs_root=configs_root
            )
            against_ds = Dataset.load(against, data_root=data_root, configs_root=configs_root)
        ctx = AuditContext(
            dataset=ds,
            paths=paths,
            opts=AuditOptions(
                max_bad_boxes=max_bad_boxes,
                min_box_px=min_box_px,
                max_aspect=max_aspect,
                max_cover=max_cover,
                hamming=hamming,
                corr=corr,
                view_hits=view_hits,
                recompute=recompute,
            ),
            against=against_ds,
            against_paths=against_paths,
        )
        status, results = run_audit(ctx)
        skipped = [n for n in AUDITS if n not in results]
        human = [
            Verdict(cmd=f"audit.{n}", status=r.status, fields=r.fields).line()
            for n, r in results.items()
        ]
        fields: dict[str, FieldValue] = {"name": name}
        fields.update({n: r.status for n, r in results.items()})
        if skipped:
            fields["skipped"] = ",".join(skipped)
        fields["summary"] = str(ctx.out_dir / "summary.json")
        payload = {
            "summary": str(ctx.out_dir / "summary.json"),
            "checks": {n: {"status": r.status, "fields": r.fields} for n, r in results.items()},
            "skipped": skipped,
        }
        return status, fields, payload, human

    run_command("audit", json_mode, data_root, fn)


@data_app.command("materialize")
def materialize_cmd(
    name: NameOpt,
    mode: Annotated[str, typer.Option("--mode", help="npy | png")],
    resize: Annotated[
        int | None, typer.Option("--resize", help="png only: long side in px")
    ] = None,
    stack_seq: Annotated[
        bool, typer.Option("--stack-seq", help="stack views of a seq into S×H×W")
    ] = False,
    window: Annotated[
        str | None,
        typer.Option("--window", help="png only: dicom | minmax | percentile (default dicom)"),
    ] = None,
    workers: Annotated[int, typer.Option("--workers", help="decode processes")] = 1,
    force: Annotated[bool, typer.Option("--force", help="redo outputs that already exist")] = False,
    decoder: Annotated[
        str | None, typer.Option("--decoder", help="force a registered decoder")
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Decode every view once into cache/materialize/<mode>/ with a portable manifest."""

    def fn() -> CmdResult:
        res = materialize(
            MaterializeSpec(
                name=name,
                mode=mode,
                resize=resize,
                stack_seq=stack_seq,
                window=window,
                workers=workers,
                force=force,
                decoder=decoder,
                data_root=data_root,
                configs_root=configs_root,
            )
        )
        status: Status = "FAIL" if res.failed else ("WARN" if res.warnings else "OK")
        fields: dict[str, FieldValue] = {"name": name, "mode": mode}
        if resize is not None:
            fields["resize"] = resize
        fields.update(
            {
                "materialized": res.materialized,
                "skipped": res.skipped,
                "failed": res.failed,
                "out": str(res.out_dir),
            }
        )
        if res.orphans_removed:
            fields["orphans_removed"] = res.orphans_removed
        human = [
            f"materialized {res.materialized}, skipped {res.skipped}, "
            f"failed {res.failed} -> {res.out_dir}",
            *res.warnings,
        ]
        if res.failed:
            human.append(f"see {res.out_dir / 'failed.jsonl'}")
        payload = {"manifest": str(res.manifest_path), "warnings": res.warnings}
        return status, fields, payload, human

    run_command("materialize", json_mode, data_root, fn)
