# ADR-0017: Additional database engines, starting with MariaDB

Status: Accepted

## Context

The product shipped with PostgreSQL, MySQL and MongoDB; the PRD (§4, §14) listed further engines as
future work and said MariaDB must be a *separate* adapter, not "MySQL". The owner authorised
extending the scope, in this order: **MariaDB, Microsoft SQL Server, Redis/Valkey, ClickHouse,
SQLite, Elasticsearch/OpenSearch**. Oracle stays out of scope.

Each engine still has to satisfy AGENTS.md section 2: adapters behind a family contract, database-level
read-only enforcement, network policy and address pinning, authorization and audit unchanged.

## Decision

- **One engine per phase, each with its own ADR amendment or ADR when it needs a new family.**
  Relational engines (MariaDB, SQL Server, ClickHouse, SQLite) extend the relational family;
  Redis needs a new key-value family and Elasticsearch a new search family, whose adapter
  interfaces are defined before the first adapter (AGENTS.md 2.2).
- **MariaDB is a subclass of the MySQL adapter, not an alias.** Same wire protocol and
  `information_schema`, so the connection, metadata, editing, export, import and cancellation code
  is shared. Everything that differs on a real server is overridden in `adapters/mariadb.py`,
  verified against 10.11 and 11.4 containers:
  - statement timeout is `max_statement_time` (seconds; error 1969), not `max_execution_time`;
  - read-only session variable is `tx_read_only` (`transaction_read_only` exists only from 11.x),
    and user SQL is still run inside an explicit `START TRANSACTION READ ONLY`;
  - `mysql.user` has no `account_locked`; lock state and role flag come from `mysql.global_priv`,
    and roles are excluded from the account list;
  - `JSON` is `LONGTEXT` plus a `json_valid` CHECK, so such columns are reported as `json`.
- **Type-check key values for numeric columns in the shared editor layer.** MariaDB, unlike
  PostgreSQL and MySQL strict mode, coerces `WHERE int_col = '1 OR 1=1'` to `= 1`, which made a
  forged row key delete a real row (found by the existing forged-key security test). Row-key and
  `expected` values for integer/decimal columns must now be numeric, regardless of engine. The value
  was always parameterised, so this was a coercion flaw, not injection.
- **Test matrix.** MariaDB 10.11 and 11.4 are in the CI version matrix; the shared relational
  integration suites (explorer, editor, admin, principals, query, import, adapter safety) are
  parametrised over `mariadb`.

## Consequences

- `db_type` gains `mariadb` (API enum, OpenAPI, UI selector). No migration: the column is a string.
- MariaDB `sequences` and `INTERSECT`/`EXCEPT` specifics, Aria/ColumnStore engines and Galera are not
  modelled; MariaDB roles (`CREATE ROLE`) are not managed (Phase 6 manages accounts only).
- Later engines each need their own read-only enforcement story documented before implementation:
  SQL Server (`KILL`, read-only intent / read-only login), Redis (ACL users, no transactions for
  read-only), ClickHouse (`readonly=1` setting, HTTP interface), SQLite (read-only file open only),
  Elasticsearch (read-only role, no scripting).
