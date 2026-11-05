"""Audit forwarding to syslog / SIEM (RFC 5424 over UDP, TCP or TLS).

The database is the source of truth. A background task (one active replica at a time, chosen
by an advisory lock) reads events after the last forwarded id and sends them, so a SIEM outage
only delays delivery (at-least-once; the event id and chain hash are in every message so the
receiver can de-duplicate). The destination is checked against the outbound network policy on
every delivery, and TLS destinations are verified against the system trust store.
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text

from app.audit.service import serialize
from app.config import Settings
from app.connections.network import NetworkPolicy
from app.db import session_factory
from app.errors import ValidationFailed
from app.models import AppSetting, AuditEvent, utcnow
from app.observability import AUDIT_FORWARD_ERRORS, AUDIT_FORWARDED, logger
from app.services.runtime import run_blocking

SINK_KEY = "audit_sink"
_LOCK_KEY = 0x44425349454D  # "DBSIEM"
BATCH = 500
SEND_TIMEOUT_S = 5.0
PROTOCOLS = ("udp", "tcp", "tls")
FORMATS = ("rfc5424", "json")
_SEVERITY = {"success": 6, "failure": 4, "denied": 5}  # info / warning / notice


@dataclass(frozen=True)
class SinkConfig:
    enabled: bool = False
    protocol: str = "tcp"
    host: str = ""
    port: int = 514
    format: str = "rfc5424"
    facility: int = 13  # log audit
    last_id: int = 0

    @classmethod
    def from_value(cls, v: dict[str, Any]) -> SinkConfig:
        known = {k: v[k] for k in cls.__dataclass_fields__ if k in v}
        return cls(**known)

    def to_value(self) -> dict[str, Any]:
        return dict(self.__dict__)


def validate(cfg: dict[str, Any]) -> None:
    if cfg["protocol"] not in PROTOCOLS:
        raise ValidationFailed(f"protocol must be one of {', '.join(PROTOCOLS)}.")
    if cfg["format"] not in FORMATS:
        raise ValidationFailed(f"format must be one of {', '.join(FORMATS)}.")
    if not 0 <= cfg["facility"] <= 23:
        raise ValidationFailed("facility must be between 0 and 23.")
    if not 1 <= cfg["port"] <= 65535:
        raise ValidationFailed("port must be between 1 and 65535.")
    host = cfg["host"]
    if not host or len(host) > 255 or any(c in host for c in " /\\\x00"):
        raise ValidationFailed("host is invalid.")


def _sd_escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")


def format_message(cfg: SinkConfig, e: dict[str, Any], hostname: str) -> bytes:
    payload = json.dumps(e, separators=(",", ":"), sort_keys=True, default=str)
    if cfg.format == "json":
        return payload.encode()
    pri = cfg.facility * 8 + _SEVERITY.get(e["result"], 6)
    sd = (
        f'[dbw@32473 id="{e["id"]}" action="{_sd_escape(e["action"])}" '
        f'result="{_sd_escape(e["result"])}" actor="{_sd_escape(e["actor_username"] or "-")}" '
        f'hash="{e["hash"]}"]'
    )
    ts = e["ts"]
    request = _sd_escape(e["request_id"] or "-")
    return f"<{pri}>1 {ts} {hostname} db-wrench - {request} {sd} {payload}".encode()


def _frame(cfg: SinkConfig, msg: bytes) -> bytes:
    # RFC 6587 octet counting avoids newline ambiguity inside JSON.
    return msg if cfg.protocol == "udp" else str(len(msg)).encode() + b" " + msg


def send_blocking(cfg: SinkConfig, policy: NetworkPolicy, messages: list[bytes]) -> int:
    """Deliver messages; returns how many were sent before any failure (raises after)."""
    addr = policy.resolve(cfg.host, cfg.port)[0]  # validated and pinned
    sent = 0
    if cfg.protocol == "udp":
        with socket.socket(
            socket.AF_INET6 if ":" in addr else socket.AF_INET, socket.SOCK_DGRAM
        ) as s:
            s.settimeout(SEND_TIMEOUT_S)
            for m in messages:
                s.sendto(m, (addr, cfg.port))
                sent += 1
        return sent
    raw = socket.create_connection((addr, cfg.port), timeout=SEND_TIMEOUT_S)
    try:
        conn: socket.socket = raw
        if cfg.protocol == "tls":
            ctx = ssl.create_default_context()
            conn = ctx.wrap_socket(raw, server_hostname=cfg.host)
        for m in messages:
            conn.sendall(_frame(cfg, m))
            sent += 1
    finally:
        raw.close()
    return sent


async def load_config(db: Any) -> SinkConfig:
    row = await db.get(AppSetting, SINK_KEY)
    return SinkConfig.from_value(row.value) if row else SinkConfig()


async def forward_once(settings: Settings, policy: NetworkPolicy) -> int:
    """One delivery pass. Returns the number of events sent."""
    hostname = socket.gethostname() or "db-wrench"
    async with session_factory()() as db, db.begin():
        got = (
            await db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
        ).scalar()
        if not got:  # another replica is forwarding
            return 0
        row = await db.get(AppSetting, SINK_KEY)
        if row is None:
            return 0
        cfg = SinkConfig.from_value(row.value)
        if not cfg.enabled or not cfg.host:
            return 0
        events = list(
            (
                await db.execute(
                    select(AuditEvent)
                    .where(AuditEvent.id > cfg.last_id)
                    .order_by(AuditEvent.id)
                    .limit(BATCH)
                )
            ).scalars()
        )
        if not events:
            return 0
        messages = [format_message(cfg, serialize(e), hostname) for e in events]
        sent = 0
        try:
            sent = await run_blocking(send_blocking, cfg, policy, messages)
        except Exception as exc:
            AUDIT_FORWARD_ERRORS.inc()
            logger.warning("audit_forward_failed", extra={"fields": {"error": type(exc).__name__}})
            # a partial send is unknowable for UDP/TCP: resend the batch (at-least-once)
            return 0
        AUDIT_FORWARDED.inc(sent)
        row.value = {**row.value, "last_id": events[-1].id}
        row.updated_at = utcnow()
        return sent


async def forward_loop(settings: Settings) -> None:
    from app.connections.service import get_network_policy

    while True:
        try:
            await asyncio.sleep(settings.audit_forward_interval_s)
            async with session_factory()() as db:
                policy = await get_network_policy(db, settings)
            while await forward_once(settings, policy) >= BATCH:
                pass  # drain a backlog without waiting
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("audit_forward_loop_error")
