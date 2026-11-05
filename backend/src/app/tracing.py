"""OpenTelemetry tracing (opt-in, ADR-0012).

Off by default. When ``DBW_OTEL_ENABLED`` is set the application *requires* the ``otel`` extra
and an endpoint, and refuses to start otherwise (secure/explicit by default: a silently missing
trace pipeline is worse than a loud failure).

What is traced: one server span per HTTP request (route template, method, status) and one span
per managed-database call made from the worker pool (the adapter method name only). What is
never recorded: request or response bodies, query strings, cookies, authorization headers,
statement text, row data or credentials. Health and metrics endpoints are not traced.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from app.config import Settings
from app.observability import logger

_EXCLUDED = "/health/live,/health/ready,/metrics"
_provider: Any = None


def _scrub_request(span: Any, scope: dict[str, Any]) -> None:
    """Query strings carry filters and search text (possibly sensitive literals): drop them from
    every attribute the instrumentation recorded."""
    qs = scope.get("query_string", b"")
    if not qs or span is None or not span.is_recording():
        return
    raw = qs.decode("latin-1")
    for key, value in list((span.attributes or {}).items()):
        if isinstance(value, str) and raw in value:
            span.set_attribute(key, value.replace("?" + raw, "").replace(raw, "[redacted]"))


def tracing_active() -> bool:
    return _provider is not None


def setup_tracing(
    app: FastAPI, settings: Settings, *, exporter: Any | None = None
) -> Callable[[], None]:
    """Install tracing; returns a function that shuts it down. ``exporter`` is for tests."""
    global _provider
    if not settings.otel_enabled:
        return lambda: None
    if exporter is None and not settings.otel_endpoint:
        raise RuntimeError("DBW_OTEL_ENABLED requires DBW_OTEL_ENDPOINT.")
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError as exc:  # fail closed
        raise RuntimeError(
            "DBW_OTEL_ENABLED is set but the 'otel' extra is not installed "
            "(pip install 'db-wrench-backend[otel]')."
        ) from exc

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name}),
        sampler=ParentBased(TraceIdRatioBased(settings.otel_sample_ratio)),
    )
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = (settings.otel_endpoint or "").rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=_EXCLUDED,
        exclude_spans=["receive", "send"],
        server_request_hook=_scrub_request,
    )
    _provider = provider
    logger.info("tracing_enabled", extra={"fields": {"service": settings.otel_service_name}})

    def shutdown() -> None:
        global _provider
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()
        _provider = None

    return shutdown


def run_in_context[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run ``fn`` with the caller's context (trace + request id) and, when tracing, in a span."""
    if _provider is None:
        return fn(*args, **kwargs)
    tracer = _provider.get_tracer("db-wrench")
    with tracer.start_as_current_span("dbw.managed_call") as span:
        span.set_attribute("dbw.call", getattr(fn, "__qualname__", "call"))
        return fn(*args, **kwargs)
