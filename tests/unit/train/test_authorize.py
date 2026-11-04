"""Claims of a training run (spec 2026-10-31 §4.6, right column, §5.6): ``train_claims`` names
what a ``train`` consumer can check, ``run_claims`` reads them from a finished run -- the last
finished attempt's, else the last attempt's -- and ``authorize`` writes them as an authorization
that ``consume`` holds a later run against."""

import json
import shutil
import sys
from functools import partial

import pytest

from helpers import make_label_set, seed_train
from vcp.artifact import store
from vcp.artifact.authorization import AuthorizeSpec, authorize, consume, in_vocabulary
from vcp.code.schema import EnvIdentity
from vcp.code.snapshot import SnapshotOutcome, snapshot_json_path
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file, sha256_json
from vcp.core.paths import DatasetPaths, artifact_dir
from vcp.data.evidence import RunScope, attach_evidence
from vcp.measure.runs import load_run, run_dir
from vcp.train import coderun
from vcp.train.authorize import run_claims, train_claims
from vcp.train.records import bind_ref, load_record, save_record, train_yaml
from vcp.train.run import RunSpec, train_run

ALL = {
    "dataset",
    "plan_id",
    "seed",
    "param:samples_hash",
    "param:trained_on",
    "param:config_sha256",
    "param:command_sha256",
    "input:plan",
    "input:code_snapshot.cwd",
    "env:python",
    "env:packages",
    "env:gpus",
}
SHA = "ab" * 32


def _spec(roots, cwd, **kw) -> RunSpec:
    base = dict(
        run_id="r",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        cwd=cwd,
        command=[sys.executable, "fake_train.py", "0"],
        checkpoints=["weights/*.pt"],
        final="weights/best.pt",
        data_root=roots.data,
        configs_root=roots.configs,
    )
    return RunSpec(**{**base, **kw})


def _claims(roots, run_id="r") -> dict[str, str]:
    return run_claims(roots.data, roots.configs, run_id)


def _env(roots, n=1, run_id="r") -> dict:
    return json.loads((run_dir(roots.data, run_id) / "train" / f"env.{n}.json").read_bytes())


def _plan_file(roots, dataset="tiny", plan_id="fixed-v1"):
    paths = DatasetPaths.resolve(dataset, data_root=roots.data, configs_root=roots.configs)
    return paths.plan_json(plan_id)


def _two_attempts(roots, work):
    """Attempt 1 finished with seed 1; attempt 2 (``--resume --seed 2``) ran a command that
    failed. A ``--config`` keeps the config hash, so the command may differ."""
    seed_train(roots)
    cfg = work / "cfg.yaml"
    cfg.write_text("epochs: 1\n", encoding="utf-8")
    first = train_run(_spec(roots, work, config=cfg, seed=1))
    failing = [sys.executable, "fake_train.py", "1"]
    second = train_run(_spec(roots, work, config=cfg, seed=2, resume=True, command=failing))
    assert (first.attempt.status, second.attempt.status) == ("finished", "failed")
    return cfg


# -- the claims of a run --------------------------------------------------------------------------


def test_run_claims_from_the_last_finished_attempt(roots, work):
    cfg = _two_attempts(roots, work)
    c = _claims(roots)
    record = load_record(roots.data, "r")
    env1 = _env(roots, 1)
    snapshot = snapshot_json_path(roots.data, env1["code"]["roots"][0]["snapshot"])
    assert set(c) == ALL and list(c) == sorted(c)
    assert c["seed"] == "1" and c["param:config_sha256"] == record.config_hash == sha256_file(cfg)
    assert c["dataset"] == "tiny" and c["plan_id"] == "fixed-v1"
    assert c["param:trained_on"] == "train"
    assert c["param:samples_hash"] == load_run(roots.data, "r").samples_hash
    assert c["param:command_sha256"] == sha256_json(record.attempts[0].command)
    assert c["param:command_sha256"] != sha256_json(record.attempts[1].command)
    assert c["input:plan"] == sha256_file(_plan_file(roots))
    assert c["input:code_snapshot.cwd"] == sha256_file(snapshot)
    identity = env1["identity"]
    assert (
        c["env:python"] == identity["python"] and c["env:packages"] == identity["packages_sha256"]
    )
    assert c["env:gpus"] == identity["gpus"]
    assert all(in_vocabulary(name, "train") for name in c)


def test_without_a_finished_attempt_the_last_one_speaks(roots, work):
    seed_train(roots)
    failing = [sys.executable, "fake_train.py", "1"]
    cfg = work / "cfg.yaml"
    cfg.write_text("epochs: 1\n", encoding="utf-8")
    train_run(_spec(roots, work, config=cfg, seed=1, command=failing))
    train_run(_spec(roots, work, config=cfg, seed=2, command=failing, resume=True))
    assert [a.status for a in load_record(roots.data, "r").attempts] == ["failed", "failed"]
    assert _claims(roots)["seed"] == "2"


