"""``authorization`` artifacts (spec 2026-10-31 §4.5, §4.6 left column, §5.6; VCP-006): claims from
an artifact basis, ``authorize`` writes the artifact, ``consume`` -- used by ``ArtifactWriter`` for
its reserved input ``authorization`` -- holds a job against them. A run basis arrives through
``run_claims``, the training layer's part (Task 13); here it is a stub."""

import json
import shutil

import pytest
from pydantic import ValidationError

from vcp.artifact import store
from vcp.artifact.authorization import (
    FILE,
    KIND,
    AuthorizationDoc,
    AuthorizeSpec,
    artifact_claims,
    authorization_input,
    authorize,
    consume,
    in_vocabulary,
    is_superseded,
    load_authorization,
    parse_basis,
)
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_json
from vcp.core.paths import artifact_dir

REASON = "50-sample benchmark passed"
SHA = "ab" * 32


def _authorize(
    bench,
    authorization_id="a1",
    *,
    basis="artifact:benchmark/b-50",
    consumer="artifact",
    target_kind="features",
    free=("param:limit",),
    set_=(),
    reason=REASON,
    supersedes=None,
    run_claims=None,
):
    spec = AuthorizeSpec(
        authorization_id, basis, consumer, target_kind, list(free), list(set_), reason, supersedes
    )
    return authorize(spec, data_root=bench.data, run_claims=run_claims)


def _doc(roots, authorization_id="a1") -> dict:
    path = artifact_dir(roots.data, KIND, authorization_id) / FILE
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest(roots, kind, artifact_id):
    return store.load_manifest(roots.data, kind, artifact_id)


def _run_job(roots, spec):
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        art.write_bytes("features.bin", b"f")
        return art.commit()


def _supersede(roots, kind, old, new):
    spec = ArtifactSpec(kind=kind, id=new, supersedes=old, supersedes_reason="measured again")
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        art.write_text("note.txt", "again")
        art.commit()


def _unauthorized(roots, spec) -> ValidationFailed:
    with pytest.raises(ValidationFailed, match="^unauthorized:") as ei:
        ArtifactWriter.create(spec, data_root=roots.data)
    assert not artifact_dir(roots.data, spec.kind, spec.id).exists()
    return ei.value


def _run_card(roots, run_id="r1"):
    card = roots.data / "runs" / run_id / "run.yaml"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_bytes(b"run_id: r1\n")
    return card


# -- the vocabulary, the basis text, the document ---------------------------------------------


@pytest.mark.parametrize(
    ("name", "consumer", "ok"),
    [
        ("dataset", "artifact", True),
        ("plan_id", "artifact", True),
        ("seed", "artifact", True),
        ("param:batch", "artifact", True),
        ("param:", "artifact", False),
        ("input:ids", "artifact", True),
        ("input:", "artifact", False),
        ("input:authorization", "artifact", False),
        ("env:python", "artifact", False),
        ("batch", "artifact", False),
        ("seed", "train", True),
        ("dataset", "train", True),
        ("param:batch", "train", False),
        ("param:trained_on", "train", True),
        ("param:samples_hash", "train", True),
        ("param:config_sha256", "train", True),
        ("param:command_sha256", "train", True),
        ("input:plan", "train", True),
        ("input:ids", "train", False),
        ("input:code_snapshot.cwd", "train", True),
        ("input:code_snapshot.", "train", False),
        ("input:evidence.cfg", "train", True),
        ("input:labels.pseudo-v1", "train", True),
        ("env:python", "train", True),
        ("env:packages", "train", True),
        ("env:gpus", "train", True),
        ("env:other", "train", False),
    ],
)
def test_the_vocabulary_of_each_consumer(name, consumer, ok):
    assert in_vocabulary(name, consumer) is ok


def test_parse_basis():
    assert parse_basis("artifact:benchmark/b-50") == ("artifact", "benchmark", "b-50")
    assert parse_basis("run:r1") == ("run", "r1", "")
    for bad in ("", "r1", "run:", "artifact:", "artifact:benchmark", "artifact:a/b/c", "run:a b"):
        with pytest.raises(ValidationFailed, match="^invalid:"):
            parse_basis(bad)


