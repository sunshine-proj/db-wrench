"""Environment policies and the four-eyes approval workflow (PRD 5.12).

A *policy* lists operation keys that need approval on a connection (or on every connection of
an environment). When a gated operation is attempted without an approval, an approval request
is stored (in its own transaction, so it survives the rejected request) and the API answers
``428 approval_required``. A second user holding ``governance.approve`` on that connection
approves or rejects it. The requester then repeats the *same* request with
``Approval-Id: <id>``; the approval is bound to the requester, the connection, the operation
and a digest of the parameters, expires, and is consumed atomically (single use) in the
request's own transaction, so a failed execution does not burn it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, NoReturn

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.deps import AuthContext
from app.auth.permissions import Perm
from app.connections.service import Access, resolve_access
from app.db import session_factory
from app.editor import tokens
from app.errors import ApprovalRequired, Conflict, Forbidden, NotFound, ValidationFailed
from app.models import Approval, ApprovalPolicy, Connection, utcnow

APPROVAL_TTL = timedelta(hours=24)

# Operations that can be placed under a policy.
POLICY_OPERATIONS: frozenset[str] = frozenset(
    {
        "admin.create_database",
        "admin.drop_database",
        "admin.create_schema",
        "admin.drop_schema",
        "admin.create_collection",
        "admin.drop_collection",
        "admin.create_index",
        "admin.drop_index",
        "data.bulk_delete",
        "data.bulk_update",
        "import.commit",
        "admin.create_principal",
        "admin.alter_principal",
        "admin.drop_principal",
        "admin.grant_privileges",
        "admin.revoke_privileges",
        "query.mongo_write_stage",
    }
)
# Never executed without a second person, whatever the policy says.
ALWAYS_APPROVED: frozenset[str] = frozenset({"query.mongo_write_stage"})
ENVIRONMENTS = ("production", "staging", "development", "other")


def params_digest(operation: str, connection_id: uuid.UUID, params: dict[str, Any]) -> str:
    return tokens.digest({"op": operation, "cid": str(connection_id), "p": params})


# ---------------------------------------------------------------------------- policies


async def required_operations(db: AsyncSession, conn: Connection) -> frozenset[str]:
    own = (
        await db.execute(select(ApprovalPolicy).where(ApprovalPolicy.connection_id == conn.id))
    ).scalar_one_or_none()
    if own is not None:
        return frozenset(own.operations) | ALWAYS_APPROVED
    env = (
        await db.execute(
            select(ApprovalPolicy).where(ApprovalPolicy.environment == conn.environment)
        )
    ).scalar_one_or_none()
    return frozenset(env.operations if env else ()) | ALWAYS_APPROVED


def _validate_operations(operations: list[str]) -> list[str]:
    unknown = sorted(set(operations) - POLICY_OPERATIONS)
    if unknown:
        raise ValidationFailed(f"Unknown operation(s): {', '.join(unknown)}.")
    return sorted(set(operations))


async def set_policy(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    connection_id: uuid.UUID | None,
    environment: str | None,
    operations: list[str],
) -> ApprovalPolicy | None:
    """Replace a policy. An empty list removes it (returns ``None``)."""
    if (connection_id is None) == (environment is None):
        raise ValidationFailed("Give either a connection or an environment.")
    if environment is not None and environment not in ENVIRONMENTS:
        raise ValidationFailed(f"environment must be one of {', '.join(ENVIRONMENTS)}.")
    ops = _validate_operations(operations)
    if connection_id is not None and await db.get(Connection, connection_id) is None:
        raise NotFound("Connection not found.")
    stmt = select(ApprovalPolicy)
    stmt = (
        stmt.where(ApprovalPolicy.connection_id == connection_id)
        if connection_id is not None
        else stmt.where(ApprovalPolicy.environment == environment)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if not ops:
        if row is not None:
            await db.delete(row)
        return None
    if row is None:
        row = ApprovalPolicy(connection_id=connection_id, environment=environment)
        db.add(row)
    row.operations, row.updated_by, row.updated_at = ops, ctx.user.id, utcnow()
    await db.flush()
    return row


async def list_policies(db: AsyncSession) -> list[ApprovalPolicy]:
    return list((await db.execute(select(ApprovalPolicy))).scalars())


# ---------------------------------------------------------------------------- gate


async def gate(  # noqa: PLR0913
    db: AsyncSession,
    ctx: AuthContext,
    access: Access,
    operation: str,
    params: dict[str, Any],
    approval_id: uuid.UUID | None,
    *,
    summary: dict[str, Any] | None = None,
) -> None:
    """Let the operation proceed, or demand / verify an approval for it."""
    conn = access.connection
    if operation not in await required_operations(db, conn):
        return
    digest = params_digest(operation, conn.id, params)
    if approval_id is None:
        await _request(ctx, conn, operation, digest, summary or params)
    await _consume(db, ctx, conn, operation, digest, approval_id)


async def _request(
    ctx: AuthContext, conn: Connection, operation: str, digest: str, summary: dict[str, Any]
) -> NoReturn:
    approval = Approval(
        id=uuid.uuid4(),
        connection_id=conn.id,
        requester_id=ctx.user.id,
        operation=operation,
        params_digest=digest,
        summary=summary,
        status="pending",
        expires_at=utcnow() + APPROVAL_TTL,
    )
    # Own transaction: the caller's is rolled back when we raise.
    async with session_factory()() as session, session.begin():
        session.add(approval)
    await audit.record(
        "approval.request",
        actor=ctx.actor,
        connection_id=conn.id,
        target={"approval_id": str(approval.id), "operation": operation},
        detail={"summary": summary},
    )
    raise ApprovalRequired(
        f"{operation} needs approval from a second person. Approval {approval.id} was requested; "
        "repeat the request with the Approval-Id header once it is approved.",
        extra={"approval_id": str(approval.id)},
    )


async def _consume(
    db: AsyncSession,
    ctx: AuthContext,
    conn: Connection,
    operation: str,
    digest: str,
    approval_id: uuid.UUID,
) -> None:
    now = utcnow()
    claimed = (
        await db.execute(
            update(Approval)
            .where(
                Approval.id == approval_id,
                Approval.status == "approved",
                Approval.requester_id == ctx.user.id,
                Approval.connection_id == conn.id,
                Approval.operation == operation,
                Approval.params_digest == digest,
                Approval.expires_at > now,
            )
            .values(status="executed", executed_at=now)
            .returning(Approval.id)
        )
    ).first()
    if claimed is not None:
        await audit.record(
            "approval.consume",
            actor=ctx.actor,
            connection_id=conn.id,
            target={"approval_id": str(approval_id), "operation": operation},
        )
        return
    # Explain why, without revealing other users' approvals.
    row = await db.get(Approval, approval_id)
    if row is None or row.requester_id != ctx.user.id or row.connection_id != conn.id:
        raise NotFound("Approval not found.")
    if row.operation != operation or row.params_digest != digest:
        raise ValidationFailed("This approval was granted for a different request.")
    if row.status == "pending":
        raise Conflict("This approval has not been decided yet.")
    if row.status == "rejected":
        raise Forbidden("This approval was rejected.")
    if row.status == "executed":
        raise Conflict("This approval was already used.")
    if row.status == "cancelled":
        raise Conflict("This approval was cancelled.")
    raise Conflict("This approval has expired.")


# ---------------------------------------------------------------------------- decisions


async def _load_for_decision(
    db: AsyncSession, ctx: AuthContext, approval_id: uuid.UUID
) -> tuple[Approval, Access]:
    row = await db.get(Approval, approval_id)
    if row is None:
        raise NotFound("Approval not found.")
    conn = await db.get(Connection, row.connection_id)
    assert conn is not None
    access = await resolve_access(db, ctx, conn)
    if Perm.GOVERNANCE_APPROVE.value not in access.permissions:
        raise NotFound("Approval not found.")  # no existence oracle for non-approvers
    return row, access


async def decide(
    db: AsyncSession, ctx: AuthContext, approval_id: uuid.UUID, *, approve: bool, reason: str | None
) -> Approval:
    row, _ = await _load_for_decision(db, ctx, approval_id)
    if row.requester_id == ctx.user.id:
        raise Forbidden("You cannot decide your own request (four-eyes principle).")
    if row.status != "pending":
        raise Conflict(f"This approval is already {row.status}.")
    if row.expires_at <= utcnow():
        raise Conflict("This approval has expired.")
    row.status = "approved" if approve else "rejected"
    row.decided_by, row.decided_at, row.reason = ctx.user.id, utcnow(), reason
    await db.flush()
    await audit.record(
        "approval.approve" if approve else "approval.reject",
        actor=ctx.actor,
        connection_id=row.connection_id,
        target={"approval_id": str(row.id), "operation": row.operation},
        detail={"requester_id": str(row.requester_id), "reason": reason},
    )
    return row


async def cancel(db: AsyncSession, ctx: AuthContext, approval_id: uuid.UUID) -> Approval:
    row = await db.get(Approval, approval_id)
    if row is None or row.requester_id != ctx.user.id:
        raise NotFound("Approval not found.")
    if row.status not in ("pending", "approved"):
        raise Conflict(f"This approval is already {row.status}.")
    row.status = "cancelled"
    await db.flush()
    await audit.record(
        "approval.cancel",
        actor=ctx.actor,
        connection_id=row.connection_id,
        target={"approval_id": str(row.id), "operation": row.operation},
    )
    return row


async def list_visible(
    db: AsyncSession, ctx: AuthContext, *, scope: str, status: str | None, limit: int
) -> list[Approval]:
    """``mine``: requested by the caller. ``review``: pending on connections the caller can
    approve on (excluding their own requests)."""
    stmt = select(Approval).order_by(Approval.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Approval.status == status)
    if scope == "mine":
        return list((await db.execute(stmt.where(Approval.requester_id == ctx.user.id))).scalars())
    rows = (await db.execute(stmt.where(Approval.requester_id != ctx.user.id))).scalars()
    out: list[Approval] = []
    cache: dict[uuid.UUID, bool] = {}
    for a in rows:
        if a.connection_id not in cache:
            conn = await db.get(Connection, a.connection_id)
            assert conn is not None
            cache[a.connection_id] = (
                Perm.GOVERNANCE_APPROVE.value in (await resolve_access(db, ctx, conn)).permissions
            )
        if cache[a.connection_id]:
            out.append(a)
    return out
