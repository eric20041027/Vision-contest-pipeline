"""The backup manifests are byte-for-byte what they were before the walker grew a replaceable
recording end (0.17.0 spec 5.3). The golden rows were printed from the unrefactored
``Collector`` (twice, identical) and pasted in as literals: (key, role, tier, kind, has_remote).
Every row is present and serves exactly the conclusion that was asked for."""

from __future__ import annotations

from pathlib import Path

import pytest

from submit_fixtures import EVAL, TEST
from vcp.backup import evidence
from vcp.backup.evidence import Collector, build_manifest
from vcp.backup.manifest import local_path
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, logs_dir


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _paths(world, name):
    return DatasetPaths.resolve(name, **_kw(world))


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


class _Book:  # a recorder double: every add is a row; a unit is taken back by truncation
    tolerates = (ValidationFailed, OSError)

    def __init__(self):
        self.rows = []

    def add(self, path, role, conclusion, *, sha256=None, size=None, remote=None):
        self.rows.append((Path(path), role, conclusion))

    def mark(self):
        return len(self.rows)

    def rollback(self, mark):
        del self.rows[mark:]

    def conclusions(self):
        return {c for _, _, c in self.rows}

    def roles(self):
        return {r for _, r, _ in self.rows}


def _never_hashed(*_args, **_kwargs):
    raise AssertionError("a recorder is handed the file, not its hash")


def _write_log(world):
    logs_dir(world.roots.data).mkdir(parents=True, exist_ok=True)
    (logs_dir(world.roots.data) / "vcp-2026-09-06.jsonl").write_text(
        '{"ts": "2026-09-06T00:00:00.000Z"}\n', encoding="utf-8", newline="\n"
    )


def _fail_walk_run(monkeypatch, *, only_for: str | None):
    """Collector.walk_run raises OSError for a conclusion that starts with ``only_for`` (any
    conclusion when None) and otherwise runs the real walk."""
    real = Collector.walk_run

    def walk_run(self, run_id, conclusion):
        if only_for is None or conclusion.startswith(only_for):
            raise OSError("disk went away")
        real(self, run_id, conclusion)

    monkeypatch.setattr(Collector, "walk_run", walk_run)


def test_a_recorder_gets_every_file_and_nothing_is_hashed(world, monkeypatch):
    _write_log(world)
    monkeypatch.setattr(evidence, "sha256_file", _never_hashed)
    monkeypatch.setattr(evidence, "sha256_prefix", _never_hashed)
    col = Collector(world.roots.data, world.roots.configs, recorder=(book := _Book()))
    col.walk_all(_paths(world, TEST), split=True)
    conclusions = book.conclusions()
    assert {"dataset:beach-test", "submission:beach-test/S1"} <= conclusions
    assert "all" not in conclusions
    assert all(c.startswith(("dataset:", "run:", "submission:beach-test/")) for c in conclusions)
    assert "logs" not in book.roles() and col.entries == {} and col.files_of() == []
    col.walk_all(_paths(world, EVAL), split=True)
    assert "judgement:beach/p-good" in book.conclusions()


def test_split_conclusions_name_the_walk_unit_of_each_file(world):
    col = Collector(world.roots.data, world.roots.configs, recorder=(book := _Book()))
    col.walk_all(_paths(world, TEST), split=True)
    by_conclusion: dict[str, set[str]] = {}
    for path, role, c in book.rows:
        by_conclusion.setdefault(c, set()).add(role)
        if role == "stage":
            assert c == "submission:beach-test/S1" and path.name == "stage.json"
    assert "run_card" in by_conclusion["run:good.test"]
    assert {"submit_profile", "stage", "artifact"} <= by_conclusion["submission:beach-test/S1"]
    assert {"samples", "plan", "dataset_card"} <= by_conclusion["dataset:beach-test"]
    assert "stage" not in by_conclusion["dataset:beach-test"]


def test_without_split_a_recorder_sees_the_backup_conclusion_and_the_logs(world):
    _write_log(world)
    col = Collector(world.roots.data, world.roots.configs, recorder=(book := _Book()))
    col.walk_all(_paths(world, TEST))
    assert book.conclusions() == {"all"} and "logs" in book.roles()


def test_a_unit_that_fails_takes_back_only_its_own_rows(world, monkeypatch):
    _fail_walk_run(monkeypatch, only_for="submission:")
    col = Collector(world.roots.data, world.roots.configs, recorder=(book := _Book()))
    col.walk_all(_paths(world, TEST), split=True)
    assert not any(c.startswith("submission:") for _, _, c in book.rows)
    assert "run:good.test" in book.conclusions() and "dataset:beach-test" in book.conclusions()
    assert col.skipped == ["submission:beach-test/S1: disk went away"]


def test_the_ledger_wide_walk_is_labelled_by_dataset_when_split(world):
    _paths(world, TEST).submit_yaml.write_bytes(b"dataset: [unclosed\n")
    col = Collector(world.roots.data, world.roots.configs, recorder=_Book())
    col.walk_all(_paths(world, TEST), split=True)
    assert len(col.skipped) == 1 and col.skipped[0].startswith("submissions:beach-test: ")
    plain = Collector(world.roots.data, world.roots.configs)
    plain.walk_all(_paths(world, TEST))
    assert len(plain.skipped) == 1 and plain.skipped[0].startswith("submissions: ")


def test_without_a_recorder_backup_still_stops_on_what_it_never_tolerated(world, monkeypatch):
    _fail_walk_run(monkeypatch, only_for=None)
    col = Collector(world.roots.data, world.roots.configs)
    with pytest.raises(OSError, match="disk went away"):
        col.walk_all(_paths(world, TEST))
