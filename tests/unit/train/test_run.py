import errno
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from helpers import (
    ROOMY_BYTES,
    det_samples,
    det_with_runs,
    git,
    git_repo,
    make_card,
    make_label_set,
    write_images,
)
from vcp.core import atomic
from vcp.core.errors import PlanMismatchError, ValidationFailed, VcpError
from vcp.core.hashing import sha256_file, sha256_json
from vcp.core.paths import DatasetPaths
from vcp.data.access.access import DatasetAccess
from vcp.data.dataset import Dataset
from vcp.data.exporters import ExportSpec, export_subset
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.data.source_audit import write_source_audit
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.provenance import attach_receipts
from vcp.measure.runs import load_run, run_dir, save_run
from vcp.train import disk
from vcp.train import run as run_module
from vcp.train.disk import GIB, free_gib
from vcp.train.records import load_record, read_events, save_record
from vcp.train.run import (
    RUN_BOUND_ELSEWHERE,
    RUN_EXISTS,
    TRAINED_ON_MISMATCH,
    RunSpec,
    command_found,
    config_hash,
    derive_trained_on,
    execute,
    train_run,
)
from vcp.train.status import status as status_view
from vcp.train.status import upload_run

FAKE = """
import os, sys, time
from pathlib import Path
print("VCP_RUN_ID", os.environ.get("VCP_RUN_ID"))
print("VCP_SEED", os.environ.get("VCP_SEED"), "HASHSEED", os.environ.get("PYTHONHASHSEED"))
code = int(sys.argv[1]) if len(sys.argv) > 1 else 0
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best-" + str(code).encode())
Path("weights/last.pt").write_bytes(b"last")
if len(sys.argv) > 2 and sys.argv[2] == "sleep":
    for i in range(50):
        print("tick", i, flush=True)
        time.sleep(0.1)
sys.exit(code)
"""

# C1 regression: a hand-written loop registers a checkpoint and a note via Session WHILE
# train_run's child is still running -- train.yaml must keep what Session wrote.
SESSION_FAKE = """
from pathlib import Path
from vcp.train import Session

Path("weights").mkdir(exist_ok=True)
Path("weights/epoch.pt").write_bytes(b"epoch-weights")
s = Session.current()
s.register_checkpoint("weights/epoch.pt", final=True)
s.note("val_auc", 0.9)
"""

# 5-1: prints one line (so on_line runs) and then outlives any patience the wrapper has.
SLEEPER = """
import time
print("tick", flush=True)
time.sleep(30)
"""

# C2/I1/I3 + resume regression: the command is byte-identical across attempts, but a counter
# file (persisted in cwd, which --resume shares) makes best.pt's bytes differ between attempts,
# the way a real trainer's weights differ after more epochs. last.pt never changes.
RESUME_FAKE = """
from pathlib import Path

state = Path("state.txt")
counter = int(state.read_text()) if state.is_file() else 0
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best-" + str(counter).encode())
Path("weights/last.pt").write_bytes(b"last")
state.write_text(str(counter + 1))
"""

# Wave 1b-1: a loop that reads its subset through the reader under the run's session.
ACCESS_FAKE = """
from pathlib import Path
from vcp.train import MaterializedReader

with MaterializedReader("tiny", "npy", plan_id="fixed-v1", subset="train") as reader:
    n = sum(1 for _ in reader)
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best-%d" % n)
"""

# ... and one that also peeks at valA: the receipt says so, whatever trained_on claims.
PEEK_FAKE = (
    ACCESS_FAKE
    + """
from vcp.train import Session

with Session.current().access(subsets={"valA"}) as peek:
    list(peek.iter("valA"))
"""
)


# VCP-043: the child learns its attempt number, and it agrees with the receipt ids it leaves.
ATTEMPT_FAKE = """
import os
from pathlib import Path
from vcp.train import Session

s = Session.current()
print("VCP_ATTEMPT", os.environ.get("VCP_ATTEMPT"), "SESSION", s.attempt)
with s.access(subsets={"train"}) as access:
    list(access.iter("train"))
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best")
"""


def _seed(roots, name="tiny", n=40):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def _export(roots, subset, out):
    return export_subset(
        ExportSpec(
            name="tiny",
            plan_id="fixed-v1",
            subset=subset,
            format="yolo",
            out=out,
            data_root=roots.data,
            configs_root=roots.configs,
        )
    ).out


@pytest.fixture
def work(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "fake_train.py").write_text(FAKE, encoding="utf-8")
    return tmp_path / "work"


def _spec(roots, work, **kw):
    base = dict(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        cwd=work,
        command=[sys.executable, "fake_train.py", "0"],
        checkpoints=["weights/*.pt"],
        final="weights/best.pt",
        data_root=roots.data,
        configs_root=roots.configs,
    )
    return RunSpec(**{**base, **kw})


def test_derive_trained_on_from_exports(roots, tmp_path):
    ds, plan, paths = _seed(roots)
    train = _export(roots, "train", tmp_path / "e-train")
    val = _export(roots, "valA", tmp_path / "e-valA")
    names, refs = derive_trained_on([train, val], [], card=ds.card, plan=plan, data_root=roots.data)
    assert names == ["train", "valA"] and [r.subset for r in refs] == ["train", "valA"]
    assert refs[0].format == "yolo" and refs[0].manifest_sha256 == sha256_file(
        train / "manifest.json"
    )
    assert refs[0].sample_count == len(plan.ids_in("train"))
    assert derive_trained_on([train], ["train"], card=ds.card, plan=plan, data_root=roots.data)[
        0
    ] == ["train"]
    with pytest.raises(ValidationFailed, match=TRAINED_ON_MISMATCH):
        derive_trained_on([train], ["valA"], card=ds.card, plan=plan, data_root=roots.data)
    with pytest.raises(ValidationFailed, match="trained_on is required"):
        derive_trained_on([], [], card=ds.card, plan=plan, data_root=roots.data)
    with pytest.raises(PlanMismatchError, match="no subset"):
        derive_trained_on([], ["nope"], card=ds.card, plan=plan, data_root=roots.data)
    manifest = train / "manifest.json"
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    manifest.write_text(json.dumps({**doc, "plan_id": "other"}), encoding="utf-8")
    with pytest.raises(PlanMismatchError, match="plan_id") as ei:
        derive_trained_on([train], [], card=ds.card, plan=plan, data_root=roots.data)
    assert ei.value.fields == {"export": str(train)}
    with pytest.raises(ValidationFailed, match="manifest.json not found"):
        derive_trained_on([tmp_path / "nowhere"], [], card=ds.card, plan=plan, data_root=roots.data)


def test_config_hash_two_routes(tmp_path):
    cmd = ["yolo", "train", "imgsz=640"]
    assert config_hash(None, cmd) == sha256_json(cmd)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("lr: 0.01\n", encoding="utf-8")
    assert config_hash(cfg, cmd) == sha256_file(cfg)
    with pytest.raises(ValidationFailed, match="--config not found"):
        config_hash(tmp_path / "none.yaml", cmd)


def test_execute_tees_and_reports_exit_code(work, tmp_path):
    console = tmp_path / "console.log"
    seen = []
    code, status = execute(
        [sys.executable, "fake_train.py", "0"],
        cwd=work,
        env=None,
        console=console,
        on_line=seen.append,
    )
    assert (code, status) == (0, "finished")
    text = console.read_text(encoding="utf-8")
    assert "VCP_RUN_ID None" in text and "".join(seen) == text
    code, status = execute(
        [sys.executable, "fake_train.py", "3"], cwd=work, env=None, console=console, on_line=None
    )
    assert (code, status) == (3, "failed")