def test_an_attempt_without_a_seed_binds_no_seed(roots, work):
    seed_train(roots)
    train_run(_spec(roots, work))
    assert "seed" not in _claims(roots) and set(_claims(roots)) == ALL - {"seed"}


def test_the_record_command_stands_in_for_an_attempt_that_has_none(roots, work):
    seed_train(roots)
    train_run(_spec(roots, work))
    record = load_record(roots.data, "r")
    bare = record.attempts[0].model_copy(update={"command": None})
    save_record(roots.data, record.model_copy(update={"attempts": [bare]}))
    assert _claims(roots)["param:command_sha256"] == sha256_json(record.command)


def test_train_claims_agree_with_run_claims(roots, work):
    """A consumer that computes its claims from its own inputs gets the same values the run
    basis gave: that is what lets ``consume`` compare them."""
    seed_train(roots)
    command = [sys.executable, "fake_train.py", "0"]
    train_run(_spec(roots, work, seed=7, command=command))
    env1 = _env(roots)
    mine = train_claims(
        dataset="tiny",
        plan_id="fixed-v1",
        seed=7,
        samples_hash=load_run(roots.data, "r").samples_hash,
        trained_on=["train"],
        config_sha256=sha256_json(command),
        command=command,
        plan_sha256=sha256_file(_plan_file(roots)),
        code={
            "cwd": sha256_file(snapshot_json_path(roots.data, env1["code"]["roots"][0]["snapshot"]))
        },
        evidence={},
        labels={},
        identity=EnvIdentity(**env1["identity"]),
    )
    assert mine == _claims(roots) and list(mine) == sorted(mine)


def test_train_claims_shape():
    base = dict(
        dataset="d",
        plan_id="p",
        seed=None,
        samples_hash="s",
        trained_on=["valB", "train", "valA"],
        config_sha256="c",
        command=("python", "x.py"),
        plan_sha256="pl",
        code={"z": "1", "cwd": "2"},
        evidence={"cfg": "e"},
        labels={"pseudo-v1": "l"},
        identity=EnvIdentity(python="cp313", packages_sha256="pk", gpus=""),
    )
    claims = train_claims(**base)
    assert "seed" not in claims
    assert claims["param:trained_on"] == "train,valA,valB"
    assert claims["param:command_sha256"] == sha256_json(["python", "x.py"])
    assert claims["input:code_snapshot.cwd"] == "2" and claims["input:code_snapshot.z"] == "1"
    assert claims["input:evidence.cfg"] == "e" and claims["input:labels.pseudo-v1"] == "l"
    assert claims["env:gpus"] == "" and claims["env:python"] == "cp313"
    assert list(claims) == sorted(claims) and all(in_vocabulary(n, "train") for n in claims)
    assert train_claims(**{**base, "seed": 0})["seed"] == "0"
    bare = train_claims(**{**base, "identity": None})
    assert not any(name.startswith("env:") for name in bare)
    assert train_claims(**{**base, "code": {}, "evidence": {}, "labels": {}}).keys() == {
        "dataset",
        "plan_id",
        "param:samples_hash",
        "param:trained_on",
        "param:config_sha256",
        "param:command_sha256",
        "input:plan",
        "env:python",
        "env:packages",
        "env:gpus",
    }
    assert base["trained_on"] == ["valB", "train", "valA"]  # the caller's list is left alone


# -- evidence and labels (R6) ---------------------------------------------------------------------


def test_evidence_and_labels_become_claims(roots, work, tmp_path):
    _, plan = seed_train(roots)
    make_label_set(roots, tmp_path, plan)
    f = tmp_path / "cfg.yaml"
    f.write_text("a: 1\n", encoding="utf-8")
    train_run(_spec(roots, work, evidence=[f"cfg={f}"], labels=["pseudo-v1"]))
    c = _claims(roots)
    label_manifest = store.manifest_path(roots.data, "label_set", "pseudo-v1")
    assert c["input:evidence.cfg"] == sha256_file(f)
    assert c["input:labels.pseudo-v1"] == sha256_file(label_manifest)
    assert set(c) == ALL - {"seed"} | {"input:evidence.cfg", "input:labels.pseudo-v1"}