def _doc_fields(**over) -> dict:
    claims = {"dataset": "demo", "param:batch": "8"}
    fields = {
        "consumer": "artifact",
        "target_kind": "features",
        "basis": "artifact:benchmark/b-50",
        "claims": claims,
        "claims_sha256": sha256_json(claims),
        "freed": [],
        "reason": REASON,
    }
    return {**fields, **over}


def test_the_document_checks_its_own_shape():
    assert AuthorizationDoc(**_doc_fields()).schema_version == 1
    unsorted_claims = {"param:batch": "8", "dataset": "demo"}
    bad = [
        _doc_fields(claims={}, claims_sha256=sha256_json({})),
        _doc_fields(claims=unsorted_claims, claims_sha256=sha256_json(unsorted_claims)),
        _doc_fields(claims_sha256=SHA),
        _doc_fields(target_kind=None),
        _doc_fields(consumer="train"),
        _doc_fields(schema_version=2),
    ]
    for fields in bad:
        with pytest.raises(ValidationError):
            AuthorizationDoc(**fields)
    train = AuthorizationDoc(**_doc_fields(consumer="train", target_kind=None))
    assert train.target_kind is None


def test_artifact_claims_read_the_resolved_spec():
    spec = ArtifactSpec(
        kind="features",
        id="f1",
        dataset="demo",
        plan_id="p1",
        seed=3,
        params={"batch": "8"},
        inputs=[
            InputRef(name="ids", sha256="1" * 64),
            InputRef(name="authorization", sha256="2" * 64),
            InputRef(name="code_snapshot.cwd", sha256="3" * 64),
        ],
    )
    claims = artifact_claims(spec)
    assert claims == {
        "dataset": "demo",
        "input:code_snapshot.cwd": "3" * 64,
        "input:ids": "1" * 64,
        "param:batch": "8",
        "plan_id": "p1",
        "seed": "3",
    }
    assert list(claims) == sorted(claims)
    assert artifact_claims(ArtifactSpec(kind="features", id="f2")) == {}
    with pytest.raises(ValueError, match="never resolved"):
        artifact_claims(ArtifactSpec(kind="x", id="x", inputs=[InputRef(name="ids", path="p")]))


# -- authorize ---------------------------------------------------------------------------------


def test_an_artifact_basis_gives_these_claims(roots, bench):
    res = _authorize(bench)
    assert res.claims == {
        "dataset": "demo",
        "input:code_snapshot.cwd": bench.sha("code_snapshot.cwd"),
        "input:ids": bench.sha("ids"),
        "input:model": bench.sha("model"),
        "input:source_audit": bench.sha("source_audit"),
        "param:batch": "8",
    }
    assert res.freed == ["param:limit"] and (res.authorization_id, res.consumer) == (
        "a1",
        "artifact",
    )
    assert res.basis == "artifact:benchmark/b-50"
    doc = _doc(roots)
    assert doc["claims"] == res.claims and list(doc["claims"]) == sorted(doc["claims"])
    assert doc["claims_sha256"] == sha256_json(doc["claims"]) and doc["target_kind"] == "features"
    assert (doc["consumer"], doc["basis"], doc["freed"]) == ("artifact", res.basis, ["param:limit"])
    assert doc["reason"] == REASON and doc["schema_version"] == 1
    m = _manifest(roots, KIND, "a1")
    assert m.spec.params == {
        "consumer": "artifact",
        "basis": "artifact:benchmark/b-50",
        "target_kind": "features",
    }
    assert [(i.name, i.sha256) for i in m.spec.inputs] == [("basis", sha256_file(bench.manifest))]
    assert (m.spec.dataset, m.spec.plan_id, m.spec.supersedes) == ("demo", None, None)
    assert res.manifest_sha256 == sha256_file(store.manifest_path(roots.data, KIND, "a1"))
    assert load_authorization(roots.data, "a1") == AuthorizationDoc(**doc)


