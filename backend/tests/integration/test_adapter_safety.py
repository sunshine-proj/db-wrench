"""Adapter-level guarantees that do not depend on the HTTP layer."""

from __future__ import annotations

import time
import uuid
from typing import Any

import psycopg
import pymysql
import pytest

from app.connections.network import NetworkPolicy
from app.database.adapters.mariadb import MariaDBAdapter
from app.database.adapters.mongodb import MongoAdapter
from app.database.adapters.mysql import MySQLAdapter
from app.database.adapters.postgres import PostgresAdapter
from app.database.core.base import ConnectionSpec, DbType, Limits, TlsMode
from app.errors import (
    ConnectionFailed,
    NetworkPolicyViolation,
    QueryFailed,
    StatementTimeout,
)

LOOPBACK_OK = NetworkPolicy(allow_loopback=True)


def spec(db_type: DbType, info: dict[str, Any], **over: Any) -> ConnectionSpec:
    return ConnectionSpec(
        id=uuid.uuid4(),
        db_type=db_type,
        host=info["host"],
        port=info["port"],
        database=info.get("database"),
        username=info["username"],
        password=over.pop("password", info["password"]),
        options=info.get("options", {}),
        pool_max=2,
        **over,
    )


@pytest.fixture
def pg(pg_managed):
    a = PostgresAdapter(spec(DbType.POSTGRESQL, pg_managed), LOOPBACK_OK, Limits())
    yield a
    a.close()


@pytest.fixture
def my(mysql_managed):
    a = MySQLAdapter(spec(DbType.MYSQL, mysql_managed), LOOPBACK_OK, Limits())
    yield a
    a.close()


@pytest.fixture
def maria(mariadb_managed):
    a = MariaDBAdapter(spec(DbType.MARIADB, mariadb_managed), LOOPBACK_OK, Limits())
    yield a
    a.close()


def secret_sink_unchanged_pg(pg_container) -> bool:
    from tests.integration.conftest import pg_dsn

    with psycopg.connect(**pg_dsn(pg_container, "shop")) as c:
        return c.execute("SELECT v FROM secret_sink").fetchall() == [("must-not-change",)]


@pytest.mark.security
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO secret_sink VALUES (2, 'pwned')",
        "UPDATE secret_sink SET v = 'pwned'",
        "DELETE FROM secret_sink",
        "DROP TABLE secret_sink",
        "CREATE TABLE evil (x int)",
        "TRUNCATE secret_sink",
    ],
)
def test_postgres_writes_are_refused_by_the_database(pg, pg_container, sql):
    with pytest.raises(QueryFailed, match="read-only"):
        pg._query("shop", sql, [])
    assert secret_sink_unchanged_pg(pg_container)


@pytest.mark.security
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO secret_sink VALUES (2, 'pwned')",
        "UPDATE secret_sink SET v = 'pwned'",
        "DELETE FROM secret_sink",
        "TRUNCATE TABLE secret_sink",
    ],
)
def test_mysql_dml_is_refused_by_the_database(my, mysql_container, sql):
    with pytest.raises(QueryFailed):
        my._query("shop", sql, [])
    conn = pymysql.connect(
        host=mysql_container.get_container_host_ip(),
        port=int(mysql_container.get_exposed_port(3306)),
        user="root", password="rootpw", database="shop",
    )  # fmt: skip
    with conn.cursor() as cur:
        cur.execute("SELECT v FROM secret_sink")
        assert cur.fetchall() == (("must-not-change",),)
    conn.close()


@pytest.mark.security
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO secret_sink VALUES (2, 'pwned')",
        "UPDATE secret_sink SET v = 'pwned'",
        "DELETE FROM secret_sink",
        "TRUNCATE TABLE secret_sink",
    ],
)
def test_mariadb_dml_is_refused_by_the_database(maria, mariadb_container, sql):
    with pytest.raises(QueryFailed):
        maria._query("shop", sql, [])
    conn = pymysql.connect(
        host=mariadb_container.get_container_host_ip(),
        port=int(mariadb_container.get_exposed_port(3306)),
        user="root", password="rootpw", database="shop",
    )  # fmt: skip
    with conn.cursor() as cur:
        cur.execute("SELECT v FROM secret_sink")
        assert cur.fetchall() == (("must-not-change",),)
    conn.close()


