"""Phase 4: SQL workspace on real PostgreSQL and MySQL."""

import asyncio
import csv
import io
import json
import time
import uuid

import pytest

from tests.integration.conftest import create_connection, grant, writable_conn

HEAVY_MYSQL = (
    "SELECT COUNT(*) FROM customers a, customers b, customers c, customers d "
    "WHERE MD5(a.id + b.id + c.id + d.id) LIKE 'abc%%'"
)


def payload(w, sql, **kw):
    d = {"sql": sql, "database": "shop", **kw}
    if w.engine == "postgresql":
        d.setdefault("schema", "public")
    return d


async def run(api, w, sql, cid=None, **kw):
    return await api.post(f"connections/{cid or w.cid}/queries/execute", payload(w, sql, **kw))


def slow_sql(w):
    return "SELECT pg_sleep(30)" if w.engine == "postgresql" else HEAVY_MYSQL.replace("%%", "%")


async def wait_for_ref(app_sql, cid, wait_s=15):
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        rows = app_sql(
            "SELECT id, backend_ref::text FROM operations WHERE status = 'running' AND connection_id = %s",
            (cid,),
        )
        if rows and json.loads(rows[0][1]):
            return str(rows[0][0])
        await asyncio.sleep(0.2)
    raise AssertionError("query never registered a backend reference")


# ----------------------------------------------------------------------------- reading


async def test_select_returns_typed_columns_and_values(wr):
    r = await run(wr.api, wr, f"SELECT id, name, big, price, meta FROM {wr.table} ORDER BY id")
    assert r.status_code == 200, r.text
    out = r.json()
    rs = out["results"][0]
    assert [c["name"] for c in rs["columns"]] == ["id", "name", "big", "price", "meta"]
    assert all(c["type"] for c in rs["columns"])
    assert len(rs["rows"]) == 5 and rs["truncated"] is False
    assert rs["rows"][0][2] == "9007199254740993"  # big integers stay exact
    assert rs["rows"][1][2] is None
    assert out["operation_id"] and out["committed"] is False and out["elapsed_ms"] >= 0


async def test_percent_signs_and_semicolons_in_literals_survive(wr):
    r = await run(wr.api, wr, "SELECT '50%% off; really' AS a, 'x%s' AS b".replace("%%", "%"))
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["rows"][0] == ["50% off; really", "x%s"]


async def test_results_are_limited_and_flagged(wr):
    r = await run(wr.api, wr, f"SELECT id FROM {wr.table} ORDER BY id", max_rows=2)
    rs = r.json()["results"][0]
    assert len(rs["rows"]) == 2 and rs["truncated"] is True


async def test_sql_errors_are_reported_cleanly_and_recorded(wr):
    r = await run(wr.api, wr, "SELEKT 1")
    assert r.status_code == 400 and r.json()["code"] == "query_failed"
    assert "Traceback" not in r.text
    r2 = await run(wr.api, wr, "SELECT * FROM table_that_does_not_exist")
    assert r2.status_code == 400
    hist = (await wr.api.get(f"connections/{wr.cid}/queries/history")).json()
    assert hist[0]["status"] == "failed" and hist[0]["error_code"] == "query_failed"


async def test_statement_timeout(wr):
    sql = "SELECT pg_sleep(5)" if wr.engine == "postgresql" else HEAVY_MYSQL.replace("%%", "%")
    started = time.monotonic()
    r = await run(wr.api, wr, sql, timeout_s=1)
    assert r.status_code == 408 and r.json()["code"] == "statement_timeout"
    assert time.monotonic() - started < 8
    hist = (await wr.api.get(f"connections/{wr.cid}/queries/history")).json()
    assert hist[0]["status"] == "timeout"
    # the workspace keeps working afterwards
    assert (await run(wr.api, wr, "SELECT 1")).status_code == 200


# ----------------------------------------------------------------------------- cancellation


async def test_a_running_query_can_be_cancelled(wr, app_sql):
    long_run = asyncio.create_task(run(wr.api, wr, slow_sql(wr)))
    op_id = await wait_for_ref(app_sql, wr.cid)
    started = time.monotonic()
    c = await wr.api.post(f"connections/{wr.cid}/queries/{op_id}/cancel")
    assert c.status_code == 202 and c.json()["status"] == "cancelling"
    r = await asyncio.wait_for(long_run, timeout=20)
    assert r.status_code == 409 and r.json()["code"] == "cancelled"
    assert time.monotonic() - started < 15  # nowhere near the 30 s the query would take
    assert app_sql("SELECT status FROM operations WHERE id = %s", (op_id,))[0][0] == "cancelled"
    assert (await wr.api.get(f"connections/{wr.cid}/queries/history")).json()[0][
        "status"
    ] == "cancelled"
    again = await wr.api.post(f"connections/{wr.cid}/queries/{op_id}/cancel")
    assert again.status_code == 409  # already finished
    assert (await run(wr.api, wr, "SELECT 1")).status_code == 200