def test_execute_interrupt_terminates_child(work, tmp_path):
    def boom(line):
        if line.startswith("tick 2"):
            raise KeyboardInterrupt

    code, status = execute(
        [sys.executable, "fake_train.py", "0", "sleep"],
        cwd=work,
        env=None,
        console=tmp_path / "c.log",
        on_line=boom,
    )
    assert status == "interrupted" and code != 0
    assert "[vcp] interrupted" in (tmp_path / "c.log").read_text(encoding="utf-8")


def test_execute_terminates_the_child_before_re_raising_any_exception(work, tmp_path, monkeypatch):
    """5-1: only KeyboardInterrupt used to terminate the child. Anything else raised by
    ``on_line`` (a --json writer failing, a bug in the caller) left the child running and
    ``with Popen`` waiting for it -- a training command sleeping for hours hung vcp with it.
    Now every exception terminates the child, on a bounded wait, and is re-raised unchanged."""
    (work / "sleeper.py").write_text(SLEEPER, encoding="utf-8")
    real_popen = subprocess.Popen
    spawned = {}

    def spy(*args, **kwargs):
        spawned["proc"] = proc = real_popen(*args, **kwargs)
        return proc

    monkeypatch.setattr(subprocess, "Popen", spy)

    def boom(line):
        raise RuntimeError("the caller blew up")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="the caller blew up"):
        execute(
            [sys.executable, "sleeper.py"],
            cwd=work,
            env=None,
            console=tmp_path / "c.log",
            on_line=boom,
        )
    assert time.monotonic() - started < 2.0  # not the child's 30 s
    assert spawned["proc"].poll() is not None  # and the child is gone, not orphaned


