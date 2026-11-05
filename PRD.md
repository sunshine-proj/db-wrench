# DB Wrench

Product Requirements Document

Version: 1.1

Status: Initial Specification (revised)

## 1. Product Vision

DB Wrench is a modern, self-hosted, Docker-based, enterprise-grade database
management platform.

It provides a unified interface for managing PostgreSQL, MySQL, and MongoDB
databases, with centralized access control, auditing, and governance.

The application allows users to explore database structures, inspect and modify
data, execute queries, manage database users, and perform administrative
operations.

The application must be suitable for development, staging, and production
environments, including regulated environments that require strong access
control and audit trails.

Future versions integrate with AI coding assistants through MCP.

## 2. Problem Statement

Developers frequently use different tools to manage different database systems.

This creates several problems:

- Multiple applications and interfaces
- Different workflows for different database engines
- Limited integration with AI coding assistants
- Difficulty maintaining shared database connections
- Inconsistent user access management
- Shared database credentials with no per-person accountability
- Fragmented query history, audit, and administration workflows

DB Wrench aims to consolidate these operations into one self-hosted application.

## 3. Target Users

Primary users:

- Software developers
- Database administrators
- DevOps engineers
- Technical leads
- Development teams
- Security and compliance reviewers (audit access)
- Users managing multiple database environments

## 4. Supported Database Systems

Initial supported engines and versions:

| Database   | Versions       | Support              | Priority |
| ---------- | -------------- | -------------------- | -------- |
| PostgreSQL | 14, 15, 16, 17 | Full planned support | Highest  |
| MySQL      | 8.0, 8.4       | Full planned support | High     |
| MongoDB    | 6.0, 7.0, 8.0  | Full planned support | High     |

MariaDB is not treated as MySQL; it is a separate future adapter. Every
supported version is covered by integration tests in CI.

The architecture should allow future adapters.

Potential future databases:

- MariaDB
- Microsoft SQL Server
- SQLite
- Oracle

## 5. Product Modules

### 5.1 Dashboard

Provide an overview of the application.

Features:

- Registered connections
- Connection status and environment tag
- Recently accessed databases
- Recent queries
- Recent activity
- Quick access to database explorers

### 5.2 Connection Manager

Users must be able to configure multiple database connections.

Connection properties:

- Connection name
- Database type
- Environment tag (production, staging, development, other)
- Host
- Port
- Database name
- Credential model (shared service account or per-user credentials)
- Username
- Password
- Authentication method
- TLS configuration (CA, client certificates, verification mode)
- Connection timeout
- Pool limits
- Read-only flag (default: enabled)
- Sensitive column/field markers
- Additional database-specific options

Features:

- Create connections
- Edit connections
- Delete connections
- Test connections
- Enable and disable connections
- Assign users and permissions
- Store credentials securely (envelope encryption, rotatable keys)

Behavior:

- New connections are read-only until write access is explicitly enabled.
- Production-tagged connections show a persistent visual indicator, default to
  read-only, and require typed confirmation for destructive actions.
- Every connection target is validated against the network destination policy
  (§8) on creation, edit, test, and every new physical connection.

Credentials must never be returned to the frontend after initial submission.

### 5.3 Database Explorer

#### PostgreSQL

Support:

- Database navigation
- Schema navigation
- Table listing
- View listing
- Sequence listing
- Function inspection
- Index inspection
- Column definitions
- Primary keys
- Foreign keys
- Constraints

#### MySQL

Support:

- Database navigation
- Table listing
- View listing
- Index inspection
- Column definitions
- Primary keys
- Foreign keys
- Constraints

#### MongoDB

Support:

- Database navigation
- Collection listing
- Collection statistics
- Index inspection
- Document inspection
- Collection metadata

Metadata is cached with explicit invalidation to avoid unnecessary requests
against managed databases.

### 5.4 Data Browser

Users must be able to inspect database contents.

#### Relational Data Browser

Features:

- Server-side keyset/cursor pagination
- Estimated total counts (marked approximate) for large tables
- Sorting
- Filtering
- Column selection
- NULL visualization
- Search
- Record selection
- Export
- Masking of sensitive columns for roles without the "view sensitive data"
  permission
- Safe handling of large values (truncated with on-demand fetch)

#### MongoDB Document Browser

Features:

