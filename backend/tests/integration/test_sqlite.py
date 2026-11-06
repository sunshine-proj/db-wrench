"""SQLite (ADR-0021): confinement to the root, browsing, editing, SQL workspace, read-only."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from typing import Any

import pytest

from tests.integration.conftest import SQLITE_ROOT, create_connection


class Sq:
    def __init__(self, api: Any, cid: str, info: dict[str, Any]):
        self.api, self.cid, self.info = api, cid, info

    def url(self, path: str = "") -> str:
        return f"connections/{self.cid}{path}"

    def table(self, name: str) -> str:
        return self.url(f"/databases/main/tables/{name}")

    def sql(self, stmt: str, params: tuple = ()) -> list[tuple]:
        c = sqlite3.connect(self.info["path"])
        try:
            out = c.execute(stmt, params).fetchall()
            c.commit()
            return out
        finally:
            c.close()

    async def rows(self, table: str = "customers", **params: Any) -> dict[str, Any]:
        for k in ("sort", "filters", "columns", "search_columns"):
            if k in params and not isinstance(params[k], str):
                params[k] = json.dumps(params[k])
        r = await self.api.get(f"{self.table(table)}/rows", params=params)
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
async def sq(admin, sqlite_managed) -> Sq:
    c = await create_connection(admin, "sqlite", sqlite_managed, read_only=False)
    return Sq(admin, c["id"], sqlite_managed)


@pytest.fixture
async def sq_ro(admin, sqlite_managed) -> Sq:
    c = await create_connection(admin, "sqlite", sqlite_managed)
    return Sq(admin, c["id"], sqlite_managed)


async def run(m: Sq, sql: str, **kw: Any):
    return await m.api.post(m.url("/queries/execute"), {"database": "main", "sql": sql, **kw})


# ------------------------------------------------------------------------ confinement


@pytest.mark.security
@pytest.mark.parametrize(
    "bad",
    ["../etc/passwd", "/etc/passwd", "a/../../x.db", "", "nope.db", "sub\\x.db", "~/x.db"],
)
async def test_connections_cannot_point_outside_the_root_or_at_missing_files(
    admin, sqlite_managed, bad
):
    r = await admin.post(
        "connections",
        {"name": f"bad-{uuid.uuid4().hex[:6]}", "db_type": "sqlite", "host": "local-file",
         "port": 1, "database": bad, "username": ""},
    )  # fmt: skip
    assert r.status_code in (404, 422), (bad, r.text)


@pytest.mark.security
async def test_symlinks_and_non_database_files_are_refused(admin, sqlite_managed, tmp_path):
    outside = tmp_path / "outside.db"
    c = sqlite3.connect(outside)
    c.execute("CREATE TABLE s (x)")
    c.commit()
    c.close()
    link = SQLITE_ROOT / f"link_{uuid.uuid4().hex[:6]}.db"
    link.symlink_to(outside)
    text = SQLITE_ROOT / f"notes_{uuid.uuid4().hex[:6]}.txt"
    text.write_text("hello")
    try:
        for name in (link.name, text.name):
            r = await admin.post(
                "connections",
                {"name": f"x-{uuid.uuid4().hex[:6]}", "db_type": "sqlite", "host": "local-file",
                 "port": 1, "database": name, "username": ""},
            )  # fmt: skip
            assert r.status_code == 422, (name, r.text)
    finally:
        link.unlink()
        text.unlink()


async def test_host_and_port_are_normalised_and_no_network_check_applies(admin, sqlite_managed):
    c = await create_connection(
        admin, "sqlite", {**sqlite_managed, "host": "169.254.169.254", "port": 9}
    )
    assert c["host"] == "local-file" and c["port"] == 1 and c["tls_mode"] == "disable"


# ------------------------------------------------------------------------ explorer


async def test_connection_test_and_listing(sq):
    r = await sq.api.post(sq.url("/test"), {})
    assert r.status_code == 200 and r.json()["server_version"].startswith("SQLite")
    dbs = (await sq.api.get(sq.url("/databases"))).json()
    assert [d["name"] for d in dbs] == ["main"] and dbs[0]["size_bytes"] > 0
    tables = {t["name"]: t for t in (await sq.api.get(sq.url("/databases/main/tables"))).json()}
    assert {"customers", "orders", "order_items", "no_pk", "vip", "Weird %Table"} <= set(tables)
    assert not [n for n in tables if n.startswith("sqlite_")]
    assert tables["vip"]["kind"] == "view" and tables["customers"]["kind"] == "table"
    assert (await sq.api.get(sq.url("/databases/other/tables"))).status_code == 404
    assert (await sq.api.get(sq.url("/databases/main/schemas"))).status_code in (400, 404, 409, 422)


async def test_structure(sq):
    st = (await sq.api.get(f"{sq.table('customers')}/structure")).json()
    cols = {c["name"]: c for c in st["columns"]}
    assert st["primary_key"] == ["id"] and cols["id"]["is_identity"] is True
    assert cols["name"]["nullable"] is False and cols["email"]["nullable"] is True
    assert {"customers_name_idx"} <= {i["name"] for i in st["indexes"]}
    o = (await sq.api.get(f"{sq.table('orders')}/structure")).json()
    fk = o["foreign_keys"][0]
    assert fk["ref_table"] == "customers" and fk["on_delete"] == "CASCADE"
    items = (await sq.api.get(f"{sq.table('order_items')}/structure")).json()
    assert items["primary_key"] == ["order_id", "line_no"]
    assert (await sq.api.get(f"{sq.table('nope')}/structure")).status_code == 404


# ------------------------------------------------------------------------ browser


async def test_keyset_pagination_and_composite_keys(sq):
    first = await sq.rows(limit=100)
    assert first["pagination"] == "keyset" and first["next_cursor"]
    seen, cursor = [r[0] for r in first["rows"]], first["next_cursor"]
    while cursor:
        page = await sq.rows(limit=100, cursor=cursor)
        seen += [r[0] for r in page["rows"]]
        cursor = page["next_cursor"]
    assert seen == list(range(1, 251))
    p1 = await sq.rows("order_items", limit=2)
    p2 = await sq.rows("order_items", limit=2, cursor=p1["next_cursor"])
    assert [(r[0], r[1]) for r in p2["rows"]] == [(1, 3)]


async def test_type_fidelity_filters_and_like_escaping(sq):
    page = await sq.rows(limit=1, columns=["id", "balance", "big", "notes", "photo"])
    row = dict(zip([c["name"] for c in page["columns"]], page["rows"][0], strict=True))
    assert row["big"] == "9007199254740994"  # beyond 2**53 stays exact
    assert isinstance(row["photo"], dict) and row["photo"].get("$binary")
    assert isinstance(row["notes"], dict) and row["notes"].get("$truncated")
    nulls = await sq.rows(filters=[{"column": "email", "op": "is_null"}], limit=1000)
    assert len(nulls["rows"]) == 25
    sq.sql(
        "INSERT INTO customers (name, email) VALUES ('100% sure', 'p@x.io'), ('1005 sure', 'q@x.io')"
    )
    lit = await sq.rows(filters=[{"column": "name", "op": "contains", "value": "100%"}], limit=50)
    assert [r[1] for r in lit["rows"]] == ["100% sure"]  # % is literal, not a wildcard
    ci = await sq.rows(search="CUSTOMER 07", search_columns=["name"], limit=1000)
    assert 0 < len(ci["rows"]) <= 10
    srt = await sq.rows(sort=[{"column": "name", "direction": "desc"}], limit=3)
    assert srt["pagination"] == "offset"


async def test_unordered_views_and_odd_identifiers(sq):
    nokey = await sq.rows("no_pk")
    assert nokey["editable"] is False and len(nokey["rows"]) == 2
    weird = await sq.rows("Weird %Table")
    assert [r[0] for r in weird["rows"]] == [1, 2]
    assert (await sq.rows("vip", limit=5))["editable"] is False


async def test_unknown_columns_are_rejected_not_interpolated(sq):
    r = await sq.api.get(
        f"{sq.table('customers')}/rows",
        params={"sort": json.dumps([{"column": 'id"; DROP TABLE customers;--'}])},
    )
    assert r.status_code == 422
    assert sq.sql("SELECT count(*) FROM customers")[0][0] == 250


async def test_export_csv(sq):
    r = await sq.api.get(f"{sq.table('customers')}/export", params={"format": "csv"})
    assert r.status_code == 200 and len(r.text.strip().splitlines()) == 251


# ------------------------------------------------------------------------ editor


async def test_insert_update_delete_with_concurrency(sq):
    base = sq.table("customers")
    r = await sq.api.post(
        f"{base}/rows", {"values": {"name": "new", "email": "n@x.io", "big": "9007199254740999"}}
    )
    assert r.status_code == 201, r.text
    key = r.json()["row_key"]
    body = r.json()
    row = dict(zip([c["name"] for c in body["columns"]], body["row"], strict=True))
    assert row["big"] == "9007199254740999" and row["id"] == 251
    ok = await sq.api.patch(
        f"{base}/rows/{key}", {"values": {"name": "new2"}, "expected": {"name": "new"}}
    )
    assert ok.status_code == 200, ok.text
    stale = await sq.api.patch(
        f"{base}/rows/{key}", {"values": {"name": "x"}, "expected": {"name": "new"}}
    )
    assert stale.status_code == 409
    nul = await sq.api.patch(
        f"{base}/rows/{key}", {"values": {"notes": "n1"}, "expected": {"notes": None}}
    )
    assert nul.status_code == 200, nul.text
    again = await sq.api.patch(
        f"{base}/rows/{key}", {"values": {"notes": "n2"}, "expected": {"notes": None}}
    )
    assert again.status_code == 409
    gone = await sq.api.request(
        "DELETE", f"{base}/rows/{key}", json={"expected": {"name": "stale"}}
    )
    assert gone.status_code == 409
    done = await sq.api.request("DELETE", f"{base}/rows/{key}", json={"expected": {"name": "new2"}})
    assert done.status_code == 204
    assert sq.sql("SELECT count(*) FROM customers WHERE name LIKE 'new%'")[0][0] == 0


@pytest.mark.security
async def test_forged_row_keys_are_rejected(sq):
    import base64

    base = sq.table("customers")
    for key in (
        "garbage!!",
        base64.urlsafe_b64encode(b'{"r":[1,2]}').decode(),
        base64.urlsafe_b64encode(b'{"r":[null]}').decode(),
        base64.urlsafe_b64encode(b'{"r":["1 OR 1=1"]}').decode(),
    ):
        r = await sq.api.request("DELETE", f"{base}/rows/{key}", json={"expected": {}})
        assert r.status_code in (400, 409, 422), (key, r.status_code)
    assert sq.sql("SELECT count(*) FROM customers")[0][0] == 250


async def test_bulk_delete_with_preview_and_read_only_refusal(sq, sq_ro):
    base = sq.table("customers")
    sel = {"filters": [{"column": "id", "op": "gte", "value": 249}]}
    prev = await sq.api.post(f"{base}/rows/bulk/preview", {"operation": "delete", "selector": sel})
    assert prev.status_code == 200 and prev.json()["count"] == 2
    forged = await sq.api.post(
        f"{base}/rows/bulk/delete", {"selector": sel, "expected_count": 2, "token": "nope"}
    )
    assert forged.status_code == 422
    done = await sq.api.post(
        f"{base}/rows/bulk/delete",
        {"selector": sel, "expected_count": 2, "token": prev.json()["token"]},
    )
    assert done.status_code == 200 and done.json() == {"affected": 2}
    r = await sq_ro.api.post(f"{sq_ro.table('customers')}/rows", {"values": {"name": "x"}})
    assert r.status_code == 403


# ------------------------------------------------------------------------ SQL workspace


async def test_select_literals_and_limits(sq):
    r = await run(sq, "SELECT id, name, '50% off; ?' AS s FROM customers WHERE id = 1")
    assert r.status_code == 200, r.text
    rs = r.json()["results"][0]
    assert rs["rows"][0][2] == "50% off; ?"
    lim = await run(sq, "SELECT id FROM customers ORDER BY id", max_rows=2)
    assert lim.json()["results"][0]["truncated"] is True
    bad = await run(sq, "SELEKT 1")
    assert bad.status_code == 400 and bad.json()["code"] == "query_failed"


@pytest.mark.security
async def test_the_database_itself_refuses_writes_on_a_read_only_connection(sq, sq_ro):
    for sql in (
        "INSERT INTO secret_sink VALUES (2, 'pwned')",
        "UPDATE secret_sink SET v = 'pwned'",
        "DELETE FROM secret_sink",
        "DROP TABLE secret_sink",
        "CREATE TABLE evil (a)",
    ):
        r = await run(sq_ro, sql, confirm_destructive=True)
        assert r.status_code == 403, (sql, r.text)
    assert sq.sql("SELECT v FROM secret_sink") == [("must-not-change",)]

    from app.connections.network import NetworkPolicy
    from app.database.adapters.sqlite import SQLiteAdapter
    from app.database.core.base import ConnectionSpec, DbType, Limits
    from app.errors import AppError

    a = SQLiteAdapter(
        ConnectionSpec(id=uuid.uuid4(), db_type=DbType.SQLITE, host="x", port=1,
                       database=sq.info["database"], username="", password="", allow_writes=False),
        NetworkPolicy(), Limits(),
    )  # fmt: skip
    kw = {"write": False, "script": False, "timeout_s": 5, "max_rows": 5, "rollback": False,
          "on_start": lambda _r: None}  # fmt: skip
    for sql in (
        "INSERT INTO secret_sink VALUES (3, 'pwned')",  # mode=ro: refused by SQLite itself
        "PRAGMA query_only = OFF",
        "ATTACH DATABASE '/tmp/evil.db' AS e",
        "PRAGMA writable_schema = ON",
    ):
        with pytest.raises(AppError):
            a.run_sql("main", None, [sql], **kw)
    assert sq.sql("SELECT v FROM secret_sink") == [("must-not-change",)]


@pytest.mark.security
async def test_attach_vacuum_and_file_functions_are_blocked_even_when_writing(sq, tmp_path):
    target = tmp_path / "leak.db"
    for sql in (
        f"ATTACH DATABASE '{target}' AS leak",
        f"VACUUM INTO '{target}'",
        "SELECT load_extension('/lib/x.so')",
        "SELECT readfile('/etc/passwd')",
        "PRAGMA writable_schema = ON",
        "PRAGMA database_list",
    ):
        r = await run(sq, sql, confirm_destructive=True)
        assert r.status_code in (400, 403, 422), (sql, r.status_code, r.text)
    assert not target.exists()


async def test_writes_commit_or_roll_back(sq):
    done = await run(sq, "UPDATE customers SET name = 'zz' WHERE id = 1")
    assert done.status_code == 200 and done.json()["committed"] is True
    assert sq.sql("SELECT name FROM customers WHERE id = 1") == [("zz",)]
    dry = await run(sq, "UPDATE customers SET name = 'qq' WHERE id = 1", rollback=True)
    assert dry.status_code == 200 and dry.json()["committed"] is False
    assert sq.sql("SELECT name FROM customers WHERE id = 1") == [("zz",)]
    r = await run(sq, "INSERT INTO secret_sink VALUES (9, 'a'); INSERT INTO nope VALUES (1)")
    assert r.status_code in (400, 403, 422)
    assert sq.sql("SELECT count(*) FROM secret_sink WHERE id = 9") == [(0,)]


HEAVY = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 2000000000) "
    "SELECT count(*) FROM c"
)


async def test_statement_timeout_and_cancel(sq):
    started = time.monotonic()
    r = await run(sq, HEAVY, timeout_s=1)
    assert r.status_code == 408 and r.json()["code"] == "statement_timeout"
    assert time.monotonic() - started < 10
    op = str(uuid.uuid4())
    task = asyncio.create_task(run(sq, HEAVY, operation_id=op, timeout_s=60))
    await asyncio.sleep(2)
    c = await sq.api.post(sq.url(f"/queries/{op}/cancel"), {})
    assert c.status_code in (200, 202, 204), c.text
    res = await asyncio.wait_for(task, 20)
    assert res.status_code in (400, 408, 409, 499), res.text


# ------------------------------------------------------------------------ administration


async def test_indexes_can_be_managed_but_the_file_cannot_be_created_or_dropped(sq):
    url = sq.table("customers")
    r = await sq.api.post(f"{url}/indexes", {"name": "ix_bal", "columns": ["balance", "name"]})
    assert r.status_code == 201, r.text
    idx = {i["name"]: i for i in (await sq.api.get(f"{url}/structure")).json()["indexes"]}
    assert idx["ix_bal"]["columns"] == ["balance", "name"]
    assert (await sq.api.delete(f"{url}/indexes/ix_bal", json={})).status_code == 204
    assert (await sq.api.post(sq.url("/databases"), {"name": "other"})).status_code in (
        400,
        403,
        409,
        422,
    )
    drop = await sq.api.delete(sq.url("/databases/main"), json={"confirm": "main"})
    assert drop.status_code in (400, 403, 409, 422)
    assert sq.info["path"].exists()
