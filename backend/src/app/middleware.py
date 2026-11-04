"""Pure-ASGI middleware: request IDs, access logs/metrics, security headers, body limit."""

from __future__ import annotations

import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import LATENCY, REQUESTS, log, set_request_id

SECURITY_HEADERS = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"referrer-policy": b"no-referrer",
    b"cache-control": b"no-store",
    b"content-security-policy": b"default-src 'none'; frame-ancestors 'none'",
    b"permissions-policy": b"geolocation=(), camera=(), microphone=()",
}
DOC_PATHS = ("/api/docs", "/api/redoc")


class RequestTooLarge(Exception):
    pass


class CoreMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int):
        self.app = app
        self.max_body = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope["headers"]}
        rid = set_request_id(headers.get(b"x-request-id", b"").decode("latin-1") or None)
        method, path = scope["method"], scope["path"]
        started = time.perf_counter()
        status = 500

        declared = headers.get(b"content-length")
        if declared and declared.isdigit() and int(declared) > self.max_body:
            await self._reject(send, rid)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    raise RequestTooLarge
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                out: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                out.append((b"x-request-id", rid.encode()))
                if not path.startswith(DOC_PATHS):
                    present = {k.lower() for k, _ in out}
                    out.extend((k, v) for k, v in SECURITY_HEADERS.items() if k not in present)
                message["headers"] = out
            await send(message)

        try:
            await self.app(scope, limited_receive, send_wrapper)
        except RequestTooLarge:
            await self._reject(send, rid)
            status = 413
        finally:
            elapsed = time.perf_counter() - started
            REQUESTS.labels(method, str(status)).inc()
            LATENCY.labels(method).observe(elapsed)
            if not path.startswith("/health"):
                fields: dict[str, Any] = {
                    "method": method, "path": path, "status": status,
                    "ms": round(elapsed * 1000, 1),
                }  # fmt: skip
                log("http_request", **fields)

    @staticmethod
    async def _reject(send: Send, rid: str) -> None:
        body = (
            b'{"type":"about:blank","title":"Request body too large","status":413,'
            b'"code":"payload_too_large","request_id":"' + rid.encode() + b'"}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
