# Development Status

This file defines the **current development scope**. Agents and contributors must not implement
functionality outside the active phase without authorization (AGENTS.md section 13). Phases are
defined in PRD.md section 13 and README.md section 9.

## Active phase

**Phase 7: MCP and AI integration** (not started). Phases 0-6 are implemented. The owner has
authorised additional database engines (ADR-0017) as a parallel track, one engine at a time, in this
order: MariaDB (done), Microsoft SQL Server (done), Redis/Valkey (done), ClickHouse (done), SQLite (done), Elasticsearch/OpenSearch (done). All engines on the track are implemented.

## Phase checklist

| Phase | Name                                        | Status      |
| ----- | ------------------------------------------- | ----------- |
| 0     | Foundation                                  | Done        |
| 1     | Core platform and PostgreSQL (read-only)    | Done        |
| 2     | MySQL and MongoDB (read-only)               | Done        |
| 3     | Data editor and export                      | Done        |
| 4     | Query workspaces                            | Done        |
| 5     | Administration, import, SSO, approvals      | Done        |
| 6     | Extended administration (users and roles)   | Done        |
| 7     | MCP and AI integration                      | Not started |

### Additional engines track (ADR-0017)

| Engine                  | Family       | Status      |
| ----------------------- | ------------ | ----------- |
| MariaDB 10.11, 11.4     | relational   | Done        |
| Microsoft SQL Server 2019, 2022 | relational | Done  |
| Redis 6.2, 7.4 / Valkey 8 | key-value  | Done        |
| ClickHouse 24.3, 24.8, 25.3 | relational | Done      |
| SQLite 3.35+ (files below DBW_SQLITE_ROOT) | relational | Done |
| Elasticsearch 7.17, 8.15 / OpenSearch 2.17 | search | Done |

## What exists (Phases 0-6)

- **Foundation:** FastAPI backend, React/Vite/Tailwind frontend, Alembic migrations, Docker
  Compose (default + hardened production overlay), reverse proxy with TLS profile, health and
  metrics endpoints, structured logging, CI workflows, Makefile, ADRs, operations guide.
- **Core platform:** server-side sessions, Argon2id, CSRF + Origin checks, login lockout and IP
  throttling, TOTP MFA with recovery codes and per-role enforcement, first-run setup token,
  users/roles/custom roles with privilege-escalation guards, per-connection grants and a full
  permission matrix, hash-chained append-only audit log, envelope-encrypted credentials with key
  rotation, outbound network policy with address pinning.
- **PostgreSQL / MySQL / MongoDB (read-only):** connection management and testing, explorer
  (databases, schemas, tables, views, sequences, functions, indexes, constraints, foreign keys,
  collections, collection statistics), data browser with keyset/offset pagination, sorting,
  filtering, search, column selection, type-preserving JSON (big integers and decimals as
  strings, Extended JSON for BSON), binary/long-text markers, MongoDB operator allowlist.
- **Data editor and export (Phase 3):** insert/update/delete rows and documents with optimistic
  concurrency (previous values, null-safe; compare-and-swap for MongoDB), bulk update/delete with a
  signed preview token and count verification (rolls back on mismatch; MongoDB pins the previewed
  `_id`s), production typed confirmation, inline editing, full-value fetch of truncated cells,
  streamed CSV/JSON/NDJSON export (formula-injection safe, size-limited, audited), sensitive-field
  masking (ADR-0009).
- **Query workspaces (Phase 4):** SQL (PostgreSQL, MySQL) and MongoDB (find, aggregation allowlist)
  with Monaco, multiple tabs, cancellation (cross-replica), timeouts, dry-run, scripts by
  permission, destructive-statement confirmation, private history with retention and literal
  redaction, saved queries (private/shared), server-side query export (ADR-0010).
- **Administration and governance (Phase 5):** create/drop databases, schemas (PostgreSQL),
  collections and indexes on all three engines (typed confirmation, restrictive drops, system
  objects protected); environment/connection policies and four-eyes approvals bound to the exact
  request (ADR-0011), including mandatory approval for MongoDB `$out`/`$merge`; CSV and JSON/NDJSON
  import with a validating, rolled-back preview (ADR-0014); OIDC single sign-on with group-to-role
  mapping (ADR-0012); scoped, expiring API tokens; role grants on a database, schema or
  table/collection (ADR-0013); per-user managed-database credentials; audit export and
  syslog/SIEM forwarding; opt-in OpenTelemetry tracing (ADR-0015).
