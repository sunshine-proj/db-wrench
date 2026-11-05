"""FastAPI dependencies: client IP, Origin/CSRF checks, authentication, authorization."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import Actor
from app.auth import service, tokens
from app.auth.permissions import APP_SCOPE, SUPER_ADMIN, Perm
from app.config import Settings, get_settings
from app.db import get_session
from app.errors import Forbidden, MfaRequired, Unauthorized
from app.models import ApiToken, SessionRow, User

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

DbSession = Annotated[AsyncSession, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def client_ip(request: Request, settings: Settings) -> str:
    """Client address. ``trusted_proxy_hops`` > 0 means the rightmost X-Forwarded-For entry
    appended by our own reverse proxy is used; client-supplied prefixes are ignored."""
    xff = request.headers.get("x-forwarded-for")
    if xff and settings.trusted_proxy_hops > 0:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if len(parts) >= settings.trusted_proxy_hops:
            return parts[-settings.trusted_proxy_hops]
    return request.client.host if request.client else "unknown"


async def origin_guard(request: Request, settings: AppSettings) -> None:
    """Reject cross-origin state-changing requests (defence in depth beside SameSite)."""
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is None:
        return  # non-browser client; CSRF token still required for cookie sessions
    host = request.headers.get("host", "")
    if urlsplit(origin).netloc == host or origin in settings.allowed_origins:
        return
    raise Forbidden("Cross-origin request rejected.", extra={"code_hint": "origin"})


@dataclass
class AuthContext:
    user: User
    session: SessionRow
    ip: str
    settings: Settings
    token: ApiToken | None = None  # set when authenticated by an API token

    @property
    def is_super_admin(self) -> bool:
        return self.user.role.name == SUPER_ADMIN

    @property
    def app_permissions(self) -> frozenset[str]:
        if self.token is not None:  # tokens never carry application permissions
            return frozenset()
        if self.is_super_admin:
            return frozenset(p.value for p in Perm)
        return frozenset(set(self.user.role.permissions) & {p.value for p in APP_SCOPE})

    def has_app(self, perm: Perm) -> bool:
        return perm.value in self.app_permissions

    def require_app(self, perm: Perm) -> None:
        if not self.has_app(perm):
            raise Forbidden(f"Missing permission: {perm.value}")

    @property
    def actor(self) -> Actor:
        return Actor(
            self.user.id, self.user.username, self.ip, self.token.id if self.token else None
        )


async def session_ctx(request: Request, db: DbSession, settings: AppSettings) -> AuthContext:
    """Authenticated session in *any* MFA state. CSRF-checked for unsafe methods."""
    bearer = _bearer(request)
    if bearer is not None:
        return await _token_ctx(request, db, settings, bearer)
    token = request.cookies.get(settings.cookie_name)
    if not token:
        raise Unauthorized()
    row = await service.load_session(db, settings, token)
    if row is None:
        raise Unauthorized("Session expired or revoked.")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent, row.csrf_token):
            raise Forbidden("CSRF token missing or invalid.", extra={"code_hint": "csrf"})
    return AuthContext(row.user, row, client_ip(request, settings), settings)


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


async def _token_ctx(
    request: Request, db: AsyncSession, settings: Settings, secret: str
) -> AuthContext:
    """Authenticate an API token. No cookie, no CSRF (a bearer header is not sent ambiently),
    and only the data-plane paths: everything else is session-only."""
    row = await tokens.authenticate(db, secret) if tokens.looks_like_token(secret) else None
    if row is None:
        raise Unauthorized("Invalid, expired or revoked API token.")
    if not request.url.path.startswith(tokens.BEARER_PATH_PREFIXES):
        raise Forbidden("API tokens cannot be used for this endpoint.")
    # A transient session object keeps AuthContext uniform; it is never persisted.
    transient = SessionRow(
        id=row.id,
        user_id=row.user_id,
        csrf_token="",
        mfa_state="ok",
        token_hash="-",  # noqa: S106
    )
    transient.user = row.user
    return AuthContext(row.user, transient, client_ip(request, settings), settings, row)


async def auth_pw_ok(ctx: Annotated[AuthContext, Depends(session_ctx)]) -> AuthContext:
    """MFA satisfied; allowed even when a password change is pending."""
    if ctx.session.mfa_state != "ok":
        raise MfaRequired(extra={"mfa_state": ctx.session.mfa_state})
    return ctx


async def auth(ctx: Annotated[AuthContext, Depends(auth_pw_ok)]) -> AuthContext:
    if ctx.user.must_change_password:
        raise Forbidden(
            "Password change required.", extra={"code_hint": "password_change_required"}
        )
    return ctx


CurrentAuth = Annotated[AuthContext, Depends(auth)]


def require_app_permission(
    perm: Perm,
) -> Callable[..., Coroutine[Any, Any, AuthContext]]:
    async def dependency(ctx: CurrentAuth) -> AuthContext:
        ctx.require_app(perm)
        return ctx

    return dependency