- Document listing
- JSON document visualization (Extended JSON, type-preserving)
- Nested document expansion
- Array expansion
- Filtering
- Sorting
- Pagination
- Document selection
- JSON export
- Masking of sensitive fields

The UI should distinguish document-oriented operations from relational table
operations.

### 5.5 Data Editor

#### Relational Editing

Support:

- Insert records
- Edit records
- Delete records
- Inline editing
- Dedicated edit forms
- Transaction-aware saving
- Optimistic concurrency (edits fail with a conflict if the row changed)
- Bulk operations with explicit confirmation and affected-row preview

Rows without a reliable unique identifier are read-only.

#### MongoDB Editing

Support:

- Insert documents
- Edit documents
- Delete documents
- JSON editing
- Nested field editing
- Array editing
- Optimistic concurrency (stale-document detection)
- Bulk operations with explicit confirmation and affected-document preview

Data editing must preserve database-specific data types.

### 5.6 SQL Workspace

Applicable to PostgreSQL and MySQL.

Features:

- SQL editor
- Syntax highlighting
- Multiple query tabs
- Query execution
- Query cancellation
- Query history
- Saved queries (private or shared with a group)
- Execution timing
- Result grid
- CSV export
- JSON export
- Error display

Queries must have configurable execution limits and timeouts (defaults in
AGENTS.md §6).

Read-only and write operations must respect application permissions. Read-only
mode is enforced by the database (read-only transactions / read-only accounts),
not by inspecting SQL text. Multi-statement scripts require an explicit
permission.

### 5.7 MongoDB Query Workspace

Features:

- Filter editor
- Projection editor
- Sort editor
- Aggregation pipeline editor
- Query execution
- Query history
- Saved queries
- JSON results

Aggregation pipelines are validated against an operator/stage allowlist.
`$where`, `$function`, `$accumulator`, `$out`, and `$merge` are blocked unless a
write permission explicitly allows `$out`/`$merge`. Stages that read other
collections (`$lookup`, `$unionWith`, `$graphLookup`) require permission on each
target collection.

The application should provide an intuitive alternative to manually constructing
complex MongoDB queries.

### 5.8 Database Administration

#### PostgreSQL Administration

Where the connected database account has sufficient privileges:

- Create databases
- Drop databases
- Create schemas
- Drop schemas
- Inspect indexes
- Create indexes
- Drop indexes

#### MySQL Administration

Where supported by the connected account:

- Create databases
- Drop databases
- Inspect indexes
- Create indexes
- Drop indexes

#### MongoDB Administration

Where supported by the connected account:

- Create databases through supported database operations
- Inspect collections
- Create collections
- Drop collections
- Inspect indexes
- Create indexes
- Drop indexes

#### Extended Administration (separate phase)

User, role, and privilege management is a larger surface and is delivered in
its own phase:

- PostgreSQL: inspect/create/modify roles, grant/revoke permissions
- MySQL: inspect/create/modify users, grant/revoke privileges
- MongoDB: manage database users, roles, and privileges

Not all operations are identical across engines. Unsupported capabilities must
be clearly identified in the UI.

### 5.9 Application User Management

This module manages users of DB Wrench itself.

Features:

- User creation
- User modification
- User deactivation
- Password management and password policy
- Multi-factor authentication (TOTP), enforceable per role
- Single sign-on via OIDC with group-to-role mapping
- Scoped, expiring API tokens
- Role assignment
- Session listing and revocation
- Permission management
- First-run bootstrap through a one-time setup token (no default credentials)

Initial roles:

| Role           | Permissions                               |
| -------------- | ----------------------------------------- |
| Super Admin    | Full application access                   |
| Database Admin | Manage assigned connections and databases |
| Editor         | Read and modify authorized data           |
| Viewer         | Read-only access                          |
| Custom         | Configurable permissions                  |

#### Permission model

Permissions are a matrix of resource × action. Grants apply at connection,
database, and schema scope (table scope where practical).

| Resource            | Actions                                                       |
| ------------------- | ------------------------------------------------------------- |
| Connection          | view, create, edit, delete, test, manage-access               |
| Metadata (explorer) | view                                                          |
| Data                | read, read-sensitive, insert, update, delete, bulk-write      |
| Query               | execute-read, execute-write, execute-script, view-history, manage-saved |
| Export / Import     | export, import                                                |
| Administration      | manage-databases, manage-schemas, manage-indexes, manage-users, manage-privileges |
| Audit               | view, export                                                  |
| Application         | manage-users, manage-roles, manage-settings, manage-tokens    |

