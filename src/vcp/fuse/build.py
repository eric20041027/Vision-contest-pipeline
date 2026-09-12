"""``vcp fuse build``: a recipe's members, fused subset by subset, written as one ordinary run.

Nothing is written until every requested subset has been fused in memory and validated (spec
6.2): a member whose file drifted from its recorded sha, a fuser that emitted a row for a sample
outside the subset, an output that would silently differ from the one already on disk -- each
fails before the first byte, so a failed build leaves the run exactly as it was. What does get
written is bit-reproducible: the same member bytes and the same recipe give the same output
sha, and ``fuse.json`` records both sides so anyone can check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vcp.core.build import build_string
from vcp.core.errors import (
    IntegrityError,
    InvariantError,
    PlanMismatchError,
    ValidationFailed,
    VcpError,
)
from vcp.core.hashing import sha256_text
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.split import SplitPlan, load_plan
from vcp.fuse.fusers import (
    FuseContext,
    Fuser,
    MemberPredictions,
    get_fuser,
    require_payload,
    resolve_params,
)
from vcp.fuse.members import check_members, check_plan, common_subsets, union_trained_on
from vcp.fuse.recipes import load_recipe, recipe_sha
from vcp.fuse.schema import FuseRecord, MemberRecord, Recipe, SubsetBuild
from vcp.measure.predictions import (
    PredictionStats,
    check_predictions,
    predictions_by_id,
    predictions_text,
    read_predictions,
    write_predictions,
)
from vcp.measure.runs import (
    FUSE_FRAMEWORK,
    append_history,
    assert_run_matches,
    load_run,
    prediction_path,
    run_dir,
    save_run,
    verify_prediction,
)
from vcp.measure.schema import Prediction, PredictionFile, RunCard, RunSource

FRAMEWORK = FUSE_FRAMEWORK
RECORD_FILE = "fuse.json"
NO_COMMON_SUBSET = "no_common_subset"
OUTPUT_EXISTS = "output_exists"
RUN_BOUND_ELSEWHERE = "run_bound_elsewhere"


class BuildSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    recipe_id: str
    run_id: str | None = None
    subsets: list[str] = Field(default_factory=list)
    replace: bool = False
    data_root: Path | None = None
    configs_root: Path | None = None


class SubsetOutcome(BaseModel):
    sha256: str
    samples: int
    empty: int
    cached: bool


class BuildResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    run: RunCard
    recipe: Recipe
    record: FuseRecord
    subsets: dict[str, SubsetOutcome]
    built: int
    cached: int
    created_run: bool


@dataclass(frozen=True)
class _Fused:
    rows: list[Prediction]
    stats: PredictionStats
    sha256: str
    member_sha256: dict[str, str]


def default_run_id(recipe_id: str) -> str:
    return f"fuse-{recipe_id}"


def record_path(data_root: Path, run_id: str) -> Path:
    return run_dir(data_root, run_id) / RECORD_FILE


def load_record(data_root: Path, run_id: str) -> FuseRecord:
    path = record_path(data_root, run_id)
    if not path.is_file():
        raise ValidationFailed(f"fusion record not found: {path}", fields={"run": run_id})
    try:
        record = FuseRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValidationFailed(
            f"bad fusion record: {e}", location=str(path), fields={"run": run_id}
        ) from e
    if record.run_id != run_id:
        raise ValidationFailed(
            f"fuse.json names run {record.run_id!r}, not {run_id!r}",
            location=str(path),
            fields={"run": run_id},
        )
    return record


def write_record(data_root: Path, run_id: str, record: FuseRecord) -> Path:
    path = record_path(data_root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
    return path


def content_sha(preds: list[Prediction]) -> str:
    """The sha256 ``write_predictions`` would record for these rows, without touching the disk
    (the cache check runs before any write). Shares ``predictions_text`` with
    ``write_predictions`` so the two can never drift apart; a test pins them equal."""
    return sha256_text(predictions_text(preds))


def resolve_subsets(requested: list[str], plan: SplitPlan, cards: list[RunCard]) -> list[str]:
    """Spec 6.1: the subsets named, each checked for every member; else the common ones."""
    if requested:
        names = list(dict.fromkeys(requested))
        for name in names:
            plan.subset(name)  # PlanMismatchError for an unknown subset
            for card in cards:
                if name not in card.predictions:
                    raise ValidationFailed(
                        f"member {card.run_id!r} has no predictions for subset {name!r}",
                        fields={"member": card.run_id, "subset": name},
                    )
        return names
    names = common_subsets(plan, cards)
    if not names:
        raise ValidationFailed(
            f"{NO_COMMON_SUBSET}: no subset has predictions from every member; "
            "name one with --subsets after ingesting it for each member"
        )
    return names


def check_existing_run(
    data_root: Path,
    run_id: str,
    *,
    recipe: Recipe,
    recipe_sha256: str,
    dataset: Dataset,
    trained_on: list[str],
) -> RunCard | None:
    """The run.yaml already under this id, if it is a build of THIS recipe; None when absent.

    A run id binds to one recipe file (spec 4.1): an ingested run, or a build of some other
    recipe, must never be silently overwritten with this recipe's output.
    """
    if not (run_dir(data_root, run_id) / "run.yaml").is_file():
        return None
    card = load_run(data_root, run_id)
    if card.source.framework != FRAMEWORK or card.source.config_hash != recipe_sha256:
        raise ValidationFailed(
            f"{RUN_BOUND_ELSEWHERE}: run {run_id!r} is not a build of recipe "
            f"{recipe.recipe_id!r} (framework={card.source.framework!r}, "
            f"config_hash={card.source.config_hash!r}); pick another --run",
            fields={"run": run_id},
        )
    try:
        assert_run_matches(card, dataset.card)
    except VcpError as e:
        # 4-7: shared with the measurement layer, so it knows nothing of this run id; every
        # other refusal in this function names the run, and so must this one.
        e.fields.setdefault("run", run_id)
        raise
    if card.plan_id != recipe.plan_id:
        raise PlanMismatchError(
            f"run {run_id!r} uses plan {card.plan_id!r}, not {recipe.plan_id!r}",
            fields={"run": run_id},
        )
    if card.trained_on != trained_on:
        raise ValidationFailed(
            f"run {run_id!r} declares trained_on={card.trained_on}; the members now give "
            f"{trained_on}",
            fields={"run": run_id},
        )
    return card


def _fuse_subset(
    subset: str,
    *,
    data_root: Path,
    dataset: Dataset,
    plan: SplitPlan,
    recipe: Recipe,
    cards: list[RunCard],
    fuser: Fuser,
    params: dict[str, str],
) -> _Fused:
    ids = plan.ids_in(subset)
    ordered = [s.sample_id for s in dataset.samples if s.sample_id in ids]
    members: list[MemberPredictions] = []
    shas: dict[str, str] = {}
    for m, card in zip(recipe.members, cards, strict=True):
        try:
            path = verify_prediction(data_root, card, subset)
        except (ValidationFailed, IntegrityError) as e:
            e.fields.setdefault("member", m.run)
            e.fields.setdefault("subset", subset)
            raise
        shas[m.run] = card.predictions[subset].sha256
        members.append(
            MemberPredictions(
                run_id=m.run, weight=m.weight, predictions=predictions_by_id(read_predictions(path))
            )
        )
    ctx = FuseContext(
        dataset=dataset,
        subset=subset,
        ids=ordered,
        samples={sid: dataset.by_id[sid] for sid in ordered},
        params=params,
    )
    try:
        rows = fuser.fuse(members, ctx)
        kept, stats = check_predictions(rows, dataset, ids)
    except VcpError as e:
        e.fields.setdefault("subset", subset)
        raise
    return _Fused(rows=kept, stats=stats, sha256=content_sha(kept), member_sha256=shas)


def _new_card(
    recipe: Recipe, dataset: Dataset, run_id: str, sha: str, trained_on: list[str]
) -> RunCard:
    return RunCard(
        run_id=run_id,
        dataset=recipe.dataset,
        samples_hash=dataset.card.samples_hash,
        plan_id=recipe.plan_id,
        trained_on=trained_on,
        source=RunSource(framework=FRAMEWORK, config_hash=sha, notes=recipe.notes),
        created_at=stamp(),
    )


def _new_record(
    recipe: Recipe,
    run_id: str,
    sha: str,
    fuser: Fuser,
    params: dict[str, str],
    cards: list[RunCard],
) -> FuseRecord:
    return FuseRecord(
        run_id=run_id,
        recipe_id=recipe.recipe_id,
        recipe_sha256=sha,
        method=fuser.name,
        method_version=fuser.version,
        params=params,
        members=[
            MemberRecord(run=m.run, weight=m.weight, trained_on=list(c.trained_on))
            for m, c in zip(recipe.members, cards, strict=True)
        ],
        vcp_version=build_string(),
    )


def _write(
    data_root: Path,
    run_id: str,
    fuser: Fuser,
    card: RunCard,
    record: FuseRecord,
    pending: list[tuple[str, _Fused]],
) -> tuple[RunCard, FuseRecord, dict[str, SubsetOutcome]]:
    """Spec 6.2 step 5: replace events -> prediction files -> run.yaml -> fuse.json."""
    outcomes: dict[str, SubsetOutcome] = {}
    for subset, f in pending:
        prev = card.predictions.get(subset)
        if prev is not None:
            append_history(
                data_root,
                run_id,
                {
                    "event": "replace",
                    "subset": subset,
                    "old_sha256": prev.sha256,
                    "via": "fuse.build",
                },
            )
        path = prediction_path(data_root, run_id, subset)
        written = write_predictions(path, f.rows)
        if written != f.sha256:
            raise InvariantError(
                f"fused predictions for {subset!r} hashed {written[:12]} on disk but "
                f"{f.sha256[:12]} in memory"
            )
        entry = PredictionFile(
            path=path.relative_to(run_dir(data_root, run_id)).as_posix(),
            sha256=written,
            samples=f.stats.predicted,
            empty=f.stats.empty,
            format_in=f"fuse:{fuser.name}",
            ingested_at=stamp(),
        )
        card = card.model_copy(update={"predictions": {**card.predictions, subset: entry}})
        build = SubsetBuild(
            member_sha256=f.member_sha256,
            output_sha256=written,
            samples=f.stats.predicted,
            empty=f.stats.empty,
            built_at=stamp(),
        )
        record = record.model_copy(update={"subsets": {**record.subsets, subset: build}})
        outcomes[subset] = SubsetOutcome(
            sha256=written, samples=f.stats.predicted, empty=f.stats.empty, cached=False
        )
    save_run(data_root, card)
    write_record(data_root, run_id, record)
    return card, record, outcomes


def build_run(spec: BuildSpec) -> BuildResult:
    paths = DatasetPaths.resolve(
        spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    dataset = Dataset.load(spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root)
    recipe = load_recipe(paths, spec.recipe_id)
    plan = load_plan(paths, recipe.plan_id)
    check_plan(plan, dataset)
    fuser = get_fuser(recipe.method)
    require_payload(fuser, dataset.card.task)
    params = resolve_params(fuser, recipe.params)
    cards = check_members(
        recipe.members, data_root=paths.data_root, dataset=dataset, plan_id=recipe.plan_id
    )
    subsets = resolve_subsets(spec.subsets, plan, cards)
    run_id = spec.run_id or default_run_id(recipe.recipe_id)
    sha = recipe_sha(paths, recipe.recipe_id)
    trained_on = union_trained_on(cards)
    existing = check_existing_run(
        paths.data_root,
        run_id,
        recipe=recipe,
        recipe_sha256=sha,
        dataset=dataset,
        trained_on=trained_on,
    )
    card = existing or _new_card(recipe, dataset, run_id, sha, trained_on)
    missing_record = existing is not None and not record_path(paths.data_root, run_id).is_file()
    rebuild_all = missing_record and spec.replace
    if rebuild_all:
        # A fresh record must cover the whole card, even when --subsets names fewer subsets.
        subsets = resolve_subsets([*subsets, *card.predictions], plan, cards)
    if existing is not None and not missing_record:
        # Metadata (method_version, vcp_version, members[].trained_on) is rebuilt fresh every
        # time -- only the subsets already on disk carry forward (spec 4.2: the file is a
        # snapshot rewritten whole on each build, not extended in place).
        record = _new_record(recipe, run_id, sha, fuser, params, cards).model_copy(
            update={"subsets": load_record(paths.data_root, run_id).subsets}
        )
    else:
        record = _new_record(recipe, run_id, sha, fuser, params, cards)
    # Steps 2-3: every subset fused and validated in memory before anything is compared or written.
    fused = {
        subset: _fuse_subset(
            subset,
            data_root=paths.data_root,
            dataset=dataset,
            plan=plan,
            recipe=recipe,
            cards=cards,
            fuser=fuser,
            params=params,
        )
        for subset in subsets
    }
    # Step 4: cache hits and conflicts, still before the first write.
    if (
        missing_record
        and not spec.replace
        and any(
            name not in fused or prev.sha256 == fused[name].sha256
            for name, prev in card.predictions.items()
        )
    ):
        raise ValidationFailed(
            f"not_found: fuse.json for run {run_id!r}; pass --replace to rebuild every subset",
            fields={"run": run_id},
        )
    outcomes: dict[str, SubsetOutcome] = {}
    pending: list[tuple[str, _Fused]] = []
    for subset, f in fused.items():
        prev = card.predictions.get(subset)
        if prev is not None and prev.sha256 == f.sha256 and not rebuild_all:
            outcomes[subset] = SubsetOutcome(
                sha256=f.sha256, samples=f.stats.predicted, empty=f.stats.empty, cached=True
            )
            continue
        if prev is not None and not spec.replace:
            raise ValidationFailed(
                f"{OUTPUT_EXISTS}: run {run_id!r} already has different predictions for "
                f"{subset!r} ({prev.sha256[:12]} vs {f.sha256[:12]}); pass --replace to "
                "overwrite (the old sha goes to history.jsonl)",
                fields={"run": run_id, "subset": subset},
            )
        pending.append((subset, f))
    if pending:
        card, record, written = _write(paths.data_root, run_id, fuser, card, record, pending)
        outcomes.update(written)
    return BuildResult(
        run=card,
        recipe=recipe,
        record=record,
        subsets={s: outcomes[s] for s in subsets},
        built=len(pending),
        cached=len(subsets) - len(pending),
        created_run=existing is None,
    )
