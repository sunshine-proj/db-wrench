"""Structured logging, request IDs and Prometheus metrics."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

from prometheus_client import Counter, Histogram

_request_id: ContextVar[str] = ContextVar("request_id", default="-")
logger = logging.getLogger("dbwrench")

REQUESTS = Counter("dbw_http_requests_total", "HTTP requests", ["method", "status"])
LATENCY = Histogram("dbw_http_request_seconds", "HTTP request latency", ["method"])
DB_OPS = Counter(
    "dbw_managed_db_operations_total", "Managed database operations", ["engine", "op", "result"]
)

_SECRET_PATTERNS = [
    re.compile(r"(password|passwd|pwd|secret|token)=([^&\s]+)", re.I),
    re.compile(r"(://[^:/\s]+:)([^@\s]+)(@)"),
]


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(value: str | None = None) -> str:
    rid = value if value and re.fullmatch(r"[A-Za-z0-9._-]{8,64}", value) else uuid.uuid4().hex
    _request_id.set(rid)
    return rid


def scrub(text: str, *secrets: str | None) -> str:
    """Remove known secrets and credential-looking fragments from a message."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    text = _SECRET_PATTERNS[0].sub(r"\1=***", text)
    return _SECRET_PATTERNS[1].sub(r"\1***\3", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub(record.getMessage()),
            "request_id": get_request_id(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("uvicorn.access").disabled = True  # replaced by app-level request logs


def log(event: str, **fields: Any) -> None:
    logger.info(event, extra={"fields": fields})
