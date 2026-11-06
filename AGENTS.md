# DB Wrench — AI Agent Development Instructions

## 1. Project Overview

DB Wrench is a self-hosted, enterprise-grade database management platform
supporting:

- PostgreSQL
- MySQL
- MongoDB

The application provides a unified web interface for database exploration, data
editing, query execution, and database administration, with centralized access
control, auditing, and governance.

It is distributed using Docker Compose and is designed to be deployable on
orchestrators such as Kubernetes without architectural changes.

The application will integrate with AI coding assistants through MCP.

Related documents:

- `PRD.md` — product requirements (authoritative for scope and behavior)
- `README.md` — overview, setup, and roadmap
- `SKILLS.md` — agent workflow definitions
- `docs/STATUS.md` — **current development scope** (authoritative for what may
  be implemented now)
- `docs/adr/` — architecture decision records

## 2. Mandatory Architectural Principles

### 2.1 Modular Monolith

The initial implementation must use a modular monolithic architecture.

Do not introduce microservices without an ADR in `docs/adr/` documenting the
architectural justification.

The backend must be stateless. All shared state (sessions, running-query
registry, rate-limit counters, job state) lives in the application database so
that multiple backend replicas can run behind a load balancer.

### 2.2 Database Independence

Database-specific functionality must be isolated.

- PostgreSQL and MySQL may share relational database abstractions.
- MongoDB must have a separate document-oriented implementation.
- Never assume that all database engines support identical operations.
- Adapter interfaces for both the relational and document families are defined
  before the first adapter is implemented, even though PostgreSQL ships first.

### 2.3 Database Adapters

All managed database interactions must go through database adapters or their
associated services.

- Frontend components must never connect directly to managed databases.
- Database-specific logic must not be scattered throughout API endpoints.

### 2.4 Separation of Application and Managed Databases

The application has its own internal PostgreSQL database.

It stores:

- Application users
- Roles and permissions
- Connection metadata
- Encrypted connection credentials
- Saved queries
- Query history
- Audit records
- Application settings
- Sessions, running-query registry, and job state

Managed database data must remain in the external database systems.

### 2.5 Security First

Every database operation must pass through authorization and connection
validation.

Never trust frontend-supplied database identifiers, table names, collection
names, or query parameters.

Never expose connection credentials to frontend clients.

### 2.6 Secure by Default

Defaults must be the safe option: new connections are read-only until explicitly
enabled for writes, production-tagged connections require additional
confirmation, and unsafe configuration must fail startup rather than degrade
silently.

## 3. Technology Stack

### Backend

- Python 3.12+
- FastAPI
- Pydantic
- SQLAlchemy 2.x
- PyMongo
- Alembic
- Pytest

Concurrency model (see ADR-0002):

- Application database: SQLAlchemy async with psycopg 3.
- Managed-database adapters: synchronous drivers executed in a bounded,
  dedicated thread pool, so that cancellation (`pg_cancel_backend`,
  `KILL QUERY`, `killOp`) and driver quirks are handled uniformly.
- Do not force asynchronous implementations when they complicate database driver
  integration.

### Frontend

- React
- TypeScript
- Vite
- Tailwind CSS
- shadcn/ui
- TanStack Query
- TanStack Table v8 with virtualization (AG Grid Enterprise is not permitted
  without a licensing ADR; see ADR-0006 for version pins)
- Monaco Editor, bundled locally (no CDN loading; the application must work
  fully offline / air-gapped)

### Tooling

| Area                | Tool                                          |
| ------------------- | --------------------------------------------- |
| Python packages     | uv                                            |
| Python lint/format  | ruff                                          |
| Python type checks  | mypy (strict for `app.database` and `app.auth`) |
| Python tests        | pytest, testcontainers                        |
| Frontend packages   | pnpm                                          |
| Frontend lint       | eslint, prettier                              |
| Frontend type check | tsc --noEmit                                  |
| Frontend tests      | vitest, React Testing Library                 |

Canonical commands are exposed as `make` targets and must be kept current
(update this table and the Makefile together):

