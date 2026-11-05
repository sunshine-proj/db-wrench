"""Phase 5: scoped, expiring API tokens."""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.integration.conftest import USER_PASSWORD, Api, create_connection, grant


@pytest.fixture
async def conn_id(admin, pg_managed):
    return (await create_connection(admin, "postgresql", pg_managed, read_only=False))["id"]


def bearer(api: Api, secret: str) -> Api:
    """A client that authenticates with a token only (no cookie, no CSRF)."""
    other = Api.__new__(Api)
    other.http = httpx.AsyncClient(
        transport=api.http._transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {secret}"},
    )
    other.csrf = None
    other.user = None
    return other


async def make_token(api: Api, **over) -> dict:
    body = {"name": "ci", "scopes": ["connection.view", "metadata.view", "data.read"], **over}
    r = await api.post("tokens", body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_token_authenticates_without_cookie_or_csrf(admin, conn_id):
    t = await make_token(admin)
    assert t["secret"].startswith("dbw_") and t["prefix"] == t["secret"][:12]
    tok = bearer(admin, t["secret"])
    r = await tok.get("connections")
    assert r.status_code == 200 and conn_id in [c["id"] for c in r.json()]
    r = await tok.get(f"connections/{conn_id}/databases")
    assert r.status_code == 200
    await tok.aclose()


async def test_secret_is_shown_once_and_stored_hashed(admin, app_sql):
    t = await make_token(admin, name="hash-check")
    assert all("secret" not in x for x in (await admin.get("tokens")).json())
    stored = app_sql("SELECT token_hash, prefix FROM api_tokens WHERE id = %s", (t["id"],))[0]
    assert t["secret"] not in stored[0] and len(stored[0]) == 64 and stored[1] == t["prefix"]
    assert not app_sql("SELECT 1 FROM api_tokens WHERE token_hash = %s", (t["secret"],))


async def test_scopes_limit_what_a_token_can_do(admin, conn_id):
    tok = bearer(admin, (await make_token(admin))["secret"])
    base = f"connections/{conn_id}/databases/shop/schemas/public/tables"
    assert (await tok.get(f"{base}/customers/rows", params={"limit": 2})).status_code == 200
    r = await tok.post(f"{base}/customers/rows", {"values": {"name": "x"}})
    assert r.status_code == 403 and "data.insert" in r.json()["detail"]
    r = await tok.post(f"connections/{conn_id}/queries/execute",
                       {"database": "shop", "schema": "public", "sql": "select 1"})  # fmt: skip
    assert r.status_code == 403
    await tok.aclose()


async def test_a_token_is_never_more_powerful_than_its_owner(admin, new_user, conn_id):
    viewer = await new_user("Viewer")
    await grant(admin, conn_id, viewer, "Viewer")
    # give the viewer a role that may create tokens
    role = await admin.post(
        "roles", {"name": f"tok-{uuid.uuid4().hex[:6]}", "permissions": ["app.manage_tokens"]}
    )
    assert role.status_code == 201, role.text
    owner = await new_user("Viewer")
    await admin.patch(f"users/{owner.user_id}", {"role_id": role.json()["id"]})
    await owner.login(owner.username, USER_PASSWORD)  # a role change ends their sessions
    await grant(admin, conn_id, owner, "Viewer")
    t = await make_token(owner, scopes=["data.read", "data.insert", "connection.view"])
    tok = bearer(admin, t["secret"])
    base = f"connections/{conn_id}/databases/shop/schemas/public/tables/customers/rows"
    assert (await tok.get(base, params={"limit": 1})).status_code == 200
    r = await tok.post(base, {"values": {"name": "nope"}})
    assert r.status_code == 403  # scope says yes, the owner's Viewer grant says no
    await tok.aclose()


async def test_token_can_be_limited_to_connections(admin, conn_id, pg_managed):
    other = (await create_connection(admin, "postgresql", pg_managed))["id"]
    t = await make_token(admin, connection_ids=[conn_id])
    tok = bearer(admin, t["secret"])
    assert [c["id"] for c in (await tok.get("connections")).json()] == [conn_id]
    assert (await tok.get(f"connections/{other}/databases")).status_code in (403, 404)
    assert (await tok.get(f"connections/{conn_id}/databases")).status_code == 200
    await tok.aclose()


async def test_expired_revoked_and_garbage_tokens_are_rejected(admin, app_sql, conn_id):
    t = await make_token(admin)
    tok = bearer(admin, t["secret"])
    assert (await tok.get("connections")).status_code == 200
    app_sql(
        "UPDATE api_tokens SET expires_at = now() - interval '1 minute' WHERE id = %s", (t["id"],)
    )
    assert (await tok.get("connections")).status_code == 401
    t2 = await make_token(admin)
    tok2 = bearer(admin, t2["secret"])
    assert (await admin.delete(f"tokens/{t2['id']}")).status_code == 204
    assert (await tok2.get("connections")).status_code == 401
    for junk in ("dbw_" + "a" * 43, "nonsense", "dbw_", "x" * 500):
        assert (await bearer(admin, junk).get("connections")).status_code == 401
    await tok.aclose()
    await tok2.aclose()


async def test_deactivating_the_owner_disables_the_token(admin, new_user, app_sql):
    role = await admin.post(
        "roles", {"name": f"tok-{uuid.uuid4().hex[:6]}", "permissions": ["app.manage_tokens"]}
    )
    owner = await new_user("Viewer")
    await admin.patch(f"users/{owner.user_id}", {"role_id": role.json()["id"]})
    await owner.login(owner.username, USER_PASSWORD)  # a role change ends their sessions
    t = await make_token(owner, scopes=["connection.view"])
    tok = bearer(admin, t["secret"])
    assert (await tok.get("connections")).status_code == 200
    app_sql("UPDATE users SET is_active = false WHERE id = %s", (owner.user_id,))
    assert (await tok.get("connections")).status_code == 401
    await tok.aclose()


@pytest.mark.security
async def test_tokens_cannot_reach_session_only_endpoints(admin, conn_id):
    tok = bearer(admin, (await make_token(admin))["secret"])
    for method, path in (
        ("GET", "users"), ("GET", "roles"), ("GET", "tokens"), ("POST", "tokens"),
        ("GET", "audit/events"), ("GET", "settings/network-policy"),
        ("GET", "governance/policies"), ("GET", "auth/session"), ("POST", "auth/logout"),
        ("GET", "auth/sessions"),
    ):  # fmt: skip
        r = await tok.request(method, path, json={} if method == "POST" else None)
        assert r.status_code in (401, 403), (method, path, r.status_code)
    await tok.aclose()


@pytest.mark.security
async def test_privileged_scopes_cannot_be_put_on_a_token(admin):
    for scope in ("connection.edit", "connection.delete", "connection.manage_access",
                  "governance.approve", "app.manage_users", "audit.view", "bogus"):  # fmt: skip
        r = await admin.post("tokens", {"name": "x", "scopes": [scope]})
        assert r.status_code == 422, (scope, r.text)
    assert (await admin.post("tokens", {"name": "x", "scopes": []})).status_code == 422
    assert (
        await admin.post("tokens", {"name": "x", "scopes": ["data.read"], "expires_in_days": 400})
    ).status_code == 422
    assert (
        await admin.post(
            "tokens", {"name": "x", "scopes": ["data.read"], "connection_ids": [str(uuid.uuid4())]}
        )
    ).status_code == 404


@pytest.mark.security
async def test_token_management_needs_the_permission_and_isolation(admin, new_user):
    viewer = await new_user("Viewer")
    assert (await viewer.post("tokens", {"name": "x", "scopes": ["data.read"]})).status_code == 403
    t = await make_token(admin, name="admins-token")
    assert (await viewer.delete(f"tokens/{t['id']}")).status_code in (403, 404)
    names = [x["name"] for x in (await admin.get("tokens", params={"scope": "all"})).json()]
    assert "admins-token" in names


async def test_token_activity_is_audited_with_the_token_identity(admin, conn_id):
    t = await make_token(
        admin, name="audited", scopes=["connection.view", "query.execute_read", "data.read"]
    )
    tok = bearer(admin, t["secret"])
    r = await tok.post(
        f"connections/{conn_id}/queries/execute",
        {"database": "shop", "schema": "public", "sql": "select 1"},
    )
    assert r.status_code == 200, r.text
    events = (await admin.get("audit/events", params={"limit": 50})).json()["events"]
    assert any(e["action"] == "token.create" and e["target"]["name"] == "audited" for e in events)
    used = [e for e in events if e["detail"].get("api_token_id") == t["id"]]
    assert used, "an action performed with the token must record the token id"
    assert t["secret"] not in str(events)
    await tok.aclose()
