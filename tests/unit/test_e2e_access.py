"""Audit release-gate scenarios 1 and 2 through the CLI (spec 13): a training loop that reads
its subset through the reader under `vcp train run` leaves a receipt that grades the run; a
loop that also peeks at valA is caught -- `train run` warns, `measure` drops valA, `judge`
on valA is INVALID; a profile that requires receipts refuses a declared run and admits a
receipt-backed one; the sealed subset cannot be read without an unseal, id or no id."""

import json
import sys

import pytest
from typer.testing import CliRunner

from helpers import det_samples, make_card, noisy_predictions, perfect_predictions, write_images
from submit_fixtures import EVAL, STAMP, TEST, seed_eval_runs, seed_judgements, seed_test_runs
from vcp.cli import app
from vcp.core.errors import AccessDeniedError, SealedSubsetError
from vcp.core.paths import DatasetPaths
from vcp.data.access.access import DatasetAccess
from vcp.data.dataset import Dataset
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.predictions import write_predictions
from vcp.measure.provenance import attach_receipts
from vcp.measure.runs import load_run, save_run
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile

runner = CliRunner()

ACCESS_FAKE = """
from pathlib import Path
from vcp.train import MaterializedReader

with MaterializedReader("flow", "npy", plan_id="fixed-v1", subset="train") as reader:
    n = sum(1 for _ in reader)
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best-%d" % n)
"""

PEEK_FAKE = (
    ACCESS_FAKE
    + """
from vcp.train import Session

with Session.current().access(subsets={"valA"}) as peek:
    list(peek.iter("valA"))
"""
)


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _run(*args):
    return runner.invoke(app, list(args))


def _train(work, run_id, script):
    return _run(
        "train",
        "run",
        "--run",
        run_id,
        "--dataset",
        "flow",
        "--plan",
        "fixed-v1",
        "--trained-on",
        "train",
        "--seed",
        "1",
        "--cwd",
        str(work),
        "--checkpoints",
        "weights/*.pt",
        "--final",
        "weights/best.pt",
        "--",
        sys.executable,
        script,
    )


def _ingest(tmp_path, ds, plan, run_id, subset, maker):
    src = tmp_path / f"{run_id}-{subset}.jsonl"
    write_predictions(src, maker(ds.subset(subset, plan), ds.card))
    return _run(
        "eval",
        "ingest",
        "--run",
        run_id,
        "--dataset",
        "flow",
        "--plan",
        "fixed-v1",
        "--subset",
        subset,
        "--format",
        "jsonl",
        "--src",
        str(src),
    )


