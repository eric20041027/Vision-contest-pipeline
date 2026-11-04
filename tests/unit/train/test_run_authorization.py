"""``vcp train run --authorization`` (spec 2026-10-31 §5.1 steps 6 and 8, §5.6, §10.2, §10.5, §16
items 5-6): before its first write the run computes its own claims and holds them against the
authorization -- a claim that differs is ``unauthorized:`` with nothing written -- and, with an
authorization, a root whose snapshot cannot be written fails closed instead of degrading to a WARN.
The run pins the authorization as an ``authorization`` ref; it holds for the one call that names
it."""

import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helpers import FAKE, git_repo, make_label_set, seed_train
from vcp.artifact import store
from vcp.artifact.authorization import AuthorizeSpec, authorize
from vcp.cli import app
from vcp.code import rules
from vcp.code.snapshot import SnapshotOutcome
from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.hashing import sha256_file
from vcp.data.split import SplitPlan
from vcp.measure.runs import load_run, run_dir
from vcp.train import coderun
from vcp.train import run as run_module
from vcp.train.authorize import run_claims
from vcp.train.records import load_record, read_events
from vcp.train.run import RunSpec, train_run

# C1: a framework writes its settings and metrics into --cwd, untracked.
OUTPUT_FAKE = (
    FAKE.replace("sys.exit(code)\n", "")
    + """
for name in ("args.yaml", "hparams.yaml", "config.json", "metrics.json"):
    Path(name).write_text("epoch: " + str(time.time()) + "\\n")
sys.exit(code)
"""
)
OUTPUTS = ("args.yaml", "hparams.yaml", "config.json", "metrics.json")
FREED = ["param:command_sha256"]  # the full run trains longer than its benchmark


@dataclass(frozen=True)
class World:
    """A benchmark run ``R1`` and the authorization ``a1`` over it. R1 and every later run share
    one ``--config`` file (R17), one ``--evidence cfg`` file and the label set ``pseudo-v1``."""

    work: Path
    cfg: Path
    notes: Path
    plan: SplitPlan


def _spec(roots, w: World, run_id: str, **kw) -> RunSpec:
    base = dict(
        run_id=run_id,
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        cwd=w.work,
        command=[sys.executable, "fake_train.py", "0"],
        config=w.cfg,
        seed=1,
        evidence=[f"cfg={w.notes}"],
        labels=["pseudo-v1"],
        checkpoints=["weights/*.pt"],
        final="weights/best.pt",
        data_root=roots.data,
        configs_root=roots.configs,
    )
    return RunSpec(**{**base, **kw})


def _authorize(roots, authorization_id: str, *, free=FREED, supersedes=None):
    spec = AuthorizeSpec(
        authorization_id,
        "run:R1",
        "train",
        None,
        list(free),
        [],
        "the benchmark run passed",
        supersedes=supersedes,
    )
    return authorize(
        spec, data_root=roots.data, run_claims=partial(run_claims, roots.data, roots.configs)
    )


def _world(roots, tmp_path, work: Path) -> World:
    _, plan = seed_train(roots)
    make_label_set(roots, tmp_path, plan)
    cfg, notes = tmp_path / "cfg.yaml", tmp_path / "notes.txt"  # outside the code root
    cfg.write_bytes(b"epochs: 1\n")
    notes.write_bytes(b"lr 0.01\n")
    w = World(work=work, cfg=cfg, notes=notes, plan=plan)
    assert train_run(_spec(roots, w, "R1")).attempt.status == "finished"
    _authorize(roots, "a1")
    return w


@pytest.fixture
def repo(roots, tmp_path, isolated_git) -> World:
    """The benchmark in a repository whose child leaves framework outputs in --cwd."""
    files = {"fake_train.py": OUTPUT_FAKE.encode(), ".gitignore": b"weights/\n"}
    return _world(roots, tmp_path, git_repo(tmp_path / "work", files))