@pytest.mark.security
async def test_only_the_owner_or_an_access_manager_can_cancel(wr, admin, new_user, app_sql):
    owner, other = await new_user("Database Admin"), await new_user("Database Admin")
    await grant(admin, wr.cid, owner, "Database Admin")
    await grant(admin, wr.cid, other, "Database Admin")
    long_run = asyncio.create_task(run(owner, wr, slow_sql(wr)))
    op_id = await wait_for_ref(app_sql, wr.cid)
    viewer = await new_user("Viewer")
    await grant(admin, wr.cid, viewer, "Viewer")
    assert (await viewer.post(f"connections/{wr.cid}/queries/{op_id}/cancel")).status_code == 404
    # a Database Admin holds connection.manage_access, so may cancel others' queries
    assert (await other.post(f"connections/{wr.cid}/queries/{op_id}/cancel")).status_code == 202
    assert (await asyncio.wait_for(long_run, 20)).status_code == 409
    ev = (await admin.get("audit/events", params={"action": "query.cancel"})).json()["events"][0]
    assert ev["target"]["owner_is_actor"] is False


async def test_per_user_concurrency_limit(wr, admin, app_sql, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "concurrent_queries_per_user", 1)
    first = asyncio.create_task(run(admin, wr, slow_sql(wr)))
    op_id = await wait_for_ref(app_sql, wr.cid)
    second = await run(admin, wr, "SELECT 1")
    assert second.status_code == 429 and second.json()["code"] == "rate_limited"
    await admin.post(f"connections/{wr.cid}/queries/{op_id}/cancel")
    await asyncio.wait_for(first, 20)
    assert (await run(admin, wr, "SELECT 1")).status_code == 200  # slot is free again


# ----------------------------------------------------------------------------- read-only and escapes


@pytest.mark.security
@pytest.mark.parametrize(
    "sql",
    [
        "SET TRANSACTION READ WRITE",
        "SET SESSION transaction_read_only = 0",
        "BEGIN",
        "COMMIT; SELECT 1",
        "SET ROLE postgres",
        "RESET ALL",
        "USE mysql",
        "KILL 1",
    ],
)
async def test_session_and_transaction_control_is_refused(wr, sql):
    r = await run(wr.api, wr, sql)
    assert r.status_code == 422 and r.json()["code"] == "statement_not_allowed", r.text
    denied = (
        await wr.api.get("audit/events", params={"action": "query.execute", "result": "denied"})
    ).json()["events"]
    assert denied and denied[0]["detail"]["reason"] == "statement_not_allowed"


@pytest.mark.security
async def test_read_only_connections_only_run_reads(wr, admin, pg_managed, mysql_managed):
    info = pg_managed if wr.engine == "postgresql" else mysql_managed
    ro = await create_connection(admin, wr.engine, info)  # read-only by default
    ok = await run(admin, wr, f"SELECT COUNT(*) FROM {wr.table}", cid=ro["id"])
    assert ok.status_code == 200
    for sql in (
        f"DELETE FROM {wr.table} WHERE id = 1",
        f"UPDATE {wr.table} SET qty = 0 WHERE id = 1",
        f"CREATE TABLE x_{wr.table} (a int)",
        f"DROP TABLE {wr.table}",
    ):
        r = await run(admin, wr, sql, cid=ro["id"], confirm_destructive=True)
        assert r.status_code == 403 and "read-only" in r.json()["detail"], sql
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == 5


@pytest.mark.security
async def test_the_database_itself_refuses_writes_hidden_in_a_select(wr, admin, pg_managed):
    """Classification is not the boundary: a SELECT with a side effect fails inside the
    read-only transaction even on a writable connection."""
    if wr.engine != "postgresql":
        pytest.skip("PostgreSQL-specific side-effect function")
    wr.sql("CREATE SEQUENCE IF NOT EXISTS side_effect_seq")
    r = await run(wr.api, wr, "SELECT nextval('side_effect_seq')")
    assert r.status_code == 400 and "read-only" in r.json()["detail"]
    assert wr.sql("SELECT is_called FROM side_effect_seq")[0][0] is False


