"""Integration fixtures: real PostgreSQL / MySQL / MongoDB via testcontainers.

Image versions are overridable (CI runs the full supported-version matrix):
``DBW_TEST_PG_IMAGE``, ``DBW_TEST_MYSQL_IMAGE``, ``DBW_TEST_MARIADB_IMAGE``,
``DBW_TEST_MSSQL_IMAGE``, ``DBW_TEST_REDIS_IMAGE``, ``DBW_TEST_MONGO_IMAGE``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pymongo
import pymysql
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from bson import Binary, Decimal128, Int64, ObjectId
from testcontainers.mongodb import MongoDbContainer
from testcontainers.mssql import SqlServerContainer
from testcontainers.mysql import MySqlContainer
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

PG_IMAGE = os.getenv("DBW_TEST_PG_IMAGE", "postgres:17.6-alpine")
MYSQL_IMAGE = os.getenv("DBW_TEST_MYSQL_IMAGE", "mysql:8.4")
MARIADB_IMAGE = os.getenv("DBW_TEST_MARIADB_IMAGE", "mariadb:11.4")
MSSQL_IMAGE = os.getenv("DBW_TEST_MSSQL_IMAGE", "mcr.microsoft.com/mssql/server:2022-latest")
MSSQL_PASSWORD = "Rootpw!12345"
REDIS_IMAGE = os.getenv("DBW_TEST_REDIS_IMAGE", "redis:7")
REDIS_PASSWORD = "redispw-123"
CLICKHOUSE_IMAGE = os.getenv("DBW_TEST_CLICKHOUSE_IMAGE", "clickhouse/clickhouse-server:24.8")
CLICKHOUSE_PASSWORD = "chpw-123"
ES_IMAGE = os.getenv("DBW_TEST_ES_IMAGE", "docker.elastic.co/elasticsearch/elasticsearch:8.15.3")
ES_PASSWORD = "espw-12345"
SQLITE_ROOT = Path(tempfile.mkdtemp(prefix="dbw-sqlite-")).resolve()
MONGO_IMAGE = os.getenv("DBW_TEST_MONGO_IMAGE", "mongo:7.0")
BACKEND = Path(__file__).resolve().parents[2]

ADMIN_PASSWORD = "Adm1n-Passw0rd-Long!"
USER_PASSWORD = "Us3r-Passw0rd-Long!"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


# ------------------------------------------------------------------ containers


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        PG_IMAGE, username="postgres", password="pw", dbname="appdb"
    ).with_command("postgres -c max_connections=400") as c:
        yield c


@pytest.fixture(scope="session")
def mysql_container() -> Iterator[MySqlContainer]:
    with MySqlContainer(MYSQL_IMAGE, root_password="rootpw", dbname="seed") as c:
        yield c


@pytest.fixture(scope="session")
def mariadb_container() -> Iterator[MySqlContainer]:
    # The MySQL testcontainer's readiness probe only needs the wire protocol; MariaDB images take
    # MARIADB_* variables, which the helper does not set, so configure them explicitly.
    c = MySqlContainer(MARIADB_IMAGE, root_password="rootpw", dbname="seed")
    c.with_env("MARIADB_ROOT_PASSWORD", "rootpw").with_env("MARIADB_DATABASE", "seed")
    with c:
        yield c


@pytest.fixture(scope="session")
def mssql_container() -> Iterator[SqlServerContainer]:
    with SqlServerContainer(MSSQL_IMAGE, password=MSSQL_PASSWORD, dbname="master") as c:
        yield c


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer(REDIS_IMAGE, password=REDIS_PASSWORD) as c:
        yield c


@pytest.fixture(scope="session")
def clickhouse_container() -> Iterator[Any]:
    import time

    from testcontainers.core.container import DockerContainer

    c = (
        DockerContainer(CLICKHOUSE_IMAGE)
        .with_env("CLICKHOUSE_USER", "default")
        .with_env("CLICKHOUSE_PASSWORD", CLICKHOUSE_PASSWORD)
        .with_env("CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT", "1")
        .with_exposed_ports(8123)
    )
    with c:
        host, port = c.get_container_host_ip(), int(c.get_exposed_port(8123))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://{host}:{port}/ping", timeout=2).text.strip() == "Ok.":
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("ClickHouse did not become ready")
        yield c


@pytest.fixture(scope="session")
def es_container() -> Iterator[Any]:
    import time

    from testcontainers.core.container import DockerContainer

    opensearch = "opensearch" in ES_IMAGE
    c = DockerContainer(ES_IMAGE).with_exposed_ports(9200).with_env("discovery.type", "single-node")
    if opensearch:
        c = (
            c.with_env("OPENSEARCH_INITIAL_ADMIN_PASSWORD", ES_PASSWORD)
            .with_env("DISABLE_SECURITY_PLUGIN", "true")
            .with_env("OPENSEARCH_JAVA_OPTS", "-Xms512m -Xmx512m")
        )
    else:
        c = (
            c.with_env("xpack.security.enabled", "true")
            .with_env("xpack.security.http.ssl.enabled", "false")
            .with_env("ELASTIC_PASSWORD", ES_PASSWORD)
            .with_env("xpack.ml.enabled", "false")
            .with_env("ES_JAVA_OPTS", "-Xms512m -Xmx512m")
        )
    with c:
        host, port = c.get_container_host_ip(), int(c.get_exposed_port(9200))
        auth = None if opensearch else ("elastic", ES_PASSWORD)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"http://{host}:{port}/_cluster/health", auth=auth, timeout=3)
                if r.status_code == 200 and r.json().get("status") in ("green", "yellow"):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            raise RuntimeError("Elasticsearch did not become ready")
        yield c


@pytest.fixture(scope="session")
def mongo_container() -> Iterator[MongoDbContainer]:
    with MongoDbContainer(MONGO_IMAGE, username="root", password="rootpw") as c:
        yield c


def pg_dsn(c: PostgresContainer, db: str) -> dict[str, Any]:
    return {
        "host": c.get_container_host_ip(),
        "port": int(c.get_exposed_port(5432)),
        "user": "postgres",
        "password": "pw",
        "dbname": db,
    }


# ------------------------------------------------------------------ application database


@pytest.fixture(scope="session")
def app_db(pg_container: PostgresContainer) -> str:
    d = pg_dsn(pg_container, "appdb")
    url = f"postgresql+psycopg://{d['user']}:{d['password']}@{d['host']}:{d['port']}/appdb"
    os.environ["DBW_DATABASE_URL"] = url
    os.environ["DBW_SQLITE_ROOT"] = str(SQLITE_ROOT)
    from app.config import get_settings

    get_settings.cache_clear()
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    command.upgrade(cfg, "head")
    return url


@pytest.fixture(scope="session")
def app_sql(pg_container: PostgresContainer, app_db: str):
    """Run SQL as the superuser directly against the application DB (test inspection only)."""

    def run(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with psycopg.connect(**pg_dsn(pg_container, "appdb"), autocommit=True) as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    return run


@pytest_asyncio.fixture(scope="session")
async def setup_token(app_db: str) -> str:
    from app.auth import service
    from app.db import session_factory

    async with session_factory()() as db, db.begin():
        await service.ensure_builtin_roles(db)
        token = await service.ensure_setup_token(db)
    assert token, "first-run setup token should be created on an empty database"
    return token


# ------------------------------------------------------------------ HTTP client helper


class Api:
    def __init__(self, app: Any):
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.csrf: str | None = None
        self.user: dict[str, Any] | None = None

    async def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = dict(kw.pop("headers", {}))
        if self.csrf and method not in ("GET", "HEAD", "OPTIONS"):
            headers.setdefault("X-CSRF-Token", self.csrf)
        url = path if path.startswith("/") else f"/api/v1/{path}"
        return await self.http.request(method, url, headers=headers, **kw)

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.request("POST", path, json=json, **kw)

    async def patch(self, path: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.request("PATCH", path, json=json, **kw)

    async def put(self, path: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.request("PUT", path, json=json, **kw)

    async def delete(self, path: str, **kw: Any) -> httpx.Response:
        return await self.request("DELETE", path, **kw)

    async def login(self, username: str, password: str) -> httpx.Response:
        r = await self.post("auth/login", {"username": username, "password": password})
        if r.status_code == 200:
            self.csrf = r.json()["csrf_token"]
            self.user = r.json()["user"]
        return r

    async def aclose(self) -> None:
        await self.http.aclose()


@pytest_asyncio.fixture(scope="session")
async def asgi_app(app_db: str, setup_token: str) -> Any:
    from app.main import app

    return app


@pytest_asyncio.fixture(scope="session")
async def admin(asgi_app: Any, setup_token: str) -> AsyncIterator[Api]:
    api = Api(asgi_app)
    r = await api.post(
        "auth/setup",
        {
            "token": setup_token,
            "username": "admin",
            "password": ADMIN_PASSWORD,
            "email": "a@example.com",
        },
    )
    assert r.status_code == 201, r.text
    r = await api.login("admin", ADMIN_PASSWORD)
    assert r.status_code == 200, r.text
    yield api
    await api.aclose()


@pytest_asyncio.fixture
async def anon(asgi_app: Any) -> AsyncIterator[Api]:
    api = Api(asgi_app)
    yield api
    await api.aclose()


@pytest_asyncio.fixture
async def new_user(admin: Api, asgi_app: Any, app_sql):
    """Factory: create an active user with the given built-in role and log them in."""
    made: list[Api] = []

    async def factory(
        role: str = "Viewer", *, password: str = USER_PASSWORD, login: bool = True
    ) -> Api:
        roles = {r["name"]: r for r in (await admin.get("roles")).json()}
        name = f"u{uuid.uuid4().hex[:10]}"
        r = await admin.post(
            "users", {"username": name, "password": password, "role_id": roles[role]["id"]}
        )
        assert r.status_code == 201, r.text
        app_sql("UPDATE users SET must_change_password = false WHERE username = %s", (name,))
        api = Api(asgi_app)
        made.append(api)
        api.username = name  # type: ignore[attr-defined]
        api.user_id = r.json()["id"]  # type: ignore[attr-defined]
        if login:
            lr = await api.login(name, password)
            assert lr.status_code == 200, lr.text
        return api

    yield factory
    for a in made:
        await a.aclose()


# ------------------------------------------------------------------ managed databases


PG_SETUP = """
CREATE TABLE customers (
    id serial PRIMARY KEY, name text NOT NULL, email text UNIQUE, balance numeric(12,2),
    big bigint, meta jsonb, created_at timestamptz DEFAULT now(), photo bytea, notes text
);
INSERT INTO customers (name, email, balance, big, meta, photo, notes)
SELECT 'Customer ' || lpad(g::text, 3, '0'),
       CASE WHEN g % 10 = 0 THEN NULL ELSE 'c' || g || '@example.com' END,
       g * 10.25, 9007199254740993 + g, jsonb_build_object('n', g, 'tags', jsonb_build_array('a','b')),
       decode('deadbeef', 'hex'), CASE WHEN g = 1 THEN repeat('x', 6000) END
