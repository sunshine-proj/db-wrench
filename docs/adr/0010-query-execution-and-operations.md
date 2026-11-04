# ADR-0010: Query execution, operations and cancellation

Status: Accepted

## Decision

- **Dedicated connections.** User SQL never runs on a pooled connection: `SET`, temporary
  tables, session variables or `SET ROLE` would leak to the next user. Each execution opens its
  own connection (network policy re-checked, address pinned) and closes it afterwards. Tests
  assert that session changes do not survive.
- **Layers of read-only** (ADR-0008): classifier (policy and UX) -> read-only transaction /
  `START TRANSACTION READ ONLY` -> priming query -> database privileges. Transaction and session
  control statements (`BEGIN`, `SET`, `USE`, `KILL`, ...) are refused for everyone.
- **One statement per call** unless the user holds `query.execute_script`. PostgreSQL uses the
  extended protocol (a multi-statement string is a database error); MySQL's multi-statement flag
  is only enabled for permitted scripts.
- **Operations live in the application database** with a *backend reference* (PostgreSQL pid,
  MySQL thread id, MongoDB comment). Any replica can cancel by opening a new connection to the
  managed database (`pg_cancel_backend`, `KILL QUERY`, `killOp`). The client chooses the operation
  id so it can cancel while the query is still running.
- **Limits:** statement timeout (default 30 s, per-request up to `DBW_MAX_STATEMENT_TIMEOUT_S`),
  row and byte caps, per-user concurrency (default 5, enforced with an advisory lock so it holds
  across replicas), a MySQL watchdog for non-`SELECT` statements.
- **Writes** need `query.execute_write` on a connection that is not read-only; destructive
  statements (`DROP`, `TRUNCATE`, `DELETE`/`UPDATE` without `WHERE`, ...) need explicit
  confirmation; production writes additionally need the connection name typed back. A *dry run*
  executes and rolls back.
- **History** is private to its author, recorded in its own transaction (failures too), can drop
  literal values, and is purged by a background task after the configured retention. The audit log
  stores a digest and statement kinds, never the text.
- **MongoDB aggregation** uses a stage allowlist plus a deep denylist (`$function`, `$accumulator`,
  `$where`, `$out`, `$merge`, ...); `$lookup`-style stages may only name collections of the same
  database; a final `$limit` is always appended.

## Consequences

- MongoDB cancellation is best-effort (it needs the `inprog`/`killop` privileges).
- A connection per execution costs a few milliseconds; the isolation is worth it.
- Query export (`POST .../queries/export`) streams a single read statement, capped at
  `DBW_EXPORT_MAX_ROWS`; truncation is recorded in the audit event.
