"""SQL dump round trip: export a tricky table as SQL, restore it elsewhere, compare the data."""

from __future__ import annotations

import datetime as dt
import decimal
import sqlite3
import uuid
from typing import Any

import psycopg
import pymssql
import pymysql
import pytest

from app.query.classify import split_statements
from tests.integration.conftest import (
    SQLITE_ROOT,
    clickhouse_exec,
    create_connection,
    mssql_exec,
)

TRICKY = [
    "plain",
    "it's",
    "back\\slash",
    "trailing\\",
    "line1\nline2\r\nline3",
    "tab\there",
    "emoji 😀 é ü 漢字",
    "'; DROP TABLE secret_sink; --",
    "%s {x} %% ?",
    "",
    None,
]


def norm(v: Any) -> Any:
    if isinstance(v, memoryview | bytearray | bytes):
        return ("bytes", bytes(v).hex())
    if isinstance(v, decimal.Decimal):
        return ("num", str(v.normalize()))
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(v, str) and len(v) == 19 and v[10] in " T":  # sqlite / clickhouse timestamps
        return v.replace("T", " ")
    if isinstance(v, int | float) and not isinstance(v, bool):
        return ("num", str(decimal.Decimal(str(v)).normalize()))
    if isinstance(v, str) and v.replace(".", "", 1).replace("-", "", 1).isdigit():
        return ("num", str(decimal.Decimal(v).normalize()))
    return v


