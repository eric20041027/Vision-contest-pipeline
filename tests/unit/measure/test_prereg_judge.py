import json
import time

import pytest
import yaml

from helpers import (
    REG_CATS,
    det_with_runs,
    make_card,
    noisy_predictions,
    perfect_predictions,
    regression_samples,
    write_images,
)
from vcp.core.errors import PlanMismatchError, RegistryError, SealedSubsetError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.judge import T_CAP, JudgeSpec, _append_judgement, _t, judge_prereg
from vcp.measure.ledger import ReadingsLedger, read_rows
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.measure.metrics import METRICS, register_metric
from vcp.measure.predictions import write_predictions
from vcp.measure.prereg import create_prereg, list_preregs, load_prereg, prereg_time
from vcp.measure.schema import Judgement, MetricResult, PreRegistration, SubsetJudgement
from vcp.measure.sigma import SigmaSpec, estimate_sigma

COCO_PARAMS = {"iou": "50:95", "max_dets": "100"}
CREATED_AT = "2026-09-04T00:00:00.000Z"


def _pr(**kw):
    base = dict(
        prereg_id="p001",
        claim="noisy is worse",
        component="noise",
        component_class="model",
        baseline_run="perfect",
        candidate_run="noisy",
        metric="coco_map",
        subsets=["valA", "valB"],
        created_at=CREATED_AT,
    )
    return PreRegistration(**{**base, **kw})


def _ledger(paths):
    return ReadingsLedger(paths.measure_dir / "readings.jsonl")


def _measure(roots, run, **kw):
    return measure_run(
        MeasureSpec(run_id=run, data_root=roots.data, configs_root=roots.configs, **kw)
    )


def _judge(roots, pid="p001", dataset="tiny", **kw):
    opts = {"resamples": 30, "seed": 0, **kw}
    return judge_prereg(
        JudgeSpec(
            dataset=dataset,
            prereg_id=pid,
            data_root=roots.data,
            configs_root=roots.configs,
            **opts,
        )
    )


def _sigma(roots, prior, note, *, metric="coco_map"):
    return estimate_sigma(
        SigmaSpec(
            dataset="tiny",
            plan_id="fixed-v1",
            metric=metric,
            method="prior",
            prior=prior,
            note=note,
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )


def _log_prereg(paths, prereg_id, ts):
    """Append a log line by hand -- the only way to forge the moment a claim became binding."""
    paths.prereg_log.parent.mkdir(parents=True, exist_ok=True)
    with paths.prereg_log.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"prereg_id": prereg_id, "sha256": "0" * 64, "ts": ts}) + "\n")


def _hand_write(paths, pr, *, ts=None):
    """A pre-registration as it arrives from git: written by hand, never via create_prereg."""
    path = paths.prereg_dir / f"{pr.prereg_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(pr.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    _log_prereg(paths, pr.prereg_id, ts or stamp())
    return path


