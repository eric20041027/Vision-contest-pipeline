"""``vcp fuse``: fusion-layer commands. Every command ends with a VERDICT line."""

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
from vcp.core.log import FieldValue
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.split import load_plan
from vcp.fuse.build import BuildSpec, build_run
from vcp.fuse.fusers import get_fuser, require_payload, resolve_params
from vcp.fuse.members import check_members, check_plan, parse_member
from vcp.fuse.recipes import save_recipe
from vcp.fuse.schema import Recipe
from vcp.measure.plugins import load_plugins

fuse_app = typer.Typer(no_args_is_help=True, help="fusion commands")

DatasetOpt = Annotated[str, typer.Option("--dataset", help="dataset name")]
RecipeOpt = Annotated[str, typer.Option("--recipe", help="recipe id")]
PluginOpt = Annotated[
    list[str] | None,
    typer.Option("--plugin", help="python module to import (registers fusers / metrics)"),
]


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@fuse_app.command("recipe")
def recipe_cmd(
    dataset: DatasetOpt,
    recipe_id: Annotated[str, typer.Option("--id", help="recipe id (path-safe)")],
    plan: Annotated[str, typer.Option("--plan", help="plan every member was ingested under")],
    method: Annotated[str, typer.Option("--method", help="registered fuser name")],
    member: Annotated[
        list[str], typer.Option("--member", help="RUN[:WEIGHT], repeatable; order is kept")
    ],
    params: Annotated[
        list[str] | None, typer.Option("--params", help="fuser param key=value (repeatable)")
    ] = None,
    notes: Annotated[str, typer.Option("--notes")] = "",
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Write a fusion recipe (git) after checking every member against the dataset and plan."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
        ds = Dataset.load(dataset, data_root=data_root, configs_root=configs_root)
        check_plan(load_plan(paths, plan), ds)
        fuser = get_fuser(method)
        require_payload(fuser, ds.card.task)
        effective = resolve_params(fuser, parse_opts(params, "--params"))
        members = [parse_member(item) for item in member]
        try:
            recipe = Recipe(
                recipe_id=recipe_id,
                dataset=dataset,
                plan_id=plan,
                method=method,
                params=effective,
                members=members,
                notes=notes,
                created_at=stamp(),
            )
        except ValidationError as e:
            raise ValidationFailed(str(e), location="vcp fuse recipe") from e
        check_members(recipe.members, data_root=paths.data_root, dataset=ds, plan_id=plan)
        path = save_recipe(paths, recipe)
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "recipe": recipe_id,
            "method": method,
            "members": len(members),
            "path": str(path),
        }
        payload = {"recipe": recipe.model_dump(mode="json"), "path": str(path)}
        return "OK", fields, payload, [f"wrote recipe {recipe_id} -> {path}"]

    run_command("fuse.recipe", json_mode, data_root, fn)


@fuse_app.command("build")
def build_cmd(
    dataset: DatasetOpt,
    recipe_id: RecipeOpt,
    run: Annotated[str | None, typer.Option("--run", help="run id; default fuse-<recipe>")] = None,
    subsets: Annotated[
        str | None,
        typer.Option("--subsets", help="comma-separated; default: subsets every member has"),
    ] = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="overwrite an existing, different output")
    ] = False,
    plugin: PluginOpt = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Fuse the recipe's members into a run (nothing is written unless every subset validates)."""

    def fn() -> CmdResult:
        load_plugins(plugin)
        res = build_run(
            BuildSpec(
                dataset=dataset,
                recipe_id=recipe_id,
                run_id=run,
                subsets=_csv(subsets),
                replace=replace,
                data_root=data_root,
                configs_root=configs_root,
            )
        )
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "run": res.run.run_id,
            "recipe": recipe_id,
            "method": res.recipe.method,
            "members": len(res.recipe.members),
            "subsets": ",".join(res.subsets),
            "built": res.built,
            "cached": res.cached,
        }
        payload = {
            "run_id": res.run.run_id,
            "recipe_id": recipe_id,
            "trained_on": res.run.trained_on,
            "subsets": {s: o.model_dump(mode="json") for s, o in res.subsets.items()},
        }
        human = [
            f"{s}: {'cached' if o.cached else 'built'} {o.samples} rows ({o.empty} empty) "
            f"sha {o.sha256[:12]}"
            for s, o in res.subsets.items()
        ]
        return "OK", fields, payload, human

    run_command("fuse.build", json_mode, data_root, fn)
