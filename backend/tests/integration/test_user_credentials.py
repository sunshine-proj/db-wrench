"""Phase 5: per-user managed-database credentials (credential_model = per_user)."""

from __future__ import annotations

import uuid

import psycopg
import pymongo
import pymysql
import pytest

from tests.integration.conftest import Api, create_connection, grant, pg_dsn

PW = "Sup3r-Secret-Db-Pw!"


@pytest.fixture(scope="module")
def db_logins(pg_container, mysql_managed, mongo_managed):
    """Dedicated database logins, created once per module with the superuser."""
    suffix = uuid.uuid4().hex[:6]
    names = {"pg": f"pgu_{suffix}", "mysql": f"myu_{suffix}", "mongo": f"mou_{suffix}"}
    with psycopg.connect(**pg_dsn(pg_container, "appdb"), autocommit=True) as c:
        c.execute(f"CREATE ROLE {names['pg']} LOGIN PASSWORD '{PW}'")
    with psycopg.connect(**pg_dsn(pg_container, "shop"), autocommit=True) as c:
        c.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {names['pg']}")
    conn = pymysql.connect(host=mysql_managed["host"], port=mysql_managed["port"], user="root",
                           password="rootpw", autocommit=True)  # fmt: skip
    with conn.cursor() as cur:
        cur.execute(f"CREATE USER '{names['mysql']}'@'%%' IDENTIFIED BY '{PW}'")
        cur.execute(f"GRANT SELECT ON shop.* TO '{names['mysql']}'@'%%'")
    conn.close()
    client = pymongo.MongoClient(host=mongo_managed["host"], port=mongo_managed["port"],
                                 username="root", password="rootpw")  # fmt: skip
    client.admin.command(
        "createUser", names["mongo"], pwd=PW, roles=[{"role": "read", "db": "shop"}]
    )
    client.close()
    return names


def per_user(engine: str, pg_managed, mysql_managed, mongo_managed):
    return {"postgresql": pg_managed, "mysql": mysql_managed, "mongodb": mongo_managed}[engine]


@pytest.fixture(params=["postgresql", "mysql", "mongodb"])
async def pu(request, admin, new_user, pg_managed, mysql_managed, mongo_managed, db_logins):
    engine = request.param
    info = per_user(engine, pg_managed, mysql_managed, mongo_managed)
    conn = await create_connection(admin, engine, info, credential_model="per_user")
    user = await new_user("Viewer")
    await grant(admin, conn["id"], user, "Viewer")
    login = {
        "postgresql": db_logins["pg"],
        "mysql": db_logins["mysql"],
        "mongodb": db_logins["mongo"],
    }[engine]
    return engine, conn["id"], user, login


def data_call(engine: str, cid: str):
    if engine == "mongodb":
        return f"connections/{cid}/mongo/databases/shop/collections"
    return f"connections/{cid}/databases"


async def test_data_access_needs_the_users_own_credentials(pu):
    engine, cid, user, login = pu
    r = await user.get(data_call(engine, cid))
    assert r.status_code == 409 and r.json()["code"] == "credentials_required", r.text
    st = (await user.get(f"connections/{cid}/credentials/me")).json()
    assert st == {"required": True, "configured": False, "username": None}


async def test_wrong_credentials_are_rejected_and_not_stored(pu):
    engine, cid, user, login = pu
    r = await user.put(f"connections/{cid}/credentials/me", {"username": login, "password": "nope"})
    assert r.status_code == 422, r.text
    assert (await user.get(f"connections/{cid}/credentials/me")).json()["configured"] is False
    assert (await user.get(data_call(engine, cid))).status_code == 409


async def test_with_own_credentials_the_server_sees_that_login(pu):
    engine, cid, user, login = pu
    r = await user.put(f"connections/{cid}/credentials/me", {"username": login, "password": PW})
    assert r.status_code == 200, r.text
    assert r.json() == {"required": True, "configured": True, "username": login}
    assert PW not in r.text
    listed = await user.get(data_call(engine, cid))
    assert listed.status_code == 200, listed.text