@pytest.mark.security
def test_adapters_refuse_the_read_only_escape_even_when_called_directly(pg_managed, mysql_managed):
    """ADR-0008 at adapter level, bypassing the classifier entirely."""
    from app.connections.network import NetworkPolicy
    from app.database.adapters.mysql import MySQLAdapter
    from app.database.adapters.postgres import PostgresAdapter
    from app.database.core.base import ConnectionSpec, DbType, Limits
    from app.errors import QueryFailed

    def spec(t, info):
        return ConnectionSpec(
            id=uuid.uuid4(),
            db_type=t,
            host=info["host"],
            port=info["port"],
            database="shop",
            username=info["username"],
            password=info["password"],
            pool_max=2,
            allow_writes=True,
        )

    pg = PostgresAdapter(
        spec(DbType.POSTGRESQL, pg_managed), NetworkPolicy(allow_loopback=True), Limits()
    )
    my = MySQLAdapter(
        spec(DbType.MYSQL, mysql_managed), NetworkPolicy(allow_loopback=True), Limits()
    )
    noop = lambda _ref: None  # noqa: E731
    kw = {
        "write": False,
        "script": True,
        "timeout_s": 5,
        "max_rows": 10,
        "rollback": False,
        "on_start": noop,
    }
    try:
        with pytest.raises(QueryFailed, match="must be set before any query"):
            pg.run_sql(
                "shop",
                "public",
                ["SET TRANSACTION READ WRITE", "CREATE TABLE escaped (a int)"],
                **kw,
            )
        # MySQL: flipping the session variable from inside the read-only transaction must not
        # make the following write succeed.
        with pytest.raises(QueryFailed):
            my.run_sql(
                "shop",
                None,
                ["SET SESSION transaction_read_only = 0", "CREATE TABLE escaped (a int)"],
                **kw,
            )
        with pytest.raises(QueryFailed):
            my.run_sql(
                "shop",
                None,
                [
                    "SET SESSION transaction_read_only = 0",
                    "INSERT INTO secret_sink VALUES (99, 'pwned')",
                ],
                **kw,
            )
    finally:
        pg.close()
        my.close()
    assert not any(
        "escaped" in str(r)
        for r in __import__("psycopg")
        .connect(
            host=pg_managed["host"],
            port=pg_managed["port"],
            user="postgres",
            password="pw",
            dbname="shop",
        )
        .execute("SELECT tablename FROM pg_tables")
        .fetchall()
    )


@pytest.mark.security
async def test_user_sql_runs_on_isolated_connections(wr, admin):
    """A session setting changed by one query must not be visible to the next (no pooled state)."""
    if wr.engine == "postgresql":
        await run(wr.api, wr, "SELECT set_config('application_name', 'tainted', false)")
        r = await run(wr.api, wr, "SHOW application_name")
        assert r.json()["results"][0]["rows"][0][0] == "db-wrench"
    else:
        await run(wr.api, wr, "SELECT @tainted := 1")
        r = await run(wr.api, wr, "SELECT @tainted")
        assert r.json()["results"][0]["rows"][0][0] is None


# ----------------------------------------------------------------------------- permissions & policy


@pytest.mark.security
async def test_roles_and_scripts(wr, admin, new_user):
    viewer, editor, dba = (
        await new_user("Viewer"),
        await new_user("Editor"),
        await new_user("Database Admin"),
    )
    for u, role in ((viewer, "Viewer"), (editor, "Editor"), (dba, "Database Admin")):
        await grant(admin, wr.cid, u, role)
    assert (await run(viewer, wr, "SELECT 1")).status_code == 200
    r = await run(viewer, wr, f"UPDATE {wr.table} SET qty = 1 WHERE id = 1")
    assert r.status_code == 403 and "execute_write" in r.json()["detail"]
    # Editor may write but not run scripts; the Database Admin may
    assert (
        await run(editor, wr, f"UPDATE {wr.table} SET qty = 11 WHERE id = 1")
    ).status_code == 200
    two = await run(editor, wr, "SELECT 1; SELECT 2")
    assert two.status_code == 403 and "execute_script" in two.json()["detail"]
    ok = await run(dba, wr, "SELECT 1 AS a; SELECT 2 AS b")
    assert ok.status_code == 200
    sets = ok.json()["results"]
    assert [s["rows"][0][0] for s in sets] == [1, 2] and len(ok.json()["statements"]) == 2


