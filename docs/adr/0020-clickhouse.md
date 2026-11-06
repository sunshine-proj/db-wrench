# ADR-0020: ClickHouse

Status: Accepted (extends ADR-0017)

## Context

ClickHouse is an analytical column store. It fits the relational browsing/query surface but has no
row identity, no transactions and asynchronous mutations, so it is a **read-and-append** engine in
DB Wrench. Verified against 24.3, 24.8 and 25.3.

## Decision

- **Transport:** the HTTP interface through `httpx` (already a dependency). The destination is
  validated and the connection made to the validated address on **every request** (HTTP keep-alive
  makes per-connection validation impossible); TLS modes map to an `ssl` context with the original
  host name as SNI, so `verify-full` keeps full verification while connecting to the pinned address.
- **No interpolation.** `%s` placeholders become typed server-side parameters (`{p0:Int64}` with
  `param_p0=...`); `NULL` is inline. User-authored SQL is sent verbatim.
- **Read-only is enforced by the server.** Every read path sends `readonly=1`. Probed on 24.8: the
  server then refuses inserts, DDL, `SET readonly=0`, `SELECT ... SETTINGS readonly=0`, and the
  `url()`/`file()` table functions, and rejects multi-statement input. This is the strongest read-only
  story of the additional engines. Writes need a writable connection and the write permission.
- **Limits and cancellation:** `max_execution_time` plus a client timeout; results cut with
  `max_result_rows`/`max_result_bytes`; cancellation is `KILL QUERY WHERE query_id = ...` for a query
  id assigned per run (cross-replica via the stored reference).
- **Metadata** comes from `system.databases/tables/columns/data_skipping_indices`. The "primary key"
  is a sparse index and not unique, so tables report **no primary key**: the browser uses offset
  pagination with an ordering warning, the grid is read-only, and export streams
  `JSONCompactEachRowWithNamesAndTypes`.
- **Refused here, by design:** row insert/update/delete, bulk edits, CSV/JSON import, creating or
  dropping databases and indexes, and database users. They are done with SQL in the workspace
  (`INSERT`, `ALTER TABLE ... DELETE`, `CREATE DATABASE`).

## Consequences

- `DateTime` values carry no time zone in the JSON output; they are shown as the server prints them.
- Writes through the SQL workspace cannot be rolled back (the `rollback` option is rejected).
- Per-user ClickHouse credentials, quotas and profiles are not managed.