def test_set_and_at_path_override_a_claim(roots, bench, tmp_path):
    new_model = tmp_path / "new.bin"
    new_model.write_bytes(b"retrained")
    res = _authorize(
        bench, set_=["param:batch=16", f"input:model=@{new_model}", "seed=7", "param:note="]
    )
    assert res.claims["param:batch"] == "16" and res.claims["seed"] == "7"
    assert res.claims["input:model"] == sha256_file(new_model) != bench.sha("model")
    assert res.claims["param:note"] == "" and res.claims["input:ids"] == bench.sha("ids")
    assert list(res.claims) == sorted(res.claims)
    assert _doc(roots)["claims"] == res.claims


@pytest.mark.parametrize(
    ("kwargs", "word"),
    [
        ({"consumer": "train", "target_kind": None, "free": ()}, "^unbindable:"),
        ({"free": ["param:nope"]}, "^invalid:"),
        (
            {
                "free": [
                    "dataset",
                    "input:code_snapshot.cwd",
                    "input:ids",
                    "input:model",
                    "input:source_audit",
                    "param:batch",
                    "param:limit",
                ]
            },
            "^invalid:",
        ),
        ({"consumer": "weights"}, "^invalid:"),
        ({"target_kind": None}, "^invalid:"),
        ({"consumer": "train", "target_kind": "features"}, "^invalid:"),
        ({"target_kind": "bad kind"}, "^invalid:"),
        ({"reason": "  "}, "^invalid:"),
        ({"basis": "benchmark/b-50"}, "^invalid:"),
        ({"set_": ["batch"]}, "^invalid:"),
        ({"set_": ["=8"]}, "^invalid:"),
        ({"set_": ["param:batch=@"]}, "^invalid:"),
        ({"set_": ["param:batch=1", "param:batch=2"]}, "^invalid:"),
        ({"set_": ["param:limit=5"]}, "^invalid:"),  # freed and set: say which one you mean
        ({"set_": ["env:python=cp313"]}, "^unbindable:"),
        ({"set_": ["input:model=@no-such-file"]}, "^not_found:"),
        ({"basis": "artifact:benchmark/nope"}, "^not_found:"),
        ({"basis": "run:r1"}, "^not_found:"),  # no run.yaml
    ],
)
def test_unbindable_unknown_free_and_empty(roots, bench, kwargs, word):
    with pytest.raises(ValidationFailed, match=word):
        _authorize(bench, **kwargs)
    assert not artifact_dir(roots.data, KIND, "a1").exists()


def test_unbindable_lists_the_names_and_points_at_free(roots, bench):
    with pytest.raises(ValidationFailed, match="^unbindable:") as ei:
        _authorize(bench, consumer="train", target_kind=None, free=())
    text = str(ei.value)
    for name in ("param:batch", "input:ids", "input:model", "input:source_audit"):
        assert name in text
    assert "dataset" not in text.split("cannot be bound")[0] and "--free" in text
    # freeing exactly those makes the train authorization bind the rest
    res = _authorize(
        bench,
        consumer="train",
        target_kind=None,
        free=["param:batch", "param:limit", "input:ids", "input:model", "input:source_audit"],
    )
    assert set(res.claims) == {"dataset", "input:code_snapshot.cwd"}
    assert "target_kind" not in _manifest(roots, KIND, "a1").spec.params
    assert _doc(roots)["target_kind"] is None


def test_a_superseded_basis_is_refused(roots, bench):
    _supersede(roots, "benchmark", "b-50", "b-51")
    with pytest.raises(ValidationFailed, match="^superseded:.*b-51"):
        _authorize(bench)
    assert not artifact_dir(roots.data, KIND, "a1").exists()


def test_a_broken_basis_is_refused(roots, bench):
    (bench.manifest.parent / "receipt.json").write_bytes(b"{}")
    with pytest.raises(IntegrityError, match="^mismatch:"):
        _authorize(bench)
    assert not artifact_dir(roots.data, KIND, "a1").exists()


