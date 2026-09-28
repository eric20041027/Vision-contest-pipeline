"""``vcp submit ledger adopt`` (spec 2026-09-28 §4.1) on profiles written straight to disk, and
the ``not_adopted:`` every submit command says before it."""

import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from vcp.cli import app
from vcp.core import lock as lockmod
from vcp.core.config import dump_yaml_model
from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.paths import DatasetPaths
from vcp.submit.adopt import adopt, merge_ledgers
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import ledger_lock_file, shared_ledger
from vcp.submit.schema import Gate, LedgerRow, PlatformProfile

runner = CliRunner()
T = [f"2026-09-28T0{h}:00:00.000Z" for h in range(6)]

# Holds a lock file named by argv[1] until a line arrives on stdin (the lock-contention pattern
# of tests/unit/core/test_lock.py and tests/unit/submit/test_transactions.py). The child prints
# its own pid: on Windows the venv's python.exe is a launcher, so Popen.pid is not the
# interpreter's pid.
HOLDER = """
import os, sys
from pathlib import Path
from vcp.core.lock import file_lock
with file_lock(Path(sys.argv[1]), command="test.holder", label=sys.argv[2]):
    print(f"held {os.getpid()}", flush=True)
    sys.stdin.readline()
"""


def _hold(lock_file, label):
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_file), label],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    words = proc.stdout.readline().split()
    assert words[:1] == ["held"], words
    return proc, int(words[1])


def _release(proc):
    proc.stdin.write("\n")
    proc.stdin.flush()
    proc.wait(timeout=30)


def _reverse_keys(obj):
    """The same JSON value with every object's keys in reverse order -- for proving dedup
    compares parsed content, not raw bytes."""
    if isinstance(obj, dict):
        return {k: _reverse_keys(obj[k]) for k in reversed(list(obj))}
    if isinstance(obj, list):
        return [_reverse_keys(v) for v in obj]
    return obj


def _profile(ledger="shared", **over):
    base = dict(
        dataset="t",
        eval_dataset="d",
        plan_id="p",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        ledger=ledger,
        created_at=T[0],
    )
    return PlatformProfile(**{**base, **over})


def _staged(sid, ts, eval_run="e"):
    return LedgerRow(
        event="staged",
        ts=ts,
        submission_id=sid,
        kind="candidate",
        eval_run=eval_run,
        gate=Gate(admission="PASS"),
        profile_sha256="p" * 64,
    )


def _uploaded(sid, ts):
    return LedgerRow(
        event="uploaded",
        ts=ts,
        submission_id=sid,
        at=ts,
        source="manual",
        confirmed=True,
        profile_sha256="p" * 64,
    )


def _ledger(path, rows):
    led = SubmissionLedger(path)
    for row in rows:
        led.append(row)
    return path


def _setup(roots, profile):
    paths = DatasetPaths.resolve("t", data_root=roots.data, configs_root=roots.configs)
    dump_yaml_model(profile, paths.submit_yaml)
    return paths


def _kw(roots):
    return {"data_root": roots.data, "configs_root": roots.configs}


def _verdict(output):
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def test_two_checkouts_ledgers_merge_by_ts_and_their_shared_history_is_kept_once(roots, tmp_path):
    paths = _setup(roots, _profile())
    history = [_staged("S1", T[0]), _uploaded("S1", T[1])]  # what git gave both checkouts
    mine = _ledger(paths.submissions_log, [*history, _staged("S3", T[4])])
    theirs = _ledger(
        tmp_path / "other" / "submissions.jsonl",
        [*history, _staged("S2", T[2]), _uploaded("S2", T[3])],
    )
    before = (mine.read_bytes(), theirs.read_bytes())
    res = adopt("t", sources=[mine, theirs], **_kw(roots))
    assert (res.path, res.rows, res.sources, res.duplicates) == (shared_ledger(paths), 5, 2, 2)
    rows = SubmissionLedger(res.path).rows
    assert [(r.event, r.submission_id) for r in rows] == [
        ("staged", "S1"),
        ("uploaded", "S1"),
        ("staged", "S2"),
        ("uploaded", "S2"),
        ("staged", "S3"),
    ]
    assert (mine.read_bytes(), theirs.read_bytes()) == before  # sources are never touched
    assert not list(res.path.parent.glob(".*.tmp"))


def test_merge_orders_by_time_across_sources():
    a = [_staged("S1", T[0]), _uploaded("S1", T[2])]
    b = [_staged("S1", T[0]), _uploaded("S1", T[1])]  # the same id uploaded on each side
    rows, dropped = merge_ledgers([a, b])
    assert dropped == 1 and [r.ts for r in rows] == [T[0], T[1], T[2]]