def test_postgres_statement_timeout(pg_managed):
    a = PostgresAdapter(
        spec(DbType.POSTGRESQL, pg_managed), LOOPBACK_OK, Limits(statement_timeout_s=1)
    )
    try:
        started = time.monotonic()
        with pytest.raises(StatementTimeout):
            a._query("shop", "SELECT pg_sleep(5)", [])
        assert time.monotonic() - started < 4
        # the pooled connection is still usable afterwards
        assert a._query("shop", "SELECT 1", [])[1] == [(1,)]
    finally:
        a.close()


def test_mysql_statement_timeout(mysql_managed):
    a = MySQLAdapter(spec(DbType.MYSQL, mysql_managed), LOOPBACK_OK, Limits(statement_timeout_s=1))
    try:
        started = time.monotonic()
        with pytest.raises(StatementTimeout):
            a._query(
                "shop",
                "SELECT COUNT(*) FROM customers a, customers b, customers c, customers d "
                "WHERE MD5(a.id + b.id + c.id + d.id) LIKE 'abc%%'",
                [],
            )
        assert time.monotonic() - started < 5
    finally:
        a.close()


def test_mariadb_statement_timeout(mariadb_managed):
    a = MariaDBAdapter(
        spec(DbType.MARIADB, mariadb_managed), LOOPBACK_OK, Limits(statement_timeout_s=1)
    )
    try:
        started = time.monotonic()
        with pytest.raises(StatementTimeout):
            a._query("shop", "SELECT SLEEP(5)", [])
        assert time.monotonic() - started < 4
        # max_statement_time also bounds statements other than SELECT-only sleeps, and the
        # pooled connection stays usable afterwards
        assert a._query("shop", "SELECT 1", [])[1] == [(1,)]
    finally:
        a.close()


@pytest.mark.security
@pytest.mark.parametrize("engine", ["pg", "mysql", "mariadb", "mongo"])
def test_wrong_credentials_fail_cleanly_without_leaking_the_password(
    engine, pg_managed, mysql_managed, mariadb_managed, mongo_managed
):
    bad_pw = "Wr0ng-Credential-Value"
    cases = {
        "pg": (PostgresAdapter, DbType.POSTGRESQL, pg_managed),
        "mysql": (MySQLAdapter, DbType.MYSQL, mysql_managed),
        "mariadb": (MariaDBAdapter, DbType.MARIADB, mariadb_managed),
        "mongo": (MongoAdapter, DbType.MONGODB, mongo_managed),
    }
    cls, typ, info = cases[engine]
    a = cls(spec(typ, info, password=bad_pw), LOOPBACK_OK, Limits())
    try:
        result = a.test()
        assert result.ok is False and result.error_code == "connection_failed"
        assert bad_pw not in (result.error or "")
    finally:
        a.close()


@pytest.mark.security
@pytest.mark.parametrize("engine", ["pg", "mysql", "mariadb", "mongo"])
def test_network_policy_is_enforced_when_connecting(
    engine, pg_managed, mysql_managed, mariadb_managed, mongo_managed
):
    """The adapters re-check the policy on every new physical connection (anti-rebinding)."""
    cases = {
        "pg": (PostgresAdapter, DbType.POSTGRESQL, pg_managed),
        "mysql": (MySQLAdapter, DbType.MYSQL, mysql_managed),
        "mariadb": (MariaDBAdapter, DbType.MARIADB, mariadb_managed),
        "mongo": (MongoAdapter, DbType.MONGODB, mongo_managed),
    }
    cls, typ, info = cases[engine]
    strict = NetworkPolicy(allow_cidrs=("10.0.0.0/8",), allow_loopback=False)
    a = cls(spec(typ, info), strict, Limits())
    try:
        result = a.test()
        assert result.ok is False and result.error_code == "network_policy_denied"
        with pytest.raises(NetworkPolicyViolation):
            a.list_databases()
    finally:
        a.close()


