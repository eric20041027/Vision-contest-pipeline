"""Optional Psycopg connection boundary for the PostgreSQL provenance backend."""

from __future__ import annotations

import re
from importlib import import_module
from typing import Any

from vcp.core.errors import ValidationFailed
from vcp.provenance.backend import BackendConfig, BackendName

_SERVICE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_SQLSTATE = re.compile(r"[0-9A-Z]{5}")
_MISSING_DEPENDENCY = "missing_dependency: install with `uv sync --extra postgres`"


def validate_pg_service(value: str | None) -> str | None:
    """Accept only a libpq service name, never conninfo or credentials."""
    if value is None:
        return None
    if not _SERVICE_NAME.fullmatch(value):
        raise ValidationFailed("invalid_postgres_service")
    return value


def _load_psycopg() -> Any:
    """Load the optional driver only when PostgreSQL is explicitly selected."""
    try:
        return import_module("psycopg")
    except ImportError:
        raise ValidationFailed(_MISSING_DEPENDENCY) from None


def _safe_sqlstate(error: BaseException) -> str:
    try:
        value = getattr(error, "sqlstate", None)
    except Exception:
        return "unknown"
    if isinstance(value, str) and _SQLSTATE.fullmatch(value):
        return value
    return "unknown"


def raise_redacted_database_error(error: BaseException) -> None:
    """Raise a stable database failure without exposing driver-provided text."""
    raise ValidationFailed(
        "database_connection_failed",
        fields={"backend": BackendName.POSTGRESQL.value, "sqlstate": _safe_sqlstate(error)},
    ) from None


def _connect(config: BackendConfig) -> Any:
    """Open one autocommit connection through libpq's standard resolution."""
    service = validate_pg_service(config.pg_service)
    psycopg = _load_psycopg()
    try:
        if service is None:
            return psycopg.connect(autocommit=True)
        return psycopg.connect(service=service, autocommit=True)
    except Exception as error:
        raise_redacted_database_error(error)


class PostgresProvenanceBackend:
    """Connection configuration holder; database operations are added in later tasks."""

    name = BackendName.POSTGRESQL
    location_label = BackendName.POSTGRESQL.value

    def __init__(self, config: BackendConfig) -> None:
        self.config = BackendConfig(
            name=self.name, pg_service=validate_pg_service(config.pg_service)
        )
        self._psycopg = _load_psycopg()


__all__ = [
    "PostgresProvenanceBackend",
    "_connect",
    "_load_psycopg",
    "raise_redacted_database_error",
    "validate_pg_service",
]