def test_command_found_resolves_relative_to_cwd(tmp_path, monkeypatch):
    work = tmp_path / "proj"
    work.mkdir()
    (work / "train.sh").write_text("echo hi\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # the process cwd is NOT the training cwd
    assert command_found("train.sh", work, None)
    assert command_found("./train.sh", work, None)
    assert command_found(str(work / "train.sh"), tmp_path, None)
    assert command_found(sys.executable, work, None)
    assert not command_found("no-such-binary-xyz", work, None)
    assert not command_found("train.sh", tmp_path, None)


def test_train_run_happy_path_writes_run_and_record(roots, work, tmp_path):
    ds, plan, paths = _seed(roots)
    export = _export(roots, "train", tmp_path / "e-train")
    seen = []
    res = train_run(
        _spec(
            roots,
            work,
            exports=[export],
            trained_on=[],
            seed=7,
            framework="fake 1.0",
            uploads=[str(tmp_path / "vault")],
            on_line=seen.append,
        )
    )
    assert res.attempt.n == 1 and res.attempt.status == "finished" and res.attempt.exit_code == 0
    assert (
        res.registered == 2 and res.final is not None and res.final.path.endswith("weights/best.pt")
    )
    assert (
        res.uploaded == 2
        and res.verified == 2
        and res.skipped == 0
        and res.warnings == ["venv=inherited"]
    )
    assert "VCP_RUN_ID r1" in "".join(seen) and "VCP_SEED 7 HASHSEED 7" in "".join(seen)
    card = load_run(roots.data, "r1")
    assert card.trained_on == ["train"] and card.source.framework == "fake 1.0"
    assert card.source.config_hash == sha256_json([sys.executable, "fake_train.py", "0"])
    assert card.source.export_manifest_sha == sha256_file(export / "manifest.json")
    assert (
        card.source.weights_hash == sha256_file(work / "weights" / "best.pt")
        and card.predictions == {}
    )
    rec = load_record(roots.data, "r1")
    assert rec.exports[0].subset == "train" and rec.seed == 7 and rec.config is None
    assert (
        rec.attempts[0].console == "train/console.1.log"
        and rec.attempts[0].env == "train/env.1.json"
    )
    assert rec.attempts[0].duration_s is not None and rec.attempts[0].duration_s >= 0
    assert [c.final for c in rec.checkpoints] == [True, False] and all(
        u.verified for u in rec.uploads
    )
    run = run_dir(roots.data, "r1")
    assert "VCP_RUN_ID r1" in (run / "train" / "console.1.log").read_text(encoding="utf-8")
    env = json.loads((run / "train" / "env.1.json").read_text(encoding="utf-8"))
    assert "pydantic" in env["packages"] and env["taken_at"].endswith("Z")
    assert [e["event"] for e in read_events(roots.data, "r1")] == [
        "started",
        "env",
        "finished",
        "checkpoint",
        "checkpoint",
        "uploaded",
        "uploaded",
    ]
    assert sha256_file(tmp_path / "vault" / "r1" / "best.pt") == card.source.weights_hash


def test_train_run_config_file_is_copied_and_hashed(roots, work, tmp_path):
    _seed(roots)
    cfg = work / "cfg.yaml"
    cfg.write_text("epochs: 1\n", encoding="utf-8")
    res = train_run(_spec(roots, work, config=cfg))
    assert res.card.source.config_hash == sha256_file(cfg)
    assert res.record.config is not None and res.record.config.copied_to == "train/config.1.yaml"
    assert (run_dir(roots.data, "r1") / "train" / "config.1.yaml").read_text(
        encoding="utf-8"
    ) == "epochs: 1\n"


def test_train_run_failed_command_registers_but_does_not_upload(roots, work, tmp_path):
    _seed(roots)
    res = train_run(
        _spec(
            roots,
            work,
            command=[sys.executable, "fake_train.py", "2"],
            uploads=[str(tmp_path / "vault")],
        )
    )
    assert res.attempt.status == "failed" and res.attempt.exit_code == 2
    assert res.registered == 2 and res.final is None and res.uploaded == 0
    # 5-8: the warning names the attempt's status, because "command failed" was a lie whenever
    # the attempt ended some other way (see the interrupted case below).
    assert "final=skipped (attempt failed)" in res.warnings
    assert not (tmp_path / "vault").exists()
    assert load_run(roots.data, "r1").source.weights_hash is None


def test_train_run_interrupted_attempt_says_so_in_the_final_warning(roots, work, tmp_path):
    """5-8: an interrupted attempt did not "fail" -- nothing about the command went wrong, it
    was stopped. The warning reads the attempt's own status instead of assuming one."""
    _seed(roots)

    def boom(line):
        raise KeyboardInterrupt

    res = train_run(
        _spec(roots, work, command=[sys.executable, "fake_train.py", "0"], on_line=boom)
    )
    assert res.attempt.status == "interrupted"
    assert "final=skipped (attempt interrupted)" in res.warnings
    assert res.final is None


def test_train_run_checks_before_writing(roots, work, tmp_path):
    ds, plan, paths = _seed(roots)
    with pytest.raises(ValidationFailed, match="training command is required"):
        train_run(_spec(roots, work, command=[]))
    with pytest.raises(ValidationFailed, match="command not found"):
        train_run(_spec(roots, work, command=["no-such-binary-xyz", "a"]))
    with pytest.raises(PlanMismatchError):
        train_run(_spec(roots, work, plan_id="fixed-v1", trained_on=["ghost"]))
    with pytest.raises(ValidationFailed, match="--final .* matched 0"):
        train_run(_spec(roots, work, final="weights/none.pt", run_id="r-final"))
    assert not run_dir(roots.data, "r1").exists()
    # r-final wrote its record before the final check
    # (spec 6.1: final is resolved after the command)
    assert load_record(roots.data, "r-final").attempts[0].status == "finished"


def test_train_run_existing_runs(roots, work, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)  # run "perfect" is an ingested run
    (work / "weights").mkdir(exist_ok=True)
    with pytest.raises(ValidationFailed, match=RUN_BOUND_ELSEWHERE) as ei:
        train_run(_spec(roots, work, run_id="perfect"))
    assert ei.value.fields == {"run": "perfect"}
    first = train_run(_spec(roots, work))
    with pytest.raises(ValidationFailed, match=RUN_EXISTS):
        train_run(_spec(roots, work))
    with pytest.raises(ValidationFailed, match="different config"):
        train_run(
            _spec(roots, work, resume=True, command=[sys.executable, "fake_train.py", "0", "x"])
        )
    second = train_run(_spec(roots, work, resume=True))
    assert second.attempt.n == 2 and len(second.record.attempts) == 2
    assert second.registered == 0  # same files, same shas: nothing new to register
    assert (
        second.record.attempts[0].status == "finished"
        and first.record.attempts[0] == second.record.attempts[0]
    )
    assert (run_dir(roots.data, "r1") / "train" / "console.2.log").is_file()


def test_each_attempt_records_its_own_command_and_seed(roots, work, tmp_path):
    """5-2: --resume adds an attempt but never rewrote the record's command / seed / venv, so a
    resumed run's train.yaml described the FIRST attempt while claiming to describe the run.
    Each attempt now carries its own; the record level keeps the first attempt's values on
    purpose -- it says how the run began, and `run.yaml`'s config_hash is bound to that."""
    _seed(roots)
    cfg = work / "cfg.yaml"  # --config pins config_hash, so the command may differ across attempts
    cfg.write_text("epochs: 1\n", encoding="utf-8")
    train_run(_spec(roots, work, config=cfg, seed=11))
    train_run(
        _spec(
            roots,
            work,
            config=cfg,
            seed=22,
            resume=True,
            command=[sys.executable, "fake_train.py", "0", "second"],
        )
    )
    rec = load_record(roots.data, "r1")
    assert [a.n for a in rec.attempts] == [1, 2]
    assert [a.seed for a in rec.attempts] == [11, 22]
    assert rec.attempts[0].command == [sys.executable, "fake_train.py", "0"]
    assert rec.attempts[1].command == [sys.executable, "fake_train.py", "0", "second"]
    assert [a.venv for a in rec.attempts] == [None, None]  # neither attempt was given one
    assert rec.seed == 11 and rec.command == rec.attempts[0].command
    assert rec.venv is None


def test_resume_closes_an_attempt_left_running_by_a_crash(roots, work, tmp_path):
    """5-3: an attempt still marked ``running`` is one vcp itself did not survive -- nothing
    ever came back to close it, so `train status` WARNed about it for the life of the run.
    A --resume is the moment it is certainly not running any more: it becomes ``interrupted``
    with no invented exit code, and the event log says who decided that and when."""
    _seed(roots)
    train_run(_spec(roots, work))
    rec = load_record(roots.data, "r1")
    crashed = rec.attempts[0].model_copy(
        update={"status": "running", "exit_code": None, "finished_at": None, "duration_s": None}
    )
    save_record(roots.data, rec.model_copy(update={"attempts": [crashed]}))
    assert status_view(roots.data, "r1").running == 1

    res = train_run(_spec(roots, work, resume=True))
    assert res.attempt.n == 2 and res.attempt.status == "finished"
    rec = load_record(roots.data, "r1")
    assert [a.status for a in rec.attempts] == ["interrupted", "finished"]
    assert rec.attempts[0].exit_code is None and rec.attempts[0].finished_at is not None
    assert status_view(roots.data, "r1").running == 0
    note = next(e for e in read_events(roots.data, "r1") if e["event"] == "note")
    assert note["attempt"] == 1
    assert note["value"] == "attempt 1 found running at resume; marked interrupted"


def test_cwd_must_be_a_directory_and_nothing_is_written(roots, work, tmp_path):
    """5-8: a --cwd that does not exist used to reach Popen as a bare OSError -- an ABORT, after
    run.yaml, train.yaml and a `running` attempt were already on disk. It is plainly bad input:
    a located FAIL, checked with everything else before the first write."""
    _seed(roots)
    with pytest.raises(ValidationFailed, match="not_found: --cwd") as ei:
        train_run(_spec(roots, work, cwd=tmp_path / "nowhere"))
    assert "is not a directory" in str(ei.value)
    assert not run_dir(roots.data, "r1").exists()
    a_file = tmp_path / "a.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="not_found: --cwd"):
        train_run(_spec(roots, work, cwd=a_file))
    assert not run_dir(roots.data, "r1").exists()


def test_train_run_venv_python_must_exist(roots, work, tmp_path):
    _seed(roots)
    with pytest.raises(Exception, match="no python"):
        train_run(_spec(roots, work, venv=tmp_path / "venv"))
    assert not run_dir(roots.data, "r1").exists()


def test_train_run_keeps_session_writes_made_during_the_command(roots, work, tmp_path):
    """C1: Session.register_checkpoint / note write train.yaml WHILE the child is still running.
    The wrapper must re-read the record after execute() returns instead of layering the
    finished attempt onto the stale in-memory snapshot it held before the child started --
    otherwise the session's checkpoint (and its final=True mark) are lost."""
    _seed(roots)
    (work / "session_train.py").write_text(SESSION_FAKE, encoding="utf-8")
    res = train_run(
        _spec(
            roots,
            work,
            command=[sys.executable, "session_train.py"],
            checkpoints=["weights/*.pt"],
            final=None,
        )
    )
    ckpt = next(c for c in res.record.checkpoints if c.path.endswith("epoch.pt"))
    assert ckpt.source == "session" and ckpt.final is True
    assert res.final is not None and res.final.path.endswith("epoch.pt")
    card = load_run(roots.data, "r1")
    assert card.source.weights_hash == sha256_file(work / "weights" / "epoch.pt")
    events = read_events(roots.data, "r1")
    note = next(e for e in events if e["event"] == "note")
    assert note["key"] == "val_auc"
    assert any(e["event"] == "checkpoint" and e.get("source") == "session" for e in events)


def test_train_run_resume_with_changed_weights(roots, work, tmp_path):
    """C2/I1/I3: a --resume whose command is identical but whose weights differ (a real
    trainer writing more epochs). Covers three findings at once: the resumed record must not
    raise name_collision (I1), the old weights_hash must be preserved in history.jsonl before
    being overwritten (I3), and `status` must key backed-ness by bytes, not path, so the new
    (not yet uploaded) best.pt is correctly reported unbacked even though the old bytes at the
    same path were verified (C2)."""
    _seed(roots)
    (work / "resume_train.py").write_text(RESUME_FAKE, encoding="utf-8")
    vault = tmp_path / "vault"
    kwargs = dict(command=[sys.executable, "resume_train.py"])
    train_run(_spec(roots, work, uploads=[str(vault)], **kwargs))
    second = train_run(_spec(roots, work, resume=True, **kwargs))

    # two records for best.pt with different shas; only the newest carries final=True
    best = [c for c in second.record.checkpoints if c.path.endswith("best.pt")]
    assert len(best) == 2
    assert best[0].sha256 != best[1].sha256
    assert [c.final for c in best] == [False, True]

    # the old weights_hash survives in history.jsonl before being overwritten
    history_path = run_dir(roots.data, "r1") / "history.jsonl"
    history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
    replaced = [h for h in history if h.get("field") == "weights_hash"]
    assert len(replaced) == 1
    assert replaced[0]["event"] == "replace"
    assert replaced[0]["old_sha256"] == best[0].sha256
    assert replaced[0]["via"] == "train.run"

    # the run card now points at the new bytes
    card = load_run(roots.data, "r1")
    assert card.source.weights_hash == best[1].sha256

    # status: the old best.pt bytes are still backed (verified in the first run's upload), the
    # new ones are not -- keyed by sha256, not by the shared path
    best_path = best[0].path
    assert best_path == best[1].path
    st = status_view(roots.data, "r1")
    assert st.unbacked == [best_path]
    assert st.backed == 2  # the old best.pt record and last.pt (unchanged) are both verified

    # uploading now: only the new best.pt is new content; last.pt is unchanged (skipped); no
    # name_collision despite two checkpoints named "best.pt"
    _, out = upload_run(roots.data, "r1", str(vault))
    assert out.uploaded == 1 and out.skipped == 1
    assert {r.name for r in out.records} == {"best.pt", "last.pt"}

    assert status_view(roots.data, "r1").unbacked == []


def test_train_run_binds_the_childs_receipts_and_warns_on_observed_beyond(roots, work):
    _seed(roots)
    res_mat = materialize(
        MaterializeSpec(name="tiny", mode="npy", data_root=roots.data, configs_root=roots.configs)
    )
    assert res_mat.failed == 0
    (work / "access_train.py").write_text(ACCESS_FAKE, encoding="utf-8")
    res = train_run(_spec(roots, work, command=[sys.executable, "access_train.py"]))
    assert res.attempt.status == "finished", res.record
    assert (res.receipts, res.denied, res.provenance) == (1, 0, "receipt")
    assert res.observed_beyond == [] and res.receipt_invalid == 0
    card = load_run(roots.data, "r1")
    assert [r.artifact_id for r in card.access] == ["r1-a1-1"]
    assert card.access[0].subsets == ["train"] and card.access == res.record.access
    assert any(e["event"] == "access" for e in read_events(roots.data, "r1"))
    (work / "peek_train.py").write_text(PEEK_FAKE, encoding="utf-8")
    res = train_run(_spec(roots, work, run_id="r2", command=[sys.executable, "peek_train.py"]))
    assert res.attempt.status == "finished", res.record
    assert res.receipts == 2 and res.observed_beyond == ["valA"] and res.provenance == "receipt"
    assert "observed_beyond_trained_on=valA" in res.warnings
    assert [r.artifact_id for r in load_run(roots.data, "r2").access] == ["r2-a1-1", "r2-a1-2"]
    # a resume adds attempt-2 receipts next to the attempt-1 ones
    res = train_run(
        _spec(roots, work, run_id="r2", command=[sys.executable, "peek_train.py"], resume=True)
    )
    ids = [r.artifact_id for r in load_run(roots.data, "r2").access]
    assert ids == ["r2-a1-1", "r2-a1-2", "r2-a2-1", "r2-a2-2"] and res.receipts == 4


def test_the_child_knows_its_attempt_and_its_receipts_agree(roots, work):
    _seed(roots)
    (work / "attempt_train.py").write_text(ATTEMPT_FAKE, encoding="utf-8")
    command = [sys.executable, "attempt_train.py"]
    for n in (1, 2, 3):
        if n == 3:  # vcp itself died during attempt 2: the resume closes it, then runs attempt 3
            rec = load_record(roots.data, "r1")
            crashed = rec.attempts[-1].model_copy(update={"status": "running", "exit_code": None})
            save_record(
                roots.data, rec.model_copy(update={"attempts": [*rec.attempts[:-1], crashed]})
            )
        res = train_run(_spec(roots, work, command=command, resume=n > 1))
        console = run_dir(roots.data, "r1") / "train" / f"console.{n}.log"
        text = console.read_text(encoding="utf-8")
        assert res.attempt.n == n and res.attempt.status == "finished", text
        assert f"VCP_ATTEMPT {n} SESSION {n}" in text
    ids = [r.artifact_id for r in load_run(roots.data, "r1").access]
    assert ids == ["r1-a1-1", "r1-a2-1", "r1-a3-1"]


def test_train_run_without_receipts_grades_export_or_declared(roots, work, tmp_path):
    _seed(roots)
    res = train_run(_spec(roots, work))
    assert res.receipts == 0 and res.provenance == "declared"
    export = _export(roots, "train", tmp_path / "yolo-train")
    res = train_run(_spec(roots, work, run_id="r3", exports=[export], trained_on=[]))
    assert res.provenance == "export"


def test_train_run_resume_keeps_a_manually_attached_receipt(roots, work):
    """F5 (final review Important #5): --resume used to replace run.yaml's access list wholesale
    with train.yaml's, dropping a receipt attached by `ingest --receipt` between attempts. It
    must now merge by artifact_id: train.yaml's own refs first, then any ref already on the card
    that train.yaml does not know about."""
    _seed(roots)
    assert (
        materialize(
            MaterializeSpec(
                name="tiny", mode="npy", data_root=roots.data, configs_root=roots.configs
            )
        ).failed
        == 0
    )
    (work / "access_train.py").write_text(ACCESS_FAKE, encoding="utf-8")
    train_run(_spec(roots, work, command=[sys.executable, "access_train.py"]))
    with DatasetAccess.open(
        "tiny",
        "fixed-v1",
        subsets={"valA"},
        purpose="custom",
        data_root=roots.data,
        configs_root=roots.configs,
    ) as access:
        list(access.iter("valA"))
    manual_id = access.receipt_id
    card = attach_receipts(load_run(roots.data, "r1"), [manual_id], data_root=roots.data)
    save_run(roots.data, card)
    res = train_run(_spec(roots, work, command=[sys.executable, "access_train.py"], resume=True))
    ids = [r.artifact_id for r in load_run(roots.data, "r1").access]
    assert ids == ["r1-a1-1", "r1-a2-1", manual_id]
    assert res.receipts == 3


def test_train_run_warns_when_the_dataset_has_no_source_audit(roots, work):
    ds, plan, paths = _seed(roots)
    assert (
        materialize(
            MaterializeSpec(
                name="tiny", mode="npy", data_root=roots.data, configs_root=roots.configs
            )
        ).failed
        == 0
    )
    (work / "access_train.py").write_text(ACCESS_FAKE, encoding="utf-8")
    res = train_run(_spec(roots, work, command=[sys.executable, "access_train.py"]))
    assert res.source_audit_missing == 1 and "source_audit=missing" in res.warnings
    assert load_run(roots.data, "r1").access[0].identity == "full_hash"
    write_source_audit(paths, ds.card, data_root=roots.data)
    res = train_run(_spec(roots, work, run_id="r2", command=[sys.executable, "access_train.py"]))
    assert res.source_audit_missing == 0 and "source_audit=missing" not in res.warnings
    ref = load_run(roots.data, "r2").access[0]
    assert ref.identity == "source_audit" and ref.source_audit is not None


# VCP-040: a loop that attaches what it read, beside what `train run --evidence` attached.
EVIDENCE_FAKE = """
from pathlib import Path
from vcp.train import Session

Path("teacher.jsonl").write_text("t")
Session.current().attach_evidence("teacher", "teacher.jsonl")
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best")
"""

# ... and one that rewrites the file `--evidence corpus=` attached before it started.
CHANGE_FAKE = """
from pathlib import Path
Path("corpus.json").write_text("changed")
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best")
"""


def test_evidence_and_labels_are_checked_before_the_first_write(roots, work, tmp_path):
    _, plan, _ = _seed(roots)
    make_label_set(roots, tmp_path, plan, subsets=("train", "valA"), label_set_id="wide")
    make_label_set(roots, tmp_path, plan)  # "pseudo-v1" over "train": fits trained_on=["train"]
    evidence_file = tmp_path / "some_evidence.txt"
    evidence_file.write_text("x", encoding="utf-8")
    # A file named like one the artifact layer writes itself, in any case (NTFS folds it): each
    # in its own directory, since the two names are one file on a case-insensitive disk.
    reserved = []
    for folder, file_name in (("lower", "manifest.json"), ("upper", "MANIFEST.JSON")):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / file_name).write_text('{"derived": true}', encoding="utf-8")
        reserved.append(tmp_path / folder / file_name)
    evidence_dir = roots.data / "artifacts" / "evidence"
    for kw, reason in (
        ({"evidence": [f"teacher={tmp_path / 'gone.jsonl'}"]}, "not_found"),
        ({"evidence": [f"teacher={tmp_path}"]}, "not_a_file"),
        ({"evidence": ["teacher"]}, "NAME=PATH"),
        ({"labels": ["nope"]}, "not_found"),
        ({"labels": ["wide"]}, "labels_mismatch"),  # valA is not in trained_on
        ({"evidence": [f"labels={evidence_file}"]}, "role_reserved"),
        (
            {"evidence": [f"pseudo-v1={evidence_file}"], "labels": ["pseudo-v1"]},
            "evidence_conflict",
        ),
        ({"evidence": [f"derived={reserved[0]}"]}, "reserved_name"),
        ({"evidence": [f"derived={reserved[1]}"]}, "reserved_name"),
    ):
        with pytest.raises(ValidationFailed, match=reason):
            train_run(_spec(roots, work, **kw))
        assert not (run_dir(roots.data, "r1") / "run.yaml").exists()
        assert not evidence_dir.exists() or not any(evidence_dir.iterdir())


