import pytest

from helpers import det_samples, make_card
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths, provenance_index_path
from vcp.data.dataset import Dataset
from vcp.data.source_audit import write_source_audit
from vcp.provenance.backend import BackendConfig, BackendName, SQLiteBackend, make_backend
from vcp.provenance.diff import DatasetDiffSpec, create_dataset_diff
from vcp.provenance.index import ProvenanceIndex


def test_default_factory_wraps_existing_sqlite_without_importing_psycopg(roots, monkeypatch):
    imported = []
    monkeypatch.setattr("vcp.provenance.backend.import_module", lambda name: imported.append(name))
    backend = make_backend(BackendConfig(), roots.data)
    assert backend.name is BackendName.SQLITE
    assert backend.location_label == str(provenance_index_path(roots.data))
    assert imported == []


def test_sqlite_adapter_preserves_normalized_results(roots):
    direct = ProvenanceIndex(provenance_index_path(roots.data))
    adapter = SQLiteBackend(direct)
    direct.rebuild(roots.data, roots.configs)
    assert adapter.normalized() == direct.normalized()
    with adapter.read_snapshot() as reader:
        assert reader.normalized() == direct.normalized()
        assert reader.load_graph().normalized() == direct.load_graph().normalized()
        assert reader.stats() == direct.stats()
        assert reader.verify(roots.data, roots.configs) == direct.verify(roots.data, roots.configs)


@pytest.mark.parametrize("strategy", ["full", "auto"])
def test_sqlite_adapter_rejects_unsupported_strategies(roots, strategy):
    adapter = SQLiteBackend(ProvenanceIndex(provenance_index_path(roots.data)))
    with pytest.raises(ValidationFailed, match="unsupported_strategy"):
        adapter.ingest_diff("missing", roots.data, roots.configs, requested_strategy=strategy)


def _save_dataset(roots, name, samples):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    dataset = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    dataset.save(paths)
    write_source_audit(paths, dataset.card, data_root=roots.data)