def test_a_run_basis_takes_its_claims_from_the_training_layer(roots, bench):
    claims = {
        "dataset": "demo",
        "plan_id": "p1",
        "seed": "1",
        "env:python": "cp313",
        "input:plan": SHA,
    }
    asked = []

    def run_claims(run_id):
        asked.append(run_id)
        return dict(claims)

    with pytest.raises(ValidationFailed, match="^not_found:"):  # no run.yaml
        _authorize(
            bench,
            basis="run:r1",
            consumer="train",
            target_kind=None,
            free=(),
            run_claims=run_claims,
        )
    assert asked == []
    _run_card(roots)
    with pytest.raises(ValidationFailed, match="^invalid:"):  # the training layer's part is missing
        _authorize(bench, basis="run:r1", consumer="train", target_kind=None, free=())
    res = _authorize(
        bench,
        basis="run:r1",
        consumer="train",
        target_kind=None,
        free=["seed"],
        run_claims=run_claims,
    )
    assert asked == ["r1"]
    assert res.claims == {k: v for k, v in claims.items() if k != "seed"} and res.freed == ["seed"]
    m = _manifest(roots, KIND, "a1")
    assert m.spec.params == {"consumer": "train", "basis": "run:r1"}
    assert m.spec.inputs == [] and (m.spec.dataset, m.spec.plan_id) == ("demo", "p1")
    with pytest.raises(ValidationFailed, match="^unbindable:"):  # a run's claims are train's
        _authorize(bench, "a2", basis="run:r1", free=(), run_claims=run_claims)


def test_an_authorization_can_supersede_another_and_ids_are_claimed_once(roots, bench):
    _authorize(bench)
    with pytest.raises(ValidationFailed, match="^exists: artifact authorization/a1"):
        _authorize(bench)
    _authorize(bench, "a2", supersedes="a1", reason="batch stays 8")
    m = _manifest(roots, KIND, "a2")
    assert (m.spec.supersedes, m.spec.supersedes_reason) == ("a1", "batch stays 8")
    assert is_superseded(roots.data, KIND, "a1") and not is_superseded(roots.data, KIND, "a2")
    assert not is_superseded(roots.data, "benchmark", "b-50")


def test_authorization_input_is_the_manifest(roots, bench):
    _authorize(bench)
    ref = authorization_input(roots.data, "a1")
    assert ref.name == "authorization" and ref.sha256 is None
    assert ref.path == str(store.manifest_path(roots.data, KIND, "a1"))
    with pytest.raises(ValidationFailed, match="^not_found:"):
        authorization_input(roots.data, "nope")


# -- consume, through the writer ---------------------------------------------------------------


def test_a_benchmark_authorizes_the_full_job(roots, bench):
    """VCP-006: benchmark -> authorize -> the real job runs and pins the authorization."""
    _authorize(bench)
    manifest = _run_job(roots, bench.spec("f-all", authorization="a1"))
    pinned = {i.name: i for i in manifest.spec.inputs}["authorization"]
    assert pinned.path == "artifacts/authorization/a1/manifest.json"
    assert pinned.sha256 == sha256_file(store.manifest_path(roots.data, KIND, "a1"))
    assert manifest.spec.params == {"batch": "8", "limit": "all"}  # limit was freed


@pytest.mark.parametrize(
    "changed",
    ["param:batch", "input:code_snapshot.cwd", "input:ids", "input:source_audit", "input:model"],
)
def test_any_bound_input_that_changes_is_unauthorized(roots, bench, changed):
    _authorize(bench)
    batch = "16" if changed == "param:batch" else "8"
    change = changed.removeprefix("input:") if changed.startswith("input:") else None
    err = _unauthorized(roots, bench.spec("f-all", batch=batch, change=change, authorization="a1"))
    assert err.fields == {"authorization": "a1", "differs": changed}
    assert "authorized " in str(err) and changed in str(err)


def test_every_difference_is_named(roots, bench):
    _authorize(bench)
    err = _unauthorized(roots, bench.spec("f-all", batch="16", change="model", authorization="a1"))
    assert err.fields == {"authorization": "a1", "differs": "input:model,param:batch"}
    assert "authorized 8, got 16" in str(err)


def test_a_claim_the_job_lacks_is_a_difference(roots, bench):
    _authorize(bench)
    spec = bench.spec("f-all", authorization="a1")
    without_ids = spec.model_copy(update={"inputs": [i for i in spec.inputs if i.name != "ids"]})
    err = _unauthorized(roots, without_ids)
    assert err.fields["differs"] == "input:ids" and ", got none" in str(err)