def test_evidence_conflict_on_resume_leaves_no_trace_of_the_second_attempt(roots, work, tmp_path):
    """The `evidence_conflict:` check must run before ANY write of the new attempt: a --resume
    that trips it (the name is already bound under another role/kind from attempt 1) must not
    leave a `started` attempt, a console/env file, or a single changed byte of train.yaml /
    train.log.jsonl."""
    _, plan, _ = _seed(roots)
    make_label_set(roots, tmp_path, plan)  # "pseudo-v1" over "train"
    evidence_file = tmp_path / "corpus.txt"
    evidence_file.write_text("x", encoding="utf-8")
    first = train_run(_spec(roots, work, evidence=[f"pseudo-v1={evidence_file}"]))
    assert first.attempt.status == "finished", first.record
    run = run_dir(roots.data, "r1")
    before_yaml = (run / "train.yaml").read_bytes()
    before_log = (run / "train.log.jsonl").read_bytes()
    with pytest.raises(ValidationFailed, match="evidence_conflict"):
        train_run(_spec(roots, work, resume=True, labels=["pseudo-v1"]))
    assert (run / "train.yaml").read_bytes() == before_yaml
    assert (run / "train.log.jsonl").read_bytes() == before_log
    assert len(load_record(roots.data, "r1").attempts) == 1
    assert not (run / "train" / "console.2.log").exists()
    assert not (run / "train" / "env.2.json").exists()