FROM generate_series(1, 250) g;
CREATE TABLE orders (id bigserial PRIMARY KEY, customer_id int REFERENCES customers(id) ON DELETE CASCADE,
    total numeric, placed_at date);
INSERT INTO orders (customer_id, total, placed_at) VALUES (1, 10, '2024-01-01'), (2, 20, '2024-01-02');
CREATE TABLE order_items (order_id int, line_no int, sku text, qty int, PRIMARY KEY (order_id, line_no));
INSERT INTO order_items SELECT 1, g, 'sku' || g, g FROM generate_series(1, 5) g;
CREATE TABLE no_pk (a int, b text);
INSERT INTO no_pk VALUES (1, 'x'), (2, 'y');
CREATE TABLE "Weird %Table" ("a%b" int PRIMARY KEY, "q""uote" text);
INSERT INTO "Weird %Table" VALUES (1, 'a'), (2, 'b');
CREATE VIEW vip AS SELECT id, name FROM customers WHERE balance > 2000;
CREATE SEQUENCE ticket_seq START 100 INCREMENT 5;
CREATE FUNCTION add_one(i int) RETURNS int LANGUAGE sql AS 'SELECT i + 1';
CREATE SCHEMA extra;
CREATE TABLE extra.notes (id int PRIMARY KEY, body text);
CREATE INDEX customers_name_idx ON customers (name);
CREATE TABLE secret_sink (id int PRIMARY KEY, v text);
INSERT INTO secret_sink VALUES (1, 'must-not-change');
"""


@pytest.fixture(scope="session")
def pg_managed(pg_container: PostgresContainer) -> dict[str, Any]:
    with psycopg.connect(**pg_dsn(pg_container, "appdb"), autocommit=True) as conn:
        conn.execute("CREATE DATABASE shop")
        conn.execute("CREATE ROLE reader LOGIN PASSWORD 'readerpw'")
    with psycopg.connect(**pg_dsn(pg_container, "shop"), autocommit=True) as conn:
        conn.execute(PG_SETUP)
        conn.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO reader")
    d = pg_dsn(pg_container, "shop")
    return {"host": d["host"], "port": d["port"], "username": "postgres", "password": "pw",
            "database": "shop"}  # fmt: skip


MYSQL_SETUP = [
    "CREATE DATABASE shop",
    "USE shop",
    """CREATE TABLE customers (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100) NOT NULL,
        email VARCHAR(200) UNIQUE, balance DECIMAL(12,2), big BIGINT UNSIGNED, meta JSON,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, photo BLOB, notes TEXT)""",
    """INSERT INTO customers (name, email, balance, big, meta, photo, notes)
       WITH RECURSIVE seq(g) AS (SELECT 1 UNION ALL SELECT g + 1 FROM seq WHERE g < 250)
       SELECT CONCAT('Customer ', LPAD(g, 3, '0')),
              IF(g % 10 = 0, NULL, CONCAT('c', g, '@example.com')),
              g * 10.25, 9007199254740993 + g, JSON_OBJECT('n', g), UNHEX('DEADBEEF'),
              IF(g = 1, REPEAT('x', 6000), NULL) FROM seq""",
    """CREATE TABLE orders (id BIGINT AUTO_INCREMENT PRIMARY KEY, customer_id INT, total DECIMAL(10,2),
        CONSTRAINT fk_cust FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
        ON UPDATE RESTRICT, CONSTRAINT chk_total CHECK (total >= 0))""",
    "INSERT INTO orders (customer_id, total) VALUES (1, 10), (2, 20)",
    """CREATE TABLE order_items (order_id INT, line_no INT, sku VARCHAR(20), qty INT,
        PRIMARY KEY (order_id, line_no))""",
    "INSERT INTO order_items VALUES (1,1,'a',1),(1,2,'b',2),(1,3,'c',3)",
    "CREATE TABLE no_pk (a INT, b VARCHAR(10))",
    "INSERT INTO no_pk VALUES (1,'x'),(2,'y')",
    "CREATE TABLE `Weird %Table` (`a%b` INT PRIMARY KEY, `q``uote` VARCHAR(10))",
    "INSERT INTO `Weird %Table` VALUES (1,'a'),(2,'b')",
    "CREATE VIEW vip AS SELECT id, name FROM customers WHERE balance > 2000",
    "CREATE INDEX customers_name_idx ON customers (name)",
    "CREATE TABLE secret_sink (id INT PRIMARY KEY, v VARCHAR(20))",
    "INSERT INTO secret_sink VALUES (1, 'must-not-change')",
]


@pytest.fixture(scope="session")
def mysql_managed(mysql_container: MySqlContainer) -> dict[str, Any]:
    host = mysql_container.get_container_host_ip()
    port = int(mysql_container.get_exposed_port(3306))
    conn = pymysql.connect(host=host, port=port, user="root", password="rootpw", autocommit=True)
    with conn.cursor() as cur:
        for stmt in MYSQL_SETUP:
            cur.execute(stmt)
    conn.close()
    return {
        "host": host,
        "port": port,
        "username": "root",
        "password": "rootpw",
        "database": "shop",
    }


@pytest.fixture(scope="session")
def mariadb_managed(mariadb_container: MySqlContainer) -> dict[str, Any]:
    host = mariadb_container.get_container_host_ip()
    port = int(mariadb_container.get_exposed_port(3306))
    conn = pymysql.connect(host=host, port=port, user="root", password="rootpw", autocommit=True)
    with conn.cursor() as cur:
        for stmt in MYSQL_SETUP:
            cur.execute(stmt)
    conn.close()
    return {"host": host, "port": port, "username": "root", "password": "rootpw",
            "database": "shop"}  # fmt: skip


MSSQL_SETUP = [
    "CREATE DATABASE shop",
    "USE shop",
    """CREATE TABLE customers (id INT IDENTITY(1,1) PRIMARY KEY, name NVARCHAR(100) NOT NULL,
        email NVARCHAR(200) NULL, balance DECIMAL(12,2), big BIGINT, notes NVARCHAR(MAX),
        created_at DATETIME2 DEFAULT SYSUTCDATETIME(), photo VARBINARY(MAX))""",
    "CREATE UNIQUE INDEX customers_email_uq ON customers (email) WHERE email IS NOT NULL",
    """WITH seq AS (SELECT TOP 250 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS g
                   FROM sys.all_objects a CROSS JOIN sys.all_objects b)
       INSERT INTO customers (name, email, balance, big, photo, notes)
       SELECT 'Customer ' + RIGHT('000' + CAST(g AS VARCHAR(5)), 3),
              CASE WHEN g % 10 = 0 THEN NULL ELSE 'c' + CAST(g AS VARCHAR(5)) + '@example.com' END,
              g * 10.25, 9007199254740993 + g, 0xDEADBEEF,
              CASE WHEN g = 1 THEN REPLICATE(CAST('x' AS NVARCHAR(MAX)), 6000) END FROM seq""",
    """CREATE TABLE orders (id BIGINT IDENTITY(1,1) PRIMARY KEY, customer_id INT, total DECIMAL(10,2),
        CONSTRAINT fk_cust FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE,
        CONSTRAINT chk_total CHECK (total >= 0))""",
    "INSERT INTO orders (customer_id, total) VALUES (1, 10), (2, 20)",
    """CREATE TABLE order_items (order_id INT, line_no INT, sku VARCHAR(20), qty INT,
        PRIMARY KEY (order_id, line_no))""",
    "INSERT INTO order_items VALUES (1,1,'a',1),(1,2,'b',2),(1,3,'c',3)",
    "CREATE TABLE no_pk (a INT, b VARCHAR(10))",
    "INSERT INTO no_pk VALUES (1,'x'),(2,'y')",
    "CREATE TABLE [Weird %Table] ([a%b] INT PRIMARY KEY, [q]]uote] VARCHAR(10))",
    "INSERT INTO [Weird %Table] VALUES (1,'a'),(2,'b')",
    "CREATE VIEW vip AS SELECT id, name FROM customers WHERE balance > 2000",
    "CREATE INDEX customers_name_idx ON customers (name)",
    "CREATE TABLE secret_sink (id INT PRIMARY KEY, v VARCHAR(20))",
    "INSERT INTO secret_sink VALUES (1, 'must-not-change')",
]


def mssql_exec(info: dict[str, Any], stmt: str, database: str | None = None) -> list[tuple]:
    import pymssql

    conn = pymssql.connect(
        server=info["host"], port=str(info["port"]), user=info["username"],
        password=info["password"], database=database or "master", autocommit=True,
        login_timeout=20,
    )  # fmt: skip
    try:
        cur = conn.cursor()
        cur.execute(stmt)
        return list(cur.fetchall()) if cur.description else []
    finally:
        conn.close()


@pytest.fixture(scope="session")
def mssql_managed(mssql_container: SqlServerContainer) -> dict[str, Any]:
    info = {
        "host": mssql_container.get_container_host_ip(),
        "port": int(mssql_container.get_exposed_port(1433)),
        "username": "sa",
        "password": MSSQL_PASSWORD,
        "database": "shop",
    }
    for stmt in MSSQL_SETUP:
        if stmt.startswith("USE "):
            continue
        mssql_exec(info, stmt, "shop" if "CREATE DATABASE" not in stmt else None)
    return info


@pytest.fixture(scope="session")
def redis_managed(redis_container: RedisContainer) -> dict[str, Any]:
    import redis as redis_lib

    info = {
        "host": redis_container.get_container_host_ip(),
        "port": int(redis_container.get_exposed_port(6379)),
        "username": "",
        "password": REDIS_PASSWORD,
        "database": "0",
    }
    r = redis_lib.Redis(host=info["host"], port=info["port"], password=REDIS_PASSWORD)
    pipe = r.pipeline()
    for i in range(300):
        pipe.set(f"user:{i:04d}", f"value-{i}")
    pipe.set("session:ttl", "x", ex=3600)
    pipe.set("blob:bin", b"\xff\xfe\x00\x01")
    pipe.set("big:text", "y" * 20000)
    pipe.set(b"\xde\xad\xbe\xef", "binary-key")
    pipe.hset("h:profile", mapping={"name": "Ann", "city": "Paris"})
    pipe.rpush("l:queue", "a", "b", "c")
    pipe.sadd("s:tags", "x", "y")
    pipe.zadd("z:board", {"ann": 10.5, "bob": 7})
    pipe.xadd("x:events", {"kind": "login"}, id="1-1")
    pipe.xadd("x:events", {"kind": "logout"}, id="2-1")
    pipe.set(
        "other:1",
        "keep-me",
    )
    pipe.execute()
    one = redis_lib.Redis(host=info["host"], port=info["port"], password=REDIS_PASSWORD, db=1)
    one.set("only:in:one", "1")
    # a restricted ACL user: read commands only
    r.execute_command(
        "ACL",
        "SETUSER",
        "reader",
        "on",
        ">readerpw-123",
        "~*",
        "+@read",
        "+ping",
        "+info",
        "+command",
        "+type",
        "+pttl",
        "+scan",
        "+dump",
        "+object",
        "+memory",
        "+dbsize",
        "+select",
        "+client",
    )
    r.close()
    return info


CLICKHOUSE_SETUP = [
    "CREATE DATABASE shop",
    """CREATE TABLE shop.customers (id UInt64, name String, email Nullable(String),
        balance Decimal(12,2), big UInt64, notes String, created_at DateTime,
        tags Array(String)) ENGINE = MergeTree ORDER BY id""",
    """INSERT INTO shop.customers SELECT number + 1, concat('Customer ', leftPad(toString(number + 1), 3, '0')),
        if((number + 1) % 10 = 0, NULL, concat('c', toString(number + 1), '@example.com')),
        toDecimal64((number + 1) * 10.25, 2), 9007199254740993 + number,
        if(number = 0, repeat('x', 6000), ''), toDateTime('2024-01-02 03:04:05'), ['a', 'b']
        FROM numbers(250)""",
    """CREATE TABLE shop.orders (id UInt64, customer_id UInt64, total Decimal(10,2))
        ENGINE = MergeTree ORDER BY id""",
    "INSERT INTO shop.orders VALUES (1, 1, 10), (2, 2, 20)",
    "CREATE TABLE shop.no_key (a Int32, b String) ENGINE = MergeTree ORDER BY tuple()",
    "INSERT INTO shop.no_key VALUES (1, 'x'), (2, 'y')",
    "CREATE TABLE shop.`Weird %Table` (`a%b` Int32, `q``uote` String) ENGINE = MergeTree ORDER BY `a%b`",
    "INSERT INTO shop.`Weird %Table` VALUES (1, 'a'), (2, 'b')",
    "CREATE VIEW shop.vip AS SELECT id, name FROM shop.customers WHERE balance > 2000",
    """CREATE TABLE shop.secret_sink (id UInt64, v String) ENGINE = MergeTree ORDER BY id""",
    "INSERT INTO shop.secret_sink VALUES (1, 'must-not-change')",
]


def clickhouse_exec(info: dict[str, Any], sql: str) -> list[list]:
    r = httpx.post(
        f"http://{info['host']}:{info['port']}/",
        params={"default_format": "JSONCompact"},
        content=sql.encode(),
        headers={"X-ClickHouse-User": info["username"], "X-ClickHouse-Key": info["password"]},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    return r.json().get("data", []) if r.content else []


@pytest.fixture(scope="session")
def clickhouse_managed(clickhouse_container: Any) -> dict[str, Any]:
    info = {
        "host": clickhouse_container.get_container_host_ip(),
        "port": int(clickhouse_container.get_exposed_port(8123)),
        "username": "default",
        "password": CLICKHOUSE_PASSWORD,
        "database": "shop",
    }
    for stmt in CLICKHOUSE_SETUP:
        clickhouse_exec(info, stmt)
    return info


SQLITE_SETUP = [
    """CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE,
        balance NUMERIC, big INTEGER, notes TEXT, meta JSON, photo BLOB)""",
    """WITH RECURSIVE seq(g) AS (SELECT 1 UNION ALL SELECT g + 1 FROM seq WHERE g < 250)
       INSERT INTO customers (name, email, balance, big, notes, photo)
       SELECT 'Customer ' || printf('%03d', g),
              CASE WHEN g % 10 = 0 THEN NULL ELSE 'c' || g || '@example.com' END,
              g * 10.25, 9007199254740993 + g,
              CASE WHEN g = 1 THEN replace(hex(zeroblob(3000)), '0', 'x') END, x'DEADBEEF'
       FROM seq""",
    """CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total NUMERIC,
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE ON UPDATE RESTRICT)""",
    "INSERT INTO orders (customer_id, total) VALUES (1, 10), (2, 20)",
    """CREATE TABLE order_items (order_id INTEGER, line_no INTEGER, sku TEXT, qty INTEGER,
        PRIMARY KEY (order_id, line_no))""",
    "INSERT INTO order_items VALUES (1,1,'a',1),(1,2,'b',2),(1,3,'c',3)",
    "CREATE TABLE no_pk (a INTEGER, b TEXT)",
    "INSERT INTO no_pk VALUES (1,'x'),(2,'y')",
    'CREATE TABLE "Weird %Table" ("a%b" INTEGER PRIMARY KEY, "q""uote" TEXT)',
    "INSERT INTO \"Weird %Table\" VALUES (1,'a'),(2,'b')",
    "CREATE VIEW vip AS SELECT id, name FROM customers WHERE balance > 2000",
    "CREATE INDEX customers_name_idx ON customers (name)",
    "CREATE TABLE secret_sink (id INTEGER PRIMARY KEY, v TEXT)",
    "INSERT INTO secret_sink VALUES (1, 'must-not-change')",
]


def make_sqlite(path: Path) -> None:
    conn = sqlite3.connect(path)
    for stmt in SQLITE_SETUP:
        conn.execute(stmt)
    conn.commit()
    conn.close()


@pytest.fixture(scope="session")
def sqlite_template() -> Path:
    path = SQLITE_ROOT / "shop.db"
    make_sqlite(path)
    return path


@pytest.fixture
def sqlite_managed(sqlite_template: Path) -> dict[str, Any]:
    """A private copy of the seeded database inside the SQLite root (tests may write to it)."""
    name = f"t_{uuid.uuid4().hex[:8]}.db"
    shutil.copy(sqlite_template, SQLITE_ROOT / name)
    return {"host": "local-file", "port": 1, "username": "", "password": "", "database": name,
            "path": SQLITE_ROOT / name}  # fmt: skip


def es_request(
    info: dict[str, Any], method: str, path: str, body: Any = None, **params: Any
) -> Any:
    auth = (info["username"], info["password"]) if info["username"] else None
    r = httpx.request(
        method, f"http://{info['host']}:{info['port']}{path}", json=body, params=params,
        auth=auth, timeout=60,
    )  # fmt: skip
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.fixture(scope="session")
def es_managed(es_container: Any) -> dict[str, Any]:
    opensearch = "opensearch" in ES_IMAGE
    info = {
        "host": es_container.get_container_host_ip(),
        "port": int(es_container.get_exposed_port(9200)),
        "username": "" if opensearch else "elastic",
        "password": "" if opensearch else ES_PASSWORD,
        "database": None,
    }
    es_request(info, "PUT", "/shop", {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {"properties": {
            "name": {"type": "text", "fields": {"kw": {"type": "keyword"}}},
            "email": {"type": "keyword"}, "balance": {"type": "scaled_float", "scaling_factor": 100},
            "big": {"type": "long"}, "addr": {"properties": {"city": {"type": "keyword"}}},
            "n": {"type": "integer"},
        }},
    })  # fmt: skip
    lines = []
    for i in range(1, 251):
        lines.append(json.dumps({"index": {"_index": "shop", "_id": str(i)}}))
        doc = {"name": f"Customer {i:03d}", "balance": i * 10.25, "big": 9007199254740993 + i,
               "addr": {"city": "Paris" if i % 3 else "Rome"}, "n": i}  # fmt: skip
        if i % 10:
            doc["email"] = f"c{i}@example.com"
        lines.append(json.dumps(doc))
    auth = (info["username"], info["password"]) if info["username"] else None
    r = httpx.post(f"http://{info['host']}:{info['port']}/_bulk", params={"refresh": "true"},
                   content=("\n".join(lines) + "\n").encode(), auth=auth,
                   headers={"Content-Type": "application/x-ndjson"}, timeout=60)  # fmt: skip
    assert r.status_code == 200 and not r.json()["errors"], r.text[:300]
    es_request(info, "PUT", "/secret_sink/_doc/1", {"v": "must-not-change"}, refresh="true")
    return info


@pytest.fixture(scope="session")
def mongo_managed(mongo_container: MongoDbContainer) -> dict[str, Any]:
    host = mongo_container.get_container_host_ip()
    port = int(mongo_container.get_exposed_port(27017))
    client = pymongo.MongoClient(host=host, port=port, username="root", password="rootpw")
    shop = client["shop"]
    shop.customers.insert_many(
        [
            {
                "_id": ObjectId.from_datetime(
                    dt.datetime(2024, 1, 1, tzinfo=dt.UTC) + dt.timedelta(seconds=i)
                ),
                "name": f"Customer {i:03d}",
                "email": None if i % 10 == 0 else f"c{i}@example.com",
                "age": 20 + i % 50,
                "tags": ["a", "b"] if i % 2 else ["c"],
                "address": {"city": "Paris" if i % 3 else "Rome", "geo": {"lat": 1.5, "lon": 2.5}},
                "balance": Decimal128(f"{i}.10"),
                "big": Int64(9007199254740993 + i),
                "joined": dt.datetime(2020, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=i),
                "photo": Binary(b"\xde\xad\xbe\xef"),
            }
            for i in range(1, 251)
        ]
    )
    shop.customers.create_index("name")
    shop.customers.create_index(
        [("email", 1)],
        unique=True,
        name="email_unique",
        partialFilterExpression={"email": {"$type": "string"}},
    )
    shop.events.insert_many([{"_id": i, "kind": "click", "n": i} for i in range(1, 6)])
    shop.create_collection(
        "recent_events", viewOn="events", pipeline=[{"$match": {"n": {"$gt": 2}}}]
    )
    shop.system_marker.insert_one({"x": 1})
    client.close()
    return {"host": host, "port": port, "username": "root", "password": "rootpw",
            "database": "shop", "options": {"auth_source": "admin"}}  # fmt: skip


# ------------------------------------------------------------------ connection helper


async def create_connection(
    api: Api, db_type: str, info: dict[str, Any], **over: Any
) -> dict[str, Any]:
    body = {
        "name": f"{db_type}-{uuid.uuid4().hex[:8]}",
        "db_type": db_type,
        "environment": "development",
        "host": info["host"],
        "port": info["port"],
        "database": info.get("database"),
        "username": info["username"],
        "password": info["password"],
        "options": info.get("options", {}),
        **over,
    }
    r = await api.post("connections", body)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ Phase 3/4: writable fixtures


class Writable:
    """A writable connection plus a private table, so write tests never disturb shared data."""

    def __init__(self, api: Api, engine: str, cid: str, table: str, info: dict[str, Any]):
        self.api, self.engine, self.cid, self.table, self.info = api, engine, cid, table, info

    def base(self, table: str | None = None) -> str:
        t = table or self.table
        root = f"connections/{self.cid}/databases/shop"
        return (
            f"{root}/schemas/public/tables/{t}"
            if self.engine == "postgresql"
            else f"{root}/tables/{t}"
        )

    def sql(self, stmt: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        if self.engine == "postgresql":
            with psycopg.connect(
                host=self.info["host"], port=self.info["port"], user=self.info["username"],
                password=self.info["password"], dbname="shop", autocommit=True,
            ) as c:  # fmt: skip
                cur = c.execute(stmt, params)
                return cur.fetchall() if cur.description else []
        conn = pymysql.connect(
            host=self.info["host"], port=self.info["port"], user=self.info["username"],
            password=self.info["password"], database="shop", autocommit=True,
        )  # fmt: skip
        try:
            with conn.cursor() as cur:
                cur.execute(stmt, params)
                return list(cur.fetchall()) if cur.description else []
        finally:
            conn.close()

    async def rows(self, **params: Any) -> dict[str, Any]:
        import json as _json

        for k in ("sort", "filters", "columns", "search_columns"):
            if k in params and not isinstance(params[k], str):
                params[k] = _json.dumps(params[k])
        r = await self.api.get(f"{self.base()}/rows", params={"limit": 1000, **params})
        assert r.status_code == 200, r.text
        return r.json()

    async def row_key(self, name: str) -> str:
        page = await self.rows(filters=[{"column": "name", "op": "eq", "value": name}])
        assert page["rows"], f"row {name!r} not found"
        return page["row_keys"][0]


PG_EDIT_TABLE = """
CREATE TABLE {t} (id serial PRIMARY KEY, name text NOT NULL, qty int DEFAULT 0, price numeric(10,2),
    big bigint, meta jsonb, note text, photo bytea, added timestamptz DEFAULT now());