Rules: deny by default; the most restrictive of role grant and connection flags
(e.g. read-only) wins; application permissions are separate from database-level
privileges; every grant change is audited.

### 5.10 Audit Logging

Record significant operations.

Events:

- Authentication attempts, MFA events, and session revocations
- Connection creation, modification, and credential rotation
- Query execution
- Data insertion
- Data updates
- Data deletion
- Exports and imports
- Database administration
- Permission and role changes
- API-token and MCP activity (token and acting identity)

Audit records should include:

- Application user (and API token, if used)
- Source IP and request ID
- Connection identifier
- Operation type
- Database object
- Timestamp
- Execution result
- Affected-row counts

Integrity and operations:

- Append-only and tamper-evident (hash-chained); the backend's database role
  cannot update or delete audit rows
- Integrity verification command/endpoint
- Configurable retention
- Export (CSV/JSON) and forwarding to syslog/SIEM sinks

Do not store sensitive row contents, passwords, or secrets in audit logs by
default.

### 5.11 Import and Export

Initial formats:

- CSV
- JSON

Relational databases:

- CSV import
- CSV export
- JSON export

MongoDB:

- JSON import
- JSON export

Import operations must support validation and preview before committing changes.
Exports are streamed, size-limited, permission-gated, mask sensitive data, and
audited.

### 5.12 Governance and Approvals

For regulated environments:

- Environment-based policies (e.g. production is read-only by default)
- Approval workflow (four-eyes): selected operations (such as production schema
  changes, bulk deletes, and privilege changes) require approval from a second
  authorized user before execution
- Policies are configurable per connection and role and are fully audited

Implementation note (Phase 5): policies are configurable per connection and per environment (a
connection policy replaces the environment one); per-role policies remain future work. Approvals are
bound to the requester and to the exact request, expire after 24 hours and work once (ADR-0011).
MongoDB `$out`/`$merge` always require an approval.

## 6. Architecture

Use a modular monolithic backend and a separate frontend application. The
backend is stateless and horizontally scalable; all shared state is in the
application database.

### Backend

Python 3.12+ and FastAPI.

Major modules:

- Authentication
- Connections
- Database core
- Relational adapters
- Document adapters
- Explorer
- Data editor
- Query execution
- Administration
- Audit
- MCP integration

### Frontend

React and TypeScript.

Major modules:

- Dashboard
- Connections
- Database explorer
- Table browser
- Document browser
- Data editor
- SQL workspace
- MongoDB workspace
- Administration
- User management
- Settings

The frontend bundles all assets locally (including Monaco) and works fully
offline / air-gapped.

### Application Database

Use PostgreSQL for application metadata.

It stores application users, connections, permissions, settings, sessions,
running-operation state, and audit information.

Managed database connections remain external.

## 7. Database Abstraction

The system must use separate database capability interfaces. Both interface
families are defined before the first adapter is implemented, although
PostgreSQL ships first.

### Relational Adapter

Initial implementations:

- PostgreSQL
- MySQL

Shared capabilities:

- List databases
- List schemas where supported
- List tables
- Get table structure
- Get table data
- Insert records
- Update records
- Delete records
- Execute SQL
- Inspect indexes
- Cancel running operation

### Document Adapter

Initial implementation:

- MongoDB

Capabilities:

- List databases
- List collections
- Get collection metadata
- Get documents
- Insert documents
- Update documents
- Delete documents
- Execute filters
- Execute aggregation pipelines
- Inspect indexes
- Cancel running operation

Adapters should expose capability information so the frontend can display only
supported operations.

## 8. Security Requirements

Security is a release-blocking requirement.

### Authentication

- Argon2id password hashing
- Server-side sessions in `HttpOnly`, `Secure`, `SameSite` cookies with idle and
  absolute expiry
- CSRF protection (per-session token header plus Origin validation)
- Login rate limiting and progressive lockout
- Password policy
- TOTP multi-factor authentication
- OIDC single sign-on
- Scoped API tokens
- Account deactivation

### Authorization

- Role-based access control (see §5.9 permission model)
- Connection-level permissions
- Database- and schema-level permissions
- Read-only access
- Operation-level permissions
- Deny by default

