"""Backup follows the code layer's records (0.18.0, spec 2026-10-31 §4.8, §10.6): a run's code
snapshot, code trace and authorization are manifest roles of their own, and an authorization
brings its basis -- an artifact's files as ``authorization_basis``, or the run itself. The
completeness rule needs no change: a ref bound before the manifest must be in it."""

import json
import sys
from types import SimpleNamespace

import pytest

import helpers
from backup_fixtures import (
    BENCH_ID,
    attach_authorization,
    attach_snapshot,
    attach_trace,
    tie_ref,
    write_bench,
    write_old_manifest,
)
from helpers import FAKE, make_label_set, seed_train
from submit_fixtures import (
    EVAL,
    STAMP,
    TEST,
    code_snapshot,
    make_pair,
    seed_eval_runs,
    seed_judgements,
)
from vcp.artifact import store
from vcp.artifact.authorization import AuthorizeSpec, authorize, consume
from vcp.backup.completeness import manifest_gaps
from vcp.backup.evidence import Collector, build_manifest
from vcp.backup.push import push
from vcp.backup.schema import CARD_ROLES, ROLES, TIER_OF
from vcp.backup.verify import verify
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, artifact_dir
from vcp.data.evidence import RunScope, label_ref
from vcp.data.split import load_plan
from vcp.measure.runs import load_run, run_dir
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile
from vcp.submit.stage import StageSpec, stage
from vcp.train.run import RunSpec, train_run

CODE_ROLES = ("code_snapshot", "code_trace", "authorization", "authorization_basis")


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _col(world) -> Collector:
    return Collector(world.roots.data, world.roots.configs)


def _paths(world, name=EVAL):
    return DatasetPaths.resolve(name, **_kw(world))


def _roles(col) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for e in col.files_of():
        out.setdefault(e.role, []).append(e.path)
    return out


def _snapshot_files(snapshot_id: str) -> list[str]:
    base = f"artifacts/code_snapshot/{snapshot_id}"
    return [f"{base}/{name}" for name in ("files/a.py", "index.jsonl", "manifest.json")] + [
        f"{base}/snapshot.json"
    ]


@pytest.fixture
def coded(world) -> SimpleNamespace:
    """Run ``good`` carries a snapshot, a trace of it and an authorization over an artifact."""
    snapshot = attach_snapshot(world, "good")
    trace = attach_trace(world, "good", snapshot)
    return SimpleNamespace(
        snapshot=snapshot, trace=trace, authorization=attach_authorization(world, "good")
    )


def test_new_roles_and_tiers():
    after = ROLES.index("evidence") + 1
    assert ROLES[after : after + 4] == CODE_ROLES
    assert [TIER_OF[role] for role in CODE_ROLES] == [2, 1, 1, 2]
    assert {"authorization", "code_trace"} <= CARD_ROLES
    assert not {"code_snapshot", "authorization_basis"} & CARD_ROLES


def test_walk_run_collects_code_records(world, coded):
    col = _col(world)
    col.walk_run("good", "run:good")
    roles = _roles(col)
    assert roles["code_snapshot"] == _snapshot_files(coded.snapshot)
    assert roles["code_trace"] == [
        f"artifacts/code_trace/{coded.trace}/manifest.json",
        f"artifacts/code_trace/{coded.trace}/trace.json",
    ]
    assert roles["authorization"] == [
        f"artifacts/authorization/{coded.authorization}/authorization.json",
        f"artifacts/authorization/{coded.authorization}/manifest.json",
    ]
    # the basis the authorization pinned: its manifest and its files, a tier-2 role of their own
    assert roles["authorization_basis"] == [
        f"artifacts/benchmark/{BENCH_ID}/manifest.json",
        f"artifacts/benchmark/{BENCH_ID}/receipt.json",
    ]
    tier_of = {e.role: e.tier for e in col.files_of()}
    assert [tier_of[role] for role in CODE_ROLES] == [2, 1, 1, 2]
    assert col.missing == [] and col.unlisted == []
    assert all(e.for_ == ["run:good"] for e in col.files_of())


