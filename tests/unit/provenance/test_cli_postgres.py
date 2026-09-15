from __future__ import annotations

import json
from dataclasses import fields
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from vcp.cli import app
from vcp.provenance.backend import (
    BackendName,
    MaintenanceResult,
    RequestedStrategy,
    SelectedStrategy,
)

runner = CliRunner()


def _roots_args(roots):
    return [
        "--data-root",
        str(roots.data),
        "--configs-root",
        str(roots.configs),
    ]


class _IngestBackend:
    name = BackendName.POSTGRESQL
    location_label = "postgresql"

    def __init__(self):
        self.calls = []

    def ingest_diff(
        self,
        artifact_id,
        data_root,
        configs_root,
        *,
        requested_strategy,
        policy_id,
    ):
        self.calls.append((artifact_id, data_root, configs_root, requested_strategy, policy_id))
        return MaintenanceResult(
            artifact_id=artifact_id,
            inserted=True,
            backend="postgresql",
            requested_strategy=RequestedStrategy.AUTO,
            selected_strategy=SelectedStrategy.INCREMENTAL,
            strategy_reason="calibrated_incremental_lower_confident_cost",
            changed_samples=3,
            dirty_entities=7,
            total_entities=20,
            dirty_ratio=0.35,
            estimated_incremental_ms=4.5,
            estimated_full_ms=9.25,
            policy_version="postgres-adaptive-v1",
            elapsed_ms=5.0,
            graph_hash="a" * 64,
        )


def test_postgres_ingest_reports_complete_decision(monkeypatch, roots):
    backend = _IngestBackend()
    configs = []

    def factory(config, data_root):
        configs.append((config, data_root))
        return backend

    monkeypatch.setattr("vcp.cli_provenance.make_backend", factory)
    args = [
        "provenance",
        "ingest",
        "--backend",
        "postgresql",
        "--pg-service",
        "private-service",
        "--strategy",
        "auto",
        "--policy",
        "policy-1",
        "--artifact",
        "diff-1",
        *_roots_args(roots),
    ]

    result = runner.invoke(app, [*args, "--json"])

    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    expected = {field.name for field in fields(MaintenanceResult)}
    assert expected <= doc["result"].keys()
    assert expected <= doc["fields"].keys()
    assert all(f"{key}=" in result.stderr for key in expected)
    assert "private-service" not in result.stdout + result.stderr
    assert configs[0][0].name is BackendName.POSTGRESQL
    assert configs[0][0].pg_service == "private-service"
    assert backend.calls[0][3:] == (RequestedStrategy.AUTO, "policy-1")

    human = runner.invoke(app, args)
    assert human.exit_code == 0
    assert all(f"{key}=" in human.stdout for key in expected)
    assert "private-service" not in human.stdout


