"""OpenID Connect single sign-on (authorization-code flow with PKCE) and group-to-role mapping.

Security properties (ADR-0011):

* login state, nonce and PKCE verifier live in the application database (any replica can finish
  a login); the state is stored hashed and is single use with a short expiry;
* the ID token is verified: signature against the issuer's JWKS with an algorithm allowlist
  (no ``none``/HMAC), ``iss``, ``aud``, ``exp``/``iat`` and ``nonce``;
* every outbound request goes through the network policy; endpoints must be HTTPS (relaxed only
  with the development loopback flag);
* identities are keyed by the immutable ``sub``; an SSO login never attaches to a pre-existing
  local account by e-mail address (account-takeover vector) and never yields Super Admin;
* the role is recomputed from the groups claim at every login; no matching group and no default
  role means access is refused.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.passwords import hash_password
from app.auth.permissions import SUPER_ADMIN
from app.config import Settings
from app.connections import service as conns
from app.errors import AppError, ValidationFailed
from app.models import AppSetting, OidcLoginState, Role, SessionRow, User, UserIdentity, utcnow
from app.services.runtime import get_envelope, run_blocking

OIDC_KEY = "oidc"
PROVIDER = "oidc"
STATE_TTL = timedelta(minutes=10)
CACHE_TTL = 600
MAX_BODY = 1_000_000
ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256", "PS384", "PS512"]
_SECRET_CONTEXT = "oidc:client_secret"  # noqa: S105 - an encryption context label


class SsoDenied(AppError):
    status = 403
    code = "sso_denied"
    title = "Single sign-on was refused"


@dataclass
class OidcConfig:
    enabled: bool = False
    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""  # decrypted; never leaves the backend
    scopes: str = "openid email profile"
    groups_claim: str = "groups"
    username_claim: str = "preferred_username"
    display_name: str = "Single sign-on"
    role_mappings: list[dict[str, str]] = field(default_factory=list)
    default_role: str | None = None


# ----------------------------------------------------------------------------- config


async def load_config(db: AsyncSession) -> OidcConfig:
    row = await db.get(AppSetting, OIDC_KEY)
    if row is None:
        return OidcConfig()
    v = dict(row.value)
    enc = v.pop("client_secret", "")
    cfg = OidcConfig(**{k: v[k] for k in OidcConfig.__dataclass_fields__ if k in v})
    cfg.client_secret = (
        str(get_envelope().decrypt_json(enc, _SECRET_CONTEXT).get("secret", "")) if enc else ""
    )
    return cfg


async def save_config(
    db: AsyncSession, settings: Settings, user_id: Any, new: OidcConfig, *, keep_secret: bool
) -> None:
    if new.enabled:
        if not new.issuer or not new.client_id:
            raise ValidationFailed("issuer and client_id are required.")
        _require_https(settings, new.issuer, "issuer")
        if not settings.public_url:
            raise ValidationFailed("Set DBW_PUBLIC_URL before enabling single sign-on.")
        if not (new.client_secret or keep_secret):
            raise ValidationFailed("client_secret is required.")
    names = {r for r in (await db.execute(select(Role.name))).scalars()}
    for m in new.role_mappings:
        if m["role"] == SUPER_ADMIN:
            raise ValidationFailed("Single sign-on cannot assign the Super Admin role.")
        if m["role"] not in names:
            raise ValidationFailed(f"Unknown role: {m['role']!r}.")
    if new.default_role is not None and (
        new.default_role == SUPER_ADMIN or new.default_role not in names
    ):
        raise ValidationFailed("default_role must be an existing role other than Super Admin.")
    if "openid" not in new.scopes.split():
        raise ValidationFailed("scopes must include openid.")
    value = {k: getattr(new, k) for k in OidcConfig.__dataclass_fields__ if k != "client_secret"}
    row = await db.get(AppSetting, OIDC_KEY)
    if keep_secret and row is not None:
        value["client_secret"] = row.value.get("client_secret", "")
    else:
        value["client_secret"] = get_envelope().encrypt_json(
            {"secret": new.client_secret}, _SECRET_CONTEXT
        )
    if row is None:
        db.add(AppSetting(key=OIDC_KEY, value=value, updated_by=user_id))
    else:
        row.value, row.updated_at, row.updated_by = value, utcnow(), user_id
    _DISCOVERY.clear()
    _JWKS.clear()


# ----------------------------------------------------------------------------- http


def _require_https(settings: Settings, url: str, what: str) -> None:
    parts = urlsplit(url)
    if not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValidationFailed(f"{what} is not a valid URL.")
    if parts.scheme != "https" and not (settings.network_allow_loopback and parts.scheme == "http"):
        raise ValidationFailed(f"{what} must use https.")


def http_client() -> httpx.AsyncClient:
    """Overridden in tests. Redirects are never followed: a redirect could leave the policy."""
    return httpx.AsyncClient(timeout=10.0, follow_redirects=False)


async def _check_destination(db: AsyncSession, settings: Settings, url: str) -> None:
    parts = urlsplit(url)
    policy = await conns.get_network_policy(db, settings)
    await run_blocking(policy.resolve, parts.hostname or "", parts.port or 443)


async def _fetch(
    db: AsyncSession, settings: Settings, method: str, url: str, **kw: Any
) -> dict[str, Any]:
    _require_https(settings, url, "endpoint")
    await _check_destination(db, settings, url)
    try:
        async with http_client() as client:
            resp = await client.request(method, url, **kw)
    except httpx.HTTPError as exc:
        raise SsoDenied("The identity provider could not be reached.") from exc
    if resp.status_code != 200 or len(resp.content) > MAX_BODY:
        raise SsoDenied("The identity provider returned an unexpected response.")
    try:
        data = resp.json()
    except ValueError as exc:
        raise SsoDenied("The identity provider returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise SsoDenied("The identity provider returned an unexpected document.")
    return data


_DISCOVERY: dict[str, tuple[float, dict[str, Any]]] = {}
_JWKS: dict[str, tuple[float, dict[str, Any]]] = {}


async def discovery(db: AsyncSession, settings: Settings, cfg: OidcConfig) -> dict[str, Any]:
    hit = _DISCOVERY.get(cfg.issuer)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    doc = await _fetch(
        db, settings, "GET", cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
    )
    if doc.get("issuer") != cfg.issuer:
        raise SsoDenied("The identity provider's issuer does not match the configuration.")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not isinstance(doc.get(key), str):
            raise SsoDenied(f"The identity provider's metadata lacks {key}.")
        _require_https(settings, doc[key], key)
    _DISCOVERY[cfg.issuer] = (time.monotonic(), doc)
    return doc


async def _jwks(db: AsyncSession, settings: Settings, uri: str, *, refresh: bool) -> dict[str, Any]:
    hit = _JWKS.get(uri)
    if hit and not refresh and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    doc = await _fetch(db, settings, "GET", uri)
    _JWKS[uri] = (time.monotonic(), doc)
    return doc


# ----------------------------------------------------------------------------- login


def redirect_uri(settings: Settings) -> str:
    assert settings.public_url
    return settings.public_url.rstrip("/") + "/api/v1/auth/oidc/callback"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def begin_login(db: AsyncSession, settings: Settings) -> str:
    cfg = await load_config(db)
    if not cfg.enabled:
        raise SsoDenied("Single sign-on is not enabled.")
    meta = await discovery(db, settings, cfg)
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    await db.execute(delete(OidcLoginState).where(OidcLoginState.expires_at < utcnow()))
    db.add(
        OidcLoginState(
            state_hash=_hash(state),
            nonce=nonce,
            code_verifier=verifier,
            expires_at=utcnow() + STATE_TTL,
        )
    )
    await db.flush()
    query = urlencode(
        {
            "response_type": "code",
            "client_id": cfg.client_id,
            "redirect_uri": redirect_uri(settings),
            "scope": cfg.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge.decode(),
            "code_challenge_method": "S256",
        }
    )
    sep = "&" if "?" in meta["authorization_endpoint"] else "?"
    return meta["authorization_endpoint"] + sep + query


async def _verify_id_token(
    db: AsyncSession,
    settings: Settings,
    cfg: OidcConfig,
    meta: dict[str, Any],
    token: str,
    nonce: str,
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise SsoDenied("The ID token is malformed.") from exc
    alg = header.get("alg")
    if alg not in ALGORITHMS:
        raise SsoDenied("The ID token uses an unsupported signature algorithm.")
    key = None
    for refresh in (False, True):  # a rotated key shows up as an unknown kid: refetch once
        jwks = await _jwks(db, settings, meta["jwks_uri"], refresh=refresh)
        candidates = [k for k in jwks.get("keys", []) if isinstance(k, dict)]
        kid = header.get("kid")
        match = [k for k in candidates if k.get("kid") == kid] if kid else candidates
        if len(match) == 1:
            try:
                key = jwt.PyJWK.from_dict(match[0]).key
            except jwt.PyJWTError as exc:
                raise SsoDenied("The identity provider published an unusable key.") from exc
            break
    if key is None:
        raise SsoDenied("The ID token was signed with an unknown key.")
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[alg],
            audience=cfg.client_id,
            issuer=cfg.issuer,
            leeway=60,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise SsoDenied("The ID token could not be verified.") from exc
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise SsoDenied("The ID token nonce does not match.")
    if (
        isinstance(claims.get("aud"), list)
        and len(claims["aud"]) > 1
        and claims.get("azp") != cfg.client_id
    ):
        raise SsoDenied("The ID token was issued to a different party.")
    return claims


def _groups(claims: dict[str, Any], claim: str) -> set[str]:
    node: Any = claims
    for part in claim.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    if isinstance(node, str):
        return {node}
    if isinstance(node, list):
        return {g for g in node if isinstance(g, str)}
    return set()


def map_role(cfg: OidcConfig, groups: set[str]) -> str | None:
    for m in cfg.role_mappings:  # ordered: the first matching mapping wins
        if m["group"] in groups:
            return m["role"]
    return cfg.default_role


_USERNAME_BAD = re.compile(r"[^a-z0-9._-]+")


async def _free_username(db: AsyncSession, claims: dict[str, Any], cfg: OidcConfig) -> str:
    base = str(claims.get(cfg.username_claim) or "").strip()
    if not base and isinstance(claims.get("email"), str):
        base = claims["email"].split("@", 1)[0]
    base = _USERNAME_BAD.sub("-", (base or "sso-" + str(claims["sub"])[:12]).lower()).strip("-.")[
        :80
    ]
    if len(base) < 3:
        base = ("sso-" + base)[:80]
    name, n = base, 1
    while (await db.execute(select(User.id).where(User.username == name))).first():
        n += 1
        name = f"{base}-{n}"
    return name


async def finish_login(
    db: AsyncSession,
    settings: Settings,
    *,
    code: str,
    state: str,
    ip: str | None,
    user_agent: str | None,
) -> tuple[User, SessionRow, str, bool]:
    """Returns (user, session, session token, created)."""
    cfg = await load_config(db)
    if not cfg.enabled:
        raise SsoDenied("Single sign-on is not enabled.")
    row = (
        await db.execute(
            delete(OidcLoginState)
            .where(OidcLoginState.state_hash == _hash(state))
            .returning(
                OidcLoginState.nonce, OidcLoginState.code_verifier, OidcLoginState.expires_at
            )
        )
    ).first()  # single use: consumed by the lookup itself
    if row is None or row.expires_at < utcnow():
        raise SsoDenied("The login attempt is unknown or expired. Start again.")
    meta = await discovery(db, settings, cfg)
    token_response = await _fetch(
        db,
        settings,
        "POST",
        meta["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(settings),
            "code_verifier": row.code_verifier,
        },
        auth=(cfg.client_id, cfg.client_secret),
        headers={"Accept": "application/json"},
    )
    id_token = token_response.get("id_token")
    if not isinstance(id_token, str):
        raise SsoDenied("The identity provider returned no ID token.")
    claims = await _verify_id_token(db, settings, cfg, meta, id_token, row.nonce)
    role_name = map_role(cfg, _groups(claims, cfg.groups_claim))
    if role_name is None or role_name == SUPER_ADMIN:
        raise SsoDenied("Your account is not a member of a group that may use this application.")
    role = (await db.execute(select(Role).where(Role.name == role_name))).scalar_one_or_none()
    if role is None:
        raise SsoDenied("The configured role no longer exists.")
    sub = str(claims["sub"])[:255]
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    identity = (
        await db.execute(
            select(UserIdentity).where(
                UserIdentity.provider == PROVIDER, UserIdentity.subject == sub
            )
        )
    ).scalar_one_or_none()
    created = False
    if identity is None:
        user = User(
            username=await _free_username(db, claims, cfg),
            display_name=str(claims.get("name") or "")[:200],
            email=email,
            password_hash=hash_password(secrets.token_urlsafe(48)),  # unusable: SSO only
            role_id=role.id,
            must_change_password=False,
        )
        db.add(user)
        await db.flush()
        db.add(UserIdentity(user_id=user.id, provider=PROVIDER, subject=sub, email=email))
        created = True
    else:
        user = await db.get(User, identity.user_id)  # type: ignore[assignment]
        assert user is not None
        if not user.is_active:
            raise SsoDenied("This account is disabled.")
        identity.last_login_at, identity.email = utcnow(), email
        if user.role_id != role.id:
            if user.role.name == SUPER_ADMIN:
                raise SsoDenied("This account holds a role that single sign-on does not manage.")
            user.role_id = role.id
            await service.revoke_user_sessions(db, user.id)  # role change ends old sessions
        if email:
            user.email = email
    await db.refresh(user, ["role"])
    user.last_login_at = utcnow()
    session, token = await service.create_session(
        db, settings, user, mfa_state="ok", ip=ip, user_agent=user_agent
    )
    return user, session, token, created