```text
make setup        install backend and frontend dependencies
make lint         ruff, mypy, eslint, prettier --check, tsc
make test         backend unit + frontend tests (no containers)
make test-int     integration tests (testcontainers: PostgreSQL, MySQL, MongoDB)
make test-sec     security test suite
make dev          start the development Docker Compose stack
make migrate      run Alembic migrations
```

### Infrastructure

- Docker
- Docker Compose
- PostgreSQL for application storage
- Nginx or an equivalent reverse proxy (TLS termination)

Pin production container image versions (by digest where practical).

### Supported Managed Database Versions

| Engine     | Supported versions | Notes                                  |
| ---------- | ------------------ | -------------------------------------- |
| PostgreSQL | 14, 15, 16, 17     |                                        |
| MySQL      | 8.0, 8.4           |                                        |
| MariaDB    | 10.11, 11.4        | Separate adapter (ADR-0017)            |
| SQL Server | 2019, 2022         | No read-only transactions (ADR-0018)   |
| Redis      | 6.2, 7.4; Valkey 8 | Key-value family, standalone (ADR-0019) |
| ClickHouse | 24.3, 24.8, 25.3   | Read and append only (ADR-0020)        |
| SQLite     | 3.35+ (stdlib)     | Files below DBW_SQLITE_ROOT (ADR-0021) |
| Elasticsearch | 7.17, 8.15; OpenSearch 2.17 | Search family (ADR-0022) |
| MongoDB    | 6.0, 7.0, 8.0      | Standalone, replica set, sharded       |

Every supported version must be covered by integration tests in CI.

## 4. Backend Architecture

Use the following logical structure:

```text
backend/src/app/

api/
auth/
connections/
database/
    core/
    relational/
    document/
    adapters/
explorer/
editor/
query/
administration/
audit/
mcp/
services/
```

### API Layer

Responsibilities:

- Request validation
- Authentication
- Authorization
- Response serialization
- Error handling

API endpoints must not contain substantial database business logic.

### Database Core

Responsibilities:

- Connection lifecycle
- Connection pool management (per-connection pool limits, idle eviction,
  invalidation on credential or configuration change)
- Adapter registration
- Connection health
- Shared data models
- Capability discovery
- Network destination policy enforcement
- Running-operation registry and cancellation
- Transaction coordination where supported

### Relational Database Layer

Implement shared relational functionality.

Database-specific adapters:

- PostgreSQL
- MySQL

Use SQLAlchemy where appropriate.

### Document Database Layer

Implement MongoDB-specific functionality.

Use PyMongo.

Support:

- Databases
- Collections
- Documents
- Indexes
- Filters
- Aggregation pipelines

Do not represent MongoDB documents as ordinary relational rows.

Connections use `directConnection=True`: topology discovery would follow
server-advertised member addresses past the network policy (ADR-0007).

### Administration Layer

Responsibilities:

- Database creation and deletion
- Schema management
- Index management
- Database user management
- Permission management

Implement database-specific operations separately.

### MCP Layer

Thin protocol layer over existing services. It must contain no database logic
and no authorization logic of its own (see PRD §11).

## 5. Frontend Architecture

Organize frontend functionality by feature.

Suggested structure:

```text
frontend/src/

components/
features/
    dashboard/
    authentication/
    connections/
    explorer/
    data-browser/
    data-editor/
    sql-workspace/
    mongo-workspace/
    administration/
    audit/
    settings/
hooks/
services/
types/
```

Use reusable components for:

- Data grids
- Pagination
- Filtering
- Connection selection
- Confirmation dialogs
- Forms
- Notifications
- Query results

Avoid embedding database-specific business logic in generic UI components.

Frontend security rules:

- Render all database-originated content as text; never as HTML.
- No inline scripts; the application must work under a strict
  Content-Security-Policy (Monaco workers served from the same origin).
- Visually distinguish connection environments (production, staging,
  development) in every view that can read or modify data.
- All user-visible strings go through an i18n layer; meet WCAG 2.1 AA.

## 6. API Design

Use REST endpoints with consistent resource naming. The API is versioned under
`/api/v1`. The OpenAPI schema is generated, published, and checked for
breaking changes in CI.

### Resource addressing

