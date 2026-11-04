"""The provenance graph of code snapshots, traces and authorizations (spec 2026-10-31 §4.8, §10.6):
the three new kinds are ordinary artifact entities whose edges come from ``inputs`` and from a
run's evidence list; run entities carry ``code_grade``; a run basis feeds its authorization and a
kernel submission consumes its notebook snapshot. Nothing here touches PostgreSQL."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing

import pytest

import vcp.data.evidence as evidence_module
from backup_fixtures import (
    attach_authorization,
    attach_snapshot,
    attach_trace,
    make_fusion,
    make_world,
)
from submit_fixtures import EVAL, STAMP, TEST
from vcp.artifact.authorization import AuthorizeSpec, authorize
from vcp.artifact.schema import ArtifactSpec
from vcp.artifact.writer import ArtifactWriter
from vcp.code.schema import CodeRoot, CodeSection
from vcp.core.config import dump_yaml_model
from vcp.core.errors import IntegrityError
from vcp.core.paths import artifact_dir, provenance_index_path, store_path
from vcp.data.evidence_ref import add_refs
from vcp.measure.runs import load_run, save_run
from vcp.provenance.graph import ProvenanceGraph, build_graph
from vcp.provenance.index import ProvenanceIndex, graph_hash
from vcp.submit.schema import PlatformProfile
from vcp.submit.stage import StageSpec, stage
from vcp.train.records import load_record, save_record, train_dir
from vcp.train.schema import Attempt

COMMIT = "6f867fe" + "0" * 33


@pytest.fixture
def world(roots, tmp_path):
    return make_world(roots, tmp_path)


def _build(world) -> ProvenanceGraph:
    return build_graph(world.roots.data, world.roots.configs)


def _kinds(graph: ProvenanceGraph, source: str, target: str, edge_type: str) -> list[dict]:
    """The attributes of every ``edge_type`` edge from ``source`` to ``target``."""
    return [
        e.attributes
        for e in graph.edges.values()
        if (e.source_id, e.target_id, e.edge_type) == (source, target, edge_type)
    ]


def _artifact(kind: str, artifact_id: str) -> str:
    return f"artifact:{kind}/{artifact_id}"


def _trace_run(world) -> tuple[str, str]:
    """Run ``good`` becomes a training run of one attempt: a snapshot of its code, a complete
    trace, and ``train.yaml`` / ``env.1.json`` that say so -- its code grade is ``traced``.
    Returns the snapshot id and the trace id."""
    data = world.roots.data
    snapshot_id = attach_snapshot(world, "good")
    trace_id = attach_trace(world, "good", snapshot_id)
    record = load_record(data, "good")
    attempt = Attempt(n=1, started_at=STAMP, console="train/console.1.log", env="train/env.1.json")
    save_record(
        data,
        record.model_copy(
            update={
                "attempts": [attempt],
                "evidence": add_refs(record.evidence, load_run(data, "good").evidence),
            }
        ),
    )
    root = CodeRoot(
        label="cwd",
        source="cwd",
        kind="dir",
        path=store_path(world.pair.tmp / "code-good", data),
        commit=None,
        snapshot=snapshot_id,
        reason=None,
        files=1,
        untracked_code=0,
        untracked_code_paths=[],
        excluded=0,
        excluded_paths=[],
    )
    env = {
        "git": {"commit": COMMIT, "dirty": False},
        "code": CodeSection(rules=1, excluded_dirs=[], roots=[root]).model_dump(mode="json"),
    }
    (train_dir(data, "good") / "env.1.json").write_bytes(json.dumps(env).encode("utf-8"))
    return snapshot_id, trace_id


def _grade(graph: ProvenanceGraph, run_id: str) -> str:
    """The run entity's code grade; a run without the attribute is declared."""
    return graph.entities[f"run:{run_id}"].attributes.get("code_grade", "declared")


# -- run entities ----------------------------------------------------------------------------------


