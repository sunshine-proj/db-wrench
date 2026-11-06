"""ClickHouse (ADR-0020): browsing, SQL workspace, server-enforced read-only, append-only editor."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import pytest

from app.connections.network import NetworkPolicy
from app.database.adapters.clickhouse import ClickHouseAdapter
from app.database.core.base import ConnectionSpec, DbType, Limits
from app.errors import ConnectionFailed, QueryFailed, StatementTimeout
from tests.integration.conftest import clickhouse_exec, create_connection


class Ch:
    def __init__(self, api: Any, cid: str, info: dict[str, Any]):
        self.api, self.cid, self.info = api, cid, info

    def url(self, path: str = "") -> str:
        return f"connections/{self.cid}{path}"

    def table(self, name: str) -> str:
        return self.url(f"/databases/shop/tables/{name}")

    def sql(self, stmt: str) -> list[list]:
        return clickhouse_exec(self.info, stmt)

    async def rows(self, table: str = "customers", **params: Any) -> dict[str, Any]:
        for k in ("sort", "filters", "columns", "search_columns"):
            if k in params and not isinstance(params[k], str):
                params[k] = json.dumps(params[k])
        r = await self.api.get(f"{self.table(table)}/rows", params=params)
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
async def ch(admin, clickhouse_managed) -> Ch:
    c = await create_connection(admin, "clickhouse", clickhouse_managed, read_only=False)
    return Ch(admin, c["id"], clickhouse_managed)


@pytest.fixture
async def ch_ro(admin, clickhouse_managed) -> Ch:
    c = await create_connection(admin, "clickhouse", clickhouse_managed)
    return Ch(admin, c["id"], clickhouse_managed)


async def run(m: Ch, sql: str, **kw: Any):
    return await m.api.post(m.url("/queries/execute"), {"database": "shop", "sql": sql, **kw})


# ------------------------------------------------------------------------ explorer


async def test_connection_and_capabilities(ch):
    r = await ch.api.post(ch.url("/test"), {})
    assert r.status_code == 200 and r.json()["server_version"].startswith("ClickHouse")
    caps = (await ch.api.get(ch.url())).json()["capabilities"]
    assert caps["engine"] == "clickhouse" and caps["supports_schemas"] is False


async def test_databases_and_tables(ch):
    dbs = [d["name"] for d in (await ch.api.get(ch.url("/databases"))).json()]
    assert "shop" in dbs and "system" not in dbs and "INFORMATION_SCHEMA" not in dbs
    tables = {t["name"]: t for t in (await ch.api.get(ch.url("/databases/shop/tables"))).json()}
    assert {"customers", "orders", "no_key", "vip", "Weird %Table"} <= set(tables)
    assert tables["vip"]["kind"] == "view" and tables["customers"]["kind"] == "table"
    assert (await ch.api.get(ch.url("/databases/nope/tables"))).status_code == 404
    assert (await ch.api.get(ch.url("/databases/shop/schemas"))).status_code in (400, 404, 409, 422)


async def test_structure(ch):
    st = (await ch.api.get(f"{ch.table('customers')}/structure")).json()
    cols = {c["name"]: c for c in st["columns"]}
    assert cols["email"]["nullable"] is True and cols["name"]["nullable"] is False
    assert cols["balance"]["data_type"] == "Decimal(12, 2)"
    assert st["primary_key"] == []  # a sparse index is not an identity
    assert any(i["name"] == "(sorting key)" and i["columns"] == ["id"] for i in st["indexes"])
    assert st["estimated_rows"] == 250


# ------------------------------------------------------------------------ browser


async def test_offset_pagination_walks_everything(ch):
    first = await ch.rows(limit=100)
    assert first["pagination"] != "keyset" and first["editable"] is False
    seen, cursor = [r[0] for r in first["rows"]], first["next_cursor"]
    while cursor:
        page = await ch.rows(limit=100, cursor=cursor)
        seen += [r[0] for r in page["rows"]]
        cursor = page["next_cursor"]
    assert sorted(seen, key=int) == [str(i) for i in range(1, 251)] or sorted(seen) == sorted(
        i for i in range(1, 251)
    )
    assert len(seen) == 250 and len(set(map(str, seen))) == 250


async def test_type_fidelity(ch):
    page = await ch.rows(limit=1, sort=[{"column": "id", "direction": "asc"}])
    row = dict(zip([c["name"] for c in page["columns"]], page["rows"][0], strict=True))
    assert row["balance"] == "10.25" and row["big"] == "9007199254740993"
    assert row["id"] in (1, "1") and row["tags"] == ["a", "b"]
    assert isinstance(row["notes"], dict) and row["notes"].get("$truncated")


async def test_sort_filter_search_null_and_odd_names(ch):
    s = await ch.rows(sort=[{"column": "balance", "direction": "desc"}], limit=2)
    assert str(s["rows"][0][0]) == "250"
    nulls = await ch.rows(filters=[{"column": "email", "op": "is_null"}], limit=1000)
    assert len(nulls["rows"]) == 25
    eq = await ch.rows(filters=[{"column": "balance", "op": "eq", "value": "10.25"}], limit=10)
    assert len(eq["rows"]) == 1
    big = await ch.rows(
        filters=[{"column": "big", "op": "gte", "value": "9007199254741000"}], limit=1000
    )
    assert len(big["rows"]) == 250 - 7
    like = await ch.rows(search="CUSTOMER 07", search_columns=["name"], limit=1000)
    assert 0 < len(like["rows"]) <= 10  # ILIKE: case-insensitive
    weird = await ch.rows("Weird %Table")
    assert len(weird["rows"]) == 2
    nokey = await ch.rows("no_key")
    assert len(nokey["rows"]) == 2 and nokey["warnings"]


async def test_unknown_columns_are_rejected_not_interpolated(ch):
    r = await ch.api.get(
        f"{ch.table('customers')}/rows",
        params={"sort": json.dumps([{"column": "id`; DROP TABLE shop.customers;--"}])},
    )
    assert r.status_code == 422
    assert ch.sql("SELECT count() FROM shop.customers")[0][0] == "250"


async def test_export_csv(ch):
    r = await ch.api.get(f"{ch.table('customers')}/export", params={"format": "csv"})
    assert r.status_code == 200, r.text
    assert len(r.text.strip().splitlines()) == 251


# ------------------------------------------------------------------------ append-only editor


async def test_row_editing_import_and_bulk_are_refused(ch):
    base = ch.table("customers")
    for call in (
        ch.api.post(f"{base}/rows", {"values": {"id": 999, "name": "x"}}),
        ch.api.post(
            f"{base}/rows/bulk/preview", {"operation": "delete", "selector": {"all_rows": True}}
        ),
    ):
        r = await call
        assert r.status_code in (400, 403, 409, 422), r.text
    assert ch.sql("SELECT count() FROM shop.customers")[0][0] == "250"


async def test_administration_is_left_to_sql(ch):
    r = await ch.api.post(ch.url("/databases"), {"name": "nope_db"})
    assert r.status_code in (400, 403, 409, 422), r.text
    assert "nope_db" not in [d["name"] for d in (await ch.api.get(ch.url("/databases"))).json()]


# ------------------------------------------------------------------------ SQL workspace


async def test_select_and_literals(ch):
    r = await run(
        ch, "SELECT id, name, big, balance, '50% off; {x} %s' AS s FROM customers WHERE id = 1"
    )
    assert r.status_code == 200, r.text
    rs = r.json()["results"][0]
    assert [c["name"] for c in rs["columns"]] == ["id", "name", "big", "balance", "s"]
    assert rs["rows"][0][2] == "9007199254740993" and rs["rows"][0][3] == "10.25"
    assert rs["rows"][0][4] == "50% off; {x} %s"


async def test_limits_errors_and_multi_statement(ch):
    r = await run(ch, "SELECT id FROM customers ORDER BY id", max_rows=2)
    rs = r.json()["results"][0]
    assert len(rs["rows"]) == 2 and rs["truncated"] is True
    bad = await run(ch, "SELEKT 1")
    assert bad.status_code == 400 and bad.json()["code"] == "query_failed"
    assert "Traceback" not in bad.text
    ms = await run(ch, "SELECT 1; SELECT 2")
    assert ms.status_code in (400, 403, 422)


@pytest.mark.security
async def test_the_server_enforces_read_only_even_for_disguised_statements(ch, ch_ro):
    for sql in (
        "INSERT INTO secret_sink VALUES (2, 'pwned')",
        "ALTER TABLE secret_sink DELETE WHERE 1",
        "DROP TABLE secret_sink",
        "TRUNCATE TABLE secret_sink",
        "CREATE TABLE evil (a Int8) ENGINE = Memory",
    ):
        r = await run(ch_ro, sql, confirm_destructive=True)
        assert r.status_code == 403, (sql, r.text)
    assert ch.sql("SELECT v FROM shop.secret_sink") == [["must-not-change"]]

    # reads that try to turn read-only off or reach outside the database fail *in the server*
    adapter = ClickHouseAdapter(
        spec(ch.info, allow_writes=True), NetworkPolicy(allow_loopback=True), Limits()
    )
    try:
        for sql in (
            "SELECT 1 SETTINGS readonly = 0",
            "SELECT * FROM url('http://127.0.0.1:1/x', 'CSV', 'a String')",
            "SELECT * FROM file('/etc/passwd', 'CSV', 'a String')",
            "INSERT INTO shop.secret_sink VALUES (3, 'pwned')",
            "SET readonly = 0",
        ):
            with pytest.raises(QueryFailed):
                adapter.run_sql("shop", None, [sql], write=False, script=False, timeout_s=5,
                                max_rows=5, rollback=False, on_start=lambda _r: None)  # fmt: skip
        assert ch.sql("SELECT v FROM shop.secret_sink") == [["must-not-change"]]
    finally:
        adapter.close()


async def test_writes_need_a_writable_connection_and_commit(ch):
    done = await run(ch, "INSERT INTO secret_sink VALUES (50, 'new')")
    assert done.status_code == 200, done.text
    assert ch.sql("SELECT count() FROM shop.secret_sink WHERE id = 50")[0][0] == "1"
    rb = await run(ch, "INSERT INTO secret_sink VALUES (51, 'x')", rollback=True)
    assert rb.status_code in (400, 422)  # no transactions
    ch.sql("ALTER TABLE shop.secret_sink DELETE WHERE id >= 50 SETTINGS mutations_sync = 2")


async def test_statement_timeout_and_cancel(ch):
    heavy = "SELECT count() FROM numbers(100000000000) WHERE cityHash64(number) = 1"
    started = time.monotonic()
    r = await run(ch, heavy, timeout_s=1)
    assert r.status_code == 408 and r.json()["code"] == "statement_timeout"
    assert time.monotonic() - started < 15
    op = str(uuid.uuid4())
    task = asyncio.create_task(run(ch, heavy, operation_id=op, timeout_s=60))
    await asyncio.sleep(3)
    c = await ch.api.post(ch.url(f"/queries/{op}/cancel"), {})
    assert c.status_code in (200, 202, 204), c.text
    res = await asyncio.wait_for(task, 20)
    assert res.status_code in (400, 408, 409, 499), res.text


# ------------------------------------------------------------------------ adapter level


def spec(info: dict[str, Any], **over: Any) -> ConnectionSpec:
    return ConnectionSpec(
        id=uuid.uuid4(), db_type=DbType.CLICKHOUSE, host=info["host"], port=info["port"],
        database=info["database"], username=info["username"],
        password=over.pop("password", info["password"]), pool_max=2, **over,
    )  # fmt: skip


@pytest.mark.security
def test_wrong_credentials_do_not_leak_the_password(clickhouse_managed):
    bad = "Wr0ng-Credential-Value"
    a = ClickHouseAdapter(
        spec(clickhouse_managed, password=bad), NetworkPolicy(allow_loopback=True), Limits()
    )
    try:
        res = a.test()
        assert res.ok is False and res.error_code == "connection_failed"
        assert bad not in (res.error or "")
    finally:
        a.close()


@pytest.mark.security
def test_network_policy_is_enforced(clickhouse_managed):
    strict = NetworkPolicy(allow_cidrs=("10.0.0.0/8",), allow_loopback=False)
    a = ClickHouseAdapter(spec(clickhouse_managed), strict, Limits())
    try:
        res = a.test()
        assert res.ok is False and res.error_code == "network_policy_denied"
    finally:
        a.close()


def test_unreachable_server_is_a_typed_failure(clickhouse_managed):
    a = ClickHouseAdapter(
        spec({**clickhouse_managed, "port": 1}), NetworkPolicy(allow_loopback=True), Limits()
    )
    try:
        with pytest.raises(ConnectionFailed):
            a.list_databases()
    finally:
        a.close()


def test_adapter_timeout_is_a_typed_error(clickhouse_managed):
    a = ClickHouseAdapter(
        spec(clickhouse_managed), NetworkPolicy(allow_loopback=True), Limits(statement_timeout_s=1)
    )
    try:
        with pytest.raises(StatementTimeout):
            a._query(
                "shop", "SELECT count() FROM numbers(100000000000) WHERE cityHash64(number) = 1", []
            )
    finally:
        a.close()
