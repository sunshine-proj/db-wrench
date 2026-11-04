"""Query workspace orchestration: policy, operation registry, history, saved queries.

Operations are rows in the application database (not process memory), so any replica can
cancel a query another replica is running: the row holds the *backend reference* (PostgreSQL
pid, MySQL thread id, MongoDB comment) and the cancel path opens a fresh connection to the
managed database to act on it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AuthContext
from app.auth.permissions import Perm
from app.config import Settings
from app.connections.service import Access
from app.db import session_factory
from app.editor.guards import can_read_sensitive, has_markers, require_confirmation
from app.errors import (
    ConfirmationRequired,
    Conflict,
    Forbidden,
    NotFound,
    RateLimited,
    StatementNotAllowed,
    ValidationFailed,
)
from app.models import AppSetting, Operation, QueryHistory, utcnow
from app.observability import logger
from app.query.classify import Statement, classify, redact_literals

HISTORY_KEY = "query_history"
DEFAULT_RETENTION_DAYS = 90
MAX_SQL_CHARS = 200_000


# ------------------------------------------------------------------------------ policy


@dataclass
class Plan:
    statements: list[Statement]
    write: bool
    script: bool
    destructive: bool

    @property
    def texts(self) -> list[str]:
        return [s.text for s in self.statements]


def plan_sql(
    access: Access, sql: str, *, confirm_destructive: bool, confirm: str | None, rollback: bool
) -> Plan:
    """Decide what a statement needs. Read-only enforcement itself stays in the database."""
    if len(sql) > MAX_SQL_CHARS:
        raise ValidationFailed(f"The statement is longer than {MAX_SQL_CHARS:,} characters.")
    stmts = classify(sql)
    if not stmts:
        raise ValidationFailed("Enter a statement to run.")
    bad = next((s for s in stmts if s.kind == "session"), None)
    if bad:
        raise StatementNotAllowed(
            f"{bad.verb} statements are not allowed. Transaction and session settings are "
            "controlled by the workspace (use the database/schema selectors and the "
            "rollback option)."
        )
    perms = access.permissions
    script = len(stmts) > 1
    if script and Perm.QUERY_EXECUTE_SCRIPT.value not in perms:
        raise Forbidden("Running several statements at once needs query.execute_script.")
    write = any(s.kind != "read" for s in stmts)
    if write:
        if access.connection.read_only:
            raise Forbidden("This connection is read-only; only read statements can be run on it.")
        if Perm.QUERY_EXECUTE_WRITE.value not in perms:
            raise Forbidden("Missing permission: query.execute_write")
    elif Perm.QUERY_EXECUTE_READ.value not in perms:
        raise Forbidden("Missing permission: query.execute_read")
    if rollback and not write:
        raise ValidationFailed("rollback only applies to statements that write.")
    destructive = any(s.destructive for s in stmts)
    if destructive and not confirm_destructive:
        raise ConfirmationRequired(
            "This statement looks destructive (DROP, TRUNCATE, or DELETE/UPDATE without WHERE). "
            "Confirm explicitly to run it."
        )
    if write and not rollback:
        require_confirmation(access, confirm, access.connection.name)
    return Plan(stmts, write, script, destructive)


async def check_sensitive_gate(db: AsyncSession, access: Access) -> None:
    """Free-form queries cannot be masked (aliases, expressions, joins), so connections that
    contain sensitive-field markers are closed to users without data.read_sensitive."""
    if not can_read_sensitive(access) and await has_markers(db, access.connection.id):
        raise Forbidden(
            "This connection contains sensitive fields, so free-form queries need the "
            "data.read_sensitive permission."
        )


def clamp_timeout(requested: int | None, settings: Settings) -> int:
    value = requested or settings.statement_timeout_s
    return max(1, min(value, settings.max_statement_timeout_s))


# ------------------------------------------------------------------------------ operations


async def begin_operation(
    ctx: AuthContext,
    connection_id: uuid.UUID,
    kind: str,
    database: str | None,
    settings: Settings,
    operation_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Register a running operation, enforcing the per-user concurrency limit."""
    horizon = utcnow() - timedelta(seconds=settings.max_statement_timeout_s + 120)
    async with session_factory()() as s, s.begin():
        await s.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"ops:{ctx.user.id}"}
        )
        # Rows left 'running' by a crashed replica are swept once past any possible deadline.
        await s.execute(
            update(Operation)
            .where(Operation.status == "running", Operation.started_at < horizon)
            .values(status="failed", finished_at=utcnow())
        )
        running = (
            await s.execute(
                select(func.count())
                .select_from(Operation)
                .where(Operation.user_id == ctx.user.id, Operation.status == "running")
            )
        ).scalar_one()
        if running >= settings.concurrent_queries_per_user:
            raise RateLimited(
                f"You already have {running} queries running (limit "
                f"{settings.concurrent_queries_per_user}). Wait or cancel one."
            )
        if operation_id is not None and await s.get(Operation, operation_id) is not None:
            raise Conflict("This operation id is already in use.")
        op = Operation(
            connection_id=connection_id, user_id=ctx.user.id, kind=kind, database=database
        )
        if operation_id is not None:
            op.id = operation_id  # chosen by the client so it can cancel while the query runs
        s.add(op)
        await s.flush()
        return op.id