def test_run_entities_carry_code_grade(world):
    _trace_run(world)
    graph = _build(world)
    assert graph.entities["run:good"].attributes["code_grade"] == "traced"  # snapshot and trace

    attach_snapshot(world, "bad")  # an ingested run with an intact code ref
    graph = _build(world)
    assert graph.entities["run:bad"].attributes["code_grade"] == "snapshot"
    assert graph.entities["run:good"].attributes["code_grade"] == "traced"


def test_a_declared_run_adds_no_attribute_so_a_graph_without_code_keeps_its_bytes(world):
    """Absent means declared: the registered benchmark workloads pin the hash of the graph they
    build (tests/unit/provenance/test_break_even_*), and none of them ties a run to code."""
    make_fusion(world)  # a world of runs, a fusion and a submission, none of it tied to code
    graph = _build(world)

    runs = [e for e in graph.entities.values() if e.entity_type in ("run", "fusion_run")]
    assert {e.entity_id for e in runs} >= {"run:good", "run:bad", "run:good.test", "run:fx"}
    assert all("code_grade" not in e.attributes for e in runs)
    assert all(_grade(graph, e.key) == "declared" for e in runs)

    attach_snapshot(world, "bad")  # ...and only the run that gained code gains the key
    graph = _build(world)
    assert {e.key for e in graph.entities.values() if "code_grade" in e.attributes} == {"bad"}


def test_a_fused_run_carries_its_weakest_members_grade(world):
    _trace_run(world)
    fused = make_fusion(world)
    graph = _build(world)
    assert graph.entities[f"run:{fused}"].entity_type == "fusion_run"
    assert _grade(graph, fused) == "declared"  # bad has no code: the fusion has none

    attach_snapshot(world, "bad")
    graph = _build(world)
    entity = graph.entities[f"run:{fused}"]
    assert entity.entity_type == "fusion_run" and entity.attributes["recipe_id"] == "r1"
    assert entity.attributes["code_grade"] == "snapshot"  # traced + snapshot -> snapshot


def test_the_grade_falls_when_a_snapshot_is_gone(world):
    snapshot_id, _ = _trace_run(world)
    assert _grade(_build(world), "good") == "traced"
    shutil.rmtree(artifact_dir(world.roots.data, "code_snapshot", snapshot_id))
    assert _grade(_build(world), "good") != "traced"


def test_one_graph_build_verifies_each_pinned_artifact_once(world, monkeypatch):
    snapshot_id, trace_id = _trace_run(world)
    attach_snapshot(world, "bad")  # the same bytes: the same snapshot, pinned by a second run
    make_fusion(world)  # ...and a fusion that grades both of its members again
    asked: list[tuple[str, str]] = []
    real = evidence_module.ref_intact

    def counted(data_root, ref):
        asked.append((ref.kind, ref.artifact_id))
        return real(data_root, ref)

    monkeypatch.setattr(evidence_module, "ref_intact", counted)

    _build(world)

    assert ("code_snapshot", snapshot_id) in asked and ("code_trace", trace_id) in asked
    assert len(asked) == len(set(asked))  # one memo for the whole build, not one per run


# -- edges -----------------------------------------------------------------------------------------


def test_a_snapshot_and_a_trace_reach_the_run_through_its_card(world):
    snapshot_id, trace_id = _trace_run(world)
    graph = _build(world)
    snapshot, trace = _artifact("code_snapshot", snapshot_id), _artifact("code_trace", trace_id)

    assert _kinds(graph, snapshot, "run:good", "CONSUMED_BY") == [{}]
    assert _kinds(graph, trace, "run:good", "CONSUMED_BY") == [{}]
    assert _kinds(graph, snapshot, trace, "PRODUCED_BY") == [{"input": "snapshot.cwd"}]
    assert graph.entities[trace].broken_reason is None


