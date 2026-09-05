"""``vcp fuse ablate``: one variant per member (the recipe without it), built alongside the full
recipe, and -- with ``--preregister`` -- one admission claim per member for ``vcp eval judge``:
candidate = the full recipe, baseline = the recipe without the member (spec 6.3; postmortem 9.6).

All-or-nothing: every check runs before the first file is written, including "the candidate has
not been measured yet" -- an admission claim written after the answer is known is not a claim.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.split import SplitPlan, load_plan
from vcp.fuse.build import (
    RUN_BOUND_ELSEWHERE,
    BuildSpec,
    build_run,
    check_existing_run,
    default_run_id,
    resolve_subsets,
)
from vcp.fuse.fusers import get_fuser, require_payload, resolve_params
from vcp.fuse.members import check_members, check_plan, union_trained_on
from vcp.fuse.recipes import load_recipe, recipe_path, recipe_sha, same_recipe, save_recipe
from vcp.fuse.schema import Member, Recipe
from vcp.measure.ledger import ReadingsLedger
from vcp.measure.metrics import effective_params as metric_params
from vcp.measure.metrics import get_metric, params_key
from vcp.measure.prereg import create_prereg, measured_subsets, prereg_path
from vcp.measure.report import READINGS_LEDGER
from vcp.measure.runs import run_dir
from vcp.measure.schema import PreRegistration, RunCard

SINGLE_MEMBER = "single_member"
CANDIDATE_MEASURED = "candidate_measured"
VARIANT_CONFLICT = "variant_conflict"


class AblateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    recipe_id: str
    subsets: list[str] = Field(default_factory=list)
    build: bool = True
    preregister: bool = False
    metric: str | None = None
    metric_params: dict[str, str] = Field(default_factory=dict)
    bases: list[str] = Field(default_factory=lambda: ["valA", "valB"])
    t_min: float = 2.0
    min_bases: int = 2
    data_root: Path | None = None
    configs_root: Path | None = None


class AblateResult(BaseModel):
    recipe_id: str
    variants: list[str]
    runs: list[str]
    preregs: list[str]
    built: int
    cached: int


def variant_id(recipe_id: str, member_run: str) -> str:
    return f"{recipe_id}-minus-{member_run}"


def admit_id(recipe_id: str, member_run: str) -> str:
    return f"{recipe_id}-admit-{member_run}"


def _variant(recipe: Recipe, member: Member) -> Recipe:
    """The recipe without one member: same method, params, weights, order and notes."""
    return Recipe(
        recipe_id=variant_id(recipe.recipe_id, member.run),
        dataset=recipe.dataset,
        plan_id=recipe.plan_id,
        method=recipe.method,
        params=dict(recipe.params),
        members=[m for m in recipe.members if m.run != member.run],
        notes=recipe.notes,
        created_at=stamp(),
    )


def _admission(
    recipe: Recipe, member: Member, spec: AblateSpec, metric: str, params: dict[str, str]
) -> PreRegistration:
    return PreRegistration(
        prereg_id=admit_id(recipe.recipe_id, member.run),
        claim=(
            f"recipe {recipe.recipe_id}: member {member.run} contributes "
            "(fused with it beats fused without it)"
        ),
        component=member.run,
        component_class="model",
        baseline_run=default_run_id(variant_id(recipe.recipe_id, member.run)),
        candidate_run=default_run_id(recipe.recipe_id),
        metric=metric,
        params=params,
        subsets=list(spec.bases),
        t_min=spec.t_min,
        min_bases=spec.min_bases,
        created_at=stamp(),
    )


def _check_variants(
    paths: DatasetPaths, recipe: Recipe, dataset: Dataset, cards: list[RunCard]
) -> list[tuple[Recipe, bool]]:
    """Each variant's content and whether its file already exists (identical) -- or a conflict."""
    out: list[tuple[Recipe, bool]] = []
    for m in recipe.members:
        content = _variant(recipe, m)
        exists = recipe_path(paths, content.recipe_id).is_file()
        if exists and not same_recipe(load_recipe(paths, content.recipe_id), content):
            raise ValidationFailed(
                f"{VARIANT_CONFLICT}: recipe {content.recipe_id!r} already exists with different "
                "content; remove it or rename the parent recipe",
                fields={"recipe": content.recipe_id},
            )
        rid = default_run_id(content.recipe_id)
        if exists:
            keep = {x.run for x in content.members}
            check_existing_run(
                paths.data_root,
                rid,
                recipe=content,
                recipe_sha256=recipe_sha(paths, content.recipe_id),
                dataset=dataset,
                trained_on=union_trained_on([c for c in cards if c.run_id in keep]),
            )
        elif (run_dir(paths.data_root, rid) / "run.yaml").is_file():
            raise ValidationFailed(
                f"{RUN_BOUND_ELSEWHERE}: run {rid!r} exists but recipe {content.recipe_id!r} does "
                "not; a fused run without its recipe was not built by this command",
                fields={"run": rid},
            )
        out.append((content, exists))
    return out