def test_postgres_auto_without_policy_is_warn(monkeypatch, roots):
    backend = _IngestBackend()
    monkeypatch.setattr("vcp.cli_provenance.make_backend", lambda config, data_root: backend)

    result = runner.invoke(
        app,
        [
            "provenance",
            "ingest",
            "--backend",
            "postgresql",
            "--strategy",
            "auto",
            "--artifact",
            "diff-1",
            "--json",
            *_roots_args(roots),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "WARN"
    assert "VERDICT cmd=provenance.ingest status=WARN" in result.stderr
    assert backend.calls[0][3:] == (RequestedStrategy.AUTO, None)


@pytest.mark.parametrize(
    ("command", "required"),
    [
        ("rebuild", []),
        ("ingest", ["--artifact", "diff-1"]),
        ("sync", []),
        ("impact", ["--dataset", "dataset-1"]),
        ("stale", ["--head", "dataset-1"]),
        ("explain", ["--entity", "run:1"]),
        ("status", []),
        ("verify-index", []),
    ],
)
def test_every_command_parses_unknown_backend_inside_run_command(roots, command, required):
    result = runner.invoke(
        app,
        [
            "provenance",
            command,
            *required,
            "--backend",
            "unknown",
            "--json",
            *_roots_args(roots),
        ],
    )

    assert result.exit_code == 1
    assert result.stdout
    assert f"VERDICT cmd=provenance.{command} status=FAIL" in result.stderr
    assert "unsupported_backend" in result.stderr


@pytest.mark.parametrize("strategy", ["unknown", "full", "auto"])
def test_invalid_or_unsupported_sqlite_strategy_ends_in_verdict(roots, strategy):
    result = runner.invoke(
        app,
        [
            "provenance",
            "ingest",
            "--artifact",
            "diff-1",
            "--strategy",
            strategy,
            "--json",
            *_roots_args(roots),
        ],
    )

    assert result.exit_code == 1
    assert "VERDICT cmd=provenance.ingest status=FAIL" in result.stderr
    assert "unsupported_strategy" in result.stderr


def test_pg_service_is_postgresql_only_and_rejects_dsn_without_echoing_it(roots):
    sqlite = runner.invoke(
        app,
        [
            "provenance",
            "status",
            "--pg-service",
            "service-1",
            "--json",
            *_roots_args(roots),
        ],
    )
    marker = "postgresql://user:TOPSECRET@private/database"
    postgres = runner.invoke(
        app,
        [
            "provenance",
            "status",
            "--backend",
            "postgresql",
            "--pg-service",
            marker,
            "--json",
            *_roots_args(roots),
        ],
    )

    assert sqlite.exit_code == postgres.exit_code == 1
    assert "pg_service_requires_postgresql" in sqlite.stderr
    assert "invalid_postgres_service" in postgres.stderr
    assert marker not in postgres.stdout + postgres.stderr
    assert "TOPSECRET" not in postgres.stdout + postgres.stderr


def test_backend_and_strategy_errors_do_not_echo_connection_shaped_values(roots):
    marker = "postgresql://user:TOPSECRET@private/database"
    results = [
        runner.invoke(
            app,
            [
                "provenance",
                "status",
                "--backend",
                marker,
                "--json",
                *_roots_args(roots),
            ],
        ),
        runner.invoke(
            app,
            [
                "provenance",
                "ingest",
                "--artifact",
                "diff-1",
                "--strategy",
                marker,
                "--json",
                *_roots_args(roots),
            ],
        ),
    ]

    assert all(result.exit_code == 1 for result in results)
    assert all("TOPSECRET" not in result.stdout + result.stderr for result in results)


@pytest.mark.parametrize(
    ("backend", "strategy"),
    [("sqlite", "auto"), ("postgresql", "incremental"), ("postgresql", "full")],
)
def test_policy_requires_postgresql_auto(roots, backend, strategy):
    result = runner.invoke(
        app,
        [
            "provenance",
            "ingest",
            "--artifact",
            "diff-1",
            "--backend",
            backend,
            "--strategy",
            strategy,
            "--policy",
            "policy-1",
            "--json",
            *_roots_args(roots),
        ],
    )

    assert result.exit_code == 1
    assert "policy_requires_postgresql_auto" in result.stderr
    assert "VERDICT cmd=provenance.ingest status=FAIL" in result.stderr


def test_postgres_driver_error_is_redacted_from_cli_and_log(monkeypatch, roots):
    class DriverError(Exception):
        sqlstate = "08001"

    driver = SimpleNamespace(
        Error=DriverError,
        connect=lambda **kwargs: (_ for _ in ()).throw(
            DriverError("password=TOPSECRET host=private")
        ),
    )
    monkeypatch.setattr("vcp.provenance.postgres._load_psycopg", lambda: driver)

    result = runner.invoke(
        app,
        [
            "provenance",
            "status",
            "--backend",
            "postgresql",
            "--json",
            *_roots_args(roots),
        ],
    )
    logs = "".join(path.read_text(encoding="utf-8") for path in roots.data.rglob("*.jsonl"))

    assert result.exit_code == 1
    assert "database_connection_failed" in result.stderr
    assert "sqlstate=08001" in result.stderr
    assert "TOPSECRET" not in result.stdout + result.stderr + logs
