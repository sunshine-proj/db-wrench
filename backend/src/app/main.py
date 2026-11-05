"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.api.admin import health
from app.api.router import api_router
from app.audit.sink import forward_loop
from app.config import get_settings
from app.db import dispose_engine
from app.errors import install_error_handlers
from app.middleware import CoreMiddleware
from app.observability import configure_logging, log
from app.query.service import purge_loop
from app.services.bootstrap import run_startup
from app.services.runtime import reset_runtime
from app.tracing import setup_tracing


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.database.core.manager import reset_adapter_manager

    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.network_allow_loopback:
        log("warning_loopback_allowed", note="DBW_NETWORK_ALLOW_LOOPBACK is on (development only)")
    await run_startup()
    purge = asyncio.create_task(purge_loop(settings.history_purge_interval_s))
    forwarder = asyncio.create_task(forward_loop(settings))
    yield
    purge.cancel()
    forwarder.cancel()
    await asyncio.gather(purge, forwarder, return_exceptions=True)
    app.state.stop_tracing()
    reset_adapter_manager()
    reset_runtime()
    await dispose_engine()


def _operation_id(route: APIRoute) -> str:
    """Unique per method+path (relational routes exist in schema and schema-less shapes)."""
    path = route.path_format.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return f"{route.name}__{sorted(route.methods or {'get'})[0].lower()}__{path}"


def create_app() -> FastAPI:
    settings = get_settings()
    dev = settings.env != "production"
    app = FastAPI(
        title="DB Wrench API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if dev else None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        generate_unique_id_function=_operation_id,
    )
    install_error_handlers(app)
    app.include_router(api_router)
    app.include_router(health)
    app.add_middleware(CoreMiddleware, max_body_bytes=settings.max_request_bytes)
    # Before the first request: middleware cannot be added to a started application.
    app.state.stop_tracing = setup_tracing(app, settings)
    return app


app = create_app()