### Credential Protection

- Envelope encryption (AES-256-GCM) with per-secret data keys
- Key-encryption keys from deployment secrets or an external KMS/secret
  manager (pluggable provider); key identifier stored with each ciphertext
- Online, audited key rotation
- Never expose passwords through APIs
- Never log connection credentials
- Support TLS database connections with certificate verification
- Optional per-user credentials for managed databases

### Query Safety

- Parameterize SQL values
- Validate SQL identifiers against fetched metadata
- Enforce execution timeouts
- Limit returned rows and payload size
- Support query cancellation
- Enforce read-only at the database level (read-only accounts/transactions;
  never by inspecting SQL text alone, see ADR-0008)
- Restrict destructive operations

### MongoDB Safety

- Validate filters and updates
- Operator and stage allowlists
- Limit aggregation execution (`maxTimeMS`, memory and result limits)
- Prevent arbitrary server-side JavaScript
- Protect against NoSQL injection

### Network Safety

- Administrator-defined allowlist of hosts and CIDR ranges
- Always deny link-local and cloud metadata addresses
- Deny loopback by default (a development-only deployment flag can permit it;
  it is refused in production)
- Resolve DNS once, validate all resolved addresses, and connect to the pinned
  address (anti-rebinding)
- Re-validate on every connection and edit
- Prevent unrestricted internal network access

### Web Security

- TLS at the reverse proxy, HSTS
- Strict CSP and security headers
- Safe rendering of all database content
- Request size and rate limits

### Destructive Operations

Require explicit confirmation for destructive actions; typed-name confirmation
on production connections.

For particularly dangerous operations, require additional authorization
(approval workflow, §5.12).

### Sensitive Data

- Sensitive column/field markers with masking by permission
- Query-history retention controls and optional literal redaction
- Permission-gated, audited exports

### Supply Chain

- Locked dependencies, vulnerability scanning in CI
- SBOM for each release
- Signed container images

## 9. Docker Deployment

The application must be deployable using Docker Compose, and must not
preclude orchestrator deployment (stateless backend, health probes, secret
files).

Initial containers:

- Frontend
- Backend
- Application PostgreSQL database
- Reverse proxy

Managed PostgreSQL, MySQL, and MongoDB servers are external connections.

The application must support persistent volumes and configurable networking.

Provide:

- docker-compose.yml (development) and production compose configuration
- .env.example
- Backend Dockerfile
- Frontend Dockerfile
- Liveness and readiness health checks
- Persistent storage configuration
- Secrets support (`*_FILE` variables, Compose secrets)
- Documentation on reaching databases on the host or private networks
- Backup/restore and upgrade documentation for the application database

## 10. API Requirements

Use REST APIs with OpenAPI documentation (versioned under `/api/v1`, with
breaking-change checks in CI).

Resources are addressed by full path (connection → database → schema → object);
there are no bare global object IDs. Full conventions (problem+json errors,
cursor pagination, opaque row keys, optimistic concurrency, type fidelity,
idempotency keys) are defined in AGENTS.md §6.

All database operations must be performed through authenticated backend
endpoints.

The API must support:

- Connection management
- Database exploration
- Data browsing
- Data editing
- Query execution and cancellation
- Database administration
- Application user management
- Audit access
- Approvals

MongoDB-specific operations must have appropriate document-oriented API
contracts.

## 11. MCP Integration

Future versions should expose selected functionality through MCP.

Initial read-only tools:

- database_list_connections
- database_test_connection
- database_list_databases
- database_list_schemas
- database_list_tables
- database_list_collections
- database_get_table_structure
- database_get_collection_structure
- database_get_table_data
- database_get_documents
- database_execute_readonly_query

Write operations should be introduced separately with explicit authorization.

MCP must reuse the same backend authorization and security mechanisms.

Additional requirements:

- Access uses scoped, expiring API tokens bound to a user; effective permission
  is the intersection of the token scope and the user's permissions
- Tighter default row, size, and rate limits than interactive use
- Every call is audited with the token and acting identity
- Returned data is untrusted input to the model (prompt-injection risk):
  responses are clearly delimited as data, and tool descriptions state this
- Sensitive-data masking applies
- Write tools, when introduced, require an approval step by default

## 12. Non-Functional Requirements

### Performance