def test_a_trace_that_the_run_card_does_not_list_is_not_linked_to_the_run_it_names(world):
    snapshot_id, _ = _trace_run(world)
    loose = attach_trace(world, "bad", snapshot_id)  # params.run = bad ...
    card = load_run(world.roots.data, "bad")
    kept = [ref for ref in card.evidence if ref.kind != "code_trace"]  # ... but run.yaml lacks it
    save_run(world.roots.data, card.model_copy(update={"evidence": kept}))

    graph = _build(world)

    trace = _artifact("code_trace", loose)
    assert trace in graph.entities and graph.entities[trace].attributes["params"]["run"] == "bad"
    assert _kinds(graph, trace, "run:bad", "CONSUMED_BY") == []
    assert graph.outgoing(trace) == []  # nothing consumes it
    assert _kinds(graph, _artifact("code_snapshot", snapshot_id), trace, "PRODUCED_BY")
    # a card that lists it does link it, through its own list
    attach_trace(world, "bad", snapshot_id, seq=2)
    assert _kinds(
        _build(world), _artifact("code_trace", "bad-a1-t2"), "run:bad", "CONSUMED_BY"
    ) == [{}]


def test_an_authorization_links_to_its_basis_and_to_what_pins_it(world, bench):
    # an artifact basis: the benchmark feeds the authorization, which feeds the job
    authorize(
        AuthorizeSpec(
            "a1",
            "artifact:benchmark/b-50",
            "artifact",
            "features",
            ["param:limit"],
            [],
            "50 samples passed",
        ),
        data_root=world.roots.data,
    )
    with ArtifactWriter.create(
        bench.spec("f-all", authorization="a1"), data_root=world.roots.data
    ) as art:
        art.write_bytes("features.bin", b"f")
        art.commit()
    # a run basis: the run `bad` is what the authorization stands on, `good` pins it
    attach_authorization(world, "good", "auth-r", basis="run:bad")

    graph = _build(world)

    benchmark, auth = _artifact("benchmark", "b-50"), _artifact("authorization", "a1")
    features = _artifact("features", "f-all")
    snapshot = _artifact("code_snapshot", bench.files["code_snapshot.cwd"].parent.name)
    assert snapshot in graph.entities
    assert _kinds(graph, benchmark, auth, "PRODUCED_BY") == [{"input": "basis"}]
    assert _kinds(graph, auth, features, "PRODUCED_BY") == [{"input": "authorization"}]
    assert _kinds(graph, snapshot, features, "PRODUCED_BY") == [{"input": "code_snapshot.cwd"}]
    run_auth = _artifact("authorization", "auth-r")
    assert _kinds(graph, "run:bad", run_auth, "PRODUCED_BY") == [{"input": "basis"}]
    assert _kinds(graph, run_auth, "run:good", "CONSUMED_BY") == [{}]
    assert [
        e.entity_id for e in graph.entities.values() if e.broken_reason and "auth" in e.key
    ] == []


def test_a_run_basis_that_cannot_be_followed_adds_no_edge(world):
    data = world.roots.data
    attach_authorization(world, "good", "auth-r", basis="run:bad")
    odd = ArtifactSpec(
        kind="authorization",
        id="auth-odd",
        params={"consumer": "train", "basis": "run:no-such-run"},
    )
    with ArtifactWriter.create(odd, data_root=data) as art:
        art.write_text("note.txt", "an authorization whose run is not in the graph")
        art.commit()
    garbled = odd.model_copy(update={"id": "auth-garbled", "params": {"basis": "run:a:b/c"}})
    with ArtifactWriter.create(garbled, data_root=data) as art:
        art.write_text("note.txt", "an authorization whose basis is not a basis")
        art.commit()
    bare = odd.model_copy(update={"id": "auth-bare", "params": {}})
    with ArtifactWriter.create(bare, data_root=data) as art:
        art.write_text("note.txt", "an authorization without a basis")
        art.commit()

    graph = _build(world)

    produced = {
        e.target_id
        for e in graph.edges.values()
        if e.edge_type == "PRODUCED_BY" and e.attributes == {"input": "basis"}
    }
    assert produced == {_artifact("authorization", "auth-r")}
    for name in ("auth-odd", "auth-garbled", "auth-bare"):
        assert _artifact("authorization", name) in graph.entities