def test_what_the_job_adds_is_not_bound(roots, bench):
    _authorize(bench)
    spec = bench.spec("f-all", authorization="a1").model_copy(update={"seed": 9})
    spec = spec.model_copy(update={"params": {**spec.params, "extra": "1"}})
    assert _run_job(roots, spec).spec.id == "f-all"


def test_the_target_kind_must_match(roots, bench):
    _authorize(bench)
    err = _unauthorized(roots, bench.spec("t1", kind="tiles", authorization="a1"))
    assert err.fields == {"authorization": "a1", "differs": "target_kind"}


def test_the_consumer_must_match(roots, bench):
    _authorize(bench)
    claims = dict(_doc(roots)["claims"])
    with pytest.raises(ValidationFailed, match="^unauthorized:") as ei:
        consume(roots.data, "a1", consumer="train", target_kind=None, claims=claims)
    assert ei.value.fields == {"authorization": "a1", "differs": "consumer"}
    doc = consume(roots.data, "a1", consumer="artifact", target_kind="features", claims=claims)
    assert doc.claims == claims


def test_a_superseded_authorization_is_refused(roots, bench):
    _authorize(bench)
    _authorize(bench, "a2", supersedes="a1")
    with pytest.raises(ValidationFailed, match="^superseded: authorization a1.*a2"):
        ArtifactWriter.create(bench.spec("f-all", authorization="a1"), data_root=roots.data)
    assert not artifact_dir(roots.data, "features", "f-all").exists()
    assert _run_job(roots, bench.spec("f-all", authorization="a2")).spec.id == "f-all"


def test_one_authorization_serves_two_jobs(roots, bench):
    _authorize(bench)
    first = _run_job(roots, bench.spec("f-1", authorization="a1"))
    second = _run_job(roots, bench.spec("f-2", authorization="a1"))
    pins = [{i.name: i.sha256 for i in m.spec.inputs}["authorization"] for m in (first, second)]
    assert pins[0] == pins[1] == sha256_file(store.manifest_path(roots.data, KIND, "a1"))


def test_commit_asks_again(roots, bench):
    """The authorization was fine at open; it is superseded before the job commits."""
    _authorize(bench)
    writer = ArtifactWriter.create(bench.spec("f-all", authorization="a1"), data_root=roots.data)
    writer.write_bytes("features.bin", b"f")
    _authorize(bench, "a2", supersedes="a1")
    with pytest.raises(ValidationFailed, match="^superseded:"):
        writer.commit()
    assert not (writer.dir / "manifest.json").exists()


@pytest.mark.parametrize("input_path", ["elsewhere", "benchmark", "sha_only", "nested"])
def test_a_wrong_authorization_input_leaves_no_directory(roots, bench, tmp_path, input_path):
    _authorize(bench)
    if input_path == "elsewhere":  # the right shape, but not in this data root
        copy = tmp_path / "other" / "artifacts" / KIND / "a1" / "manifest.json"
        copy.parent.mkdir(parents=True)
        copy.write_bytes(store.manifest_path(roots.data, KIND, "a1").read_bytes())
        ref = InputRef(name="authorization", path=str(copy))
    elif input_path == "benchmark":  # an artifact, but not an authorization
        ref = InputRef(name="authorization", path=str(bench.manifest))
    elif input_path == "nested":  # a file of the authorization, not its manifest
        ref = InputRef(name="authorization", path=str(artifact_dir(roots.data, KIND, "a1") / FILE))
    else:
        ref = InputRef(name="authorization", sha256=sha256_file(bench.manifest))
    spec = ArtifactSpec(kind="features", id="f-all", inputs=[ref])
    with pytest.raises(ValidationFailed, match="^not_found:") as ei:
        ArtifactWriter.create(spec, data_root=roots.data)
    assert ei.value.fields["input"] == "authorization"
    assert not (roots.data / "artifacts" / "features").exists()


def test_a_missing_authorization_is_not_found(roots, bench):
    with pytest.raises(ValidationFailed, match="^not_found:"):
        consume(roots.data, "nope", consumer="artifact", target_kind="features", claims={})
    ref = InputRef(
        name="authorization",
        path=str(artifact_dir(roots.data, KIND, "nope") / "manifest.json"),
    )
    with pytest.raises(ValidationFailed, match="^not_found:"):
        ArtifactWriter.create(
            ArtifactSpec(kind="features", id="f1", inputs=[ref]), data_root=roots.data
        )