- **Database users, roles and privileges (Phase 6):** inspect, create, alter (password, lock,
  role attributes) and drop PostgreSQL roles, MySQL accounts and MongoDB users and custom roles;
  grant and revoke privileges on databases, schemas, tables (PostgreSQL also all tables in a
  schema and role membership; MongoDB role assignment and custom-role privileges). Allowlisted
  privileges, protected accounts (superusers, system accounts, DB Wrench's own account), write-only
  passwords, typed confirmation, optional four-eyes approval, audit (ADR-0016).
- **MariaDB (ADR-0017):** a separate adapter on the shared MySQL code path with MariaDB-specific
  timeout, read-only, account and `JSON` handling; full parity with MySQL (explorer, editor, export,
  import, SQL workspace, administration, users and privileges), 10.11 and 11.4 in the CI matrix.
  Numeric key/expected values are now type-checked in the editor for every relational engine.
- **Microsoft SQL Server (ADR-0018):** database -> schema -> table explorer, browser with keyset
  paging, editor, export, SQL workspace, create/drop databases, schemas and indexes, logins and
  privileges. No engine-level read-only transaction: rollback-only paths, a strict token gate for
  user SQL and a recommended least-privilege login. TLS verification modes are refused, not skipped.
- **Redis / Valkey (ADR-0019):** new key-value adapter family; database picker, SCAN-based key
  browser (pattern and type filters, TTL), typed value viewer with paging, whole-value create /
  replace / delete with version-checked concurrency, TTL control, and an allowlisted command
  console whose commands are also checked against the server's own command flags. Standalone
  servers only; ACL-limited accounts are the recommended read-only boundary.
- **ClickHouse (ADR-0020):** explorer, browser (offset pagination, no row identity), export, SQL
  workspace with server-enforced `readonly=1`, typed server-side parameters, timeouts and
  cancellation. Append-only: row edits, bulk edits, import, database/index administration and users
  are refused (use SQL).
- **SQLite (ADR-0021):** opt-in via `DBW_SQLITE_ROOT`; connections name a file inside it (symlink
  and traversal safe, never created); `mode=ro` + authorizer on every connection (no `ATTACH`,
  `VACUUM`, file functions or unsafe pragmas); browsing, editing, bulk edits, import, export, SQL
  workspace with rollback, timeouts and same-replica cancellation, index administration.
- **Elasticsearch / OpenSearch (ADR-0022):** new search adapter family; index list and detail
  (fields, settings), document browser (text search, sorting, paging within the 10,000 window),
  create / replace / delete with native `_seq_no` concurrency, create/delete index with typed
  confirmation, and a console that can only run validated `_search` bodies (no scripts, no lookups
  into other indices, bounded size).
- **SQL table dump:** `GET .../tables/{table}/export?format=sql` streams a dump of one relational
  table (PostgreSQL, MySQL, MariaDB, SQL Server, ClickHouse, SQLite): an optional best-effort
  `CREATE TABLE` (`ddl=false` for data only) and batched multi-row `INSERT`s, respecting filters,
  search, sort and column choice. Escaping is per dialect, masked columns are written as `NULL`,
  generated columns are skipped, and hostile names cannot escape the header comments. Needs
  `export.export` + `data.read`; audited like other exports; verified by dump-and-restore round trips
  on all six engines.
- **Frontend:** setup, login (+MFA, +SSO), account/security, dashboard, connections (+access dialog),
  explorer tree, grid and document browsers, users, roles, network policy, audit log; i18n layer;
  strict CSP; WCAG-minded markup.

## Deferred or incomplete (by design, with owners)

| Item | Where it lands |
| --- | --- |
| SAML / LDAP single sign-on | future (ADR required) |
| Column-level, routine-level and default privileges; MySQL global privileges | future |
| Approval policies per role (policies are per connection and environment today) | future |
| Table creation/alteration through the UI (use the SQL workspace) | future |
| MCP server and AI assistance | Phase 7 |
| Mongo replica-set topology discovery (needs address filtering, ADR-0007) | future |

## Known limits

- MySQL/MongoDB `verify-full` TLS connect by host name after validation (ADR-0007).
- Metadata cache is per replica (30 s TTL).
- Workflows in `.github/` are linted but have not run on GitHub yet.
- Per-connection concurrent-query limit is not enforced (only the per-user limit and pool sizes).
- MongoDB bulk operations are not atomic (no multi-document transactions on standalone servers);
  they are pinned to the previewed `_id`s and refuse when the count changed. MongoDB query
  cancellation is best-effort.
- Free-form SQL/aggregation cannot be masked; connections with sensitive markers close them to
  users without `data.read_sensitive` (ADR-0009).
- Saved-query sharing is per connection (everyone who can query it), not per group.
- The CSP allows inline *styles* for Monaco (ADR-0006).
- OIDC: the identity-provider HTTP client validates the destination but does not pin the resolved
  address (ADR-0012); sessions of a user removed from an IdP group live until they expire or an
  administrator deactivates the user.
- MongoDB import is not atomic (no multi-document transactions on standalone servers). A relational
  import preview can advance sequences/identity counters.
- Scoped grants never cover free-form queries; users with only scoped grants cannot use the SQL or
  MongoDB workspaces.
- Approvals have no delegation, quorum greater than one, or notification channel yet; reviewers see
  them under *Approvals*.
- Database principals: objects owned by a role block its drop; MySQL grants cover a database or a
  table only; MongoDB users live in a database other than `admin`/`local`/`config` (system
  databases are not managed here).
- The Phase 5 screens were exercised in a real browser against the Docker stack (database
  create/drop with typed confirmation, CSV import with and without errors, an approval round trip
  between two users, API tokens, the integrations/governance/audit/approvals pages). The SSO login
  button, the scoped-access and personal-credentials dialogs are covered by unit, component and
  integration tests only (no identity provider was available in the browser run).

- MariaDB: roles (`CREATE ROLE`) are not managed, sequences are not listed, Galera/ColumnStore are
  not modelled.

## Notes

- Update this file as part of every feature's Definition of Done.
- Record scope changes as ADRs in `docs/adr/`.
