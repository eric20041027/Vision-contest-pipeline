import pytest

from helpers import make_label_set, seed_tiny
from vcp.artifact import store
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir
from vcp.data.evidence import (
    RunScope,
    attach_evidence,
    broken_refs,
    check_evidence_file,
    label_ref,
    parse_evidence_args,
    ref_intact,
    source_sha,
)


def _scope(ds, **over):
    base = dict(
        run_id="r1",
        dataset="tiny",
        samples_hash=ds.card.samples_hash,
        plan_id="fixed-v1",
        trained_on=("train",),
    )
    return RunScope(**{**base, **over})


def test_an_evidence_file_becomes_an_immutable_artifact_once(roots, tmp_path):
    ds, _ = seed_tiny(roots)
    f = tmp_path / "teacher.jsonl"
    f.write_text("t1", encoding="utf-8")
    ref = attach_evidence(
        roots.data, _scope(ds), "teacher", f, role="teacher", attempt=1, binding="cli"
    )
    digest = sha256_file(f)
    assert ref.artifact_id == f"r1-teacher-{digest[:12]}" and ref.kind == "evidence"
    assert (ref.role, ref.attempt, ref.binding) == ("teacher", 1, "cli")
    d = artifact_dir(roots.data, "evidence", ref.artifact_id)
    assert (d / "teacher.jsonl").read_text(encoding="utf-8") == "t1"
    assert ref.manifest_sha256 == sha256_file(
        store.manifest_path(roots.data, "evidence", ref.artifact_id)
    )
    assert source_sha(roots.data, ref) == digest
    again = attach_evidence(
        roots.data, _scope(ds), "teacher", f, role="teacher", attempt=2, binding="cli"
    )
    assert again.artifact_id == ref.artifact_id and again.manifest_sha256 == ref.manifest_sha256
    f.write_text("t2", encoding="utf-8")
    moved = attach_evidence(
        roots.data, _scope(ds), "teacher", f, role="teacher", attempt=2, binding="cli"
    )
    assert moved.artifact_id != ref.artifact_id


def test_evidence_files_are_checked_before_anything_is_copied(roots, tmp_path):
    f = tmp_path / "x.json"
    f.write_text("{}", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="role_reserved"):
        check_evidence_file("x", f, "labels")
    with pytest.raises(ValidationFailed, match="not_found"):
        check_evidence_file("x", tmp_path / "gone.json", "x")
    with pytest.raises(ValidationFailed, match="not_a_file"):
        check_evidence_file("x", tmp_path, "x")
    with pytest.raises(ValidationFailed, match="invalid name"):
        check_evidence_file("bad name", f, "bad name")


@pytest.mark.parametrize(
    "file_name", ["manifest.json", "MANIFEST.JSON", "Spec.json", ".teacher.jsonl.0123abcd.tmp"]
)
def test_an_evidence_file_the_artifact_layer_would_refuse_is_refused_first(tmp_path, file_name):
    """The copy keeps the source's file name, so a name the writer refuses -- its own files, in
    any case, or a temp name `clean` may remove -- fails here, before a byte is written."""
    f = tmp_path / file_name
    f.write_text('{"derived": true}', encoding="utf-8")
    with pytest.raises(ValidationFailed, match="reserved_name") as ei:
        check_evidence_file("derived", f, "derived")
    assert ei.value.fields == {"evidence_name": "derived"}


def test_parse_evidence_args():
    assert parse_evidence_args(["a=x.json", "b=y/z.csv"])[1][0] == "b"
    with pytest.raises(ValidationFailed, match="^invalid: --evidence expects NAME=PATH") as ei:
        parse_evidence_args(["a"])
    assert ei.value.fields == {"evidence_name": "a"}
    with pytest.raises(ValidationFailed, match="evidence_conflict"):
        parse_evidence_args(["a=x", "a=y"])


def test_a_label_set_must_fit_the_run(roots, tmp_path):
    ds, plan = seed_tiny(roots)
    make_label_set(roots, tmp_path, plan)
    ref = label_ref(roots.data, _scope(ds), "pseudo-v1", attempt=None, binding="manual")
    assert (ref.name, ref.role, ref.kind, ref.artifact_id) == (
        "pseudo-v1",
        "labels",
        "label_set",
        "pseudo-v1",
    )
    for over, what in (
        ({"dataset": "other"}, "dataset"),
        ({"samples_hash": "0" * 64}, "samples_hash"),
        ({"plan_id": "other-v1"}, "plan"),
        ({"trained_on": ("valA",)}, "trained_on"),
    ):
        with pytest.raises(ValidationFailed, match="labels_mismatch") as ei:
            label_ref(roots.data, _scope(ds, **over), "pseudo-v1", attempt=None, binding="manual")
        assert what in str(ei.value)
    with pytest.raises(ValidationFailed, match="not_found"):
        label_ref(roots.data, _scope(ds), "nope", attempt=None, binding="manual")


def test_broken_refs_names_what_no_longer_verifies(roots, tmp_path):
    ds, plan = seed_tiny(roots)
    make_label_set(roots, tmp_path, plan)
    f = tmp_path / "teacher.jsonl"
    f.write_text("t1", encoding="utf-8")
    ev = attach_evidence(
        roots.data, _scope(ds), "teacher", f, role="teacher", attempt=1, binding="cli"
    )
    lab = label_ref(roots.data, _scope(ds), "pseudo-v1", attempt=1, binding="cli")
    assert broken_refs(roots.data, [ev, lab]) == [] and ref_intact(roots.data, ev)
    (artifact_dir(roots.data, "evidence", ev.artifact_id) / "teacher.jsonl").write_text(
        "x", encoding="utf-8"
    )
    stale = lab.model_copy(update={"manifest_sha256": "0" * 64})
    assert broken_refs(roots.data, [ev, stale]) == ["teacher", "pseudo-v1"]