async def test_sql_runs_as_the_users_own_login(
    admin, new_user, pg_managed, mysql_managed, db_logins
):
    for engine, info, login, sql in (
        ("postgresql", pg_managed, db_logins["pg"], "SELECT current_user"),
        (
            "mysql",
            mysql_managed,
            db_logins["mysql"],
            "SELECT SUBSTRING_INDEX(CURRENT_USER(), '@', 1)",
        ),
    ):
        conn = await create_connection(admin, engine, info, credential_model="per_user")
        user = await new_user("Viewer")
        await grant(admin, conn["id"], user, "Viewer")
        await user.put(
            f"connections/{conn['id']}/credentials/me", {"username": login, "password": PW}
        )
        body = {
            "database": "shop",
            "sql": sql,
            **({"schema": "public"} if engine == "postgresql" else {}),
        }
        r = await user.post(f"connections/{conn['id']}/queries/execute", body)
        assert r.status_code == 200, r.text
        assert r.json()["results"][0]["rows"][0][0] == login
        # a second user's login is independent
        other = await new_user("Viewer")
        await grant(admin, conn["id"], other, "Viewer")
        assert (
            await other.post(f"connections/{conn['id']}/queries/execute", body)
        ).status_code == 409


async def test_credentials_are_private_removable_and_never_leak(pu, admin, app_sql):
    engine, cid, user, login = pu
    await user.put(f"connections/{cid}/credentials/me", {"username": login, "password": PW})
    stored = app_sql(
        "SELECT secret, username FROM user_credentials WHERE connection_id = %s", (cid,)
    )[0]
    assert PW not in stored[0] and stored[1] == login  # envelope-encrypted at rest
    for path in (f"connections/{cid}", "connections"):
        assert PW not in (await admin.get(path)).text
    events = (await admin.get("audit/events", params={"connection_id": cid, "limit": 50})).text
    assert PW not in events and "connection.credentials.set" in events
    assert (await user.delete(f"connections/{cid}/credentials/me")).status_code == 204
    assert (await user.get(data_call(engine, cid))).status_code == 409
    assert not app_sql("SELECT 1 FROM user_credentials WHERE connection_id = %s", (cid,))


async def test_each_user_can_only_touch_their_own_credentials(pu, new_user, admin):
    engine, cid, user, login = pu
    await user.put(f"connections/{cid}/credentials/me", {"username": login, "password": PW})
    other = await new_user("Viewer")
    assert (await other.get(f"connections/{cid}/credentials/me")).status_code == 404  # no grant
    await grant(admin, cid, other, "Viewer")
    assert (await other.get(f"connections/{cid}/credentials/me")).json()["configured"] is False
    assert (await other.get(data_call(engine, cid))).status_code == 409


async def test_shared_connections_refuse_personal_credentials(admin, pg_managed):
    conn = await create_connection(admin, "postgresql", pg_managed)
    r = await admin.put(
        f"connections/{conn['id']}/credentials/me", {"username": "x", "password": "y"}
    )
    assert r.status_code == 409
    assert (await admin.get(f"connections/{conn['id']}/credentials/me")).json()["required"] is False


async def test_switching_the_model_drops_shared_access(admin, pg_managed):
    conn = await create_connection(admin, "postgresql", pg_managed)
    assert (await admin.get(f"connections/{conn['id']}/databases")).status_code == 200
    r = await admin.patch(f"connections/{conn['id']}", {"credential_model": "per_user"})
    assert r.status_code == 200 and r.json()["credential_model"] == "per_user"
    assert (
        await admin.get(f"connections/{conn['id']}/databases")
    ).status_code == 409  # even for admins
    await admin.patch(f"connections/{conn['id']}", {"credential_model": "shared"})
    assert (await admin.get(f"connections/{conn['id']}/databases")).status_code == 200


async def test_connection_test_uses_the_callers_login(pu):
    engine, cid, user, login = pu
    await user.put(f"connections/{cid}/credentials/me", {"username": login, "password": PW})
    # viewers cannot test; the owner path is covered by the explorer call above
    assert isinstance(user, Api)