async def test_write_statements_commit_and_can_be_rolled_back(wr):
    r = await run(wr.api, wr, f"UPDATE {wr.table} SET qty = 77 WHERE name = 'a'")
    assert r.status_code == 200 and r.json()["committed"] is True
    assert r.json()["results"][0]["row_count"] == 1
    assert wr.sql(f"SELECT qty FROM {wr.table} WHERE name = 'a'")[0][0] == 77
    dry = await run(wr.api, wr, f"UPDATE {wr.table} SET qty = 5 WHERE name = 'a'", rollback=True)
    assert dry.status_code == 200 and dry.json()["committed"] is False
    assert dry.json()["results"][0]["row_count"] == 1  # shows what it *would* have done
    assert wr.sql(f"SELECT qty FROM {wr.table} WHERE name = 'a'")[0][0] == 77  # ...but did nothing
    assert (await run(wr.api, wr, "SELECT 1", rollback=True)).status_code == 422


async def test_destructive_statements_need_explicit_confirmation(wr):
    for sql in (
        f"DELETE FROM {wr.table}",
        f"UPDATE {wr.table} SET qty = 0",
        f"TRUNCATE TABLE {wr.table}",
        f"DROP TABLE {wr.table}",
    ):
        r = await run(wr.api, wr, sql)
        assert r.status_code == 428 and r.json()["code"] == "confirmation_required", sql
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == 5
    ok = await run(wr.api, wr, f"DELETE FROM {wr.table}", confirm_destructive=True)
    assert ok.status_code == 200 and wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == 0


@pytest.mark.security
async def test_production_writes_need_the_connection_name(wr, admin):
    prod = await writable_conn(admin, wr, environment="production")
    name = (await admin.get(f"connections/{prod.cid}")).json()["name"]
    sql = f"UPDATE {wr.table} SET qty = 1 WHERE id = 1"
    for confirm in (None, "wrong"):
        assert (await run(admin, wr, sql, cid=prod.cid, confirm=confirm)).status_code == 428
    assert (await run(admin, wr, sql, cid=prod.cid, confirm=name)).status_code == 200
    # reads and dry runs on production are not blocked
    assert (await run(admin, wr, "SELECT 1", cid=prod.cid)).status_code == 200
    assert (await run(admin, wr, sql, cid=prod.cid, rollback=True)).status_code == 200


async def test_ddl_runs_with_write_permission(wr):
    r = await run(wr.api, wr, f"CREATE TABLE made_{wr.table} (a int)")
    assert r.status_code == 200
    assert wr.sql(f"SELECT COUNT(*) FROM made_{wr.table}")[0][0] == 0
    wr.sql(f"DROP TABLE made_{wr.table}")


@pytest.mark.security
async def test_sensitive_fields_close_free_form_sql_to_users_without_permission(
    wr, admin, new_user
):
    marker = {
        "database": "shop",
        "schema": "public" if wr.engine == "postgresql" else "",
        "object": wr.table,
        "field": "note",
    }
    assert (
        await admin.put(f"connections/{wr.cid}/sensitive", {"markers": [marker]})
    ).status_code == 200
    editor = await new_user("Editor")
    await grant(admin, wr.cid, editor, "Editor")
    r = await run(editor, wr, f"SELECT note FROM {wr.table}")
    assert r.status_code == 403 and "sensitive" in r.json()["detail"]
    assert (
        await run(admin, wr, f"SELECT note FROM {wr.table}")
    ).status_code == 200  # may read sensitive


async def test_wrong_connection_family_and_validation(wr, admin, mongo_managed):
    mongo = await create_connection(admin, "mongodb", mongo_managed)
    r = await run(admin, wr, "SELECT 1", cid=mongo["id"])
    assert r.status_code == 422 and r.json()["code"] == "unsupported_capability"
    assert (await run(wr.api, wr, "   ")).status_code in (400, 422)
    assert (
        await admin.post(f"connections/{wr.cid}/queries/execute", {"sql": "SELECT 1"})
    ).status_code == 422
    bad_db = await run(wr.api, wr, "SELECT 1", database="no_such_db")
    assert bad_db.status_code == 404


# ----------------------------------------------------------------------------- audit & history