def test_merge_dedupes_by_parsed_content_not_raw_bytes(tmp_path):
    """spec 2026-09-28 §4.1: the same row written with its JSON keys in a different order (two
    vcp versions, or two editors, serializing the same event) is still one row -- dedup compares
    parsed fields, not the source text."""
    row = _staged("S1", T[0])
    canonical = row.model_dump_json(exclude_none=True)
    reordered = json.dumps(_reverse_keys(json.loads(canonical)))
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    a.write_bytes((canonical + "\n").encode("utf-8"))
    b.write_bytes((reordered + "\n").encode("utf-8"))
    assert a.read_bytes() != b.read_bytes()  # same content, different bytes
    rows, dropped = merge_ledgers([SubmissionLedger(a).rows, SubmissionLedger(b).rows])
    assert dropped == 1
    assert len(rows) == 1


def test_the_cli_adopts_this_checkouts_configs_ledger_by_default(roots):
    paths = _setup(roots, _profile())
    _ledger(paths.submissions_log, [_staged("S1", T[0])])
    r = runner.invoke(app, ["submit", "ledger", "adopt", "--dataset", "t"])
    v = _verdict(r.output)
    assert r.exit_code == 0, r.output
    assert v.startswith("VERDICT cmd=submit.ledger.adopt status=OK")
    for part in ("ledger=shared", "rows=1", "sources=1", "duplicates=0"):
        assert part in v
    assert [r.submission_id for r in SubmissionLedger(shared_ledger(paths)).rows] == ["S1"]


def test_adopt_refusals_write_nothing(roots, tmp_path):
    paths = _setup(roots, _profile("configs"))
    _ledger(paths.submissions_log, [_staged("S1", T[0])])
    with pytest.raises(
        ValidationFailed, match="not_shared: submit.yaml says ledger: configs"
    ) as ei:
        adopt("t", **_kw(roots))
    assert ei.value.fields == {"ledger": "configs"}
    dump_yaml_model(_profile(), paths.submit_yaml)
    other = _ledger(tmp_path / "o.jsonl", [_staged("S1", T[1], eval_run="other")])
    with pytest.raises(ValidationFailed, match="ledger_conflict: .*S1"):
        adopt("t", sources=[paths.submissions_log, other], **_kw(roots))
    with pytest.raises(ValidationFailed, match="not_found: ledger source"):
        adopt("t", sources=[tmp_path / "missing.jsonl"], **_kw(roots))
    assert not shared_ledger(paths).exists()
    adopt("t", **_kw(roots))
    before = shared_ledger(paths).read_bytes()
    with pytest.raises(ValidationFailed, match="exists"):
        adopt("t", **_kw(roots))
    assert shared_ledger(paths).read_bytes() == before


def test_adopt_locks_each_source_while_reading_it(roots, monkeypatch):
    """spec 2026-09-28 §4.1/§4.2: a sibling checkout whose submit.yaml still says
    ``ledger: configs`` writes that source under its own transaction's lock. Adopt only gets one
    shot at a row (a retry is ``exists:``), so it must wait for that lock too instead of risking
    a torn read or a silently-stale snapshot that would lose the row for good."""
    paths = _setup(roots, _profile())
    source = _ledger(paths.submissions_log, [_staged("S1", T[0])])
    before = source.read_bytes()
    monkeypatch.setattr(lockmod, "WAIT_SECONDS", 0.3)
    monkeypatch.setattr(lockmod, "RETRY_SECONDS", 0.05)
    proc, pid = _hold(ledger_lock_file(paths, source), str(source))
    try:
        with pytest.raises(VcpError, match="locked:"):
            adopt("t", **_kw(roots))
    finally:
        _release(proc)
    assert not shared_ledger(paths).exists()
    assert source.read_bytes() == before


@pytest.mark.parametrize(
    "args",
    [
        ["stage", "--id", "S2", "--eval-run", "e", "--test-run", "t1"],
        ["upload", "--id", "S1"],
        ["record", "--id", "S1", "--at", "2026-09-28 00:00", "--tz", "utc"],
        ["score", "--id", "S1", "--public", "0.5"],
        ["sync"],
        ["final"],
        ["lock", "--reason", "x"],
        ["unlock", "--reason", "x"],
        ["status"],
        ["report"],
    ],
    ids=lambda a: a[0],
)
def test_every_submit_command_refuses_a_shared_ledger_before_adopt(roots, args):
    """spec 2026-09-28 §4.1: 正本不存在、configs 台帳又有列 → 所有 submit 命令 FAIL not_adopted."""
    paths = _setup(roots, _profile(platform="kaggle", competition="c1"))
    _ledger(paths.submissions_log, [_staged("S1", T[0])])
    r = runner.invoke(app, ["submit", args[0], "--dataset", "t", *args[1:]])
    v = _verdict(r.output)
    assert r.exit_code == 1 and "not_adopted:" in v and "ledger=shared" in v, r.output
    assert not shared_ledger(paths).exists()
