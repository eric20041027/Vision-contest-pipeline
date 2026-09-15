from __future__ import annotations

from vcp.provenance.policy import classify_rows, register_impact_policy
from vcp.provenance.schema import (
    ChangeDomain,
    ChangeType,
    SemanticEffect,
    make_change_id,
)


def test_change_id_is_deterministic_and_sensitive_to_stable_inputs():
    args = ("a" * 64, "b" * 64, "s1", ChangeType.MODIFIED, "c" * 64, "d" * 64)
    first = make_change_id(*args)
    assert first == make_change_id(*args)
    assert len(first) == 64
    assert first != make_change_id(*args[:-2], "e" * 64, args[-1])


def test_builtin_policy_classifies_recursive_fields_conservatively():
    before = {
        "sample_id": "s1",
        "views": [{"path": "old.png"}],
        "labels": {"targets": {"x": 0.0}},
        "label_source": "derived",
        "group": "g1",
        "meta": {"opaque": {"score": 0.2}},
    }
    after = {
        "sample_id": "s1",
        "views": [{"path": "new.png"}],
        "labels": {"targets": {"x": 1.0}},
        "label_source": "gold",
        "group": "g2",
        "meta": {"opaque": {"score": 0.3}},
    }

    fields, domains, effects = classify_rows(before, after)

    assert fields == [
        "group",
        "label_source",
        "labels.targets.x",
        "meta.opaque.score",
        "views[0].path",
    ]
    assert domains == [
        ChangeDomain.GROUP,
        ChangeDomain.LABELS,
        ChangeDomain.LABEL_SOURCE,
        ChangeDomain.META,
        ChangeDomain.VIEWS,
    ]
    assert effects == [
        SemanticEffect.EVALUATION_AFFECTING,
        SemanticEffect.INPUT_AFFECTING,
        SemanticEffect.SPLIT_AFFECTING,
        SemanticEffect.TRAINING_AFFECTING,
        SemanticEffect.UNKNOWN,
    ]


def test_plugin_policy_can_classify_nested_metadata_without_changing_core():
    name = "test-soft-targets"

    def soft_targets(path, _before, _after):
        if path.startswith("meta.soft_targets."):
            return {SemanticEffect.TRAINING_AFFECTING, SemanticEffect.EVALUATION_AFFECTING}
        return None

    register_impact_policy(name, soft_targets, version="7")
    before = {"sample_id": "s1", "meta": {"soft_targets": {"x": 0.1}}}
    after = {"sample_id": "s1", "meta": {"soft_targets": {"x": 0.9}}}

    _, domains, effects = classify_rows(before, after, policy_names=[name])

    assert domains == [ChangeDomain.META]
    assert effects == [
        SemanticEffect.EVALUATION_AFFECTING,
        SemanticEffect.TRAINING_AFFECTING,
    ]
