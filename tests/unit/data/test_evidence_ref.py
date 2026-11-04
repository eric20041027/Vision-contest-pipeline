import pytest
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.data.evidence import check_evidence_file
from vcp.data.evidence_ref import (
    AUTHORIZATION_NAME,
    AUTHORIZATION_ROLE,
    CODE_ROLE,
    CODE_TRACE_ROLE,
    LABELS_ROLE,
    ORIGINAL_KINDS,
    RESERVED_NAME_PREFIXES,
    RESERVED_ROLES,
    EvidenceRef,
    add_ref,
    add_refs,
    current,
    evidence_count,
    is_reserved_name,
    labels_field,
    merge_refs,
)
from vcp.measure.schema import RunCard, RunSource
from vcp.train.schema import EVENTS, TrainRecord

STAMP = "2026-09-26T00:00:00.000Z"


def _ref(name="teacher", sha="a", *, kind="evidence", role=None, artifact_id=None):
    return EvidenceRef(
        name=name,
        role=role or ("labels" if kind == "label_set" else name),
        kind=kind,
        artifact_id=artifact_id or f"r1-{name}-{sha * 12}",
        manifest_sha256=sha * 64,
        attempt=1,
        attached_at=STAMP,
        binding="cli",
    )


def test_the_same_name_and_bytes_twice_is_one_row():
    refs = add_ref([], _ref())
    assert add_ref(refs, _ref()) == refs


def test_new_bytes_under_a_name_become_its_current_row():
    refs = add_refs([], [_ref(sha="a"), _ref(sha="b")])
    assert [r.manifest_sha256[0] for r in refs] == ["a", "b"]
    assert [r.manifest_sha256[0] for r in current(refs)] == ["b"]


def test_a_name_cannot_change_its_role_or_kind():
    refs = add_ref([], _ref())
    with pytest.raises(ValidationFailed, match="evidence_conflict") as ei:
        add_ref(refs, _ref(role="corpus", sha="b"))
    assert ei.value.fields == {"evidence_name": "teacher"}


def test_merge_keeps_the_first_list_then_what_it_lacks():
    a, b, c = _ref("a"), _ref("b"), _ref("c")
    assert merge_refs([a, b], [b, c]) == [a, b, c]


def test_labels_field_names_the_current_label_sets():
    assert labels_field([]) == "dataset"
    assert labels_field([_ref()]) == "dataset"
    refs = [_ref(), _ref("pseudo-v1", kind="label_set", artifact_id="pseudo-v1")]
    assert labels_field(refs) == "pseudo-v1"


def test_names_and_roles_must_be_path_safe():
    with pytest.raises(ValidationError):
        _ref(name="bad name")


def _card(**over):
    base = dict(
        run_id="r1",
        dataset="tiny",
        samples_hash="f" * 64,
        plan_id="fixed-v1",
        trained_on=["train"],
        source=RunSource(),
        created_at=STAMP,
    )
    return RunCard(**{**base, **over})


def _record(**over):
    base = dict(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="work",
        command=["python"],
    )
    return TrainRecord(**{**base, **over})


@pytest.mark.parametrize("make", [_card, _record])
def test_an_empty_evidence_list_is_not_written(make):
    """An older vcp (extra="forbid") still reads a card or record that attached nothing."""
    assert "evidence" not in make().model_dump(mode="json")
    assert '"evidence"' not in make().model_dump_json()
    full = make(evidence=[_ref()])
    assert full.model_dump(mode="json")["evidence"][0]["name"] == "teacher"
    assert type(full).model_validate(full.model_dump(mode="json")) == full


def test_evidence_is_a_training_event():
    assert "evidence" in EVENTS


def test_the_reserved_vocabulary():
    assert (CODE_ROLE, CODE_TRACE_ROLE, AUTHORIZATION_ROLE) == (
        "code",
        "code_trace",
        "authorization",
    )
    assert RESERVED_ROLES == (LABELS_ROLE, "code", "code_trace", "authorization")
    assert AUTHORIZATION_NAME == "authorization"
    assert RESERVED_NAME_PREFIXES == ("code.", "code_trace.")
    assert ORIGINAL_KINDS == ("evidence", "label_set")


@pytest.mark.parametrize(
    ("kind", "name", "role"),
    [
        ("code_snapshot", "code.cwd", "code"),
        ("code_trace", "code_trace.a1.1", "code_trace"),
        ("authorization", "authorization", "authorization"),
    ],
)
def test_the_new_kinds_load(kind, name, role):
    ref = _ref(name, kind=kind, role=role)
    assert type(ref).model_validate(ref.model_dump(mode="json")) == ref
    assert ref.kind == kind


def test_an_unknown_kind_is_still_refused():
    with pytest.raises(ValidationError):
        _ref(kind="elsewhere")


@pytest.mark.parametrize(
    ("name", "reserved"),
    [
        ("code.cwd", True),
        ("code.dist-vcp", True),
        ("code_trace.a1.1", True),
        ("authorization", True),
        ("teacher", False),
        ("code", False),  # the role is reserved; the bare name is not a prefix match
        ("codec.v1", False),
        ("xcode.cwd", False),
        ("authorization2", False),
        ("my.authorization", False),
    ],
)
def test_reserved_names(name, reserved):
    assert is_reserved_name(name) is reserved


@pytest.mark.parametrize(
    ("name", "role"),
    [
        ("code.cwd", "code.cwd"),
        ("code_trace.a1.1", "x"),
        ("authorization", "authorization"),
        ("x", "code"),
        ("x", "code_trace"),
        ("x", "authorization"),
    ],
)
def test_reserved_names_and_roles_are_refused(tmp_path, name, role):
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="^role_reserved:") as ei:
        check_evidence_file(name, f, role)
    assert ei.value.fields == {"evidence_name": name}


def test_the_labels_role_keeps_its_message(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationFailed) as ei:
        check_evidence_file("x", f, "labels")
    assert str(ei.value) == (
        "role_reserved: role 'labels' is for label sets; attach them with --labels "
        "or Session.attach_labels"
    )


def test_an_ordinary_name_and_role_still_pass(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    check_evidence_file("code", f, "corpus")  # the bare word is not reserved as a name
    check_evidence_file("teacher", f, "teacher")


def test_evidence_and_labels_count_only_the_original_kinds():
    refs = [
        _ref("teacher"),
        _ref("pseudo-v1", kind="label_set", artifact_id="pseudo-v1"),
        _ref("code.cwd", kind="code_snapshot", role="code"),
        _ref("code_trace.a1.1", kind="code_trace", role="code_trace"),
        _ref("authorization", kind="authorization", role="authorization"),
    ]
    assert evidence_count(refs) == 2
    assert labels_field(refs) == "pseudo-v1"
    assert evidence_count([]) == 0


def test_evidence_count_counts_names_not_rows():
    refs = add_refs(
        [], [_ref(sha="a"), _ref(sha="b"), _ref("code.cwd", kind="code_snapshot", role="code")]
    )
    assert len(refs) == 3
    assert evidence_count(refs) == 1