def test_train_run_binds_evidence_and_labels_to_the_run(roots, work, tmp_path):
    _, plan, _ = _seed(roots)
    make_label_set(roots, tmp_path, plan)
    corpus = tmp_path / "corpus-receipt.json"
    corpus.write_text('{"source": "external"}', encoding="utf-8")
    (work / "evidence_train.py").write_text(EVIDENCE_FAKE, encoding="utf-8")
    kw = dict(
        command=[sys.executable, "evidence_train.py"],
        evidence=[f"corpus={corpus}"],
        labels=["pseudo-v1"],
    )
    res = train_run(_spec(roots, work, **kw))
    assert res.attempt.status == "finished", res.record
    assert (res.evidence, res.labels, res.evidence_changed) == (3, "pseudo-v1", [])
    card = load_run(roots.data, "r1")
    assert [(r.name, r.kind, r.binding) for r in card.evidence] == [
        ("corpus", "evidence", "cli"),
        ("pseudo-v1", "label_set", "cli"),
        ("teacher", "evidence", "session"),
    ]
    assert card.evidence == load_record(roots.data, "r1").evidence
    again = train_run(_spec(roots, work, resume=True, **kw))  # same bytes: same artifacts
    assert again.evidence == 3 and len(load_run(roots.data, "r1").evidence) == 3
    assert len(list((roots.data / "artifacts" / "evidence").iterdir())) == 2


def test_an_evidence_file_that_changes_during_the_run_is_a_warning(roots, work):
    _seed(roots)
    corpus = work / "corpus.json"
    corpus.write_text("original", encoding="utf-8")
    (work / "change_train.py").write_text(CHANGE_FAKE, encoding="utf-8")
    res = train_run(
        _spec(
            roots,
            work,
            command=[sys.executable, "change_train.py"],
            evidence=[f"corpus={corpus}"],
        )
    )
    assert res.evidence_changed == ["corpus"] and "evidence_changed=corpus" in res.warnings
    art = load_run(roots.data, "r1").evidence[0].artifact_id
    kept = roots.data / "artifacts" / "evidence" / art / "corpus.json"
    assert kept.read_text(encoding="utf-8") == "original"  # the attached copy never moves
    notes = [e for e in read_events(roots.data, "r1") if e["event"] == "note"]
    assert any(e.get("key") == "evidence_changed" and e["value"] == "corpus" for e in notes)


# VCP-041: a loop that edits a tracked file while it trains.
EDIT_FAKE = """
from pathlib import Path
model = Path("model.py")
model.write_bytes(model.read_bytes() + b"# edited while training\\n")
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best")
"""


def test_git_state_of_the_training_tree(roots, tmp_path, isolated_git):
    _seed(roots)
    repo = git_repo(tmp_path / "repo", {"fake_train.py": FAKE.encode()})
    clean = train_run(_spec(roots, repo))
    assert clean.git is not None and len(clean.git.commit) == 40 and clean.git.modified == 0
    assert clean.git_changed == []  # weights/ is a new untracked output, not a change
    assert not any(w.startswith("modified=") for w in clean.warnings)
    (repo / "fake_train.py").write_bytes(FAKE.encode() + b"# local tweak\n")
    dirty = train_run(_spec(roots, repo, run_id="r2"))
    assert dirty.git.modified == 1 and dirty.git.modified_paths == ["fake_train.py"]
    assert dirty.git.patch == "train/git.1.patch"
    run = run_dir(roots.data, "r2")
    assert sha256_file(run / "train" / "git.1.patch") == dirty.git.diff_sha256
    assert "modified=1 tracked path(s); patch train/git.1.patch" in dirty.warnings
    env = json.loads((run / "train" / "env.1.json").read_text(encoding="utf-8"))
    assert env["git"]["patch"] == "train/git.1.patch"
    assert env["git"]["untracked_paths"] == ["weights/"]