@pytest.fixture
def plain(roots, tmp_path, work) -> World:
    """The benchmark in a plain directory (no git needed)."""
    return _world(roots, tmp_path, work)


def _authorization_refs(roots, run_id: str):
    """The run's authorization refs, picked by kind and role."""
    record = load_record(roots.data, run_id)
    return [r for r in record.evidence if r.kind == "authorization" and r.role == "authorization"]


def _snapshots(roots) -> list[str]:
    base = roots.data / "artifacts" / "code_snapshot"
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


def _manifest_sha(roots, authorization_id: str) -> str:
    return sha256_file(store.manifest_path(roots.data, "authorization", authorization_id))


# -- an authorized run ----------------------------------------------------------------------------


def test_a_benchmark_run_authorizes_the_full_run(roots, repo):
    """C1's reason itself: the benchmark's framework wrote its settings into --cwd, and the full
    run -- the same setup, more epochs -- still comes out the same on every bound claim."""
    assert all((repo.work / name).is_file() for name in OUTPUTS)
    longer = [sys.executable, "fake_train.py", "0", "--epochs", "50"]
    res = train_run(_spec(roots, repo, "R2", command=longer, authorization="a1"))
    assert res.attempt.status == "finished" and res.authorization == "a1"
    evidence = load_record(roots.data, "R2").evidence
    assert [r.name for r in evidence if r.kind == "authorization"] == ["authorization"]
    (ref,) = _authorization_refs(roots, "R2")
    assert (ref.artifact_id, ref.binding, ref.attempt) == ("a1", "cli", 1)
    assert ref.manifest_sha256 == _manifest_sha(roots, "a1")
    assert "authorization" in [r.name for r in load_run(roots.data, "R2").evidence]
    events = [e for e in read_events(roots.data, "R2") if e["event"] == "evidence"]
    assert [e["name"] for e in events if e["kind"] == "authorization"] == ["authorization"]
    assert res.evidence == 2  # cfg and pseudo-v1: vcp's own refs are not counted


def test_the_claims_before_the_first_write_are_the_basis_claims(roots, repo, monkeypatch):
    """What R2 computes from its own inputs before it writes anything is byte-equal to what R1's
    records give for the same setup: that is what lets ``consume`` compare them."""
    seen: list[dict[str, str]] = []
    real = run_module.consume

    def spy(*args, claims, **kwargs):
        seen.append(dict(claims))
        return real(*args, claims=claims, **kwargs)

    monkeypatch.setattr(run_module, "consume", spy)
    train_run(_spec(roots, repo, "R2", authorization="a1"))
    assert seen == [run_claims(roots.data, roots.configs, "R1")]
    assert {"input:evidence.cfg", "input:labels.pseudo-v1", "input:code_snapshot.cwd"} <= set(
        seen[0]
    )


@pytest.mark.parametrize("change", ["seed", "code", "evidence", "config"])
def test_any_bound_train_claim_that_changes_is_unauthorized(roots, repo, change):
    want = {
        "seed": "seed",
        "code": "input:code_snapshot.cwd",
        "evidence": "input:evidence.cfg",
        "config": "param:config_sha256",
    }
    kw = {}
    if change == "seed":
        kw["seed"] = 2
    elif change == "code":
        (repo.work / "fake_train.py").write_bytes(OUTPUT_FAKE.encode() + b"# v2\n")
    elif change == "evidence":
        repo.notes.write_bytes(b"lr 0.02\n")
    else:
        repo.cfg.write_bytes(b"epochs: 2\n")  # R17: the one --config file R1 and R2 share
    before = _snapshots(roots)
    with pytest.raises(ValidationFailed, match="^unauthorized: authorization a1 ") as ei:
        train_run(_spec(roots, repo, "R2", authorization="a1", **kw))
    assert ei.value.fields == {"authorization": "a1", "differs": want[change]}
    assert not run_dir(roots.data, "R2").exists() and _snapshots(roots) == before


