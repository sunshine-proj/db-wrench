"""Phase 6: database users, roles and privileges on PostgreSQL, MySQL and MongoDB."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pymongo
import pymongo.errors
import pymysql
import pytest

from tests.integration.conftest import create_connection, grant
from tests.integration.test_admin import Adm, adm, madm, mongo_client

PW = "Sup3r-Secret-Db-Pw!"
PW2 = "An0ther-Secret-Db-Pw!"


def uname(prefix: str = "u") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def login_ok(adm: Adm, user: str, password: str, sql: str = "SELECT 1") -> bool:
    try:
        return _login_once(adm, user, password, sql)
    except AttributeError:  # PyMySQL crashes on a caching_sha2_password account's first login
        return _login_once(adm, user, password, sql)


def _login_once(adm: Adm, user: str, password: str, sql: str) -> bool:
    i = adm.srv.info
    try:
        if adm.engine == "postgresql":
            with psycopg.connect(host=i["host"], port=i["port"], user=user, password=password,
                                 dbname="shop", connect_timeout=5) as c:  # fmt: skip
                c.execute(sql).fetchall()
        else:
            conn = pymysql.connect(host=i["host"], port=i["port"], user=user, password=password,
                                   connect_timeout=5)  # fmt: skip
            try:
                with conn.cursor() as cur:
                    cur.execute(sql.replace("FROM customers", "FROM shop.customers"))
                    cur.fetchall()
            finally:
                conn.close()
    except (psycopg.Error, pymysql.err.MySQLError):
        return False
    return True


def table_target(adm: Adm, table: str = "customers") -> dict[str, Any]:
    t = {"kind": "table", "database": "shop", "object": table}
    return {**t, "schema": "public"} if adm.engine == "postgresql" else t


async def make_user(adm: Adm, name: str, **over: Any) -> dict[str, Any]:
    body = {
        "name": name,
        "password": PW,
        "options": {"login": True} if adm.engine == "postgresql" else {},
        **over,
    }
    r = await adm.api.post(adm.url("/principals"), body)
    assert r.status_code == 201, r.text
    return r.json()


def host_q(adm: Adm) -> dict[str, str]:
    return {"host": "%"} if adm.engine in ("mysql", "mariadb") else {}


# ------------------------------------------------------------------------ relational


async def test_create_list_login_and_drop(adm):
    name = uname()
    created = await make_user(adm, name)
    assert created["name"] == name and not created["protected"] and PW not in str(created)
    listed = (await adm.api.get(adm.url("/principals"))).json()
    assert name in [p["name"] for p in listed] and PW not in str(listed)
    assert login_ok(adm, name, PW)
    assert not login_ok(adm, name, "wrong-password-value")
    path = adm.url(f"/principals/{name}")
    for body in ({}, {"confirm": "nope"}):
        r = await adm.api.delete(path, json=body, params=host_q(adm))
        assert r.status_code == 428 and r.json()["code"] == "confirmation_required"
    assert (
        await adm.api.delete(path, json={"confirm": name}, params=host_q(adm))
    ).status_code == 204
    assert name not in [p["name"] for p in (await adm.api.get(adm.url("/principals"))).json()]
    assert not login_ok(adm, name, PW)


async def test_grant_and_revoke_table_privileges_really_change_access(adm):
    name = uname()
    await make_user(adm, name)
    q = host_q(adm)
    assert not login_ok(adm, name, PW, "SELECT count(*) FROM customers")
    body = {"privileges": ["select"], "target": table_target(adm)}
    r = await adm.api.post(adm.url(f"/principals/{name}/grants"), body, params=q)
    assert r.status_code == 204, r.text
    assert login_ok(adm, name, PW, "SELECT count(*) FROM customers")
    grants = (
        await adm.api.get(adm.url(f"/principals/{name}/grants"), params={**q, "database": "shop"})
    ).json()
    entry = next(g for g in grants if g["object"] == "customers")
    assert entry["privileges"] == ["SELECT"] and entry["kind"] == "table"
    r = await adm.api.request("DELETE", adm.url(f"/principals/{name}/grants"), json=body, params=q)
    assert r.status_code == 204, r.text
    assert not login_ok(adm, name, PW, "SELECT count(*) FROM customers")
    await adm.api.delete(adm.url(f"/principals/{name}"), json={"confirm": name}, params=q)


async def test_database_level_grants_and_all_expansion(adm):
    name = uname()
    await make_user(adm, name)
    q = host_q(adm)
    target = {"kind": "database", "database": "shop"}
    r = await adm.api.post(
        adm.url(f"/principals/{name}/grants"), {"privileges": ["ALL"], "target": target}, params=q
    )
    assert r.status_code == 204, r.text
    entries = (
        await adm.api.get(adm.url(f"/principals/{name}/grants"), params={**q, "database": "shop"})
    ).json()
    db = next(g for g in entries if g["kind"] == "database")
    assert db["privileges"]  # expanded to the allowlist, never an engine-level ALL
    if adm.engine in ("mysql", "mariadb"):
        assert not [g for g in entries if g["kind"] == "global"]  # nothing global was handed out
    await adm.api.delete(adm.url(f"/principals/{name}"), json={"confirm": name}, params=q)


async def test_alter_password_and_account_state(adm):
    name = uname()
    await make_user(adm, name)
    q = host_q(adm)
    r = await adm.api.patch(adm.url(f"/principals/{name}"), {"password": PW2}, params=q)
    assert r.status_code == 204, r.text
    assert not login_ok(adm, name, PW) and login_ok(adm, name, PW2)
    if adm.engine == "postgresql":
        r = await adm.api.patch(
            adm.url(f"/principals/{name}"), {"options": {"connection_limit": 3, "createdb": True}}
        )
        assert r.status_code == 204, r.text
        row = adm.srv.sql(
            "SELECT rolconnlimit, rolcreatedb FROM pg_roles WHERE rolname = %s", "shop", (name,)
        )[0]
        assert row == (3, True)
        r = await adm.api.patch(adm.url(f"/principals/{name}"), {"options": {"login": False}})
        assert not login_ok(adm, name, PW2)
    else:
        assert (
            await adm.api.patch(
                adm.url(f"/principals/{name}"), {"options": {"lock": True}}, params=q
            )
        ).status_code == 204
        assert not login_ok(adm, name, PW2)
        assert (
            await adm.api.patch(
                adm.url(f"/principals/{name}"), {"options": {"lock": False}}, params=q
            )
        ).status_code == 204
        assert login_ok(adm, name, PW2)
    assert (
        await adm.api.patch(adm.url(f"/principals/{name}"), {}, params=q)
    ).status_code == 422  # nothing to change
    await adm.api.delete(adm.url(f"/principals/{name}"), json={"confirm": name}, params=q)


async def test_postgres_role_membership(adm):
    if adm.engine != "postgresql":
        pytest.skip("PostgreSQL only")
    group, member = uname("grp"), uname("mem")
    assert (
        await adm.api.post(adm.url("/principals"), {"name": group, "options": {"login": False}})
    ).status_code == 201
    await make_user(adm, member)
    r = await adm.api.post(adm.url(f"/principals/{member}/memberships"), {"role": group})
    assert r.status_code == 204, r.text
    listed = {p["name"]: p for p in (await adm.api.get(adm.url("/principals"))).json()}
    assert listed[member]["member_of"] == [group] and listed[group]["kind"] == "role"
    r = await adm.api.request(
        "DELETE", adm.url(f"/principals/{member}/memberships"), json={"role": group}
    )
    assert r.status_code == 204
    assert (await adm.api.get(adm.url("/principals"))).json() and not next(
        p for p in (await adm.api.get(adm.url("/principals"))).json() if p["name"] == member
    )["member_of"]
    for n in (member, group):
        await adm.api.delete(adm.url(f"/principals/{n}"), json={"confirm": n})


# --------------------------------------------------------------------- relational: safety


@pytest.mark.security
@pytest.mark.parametrize("bad", ['x"; DROP ROLE postgres; --', "a b", "", "x'y", "a;b", "1" * 70])
async def test_invalid_names_are_rejected_before_any_sql(adm, bad):
    body = {
        "name": bad,
        "password": PW,
        "options": {"login": True} if adm.engine == "postgresql" else {},
    }
    r = await adm.api.post(adm.url("/principals"), body)
    assert r.status_code == 422, (bad, r.text)


@pytest.mark.security
async def test_forbidden_attributes_and_weak_passwords(adm):
    name = uname()
    pg = adm.engine == "postgresql"
    for opts in ({"superuser": True}, {"replication": True}, {"bypassrls": True}, {"nope": 1}):
        r = await adm.api.post(
            adm.url("/principals"),
            {"name": name, "password": PW, "options": {"login": True, **opts}},
        )
        assert r.status_code == 422, (opts, r.text)
    for pw in ("short", "x" * 300):
        r = await adm.api.post(
            adm.url("/principals"),
            {"name": name, "password": pw, "options": {"login": True} if pg else {}},
        )
        assert r.status_code == 422
    r = await adm.api.post(
        adm.url("/principals"), {"name": name, "options": {"login": True} if pg else {}}
    )
    assert r.status_code == 422  # a login needs a password
    assert name not in [p["name"] for p in (await adm.api.get(adm.url("/principals"))).json()]


@pytest.mark.security
async def test_protected_accounts_cannot_be_changed(adm):
    own = adm.srv.info["username"]
    listed = {p["name"]: p for p in (await adm.api.get(adm.url("/principals"))).json()}
    assert listed[own]["protected"] is True
    q = host_q(adm)
    assert (
        await adm.api.patch(adm.url(f"/principals/{own}"), {"password": PW}, params=q)
    ).status_code in (403, 404)
    assert (
        await adm.api.delete(adm.url(f"/principals/{own}"), json={"confirm": own}, params=q)
    ).status_code in (403, 404)
    r = await adm.api.post(
        adm.url(f"/principals/{own}/grants"),
        {"privileges": ["SELECT"], "target": table_target(adm)},
        params=q,
    )
    assert r.status_code in (403, 404)
    assert login_ok(adm, own, adm.srv.info["password"])  # untouched
    assert (
        await adm.api.delete(
            adm.url("/principals/nobody_here"), json={"confirm": "nobody_here"}, params=q
        )
    ).status_code == 404


@pytest.mark.security
async def test_privilege_and_target_allowlists(adm):
    name = uname()
    await make_user(adm, name)
    q = host_q(adm)
    url = adm.url(f"/principals/{name}/grants")
    bad_privs = [
        "GRANT OPTION",
        "SUPER",
        "FILE",
        "SELECT; DROP TABLE x",
        "ALL ON *.* TO root",
        "",
        "CREATEROLE",
    ]
    for p in bad_privs:
        r = await adm.api.post(url, {"privileges": [p], "target": table_target(adm)}, params=q)
        assert r.status_code == 422, (p, r.text)
    for target in (
        {
            "kind": "table",
            "database": "shop",
            "object": "no_such_table",
            **({"schema": "public"} if adm.engine == "postgresql" else {}),
        },
        {"kind": "database", "database": "no_such_db"},
        {"kind": "bogus", "database": "shop"},
    ):
        r = await adm.api.post(url, {"privileges": ["SELECT"], "target": target}, params=q)
        assert r.status_code in (404, 422), (target, r.text)
    await adm.api.delete(adm.url(f"/principals/{name}"), json={"confirm": name}, params=q)


@pytest.mark.security
async def test_roles_connection_flags_and_scoped_grants_gate_access(adm, admin, new_user):
    viewer, editor = await new_user("Viewer"), await new_user("Editor")
    await grant(admin, adm.cid, viewer, "Viewer")
    await grant(admin, adm.cid, editor, "Editor")
    for u in (viewer, editor):
        assert (await u.get(adm.url("/principals"))).status_code == 403
        assert (
            await u.post(adm.url("/principals"), {"name": uname(), "password": PW})
        ).status_code == 403
    outsider = await new_user("Database Admin")
    assert (await outsider.get(adm.url("/principals"))).status_code == 404
    # a Database Admin may inspect and manage
    dba = await new_user("Database Admin")
    await grant(admin, adm.cid, dba, "Database Admin")
    assert (await dba.get(adm.url("/principals"))).status_code == 200
    # scoped grants never reach principal management
    roles = {r["name"]: r["id"] for r in (await admin.get("roles")).json()}
    scoped = await new_user("Viewer")
    await admin.put(
        adm.url("/scope-grants"),
        {
            "grants": [
                {"user_id": scoped.user_id, "role_id": roles["Database Admin"], "database": "shop"}
            ]
        },
    )
    assert (await scoped.get(adm.url("/principals"))).status_code == 403


async def test_read_only_connection_inspection_is_allowed_but_not_writing(adm, admin):
    ro = await create_connection(admin, adm.engine, adm.srv.info)  # read-only by default
    r = await admin.get(f"connections/{ro['id']}/principals")
    assert r.status_code == 200, r.text  # inspecting is not a write
    r = await admin.post(f"connections/{ro['id']}/principals", {"name": uname(), "password": PW})
    assert r.status_code == 403


async def test_duplicate_and_production_confirmation(adm, admin):
    name = uname()
    await make_user(adm, name)
    dup = {
        "name": name,
        "password": PW,
        "options": {"login": True} if adm.engine == "postgresql" else {},
    }
    assert (await adm.api.post(adm.url("/principals"), dup)).status_code == 409
    prod = await create_connection(
        admin, adm.engine, adm.srv.info, read_only=False, environment="production"
    )
    p = Adm(admin, adm.engine, prod["id"], adm.srv)
    q = host_q(adm)
    body = {"privileges": ["SELECT"], "target": table_target(adm)}
    r = await admin.post(p.url(f"/principals/{name}/grants"), body, params=q)
    assert r.status_code == 428 and r.json()["code"] == "confirmation_required"
    assert (
        await admin.post(p.url(f"/principals/{name}/grants"), {**body, "confirm": name}, params=q)
    ).status_code == 204
    await adm.api.delete(adm.url(f"/principals/{name}"), json={"confirm": name}, params=q)


async def test_changes_are_audited_without_secrets(adm, admin):
    name = uname()
    await make_user(adm, name)
    q = host_q(adm)
    await adm.api.post(
        adm.url(f"/principals/{name}/grants"),
        {"privileges": ["SELECT"], "target": table_target(adm)},
        params=q,
    )
    await adm.api.patch(adm.url(f"/principals/{name}"), {"password": PW2}, params=q)
    await adm.api.delete(adm.url(f"/principals/{name}"), json={"confirm": name}, params=q)
    text = (await admin.get("audit/events", params={"connection_id": adm.cid, "limit": 40})).text
    for action in (
        "admin.principal.create",
        "admin.privilege.grant",
        "admin.principal.alter",
        "admin.principal.drop",
    ):
        assert action in text, action
    assert PW not in text and PW2 not in text


async def test_privilege_changes_can_require_a_second_person(adm, admin, new_user):
    requester, reviewer = await new_user("Database Admin"), await new_user("Database Admin")
    await grant(admin, adm.cid, requester, "Database Admin")
    await grant(admin, adm.cid, reviewer, "Database Admin")
    await admin.put(
        "governance/policies",
        {
            "connection_id": adm.cid,
            "operations": ["admin.create_principal", "admin.grant_privileges"],
        },
    )
    name = uname()
    body = {
        "name": name,
        "password": PW,
        "options": {"login": True} if adm.engine == "postgresql" else {},
    }
    r = await requester.post(adm.url("/principals"), body)
    assert r.status_code == 428 and r.json()["code"] == "approval_required"
    aid = r.json()["approval_id"]
    pending = (await reviewer.get("approvals", params={"scope": "review"})).json()
    mine = next(a for a in pending if a["id"] == aid)
    assert (
        PW not in str(mine) and mine["summary"]["principal"] == name
    )  # secrets never reach approvals
    await reviewer.post(f"approvals/{aid}/approve", {})
    other = {**body, "name": name + "x"}
    assert (
        await requester.post(adm.url("/principals"), other, headers={"Approval-Id": aid})
    ).status_code == 422
    assert (
        await requester.post(adm.url("/principals"), body, headers={"Approval-Id": aid})
    ).status_code == 201
    await admin.put("governance/policies", {"connection_id": adm.cid, "operations": []})
    await adm.api.delete(adm.url(f"/principals/{name}"), json={"confirm": name}, params=host_q(adm))


# -------------------------------------------------------------------------- MongoDB


@pytest.fixture
def mdb(mongo_managed):
    name = f"pr_{uuid.uuid4().hex[:8]}"
    c = mongo_client(mongo_managed)
    c[name].items.insert_many([{"_id": 1}, {"_id": 2}])
    c[name].other.insert_one({"_id": 1})
    yield name, c
    c.drop_database(name)
    c.close()


def user_client(info: dict[str, Any], db: str, user: str, pw: str) -> pymongo.MongoClient:
    return pymongo.MongoClient(host=info["host"], port=info["port"], username=user, password=pw,
                               authSource=db, serverSelectionTimeoutMS=4000)  # fmt: skip


def can_read(info, db, user, pw, coll="items") -> bool:
    c = user_client(info, db, user, pw)
    try:
        return c[db][coll].count_documents({}) >= 0
    except pymongo.errors.PyMongoError:
        return False
    finally:
        c.close()


def can_write(info, db, user, pw, coll="items") -> bool:
    c = user_client(info, db, user, pw)
    try:
        c[db][coll].insert_one({"w": uuid.uuid4().hex})
        return True
    except pymongo.errors.PyMongoError:
        return False
    finally:
        c.close()


async def test_mongo_user_lifecycle_and_role_changes(madm, mdb):
    db, _ = mdb
    info = madm.srv.info
    base = madm.url(f"/mongo/databases/{db}")
    name = uname("mu")
    r = await madm.api.post(
        f"{base}/users", {"name": name, "password": PW, "roles": [{"role": "read"}]}
    )
    assert r.status_code == 201, r.text
    users = (await madm.api.get(f"{base}/users")).json()
    assert [u["name"] for u in users] == [name] and users[0]["roles"] == [
        {"role": "read", "db": db}
    ]
    assert PW not in str(users)
    assert can_read(info, db, name, PW) and not can_write(info, db, name, PW)
    r = await madm.api.post(f"{base}/users/{name}/roles", {"roles": [{"role": "readWrite"}]})
    assert r.status_code == 204, r.text
    assert can_write(info, db, name, PW)
    r = await madm.api.request(
        "DELETE", f"{base}/users/{name}/roles", json={"roles": [{"role": "readWrite"}]}
    )
    assert r.status_code == 204 and not can_write(info, db, name, PW)
    assert (
        await madm.api.put(f"{base}/users/{name}/password", {"password": PW2})
    ).status_code == 204
    assert not can_read(info, db, name, PW) and can_read(info, db, name, PW2)
    assert (await madm.api.delete(f"{base}/users/{name}", json={})).status_code == 428
    assert (
        await madm.api.delete(f"{base}/users/{name}", json={"confirm": name})
    ).status_code == 204
    assert not can_read(info, db, name, PW2)


async def test_mongo_custom_role_with_collection_privileges(madm, mdb):
    db, _ = mdb
    info, base = madm.srv.info, madm.url(f"/mongo/databases/{db}")
    role, user = uname("role"), uname("mu")
    r = await madm.api.post(
        f"{base}/roles",
        {"name": role, "privileges": [{"collection": "items", "actions": ["find"]}]},
    )
    assert r.status_code == 201, r.text
    roles = (await madm.api.get(f"{base}/roles")).json()
    mine = next(x for x in roles if x["role"] == role)
    assert not mine["builtin"] and mine["privileges"][0]["actions"] == ["find"]
    assert any(x["builtin"] for x in roles)
    await madm.api.post(f"{base}/users", {"name": user, "password": PW, "roles": [{"role": role}]})
    assert can_read(info, db, user, PW, "items") and not can_read(info, db, user, PW, "other")
    r = await madm.api.post(
        f"{base}/roles/{role}/privileges",
        {"privileges": [{"collection": "other", "actions": ["find"]}]},
    )
    assert r.status_code == 204, r.text
    assert can_read(info, db, user, PW, "other")
    r = await madm.api.request(
        "DELETE",
        f"{base}/roles/{role}/privileges",
        json={"privileges": [{"collection": "other", "actions": ["find"]}]},
    )
    assert r.status_code == 204 and not can_read(info, db, user, PW, "other")
    await madm.api.delete(f"{base}/users/{user}", json={"confirm": user})
    assert (await madm.api.delete(f"{base}/roles/{role}", json={})).status_code == 428
    assert (
        await madm.api.delete(f"{base}/roles/{role}", json={"confirm": role})
    ).status_code == 204


@pytest.mark.security
async def test_mongo_escalation_and_protection_rules(madm, mdb):
    db, c = mdb
    base = madm.url(f"/mongo/databases/{db}")
    for roles in ([{"role": "root"}], [{"role": "readAnyDatabase"}], [{"role": "read", "db": "admin"}],
                  [{"role": "__system"}], [{"role": "read", "db": "local"}], [{"role": "nope"}]):  # fmt: skip
        r = await madm.api.post(
            f"{base}/users", {"name": uname("mu"), "password": PW, "roles": roles}
        )
        assert r.status_code == 422, (roles, r.text)
    assert (
        await madm.api.post(
            f"{base}/users", {"name": "bad name", "password": PW, "roles": [{"role": "read"}]}
        )
    ).status_code == 422
    assert (
        await madm.api.post(
            f"{base}/users", {"name": uname(), "password": "short", "roles": [{"role": "read"}]}
        )
    ).status_code == 422
    for p in ({"actions": ["anyAction"]}, {"actions": ["find"], "db": "admin"}, {"actions": ["find"], "collection": "nope"},
              {"actions": ["find", "dropDatabase"]}):  # fmt: skip
        r = await madm.api.post(f"{base}/roles", {"name": uname("r"), "privileges": [p]})
        assert r.status_code in (404, 422), (p, r.text)
    for system_db in ("admin", "local", "config"):
        r = await madm.api.post(
            madm.url(f"/mongo/databases/{system_db}/users"),
            {"name": uname(), "password": PW, "roles": [{"role": "read", "db": db}]},
        )
        assert r.status_code in (403, 404), (system_db, r.status_code)
    assert (
        await madm.api.delete(
            madm.url("/mongo/databases/admin/users/root"), json={"confirm": "root"}
        )
    ).status_code in (403, 404)
    assert c.admin.command("usersInfo", "root")["users"]  # still there
    assert (
        await madm.api.delete(f"{base}/users/ghost", json={"confirm": "ghost"})
    ).status_code == 404
    builtin = await madm.api.delete(f"{base}/roles/read", json={"confirm": "read"})
    assert builtin.status_code in (403, 404)


@pytest.mark.security
async def test_mongo_principals_need_permissions_and_a_writable_connection(
    madm, mdb, admin, new_user, mongo_managed
):
    db, _ = mdb
    base = f"/mongo/databases/{db}"
    editor = await new_user("Editor")
    await grant(admin, madm.cid, editor, "Editor")
    assert (await editor.get(madm.url(f"{base}/users"))).status_code == 403
    assert (
        await editor.post(
            madm.url(f"{base}/users"),
            {"name": uname(), "password": PW, "roles": [{"role": "read"}]},
        )
    ).status_code == 403
    ro = (await create_connection(admin, "mongodb", mongo_managed))["id"]
    assert (await admin.get(f"connections/{ro}{base}/users")).status_code == 200
    assert (
        await admin.post(
            f"connections/{ro}{base}/users",
            {"name": uname(), "password": PW, "roles": [{"role": "read"}]},
        )
    ).status_code == 403
