"""Phase 5: opt-in OpenTelemetry tracing."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

pytest.importorskip("opentelemetry.sdk", reason="needs the 'otel' extra")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from app.config import Settings, get_settings
from app.tracing import setup_tracing, tracing_active
from tests.integration.conftest import Api, create_connection


def settings_with(**over) -> Settings:
    return get_settings().model_copy(update={"otel_enabled": True, **over})


def test_disabled_by_default_and_fail_closed_when_misconfigured():
    app = FastAPI()
    assert get_settings().otel_enabled is False
    setup_tracing(app, get_settings())()
    assert not tracing_active()
    with pytest.raises(RuntimeError, match="DBW_OTEL_ENDPOINT"):
        setup_tracing(app, settings_with())


def reuse_session(api: Api, admin: Api) -> Api:
    """Sessions live in the database, so the admin's cookie works on any application instance
    (and a fresh login could trip the login throttling other tests exercise)."""
    api.http.cookies.update(admin.http.cookies)
    api.csrf = admin.csrf
    return api


async def fresh_traced_app(exporter, **over):
    """A new application instrumented before its first request (as in create_app)."""
    from app.main import create_app

    app = create_app()
    return app, setup_tracing(app, settings_with(**over), exporter=exporter)


async def test_requests_and_managed_calls_are_traced_without_sensitive_data(admin, pg_managed):
    exporter = InMemorySpanExporter()
    app, stop = await fresh_traced_app(exporter)
    api = reuse_session(Api(app), admin)
    try:
        assert tracing_active()
        conn = await create_connection(api, "postgresql", pg_managed)
        r = await api.get(f"connections/{conn['id']}/databases", params={"secret_q": "hunter2"})
        assert r.status_code == 200
        await api.get("/health/live")
        await api.get("/metrics")
        spans = exporter.get_finished_spans()
        names = [s.name for s in spans]
        assert any("/api/v1/connections/{cid}/databases" in n for n in names), names
        managed = [s for s in spans if s.name == "dbw.managed_call"]
        assert any("list_databases" in s.attributes["dbw.call"] for s in managed)
        # the managed call is a child of the request span: one trace
        req = next(s for s in spans if "/api/v1/connections/{cid}/databases" in s.name)
        assert any(m.context.trace_id == req.context.trace_id for m in managed)
        # nothing sensitive, and no excluded endpoints
        blob = " ".join(f"{s.name} {dict(s.attributes)}" for s in spans).lower()
        for secret in ("hunter2", "cookie", "dbw_session", "csrf", "authorization", "password"):
            assert secret not in blob, secret
        assert not any("/health" in n or "/metrics" in n for n in names)
    finally:
        await api.aclose()
        stop()
    assert not tracing_active()


async def test_sampling_ratio_zero_records_nothing(admin):
    exporter = InMemorySpanExporter()
    app, stop = await fresh_traced_app(exporter, otel_sample_ratio=0.0)
    api = reuse_session(Api(app), admin)
    try:
        await api.get("connections")
        assert exporter.get_finished_spans() == ()
    finally:
        await api.aclose()
        stop()
