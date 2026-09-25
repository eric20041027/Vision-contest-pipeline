"""Kernel submissions may only load the weights that were judged (VCP-036)."""

from types import SimpleNamespace

import pytest

from backup_fixtures import make_fusion
from submit_fixtures import EVAL, seed_eval_runs
from vcp.core.errors import ValidationFailed
from vcp.measure.runs import load_run, save_run
from vcp.submit.kernel import candidate_runs, check_weights, kernel_provenance
from vcp.submit.schema import WeightRef


def _seeded(pair):
    seed_eval_runs(pair)
    return pair


def _ref(pair, run: str) -> WeightRef:
    return WeightRef(run=run, sha256=load_run(pair.roots.data, run).source.weights_hash)


def _check(pair, eval_run: str, runs: list[str], kind: str, require: str = "declared"):
    return check_weights(
        data_root=pair.roots.data,
        configs_root=pair.roots.configs,
        eval_run=eval_run,
        weights=[_ref(pair, run) for run in runs],
        kind=kind,
        sealed_subset="holdout",
        require_provenance=require,
    )


def _sealed_trained(pair, run: str) -> None:
    card = load_run(pair.roots.data, run)
    save_run(
        pair.roots.data,
        card.model_copy(update={"trained_on": ["holdout", "train", "valA", "valB"]}),
    )


def test_candidate_runs_are_the_eval_run_and_its_fusion_members(pair):
    _seeded(pair)
    fused = make_fusion(SimpleNamespace(roots=pair.roots, pair=pair))

    assert candidate_runs(pair.roots.data, "good") == {"good"}
    assert candidate_runs(pair.roots.data, fused) == {fused, "good", "bad"}


def test_a_candidate_may_load_only_the_judged_weights(pair):
    _seeded(pair)

    with pytest.raises(ValidationFailed, match="^weights_not_in_candidate: run 'bad'") as ei:
        _check(pair, "good", ["good", "bad"], "candidate")
    assert ei.value.fields == {"run": "bad"}


def test_a_fusion_candidate_may_load_its_members(pair):
    _seeded(pair)
    fused = make_fusion(SimpleNamespace(roots=pair.roots, pair=pair))

    result = _check(pair, fused, ["good", "bad"], "candidate")

    assert result.notes == [] and result.grade == "declared"


def test_every_candidate_weights_run_passes_the_sealed_checks(pair):
    _seeded(pair)
    fused = make_fusion(SimpleNamespace(roots=pair.roots, pair=pair))
    _sealed_trained(pair, "bad")

    with pytest.raises(ValidationFailed, match="^trained_on_sealed: weights run 'bad'") as ei:
        _check(pair, fused, ["good", "bad"], "candidate")
    assert ei.value.fields == {"run": "bad"}


def test_a_candidate_weights_run_below_the_required_provenance_is_refused(pair):
    _seeded(pair)

    with pytest.raises(ValidationFailed, match="^provenance_required: weights run 'good'"):
        _check(pair, "good", ["good"], "candidate", require="receipt")


def test_a_baseline_is_held_to_membership_only(pair):
    _seeded(pair)
    _sealed_trained(pair, "good")

    with pytest.raises(ValidationFailed, match="^weights_not_in_candidate"):
        _check(pair, "good", ["good", "bad"], "baseline")
    result = _check(pair, "good", ["good"], "baseline")
    assert result.notes == ["weights:good=trained_on_sealed"]


def test_a_probe_is_exempt_but_every_finding_is_written_down(pair):
    _seeded(pair)
    _sealed_trained(pair, "bad")

    result = _check(pair, "good", ["good", "bad"], "probe", require="receipt")

    assert result.notes == [
        "weights:good=provenance_declared",
        "weights:bad=not_in_candidate",
        "weights:bad=trained_on_sealed",
        "weights:bad=provenance_declared",
    ]
    assert result.grade == "declared"


def _peek_at_holdout(pair, run: str) -> None:
    """A receipt that read the sealed subset, attached to ``run``."""
    from vcp.data.access.access import DatasetAccess
    from vcp.measure.provenance import attach_receipts

    with DatasetAccess.open(
        EVAL,
        "fixed-v1",
        subsets={"holdout"},
        purpose="custom",
        run_id=run,
        unseal_reason="peek",
        caller="t",
        data_root=pair.roots.data,
        configs_root=pair.roots.configs,
    ) as access:
        list(access.iter("holdout"))
    card = attach_receipts(
        load_run(pair.roots.data, run), [access.receipt_id], data_root=pair.roots.data
    )
    save_run(pair.roots.data, card)


def test_kernel_provenance_folds_every_weights_run_in(pair):
    """What `submit final` re-reads for a kernel submission: the lowest grade and every subset
    read, across the eval run and all the runs whose weights the notebook loads."""
    _seeded(pair)
    _peek_at_holdout(pair, "bad")

    grade, observed = kernel_provenance(
        data_root=pair.roots.data,
        configs_root=pair.roots.configs,
        grade="receipt",
        observed=["train"],
        weights=[_ref(pair, "good"), _ref(pair, "bad")],
    )

    assert grade == "declared"
    assert observed == {"train", "holdout"}


def test_a_weights_run_below_the_bar_names_its_grade(pair):
    _seeded(pair)

    with pytest.raises(ValidationFailed, match="^provenance_required") as ei:
        _check(pair, "good", ["good"], "candidate", require="receipt")
    assert ei.value.fields == {"run": "good", "provenance": "declared"}