def _check_claims(
    paths: DatasetPaths,
    recipe: Recipe,
    dataset: Dataset,
    plan: SplitPlan,
    spec: AblateSpec,
    subsets: list[str],
) -> tuple[list[PreRegistration], ReadingsLedger]:
    if not spec.metric:
        raise ValidationFailed("--preregister needs --metric")
    metric = get_metric(spec.metric)
    if dataset.card.task not in metric.tasks:
        raise ValidationFailed(
            f"metric {spec.metric!r} is not applicable to task {dataset.card.task!r}"
        )
    params = metric_params(metric, spec.metric_params)
    # Unconditional, even with --no-build: a claim's ``subsets`` names the bases it will be
    # judged on, and that must be well-formed before anything is written, not just when this
    # command happens to build the runs it points at.
    if not spec.bases:
        raise ValidationFailed("--bases must name at least one subset")
    dupes = sorted({b for b in spec.bases if spec.bases.count(b) > 1})
    if dupes:
        raise ValidationFailed(f"--bases has duplicate subsets {dupes}")
    for b in spec.bases:
        plan.subset(b)  # PlanMismatchError for a subset the plan does not have
    if spec.build:
        missing = [b for b in spec.bases if b not in subsets]
        if missing:
            raise ValidationFailed(
                f"bases {missing} are not among the subsets this ablation builds {subsets}"
            )
    readings = ReadingsLedger(paths.measure_dir / READINGS_LEDGER)
    claims: list[PreRegistration] = []
    for m in recipe.members:
        pr = _admission(recipe, m, spec, spec.metric, params)
        if prereg_path(paths, pr.prereg_id).exists():
            raise ValidationFailed(
                f"pre-registration {pr.prereg_id!r} already exists", fields={"prereg": pr.prereg_id}
            )
        measured = measured_subsets(readings, pr, params_key(params))
        if measured:
            raise ValidationFailed(
                f"{CANDIDATE_MEASURED}: candidate {pr.candidate_run!r} already has {spec.metric} "
                f"readings on {measured}; an admission claim must be written before the full "
                "recipe is measured",
                fields={"run": pr.candidate_run},
            )
        claims.append(pr)
    return claims, readings


def ablate_recipe(spec: AblateSpec) -> AblateResult:
    paths = DatasetPaths.resolve(
        spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    dataset = Dataset.load(spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root)
    recipe = load_recipe(paths, spec.recipe_id)
    if len(recipe.members) < 2:
        raise ValidationFailed(
            f"{SINGLE_MEMBER}: recipe {recipe.recipe_id!r} has one member; nothing to ablate",
            fields={"recipe": recipe.recipe_id},
        )
    plan = load_plan(paths, recipe.plan_id)
    check_plan(plan, dataset)
    fuser = get_fuser(recipe.method)
    require_payload(fuser, dataset.card.task)
    resolve_params(fuser, recipe.params)
    cards = check_members(
        recipe.members, data_root=paths.data_root, dataset=dataset, plan_id=recipe.plan_id
    )
    subsets = resolve_subsets(spec.subsets, plan, cards)
    check_existing_run(
        paths.data_root,
        default_run_id(recipe.recipe_id),
        recipe=recipe,
        recipe_sha256=recipe_sha(paths, recipe.recipe_id),
        dataset=dataset,
        trained_on=union_trained_on(cards),
    )
    variants = _check_variants(paths, recipe, dataset, cards)
    claims: list[PreRegistration] = []
    readings: ReadingsLedger | None = None
    if spec.preregister:
        claims, readings = _check_claims(paths, recipe, dataset, plan, spec, subsets)
    # Every check has passed: variant recipes -> builds -> claims (a claim never names a run
    # that does not exist yet, unless the caller asked for --no-build).
    for content, exists in variants:
        if not exists:
            save_recipe(paths, content)
    runs: list[str] = []
    built = cached = 0
    if spec.build:
        for rid in [recipe.recipe_id, *(c.recipe_id for c, _ in variants)]:
            res = build_run(
                BuildSpec(
                    dataset=spec.dataset,
                    recipe_id=rid,
                    subsets=subsets,
                    data_root=spec.data_root,
                    configs_root=spec.configs_root,
                )
            )
            runs.append(res.run.run_id)
            built += res.built
            cached += res.cached
    for pr in claims:
        create_prereg(paths, pr, readings)  # type: ignore[arg-type]
    return AblateResult(
        recipe_id=recipe.recipe_id,
        variants=[c.recipe_id for c, _ in variants],
        runs=runs,
        preregs=[p.prereg_id for p in claims],
        built=built,
        cached=cached,
    )
