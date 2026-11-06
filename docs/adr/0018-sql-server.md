# ADR-0018: Microsoft SQL Server

Status: Accepted (extends ADR-0017)

## Context

SQL Server is the second engine of the additional-engines track. It fits the relational family but
differs in ways that matter for safety (AGENTS.md 7, ADR-0008): **there is no read-only
transaction**, statements need no `;` separator, and some statements (`CREATE DATABASE`,
`BACKUP`, `xp_cmdshell`) are not transactional.

## Decision

- **Driver:** `pymssql` (FreeTDS), a pooled `QueuePool` per connection spec, synchronous in the
  adapter thread pool like the other engines. No ODBC driver is needed in the image.
- **Read-only enforcement (the honest version).** The database cannot refuse writes on a session,
  so three layers are used and documented as weaker than PostgreSQL/MySQL:
  1. every non-write path runs in a transaction that is **never committed**, and pooled
     connections are rolled back on return;
  2. user-authored SQL on a non-write path passes a strict token gate: it must start with
     `SELECT`/`WITH`, and no `INSERT/UPDATE/DELETE/MERGE/DDL/EXEC/INTO/OPENROWSET/OPENQUERY/
     BULK/WAITFOR/SET/USE/DECLARE/KILL/BACKUP/...` or `xp_*`/`sp_*`/`fn_*` word may appear anywhere
     (comments, strings and `[bracketed]` identifiers are skipped). This also closes the T-SQL
     `SELECT 1 DELETE FROM t` form that the `;`-based classifier cannot see;
  3. the connection form recommends, and the capability `supports_read_only_transactions: false`
     tells the UI, that a **least-privilege login** is the real boundary. Read-only connections
     refuse writes at the application layer as before.
- **TLS:** `disable` and `require` (encrypted, certificate not verified) work. `verify-ca` and
  `verify-full` are **refused with an explicit error** rather than silently downgraded, because
  FreeTDS has no per-connection CA option. Address pinning is unchanged (connect by validated IP).
- **Timeouts and cancellation:** `SET LOCK_TIMEOUT`, a watchdog timer that issues `KILL <spid>`
  from a second connection, and the same `KILL` for user cancellation (cross-replica via the
  stored `spid`). A watchdog kill is reported as a statement timeout.
- **SQL dialect hooks** in the shared builder (`limit_sql`, `keyset_after`, `capped_count`,
  `null_safe_eq`, `quote`): `OFFSET .. FETCH`, expanded keyset predicate instead of row values,
  `TOP` in capped counts, `EXISTS (SELECT col INTERSECT SELECT ?)` for NULL-safe comparison,
  `[bracket]` quoting. The output for the other dialects is unchanged.
- **Scope:** database -> schema -> table, like PostgreSQL. Administration creates/drops databases
  (drop uses `SINGLE_USER WITH ROLLBACK IMMEDIATE`), schemas (restrictive drop) and indexes.
  Principals are **server logins** with a per-database user created on first grant; allowlisted
  privileges only (`CONTROL`, `IMPERSONATE`, server roles and `sysadmin` are never grantable); `sa`,
  sysadmin members, Windows logins and the connecting account are protected.

## Consequences

- A login with write rights that a user reaches through a *read* statement is still limited by the
  gate and the rollback, but a determined bypass of the gate is not stopped by the engine. Use a
  read-only login for connections that must be provably read-only.
- Not modelled: Windows/AD authentication, Azure SQL / managed-instance specifics, linked servers,
  temporal and in-memory tables, Always Encrypted, server roles, row-level security policies.
- Versions in CI: SQL Server 2019 and 2022 (Linux containers, Developer edition).
