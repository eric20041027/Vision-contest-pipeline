"""``vcp.gc.references``: every file and directory the canonical records name, and who names it
(0.17.0 spec 5.3). The records are read, never hashed, and never through the provenance index."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from backup_fixtures import FakeRemote, make_world
from submit_fixtures import EVAL, STAMP, TEST
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.backup import evidence
from vcp.backup.evidence import Collector, build_manifest
from vcp.backup.push import push
from vcp.backup.schema import RemoteCopy
from vcp.core.config import dump_yaml_model
from vcp.core.errors import VcpError
from vcp.core.paths import artifact_dir
from vcp.data.dataset import Dataset
from vcp.gc.references import Reference, ReferenceRecorder, collect_references, path_key
from vcp.measure.runs import run_dir, save_run
from vcp.measure.schema import RunCard, RunSource
from vcp.submit.stage import stage_json
from vcp.train.checkpoints import register
from vcp.train.records import append_event, load_record, save_record
from vcp.train.schema import Attempt, ConfigRef, ExportRef, TrainRecord, UploadRecord


@pytest.fixture
def world(roots, tmp_path):
    return make_world(roots, tmp_path)


def _collect(world):
    return collect_references(world.roots.data, world.roots.configs)


def _referrers(refs, path: Path) -> frozenset[str]:
    return refs.files[path_key(path)].referrers


def _labels(refs) -> list[str]:
    return [s.split(": ", 1)[0] for s in refs.skipped]


def _fail_walk_run(monkeypatch, *, only_for: str, error: Exception):
    """Collector.walk_run raises ``error`` for a conclusion that starts with ``only_for`` and
    otherwise runs the real walk."""
    real = Collector.walk_run

    def walk_run(self, run_id, conclusion):
        if conclusion.startswith(only_for):
            raise error
        real(self, run_id, conclusion)

    monkeypatch.setattr(Collector, "walk_run", walk_run)


def test_a_checkpoint_a_submission_uses_has_both_referrers_and_its_copy(world):
    refs = _collect(world)

    best = refs.files[path_key(world.weights / "best.pt")]
    assert {"run:good#checkpoint_final", "submission:beach-test/S1#checkpoint_final"} <= (
        best.referrers
    )
    assert best.copy == str(world.vault) and best.sha256 is not None
    assert "run:good#upload_copy" in _referrers(refs, world.vault / "good" / "best.pt")
    assert refs.counts["runs"] >= 2 and refs.counts["datasets"] == 2


def test_train_record_dirs_are_referenced(world, tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("lr: 0.1\n", encoding="utf-8", newline="\n")
    record = load_record(world.roots.data, "good")
    record = record.model_copy(
        update={
            "exports": [
                ExportRef(
                    dir=str(tmp_path / "export"),
                    subset="train",
                    format="coco",
                    manifest_sha256="1" * 64,
                    sample_count=3,
                )
            ],
            "venv": str(tmp_path / "venv"),
            "config": ConfigRef(path=str(cfg), sha256="1" * 64, copied_to="train/config.yaml"),
            "attempts": [
                Attempt(
                    n=1,
                    started_at=STAMP,
                    console="train/console.1.log",
                    status="finished",
                    exit_code=0,
                    venv=str(tmp_path / "venv-a1"),
                )
            ],
            "uploads": [
                *record.uploads,
                UploadRecord(
                    dest="gdrive:backups",
                    kind="rclone",
                    name="best.pt",
                    sha256="2" * 64,
                    verified=True,
                    uploaded_at=STAMP,
                ),
            ],
        }
    )
    save_record(world.roots.data, record)

    refs = _collect(world)

    assert "run:good#export" in refs.dirs[path_key(tmp_path / "export")]
    assert "run:good#venv" in refs.dirs[path_key(tmp_path / "venv")]
    assert "run:good#venv" in refs.dirs[path_key(tmp_path / "venv-a1")]
    assert "run:good#config" in _referrers(refs, cfg)
    assert refs.files[path_key(cfg)].sha256 == "1" * 64
    # an rclone upload is no path on this machine
    assert not any("gdrive" in f.path.as_posix() for f in refs.files.values())


def test_image_roots_backups_and_artifact_inputs_are_referenced(world, tmp_path):
    roots, kw = world.roots, {"data_root": world.roots.data, "configs_root": world.roots.configs}
    images = tmp_path / "images"
    card = Dataset.load_card(TEST, **kw)
    dump_yaml_model(
        card.model_copy(update={"image_root": str(images)}), world.pair.test_paths.card_yaml
    )
    build_manifest(EVAL, "run:good", manifest_id="m1", **kw)
    push(EVAL, "m1", str(tmp_path / "usb"), **kw)
    push(EVAL, "m1", "fake:vault", runner=FakeRemote(), **kw)
    src = tmp_path / "src.bin"
    src.write_bytes(b"source")
    spec = ArtifactSpec(kind="note", id="n1", inputs=[InputRef(name="src", path=str(src))])
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        art.write_text("note.txt", "hello\n")
        art.commit()

    refs = _collect(world)

    assert "dataset:beach-test#image_root" in refs.dirs[path_key(images)]
    assert "backup:beach/m1#backup_dest" in refs.dirs[path_key(tmp_path / "usb")]
    assert not any("fake" in d for d in refs.dirs)  # an rclone destination is no directory here
    assert "backup:beach/m1#checkpoint_final" in _referrers(refs, world.weights / "best.pt")
    assert "artifact:note/n1#artifact_input" in _referrers(refs, src)
    assert refs.counts["backup_manifests"] == 1 and refs.counts["artifacts"] >= 1


def test_a_later_reference_with_a_recorded_sha_fills_in_the_first_that_had_none(world):
    kw = {"data_root": world.roots.data, "configs_root": world.roots.configs}
    build_manifest(EVAL, "run:good", manifest_id="m1", **kw)
    log = run_dir(world.roots.data, "good") / "train.log.jsonl"

    ref = _collect(world).files[path_key(log)]

    assert {"run:good#train_log", "backup:beach/m1#train_log"} <= ref.referrers
    assert ref.sha256 is not None and ref.size == log.stat().st_size


def test_a_run_of_another_checkout_is_still_walked(world, tmp_path):
    data = world.roots.data
    model = tmp_path / "ext" / "m.pt"
    model.parent.mkdir()
    model.write_bytes(b"m")
    save_run(
        data,
        RunCard(
            run_id="far",
            dataset="elsewhere",
            samples_hash="0" * 64,
            plan_id="fixed-v1",
            trained_on=[],
            source=RunSource(),
            created_at=STAMP,
        ),
    )
    record = TrainRecord(
        run_id="far",
        dataset="elsewhere",
        plan_id="fixed-v1",
        trained_on=[],
        config_hash="ab" * 32,
        cwd="w",
        command=["python"],
    )
    record, _ = register(record, [model], data_root=data, attempt=1)
    save_record(data, record)

    refs = _collect(world)

    assert "run:far#checkpoint" in _referrers(refs, model)


def test_a_failed_submission_unit_leaves_no_referrer_behind(world, monkeypatch):
    _fail_walk_run(monkeypatch, only_for="submission:", error=OSError("boom"))

    refs = _collect(world)

    assert not any(r.startswith("submission:") for f in refs.files.values() for r in f.referrers)
    assert path_key(stage_json(world.pair.test_paths, "S1")) not in refs.files
    assert refs.skipped == ["submission:beach-test/S1: boom"]


def test_a_broken_dataset_card_skips_its_dataset_whole(world):
    """The dataset is one unit: its own files, its judgements and submissions, and its image
    root. A card that does not load takes all of them back and says which dataset it was."""
    paths = world.pair.test_paths
    paths.card_yaml.write_bytes(b"name: [unclosed\n")

    refs = _collect(world)

    assert [s for s in _labels(refs) if s.startswith("dataset:")] == ["dataset:beach-test"]
    assert refs.counts["datasets"] == 1
    assert not any(
        r.startswith(("dataset:beach-test#", "submission:beach-test/"))
        for f in refs.files.values()
        for r in f.referrers
    )
    assert path_key(stage_json(paths, "S1")) not in refs.files
    assert "run:good.test#run_card" in _referrers(
        refs, run_dir(world.roots.data, "good.test") / "run.yaml"
    )


def test_unreadable_run_cards_are_skipped_once(world, monkeypatch):
    bad = run_dir(world.roots.data, "x")
    bad.mkdir(parents=True)
    (bad / "run.yaml").write_bytes(b"\xff\xfe\x00bad")
    real = evidence.load_run

    def load_run_(data_root, run_id):
        if run_id == "bad":
            raise PermissionError(13, "denied")
        return real(data_root, run_id)

    monkeypatch.setattr(evidence, "load_run", load_run_)

    refs = _collect(world)

    labels = _labels(refs)
    assert labels.count("run:x") == 1 and labels.count("run:bad") == 1
    assert {"x", "bad"} <= refs.skipped_runs


def test_a_broken_train_record_skips_its_run_and_aborts_nothing(world):
    data = world.roots.data
    (run_dir(data, "good") / "train.yaml").write_bytes(b"run_id: [unclosed\n")

    refs = _collect(world)

    assert "good" in refs.skipped_runs and refs.running == [] and refs.checkpoint_dirs == []
    assert any(s.startswith("run:good: ") for s in refs.skipped)
    assert path_key(run_dir(data, "good") / "run.yaml") not in refs.files
    assert "run:bad#run_card" in _referrers(refs, run_dir(data, "bad") / "run.yaml")


def test_a_broken_backup_manifest_or_artifact_is_skipped_by_name(world):
    data = world.roots.data
    manifests = world.pair.eval_paths.backup_dir
    manifests.mkdir(parents=True)
    (manifests / "bad.json").write_bytes(b"{")
    broken = artifact_dir(data, "note", "broken")
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_bytes(b"{}")

    refs = _collect(world)

    assert {"backup:beach/bad", "artifact:note/broken"} <= set(_labels(refs))
    assert refs.counts["backup_manifests"] == 0


def test_a_programming_error_is_not_tolerated(world, monkeypatch):
    _fail_walk_run(monkeypatch, only_for="run:", error=KeyError("oops"))

    with pytest.raises(KeyError):
        _collect(world)


def test_collecting_references_hashes_nothing(world, monkeypatch):
    def never(*_args, **_kwargs):
        raise AssertionError("collecting references hashes nothing")

    monkeypatch.setattr(evidence, "sha256_file", never)
    monkeypatch.setattr(evidence, "sha256_prefix", never)

    refs = _collect(world)

    assert refs.files and refs.counts["runs"] >= 2


def test_running_checkpoint_dirs_and_missing(world):
    data = world.roots.data
    record = load_record(data, "good")
    running = Attempt(n=1, started_at=STAMP, console="train/console.1.log")  # status: running
    save_record(data, record.model_copy(update={"attempts": [*record.attempts, running]}))
    before = _collect(world).missing

    (world.weights / "last.pt").unlink()
    refs = _collect(world)

    assert refs.running == ["good"] and refs.checkpoint_dirs == [world.weights.resolve()]
    assert refs.missing == before + 1


def test_a_cwd_that_cannot_be_examined_is_reported_unreadable(world, monkeypatch):
    data = world.roots.data
    append_event(
        data,
        "good",
        "started",
        1,
        command=["python"],
        seed=None,
        checkpoints=["c/*.pt"],
        final=None,
    )
    real = Path.is_dir

    def is_dir(self):
        if self.name == "w":  # the record's cwd
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "is_dir", is_dir)

    refs = _collect(world)

    assert refs.unreadable == ["w"] and refs.checkpoint_dirs == [world.weights.resolve()]


def test_an_event_log_that_cannot_be_read_is_unreadable_and_the_run_still_counts(
    world, monkeypatch
):
    data = world.roots.data
    record = load_record(data, "good")
    running = Attempt(n=1, started_at=STAMP, console="train/console.1.log")  # status: running
    save_record(data, record.model_copy(update={"attempts": [running]}))
    real = Path.read_bytes

    def read_bytes(self):
        if self.name == "train.log.jsonl":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    refs = _collect(world)

    assert refs.unreadable == ["runs/good/train.log.jsonl"]
    assert refs.running == ["good"] and refs.skipped == []
    assert "run:good#checkpoint_final" in _referrers(refs, world.weights / "best.pt")


def test_the_recorder_resolves_paths_and_a_rollback_truncates(tmp_path):
    book = ReferenceRecorder()
    book.add(
        tmp_path / "a" / ".." / "f.bin",
        "checkpoint",
        "run:r",
        sha256="1" * 64,
        size=3,
        remote=RemoteCopy(dest="vault", run="r", name="f.bin"),
    )
    mark = book.mark()
    book.add_dir(tmp_path / "d", "venv", "run:r")

    assert book.refs == [
        Reference((tmp_path / "f.bin").resolve(), "run:r#checkpoint", False, "1" * 64, 3, "vault"),
        Reference((tmp_path / "d").resolve(), "run:r#venv", True, None, None, None),
    ]
    book.rollback(mark)
    assert len(book.refs) == 1
    assert issubclass(OSError, book.tolerates) and issubclass(VcpError, book.tolerates)
    assert not issubclass(KeyError, book.tolerates)


def test_a_path_key_is_the_resolved_path_in_the_platform_case(tmp_path):
    assert path_key(tmp_path / "a" / ".." / "B") == os.path.normcase(
        str((tmp_path / "B").resolve())
    )
    assert path_key(tmp_path / "B") == path_key(Path(str(tmp_path / "B")))


def test_gc_reads_the_records_not_the_provenance_index(world, monkeypatch):
    index = world.roots.data / "indexes" / "provenance-0123456789abcdef.sqlite3"
    index.parent.mkdir(parents=True)
    index.write_bytes(b"not a database")

    def never(*_args, **_kwargs):
        raise AssertionError("gc scan never opens the provenance index")

    monkeypatch.setattr(sqlite3, "connect", never)

    refs = _collect(world)

    assert refs.files and refs.skipped == [] and path_key(index) not in refs.files
