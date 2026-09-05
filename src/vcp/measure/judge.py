"""vcp eval judge: the decision (spec 6.2).

The pre-registered baseline and candidate, on every pre-registered subset, compared by a
paired bootstrap; a subset counts as a base only if the candidate is genuinely ahead on it
(``t >= t_min``); enough independent bases must agree; and a tuning-class component must also
clear sigma_p, the spread two equivalent eval sets put between the same run. Never max-of-N:
the two runs were named before the candidate was measured, and this module only ever reads
the pair the pre-registration names.

The verdict is data, not a tool status: a claim that fails is a fact about the claim, so the
command still ends ``status=OK verdict=FAIL``. Only a structural problem -- a mangled file, a
plan that does not match, a metric no registry knows -- is an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.measure.ledger import ReadingsLedger, append_row
from vcp.measure.measure import load_context
from vcp.measure.metrics import Metric, effective_params, get_metric, params_key
from vcp.measure.predictions import predictions_by_id, read_predictions
from vcp.measure.prereg import load_prereg, prereg_time
from vcp.measure.runs import verify_prediction
from vcp.measure.schema import (
    Judgement,
    PreRegistration,
    Reading,
    SigmaRef,
    SubsetJudgement,
)
from vcp.measure.sigma import latest_sigma
from vcp.measure.stats import paired_bootstrap

JUDGEMENTS_LEDGER = "judgements.jsonl"
READINGS_LEDGER = "readings.jsonl"
TUNING_CLASS = "tuning"

# A difference with no spread at all is not infinite certainty, but no jsonl ledger can hold
# an inf (pydantic writes it as JSON null, which then fails to load back), so t saturates.
T_CAP = 1e9

# The reasons vocabulary, in one place: these strings are the machine-readable half of a
# judgement row, and anything reading judgements.jsonl matches on them.
MISSING_READINGS = "missing_readings"
MEASURED_BEFORE_PREREG = "measured_before_prereg"
TOO_FEW_BASES = "bases_positive"
NO_SIGMA = "no_sigma"
SIGMA_ZERO = "sigma_zero"
BELOW_SIGMA_P = "below_sigma_p"


class JudgeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    prereg_id: str
    # Same two guards as SigmaSpec: fewer than two draws leaves no spread to measure, and a
    # negative seed reaches numpy's default_rng raw (a bare ValueError, ABORT instead of FAIL).
    # The CLI wraps construction so both arrive as a FAIL the user can act on.
    resamples: int = Field(default=200, ge=2)
    seed: int = Field(default=0, ge=0)
    data_root: Path | None = None
    configs_root: Path | None = None


@dataclass
class _Reasons:
    """What the judgement records, and which of it forbids a PASS.

    Not every reason blocks: a sigma_p of exactly 0 makes the sigma_p condition vacuously
    true, so the row must say so without changing the verdict (ruling 0b).
    """

    all: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)

    def block(self, reason: str) -> None:
        self.all.append(reason)
        self.blocking.append(reason)

    def note(self, reason: str) -> None:
        self.all.append(reason)


def _latest(rows: list[Reading], run_id: str, subset: str, metric: str, pk: str) -> Reading | None:
    """This run's newest reading of one (subset, metric, params) cell, or ``None``."""
    hits = [
        r
        for r in rows
        if r.run_id == run_id
        and r.subset == subset
        and r.metric == metric
        and params_key(r.params) == pk
    ]
    return max(hits, key=lambda r: r.ts) if hits else None


def _t(delta: float, se: float) -> float:
    """spec 6.2 step 2, with inf spelled as the cap: no difference and no spread is t = 0."""
    if se > 0:
        return delta / se
    return 0.0 if delta == 0 else (T_CAP if delta > 0 else -T_CAP)


def _pairs(
    rows: list[Reading], pr: PreRegistration, pk: str
) -> tuple[dict[str, tuple[Reading, Reading]], list[str]]:
    """spec 6.2 step 1: both runs' latest reading on every pre-registered subset.

    A subset that is missing either one is reported rather than quietly dropped -- judging a
    claim on the subsets that happen to have readings is exactly the selection this framework
    exists to prevent.
    """
    pairs: dict[str, tuple[Reading, Reading]] = {}
    missing: list[str] = []
    for subset in pr.subsets:
        a = _latest(rows, pr.baseline_run, subset, pr.metric, pk)
        b = _latest(rows, pr.candidate_run, subset, pr.metric, pk)
        if a is None or b is None:
            absent = [name for name, r in (("baseline", a), ("candidate", b)) if r is None]
            missing.append(f"{MISSING_READINGS}: {subset} {absent}")
            continue
        pairs[subset] = (a, b)
    return pairs, missing


def _assert_one_plan(pr: PreRegistration, pairs: dict[str, tuple[Reading, Reading]]) -> None:
    """Every reading must have been taken under the same plan. A subset name means different
    samples under each, so two plans would score one run's predictions against the other's
    samples -- and the arithmetic underneath would still produce a number."""
    plan_ids = sorted({r.plan_id for pair in pairs.values() for r in pair})
    if len(plan_ids) > 1:
        raise PlanMismatchError(
            f"pre-registration {pr.prereg_id!r} compares readings taken under different plans "
            f"{plan_ids}; a subset name means different samples under each"
        )


