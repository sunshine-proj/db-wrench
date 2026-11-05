"""Approval requests and environment policies (Phase 5, PRD 5.12)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.schemas import Strict
from app.audit import service as audit
from app.auth.deps import AuthContext, CurrentAuth, DbSession, require_app_permission
from app.auth.permissions import Perm
from app.governance import service as gov
from app.models import Approval, Connection, User

router = APIRouter(tags=["governance"])

CanManageSettings = Annotated[
    AuthContext, Depends(require_app_permission(Perm.APP_MANAGE_SETTINGS))
]


class ApprovalOut(BaseModel):
    id: uuid.UUID
    connection_id: uuid.UUID
    connection_name: str | None
    environment: str | None
    requester: str | None
    operation: str
    summary: dict[str, Any]
    status: str
    reason: str | None
    decided_by: str | None
    created_at: datetime
    decided_at: datetime | None
    expires_at: datetime


class DecisionBody(Strict):
    reason: str | None = Field(default=None, max_length=500)


class PolicyIn(Strict):
    connection_id: uuid.UUID | None = None
    environment: str | None = Field(default=None, max_length=20)
    operations: list[str] = Field(max_length=50)


class PolicyOut(BaseModel):
    connection_id: uuid.UUID | None
    connection_name: str | None
    environment: str | None
    operations: list[str]


class PoliciesOut(BaseModel):
    policies: list[PolicyOut]
    available_operations: list[str]
    always_approved: list[str]


async def _out(db: DbSession, a: Approval) -> ApprovalOut:
    conn = await db.get(Connection, a.connection_id)
    requester = await db.get(User, a.requester_id)
    decider = await db.get(User, a.decided_by) if a.decided_by else None
    return ApprovalOut(
        id=a.id,
        connection_id=a.connection_id,
        connection_name=conn.name if conn else None,
        environment=conn.environment if conn else None,
        requester=requester.username if requester else None,
        operation=a.operation,
        summary=a.summary,
        status="expired"
        if a.status in ("pending", "approved") and a.expires_at < _now()
        else a.status,
        reason=a.reason,
        decided_by=decider.username if decider else None,
        created_at=a.created_at,
        decided_at=a.decided_at,
        expires_at=a.expires_at,
    )


def _now() -> datetime:
    from app.models import utcnow

    return utcnow()


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    db: DbSession,
    ctx: CurrentAuth,
    scope: Annotated[Literal["mine", "review"], Query()] = "mine",
    status: Annotated[
        str | None, Query(pattern="^(pending|approved|rejected|executed|cancelled)$")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ApprovalOut]:
    rows = await gov.list_visible(db, ctx, scope=scope, status=status, limit=limit)
    return [await _out(db, a) for a in rows]


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalOut)
async def approve(
    approval_id: uuid.UUID, body: DecisionBody, db: DbSession, ctx: CurrentAuth
) -> ApprovalOut:
    return await _out(db, await gov.decide(db, ctx, approval_id, approve=True, reason=body.reason))


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalOut)
async def reject(
    approval_id: uuid.UUID, body: DecisionBody, db: DbSession, ctx: CurrentAuth
) -> ApprovalOut:
    return await _out(db, await gov.decide(db, ctx, approval_id, approve=False, reason=body.reason))


@router.post("/approvals/{approval_id}/cancel", response_model=ApprovalOut)
async def cancel(approval_id: uuid.UUID, db: DbSession, ctx: CurrentAuth) -> ApprovalOut:
    return await _out(db, await gov.cancel(db, ctx, approval_id))


@router.get("/governance/policies", response_model=PoliciesOut)
async def get_policies(db: DbSession, _: CanManageSettings) -> PoliciesOut:
    out: list[PolicyOut] = []
    names = {c.id: c.name for c in (await db.execute(select(Connection))).scalars()}
    for p in await gov.list_policies(db):
        out.append(
            PolicyOut(
                connection_id=p.connection_id,
                connection_name=names.get(p.connection_id) if p.connection_id else None,
                environment=p.environment,
                operations=list(p.operations),
            )
        )
    return PoliciesOut(
        policies=out,
        available_operations=sorted(gov.POLICY_OPERATIONS),
        always_approved=sorted(gov.ALWAYS_APPROVED),
    )


@router.put("/governance/policies", response_model=PolicyOut)
async def put_policy(body: PolicyIn, db: DbSession, ctx: CanManageSettings) -> PolicyOut:
    row = await gov.set_policy(
        db,
        ctx,
        connection_id=body.connection_id,
        environment=body.environment,
        operations=body.operations,
    )
    await audit.record(
        "governance.policy.update",
        actor=ctx.actor,
        connection_id=body.connection_id,
        target={"environment": body.environment},
        detail={"operations": sorted(set(body.operations))},
    )
    return PolicyOut(
        connection_id=body.connection_id,
        connection_name=None,
        environment=body.environment,
        operations=list(row.operations) if row else [],
    )
