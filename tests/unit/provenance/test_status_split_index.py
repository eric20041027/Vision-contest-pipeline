"""Status passes must resolve splits through one index, not one graph scan per run or reading.

Profiled 2026-10-30: on a 20,000-edge graph with 13,194 readings the status work took 25.4 s
and 0.85 s once the split lookup was memoized, with identical statuses on every head.
"""

from __future__ import annotations

from typing import Any

from vcp.core.hashing import sha256_text
from vcp.provenance.graph import ProvenanceGraph, dataset_version_id, entity_id
from vcp.provenance.schema import (
    ChangeDomain,
    ChangeType,
    EntityStatus,
    ProvenanceEntity,
    SampleChange,
    SemanticEffect,
    StatusRecord,
    make_change_id,
)
from vcp.provenance.views import compute_statuses, compute_statuses_for_entities

# A handful of whole-graph passes is fine; one pass per run or reading is the defect.
MAX_ENTITY_SCANS = 6
MANY_READINGS = 2000

DATASET_A = dataset_version_id("d", "a" * 64)
DATASET_B = dataset_version_id("d", "b" * 64)
DATASET_C = dataset_version_id("d", "c" * 64)
DATASET_D = dataset_version_id("d", "d" * 64)
HEADS = {"a": DATASET_A, "b": DATASET_B, "c": DATASET_C, "d": DATASET_D}

# Statuses of every head, computed by the implementation that rescanned the graph per lookup
# (main a4f4b73), as a digest of (entity, status, reason, predecessor) rows.
SMALL_GRAPH_DIGESTS = {
    "a": "8084b2b7513e4c30ddc2f3d9e0679d6d0a6b49c1b6bd897934763ee9dc7870a2",
    "b": "6908133586c9aee19ac33ac8b7683c3c3749effe49973b221882d8f9766ce599",
    "c": "b69249cffacff4e10d8416efc5069ee05b34270685745f446f22dbc4ab4d5945",
    "d": "411e26f49681088cebde75e8558107d8c03fe961795ab2bb8eda70e2c883ea7b",
}
LARGE_GRAPH_DIGESTS = {
    "a": "abc1cef074d0e490f78889a88b08748d29c2cd45c31f2b2f8efdfe6a2952d42c",
    "b": "b65edb8b3942519014599811c5955106c9bd3d63a8d1dd0e98092b5115da977f",
    "c": "b5ae8c176b920404beaf7f92534fcce6ffdaa5405fea58c397cfb06bcbaed222",
    "d": "33053658fb19bf81ba8fa7e7f5b101b26c007c140730334b860326b8319e5e3f",
}


class _CountingEntities(dict):
    """An entity table that counts whole-table iterations."""

    def __init__(self) -> None:
        super().__init__()
        self.scans = 0

    def __iter__(self):
        self.scans += 1
        return super().__iter__()

    def keys(self):
        self.scans += 1
        return super().keys()

    def values(self):
        self.scans += 1
        return super().values()

    def items(self):
        self.scans += 1
        return super().items()