class Engine:
    """One engine: how to create/fill a source table, restore a dump, and read rows back."""

    def __init__(self, name: str, info: dict[str, Any]):
        self.name, self.info = name, info
        self.table = f"dump_{uuid.uuid4().hex[:8]}"
        self.dst = f"dumpdst_{uuid.uuid4().hex[:8]}"

    # -- paths ------------------------------------------------------------------------
    def export_url(self, cid: str) -> str:
        base = f"connections/{cid}/databases"
        if self.name == "postgresql":
            return f"{base}/shop/schemas/public/tables/{self.table}/export"
        if self.name == "mssql":
            return f"{base}/shop/schemas/dbo/tables/{self.table}/export"
        db = {"sqlite": "main"}.get(self.name, "shop")
        return f"{base}/{db}/tables/{self.table}/export"

    # -- plumbing ---------------------------------------------------------------------
    def _pg(self, db: str) -> psycopg.Connection:
        d = pg_dsn_for(self.info, db)
        return psycopg.connect(**d, autocommit=True)

    def _my(self, db: str | None) -> pymysql.Connection:
        i = self.info
        return pymysql.connect(host=i["host"], port=i["port"], user=i["username"],
                               password=i["password"], database=db, autocommit=True)  # fmt: skip

    def setup(self) -> None:
        t = self.table
        n = self.name
        rows = list(enumerate(TRICKY, start=1))
        if n == "postgresql":
            with self._pg("shop") as c:
                c.execute(
                    f"CREATE TABLE {t} (id integer primary key, name text, amount numeric(10,2), "
                    "payload bytea, flag boolean, created timestamp, meta jsonb, tags text[])"
                )
                for i, name in rows:
                    c.execute(
                        f"INSERT INTO {t} VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (i, name, decimal.Decimal(i) / 4, bytes([i, 0, 255]), i % 2 == 0,
                         dt.datetime(2024, 1, 2, 3, 4, 5), '{"k": [1, "x\'y"]}', ["a", "b'c"]),
                    )  # fmt: skip
        elif n in ("mysql", "mariadb"):
            c = self._my("shop")
            with c.cursor() as cur:
                cur.execute(
                    f"CREATE TABLE {t} (id int primary key, name text, amount decimal(10,2), "
                    "payload blob, flag tinyint(1), created datetime, meta text)"
                )
                for i, name in rows:
                    cur.execute(
                        f"INSERT INTO {t} VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (i, name, decimal.Decimal(i) / 4, bytes([i, 0, 255]), i % 2,
                         dt.datetime(2024, 1, 2, 3, 4, 5), '{"k": 1}'),
                    )  # fmt: skip
            c.close()
        elif n == "mssql":
            mssql_exec(self.info, f"CREATE TABLE {t} (id int primary key, name nvarchar(max), "
                       "amount decimal(10,2), payload varbinary(max), flag bit, created datetime2)", "shop")  # fmt: skip
            conn = pymssql.connect(server=self.info["host"], port=str(self.info["port"]),
                                   user=self.info["username"], password=self.info["password"],
                                   database="shop", autocommit=True)  # fmt: skip
            cur = conn.cursor()
            for i, name in rows:
                cur.execute(
                    f"INSERT INTO {t} VALUES (%s,%s,%s,%s,%s,%s)",
                    (i, name, decimal.Decimal(i) / 4, bytes([i, 0, 255]), i % 2,
                     dt.datetime(2024, 1, 2, 3, 4, 5)),
                )  # fmt: skip
            conn.close()
        elif n == "sqlite":
            c = sqlite3.connect(self.info["path"])
            c.execute(f"CREATE TABLE {t} (id integer primary key, name text, amount numeric, "
                      "payload blob, flag integer, created text)")  # fmt: skip
            for i, name in rows:
                c.execute(f"INSERT INTO {t} VALUES (?,?,?,?,?,?)",
                          (i, name, i / 4, bytes([i, 0, 255]), i % 2, "2024-01-02 03:04:05"))  # fmt: skip
            c.commit()
            c.close()
        else:  # clickhouse (no binary column: JSON transport is text)
            clickhouse_exec(self.info, f"CREATE TABLE shop.{t} (id UInt64, name Nullable(String), "
                            "amount Decimal(10,2), flag UInt8, created DateTime) "
                            "ENGINE = MergeTree ORDER BY id")  # fmt: skip
            from app.editor.sqldump import literal

            for i, name in rows:
                clickhouse_exec(
                    self.info,
                    f"INSERT INTO shop.{t} VALUES ({i}, {literal('clickhouse', name)}, {i / 4}, "
                    f"{i % 2}, '2024-01-02 03:04:05')",
                )

    def restore(self, script: str) -> None:
        n = self.name
        if n == "postgresql":
            self._pg("postgres").execute(f"CREATE DATABASE {self.dst}")
            with self._pg(self.dst) as c:
                c.execute(script)
        elif n in ("mysql", "mariadb"):
            self._my(None).cursor().execute(f"CREATE DATABASE {self.dst}")
            c = self._my(self.dst)
            with c.cursor() as cur:
                for stmt in split_statements(script):
                    cur.execute(stmt)
            c.close()
        elif n == "mssql":
            mssql_exec(self.info, f"CREATE DATABASE {self.dst}")
            mssql_exec(self.info, script, self.dst)
        elif n == "sqlite":
            c = sqlite3.connect(self.dst_path())
            c.executescript(script)
            c.commit()
            c.close()
        else:
            clickhouse_exec(self.info, f"CREATE DATABASE {self.dst}")
            import httpx

            for stmt in split_statements(script):
                r = httpx.post(
                    f"http://{self.info['host']}:{self.info['port']}/",
                    params={"database": self.dst}, content=stmt.encode(), timeout=60,
                    headers={"X-ClickHouse-User": self.info["username"],
                             "X-ClickHouse-Key": self.info["password"]},
                )  # fmt: skip
                assert r.status_code == 200, (stmt[:200], r.text)

    def dst_path(self) -> str:
        return str(SQLITE_ROOT / f"{self.dst}.sqlite")

    def read(self, restored: bool) -> list[tuple]:
        t, n = self.table, self.name
        cols = "id, name, amount, flag, created" if n == "clickhouse" else None
        if n == "postgresql":
            with self._pg(self.dst if restored else "shop") as c:
                return c.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
        if n in ("mysql", "mariadb"):
            c = self._my(self.dst if restored else "shop")
            with c.cursor() as cur:
                cur.execute(f"SELECT * FROM {t} ORDER BY id")
                out = list(cur.fetchall())
            c.close()
            return out
        if n == "mssql":
            return [tuple(r) for r in mssql_exec(self.info, f"SELECT * FROM {t} ORDER BY id",
                                                 self.dst if restored else "shop")]  # fmt: skip
        if n == "sqlite":
            c = sqlite3.connect(self.dst_path() if restored else self.info["path"])
            out = c.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
            c.close()
            return out
        db = self.dst if restored else "shop"
        return [
            tuple(r) for r in clickhouse_exec(self.info, f"SELECT {cols} FROM {db}.{t} ORDER BY id")
        ]