INSERT INTO {t} (name, qty, price, big, meta, note, photo) VALUES
    ('a', 1, 1.10, 9007199254740993, '{{"k": 1}}', 'secret-a', decode('deadbeef','hex')),
    ('b', 2, 2.20, NULL, NULL, 'secret-b', NULL),
    ('c', 3, 3.30, 5, '[1,2]', repeat('x', 6000), NULL),
    ('d', 4, 4.40, 6, NULL, NULL, NULL),
    ('e', 5, 5.50, 7, NULL, 'secret-e', NULL);
CREATE TABLE {t}_nopk (a int, b text);
INSERT INTO {t}_nopk VALUES (1, 'x');
CREATE VIEW {t}_v AS SELECT id, name FROM {t};
"""

MYSQL_EDIT_TABLE = [
    """CREATE TABLE {t} (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100) NOT NULL, qty INT DEFAULT 0,
        price DECIMAL(10,2), big BIGINT, meta JSON, note LONGTEXT, photo LONGBLOB,
        added DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    """INSERT INTO {t} (name, qty, price, big, meta, note, photo) VALUES
        ('a', 1, 1.10, 9007199254740993, '{{"k": 1}}', 'secret-a', UNHEX('DEADBEEF')),
        ('b', 2, 2.20, NULL, NULL, 'secret-b', NULL),
        ('c', 3, 3.30, 5, '[1,2]', REPEAT('x', 6000), NULL),
        ('d', 4, 4.40, 6, NULL, NULL, NULL),
        ('e', 5, 5.50, 7, NULL, 'secret-e', NULL)""",
    "CREATE TABLE {t}_nopk (a INT, b VARCHAR(10))",
    "INSERT INTO {t}_nopk VALUES (1, 'x')",
    "CREATE VIEW {t}_v AS SELECT id, name FROM {t}",
]