def test_connections_are_pinned_to_the_validated_address(pg_managed, monkeypatch):
    """psycopg is given hostaddr (validated IP) so later DNS answers cannot redirect it."""
    seen: dict[str, Any] = {}
    real = psycopg.connect

    def spy(*args: Any, **kw: Any):
        seen.update(kw)
        return real(*args, **kw)

    monkeypatch.setattr("app.database.adapters.postgres.psycopg.connect", spy)
    s = spec(DbType.POSTGRESQL, {**pg_managed, "host": "localhost"})
    a = PostgresAdapter(s, LOOPBACK_OK, Limits())
    try:
        assert a.test().ok
        assert seen["hostaddr"] in ("127.0.0.1", "::1") and seen["host"] == "localhost"
        assert "default_transaction_read_only=on" in seen["options"]
    finally:
        a.close()


def test_pool_is_bounded_per_connection(pg_managed):
    a = PostgresAdapter(spec(DbType.POSTGRESQL, pg_managed), LOOPBACK_OK, Limits())
    try:
        e = a._engine("shop")
        assert e.pool.size() == 2  # pool_max from the spec
    finally:
        a.close()


def test_adapter_manager_replaces_adapter_when_connection_changes(pg_managed):
    from app.database.core.manager import AdapterManager

    m = AdapterManager()
    m.register(DbType.POSTGRESQL, PostgresAdapter)
    s1 = spec(DbType.POSTGRESQL, pg_managed, version_key="v1")
    a1 = m.get(s1, LOOPBACK_OK, Limits())
    assert m.get(s1, LOOPBACK_OK, Limits()) is a1
    s2 = ConnectionSpec(**{**s1.__dict__, "version_key": "v2"})
    a2 = m.get(s2, LOOPBACK_OK, Limits())
    assert a2 is not a1
    # a policy change also yields a fresh adapter
    assert m.get(s2, NetworkPolicy(allow_cidrs=("10.0.0.0/8",)), Limits()) is not a2
    m.close_all()


def test_tls_modes_are_defined():
    assert {m.value for m in TlsMode} == {"disable", "require", "verify-ca", "verify-full"}


def test_connection_failure_to_closed_port_is_reported_not_raised():
    closed = {"host": "127.0.0.1", "port": 1, "username": "u", "password": "p", "database": "x"}
    a = PostgresAdapter(spec(DbType.POSTGRESQL, closed, connect_timeout_s=2), LOOPBACK_OK, Limits())
    try:
        assert a.test().ok is False
        with pytest.raises(ConnectionFailed):
            a.list_databases()
    finally:
        a.close()


@pytest.mark.security
def test_postgres_read_only_cannot_be_switched_off_by_sql(pg, pg_container):
    """ADR-0008: `SET TRANSACTION READ WRITE` as the first statement of a read-only
    transaction re-enables writes (and non-transactional side effects such as nextval()).
    The adapter primes every transaction so the escape is refused by the server itself."""
    with pytest.raises(QueryFailed, match="must be set before any query"):
        pg._query(
            "shop",
            "SET TRANSACTION READ WRITE; SELECT nextval('ticket_seq')",
            [],
        )
    with pytest.raises(QueryFailed, match="must be set before any query"):
        pg._query("shop", "SET TRANSACTION READ WRITE", [])
    # the escape would also have advanced the sequence (a side effect that survives rollback)
    assert pg._query("shop", "SELECT last_value, is_called FROM ticket_seq", [])[1] == [
        (100, False)
    ]
    assert secret_sink_unchanged_pg(pg_container)


@pytest.mark.security
def test_mysql_driver_rejects_multi_statement_input(my):
    with pytest.raises(QueryFailed):
        my._query("shop", "SELECT 1; SELECT 2", [])


@pytest.mark.security
def test_mariadb_driver_rejects_multi_statement_input(maria):
    with pytest.raises(QueryFailed):
        maria._query("shop", "SELECT 1; SELECT 2", [])