async def attach_ref(op_id: uuid.UUID, ref: dict[str, Any]) -> None:
    async with session_factory()() as s, s.begin():
        await s.execute(update(Operation).where(Operation.id == op_id).values(backend_ref=ref))


def make_ref_callback(op_id: uuid.UUID, loop: asyncio.AbstractEventLoop) -> Any:
    """Callable for worker threads: stores the backend reference before the query starts."""

    def on_start(ref: dict[str, Any]) -> None:
        asyncio.run_coroutine_threadsafe(attach_ref(op_id, ref), loop).result(timeout=10)

    return on_start


async def finish_operation(op_id: uuid.UUID, status: str) -> str:
    """Close the operation unless it was already cancelled; returns the final status."""
    async with session_factory()() as s, s.begin():
        op = await s.get(Operation, op_id, with_for_update=True)
        if op is None:
            return status
        if op.status == "cancelled":
            return "cancelled"
        op.status, op.finished_at = status, utcnow()
        return status


async def was_cancelled(op_id: uuid.UUID) -> bool:
    async with session_factory()() as s:
        op = await s.get(Operation, op_id)
        return bool(op and op.status == "cancelled")


async def mark_cancelling(db: AsyncSession, op_id: uuid.UUID) -> Operation:
    op = await db.get(Operation, op_id, with_for_update=True)
    if op is None:
        raise NotFound("Operation not found.")
    return op


# ------------------------------------------------------------------------------ history


async def history_settings(db: AsyncSession) -> dict[str, Any]:
    row = await db.get(AppSetting, HISTORY_KEY)
    value = row.value if row else {}
    return {
        "retention_days": int(value.get("retention_days", DEFAULT_RETENTION_DAYS)),
        "store_literals": bool(value.get("store_literals", True)),
    }


async def set_history_settings(
    db: AsyncSession, user_id: uuid.UUID, retention_days: int, store_literals: bool
) -> None:
    if not 1 <= retention_days <= 3650:
        raise ValidationFailed("retention_days must be between 1 and 3650.")
    value = {"retention_days": retention_days, "store_literals": store_literals}
    row = await db.get(AppSetting, HISTORY_KEY)
    if row is None:
        db.add(AppSetting(key=HISTORY_KEY, value=value, updated_by=user_id))
    else:
        row.value, row.updated_at, row.updated_by = value, utcnow(), user_id


def redact_json(value: Any) -> Any:
    """Keep the shape of a MongoDB query, drop its values."""
    if isinstance(value, dict):
        return {k: redact_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_json(v) for v in value]
    return "?" if isinstance(value, str | int | float) and not isinstance(value, bool) else value


async def record_history(  # noqa: PLR0913
    db: AsyncSession,
    ctx: AuthContext,
    connection_id: uuid.UUID,
    *,
    kind: str,
    database: str | None,
    schema: str | None,
    statement: str,
    status: str,
    duration_ms: int,
    row_count: int | None,
    error_code: str | None = None,
) -> None:
    cfg = await history_settings(db)
    digest = hashlib.sha256(statement.encode()).hexdigest()
    stored, redacted = statement, False
    if not cfg["store_literals"]:
        redacted = True
        if kind == "sql":
            stored = redact_literals(statement)
        else:
            try:
                stored = json.dumps(redact_json(json.loads(statement)))
            except json.JSONDecodeError:
                stored = "?"
    db.add(
        QueryHistory(
            user_id=ctx.user.id,
            connection_id=connection_id,
            kind=kind,
            database=database,
            schema=schema,
            statement=stored[:MAX_SQL_CHARS],
            redacted=redacted,
            statement_digest=digest,
            status=status,
            error_code=error_code,
            duration_ms=duration_ms,
            row_count=row_count,
        )
    )
    await db.flush()


async def purge_history() -> dict[str, int]:
    """Delete history older than the configured retention and stale operation rows."""
    async with session_factory()() as s, s.begin():
        got = (
            await s.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": 0x4442485354})
        ).scalar()
        if not got:  # another replica is purging
            return {"history": 0, "operations": 0}
        cfg = await history_settings(s)
        cutoff = utcnow() - timedelta(days=cfg["retention_days"])
        h = await s.execute(delete(QueryHistory).where(QueryHistory.created_at < cutoff))
        o = await s.execute(
            delete(Operation).where(
                Operation.started_at < utcnow() - timedelta(days=7), Operation.status != "running"
            )
        )
        return {"history": h.rowcount or 0, "operations": o.rowcount or 0}  # type: ignore[attr-defined]


async def purge_loop(interval_s: int) -> None:
    while True:
        try:
            await asyncio.sleep(interval_s)
            counts = await purge_history()
            if any(counts.values()):
                logger.info("history_purged", extra={"fields": counts})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("history_purge_failed")


async def log_history(**kw: Any) -> None:
    """History is written in its own transaction so failed runs are recorded too."""
    ctx: AuthContext = kw.pop("ctx")
    connection_id: uuid.UUID = kw.pop("connection_id")
    async with session_factory()() as s, s.begin():
        await record_history(s, ctx, connection_id, **kw)


async def mark_cancelled(op_id: uuid.UUID) -> Operation | None:
    async with session_factory()() as s, s.begin():
        op = await s.get(Operation, op_id, with_for_update=True)
        if op is None or op.status != "running":
            return None
        op.status, op.finished_at = "cancelled", utcnow()
        await s.flush()
        s.expunge(op)
        return op
