"""sigma_p: how much a reading can move between two "equivalent" evaluation sets.

It is the number every claimed improvement is compared against, so it is estimated three ways
and each estimate is filed, with its inputs, in an append-only ledger: ``splithalf`` (how far
apart two eval halves put the same runs), ``bootstrap`` (the sampling-noise floor under one
run's subset) and ``prior`` (a number carried in from outside, with its source).

sigma_p is a magnitude -- a spread, not a direction -- so it is the same number for a
lower-is-better metric as for a higher-is-better one, and nothing here consults
``Metric.higher_is_better``. Turning a signed delta into "better" or "worse" is the judge's
job; this module only says how big a move is unremarkable.

The three methods are dispatched by name through ``SIGMA_ESTIMATORS``: a fourth is one entry.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_json
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.split import SplitPlan, load_plan
from vcp.measure.anchors import anchor_key, load_anchors
from vcp.measure.ledger import ReadingsLedger, append_row, read_rows
from vcp.measure.measure import load_context
from vcp.measure.metrics import effective_params, get_metric, params_key
from vcp.measure.predictions import predictions_by_id, read_predictions
from vcp.measure.runs import verify_prediction
from vcp.measure.schema import Reading, SigmaEstimate
from vcp.measure.stats import bootstrap_sd, sample_sd

SIGMA_LEDGER = "sigma.jsonl"
READINGS_LEDGER = "readings.jsonl"
SPLITHALF_SUBSETS = 2
SPLITHALF_MIN_RUNS = 3


class SigmaSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    plan_id: str
    metric: str
    params: dict[str, str] = Field(default_factory=dict)
    method: str
    subsets: list[str] = Field(default_factory=list)
    run_id: str | None = None
    prior: float | None = None
    note: str = ""
    # Fewer than two draws leaves no spread to measure (ruling 1). The CLI wraps construction
    # so this arrives as a FAIL the user can act on, not a pydantic error escaping as an ABORT.
    resamples: int = Field(default=200, ge=2)
    # A negative seed reaches numpy's default_rng raw otherwise (I1): a bare ValueError, ABORT
    # instead of FAIL. Same CLI-wrapping story as resamples above.
    seed: int = Field(default=0, ge=0)
    data_root: Path | None = None
    configs_root: Path | None = None


class SigmaResult(BaseModel):
    """One estimate, and whether it was already in the ledger rather than newly appended."""

    estimate: SigmaEstimate
    cached: bool


@dataclass(frozen=True)
class _Inputs:
    """What every estimator is handed: the spec, and what it resolves to."""

    spec: SigmaSpec
    paths: DatasetPaths
    plan: SplitPlan
    params: dict[str, str]
    pk: str


Estimator = Callable[[_Inputs], tuple[float, dict[str, Any]]]


def estimate_id(row: SigmaEstimate) -> str:
    """The identity of an estimate: the row minus the clock and the id itself.

    Derived the way ``reading_id`` is -- from what the estimate IS, never from when it was
    taken -- so re-running the same estimate is recognised as the same row instead of
    appending a duplicate to an append-only ledger.
    """
    return sha256_json(row.model_dump(mode="json", exclude={"estimate_id", "ts"}))


def latest_sigma(
    paths: DatasetPaths, plan_id: str, metric: str, pk: str, method: str
) -> SigmaEstimate | None:
    """The newest estimate for this plan / metric / params / method, or ``None``.

    ``pk`` is a ``params_key`` string, so a caller holding raw params normalises them through
    ``effective_params`` first. Stamps have millisecond precision and two estimates can land in
    the same one, so a tie goes to the row appended last: that is what "latest" means in a
    ledger that is only ever appended to.
    """
    latest: SigmaEstimate | None = None
    for row in read_rows(paths.measure_dir / SIGMA_LEDGER, SigmaEstimate):
        if (
            row.plan_id == plan_id
            and row.metric == metric
            and row.method == method
            and params_key(row.params) == pk
            and (latest is None or row.ts >= latest.ts)
        ):
            latest = row
    return latest


def _eval_subsets(plan: SplitPlan) -> list[str]:
    return [s.name for s in plan.subsets if s.role == "eval"]


def _first_eval_subset(plan: SplitPlan) -> str:
    names = _eval_subsets(plan)
    if not names:
        raise ValidationFailed(f"plan {plan.plan_id!r} has no eval subset to resample")
    return names[0]


def _latest_per_run(rows: list[Reading], subset: str) -> dict[str, Reading]:
    """This subset's newest reading per run. Sorting is stable, so rows sharing a timestamp
    resolve to the one appended last."""
    out: dict[str, Reading] = {}
    for r in sorted(rows, key=lambda r: r.ts):
        if r.subset == subset:
            out[r.run_id] = r
    return out


def _splithalf(ctx: _Inputs) -> tuple[float, dict[str, Any]]:
    """spec 6.3: over every run with a reading on both halves, d_r = value(A) - value(B), and
    sigma_p = std(d_r, ddof=1) / sqrt(2) -- the spread of ONE half, since d is the difference
    of two of them. This is the honest answer to "how far apart do two equivalent eval sets put
    the same run", which is exactly what a public-to-private shift is.
    """
    subsets = ctx.spec.subsets or _eval_subsets(ctx.plan)[:SPLITHALF_SUBSETS]
    if len(subsets) != SPLITHALF_SUBSETS:
        raise ValidationFailed(f"splithalf needs exactly two eval subsets, got {subsets}")
    if subsets[0] == subsets[1]:
        # Both halves would be the same mapping and every diff exactly 0 -- a sigma_p of 0 that
        # is indistinguishable in the ledger from a genuinely noiseless pair (I2).
        raise ValidationFailed(f"splithalf needs two different eval subsets, got {subsets}")
    rows = [
        r
        for r in ReadingsLedger(ctx.paths.measure_dir / READINGS_LEDGER).rows
        if r.plan_id == ctx.spec.plan_id
        and r.metric == ctx.spec.metric
        and params_key(r.params) == ctx.pk
    ]
    a, b = _latest_per_run(rows, subsets[0]), _latest_per_run(rows, subsets[1])
    runs = sorted(set(a) & set(b))
    if len(runs) < SPLITHALF_MIN_RUNS:
        raise ValidationFailed(
            f"splithalf needs at least {SPLITHALF_MIN_RUNS} runs with readings on both "
            f"{subsets}; found {len(runs)}"
        )
    diffs = [a[r].value - b[r].value for r in runs]
    inputs = {
        "subsets": subsets,
        "runs": runs,
        "reading_ids": [a[r].reading_id for r in runs] + [b[r].reading_id for r in runs],
        "diffs": diffs,
    }
    return sample_sd(diffs) / math.sqrt(2), inputs


def _check_eval_role(plan: SplitPlan, subset: str) -> None:
    """bootstrap on a train or sealed subset would report training noise, or an unaudited
    unseal's, as sigma_p. The default subset path already only ever offers an eval subset
    (``_first_eval_subset``); an explicit ``--subsets`` must be held to the same rule."""
    role = next((s.role for s in plan.subsets if s.name == subset), None)
    if role is not None and role != "eval":
        raise ValidationFailed(f"bootstrap needs an eval subset, but {subset!r} has role {role!r}")


def _bootstrap(ctx: _Inputs) -> tuple[float, dict[str, Any]]:
    """spec 6.3: resample one run's subset and take the standard deviation of the metric --
    the sampling-noise floor under a reading, and a lower bound on sigma_p rather than a
    substitute for it (it says nothing about how two different eval sets differ).

    ``--run`` defaults to the anchor run for this (plan, subset, metric, params) when omitted
    (spec 6.3's "對 --run，預設錨點 run").
    """
    spec = ctx.spec
    subset = spec.subsets[0] if spec.subsets else _first_eval_subset(ctx.plan)
    _check_eval_role(ctx.plan, subset)
    run_id = spec.run_id
    from_anchor = False
    if not run_id:
        key = anchor_key(spec.plan_id, subset, spec.metric, ctx.pk)
        anchor = load_anchors(ctx.paths).get(key)
        if anchor is None:
            raise ValidationFailed(f"bootstrap needs --run <run_id> or an anchor for {key}")
        run_id = anchor.run_id
        from_anchor = True
    card, dataset, _, _ = load_context(run_id, spec.data_root, spec.configs_root)
    if card.dataset != spec.dataset or card.plan_id != spec.plan_id:
        # The estimate is filed under spec.dataset / spec.plan_id and the judge looks it up by
        # them, so resampling a run made elsewhere would file that run's noise under this plan.
        raise ValidationFailed(
            f"run {run_id!r} was made on {card.dataset}/{card.plan_id}, but this estimate "
            f"is for {spec.dataset}/{spec.plan_id}"
        )
    samples = dataset.subset(subset, ctx.plan, paths=ctx.paths)
    path = verify_prediction(ctx.paths.data_root, card, subset)
    value = bootstrap_sd(
        samples,
        predictions_by_id(read_predictions(path)),
        get_metric(spec.metric),
        dataset.card,
        ctx.params,
        resamples=spec.resamples,
        seed=spec.seed,
    )
    inputs: dict[str, Any] = {
        "run_id": run_id,
        "subset": subset,
        "resamples": spec.resamples,
        "seed": spec.seed,
    }
    if from_anchor:
        inputs["run_source"] = "anchor"
    return value, inputs


def _prior(ctx: _Inputs) -> tuple[float, dict[str, Any]]:
    """spec 6.3: a number carried in from outside this dataset -- say the public-to-private
    shift of past contests on the same platform. Where it came from is the only thing that
    makes it auditable, so ``--note`` is not optional."""
    spec = ctx.spec
    if spec.prior is None or not spec.note.strip():
        raise ValidationFailed("prior needs --prior <value> and --note <where it comes from>")
    return float(spec.prior), {"note": spec.note}


SIGMA_ESTIMATORS: dict[str, Estimator] = {
    "splithalf": _splithalf,
    "bootstrap": _bootstrap,
    "prior": _prior,
}
SIGMA_METHODS: tuple[str, ...] = tuple(SIGMA_ESTIMATORS)


def _check_magnitude(value: float, method: str) -> None:
    """sigma_p is a magnitude. A nan/inf would be written to the ledger as JSON null and fail
    to load back (the readings ledger refuses one the same way, and neither ledger can ever
    drop a poisoned row), and a negative one would let every candidate clear the judge's
    ``mean_delta >= sigma_ratio * sigma_p`` bar."""
    if not math.isfinite(value) or value < 0:
        raise ValidationFailed(
            f"sigma_p from method {method!r} must be a finite, non-negative magnitude, "
            f"got {value!r}; refusing to append it"
        )


def estimate_sigma_result(spec: SigmaSpec) -> SigmaResult:
    """Estimate sigma_p and append it to ``measure/<dataset>/sigma.jsonl``.

    Unless that exact estimate is already there: the ledger is append-only, so the same
    estimate is one row for ever and re-running it hands back the stored one.
    """
    estimator = SIGMA_ESTIMATORS.get(spec.method)
    if estimator is None:
        raise ValidationFailed(f"--method must be one of {SIGMA_METHODS}, got {spec.method!r}")
    paths = DatasetPaths.resolve(
        spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    plan = load_plan(paths, spec.plan_id)
    metric = get_metric(spec.metric)
    params = effective_params(metric, spec.params)
    value, inputs = estimator(
        _Inputs(spec=spec, paths=paths, plan=plan, params=params, pk=params_key(params))
    )
    _check_magnitude(value, spec.method)
    draft = SigmaEstimate(
        estimate_id="",
        ts=stamp(),
        plan_id=spec.plan_id,
        metric=spec.metric,
        params=params,
        method=spec.method,
        value=value,
        inputs=inputs,
        note=spec.note,
    )
    est = draft.model_copy(update={"estimate_id": estimate_id(draft)})
    path = paths.measure_dir / SIGMA_LEDGER
    for row in read_rows(path, SigmaEstimate):
        if row.estimate_id == est.estimate_id:
            return SigmaResult(estimate=row, cached=True)
    append_row(path, est)
    return SigmaResult(estimate=est, cached=False)


def estimate_sigma(spec: SigmaSpec) -> SigmaEstimate:
    """The estimate alone; ``estimate_sigma_result`` also says whether it was already stored."""
    return estimate_sigma_result(spec).estimate
