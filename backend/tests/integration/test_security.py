"""Cross-cutting security regression tests (run with ``make test-sec``)."""

from __future__ import annotations

import json
import re
import uuid

import pytest

from tests.integration.conftest import ADMIN_PASSWORD, Api

pytestmark = pytest.mark.security

PUBLIC = {
    ("GET", "/api/v1/auth/setup-status"),
    ("POST", "/api/v1/auth/setup"),
    ("POST", "/api/v1/auth/login"),
    # single sign-on: the login flow starts and ends unauthenticated (state-bound, no CSRF
    # surface: GET only, no session needed to *begin*, and the callback validates single-use state)
    ("GET", "/api/v1/auth/oidc/config"),
    ("GET", "/api/v1/auth/oidc/login"),
    ("GET", "/api/v1/auth/oidc/callback"),
}


def routes(app):
    """Every (METHOD, path) the API exposes, taken from the generated OpenAPI document."""
    out = []
    for path, ops in app.openapi()["paths"].items():
        if path.startswith("/api/v1"):
            out.extend(
                (m.upper(), path) for m in ops if m in ("get", "post", "put", "patch", "delete")
            )
    return sorted(out)


def fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", lambda m: str(uuid.uuid4()) if "id" in m.group(0) else "x", path)


async def test_every_non_public_route_requires_authentication(asgi_app, anon):
    checked = 0
    for method, path in routes(asgi_app):
        if (method, path) in PUBLIC:
            continue
        r = await anon.request(
            method, fill(path), json={} if method in ("POST", "PUT", "PATCH") else None
        )
        assert r.status_code == 401, f"{method} {path} -> {r.status_code}"
        checked += 1
    assert checked > 40  # guard against the sweep silently covering nothing


async def test_every_state_changing_route_enforces_csrf(asgi_app, admin):
    unsafe = [(m, p) for m, p in routes(asgi_app) if m in ("POST", "PUT", "PATCH", "DELETE")]
    session_only = Api(asgi_app)
    r = await session_only.post("auth/login", {"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 200  # cookie jar holds a valid session; we deliberately omit the token
    checked = 0
    for method, path in unsafe:
        if (method, path) in PUBLIC:
            continue
        r = await session_only.request(method, fill(path), json={} if method != "DELETE" else None)
        assert r.status_code == 403 and "CSRF" in r.json()["detail"], (
            f"{method} {path} -> {r.status_code}"
        )
        checked += 1
    assert checked >= 15
    await session_only.aclose()


async def test_every_api_response_carries_security_headers_and_request_id(admin, anon):
    for r in (
        await anon.get("auth/setup-status"),
        await admin.get("connections"),
        await anon.get("connections"),
    ):
        h = r.headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY"
        assert h["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert re.fullmatch(r"[0-9a-f]{32}", h["x-request-id"])


async def test_request_id_is_propagated_and_sanitised(anon):
    r = await anon.get("auth/setup-status", headers={"X-Request-ID": "trace-abc-12345"})
    assert r.headers["x-request-id"] == "trace-abc-12345"
    r = await anon.get("auth/setup-status", headers={"X-Request-ID": "bad id\r\nX-Evil: 1"})
    assert r.headers["x-request-id"] != "bad id" and "x-evil" not in r.headers


async def test_errors_are_problem_json_with_request_id_and_no_internals(admin, anon):
    for r in (await anon.get("connections"), await admin.get(f"connections/{uuid.uuid4()}"),
              await admin.post("roles", {"bogus": 1})):  # fmt: skip
        assert r.headers["content-type"].startswith("application/problem+json")
        body = r.json()
        assert {"status", "code", "title", "request_id"} <= set(body)
        assert (
            "Traceback" not in r.text
            and "sqlalchemy" not in r.text.lower()
            and "psycopg" not in r.text.lower()
        )


async def test_oversized_request_bodies_are_rejected(admin):
    big = {"name": "x" * (9 * 1024 * 1024), "permissions": []}
    r = await admin.post("roles", big)
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"


async def test_user_listing_never_exposes_credentials_or_mfa_secrets(admin):
    text = (await admin.get("users")).text.lower()
    for forbidden in ("password_hash", "argon2", "mfa_secret", "recovery", "token_hash"):
        assert forbidden not in text


async def test_sessions_are_stored_hashed_not_raw(admin, app_sql):
    cookie = admin.http.cookies.get("dbw_session")
    assert cookie
    stored = " ".join(r[0] for r in app_sql("SELECT token_hash FROM sessions"))
    assert cookie not in stored and len(stored.split()[0]) == 64


async def test_unknown_user_login_costs_roughly_the_same_as_wrong_password(anon):
    """Timing equalisation: an Argon2 verify happens even when the account does not exist."""
    import time

    async def timed(username: str) -> float:
        t = time.perf_counter()
        await anon.post("auth/login", {"username": username, "password": "Wr0ng-Password-123!"})
        return time.perf_counter() - t

    known = min([await timed("admin") for _ in range(3)])
    unknown = min([await timed(f"ghost{uuid.uuid4().hex[:6]}") for _ in range(3)])
    assert unknown > known * 0.4  # a skipped hash would be orders of magnitude faster


async def test_ip_rate_limit_blocks_credential_stuffing(asgi_app, app_sql, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "login_ip_max_attempts", 5)
    app_sql("DELETE FROM login_attempts")  # failures from earlier tests share this client IP
    api = Api(asgi_app)
    try:
        statuses = []
        for i in range(8):
            r = await api.post(
                "auth/login", {"username": f"stuff{i}", "password": "Wr0ng-Password-123!"}
            )
            statuses.append(r.status_code)
        assert statuses[:5] == [401] * 5 and set(statuses[5:]) == {429}
        # correct credentials from the throttled address are refused too
        assert (
            await api.post("auth/login", {"username": "admin", "password": ADMIN_PASSWORD})
        ).status_code == 429
    finally:
        app_sql("DELETE FROM login_attempts")
        await api.aclose()


async def test_idor_connection_ids_do_not_leak_other_users_connections(admin, new_user, pg_managed):
    from tests.integration.conftest import create_connection

    secret_conn = await create_connection(admin, "postgresql", pg_managed)
    attacker = await new_user("Editor")
    for path in (
        f"connections/{secret_conn['id']}",
        f"connections/{secret_conn['id']}/databases",
        f"connections/{secret_conn['id']}/databases/shop/schemas/public/tables/customers/rows",
        f"connections/{secret_conn['id']}/grants",
    ):
        assert (await attacker.get(path)).status_code == 404, path
    assert (await attacker.delete(f"connections/{secret_conn['id']}")).status_code == 404
    assert (
        await attacker.patch(f"connections/{secret_conn['id']}", {"name": "pwn"})
    ).status_code == 404
    # and it is absent from the audit trail they can see (they have no audit.view)
    assert (await attacker.get("audit/events")).status_code == 403


async def test_audit_log_never_contains_secrets_anywhere(admin, app_sql):
    dump = json.dumps(
        app_sql("SELECT target::text, detail::text, actor_username FROM audit_events")
    )
    for needle in (
        ADMIN_PASSWORD,
        "Sup3r-Secret-Connection-Pw",
        "Another-Secret-Value-77",
        "WrongPw-12345",
    ):
        assert needle not in dump
