# syntax=docker/dockerfile:1
FROM python:3.12.11-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Optional extras, e.g. DBW_EXTRAS="--extra otel" to ship OpenTelemetry tracing support.
ARG DBW_EXTRAS=""
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-editable ${DBW_EXTRAS}
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable ${DBW_EXTRAS}

FROM python:3.12.11-slim
# Non-root user; no shell login, no home directory contents.
RUN groupadd --system --gid 10001 dbwrench \
 && useradd --system --uid 10001 --gid dbwrench --no-create-home --shell /usr/sbin/nologin dbwrench
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3).status == 200 else 1)"
# One worker per container: scale with replicas. Proxy headers are handled by the app itself
# (DBW_TRUSTED_PROXY_HOPS), not by uvicorn.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-proxy-headers", "--no-server-header", "--no-access-log"]