def test_a_broken_authorization_is_superseded(roots, bench):
    _authorize(bench)
    path = artifact_dir(roots.data, KIND, "a1") / FILE
    path.write_bytes(path.read_bytes().replace(b'"8"', b'"9"'))
    with pytest.raises(ValidationFailed, match="^superseded: authorization a1 no longer matches"):
        ArtifactWriter.create(bench.spec("f-all", authorization="a1"), data_root=roots.data)


def test_something_that_is_not_an_authorization_is_invalid(roots, bench):
    spec = ArtifactSpec(kind=KIND, id="odd", params={"consumer": "artifact"})
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        art.write_json(FILE, {"hello": "world"})
        art.commit()
    with pytest.raises(ValidationFailed, match="^invalid: artifact authorization/odd"):
        consume(roots.data, "odd", consumer="artifact", target_kind="features", claims={})
    spec = ArtifactSpec(kind=KIND, id="empty")
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        art.write_text("note.txt", "no authorization.json here")
        art.commit()
    with pytest.raises(ValidationFailed, match="^invalid: artifact authorization/empty"):
        load_authorization(roots.data, "empty")


# -- the basis may stop holding after the authorization was made ------------------------------


def test_a_basis_that_stops_holding_makes_the_authorization_superseded(roots, bench):
    _authorize(bench)
    spec = bench.spec("f-all", authorization="a1")
    receipt = bench.manifest.parent / "receipt.json"
    original = receipt.read_bytes()
    receipt.write_bytes(b"{}")
    with pytest.raises(ValidationFailed, match="^superseded:.*basis benchmark/b-50.*no longer"):
        ArtifactWriter.create(spec, data_root=roots.data)
    receipt.write_bytes(original)
    # the manifest itself rewritten: the sha pinned in the authorization no longer matches
    manifest = bench.manifest.read_bytes()
    bench.manifest.write_bytes(manifest.replace(b'"created_at": "', b'"created_at": "0', 1))
    with pytest.raises(ValidationFailed, match="^superseded:.*basis benchmark/b-50.*no longer"):
        ArtifactWriter.create(spec, data_root=roots.data)
    bench.manifest.write_bytes(manifest)
    _supersede(roots, "benchmark", "b-50", "b-51")
    with pytest.raises(ValidationFailed, match="^superseded:.*basis benchmark/b-50.*b-51"):
        ArtifactWriter.create(spec, data_root=roots.data)
    assert not artifact_dir(roots.data, "features", "f-all").exists()


def test_a_basis_that_is_gone_makes_the_authorization_superseded(roots, bench):
    _authorize(bench)
    shutil.rmtree(bench.manifest.parent)
    with pytest.raises(ValidationFailed, match="^superseded:.*basis benchmark/b-50.*no longer"):
        ArtifactWriter.create(bench.spec("f-all", authorization="a1"), data_root=roots.data)


def test_a_run_basis_holds_while_its_run_card_is_there(roots, bench):
    claims = {"dataset": "demo", "seed": "1", "env:python": "cp313", "input:plan": SHA}
    card = _run_card(roots)
    _authorize(
        bench,
        basis="run:r1",
        consumer="train",
        target_kind=None,
        free=(),
        run_claims=lambda _: claims,
    )
    now = {**claims, "param:samples_hash": "x" * 64}  # what the run adds is not bound
    doc = consume(roots.data, "a1", consumer="train", target_kind=None, claims=now)
    assert doc.basis == "run:r1" and doc.target_kind is None
    with pytest.raises(ValidationFailed, match="^unauthorized:") as ei:
        consume(roots.data, "a1", consumer="train", target_kind=None, claims={**now, "seed": "2"})
    assert ei.value.fields == {"authorization": "a1", "differs": "seed"}
    card.unlink()
    with pytest.raises(ValidationFailed, match="^superseded:.*basis run r1.*no longer"):
        consume(roots.data, "a1", consumer="train", target_kind=None, claims=now)