def test_a_superseded_authorization_stops_the_run(roots, plain):
    _authorize(roots, "a2", supersedes="a1")
    with pytest.raises(ValidationFailed, match="^superseded: authorization a1 "):
        train_run(_spec(roots, plain, "R2", authorization="a1"))
    assert not run_dir(roots.data, "R2").exists()
    assert train_run(_spec(roots, plain, "R2", authorization="a2")).authorization == "a2"


def test_an_authorization_that_is_not_there_is_not_found(roots, plain):
    with pytest.raises(ValidationFailed, match="^not_found:") as ei:
        train_run(_spec(roots, plain, "R2", authorization="nope"))
    assert ei.value.fields == {"authorization": "nope"}
    assert not run_dir(roots.data, "R2").exists()


def test_a_too_large_root_the_authorization_binds_is_unauthorized(roots, plain, monkeypatch):
    monkeypatch.setattr(rules, "MAX_FILES", 0)  # cwd holds fake_train.py: too_large
    before = _snapshots(roots)
    with pytest.raises(ValidationFailed, match="^unauthorized:") as ei:
        train_run(_spec(roots, plain, "R2", authorization="a1"))
    assert ei.value.fields["differs"] == "input:code_snapshot.cwd"
    assert "input:code_snapshot.cwd: authorized " in str(ei.value) and "got none" in str(ei.value)
    assert not run_dir(roots.data, "R2").exists() and _snapshots(roots) == before


def test_a_too_large_root_the_authorization_frees_is_only_a_warn(roots, plain, monkeypatch):
    """Only what the authorization binds is held: a root it frees keeps §5.1's downgrade."""
    _authorize(roots, "a-free", free=[*FREED, "input:code_snapshot.cwd"])
    monkeypatch.setattr(rules, "MAX_FILES", 0)
    res = train_run(_spec(roots, plain, "R2", authorization="a-free"))
    assert res.attempt.status == "finished" and res.authorization == "a-free"
    assert (res.code_snapshot, res.code_missing) == ("none", ["cwd"])
    assert "code_snapshot=none (code_missing=cwd)" in res.warnings
    assert [r.artifact_id for r in _authorization_refs(roots, "R2")] == ["a-free"]


@pytest.mark.parametrize(
    ("reason", "word", "status"),
    [
        ("drift", "drift:", "FAIL"),
        ("locked", "code_unavailable:", "ABORT"),
        ("partial", "code_unavailable:", "ABORT"),
        ("unwritable", "code_unavailable:", "ABORT"),
    ],
)
def test_with_an_authorization_a_snapshot_that_cannot_be_written_fails_closed(
    roots, plain, monkeypatch, reason, word, status
):
    """C3: no authorization ref on a run with a root that has no snapshot -- and the run has no
    files yet when that is known."""

    def refused(data_root, scan, *, command):
        return SnapshotOutcome(str(scan.snapshot_id), None, reason, 0, 0)

    monkeypatch.setattr(coderun, "write_snapshot", refused)
    with pytest.raises(VcpError) as ei:
        train_run(_spec(roots, plain, "R2", authorization="a1"))
    assert ei.value.status == status and str(ei.value).startswith(word)
    assert ei.value.fields["code_root"] == "cwd"
    assert reason == "drift" or ei.value.fields["cause"] == reason
    assert not run_dir(roots.data, "R2").exists()