def _regression_with_runs(roots, tmp_path, *, n=120):
    """A regression dataset with a 'worse' baseline and a 'better' candidate, both ingested.

    Built the way ``helpers.det_with_runs`` builds the det one, but for a lower-is-better
    metric: on ``rmse`` the better run is the one with the SMALLER value.
    """
    paths = DatasetPaths.resolve("reg", data_root=roots.data, configs_root=roots.configs)
    samples = regression_samples(n, seed=0)
    write_images(roots.data / "raw" / "reg", samples)
    card = make_card("regression", name="reg", categories=REG_CATS, image_root="raw/reg")
    ds = Dataset.from_parts(card, samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    for run_id, maker in (("better", perfect_predictions), ("worse", noisy_predictions)):
        for subset in ("valA", "valB"):
            sub = ds.subset(subset, plan, paths=paths)
            src = tmp_path / f"{run_id}-{subset}.jsonl"
            write_predictions(src, maker(sub, ds.card))
            ingest(
                IngestSpec(
                    run_id=run_id,
                    dataset="reg",
                    plan_id="fixed-v1",
                    subset=subset,
                    format="jsonl",
                    src=src,
                    trained_on=["train"],
                    data_root=roots.data,
                    configs_root=roots.configs,
                )
            )
    return ds, plan, paths


def test_create_prereg_refuses_measured_candidate(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    _measure(roots, "perfect")  # the BASELINE may be measured; the candidate may not
    ledger = _ledger(paths)
    path = create_prereg(paths, _pr(), ledger)
    assert path == paths.prereg_dir / "p001.yaml"
    assert load_prereg(paths, "p001").params == COCO_PARAMS
    assert prereg_time(paths, "p001") is not None and list_preregs(paths) == ["p001"]
    log = json.loads(paths.prereg_log.read_text(encoding="utf-8").splitlines()[0])
    assert log["prereg_id"] == "p001" and len(log["sha256"]) == 64
    # ruling 5: a log whose timestamps prove ordering reads its own clock, never created_at
    assert log["ts"] != CREATED_AT and prereg_time(paths, "p001") == log["ts"]
    with pytest.raises(ValidationFailed, match="already exists"):
        create_prereg(paths, _pr(), ledger)
    _measure(roots, "noisy", subsets=["valA"])
    with pytest.raises(ValidationFailed, match="already_measured") as seen:
        create_prereg(paths, _pr(prereg_id="p002"), _ledger(paths))
    assert "['valA']" in str(seen.value)  # only the claimed subset that was already measured
    # a claim on a subset the candidate has NOT been measured on is still open to be made
    create_prereg(paths, _pr(prereg_id="p003", subsets=["valB"]), _ledger(paths))
    assert list_preregs(paths) == ["p001", "p003"]
    assert prereg_time(paths, "nope") is None


def test_prereg_yaml_round_trips_with_stable_keys_and_lf(roots, tmp_path):
    """The prereg file is committed to git and read back by the judge, so its bytes are part
    of the contract: LF, no BOM, and the spec 4.5 key order."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    path = create_prereg(paths, _pr(), _ledger(paths))
    raw = path.read_bytes()
    assert b"\r\n" not in raw and not raw.startswith(b"\xef\xbb\xbf")
    assert list(yaml.safe_load(raw.decode("utf-8"))) == list(PreRegistration.model_fields)
    assert load_prereg(paths, "p001") == _pr(params=COCO_PARAMS)


def test_create_prereg_failures(roots, tmp_path):
    """Every way a claim can be refused before it is written, and what each one is."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    with pytest.raises(ValidationFailed, match="no subsets"):
        create_prereg(paths, _pr(prereg_id="empty", subsets=[]), _ledger(paths))
    # Minor 1: a duplicate subset would otherwise produce a permanent, confusingly-worded FAIL
    # ("bases_positive 1 < 2") every time the claim is judged, instead of being caught at write
    # time.
    with pytest.raises(ValidationFailed, match="duplicate subsets"):
        create_prereg(paths, _pr(prereg_id="dup", subsets=["valA", "valA"]), _ledger(paths))
    with pytest.raises(ValidationFailed, match="not applicable"):
        create_prereg(paths, _pr(prereg_id="wrong-task", metric="accuracy"), _ledger(paths))
    with pytest.raises(RegistryError):
        create_prereg(paths, _pr(prereg_id="ghost-metric", metric="nope"), _ledger(paths))
    with pytest.raises(ValidationFailed, match="invalid name"):
        create_prereg(paths, _pr(prereg_id="../escape"), _ledger(paths))
    assert list_preregs(paths) == []  # not one of them left a file behind


def test_judge_fail_pass_and_sigma_rules(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=60)
    # ruling 1: every pre-registration is written BEFORE anything is measured
    ledger = _ledger(paths)
    create_prereg(paths, _pr(), ledger)  # candidate noisy (worse)
    create_prereg(
        paths,
        _pr(
            prereg_id="p002",
            baseline_run="noisy",
            candidate_run="perfect",
            component_class="tuning",
            sigma_method="prior",  # ruling 3: a claim pre-registers WHICH sigma_p judges it
        ),
        ledger,
    )
    _measure(roots, "perfect")
    _measure(roots, "noisy")
    j1 = _judge(roots, "p001")
    assert j1.verdict == "FAIL" and j1.bases_positive == 0
    assert set(j1.per_subset) == {"valA", "valB"}
    assert all(s.delta < 0 for s in j1.per_subset.values())
    assert "bases_positive 0 < 2" in " ".join(j1.reasons)
    j2 = _judge(roots, "p002")
    assert j2.verdict == "FAIL" and "no_sigma" in j2.reasons and j2.sigma_p is None
    _sigma(roots, 0.001, "test")
    j3 = _judge(roots, "p002")
    assert j3.verdict == "PASS" and j3.sigma_p.method == "prior" and j3.bases_positive == 2
    assert j3.higher_is_better is True and j3.params == COCO_PARAMS
    assert all(s.t >= 2.0 for s in j3.per_subset.values())
    _sigma(roots, 5.0, "huge")
    j4 = _judge(roots, "p002")
    assert j4.verdict == "FAIL" and any("sigma" in r for r in j4.reasons)
    rows = read_rows(paths.measure_dir / "judgements.jsonl", Judgement)
    assert [r.verdict for r in rows] == ["FAIL", "FAIL", "PASS", "FAIL"]
    assert rows[0].bootstrap == {"resamples": 30, "seed": 0} and rows[0].reading_ids


def test_judge_missing_readings_and_invalid_ordering(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    create_prereg(paths, _pr(), _ledger(paths))
    _measure(roots, "perfect")
    j = _judge(roots)  # noisy never measured
    assert j.verdict == "FAIL" and any("missing_readings" in r for r in j.reasons)
    assert j.per_subset == {} and j.bases_positive == 0
    _measure(roots, "noisy")
    # forge a pre-registration whose log time is later than the candidate's readings
    _hand_write(paths, _pr(prereg_id="p009"), ts="2099-01-01T00:00:00.000Z")
    j = _judge(roots, "p009")
    assert j.verdict == "INVALID" and "measured_before_prereg" in j.reasons
    # ruling 2: the hand-written file carries `params: {}`; the judge normalises it itself,
    # or it would find no readings and report missing_readings instead of the INVALID it is.
    assert j.params == COCO_PARAMS
    with pytest.raises(ValidationFailed, match="not found"):
        _judge(roots, "p404")


def test_one_missing_base_stops_the_whole_judgement(roots, tmp_path):
    """Judging on whichever subsets happen to have readings is exactly the selection this
    framework exists to prevent: one missing base stops the claim, however low its bar."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    create_prereg(
        paths,
        _pr(prereg_id="partial", baseline_run="noisy", candidate_run="perfect", min_bases=1),
        _ledger(paths),
    )
    for run in ("noisy", "perfect"):
        _measure(roots, run, subsets=["valA"])  # valB, the other claimed base, is never measured
    j = _judge(roots, "partial")
    assert j.verdict == "FAIL" and j.bases_positive == 0 and j.per_subset == {}
    assert any("missing_readings: valB" in r for r in j.reasons)


def test_identical_runs_have_zero_delta_and_t_is_capped(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    ledger = _ledger(paths)
    same = dict(baseline_run="perfect", candidate_run="perfect")
    create_prereg(paths, _pr(prereg_id="same", **same), ledger)
    # the same claim with every bar lowered: "nothing changed" is still not a positive base
    create_prereg(paths, _pr(prereg_id="same-loose", t_min=0.0, min_bases=1, **same), ledger)
    _measure(roots, "perfect")
    j = _judge(roots, "same")
    assert all(s.delta == 0.0 and s.se == 0.0 and s.t == 0.0 for s in j.per_subset.values())
    assert j.verdict == "FAIL"
    loose = _judge(roots, "same-loose")
    assert loose.bases_positive == 0 and loose.verdict == "FAIL"
    # a real difference with no spread is the signed cap, never inf: no jsonl ledger holds one
    assert T_CAP == 1e9 and _t(0.1, 0.0) == T_CAP and _t(-0.1, 0.0) == -T_CAP


def test_judge_honours_a_lower_is_better_metric(roots, tmp_path):
    """ruling 0: on rmse the better run has the SMALLER value, so the judge applies the
    metric's direction once and every later comparison keeps 'positive means better'."""
    _, _, paths = _regression_with_runs(roots, tmp_path)
    create_prereg(
        paths,
        PreRegistration(
            prereg_id="r001",
            claim="better beats worse",
            component="head",
            component_class="model",
            baseline_run="worse",
            candidate_run="better",
            metric="rmse",
            subsets=["valA", "valB"],
            created_at=CREATED_AT,
        ),
        _ledger(paths),
    )
    for run in ("worse", "better"):
        _measure(roots, run, metrics=["rmse"])
    j = _judge(roots, "r001", dataset="reg")
    assert j.higher_is_better is False
    assert j.verdict == "PASS" and j.bases_positive == 2
    for s in j.per_subset.values():
        assert s.candidate < s.baseline and s.delta > 0 and s.t >= 2.0


def test_a_zero_sigma_p_is_recorded_as_a_reason(roots, tmp_path):
    """ruling 0b: a sigma_p of exactly 0 makes the sigma_p condition vacuously true. The
    verdict is unchanged, but the row has to say the bar it cleared was no bar at all."""
    _, _, paths = det_with_runs(roots, tmp_path, n=60)
    create_prereg(
        paths,
        _pr(
            prereg_id="p010",
            baseline_run="noisy",
            candidate_run="perfect",
            component_class="tuning",
            sigma_method="prior",
        ),
        _ledger(paths),
    )
    _measure(roots, "perfect")
    _measure(roots, "noisy")
    _sigma(roots, 0.0, "flat")
    j = _judge(roots, "p010")
    assert j.verdict == "PASS" and "sigma_zero" in j.reasons
    assert j.sigma_p is not None and j.sigma_p.value == 0.0


def test_judging_twice_decides_the_same_and_records_both(roots, tmp_path):
    """A judgement is an event, not an identity (spec 4.6 gives the row a ts and no id):
    the same inputs must decide the same way, and both decisions stay in the ledger."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    create_prereg(paths, _pr(), _ledger(paths))
    _measure(roots, "perfect")
    _measure(roots, "noisy")
    first, second = _judge(roots), _judge(roots)
    assert first.model_dump(exclude={"ts"}) == second.model_dump(exclude={"ts"})
    rows = read_rows(paths.measure_dir / "judgements.jsonl", Judgement)
    assert len(rows) == 2 and rows[0].ts <= rows[1].ts


def test_judgements_ledger_is_append_only(roots, tmp_path):
    """Nothing rewrites judgements.jsonl: a line this code did not write survives untouched."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    create_prereg(paths, _pr(), _ledger(paths))
    _measure(roots, "perfect")
    _measure(roots, "noisy")
    path = paths.measure_dir / "judgements.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    foreign = '{"hand":"written","by":"a human"}'
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(foreign + "\n")
    _judge(roots)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == foreign and len(lines) == 2
    assert json.loads(lines[1])["prereg_id"] == "p001"


def test_judge_failures(roots, tmp_path):
    """Every way a committed pre-registration can fail to decide, and what each one is."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    create_prereg(paths, _pr(), _ledger(paths))
    _measure(roots, "perfect")
    _measure(roots, "noisy")
    # a subset the plan does not have simply has no readings: FAIL, not a crash in the splitter
    _hand_write(paths, _pr(prereg_id="ghost-subset", subsets=["ghost"]))
    j = _judge(roots, "ghost-subset")
    assert j.verdict == "FAIL" and any("missing_readings: ghost" in r for r in j.reasons)
    # a claim over no subsets at all decides nothing
    _hand_write(paths, _pr(prereg_id="no-subsets", subsets=[]))
    with pytest.raises(ValidationFailed, match="no subsets"):
        _judge(roots, "no-subsets")
    # a metric no registry knows keeps its ABORT semantics (a plugin that was not loaded)
    _hand_write(paths, _pr(prereg_id="ghost-metric", metric="nope"))
    with pytest.raises(RegistryError):
        _judge(roots, "ghost-metric")
    # a hand-edited yaml in the wrong shape is a located FAIL, never a raw pydantic error
    (paths.prereg_dir / "broken.yaml").write_text(
        "claim: no id here\n", encoding="utf-8", newline="\n"
    )
    _log_prereg(paths, "broken", stamp())
    with pytest.raises(ValidationFailed, match="broken.yaml"):
        _judge(roots, "broken")
    # a yaml that was never logged has no binding moment to compare readings against
    _hand_write(paths, _pr(prereg_id="unlogged"))
    paths.prereg_log.write_text(
        "\n".join(
            line
            for line in paths.prereg_log.read_text(encoding="utf-8").splitlines()
            if "unlogged" not in line
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(ValidationFailed, match="not in"):
        _judge(roots, "unlogged")
    # a logged id whose file is gone
    (paths.prereg_dir / "p001.yaml").unlink()
    with pytest.raises(ValidationFailed, match="not found"):
        _judge(roots)


def test_judge_refuses_readings_from_two_different_plans(roots, tmp_path):
    """A subset name means different samples under each plan, so comparing readings taken
    under two of them would score one run's predictions against the other's samples."""
    ds, _, paths = det_with_runs(roots, tmp_path, n=40)
    plan2 = build_plan(ds, plan_id="fixed-v2", subsets=parse_subsets(DEFAULT_SUBSETS), seed=7)
    save_plan(plan2, paths)
    sub = ds.subset("valA", plan2, paths=paths)
    src = tmp_path / "other-valA.jsonl"
    write_predictions(src, perfect_predictions(sub, ds.card))
    ingest(
        IngestSpec(
            run_id="other",
            dataset="tiny",
            plan_id="fixed-v2",
            subset="valA",
            format="jsonl",
            src=src,
            trained_on=["train"],
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    create_prereg(
        paths,
        _pr(prereg_id="cross", candidate_run="other", subsets=["valA"], min_bases=1),
        _ledger(paths),
    )
    _measure(roots, "perfect")
    _measure(roots, "other", subsets=["valA"])
    with pytest.raises(PlanMismatchError, match="fixed-v2"):
        _judge(roots, "cross")


def test_prereg_log_with_a_mangled_line_is_a_located_failure(roots, tmp_path):
    """prereg.log.jsonl lives in git, so a merge can mangle it: that is a located FAIL, not a
    JSONDecodeError escaping as an ABORT."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    create_prereg(paths, _pr(), _ledger(paths))
    with paths.prereg_log.open("a", encoding="utf-8", newline="\n") as f:
        f.write("{not json\n")
    with pytest.raises(ValidationFailed, match="prereg.log.jsonl:2"):
        prereg_time(paths, "nobody")


class _NanOnResample:
    """A metric whose value depends only on whether its input list has duplicate sample_ids:
    the full ordered subset never does, but a bootstrap resample (drawn with replacement)
    almost always does once a subset has more than a couple of samples. Registered and popped
    per test (Task 12 ruling 2's leak-proof pattern) so it never lingers in process-global
    METRICS for the rest of the session."""

    name = "nan_on_resample"
    version = "1"
    tasks = frozenset({"det"})
    defaults: dict[str, str] = {}
    higher_is_better = True

    def compute(self, samples, predictions, card, params):
        ids = [s.sample_id for s in samples]
        value = 1.0 if len(set(ids)) == len(ids) else float("nan")
        return MetricResult(value=value, per_class=None, n=len(samples))


def test_judge_refuses_a_non_finite_bootstrap_se(roots, tmp_path):
    """I1: a plugin metric that returns nan on a bootstrap resample must not fall through
    `_t`'s `se > 0` check to +-T_CAP ("infinite certainty") -- that would let a subset with no
    real answer count as a positive base and could flip a FAIL to a PASS. Nothing may reach
    judgements.jsonl when this happens."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    register_metric(_NanOnResample())
    try:
        create_prereg(paths, _pr(metric="nan_on_resample"), _ledger(paths))
        _measure(roots, "perfect", metrics=["nan_on_resample"])
        _measure(roots, "noisy", metrics=["nan_on_resample"])
        before = len(read_rows(paths.measure_dir / "judgements.jsonl", Judgement))
        with pytest.raises(ValidationFailed, match="not finite"):
            _judge(roots)
        after = len(read_rows(paths.measure_dir / "judgements.jsonl", Judgement))
        assert after == before == 0
    finally:
        METRICS.pop("nan_on_resample", None)


def test_append_judgement_refuses_a_non_finite_field(tmp_path):
    """I1: judgements.jsonl gets the same discipline ReadingsLedger.append gives readings.jsonl
    -- a hand-built row with a non-finite se (bypassing `_t` entirely) is still refused, with
    the ledger's own "refusing to append it" wording."""
    bad = Judgement(
        prereg_id="x",
        ts=stamp(),
        baseline_run="a",
        candidate_run="b",
        metric="coco_map",
        params={},
        higher_is_better=True,
        per_subset={
            "valA": SubsetJudgement(
                baseline=1.0, candidate=1.0, delta=0.0, se=float("nan"), t=0.0, n=1
            )
        },
        bases_positive=0,
        sigma_p=None,
        verdict="FAIL",
        reasons=[],
        reading_ids=[],
        bootstrap={"resamples": 30, "seed": 0},
    )
    path = tmp_path / "judgements.jsonl"
    with pytest.raises(ValidationFailed, match="refusing to append it"):
        _append_judgement(path, bad)
    assert not path.exists()


def test_judge_needs_unseal_and_reason_for_a_sealed_subset(roots, tmp_path):
    """Task 12 ruling 0c: judge gains --unseal/--reason exactly like measure, so a claim that
    was legitimately measured with --unseal --reason is not permanently unjudgeable."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    create_prereg(
        paths,
        _pr(
            prereg_id="sealed-claim",
            subsets=["holdout"],
            min_bases=1,
            baseline_run="perfect",
            candidate_run="perfect",
        ),
        _ledger(paths),
    )
    _measure(roots, "perfect", subsets=["holdout"], unseal=True, reason="fixture")
    with pytest.raises(SealedSubsetError):
        _judge(roots, "sealed-claim")
    unseal_log = paths.unseal_jsonl("fixed-v1")
    before = unseal_log.read_text(encoding="utf-8").splitlines()
    j = _judge(roots, "sealed-claim", unseal=True, reason="final read")
    assert j.per_subset  # decided at all -- the sealed subset was readable through the judge
    after = unseal_log.read_text(encoding="utf-8").splitlines()
    assert len(after) == len(before) + 1 and '"caller": "vcp eval judge"' in after[-1]


def test_judge_refuses_a_stale_reading_then_uses_the_newer_one_after_replace(roots, tmp_path):
    """Minor 2 + Minor 3: after `ingest --replace`, a stored reading's prediction_sha no longer
    matches the run card, so judging on it would silently mix a stale baseline/candidate value
    with a fresh bootstrap over the NEW file -- that must be a located refusal. Once the
    candidate is re-measured, `_latest`'s max-by-ts must pick the fresh reading (whose sha DOES
    match the card again), not the stale one.
    """
    ds, plan, paths = det_with_runs(roots, tmp_path, n=40)
    create_prereg(paths, _pr(), _ledger(paths))
    _measure(roots, "perfect")
    _measure(roots, "noisy")
    stale = next(r for r in _ledger(paths).rows if r.run_id == "noisy" and r.subset == "valA")
    sub = ds.subset("valA", plan, paths=paths)
    src = tmp_path / "noisy-valA-2.jsonl"
    write_predictions(src, perfect_predictions(sub, ds.card))  # deliberately different content
    ingest(
        IngestSpec(
            run_id="noisy",
            dataset="tiny",
            plan_id="fixed-v1",
            subset="valA",
            format="jsonl",
            src=src,
            trained_on=["train"],
            replace=True,
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    with pytest.raises(ValidationFailed, match="re-measure"):
        _judge(roots)
    # A deliberate delay: the fix must pick the reading with the LATER ts, and this test must
    # not depend on ingest()'s own wall-clock cost alone to guarantee that ordering.
    time.sleep(0.01)
    _measure(roots, "noisy", subsets=["valA"])
    fresh = next(
        r
        for r in _ledger(paths).rows
        if r.run_id == "noisy" and r.subset == "valA" and r.reading_id != stale.reading_id
    )
    assert fresh.value != stale.value and fresh.ts > stale.ts
    j = _judge(roots)
    assert j.per_subset["valA"].candidate == fresh.value
