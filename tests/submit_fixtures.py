"""Shared by the submission-layer tests: an eval dataset with a sealed holdout, an unlabeled
test dataset, and helpers that ingest runs on either side and judge claims. A plain module (not
a conftest) so tests outside tests/unit/submit can build the same fixture."""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace

from helpers import (
    CATS,
    cls_samples,
    make_card,
    noisy_predictions,
    perfect_predictions,
    write_images,
)
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.judge import JudgeSpec, judge_prereg
from vcp.measure.ledger import READINGS_LEDGER, ReadingsLedger
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.measure.predictions import write_predictions
from vcp.measure.prereg import create_prereg
from vcp.measure.schema import Prediction, PreRegistration

EVAL = "beach"
TEST = "beach-test"
STAMP = "2026-09-05T00:00:00.000Z"


def random_scores(samples, *, seed: int) -> list[Prediction]:
    """Class-probability rows for unlabeled samples (a test set has no gold to copy)."""
    rng = random.Random(seed)
    names = [c.name for c in CATS]
    out = []
    for s in samples:
        raw = [rng.random() for _ in names]
        total = sum(raw)
        scores = {n: v / total for n, v in zip(names, raw, strict=True)}
        out.append(Prediction(sample_id=s.sample_id, scores=scores))
    return out


def make_pair(roots, tmp_path) -> SimpleNamespace:
    """Eval dataset ``beach`` (cls, 200 gold samples, plan fixed-v1 with sealed ``holdout``) and
    test dataset ``beach-test`` (cls, 50 unlabeled samples); two weights files. No profile yet."""
    eval_paths = DatasetPaths.resolve(EVAL, data_root=roots.data, configs_root=roots.configs)
    eval_samples = cls_samples(200, seed=1)
    write_images(roots.data / "raw" / EVAL, eval_samples)
    eval_ds = Dataset.from_parts(
        make_card("cls", name=EVAL, image_root=f"raw/{EVAL}"), eval_samples
    )
    eval_ds.save(eval_paths)
    eval_plan = build_plan(
        eval_ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0
    )
    save_plan(eval_plan, eval_paths)
    test_paths = DatasetPaths.resolve(TEST, data_root=roots.data, configs_root=roots.configs)
    test_samples = cls_samples(50, seed=2, gold_frac=0.0)
    write_images(roots.data / "raw" / TEST, test_samples)
    test_ds = Dataset.from_parts(
        make_card("cls", name=TEST, image_root=f"raw/{TEST}"), test_samples
    )
    test_ds.save(test_paths)
    weights = {}
    for name in ("good", "bad"):
        p = tmp_path / f"{name}.pt"
        p.write_bytes(name.encode())
        weights[name] = p
    return SimpleNamespace(
        roots=roots,
        tmp=tmp_path,
        eval_ds=eval_ds,
        eval_plan=eval_plan,
        eval_paths=eval_paths,
        test_ds=test_ds,
        test_paths=test_paths,
        weights=weights,
    )


def ingest_run(
    pair,
    run_id: str,
    dataset: Dataset,
    plan_id: str,
    subset: str,
    preds: list[Prediction],
    *,
    weights: Path | None,
    trained_on: tuple[str, ...] = (),
    replace: bool = False,
) -> None:
    src = pair.tmp / f"{run_id}-{subset}.jsonl"
    write_predictions(src, preds)
    ingest(
        IngestSpec(
            run_id=run_id,
            dataset=dataset.card.name,
            plan_id=plan_id,
            subset=subset,
            format="jsonl",
            src=src,
            trained_on=list(trained_on),
            weights=weights,
            replace=replace,
            data_root=pair.roots.data,
            configs_root=pair.roots.configs,
        )
    )


def seed_eval_runs(pair) -> None:
    """Run ``good`` (perfect) and ``bad`` (noisy) on valA, valB and holdout, each with its own
    weights file, trained on ``train``."""
    ds, plan = pair.eval_ds, pair.eval_plan
    for subset in ("valA", "valB", "holdout"):
        samples = ds.subset(
            subset, plan, unseal=subset == "holdout", reason="fixture", paths=pair.eval_paths
        )
        ingest_run(
            pair,
            "good",
            ds,
            "fixed-v1",
            subset,
            perfect_predictions(samples, ds.card),
            weights=pair.weights["good"],
            trained_on=("train",),
        )
        ingest_run(
            pair,
            "bad",
            ds,
            "fixed-v1",
            subset,
            noisy_predictions(samples, ds.card, seed=3, flip=0.6),
            weights=pair.weights["bad"],
            trained_on=("train",),
        )


def seed_test_runs(pair, plan_id: str = "all-v1", subset: str = "test") -> None:
    """Test-side runs: ``good.test`` / ``bad.test`` carry their eval twin's weights;
    ``bad.mismatch`` carries good's weights under bad's name (an identity failure)."""
    ds = pair.test_ds
    samples = list(ds.samples)
    for run_id, seed, weights in (
        ("good.test", 10, "good"),
        ("bad.test", 11, "bad"),
        ("bad.mismatch", 12, "good"),
    ):
        ingest_run(
            pair,
            run_id,
            ds,
            plan_id,
            subset,
            random_scores(samples, seed=seed),
            weights=pair.weights[weights],
        )


def seed_judgements(pair) -> None:
    """Claims ``p-good`` (good beats bad -> PASS) and ``p-bad`` (bad beats good -> FAIL), both
    class model on accuracy over valA / valB. Both are registered before either candidate is
    measured, as the measurement layer demands."""
    readings = ReadingsLedger(pair.eval_paths.measure_dir / READINGS_LEDGER)
    for pid, cand, base in (("p-good", "good", "bad"), ("p-bad", "bad", "good")):
        create_prereg(
            pair.eval_paths,
            PreRegistration(
                prereg_id=pid,
                claim=f"{cand} beats {base}",
                component=cand,
                component_class="model",
                baseline_run=base,
                candidate_run=cand,
                metric="accuracy",
                subsets=["valA", "valB"],
                created_at=STAMP,
            ),
            readings,
        )
    for run in ("good", "bad"):
        measure_run(
            MeasureSpec(
                run_id=run,
                metrics=["accuracy"],
                subsets=["valA", "valB"],
                data_root=pair.roots.data,
                configs_root=pair.roots.configs,
            )
        )
    for pid in ("p-good", "p-bad"):
        judge_prereg(
            JudgeSpec(
                dataset=EVAL,
                prereg_id=pid,
                resamples=50,
                seed=0,
                data_root=pair.roots.data,
                configs_root=pair.roots.configs,
            )
        )