def _per_subset(
    spec: JudgeSpec,
    pr: PreRegistration,
    pairs: dict[str, tuple[Reading, Reading]],
    metric: Metric,
    params: dict[str, str],
) -> dict[str, SubsetJudgement]:
    """spec 6.2 step 2: one paired bootstrap per subset, in the metric's own direction.

    ``sign`` is applied ONCE, here, so that every later comparison -- ``delta > 0``, ``t``,
    the sigma_p condition -- keeps its "positive means better" meaning for a lower-is-better
    metric (rmse, mae, log_loss) exactly as for a higher-is-better one (ruling 0).
    """
    card_b, dataset, plan, run_paths = load_context(
        pr.candidate_run, spec.data_root, spec.configs_root
    )
    card_a, _, _, _ = load_context(pr.baseline_run, spec.data_root, spec.configs_root)
    sign = 1.0 if metric.higher_is_better else -1.0
    out: dict[str, SubsetJudgement] = {}
    for subset, (a, b) in pairs.items():
        samples = dataset.subset(subset, plan, paths=run_paths)
        root = run_paths.data_root
        preds_a = predictions_by_id(read_predictions(verify_prediction(root, card_a, subset)))
        preds_b = predictions_by_id(read_predictions(verify_prediction(root, card_b, subset)))
        raw, se, _ = paired_bootstrap(
            samples,
            preds_a,
            preds_b,
            metric,
            dataset.card,
            params,
            resamples=spec.resamples,
            seed=spec.seed,
        )
        delta = sign * raw
        out[subset] = SubsetJudgement(
            baseline=a.value, candidate=b.value, delta=delta, se=se, t=_t(delta, se), n=len(samples)
        )
    return out


def _sigma_condition(
    paths: DatasetPaths,
    pr: PreRegistration,
    plan_id: str,
    pk: str,
    per_subset: dict[str, SubsetJudgement],
    reasons: _Reasons,
) -> SigmaRef | None:
    """spec 6.2 steps 4-5 for a tuning-class component: the mean improvement must clear
    ``sigma_ratio`` x sigma_p, the spread two equivalent eval sets put between the same run.

    Without an estimate there is nothing to clear, so the claim cannot pass at all.
    """
    est = latest_sigma(paths, plan_id, pr.metric, pk, pr.sigma_method)
    if est is None:
        reasons.block(NO_SIGMA)
        return None
    if est.value == 0.0:
        # The condition is vacuously true. A row that only said PASS would hide that the bar
        # this claim cleared was no bar at all (ruling 0b).
        reasons.note(SIGMA_ZERO)
    mean_delta = float(np.mean([s.delta for s in per_subset.values()]))
    if mean_delta < pr.sigma_ratio * est.value:
        reasons.block(
            f"{BELOW_SIGMA_P}: mean delta {mean_delta!r} < sigma_ratio {pr.sigma_ratio} "
            f"x sigma_p {est.value!r}"
        )
    return SigmaRef(method=est.method, value=est.value, estimate_id=est.estimate_id)


def judge_prereg(spec: JudgeSpec) -> Judgement:
    """Decide one pre-registered claim and append the judgement (spec 6.2, one clause each)."""
    paths = DatasetPaths.resolve(
        spec.dataset, data_root=spec.data_root, configs_root=spec.configs_root
    )
    pr = load_prereg(paths, spec.prereg_id)
    logged_at = prereg_time(paths, spec.prereg_id)
    if logged_at is None:
        raise ValidationFailed(f"pre-registration {spec.prereg_id!r} is not in {paths.prereg_log}")
    if not pr.subsets:
        raise ValidationFailed(f"pre-registration {spec.prereg_id!r} names no subsets to judge on")
    metric = get_metric(pr.metric)
    # ruling 2: a hand-written prereg committed to git is the point, so the judge normalises
    # the claim's params itself instead of trusting create_prereg to have done it.
    params = effective_params(metric, pr.params)
    pk = params_key(params)
    reasons = _Reasons()
    pairs, missing = _pairs(ReadingsLedger(paths.measure_dir / READINGS_LEDGER).rows, pr, pk)
    for reason in missing:  # step 1: no readings, nothing to decide
        reasons.block(reason)
    _assert_one_plan(pr, pairs)

    verdict = "FAIL"
    per_subset: dict[str, SubsetJudgement] = {}
    sigma_ref: SigmaRef | None = None
    if any(b.ts < logged_at for _, b in pairs.values()):  # step 1: the answer came first
        verdict = "INVALID"
        reasons.block(MEASURED_BEFORE_PREREG)
    elif not reasons.blocking:  # step 2
        per_subset = _per_subset(spec, pr, pairs, metric, params)
    # step 3: a base is a subset where the candidate is ahead AND the bootstrap says so
    bases_positive = sum(1 for s in per_subset.values() if s.delta > 0 and s.t >= pr.t_min)
    if verdict != "INVALID" and not reasons.blocking:
        if bases_positive < pr.min_bases:
            reasons.block(f"{TOO_FEW_BASES} {bases_positive} < {pr.min_bases}")
        if pr.component_class == TUNING_CLASS:  # steps 4-5
            # No blocking reason means every subset paired, so there is a reading to ask.
            plan_id = next(iter(pairs.values()))[0].plan_id
            sigma_ref = _sigma_condition(paths, pr, plan_id, pk, per_subset, reasons)
        if not reasons.blocking:
            verdict = "PASS"
    judgement = Judgement(  # step 6
        prereg_id=pr.prereg_id,
        ts=stamp(),
        baseline_run=pr.baseline_run,
        candidate_run=pr.candidate_run,
        metric=pr.metric,
        params=params,
        higher_is_better=metric.higher_is_better,
        per_subset=per_subset,
        bases_positive=bases_positive,
        sigma_p=sigma_ref,
        verdict=verdict,
        reasons=reasons.all,
        reading_ids=[r.reading_id for pair in pairs.values() for r in pair],
        bootstrap={"resamples": spec.resamples, "seed": spec.seed},
    )
    append_row(paths.measure_dir / JUDGEMENTS_LEDGER, judgement)
    return judgement
