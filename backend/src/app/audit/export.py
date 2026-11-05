"""Streamed audit export (CSV / JSON / NDJSON).

Each record carries ``prev_hash`` and ``hash`` plus the fields the hash covers, so an export can
be verified offline (see docs/operations.md). CSV is formula-injection safe.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.audit.service import serialize
from app.db import session_factory
from app.editor.export import csv_safe
from app.models import AuditEvent

COLUMNS = [
    "id", "ts", "actor_user_id", "actor_username", "ip", "request_id", "action",
    "connection_id", "target", "result", "detail", "prev_hash", "hash",
]  # fmt: skip
MEDIA = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
    "ndjson": "application/x-ndjson",
}
PAGE = 1000


def filters(
    *,
    since: datetime | None,
    until: datetime | None,
    action: str | None,
    result: str | None,
    connection_id: Any,
    actor: str | None,
) -> list[Any]:
    conds: list[Any] = []
    if since:
        conds.append(AuditEvent.ts >= since)
    if until:
        conds.append(AuditEvent.ts < until)
    if action:
        conds.append(AuditEvent.action.like(action.replace("%", "") + "%"))
    if result:
        conds.append(AuditEvent.result == result)
    if connection_id:
        conds.append(AuditEvent.connection_id == connection_id)
    if actor:
        conds.append(AuditEvent.actor_username == actor.lower())
    return conds


def _record(e: AuditEvent) -> dict[str, Any]:
    d = serialize(e)
    d["prev_hash"] = e.prev_hash
    # the exact form the chain hash covers, so an export verifies offline
    d["ts"] = e.ts.astimezone(UTC).isoformat(timespec="microseconds")
    return {k: d[k] for k in COLUMNS}


async def stream(
    fmt: str, conds: list[Any], max_rows: int, state: dict[str, Any]
) -> AsyncIterator[bytes]:
    """Own session (the request's is closed by the time a stream is consumed)."""
    sent, last = 0, 0
    first = True
    if fmt == "json":
        yield b"["
    elif fmt == "csv":
        buf = io.StringIO()
        csv.writer(buf).writerow(COLUMNS)
        yield buf.getvalue().encode()
    async with session_factory()() as db:
        while sent < max_rows:
            rows = list(
                (
                    await db.execute(
                        select(AuditEvent)
                        .where(AuditEvent.id > last, *conds)
                        .order_by(AuditEvent.id)
                        .limit(min(PAGE, max_rows - sent))
                    )
                ).scalars()
            )
            if not rows:
                break
            out: list[str] = []
            for e in rows:
                rec = _record(e)
                if fmt == "ndjson":
                    out.append(json.dumps(rec, default=str) + "\n")
                elif fmt == "json":
                    out.append(("" if first else ",") + json.dumps(rec, default=str))
                else:
                    buf = io.StringIO()
                    csv.writer(buf).writerow(
                        [
                            csv_safe(v) if isinstance(v, str) else json.dumps(v, default=str)
                            if isinstance(v, dict) else ("" if v is None else v)
                            for v in rec.values()
                        ]
                    )  # fmt: skip
                    out.append(buf.getvalue())
                first = False
                last = e.id
            sent += len(rows)
            yield "".join(out).encode()
        else:
            state["truncated"] = True
    state["rows"] = sent
    if fmt == "json":
        yield b"]"
