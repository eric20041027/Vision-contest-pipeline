"""Every write command is one transaction on the ledger submit.yaml names (spec 2026-09-28
§3.1, §4.2): stage / final / lock / unlock take the ledger's lock, read the ledger only then and
write where ``ledger:`` says; status and report read the same ledger without the lock."""

import subprocess
import sys
import threading
import time

from typer.testing import CliRunner

from submit_fixtures import EVAL, STAMP, TEST, seed_eval_runs, seed_judgements, seed_test_runs
from vcp.cli import app
from vcp.core import lock as lockmod
from vcp.core.config import dump_yaml_model
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.submit.final import final, lock, unlock
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import ledger_lock_file, shared_ledger
from vcp.submit.profile import init_profile
from vcp.submit.report import report, status
from vcp.submit.schema import LedgerRow, PlatformProfile
from vcp.submit.stage import StageSpec, stage

runner = CliRunner()

# Holds the ledger lock named by argv[1]. On a line "write" from stdin it appends a lock row to
# the ledger argv[2], still holding the lock, then exits. It prints its own pid: on Windows the
# venv's python.exe is a launcher, so Popen.pid is not the interpreter's pid.
HOLDER = """
import os, sys
from pathlib import Path
from vcp.core.lock import file_lock
from vcp.core.time import stamp
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.schema import LedgerRow
with file_lock(Path(sys.argv[1]), command="test.holder", label=sys.argv[2]):
    print(f"held {os.getpid()}", flush=True)
    if sys.stdin.readline().strip() == "write":
        SubmissionLedger(Path(sys.argv[2])).append(
            LedgerRow(event="lock", ts=stamp(), reason="theirs")
        )
"""


def _hold(lock_file, ledger):
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_file), str(ledger)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    words = proc.stdout.readline().split()
    assert words[:1] == ["held"], words
    return proc, int(words[1])


def _release(proc, answer=""):
    proc.stdin.write(answer + "\n")
    proc.stdin.flush()
    proc.wait(timeout=30)


def _profile(**over) -> PlatformProfile:
    base = dict(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


def _kw(pair):
    return {"data_root": pair.roots.data, "configs_root": pair.roots.configs}


def _paths(roots):
    return DatasetPaths.resolve(TEST, data_root=roots.data, configs_root=roots.configs)


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _uploaded(sid):
    now = stamp()
    return LedgerRow(
        event="uploaded",
        ts=now,
        submission_id=sid,
        at=now,
        source="manual",
        confirmed=True,
        profile_sha256="p" * 64,
    )


def test_shared_mode_writes_and_reads_the_data_roots_ledger(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(_profile(ledger="shared"), **_kw(pair))
    seed_test_runs(pair)
    for sid, eval_run, test_run, kind in (
        ("S1", "good", "good.test", "candidate"),
        ("S2", "bad", "bad.test", "baseline"),
    ):
        stage(
            StageSpec(
                dataset=TEST,
                submission_id=sid,
                eval_run=eval_run,
                test_run=test_run,
                kind=kind,
                reason=None if kind == "candidate" else "anchor",
                **_kw(pair),
            )
        )
    paths = pair.test_paths
    ledger = shared_ledger(paths)
    SubmissionLedger(ledger).append(_uploaded("S1"))  # the row `record` would write
    lock(TEST, "freeze", **_kw(pair))
    unlock(TEST, "go on", **_kw(pair))
    for run in ("good", "bad"):
        measure_run(
            MeasureSpec(
                run_id=run,
                metrics=["accuracy"],
                subsets=["holdout"],
                unseal=True,
                reason="final pick",
                **_kw(pair),
            )
        )
    assert final(TEST, **_kw(pair)).chosen == ["S1"]
    assert not paths.submissions_log.exists()
    events = [r.event for r in SubmissionLedger(ledger).rows]
    assert events == ["staged", "staged", "uploaded", "lock", "unlock", "final", "lock"]
    view = status(TEST, **_kw(pair))
    assert (view.staged, view.uploaded, view.locked is not None) == (2, 1, True)
    assert [r.submission_id for r in report(TEST, **_kw(pair))] == ["S1"]


def test_a_command_waits_for_the_lock_and_then_sees_what_the_holder_wrote(roots, monkeypatch):
    """spec 2026-09-28 §4.2, §8: the command finds the lock taken, waits, and reads the ledger
    only once it has the lock -- so it sees the lock row the holder appended while it waited."""
    paths = _paths(roots)
    dump_yaml_model(_profile(), paths.submit_yaml)
    monkeypatch.setattr(lockmod, "RETRY_SECONDS", 0.05)
    waiting = threading.Event()
    real_sleep = time.sleep

    def sleep(seconds):
        waiting.set()  # the command met the lock: it waits, its ledger not read yet
        real_sleep(seconds)

    monkeypatch.setattr(lockmod.time, "sleep", sleep)
    proc, _ = _hold(ledger_lock_file(paths, paths.submissions_log), paths.submissions_log)
    outcome: dict[str, Exception] = {}

    def run():
        try:
            lock(TEST, "mine", data_root=roots.data, configs_root=roots.configs)
        except Exception as e:  # handed to the test thread
            outcome["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    try:
        thread.start()
        assert waiting.wait(timeout=30)
        _release(proc, "write")
        thread.join(timeout=60)
    finally:
        proc.kill()
        proc.wait(timeout=30)
    error = outcome.get("error")
    assert error is not None and "locked: since" in str(error) and "theirs" in str(error)
    rows = SubmissionLedger(paths.submissions_log).rows
    assert [(r.event, r.reason) for r in rows] == [("lock", "theirs")]


def test_a_write_command_aborts_while_another_process_holds_the_ledger(roots, monkeypatch):
    paths = _paths(roots)
    dump_yaml_model(_profile(), paths.submit_yaml)
    monkeypatch.setattr(lockmod, "WAIT_SECONDS", 0.3)
    monkeypatch.setattr(lockmod, "RETRY_SECONDS", 0.05)
    proc, pid = _hold(ledger_lock_file(paths, paths.submissions_log), paths.submissions_log)
    try:
        r = runner.invoke(app, ["submit", "lock", "--dataset", TEST, "--reason", "x"])
    finally:
        _release(proc)
    v = _verdict(r.output)
    assert r.exit_code == 2 and "status=ABORT" in v and "locked:" in v, r.output
    assert f"held by test.holder (pid {pid} on " in v
    assert not paths.submissions_log.exists()
    r = runner.invoke(app, ["submit", "lock", "--dataset", TEST, "--reason", "x"])
    assert r.exit_code == 0 and "ledger=configs" in _verdict(r.output), r.output
    r = runner.invoke(app, ["submit", "status", "--dataset", TEST])
    v = _verdict(r.output)
    assert "ledger=configs" in v and "locked=true" in v
    dump_yaml_model(_profile(ledger="shared"), paths.submit_yaml)
    r = runner.invoke(app, ["submit", "unlock", "--dataset", TEST, "--reason", "y"])
    v = _verdict(r.output)
    assert r.exit_code == 1 and "not_adopted:" in v and "ledger=shared" in v