def test_an_authorization_holds_for_one_call(roots, plain):
    """--resume: each attempt is checked again, and --authorization applies to the call that
    gives it."""
    train_run(_spec(roots, plain, "R2", authorization="a1"))
    again = train_run(_spec(roots, plain, "R2", resume=True))
    assert again.attempt.status == "finished" and again.authorization is None
    assert len(_authorization_refs(roots, "R2")) == 1
    third = train_run(_spec(roots, plain, "R2", resume=True, authorization="a1"))
    assert third.authorization == "a1" and third.attempt.n == 3
    assert len(_authorization_refs(roots, "R2")) == 1  # the same manifest: no second row
    with pytest.raises(ValidationFailed, match="^unauthorized:") as ei:
        train_run(_spec(roots, plain, "R2", resume=True, authorization="a1", seed=2))
    assert ei.value.fields["differs"] == "seed"
    assert len(load_record(roots.data, "R2").attempts) == 3


def test_one_authorization_serves_two_runs_and_both_pin_it(roots, plain):
    """R26b (spec §10.5): an authorization is reused, and every run pins it in its own record."""
    for run_id in ("R2", "R3"):
        assert train_run(_spec(roots, plain, run_id, authorization="a1")).authorization == "a1"
    pinned = {
        run_id: [(r.artifact_id, r.manifest_sha256) for r in _authorization_refs(roots, run_id)]
        for run_id in ("R2", "R3")
    }
    manifest = _manifest_sha(roots, "a1")
    assert pinned == {"R2": [("a1", manifest)], "R3": [("a1", manifest)]}


def test_a_label_set_named_authorization_fails_before_any_write(roots, plain, tmp_path):
    """The ``authorization`` ref's name is checked with the code refs' names, before anything."""
    make_label_set(roots, tmp_path, plain.plan, label_set_id="authorization")
    labels = ["pseudo-v1", "authorization"]
    with pytest.raises(ValidationFailed, match="^evidence_conflict: 'authorization'"):
        train_run(_spec(roots, plain, "R2", labels=labels, authorization="a1"))
    assert not run_dir(roots.data, "R2").exists()


# -- the CLI --------------------------------------------------------------------------------------


def test_train_run_cli_authorization_fields(roots, plain, monkeypatch):
    def invoke(run_id: str, *extra: str):
        args = ["train", "run", "--run", run_id, "--dataset", "tiny", "--plan", "fixed-v1"]
        args += ["--trained-on", "train", "--cwd", str(plain.work), "--config", str(plain.cfg)]
        args += ["--evidence", f"cfg={plain.notes}", "--labels", "pseudo-v1"]
        args += ["--checkpoints", "weights/*.pt", "--final", "weights/best.pt", *extra]
        out = CliRunner().invoke(app, [*args, "--", sys.executable, "fake_train.py", "0"])
        verdicts = [line for line in out.output.splitlines() if line.startswith("VERDICT ")]
        assert verdicts, out.output
        return out, verdicts[-1]

    out, v = invoke("R0", "--seed", "1")
    assert out.exit_code == 0 and "authorization=" not in v
    out, v = invoke("R2", "--seed", "1", "--authorization", "a1")
    assert out.exit_code == 0, out.output
    assert " authorization=a1" in v
    out, v = invoke("R3", "--seed", "2", "--authorization", "a1")
    assert out.exit_code == 1 and "status=FAIL" in v and "unauthorized:" in v
    assert "authorization=a1" in v and "differs=seed" in v and "run=R3" in v
    with monkeypatch.context() as m:

        def locked(data_root, scan, *, command):
            return SnapshotOutcome(str(scan.snapshot_id), None, "locked", 0, 0)

        m.setattr(coderun, "write_snapshot", locked)
        out, v = invoke("R4", "--seed", "1", "--authorization", "a1")
    assert out.exit_code == 2 and "status=ABORT" in v and "code_unavailable:" in v
    assert "code_root=cwd" in v and "cause=locked" in v and "authorization=a1" in v
    _authorize(roots, "a2", supersedes="a1")
    out, v = invoke("R5", "--seed", "1", "--authorization", "a1")
    assert out.exit_code == 1 and "superseded:" in v and "authorization=a1" in v
    assert not any(run_dir(roots.data, r).exists() for r in ("R3", "R4", "R5"))
