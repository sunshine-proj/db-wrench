"""Phase 5: grants below connection level (database / schema / table or collection)."""

from __future__ import annotations

import uuid

import pytest

from tests.integration.conftest import create_connection, grant

PG = "connections/{cid}/databases/shop/schemas/{schema}/tables"
MY = "connections/{cid}/databases/shop/tables"


async def scoped(admin, cid, user, role, **scope):
    roles = {r["name"]: r for r in (await admin.get("roles")).json()}
    cur = (await admin.get(f"connections/{cid}/scope-grants")).json()
    items = [
        {"user_id": g["user_id"], "role_id": g["role_id"], "database": g["database"],
         "schema": g["schema"], "object": g["object"]} for g in cur
    ]  # fmt: skip
    items.append(
        {"user_id": user.user_id, "role_id": roles[role]["id"], "database": "shop", **scope}
    )
    r = await admin.put(f"connections/{cid}/scope-grants", {"grants": items})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
async def pgc(admin, pg_managed):
    return (await create_connection(admin, "postgresql", pg_managed, read_only=False))["id"]


@pytest.fixture
async def myc(admin, mysql_managed):
    return (await create_connection(admin, "mysql", mysql_managed, read_only=False))["id"]


@pytest.fixture
async def moc(admin, mongo_managed):
    return (await create_connection(admin, "mongodb", mongo_managed, read_only=False))["id"]


async def test_table_grant_sees_only_that_table_postgres(admin, new_user, pgc):
    u = await new_user("Viewer")  # no connection-level grant at all
    assert (await u.get(f"connections/{pgc}/databases")).status_code == 404
    await scoped(admin, pgc, u, "Viewer", schema="public", object="customers")
    # the connection and the path down to the table become visible, nothing else
    assert pgc in [c["id"] for c in (await u.get("connections")).json()]
    assert [d["name"] for d in (await u.get(f"connections/{pgc}/databases")).json()] == ["shop"]
    schemas = [s["name"] for s in (await u.get(f"connections/{pgc}/databases/shop/schemas")).json()]
    assert schemas == ["public"]
    tables = [t["name"] for t in (await u.get(PG.format(cid=pgc, schema="public"))).json()]
    assert tables == ["customers"]
    base = PG.format(cid=pgc, schema="public")
    assert (await u.get(f"{base}/customers/structure")).status_code == 200
    rows = await u.get(f"{base}/customers/rows", params={"limit": 3})
    assert rows.status_code == 200 and len(rows.json()["rows"]) == 3
    for other in ("orders", "secret_sink", "no_pk"):  # other tables stay closed
        assert (await u.get(f"{base}/{other}/rows")).status_code == 403
        assert (await u.get(f"{base}/{other}/structure")).status_code == 403
    assert (
        await u.get(f"connections/{pgc}/databases/shop/schemas/extra/tables")
    ).status_code == 403


async def test_a_viewer_grant_does_not_allow_writes_but_an_editor_grant_does(
    admin, new_user, pgc, app_sql
):
    u = await new_user("Viewer")
    await scoped(admin, pgc, u, "Viewer", schema="public", object="customers")
    base = PG.format(cid=pgc, schema="public") + "/customers"
    assert (await u.post(f"{base}/rows", {"values": {"name": "x"}})).status_code == 403
    await scoped(admin, pgc, u, "Editor", schema="public", object="orders")
    ob = PG.format(cid=pgc, schema="public") + "/orders"
    r = await u.post(f"{ob}/rows", {"values": {"customer_id": 1, "total": "5"}})
    assert r.status_code == 201, r.text  # Editor on orders ...
    assert (
        await u.post(f"{base}/rows", {"values": {"name": "y"}})
    ).status_code == 403  # ... not customers
    assert (
        await u.post(
            f"{PG.format(cid=pgc, schema='public')}/secret_sink/rows",
            {"values": {"id": 9, "v": "x"}},
        )
    ).status_code == 403


async def test_schema_and_database_scopes_cover_their_objects(admin, new_user, pgc):
    u = await new_user("Viewer")
    await scoped(admin, pgc, u, "Viewer", schema="extra")
    assert [
        s["name"] for s in (await u.get(f"connections/{pgc}/databases/shop/schemas")).json()
    ] == ["extra"]
    assert (
        await u.get(f"connections/{pgc}/databases/shop/schemas/extra/tables")
    ).status_code == 200
    assert (
        await u.get(f"connections/{pgc}/databases/shop/schemas/extra/tables/notes/rows")
    ).status_code == 200
    assert (await u.get(f"{PG.format(cid=pgc, schema='public')}/customers/rows")).status_code == 403
    d = await new_user("Viewer")
    await scoped(admin, pgc, d, "Viewer")  # whole database
    assert (await d.get(f"{PG.format(cid=pgc, schema='public')}/customers/rows")).status_code == 200
    assert (
        await d.get(f"connections/{pgc}/databases/shop/schemas/public/sequences")
    ).status_code == 200