def test_a_run_without_code_records_lists_none_of_the_new_roles(world):
    col = _col(world)
    col.walk_run("good", "run:good")
    assert not set(_roles(col)) & set(CODE_ROLES)


def test_a_basis_that_is_gone_is_listed_missing_with_the_sha_the_authorization_pinned(world, coded):
    basis = store.manifest_path(world.roots.data, "benchmark", BENCH_ID)
    authorization = store.load_manifest(world.roots.data, "authorization", coded.authorization)
    (pinned,) = [i.sha256 for i in authorization.spec.inputs if i.name == "basis"]
    basis.unlink()
    col = _col(world)
    col.walk_run("good", "run:good")
    key = f"data/artifacts/benchmark/{BENCH_ID}/manifest.json"
    listed = {e.key: e for e in col.files_of()}[key]
    assert (listed.role, listed.sha256, listed.present) == ("authorization_basis", pinned, False)
    assert col.missing == [key]


def test_a_run_basis_is_walked_as_a_run(world):
    attach_authorization(world, "good", "auth-run", basis="run:bad")
    col = _col(world)
    col.walk_run("good", "run:good")
    roles = _roles(col)
    assert sorted(roles["run_card"]) == ["runs/bad/run.yaml", "runs/good/run.yaml"]
    assert any(path.startswith("runs/bad/predictions/") for path in roles["prediction"])
    assert "authorization_basis" not in roles  # a run is not an artifact basis
    assert all(e.for_ == ["run:good"] for e in col.files_of())


def test_a_run_basis_without_a_run_card_fails_the_run_conclusion(world):
    """A run basis is walked as a run, so one that is gone fails the way a missing fuse member
    does: ``not_found:`` and no manifest (controller ruling, Task 18)."""
    attach_authorization(world, "good", "auth-run", basis="run:bad")
    (run_dir(world.roots.data, "bad") / "run.yaml").unlink()
    with pytest.raises(ValidationFailed, match="not_found: run 'bad'"):
        build_manifest(EVAL, "run:good", manifest_id="gone", **_kw(world))
    assert not _paths(world).backup_manifest("gone").exists()


def test_a_rewritten_basis_is_backed_up_as_it_is_and_consume_refuses_it(world, coded):
    """Backup records evidence as it exists: a basis ``manifest.json`` rewritten after the
    authorization is listed with the bytes it has now, and ``verify`` finds nothing wrong. That
    the basis is no longer the one authorized is ``consume``'s to say (``superseded:``)."""
    claims = {"dataset": EVAL, "plan_id": "fixed-v1"}
    held = {"consumer": "train", "target_kind": None, "claims": claims}
    consume(world.roots.data, coded.authorization, **held)
    basis = store.manifest_path(world.roots.data, "benchmark", BENCH_ID)
    authorization = store.load_manifest(world.roots.data, "authorization", coded.authorization)
    (pinned,) = [i.sha256 for i in authorization.spec.inputs if i.name == "basis"]
    assert sha256_file(basis) == pinned
    basis.write_bytes(basis.read_bytes() + b"\n\n")  # still the same manifest, other bytes

    built = build_manifest(EVAL, "run:good", manifest_id="rewritten", **_kw(world))
    key = f"data/artifacts/benchmark/{BENCH_ID}/manifest.json"
    row = {f.key: f for f in built.manifest.files}[key]
    assert row.role == "authorization_basis" and row.present
    assert row.sha256 == sha256_file(basis) != pinned
    res = verify(EVAL, "rewritten", **_kw(world))
    assert res.ok and res.drift == [] and res.bad_stamps == []
    with pytest.raises(ValidationFailed, match="superseded"):
        consume(world.roots.data, coded.authorization, **held)


