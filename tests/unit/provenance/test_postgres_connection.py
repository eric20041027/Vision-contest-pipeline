from types import SimpleNamespace

import pytest

from vcp.core.errors import ValidationFailed
from vcp.provenance.backend import BackendConfig, BackendName, make_backend
from vcp.provenance.postgres import (
    PostgresProvenanceBackend,
    _connect,
    raise_redacted_database_error,
    validate_pg_service,
)


def test_postgres_import_is_lazy_and_missing_extra_is_actionable(monkeypatch, roots):
    def missing_driver(name):
        raise ImportError("no psycopg")

    monkeypatch.setattr("vcp.provenance.backend.import_module", missing_driver)
    with pytest.raises(ValidationFailed, match="uv sync --extra postgres"):
        make_backend(BackendConfig(BackendName.POSTGRESQL), roots.data)


@pytest.mark.parametrize(
    "value",
    ["postgresql://u:secret@host/db", "password=secret", "bad service", " service", "service "],
)
def test_service_rejects_dsn_or_whitespace(value):
    with pytest.raises(ValidationFailed, match="invalid_postgres_service"):
        validate_pg_service(value)


@pytest.mark.parametrize("value", ["vcp", "vcp-prod_17.11", "a" * 128])
def test_service_accepts_only_safe_service_names(value):
    assert validate_pg_service(value) == value


def test_postgres_constructor_loads_driver_but_does_not_connect(monkeypatch):
    calls = []
    driver = SimpleNamespace(connect=lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr("vcp.provenance.postgres._load_psycopg", lambda: driver)

    backend = PostgresProvenanceBackend(BackendConfig(BackendName.POSTGRESQL, "vcp"))

    assert backend.location_label == "postgresql"
    assert calls == []


def test_connect_uses_only_validated_service_and_autocommit(monkeypatch):
    calls = []
    connection = object()
    driver = SimpleNamespace(connect=lambda **kwargs: calls.append(kwargs) or connection)
    monkeypatch.setattr("vcp.provenance.postgres._load_psycopg", lambda: driver)

    assert _connect(BackendConfig(BackendName.POSTGRESQL, "vcp")) is connection
    assert calls == [{"service": "vcp", "autocommit": True}]


def test_connect_without_service_uses_libpq_defaults(monkeypatch):
    calls = []
    driver = SimpleNamespace(connect=lambda **kwargs: calls.append(kwargs) or object())
    monkeypatch.setattr("vcp.provenance.postgres._load_psycopg", lambda: driver)

    _connect(BackendConfig(BackendName.POSTGRESQL))

    assert calls == [{"autocommit": True}]


def test_driver_error_never_exposes_secret():
    class FakePsycopgError(Exception):
        sqlstate = "08001"

    error = FakePsycopgError("password=TOPSECRET host=private")
    with pytest.raises(ValidationFailed) as caught:
        raise_redacted_database_error(error)

    assert "TOPSECRET" not in str(caught.value)
    assert caught.value.fields == {"backend": "postgresql", "sqlstate": "08001"}


def test_connect_error_never_exposes_secret(monkeypatch):
    class FakePsycopgError(Exception):
        sqlstate = "08001"

    driver = SimpleNamespace(
        connect=lambda **kwargs: (_ for _ in ()).throw(FakePsycopgError("password=TOPSECRET"))
    )
    monkeypatch.setattr("vcp.provenance.postgres._load_psycopg", lambda: driver)

    with pytest.raises(ValidationFailed) as caught:
        _connect(BackendConfig(BackendName.POSTGRESQL, "vcp"))

    assert "TOPSECRET" not in str(caught.value)
    assert caught.value.fields == {"backend": "postgresql", "sqlstate": "08001"}


def test_unreadable_driver_sqlstate_remains_redacted():
    class FakePsycopgError(Exception):
        @property
        def sqlstate(self):
            raise RuntimeError("TOPSECRET")

    with pytest.raises(ValidationFailed) as caught:
        raise_redacted_database_error(FakePsycopgError("password=TOPSECRET"))

    assert "TOPSECRET" not in str(caught.value)
    assert caught.value.fields == {"backend": "postgresql", "sqlstate": "unknown"}