def test_require_clean_fails_before_the_first_write(roots, tmp_path, work, isolated_git):
    _seed(roots)
    repo = git_repo(tmp_path / "repo", {"fake_train.py": FAKE.encode()})
    (repo / "fake_train.py").write_bytes(FAKE.encode() + b"# local tweak\n")
    with pytest.raises(ValidationFailed, match="dirty_tree: 1 tracked path"):
        train_run(_spec(roots, repo, require_clean=True))
    assert not run_dir(roots.data, "r1").exists()
    with pytest.raises(ValidationFailed, match="not_found: git repository"):
        train_run(_spec(roots, work, require_clean=True))
    assert not run_dir(roots.data, "r1").exists()
    git(repo, "commit", "-q", "--no-verify", "-am", "keep the tweak")
    (repo / "notes.txt").write_bytes(b"untracked is fine")
    ok = train_run(_spec(roots, repo, require_clean=True))
    assert ok.attempt.status == "finished" and ok.git.modified == 0 and ok.git.untracked == 1


def test_a_tracked_edit_during_training_is_a_warning(roots, tmp_path, isolated_git):
    _seed(roots)
    repo = git_repo(
        tmp_path / "repo", {"edit_train.py": EDIT_FAKE.encode(), "model.py": b"W = 1\n"}
    )
    res = train_run(_spec(roots, repo, command=[sys.executable, "edit_train.py"]))
    assert res.attempt.status == "finished"
    assert res.git.modified == 0 and res.git_changed == ["diff"]
    assert "git_changed=diff" in res.warnings
    notes = [e for e in read_events(roots.data, "r1") if e.get("key") == "git_changed"]
    assert len(notes) == 1 and notes[0]["value"] == "diff"
    assert notes[0]["commit"] == res.git.commit and len(notes[0]["diff_sha256"]) == 64


def test_each_attempt_keeps_its_own_git_record(roots, tmp_path, isolated_git):
    _seed(roots)
    repo = git_repo(tmp_path / "repo", {"fake_train.py": FAKE.encode()})
    (repo / "fake_train.py").write_bytes(FAKE.encode() + b"# tweak 1\n")
    first = train_run(_spec(roots, repo))
    (repo / "fake_train.py").write_bytes(FAKE.encode() + b"# tweak 2\n")
    second = train_run(_spec(roots, repo, resume=True))
    train = run_dir(roots.data, "r1") / "train"
    assert (train / "git.1.patch").is_file() and (train / "git.2.patch").is_file()
    assert first.git.diff_sha256 != second.git.diff_sha256
    assert second.git.patch == "train/git.2.patch"


def _files(roots):
    """The bytes of what vcp writes for a run before and around its first attempt."""
    run = run_dir(roots.data, "r1")
    return {
        name: (run / name).read_bytes() for name in ("run.yaml", "train.yaml", "train.log.jsonl")
    }


def _low_disk(monkeypatch, free=2 * GIB):
    monkeypatch.setattr(disk, "usage", lambda path: (free, 100 * GIB))


def _two_devices(monkeypatch, work):
    """``work/ckpt`` is its own volume (5 GiB free); everything else shares another (50 GiB)."""
    ckpt = work / "ckpt"
    monkeypatch.setattr(disk, "device", lambda p: 2 if Path(p) == ckpt else 1)
    monkeypatch.setattr(
        disk,
        "usage",
        lambda p: (5 * GIB, 100 * GIB) if Path(p) == ckpt else (50 * GIB, 100 * GIB),
    )


def test_train_run_aborts_before_any_write_when_a_run_disk_is_low(roots, work, monkeypatch):
    _seed(roots)
    _low_disk(monkeypatch)
    with pytest.raises(VcpError, match="^low_disk:") as ei:
        train_run(_spec(roots, work))
    assert ei.value.status == "ABORT"
    assert set(ei.value.fields) == {"free_gib", "min_free_gib", "disk_path"}
    assert ei.value.fields["free_gib"] == 2.0 and ei.value.fields["min_free_gib"] == 10.0
    assert not run_dir(roots.data, "r1").exists()


def test_resume_on_a_low_disk_aborts_and_leaves_the_record_as_it_was(roots, work, monkeypatch):
    _seed(roots)
    train_run(_spec(roots, work))
    before = _files(roots)
    _low_disk(monkeypatch)
    with pytest.raises(VcpError, match="^low_disk:"):
        train_run(_spec(roots, work, resume=True))
    assert _files(roots) == before
    assert not (run_dir(roots.data, "r1") / "train" / "console.2.log").exists()


def test_a_low_disk_resume_does_not_close_the_attempt_left_running(roots, work, monkeypatch):
    """The measurement comes before ``_close_running``, which would append to train.log.jsonl and
    rewrite train.yaml: an ABORT writes nothing, not even the reconciliation of the old attempt."""
    _seed(roots)
    train_run(_spec(roots, work))
    rec = load_record(roots.data, "r1")
    crashed = rec.attempts[0].model_copy(
        update={"status": "running", "exit_code": None, "finished_at": None, "duration_s": None}
    )
    save_record(roots.data, rec.model_copy(update={"attempts": [crashed]}))
    before = _files(roots)
    _low_disk(monkeypatch)
    with pytest.raises(VcpError, match="^low_disk:"):
        train_run(_spec(roots, work, resume=True))
    assert _files(roots) == before
    assert load_record(roots.data, "r1").attempts[0].status == "running"


def test_input_errors_come_before_low_disk(roots, work, tmp_path, monkeypatch):
    _seed(roots)
    train_run(_spec(roots, work))
    _low_disk(monkeypatch)
    with pytest.raises(ValidationFailed, match=RUN_EXISTS):
        train_run(_spec(roots, work))  # same id, no --resume
    with pytest.raises(ValidationFailed, match="not_found: --cwd"):
        train_run(_spec(roots, work, run_id="r2", cwd=tmp_path / "nowhere"))
    assert not run_dir(roots.data, "r2").exists()


def test_an_evidence_conflict_comes_before_low_disk(roots, work, tmp_path, monkeypatch):
    """The measurement sits after ``check_names`` too: a --resume that trips an input check is
    told so, however little room the disk has."""
    _, plan, _ = _seed(roots)
    make_label_set(roots, tmp_path, plan)
    evidence_file = tmp_path / "corpus.txt"
    evidence_file.write_text("x", encoding="utf-8")
    train_run(_spec(roots, work, evidence=[f"pseudo-v1={evidence_file}"]))
    _low_disk(monkeypatch)
    with pytest.raises(ValidationFailed, match="evidence_conflict"):
        train_run(_spec(roots, work, resume=True, labels=["pseudo-v1"]))


@pytest.mark.parametrize("bad", [-1.0, float("nan")])
def test_train_run_refuses_a_bad_min_free_gib(roots, work, bad):
    _seed(roots)
    with pytest.raises(ValidationFailed, match="^invalid:"):
        train_run(_spec(roots, work, min_free_gib=bad))
    assert not run_dir(roots.data, "r1").exists()


