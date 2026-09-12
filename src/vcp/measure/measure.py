"""vcp eval measure: guardrail first, then one reading per (subset, metric) into the ledger."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import GuardrailError, SealedSubsetError, ValidationFailed
from vcp.core.paths import DatasetPaths, resolve_data_root
from vcp.core.time import stamp
from vcp.data.access.access import DatasetAccess
from vcp.data.dataset import Dataset
from vcp.data.lineage import clean_eval_subsets
from vcp.data.schema import DatasetCard, Sample
from vcp.data.split import SplitPlan, load_plan
from vcp.measure.anchors import anchor_key, load_anchors
from vcp.measure.ledger import READINGS_LEDGER, ReadingsLedger, reading_id
from vcp.measure.metrics import (
    Metric,
    applicable_metrics,
    effective_params,
    get_metric,
    params_key,
)
from vcp.measure.predictions import predictions_by_id, read_predictions
from vcp.measure.provenance import ProvenanceInfo, provenance
from vcp.measure.runs import assert_run_matches, load_run, verify_prediction
from vcp.measure.schema import Anchor, GuardrailInfo, Prediction, Reading, RunCard

CALLER = "vcp eval measure"
SEALED_SUFFIX = "(sealed)"


class MeasureSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    metrics: list[str] = Field(default_factory=list)
    subsets: list[str] = Field(default_factory=list)
    params: dict[str, str] = Field(default_factory=dict)
    unseal: bool = False
    reason: str | None = None
    data_root: Path | None = None
    configs_root: Path | None = None


class MeasureResult(BaseModel):
    run_id: str
    dataset: str
    readings: list[Reading]
    new: int
    cached: int
    guardrail: str  # OK | partial | none | cached
    warnings: list[str]
    provenance: str
    observed: list[str] = Field(default_factory=list)
    receipt_invalid: int = 0


def load_context(
    run_id: str, data_root: Path | None, configs_root: Path | None
) -> tuple[RunCard, Dataset, SplitPlan, DatasetPaths]:
    """A run card together with the dataset, plan and paths it must still agree with."""
    card = load_run(resolve_data_root(data_root), run_id)
    paths = DatasetPaths.resolve(card.dataset, data_root=data_root, configs_root=configs_root)
    dataset = Dataset.load(card.dataset, data_root=data_root, configs_root=configs_root)
    assert_run_matches(
        card, dataset.card
    )  # one owner for the run-vs-dataset check (Task 5 ruling 4)
    plan = load_plan(paths, card.plan_id)
    return card, dataset, plan, paths


def load_card_context(
    run_id: str, data_root: Path | None, configs_root: Path | None
) -> tuple[RunCard, DatasetCard, SplitPlan, DatasetPaths]:
    """``load_context`` without parsing the samples file: what ``measure_run`` needs before it
    opens a role-scoped access (spec 6.7). judge / sigma / anchor keep ``load_context``."""
    card = load_run(resolve_data_root(data_root), run_id)
    paths = DatasetPaths.resolve(card.dataset, data_root=data_root, configs_root=configs_root)
    dataset_card = Dataset.load_card(card.dataset, data_root=data_root, configs_root=configs_root)
    assert_run_matches(card, dataset_card)
    plan = load_plan(paths, card.plan_id)
    return card, dataset_card, plan, paths


def default_subsets(
    plan: SplitPlan, card: RunCard, *, unseal: bool, observed: list[str] | tuple[str, ...] = ()
) -> list[str]:
    """Clean eval subsets for this run; sealed ones only when unsealing.

    A subset the run's receipts show it read is not clean whatever ``trained_on`` says (spec 8);
    with nothing trained on and nothing observed, every eval subset is clean (spec 6.1).
    """
    touched = set(card.trained_on) | set(observed)
    if touched:
        # clean_eval_subsets decorates a sealed subset as "holdout(sealed)"; the marker has to
        # come off before the name is looked up anywhere.
        names = [n.removesuffix(SEALED_SUFFIX) for n in clean_eval_subsets(plan, touched)]
    else:
        names = [s.name for s in plan.subsets if s.role in ("eval", "sealed")]
    roles = {s.name: s.role for s in plan.subsets}
    return [n for n in names if roles[n] != "sealed" or unseal]


def _unique(names: list[str]) -> list[str]:
    """Order-preserving dedup. A reading is one row per identity (spec 4.3), so naming the same
    subset or metric twice must ask for it once, not try to append the same row twice."""
    return list(dict.fromkeys(names))


def _check_subsets(card: RunCard, subsets: list[str], info: ProvenanceInfo) -> None:
    if not subsets:
        raise ValidationFailed(
            f"run {card.run_id!r} has no clean eval subset to measure "
            f"(trained_on={card.trained_on}); name one with --subsets, or --unseal --reason "
            "to open a sealed one"
        )
    for name in subsets:
        if name in card.trained_on:
            raise ValidationFailed(f"subset {name!r} is in the run's trained_on {card.trained_on}")
        if name in info.observed:
            receipt = next((r.artifact_id for r in info.receipts if name in r.subsets), "?")
            raise ValidationFailed(
                f"contaminated: subset {name!r} was read by the run (receipt {receipt})",
                fields={"subset": name},
            )
        if name not in card.predictions:
            raise ValidationFailed(f"run {card.run_id!r} has no predictions for subset {name!r}")


def _resolve_metrics(task: str, requested: list[str]) -> list[str]:
    names = requested or applicable_metrics(task)
    for name in names:
        if task not in get_metric(name).tasks:
            raise ValidationFailed(f"metric {name!r} is not applicable to task {task!r}")
    if not names:
        raise ValidationFailed(f"no registered metric applies to task {task!r}")
    return names


def _check_params(metric_names: list[str], params: dict[str, str]) -> None:
    """A param key must be declared by at least one requested metric; a metric that does not
    declare it simply keeps its own default for that reading."""
    known: set[str] = set().union(*(set(get_metric(n).defaults) for n in metric_names))
    unknown = sorted(set(params) - known)
    if unknown:
        raise ValidationFailed(
            f"no registered metric accepts params {unknown}; known: {sorted(known)}"
        )


def _metric_params(metric: Metric, spec_params: dict[str, str]) -> dict[str, str]:
    """One metric's effective params: only the keys it declares ever reach it."""
    return effective_params(metric, {k: v for k, v in spec_params.items() if k in metric.defaults})


