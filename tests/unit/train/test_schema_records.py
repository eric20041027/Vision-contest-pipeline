import errno
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.train.records import (
    EVENTS_LOG,
    TRAIN_YAML,
    append_event,
    current_attempt,
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
    assert EVENTS == (
        "started",
        "env",
        "checkpoint",
        "uploaded",
        "finished",
        "note",
        "access",
        "evidence",
    )


def test_record_defaults():
    r = _record()
    assert r.exports == [] and r.config is None and r.seed is None and r.framework == ""
    assert r.attempts == [] and r.checkpoints == [] and r.uploads == [] and r.notes == ""
    a = Attempt(n=1, started_at=STAMP, console="train/console.1.log")
    assert a.status == "running" and a.exit_code is None and a.env is None
    # 5-2: an attempt records the command / seed / venv IT ran with. A train.yaml written before
    # those fields existed simply has none of them, and must still load.
    assert a.command is None and a.seed is None and a.venv is None
    old = Attempt.model_validate(
        {"n": 2, "started_at": STAMP, "console": "train/console.2.log", "status": "finished"}
    )
    assert old.command is None and old.seed is None and old.venv is None
    full = Attempt(
        n=3, started_at=STAMP, console="c", command=["python", "t.py"], seed=7, venv="venvs/ultra"
    )
    assert full.command == ["python", "t.py"] and full.seed == 7 and full.venv == "venvs/ultra"
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


def test_append_event_refuses_to_let_a_payload_overwrite_its_own_keys(roots):
    """5-6: ``ts`` / ``event`` / ``attempt`` are this module's, and ``**payload`` came last --
    a caller passing one of them would silently forge the row's timestamp, kind or attempt
    number. No caller does; the point is that none can."""
    for reserved in ("ts", "event", "attempt"):
        with pytest.raises(ValueError, match=reserved):
            append_event(roots.data, "r1", "note", 1, **{reserved: "forged"})
    assert read_events(roots.data, "r1") == []  # and nothing reached the log
    append_event(roots.data, "r1", "note", 1, key="ok", value=1)
    assert read_events(roots.data, "r1")[0]["attempt"] == 1


def _full_disk_on_append(monkeypatch, target, *, fraction=0.5, at="write", also_append=b""):
    """Make appends to ``target`` die the way a full disk does and return the error they raise.

    ``at="write"``: ``fraction`` of the bytes handed to ``write`` reach the file, then
    ``OSError(ENOSPC)`` -- the OS taking part of a line before refusing the rest.
    ``at="close"``: the whole line is written and closing the file is what fails (a buffered
    writer only meets the full disk at its flush).
    ``also_append``: bytes another writer appends to the file after our partial row and before
    the error reaches the caller."""
    real_open = Path.open
    error = OSError(errno.ENOSPC, "No space left on device")

    class Failing:
        def __init__(self, f):
            self._f = f

        def write(self, data):
            if at == "close":
                return self._f.write(data)
            part = data[: int(len(data) * fraction)]
            if part:
                self._f.write(part)
                self._f.flush()
            if also_append:
                with open(target, "ab") as other:  # the builtin: not the patched Path.open
                    other.write(also_append)
            raise error

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()
            if at == "close" and exc[0] is None:
                raise error
            return False

        def __getattr__(self, name):
            return getattr(self._f, name)

    def patched(self, mode="r", *args, **kwargs):
        f = real_open(self, mode, *args, **kwargs)
        return Failing(f) if self == target and "a" in mode else f

    monkeypatch.setattr(Path, "open", patched)
    return error


def _two_rows(roots):
    append_event(roots.data, "r1", "started", 1, command=["python"], seed=None)
    append_event(roots.data, "r1", "note", 1, key="k", value="v")
    return events_path(roots.data, "r1")


@pytest.mark.parametrize(
    ("at", "fraction"),
    [("write", 0.0), ("write", 0.5), ("write", 0.99), ("close", 1.0)],
    ids=["no-byte-written", "half-a-line", "all-but-the-newline", "fails-on-close"],
)
def test_a_failed_append_leaves_train_log_as_it_was_and_the_error_unchanged(
    roots, monkeypatch, at, fraction
):
    """An ENOSPC halfway through a row must not leave half a line: the next append would join
    onto it and ``backup verify`` would report ``bad_stamps:`` for that file for ever (only the
    submissions ledger has a ``partial_tail`` allowance). The OSError itself is still what the
    caller sees -- ``own_writes`` turns it into ``disk_full:``."""
    path = _two_rows(roots)
    before = path.read_bytes()
    error = _full_disk_on_append(monkeypatch, path, fraction=fraction, at=at)

    with pytest.raises(OSError) as ei:
        append_event(roots.data, "r1", "note", 1, key="lost", value="x" * 300)

    assert ei.value is error and error.errno == errno.ENOSPC
    assert path.read_bytes() == before


def test_an_append_after_a_failed_one_leaves_every_line_parsable(roots, monkeypatch):
    path = _two_rows(roots)
    with monkeypatch.context() as m:
        _full_disk_on_append(m, path, fraction=0.5)
        with pytest.raises(OSError):
            append_event(roots.data, "r1", "note", 1, key="lost", value="x" * 300)

    append_event(roots.data, "r1", "note", 1, key="kept", value="y")

    lines = path.read_bytes().split(b"\n")
    assert lines[-1] == b""  # ends with a newline, nothing after it
    rows = [json.loads(line) for line in lines[:-1]]  # every line parses
    assert [r.get("key") for r in rows] == [None, "k", "kept"]
    assert read_events(roots.data, "r1") == rows


def test_a_failed_first_append_leaves_no_row_behind(roots, monkeypatch):
    path = events_path(roots.data, "r1")
    _full_disk_on_append(monkeypatch, path, fraction=0.5)
    with pytest.raises(OSError):
        append_event(roots.data, "r1", "started", 1, command=["python"], seed=None)
    assert read_events(roots.data, "r1") == []
    assert not path.exists() or path.read_bytes() == b""


def test_a_row_that_cannot_be_encoded_fails_before_the_file_is_touched(roots, monkeypatch):
    """A lone surrogate in a payload cannot be written as UTF-8. That is bad input, not a torn
    write: the line is encoded before the file is opened, so there is nothing to truncate."""
    path = _two_rows(roots)
    before = path.read_bytes()
    truncations = []
    monkeypatch.setattr(os, "truncate", lambda *args: truncations.append(args))

    with pytest.raises(UnicodeEncodeError):
        append_event(roots.data, "r1", "note", 1, key="lone", value="\ud800")

    assert path.read_bytes() == before and truncations == []


def test_a_row_that_cannot_be_encoded_does_not_create_the_file(roots):
    with pytest.raises(UnicodeEncodeError):
        append_event(roots.data, "r1", "note", 1, key="lone", value="\ud800")
    assert not events_path(roots.data, "r1").exists()


def test_a_failed_append_does_not_cut_a_row_another_writer_appended_after_it(roots, monkeypatch):
    """The truncate takes no lock. If the file has grown past where our row would have ended,
    somebody else wrote after us: their complete row stays, whatever it costs the torn one."""
    path = _two_rows(roots)
    before = path.read_bytes()
    foreign = (
        json.dumps({"ts": STAMP, "event": "note", "attempt": 1, "key": "other", "value": "z" * 600})
        + "\n"
    ).encode()
    error = _full_disk_on_append(monkeypatch, path, fraction=0.5, also_append=foreign)

    with pytest.raises(OSError) as ei:
        append_event(roots.data, "r1", "note", 1, key="lost", value="x" * 300)

    assert ei.value is error
    raw = path.read_bytes()
    assert raw.startswith(before) and raw.endswith(foreign)  # nothing was cut


def test_a_failing_truncate_does_not_replace_the_original_error(roots, monkeypatch):
    """Cleaning up is best effort: if the truncate fails too, the caller still gets the ENOSPC
    that started it, not the cleanup's error."""
    path = _two_rows(roots)
    error = _full_disk_on_append(monkeypatch, path, fraction=0.5)

    attempts = []

    def refuse(*args, **_kwargs):
        attempts.append(args)
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(os, "truncate", refuse)
    with pytest.raises(OSError) as ei:
        append_event(roots.data, "r1", "note", 1, key="lost", value="x" * 300)
    assert ei.value is error
    assert len(attempts) == 1  # it did try to clean up


def test_current_attempt_of_a_record_with_no_attempts_is_one(roots):
    """5-5: one helper for "the attempt a Session write belongs to", shared by Session.note and
    Session.register_checkpoint -- a record with no attempt yet still needs a number."""
    assert current_attempt(_record()) == 1
    two = _record(
        attempts=[
            Attempt(n=1, started_at=STAMP, console="train/console.1.log", status="finished"),
            Attempt(n=2, started_at=STAMP, console="train/console.2.log"),
        ]
    )
    assert current_attempt(two) == 2


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
