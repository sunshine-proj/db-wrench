"""Append-only, hash-chained audit log.

Each record's hash covers its content and the previous record's hash, so any later
modification, deletion or reordering is detectable with :func:`verify_chain`. Writes use
their own transaction (so failures of the audited operation are still recorded) and are
serialised with a Postgres advisory lock to keep the chain linear across replicas.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_factory
from app.models import AuditEvent, utcnow
from app.observability import get_request_id, logger

GENESIS = "0" * 64
_LOCK_KEY = 0x44425752454E4348  # "DBWRENCH"


def compute_hash(prev_hash: str, record: dict[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


def _record_body(e: AuditEvent) -> dict[str, Any]:
    return {
        "ts": e.ts.astimezone(UTC).isoformat(timespec="microseconds"),
        "actor_user_id": str(e.actor_user_id) if e.actor_user_id else None,
        "actor_username": e.actor_username,
        "ip": e.ip,
        "request_id": e.request_id,
        "action": e.action,
        "connection_id": str(e.connection_id) if e.connection_id else None,
        "target": e.target,
        "result": e.result,
        "detail": e.detail,
    }


@dataclass
class Actor:
    user_id: uuid.UUID | None = None
    username: str | None = None
    ip: str | None = None
    token_id: uuid.UUID | None = None  # set when the request used an API token


async def record(
    action: str,
    *,
    actor: Actor | None = None,
    result: str = "success",
    connection_id: uuid.UUID | None = None,
    target: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Persist an audit event. Never raises into the request path."""
    actor = actor or Actor()
    if actor.token_id is not None:  # who acted through which token (PRD 5.10)
        detail = {**(detail or {}), "api_token_id": str(actor.token_id)}
    try:
        async with session_factory()() as session, session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
            prev = (
                await session.execute(
                    select(AuditEvent.hash).order_by(AuditEvent.id.desc()).limit(1)
                )
            ).scalar_one_or_none() or GENESIS
            event = AuditEvent(
                ts=utcnow(),
                actor_user_id=actor.user_id,
                actor_username=actor.username,
                ip=actor.ip,
                request_id=get_request_id(),
                action=action,
                connection_id=connection_id,
                target=target or {},
                result=result,
                detail=detail or {},
                prev_hash=prev,
                hash="",
            )
            event.hash = compute_hash(prev, _record_body(event))
            session.add(event)
    except Exception:
        logger.exception("audit_write_failed", extra={"fields": {"action": action}})
        raise


@dataclass
class ChainReport:
    ok: bool
    checked: int
    first_bad_id: int | None = None
    reason: str | None = None


async def verify_chain(session: AsyncSession, batch: int = 1000) -> ChainReport:
    prev = GENESIS
    checked = 0
    last_id = 0
    while True:
        rows = (
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.id > last_id)
                    .order_by(AuditEvent.id)
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return ChainReport(True, checked)
        for e in rows:
            if e.prev_hash != prev:
                return ChainReport(False, checked, e.id, "chain link broken (record removed?)")
            if compute_hash(e.prev_hash, _record_body(e)) != e.hash:
                return ChainReport(False, checked, e.id, "record content modified")
            prev = e.hash
            last_id = e.id
            checked += 1


def serialize(e: AuditEvent) -> dict[str, Any]:
    ts: datetime = e.ts
    return {
        "id": e.id,
        "ts": ts.isoformat(),
        "actor_user_id": str(e.actor_user_id) if e.actor_user_id else None,
        "actor_username": e.actor_username,
        "ip": e.ip,
        "request_id": e.request_id,
        "action": e.action,
        "connection_id": str(e.connection_id) if e.connection_id else None,
        "target": e.target,
        "result": e.result,
        "detail": e.detail,
        "hash": e.hash,
    }
