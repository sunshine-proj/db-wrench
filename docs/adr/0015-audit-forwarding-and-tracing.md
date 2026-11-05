# ADR-0015: Audit export, SIEM forwarding and OpenTelemetry tracing

Status: Accepted

## Audit export and forwarding

- `GET /audit/export` streams CSV/JSON/NDJSON (permission `audit.export`, row-capped, the export
  itself audited). Each record includes `prev_hash` and `hash` in the exact form the chain covers,
  so an export can be verified offline (see docs/operations.md).
- **Forwarding** is a background task, active on one replica at a time (advisory lock), that reads
  events after the last forwarded id and sends RFC 5424 (or JSON) over UDP, TCP (octet counting) or
  TLS (system trust store). The database stays the source of truth: an outage only delays delivery
  (at-least-once; the event id and hash let the receiver de-duplicate). The destination is checked
  against the outbound network policy on every delivery. Enabling it forwards new events only,
  unless a backfill is requested.

## Tracing

- Off by default. `DBW_OTEL_ENABLED` requires the `otel` extra and `DBW_OTEL_ENDPOINT`, otherwise
  startup fails (a silently missing trace pipeline is worse than a loud failure). Images include the
  extra only when built with `DBW_EXTRAS="--extra otel"`.
- Traced: a server span per request (route template, method, status) and a span per managed-database
  call from the worker pool (adapter method name). Never recorded: bodies, **query strings** (they
  carry filters and search text; a test caught the default instrumentation recording them), cookies,
  authorization headers, statement text, row data, credentials. Health and metrics are not traced.
- Instrumentation is installed in `create_app()` before the first request (middleware cannot be
  added to a started application).
