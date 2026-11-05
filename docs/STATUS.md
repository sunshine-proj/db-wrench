# Development Status

This file defines the **current development scope**. Agents and contributors must not implement
functionality outside the active phase without authorization (AGENTS.md section 13). Phases are
defined in PRD.md section 13 and README.md section 9.

## Active phase

**Phase 6: Extended administration (database users, roles and privileges)** (not started). Phases 0-5 are implemented.

## Phase checklist

| Phase | Name                                        | Status      |
| ----- | ------------------------------------------- | ----------- |
| 0     | Foundation                                  | Done        |
| 1     | Core platform and PostgreSQL (read-only)    | Done        |
| 2     | MySQL and MongoDB (read-only)               | Done        |
| 3     | Data editor and export                      | Done        |
| 4     | Query workspaces                            | Done        |
| 5     | Administration, import, SSO, approvals      | Done        |
| 6     | Extended administration (users and roles)   | Not started |
| 7     | MCP and AI integration                      | Not started |

## What exists (Phases 0-5)

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
- **Frontend:** setup, login (+MFA, +SSO), account/security, dashboard, connections (+access dialog),
  explorer tree, grid and document browsers, users, roles, network policy, audit log; i18n layer;
  strict CSP; WCAG-minded markup.

## Deferred or incomplete (by design, with owners)

| Item | Where it lands |
| --- | --- |
| SAML / LDAP single sign-on | future (ADR required) |
| Approval policies per role (policies are per connection and environment today) | future |
| Table creation/alteration through the UI (use the SQL workspace) | future |
| Database user/role/privilege management | Phase 6 |
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
- The Phase 5 screens were exercised in a real browser against the Docker stack (database
  create/drop with typed confirmation, CSV import with and without errors, an approval round trip
  between two users, API tokens, the integrations/governance/audit/approvals pages). The SSO login
  button, the scoped-access and personal-credentials dialogs are covered by unit, component and
  integration tests only (no identity provider was available in the browser run).

## Notes

- Update this file as part of every feature's Definition of Done.
- Record scope changes as ADRs in `docs/adr/`.
