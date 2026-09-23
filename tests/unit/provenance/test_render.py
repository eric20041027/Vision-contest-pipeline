from __future__ import annotations

import pytest

from graph_fixtures import (
    AUDIT,
    BACKUP,
    CACHE,
    DIFF,
    DS,
    DS2,
    EXPORT,
    FUSED,
    JUDGE,
    NOISY,
    NOTE_A,
    NOTE_B,
    PERFECT,
    RECEIPT,
    SAMPLE,
    SPLIT,
    SUB,
    add_entity,
    contest_graph,
    status,
)
from vcp.core.errors import ValidationFailed
from vcp.provenance.render import (
    build_view,
    fallback_statuses,
    node_id,
    scope_entities,
    worst_statuses,
)
from vcp.provenance.schema import EntityStatus


def _nodes(view):
    return {node.node_id: node for node in view.nodes}


def _edge(view, source, target):
    matches = [e for e in view.edges if (e.source, e.target) == (node_id(source), node_id(target))]
    assert len(matches) <= 1
    return matches[0] if matches else None


def test_overview_keeps_the_contest_skeleton_and_folds_the_rest():
    view = build_view(contest_graph(), {})

    kinds = sorted(node.kind for node in view.nodes)
    assert kinds == sorted(
        [
            "dataset",
            "dataset",
            "split",
            "run",
            "run",
            "fusion_run",
            "judgement",
            "submission",
            "artifact_kind",
        ]
    )
    drawn = {member for node in view.nodes for member in node.members}
    for folded in (RECEIPT, AUDIT, EXPORT, CACHE, BACKUP, DIFF, SAMPLE, "reading:r1"):
        assert folded not in drawn
    assert (view.scope, view.detail, view.entities) == ("all", "overview", 21)
    assert view.folded == 21 - 8  # eight entities are drawn as their own node


def test_readings_fold_into_run_to_judgement_edges_labelled_by_subset():
    view = build_view(contest_graph(), {})

    assert _edge(view, PERFECT, JUDGE).label == "valA, valB"
    assert _edge(view, NOISY, JUDGE).label == "valA"
    assert _edge(view, FUSED, JUDGE) is None  # its reading was never judged


def test_dataset_to_run_edges_are_dropped_when_a_split_already_carries_them():
    view = build_view(contest_graph(), {})

    for run in (PERFECT, NOISY, FUSED):
        assert _edge(view, DS, run) is None
        assert _edge(view, SPLIT, run) is not None
    assert _edge(view, DS, SPLIT) is not None
    assert len(view.edges) == 11


def test_dataset_evolution_edge_names_the_diff_and_its_change_count():
    edge = _edge(build_view(contest_graph(), {}), DS, DS2)

    assert edge.label == "diff d1: 3 changes"
    assert edge.thick is True


def test_contest_artifact_kinds_collapse_to_one_node_with_counts():
    view = build_view(contest_graph(), {})
    group = _nodes(view)[node_id("kind:contest-note")]

    assert group.label == ("contest-note x2", "1 BROKEN")
    assert group.members == (NOTE_A, NOTE_B)
    assert group.status is EntityStatus.BROKEN
    assert group.reason == "artifact verification failed"
    assert _edge(view, PERFECT, "kind:contest-note") is not None
    assert all(edge.source != edge.target for edge in view.edges)


def test_node_status_comes_from_the_status_map_and_hidden_problems_are_reported():
    statuses = {
        PERFECT: status(PERFECT, EntityStatus.STALE, "training-affecting change"),
        CACHE: status(CACHE, EntityStatus.BROKEN, "materialized output mismatch"),
    }
    view = build_view(contest_graph(), statuses)
    node = _nodes(view)[node_id(PERFECT)]

    assert (node.status, node.reason) == (EntityStatus.STALE, "training-affecting change")
    assert view.counts == {"VALID": 18, "REVIEW": 0, "STALE": 1, "BROKEN": 2}
    assert [record.entity_id for record in view.hidden_alerts] == [CACHE]


