"""A hand-built provenance graph shaped like one small contest, for the drawing tests.

Built through ``ProvenanceGraph`` directly (no files): every entity type the canonical scan emits
appears once, so each fold rule of ``vcp.provenance.render`` has something to act on.
"""

from __future__ import annotations

from vcp.provenance.graph import ProvenanceGraph
from vcp.provenance.schema import EntityStatus, ProvenanceEntity, StatusRecord

DS = "dataset:tiny@aaaa1111bbbb"
DS2 = "dataset:tiny-v2@cccc2222dddd"
SPLIT = "split:tiny/fixed-v1@aaaa1111bbbb"
PERFECT, NOISY, FUSED = "run:perfect", "run:noisy", "run:fused"  # a fusion keeps its run: id
JUDGE = "judgement:tiny/admit-perfect@e1"
RECEIPT = "artifact:access_receipt/perfect-a1-0"
AUDIT = "artifact:source_audit/src-tiny"
EXPORT = "export:eeeeeeeeeeee"
CACHE = "materialized_cache:tiny/npy@m1"
BACKUP = "backup:tiny/m-final"
NOTE_A, NOTE_B = "artifact:contest-note/a", "artifact:contest-note/b"
SUB = "submission:tiny-test/s1"
DIFF = "artifact:dataset_diff/d1"
SAMPLE = "sample:tiny-v2@cccc2222dddd/s-7@f00d"


def add_entity(graph, ident, *, entity_type=None, dsv=None, broken=None, **attributes):
    prefix, key = ident.split(":", 1)
    graph.add_entity(
        ProvenanceEntity(
            entity_id=ident,
            entity_type=entity_type or prefix,
            key=key,
            dataset_version_id=dsv,
            attributes=attributes,
            broken_reason=broken,
        )
    )


def status(ident, value: EntityStatus, reason: str = "test") -> StatusRecord:
    return StatusRecord(entity_id=ident, status=value, reason=reason)


def contest_graph() -> ProvenanceGraph:
    """One dataset with a split, two runs, a fusion, readings, a judgement, a submission, every
    folded kind (receipt, audit, export, cache, backup, diff, sample) and one contest kind."""
    g = ProvenanceGraph()
    add_entity(g, DS, dsv=DS, dataset="tiny", samples_hash="aaaa1111bbbb", sample_count=1200)
    add_entity(g, DS2, dsv=DS2, dataset="tiny-v2", samples_hash="cccc2222dddd", sample_count=1201)
    add_entity(g, SPLIT, dsv=DS, dataset="tiny", plan_id="fixed-v1", roles={"train": "train"})
    add_entity(g, PERFECT, dsv=DS, dataset="tiny", provenance_grade="receipt")
    add_entity(g, NOISY, dsv=DS, dataset="tiny", provenance_grade="export")
    add_entity(
        g,
        FUSED,
        entity_type="fusion_run",
        dsv=DS,
        dataset="tiny",
        provenance_grade="declared",
        recipe_id="mean-v1",
    )
    readings = (("r1", "perfect", "valA"), ("r2", "perfect", "valB"), ("r3", "noisy", "valA"))
    for rid, run, subset in (*readings, ("r4", "fused", "valA")):
        add_entity(g, f"reading:{rid}", dsv=DS, run_id=run, subset=subset, metric="map50")
    add_entity(g, JUDGE, prereg_id="admit-perfect", verdict="PASS")
    add_entity(g, RECEIPT, dsv=DS, kind="access_receipt", id="perfect-a1-0")
    add_entity(g, AUDIT, dsv=DS, kind="source_audit", id="src-tiny")
    add_entity(g, EXPORT, dsv=DS, manifest_sha256="e" * 12)
    add_entity(g, CACHE, dsv=DS, dataset="tiny", mode="npy", rows=1200)
    add_entity(g, BACKUP, conclusion="submission:s1")
    add_entity(g, NOTE_A, kind="contest-note", id="a")
    add_entity(g, NOTE_B, kind="contest-note", id="b", broken="artifact verification failed")
    add_entity(g, SUB, dataset="tiny-test", kind="candidate")
    add_entity(g, DIFF, kind="dataset_diff", id="d1")
    add_entity(g, SAMPLE, dsv=DS2, sample_id="s-7", row_sha256="f00d")
    edges = [
        (DS, SPLIT, "USES_SPLIT", {}),
        (SPLIT, PERFECT, "USES_SPLIT", {}),
        (SPLIT, NOISY, "USES_SPLIT", {}),
        (SPLIT, FUSED, "USES_SPLIT", {}),
        (DS, PERFECT, "CONSUMED_BY", {}),
        (DS, NOISY, "CONSUMED_BY", {}),
        (DS, FUSED, "CONSUMED_BY", {}),
        (PERFECT, "reading:r1", "EVALUATED_BY", {"subset": "valA"}),
        (PERFECT, "reading:r2", "EVALUATED_BY", {"subset": "valB"}),
        (NOISY, "reading:r3", "EVALUATED_BY", {"subset": "valA"}),
        (FUSED, "reading:r4", "EVALUATED_BY", {"subset": "valA"}),
        ("reading:r1", JUDGE, "EVALUATED_BY", {}),
        ("reading:r2", JUDGE, "EVALUATED_BY", {}),
        ("reading:r3", JUDGE, "EVALUATED_BY", {}),
        (PERFECT, FUSED, "COMBINED_INTO", {}),
        (NOISY, FUSED, "COMBINED_INTO", {}),
        (DS, AUDIT, "PRODUCED_BY", {"input": "dataset"}),
        (DS, RECEIPT, "PRODUCED_BY", {"input": "dataset"}),
        (AUDIT, RECEIPT, "PRODUCED_BY", {"input": "source_audit"}),
        (RECEIPT, PERFECT, "CONSUMED_BY", {}),
        (DS, EXPORT, "PRODUCED_BY", {}),
        (EXPORT, NOISY, "CONSUMED_BY", {}),
        (DS, CACHE, "PRODUCED_BY", {}),
        (PERFECT, BACKUP, "BACKED_UP_AS", {}),
        (RECEIPT, BACKUP, "BACKED_UP_AS", {}),
        (PERFECT, NOTE_A, "PRODUCED_BY", {"input": "run"}),
        (NOTE_A, NOTE_B, "PRODUCED_BY", {"input": "a"}),
        (FUSED, SUB, "SUBMITTED_AS", {}),
        (DS, DS2, "DERIVED_FROM", {"artifact": "d1", "total_changes": 3}),
        (DS, DIFF, "PRODUCED_BY", {"input": "dataset"}),
        (DS2, DIFF, "PRODUCED_BY", {"input": "dataset"}),
        (DIFF, SAMPLE, "CONTAINS_CHANGE", {"change_id": "c1", "side": "after"}),
    ]
    for source, target, edge_type, attrs in edges:
        g.add_edge(source, target, edge_type, attrs)
    return g
