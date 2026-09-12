import json

import pytest

from vcp.artifact import store
from vcp.artifact.ledger import (
    SUPERSESSION_LEDGER,
    append_supersession,
    read_supersession,
    supersession_log,
    supersession_of,
)
from vcp.artifact.schema import ArtifactManifest, ArtifactSpec, FileEntry, InputRef, SupersessionRow
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir

SHA = "a" * 64
STAMP = "2026-09-11T00:00:00.000Z"


def _write_by_hand(data_root, kind, artifact_id, *, manifest=True):
    """A committed-looking directory made without the writer (Task 6 adds the writer)."""
    d = artifact_dir(data_root, kind, artifact_id)
    d.mkdir(parents=True)
    spec = ArtifactSpec(kind=kind, id=artifact_id)
    (d / "spec.json").write_text(
        json.dumps({"spec": spec.model_dump(), "opened_at": STAMP, "vcp_version": "t"}),
        encoding="utf-8",
        newline="\n",
    )
    if manifest:
        m = ArtifactManifest(
            spec=spec,
            files=[FileEntry(name="a.txt", bytes=1, sha256=SHA)],
            created_at=STAMP,
            vcp_version="t",
        )
        (d / "manifest.json").write_text(
            m.model_dump_json(indent=1) + "\n", encoding="utf-8", newline="\n"
        )
    return d


def test_ledger_round_trip(roots):
    assert supersession_log(roots.data, "k").name == SUPERSESSION_LEDGER == "supersession.jsonl"
    assert (
        supersession_log(roots.data, "k") == roots.data / "artifacts" / "k" / "supersession.jsonl"
    )
    assert (
        read_supersession(roots.data, "k") == [] and supersession_of(roots.data, "k", "x") is None
    )
    row = SupersessionRow(
        ts=STAMP,
        kind="k",
        id="new",
        manifest_sha256=SHA,
        supersedes_id="old",
        supersedes_sha256="b" * 64,
        reason="r",
    )
    append_supersession(roots.data, row)
    append_supersession(roots.data, row.model_copy(update={"id": "new2"}))
    assert read_supersession(roots.data, "k") == [row, row.model_copy(update={"id": "new2"})]
    assert supersession_of(roots.data, "k", "new2").id == "new2"
    assert supersession_of(roots.data, "k", "old") is None
    assert b"\r" not in supersession_log(roots.data, "k").read_bytes()
    with pytest.raises(ValidationFailed):
        supersession_log(roots.data, "../k")


def test_load_manifest_three_states(roots):
    with pytest.raises(ValidationFailed, match="^not_found: artifact k/none") as ei:
        store.load_manifest(roots.data, "k", "none")
    assert ei.value.fields == {"kind": "k", "id": "none"}
    _write_by_hand(roots.data, "k", "half", manifest=False)
    with pytest.raises(ValidationFailed, match="^partial: artifact k/half") as ei:
        store.load_manifest(roots.data, "k", "half")
    assert ei.value.fields == {"kind": "k", "id": "half"}
    assert store.is_partial(roots.data, "k", "half")
    assert not store.is_partial(roots.data, "k", "none")
    d = _write_by_hand(roots.data, "k", "full")
    m = store.load_manifest(roots.data, "k", "full")
    assert m.spec.id == "full" and m.files[0].name == "a.txt"
    assert not store.is_partial(roots.data, "k", "full")
    assert store.manifest_path(roots.data, "k", "full") == d / "manifest.json"
    (d / "manifest.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="bad manifest") as ei:
        store.load_manifest(roots.data, "k", "full")
    assert ei.value.location == str(d / "manifest.json")


def test_load_manifest_refuses_a_manifest_from_another_id(roots):
    src = _write_by_hand(roots.data, "k", "one")
    other = artifact_dir(roots.data, "k", "two")
    other.mkdir()
    (other / "manifest.json").write_bytes((src / "manifest.json").read_bytes())
    with pytest.raises(IntegrityError, match="^mismatch: .*says it is k/one"):
        store.load_manifest(roots.data, "k", "two")


def test_resolve_inputs_hashes_relativises_and_checks(roots, tmp_path):
    inside = roots.data / "plans" / "plan.json"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"plan")
    outside = tmp_path / "elsewhere.bin"
    outside.write_bytes(b"out")
    spec = ArtifactSpec(
        kind="k",
        id="i",
        inputs=[
            InputRef(name="plan", path=str(inside)),
            InputRef(name="weights", path=str(outside)),
            InputRef(name="code", sha256=SHA),
            InputRef(name="rel", path="plans/plan.json"),
        ],
    )
    r = store.resolve_inputs(spec, roots.data)
    by = {i.name: i for i in r.inputs}
    assert by["plan"].path == "plans/plan.json" and by["plan"].sha256 == sha256_file(inside)
    assert by["rel"].path == "plans/plan.json" and by["rel"].sha256 == sha256_file(inside)
    assert by["weights"].path == outside.resolve().as_posix()
    assert by["weights"].sha256 == sha256_file(outside)
    assert by["code"] == InputRef(name="code", sha256=SHA)
    assert spec.inputs[0].sha256 is None  # the caller's spec is not mutated
    gone = ArtifactSpec(
        kind="k", id="i", inputs=[InputRef(name="gone", path=str(tmp_path / "gone"))]
    )
    with pytest.raises(ValidationFailed, match="^not_found: input 'gone'") as ei:
        store.resolve_inputs(gone, roots.data)
    assert ei.value.fields == {"input": "gone"}
    wrong = ArtifactSpec(
        kind="k", id="i", inputs=[InputRef(name="plan", path=str(inside), sha256=SHA)]
    )
    with pytest.raises(IntegrityError, match="^mismatch: input 'plan'") as ei:
        store.resolve_inputs(wrong, roots.data)
    assert ei.value.fields == {"input": "plan"} and ei.value.location == str(inside)
    right = ArtifactSpec(
        kind="k",
        id="i",
        inputs=[InputRef(name="plan", path=str(inside), sha256=sha256_file(inside))],
    )
    assert store.resolve_inputs(right, roots.data).inputs[0].path == "plans/plan.json"


def test_spec_diff_ignores_notes_and_names_inputs():
    a = ArtifactSpec(
        kind="k",
        id="i",
        seed=1,
        params={"x": "1"},
        inputs=[InputRef(name="p", sha256=SHA)],
        notes="one",
    )
    assert store.spec_diff(a, a.model_copy(update={"notes": "two"})) == []
    b = a.model_copy(
        update={
            "seed": 2,
            "params": {"x": "2"},
            "inputs": [InputRef(name="p", sha256="b" * 64), InputRef(name="q", sha256=SHA)],
        }
    )
    assert store.spec_diff(a, b) == ["seed", "params", "inputs.p", "inputs.q"]