async def test_audit_records_queries_without_their_text(wr):
    marker = f"needle_{uuid.uuid4().hex[:8]}"
    await run(wr.api, wr, f"SELECT '{marker}' AS v")
    events = (
        await wr.api.get(
            "audit/events", params={"connection_id": wr.cid, "action": "query.execute"}
        )
    ).json()["events"]
    ev = events[0]
    assert (
        ev["detail"]["kinds"] == ["read"] and ev["detail"]["digest"] and ev["detail"]["rows"] == 1
    )
    assert marker not in json.dumps(events)


async def test_history_is_private_paginated_and_deletable(wr, admin, new_user):
    for i in range(3):
        await run(wr.api, wr, f"SELECT {i} AS n")
    hist = (await wr.api.get(f"connections/{wr.cid}/queries/history", params={"limit": 2})).json()
    assert (
        len(hist) == 2
        and hist[0]["statement"].endswith("2 AS n")
        and hist[0]["status"] == "succeeded"
    )
    older = (
        await wr.api.get(
            f"connections/{wr.cid}/queries/history", params={"before_id": hist[-1]["id"]}
        )
    ).json()
    assert older and older[0]["id"] < hist[-1]["id"]
    # another user with the same role on the same connection sees none of it
    peer = await new_user("Database Admin")
    await grant(admin, wr.cid, peer, "Database Admin")
    assert (await peer.get(f"connections/{wr.cid}/queries/history")).json() == []
    assert (
        await peer.delete(f"connections/{wr.cid}/queries/history/{hist[0]['id']}")
    ).status_code == 404
    assert (
        await wr.api.delete(f"connections/{wr.cid}/queries/history/{hist[0]['id']}")
    ).status_code == 204
    assert (await wr.api.delete(f"connections/{wr.cid}/queries/history")).status_code == 204
    assert (await wr.api.get(f"connections/{wr.cid}/queries/history")).json() == []


@pytest.mark.security
async def test_history_literals_can_be_redacted_and_retention_enforced(wr, admin, app_sql):
    assert (
        await admin.put("settings/query-history", {"retention_days": 30, "store_literals": False})
    ).status_code == 200
    try:
        await run(wr.api, wr, "SELECT 'pii-value-123' AS a, 987654 AS b")
        h = (await wr.api.get(f"connections/{wr.cid}/queries/history")).json()[0]
        assert (
            h["redacted"] is True
            and "pii-value-123" not in h["statement"]
            and "987654" not in h["statement"]
        )
        assert h["statement"] == "SELECT ? AS a, ? AS b"
    finally:
        assert (
            await admin.put("settings/query-history", {"retention_days": 1, "store_literals": True})
        ).status_code == 200
    await run(wr.api, wr, "SELECT 'plain-value' AS a")
    assert (
        "plain-value"
        in (await wr.api.get(f"connections/{wr.cid}/queries/history")).json()[0]["statement"]
    )
    app_sql(
        "UPDATE query_history SET created_at = now() - interval '3 days' WHERE connection_id = %s",
        (wr.cid,),
    )
    from app.query.service import purge_history

    counts = await purge_history()
    assert counts["history"] >= 1
    assert (await wr.api.get(f"connections/{wr.cid}/queries/history")).json() == []
    await admin.put("settings/query-history", {"retention_days": 90, "store_literals": True})


async def test_history_settings_need_the_settings_permission_and_validate(admin, new_user):
    assert (await admin.get("settings/query-history")).status_code == 200
    assert (
        await admin.put("settings/query-history", {"retention_days": 0, "store_literals": True})
    ).status_code == 422
    viewer = await new_user("Viewer")
    assert (await viewer.get("settings/query-history")).status_code == 403
    assert (
        await viewer.put("settings/query-history", {"retention_days": 5, "store_literals": False})
    ).status_code == 403


# ----------------------------------------------------------------------------- saved queries