def _create_diff(roots, from_name, to_name, artifact_id):
    return create_dataset_diff(
        DatasetDiffSpec(
            from_dataset=from_name,
            to_dataset=to_name,
            artifact_id=artifact_id,
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )


def test_sqlite_adapter_classifies_duplicate_and_incremental_results(roots):
    old_samples = det_samples(3, seed=31)
    new_samples = [sample.model_copy(deep=True) for sample in old_samples]
    new_samples[0] = new_samples[0].model_copy(update={"group": "changed"})
    _save_dataset(roots, "class-old", old_samples)
    _save_dataset(roots, "class-new", new_samples)
    adapter = make_backend(BackendConfig(), roots.data)
    adapter.rebuild(roots.data, roots.configs)
    _create_diff(roots, "class-old", "class-new", "class-diff")

    result = adapter.ingest_diff("class-diff", roots.data, roots.configs)
    duplicate = adapter.ingest_diff("class-diff", roots.data, roots.configs)

    assert result.selected_strategy == "INCREMENTAL"
    assert result.strategy_reason == "requested_incremental"
    assert result.changed_samples == 1
    assert duplicate.selected_strategy == "NO_OP"
    assert duplicate.strategy_reason == "duplicate_artifact_no_op"


def test_sqlite_adapter_classifies_verified_zero_change_diff(roots):
    samples = det_samples(3, seed=32)
    _save_dataset(roots, "noop-old", samples)
    _save_dataset(roots, "noop-new", [sample.model_copy(deep=True) for sample in samples])
    adapter = make_backend(BackendConfig(), roots.data)
    adapter.rebuild(roots.data, roots.configs)
    _create_diff(roots, "noop-old", "noop-new", "noop-diff")

    result = adapter.ingest_diff("noop-diff", roots.data, roots.configs)

    assert result.selected_strategy == "NO_OP"
    assert result.strategy_reason == "verified_zero_semantic_changes"
    assert result.changed_samples == 0


@pytest.mark.parametrize("mode", ["incremental", "duplicate", "zero-change"])
def test_sqlite_adapter_ingest_does_not_load_historical_payloads(roots, monkeypatch, mode):
    from vcp.provenance import index as index_module

    samples = det_samples(3, seed=33)
    changed = [sample.model_copy(deep=True) for sample in samples]
    changed[0] = changed[0].model_copy(update={"group": "changed"})
    newest = [sample.model_copy(deep=True) for sample in changed]
    if mode != "zero-change":
        newest[1] = newest[1].model_copy(update={"group": "newest"})
    for name, rows in (("history-old", samples), ("history-new", changed), ("latest", newest)):
        _save_dataset(roots, name, rows)
    adapter = make_backend(BackendConfig(), roots.data)
    adapter.rebuild(roots.data, roots.configs)
    _create_diff(roots, "history-old", "history-new", "history-diff")
    adapter.ingest_diff("history-diff", roots.data, roots.configs)
    _create_diff(roots, "history-new", "latest", "latest-diff")
    if mode == "duplicate":
        adapter.ingest_diff("latest-diff", roots.data, roots.configs)

    load_graph = index_module._load_graph
    open_index = adapter.index._open
    statements = []

    def structural_only(connection, **kwargs):
        assert kwargs.get("include_changes", True) is False, "loaded historical change payloads"
        assert kwargs.get("include_change_ids", True) is False, "loaded complete transition history"
        return load_graph(connection, **kwargs)

    def traced_open():
        connection = open_index()
        connection.set_trace_callback(statements.append)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(index_module, "_load_graph", structural_only)
        patch.setattr(adapter.index, "_open", traced_open)
        result = adapter.ingest_diff("latest-diff", roots.data, roots.configs)

    graph = adapter.load_graph()
    assert result.total_entities == len(graph.entities)
    assert result.dirty_ratio == result.dirty_entities / len(graph.entities)
    assert result.artifact_id == "latest-diff"
    assert result.backend == "sqlite"
    assert result.requested_strategy == "incremental"
    assert result.inserted is (mode != "duplicate")
    assert result.changed_samples == (0 if mode == "zero-change" else 1)
    assert result.selected_strategy == ("INCREMENTAL" if mode == "incremental" else "NO_OP")
    assert (
        result.strategy_reason
        == {
            "incremental": "requested_incremental",
            "duplicate": "duplicate_artifact_no_op",
            "zero-change": "verified_zero_semantic_changes",
        }[mode]
    )
    assert result.graph_hash == adapter.stats()["graph_hash"]
    assert result.policy_version == "sqlite-compat-v1"
    assert result.estimated_incremental_ms is None
    assert result.estimated_full_ms is None
    assert any("SELECT COUNT(*) AS n FROM entities" in sql for sql in statements)
    assert not any(
        "event_json" in sql and "FROM sample_changes" in sql and "WHERE change_id=" not in sql
        for sql in statements
    )


def test_sqlite_adapter_elapsed_includes_verification_ingest_and_counts(roots, monkeypatch):
    from vcp.provenance import backend as backend_module

    samples = det_samples(3, seed=34)
    _save_dataset(roots, "timed-old", samples)
    _save_dataset(roots, "timed-new", [sample.model_copy(deep=True) for sample in samples])
    adapter = make_backend(BackendConfig(), roots.data)
    adapter.rebuild(roots.data, roots.configs)
    _create_diff(roots, "timed-old", "timed-new", "timed-diff")
    clock_ns = 0

    def timed(operation, duration_ns):
        def wrapper(*args, **kwargs):
            nonlocal clock_ns
            result = operation(*args, **kwargs)
            clock_ns += duration_ns
            return result

        return wrapper

    monkeypatch.setattr(backend_module, "perf_counter_ns", lambda: clock_ns)
    monkeypatch.setattr(
        backend_module, "load_dataset_diff", timed(backend_module.load_dataset_diff, 2_000_000)
    )
    monkeypatch.setattr(adapter.index, "ingest_diff", timed(adapter.index.ingest_diff, 3_000_000))
    monkeypatch.setattr(adapter.index, "stats", timed(adapter.index.stats, 5_000_000))

    result = adapter.ingest_diff("timed-diff", roots.data, roots.configs)

    assert result.elapsed_ms == 10.0
