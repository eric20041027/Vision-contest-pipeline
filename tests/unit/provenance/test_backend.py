from vcp.core.paths import provenance_index_path
from vcp.provenance.backend import BackendConfig, BackendName, SQLiteBackend, make_backend
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