async def test_saved_queries_private_and_shared(wr, admin, new_user):
    base = f"connections/{wr.cid}/queries/saved"
    editor, viewer = await new_user("Editor"), await new_user("Viewer")
    await grant(admin, wr.cid, editor, "Editor")
    await grant(admin, wr.cid, viewer, "Viewer")
    mine = await editor.post(
        base, {"kind": "sql", "name": "my private", "statement": "SELECT 1", "database": "shop"}
    )
    shared = await editor.post(
        base,
        {"kind": "sql", "name": "team report", "statement": "SELECT 2", "visibility": "shared"},
    )
    assert mine.status_code == shared.status_code == 201
    assert (
        await editor.post(base, {"kind": "sql", "name": "my private", "statement": "x"})
    ).status_code == 409
    names_viewer = {q["name"] for q in (await viewer.get(base)).json()}
    assert names_viewer == {"team report"}  # shared visible, private hidden
    assert {q["name"] for q in (await editor.get(base)).json()} == {"my private", "team report"}
    # others cannot change or delete it; the viewer cannot create at all
    sid = shared.json()["id"]
    assert (await viewer.delete(f"{base}/{sid}")).status_code == 403 or (
        await viewer.delete(f"{base}/{sid}")
    ).status_code == 404
    assert (
        await viewer.post(base, {"kind": "sql", "name": "v", "statement": "SELECT 1"})
    ).status_code == 403
    peer = await new_user("Editor")
    await grant(admin, wr.cid, peer, "Editor")
    assert (await peer.patch(f"{base}/{sid}", {"statement": "DROP TABLE x"})).status_code == 403
    upd = await editor.patch(f"{base}/{sid}", {"statement": "SELECT 22", "visibility": "private"})
    assert upd.status_code == 200 and upd.json()["statement"] == "SELECT 22"
    assert {q["name"] for q in (await viewer.get(base)).json()} == set()  # no longer shared
    assert (await editor.delete(f"{base}/{sid}")).status_code == 204


# ----------------------------------------------------------------------------- export


def parse_csv(text):
    return list(csv.reader(io.StringIO(text)))


async def test_query_results_can_be_exported(wr):
    r = await wr.api.post(
        f"connections/{wr.cid}/queries/export",
        payload(wr, f"SELECT id, name FROM {wr.table} ORDER BY id", format="csv"),
    )
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = parse_csv(r.text)
    assert rows[0] == ["id", "name"] and len(rows) == 6 and rows[1][1] == "a"
    j = await wr.api.post(
        f"connections/{wr.cid}/queries/export",
        payload(wr, f"SELECT id, big FROM {wr.table} ORDER BY id", format="json"),
    )
    assert j.json()[0] == {"id": 1, "big": "9007199254740993"}
    ev = (
        await wr.api.get("audit/events", params={"connection_id": wr.cid, "action": "data.export"})
    ).json()["events"][0]
    assert (
        ev["detail"]["rows"] == 5
        and ev["detail"]["kind"] == "query"
        and "SELECT" not in json.dumps(ev)
    )


@pytest.mark.security
async def test_query_export_rules(wr, admin, new_user):
    viewer = await new_user("Viewer")
    await grant(admin, wr.cid, viewer, "Viewer")
    ok_body = payload(wr, "SELECT 1 AS a", format="csv")
    assert (
        await viewer.post(f"connections/{wr.cid}/queries/export", ok_body)
    ).status_code == 403  # no export permission
    for sql in (
        f"DELETE FROM {wr.table}",
        "SELECT 1; SELECT 2",
        f"UPDATE {wr.table} SET qty = 1 WHERE id = 1",
    ):
        r = await wr.api.post(
            f"connections/{wr.cid}/queries/export", payload(wr, sql, format="csv")
        )
        assert r.status_code == 422, sql
    assert wr.sql(f"SELECT COUNT(*) FROM {wr.table}")[0][0] == 5


async def test_query_export_is_size_limited(wr, admin, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "export_max_rows", 2)
    capped = await writable_conn(admin, wr)
    r = await admin.post(
        f"connections/{capped.cid}/queries/export",
        payload(wr, f"SELECT id FROM {wr.table} ORDER BY id", format="csv"),
    )
    assert len(parse_csv(r.text)) == 3  # header + 2 rows
    ev = (
        await admin.get(
            "audit/events", params={"connection_id": capped.cid, "action": "data.export"}
        )
    ).json()["events"][0]
    assert ev["detail"]["truncated"] is True and ev["detail"]["rows"] == 2


async def test_client_chosen_operation_id_allows_cancelling_a_running_query(wr, app_sql):
    op = str(uuid.uuid4())
    long_run = asyncio.create_task(run(wr.api, wr, slow_sql(wr), operation_id=op))
    await wait_for_ref(app_sql, wr.cid)
    assert (await wr.api.post(f"connections/{wr.cid}/queries/{op}/cancel")).status_code == 202
    assert (await asyncio.wait_for(long_run, 20)).status_code == 409
    again = await run(wr.api, wr, "SELECT 1", operation_id=op)
    assert again.status_code == 409 and again.json()["code"] == "conflict"  # ids cannot be reused
