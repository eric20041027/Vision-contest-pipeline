from __future__ import annotations

from helpers import det_with_runs, make_card
from vcp.artifact.schema import ArtifactSpec
from vcp.artifact.writer import ArtifactWriter
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.access.schema import AccessRef
from vcp.data.dataset import Dataset
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.data.source_audit import write_source_audit
from vcp.fuse.build import write_record
from vcp.fuse.schema import FuseRecord, MemberRecord
from vcp.measure.ledger import JUDGEMENTS_LEDGER, READINGS_LEDGER, append_row
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.measure.runs import load_run, save_run
from vcp.measure.schema import Judgement
from vcp.provenance.diff import DatasetDiffSpec, create_dataset_diff
from vcp.provenance.graph import ProvenanceGraph, build_graph, dataset_version_id, entity_id
from vcp.provenance.schema import (
    ChangeDomain,
    ChangeType,
    EntityStatus,
    ProvenanceEntity,
    SampleChange,
    SemanticEffect,
    make_change_id,
)
from vcp.provenance.views import compute_statuses, explain, impact


def _new_version(roots, old: Dataset, name: str, samples):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    card = make_card(old.card.task, name=name, categories=old.card.categories, image_root="raw/new")
    ds = Dataset.from_parts(card, samples)
    ds.save(paths)
    write_source_audit(paths, ds.card, data_root=roots.data)
    return ds


