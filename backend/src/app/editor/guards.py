"""Shared guards for data access and edits: sensitive-field masking and typed confirmation."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import Perm
from app.connections.service import Access
from app.errors import ConfirmationRequired, Forbidden
from app.models import SensitiveMarker


async def load_markers(
    db: AsyncSession, cid: uuid.UUID, database: str, schema: str | None, obj: str
) -> frozenset[str]:
    rows = (
        await db.execute(
            select(SensitiveMarker.field).where(
                SensitiveMarker.connection_id == cid,
                SensitiveMarker.database == database,
                SensitiveMarker.schema == (schema or ""),
                SensitiveMarker.object == obj,
            )
        )
    ).scalars()
    return frozenset(rows)


async def has_markers(db: AsyncSession, cid: uuid.UUID) -> bool:
    n = (
        await db.execute(
            select(func.count())
            .select_from(SensitiveMarker)
            .where(SensitiveMarker.connection_id == cid)
        )
    ).scalar_one()
    return n > 0


def can_read_sensitive(access: Access) -> bool:
    return Perm.DATA_READ_SENSITIVE.value in access.permissions


async def masked_fields(
    db: AsyncSession, access: Access, database: str, schema: str | None, obj: str
) -> frozenset[str]:
    """Fields to hide from this user (empty when they may read sensitive data)."""
    if can_read_sensitive(access):
        return frozenset()
    return await load_markers(db, access.connection.id, database, schema, obj)


def reject_masked(touched: Iterable[str], masked: frozenset[str], action: str) -> None:
    """Filtering, sorting, searching or writing a masked field would leak or corrupt it."""
    hit = sorted({t for t in touched if t in masked})
    if hit:
        raise Forbidden(
            f"You cannot {action} sensitive field(s): {', '.join(hit)}. "
            "The data.read_sensitive permission is required."
        )


def require_all(access: Access, perms: Iterable[Perm]) -> None:
    for p in perms:
        if p.value not in access.permissions:
            raise Forbidden(f"Missing permission: {p.value}")


def require_confirmation(access: Access, confirm: str | None, name: str) -> None:
    """Production objects need the object's name typed back for destructive operations."""
    if access.connection.environment == "production" and confirm != name:
        raise ConfirmationRequired(
            f"This is a production connection. Type the object name {name!r} to confirm."
        )