def pg_dsn_for(info: dict[str, Any], db: str) -> dict[str, Any]:
    return {"host": info["host"], "port": info["port"], "user": info["username"],
            "password": info["password"], "dbname": db}  # fmt: skip


@pytest.fixture(params=["postgresql", "mysql", "mariadb", "mssql", "sqlite", "clickhouse"])
async def eng(request, admin, pg_managed, mysql_managed, mariadb_managed, mssql_managed,
              sqlite_managed, clickhouse_managed):  # fmt: skip
    info = {"postgresql": pg_managed, "mysql": mysql_managed, "mariadb": mariadb_managed,
            "mssql": mssql_managed, "sqlite": sqlite_managed, "clickhouse": clickhouse_managed}[
        request.param
    ]  # fmt: skip
    e = Engine(request.param, info)
    e.setup()
    conn = await create_connection(admin, request.param, info)
    e.cid = conn["id"]
    e.api = admin
    return e


async def test_a_table_survives_dump_and_restore(eng):
    r = await eng.api.get(eng.export_url(eng.cid), params={"format": "sql"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/sql")
    assert f'filename="{eng.table}.sql"' in r.headers["content-disposition"]
    script = r.text
    assert script.startswith("-- DB Wrench SQL dump") and "CREATE TABLE" in script
    assert script.count("INSERT INTO") >= 1
    eng.restore(script)
    src, dst = eng.read(False), eng.read(True)
    assert len(src) == len(dst) == len(TRICKY)
    for a, b in zip(src, dst, strict=True):
        assert [norm(x) for x in a] == [norm(x) for x in b], (a, b)
    # the hostile value was stored as data, not executed
    assert any(row[1] == "'; DROP TABLE secret_sink; --" for row in dst)


async def test_data_only_dump_has_no_ddl_and_respects_filters_and_columns(eng):
    r = await eng.api.get(
        eng.export_url(eng.cid),
        params={"format": "sql", "ddl": "false", "columns": '["id","name"]',
                "filters": '[{"column":"id","op":"lte","value":3}]'},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    assert "CREATE TABLE" not in r.text
    assert r.text.count("(1,") + r.text.count("(2,") + r.text.count("(3,") == 3
    assert "(4," not in r.text and "amount" not in r.text


async def test_unsupported_engines_and_permissions(admin, eng, new_user):
    viewer = await new_user("Viewer")  # a role without export.export
    from tests.integration.conftest import grant

    await grant(admin, eng.cid, viewer, "Viewer")
    r = await viewer.get(eng.export_url(eng.cid), params={"format": "sql"})
    assert r.status_code == 403
    bad = await admin.get(eng.export_url(eng.cid), params={"format": "sqlx"})
    assert bad.status_code == 422


@pytest.mark.security
async def test_masked_columns_are_written_as_null(wr, admin, new_user):
    from tests.integration.conftest import grant

    schema = "public" if wr.engine == "postgresql" else ""
    r = await admin.put(
        f"connections/{wr.cid}/sensitive",
        {"markers": [{"database": "shop", "schema": schema, "object": wr.table, "field": "note"}]},
    )
    assert r.status_code == 200, r.text
    role = (
        await admin.post(
            "roles",
            {
                "name": f"dump-{uuid.uuid4().hex[:6]}",
                "permissions": ["connection.view", "metadata.view", "data.read", "export.export"],
            },
        )
    ).json()
    limited = await new_user("Viewer")
    cur = [
        {"user_id": g["user_id"], "role_id": g["role_id"]}
        for g in (await admin.get(f"connections/{wr.cid}/grants")).json()
    ]
    cur.append({"user_id": limited.user_id, "role_id": role["id"]})
    await admin.put(f"connections/{wr.cid}/grants", {"grants": cur})
    seen = (await limited.get(f"{wr.base()}/export", params={"format": "sql"})).text
    assert "secret-a" not in seen and "secret-b" not in seen
    assert "Masked columns are written as NULL: note" in seen
    full = await new_user("Database Admin")  # has data.read_sensitive
    await grant(admin, wr.cid, full, "Database Admin")
    assert "secret-a" in (await full.get(f"{wr.base()}/export", params={"format": "sql"})).text
    events = (
        await admin.get("audit/events", params={"connection_id": wr.cid, "action": "data.export"})
    ).json()["events"]
    assert any(e["detail"].get("format") == "sql" for e in events)