def test_labels_carry_what_a_reader_needs():
    nodes = _nodes(build_view(contest_graph(), {}))

    assert nodes[node_id(DS)].label == ("tiny", "@aaaa1111 - 1,200 samples")
    assert nodes[node_id(SPLIT)].label == ("fixed-v1", "train")
    assert nodes[node_id(PERFECT)].label == ("perfect", "grade receipt")
    assert nodes[node_id(FUSED)].label == ("fused", "fusion mean-v1")
    assert nodes[node_id(JUDGE)].label == ("admit-perfect", "PASS")
    assert nodes[node_id(SUB)].label == ("s1", "candidate")  # framed by tiny-test


def test_nodes_are_framed_by_their_dataset_name():
    nodes = _nodes(build_view(contest_graph(), {}))

    assert nodes[node_id(DS)].group == "tiny"
    assert nodes[node_id(PERFECT)].group == "tiny"
    assert nodes[node_id(JUDGE)].group == "tiny"  # the measure directory it was judged in
    assert nodes[node_id(SUB)].group == "tiny-test"
    assert nodes[node_id(DS2)].group == "tiny-v2"
    assert nodes[node_id("kind:contest-note")].group is None


def test_backed_up_marks_only_nodes_a_backup_manifest_covers():
    nodes = _nodes(build_view(contest_graph(), {}))

    assert nodes[node_id(PERFECT)].backed_up is True
    assert nodes[node_id(NOISY)].backed_up is False


def test_full_detail_draws_every_entity_except_samples():
    view = build_view(contest_graph(), {}, detail="full")
    drawn = {member for node in view.nodes for member in node.members}

    assert len(view.nodes) == 20
    assert SAMPLE not in drawn
    assert {RECEIPT, BACKUP, "reading:r4", DIFF} <= drawn
    assert _edge(view, DS, PERFECT) is not None  # nothing is pruned in full detail
    assert _edge(view, PERFECT, "reading:r1").label == "valA"
    assert len(view.edges) == 31  # every canonical edge except the one into the sample
    assert view.folded == 1


def test_unknown_entity_types_are_drawn_rather_than_hidden():
    graph = contest_graph()
    add_entity(graph, "checkpoint:perfect/final", dsv=DS, dataset="tiny")
    graph.add_edge(PERFECT, "checkpoint:perfect/final", "PRODUCED_BY")

    nodes = _nodes(build_view(graph, {}))

    assert nodes[node_id("checkpoint:perfect/final")].label == ("checkpoint", "perfect/final")


def test_scope_by_dataset_takes_its_versions_ancestors_and_descendants():
    label, selected = scope_entities(contest_graph(), dataset="tiny-v2")

    assert label == "dataset:tiny-v2"
    assert selected == {DS2, DS, DIFF, SAMPLE}
    assert scope_entities(contest_graph(), dataset=DS2)[1] == selected


def test_scope_by_entity_takes_its_ancestors_and_descendants():
    label, selected = scope_entities(contest_graph(), entity=NOISY)

    assert label == f"entity:{NOISY}"
    assert selected == {NOISY, SPLIT, DS, EXPORT, "reading:r3", JUDGE, FUSED, "reading:r4", SUB}


def test_scoped_view_only_draws_selected_entities():
    view = build_view(contest_graph(), {}, entity=NOISY)

    assert view.scope == f"entity:{NOISY}"
    assert view.entities == 9
    assert node_id(PERFECT) not in _nodes(view)
    assert _edge(view, NOISY, JUDGE).label == "valA"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"dataset": "missing"}, "not_found"),
        ({"entity": "run:missing"}, "not_found"),
        ({"dataset": "tiny", "entity": PERFECT}, "scope_conflict"),
    ],
)
def test_scope_errors_are_validation_failures(kwargs, reason):
    with pytest.raises(ValidationFailed, match=reason):
        scope_entities(contest_graph(), **kwargs)