- Server-side pagination (keyset where possible)
- Configurable result limits (defaults in AGENTS.md §6)
- Connection pooling with per-connection limits
- Query execution timeouts
- Efficient, cached metadata retrieval
- No unnecessary full-table loading
- Reference target: first page of a 100M-row table in under 1 s (p95); schema
  tree load for a 1,000-table schema in under 2 s (p95)

### Reliability

- Persistent application storage
- Graceful connection failures
- Structured error handling
- Recovery after container restarts
- Transaction handling where supported
- Horizontal scalability of the stateless backend

### Observability

- Structured JSON logs with request IDs
- Prometheus metrics and OpenTelemetry tracing
- Liveness and readiness endpoints

### Maintainability

- Modular backend
- Feature-oriented frontend
- Independent database adapters
- Automated testing (including security and version-matrix tests)
- Documented API contracts
- Architecture decision records

### Accessibility and Localization

- WCAG 2.1 AA
- Internationalization-ready UI strings

### Extensibility

The architecture must allow future database engines and integrations without
substantial changes to existing modules.

## 13. Delivery Phases

The phases below are the single source of truth; README.md and docs/STATUS.md
reference them. PostgreSQL ships first; adapter interfaces for the relational
and document families are designed up front.

### Phase 0: Foundation

- Repository, CI, Makefile, linting, type checking
- Docker infrastructure (development and production)
- FastAPI backend and React frontend skeletons
- Application database, Alembic migrations
- Configuration, secrets handling, structured logging, health checks

### Phase 1: Core Platform and PostgreSQL (read-only)

- Application authentication (sessions, CSRF, rate limiting, MFA)
- First-run bootstrap
- Application user management and RBAC permission model
- Audit logging core (hash-chained, append-only)
- Connection management with envelope encryption and network policy
- PostgreSQL connection support and connection testing
- Database explorer, structure inspection
- Data browser: pagination, sorting, basic filtering, read-only

### Phase 2: MySQL and MongoDB (read-only)

- MySQL and MongoDB adapters
- Explorer, structure inspection, and data/document browsing for both
- Collection statistics and metadata

### Phase 3: Data Editor and Export

- Insert, edit, delete for records and documents
- Optimistic concurrency, transactions, bulk-operation previews
- Destructive-operation confirmation
- CSV and JSON export (streamed, masked, audited)
- Sensitive column/field markers and masking

### Phase 4: Query Workspaces

- SQL workspace and MongoDB query workspace
- Query cancellation, history, saved queries
- Aggregation allowlist enforcement
- Query-history retention controls

### Phase 5: Administration, Import, and Governance

- Database, schema, collection, and index administration
- Import with validation and preview
- OIDC single sign-on and group mapping
- API tokens
- Environment policies and approval workflow
- Audit export and SIEM forwarding
- Advanced permissions (schema/table scope)

### Phase 6: Extended Administration

- Database user and role management for PostgreSQL, MySQL, and MongoDB
- Privilege grant and revoke

### Phase 7: MCP and AI Integration

- MCP server with read-only tools
- AI-assisted SQL and MongoDB queries
- Schema documentation
- AI-assisted database analysis
- Controlled write tools through the approval workflow

## 14. Explicitly Out of Scope

Initial releases will not include:

- Database server installation
- Database hosting
- Automated database backups
- Full database replication management
- Distributed query execution
- Database migration orchestration
- Autonomous AI database modifications (AI-initiated writes always require a
  human approval step)
- Multi-tenant SaaS infrastructure
- MariaDB, SQL Server, SQLite, and Oracle adapters

These may be considered later.

## 15. Success Criteria

The product will be considered successful when users can:

- Deploy the application using Docker Compose with no external cloud services
- Configure PostgreSQL, MySQL, and MongoDB connections within the network policy
- Browse databases and their structures
- Inspect large tables and collections efficiently (targets in §12)
- Insert, edit, and delete data safely, with conflict detection
- Execute SQL and MongoDB queries within enforced limits
- Manage application users, SSO, and permissions
- Perform authorized database administration with approvals where required
- Review a tamper-evident history of database operations
- Pass an internal security review with no unresolved critical findings
- Upgrade between releases without data loss

## 16. Future Direction

DB Wrench should evolve into a unified database management environment for
developers and administrators.

The long-term objective is to combine conventional database administration with
safe, controlled AI-assisted database exploration and management.
