"""vcp eval ingest: framework output -> canonical predictions inside a run directory."""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.split import load_plan
from vcp.measure.converters import ConvertContext, get_converter
from vcp.measure.predictions import check_predictions, write_predictions
from vcp.measure.runs import (
    append_history,
    assert_run_matches,
    load_run,
    prediction_path,
    run_dir,
    save_run,
)
from vcp.measure.schema import PredictionFile, RunCard, RunSource

# Same truthiness convention as the converters' `--opt` values (Task 3 ruling 2 / Task 5 ruling 3).
_TRUE = {"1", "true", "yes"}


class IngestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    dataset: str
    plan_id: str
    subset: str
    format: str
    src: Path
    export_dir: Path | None = None
    trained_on: list[str] = Field(default_factory=list)
    framework: str = ""
    notes: str = ""
    keep_input: bool = False
    replace: bool = False
    options: dict[str, str] = Field(default_factory=dict)
    data_root: Path | None = None
    configs_root: Path | None = None


class IngestResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    run: RunCard
    subset: str
    samples: int
    predicted: int
    empty: int
    unknown: list[str]
    path: Path
    sha256: str
    replaced: bool
    created_run: bool


def _run_card(
    spec: IngestSpec, data_root: Path, dataset: Dataset, plan_subsets: set[str]
) -> tuple[RunCard, bool]:
    """Existing run (checked against dataset / plan) or a fresh card."""
    if (run_dir(data_root, spec.run_id) / "run.yaml").is_file():
        card = load_run(data_root, spec.run_id)
        assert_run_matches(card, dataset)
        if card.plan_id != spec.plan_id:
            raise PlanMismatchError(
                f"run {spec.run_id!r} uses plan {card.plan_id!r}, not {spec.plan_id!r}"
            )
        if spec.trained_on and spec.trained_on != card.trained_on:
            raise ValidationFailed(
                f"run {spec.run_id!r} already declares trained_on={card.trained_on}; "
                f"got {spec.trained_on}"
            )
        return card, False
    unknown = sorted(set(spec.trained_on) - plan_subsets)
    if unknown:
        raise ValidationFailed(
            f"trained_on names unknown subsets {unknown}; plan has {sorted(plan_subsets)}"
        )
    export_sha = None
    if spec.export_dir is not None and (spec.export_dir / "manifest.json").is_file():
        export_sha = sha256_file(spec.export_dir / "manifest.json")
    card = RunCard(
        run_id=spec.run_id,
        dataset=spec.dataset,
        samples_hash=dataset.card.samples_hash,
        plan_id=spec.plan_id,
        trained_on=list(spec.trained_on),
        source=RunSource(
            framework=spec.framework, notes=spec.notes, export_manifest_sha=export_sha
        ),
        created_at=stamp(),
    )
    return card, True


def ingest(spec: IngestSpec) -> IngestResult:
    paths = DatasetPaths.resolve(
        spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    dataset = Dataset.load(spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root)
    plan = load_plan(paths, spec.plan_id)
    if plan.dataset_hash != dataset.card.samples_hash:
        raise PlanMismatchError(
            f"plan {spec.plan_id!r} was built on samples_hash {plan.dataset_hash[:12]}, "
            f"dataset now has {dataset.card.samples_hash[:12]}"
        )
    plan.subset(spec.subset)  # PlanMismatchError for an unknown subset
    ids = plan.ids_in(spec.subset)
    card, created = _run_card(spec, paths.data_root, dataset, {s.name for s in plan.subsets})
    converter = get_converter(spec.format)
    ctx = ConvertContext(dataset, ids, spec.export_dir, dict(spec.options))
    preds = converter.convert(spec.src, ctx)
    allow_unknown = spec.options.get("allow_unknown", "false").lower() in _TRUE
    kept, stats = check_predictions(preds, dataset, ids, allow_unknown=allow_unknown)
    replaced = spec.subset in card.predictions
    if replaced and not spec.replace:
        raise ValidationFailed(
            f"run {spec.run_id!r} already has predictions for {spec.subset!r}; pass --replace"
        )
    if replaced:
        append_history(
            paths.data_root,
            spec.run_id,
            {
                "event": "replace",
                "subset": spec.subset,
                "old_sha256": card.predictions[spec.subset].sha256,
            },
        )
    path = prediction_path(paths.data_root, spec.run_id, spec.subset)
    sha = write_predictions(path, kept)
    if spec.keep_input:
        dest = run_dir(paths.data_root, spec.run_id) / "inputs" / spec.subset
        if dest.exists():
            shutil.rmtree(dest)
        if spec.src.is_dir():
            shutil.copytree(spec.src, dest)
        else:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(spec.src, dest / spec.src.name)
    entry = PredictionFile(
        path=f"predictions/{spec.subset}.jsonl",
        sha256=sha,
        samples=stats.predicted,
        empty=stats.empty,
        format_in=spec.format,
        ingested_at=stamp(),
    )
    card = card.model_copy(update={"predictions": {**card.predictions, spec.subset: entry}})
    save_run(paths.data_root, card)
    return IngestResult(
        run=card,
        subset=spec.subset,
        samples=stats.samples,
        predicted=stats.predicted,
        empty=stats.empty,
        unknown=stats.unknown,
        path=path,
        sha256=sha,
        replaced=replaced,
        created_run=created,
    )
