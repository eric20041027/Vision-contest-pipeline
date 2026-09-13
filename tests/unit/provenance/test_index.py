from __future__ import annotations

import json
import random
import sqlite3

import pytest

from helpers import det_samples, det_with_runs, make_card
from vcp.core.errors import IntegrityError
from vcp.core.paths import DatasetPaths, provenance_index_path
from vcp.data.dataset import Dataset
from vcp.data.source_audit import write_source_audit
from vcp.provenance.diff import DatasetDiffSpec, create_dataset_diff
from vcp.provenance.graph import build_graph
from vcp.provenance.index import ProvenanceIndex


def _dataset(roots, name, samples):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    write_source_audit(paths, ds.card, data_root=roots.data)
    return ds


def _versions(roots):
    old_samples = det_samples(4, seed=21)
    new_samples = [sample.model_copy(deep=True) for sample in old_samples]
    new_samples[0] = new_samples[0].model_copy(update={"group": "new"})
    old = _dataset(roots, "idx-old", old_samples)
    new = _dataset(roots, "idx-new", new_samples)
    return old, new


def _diff(roots, artifact_id="idx-diff"):
    return create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="idx-old",
            to_dataset="idx-new",
            artifact_id=artifact_id,
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )


def test_incremental_ingest_matches_full_graph_and_is_idempotent(roots):
    _versions(roots)
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    _diff(roots)

    first = index.ingest_diff("idx-diff", roots.data, roots.configs)
    second = index.ingest_diff("idx-diff", roots.data, roots.configs)

    assert first.inserted is True
    assert first.dirty_entities >= 2
    assert second.inserted is False
    assert index.load_graph().normalized() == build_graph(roots.data, roots.configs).normalized()
    verified = index.verify(roots.data, roots.configs)
    assert verified.ok is True, verified.issues


def test_interrupted_ingest_rolls_back_to_previous_usable_state(roots):
    _versions(roots)
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    before = index.load_graph().normalized()
    _diff(roots)

    with pytest.raises(RuntimeError, match="injected interruption"):
        index.ingest_diff("idx-diff", roots.data, roots.configs, _fail_after="changes")

    assert index.load_graph().normalized() == before
    assert index.ingest_diff("idx-diff", roots.data, roots.configs).inserted is True


def test_delete_and_rebuild_restores_identical_normalized_results(roots):
    _versions(roots)
    _diff(roots)
    index = ProvenanceIndex(provenance_index_path(roots.data))
    first = index.rebuild(roots.data, roots.configs)
    expected = index.normalized()
    index.path.unlink()

    second = index.rebuild(roots.data, roots.configs)

    assert first.graph_hash == second.graph_hash
    assert index.normalized() == expected


def test_verify_fails_closed_when_consumed_ledger_prefix_changes(roots):
    _versions(roots)
    ledger = roots.configs / "datasets" / "idx-old" / "events.jsonl"
    ledger.write_text(json.dumps({"event": "x"}) + "\n", encoding="utf-8", newline="\n")
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    raw = ledger.read_bytes()
    ledger.write_bytes(b"X" + raw[1:])

    with pytest.raises(IntegrityError, match="prefix_drift"):
        index.verify(roots.data, roots.configs)


def test_ingest_rejects_missing_previously_indexed_diff(roots):
    _versions(roots)
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    _diff(roots)
    index.ingest_diff("idx-diff", roots.data, roots.configs)
    _diff(roots, "idx-diff-2")
    before = index.normalized()
    (roots.data / "artifacts" / "dataset_diff" / "idx-diff" / "manifest.json").unlink()

    with pytest.raises(IntegrityError, match="ingested dataset diff is missing"):
        index.ingest_diff("idx-diff-2", roots.data, roots.configs)

    assert index.normalized() == before


