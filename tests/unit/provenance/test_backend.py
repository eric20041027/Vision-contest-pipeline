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