def test_refs_are_picked_by_kind_and_role_and_not_by_name(roots, work, tmp_path):
    """A label set may be called ``code.v1``: it is a label claim, and the code snapshot named
    ``code.cwd`` is not an evidence claim."""
    _, plan = seed_train(roots)
    make_label_set(roots, tmp_path, plan, label_set_id="code.v1")
    train_run(_spec(roots, work, labels=["code.v1"]))
    c = _claims(roots)
    assert "input:labels.code.v1" in c and "input:code_snapshot.cwd" in c
    assert not any(name.startswith("input:evidence.") for name in c)
    assert [r.name for r in load_record(roots.data, "r").evidence if r.kind == "code_snapshot"] == [
        "code.cwd"
    ]


def test_only_the_chosen_attempts_refs_are_claims(roots, work, tmp_path):
    """Attempt 2 attaches a newer ``cfg`` and a new ``extra``; the run's claims are attempt 1's
    (spec §4.6: the chosen attempt's record), so neither is among them."""
    seed_train(roots)
    cfg = work / "cfg.yaml"
    cfg.write_text("epochs: 1\n", encoding="utf-8")
    f, g = tmp_path / "f.txt", tmp_path / "g.txt"
    f.write_text("one", encoding="utf-8")
    g.write_text("g", encoding="utf-8")
    train_run(_spec(roots, work, config=cfg, seed=1, evidence=[f"cfg={f}"]))
    before = _claims(roots)
    assert before["input:evidence.cfg"] == sha256_file(f)
    original = before["input:evidence.cfg"]
    f.write_text("two", encoding="utf-8")
    failing = [sys.executable, "fake_train.py", "1"]
    second = train_run(
        _spec(
            roots,
            work,
            config=cfg,
            seed=1,
            resume=True,
            command=failing,
            evidence=[f"cfg={f}", f"extra={g}"],
        )
    )
    assert second.attempt.status == "failed"
    after = _claims(roots)
    assert after["input:evidence.cfg"] == original != sha256_file(f)
    assert "input:evidence.extra" not in after


def test_the_last_attempt_sees_its_own_newer_refs_when_none_finished(roots, work, tmp_path):
    seed_train(roots)
    f = tmp_path / "f.txt"
    f.write_text("one", encoding="utf-8")
    failing = [sys.executable, "fake_train.py", "1"]
    cfg = work / "cfg.yaml"
    cfg.write_text("epochs: 1\n", encoding="utf-8")
    train_run(_spec(roots, work, config=cfg, command=failing, evidence=[f"cfg={f}"]))
    f.write_text("two", encoding="utf-8")
    train_run(_spec(roots, work, config=cfg, command=failing, resume=True, evidence=[f"cfg={f}"]))
    assert _claims(roots)["input:evidence.cfg"] == sha256_file(f)


def test_session_bound_evidence_is_not_a_claim(roots, work, tmp_path):
    """R6: only what ``train run --evidence`` / ``--labels`` attached is known before a consumer's
    first write, so it is all a consumer can be asked to match."""
    seed_train(roots)
    train_run(_spec(roots, work))
    teacher = tmp_path / "teacher.jsonl"
    teacher.write_text("{}\n", encoding="utf-8")
    scope = RunScope("r", "tiny", load_run(roots.data, "r").samples_hash, "fixed-v1", ("train",))
    ref = attach_evidence(
        roots.data, scope, "teacher", teacher, role="teacher", attempt=1, binding="session"
    )
    bind_ref(roots.data, load_record(roots.data, "r"), ref, 1)
    assert any(r.name == "teacher" for r in load_record(roots.data, "r").evidence)
    assert not any(name.startswith("input:evidence.") for name in _claims(roots))


# -- a basis that cannot give the claims ----------------------------------------------------------


def test_a_basis_run_missing_a_snapshot_is_refused(roots, work, monkeypatch):
    seed_train(roots)

    def locked(data_root, scan, *, command):
        return SnapshotOutcome(str(scan.snapshot_id), None, "locked", 0, 0)

    monkeypatch.setattr(coderun, "write_snapshot", locked)
    res = train_run(_spec(roots, work))
    assert res.attempt.status == "finished" and res.code_missing == ["cwd"]
    with pytest.raises(ValidationFailed, match="^invalid: basis run r attempt 1 .*snapshot.*cwd"):
        _claims(roots)


def test_a_missing_environment_file_means_no_snapshot_either(roots, work):
    seed_train(roots)
    train_run(_spec(roots, work))
    (run_dir(roots.data, "r") / "train" / "env.1.json").unlink()
    with pytest.raises(ValidationFailed, match="^invalid: basis run r attempt 1 has no code snap"):
        _claims(roots)


