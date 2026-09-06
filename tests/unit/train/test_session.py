import os
import subprocess
import sys

import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.train import Session
from vcp.train.records import load_record, read_events, save_record
from vcp.train.schema import Attempt, TrainRecord

STAMP = "2026-09-05T00:00:00.000Z"


def _running(roots):
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="work",
        command=["python"],
        attempts=[Attempt(n=2, started_at=STAMP, console="train/console.2.log")],
    )
    save_record(roots.data, rec)
    return rec


def test_current_requires_the_wrapper_env(roots, monkeypatch):
    monkeypatch.delenv("VCP_RUN_ID", raising=False)
    with pytest.raises(ValidationFailed, match="VCP_RUN_ID"):
        Session.current()
    monkeypatch.setenv("VCP_RUN_ID", "ghost")
    with pytest.raises(ValidationFailed, match="no training record"):
        Session.current(roots.data)


def test_register_and_note_in_process(roots, monkeypatch, tmp_path):
    _running(roots)
    monkeypatch.setenv("VCP_RUN_ID", "r1")
    monkeypatch.setenv("VCP_DATA_ROOT", str(roots.data))
    ckpt = tmp_path / "epoch3.pt"
    ckpt.write_bytes(b"e3")
    s = Session.current()
    rec = s.register_checkpoint(ckpt)
    assert (
        rec.sha256 == sha256_file(ckpt)
        and rec.attempt == 2
        and rec.source == "session"
        and not rec.final
    )
    best = tmp_path / "best.pt"
    best.write_bytes(b"b")
    final = s.register_checkpoint(best, final=True)
    assert final.final
    s.note("val_auc", 0.91)
    stored = load_record(roots.data, "r1")
    assert [c.final for c in stored.checkpoints] == [False, True]
    events = read_events(roots.data, "r1")
    assert [e["event"] for e in events] == ["checkpoint", "checkpoint", "note"]
    assert events[2] == {**events[2], "key": "val_auc", "value": 0.91, "attempt": 2}
    assert s.register_checkpoint(ckpt).sha256 == rec.sha256  # same bytes: no duplicate
    assert len(load_record(roots.data, "r1").checkpoints) == 2
    with pytest.raises(ValidationFailed, match="not a file"):
        s.register_checkpoint(tmp_path / "nope.pt")


def test_session_from_a_child_process(roots, tmp_path):
    _running(roots)
    ckpt = tmp_path / "child.pt"
    ckpt.write_bytes(b"child")
    code = (
        "from vcp.train import Session; import sys; "
        "s = Session.current(); s.register_checkpoint(sys.argv[1], final=True); s.note('epoch', 1)"
    )
    env = {**os.environ, "VCP_RUN_ID": "r1", "VCP_DATA_ROOT": str(roots.data)}
    proc = subprocess.run(
        [sys.executable, "-c", code, str(ckpt)], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, proc.stderr
    stored = load_record(roots.data, "r1")
    assert stored.checkpoints[0].final and stored.checkpoints[0].sha256 == sha256_file(ckpt)
    assert read_events(roots.data, "r1")[-1]["key"] == "epoch"