Managed objects are addressed by their full path so that every request is
authorized against an explicit connection, never against a bare client-supplied
ID:

```text
GET    /api/v1/connections
POST   /api/v1/connections
GET    /api/v1/connections/{cid}
PATCH  /api/v1/connections/{cid}
DELETE /api/v1/connections/{cid}
POST   /api/v1/connections/{cid}/test

GET    /api/v1/connections/{cid}/databases
GET    /api/v1/connections/{cid}/databases/{db}/schemas
GET    /api/v1/connections/{cid}/databases/{db}/schemas/{schema}/tables

GET    /api/v1/connections/{cid}/databases/{db}/schemas/{schema}/tables/{table}/structure
GET    /api/v1/connections/{cid}/databases/{db}/schemas/{schema}/tables/{table}/rows
POST   /api/v1/connections/{cid}/databases/{db}/schemas/{schema}/tables/{table}/rows
PATCH  /api/v1/connections/{cid}/databases/{db}/schemas/{schema}/tables/{table}/rows/{row_key}
DELETE /api/v1/connections/{cid}/databases/{db}/schemas/{schema}/tables/{table}/rows/{row_key}

POST   /api/v1/connections/{cid}/queries/execute
POST   /api/v1/connections/{cid}/queries/{operation_id}/cancel
```

MySQL omits the `schemas/{schema}` segment (a database is the namespace).
Editing and export live under the same table path: `POST .../rows`,
`PATCH|DELETE .../rows/{row_key}` (previous values required; `DELETE` takes a JSON body),
`POST .../rows/bulk/{preview|delete|update}`, `GET .../rows/{row_key}/cells/{column}` and
`GET .../export?format=csv|json|ndjson|sql[&ddl=false]` (`sql` = table dump, relational engines). Query workspaces: `POST /connections/{cid}/queries/execute`,
`POST .../queries/{operation_id}/cancel`, `POST .../queries/export`, `GET|DELETE .../queries/history`,
`.../queries/saved`, and `POST .../mongo/queries/execute`.

Administration (databases, schemas, indexes) and import live under the same paths:
`POST|DELETE .../databases[/{db}]`, `POST|DELETE .../databases/{db}/schemas[/{schema}]`,
`POST .../tables/{table}/indexes`, `DELETE .../indexes/{index}`, `POST .../tables/{table}/import/{preview|commit}`;
MongoDB: `POST|DELETE .../mongo/databases[/{db}]`, `.../collections[/{coll}]`, `.../indexes[/{index}]`,
`.../collections/{coll}/import/{preview|commit}`. Operations under an approval policy answer
`428 approval_required` and are repeated with an `Approval-Id` header. Application-level resources:
`/api/v1/approvals`, `/api/v1/governance/policies`, `/api/v1/tokens`, `/api/v1/auth/oidc/*`,
`/api/v1/settings/{oidc,audit-sink}`, `/api/v1/audit/export`,
`/api/v1/connections/{cid}/{scope-grants,credentials/me}`.

Database users, roles and privileges (Phase 6): `GET|POST .../principals`,
`PATCH|DELETE .../principals/{name}?host=` (MySQL account host), `GET|POST|DELETE
.../principals/{name}/grants`, `POST|DELETE .../principals/{name}/memberships` (PostgreSQL);
MongoDB: `.../mongo/databases/{db}/users[/{name}[/password|/roles]]` and
`.../mongo/databases/{db}/roles[/{role}[/privileges]]`.

PostgreSQL additionally exposes `.../schemas/{schema}/sequences` and
`.../functions[/{function_id}]`. The generated OpenAPI document
(`docs/openapi.json`, regenerate with `make openapi`) is the authoritative
contract; CI fails when it changes without being committed.

MongoDB uses document-oriented contracts:

```text
GET    /api/v1/connections/{cid}/mongo/databases
GET    /api/v1/connections/{cid}/mongo/databases/{db}/collections
GET    /api/v1/connections/{cid}/mongo/databases/{db}/collections/{coll}/documents
POST   /api/v1/connections/{cid}/mongo/databases/{db}/collections/{coll}/documents
PATCH  /api/v1/connections/{cid}/mongo/databases/{db}/collections/{coll}/documents/{doc_id}
DELETE /api/v1/connections/{cid}/mongo/databases/{db}/collections/{coll}/documents/{doc_id}
POST   /api/v1/connections/{cid}/mongo/databases/{db}/collections/{coll}/aggregate
```

