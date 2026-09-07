import pytest

from helpers import noisy_predictions
from submit_fixtures import STAMP, ingest_run, random_scores, seed_eval_runs, seed_judgements
from vcp.core.config import dump_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.fuse.schema import FuseRecord, MemberRecord
from vcp.measure.ledger import JUDGEMENTS_LEDGER, append_row
from vcp.measure.prereg import prereg_path
from vcp.measure.runs import load_run
from vcp.measure.schema import Judgement, PreRegistration
from vcp.submit.gate import admit, latest_judgements


def test_candidate_needs_a_pass_judgement(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    good = load_run(pair.roots.data, "good")
    gate = admit(pair.roots.data, pair.eval_paths, good, None, "candidate", None)
    assert gate.admission == "PASS" and gate.judgements == ["p-good"] and gate.reason == ""
    bad = load_run(pair.roots.data, "bad")
    with pytest.raises(ValidationFailed, match="not_admitted") as ei:
        admit(pair.roots.data, pair.eval_paths, bad, None, "candidate", None)
    assert ei.value.fields == {"run": "bad"}
    assert set(latest_judgements(pair.eval_paths)) == {"p-good", "p-bad"}


@pytest.mark.parametrize("verdicts", [("PASS", "FAIL"), ("FAIL", "PASS")])
def test_same_prereg_uses_its_latest_judgement(pair, verdicts):
    seed_eval_runs(pair)
    ledger = pair.eval_paths.measure_dir / JUDGEMENTS_LEDGER
    for verdict in verdicts:
        append_row(ledger, _judgement("p", "good", verdict))
    good = load_run(pair.roots.data, "good")
    if verdicts[-1] == "FAIL":
        with pytest.raises(ValidationFailed, match="not_admitted"):
            admit(pair.roots.data, pair.eval_paths, good, None, "candidate", None)
    else:
        gate = admit(pair.roots.data, pair.eval_paths, good, None, "candidate", None)
        assert gate.admission == "PASS" and gate.judgements == ["p"]


def test_waived_kinds_need_a_reason(pair):
    seed_eval_runs(pair)
    bad = load_run(pair.roots.data, "bad")
    gate = admit(pair.roots.data, pair.eval_paths, bad, None, "baseline", "first anchor")
    assert gate.admission == "waived" and gate.reason == "first anchor"
    with pytest.raises(ValidationFailed, match="reason_required"):
        admit(pair.roots.data, pair.eval_paths, bad, None, "probe", None)


def test_replaced_predictions_make_the_judgement_stale(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    ds, plan = pair.eval_ds, pair.eval_plan
    samples = ds.subset("valA", plan, paths=pair.eval_paths)
    # Deviation from the brief (see task-7-report.md): cls predictions are re-sorted by
    # sample_id before hashing, so merely reordering a full sample list reproduces the same
    # sha256, and cls requires every sample present so dropping one is rejected by
    # check_predictions before admit() is ever reached. noisy_predictions keeps full coverage
    # while guaranteeing different content, which is what "stale judgement" needs.
    shuffled = noisy_predictions(samples, ds.card, seed=42, flip=1.0)
    ingest_run(
        pair,
        "good",
        ds,
        "fixed-v1",
        "valA",
        shuffled,
        weights=pair.weights["good"],
        trained_on=("train",),
        replace=True,
    )
    good = load_run(pair.roots.data, "good")
    with pytest.raises(ValidationFailed, match="stale_judgement") as ei:
        admit(pair.roots.data, pair.eval_paths, good, None, "candidate", None)
    assert ei.value.fields == {"prereg": "p-good"}


def _judgement(pid, candidate, verdict):
    return Judgement(
        prereg_id=pid,
        ts=STAMP,
        baseline_run="base",
        candidate_run=candidate,
        metric="accuracy",
        metric_version="1",
        params={},
        higher_is_better=True,
        per_subset={},
        bases_positive=2,
        sigma_p=None,
        verdict=verdict,
        reasons=[],
        reading_ids=[],
        bootstrap={"resamples": 50, "seed": 0},
    )


def _prereg(pair, pid, component, candidate):
    pr = PreRegistration(
        prereg_id=pid,
        claim="x",
        component=component,
        component_class="model",
        baseline_run="base",
        candidate_run=candidate,
        metric="accuracy",
        subsets=["valA", "valB"],
        created_at=STAMP,
    )
    dump_yaml_model(pr, prereg_path(pair.eval_paths, pid))


def test_fusion_candidate_needs_every_member_admitted(pair):
    seed_eval_runs(pair)
    ingest_run(
        pair,
        "fused",
        pair.eval_ds,
        "fixed-v1",
        "valA",
        random_scores(pair.eval_ds.subset("valA", pair.eval_plan, paths=pair.eval_paths), seed=5),
        weights=None,
        trained_on=("train",),
    )
    fuse = FuseRecord(
        run_id="fused",
        recipe_id="r",
        recipe_sha256="s" * 64,
        method="mean",
        method_version="1",
        params={},
        members=[
            MemberRecord(run="good", weight=1.0, trained_on=["train"]),
            MemberRecord(run="bad", weight=1.0, trained_on=["train"]),
        ],
        vcp_version="0",
    )
    ledger = pair.eval_paths.measure_dir / JUDGEMENTS_LEDGER
    _prereg(pair, "r-admit-good", "good", "fused")
    append_row(ledger, _judgement("r-admit-good", "fused", "PASS"))
    fused = load_run(pair.roots.data, "fused")
    with pytest.raises(ValidationFailed, match="member_not_admitted") as ei:
        admit(pair.roots.data, pair.eval_paths, fused, fuse, "candidate", None)
    assert ei.value.fields == {"member": "bad"}
    _prereg(pair, "r-admit-bad", "bad", "fused")
    append_row(ledger, _judgement("r-admit-bad", "fused", "FAIL"))
    with pytest.raises(ValidationFailed, match="member_not_admitted"):
        admit(pair.roots.data, pair.eval_paths, fused, fuse, "candidate", None)
    append_row(ledger, _judgement("r-admit-bad", "fused", "PASS"))
    gate = admit(pair.roots.data, pair.eval_paths, fused, fuse, "candidate", None)
    assert gate.judgements == ["r-admit-good", "r-admit-bad"]