def test_run_bases_that_name_each_other_end_the_walk(world):
    """An authorization whose basis run is, directly or through another, the run being walked."""
    attach_authorization(world, "good", "auth-a", basis="run:bad")
    attach_authorization(world, "bad", "auth-b", basis="run:good")
    attach_authorization(world, "good", "auth-self", basis="run:good")
    col = _col(world)
    col.walk_run("good", "run:good")
    roles = _roles(col)
    assert sorted(roles["run_card"]) == ["runs/bad/run.yaml", "runs/good/run.yaml"]
    manifests = [p for p in roles["authorization"] if p.endswith("manifest.json")]
    assert manifests == [
        f"artifacts/authorization/{a}/manifest.json" for a in ("auth-a", "auth-b", "auth-self")
    ]


def test_an_authorization_that_cannot_be_read_costs_its_basis_and_nothing_else(world, coded):
    adir = artifact_dir(world.roots.data, "authorization", coded.authorization)
    (adir / "authorization.json").unlink()
    col = _col(world)
    col.walk_run("good", "run:good")
    roles = _roles(col)
    assert "authorization_basis" not in roles and "code_trace" in roles
    assert col.missing == [f"data/artifacts/authorization/{coded.authorization}/authorization.json"]


def test_an_authorization_without_a_manifest_is_listed_missing_and_the_walk_goes_on(world, coded):
    adir = artifact_dir(world.roots.data, "authorization", coded.authorization)
    (adir / "manifest.json").unlink()
    col = _col(world)
    col.walk_run("good", "run:good")
    assert col.missing == [f"data/artifacts/authorization/{coded.authorization}/manifest.json"]
    assert "authorization_basis" not in _roles(col) and "code_trace" in _roles(col)


def test_refs_are_told_apart_by_kind_not_by_name(world, tmp_path):
    """A label set attached under the reserved name ``authorization`` is still a label set."""
    make_label_set(world.roots, tmp_path, load_plan(_paths(world), "fixed-v1"), dataset=EVAL)
    card = load_run(world.roots.data, "good")
    scope = RunScope("good", EVAL, card.samples_hash, "fixed-v1", tuple(card.trained_on))
    ref = label_ref(world.roots.data, scope, "pseudo-v1", attempt=None, binding="manual")
    tie_ref(world, "good", ref.model_copy(update={"name": "authorization"}))
    col = _col(world)
    col.walk_run("good", "run:good")
    assert "label_set" in _roles(col) and not set(_roles(col)) & set(CODE_ROLES)
    assert col.missing == [] and col.unlisted == []


@pytest.mark.parametrize("kind", ["code_snapshot", "code_trace", "authorization"])
def test_manifest_gaps_know_the_new_refs(world, coded, kind):
    built = build_manifest(EVAL, "run:good", manifest_id="today", **_kw(world))
    assert manifest_gaps(built.manifest, _paths(world)) == []
    ident = getattr(coded, kind.removeprefix("code_"))
    key = f"data/artifacts/{kind}/{ident}/manifest.json"
    old = write_old_manifest(world, EVAL, "run:good", "old", drop={key})
    gaps = manifest_gaps(old, _paths(world))
    assert [(g.key, g.what) for g in gaps] == [(key, f"good/run.yaml:evidence.{kind}/{ident}")]


def test_a_ref_attached_after_the_manifest_is_not_required(world):
    built = build_manifest(EVAL, "run:good", manifest_id="before", **_kw(world))
    snapshot = attach_snapshot(world, "good")  # bound later: the manifest is stale, not incomplete
    attach_trace(world, "good", snapshot)
    assert manifest_gaps(built.manifest, _paths(world)) == []


