import os
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import det_samples, make_card, write_images
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.access.receipt import read_receipt
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.train import Session
from vcp.train import checkpoints as ckptmod
from vcp.train import session as sessionmod
from vcp.train.records import load_record, read_events, save_record
from vcp.train.schema import Attempt, TrainRecord
from vcp.train.session import SessionBinding

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


def test_register_checkpoint_hashes_the_file_once(roots, monkeypatch, tmp_path):
    """5-5: the session hashed the checkpoint, then `register` hashed the same bytes again --
    twice through a multi-GB file on every epoch that saved one. One read, one digest, shared."""
    _running(roots)
    real = sha256_file
    hashed: list[Path] = []

    def counting(path):
        hashed.append(Path(path))
        return real(path)

    monkeypatch.setattr(sessionmod, "sha256_file", counting)
    monkeypatch.setattr(ckptmod, "sha256_file", counting)
    ckpt = tmp_path / "big.pt"
    ckpt.write_bytes(b"weights")
    entry = Session("r1", roots.data).register_checkpoint(ckpt, final=True)
    assert hashed == [ckpt.resolve()]
    assert entry.sha256 == real(ckpt) and entry.final and entry.attempt == 2


def test_session_writes_belong_to_attempt_one_before_any_attempt_exists(roots, tmp_path):
    """5-5: Session and register_checkpoint answer "which attempt is this?" the same way --
    one helper, so a record with no attempt yet cannot number the two writes differently."""
    rec = TrainRecord(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="work",
        command=["python"],
    )
    save_record(roots.data, rec)
    ckpt = tmp_path / "e1.pt"
    ckpt.write_bytes(b"e1")
    s = Session("r1", roots.data)
    assert s.register_checkpoint(ckpt).attempt == 1
    s.note("epoch", 1)
    assert [e["attempt"] for e in read_events(roots.data, "r1")] == [1, 1]


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


def _dataset(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(40, seed=0)
    write_images(roots.data / "raw" / "tiny", samples)
    ds = Dataset.from_parts(make_card("det", name="tiny", image_root="raw/tiny"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return plan


def test_session_access_binds_receipts_to_the_current_attempt(roots, monkeypatch):
    plan = _dataset(roots)
    _running(roots)  # attempt 2 is running
    monkeypatch.setenv("VCP_RUN_ID", "r1")
    session = Session.current(roots.data)
    binding = SessionBinding(session)
    assert (binding.run_id, binding.attempt, binding.next_seq()) == ("r1", 2, 1)
    assert binding.receipt_id(3) == "r1-a2-3"
    with session.access(subsets={"train"}) as access:
        assert access.receipt_id == "r1-a2-1" and access.purpose == "train"
        list(access.iter("train"))
    with session.access(roles={"train"}, notes="second") as again:
        pass
    assert again.receipt_id == "r1-a2-2"
    record = load_record(roots.data, "r1")
    assert [r.artifact_id for r in record.access] == ["r1-a2-1", "r1-a2-2"]
    assert record.access[0].subsets == ["train"] and record.access[0].binding == "session"
    assert record.access[0].receipt_sha256 == read_receipt(roots.data, "r1-a2-1").sha256
    events = [e for e in read_events(roots.data, "r1") if e["event"] == "access"]
    assert [e["artifact_id"] for e in events] == ["r1-a2-1", "r1-a2-2"]
    assert events[0]["attempt"] == 2 and events[0]["subsets"] == ["train"]
    assert events[0]["denied"] == 0 and events[0]["sealed_accessed"] is False
    receipt = read_receipt(roots.data, "r1-a2-1").receipt
    assert receipt.run_id == "r1" and receipt.attempt == 2 and receipt.plan_id == plan.plan_id
