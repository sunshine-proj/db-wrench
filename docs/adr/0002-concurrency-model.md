# ADR-0002: Concurrency model

Status: Accepted

## Context

AGENTS.md asks for async only where it brings measurable benefit and warns against forcing
async onto database drivers. The application has two very different database workloads.

## Decision

- **Application database:** SQLAlchemy 2 async with psycopg 3. Requests are short and
  I/O-bound; async keeps the event loop free.
- **Managed databases:** synchronous drivers (psycopg 3, PyMySQL, PyMongo) behind SQLAlchemy
  pools (relational) or the driver's own pool (MongoDB), executed on one **bounded, dedicated
  thread pool** (`DBW_DB_WORKER_THREADS`, default 32) via `run_blocking`.
- One adapter instance per connection, cached by the `AdapterManager` and replaced whenever the
  stored connection or the network policy changes; idle adapters are closed.

## Consequences

- Cancellation (`pg_cancel_backend`, `KILL QUERY`, `killOp`) is driver-uniform; a worker thread
  holds a connection for the duration of a call, so pool size and thread count bound load.
- The backend remains stateless: no in-process state matters for correctness. The metadata
  cache (30 s TTL) is per replica and may be briefly stale after schema changes.
- Revisit if PyMongo's async API or an async MySQL driver proves clearly better.
