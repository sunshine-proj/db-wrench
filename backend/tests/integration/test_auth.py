"""Authentication, sessions, MFA, user/role management and privilege-escalation guards."""

from __future__ import annotations

import time
import uuid

import pyotp
import pytest

from tests.integration.conftest import ADMIN_PASSWORD, USER_PASSWORD, Api


async def test_setup_is_one_time_and_validates_token(asgi_app, setup_token, admin):
    # `admin` fixture already consumed the token.
    api = Api(asgi_app)
    assert (await api.get("auth/setup-status")).json() == {"setup_required": False}
    r = await api.post(
        "auth/setup",
        {"token": setup_token, "username": "second", "password": ADMIN_PASSWORD},
    )
    assert r.status_code == 403
    r = await api.post(
        "auth/setup", {"token": "x" * 40, "username": "third", "password": ADMIN_PASSWORD}
    )
    assert r.status_code == 403
    await api.aclose()


async def test_startup_tasks_are_idempotent(admin):
    from app.services.bootstrap import run_startup

    await run_startup()  # schema is current, roles synced, no token needed


async def test_login_sets_hardened_cookie_and_returns_csrf(asgi_app, admin):
    api = Api(asgi_app)
    r = await api.post("auth/login", {"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    body = r.json()
    assert (
        body["mfa_state"] == "ok"
        and body["csrf_token"]
        and "argon2" not in r.text
        and "password_hash" not in r.text
    )
    await api.aclose()


async def test_wrong_credentials_are_indistinguishable(anon, admin):
    a = await anon.post("auth/login", {"username": "admin", "password": "nope-nope-nope-1A!"})
    b = await anon.post("auth/login", {"username": "ghost-user", "password": "nope-nope-nope-1A!"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]


async def test_unauthenticated_and_bad_csrf_are_rejected(admin, anon):
    assert (await anon.get("auth/session")).status_code == 401
    assert (await anon.get("connections")).status_code == 401
    # authenticated but missing / wrong CSRF token on an unsafe method
    saved, admin.csrf = admin.csrf, None
    r = await admin.post("roles", {"name": "csrf-probe", "permissions": []})
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]
    admin.csrf = "wrong"
    assert (await admin.post("roles", {"name": "csrf-probe", "permissions": []})).status_code == 403
    admin.csrf = saved


@pytest.mark.security
async def test_cross_origin_state_change_is_rejected(admin):
    r = await admin.post(
        "roles",
        {"name": "origin-probe", "permissions": []},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    ok = await admin.post(
        "roles",
        {"name": f"origin-ok-{uuid.uuid4().hex[:6]}", "permissions": []},
        headers={"Origin": "http://testserver"},
    )
    assert ok.status_code == 201


async def test_account_lockout_after_repeated_failures(new_user, asgi_app):
    u = await new_user("Viewer", login=False)
    api = Api(asgi_app)
    for _ in range(3):  # DBW_LOGIN_MAX_FAILURES=3 in tests
        assert (await api.login(u.username, "wrong-Password-1!")).status_code == 401
    # Even the correct password is refused while locked.
    r = await api.login(u.username, USER_PASSWORD)
    assert r.status_code == 401
    await api.aclose()


async def test_session_idle_timeout_and_logout(new_user, app_sql):
    u = await new_user("Viewer")
    assert (await u.get("auth/session")).status_code == 200
    app_sql(
        "UPDATE sessions SET last_seen_at = now() - interval '2 hours' WHERE user_id = %s",
        (u.user_id,),
    )
    assert (await u.get("auth/session")).status_code == 401  # idle expired

    u2 = await new_user("Viewer")
    assert (await u2.post("auth/logout")).status_code == 204
    assert (await u2.get("auth/session")).status_code == 401


async def test_absolute_session_expiry(new_user, app_sql):
    u = await new_user("Viewer")
    app_sql(
        "UPDATE sessions SET expires_at = now() - interval '1 minute' WHERE user_id = %s",
        (u.user_id,),
    )
    assert (await u.get("auth/session")).status_code == 401


async def test_password_change_revokes_other_sessions_and_enforces_policy(new_user, asgi_app):
    u = await new_user("Viewer")
    other = Api(asgi_app)
    assert (await other.login(u.username, USER_PASSWORD)).status_code == 200
    r = await u.post("auth/password", {"current_password": USER_PASSWORD, "new_password": "weak"})
    assert r.status_code == 422
    r = await u.post(
        "auth/password", {"current_password": "bad", "new_password": "N3w-Passw0rd-Long!"}
    )
    assert r.status_code == 403
    r = await u.post(
        "auth/password", {"current_password": USER_PASSWORD, "new_password": "N3w-Passw0rd-Long!"}
    )
    assert r.status_code == 204
    assert (await u.get("auth/session")).status_code == 200  # current session kept
    assert (await other.get("auth/session")).status_code == 401  # others revoked
    await other.aclose()


async def test_admin_created_users_must_change_password(admin, asgi_app):
    roles = {r["name"]: r for r in (await admin.get("roles")).json()}
    name = f"fresh{uuid.uuid4().hex[:6]}"
    r = await admin.post(
        "users", {"username": name, "password": USER_PASSWORD, "role_id": roles["Viewer"]["id"]}
    )
    assert r.status_code == 201 and r.json()["must_change_password"] is True
    api = Api(asgi_app)
    assert (await api.login(name, USER_PASSWORD)).status_code == 200
    r = await api.get("connections")
    assert r.status_code == 403 and "Password change" in r.json()["detail"]
    assert (await api.get("auth/session")).json()["password_change_required"] is True
    r = await api.post(
        "auth/password", {"current_password": USER_PASSWORD, "new_password": "Br4nd-New-Passw0rd!"}
    )
    assert r.status_code == 204
    assert (await api.get("connections")).status_code == 200
    await api.aclose()


async def test_mfa_enrollment_login_replay_and_recovery(new_user, asgi_app):
    u = await new_user("Viewer")
    start = await u.post("auth/mfa/enroll")
    assert start.status_code == 200
    secret = start.json()["secret"]
    assert start.json()["otpauth_uri"].startswith("otpauth://totp/")
    totp = pyotp.TOTP(secret)
    assert (await u.post("auth/mfa/enroll/confirm", {"code": "000000"})).status_code == 422
    confirm = await u.post("auth/mfa/enroll/confirm", {"code": totp.now()})
    assert confirm.status_code == 200
    recovery = confirm.json()["recovery_codes"]
    assert len(recovery) == 10

    # new login now requires the second factor
    api = Api(asgi_app)
    r = await api.login(u.username, USER_PASSWORD)
    assert r.json()["mfa_state"] == "pending_verify"
    blocked = await api.get("connections")
    assert blocked.status_code == 403 and blocked.json()["code"] == "mfa_required"
    assert (await api.post("auth/mfa/verify", {"code": "123456"})).status_code == 401

    # the code accepted at enrollment cannot be replayed; use a later step
    code = totp.at(int(time.time()) + 30)
    r = await api.post("auth/mfa/verify", {"code": code})
    assert r.status_code == 200 and r.json()["mfa_state"] == "ok"
    api.csrf = r.json()["csrf_token"]
    assert (await api.get("connections")).status_code == 200

    replay = Api(asgi_app)
    await replay.login(u.username, USER_PASSWORD)
    assert (await replay.post("auth/mfa/verify", {"code": code})).status_code == 401  # replay

    # recovery code works exactly once
    rc = Api(asgi_app)
    await rc.login(u.username, USER_PASSWORD)
    assert (await rc.post("auth/mfa/verify", {"code": recovery[0]})).status_code == 200
    rc2 = Api(asgi_app)
    await rc2.login(u.username, USER_PASSWORD)
    assert (await rc2.post("auth/mfa/verify", {"code": recovery[0]})).status_code == 401
    for a in (api, replay, rc, rc2):
        await a.aclose()


async def test_role_requiring_mfa_forces_enrollment(admin, new_user, asgi_app):
    roles = {r["name"]: r for r in (await admin.get("roles")).json()}
    r = await admin.post(
        "roles",
        {
            "name": f"strict-{uuid.uuid4().hex[:6]}",
            "permissions": ["connection.view"],
            "require_mfa": True,
        },
    )
    assert r.status_code == 201
    role_id = r.json()["id"]
    name = f"m{uuid.uuid4().hex[:8]}"
    created = await admin.post(
        "users", {"username": name, "password": USER_PASSWORD, "role_id": role_id}
    )
    assert created.status_code == 201
    from tests.integration import conftest as c  # noqa: F401

    api = Api(asgi_app)
    login = await api.login(name, USER_PASSWORD)
    assert login.json()["mfa_state"] == "pending_enroll"
    # everything but enrollment is blocked
    assert (await api.get("connections")).status_code == 403
    secret = (await api.post("auth/mfa/enroll")).json()["secret"]
    done = await api.post("auth/mfa/enroll/confirm", {"code": pyotp.TOTP(secret).now()})
    assert done.status_code == 200
    api.csrf = done.json()["csrf_token"]
    assert (await api.get("auth/session")).json()["mfa_state"] == "ok"
    # a role that requires MFA cannot opt out
    r = await api.post(
        "auth/mfa/disable",
        {"password": USER_PASSWORD, "code": pyotp.TOTP(secret).at(int(time.time()) + 30)},
    )
    assert r.status_code in (403, 422)
    del roles
    await api.aclose()


async def test_session_listing_and_revocation(new_user, asgi_app):
    u = await new_user("Viewer")
    second = Api(asgi_app)
    await second.login(u.username, USER_PASSWORD)
    listing = (await u.get("auth/sessions")).json()
    assert len(listing) == 2 and sum(s["current"] for s in listing) == 1
    other = next(s for s in listing if not s["current"])
    assert (await u.delete(f"auth/sessions/{other['id']}")).status_code == 204
    assert (await second.get("auth/session")).status_code == 401
    assert (await u.delete(f"auth/sessions/{uuid.uuid4()}")).status_code == 404
    await second.aclose()


# ------------------------------------------------------------------------- users / roles


@pytest.mark.security
async def test_viewer_cannot_manage_users_or_roles(new_user):
    v = await new_user("Viewer")
    assert (await v.get("users")).status_code == 403
    assert (await v.post("roles", {"name": "x1", "permissions": []})).status_code == 403
    assert (await v.get("audit/events")).status_code == 403
    assert (await v.get("settings/network-policy")).status_code == 403


@pytest.mark.security
async def test_privilege_escalation_is_blocked(admin, new_user):
    roles = {r["name"]: r for r in (await admin.get("roles")).json()}
    # A custom role that may manage users, but is not a Super Admin.
    r = await admin.post(
        "roles",
        {
            "name": f"hr-{uuid.uuid4().hex[:6]}",
            "permissions": ["app.manage_users", "connection.view"],
        },
    )
    hr_role = r.json()
    helpdesk = await new_user("Viewer")
    await admin.patch(f"users/{helpdesk.user_id}", {"role_id": hr_role["id"]})
    await helpdesk.login(helpdesk.username, USER_PASSWORD)  # role change revoked old sessions

    nm = f"esc{uuid.uuid4().hex[:6]}"
    # cannot create a Super Admin
    r = await helpdesk.post(
        "users", {"username": nm, "password": USER_PASSWORD, "role_id": roles["Super Admin"]["id"]}
    )
    assert r.status_code == 403
    # cannot hand out a role with permissions it does not hold
    r = await helpdesk.post(
        "users", {"username": nm, "password": USER_PASSWORD, "role_id": roles["Editor"]["id"]}
    )
    assert r.status_code == 403
    # cannot create a role that grants more than it has
    r = await helpdesk.post("roles", {"name": "grab", "permissions": ["app.manage_users"]})
    assert r.status_code == 403  # lacks app.manage_roles
    # cannot touch the Super Admin account
    admin_id = next(
        u["id"] for u in (await helpdesk.get("users")).json() if u["username"] == "admin"
    )
    assert (await helpdesk.patch(f"users/{admin_id}", {"is_active": False})).status_code == 403
    assert (
        await helpdesk.post(
            f"users/{admin_id}/password-reset", {"new_password": "Hijack-Passw0rd-1!"}
        )
    ).status_code == 403
    # but may create a user with a role that is a strict subset of its own permissions
    small = (
        await admin.post(
            "roles", {"name": f"small-{uuid.uuid4().hex[:6]}", "permissions": ["connection.view"]}
        )
    ).json()
    ok = await helpdesk.post(
        "users", {"username": nm, "password": USER_PASSWORD, "role_id": small["id"]}
    )
    assert ok.status_code == 201


async def test_cannot_deactivate_self_or_last_super_admin_and_deactivation_revokes_sessions(
    admin, new_user
):
    me = next(u for u in (await admin.get("users")).json() if u["username"] == "admin")
    r = await admin.patch(f"users/{me['id']}", {"is_active": False})
    assert r.status_code == 409
    roles = {r["name"]: r for r in (await admin.get("roles")).json()}
    r = await admin.patch(f"users/{me['id']}", {"role_id": roles["Viewer"]["id"]})
    assert r.status_code == 409 and "Super Admin" in r.json()["detail"]

    victim = await new_user("Editor")
    assert (await victim.get("connections")).status_code == 200
    assert (await admin.patch(f"users/{victim.user_id}", {"is_active": False})).status_code == 200
    assert (await victim.get("connections")).status_code == 401
    assert (await victim.login(victim.username, USER_PASSWORD)).status_code == 401


async def test_builtin_roles_are_immutable_and_custom_roles_validated(admin):
    roles = {r["name"]: r for r in (await admin.get("roles")).json()}
    assert (
        await admin.patch(f"roles/{roles['Viewer']['id']}", {"permissions": ["data.read"]})
    ).status_code == 409
    assert (await admin.delete(f"roles/{roles['Viewer']['id']}")).status_code == 409
    assert (
        await admin.post("roles", {"name": "bogus-perm", "permissions": ["no.such"]})
    ).status_code == 422
    dup = await admin.post("roles", {"name": "Viewer", "permissions": []})
    assert dup.status_code == 409


async def test_admin_password_reset_forces_change_and_revokes(admin, new_user):
    u = await new_user("Viewer")
    r = await admin.post(
        f"users/{u.user_id}/password-reset", {"new_password": "Reset-Passw0rd-Long!"}
    )
    assert r.status_code == 204
    assert (await u.get("auth/session")).status_code == 401
    again = await u.login(u.username, "Reset-Passw0rd-Long!")
    assert again.status_code == 200 and again.json()["user"]["must_change_password"] is True


async def test_user_directory_is_limited_to_people_who_manage_access(admin, new_user):
    dba = await new_user("Database Admin")
    viewer = await new_user("Viewer")
    names = [u["username"] for u in (await dba.get("directory/users")).json()]
    assert dba.username in names and viewer.username in names  # type: ignore[attr-defined]
    entry = (await admin.get("directory/users")).json()[0]
    assert set(entry) == {"id", "username", "display_name"}  # no email / role / MFA details
    assert (await viewer.get("directory/users")).status_code == 403
