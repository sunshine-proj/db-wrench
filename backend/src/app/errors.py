"""Domain errors and RFC 9457 problem+json mapping."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.observability import get_request_id, logger


class AppError(Exception):
    status = 400
    code = "bad_request"
    title = "Bad request"

    def __init__(self, detail: str | None = None, *, extra: dict[str, Any] | None = None):
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.extra = extra or {}


class ValidationFailed(AppError):
    status = 422
    code = "validation_failed"
    title = "Validation failed"


class Unauthorized(AppError):
    status = 401
    code = "unauthorized"
    title = "Authentication required"


class MfaRequired(AppError):
    status = 403
    code = "mfa_required"
    title = "Multi-factor authentication required"


class Forbidden(AppError):
    status = 403
    code = "forbidden"
    title = "Forbidden"


class NetworkPolicyViolation(AppError):
    status = 403
    code = "network_policy_denied"
    title = "Destination denied by network policy"


class NotFound(AppError):
    status = 404
    code = "not_found"
    title = "Not found"


class Conflict(AppError):
    status = 409
    code = "conflict"
    title = "Conflict"


class RateLimited(AppError):
    status = 429
    code = "rate_limited"
    title = "Too many requests"


class ConnectionFailed(AppError):
    status = 502
    code = "connection_failed"
    title = "Could not connect to the managed database"


class QueryFailed(AppError):
    status = 400
    code = "query_failed"
    title = "The database rejected the request"


class StatementTimeout(AppError):
    status = 408
    code = "statement_timeout"
    title = "The operation exceeded the execution time limit"


class OperationCancelled(AppError):
    status = 409
    code = "cancelled"
    title = "The operation was cancelled"


class StatementNotAllowed(AppError):
    status = 422
    code = "statement_not_allowed"
    title = "This statement is not allowed here"


class NotEditable(AppError):
    status = 422
    code = "not_editable"
    title = "This object cannot be edited safely"


class ConfirmationRequired(AppError):
    status = 428
    code = "confirmation_required"
    title = "Explicit confirmation required"


class UnsupportedCapability(AppError):
    status = 422
    code = "unsupported_capability"
    title = "Operation not supported by this database engine"


class SchemaMismatch(RuntimeError):
    """Raised at startup when the application database is not at the expected revision."""


def problem(
    status: int, code: str, title: str, detail: str | None = None, **extra: Any
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "code": code,
        "request_id": get_request_id(),
    }
    if detail and detail != title:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return problem(exc.status, exc.code, exc.title, exc.detail, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": [str(p) for p in e.get("loc", [])], "msg": e.get("msg", "")}
            for e in exc.errors()
        ]
        return problem(422, "validation_failed", "Validation failed", errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(exc.status_code, f"http_{exc.status_code}", str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", exc_info=exc)
        return problem(500, "internal_error", "Internal server error")