async def test_scopes_add_to_connection_level_rights_and_never_subtract(admin, new_user, pgc):
    u = await new_user("Viewer")
    await grant(admin, pgc, u, "Viewer")
    await scoped(admin, pgc, u, "Editor", schema="public", object="orders")
    assert (await u.get(f"{PG.format(cid=pgc, schema='public')}/customers/rows")).status_code == 200
    r = await u.post(
        f"{PG.format(cid=pgc, schema='public')}/orders/rows",
        {"values": {"customer_id": 1, "total": "1"}},
    )
    assert r.status_code == 201
    assert (
        await u.post(
            f"{PG.format(cid=pgc, schema='public')}/customers/rows", {"values": {"name": "n"}}
        )
    ).status_code == 403


@pytest.mark.security
async def test_scoped_users_get_no_connection_wide_capabilities(admin, new_user, pgc):
    u = await new_user("Database Admin")  # even the most powerful role, scoped to one table
    await scoped(admin, pgc, u, "Database Admin", schema="public", object="customers")
    base = f"connections/{pgc}"
    for call in (
        u.post(
            f"{base}/queries/execute", {"database": "shop", "schema": "public", "sql": "select 1"}
        ),
        u.post(f"{base}/databases", {"name": "should_not_exist"}),
        u.post(f"{base}/databases/shop/schemas", {"name": "nope"}),
        u.get(f"{base}/grants"),
        u.get(f"{base}/scope-grants"),
        u.put(f"{base}/scope-grants", {"grants": []}),
        u.patch(base, {"read_only": False}),
        u.delete(base),
        u.get(f"{base}/queries/history"),
        u.post(f"{base}/test"),
    ):
        r = await call
        assert r.status_code in (403, 404), (r.request.url, r.status_code, r.text)
    # object-level administration inside the scope works
    ix = await u.post(
        f"{PG.format(cid=pgc, schema='public')}/customers/indexes",
        {"name": f"ix_{uuid.uuid4().hex[:6]}", "columns": ["name"]},
    )
    assert ix.status_code == 201, ix.text
    other = await u.post(
        f"{PG.format(cid=pgc, schema='public')}/orders/indexes",
        {"name": "ix_x", "columns": ["total"]},
    )
    assert other.status_code == 403


@pytest.mark.security
async def test_a_table_grant_never_authorises_schema_wide_actions(admin, new_user, pgc):
    u = await new_user("Database Admin")
    await scoped(admin, pgc, u, "Database Admin", schema="public", object="customers")
    base = f"connections/{pgc}/databases/shop/schemas"
    assert (await u.post(base, {"name": "sneaky"})).status_code == 403
    assert (await u.delete(f"{base}/extra", json={"confirm": "extra"})).status_code == 403
    assert (await u.get(f"{base}/public/functions")).status_code == 403
    assert (await u.get(f"{base}/public/sequences")).status_code == 403


async def test_mysql_table_scope(admin, new_user, myc):
    u = await new_user("Viewer")
    await scoped(admin, myc, u, "Viewer", object="customers")
    assert [d["name"] for d in (await u.get(f"connections/{myc}/databases")).json()] == ["shop"]
    assert [t["name"] for t in (await u.get(MY.format(cid=myc))).json()] == ["customers"]
    assert (await u.get(f"{MY.format(cid=myc)}/customers/rows")).status_code == 200
    assert (await u.get(f"{MY.format(cid=myc)}/orders/rows")).status_code == 403


async def test_mongo_collection_scope(admin, new_user, moc):
    u = await new_user("Editor")
    await scoped(admin, moc, u, "Editor", object="events")
    base = f"connections/{moc}/mongo/databases/shop/collections"
    assert [d["name"] for d in (await u.get(f"connections/{moc}/mongo/databases")).json()] == [
        "shop"
    ]
    assert [c["name"] for c in (await u.get(base)).json()] == ["events"]
    assert (await u.get(f"{base}/events/documents")).status_code == 200
    assert (await u.get(f"{base}/customers/documents")).status_code == 403
    assert (await u.get(f"{base}/customers")).status_code == 403
    ins = await u.post(
        f"{base}/events/documents", {"document": {"_id": uuid.uuid4().hex, "kind": "x"}}
    )
    assert ins.status_code == 201, ins.text
    assert (await u.post(f"{base}/customers/documents", {"document": {"x": 1}})).status_code == 403
    assert (
        await u.post(
            f"connections/{moc}/mongo/queries/execute",
            {"database": "shop", "collection": "events", "kind": "find"},
        )
    ).status_code == 403