Path segments are resolved against metadata fetched through the adapter; names
that do not exist are rejected, never interpolated.

### Conventions

- **Errors:** RFC 9457 `application/problem+json` with a stable machine-readable
  `code`, a `request_id`, and no internal details or credentials.
- **Pagination:** list endpoints use cursor pagination (`cursor`, `limit`).
  Table data uses keyset pagination on the primary key where one exists;
  offset pagination is allowed only on small result sets. Responses include a
  `truncated` flag when limits were applied and `total_estimate` (marked
  approximate) instead of exact `COUNT(*)` on large tables.
- **Row keys:** `row_key` is an opaque, URL-safe, server-validated encoding of
  the primary key (including composite keys). Clients never construct it.
- **Optimistic concurrency:** updates and deletes require the row's version
  (ETag / `If-Match`, or the prior values of the edited columns). A mismatch
  returns `409 Conflict`.
- **Type fidelity:** integers outside the JavaScript safe range (±2^53−1) and
  all decimals are serialized as strings. BSON uses MongoDB Extended JSON
  (canonical). Timestamps are ISO 8601 with
  explicit time zone. Large text/binary values are truncated with a flag and
  fetched on demand.
- **Idempotency:** destructive and bulk operations accept an
  `Idempotency-Key` header.
- **Request IDs:** every response carries `X-Request-ID`, propagated to logs
  and audit records.

All endpoints must:

- Validate input
- Enforce permissions
- Apply execution limits
- Return structured errors
- Avoid leaking internal credentials

### Default execution limits

Configurable per deployment and overridable (downwards) per connection and
role.

| Limit                              | Default |
| ---------------------------------- | ------- |
| Statement timeout                  | 30 s    |
| Default page size                  | 100     |
| Maximum page size                  | 1,000   |
| Maximum rows per query result      | 10,000  |
| Maximum result payload             | 50 MB   |
| Concurrent queries per user        | 5       |
| Concurrent queries per connection  | 20      |
| Pool size per managed connection   | 10      |
| Export (streamed) maximum rows     | 1,000,000 |
| Bulk edit maximum rows             | 10,000  |
| Maximum statement timeout (request)| 300 s   |

## 7. Security Requirements

### Authentication

- Use Argon2id for password hashing
- Server-side sessions stored in the application database, delivered in an
  `HttpOnly`, `Secure`, `SameSite=Lax` cookie with idle and absolute expiry
- CSRF protection on all state-changing requests: a per-session token sent in
  `X-CSRF-Token`, plus `Origin` validation (ADR-0004)
- Protect against brute-force login attempts (per-account and per-IP rate
  limiting, progressive lockout)
- Configurable password policy
- TOTP-based multi-factor authentication; MFA can be enforced per role
- OIDC single sign-on (and SAML/LDAP via ADR when required), with group-to-role
  mapping
- Scoped, expiring API tokens for automation and MCP
- First-run bootstrap creates the initial Super Admin through a one-time setup
  token; there are no default credentials
- Users can list and revoke their sessions; administrators can revoke any

### Authorization

Implement role-based access control.

Initial roles:

- Super Admin
- Database Admin
- Editor
- Viewer
- Custom

Permissions are a matrix of **resource × action**, grantable at connection,
database, and schema level (table-level where practical), and are enforceable
per connection and operation. Application permissions are separate from
database-level privileges. Deny by default; every grant change is audited.

### Credential Storage

Database credentials must be encrypted at rest using envelope encryption: a
per-secret data key encrypted by a key-encryption key (KEK). AES-256-GCM is the
required algorithm.

- Each ciphertext stores the KEK identifier, enabling rotation
- Key sources: environment/file secret, or an external KMS/secret manager
  (e.g. Vault, cloud KMS) through a pluggable provider
- Rotation is an online, audited operation; loss of the KEK is documented as
  unrecoverable
