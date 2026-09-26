import pytest
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.data.evidence_ref import (
    EvidenceRef,
    add_ref,
    add_refs,
    current,
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
    assert ei.value.fields == {"evidence": "teacher"}


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