async def test_read_only_connections_strip_scoped_writes(admin, new_user, pg_managed):
    cid = (await create_connection(admin, "postgresql", pg_managed))["id"]  # read-only
    u = await new_user("Editor")
    await scoped(admin, cid, u, "Editor", schema="public", object="customers")
    base = PG.format(cid=cid, schema="public") + "/customers"
    assert (await u.get(f"{base}/rows")).status_code == 200
    assert (await u.post(f"{base}/rows", {"values": {"name": "z"}})).status_code == 403


@pytest.mark.security
async def test_granting_is_validated_and_cannot_escalate(admin, new_user, pgc, myc):
    roles = {r["name"]: r for r in (await admin.get("roles")).json()}
    u = await new_user("Viewer")
    bad = [
        {"database": "", "schema": "", "object": ""},
        {"database": "shop", "schema": "", "object": "customers"},  # PG: a table needs a schema
    ]
    for item in bad:
        r = await admin.put(
            f"connections/{pgc}/scope-grants",
            {"grants": [{"user_id": u.user_id, "role_id": roles["Viewer"]["id"], **item}]},
        )
        assert r.status_code == 422, item
    r = await admin.put(
        f"connections/{myc}/scope-grants",
        {
            "grants": [
                {
                    "user_id": u.user_id,
                    "role_id": roles["Viewer"]["id"],
                    "database": "shop",
                    "schema": "x",
                }
            ]
        },
    )
    assert r.status_code == 422  # MySQL has no schema level
    dup = {"user_id": u.user_id, "role_id": roles["Viewer"]["id"], "database": "shop"}
    assert (
        await admin.put(f"connections/{pgc}/scope-grants", {"grants": [dup, dup]})
    ).status_code == 422
    unknown = {**dup, "user_id": str(uuid.uuid4())}
    assert (
        await admin.put(f"connections/{pgc}/scope-grants", {"grants": [unknown]})
    ).status_code == 404
    # a granter who only holds Viewer rights may not hand out Editor rights
    manager = await new_user("Viewer")
    mgr_role = await admin.post("roles", {"name": f"mgr-{uuid.uuid4().hex[:6]}",
                                          "permissions": ["connection.view", "connection.manage_access", "metadata.view", "data.read"]})  # fmt: skip
    await admin.patch(f"users/{manager.user_id}", {"role_id": mgr_role.json()["id"]})
    await manager.login(manager.username, "Us3r-Passw0rd-Long!")
    await grant(admin, pgc, manager, mgr_role.json()["name"])
    esc = await manager.put(
        f"connections/{pgc}/scope-grants",
        {"grants": [{"user_id": u.user_id, "role_id": roles["Editor"]["id"], "database": "shop"}]},
    )
    assert esc.status_code == 403, esc.text
    ok = await manager.put(
        f"connections/{pgc}/scope-grants",
        {"grants": [{"user_id": u.user_id, "role_id": roles["Viewer"]["id"], "database": "shop"}]},
    )
    assert ok.status_code == 200, ok.text


async def test_scoped_grant_changes_are_audited_and_removable(admin, new_user, pgc):
    u = await new_user("Viewer")
    await scoped(admin, pgc, u, "Viewer", schema="public", object="customers")
    base = PG.format(cid=pgc, schema="public") + "/customers/rows"
    assert (await u.get(base)).status_code == 200
    assert (await admin.put(f"connections/{pgc}/scope-grants", {"grants": []})).status_code == 200
    assert (await u.get(base)).status_code == 404  # no grant of any kind any more
    events = (
        await admin.get(
            "audit/events", params={"connection_id": pgc, "action": "connection.scope_grants"}
        )
    ).json()["events"]
    assert len(events) >= 2


async def test_tokens_cannot_exceed_a_scoped_owner(admin, new_user, pgc):
    role = await admin.post(
        "roles", {"name": f"tk-{uuid.uuid4().hex[:6]}", "permissions": ["app.manage_tokens"]}
    )
    owner = await new_user("Viewer")
    await admin.patch(f"users/{owner.user_id}", {"role_id": role.json()["id"]})
    await owner.login(owner.username, "Us3r-Passw0rd-Long!")
    await scoped(admin, pgc, owner, "Viewer", schema="public", object="customers")
    t = (
        await owner.post(
            "tokens",
            {"name": "scoped", "scopes": ["data.read", "metadata.view", "connection.view"]},
        )
    ).json()
    import httpx

    tok = httpx.AsyncClient(transport=owner.http._transport, base_url="http://testserver",
                            headers={"Authorization": f"Bearer {t['secret']}"})  # fmt: skip
    base = PG.format(cid=pgc, schema="public")
    assert (await tok.get(f"/api/v1/{base}/customers/rows")).status_code == 200
    assert (await tok.get(f"/api/v1/{base}/orders/rows")).status_code == 403
    await tok.aclose()