- Encryption keys must be supplied through secure deployment configuration

Managed-database credential models (per connection):

- Shared service account (default)
- Per-user credentials supplied by each user (stored encrypted, scoped to that
  user)

Never:

- Store plaintext passwords
- Return passwords in API responses
- Include credentials in logs
- Hardcode secrets
- Commit production secrets

### Database Query Safety

For relational databases:

- Prefer parameterized queries
- Validate identifiers separately against fetched metadata
- Never assume SQL identifiers can be safely parameterized as values
- Restrict destructive operations
- Apply query timeouts
- **Read-only enforcement happens at the database level**: read-only
  transactions (`SET TRANSACTION READ ONLY` / `START TRANSACTION READ ONLY`)
  and, where available, read-only database accounts. SQL text inspection may be
  used for UX hints and policy warnings but is never the enforcement mechanism.
  Session settings alone are not a boundary: user SQL can switch a PostgreSQL
  read-only transaction back to read-write unless a query has already run, so
  transactions are primed and user-authored SQL has extra requirements
  (ADR-0008)
- Multi-statement input is rejected unless the role explicitly allows scripts

For MongoDB:

- Validate filters and update documents
- Use an allowlist of query, update, and aggregation operators and stages
- Block `$where`, `$function`, `$accumulator`, `$out`, and `$merge` (the last
  two only under an explicit write permission)
- Treat `$lookup`, `$unionWith`, and `$graphLookup` as reads of additional
  collections and authorize each target
- Prevent arbitrary server-side JavaScript execution
- Apply operation limits (`maxTimeMS`, result and memory limits)

### Network Security

- Outbound destinations are governed by an administrator-defined **allowlist**
  of hosts and CIDR ranges. Link-local and cloud metadata addresses (e.g.
  169.254.169.254) are **never** allowed, regardless of allowlist. Loopback is
  denied by default; only a deployment flag (`DBW_NETWORK_ALLOW_LOOPBACK`,
  development and test only, refused in production) can permit it (ADR-0007)
- Resolve DNS once, validate every resolved address, and connect to the pinned
  address (prevents DNS rebinding)
- Re-validate on every new connection and on connection edits
- Protect against SSRF
- Never expose unrestricted host networking
- Document how containerized deployments reach databases on the host or private
  networks

### Web Application Hardening

- TLS terminated at the reverse proxy; HSTS enabled
- Strict CSP (scripts and workers same-origin only; `style-src` allows inline styles solely
  because Monaco requires it, ADR-0006), `X-Content-Type-Options`, `Referrer-Policy`,
  `frame-ancestors 'none'`
- Request size limits and rate limiting on all endpoints
- Safe rendering of all database content (see §5)

### Destructive Operations

Require explicit confirmation for:

- Database deletion
- Table deletion
- Collection deletion
- TRUNCATE
- Bulk DELETE
- Bulk document deletion
- Schema changes
- User and permission changes

Confirmation for production-tagged connections requires typing the target
object's name. Configurable policies can additionally require a second
approver (four-eyes) for selected operations.

### Auditing

- Audit records are append-only and tamper-evident (hash-chained)
- The application database role used by the backend cannot update or delete
  audit rows
- Records are exportable and can be forwarded to syslog/SIEM sinks
- Retention is configurable
- Audit all authentication events, permission changes, administrative actions,
  writes, exports, and query executions; MCP/API-token actions record the
  token and acting identity

### Supply Chain

- Lock files committed; dependencies scanned in CI
- SBOM generated and container images scanned for each release
- Release images are signed

## 8. Data Handling

### Relational Databases

Support:

- Primary keys
- Composite primary keys
- NULL values
- Numeric values
- Dates and timestamps
- JSON and JSONB
- Binary values
- Foreign keys

Updates and deletions must identify records safely.

If a table has no reliable unique row identifier, do not silently perform
ambiguous updates or deletions.

### MongoDB

Support:

- ObjectId
- Nested documents
- Arrays
- BSON dates
- Decimal128
- Binary values
- Null values

Preserve BSON types when reading and writing documents.

### Sensitive Data

