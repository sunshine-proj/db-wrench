"""Scoped, expiring API tokens for automation (and, later, MCP).

* The secret (``dbw_`` + 256 random bits) is shown once; only its SHA-256 is stored.
* A token acts as its owner but is **never more powerful**: effective permissions on a
  connection are the intersection of the token's scopes and the owner's own rights, and the
  token may be limited to specific connections.
* Tokens carry connection-scope permissions only. They hold no application permissions, cannot
  manage users, roles, settings or tokens, and cannot approve requests (a human must).
* Using a token requires an active owner; revoking or expiring it takes effect immediately.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import CONNECTION_SCOPE, Perm
from app.auth.service import sha256
from app.errors import NotFound, ValidationFailed
from app.models import ApiToken, Connection, User, utcnow

PREFIX = "dbw_"
MAX_LIFETIME_DAYS = 365
MAX_TOKENS_PER_USER = 50
# Scopes a token may never carry, however it was created.
FORBIDDEN_SCOPES: frozenset[str] = frozenset(
    {
        Perm.CONNECTION_EDIT.value,
        Perm.CONNECTION_DELETE.value,
        Perm.CONNECTION_MANAGE_ACCESS.value,
        Perm.GOVERNANCE_APPROVE.value,
    }
)
ALLOWED_SCOPES: frozenset[str] = frozenset(p.value for p in CONNECTION_SCOPE) - FORBIDDEN_SCOPES
# Paths (below /api/v1) that accept bearer tokens at all; everything else is session-only.
BEARER_PATH_PREFIXES = ("/api/v1/connections", "/api/v1/approvals")


def looks_like_token(value: str) -> bool:
    return value.startswith(PREFIX) and 20 <= len(value) <= 80


async def create_token(
    db: AsyncSession,
    user: User,
    *,
    name: str,
    scopes: list[str],
    connection_ids: list[uuid.UUID] | None,
    expires_in_days: int,
) -> tuple[ApiToken, str]:
    name = name.strip()
    if not name:
        raise ValidationFailed("A token needs a name.")
    if not 1 <= expires_in_days <= MAX_LIFETIME_DAYS:
        raise ValidationFailed(f"Tokens last 1-{MAX_LIFETIME_DAYS} days.")
    wanted = sorted(set(scopes))
    if not wanted:
        raise ValidationFailed("Choose at least one scope.")
    bad = [s for s in wanted if s not in ALLOWED_SCOPES]
    if bad:
        raise ValidationFailed(f"Scope(s) not available to API tokens: {', '.join(bad)}.")
    if connection_ids is not None:
        if not connection_ids:
            raise ValidationFailed("connection_ids must be omitted (all) or non-empty.")
        found = set(
            (
                await db.execute(select(Connection.id).where(Connection.id.in_(connection_ids)))
            ).scalars()
        )
        if found != set(connection_ids):
            raise NotFound("Connection not found.")
    existing = (
        await db.execute(
            select(ApiToken.id).where(
                ApiToken.user_id == user.id,
                ApiToken.revoked_at.is_(None),
                ApiToken.expires_at > utcnow(),
            )
        )
    ).all()
    if len(existing) >= MAX_TOKENS_PER_USER:
        raise ValidationFailed(f"At most {MAX_TOKENS_PER_USER} active tokens per user.")
    secret = PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(
        id=uuid.uuid4(),
        user_id=user.id,
        name=name,
        prefix=secret[:12],
        token_hash=sha256(secret),
        scopes=wanted,
        connection_ids=[str(c) for c in connection_ids] if connection_ids else None,
        expires_at=utcnow() + timedelta(days=expires_in_days),
    )
    db.add(row)
    await db.flush()
    return row, secret


async def authenticate(db: AsyncSession, secret: str) -> ApiToken | None:
    """The token if it is valid *now* (not revoked/expired, owner active), else ``None``."""
    row = (
        await db.execute(select(ApiToken).where(ApiToken.token_hash == sha256(secret)))
    ).scalar_one_or_none()
    now = utcnow()
    if (
        row is None
        or row.revoked_at is not None
        or row.expires_at <= now
        or not row.user.is_active
        or row.user.must_change_password
    ):
        return None
    if row.last_used_at is None or now - row.last_used_at > timedelta(minutes=1):
        row.last_used_at = now  # throttled: not one write per request
    return row


async def list_tokens(db: AsyncSession, *, user_id: uuid.UUID | None) -> list[ApiToken]:
    stmt = select(ApiToken).order_by(ApiToken.created_at.desc())
    if user_id is not None:
        stmt = stmt.where(ApiToken.user_id == user_id)
    return list((await db.execute(stmt)).scalars())


async def revoke(db: AsyncSession, token_id: uuid.UUID) -> ApiToken:
    row = await db.get(ApiToken, token_id)
    if row is None:
        raise NotFound("Token not found.")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
    return row
