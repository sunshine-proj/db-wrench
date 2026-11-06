"""Microsoft SQL Server (ADR-0018): explorer, editor, export, SQL workspace, admin, principals.

Dedicated module: T-SQL differs enough from the other relational engines (no read-only
transactions, no row-value comparison, OFFSET/FETCH paging) that the shared suites would need
a branch in nearly every test.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import pymssql
import pytest

from app.connections.network import NetworkPolicy
from app.database.adapters.mssql import MSSQLAdapter, assert_read_only_batch, tsql_words
from app.database.core.base import ConnectionSpec, DbType, Limits
from app.errors import StatementNotAllowed, StatementTimeout
from tests.integration.conftest import create_connection, mssql_exec

PW = "Str0ng!Passw0rd#1"


def uniq(prefix: str = "t") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class Ms:
    def __init__(self, api: Any, cid: str, info: dict[str, Any]):
        self.api, self.cid, self.info = api, cid, info

    def url(self, path: str = "") -> str:
        return f"connections/{self.cid}{path}"

    def table(self, name: str, schema: str = "dbo") -> str:
        return self.url(f"/databases/shop/schemas/{schema}/tables/{name}")

    def sql(self, stmt: str, database: str = "shop") -> list[tuple]:
        return mssql_exec(self.info, stmt, database)

    async def rows(self, table: str = "customers", **params: Any) -> dict[str, Any]:
        for k in ("sort", "filters", "columns", "search_columns"):
            if k in params and not isinstance(params[k], str):
                params[k] = json.dumps(params[k])
        r = await self.api.get(f"{self.table(table)}/rows", params=params)
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
async def ms(admin, mssql_managed) -> Ms:
    c = await create_connection(admin, "mssql", mssql_managed, read_only=False)
    return Ms(admin, c["id"], mssql_managed)


@pytest.fixture
async def ms_ro(admin, mssql_managed) -> Ms:
    c = await create_connection(admin, "mssql", mssql_managed)  # read-only by default
    return Ms(admin, c["id"], mssql_managed)


@pytest.fixture
def edit_table(mssql_managed) -> str:
    t = uniq("e")
    mssql_exec(
        mssql_managed,
        f"""CREATE TABLE {t} (id INT IDENTITY(1,1) PRIMARY KEY, name NVARCHAR(100) NOT NULL,
            qty INT NULL DEFAULT 0, price DECIMAL(10,2), big BIGINT, note NVARCHAR(MAX))""",
        "shop",
    )
    mssql_exec(
        mssql_managed,
        f"""INSERT INTO {t} (name, qty, price, big, note) VALUES
            ('a', 1, 1.10, 9007199254740993, 'secret-a'), ('b', 2, 2.20, NULL, 'secret-b'),
            ('c', 3, 3.30, 5, NULL), ('d', 4, 4.40, 6, NULL), ('e', 5, 5.50, 7, 'secret-e')""",
        "shop",
    )
    return t


# ------------------------------------------------------------------------ pure functions


@pytest.mark.parametrize(
    "sql",
    ["SELECT 1", "select * from [update]", "WITH x AS (SELECT 1 a) SELECT a FROM x",
     "SELECT 'DROP TABLE x; DELETE' AS s -- INSERT", "SELECT /* EXEC */ 1"],
)  # fmt: skip
def test_read_gate_allows_plain_reads(sql):
    assert_read_only_batch(sql)


@pytest.mark.security
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 DELETE FROM secret_sink",  # no semicolon needed in T-SQL
        "SELECT 1; DROP TABLE secret_sink",
        "EXEC sp_who",
        "SELECT * INTO evil FROM customers",
        "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')",
        "SELECT * FROM OPENQUERY(lnk, 'select 1')",
        "WITH x AS (SELECT 1 a) DELETE FROM secret_sink",
        "SELECT 1 WAITFOR DELAY '00:00:10'",
        "SELECT xp_cmdshell",
        "SELECT sys.fn_my_permissions(NULL, 'DATABASE')",
        "INSERT secret_sink VALUES (2, 'x')",
        "SET IMPLICIT_TRANSACTIONS OFF",
        "DECLARE @a INT = 1",
        "BACKUP DATABASE shop TO DISK = '/tmp/x'",
        "SELECT 1 KILL 51",
    ],
)
def test_read_gate_refuses_anything_that_can_change_state(sql):
    with pytest.raises(StatementNotAllowed):
        assert_read_only_batch(sql)


def test_tokenizer_ignores_strings_comments_and_bracketed_identifiers():
    assert tsql_words("SELECT [drop], 'delete', \"insert\" /* exec */ -- kill\n FROM t") == [
        "SELECT",
        "FROM",
        "T",
    ]


# ------------------------------------------------------------------------ explorer


async def test_connection_test_and_capabilities(ms):
    r = await ms.api.post(ms.url("/test"), {})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "Microsoft SQL Server" in r.json()["server_version"]
    caps = (await ms.api.get(ms.url())).json()["capabilities"]
    assert caps["engine"] == "mssql" and caps["supports_schemas"] is True
    assert caps["supports_read_only_transactions"] is False  # documented in ADR-0018


async def test_databases_schemas_tables(ms):
    dbs = [d["name"] for d in (await ms.api.get(ms.url("/databases"))).json()]
    assert "shop" in dbs and not {"master", "tempdb", "model", "msdb"} & set(dbs)
    schemas = [s["name"] for s in (await ms.api.get(ms.url("/databases/shop/schemas"))).json()]
    assert "dbo" in schemas and "sys" not in schemas and "INFORMATION_SCHEMA" not in schemas
    tables = {
        t["name"]: t
        for t in (await ms.api.get(ms.url("/databases/shop/schemas/dbo/tables"))).json()
    }
    assert {"customers", "orders", "order_items", "no_pk", "vip", "Weird %Table"} <= set(tables)
    assert tables["vip"]["kind"] == "view" and tables["customers"]["kind"] == "table"
    missing = await ms.api.get(ms.url("/databases/nope/schemas"))
    assert missing.status_code == 404
    assert (await ms.api.get(ms.url("/databases/shop/schemas/nope/tables"))).status_code == 404


async def test_structure(ms):
    st = (await ms.api.get(f"{ms.table('customers')}/structure")).json()
    cols = {c["name"]: c for c in st["columns"]}
    assert cols["name"]["data_type"] == "nvarchar(100)" and cols["name"]["nullable"] is False
    assert cols["balance"]["data_type"] == "decimal(12,2)" and cols["id"]["is_identity"] is True
    assert st["primary_key"] == ["id"]
    assert {"customers_name_idx", "customers_email_uq"} <= {i["name"] for i in st["indexes"]}
    o = (await ms.api.get(f"{ms.table('orders')}/structure")).json()
    fk = o["foreign_keys"][0]
    assert fk["ref_table"] == "customers" and fk["on_delete"] == "CASCADE"
    assert any(c["kind"] == "check" for c in o["constraints"])
    items = (await ms.api.get(f"{ms.table('order_items')}/structure")).json()
    assert items["primary_key"] == ["order_id", "line_no"]


# ------------------------------------------------------------------------ data browser


async def test_keyset_pagination_walks_every_row_once(ms):
    first = await ms.rows(limit=100)
    assert first["pagination"] == "keyset" and len(first["rows"]) == 100 and first["next_cursor"]
    seen, cursor = [r[0] for r in first["rows"]], first["next_cursor"]
    while cursor:
        page = await ms.rows(limit=100, cursor=cursor)
        seen += [r[0] for r in page["rows"]]
        cursor = page["next_cursor"]
    assert seen == list(range(1, 251))


async def test_composite_key_keyset_pagination(ms):
    p1 = await ms.rows("order_items", limit=2)
    assert p1["pagination"] == "keyset" and len(p1["rows"]) == 2 and p1["next_cursor"]
    p2 = await ms.rows("order_items", limit=2, cursor=p1["next_cursor"])
    assert [(r[0], r[1]) for r in p2["rows"]] == [(1, 3)]  # (a,b) > (1,2) without row values


async def test_type_fidelity_and_markers(ms):
    page = await ms.rows(limit=1, columns=["id", "balance", "big", "notes", "photo"])
    row = dict(zip([c["name"] for c in page["columns"]], page["rows"][0], strict=True))
    assert row["balance"] == "10.25" and row["big"] == "9007199254740994"
    assert isinstance(row["notes"], dict) and row["notes"].get("$truncated")
    assert isinstance(row["photo"], dict) and row["photo"].get("$binary")


async def test_sort_filter_search_and_null_handling(ms):
    s = await ms.rows(sort=[{"column": "balance", "direction": "desc"}], limit=3)
    assert s["pagination"] == "offset" and s["rows"][0][0] == 250
    nulls = await ms.rows(filters=[{"column": "email", "op": "is_null"}], limit=1000)
    assert len(nulls["rows"]) == 25
    like = await ms.rows(search="Customer 07", search_columns=["name"], limit=1000)
    assert 0 < len(like["rows"]) <= 10
    offset_page = await ms.rows(sort=[{"column": "name", "direction": "asc"}], limit=10)
    page2 = await ms.rows(
        sort=[{"column": "name", "direction": "asc"}], limit=10, cursor=offset_page["next_cursor"]
    )
    assert [r[0] for r in page2["rows"]] == list(range(11, 21))


async def test_unordered_tables_and_odd_identifiers(ms):
    page = await ms.rows("no_pk")
    assert page["pagination"] == "unordered_offset" and page["editable"] is False
    assert len(page["rows"]) == 2 and page["warnings"]
    weird = await ms.rows("Weird %Table")
    assert [r[0] for r in weird["rows"]] == [1, 2]
    view = await ms.rows("vip", limit=5)
    assert view["editable"] is False


async def test_unknown_columns_are_rejected_not_interpolated(ms):
    r = await ms.api.get(
        f"{ms.table('customers')}/rows",
        params={
            "sort": json.dumps([{"column": "id]; DROP TABLE customers;--", "direction": "asc"}])
        },
    )
    assert r.status_code == 422
    assert ms.sql("SELECT COUNT(*) FROM customers")[0][0] == 250


# ------------------------------------------------------------------------ editor


async def test_insert_update_delete_with_concurrency(ms, edit_table):
    base = ms.table(edit_table)
    r = await ms.api.post(
        f"{base}/rows",
        {"values": {"name": "new", "qty": 9, "price": "12.50", "big": "9007199254740999"}},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    row = dict(zip([c["name"] for c in body["columns"]], body["row"], strict=True))
    assert row["price"] == "12.50" and row["big"] == "9007199254740999" and body["row_key"]
    key = body["row_key"]

    ok = await ms.api.patch(f"{base}/rows/{key}", {"values": {"qty": 42}, "expected": {"qty": 9}})
    assert ok.status_code == 200, ok.text
    stale = await ms.api.patch(f"{base}/rows/{key}", {"values": {"qty": 7}, "expected": {"qty": 9}})
    assert stale.status_code == 409
    # NULL-safe comparison of the previous value
    nul = await ms.api.patch(
        f"{base}/rows/{key}", {"values": {"note": "n1"}, "expected": {"note": None}}
    )
    assert nul.status_code == 200, nul.text
    again = await ms.api.patch(
        f"{base}/rows/{key}", {"values": {"note": "n2"}, "expected": {"note": None}}
    )
    assert again.status_code == 409

    gone = await ms.api.request("DELETE", f"{base}/rows/{key}", json={"expected": {"qty": 1}})
    assert gone.status_code == 409
    done = await ms.api.request("DELETE", f"{base}/rows/{key}", json={"expected": {"qty": 42}})
    assert done.status_code == 204
    assert ms.sql(f"SELECT COUNT(*) FROM {edit_table} WHERE name = 'new'")[0][0] == 0


@pytest.mark.security
async def test_forged_row_keys_are_rejected(ms, edit_table):
    import base64

    base = ms.table(edit_table)
    bad = [
        "garbage!!",
        base64.urlsafe_b64encode(b'{"r":[1,2]}').decode(),
        base64.urlsafe_b64encode(b'{"r":[null]}').decode(),
        base64.urlsafe_b64encode(b'{"r":["1 OR 1=1"]}').decode(),
        base64.urlsafe_b64encode(b'{"r":["1; DROP TABLE x"]}').decode(),
    ]
    for key in bad:
        r = await ms.api.request("DELETE", f"{base}/rows/{key}", json={"expected": {}})
        assert r.status_code in (400, 409, 422), (key, r.status_code)
    assert ms.sql(f"SELECT COUNT(*) FROM {edit_table}")[0][0] == 5


async def test_read_only_connection_refuses_writes(ms_ro, edit_table):
    r = await ms_ro.api.post(f"{ms_ro.table(edit_table)}/rows", {"values": {"name": "x"}})
    assert r.status_code == 403
    assert ms_ro.sql(f"SELECT COUNT(*) FROM {edit_table}")[0][0] == 5


async def test_tables_without_a_key_cannot_be_edited(ms):
    r = await ms.api.post(f"{ms.table('no_pk')}/rows", {"values": {"a": 9, "b": "z"}})
    assert r.status_code in (201, 409, 422)  # insert may work; update/delete must not
    page = await ms.rows("no_pk")
    assert page["row_keys"] == [None] * len(page["rows"])
    ms.sql("DELETE FROM no_pk WHERE a = 9")


async def test_bulk_update_and_delete_with_preview(ms, edit_table):
    base = ms.table(edit_table)
    sel = {"filters": [{"column": "qty", "op": "gte", "value": 4}]}
    prev = await ms.api.post(f"{base}/rows/bulk/preview", {"operation": "delete", "selector": sel})
    assert prev.status_code == 200, prev.text
    assert prev.json()["count"] == 2
    token = prev.json()["token"]
    forged = await ms.api.post(
        f"{base}/rows/bulk/delete", {"selector": sel, "expected_count": 2, "token": "nope"}
    )
    assert forged.status_code == 422
    deleted = await ms.api.post(
        f"{base}/rows/bulk/delete", {"selector": sel, "expected_count": 2, "token": token}
    )
    assert deleted.status_code == 200 and deleted.json() == {"affected": 2}
    assert ms.sql(f"SELECT COUNT(*) FROM {edit_table}")[0][0] == 3


async def test_export_csv_streams_all_rows(ms):
    r = await ms.api.get(f"{ms.table('customers')}/export", params={"format": "csv"})
    assert r.status_code == 200, r.text
    lines = r.text.strip().splitlines()
    assert len(lines) == 251 and lines[0].startswith("id,")


# ------------------------------------------------------------------------ SQL workspace


def run_payload(sql: str, **kw: Any) -> dict[str, Any]:
    return {"database": "shop", "schema": "dbo", "sql": sql, **kw}


async def run(m: Ms, sql: str, **kw: Any):
    return await m.api.post(m.url("/queries/execute"), run_payload(sql, **kw))


async def test_select_types_and_percent_signs(ms):
    r = await run(
        ms, "SELECT id, name, big, balance, '50% off; really' AS s FROM customers WHERE id = 1"
    )
    assert r.status_code == 200, r.text
    rs = r.json()["results"][0]
    assert [c["name"] for c in rs["columns"]] == ["id", "name", "big", "balance", "s"]
    assert rs["rows"][0][2] == "9007199254740994" and rs["rows"][0][3] == "10.25"
    assert rs["rows"][0][4] == "50% off; really"


async def test_result_limit_and_errors(ms):
    r = await run(ms, "SELECT id FROM customers ORDER BY id", max_rows=2)
    rs = r.json()["results"][0]
    assert len(rs["rows"]) == 2 and rs["truncated"] is True
    bad = await run(ms, "SELEKT 1")
    assert bad.status_code == 400 and bad.json()["code"] == "query_failed"
    assert "Traceback" not in bad.text


@pytest.mark.security
async def test_reads_with_hidden_side_effects_are_refused_and_nothing_changes(ms):
    for sql in (
        "SELECT 1 DELETE FROM secret_sink",
        "SELECT 1 EXEC sp_who",
    ):
        r = await run(ms, sql)
        assert r.status_code in (400, 403, 422), (sql, r.status_code, r.text)
    assert ms.sql("SELECT v FROM secret_sink") == [("must-not-change",)]
    # SELECT .. INTO is classified as a write: refused on a read-only connection
    ro = await create_connection(ms.api, "mssql", ms.info)
    r = await ms.api.post(
        f"connections/{ro['id']}/queries/execute",
        run_payload("SELECT * INTO evil_copy FROM customers"),
    )
    assert r.status_code == 403
    assert not ms.sql("SELECT name FROM sys.tables WHERE name = 'evil_copy'")


@pytest.mark.security
async def test_read_only_connections_only_run_reads(ms_ro, edit_table):
    ok = await run(ms_ro, f"SELECT COUNT(*) FROM {edit_table}")
    assert ok.status_code == 200
    for sql in (
        f"DELETE FROM {edit_table} WHERE id = 1",
        f"UPDATE {edit_table} SET qty = 0",
        f"DROP TABLE {edit_table}",
        f"CREATE TABLE x_{edit_table} (a int)",
    ):
        r = await run(ms_ro, sql, confirm_destructive=True)
        assert r.status_code == 403 and "read-only" in r.json()["detail"], sql
    assert ms_ro.sql(f"SELECT COUNT(*) FROM {edit_table}")[0][0] == 5


async def test_writes_commit_or_roll_back(ms, edit_table):
    done = await run(ms, f"UPDATE {edit_table} SET qty = 99 WHERE name = 'a'")
    assert done.status_code == 200 and done.json()["committed"] is True
    assert ms.sql(f"SELECT qty FROM {edit_table} WHERE name = 'a'")[0][0] == 99
    dry = await run(ms, f"UPDATE {edit_table} SET qty = 1 WHERE name = 'a'", rollback=True)
    assert dry.status_code == 200 and dry.json()["committed"] is False
    assert ms.sql(f"SELECT qty FROM {edit_table} WHERE name = 'a'")[0][0] == 99


async def test_statement_timeout_and_cancel(ms):
    started = time.monotonic()
    r = await run(ms, "WAITFOR DELAY '00:00:20'", timeout_s=1)
    assert r.status_code == 408 and r.json()["code"] == "statement_timeout"
    assert time.monotonic() - started < 10
    # cancel from another request while a statement is running
    op = str(uuid.uuid4())
    task = asyncio.create_task(run(ms, "WAITFOR DELAY '00:00:25'", operation_id=op))
    await asyncio.sleep(3)
    c = await ms.api.post(ms.url(f"/queries/{op}/cancel"), {})
    assert c.status_code in (200, 202, 204), c.text
    res = await asyncio.wait_for(task, 15)
    assert res.status_code in (400, 408, 409, 499), res.text


# ------------------------------------------------------------------------ adapter level


def spec(info: dict[str, Any], **over: Any) -> ConnectionSpec:
    return ConnectionSpec(
        id=uuid.uuid4(), db_type=DbType.MSSQL, host=info["host"], port=info["port"],
        database=info["database"], username=info["username"],
        password=over.pop("password", info["password"]), pool_max=2, **over,
    )  # fmt: skip


@pytest.mark.security
def test_adapter_refuses_writes_through_the_read_path_and_never_commits(mssql_managed):
    a = MSSQLAdapter(spec(mssql_managed), NetworkPolicy(allow_loopback=True), Limits())
    try:
        for sql in (
            "INSERT INTO secret_sink VALUES (2, 'pwned')",
            "UPDATE secret_sink SET v = 'pwned'",
            "DELETE FROM secret_sink",
        ):
            # the pooled read path rolls back even if a write slips through
            a._query("shop", sql, [])
        assert mssql_exec(mssql_managed, "SELECT v FROM secret_sink", "shop") == [
            ("must-not-change",)
        ]
        with pytest.raises(StatementNotAllowed):
            a.run_sql("shop", "dbo", ["DELETE FROM secret_sink"], write=False, script=False,
                      timeout_s=5, max_rows=5, rollback=False, on_start=lambda _r: None)  # fmt: skip
    finally:
        a.close()


def test_adapter_statement_timeout(mssql_managed):
    a = MSSQLAdapter(
        spec(mssql_managed, allow_writes=True), NetworkPolicy(allow_loopback=True), Limits()
    )
    try:
        started = time.monotonic()
        with pytest.raises(StatementTimeout):
            a.run_sql("shop", "dbo", ["WAITFOR DELAY '00:00:15'"], write=True, script=False,
                      timeout_s=1, max_rows=5, rollback=True, on_start=lambda _r: None)  # fmt: skip
        assert time.monotonic() - started < 8
    finally:
        a.close()


@pytest.mark.security
def test_wrong_credentials_do_not_leak_the_password(mssql_managed):
    bad = "Wr0ng-Credential-Value"
    a = MSSQLAdapter(
        spec(mssql_managed, password=bad), NetworkPolicy(allow_loopback=True), Limits()
    )
    try:
        res = a.test()
        assert res.ok is False and res.error_code == "connection_failed"
        assert bad not in (res.error or "")
    finally:
        a.close()


@pytest.mark.security
def test_network_policy_is_enforced(mssql_managed):
    strict = NetworkPolicy(allow_cidrs=("10.0.0.0/8",), allow_loopback=False)
    a = MSSQLAdapter(spec(mssql_managed), strict, Limits())
    try:
        res = a.test()
        assert res.ok is False and res.error_code == "network_policy_denied"
    finally:
        a.close()


@pytest.mark.security
def test_certificate_verification_is_refused_rather_than_silently_skipped(mssql_managed):
    from app.database.core.base import TlsMode

    a = MSSQLAdapter(
        spec(mssql_managed, tls_mode=TlsMode.VERIFY_FULL),
        NetworkPolicy(allow_loopback=True),
        Limits(),
    )
    try:
        res = a.test()
        assert res.ok is False and "verification" in (res.error or "").lower()
    finally:
        a.close()


# ------------------------------------------------------------------------ administration


async def test_database_schema_and_index_administration(ms, edit_table):
    name = uniq("db")
    r = await ms.api.post(ms.url("/databases"), {"name": name})
    assert r.status_code == 201, r.text
    assert name in [d["name"] for d in (await ms.api.get(ms.url("/databases"))).json()]
    assert (await ms.api.post(ms.url("/databases"), {"name": name})).status_code == 409
    for body in ({}, {"confirm": "nope"}):
        bad = await ms.api.delete(ms.url(f"/databases/{name}"), json=body)
        assert bad.status_code == 428
    assert (
        await ms.api.delete(ms.url(f"/databases/{name}"), json={"confirm": name})
    ).status_code == 204
    assert name not in [d["name"] for d in (await ms.api.get(ms.url("/databases"))).json()]
    assert (
        await ms.api.delete(ms.url("/databases/shop"), json={"confirm": "shop"})
    ).status_code == 409
    assert (
        await ms.api.delete(ms.url("/databases/master"), json={"confirm": "master"})
    ).status_code in (403, 404)

    schema = uniq("s")
    assert (
        await ms.api.post(ms.url("/databases/shop/schemas"), {"name": schema})
    ).status_code == 201
    assert schema in [
        s["name"] for s in (await ms.api.get(ms.url("/databases/shop/schemas"))).json()
    ]
    assert (
        await ms.api.delete(ms.url("/databases/shop/schemas/dbo"), json={"confirm": "dbo"})
    ).status_code in (403, 404)
    assert (
        await ms.api.delete(ms.url(f"/databases/shop/schemas/{schema}"), json={"confirm": schema})
    ).status_code == 204

    url = ms.table(edit_table)
    created = await ms.api.post(f"{url}/indexes", {"name": "ix_name", "columns": ["name", "qty"]})
    assert created.status_code == 201, created.text
    idx = {i["name"]: i for i in (await ms.api.get(f"{url}/structure")).json()["indexes"]}
    assert idx["ix_name"]["columns"] == ["name", "qty"] and not idx["ix_name"]["unique"]
    assert (await ms.api.delete(f"{url}/indexes/ix_name", json={})).status_code == 204
    pk = next(i["name"] for i in idx.values() if i["primary"])
    assert (await ms.api.delete(f"{url}/indexes/{pk}", json={})).status_code == 409


def login_ok(info: dict[str, Any], user: str, password: str, stmt: str = "SELECT 1") -> bool:
    try:
        conn = pymssql.connect(
            server=info["host"], port=str(info["port"]), user=user, password=password,
            database="shop", login_timeout=10,
        )  # fmt: skip
    except pymssql.Error:
        return False
    try:
        cur = conn.cursor()
        cur.execute(stmt)
        cur.fetchall()
        return True
    except pymssql.Error:
        return False
    finally:
        conn.close()


async def test_logins_and_privileges(ms, mssql_managed):
    name = uniq("lg")
    r = await ms.api.post(ms.url("/principals"), {"name": name, "password": PW})
    assert r.status_code == 201, r.text
    assert PW not in r.text
    listed = (await ms.api.get(ms.url("/principals"))).json()
    assert name in [p["name"] for p in listed] and PW not in str(listed)
    sa = next(p for p in listed if p["name"] == "sa")
    assert sa["protected"] is True
    assert login_ok(mssql_managed, name, PW) is False  # no database user yet
    body = {"privileges": ["select"], "target": {"kind": "table", "database": "shop",
                                                  "schema": "dbo", "object": "customers"}}  # fmt: skip
    g = await ms.api.post(ms.url(f"/principals/{name}/grants"), body)
    assert g.status_code == 204, g.text
    assert login_ok(mssql_managed, name, PW, "SELECT COUNT(*) FROM dbo.customers")
    assert not login_ok(mssql_managed, name, PW, "DELETE FROM dbo.secret_sink")
    grants = (
        await ms.api.get(ms.url(f"/principals/{name}/grants"), params={"database": "shop"})
    ).json()
    entry = next(e for e in grants if e["object"] == "customers")
    assert entry["privileges"] == ["SELECT"]
    bad = await ms.api.post(
        ms.url(f"/principals/{name}/grants"),
        {"privileges": ["CONTROL"], "target": {"kind": "database", "database": "shop"}},
    )
    assert bad.status_code == 422  # not on the allowlist
    rv = await ms.api.request("DELETE", ms.url(f"/principals/{name}/grants"), json=body)
    assert rv.status_code == 204
    assert not login_ok(mssql_managed, name, PW, "SELECT COUNT(*) FROM dbo.customers")
    own = await ms.api.delete(ms.url("/principals/sa"), json={"confirm": "sa"})
    assert own.status_code in (403, 404)
    assert (
        await ms.api.delete(ms.url(f"/principals/{name}"), json={"confirm": name})
    ).status_code == 204
    mssql_exec(mssql_managed, f"IF USER_ID('{name}') IS NOT NULL DROP USER [{name}]", "shop")
