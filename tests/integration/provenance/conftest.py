"""Opt-in real PostgreSQL fixtures. All mutation controls are test-only.

The configured service must allow CREATE DATABASE. Each clone owns a newly named
database; teardown drops only databases created by this fixture invocation.
Credential files are resolved by libpq and are never read or printed here.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from vcp.provenance import postgres
from vcp.provenance.backend import BackendConfig, BackendName

NOT_CONFIGURED = "PostgreSQL integration service is not configured"


def configured_driver():
    service = os.environ.get("VCP_TEST_PG_SERVICE")
    if not service or any(
        not os.environ.get(key) or not Path(os.environ[key]).is_file()
        for key in ("PGSERVICEFILE", "PGPASSFILE")
    ):
        pytest.skip(NOT_CONFIGURED)
    try:
        driver = import_module("psycopg")
    except ImportError:
        pytest.skip(NOT_CONFIGURED)
    try:
        postgres.validate_pg_service(service)
    except Exception:
        pytest.fail("Invalid PostgreSQL integration service name", pytrace=False)
    return driver, service


class ObservedCursor:
    def __init__(self, cursor, connection):
        self.cursor = cursor
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def executemany(self, query, rows):
        self.connection.observe(query, "before")
        result = self.cursor.executemany(query, rows)
        self.connection.observe(query, "after")
        return result


class ObservedConnection:
    def __init__(self, raw, harness):
        self.raw = raw
        self.harness = harness

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def observe(self, query, stage):
        if self.harness.hook is not None:
            self.harness.hook(" ".join(str(query).split()), stage, self.raw)

    def execute(self, query, params=()):
        self.observe(query, "before")
        result = self.raw.execute(query, params)
        self.observe(query, "after")
        return result

    @contextmanager
    def cursor(self):
        with self.raw.cursor() as cursor:
            yield ObservedCursor(cursor, self)


class PostgresHarness:
    def __init__(self, factory, database):
        self.factory = factory
        self.database = database
        self.hook = None
        self.backend = postgres.PostgresProvenanceBackend(
            BackendConfig(name=BackendName.POSTGRESQL, pg_service=factory.service)
        )

    def fresh_clone(self):
        """Create an independent empty backend to replay the same canonical inputs."""
        return self.factory.create()

    def connect(self):
        return self.factory.connect(self.database)

    def reader_generation(self, connection=None):
        if connection is not None:
            return connection.execute(
                "SELECT generation_id FROM vcp_provenance.active_generation WHERE singleton"
            ).fetchone()[0]
        with self.connect() as reader:
            return self.reader_generation(reader)

    def snapshot(self):
        """Compare every derived table, including generations/checkpoints/decisions."""
        with self.connect() as reader, reader.transaction():
            reader.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            tables = reader.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='vcp_provenance' "
                "ORDER BY tablename"
            ).fetchall()
            return {
                table: reader.execute(
                    self.factory.driver.sql.SQL(
                        "SELECT to_jsonb(t)::text FROM vcp_provenance.{} AS t ORDER BY 1"
                    ).format(self.factory.driver.sql.Identifier(table))
                ).fetchall()
                for (table,) in tables
            }

    @contextmanager
    def paused_before_publish(self, roots):
        """Pause a real writer immediately before the active pointer SQL executes."""
        reached, release = Event(), Event()

        def pause(query, stage, connection):
            if stage == "before" and query.startswith(
                "INSERT INTO vcp_provenance.active_generation"
            ):
                reached.set()
                if not release.wait(20):
                    raise RuntimeError("integration publication pause timed out")

        previous = self.hook
        self.hook = pause
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.backend.rebuild, roots.data, roots.configs)
            try:
                if not reached.wait(20):
                    if future.done():
                        future.result()
                    pytest.fail("writer did not reach publication boundary")
                yield
            finally:
                release.set()
                try:
                    future.result(timeout=25)
                finally:
                    self.hook = previous

    @contextmanager
    def fail_after(self, table):
        previous = self.hook
        fired = Event()

        def fail(query, stage, connection):
            if stage == "after" and query.startswith(f"INSERT INTO vcp_provenance.{table}"):
                fired.set()
                raise RuntimeError(f"injected rollback after {table}")

        self.hook = fail
        try:
            yield fired
        finally:
            self.hook = previous


class HarnessFactory:
    def __init__(self, driver, service):
        self.driver, self.service = driver, service
        self.clones = []

    def connect(self, database=None):
        kwargs = {"service": self.service, "autocommit": True, "connect_timeout": 5}
        if database is not None:
            kwargs["dbname"] = database
        connection = None
        try:
            connection = self.driver.connect(**kwargs)
            connection.execute("SET statement_timeout='15s'")
            return connection
        except self.driver.Error:
            if connection is not None:
                connection.close()
            pytest.fail(
                "PostgreSQL integration connection failed (details redacted)", pytrace=False
            )

    def create(self):
        database = "vcp_test_" + uuid4().hex
        with self.connect() as admin:
            admin.execute(
                self.driver.sql.SQL("CREATE DATABASE {}").format(
                    self.driver.sql.Identifier(database)
                )
            )
        # Register ownership immediately, even if backend construction subsequently fails.
        self.clones.append((database, None))
        harness = PostgresHarness(self, database)
        self.clones[-1] = (database, harness)
        return harness

    def close(self):
        failures = []
        for database, _ in reversed(self.clones):
            try:
                with self.connect() as admin:
                    admin.execute(
                        self.driver.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            self.driver.sql.Identifier(database)
                        )
                    )
            except (self.driver.Error, pytest.fail.Exception):
                failures.append(database)
        if failures:
            pytest.fail("PostgreSQL integration database cleanup failed", pytrace=False)


@pytest.fixture
def postgres_harness(monkeypatch):
    driver, service = configured_driver()
    factory = HarnessFactory(driver, service)
    original = postgres._connect

    def connect(config):
        for _, clone in factory.clones:
            if clone is not None and config is clone.backend.config:
                return ObservedConnection(clone.connect(), clone)
        return original(config)

    monkeypatch.setattr(postgres, "_connect", connect)
    try:
        yield factory.create()
    finally:
        factory.close()


@pytest.fixture
def postgres_backend(postgres_harness):
    return postgres_harness.backend
