"""Exercise installed Psycopg diagnostics offline, before any libpq I/O or resolution."""

from __future__ import annotations

import importlib.util
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, RLock
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import connection as driver_connection
from psycopg import pq
from typer.testing import CliRunner

from vcp.cli import app
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.provenance import postgres
from vcp.provenance.backend import BackendConfig, BackendName

SECRET = "synthetic-password-marker"
HOST = "synthetic-private-host"
CONFIG = BackendConfig(BackendName.POSTGRESQL, "synthetic-service")


def driver_error(kind):
    classes = {
        "connect": psycopg.errors.ConnectionFailure,
        "operation": psycopg.errors.SerializationFailure,
        "rollback": psycopg.errors.TransactionRollback,
        "close": psycopg.errors.SqlclientUnableToEstablishSqlconnection,
    }
    return classes[kind](f"{kind}: password={SECRET} host={HOST}")


def exhaust(generator, **kwargs):
    while True:
        try:
            next(generator)
        except StopIteration as done:
            return done.value


class OfflineConnection:
    """Only transport/SQL are doubles; transaction entry, exit and diagnostics are Psycopg."""

    transaction = psycopg.Connection.transaction
    __enter__ = psycopg.Connection.__enter__
    __exit__ = psycopg.Connection.__exit__
    commit = psycopg.Connection.commit
    _commit_gen = psycopg.Connection._commit_gen
    rollback = psycopg.Connection.rollback
    _rollback_gen = psycopg.Connection._rollback_gen
    closed = psycopg.Connection.closed

    def __init__(self, *, rollback_error=None, close_error=None, operation_error=None):
        self.pgconn = SimpleNamespace(
            status=pq.ConnStatus.OK,
            transaction_status=pq.TransactionStatus.IDLE,
            used_gssapi=False,
            finish=self._finish,
        )
        self.lock = RLock()
        self._num_transactions = 0
        self._pipeline = None
        self._tpc = None
        self._prepared = SimpleNamespace(clear=lambda: None, maintain_gen=lambda conn: iter(()))
        self.rollback_error = rollback_error
        self.close_error = close_error
        self.operation_error = operation_error
        self.closed_count = 0
        self.commands = []
        self.info = SimpleNamespace(server_version=170011)

    def __repr__(self):
        return f"<offline connection host={HOST}>"

    def _get_tx_start_command(self):
        return b"BEGIN"

    def _exec_command(self, command):
        self.commands.append(command)
        if command == b"ROLLBACK" and self.rollback_error:
            raise self.rollback_error
        self.pgconn.transaction_status = (
            pq.TransactionStatus.INTRANS if command == b"BEGIN" else pq.TransactionStatus.IDLE
        )
        # No I/O: real Transaction generators are still consumed by wait().
        yield from ()

    def wait(self, generator):
        return exhaust(generator)

    def execute(self, query, params=()):
        if self.operation_error:
            raise self.operation_error
        if query == "BEGIN":
            exhaust(self._exec_command(b"BEGIN"))
        return self

    def fetchone(self):
        return (None,)

    def close(self):
        self.closed_count += 1
        psycopg.Connection.close(self)

    def _finish(self):
        if self.close_error:
            raise self.close_error
        self.pgconn.status = pq.ConnStatus.BAD


@pytest.fixture
def offline_driver(monkeypatch):
    """Patch before parameter parsing so no credential files, DNS or sockets are used."""
    state = SimpleNamespace(connection=OfflineConnection(), connect_error=None)
    monkeypatch.setattr(
        psycopg.Connection, "_get_connection_params", classmethod(lambda cls, *a, **kw: {})
    )
    monkeypatch.setattr(
        driver_connection, "conninfo_attempts", lambda params: [{"host": HOST, "port": "5432"}]
    )
    monkeypatch.setattr(driver_connection, "make_conninfo", lambda *a, **kw: "offline")

    def connect_gen(cls, *args, **kwargs):
        if state.connect_error:
            raise state.connect_error
        yield from ()
        state.connection.pgconn.status = pq.ConnStatus.OK
        return state.connection

    monkeypatch.setattr(psycopg.Connection, "_connect_gen", classmethod(connect_gen))
    monkeypatch.setattr(driver_connection.waiting, "wait_conn", exhaust)
    monkeypatch.setattr("psycopg.transaction.connection_summary", lambda conn: f"host={HOST}")
    monkeypatch.setattr(postgres, "validate_schema", lambda connection: None)
    monkeypatch.setattr(postgres, "validate_schema_in_transaction", lambda connection: None)
    monkeypatch.setattr(postgres, "install_schema_in_transaction", lambda connection: None)
    monkeypatch.setattr(postgres, "_active_generation", lambda connection: "generation")
    monkeypatch.setattr(postgres.PostgresProvenanceBackend, "_publish_generation", lambda *a: None)
    return state