@pytest_asyncio.fixture(params=["postgresql", "mysql", "mariadb"])
async def wr(request, admin, pg_managed, mysql_managed, mariadb_managed) -> Writable:
    engine = request.param
    info = {"postgresql": pg_managed, "mysql": mysql_managed, "mariadb": mariadb_managed}[engine]
    table = f"t_{uuid.uuid4().hex[:8]}"
    probe = Writable(admin, engine, "", table, info)
    if engine == "postgresql":
        probe.sql(PG_EDIT_TABLE.format(t=table))
    else:
        for stmt in MYSQL_EDIT_TABLE:
            probe.sql(stmt.format(t=table))
    conn = await create_connection(admin, engine, info, read_only=False)
    probe.cid = conn["id"]
    return probe


async def writable_conn(api: Api, w: Writable, **over: Any) -> Writable:
    """Another connection to the same server/table (e.g. production, or with other limits)."""
    conn = await create_connection(api, w.engine, w.info, read_only=False, **over)
    return Writable(api, w.engine, conn["id"], w.table, w.info)


async def grant(admin: Api, cid: str, user_api: Any, role: str) -> None:
    roles = {r["name"]: r for r in (await admin.get("roles")).json()}
    r = await admin.get(f"connections/{cid}/grants")
    current = [{"user_id": g["user_id"], "role_id": g["role_id"]} for g in r.json()]
    current.append({"user_id": user_api.user_id, "role_id": roles[role]["id"]})
    assert (await admin.put(f"connections/{cid}/grants", {"grants": current})).status_code == 200