def test_min_free_gib_zero_never_aborts_and_still_records(roots, work, monkeypatch):
    _seed(roots)
    _low_disk(monkeypatch)
    res = train_run(_spec(roots, work, min_free_gib=0))
    assert res.attempt.status == "finished"
    env = json.loads(
        (run_dir(roots.data, "r1") / "train" / "env.1.json").read_text(encoding="utf-8")
    )
    assert env["disk"]["min_free_gib"] == 0.0
    assert env["disk"]["volumes"][0]["free_bytes"] == 2 * GIB


def test_a_lowered_bar_lets_a_small_disk_through(roots, work, monkeypatch):
    _seed(roots)
    _low_disk(monkeypatch)
    res = train_run(_spec(roots, work, min_free_gib=1.5))
    assert res.attempt.status == "finished" and free_gib(res.disk) == 2.0


def test_the_env_snapshot_records_the_disk_check(roots, work):
    _seed(roots)
    res = train_run(_spec(roots, work, checkpoints=[], final=None))
    env = json.loads(
        (run_dir(roots.data, "r1") / "train" / "env.1.json").read_text(encoding="utf-8")
    )
    assert env["disk"]["min_free_gib"] == 10.0 and env["disk"]["checked_at"].endswith("Z")
    vols = env["disk"]["volumes"]
    assert len(vols) == 1 and vols[0]["roles"] == ["run", "cwd"]
    assert vols[0]["free_bytes"] == ROOMY_BYTES == vols[0]["total_bytes"]
    assert res.disk.model_dump(mode="json") == env["disk"]


def test_checkpoint_globs_on_the_same_volume_add_a_role_not_an_entry(roots, work):
    _seed(roots)
    res = train_run(_spec(roots, work))  # weights/*.pt and weights/best.pt: same volume as cwd
    assert [v.roles for v in res.disk.volumes] == [["run", "cwd", "checkpoints"]]


def test_a_glob_prefix_on_another_volume_is_its_own_entry_and_the_lowest_counts(
    roots, work, monkeypatch
):
    _seed(roots)
    (work / "ckpt").mkdir()
    _two_devices(monkeypatch, work)
    res = train_run(_spec(roots, work, checkpoints=["ckpt/*.pt"], final=None, min_free_gib=1.0))
    assert len(res.disk.volumes) == 2 and free_gib(res.disk) == 5.0
    assert [v.roles for v in res.disk.volumes] == [["run", "cwd"], ["checkpoints"]]


def test_a_low_checkpoint_volume_aborts_even_when_the_run_volume_has_room(roots, work, monkeypatch):
    _seed(roots)
    (work / "ckpt").mkdir()
    _two_devices(monkeypatch, work)
    with pytest.raises(VcpError, match="^low_disk:") as ei:
        train_run(_spec(roots, work, checkpoints=["ckpt/*.pt"], final=None))
    assert ei.value.fields["free_gib"] == 5.0
    assert ei.value.fields["disk_path"] == (work / "ckpt").resolve().as_posix()
    assert not run_dir(roots.data, "r1").exists()


def test_an_unreadable_disk_warns_and_does_not_abort(roots, work, monkeypatch):
    _seed(roots)

    def broken(path):
        raise OSError("probe failed")

    monkeypatch.setattr(disk, "usage", broken)
    res = train_run(_spec(roots, work))
    assert res.attempt.status == "finished" and "disk_check=unavailable" in res.warnings
    assert free_gib(res.disk) == "unknown"
    env = json.loads(
        (run_dir(roots.data, "r1") / "train" / "env.1.json").read_text(encoding="utf-8")
    )
    assert env["disk"]["volumes"][0]["free_bytes"] is None


def test_a_readable_disk_has_no_disk_warning(roots, work):
    _seed(roots)
    res = train_run(_spec(roots, work))
    assert "disk_check=unavailable" not in res.warnings


NO_SPACE = (
    'import sys; print("OSError: [Errno 28] No space left on device", flush=True); sys.exit(1)'
)
WIN_FULL = 'import sys; print("OSError: [WinError 112] The disk is full", flush=True); sys.exit(1)'
BADBIT = 'import sys; print("terminate called: ios_base::badbit", flush=True); sys.exit(1)'
UNRELATED = 'import sys; print("KeyError: fold", flush=True); sys.exit(1)'
EARLY = 'import sys; print("No space left on device"); print("x" * 70000); sys.exit(1)'
SAYS_BUT_OK = 'print("No space left on device")'


def _space_after_child(monkeypatch, gib):
    """The disk reads ROOMY_BYTES until the child has printed its first line and ``gib`` GiB
    after it -- the room vcp finds when it measures again. Returns the ``on_line`` that flips it."""
    state = {"printed": False}
    monkeypatch.setattr(
        disk,
        "usage",
        lambda path: (gib * GIB if state["printed"] else ROOMY_BYTES, ROOMY_BYTES),
    )

    def on_line(line):
        state["printed"] = True

    return on_line


def _disk_notes(roots):
    return [
        e
        for e in read_events(roots.data, "r1")
        if e["event"] == "note" and e.get("key") in ("disk_full", "low_disk")
    ]


def _disk_spec(roots, work, script, **kw):
    return _spec(
        roots, work, command=[sys.executable, "-c", script], checkpoints=[], final=None, **kw
    )


@pytest.mark.parametrize(
    ("script", "marker"), [(NO_SPACE, "No space left on device"), (WIN_FULL, "[WinError 112]")]
)
def test_a_child_that_ran_out_of_disk_fails_with_disk_full(roots, work, script, marker):
    _seed(roots)
    res = train_run(_disk_spec(roots, work, script))
    assert res.attempt.status == "failed" and res.disk_failure.key == "disk_full"
    assert res.disk_failure.evidence == "console" and res.disk_failure.marker == marker
    assert res.disk_failure.reason.startswith(
        "disk_full: the command exited 1 and its console says"
    )
    [note] = _disk_notes(roots)
    assert (note["key"], note["marker"], note["free_bytes"]) == ("disk_full", marker, ROOMY_BYTES)
    assert note["attempt"] == 1 and note["ts"].endswith("Z")


def test_a_badbit_child_on_a_low_disk_fails_with_low_disk(roots, work, monkeypatch):
    _seed(roots)
    on_line = _space_after_child(monkeypatch, 2)
    res = train_run(_disk_spec(roots, work, BADBIT, on_line=on_line))
    assert res.attempt.status == "failed" and res.disk_failure.key == "low_disk"
    assert "may have filled" in res.disk_failure.reason
    assert res.disk_failure.evidence is None and res.disk_failure.marker is None
    assert [(n["key"], n["marker"], n["free_bytes"]) for n in _disk_notes(roots)] == [
        ("low_disk", None, 2 * GIB)
    ]


@pytest.mark.parametrize(
    ("gib", "min_free", "script"),
    [(50, 10.0, BADBIT), (2, 0.0, BADBIT), (1024, 10.0, UNRELATED), (1024, 10.0, EARLY)],
)
def test_no_disk_reason_when_the_evidence_is_not_there(
    roots, work, monkeypatch, gib, min_free, script
):
    """Room above the bar, a bar of 0, an unrelated error, or the word further back than the last
    64 KiB of the console: the failure is a plain failure, as in 0.15."""
    _seed(roots)
    on_line = _space_after_child(monkeypatch, gib)
    res = train_run(_disk_spec(roots, work, script, min_free_gib=min_free, on_line=on_line))
    assert res.attempt.status == "failed" and res.disk_failure is None
    assert _disk_notes(roots) == []


