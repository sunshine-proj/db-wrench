"""Explorer / data-browser orchestration: access check -> adapter call -> audit."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.deps import AuthContext
from app.auth.permissions import Perm
from app.config import Settings
from app.connections import service as conns
from app.connections.scope import ObjectScope, filter_listing
from app.database.document.base import DocumentAdapter
from app.database.relational.base import RelationalAdapter
from app.database.relational.models import FilterSpec, RowQuery, SortSpec
from app.errors import AppError, Forbidden, ValidationFailed


async def relational(  # noqa: PLR0913
    db: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    cid: uuid.UUID,
    perm: Perm,
    scope: ObjectScope | None = None,
) -> RelationalAdapter:
    return (await relational_access(db, ctx, settings, cid, perm, scope))[1]


async def relational_access(  # noqa: PLR0913
    db: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    cid: uuid.UUID,
    perm: Perm,
    scope: ObjectScope | None = None,
) -> tuple[conns.Access, RelationalAdapter]:
    access, adapter = await conns.open_adapter(db, ctx, settings, cid, perm, "relational", scope)
    return access, conns.as_relational(adapter)


async def document(  # noqa: PLR0913
    db: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    cid: uuid.UUID,
    perm: Perm,
    scope: ObjectScope | None = None,
) -> DocumentAdapter:
    return (await document_access(db, ctx, settings, cid, perm, scope))[1]


async def document_access(  # noqa: PLR0913
    db: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    cid: uuid.UUID,
    perm: Perm,
    scope: ObjectScope | None = None,
) -> tuple[conns.Access, DocumentAdapter]:
    access, adapter = await conns.open_adapter(db, ctx, settings, cid, perm, "document", scope)
    return access, conns.as_document(adapter)


def apply_listing[T](  # noqa: PLR0913
    access: conns.Access,
    items: list[T],
    name: Callable[[T], str],
    level: str,
    *,
    database: str | None = None,
    schema: str | None = None,
) -> list[T]:
    """Connection-level metadata viewers see everything; users who only hold scoped grants see
    exactly what their grants cover."""
    if Perm.METADATA_VIEW.value in access.connection_level:
        return items
    if not access.scoped:
        raise Forbidden("Missing permission: metadata.view")
    return filter_listing(access.scoped, items, name, level, database=database, schema=schema)


@asynccontextmanager
async def audited(
    ctx: AuthContext, action: str, cid: uuid.UUID, target: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    """Audit a data access. Failures are recorded too; row contents never are."""
    detail: dict[str, Any] = {}
    try:
        yield detail
    except AppError as exc:
        await audit.record(
            action,
            actor=ctx.actor,
            connection_id=cid,
            target=target,
            result="failure",
            detail={"error": exc.code},
        )
        raise
    await audit.record(action, actor=ctx.actor, connection_id=cid, target=target, detail=detail)


# ------------------------------------------------------------------ query parameter parsing


class _SortIn(BaseModel):
    column: str = Field(max_length=255)
    direction: str = "asc"


class _FilterIn(BaseModel):
    column: str = Field(max_length=255)
    op: str = Field(max_length=20)
    value: Any = None


def _json_param(raw: str | None, adapter: TypeAdapter[Any], what: str) -> Any:
    if not raw:
        return None
    if len(raw) > 64 * 1024:
        raise ValidationFailed(f"{what} parameter is too large.")
    try:
        return adapter.validate_python(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValidationFailed(f"{what} must be valid JSON of the documented shape.") from exc


def parse_row_query(
    *,
    limit: int,
    cursor: str | None,
    sort: str | None,
    filters: str | None,
    columns: str | None,
    search: str | None,
    search_columns: str | None,
) -> RowQuery:
    sorts = _json_param(sort, TypeAdapter(list[_SortIn]), "sort") or []
    flts = _json_param(filters, TypeAdapter(list[_FilterIn]), "filters") or []
    cols = _json_param(columns, TypeAdapter(list[str]), "columns")
    scols = _json_param(search_columns, TypeAdapter(list[str]), "search_columns") or []
    return RowQuery(
        columns=cols,
        sort=[SortSpec(s.column, s.direction) for s in sorts],
        filters=[FilterSpec(f.column, f.op, f.value) for f in flts],
        search=search or None,
        search_columns=scols,
        limit=limit,
        cursor=cursor,
    )


def filter_summary(q: RowQuery) -> list[dict[str, str]]:
    """Audit-safe description of the filters: columns and operators only, no values."""
    return [{"column": f.column, "op": f.op} for f in q.filters]