@dataclass(frozen=True)
class _Context:
    """What every (subset, metric) cell of one measure shares."""

    spec: MeasureSpec
    card: RunCard
    dataset_card: DatasetCard
    plan: SplitPlan
    paths: DatasetPaths
    ledger: ReadingsLedger
    anchors: dict[str, Anchor]
    provenance: str


@dataclass(frozen=True)
class _Cell:
    """One (subset, metric) reading and how it got here."""

    reading: Reading
    key: str
    anchored: bool
    is_new: bool


def _guardrail(
    ctx: _Context,
    anchor: Anchor | None,
    key: str,
    subset: str,
    samples: list[Sample],
    metric: Metric,
    params: dict[str, str],
) -> GuardrailInfo | None:
    """Reproduce the anchor's own reading before trusting anything measured here (spec 9).

    ``None`` when this cell has no anchor. A value that drifts beyond the anchor's tolerance
    aborts the command, so not one reading of this run reaches the ledger.
    """
    if anchor is None:
        return None
    root = ctx.paths.data_root
    anchor_run = load_run(root, anchor.run_id)
    # A stale anchor run (its card no longer matching the dataset, e.g. after a re-import) must
    # surface as the PlanMismatchError it is, not a confusing GuardrailError from recomputing a
    # metric against samples the anchor run was never measured on (Minor 4).
    assert_run_matches(anchor_run, ctx.dataset_card)
    anchor_preds = predictions_by_id(read_predictions(verify_prediction(root, anchor_run, subset)))
    got = metric.compute(samples, anchor_preds, ctx.dataset_card, params).value
    if abs(got - anchor.value) > anchor.tolerance:
        raise GuardrailError(
            f"anchor {key!r} (reading_id={anchor.reading_id!r}) expected {anchor.value!r}, "
            f"got {got!r} (tolerance {anchor.tolerance}); no readings written",
            # spec 9 wants this readable by machine as well as by eye: the CLI renders these
            # beside reason=, so a caller greps `guardrail=FAIL` instead of the message.
            fields={"guardrail": "FAIL", "anchor": anchor.reading_id, "got": got},
        )
    return GuardrailInfo(anchor_reading_id=anchor.reading_id, ok=True)


