import json

import pytest
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.train.records import (
    EVENTS_LOG,
    TRAIN_YAML,
    append_event,
    events_path,
    has_record,
    load_record,
    read_events,
    save_record,
    train_dir,
    train_yaml,
)
from vcp.train.schema import (
    EVENTS,
    Attempt,
    CheckpointRecord,
    EnvSnapshot,
    ExportRef,
    TrainRecord,
    UploadRecord,
)

STAMP = "2026-09-05T00:00:00.000Z"


def _record(**kw) -> TrainRecord:
    base = dict(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="projects/tiny",
        command=["python", "train.py"],
    )
    return TrainRecord(**{**base, **kw})


def test_models_reject_unknown_fields_and_bad_literals():
    with pytest.raises(ValidationError):
        Attempt(n=1, started_at=STAMP, console="train/console.1.log", status="done")
    with pytest.raises(ValidationError):
        CheckpointRecord(
            path="a", sha256="x", bytes=1, registered_at=STAMP, attempt=1, source="disk"
        )
    with pytest.raises(ValidationError):
        UploadRecord(dest="d", kind="ftp", name="a", sha256="x", verified=True, uploaded_at=STAMP)
    with pytest.raises(ValidationError):
        TrainRecord(**{**_record().model_dump(), "extra": 1})
    assert EVENTS == ("started", "env", "checkpoint", "uploaded", "finished", "note")


def test_record_defaults():
    r = _record()
    assert r.exports == [] and r.config is None and r.seed is None and r.framework == ""
    assert r.attempts == [] and r.checkpoints == [] and r.uploads == [] and r.notes == ""
    a = Attempt(n=1, started_at=STAMP, console="train/console.1.log")
    assert a.status == "running" and a.exit_code is None and a.env is None
    e = ExportRef(
        dir="exports/x",
        subset="train",
        format="yolo",
        manifest_sha256="cd" * 32,
        sample_count=3,
    )
    assert e.sample_count == 3


def test_paths(roots):
    assert train_yaml(roots.data, "r1") == roots.data / "runs" / "r1" / TRAIN_YAML
    assert events_path(roots.data, "r1") == roots.data / "runs" / "r1" / EVENTS_LOG
    assert train_dir(roots.data, "r1") == roots.data / "runs" / "r1" / "train"
    with pytest.raises(ValidationFailed, match="invalid name"):
        train_yaml(roots.data, "../r1")


def test_save_load_roundtrip_and_identity(roots):
    assert not has_record(roots.data, "r1")
    path = save_record(roots.data, _record(seed=7, framework="fake 1.0"))
    assert path == train_yaml(roots.data, "r1") and has_record(roots.data, "r1")
    assert b"\r" not in path.read_bytes()
    loaded = load_record(roots.data, "r1")
    assert (
        loaded.seed == 7
        and loaded.framework == "fake 1.0"
        and loaded.command == ["python", "train.py"]
    )
    with pytest.raises(ValidationFailed, match="not found") as ei:
        load_record(roots.data, "nope")
    assert ei.value.fields == {"run": "nope"}
    save_record(roots.data, _record(run_id="other"))
    (roots.data / "runs" / "r1" / TRAIN_YAML).write_text(
        (roots.data / "runs" / "other" / TRAIN_YAML).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="names run 'other'") as ei:
        load_record(roots.data, "r1")
    assert ei.value.fields == {"run": "r1"}


def test_events_append_only(roots):
    append_event(roots.data, "r1", "started", 1, command=["python"], seed=None)
    append_event(roots.data, "r1", "checkpoint", 1, path="w/best.pt", sha256="ab" * 32, bytes=10)
    rows = read_events(roots.data, "r1")
    assert [r["event"] for r in rows] == ["started", "checkpoint"]
    assert rows[0]["attempt"] == 1 and rows[0]["command"] == ["python"] and rows[0]["seed"] is None
    assert rows[0]["ts"].endswith("Z") and rows[1]["bytes"] == 10
    raw = events_path(roots.data, "r1").read_bytes()
    assert raw.count(b"\n") == 2 and b"\r" not in raw
    assert json.loads(raw.splitlines()[1])["path"] == "w/best.pt"
    with pytest.raises(ValueError, match="unknown event"):
        append_event(roots.data, "r1", "bogus", 1)
    assert read_events(roots.data, "none") == []


def test_env_snapshot_model():
    snap = EnvSnapshot(
        python="3.12.0",
        executable="C:/venv/Scripts/python.exe",
        platform="Windows-11",
        hostname="box",
        packages={"numpy": "2.1.0"},
        vcp_version="0.1.0",
        taken_at=STAMP,
    )
    assert snap.gpus == [] and snap.torch is None and snap.git is None
    assert EnvSnapshot.model_validate(snap.model_dump(mode="json")) == snap