def test_the_console_wins_over_low_disk(roots, work, monkeypatch):
    _seed(roots)
    on_line = _space_after_child(monkeypatch, 2)
    res = train_run(_disk_spec(roots, work, NO_SPACE, on_line=on_line))
    assert res.disk_failure.key == "disk_full" and res.disk_failure.free_bytes == 2 * GIB
    [note] = _disk_notes(roots)
    assert note["key"] == "disk_full" and note["free_bytes"] == 2 * GIB


def test_a_resumed_attempt_that_runs_out_of_disk_is_noted_under_its_own_number(roots, work):
    _seed(roots)
    train_run(_disk_spec(roots, work, NO_SPACE))
    res = train_run(_disk_spec(roots, work, NO_SPACE, resume=True))  # same command: same config
    assert res.attempt.n == 2 and res.disk_failure.key == "disk_full"
    assert [(n["attempt"], n["key"]) for n in _disk_notes(roots)] == [
        (1, "disk_full"),
        (2, "disk_full"),
    ]


def test_success_and_interruption_are_never_diagnosed(roots, work):
    _seed(roots)
    ok = train_run(_disk_spec(roots, work, SAYS_BUT_OK))
    assert ok.attempt.status == "finished" and ok.disk_failure is None
    assert _disk_notes(roots) == []

    def interrupt(line):
        raise KeyboardInterrupt

    res = train_run(_disk_spec(roots, work, NO_SPACE, run_id="r2", on_line=interrupt))
    assert res.attempt.status == "interrupted" and res.disk_failure is None
    assert [e for e in read_events(roots.data, "r2") if e.get("key") == "disk_full"] == []


def test_vcp_that_cannot_write_its_console_stops_the_child_and_aborts(roots, work):
    _seed(roots)
    (work / "sleeper.py").write_text(SLEEPER, encoding="utf-8")

    def full(line):
        raise OSError(errno.ENOSPC, "No space left on device")

    started = time.monotonic()
    with pytest.raises(VcpError, match="^disk_full:") as ei:
        train_run(_spec(roots, work, command=[sys.executable, "sleeper.py"], on_line=full))
    assert ei.value.status == "ABORT"
    assert ei.value.fields == {"disk_evidence": "vcp"} and time.monotonic() - started < 25
    # the child was stopped (not left sleeping its 30 s), and the attempt is still `running`
    assert load_record(roots.data, "r1").attempts[-1].status == "running"


def test_other_os_errors_pass_through_unchanged(roots, work):
    _seed(roots)
    (work / "sleeper.py").write_text(SLEEPER, encoding="utf-8")

    def denied(line):
        raise PermissionError(errno.EACCES, "denied")

    with pytest.raises(PermissionError, match="denied"):
        train_run(_spec(roots, work, command=[sys.executable, "sleeper.py"], on_line=denied))


def test_a_train_yaml_that_cannot_be_replaced_aborts_with_disk_full_and_stays_whole(
    roots, work, monkeypatch
):
    _seed(roots)
    train_run(_spec(roots, work))
    path = run_dir(roots.data, "r1") / "train.yaml"
    before = path.read_bytes()
    real_replace = os.replace

    def full_disk(src, dst):
        if Path(dst).name == "train.yaml":
            raise OSError(errno.ENOSPC, "No space left on device")
        real_replace(src, dst)

    monkeypatch.setattr(atomic.os, "replace", full_disk)
    with pytest.raises(VcpError, match="^disk_full:") as ei:
        train_run(_spec(roots, work, resume=True))
    assert ei.value.fields == {"disk_evidence": "vcp"} and path.read_bytes() == before
    assert not [p for p in path.parent.iterdir() if atomic.is_tmp_name(p.name)]


def test_a_destination_that_is_full_stays_a_plain_os_error(roots, work, tmp_path, monkeypatch):
    """The upload destination is not vcp's record: the ``upload(...)`` call in ``_upload_all`` is
    outside ``own_writes``, so a full destination keeps its 0.15 behaviour (an OSError) instead of
    becoming ``disk_full:``."""
    _seed(roots)

    def full_destination(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(run_module, "upload", full_destination)
    with pytest.raises(OSError, match="No space left") as ei:
        train_run(_spec(roots, work, uploads=[str(tmp_path / "vault")]))
    assert not isinstance(ei.value, VcpError)


def _arm_after_upload(monkeypatch):
    """The real ``upload`` runs against the local vault; once it has returned, ``armed`` is set,
    so a patched write can tell upload bookkeeping from every write before it."""
    armed = {"on": False}
    real_upload = run_module.upload

    def upload_then_arm(*args, **kwargs):
        outcome = real_upload(*args, **kwargs)
        armed["on"] = True
        return outcome

    monkeypatch.setattr(run_module, "upload", upload_then_arm)
    return armed


def _assert_recovery_is_train_upload(error):
    """The attempt had already finished: ``--resume`` would rerun the training command, so the
    message names the upload command instead."""
    assert "vcp train upload --run r1 --dest" in str(error) and "--resume" not in str(error)


def test_a_train_yaml_that_cannot_be_saved_after_an_upload_aborts_with_disk_full(
    roots, work, tmp_path, monkeypatch
):
    """The bookkeeping after ``upload(...)`` is vcp's own record: ENOSPC there is the same ABORT
    ``disk_full:`` / ``disk_evidence=vcp`` as anywhere else in ``train run``."""
    _seed(roots)
    armed = _arm_after_upload(monkeypatch)
    real_save = run_module.save_record

    def full_after_upload(data_root, record):
        if armed["on"]:
            raise OSError(errno.ENOSPC, "No space left on device")
        real_save(data_root, record)

    monkeypatch.setattr(run_module, "save_record", full_after_upload)
    with pytest.raises(VcpError, match="^disk_full:") as ei:
        train_run(_spec(roots, work, uploads=[str(tmp_path / "vault")]))
    assert ei.value.status == "ABORT" and ei.value.fields == {"disk_evidence": "vcp"}
    _assert_recovery_is_train_upload(ei.value)


def test_an_uploaded_event_that_cannot_be_appended_aborts_with_disk_full(
    roots, work, tmp_path, monkeypatch
):
    _seed(roots)
    real_append = run_module.append_event

    def full_for_uploaded(data_root, run_id, event, attempt, /, **payload):
        if event == "uploaded":
            raise OSError(errno.ENOSPC, "No space left on device")
        real_append(data_root, run_id, event, attempt, **payload)

    monkeypatch.setattr(run_module, "append_event", full_for_uploaded)
    with pytest.raises(VcpError, match="^disk_full:") as ei:
        train_run(_spec(roots, work, uploads=[str(tmp_path / "vault")]))
    assert ei.value.status == "ABORT" and ei.value.fields == {"disk_evidence": "vcp"}
    _assert_recovery_is_train_upload(ei.value)


def test_upload_bookkeeping_errors_other_than_a_full_disk_pass_through(
    roots, work, tmp_path, monkeypatch
):
    _seed(roots)
    armed = _arm_after_upload(monkeypatch)
    real_save = run_module.save_record

    def denied_after_upload(data_root, record):
        if armed["on"]:
            raise PermissionError(errno.EACCES, "denied")
        real_save(data_root, record)

    monkeypatch.setattr(run_module, "save_record", denied_after_upload)
    with pytest.raises(PermissionError, match="denied"):
        train_run(_spec(roots, work, uploads=[str(tmp_path / "vault")]))