def _measure_one(
    ctx: _Context,
    subset: str,
    samples: list[Sample],
    preds: dict[str, Prediction],
    metric: Metric,
) -> _Cell:
    params = _metric_params(metric, ctx.spec.params)
    pk = params_key(params)
    key = anchor_key(ctx.plan.plan_id, subset, metric.name, pk)
    anchor = ctx.anchors.get(key)
    # The guardrail runs before the cache is consulted, so a cached reading is re-verified too
    # and guardrail="cached" can never mean "not checked".
    guard = _guardrail(ctx, anchor, key, subset, samples, metric, params)
    sha = ctx.card.predictions[subset].sha256
    rid = reading_id(
        ctx.card.run_id, ctx.plan.plan_id, subset, metric.name, metric.version, pk, sha
    )
    stored = ctx.ledger.by_id.get(rid)
    if stored is not None:
        # The stored row keeps the guardrail it was written with: ledger rows are never rewritten.
        return _Cell(reading=stored, key=key, anchored=anchor is not None, is_new=False)
    result = metric.compute(samples, preds, ctx.dataset_card, params)
    reading = Reading(
        reading_id=rid,
        ts=stamp(),
        run_id=ctx.card.run_id,
        dataset=ctx.card.dataset,
        samples_hash=ctx.card.samples_hash,
        plan_id=ctx.plan.plan_id,
        subset=subset,
        metric=metric.name,
        metric_version=metric.version,
        params=params,
        value=result.value,
        per_class=result.per_class,
        n_samples=result.n,
        prediction_sha=sha,
        guardrail=guard,
        provenance=ctx.provenance,
    )
    return _Cell(reading=reading, key=key, anchored=anchor is not None, is_new=True)


def _guardrail_state(states: list[str], *, wrote: bool) -> str:
    """The VERDICT's ``guardrail=`` field. Every cell above was actually checked: ``cached``
    reports that nothing new was written, never that nothing was verified."""
    if not wrote:
        return "cached"
    if all(s == "OK" for s in states):
        return "OK"
    if all(s == "none" for s in states):
        return "none"
    return "partial"


def measure_run(spec: MeasureSpec) -> MeasureResult:
    card, dataset_card, plan, paths = load_card_context(
        spec.run_id, spec.data_root, spec.configs_root
    )
    info = provenance(card, data_root=paths.data_root, configs_root=paths.configs_root)
    subsets = _unique(
        spec.subsets or default_subsets(plan, card, unseal=spec.unseal, observed=info.observed)
    )
    _check_subsets(card, subsets, info)
    metric_names = _unique(_resolve_metrics(dataset_card.task, spec.metrics))
    _check_params(metric_names, spec.params)
    ctx = _Context(
        spec=spec,
        card=card,
        dataset_card=dataset_card,
        plan=plan,
        paths=paths,
        ledger=ReadingsLedger(paths.measure_dir / READINGS_LEDGER),
        anchors=load_anchors(paths),
        provenance=info.grade,
    )
    readings: list[Reading] = []
    pending: list[Reading] = []
    states: list[str] = []
    warnings: list[str] = []
    if info.invalid:
        warnings.append(f"receipt_invalid={len(info.invalid)}")
    cached = 0
    if spec.unseal and not spec.reason:  # the message Dataset.subset gave before the accessor
        raise SealedSubsetError("unseal requires a non-empty reason")
    with DatasetAccess.open(
        card.dataset,
        card.plan_id,
        subsets=set(subsets),
        purpose="measure",
        unseal_reason=spec.reason if spec.unseal else None,
        caller=CALLER,
        run_id=card.run_id,
        data_root=paths.data_root,
        configs_root=paths.configs_root,
    ) as access:
        for subset in subsets:
            samples = list(access.records(subset).values())
            path = verify_prediction(paths.data_root, card, subset)
            preds = predictions_by_id(read_predictions(path))
            for name in metric_names:
                cell = _measure_one(ctx, subset, samples, preds, get_metric(name))
                readings.append(cell.reading)
                states.append("OK" if cell.anchored else "none")
                if not cell.anchored:
                    warnings.append(
                        f"no anchor for {cell.key}; run `vcp eval anchor` once a reference run "
                        "exists"
                    )
                if cell.is_new:
                    pending.append(cell.reading)
                else:
                    cached += 1
    for reading in pending:  # only now that every guardrail has passed (spec 9)
        ctx.ledger.append(reading)
    return MeasureResult(
        run_id=card.run_id,
        dataset=card.dataset,
        readings=readings,
        new=len(pending),
        cached=cached,
        guardrail=_guardrail_state(states, wrote=bool(pending)),
        warnings=warnings,
        provenance=info.grade,
        observed=info.observed,
        receipt_invalid=len(info.invalid),
    )
