"""Audit access, application settings and health endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

from app.api.schemas import NetworkPolicyIn, NetworkPolicyOut, Strict
from app.audit import export as audit_export
from app.audit import service as audit
from app.audit import sink
from app.auth.deps import AppSettings, AuthContext, DbSession, require_app_permission
from app.auth.permissions import Perm
from app.config import get_settings
from app.connections import network
from app.connections import service as conns
from app.db import get_engine
from app.errors import NetworkPolicyViolation, ValidationFailed
from app.models import AppSetting, AuditEvent, utcnow
from app.services.runtime import run_blocking

router = APIRouter(tags=["administration"])
health = APIRouter(tags=["health"])

CanViewAudit = Annotated[AuthContext, Depends(require_app_permission(Perm.AUDIT_VIEW))]
CanManageSettings = Annotated[
    AuthContext, Depends(require_app_permission(Perm.APP_MANAGE_SETTINGS))
]


# -------------------------------------------------------------------------------- audit


@router.get("/audit/events")
async def list_audit_events(
    db: DbSession,
    ctx: CanViewAudit,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    action: Annotated[str | None, Query(max_length=80)] = None,
    actor: Annotated[str | None, Query(max_length=100)] = None,
    connection_id: uuid.UUID | None = None,
    result: Annotated[str | None, Query(pattern="^(success|failure|denied)$")] = None,
    since: datetime | None = None,
) -> dict[str, Any]:
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit + 1)
    if before_id:
        stmt = stmt.where(AuditEvent.id < before_id)
    if action:
        stmt = stmt.where(AuditEvent.action.like(action.replace("%", "") + "%"))
    if actor:
        stmt = stmt.where(AuditEvent.actor_username == actor.lower())
    if connection_id:
        stmt = stmt.where(AuditEvent.connection_id == connection_id)
    if result:
        stmt = stmt.where(AuditEvent.result == result)
    if since:
        stmt = stmt.where(AuditEvent.ts >= since)
    rows = list((await db.execute(stmt)).scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "events": [audit.serialize(r) for r in rows],
        "next_before_id": rows[-1].id if has_more and rows else None,
    }


CanExportAudit = Annotated[AuthContext, Depends(require_app_permission(Perm.AUDIT_EXPORT))]


@router.get("/audit/export")
async def export_audit(
    ctx: CanExportAudit,
    settings: AppSettings,
    format: Annotated[Literal["csv", "json", "ndjson"], Query()] = "ndjson",
    since: datetime | None = None,
    until: datetime | None = None,
    action: Annotated[str | None, Query(max_length=80)] = None,
    actor: Annotated[str | None, Query(max_length=100)] = None,
    connection_id: uuid.UUID | None = None,
    result: Annotated[str | None, Query(pattern="^(success|failure|denied)$")] = None,
) -> StreamingResponse:
    conds = audit_export.filters(
        since=since, until=until, action=action, result=result,
        connection_id=connection_id, actor=actor,
    )  # fmt: skip
    state: dict[str, Any] = {}
    await audit.record(
        "audit.export",
        actor=ctx.actor,
        detail={
            "format": format, "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None, "action": action, "result": result,
            "connection_id": str(connection_id) if connection_id else None,
        },
    )  # fmt: skip
    return StreamingResponse(
        audit_export.stream(format, conds, settings.export_max_rows, state),
        media_type=audit_export.MEDIA[format],
        headers={"Content-Disposition": f'attachment; filename="audit.{format}"',
                 "Cache-Control": "no-store"},
    )  # fmt: skip


@router.get("/audit/verify")
async def verify_audit_chain(db: DbSession, ctx: CanViewAudit) -> dict[str, Any]:
    report = await audit.verify_chain(db)
    await audit.record(
        "audit.verify",
        actor=ctx.actor,
        result="success" if report.ok else "failure",
        detail={"checked": report.checked},
    )
    return {
        "ok": report.ok,
        "checked": report.checked,
        "first_bad_id": report.first_bad_id,
        "reason": report.reason,
    }


# ----------------------------------------------------------------------------- settings


def _policy_out(policy: network.NetworkPolicy) -> NetworkPolicyOut:
    return NetworkPolicyOut(
        allow_cidrs=list(policy.allow_cidrs),
        allow_hosts=list(policy.allow_hosts),
        allow_loopback=policy.allow_loopback,
        always_denied=[str(n) for n in network.ALWAYS_DENY],
    )


@router.get("/settings/network-policy", response_model=NetworkPolicyOut)
async def get_network_policy(
    db: DbSession, settings: AppSettings, _: CanManageSettings
) -> NetworkPolicyOut:
    return _policy_out(await conns.get_network_policy(db, settings))


@router.put("/settings/network-policy", response_model=NetworkPolicyOut)
async def put_network_policy(
    body: NetworkPolicyIn, db: DbSession, settings: AppSettings, ctx: CanManageSettings
) -> NetworkPolicyOut:
    await conns.set_network_policy(db, ctx.user.id, body.allow_cidrs, body.allow_hosts)
    await audit.record(
        "settings.network_policy.update",
        actor=ctx.actor,
        detail={"allow_cidrs": body.allow_cidrs, "allow_hosts": body.allow_hosts},
    )
    await db.flush()
    return _policy_out(await conns.get_network_policy(db, settings))


@router.post("/settings/encryption/rotate")
async def rotate_encryption_keys(
    db: DbSession, settings: AppSettings, ctx: CanManageSettings
) -> dict[str, Any]:
    """Re-wrap stored data keys under the active KEK (set DBW_ACTIVE_KEK_ID, then call)."""
    counts = await conns.rotate_keys(db, settings)
    await audit.record(
        "settings.encryption.rotate",
        actor=ctx.actor,
        detail={"active_kek": settings.active_kek_id, **counts},
    )
    return {"active_kek": settings.active_kek_id, **counts}


# ------------------------------------------------------------------------------- health

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def expected_revision() -> str | None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    if not _ALEMBIC_INI.exists():
        return None
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_INI.parent / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


@health.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@health.get("/health/ready")
async def ready(response: Response) -> dict[str, Any]:
    checks: dict[str, str] = {}
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
            current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            checks["schema"] = "ok" if current == expected_revision() else "outdated"
    except Exception:
        checks.setdefault("database", "unavailable")
    ok = all(v == "ok" for v in checks.values()) and len(checks) == 2
    if not ok:
        response.status_code = 503
    return {"status": "ready" if ok else "not_ready", "checks": checks}


@health.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    if not get_settings().metrics_enabled:
        return Response(status_code=404)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ------------------------------------------------------------------------- SIEM forwarding


class AuditSinkIn(Strict):
    enabled: bool
    protocol: Literal["udp", "tcp", "tls"] = "tcp"
    host: str = Field(default="", max_length=255)
    port: int = Field(default=514, ge=1, le=65535)
    format: Literal["rfc5424", "json"] = "rfc5424"
    facility: int = Field(default=13, ge=0, le=23)
    backfill: bool = Field(
        default=False, description="Also forward events recorded before the sink was enabled."
    )


class AuditSinkOut(BaseModel):
    enabled: bool
    protocol: str
    host: str
    port: int
    format: str
    facility: int
    last_forwarded_id: int


def _sink_out(cfg: sink.SinkConfig) -> AuditSinkOut:
    return AuditSinkOut(
        enabled=cfg.enabled, protocol=cfg.protocol, host=cfg.host, port=cfg.port,
        format=cfg.format, facility=cfg.facility, last_forwarded_id=cfg.last_id,
    )  # fmt: skip


@router.get("/settings/audit-sink", response_model=AuditSinkOut)
async def get_audit_sink(db: DbSession, _: CanManageSettings) -> AuditSinkOut:
    return _sink_out(await sink.load_config(db))


@router.put("/settings/audit-sink", response_model=AuditSinkOut)
async def put_audit_sink(
    body: AuditSinkIn, db: DbSession, settings: AppSettings, ctx: CanManageSettings
) -> AuditSinkOut:
    values = body.model_dump(exclude={"backfill"})
    if body.enabled:
        sink.validate(values)
        policy = await conns.get_network_policy(db, settings)
        await conns.check_destination(policy, body.host, body.port)
    previous = await sink.load_config(db)
    last_id = previous.last_id
    if body.enabled and not previous.enabled and not body.backfill:
        newest = (await db.execute(select(func.max(AuditEvent.id)))).scalar()
        last_id = newest or 0
    if body.backfill and not previous.enabled:
        last_id = 0
    cfg = sink.SinkConfig(**values, last_id=last_id)
    row = await db.get(AppSetting, sink.SINK_KEY)
    if row is None:
        db.add(AppSetting(key=sink.SINK_KEY, value=cfg.to_value(), updated_by=ctx.user.id))
    else:
        row.value, row.updated_at, row.updated_by = cfg.to_value(), utcnow(), ctx.user.id
    await audit.record(
        "settings.audit_sink.update",
        actor=ctx.actor,
        detail={k: v for k, v in values.items()},
    )
    return _sink_out(cfg)


@router.post("/settings/audit-sink/test")
async def test_audit_sink(
    db: DbSession, settings: AppSettings, ctx: CanManageSettings
) -> dict[str, Any]:
    cfg = await sink.load_config(db)
    if not cfg.host:
        raise ValidationFailed("Configure a destination first.")
    policy = await conns.get_network_policy(db, settings)
    event = {
        "id": 0, "ts": utcnow().isoformat(), "actor_user_id": None,
        "actor_username": ctx.user.username, "ip": ctx.ip, "request_id": None,
        "action": "audit.sink.test", "connection_id": None, "target": {}, "result": "success",
        "detail": {}, "hash": "0" * 64,
    }  # fmt: skip
    msg = sink.format_message(cfg, event, "db-wrench")
    try:
        await run_blocking(sink.send_blocking, cfg, policy, [msg])
    except NetworkPolicyViolation:
        raise
    except Exception as exc:
        await audit.record("audit.sink.test", actor=ctx.actor, result="failure")
        return {"ok": False, "error": type(exc).__name__}
    await audit.record("audit.sink.test", actor=ctx.actor)
    return {"ok": True, "error": None}