def _entity(
    ident: str,
    kind: str,
    dataset: str | None,
    attributes: dict[str, Any],
    broken_reason: str | None = None,
) -> ProvenanceEntity:
    return ProvenanceEntity(
        entity_id=ident,
        entity_type=kind,
        key=ident.split(":", 1)[1],
        dataset_version_id=dataset,
        attributes=attributes,
        broken_reason=broken_reason,
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


def _add_transition(
    graph: ProvenanceGraph,
    source: str,
    target: str,
    changes: list[SampleChange],
) -> None:
    graph.add_edge(source, target, "DERIVED_FROM")
    graph.transitions[(source, target)] = [change.change_id for change in changes]
    graph.changes.update({change.change_id: change for change in changes})


_TRAIN_ALL = {"s1": "train", "s2": "train", "s3": "train"}
# (key, dataset, attributes). z-p1 and a-p1 tie on (dataset, plan); a-p1 has the lowest id.
# int-plan and list-plan carry non-string plan ids, no-plan carries none.
_SPLITS: list[tuple[str, str, dict[str, Any]]] = [
    ("z-p1", DATASET_A, {"plan_id": "p1", "assignment": {"s1": "val", "s2": "train", "s3": "val"}}),
    (
        "a-p1",
        DATASET_A,
        {"plan_id": "p1", "assignment": {"s1": "train", "s2": "val", "s3": "test"}},
    ),
    ("p2", DATASET_A, {"plan_id": "p2", "assignment": {"s1": "val", "s2": "train", "s3": "train"}}),
    ("int-plan", DATASET_A, {"plan_id": 7, "assignment": _TRAIN_ALL}),
    ("list-plan", DATASET_A, {"plan_id": ["p1"], "assignment": _TRAIN_ALL}),
    ("none-plan", DATASET_A, {"plan_id": "None", "assignment": _TRAIN_ALL}),
    ("no-plan", DATASET_A, {"assignment": _TRAIN_ALL}),
    ("b-p1", DATASET_B, {"plan_id": "p1", "assignment": {"s1": "val", "s2": "val", "s3": "val"}}),
]
# (key, entity type, dataset, plan attributes, broken reason)
_RUNS: list[tuple[str, str, str, dict[str, Any], str | None]] = [
    ("r-p1", "run", DATASET_A, {"plan_id": "p1"}, None),
    ("r-p2", "run", DATASET_A, {"plan_id": "p2"}, None),
    ("r-int", "run", DATASET_A, {"plan_id": 7}, None),
    ("r-intstr", "run", DATASET_A, {"plan_id": "7"}, None),
    ("r-absent", "run", DATASET_A, {}, None),
    ("r-b", "run", DATASET_B, {"plan_id": "p1"}, None),
    ("r-c", "run", DATASET_C, {"plan_id": "p1"}, None),
    ("r-fuse", "fusion_run", DATASET_A, {"plan_id": "p1"}, None),
    ("r-broken", "run", DATASET_A, {"plan_id": "p1"}, "missing prediction"),
]


def _status_graph(readings: int = 0, entities: dict | None = None) -> ProvenanceGraph:
    """Four dataset versions, tied / non-string / absent split plans, and ``readings`` extras."""
    graph = ProvenanceGraph() if entities is None else ProvenanceGraph(entities=entities)
    for ident in HEADS.values():
        graph.add_entity(_entity(ident, "dataset", ident, {}))
    for key, dataset, attributes in _SPLITS:
        split = _entity(entity_id("split", key), "split", dataset, attributes)
        graph.add_entity(split)
        graph.add_edge(dataset, split.entity_id, "USES_SPLIT")
    run_attributes: dict[str, dict[str, Any]] = {}
    for key, kind, dataset, plan, broken in _RUNS:
        attributes = {**plan, "trained_on": ["train"], "predicted_subsets": ["val", "test"]}
        run_attributes[key] = {**plan}
        graph.add_entity(
            _entity(entity_id("run", key), kind, dataset, attributes, broken_reason=broken)
        )
    for member in ("r-p1", "r-p2"):
        graph.add_edge(entity_id("run", member), entity_id("run", "r-fuse"), "COMBINED_INTO")

    reading_specs = [(key, subset) for key, *_ in _RUNS for subset in ("val", "test")]
    reading_specs.append(("ghost", "val"))
    reading_specs.extend(
        (_RUNS[index % len(_RUNS)][0], ("val", "test")[index % 2]) for index in range(readings)
    )
    datasets = {key: dataset for key, _, dataset, _, _ in _RUNS}
    reading_ids: list[str] = []
    for index, (run, subset) in enumerate(reading_specs):
        ident = entity_id("reading", f"{run}-{subset}-{index}")
        plan = run_attributes.get(run, {"plan_id": "p1"})
        graph.add_entity(
            _entity(
                ident,
                "reading",
                datasets.get(run, DATASET_A),
                {**plan, "run_id": run, "subset": subset, "metric": "m"},
            )
        )
        if run in run_attributes:
            graph.add_edge(entity_id("run", run), ident, "EVALUATED_BY", {"subset": subset})
        reading_ids.append(ident)
    judgement = entity_id("judgement", "j1")
    graph.add_entity(_entity(judgement, "judgement", None, {}))
    for ident in reading_ids[:6]:
        graph.add_edge(ident, judgement, "EVALUATED_BY")
    artifact = entity_id("artifact", "derived/x")
    graph.add_entity(_entity(artifact, "artifact", DATASET_A, {"kind": "derived"}))
    graph.add_edge(DATASET_A, artifact, "PRODUCED_BY")

    _add_transition(
        graph,
        DATASET_A,
        DATASET_B,
        [
            _change(DATASET_A, DATASET_B, "s1", SemanticEffect.TRAINING_AFFECTING),
            _change(DATASET_A, DATASET_B, "s2", SemanticEffect.INPUT_AFFECTING),
            _change(DATASET_A, DATASET_B, "s3", SemanticEffect.UNKNOWN),
            _change(DATASET_A, DATASET_B, "s9", SemanticEffect.UNKNOWN),
        ],
    )
    _add_transition(
        graph,
        DATASET_B,
        DATASET_C,
        [
            _change(DATASET_B, DATASET_C, "s2", SemanticEffect.EVALUATION_AFFECTING),
            _change(DATASET_B, DATASET_C, "s3", SemanticEffect.INPUT_AFFECTING),
            _change(DATASET_B, DATASET_C, "s1", SemanticEffect.DISPLAY_ONLY),
        ],
    )
    _add_transition(
        graph,
        DATASET_A,
        DATASET_D,
        [_change(DATASET_A, DATASET_D, "s1", SemanticEffect.SPLIT_AFFECTING)],
    )
    return graph


def _digest(statuses: dict[str, StatusRecord]) -> str:
    rows = "\n".join(
        f"{key}|{record.status.value}|{record.reason}|{record.predecessor_id}"
        for key, record in sorted(statuses.items())
    )
    return sha256_text(rows)


def _entity_scans(readings: int) -> int:
    entities = _CountingEntities()
    graph = _status_graph(readings, entities)
    assert entities.scans == 0
    compute_statuses(graph, DATASET_C)
    return entities.scans


def test_status_pass_scans_entities_a_constant_number_of_times():
    few = _entity_scans(20)
    many = _entity_scans(MANY_READINGS)

    assert many <= MAX_ENTITY_SCANS, (
        f"{many} whole-entity scans for {MANY_READINGS} readings: split lookup rescans the graph"
    )
    assert many == few, "entity scans must not grow with the number of readings"


def test_tied_non_string_and_absent_plans_resolve_as_before():
    statuses = compute_statuses(_status_graph(), DATASET_B)

    # a-p1 beats z-p1 on the tie (lowest entity id), so s1 lands in a trained subset.
    assert statuses["run:r-p1"].reason == (
        "training-affecting change in subset train for sample s1"
    )
    # An integer plan id never matches the stringified lookup: no plan, no subset.
    assert statuses["run:r-int"].status == EntityStatus.REVIEW
    assert statuses["run:r-int"].reason == "unclassified change for sample s3"
    # The string "7" does not match the integer 7 either.
    assert statuses["run:r-intstr"].status == EntityStatus.REVIEW
    # A run with no plan id looks up the string "None", which a split can carry.
    assert statuses["run:r-absent"].status == EntityStatus.STALE


def test_statuses_match_the_per_lookup_scan_implementation_on_every_head():
    graph = _status_graph()

    digests = {name: _digest(compute_statuses(graph, head)) for name, head in HEADS.items()}

    assert digests == SMALL_GRAPH_DIGESTS


def test_statuses_match_the_per_lookup_scan_implementation_with_many_readings():
    graph = _status_graph(MANY_READINGS)

    digests = {name: _digest(compute_statuses(graph, head)) for name, head in HEADS.items()}

    assert digests == LARGE_GRAPH_DIGESTS


def test_dirty_set_recompute_matches_the_full_pass():
    graph = _status_graph(40)
    full = compute_statuses(graph, DATASET_C)
    dirty = {ident for ident, entity in graph.entities.items() if entity.entity_type == "reading"}

    partial = compute_statuses_for_entities(graph, DATASET_C, dirty, base=full)

    assert partial == {ident: full[ident] for ident in sorted(dirty)}