- Query history stores statement text, which may contain sensitive literals:
  retention is configurable, history is private to the author by default, and
  administrators can disable literal storage
- Columns/fields can be marked sensitive per connection; their values are masked
  for roles without the "view sensitive data" permission, including in exports
- Exports require a permission, are size-limited, and are audited

## 9. Performance

- Implement server-side pagination
- Avoid loading entire tables into memory
- Use connection pooling where appropriate
- Limit query result sizes
- Apply query timeouts
- Avoid unnecessary database metadata requests (cache metadata with explicit
  invalidation)
- Support cancellation of long-running operations (cancellation state lives in
  the application database so any replica can cancel)
- Stream exports; never buffer them in memory

Reference target: browsing a table with 100M rows must return the first page in
under 1 s (p95) using keyset pagination and estimated counts.

## 10. Testing

### Backend Testing

- Pytest
- Unit tests
- API integration tests
- Adapter tests
- Authentication tests
- Authorization tests
- Security tests (SSRF, injection, authorization bypass, read-only bypass,
  MongoDB operator abuse)

### Frontend Testing

- Vitest
- React Testing Library
- Component tests

Database integration tests should use isolated test containers.

Test PostgreSQL, MySQL, and MongoDB independently, across every supported
version (§3).

Never run destructive tests against production databases.

### Continuous Integration

CI must run lint, type checks, unit, integration (version matrix), security
tests, dependency and image scanning, OpenAPI breaking-change checks, and Alembic
migration checks (upgrade from the previous release).

## 11. Docker Requirements

- Provide development and production configurations
- Use persistent volumes for application data
- Add container health checks (liveness and readiness)
- Use non-root users where possible; read-only root filesystems where possible
- Keep internal services on private networks
- Expose only the intended application entry point
- Pin container versions
- Do not bake secrets into images; support Docker/Compose secrets and
  `*_FILE` environment variables

## 12. Observability and Operations

- Structured JSON logs with request IDs; credentials and row data never logged
- Prometheus metrics endpoint and OpenTelemetry tracing (opt-in)
- `/health/live` and `/health/ready` endpoints (readiness verifies the
  application database and migration state)
- Alembic migrations run explicitly (`make migrate` / a migration job) and are
  checked at startup; the application refuses to start on schema mismatch
- Document backup and restore of the application database, and the upgrade
  procedure between releases

## 13. AI Agent Workflow

Before implementing a feature:

- Read `docs/STATUS.md` to confirm the feature is within the current scope
- Read the relevant sections of PRD.md
- Read relevant sections of AGENTS.md
- Read SKILLS.md when implementing or using defined workflows
- Inspect the affected modules
- Check existing implementation
- Identify database-specific implications
- Implement the smallest appropriate change
- Add automated tests
- Update documentation

Do not implement functionality outside the current development scope
(`docs/STATUS.md`) without authorization. Significant architectural decisions
or deviations require an ADR.

## 14. Conventions

- **Naming:** Python `snake_case` modules and functions, `PascalCase` classes;
  TypeScript `camelCase` values, `PascalCase` components; REST resources are
  plural nouns in `kebab-case`.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`,
  `test:`, `chore:`), small and focused.
- **Branches:** `feat/…`, `fix/…`, `docs/…`; changes land through reviewed pull
  requests.
- **Errors:** raise typed domain errors in services; the API layer maps them to
  problem+json.
- **Migrations:** every schema change ships an Alembic migration with a tested
  upgrade path; never edit a released migration.
- **ADRs:** `docs/adr/NNNN-title.md`, numbered sequentially.

## 15. Definition of Done

A feature is complete when:

- Requirements are implemented
- Backend validation is present
- Authorization is enforced
- Audit events are emitted
- Error handling is implemented
- Automated tests pass (`make lint test test-int test-sec`)
- Database-specific behavior is verified on every supported engine/version it
  touches
- Documentation (and OpenAPI) is updated
- No secrets are exposed
- No unnecessary dependencies are introduced
- `docs/STATUS.md` is updated

## 16. Long-Term Direction

DB Wrench should evolve into a reliable database management platform that can be
operated independently or integrated with AI development environments.

The core must remain maintainable, secure, and extensible.