def test_unknown_detail_is_rejected():
    with pytest.raises(ValidationFailed, match="unsupported_detail"):
        build_view(contest_graph(), {}, detail="everything")


def test_worst_statuses_keeps_the_most_severe_record_per_entity():
    first = {
        PERFECT: status(PERFECT, EntityStatus.REVIEW, "first review"),
        NOISY: status(NOISY, EntityStatus.STALE, "first stale"),
    }
    second = {
        PERFECT: status(PERFECT, EntityStatus.BROKEN, "second broken"),
        NOISY: status(NOISY, EntityStatus.STALE, "second stale"),
    }

    merged = worst_statuses([first, second])

    assert merged[PERFECT].reason == "second broken"
    assert merged[NOISY].reason == "first stale"  # ties keep the earlier head


def test_node_ids_are_stable_across_scopes_and_distinct_per_entity():
    whole = _nodes(build_view(contest_graph(), {}))
    scoped = _nodes(build_view(contest_graph(), {}, entity=NOISY))

    assert node_id(NOISY) in whole and node_id(NOISY) in scoped
    assert node_id(NOISY) != node_id(PERFECT)
    assert node_id(NOISY).startswith("n")


def test_every_label_is_ascii_because_json_reaches_a_code_page_console():
    view = build_view(contest_graph(), {}, detail="full")

    assert all(line.isascii() for node in view.nodes for line in node.label)
    assert all(edge.label.isascii() for edge in view.edges)


def test_a_broken_dataset_without_attributes_is_still_framed_and_labelled():
    graph = contest_graph()
    add_entity(graph, "dataset:lost@unknown", broken="dataset.yaml unreadable")

    node = _nodes(build_view(graph, {}))[node_id("dataset:lost@unknown")]

    assert (node.group, node.label, node.status) == (
        "lost",
        ("lost", "@unknown"),
        EntityStatus.BROKEN,
    )


def test_edges_between_merged_kinds_carry_their_count():
    graph = contest_graph()
    graph.add_edge(NOISY, NOTE_A, "PRODUCED_BY", {"input": "run"})
    graph.add_edge(NOISY, NOTE_B, "PRODUCED_BY", {"input": "run"})

    edge = _edge(build_view(graph, {}), NOISY, "kind:contest-note")

    assert (edge.label, edge.count) == ("x2", 2)


def test_edge_counts_are_distinct_entity_pairs_not_paths():
    graph = contest_graph()
    graph.add_edge(PERFECT, NOTE_A, "PRODUCED_BY", {"input": "weights"})  # a second input edge
    graph.add_edge(DS, NOTE_A, "PRODUCED_BY", {"input": "dataset"})
    graph.add_edge(AUDIT, NOTE_A, "PRODUCED_BY", {"input": "source_audit"})  # DS again, via audit

    view = build_view(graph, {})

    assert _edge(view, PERFECT, "kind:contest-note").count == 1
    assert _edge(view, PERFECT, "kind:contest-note").label == ""
    assert _edge(view, DS, "kind:contest-note").count == 1
    assert _edge(view, PERFECT, JUDGE).count == 1  # two readings, one run -> judgement pair


def test_without_any_dataset_head_broken_evidence_still_poisons_its_dependents():
    graph = contest_graph()
    graph.replace_entity(graph.entities[NOISY].model_copy(update={"broken_reason": "no run.yaml"}))

    statuses = fallback_statuses(graph)

    assert statuses[NOISY].status is EntityStatus.BROKEN
    assert statuses[FUSED].status is EntityStatus.BROKEN
    assert statuses[FUSED].reason == f"upstream {NOISY} is BROKEN"
    assert statuses[SUB].status is EntityStatus.BROKEN
    assert statuses[PERFECT].status is EntityStatus.VALID