@pytest.fixture
def diagnostics(monkeypatch, caplog):
    """Capture root propagation, driver handlers and the host's record factory separately."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="psycopg")
    caplog.set_level(logging.DEBUG, logger="psycopg.transaction")
    seen, records = [], []
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        seen.append(record.getMessage())
        return record

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    monkeypatch.setattr(logging, "_logRecordFactory", factory)
    logger = logging.getLogger("psycopg")
    handler = Capture()
    logger.addHandler(handler)
    try:
        yield lambda: "\n".join([*seen, *records, caplog.text])
    finally:
        logger.removeHandler(handler)


def assert_redacted(text):
    assert SECRET not in text
    assert HOST not in text


@pytest.mark.parametrize("failed", [False, True])
def test_actual_connect_diagnostics_are_redacted(offline_driver, diagnostics, failed):
    if failed:
        offline_driver.connect_error = driver_error("connect")
        with pytest.raises(ValidationFailed) as caught:
            postgres._connect(CONFIG)
        assert caught.value.fields["sqlstate"] == "08006"
    else:
        assert postgres._connect(CONFIG) is offline_driver.connection
    assert_redacted(diagnostics())


@pytest.mark.parametrize("close_fails", [False, True])
def test_actual_transaction_rollback_keeps_primary_error(offline_driver, diagnostics, close_fails):
    offline_driver.connection.rollback_error = driver_error("rollback")
    if close_fails:
        offline_driver.connection.close_error = driver_error("close")
    with pytest.raises(ValidationFailed) as caught:
        with postgres._read_transaction(CONFIG, psycopg):
            raise driver_error("operation")
    assert caught.value.fields["sqlstate"] == "40001"
    assert offline_driver.connection.closed_count == 1
    assert b"ROLLBACK" in offline_driver.connection.commands
    assert_redacted(diagnostics())


@pytest.mark.parametrize("owner", ["read", "rebuild", "ingest"])
def test_cleanup_does_not_replace_domain_error(offline_driver, diagnostics, roots, owner):
    primary = IntegrityError("canonical_drift: synthetic operation")
    offline_driver.connection.close_error = driver_error("close")
    backend = postgres.PostgresProvenanceBackend(CONFIG)
    with pytest.raises(IntegrityError) as caught:
        if owner == "read":
            with postgres._read_transaction(CONFIG, psycopg):
                raise primary
        else:
            offline_driver.connection.operation_error = primary
            if owner == "rebuild":
                backend.rebuild(roots.data, roots.configs)
            else:
                backend.ingest_diff("diff", roots.data, roots.configs)
    assert caught.value is primary
    assert offline_driver.connection.closed_count == 1
    assert_redacted(diagnostics())


@pytest.mark.parametrize("failure", ["success", "connect", "operation", "close", "rollback_close"])
def test_cli_lifecycle_json_verdict_jsonl_and_exit_class(
    offline_driver, diagnostics, roots, failure
):
    connection = offline_driver.connection
    expected = {
        "connect": "08006",
        "operation": "40001",
        "close": "08001",
        "rollback_close": "40001",
    }
    if failure == "connect":
        offline_driver.connect_error = driver_error("connect")
    if failure in {"operation", "rollback_close"}:
        connection.operation_error = driver_error("operation")
    if failure in {"close", "rollback_close"}:
        connection.close_error = driver_error("close")
    if failure == "rollback_close":
        connection.rollback_error = driver_error("rollback")
    result = CliRunner().invoke(
        app,
        [
            "provenance",
            "rebuild",
            "--backend",
            "postgresql",
            "--json",
            "--data-root",
            str(roots.data),
            "--configs-root",
            str(roots.configs),
        ],
    )
    assert result.exit_code == (0 if failure == "success" else 1)
    doc = json.loads(result.stdout)
    assert doc["status"] == ("OK" if failure == "success" else "FAIL")
    assert f"VERDICT cmd=provenance.rebuild status={doc['status']}" in result.stderr
    if failure != "success":
        assert doc["fields"]["sqlstate"] == expected[failure]
        assert "database_connection_failed" in doc["fields"]["reason"]
    logs = "".join(path.read_text(encoding="utf-8") for path in roots.data.rglob("*.jsonl"))
    assert logs
    assert_redacted(result.stdout + result.stderr + logs + diagnostics())


def test_nested_concurrent_diagnostics_restore_host_logging(diagnostics):
    previous = logging.getLogRecordFactory()
    logger = logging.getLogger("psycopg")
    configuration = (logger.level, logger.disabled, logger.propagate, tuple(logger.handlers))
    together = Barrier(3)

    def worker():
        with postgres._driver_diagnostics():
            together.wait(timeout=5)
            with postgres._driver_diagnostics():
                logging.getLogger("psycopg.new.child").warning("host=%s", HOST)
            together.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        together.wait(timeout=5)
        logger.warning("unrelated host diagnostic preserved")
        together.wait(timeout=5)
        for future in futures:
            future.result(timeout=5)
    assert logging.getLogRecordFactory() is previous
    assert configuration == (
        logger.level,
        logger.disabled,
        logger.propagate,
        tuple(logger.handlers),
    )
    assert "unrelated host diagnostic preserved" in diagnostics()
    assert_redacted(diagnostics())


def test_explicit_rollback_traceback_and_stderr_are_redacted(offline_driver, diagnostics, capsys):
    logger = logging.getLogger("psycopg.transaction")
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:
        with postgres._read_transaction(CONFIG, psycopg):
            raise psycopg.Rollback()
    finally:
        logger.removeHandler(handler)
    assert_redacted(capsys.readouterr().err + diagnostics())


@pytest.fixture
def harness_module():
    path = Path(__file__).resolve().parents[2] / "integration/provenance/conftest.py"
    spec = importlib.util.spec_from_file_location("offline_security_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_harness_setup_and_close_failures_are_redacted(offline_driver, diagnostics, harness_module):
    connection = offline_driver.connection
    connection.operation_error = driver_error("operation")
    connection.close_error = driver_error("close")
    factory = harness_module.HarnessFactory(psycopg, "synthetic-service")
    with pytest.raises(ValidationFailed) as caught:
        with factory.connect():
            pytest.fail("setup must fail before handing out the connection")
    assert caught.value.fields["sqlstate"] == "40001"
    assert connection.closed_count == 1
    assert_redacted(diagnostics())


@pytest.mark.parametrize("owner", ["harness", "benchmark"])
def test_test_only_connection_contexts_guard_rollback(
    offline_driver, diagnostics, harness_module, tmp_path, owner
):
    from performance.provenance import adaptive_benchmark as bench

    connection = offline_driver.connection
    connection.rollback_error = driver_error("rollback")
    if owner == "harness":
        factory = harness_module.HarnessFactory(psycopg, "synthetic-service")
        context = factory.connect()
    else:
        context = bench.fresh_backend(
            "postgres_full", tmp_path, pg_runtime=(psycopg, "synthetic-service")
        )
    with pytest.raises(ValidationFailed) as caught:
        with context as resource:
            if owner == "harness":
                with resource.transaction():
                    raise driver_error("operation")
            else:
                with resource.connect() as raw, raw.transaction():
                    raise driver_error("operation")
    assert caught.value.fields["sqlstate"] == "40001"
    assert_redacted(diagnostics())


@pytest.mark.parametrize("action_fails", [False, True])
def test_benchmark_rollback_failure_is_redacted_and_secondary(
    offline_driver, diagnostics, action_fails
):
    from performance.provenance import adaptive_benchmark as bench

    connection = offline_driver.connection
    connection.rollback_error = driver_error("rollback")

    def action(observed):
        if action_fails:
            raise driver_error("operation")
        observed.plans.append({"Plan": {"Node Type": "ModifyTable"}})

    with pytest.raises(ValidationFailed) as caught:
        bench.capture_explain_rollback(connection, "postgres_incremental", action)
    assert caught.value.fields["sqlstate"] == ("40001" if action_fails else "40000")
    assert_redacted(diagnostics())


def test_harness_records_database_ownership_before_close_failure(offline_driver, harness_module):
    offline_driver.connection.close_error = driver_error("close")
    factory = harness_module.HarnessFactory(psycopg, "synthetic-service")
    with pytest.raises(ValidationFailed):
        factory.create()
    assert len(factory.clones) == 1
    assert factory.clones[0][0].startswith("vcp_test_")


@pytest.mark.parametrize("sqlstate", [None, "host=" + HOST, "abcde", "08001\n", 8001])
def test_cleanup_sqlstate_allowlist(offline_driver, diagnostics, sqlstate):
    class CloseError(Exception):
        pass

    error = CloseError(SECRET)
    error.sqlstate = sqlstate
    offline_driver.connection.close_error = error
    with pytest.raises(ValidationFailed) as caught:
        with postgres._read_transaction(CONFIG, psycopg):
            pass
    assert str(caught.value) == "database_connection_failed"
    assert caught.value.fields == {"backend": "postgresql", "sqlstate": "unknown"}
    assert_redacted(diagnostics())


def test_cleanup_preserves_keyboard_interrupt(offline_driver, diagnostics):
    offline_driver.connection.close_error = driver_error("close")
    with pytest.raises(KeyboardInterrupt):
        with postgres._read_transaction(CONFIG, psycopg):
            raise KeyboardInterrupt()
    assert offline_driver.connection.closed_count == 1
    assert_redacted(diagnostics())


def test_host_logging_reconfiguration_is_preserved(monkeypatch):
    previous = logging.getLogRecordFactory()
    monkeypatch.setattr(logging, "_logRecordFactory", previous)

    def replacement(*args, **kwargs):
        return previous(*args, **kwargs)

    with postgres._driver_diagnostics():
        logging.setLogRecordFactory(replacement)
    assert logging.getLogRecordFactory() is replacement


def test_make_log_record_inside_diagnostic_scope_preserves_host_record():
    previous = logging.getLogRecordFactory()
    record = {"name": "host.application", "msg": "host record %s", "args": ("copied",)}
    with postgres._driver_diagnostics():
        # makeLogRecord first calls the active factory with name=None, then
        # populates the returned record from this mapping.
        copied = logging.makeLogRecord(record)
    assert copied.name == "host.application"
    assert copied.getMessage() == "host record copied"
    assert logging.getLogRecordFactory() is previous


@pytest.mark.parametrize("failed", [False, True])
def test_cloning_host_handler_preserves_connection_lifecycle(offline_driver, diagnostics, failed):
    copied = []

    class CloningHandler(logging.Handler):
        def emit(self, record):
            copied.append(logging.makeLogRecord(record.__dict__))

    handler = CloningHandler()
    root = logging.getLogger()
    previous = logging.getLogRecordFactory()
    root.addHandler(handler)
    connection = offline_driver.connection
    try:
        try:
            with postgres.connection_lifecycle(lambda: connection, psycopg):
                logging.getLogger("host.application").warning("host operation started")
                logging.getLogger("psycopg.transaction").warning("driver detail %s", SECRET)
                if failed:
                    raise driver_error("operation")
        except ValidationFailed as error:
            assert failed
            assert error.fields == {"backend": "postgresql", "sqlstate": "40001"}
        else:
            assert not failed
    finally:
        root.removeHandler(handler)
    assert connection.closed_count == 1
    assert [(record.name, record.getMessage()) for record in copied] == [
        ("host.application", "host operation started"),
        ("psycopg.transaction", "PostgreSQL driver diagnostic redacted"),
    ]
    assert logging.getLogRecordFactory() is previous
    assert_redacted(diagnostics())
