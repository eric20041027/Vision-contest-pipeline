"""vcp eval ingest: framework output -> canonical predictions inside a run directory."""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.config import is_true
from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.split import assert_plan_matches, load_plan
from vcp.measure.converters import ConvertContext, get_converter
from vcp.measure.predictions import check_predictions, write_predictions
from vcp.measure.provenance import attach_receipts, provenance
from vcp.measure.runs import (
    FUSE_FRAMEWORK,
    append_history,
    assert_run_matches,
    load_run,
    prediction_path,
    run_dir,
    save_run,
)
from vcp.measure.schema import PredictionFile, RunCard, RunSource


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
    # Plan 6 decision 1: the files these predictions came from. Their sha256 becomes the run's
    # weights_hash / config_hash -- a test-side run has no other way to acquire an identity.
    weights: Path | None = None
    config: Path | None = None
    keep_input: bool = False
    replace: bool = False
    options: dict[str, str] = Field(default_factory=dict)
    # spec 7.2: access receipt artifact ids to bind to the run (`vcp eval ingest --receipt`,
    # repeatable). Attached before anything else is written, so a receipt naming another run,
    # dataset or plan fails before a single byte of predictions is touched.
    receipts: list[str] = Field(default_factory=list)
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
    provenance: str


def _export_sha(export_dir: Path | None) -> str | None:
    """sha256 of a vcp export directory's manifest.json, or None when no directory was given.

    Reached from a CLI option (``--export-manifest``), so a directory that turns out not to be a
    vcp export is a located ``ValidationFailed`` here, never a silently-null recorded sha (F1a).
    """
    if export_dir is None:
        return None
    manifest = export_dir / "manifest.json"
    if not manifest.is_file():
        raise ValidationFailed(
            f"--export-manifest {export_dir}: manifest.json not found "
            "(the directory must be a vcp export)"
        )
    return sha256_file(manifest)


def _file_sha(path: Path | None, what: str) -> str | None:
    if path is None:
        return None
    if not path.is_file():
        raise ValidationFailed(f"{what} file not found: {path}")
    return sha256_file(path)


def _with_identity(card: RunCard, weights_sha: str | None, config_sha: str | None) -> RunCard:
    """Fill run.yaml's weights_hash / config_hash from files given at ingest. An empty field may
    be filled later; a filled one must agree, exactly like --framework / --notes."""
    source = card.source
    for name, sha in (("weights_hash", weights_sha), ("config_hash", config_sha)):
        if sha is None:
            continue
        current = getattr(source, name)
        if current is not None and current != sha:
            hint = " (omit --weights to keep the recorded hash)" if name == "weights_hash" else ""
            raise ValidationFailed(
                f"run {card.run_id!r} already declares {name}={current[:12]}; "
                f"the given file hashes to {sha[:12]}{hint}"
            )
        source = source.model_copy(update={name: sha})
    return card.model_copy(update={"source": source})


def _run_card(
    spec: IngestSpec,
    data_root: Path,
    dataset: Dataset,
    plan_subsets: set[str],
    export_sha: str | None,
) -> tuple[RunCard, bool]:
    """Existing run (checked against dataset / plan) or a fresh card."""
    weights_sha = _file_sha(spec.weights, "weights")
    config_sha = _file_sha(spec.config, "config")
    if (run_dir(data_root, spec.run_id) / "run.yaml").is_file():
        card = load_run(data_root, spec.run_id)
        # 4-1: a fused run's subsets all live in fuse.json with the member bytes they came from;
        # one added here would be invisible there. Refused before any write, ordinary runs are
        # untouched -- the way to change a fused subset is to rebuild it.
        if card.source.framework == FUSE_FRAMEWORK:
            raise ValidationFailed(
                f"fusion_run: {spec.run_id!r} was built by vcp fuse; rebuild it with "
                "`vcp fuse build --replace` instead of ingesting a subset into it",
                fields={"run": spec.run_id},
            )
        assert_run_matches(card, dataset.card)
        if card.plan_id != spec.plan_id:
            raise PlanMismatchError(
                f"run {spec.run_id!r} uses plan {card.plan_id!r}, not {spec.plan_id!r}"
            )
        if spec.trained_on and spec.trained_on != card.trained_on:
            raise ValidationFailed(
                f"run {spec.run_id!r} already declares trained_on={card.trained_on}; "
                f"got {spec.trained_on}"
            )
        # F1b: a differing --framework / --notes on a later ingest into the same run must not be
        # silently dropped -- treat each exactly like the trained_on conflict above (a located
        # ValidationFailed), not a value the loaded, unchanged card keeps hiding.
        # --export-manifest is deliberately NOT among them: it varies per subset by design (one
        # export directory per subset), so it is recorded on that subset's PredictionFile below
        # instead of being compared against the run's.
        if spec.framework and spec.framework != card.source.framework:
            raise ValidationFailed(
                f"run {spec.run_id!r} already declares framework={card.source.framework!r}; "
                f"got {spec.framework!r}; omit --framework"
            )
        if spec.notes and spec.notes != card.source.notes:
            raise ValidationFailed(
                f"run {spec.run_id!r} already declares notes={card.source.notes!r}; "
                f"got {spec.notes!r}; omit --notes"
            )
        return _with_identity(card, weights_sha, config_sha), False
    unknown = sorted(set(spec.trained_on) - plan_subsets)
    if unknown:
        raise ValidationFailed(
            f"trained_on names unknown subsets {unknown}; plan has {sorted(plan_subsets)}"
        )
    card = RunCard(
        run_id=spec.run_id,
        dataset=spec.dataset,
        samples_hash=dataset.card.samples_hash,
        plan_id=spec.plan_id,
        trained_on=list(spec.trained_on),
        source=RunSource(
            framework=spec.framework,
            notes=spec.notes,
            export_manifest_sha=export_sha,
            weights_hash=weights_sha,
            config_hash=config_sha,
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
    assert_plan_matches(plan, dataset.card)  # 4-2 / 5-7: the one plan-hash check
    plan.subset(spec.subset)  # PlanMismatchError for an unknown subset
    ids = plan.ids_in(spec.subset)
    # Read once, on every path: a --export-manifest that is not a vcp export directory must fail
    # here (F1a) whether the run is new or already exists.
    export_sha = _export_sha(spec.export_dir)
    card, created = _run_card(
        spec, paths.data_root, dataset, {s.name for s in plan.subsets}, export_sha
    )
    if spec.receipts:
        card = attach_receipts(card, spec.receipts, data_root=paths.data_root)
    converter = get_converter(spec.format)
    ctx = ConvertContext(dataset, ids, spec.export_dir, dict(spec.options))
    preds = converter.convert(spec.src, ctx)
    # Same truthiness convention as every other `key=value` option (vcp.core.config.is_true).
    allow_unknown = is_true(spec.options.get("allow_unknown"))
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
        # runs.prediction_path is the one owner of the run-relative layout (F4); derive the
        # recorded path from it instead of re-spelling "predictions/<subset>.jsonl" here.
        path=path.relative_to(run_dir(paths.data_root, spec.run_id)).as_posix(),
        sha256=sha,
        samples=stats.predicted,
        empty=stats.empty,
        format_in=spec.format,
        format_version=converter.version,
        ingested_at=stamp(),
        export_manifest_sha=export_sha,
    )
    card = card.model_copy(update={"predictions": {**card.predictions, spec.subset: entry}})
    save_run(paths.data_root, card)
    info = provenance(card, data_root=paths.data_root, configs_root=paths.configs_root)
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
        provenance=info.grade,
    )