def test_backup_verify_with_code_records(world, coded):
    attach_authorization(world, "bad", "auth-run", basis="run:good")
    built = build_manifest(EVAL, "run:bad", manifest_id="m1", **_kw(world))
    assert set(CODE_ROLES) <= {f.role for f in built.manifest.files}
    vault = world.tmp / "vault-code"
    assert not push(EVAL, "m1", str(vault), tier=3, **_kw(world)).failed
    res = verify(EVAL, "m1", dest=str(vault), **_kw(world))
    assert res.ok and res.drift == [] and res.bad_stamps == [] and res.copy_problems == []
    basis_receipt = vault / "data" / "artifacts" / "benchmark" / BENCH_ID / "receipt.json"
    assert basis_receipt.is_file()  # the tier-2 basis travels with the rest


def test_a_trace_stamp_that_does_not_parse_is_seen_by_the_stamp_layer(world, coded):
    """``code_trace`` is a card role: its ``taken_at`` is held to the UTC stamp format."""
    path = artifact_dir(world.roots.data, "code_trace", coded.trace) / "trace.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["taken_at"] = "yesterday"
    path.write_text(json.dumps(doc), encoding="utf-8", newline="\n")
    build_manifest(EVAL, "run:good", manifest_id="m2", **_kw(world))
    res = verify(EVAL, "m2", **_kw(world))
    assert f"data/artifacts/code_trace/{coded.trace}/trace.json:taken_at" in res.bad_stamps


def test_walk_submission_collects_the_kernel_snapshot(roots, tmp_path):
    pair = make_pair(roots, tmp_path)
    seed_eval_runs(pair)
    seed_judgements(pair)
    kw = {"data_root": roots.data, "configs_root": roots.configs}
    profile = PlatformProfile(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        submission_kind="kernel",
        writer=None,
        created_at=STAMP,
    )
    init_profile(profile, **kw)
    snap = code_snapshot(pair, "notebook", b"NOTEBOOK = 1")
    spec = StageSpec(
        dataset=TEST,
        submission_id="K1",
        eval_run="good",
        kernel="u/nb",
        version=3,
        weights=["good"],
        kind="candidate",
        code=snap,
        **kw,
    )
    stage(spec)
    col = Collector(roots.data, roots.configs)
    col.walk_submission(pair.test_paths, "K1", "submission:K1")
    assert _roles(col)["code_snapshot"] == _snapshot_files(snap)
    assert col.missing == [] and col.unlisted == []


# -- a real run: the child traces itself and the run is authorized ------------------------------


def test_a_real_authorized_traced_run_is_backed_up_and_verifies(roots, work, tmp_path, monkeypatch):
    seed_train(roots)
    helpers.trace_on(monkeypatch)
    child = "from vcp.train import Session\nSession.current()\n" + FAKE
    (work / "fake_train.py").write_bytes(child.encode("utf-8"))
    write_bench(roots.data, dataset="tiny")
    authorize(
        AuthorizeSpec(
            "auth-real",
            f"artifact:benchmark/{BENCH_ID}",
            "train",
            None,
            ["param:batch"],
            [],
            "the benchmark passed",
        ),
        data_root=roots.data,
    )
    spec = RunSpec(
        run_id="R",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        cwd=work,
        command=[sys.executable, "fake_train.py", "0"],
        checkpoints=["weights/*.pt"],
        final="weights/best.pt",
        authorization="auth-real",
        data_root=roots.data,
        configs_root=roots.configs,
    )
    res = train_run(spec)
    assert res.attempt.status == "finished" and res.code_trace == "complete"

    kw = {"data_root": roots.data, "configs_root": roots.configs}
    built = build_manifest("tiny", "run:R", manifest_id="real", **kw)
    assert set(CODE_ROLES) <= {f.role for f in built.manifest.files}
    paths = DatasetPaths.resolve("tiny", **kw)
    assert manifest_gaps(built.manifest, paths) == []
    vault = tmp_path / "vault-real"
    assert not push("tiny", "real", str(vault), tier=2, **kw).failed
    out = verify("tiny", "real", dest=str(vault), tier=2, **kw)
    assert out.ok and out.bad_stamps == [] and out.drift == []
