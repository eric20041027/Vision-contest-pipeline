"""The backup manifests are byte-for-byte what they were before the walker grew a replaceable
recording end (0.17.0 spec 5.3). The golden rows were printed from the unrefactored
``Collector`` (twice, identical) and pasted in as literals: (key, role, tier, kind, has_remote).
Every row is present and serves exactly the conclusion that was asked for."""

from __future__ import annotations

import pytest

from submit_fixtures import EVAL, TEST
from vcp.backup.evidence import build_manifest
from vcp.backup.manifest import local_path
from vcp.core.hashing import sha256_file


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _shape(f):  # no sha, no bytes, no absolute source: stable across runs
    return (f.key, f.role, f.tier, f.kind, f.present, tuple(f.for_), f.remote is not None)


def _golden(conclusion, rows):
    return [
        (key, role, tier, kind, True, (conclusion,), remote)
        for key, role, tier, kind, remote in rows
    ]


GOLDEN = {
    ("all", EVAL): _golden(
        "all",
        [
            ("data/measure/beach/judgements.jsonl", "judgements", 1, "file", False),
            ("data/measure/beach/readings.jsonl", "readings", 1, "file", False),
            ("configs/datasets/beach/prereg/p-bad.yaml", "prereg", 1, "file", False),
            ("configs/datasets/beach/prereg/p-good.yaml", "prereg", 1, "file", False),
            ("configs/datasets/beach/prereg.log.jsonl", "prereg_log", 1, "file", False),
            ("data/runs/bad/run.yaml", "run_card", 1, "file", False),
            ("data/runs/good/run.yaml", "run_card", 1, "file", False),
            ("data/runs/good/train.yaml", "train_record", 1, "file", False),
            ("data/runs/good/train.log.jsonl", "train_log", 1, "file", False),
            ("configs/datasets/beach/dataset.yaml", "dataset_card", 1, "file", False),
            ("configs/datasets/beach/splits/fixed-v1.json", "plan", 1, "file", False),
            ("configs/datasets/beach/splits/fixed-v1.unseal.jsonl", "unseal_log", 1, "file", False),
            ("data/runs/bad/predictions/holdout.jsonl", "prediction", 2, "file", False),
            ("data/runs/bad/predictions/valA.jsonl", "prediction", 2, "file", False),
            ("data/runs/bad/predictions/valB.jsonl", "prediction", 2, "file", False),
            ("data/runs/good/predictions/holdout.jsonl", "prediction", 2, "file", False),
            ("data/runs/good/predictions/valA.jsonl", "prediction", 2, "file", False),
            ("data/runs/good/predictions/valB.jsonl", "prediction", 2, "file", False),
            ("data/datasets/beach/samples.jsonl", "samples", 2, "file", False),
            ("data/runs/good/train/console.1.log", "train_dir", 2, "file", False),
            ("data/work/good/weights/best.pt", "checkpoint_final", 3, "remote_copy", True),
            ("data/work/good/weights/last.pt", "checkpoint", 3, "file", False),
        ],
    ),
    ("run:good", EVAL): _golden(
        "run:good",
        [
            ("data/measure/beach/judgements.jsonl", "judgements", 1, "file", False),
            ("data/measure/beach/readings.jsonl", "readings", 1, "file", False),
            ("data/runs/good/run.yaml", "run_card", 1, "file", False),
            ("data/runs/good/train.yaml", "train_record", 1, "file", False),
            ("data/runs/good/train.log.jsonl", "train_log", 1, "file", False),
            ("configs/datasets/beach/dataset.yaml", "dataset_card", 1, "file", False),
            ("configs/datasets/beach/splits/fixed-v1.json", "plan", 1, "file", False),
            ("configs/datasets/beach/splits/fixed-v1.unseal.jsonl", "unseal_log", 1, "file", False),
            ("data/runs/good/predictions/holdout.jsonl", "prediction", 2, "file", False),
            ("data/runs/good/predictions/valA.jsonl", "prediction", 2, "file", False),
            ("data/runs/good/predictions/valB.jsonl", "prediction", 2, "file", False),
            ("data/runs/good/train/console.1.log", "train_dir", 2, "file", False),
            ("data/work/good/weights/best.pt", "checkpoint_final", 3, "remote_copy", True),
            ("data/work/good/weights/last.pt", "checkpoint", 3, "file", False),
        ],
    ),
    ("submission:S1", TEST): _golden(
        "submission:S1",
        [
            ("configs/datasets/beach-test/submit.yaml", "submit_profile", 1, "file", False),
            ("configs/datasets/beach-test/submissions.jsonl", "submissions_log", 1, "file", False),
            ("data/submit/beach-test/S1/stage.json", "stage", 1, "file", False),
            ("data/submit/beach-test/S1/submission.csv", "artifact", 1, "file", False),
            ("data/measure/beach/judgements.jsonl", "judgements", 1, "file", False),
            ("data/measure/beach/readings.jsonl", "readings", 1, "file", False),
            ("configs/datasets/beach/prereg/p-good.yaml", "prereg", 1, "file", False),
            ("configs/datasets/beach/prereg.log.jsonl", "prereg_log", 1, "file", False),
            ("data/runs/bad/run.yaml", "run_card", 1, "file", False),
            ("data/runs/good.test/run.yaml", "run_card", 1, "file", False),
            ("data/runs/good/run.yaml", "run_card", 1, "file", False),
            ("data/runs/good/train.yaml", "train_record", 1, "file", False),
            ("data/runs/good/train.log.jsonl", "train_log", 1, "file", False),
            ("configs/datasets/beach-test/dataset.yaml", "dataset_card", 1, "file", False),
            ("configs/datasets/beach/dataset.yaml", "dataset_card", 1, "file", False),
            ("configs/datasets/beach-test/splits/all-v1.json", "plan", 1, "file", False),
            ("configs/datasets/beach/splits/fixed-v1.json", "plan", 1, "file", False),
            ("configs/datasets/beach/splits/fixed-v1.unseal.jsonl", "unseal_log", 1, "file", False),
            ("data/runs/bad/predictions/holdout.jsonl", "prediction", 2, "file", False),
            ("data/runs/bad/predictions/valA.jsonl", "prediction", 2, "file", False),
            ("data/runs/bad/predictions/valB.jsonl", "prediction", 2, "file", False),
            ("data/runs/good.test/predictions/test.jsonl", "prediction", 2, "file", False),
            ("data/runs/good/predictions/holdout.jsonl", "prediction", 2, "file", False),
            ("data/runs/good/predictions/valA.jsonl", "prediction", 2, "file", False),
            ("data/runs/good/predictions/valB.jsonl", "prediction", 2, "file", False),
            ("data/runs/good/train/console.1.log", "train_dir", 2, "file", False),
            ("data/work/good/weights/best.pt", "checkpoint_final", 3, "remote_copy", True),
            ("data/work/good/weights/last.pt", "checkpoint", 3, "file", False),
        ],
    ),
    # not in the brief's three: the one `all` that reaches _walk_submissions, which the refactor
    # rewrites, so a fourth golden keeps that path byte-locked too
    ("all", TEST): _golden(
        "all",
        [
            ("configs/datasets/beach-test/submit.yaml", "submit_profile", 1, "file", False),
            ("configs/datasets/beach-test/submissions.jsonl", "submissions_log", 1, "file", False),
            ("data/submit/beach-test/S1/stage.json", "stage", 1, "file", False),
            ("data/submit/beach-test/S1/submission.csv", "artifact", 1, "file", False),
            ("data/measure/beach/judgements.jsonl", "judgements", 1, "file", False),
            ("data/measure/beach/readings.jsonl", "readings", 1, "file", False),
            ("configs/datasets/beach/prereg/p-good.yaml", "prereg", 1, "file", False),
            ("configs/datasets/beach/prereg.log.jsonl", "prereg_log", 1, "file", False),
            ("data/runs/bad.mismatch/run.yaml", "run_card", 1, "file", False),
            ("data/runs/bad.test/run.yaml", "run_card", 1, "file", False),
            ("data/runs/bad/run.yaml", "run_card", 1, "file", False),
            ("data/runs/good.test/run.yaml", "run_card", 1, "file", False),
            ("data/runs/good/run.yaml", "run_card", 1, "file", False),
            ("data/runs/good/train.yaml", "train_record", 1, "file", False),
            ("data/runs/good/train.log.jsonl", "train_log", 1, "file", False),
            ("configs/datasets/beach-test/dataset.yaml", "dataset_card", 1, "file", False),
            ("configs/datasets/beach/dataset.yaml", "dataset_card", 1, "file", False),
            ("configs/datasets/beach-test/splits/all-v1.json", "plan", 1, "file", False),
            ("configs/datasets/beach/splits/fixed-v1.json", "plan", 1, "file", False),
            ("configs/datasets/beach/splits/fixed-v1.unseal.jsonl", "unseal_log", 1, "file", False),
            ("data/runs/bad.mismatch/predictions/test.jsonl", "prediction", 2, "file", False),
            ("data/runs/bad.test/predictions/test.jsonl", "prediction", 2, "file", False),
            ("data/runs/bad/predictions/holdout.jsonl", "prediction", 2, "file", False),
            ("data/runs/bad/predictions/valA.jsonl", "prediction", 2, "file", False),
            ("data/runs/bad/predictions/valB.jsonl", "prediction", 2, "file", False),
            ("data/runs/good.test/predictions/test.jsonl", "prediction", 2, "file", False),
            ("data/runs/good/predictions/holdout.jsonl", "prediction", 2, "file", False),
            ("data/runs/good/predictions/valA.jsonl", "prediction", 2, "file", False),
            ("data/runs/good/predictions/valB.jsonl", "prediction", 2, "file", False),
            ("data/datasets/beach-test/samples.jsonl", "samples", 2, "file", False),
            ("data/runs/good/train/console.1.log", "train_dir", 2, "file", False),
            ("data/work/good/weights/best.pt", "checkpoint_final", 3, "remote_copy", True),
            ("data/work/good/weights/last.pt", "checkpoint", 3, "file", False),
        ],
    ),
}


@pytest.mark.parametrize(("conclusion", "dataset"), list(GOLDEN))
def test_backup_manifests_keep_their_files(world, conclusion, dataset):
    res = build_manifest(dataset, conclusion, manifest_id="golden", **_kw(world))
    assert [_shape(f) for f in res.manifest.files if f.role != "logs"] == GOLDEN[
        (conclusion, dataset)
    ]
    assert all(
        f.sha256 == sha256_file(local_path(f, world.roots.data, world.roots.configs))
        for f in res.manifest.files
        if f.present and f.role != "submissions_log"
    )
