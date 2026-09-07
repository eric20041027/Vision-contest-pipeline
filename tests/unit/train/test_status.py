import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.train.checkpoints import mark_final, register
from vcp.train.records import read_events, save_record
from vcp.train.schema import Attempt, TrainRecord, UploadRecord
from vcp.train.status import status, upload_run

STAMP = "2026-09-05T00:00:00.000Z"


def _seeded(roots):
    w = roots.data / "work" / "weights"
    w.mkdir(parents=True)
    (w / "best.pt").write_bytes(b"best")
    (w / "last.pt").write_bytes(b"last")
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="work",
        command=["python"],
        attempts=[
            Attempt(
                n=1, started_at=STAMP, console="train/console.1.log", status="finished", exit_code=0
            )
        ],
    )
    rec, _ = register(rec, [w / "best.pt", w / "last.pt"], data_root=roots.data, attempt=1)
    rec = mark_final(rec, "work/weights/best.pt", sha256_file(w / "best.pt"))
    save_record(roots.data, rec)
    return rec, w


def test_status_counts_backed_and_running(roots, tmp_path):
    rec, w = _seeded(roots)
    st = status(roots.data, "r1")
    assert st.backed == 0 and st.unbacked == ["work/weights/best.pt", "work/weights/last.pt"]
    assert st.missing == [] and st.drift == [] and st.running == 0
    upload_run(roots.data, "r1", str(tmp_path / "vault"), only_final=True)
    st = status(roots.data, "r1")
    assert st.backed == 1 and st.unbacked == ["work/weights/last.pt"]
    (w / "best.pt").write_bytes(b"tampered")
    (w / "last.pt").unlink()
    st = status(roots.data, "r1")
    assert st.missing == ["work/weights/last.pt"] and st.drift == []
    st = status(roots.data, "r1", verify=True)
    assert st.drift == ["work/weights/best.pt", "work/weights/last.pt"]
    running = rec.model_copy(
        update={
            "attempts": [
                rec.attempts[0].model_copy(update={"status": "running", "exit_code": None})
            ]
        }
    )
    save_record(roots.data, running)
    assert status(roots.data, "r1").running == 1
    with pytest.raises(ValidationFailed, match="not found"):
        status(roots.data, "ghost")


def test_status_keys_backed_by_bytes_not_path(roots):
    """C2 regression: a --resume that changes a checkpoint's bytes adds a second
    CheckpointRecord for the same path. A verified upload of the OLD bytes must not mark the
    NEW record backed -- backed-ness has to be keyed by sha256, not by path."""
    w = roots.data / "work" / "weights"
    w.mkdir(parents=True)
    (w / "best.pt").write_bytes(b"best-old")
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="work",
        command=["python"],
        attempts=[
            Attempt(
                n=1, started_at=STAMP, console="train/console.1.log", status="finished", exit_code=0
            )
        ],
    )
    rec, _ = register(rec, [w / "best.pt"], data_root=roots.data, attempt=1)
    old_sha = sha256_file(w / "best.pt")
    (w / "best.pt").write_bytes(b"best-new")
    rec, _ = register(rec, [w / "best.pt"], data_root=roots.data, attempt=1)
    new_sha = sha256_file(w / "best.pt")
    assert old_sha != new_sha
    rec = mark_final(rec, "work/weights/best.pt", new_sha)
    old_upload = UploadRecord(
        dest="vault", kind="local", name="best.pt", sha256=old_sha, verified=True, uploaded_at=STAMP
    )
    rec = rec.model_copy(update={"uploads": [old_upload]})
    save_record(roots.data, rec)

    st = status(roots.data, "r1")
    assert st.backed == 1 and st.unbacked == ["work/weights/best.pt"]

    rec = rec.model_copy(
        update={"uploads": [old_upload, old_upload.model_copy(update={"sha256": new_sha})]}
    )
    save_record(roots.data, rec)
    st = status(roots.data, "r1")
    assert st.unbacked == [] and st.backed == 2


def test_status_counts_superseded_bytes_apart_from_unbacked(roots, tmp_path):
    """5-10: a --resume that changes a checkpoint's bytes leaves the older record behind. Those
    bytes are gone from the path and, if they were never uploaded, no copy will ever appear --
    so `unbacked` listed them forever: honest, and pure noise. They are counted as `superseded`
    instead, leaving `unbacked` to mean "current bytes with no copy" -- the number to act on."""
    w = roots.data / "work" / "weights"
    w.mkdir(parents=True)
    (w / "best.pt").write_bytes(b"best-old")
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="work",
        command=["python"],
        attempts=[
            Attempt(
                n=1, started_at=STAMP, console="train/console.1.log", status="finished", exit_code=0
            )
        ],
    )
    rec, _ = register(rec, [w / "best.pt"], data_root=roots.data, attempt=1)
    old_sha = sha256_file(w / "best.pt")
    (w / "best.pt").write_bytes(b"best-new")
    rec, _ = register(rec, [w / "best.pt"], data_root=roots.data, attempt=2)
    save_record(roots.data, rec)

    # before any upload: the current bytes are unbacked, the old ones are already superseded
    st = status(roots.data, "r1")
    assert st.unbacked == ["work/weights/best.pt"] and st.superseded == ["work/weights/best.pt"]
    assert st.backed == 0

    # uploading takes the newest record of the path (spec 14-4), so only the new bytes get a copy
    _, out = upload_run(roots.data, "r1", str(tmp_path / "vault"))
    assert out.uploaded == 1
    st = status(roots.data, "r1")
    assert st.unbacked == [] and st.superseded == ["work/weights/best.pt"] and st.backed == 1
    assert old_sha not in {u.sha256 for u in st.record.uploads}


def test_upload_run_records_and_is_idempotent(roots, tmp_path):
    rec, w = _seeded(roots)
    rec2, out = upload_run(roots.data, "r1", str(tmp_path / "vault"))
    assert out.uploaded == 2 and len(rec2.uploads) == 2 and all(u.verified for u in rec2.uploads)
    events = read_events(roots.data, "r1")
    assert [e["event"] for e in events] == ["uploaded", "uploaded"] and events[0]["attempt"] == 1
    rec3, again = upload_run(roots.data, "r1", str(tmp_path / "vault"))
    assert again.uploaded == 0 and again.skipped == 2 and len(rec3.uploads) == 2
    assert len(read_events(roots.data, "r1")) == 2  # a re-verification is not an event


def test_superseded_names_a_path_once_however_many_times_it_was_replaced(roots):
    """Three registrations of one path with three shas, none uploaded: one superseded entry,
    not two -- the count is files, and the human lines must not repeat."""
    w = roots.data / "work" / "weights"
    w.mkdir(parents=True)
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="work",
        command=["python"],
        attempts=[
            Attempt(
                n=1, started_at=STAMP, console="train/console.1.log", status="finished", exit_code=0
            )
        ],
    )
    for n, payload in enumerate((b"v1", b"v2", b"v3"), start=1):
        (w / "best.pt").write_bytes(payload)
        rec, _ = register(rec, [w / "best.pt"], data_root=roots.data, attempt=n)
    save_record(roots.data, rec)
    st = status(roots.data, "r1")
    assert st.superseded == ["work/weights/best.pt"] and st.unbacked == ["work/weights/best.pt"]
