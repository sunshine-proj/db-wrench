"""API token management (session-only: a token cannot mint or revoke tokens)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.schemas import Strict
from app.audit import service as audit
from app.auth import tokens as svc
from app.auth.deps import AuthContext, DbSession, require_app_permission
from app.auth.permissions import Perm
from app.errors import Forbidden, NotFound
from app.models import ApiToken

router = APIRouter(prefix="/tokens", tags=["tokens"])

CanManageTokens = Annotated[AuthContext, Depends(require_app_permission(Perm.APP_MANAGE_TOKENS))]


class TokenCreate(Strict):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(min_length=1, max_length=64)
    connection_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)
    expires_in_days: int = Field(default=30, ge=1, le=svc.MAX_LIFETIME_DAYS)


class TokenOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    owner: str
    scopes: list[str]
    connection_ids: list[uuid.UUID] | None
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    active: bool


class TokenCreated(TokenOut):
    secret: str = Field(description="Shown once; store it now.")


class TokenScopes(BaseModel):
    scopes: list[str]


def _out(t: ApiToken) -> TokenOut:
    from app.models import utcnow

    return TokenOut(
        id=t.id,
        name=t.name,
        prefix=t.prefix,
        owner=t.user.username,
        scopes=list(t.scopes),
        connection_ids=[uuid.UUID(c) for c in t.connection_ids] if t.connection_ids else None,
        created_at=t.created_at,
        expires_at=t.expires_at,
        last_used_at=t.last_used_at,
        revoked_at=t.revoked_at,
        active=t.revoked_at is None and t.expires_at > utcnow(),
    )


def _session_only(ctx: AuthContext) -> None:
    if ctx.token is not None:
        raise Forbidden("API tokens cannot manage API tokens.")


@router.get("/scopes", response_model=TokenScopes)
async def available_scopes(ctx: CanManageTokens) -> TokenScopes:
    return TokenScopes(scopes=sorted(svc.ALLOWED_SCOPES))


@router.get("", response_model=list[TokenOut])
async def list_tokens(
    db: DbSession,
    ctx: CanManageTokens,
    scope: Annotated[Literal["mine", "all"], Query()] = "mine",
) -> list[TokenOut]:
    if scope == "all":
        ctx.require_app(Perm.APP_MANAGE_USERS)
    rows = await svc.list_tokens(db, user_id=None if scope == "all" else ctx.user.id)
    return [_out(t) for t in rows]


@router.post("", status_code=201, response_model=TokenCreated)
async def create_token(body: TokenCreate, db: DbSession, ctx: CanManageTokens) -> TokenCreated:
    _session_only(ctx)
    row, secret = await svc.create_token(
        db,
        ctx.user,
        name=body.name,
        scopes=body.scopes,
        connection_ids=body.connection_ids,
        expires_in_days=body.expires_in_days,
    )
    await audit.record(
        "token.create",
        actor=ctx.actor,
        target={"token_id": str(row.id), "name": row.name, "prefix": row.prefix},
        detail={
            "scopes": row.scopes,
            "connections": row.connection_ids,
            "expires_at": row.expires_at.isoformat(),
        },
    )
    return TokenCreated(**_out(row).model_dump(), secret=secret)


@router.delete("/{token_id}", status_code=204)
async def revoke_token(token_id: uuid.UUID, db: DbSession, ctx: CanManageTokens) -> None:
    _session_only(ctx)
    row = await db.get(ApiToken, token_id)
    if row is None or (row.user_id != ctx.user.id and not ctx.has_app(Perm.APP_MANAGE_USERS)):
        raise NotFound("Token not found.")
    await svc.revoke(db, token_id)
    await audit.record(
        "token.revoke",
        actor=ctx.actor,
        target={"token_id": str(row.id), "name": row.name, "owner": row.user.username},
    )
