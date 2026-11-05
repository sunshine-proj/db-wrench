"""Phase 5: OIDC single sign-on against an in-process mock identity provider."""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ISSUER = "https://idp.test"
CLIENT_ID = "dbw-client"
CLIENT_SECRET = "s3cret-client-value"
PUBLIC_URL = "https://wrench.test"


class MockIdp:
    """Just enough of an OIDC provider: discovery, JWKS, token endpoint with PKCE + client auth."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.codes: dict[str, dict] = {}
        self.fail_token = False
        self.issuer_override: str | None = None
        self.app = self._build()

    def jwk(self, key=None, kid=None) -> dict:
        k = (key or self.key).public_key()
        jw = jwt.algorithms.RSAAlgorithm.to_jwk(k, as_dict=True)
        return {**jw, "kid": kid or self.kid, "alg": "RS256", "use": "sig"}

    def sign(self, claims: dict, *, key=None, alg="RS256", kid=None) -> str:
        return jwt.encode(claims, key or self.key, algorithm=alg, headers={"kid": kid or self.kid})

    def grant(self, sub: str, nonce: str, challenge: str, **claims) -> str:
        code = uuid.uuid4().hex
        now = int(time.time())
        self.codes[code] = {
            "challenge": challenge,
            "claims": {
                "iss": ISSUER, "aud": CLIENT_ID, "sub": sub, "iat": now, "exp": now + 300,
                "nonce": nonce, **claims,
            },
        }  # fmt: skip
        return code

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.get("/.well-known/openid-configuration")
        async def discovery():
            return {
                "issuer": self.issuer_override or ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/jwks",
            }

        @app.get("/jwks")
        async def jwks():
            return {"keys": [self.jwk()]}

        @app.post("/token")
        async def token(request: Request):
            if self.fail_token:
                return JSONResponse({"error": "server_error"}, status_code=500)
            form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
            basic = request.headers.get("authorization", "")
            expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if basic != f"Basic {expected}":
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            grant = self.codes.pop(form.get("code", ""), None)  # codes are single use
            if grant is None:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            verifier = form.get("code_verifier", "")
            digest = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if digest != grant["challenge"] or not form.get("redirect_uri", "").endswith(
                "/api/v1/auth/oidc/callback"
            ):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return {
                "id_token": self.sign(grant["claims"]),
                "access_token": "unused",
                "token_type": "Bearer",
            }

        return app


@pytest.fixture
def idp(monkeypatch):
    from app.auth import oidc
    from app.config import get_settings

    mock = MockIdp()
    monkeypatch.setattr(
        oidc, "http_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url=ISSUER),
    )  # fmt: skip

    async def allow(*_a, **_k):
        return None

    monkeypatch.setattr(oidc, "_check_destination", allow)
    monkeypatch.setattr(get_settings(), "public_url", PUBLIC_URL)
    oidc._DISCOVERY.clear()
    oidc._JWKS.clear()
    return mock


async def configure(admin, **over):
    body = {
        "enabled": True, "issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "role_mappings": [{"group": "db-admins", "role": "Database Admin"},
                          {"group": "readers", "role": "Viewer"}],
        **over,
    }  # fmt: skip
    r = await admin.put("settings/oidc", body)
    assert r.status_code == 200, r.text
    return r.json()


async def start(anon) -> dict:
    """Begin a login: returns the parameters the IdP received."""
    r = await anon.get("auth/oidc/login")
    assert r.status_code == 302, r.text
    loc = urlsplit(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{ISSUER}/authorize"
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    return q


async def sso_login(anon, idp, sub=None, groups=("readers",), **claims):
    q = await start(anon)
    code = idp.grant(sub or f"sub-{uuid.uuid4().hex[:8]}", q["nonce"], q["code_challenge"],
                     groups=list(groups), **{"email": "p@example.com", "preferred_username": "pat", **claims})  # fmt: skip
    r = await anon.get("auth/oidc/callback", params={"code": code, "state": q["state"]})
    return q, code, r


@pytest.fixture(autouse=True)
async def reset_oidc(admin):
    yield
    await admin.put("settings/oidc", {"enabled": False})


async def test_full_login_provisions_a_user_with_the_mapped_role(admin, anon, idp):
    cfg = await configure(admin)
    assert cfg["client_secret_set"] is True and "client_secret" not in cfg
    assert cfg["redirect_uri"] == f"{PUBLIC_URL}/api/v1/auth/oidc/callback"
    assert (await anon.get("auth/oidc/config")).json()["enabled"] is True

    q, _, r = await sso_login(anon, idp, groups=["readers"])
    assert q["code_challenge_method"] == "S256" and q["response_type"] == "code"
    assert q["client_id"] == CLIENT_ID and q["redirect_uri"].endswith("/api/v1/auth/oidc/callback")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "dbw_session" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]

    s = (await anon.get("auth/session")).json()  # the cookie works like a normal session
    assert s["user"]["role"] == "Viewer" and s["mfa_state"] == "ok" and s["csrf_token"]
    assert s["app_permissions"] == []


async def test_returning_user_follows_group_changes(admin, anon, idp):
    await configure(admin)
    sub = f"sub-{uuid.uuid4().hex[:8]}"
    _, _, r = await sso_login(anon, idp, sub=sub, groups=["readers"])
    assert (await anon.get("auth/session")).json()["user"]["role"] == "Viewer"
    anon.http.cookies.clear()
    _, _, r = await sso_login(anon, idp, sub=sub, groups=["db-admins"])
    assert r.status_code == 303
    assert (await anon.get("auth/session")).json()["user"]["role"] == "Database Admin"
    users = [u for u in (await admin.get("users")).json() if u["username"].startswith("pat")]
    assert len([u for u in users]) >= 1


async def test_usernames_do_not_collide_and_do_not_link_to_local_accounts(
    admin, anon, idp, new_user
):
    await configure(admin)
    local = await new_user("Super Admin" if False else "Viewer")
    # An IdP user whose preferred username equals an existing local account gets a new account
    q = await start(anon)
    code = idp.grant("sub-collide", q["nonce"], q["code_challenge"], groups=["readers"],
                     preferred_username=local.username, email="admin@example.com")  # fmt: skip
    r = await anon.get("auth/oidc/callback", params={"code": code, "state": q["state"]})
    assert r.status_code == 303 and r.headers["location"] == "/"
    me = (await anon.get("auth/session")).json()["user"]
    assert me["username"] != local.username and me["username"].startswith(local.username)


async def test_no_matching_group_is_refused_unless_a_default_role_exists(admin, anon, idp):
    await configure(admin)
    _, _, r = await sso_login(anon, idp, groups=["strangers"])
    assert r.status_code == 303 and r.headers["location"].startswith("/login?sso_error")
    assert "set-cookie" not in r.headers
    await configure(admin, default_role="Viewer")
    _, _, r = await sso_login(anon, idp, groups=[])
    assert r.headers["location"] == "/"


@pytest.mark.security
async def test_super_admin_can_never_be_assigned_by_sso(admin, idp):
    for body in (
        {"role_mappings": [{"group": "g", "role": "Super Admin"}]},
        {"default_role": "Super Admin"},
        {"role_mappings": [{"group": "g", "role": "NoSuchRole"}]},
    ):
        r = await admin.put("settings/oidc", {"enabled": True, "issuer": ISSUER, "client_id": "x",
                                              "client_secret": "y", **body})  # fmt: skip
        assert r.status_code == 422, (body, r.text)


@pytest.mark.security
async def test_state_is_single_use_and_bound_to_a_started_login(admin, anon, idp):
    await configure(admin)
    q, code, r = await sso_login(anon, idp)
    assert r.headers["location"] == "/"
    anon.http.cookies.clear()
    again = await anon.get("auth/oidc/callback", params={"code": code, "state": q["state"]})
    assert again.headers["location"].startswith("/login?sso_error")  # replay
    forged = await anon.get("auth/oidc/callback", params={"code": "x", "state": "never-issued"})
    assert forged.headers["location"].startswith("/login?sso_error")
    assert (await anon.get("auth/oidc/callback")).headers["location"].startswith("/login?sso_error")
    assert (
        (await anon.get("auth/oidc/callback", params={"error": "access_denied"}))
        .headers["location"]
        .startswith("/login")
    )


@pytest.mark.security
async def test_expired_login_state_is_refused(admin, anon, idp, app_sql):
    await configure(admin)
    q = await start(anon)
    app_sql("UPDATE oidc_login_states SET expires_at = now() - interval '1 minute'")
    code = idp.grant("sub-late", q["nonce"], q["code_challenge"], groups=["readers"])
    r = await anon.get("auth/oidc/callback", params={"code": code, "state": q["state"]})
    assert r.headers["location"].startswith("/login?sso_error")


@pytest.mark.security
@pytest.mark.parametrize(
    "tamper",
    ["nonce", "audience", "issuer", "expired", "wrong_key", "alg_none", "hs256", "missing_sub"],
)
async def test_forged_or_invalid_id_tokens_are_rejected(admin, anon, idp, tamper):
    await configure(admin)
    q = await start(anon)
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "sub-x", "iat": now, "exp": now + 300,
              "nonce": q["nonce"], "groups": ["readers"]}  # fmt: skip
    key, alg = idp.key, "RS256"
    if tamper == "nonce":
        claims["nonce"] = "other"
    elif tamper == "audience":
        claims["aud"] = "someone-else"
    elif tamper == "issuer":
        claims["iss"] = "https://evil.test"
    elif tamper == "expired":
        claims["exp"] = now - 3600
        claims["iat"] = now - 7200
    elif tamper == "wrong_key":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif tamper == "missing_sub":
        del claims["sub"]
    if tamper == "alg_none":
        token = jwt.encode(claims, None, algorithm="none", headers={"kid": idp.kid})
    elif tamper == "hs256":
        # algorithm-confusion attack: an HMAC-signed token must never be accepted
        token = jwt.encode(claims, "x" * 40, algorithm="HS256", headers={"kid": idp.kid})
    else:
        token = idp.sign(claims, key=key, alg=alg)
    code = uuid.uuid4().hex
    idp.codes[code] = {"challenge": q["code_challenge"], "claims": claims}
    # make the mock return our hand-built token
    idp.sign = lambda *_a, **_k: token  # type: ignore[method-assign]
    r = await anon.get("auth/oidc/callback", params={"code": code, "state": q["state"]})
    assert r.headers["location"].startswith("/login?sso_error"), tamper
    assert "set-cookie" not in r.headers


async def test_pkce_and_client_authentication_are_used(admin, anon, idp):
    await configure(admin, client_secret="wrong-secret")
    _, _, r = await sso_login(anon, idp)
    assert r.headers["location"].startswith("/login?sso_error")  # IdP rejected our client auth
    await configure(admin, client_secret=CLIENT_SECRET)
    _, _, r = await sso_login(anon, idp)
    assert r.headers["location"] == "/"


async def test_idp_failures_and_issuer_mismatch_deny_access(admin, anon, idp):
    await configure(admin)
    idp.fail_token = True
    _, _, r = await sso_login(anon, idp)
    assert r.headers["location"].startswith("/login?sso_error")
    idp.fail_token = False
    idp.issuer_override = "https://other.test"
    from app.auth import oidc

    oidc._DISCOVERY.clear()
    assert (await anon.get("auth/oidc/login")).status_code == 403
    idp.issuer_override = None


async def test_disabled_sso_and_deactivated_users_are_refused(admin, anon, idp, app_sql):
    assert (await anon.get("auth/oidc/login")).status_code == 403
    await configure(admin)
    sub = f"sub-{uuid.uuid4().hex[:8]}"
    await sso_login(anon, idp, sub=sub)
    anon.http.cookies.clear()
    app_sql(
        "UPDATE users SET is_active = false WHERE id = (SELECT user_id FROM user_identities WHERE subject = %s)",
        (sub,),
    )
    _, _, r = await sso_login(anon, idp, sub=sub)
    assert r.headers["location"].startswith("/login?sso_error")


async def test_sso_users_cannot_use_password_login(admin, anon, idp):
    await configure(admin)
    await sso_login(anon, idp, preferred_username="ssoonly")
    anon.http.cookies.clear()
    for pw in ("", "password", "ssoonly", "Adm1n-Passw0rd-Long!"):
        r = await anon.post("auth/login", {"username": "ssoonly", "password": pw or "x"})
        assert r.status_code in (401, 422), pw


@pytest.mark.security
async def test_configuration_needs_permission_https_and_hides_the_secret(
    admin, new_user, idp, app_sql, monkeypatch
):
    viewer = await new_user("Viewer")
    assert (await viewer.get("settings/oidc")).status_code == 403
    assert (await viewer.put("settings/oidc", {"enabled": False})).status_code == 403
    from app.config import get_settings

    with monkeypatch.context() as m:
        m.setattr(get_settings(), "network_allow_loopback", False)  # http is a dev-only allowance
        bad = await admin.put(
            "settings/oidc",
            {"enabled": True, "issuer": "http://idp.test", "client_id": "x", "client_secret": "y"},
        )
        assert bad.status_code == 422
    with monkeypatch.context() as m:
        m.setattr(get_settings(), "public_url", None)
        no_url = await admin.put(
            "settings/oidc",
            {"enabled": True, "issuer": ISSUER, "client_id": "x", "client_secret": "y"},
        )
        assert no_url.status_code == 422
    await configure(admin)
    stored = app_sql("SELECT value::text FROM app_settings WHERE key = 'oidc'")[0][0]
    assert CLIENT_SECRET not in stored  # envelope-encrypted at rest
    assert CLIENT_SECRET not in (await admin.get("settings/oidc")).text
    kept = await admin.put(
        "settings/oidc", {"enabled": True, "issuer": ISSUER, "client_id": CLIENT_ID}
    )
    assert kept.status_code == 200 and kept.json()["client_secret_set"] is True  # omitted = keep


async def test_sso_events_are_audited(admin, anon, idp):
    await configure(admin)
    await sso_login(anon, idp, preferred_username="audited-pat")
    await sso_login(anon, idp, groups=["nobody"])
    actions = [
        (e["action"], e["result"])
        for e in (
            await admin.get("audit/events", params={"action": "auth.sso", "limit": 20})
        ).json()["events"]
    ]
    assert ("auth.sso.login", "success") in actions
    assert ("auth.sso.login", "denied") in actions
    assert ("auth.sso.provision", "success") in actions