def _measured_graph(roots, tmp_path, *, change_subset: str, change: str):
    old, plan, _ = det_with_runs(roots, tmp_path, n=60)
    measured = measure_run(
        MeasureSpec(
            run_id="perfect",
            subsets=["valA", "valB"],
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    sample_id = next(sid for sid, subset in plan.assignment.items() if subset == change_subset)
    samples = [sample.model_copy(deep=True) for sample in old.samples]
    index = next(i for i, sample in enumerate(samples) if sample.sample_id == sample_id)
    if change == "group":
        samples[index] = samples[index].model_copy(update={"group": "reassigned"})
    elif change == "views":
        views = [view.model_copy(deep=True) for view in samples[index].views]
        views[0] = views[0].model_copy(update={"path": "changed/image.png"})
        samples[index] = samples[index].model_copy(update={"views": views})
    else:
        labels = samples[index].labels.model_copy(deep=True)
        labels.boxes[0].x += 0.25
        samples[index] = samples[index].model_copy(update={"labels": labels})
    new = _new_version(roots, old, "tiny-v2", samples)
    create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="tiny",
            to_dataset="tiny-v2",
            artifact_id="tiny-evolution",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    graph = build_graph(roots.data, roots.configs)
    return old, new, measured, sample_id, graph


def test_graph_ids_and_canonical_edges_are_deterministic(roots, tmp_path):
    old, new, measured, _, graph = _measured_graph(
        roots, tmp_path, change_subset="train", change="group"
    )
    old_id = dataset_version_id("tiny", old.card.samples_hash)
    new_id = dataset_version_id("tiny-v2", new.card.samples_hash)
    run_id = entity_id("run", "perfect")
    reading_id = entity_id("reading", measured.readings[0].reading_id)

    assert old_id in graph.entities
    assert new_id in graph.entities
    assert run_id in graph.entities
    assert reading_id in graph.entities
    assert any(
        edge.source_id == old_id and edge.target_id == new_id and edge.edge_type == "DERIVED_FROM"
        for edge in graph.edges.values()
    )
    assert any(e.source_id == old_id and e.target_id == run_id for e in graph.edges.values())
    assert any(e.source_id == run_id and e.target_id == reading_id for e in graph.edges.values())
    assert graph.normalized() == build_graph(roots.data, roots.configs).normalized()
    changed_samples = [
        entity for entity in graph.entities.values() if entity.entity_type == "sample"
    ]
    assert changed_samples
    assert any(edge.edge_type == "CONTAINS_CHANGE" for edge in graph.edges.values())


def test_group_change_stales_plan_run_reading_and_downstream_impact(roots, tmp_path):
    old, new, measured, sample_id, graph = _measured_graph(
        roots, tmp_path, change_subset="train", change="group"
    )
    head = dataset_version_id("tiny-v2", new.card.samples_hash)
    statuses = compute_statuses(graph, head)

    assert statuses[entity_id("run", "perfect")].status == EntityStatus.STALE
    for reading in measured.readings:
        assert statuses[entity_id("reading", reading.reading_id)].status == EntityStatus.STALE
    result = impact(graph, dataset_version_id("tiny", old.card.samples_hash), sample_id, head)
    assert entity_id("run", "perfect") in result.entity_ids
    assert result.statuses[entity_id("run", "perfect")].status == EntityStatus.STALE


def test_evaluation_only_label_change_stales_matching_reading_not_weights(roots, tmp_path):
    _, new, measured, _, graph = _measured_graph(
        roots, tmp_path, change_subset="valA", change="labels"
    )
    statuses = compute_statuses(graph, dataset_version_id("tiny-v2", new.card.samples_hash))

    assert statuses[entity_id("run", "perfect")].status == EntityStatus.VALID
    by_subset = {reading.subset: reading for reading in measured.readings}
    assert statuses[entity_id("reading", by_subset["valA"].reading_id)].status == EntityStatus.STALE
    assert statuses[entity_id("reading", by_subset["valB"].reading_id)].status == EntityStatus.VALID

    path = explain(graph, entity_id("reading", by_subset["valA"].reading_id))
    assert entity_id("run", "perfect") in path.ancestor_ids
    assert any(item.startswith("dataset:") for item in path.ancestor_ids)


def test_evaluation_input_change_stales_only_matching_reading(roots, tmp_path):
    _, new, measured, _, graph = _measured_graph(
        roots, tmp_path, change_subset="valA", change="views"
    )
    statuses = compute_statuses(graph, dataset_version_id("tiny-v2", new.card.samples_hash))
    by_subset = {reading.subset: reading for reading in measured.readings}

    assert statuses[entity_id("run", "perfect")].status == EntityStatus.VALID
    assert statuses[entity_id("reading", by_subset["valA"].reading_id)].status == (
        EntityStatus.STALE
    )
    assert statuses[entity_id("reading", by_subset["valB"].reading_id)].status == (
        EntityStatus.VALID
    )


def _change(source: str, target: str, sample: str, effect: SemanticEffect) -> SampleChange:
    before = source[-64:]
    after = target[-64:]
    return SampleChange(
        change_id=make_change_id(before, after, sample, ChangeType.MODIFIED, "1" * 64, "2" * 64),
        from_dataset="d",
        from_samples_hash=before,
        to_dataset="d",
        to_samples_hash=after,
        sample_id=sample,
        change_type=ChangeType.MODIFIED,
        changed_domains=[ChangeDomain.GROUP],
        changed_fields=["group"],
        semantic_effects=[effect],
        before_row_hash="1" * 64,
        after_row_hash="2" * 64,
    )


def test_branch_merge_unions_changes_from_every_path():
    graph = ProvenanceGraph()
    ids = {
        name: dataset_version_id("d", char * 64) for name, char in zip("abch", "abch", strict=True)
    }
    for ident in ids.values():
        graph.add_entity(
            ProvenanceEntity(
                entity_id=ident,
                entity_type="dataset",
                key=ident.removeprefix("dataset:"),
                dataset_version_id=ident,
            )
        )
    plan = entity_id("split", "base")
    run = entity_id("run", "base")
    graph.add_entity(
        ProvenanceEntity(
            entity_id=plan,
            entity_type="split",
            key="base",
            dataset_version_id=ids["a"],
            attributes={"plan_id": "p", "assignment": {"x": "train"}},
        )
    )
    graph.add_entity(
        ProvenanceEntity(
            entity_id=run,
            entity_type="run",
            key="base",
            dataset_version_id=ids["a"],
            attributes={"plan_id": "p", "trained_on": ["train"], "predicted_subsets": []},
        )
    )
    graph.add_edge(ids["a"], plan, "USES_SPLIT")
    graph.add_edge(plan, run, "USES_SPLIT")
    first = _change(ids["a"], ids["b"], "x", SemanticEffect.TRAINING_AFFECTING)
    second = _change(ids["a"], ids["c"], "y", SemanticEffect.DISPLAY_ONLY)
    for source, target, changes in (
        (ids["a"], ids["b"], [first]),
        (ids["a"], ids["c"], [second]),
        (ids["b"], ids["h"], []),
        (ids["c"], ids["h"], []),
    ):
        graph.add_edge(source, target, "DERIVED_FROM")
        graph.transitions[(source, target)] = [change.change_id for change in changes]
        graph.changes.update({change.change_id: change for change in changes})

    assert compute_statuses(graph, ids["h"])[run].status == EntityStatus.STALE


def test_broken_dependency_propagates_through_split_run_and_reading():
    graph = ProvenanceGraph()
    dataset = dataset_version_id("d", "a" * 64)
    split = entity_id("split", "p")
    run = entity_id("run", "r")
    reading = entity_id("reading", "m")
    for ident, kind, broken in (
        (dataset, "dataset", "samples hash mismatch"),
        (split, "split", None),
        (run, "run", None),
        (reading, "reading", None),
    ):
        graph.add_entity(
            ProvenanceEntity(
                entity_id=ident,
                entity_type=kind,
                key=ident,
                dataset_version_id=dataset,
                attributes={"plan_id": "p", "run_id": "r", "subset": "val"},
                broken_reason=broken,
            )
        )
    graph.add_edge(dataset, split, "USES_SPLIT")
    graph.add_edge(split, run, "USES_SPLIT")
    graph.add_edge(run, reading, "EVALUATED_BY")

    statuses = compute_statuses(graph, dataset)
    assert all(statuses[ident].status == EntityStatus.BROKEN for ident in graph.entities)


def test_missing_access_receipt_marks_run_broken(roots, tmp_path):
    det_with_runs(roots, tmp_path, n=20)
    card = load_run(roots.data, "perfect")
    card = card.model_copy(
        update={
            "access": [
                AccessRef(
                    artifact_id="missing-receipt",
                    purpose="train",
                    subsets=["train"],
                    sealed_accessed=False,
                    denied=0,
                    receipt_sha256="a" * 64,
                    binding="manual",
                )
            ]
        }
    )
    save_run(roots.data, card)

    graph = build_graph(roots.data, roots.configs)

    assert "missing-receipt" in graph.entities[entity_id("run", "perfect")].broken_reason


def test_missing_fusion_member_marks_fusion_run_broken(roots, tmp_path):
    det_with_runs(roots, tmp_path, n=20)
    write_record(
        roots.data,
        "perfect",
        FuseRecord(
            run_id="perfect",
            recipe_id="missing-member-recipe",
            recipe_sha256="a" * 64,
            method="mean",
            method_version="1",
            params={},
            members=[MemberRecord(run="absent", weight=1.0, trained_on=["train"])],
            vcp_version="0",
        ),
    )

    graph = build_graph(roots.data, roots.configs)
    fusion = graph.entities[entity_id("run", "perfect")]

    assert fusion.entity_type == "fusion_run"
    assert "missing fusion member run:absent" in (fusion.broken_reason or "")


def test_dataset_artifact_becomes_stale_and_appears_in_sample_impact(roots, tmp_path):
    old, new, _, sample_id, _ = _measured_graph(
        roots, tmp_path, change_subset="train", change="views"
    )
    with ArtifactWriter.create(
        ArtifactSpec(
            kind="derived",
            id="old-dataset-output",
            dataset="tiny",
            params={"samples_hash": old.card.samples_hash},
            inputs=[],
        ),
        data_root=roots.data,
    ) as writer:
        writer.write_text("payload.txt", f"built at {stamp()}")
        writer.commit()
    graph = build_graph(roots.data, roots.configs)
    old_id = dataset_version_id("tiny", old.card.samples_hash)
    head = dataset_version_id("tiny-v2", new.card.samples_hash)
    artifact = entity_id("artifact", "derived/old-dataset-output")

    statuses = compute_statuses(graph, head)
    result = impact(graph, old_id, sample_id, head)

    assert graph.entities[artifact].dataset_version_id == old_id
    assert statuses[artifact].status == EntityStatus.STALE
    assert artifact in result.entity_ids
    assert any(graph.entities[ident].entity_type == "sample" for ident in result.entity_ids)


def test_artifact_supersession_is_canonical_lineage(roots):
    for artifact_id, supersedes in (("z-old", None), ("a-new", "z-old")):
        with ArtifactWriter.create(
            ArtifactSpec(
                kind="derived",
                id=artifact_id,
                supersedes=supersedes,
                supersedes_reason="fixed" if supersedes else None,
            ),
            data_root=roots.data,
        ) as writer:
            writer.write_text("payload.txt", artifact_id)
            writer.commit()

    graph = build_graph(roots.data, roots.configs)
    old = entity_id("artifact", "derived/z-old")
    new = entity_id("artifact", "derived/a-new")

    assert any(
        edge.source_id == old and edge.target_id == new and edge.edge_type == "SUPERSEDES"
        for edge in graph.edges.values()
    )


def test_broken_supersession_evidence_does_not_create_lineage_edge(roots):
    for artifact_id, supersedes in (("old", None), ("new", "old")):
        with ArtifactWriter.create(
            ArtifactSpec(
                kind="derived",
                id=artifact_id,
                supersedes=supersedes,
                supersedes_reason="fixed" if supersedes else None,
            ),
            data_root=roots.data,
        ) as writer:
            writer.write_text("payload.txt", artifact_id)
            writer.commit()
    ledger = roots.data / "artifacts" / "derived" / "supersession.jsonl"
    ledger.write_text("", encoding="utf-8", newline="\n")

    graph = build_graph(roots.data, roots.configs)
    old = entity_id("artifact", "derived/old")
    new = entity_id("artifact", "derived/new")

    assert graph.entities[new].broken_reason is not None
    assert not any(
        edge.source_id == old and edge.target_id == new and edge.edge_type == "SUPERSEDES"
        for edge in graph.edges.values()
    )


def test_rejudgement_keeps_distinct_append_only_events(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=20)
    common = dict(
        prereg_id="p1",
        baseline_run="noisy",
        candidate_run="perfect",
        metric="coco_map",
        metric_version="1",
        params={},
        higher_is_better=True,
        per_subset={},
        bases_positive=0,
        sigma_p=None,
        reasons=[],
        reading_ids=[],
        bootstrap={},
    )
    append_row(
        paths.measure_dir / JUDGEMENTS_LEDGER,
        Judgement(ts="2026-09-13T00:00:00.000Z", verdict="FAIL", **common),
    )
    append_row(
        paths.measure_dir / JUDGEMENTS_LEDGER,
        Judgement(ts="2026-09-13T00:01:00.000Z", verdict="PASS", **common),
    )

    graph = build_graph(roots.data, roots.configs)
    events = [entity for entity in graph.entities.values() if entity.entity_type == "judgement"]

    assert len(events) == 2
    assert {entity.attributes["verdict"] for entity in events} == {"FAIL", "PASS"}


def test_export_pins_and_materialized_cache_are_canonical_entities(roots, tmp_path):
    det_with_runs(roots, tmp_path, n=20)
    card = load_run(roots.data, "perfect")
    export_sha = "e" * 64
    save_run(
        roots.data,
        card.model_copy(
            update={"source": card.source.model_copy(update={"export_manifest_sha": export_sha})}
        ),
    )
    materialize(
        MaterializeSpec(
            name="tiny",
            mode="png",
            resize=4,
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )

    graph = build_graph(roots.data, roots.configs)
    export = entity_id("export", export_sha)
    caches = [
        entity for entity in graph.entities.values() if entity.entity_type == "materialized_cache"
    ]

    assert export in graph.entities
    assert any(
        edge.source_id == export
        and edge.target_id == entity_id("run", "perfect")
        and edge.edge_type == "CONSUMED_BY"
        for edge in graph.edges.values()
    )
    assert len(caches) == 1
    assert caches[0].broken_reason is None


def test_reading_identity_mismatch_is_broken_and_not_linked(roots, tmp_path):
    _, _, _ = det_with_runs(roots, tmp_path, n=20)
    measured = measure_run(
        MeasureSpec(
            run_id="perfect",
            subsets=["valA"],
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    bad = measured.readings[0].model_copy(update={"reading_id": "f" * 64, "dataset": "other"})
    append_row(roots.data / "measure" / "tiny" / READINGS_LEDGER, bad)

    graph = build_graph(roots.data, roots.configs)
    ident = entity_id("reading", bad.reading_id)

    assert graph.entities[ident].broken_reason is not None
    assert not any(edge.target_id == ident for edge in graph.edges.values())


def test_sample_impact_excludes_run_on_disjoint_subset():
    graph = ProvenanceGraph()
    old = dataset_version_id("d", "a" * 64)
    head = dataset_version_id("d", "b" * 64)
    split = entity_id("split", "p")
    for ident in (old, head):
        graph.add_entity(
            ProvenanceEntity(
                entity_id=ident,
                entity_type="dataset",
                key=ident,
                dataset_version_id=ident,
            )
        )
    graph.add_entity(
        ProvenanceEntity(
            entity_id=split,
            entity_type="split",
            key="p",
            dataset_version_id=old,
            attributes={"plan_id": "p", "assignment": {"x": "valA"}},
        )
    )
    graph.add_edge(old, split, "USES_SPLIT")
    for suffix, subset in (("a", "valA"), ("b", "valB")):
        run = entity_id("run", suffix)
        reading = entity_id("reading", suffix)
        graph.add_entity(
            ProvenanceEntity(
                entity_id=run,
                entity_type="run",
                key=suffix,
                dataset_version_id=old,
                attributes={
                    "plan_id": "p",
                    "trained_on": [],
                    "predicted_subsets": [subset],
                },
            )
        )
        graph.add_entity(
            ProvenanceEntity(
                entity_id=reading,
                entity_type="reading",
                key=suffix,
                dataset_version_id=old,
                attributes={"run_id": suffix, "plan_id": "p", "subset": subset},
            )
        )
        graph.add_edge(split, run, "USES_SPLIT")
        graph.add_edge(run, reading, "EVALUATED_BY")
    change = _change(old, head, "x", SemanticEffect.EVALUATION_AFFECTING)
    graph.add_edge(old, head, "DERIVED_FROM")
    graph.transitions[(old, head)] = [change.change_id]
    graph.changes[change.change_id] = change

    result = impact(graph, old, "x", head)

    assert entity_id("run", "a") in result.entity_ids
    assert entity_id("reading", "a") in result.entity_ids
    assert entity_id("run", "b") not in result.entity_ids
    assert entity_id("reading", "b") not in result.entity_ids