def test_ingest_rehashes_current_diff_endpoint_inputs(roots):
    _versions(roots)
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    _diff(roots)
    samples = roots.data / "datasets" / "idx-old" / "samples.jsonl"
    samples.write_bytes(samples.read_bytes() + b"\n")

    with pytest.raises(IntegrityError, match="dataset diff input .*changed"):
        index.ingest_diff("idx-diff", roots.data, roots.configs)


def test_ingest_rechecks_inputs_at_commit_boundary_and_rolls_back(roots, monkeypatch):
    _versions(roots)
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    _diff(roots)
    before = index.normalized()
    samples = roots.data / "datasets" / "idx-old" / "samples.jsonl"
    from vcp.provenance import index as index_module

    original = index_module.compute_statuses_for_entities
    changed = False

    def mutate_during_status(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            samples.write_bytes(samples.read_bytes() + b"\n")
            changed = True
        return result

    monkeypatch.setattr(index_module, "compute_statuses_for_entities", mutate_during_status)

    with pytest.raises(IntegrityError, match="dataset diff input .*changed"):
        index.ingest_diff("idx-diff", roots.data, roots.configs)

    assert index.normalized() == before


def test_ingest_rehashes_current_diff_source_audit_pin(roots):
    _versions(roots)
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    diff = _diff(roots)
    from vcp.artifact import store

    manifest = store.load_manifest(roots.data, "dataset_diff", diff.artifact_id)
    audit = next(ref for ref in manifest.spec.inputs if ref.name == "from_source_audit")
    path = roots.data / audit.path
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(IntegrityError, match="dataset diff input .*changed"):
        index.ingest_diff("idx-diff", roots.data, roots.configs)


def test_ingest_rejects_dataset_evolution_cycle(roots):
    _versions(roots)
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    _diff(roots)
    index.ingest_diff("idx-diff", roots.data, roots.configs)
    create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="idx-new",
            to_dataset="idx-old",
            artifact_id="reverse-diff",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )

    with pytest.raises(IntegrityError, match="dataset_cycle"):
        index.ingest_diff("reverse-diff", roots.data, roots.configs)


def test_sync_appends_new_canonical_dataset_and_matches_replay(roots):
    old_samples = det_samples(3, seed=4)
    _dataset(roots, "idx-old", old_samples)
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    _dataset(roots, "idx-new", det_samples(2, seed=5))

    result = index.sync(roots.data, roots.configs)

    assert result.entities == len(build_graph(roots.data, roots.configs).entities)
    assert index.load_graph().normalized() == build_graph(roots.data, roots.configs).normalized()


def test_rebuild_rejects_append_during_full_replay(roots, monkeypatch):
    _versions(roots)
    ledger = roots.configs / "datasets" / "idx-old" / "events.jsonl"
    ledger.write_text('{"event":"before"}\n', encoding="utf-8", newline="\n")
    index = ProvenanceIndex(provenance_index_path(roots.data))
    original = build_graph

    def appending_build(data_root, configs_root):
        graph = original(data_root, configs_root)
        with ledger.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write('{"event":"after"}\n')
        return graph

    monkeypatch.setattr("vcp.provenance.index.build_graph", appending_build)
    with pytest.raises(IntegrityError, match="changed during provenance rebuild"):
        index.rebuild(roots.data, roots.configs)
    assert not index.path.exists()