def test_training_receipts_grade_measure_and_judge(roots, tmp_path):
    paths = DatasetPaths.resolve("flow", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(60, seed=5)
    write_images(roots.data / "raw" / "flow", samples)
    ds = Dataset.from_parts(make_card("det", name="flow", image_root="raw/flow"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=3)
    save_plan(plan, paths)
    assert (
        materialize(
            MaterializeSpec(
                name="flow", mode="npy", data_root=roots.data, configs_root=roots.configs
            )
        ).failed
        == 0
    )
    work = tmp_path / "work"
    work.mkdir()
    (work / "access.py").write_text(ACCESS_FAKE, encoding="utf-8")
    (work / "peek.py").write_text(PEEK_FAKE, encoding="utf-8")
    # 1. a clean train-only loop
    r = _train(work, "good", "access.py")
    v = _verdict(r.output)
    assert r.exit_code == 0, r.output
    assert "receipts=1" in v and "denied=0" in v and "provenance=receipt" in v
    assert "observed_beyond_trained_on" not in v
    # 2. a loop that also reads valA: the run exists, the VERDICT says what it did
    r = _train(work, "peek", "peek.py")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "receipts=2" in v
    assert "observed_beyond_trained_on=valA" in v
    for run_id, maker in (("good", perfect_predictions), ("peek", noisy_predictions)):
        for subset in ("valA", "valB"):
            r = _ingest(tmp_path, ds, plan, run_id, subset, maker)
            assert r.exit_code == 0, r.output
            assert "provenance=receipt" in _verdict(r.output)
    # 3. measure the baseline: the peeked subset is not a clean base any more
    r = _run("eval", "measure", "--run", "peek")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "subsets=valB" in v and "observed=train,valA" in v
    r = _run("eval", "measure", "--run", "peek", "--subsets", "valA")
    assert r.exit_code == 1 and "contaminated: subset 'valA'" in _verdict(r.output)
    # 4. the claims go down BEFORE the candidate is measured (create_prereg refuses otherwise);
    #    one on valA against the peeking baseline is INVALID, one on valB alone is judged
    for pid, subsets in (("p-valA", "valA,valB"), ("p-valB", "valB")):
        r = _run(
            "eval",
            "preregister",
            "--dataset",
            "flow",
            "--id",
            pid,
            "--claim",
            "good beats peek",
            "--component",
            "good",
            "--class",
            "model",
            "--baseline-run",
            "peek",
            "--candidate-run",
            "good",
            "--metric",
            "coco_map",
            "--subsets",
            subsets,
            "--min-bases",
            "1",
        )
        assert r.exit_code == 0, r.output
    r = _run("eval", "measure", "--run", "good")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "subsets=valA,valB" in v and "observed=train" in v
    r = _run("eval", "judge", "--dataset", "flow", "--prereg", "p-valA")
    v = _verdict(r.output)
    assert "verdict=INVALID" in v and "provenance=receipt" in v
    assert "reason: contaminated:peek/valA" in r.output
    r = _run("eval", "judge", "--dataset", "flow", "--prereg", "p-valB")
    v = _verdict(r.output)
    assert "verdict=" in v and "INVALID" not in v and "provenance=receipt" in v
    doc = json.loads(
        next(
            line
            for line in _run("eval", "status", "--dataset", "flow", "--json").stdout.splitlines()
            if line.startswith("{")
        )
    )
    assert doc["fields"]["receipt_runs"] == 2
    # 5. sealed: with an id or without, no read without an unseal; the unseal is recorded
    holdout_id = sorted(plan.ids_in("holdout"))[0]
    with DatasetAccess.open(
        "flow", "fixed-v1", subsets={"train"}, data_root=roots.data, configs_root=roots.configs
    ) as access:
        with pytest.raises(AccessDeniedError):
            access.by_id(holdout_id)
        with pytest.raises(AccessDeniedError):
            access.iter("holdout")
    with pytest.raises(SealedSubsetError):
        DatasetAccess.open(
            "flow",
            "fixed-v1",
            subsets={"holdout"},
            data_root=roots.data,
            configs_root=roots.configs,
        )
    with DatasetAccess.open(
        "flow",
        "fixed-v1",
        subsets={"holdout"},
        unseal_reason="final",
        data_root=roots.data,
        configs_root=roots.configs,
    ) as sealed:
        assert sealed.by_id(holdout_id).sample_id == holdout_id
    assert sealed.receipt.sealed_accessed
    assert len(paths.unseal_jsonl("fixed-v1").read_text(encoding="utf-8").splitlines()) == 1


def test_a_profile_can_require_receipts_at_the_gate(roots, tmp_path):
    from submit_fixtures import make_pair

    pair = make_pair(roots, tmp_path)
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(
        PlatformProfile(
            dataset=TEST,
            eval_dataset=EVAL,
            plan_id="fixed-v1",
            sealed_subset="holdout",
            platform="manual",
            board_rule="last",
            metric="accuracy",
            writer="scores_csv",
            require_provenance="receipt",
            created_at=STAMP,
        ),
        data_root=roots.data,
        configs_root=roots.configs,
    )
    seed_test_runs(pair)
    common = ["submit", "stage", "--dataset", TEST, "--eval-run", "good", "--test-run", "good.test"]
    r = _run(*common, "--id", "S1")
    assert r.exit_code == 1 and "provenance_required: run 'good' is declared" in _verdict(r.output)
    with DatasetAccess.open(
        EVAL,
        "fixed-v1",
        subsets={"train"},
        purpose="train",
        data_root=roots.data,
        configs_root=roots.configs,
    ) as access:
        list(access.iter("train"))
    save_run(
        roots.data,
        attach_receipts(load_run(roots.data, "good"), [access.receipt_id], data_root=roots.data),
    )
    r = _run(*common, "--id", "S1")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "provenance=receipt" in v and "admission=PASS" in v
    r = _run("submit", "status", "--dataset", TEST)
    assert "provenance: S1=receipt" in r.output