@pytest.mark.parametrize("identity", ["missing", "null", "not_an_identity"])
def test_a_basis_attempt_without_an_environment_identity_is_invalid(roots, work, identity):
    """Without its ``identity`` the attempt gives no ``env:`` claims: an authorization over it
    would bind fewer claims than the run it vouches for."""
    seed_train(roots)
    train_run(_spec(roots, work))
    path = run_dir(roots.data, "r") / "train" / "env.1.json"
    doc = json.loads(path.read_bytes())
    if identity == "missing":
        del doc["identity"]
    else:
        doc["identity"] = None if identity == "null" else {"python": "cp313"}
    path.write_bytes((json.dumps(doc, indent=2) + "\n").encode("utf-8"))
    with pytest.raises(
        ValidationFailed, match="^invalid: basis run r attempt 1 has no environment identity"
    ):
        _claims(roots)


def test_a_snapshot_that_is_gone_is_not_found(roots, work):
    seed_train(roots)
    train_run(_spec(roots, work))
    snapshot_id = _env(roots)["code"]["roots"][0]["snapshot"]
    shutil.rmtree(artifact_dir(roots.data, "code_snapshot", snapshot_id))
    with pytest.raises(ValidationFailed, match=f"^not_found: .*{snapshot_id}"):
        _claims(roots)


def test_a_run_without_attempts_or_a_record_is_invalid(roots, work):
    seed_train(roots)
    train_run(_spec(roots, work))
    record = load_record(roots.data, "r")
    save_record(roots.data, record.model_copy(update={"attempts": []}))
    with pytest.raises(ValidationFailed, match="^invalid: basis run r has no attempt"):
        _claims(roots)
    train_yaml(roots.data, "r").unlink()  # an ingested run has a run.yaml and no train.yaml
    with pytest.raises(ValidationFailed, match="^invalid: basis run r has no training record"):
        _claims(roots)


def test_a_plan_file_that_is_gone_is_not_found(roots, work):
    seed_train(roots)
    train_run(_spec(roots, work))
    _plan_file(roots).unlink()
    with pytest.raises(ValidationFailed, match="^not_found: .*fixed-v1"):
        _claims(roots)


# -- authorize and consume over a run basis ------------------------------------------------------


def _authorize(roots, authorization_id="a1", *, free=(), set_=(), basis="run:r"):
    spec = AuthorizeSpec(
        authorization_id, basis, "train", None, list(free), list(set_), "benchmark run passed"
    )
    return authorize(
        spec, data_root=roots.data, run_claims=partial(run_claims, roots.data, roots.configs)
    )


def test_a_run_basis_authorizes_a_train_consumer(roots, work):
    seed_train(roots)
    train_run(_spec(roots, work, seed=1))
    res = _authorize(roots, free=["param:command_sha256"])
    claims = _claims(roots)
    assert res.claims == {k: v for k, v in claims.items() if k != "param:command_sha256"}
    assert res.freed == ["param:command_sha256"] and res.consumer == "train"
    now = {**claims, "param:command_sha256": SHA, "param:extra": "x"}  # freed, and not bound
    doc = consume(roots.data, "a1", consumer="train", target_kind=None, claims=now)
    assert doc.basis == "run:r" and doc.target_kind is None
    for name in ("seed", "env:python", "input:code_snapshot.cwd", "param:trained_on"):
        with pytest.raises(ValidationFailed, match="^unauthorized:") as ei:
            consume(roots.data, "a1", consumer="train", target_kind=None, claims={**now, name: SHA})
        assert ei.value.fields == {"authorization": "a1", "differs": name}


def test_set_can_bind_a_run_claim_the_basis_lacks(roots, work, tmp_path):
    seed_train(roots)
    train_run(_spec(roots, work))
    f = tmp_path / "corpus.txt"
    f.write_text("x", encoding="utf-8")
    res = _authorize(roots, set_=[f"input:evidence.corpus=@{f}", "seed=3"])
    assert res.claims["input:evidence.corpus"] == sha256_file(f) and res.claims["seed"] == "3"


def test_a_basis_that_fails_in_the_training_layer_names_the_basis_and_not_its_artifact(
    roots, work, tmp_path
):
    seed_train(roots)
    f = tmp_path / "cfg.yaml"
    f.write_text("a: 1\n", encoding="utf-8")
    train_run(_spec(roots, work, evidence=[f"cfg={f}"]))
    (ref,) = [r for r in load_record(roots.data, "r").evidence if r.kind == "evidence"]
    shutil.rmtree(artifact_dir(roots.data, "evidence", ref.artifact_id))
    with pytest.raises(ValidationFailed, match="^not_found:") as ei:
        _authorize(roots)
    assert ei.value.fields == {"basis": "run:r"}
    assert not artifact_dir(roots.data, "authorization", "a1").exists()
    shutil.rmtree(run_dir(roots.data, "r") / "train")  # nothing left to read the environment from
    with pytest.raises(ValidationFailed) as ei:
        _authorize(roots)
    assert ei.value.fields == {"basis": "run:r"}