def test_multihop_incremental_statuses_match_full_replay(roots, tmp_path):
    old, _, _ = det_with_runs(roots, tmp_path, n=30)
    v2_samples = [sample.model_copy(deep=True) for sample in old.samples]
    v2_samples[0] = v2_samples[0].model_copy(update={"group": "changed"})
    v2 = _dataset(roots, "idx-v2", v2_samples)
    v3_samples = [sample.model_copy(deep=True) for sample in v2.samples]
    v3_samples[1] = v3_samples[1].model_copy(update={"meta": {"opaque": 1}})
    _dataset(roots, "idx-v3", v3_samples)
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    for artifact_id, before, after in (
        ("hop-1", "tiny", "idx-v2"),
        ("hop-2", "idx-v2", "idx-v3"),
    ):
        create_dataset_diff(
            DatasetDiffSpec(
                from_dataset=before,
                to_dataset=after,
                artifact_id=artifact_id,
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )
        index.ingest_diff(artifact_id, roots.data, roots.configs)

    verified = index.verify(roots.data, roots.configs)
    assert verified.ok is True, verified.issues


def test_fixed_seed_mixed_history_matches_after_every_event(roots, tmp_path):
    first, _, _ = det_with_runs(roots, tmp_path, n=24)
    rng = random.Random(20260913)
    versions = [("tiny", [sample.model_copy(deep=True) for sample in first.samples])]
    for step, kind in enumerate(("group", "meta", "labels", "views"), start=1):
        samples = [sample.model_copy(deep=True) for sample in versions[-1][1]]
        position = rng.randrange(len(samples))
        sample = samples[position]
        if kind == "group":
            sample = sample.model_copy(update={"group": f"g-{step}"})
        elif kind == "meta":
            sample = sample.model_copy(update={"meta": {"opaque": step}})
        elif kind == "labels":
            labels = sample.labels.model_copy(deep=True)
            labels.boxes[0].x += step / 10
            sample = sample.model_copy(update={"labels": labels})
        else:
            views = [view.model_copy(deep=True) for view in sample.views]
            views[0] = views[0].model_copy(update={"path": f"opaque/{step}.png"})
            sample = sample.model_copy(update={"views": views})
        samples[position] = sample
        name = f"history-{step}"
        _dataset(roots, name, samples)
        versions.append((name, samples))
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    for step, ((before, _), (after, _)) in enumerate(
        zip(versions[:-1], versions[1:], strict=True), start=1
    ):
        artifact_id = f"history-diff-{step}"
        create_dataset_diff(
            DatasetDiffSpec(
                from_dataset=before,
                to_dataset=after,
                artifact_id=artifact_id,
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )
        index.ingest_diff(artifact_id, roots.data, roots.configs)
        verified = index.verify(roots.data, roots.configs)
        assert verified.ok is True, verified.issues


def test_wal_allows_reader_snapshot_during_single_writer_ingest(roots):
    _versions(roots)
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    _diff(roots)
    reader = sqlite3.connect(index.path)
    try:
        assert reader.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        reader.execute("BEGIN")
        before = reader.execute("SELECT count(*) FROM sample_changes").fetchone()[0]

        index.ingest_diff("idx-diff", roots.data, roots.configs)

        assert reader.execute("SELECT count(*) FROM sample_changes").fetchone()[0] == before
    finally:
        reader.rollback()
        reader.close()
    assert index.stats()["sample_changes"] > before


def test_incremental_ingest_never_loads_or_hashes_historical_changes(roots, monkeypatch):
    _versions(roots)
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    _diff(roots)
    from vcp.provenance import index as index_module

    original = index_module._load_graph
    calls: list[tuple[bool, bool]] = []

    def structural_only(connection, **kwargs):
        calls.append((kwargs.get("include_changes", True), kwargs.get("include_change_ids", True)))
        return original(connection, **kwargs)

    monkeypatch.setattr(index_module, "_load_graph", structural_only)
    monkeypatch.setattr(
        index_module,
        "graph_hash",
        lambda _graph: (_ for _ in ()).throw(AssertionError("full graph hash during ingest")),
    )

    result = index.ingest_diff("idx-diff", roots.data, roots.configs)

    assert result.inserted is True
    assert calls == [(False, False)]


def test_verify_checks_fingerprint_accumulator_metadata(roots):
    _versions(roots)
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    connection = sqlite3.connect(index.path)
    with connection:
        connection.execute("UPDATE metadata SET value='0' WHERE key='graph_record_count'")
    connection.close()

    result = index.verify(roots.data, roots.configs)

    assert result.ok is False
    assert "recorded graph record count differs" in result.issues