def test_a_kernel_submission_consumes_its_notebook_snapshot(world):
    paths = world.pair.test_paths
    profile = PlatformProfile(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        submission_kind="kernel",
        created_at=STAMP,
    )
    dump_yaml_model(profile, paths.submit_yaml)
    notebook = attach_snapshot(world, "good", body=b"NOTEBOOK = 1")
    stage(
        StageSpec(
            dataset=TEST,
            submission_id="K1",
            eval_run="good",
            kernel="u/nb",
            version=3,
            weights=["good"],
            code=notebook,
            data_root=world.roots.data,
            configs_root=world.roots.configs,
        )
    )
    snapshot = _artifact("code_snapshot", notebook)

    graph = _build(world)

    assert _kinds(graph, snapshot, f"submission:{TEST}/K1", "CONSUMED_BY") == [{}]
    assert _kinds(graph, snapshot, f"submission:{TEST}/S1", "CONSUMED_BY") == []  # a file one

    shutil.rmtree(artifact_dir(world.roots.data, "code_snapshot", notebook))
    gone = _build(world)  # a snapshot that is gone adds no edge and breaks nothing
    assert _kinds(gone, snapshot, f"submission:{TEST}/K1", "CONSUMED_BY") == []
    assert gone.entities[f"submission:{TEST}/K1"].broken_reason is None


# -- the index -------------------------------------------------------------------------------------


def test_rebuild_twice_gives_one_graph_hash(world):
    _trace_run(world)
    attach_authorization(world, "good", "auth-r", basis="run:bad")
    make_fusion(world)

    first, second = _build(world), _build(world)
    assert graph_hash(first) == graph_hash(second)
    assert first.normalized() == second.normalized()

    index = ProvenanceIndex(provenance_index_path(world.roots.data, world.roots.configs))
    one = index.rebuild(world.roots.data, world.roots.configs)
    two = index.rebuild(world.roots.data, world.roots.configs)
    assert one.graph_hash == two.graph_hash == graph_hash(first)
    assert index.load_graph().normalized() == first.normalized()  # code_grade survives the store
    verified = index.verify(world.roots.data, world.roots.configs)
    assert verified.ok is True, verified.issues


def test_an_index_from_before_code_grade_is_refused_by_sync_until_it_is_rebuilt(world):
    """Spec §8: ``code_grade`` is a changed run entity to an index built by 0.17, so ``sync`` stops
    on it and one ``provenance rebuild`` per checkout brings the index forward."""
    _trace_run(world)
    index = ProvenanceIndex(provenance_index_path(world.roots.data, world.roots.configs))
    index.rebuild(world.roots.data, world.roots.configs)
    with closing(sqlite3.connect(index.path)) as connection:  # a 0.17 index's run `good`
        (raw,) = connection.execute(
            "SELECT attributes_json FROM entities WHERE entity_id='run:good'"
        ).fetchone()
        old = json.loads(raw)
        assert old.pop("code_grade") == "traced"
        connection.execute(
            "UPDATE entities SET attributes_json=? WHERE entity_id='run:good'",
            (json.dumps(old, sort_keys=True, separators=(",", ":")),),
        )
        connection.commit()

    with pytest.raises(IntegrityError, match="canonical_drift: entity run:good changed"):
        index.sync(world.roots.data, world.roots.configs)

    index.rebuild(world.roots.data, world.roots.configs)
    index.sync(world.roots.data, world.roots.configs)  # now it has the attribute and syncs
    assert index.verify(world.roots.data, world.roots.configs).ok is True
