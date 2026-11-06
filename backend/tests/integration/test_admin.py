"""Phase 5: database / schema / collection / index administration and the approval workflow."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pymongo
import pymysql
import pytest
import pytest_asyncio

from tests.integration.conftest import Api, create_connection, grant


def uniq(prefix: str = "adm") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class Srv:
    """Direct access to the managed server (superuser) for verifying side effects."""

    def __init__(self, engine: str, info: dict[str, Any]):
        self.engine, self.info = engine, info

    def sql(self, stmt: str, database: str | None = None, params: tuple[Any, ...] = ()) -> list:
        i = self.info
        if self.engine == "postgresql":
            with psycopg.connect(
                host=i["host"], port=i["port"], user=i["username"], password=i["password"],
                dbname=database or "postgres", autocommit=True,
            ) as c:  # fmt: skip
                cur = c.execute(stmt, params)
                return cur.fetchall() if cur.description else []
        conn = pymysql.connect(
            host=i["host"], port=i["port"], user=i["username"], password=i["password"],
            database=database, autocommit=True,
        )  # fmt: skip
        try:
            with conn.cursor() as cur:
                cur.execute(stmt, params)
                return list(cur.fetchall()) if cur.description else []
        finally:
            conn.close()

    def databases(self) -> set[str]:
        q = "SELECT datname FROM pg_database" if self.engine == "postgresql" else "SHOW DATABASES"
        return {r[0] for r in self.sql(q)}


class Adm:
    def __init__(self, api: Api, engine: str, cid: str, srv: Srv):
        self.api, self.engine, self.cid, self.srv = api, engine, cid, srv

    def url(self, path: str = "") -> str:
        return f"connections/{self.cid}{path}"


@pytest_asyncio.fixture(params=["postgresql", "mysql", "mariadb"])
async def adm(request, admin, pg_managed, mysql_managed, mariadb_managed) -> Adm:
    info = {"postgresql": pg_managed, "mysql": mysql_managed, "mariadb": mariadb_managed}[
        request.param
    ]
    conn = await create_connection(admin, request.param, info, read_only=False)
    return Adm(admin, request.param, conn["id"], Srv(request.param, info))


@pytest_asyncio.fixture
async def madm(admin, mongo_managed) -> Adm:
    conn = await create_connection(admin, "mongodb", mongo_managed, read_only=False)
    return Adm(admin, "mongodb", conn["id"], Srv("mongodb", mongo_managed))


def mongo_client(info: dict[str, Any]) -> pymongo.MongoClient:
    return pymongo.MongoClient(
        host=info["host"], port=info["port"], username=info["username"], password=info["password"]
    )


# ----------------------------------------------------------------------------- databases


async def test_create_and_drop_database(adm):
    name = uniq()
    r = await adm.api.post(adm.url("/databases"), {"name": name})
    assert r.status_code == 201, r.text
    assert name in adm.srv.databases()
    # visible through the explorer immediately (metadata cache was invalidated)
    listed = [d["name"] for d in (await adm.api.get(adm.url("/databases"))).json()]
    assert name in listed

    r = await adm.api.delete(adm.url(f"/databases/{name}"), json={"confirm": name})
    assert r.status_code == 204, r.text
    assert name not in adm.srv.databases()


async def test_duplicate_database_is_a_conflict(adm):
    name = uniq()
    assert (await adm.api.post(adm.url("/databases"), {"name": name})).status_code == 201
    r = await adm.api.post(adm.url("/databases"), {"name": name})
    assert r.status_code == 409
    await adm.api.delete(adm.url(f"/databases/{name}"), json={"confirm": name})


@pytest.mark.parametrize(
    "name",
    ['x"; DROP DATABASE shop; --', "a b", "", "1abc", "a" * 64, "ünï", "x`y", "a;b", "pg%"],
)
async def test_invalid_new_names_are_rejected_before_any_sql(adm, name):
    r = await adm.api.post(adm.url("/databases"), {"name": name})
    assert r.status_code in (422, 400), r.text
    assert "shop" in adm.srv.databases()


async def test_drop_needs_the_typed_name_everywhere(adm):
    name = uniq()
    await adm.api.post(adm.url("/databases"), {"name": name})
    for body in ({}, {"confirm": "nope"}, {"confirm": name.upper()}):
        r = await adm.api.delete(adm.url(f"/databases/{name}"), json=body)
        assert r.status_code == 428 and r.json()["code"] == "confirmation_required"
    assert name in adm.srv.databases()
    await adm.api.delete(adm.url(f"/databases/{name}"), json={"confirm": name})


async def test_system_and_configured_databases_cannot_be_dropped(adm):
    system = "postgres" if adm.engine == "postgresql" else "mysql"  # also MariaDB
    r = await adm.api.delete(adm.url(f"/databases/{system}"), json={"confirm": system})
    assert r.status_code in (403, 404)  # protected (PG) or not even listed (MySQL)
    assert system in adm.srv.databases()
    r = await adm.api.delete(adm.url("/databases/shop"), json={"confirm": "shop"})
    assert r.status_code == 409  # the connection's own database
    assert "shop" in adm.srv.databases()


async def test_drop_unknown_database_is_404(adm):
    r = await adm.api.delete(adm.url("/databases/nope_nope"), json={"confirm": "nope_nope"})
    assert r.status_code == 404


async def test_mysql_database_charset_option_is_allowlisted(adm):
    if adm.engine not in ("mysql", "mariadb"):
        pytest.skip("MySQL only")
    name = uniq()
    bad = await adm.api.post(adm.url("/databases"), {"name": name, "options": {"charset": "x;y"}})
    assert bad.status_code == 422
    ok = await adm.api.post(adm.url("/databases"), {"name": name, "options": {"charset": "latin1"}})
    assert ok.status_code == 201, ok.text
    await adm.api.delete(adm.url(f"/databases/{name}"), json={"confirm": name})


# ------------------------------------------------------------------------------ schemas


async def test_schema_create_and_drop_postgres_only(adm):
    name = uniq("sch")
    r = await adm.api.post(adm.url("/databases/shop/schemas"), {"name": name})
    if adm.engine in ("mysql", "mariadb"):
        assert r.status_code == 422 and r.json()["code"] == "unsupported_capability"
        return
    assert r.status_code == 201, r.text
    assert adm.srv.sql("SELECT 1 FROM pg_namespace WHERE nspname = %s", "shop", (name,))
    assert (
        await adm.api.post(adm.url("/databases/shop/schemas"), {"name": name})
    ).status_code == 409
    for bad in ("pg_x", "public"):
        r = await adm.api.delete(adm.url(f"/databases/shop/schemas/{bad}"), json={"confirm": bad})
        assert r.status_code in (403, 404, 422), (bad, r.text)
    r = await adm.api.delete(adm.url(f"/databases/shop/schemas/{name}"), json={})
    assert r.status_code == 428
    r = await adm.api.delete(adm.url(f"/databases/shop/schemas/{name}"), json={"confirm": name})
    assert r.status_code == 204
    assert not adm.srv.sql("SELECT 1 FROM pg_namespace WHERE nspname = %s", "shop", (name,))


async def test_drop_schema_with_objects_is_restricted(adm):
    if adm.engine != "postgresql":
        pytest.skip("PostgreSQL only")
    name = uniq("sch")
    await adm.api.post(adm.url("/databases/shop/schemas"), {"name": name})
    adm.srv.sql(f'CREATE TABLE "{name}".t (id int)', "shop")
    r = await adm.api.delete(adm.url(f"/databases/shop/schemas/{name}"), json={"confirm": name})
    assert r.status_code >= 400  # RESTRICT: never cascades silently
    assert adm.srv.sql("SELECT 1 FROM pg_tables WHERE schemaname = %s", "shop", (name,))
    adm.srv.sql(f'DROP SCHEMA "{name}" CASCADE', "shop")


# ------------------------------------------------------------------------------ indexes


def table_path(adm: Adm, table: str) -> str:
    base = "/databases/shop"
    return (
        f"{base}/schemas/public/tables/{table}"
        if adm.engine == "postgresql"
        else f"{base}/tables/{table}"
    )


@pytest_asyncio.fixture
async def tbl(adm) -> str:
    t = uniq("ix")
    adm.srv.sql(
        f"CREATE TABLE {t} (id int PRIMARY KEY, name varchar(50), email varchar(50), qty int)",
        "shop",
    )
    return t


async def test_create_and_drop_index(adm, tbl):
    url = adm.url(table_path(adm, tbl))
    r = await adm.api.post(f"{url}/indexes", {"name": "ix_name", "columns": ["name", "qty"]})
    assert r.status_code == 201, r.text
    structure = (await adm.api.get(f"{url}/structure")).json()
    idx = {i["name"]: i for i in structure["indexes"]}
    assert idx["ix_name"]["columns"] == ["name", "qty"] and not idx["ix_name"]["unique"]

    r = await adm.api.post(
        f"{url}/indexes", {"name": "ux_email", "columns": ["email"], "unique": True}
    )
    assert r.status_code == 201, r.text
    assert (
        await adm.api.post(f"{url}/indexes", {"name": "ix_name", "columns": ["qty"]})
    ).status_code == 409

    assert (await adm.api.delete(f"{url}/indexes/ix_name", json={})).status_code == 204
    names = {i["name"] for i in (await adm.api.get(f"{url}/structure")).json()["indexes"]}
    assert "ix_name" not in names and "ux_email" in names
    assert (await adm.api.delete(f"{url}/indexes/ix_name", json={})).status_code == 404


async def test_index_validation(adm, tbl):
    url = adm.url(table_path(adm, tbl)) + "/indexes"
    for body in (
        {"name": "ok", "columns": ["nope"]},
        {"name": "ok", "columns": ["name", "name"]},
        {"name": "bad name", "columns": ["name"]},
        {"name": 'x"; DROP TABLE t; --', "columns": ["name"]},
        {"name": "ok", "columns": ['name"); DROP TABLE x; --']},
    ):
        r = await adm.api.post(url, body)
        assert r.status_code in (400, 422), (body, r.text)
    assert adm.srv.sql(f"SELECT count(*) FROM {tbl}", "shop")[0][0] == 0  # table intact


async def test_constraint_backed_indexes_cannot_be_dropped_here(adm, tbl):
    url = adm.url(table_path(adm, tbl))
    pk = next(
        i["name"] for i in (await adm.api.get(f"{url}/structure")).json()["indexes"] if i["primary"]
    )
    r = await adm.api.delete(f"{url}/indexes/{pk}", json={})
    assert r.status_code == 409, r.text


async def test_production_index_drop_needs_the_name(adm, tbl, admin):
    prod = await create_connection(
        admin, adm.engine, adm.srv.info, read_only=False, environment="production"
    )
    p = Adm(admin, adm.engine, prod["id"], adm.srv)
    url = p.url(table_path(p, tbl))
    await admin.post(f"{url}/indexes", {"name": "ix_q", "columns": ["qty"]})
    assert (await admin.delete(f"{url}/indexes/ix_q", json={})).status_code == 428
    assert (await admin.delete(f"{url}/indexes/ix_q", json={"confirm": "ix_q"})).status_code == 204


# ------------------------------------------------------------------------------ MongoDB


async def test_mongo_database_collection_index_lifecycle(madm):
    db, coll = uniq("mdb"), "things"
    c = mongo_client(madm.srv.info)
    try:
        r = await madm.api.post(madm.url("/mongo/databases"), {"name": db, "collection": coll})
        assert r.status_code == 201, r.text
        assert db in c.list_database_names()
        r = await madm.api.post(
            madm.url(f"/mongo/databases/{db}/collections"),
            {"name": "capped", "capped": True, "size_bytes": 4096, "max_docs": 10},
        )
        assert r.status_code == 201, r.text
        assert c[db]["capped"].options()["capped"] is True
        assert (await madm.api.post(
            madm.url(f"/mongo/databases/{db}/collections"), {"name": coll})).status_code == 409  # fmt: skip

        base = madm.url(f"/mongo/databases/{db}/collections/{coll}")
        r = await madm.api.post(
            f"{base}/indexes",
            {"keys": [{"field": "email", "direction": 1}], "name": "ux_email", "unique": True,
             "partial_filter": {"email": {"$type": "string"}}},
        )  # fmt: skip
        assert r.status_code == 201, r.text
        info = c[db][coll].index_information()
        assert info["ux_email"]["unique"] and "partialFilterExpression" in info["ux_email"]
        r = await madm.api.post(
            f"{base}/indexes",
            {"keys": [{"field": "ts", "direction": 1}], "ttl_seconds": 3600},
        )
        assert r.status_code == 201, r.text

        assert (await madm.api.delete(f"{base}/indexes/_id_", json={})).status_code == 409
        assert (await madm.api.delete(f"{base}/indexes/ux_email", json={})).status_code == 204
        assert (await madm.api.delete(f"{base}/indexes/ux_email", json={})).status_code == 404

        r = await madm.api.delete(madm.url(f"/mongo/databases/{db}/collections/capped"), json={})
        assert r.status_code == 428
        r = await madm.api.delete(
            madm.url(f"/mongo/databases/{db}/collections/capped"), json={"confirm": "capped"}
        )
        assert r.status_code == 204
        r = await madm.api.delete(madm.url(f"/mongo/databases/{db}"), json={"confirm": db})
        assert r.status_code == 204
        assert db not in c.list_database_names()
    finally:
        c.drop_database(db)
        c.close()


@pytest.mark.parametrize(
    "body",
    [
        {"keys": [{"field": "$where", "direction": 1}]},
        {"keys": [{"field": "a", "direction": 7}]},
        {"keys": [{"field": "a", "direction": "evil"}]},
        {"keys": [{"field": "a"}, {"field": "a"}]},
        {"keys": [{"field": "a"}, {"field": "b"}], "ttl_seconds": 5},
        {"keys": [{"field": "a"}], "partial_filter": {"$where": "1"}},
        {"keys": [{"field": "a", "direction": True}]},
    ],
)
async def test_mongo_index_validation(madm, body):
    r = await madm.api.post(madm.url("/mongo/databases/shop/collections/events/indexes"), body)
    assert r.status_code in (400, 422), (body, r.text)


@pytest.mark.parametrize("name", ["admin", "local", "config"])
async def test_mongo_system_databases_are_protected(madm, name):
    r = await madm.api.delete(madm.url(f"/mongo/databases/{name}"), json={"confirm": name})
    assert r.status_code in (403, 404)


async def test_mongo_bad_collection_options(madm):
    base = madm.url("/mongo/databases/shop/collections")
    for body in ({"name": "x1", "capped": True}, {"name": "x2", "size_bytes": 10},
                 {"name": "bad name"}, {"name": "$x"}):  # fmt: skip
        assert (await madm.api.post(base, body)).status_code in (400, 422), body


# ------------------------------------------------------------------------- authorization


async def test_read_only_connections_refuse_administration(
    admin, pg_managed, mysql_managed, mongo_managed
):
    for engine, info, path, body in (
        ("postgresql", pg_managed, "/databases", {"name": uniq()}),
        ("mysql", mysql_managed, "/databases", {"name": uniq()}),
        ("mongodb", mongo_managed, "/mongo/databases", {"name": uniq(), "collection": "c"}),
    ):
        conn = await create_connection(admin, engine, info)  # read-only by default
        r = await admin.post(f"connections/{conn['id']}{path}", body)
        assert r.status_code == 403, (engine, r.text)


async def test_role_without_admin_permissions_is_refused(adm, new_user, admin):
    editor = await new_user("Editor")
    await grant(admin, adm.cid, editor, "Editor")
    r = await editor.post(adm.url("/databases"), {"name": uniq()})
    assert r.status_code == 403
    outsider = await new_user("Editor")  # no grant: the connection does not exist for them
    assert (await outsider.post(adm.url("/databases"), {"name": uniq()})).status_code == 404


async def test_requires_authentication_and_csrf(adm, anon, admin):
    assert (await anon.post(adm.url("/databases"), {"name": uniq()})).status_code == 401
    admin_csrf, admin.csrf = admin.csrf, "wrong"
    try:
        assert (await admin.post(adm.url("/databases"), {"name": uniq()})).status_code == 403
    finally:
        admin.csrf = admin_csrf


async def test_administration_is_audited(adm, admin):
    name = uniq()
    await adm.api.post(adm.url("/databases"), {"name": name})
    await adm.api.delete(adm.url(f"/databases/{name}"), json={"confirm": name})
    await adm.api.post(adm.url("/databases"), {"name": "bad name"})  # fails validation
    events = (
        await admin.get("audit/events", params={"connection_id": adm.cid, "limit": 20})
    ).json()["events"]
    actions = [(e["action"], e["result"], e["target"].get("database")) for e in events]
    assert ("admin.database.create", "success", name) in actions
    assert ("admin.database.drop", "success", name) in actions
    assert any(a == "admin.database.create" and r == "failure" for a, r, _ in actions)


# --------------------------------------------------------------------- approval workflow


async def set_policy(admin: Api, cid: str, ops: list[str]) -> None:
    r = await admin.put("governance/policies", {"connection_id": cid, "operations": ops})
    assert r.status_code == 200, r.text


async def test_four_eyes_flow_for_a_gated_operation(adm, admin, new_user):
    dba = await new_user("Database Admin")
    reviewer = await new_user("Database Admin")
    await grant(admin, adm.cid, dba, "Database Admin")
    await grant(admin, adm.cid, reviewer, "Database Admin")
    await set_policy(admin, adm.cid, ["admin.create_database", "admin.drop_database"])

    name = uniq()
    r = await dba.post(adm.url("/databases"), {"name": name})
    assert r.status_code == 428 and r.json()["code"] == "approval_required", r.text
    aid = r.json()["approval_id"]
    assert name not in adm.srv.databases()

    # pending approvals are not executable and not self-approvable
    hdr = {"Approval-Id": aid}
    r = await dba.post(adm.url("/databases"), {"name": name}, headers=hdr)
    assert r.status_code == 409
    assert (await dba.post(f"approvals/{aid}/approve", {})).status_code in (403, 404)

    # the reviewer sees it and decides
    pending = (
        await reviewer.get("approvals", params={"scope": "review", "status": "pending"})
    ).json()
    assert aid in [a["id"] for a in pending]
    assert pending[0]["summary"]["database"] == name
    r = await reviewer.post(f"approvals/{aid}/approve", {"reason": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "approved"

    # bound to the requester, the operation and the exact parameters
    assert (
        await reviewer.post(adm.url("/databases"), {"name": name}, headers=hdr)
    ).status_code in (403, 404, 409)
    other = await dba.post(adm.url("/databases"), {"name": name + "x"}, headers=hdr)
    assert other.status_code == 422, other.text
    assert (
        await dba.post(adm.url("/databases"), {"name": name, "options": {}}, headers=hdr)
    ).status_code == 201
    assert name in adm.srv.databases()

    # single use
    again = await dba.post(adm.url("/databases"), {"name": name}, headers=hdr)
    assert again.status_code == 409
    adm.srv.sql(
        f'DROP DATABASE "{name}"' if adm.engine == "postgresql" else f"DROP DATABASE `{name}`"
    )

    actions = {
        e["action"]
        for e in (
            await admin.get("audit/events", params={"connection_id": adm.cid, "limit": 100})
        ).json()["events"]
    }
    assert {"approval.request", "approval.approve", "approval.consume"} <= actions


async def test_rejected_and_cancelled_approvals_cannot_run(adm, admin, new_user):
    dba = await new_user("Database Admin")
    reviewer = await new_user("Database Admin")
    await grant(admin, adm.cid, dba, "Database Admin")
    await grant(admin, adm.cid, reviewer, "Database Admin")
    await set_policy(admin, adm.cid, ["admin.create_database"])
    name = uniq()
    aid = (await dba.post(adm.url("/databases"), {"name": name})).json()["approval_id"]
    assert (await reviewer.post(f"approvals/{aid}/reject", {"reason": "no"})).json()[
        "status"
    ] == "rejected"
    r = await dba.post(adm.url("/databases"), {"name": name}, headers={"Approval-Id": aid})
    assert r.status_code == 403
    assert (
        await reviewer.post(f"approvals/{aid}/approve", {})
    ).status_code == 409  # already decided

    aid2 = (await dba.post(adm.url("/databases"), {"name": name})).json()["approval_id"]
    assert (await dba.post(f"approvals/{aid2}/cancel")).json()["status"] == "cancelled"
    assert (await reviewer.post(f"approvals/{aid2}/approve", {})).status_code == 409
    assert name not in adm.srv.databases()


async def test_failed_execution_does_not_burn_the_approval(adm, admin, new_user):
    dba = await new_user("Database Admin")
    reviewer = await new_user("Database Admin")
    await grant(admin, adm.cid, dba, "Database Admin")
    await grant(admin, adm.cid, reviewer, "Database Admin")
    await set_policy(admin, adm.cid, ["admin.create_database"])
    name = uniq()
    aid = (await dba.post(adm.url("/databases"), {"name": name})).json()["approval_id"]
    await reviewer.post(f"approvals/{aid}/approve", {})
    adm.srv.sql(
        f'CREATE DATABASE "{name}"' if adm.engine == "postgresql" else f"CREATE DATABASE `{name}`"
    )
    hdr = {"Approval-Id": aid}
    assert (
        await dba.post(adm.url("/databases"), {"name": name}, headers=hdr)
    ).status_code == 409  # exists
    # drop through the API so the adapter's metadata cache is invalidated too
    assert (
        await admin.delete(adm.url(f"/databases/{name}"), json={"confirm": name})
    ).status_code == 204
    assert (await dba.post(adm.url("/databases"), {"name": name}, headers=hdr)).status_code == 201
    adm.srv.sql(
        f'DROP DATABASE "{name}"' if adm.engine == "postgresql" else f"DROP DATABASE `{name}`"
    )


async def test_environment_policy_applies_and_connection_policy_overrides(
    adm, admin, new_user, pg_managed, mysql_managed
):
    info = adm.srv.info
    stg = await create_connection(admin, adm.engine, info, read_only=False, environment="staging")
    dba = await new_user("Database Admin")
    await grant(admin, stg["id"], dba, "Database Admin")
    r = await admin.put(
        "governance/policies", {"environment": "staging", "operations": ["admin.create_database"]}
    )
    assert r.status_code == 200, r.text
    try:
        r = await dba.post(f"connections/{stg['id']}/databases", {"name": uniq()})
        assert r.status_code == 428
        # an explicit (empty-allowed) connection policy replaces the environment one
        await admin.put(
            "governance/policies",
            {"connection_id": stg["id"], "operations": ["admin.drop_database"]},
        )
        name = uniq()
        assert (
            await dba.post(f"connections/{stg['id']}/databases", {"name": name})
        ).status_code == 201
        await dba.delete(f"connections/{stg['id']}/databases/{name}", json={"confirm": name})
    finally:
        await admin.put("governance/policies", {"environment": "staging", "operations": []})


async def test_policy_management_validation_and_permission(admin, new_user, adm):
    assert (
        await admin.put("governance/policies", {"operations": ["admin.create_database"]})
    ).status_code == 422
    assert (
        await admin.put(
            "governance/policies",
            {"environment": "staging", "connection_id": adm.cid, "operations": []},
        )
    ).status_code == 422
    assert (
        await admin.put("governance/policies", {"environment": "staging", "operations": ["rm -rf"]})
    ).status_code == 422
    assert (
        await admin.put("governance/policies", {"environment": "mars", "operations": []})
    ).status_code == 422
    viewer = await new_user("Viewer")
    assert (await viewer.get("governance/policies")).status_code == 403
    assert (
        await viewer.put("governance/policies", {"environment": "staging", "operations": []})
    ).status_code == 403
    body = (await admin.get("governance/policies")).json()
    assert "query.mongo_write_stage" in body["always_approved"]


async def test_non_approvers_cannot_see_or_decide(adm, admin, new_user):
    dba = await new_user("Database Admin")
    await grant(admin, adm.cid, dba, "Database Admin")
    viewer = await new_user("Viewer")
    await grant(admin, adm.cid, viewer, "Viewer")
    await set_policy(admin, adm.cid, ["admin.create_database"])
    aid = (await dba.post(adm.url("/databases"), {"name": uniq()})).json()["approval_id"]
    assert aid not in [
        a["id"] for a in (await viewer.get("approvals", params={"scope": "review"})).json()
    ]
    assert (await viewer.post(f"approvals/{aid}/approve", {})).status_code == 404
    assert (await viewer.post(f"approvals/{aid}/cancel")).status_code == 404  # not the requester
    # the requester's own list includes it
    assert aid in [a["id"] for a in (await dba.get("approvals")).json()]
