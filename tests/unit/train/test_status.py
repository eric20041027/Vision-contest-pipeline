import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.train.checkpoints import mark_final, register
from vcp.train.records import read_events, save_record
from vcp.train.schema import Attempt, TrainRecord
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


def test_upload_run_records_and_is_idempotent(roots, tmp_path):
    rec, w = _seeded(roots)
    rec2, out = upload_run(roots.data, "r1", str(tmp_path / "vault"))
    assert out.uploaded == 2 and len(rec2.uploads) == 2 and all(u.verified for u in rec2.uploads)
    events = read_events(roots.data, "r1")
    assert [e["event"] for e in events] == ["uploaded", "uploaded"] and events[0]["attempt"] == 1
    rec3, again = upload_run(roots.data, "r1", str(tmp_path / "vault"))
    assert again.uploaded == 0 and again.skipped == 2 and len(rec3.uploads) == 2