# -- an error keeps the command's identity: no sibling's word or fields leak in ---------------


def _copy(roots, kind, artifact_id, name):
    """A copy of an artifact directory, as a file manager leaves it (``a1 - Copy``)."""
    base = roots.data / "artifacts" / kind
    shutil.copytree(base / artifact_id, base / name)


@pytest.mark.parametrize("copy_name", ["a1 - Copy", "a1-copy"])
def test_an_unreadable_neighbour_of_the_authorization_is_named_and_leaks_nothing(
    roots, bench, copy_name
):
    """``a1 - Copy`` is no valid name; ``a1-copy`` is one whose manifest says it is ``a1``
    (``mismatch:`` with ``kind`` / ``id`` fields). Neither may speak for the command."""
    _authorize(bench)
    _copy(roots, KIND, "a1", copy_name)
    with pytest.raises(ValidationFailed) as ei:
        consume(roots.data, "a1", consumer="artifact", target_kind="features", claims={})
    text = str(ei.value)
    assert text.startswith(
        "superseded: authorization a1 cannot be checked: authorization holds an unreadable "
        "artifact ("
    )
    assert text.endswith("); move it aside") and ei.value.fields == {}
    with pytest.raises(ValidationFailed, match="^superseded: authorization a1 cannot be checked"):
        ArtifactWriter.create(bench.spec("f-all", authorization="a1"), data_root=roots.data)


@pytest.mark.parametrize("copy_name", ["b-50 - Copy", "b-50-copy"])
def test_an_unreadable_neighbour_of_the_basis_is_named_and_leaks_nothing(roots, bench, copy_name):
    _authorize(bench)
    _copy(roots, "benchmark", "b-50", copy_name)
    with pytest.raises(ValidationFailed) as ei:
        consume(roots.data, "a1", consumer="artifact", target_kind="features", claims={})
    text = str(ei.value)
    assert text.startswith(
        "superseded: authorization a1 cannot be checked: benchmark holds an unreadable artifact ("
    )
    assert text.endswith("); move it aside") and ei.value.fields == {}
    # authorizing against the basis asks the same question, and answers it the same way
    with pytest.raises(ValidationFailed) as ei:
        _authorize(bench, "a2")
    assert str(ei.value).startswith("superseded: basis artifact benchmark/b-50 cannot be checked")
    assert "benchmark holds an unreadable artifact (" in str(ei.value) and ei.value.fields == {}
    assert not artifact_dir(roots.data, KIND, "a2").exists()


def test_is_superseded_names_an_unreadable_neighbour_too(roots, bench):
    _authorize(bench)
    _copy(roots, KIND, "a1", "a1 - Copy")
    with pytest.raises(ValidationFailed, match="^superseded: .*authorization holds an unreadable"):
        is_superseded(roots.data, KIND, "a1")


def test_a_missing_basis_keeps_the_authorization_id_and_names_the_basis(roots, bench):
    """``load_manifest``'s ``kind`` / ``id`` are the basis's: left on the error they would turn
    ``id=`` of ``artifact authorize`` into the basis's id."""
    with pytest.raises(ValidationFailed, match="^not_found:") as ei:
        _authorize(bench, basis="artifact:benchmark/nope")
    assert ei.value.fields == {"basis": "artifact:benchmark/nope"}
    (roots.data / "artifacts" / "benchmark" / "half").mkdir()
    with pytest.raises(ValidationFailed, match="^partial:") as ei:
        _authorize(bench, basis="artifact:benchmark/half")
    assert ei.value.fields == {"basis": "artifact:benchmark/half"}


def test_consuming_a_missing_authorization_names_it_and_not_the_job(roots, bench):
    with pytest.raises(ValidationFailed, match="^not_found:") as ei:
        consume(roots.data, "nope", consumer="artifact", target_kind="features", claims={})
    assert ei.value.fields == {"authorization": "nope"}
    (roots.data / "artifacts" / KIND / "half").mkdir(parents=True)
    with pytest.raises(ValidationFailed, match="^partial:") as ei:
        consume(roots.data, "half", consumer="artifact", target_kind="features", claims={})
    assert ei.value.fields == {"authorization": "half"}
