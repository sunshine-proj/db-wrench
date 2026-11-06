# DB Wrench

A modern, self-hosted, Docker-based, enterprise-grade database management
platform for PostgreSQL, MySQL, and MongoDB.

DB Wrench provides a unified web interface for exploring databases, browsing and
editing data, executing queries, managing database structures, and administering
database users, with centralized access control and auditing.

The application is designed to be lightweight, extensible, secure, and suitable
for both development and production database administration.

## 1. Vision

Build a universal, self-hosted database management application that provides a
modern alternative to traditional database administration tools.

The application should allow developers and administrators to manage multiple
database systems through one interface.

The architecture should also support future integration with AI coding
assistants through MCP (Model Context Protocol).

## 2. Supported Databases

Initial database support:

| Database   | Versions       | Priority | Query Interface   |
| ---------- | -------------- | -------- | ----------------- |
| PostgreSQL | 14 – 17        | Primary  | SQL               |
| MySQL      | 8.0, 8.4       | Primary  | SQL               |
| MariaDB    | 10.11, 11.4    | Primary  | SQL               |
| MongoDB    | 6.0, 7.0, 8.0  | Primary  | MongoDB Query API |

The architecture must allow additional database engines to be introduced through
independent adapters. MariaDB has its own adapter (ADR-0017).

## 3. Main Features

The sections below describe the full product; what is implemented today is tracked in
[docs/STATUS.md](docs/STATUS.md) and the roadmap in section 9.

### Database Explorer

- Manage multiple database connections, tagged by environment
- Browse database servers
- Browse databases and schemas
- Browse tables, views, and collections
- Inspect table and collection structures
- View indexes and relationships where supported
- Monitor connection status

### Data Management

- Browse records and documents
- Insert records
- Edit records (with conflict detection)
- Delete records
- Filter and sort data
- Search across selected fields
- Paginate large datasets
- Export data to CSV and JSON
- Import supported data formats with preview
- Mask sensitive columns and fields

### SQL and Query Workspace

For PostgreSQL and MySQL:

- SQL editor
- Syntax highlighting
- Query execution and cancellation
- Query history
- Saved queries
- Query results grid
- Execution timing
- CSV and JSON export

For MongoDB:

- Document query editor
- Filter and aggregation pipelines
- Query history
- JSON document editing
- Results inspection

### Database Administration

- Create and delete databases
- Manage schemas where supported
- Inspect indexes
- Create and manage indexes
- Manage database users and roles
- Inspect permissions
- Monitor connection information (status, server version, active sessions)

Database-specific capabilities will be exposed according to the underlying
database engine.

### Application User Management and Governance

- Application authentication with MFA and OIDC single sign-on
- User management
- Role-based access control
- Connection-level permissions
- Read-only access
- Environment policies and approval workflow for risky operations
- Tamper-evident audit logging with export and SIEM forwarding

### Future AI Integration

The application will provide an MCP interface for AI coding assistants.

Potential capabilities:

- Database schema discovery
- Read-only data inspection
- SQL generation
- Query execution
- Database documentation
- Data analysis
- Controlled database modifications (always through an approval step)

AI access must follow the same authentication and authorization rules as normal
application access.

## 4. Technology Stack

| Layer                | Technology                                   |
| -------------------- | -------------------------------------------- |
| Backend              | Python 3.12+                                 |
| Backend framework    | FastAPI                                      |
| Frontend             | React                                        |
| Frontend language    | TypeScript                                   |
| UI components        | shadcn/ui                                    |
| Styling              | Tailwind CSS                                 |
| Data grid            | TanStack Table (virtualized)                 |
| SQL editor           | Monaco Editor (bundled locally)              |
| SQL abstraction      | SQLAlchemy 2.x                               |
| MongoDB driver       | PyMongo                                      |
| Data validation      | Pydantic                                     |
| Application database | PostgreSQL                                   |
| Authentication       | Server-side sessions, MFA (TOTP), OIDC       |
| API                  | REST / OpenAPI                               |
| Containerization     | Docker                                       |
| Deployment           | Docker Compose                               |
| Testing              | Pytest / Vitest / testcontainers             |
| Tooling              | uv, ruff, mypy, pnpm, eslint, prettier       |

## 5. Architecture

DB Wrench follows a modular monolithic architecture with a stateless backend
that can be scaled horizontally.

```text
WEB BROWSER
      |
      v
REVERSE PROXY (TLS)
      |
      v
REACT FRONTEND
      |
      v
REST API
      |
      v
FASTAPI BACKEND
      |
      +----------------------+----------------------+----------------------+
      |                      |                      |                      |
      v                      v                      v                      v
 AUTH MODULE          DATABASE CORE           AUDIT MODULE           MCP MODULE
                            |
                            v
                 CONNECTION MANAGER
                 (network policy, pools)
                            |
          +-----------------+-----------------+
          |                 |                 |
          v                 v                 v
      POSTGRESQL         MYSQL             MONGODB
          |                 |                 |
          +-----------------+-----------------+
                            |
                    DATABASE SERVICES
                            |
          +-----------------+-----------------+
          |                 |                 |
          v                 v                 v
     DATA BROWSER      QUERY ENGINE     ADMINISTRATION
```

The application has its own internal PostgreSQL database for storing:

- Application users
- Roles and permissions
- Database connection configurations
- Encrypted connection credentials
- User preferences
- Saved queries
- Query history
- Audit logs
- Sessions and running-operation state

Managed databases remain external systems.

## 6. Project Structure

```text
db-wrench/
├── README.md  AGENTS.md  CLAUDE.md  PRD.md  SKILLS.md  SECURITY.md  CONTRIBUTING.md
├── Makefile
├── docker-compose.yml            default stack (evaluation / development)
├── docker-compose.prod.yml       production overlay (TLS, secrets, limits)
├── .env.example
├── backend/
│   ├── Dockerfile  pyproject.toml  uv.lock  alembic.ini
│   ├── migrations/               Alembic (application database)
│   ├── src/app/
│   │   ├── main.py  config.py  db.py  models.py  errors.py  middleware.py  observability.py  cli.py
│   │   ├── api/                  routers + request/response schemas
│   │   ├── auth/                 passwords, sessions, MFA, permissions, users and roles
│   │   ├── connections/          crypto (envelope encryption), network policy, service
│   │   ├── database/
│   │   │   ├── core/             adapter contracts, manager/pools, serialization
│   │   │   ├── relational/       shared relational models and safe SQL builder
│   │   │   ├── document/         document models, MongoDB filter allowlist
│   │   │   └── adapters/         postgres.py  mysql.py  mongodb.py
│   │   ├── explorer/  audit/  services/
│   │   └── editor/ query/ importing/ governance/ tracing.py   (mcp/ is reserved)
│   └── tests/                    unit/  integration/ (testcontainers)  security
├── frontend/
│   ├── Dockerfile  nginx.conf  package.json  pnpm-lock.yaml
│   └── src/
│       ├── components/  hooks/  services/  types/  lib/  test/
│       └── features/
│           ├── authentication/  dashboard/  connections/  explorer/  data-browser/
│           └── administration/  audit/
├── deployment/nginx/             reverse proxy configuration (HTTP and TLS profiles)
├── scripts/                      init-env.sh  init-secrets.sh  export_openapi.py
├── docs/                         STATUS.md  operations.md  openapi.json  adr/
└── .github/workflows/            ci.yml  release.yml
```

## 7. Running the Application

### Prerequisites

- Docker with Compose v2
- A free local port (8080 by default)

### Start

```bash
./scripts/init-env.sh                 # creates .env with generated secrets (mode 600)
docker compose up -d --build
docker compose ps                     # wait until every service is healthy
docker compose logs backend | grep "setup token"
```

Open <http://localhost:8080>, enter the one-time **setup token** from the log and create the
first administrator (there are no default credentials). Then add a connection and open the
explorer. Back up `DBW_ENCRYPTION_KEYS` from `.env`: without it, stored connection credentials
cannot be decrypted.

```bash
docker compose logs -f     # follow logs
docker compose down        # stop (data is kept in the dbdata volume; add -v to delete it)
```

Application data persists between restarts through Docker volumes. The stack listens on
`127.0.0.1` only; change `DBW_BIND` deliberately.

### Production

```bash
./scripts/init-secrets.sh             # then place secrets/tls_cert.pem and secrets/tls_key.pem
DBW_VERSION=1.0.0 docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

See [docs/operations.md](docs/operations.md) for TLS, secrets and key rotation, backup/restore,
upgrades, monitoring and how to reach databases on private networks or the Docker host.

### Connecting to databases

Managed databases are reached through the outbound **network policy**: private ranges are allowed
by default, anything else must be allowlisted (Network & keys page). Link-local and cloud metadata
addresses are always blocked. To reach a database on the Docker host use `host.docker.internal`.

### Development

Tooling: `uv` (Python), `pnpm` (frontend), Docker (integration tests use testcontainers).

```bash
make setup      # install dependencies
make lint       # ruff, mypy, tsc, eslint, prettier
make test       # backend unit tests + frontend tests (no containers)
make test-int   # integration tests against real PostgreSQL, MySQL and MongoDB containers
make test-sec   # security regression suite
make dev-backend / make dev-frontend   # run locally (see .env.example for DBW_* variables)
make openapi    # regenerate docs/openapi.json after an API change
```

The versions under test are overridable (`DBW_TEST_PG_IMAGE`, `DBW_TEST_MYSQL_IMAGE`,
`DBW_TEST_MONGO_IMAGE`); CI runs every supported version. Interactive API docs are served at
`/api/docs` when `DBW_ENV` is not `production`.

## 8. Security

Security is a core requirement. See `SECURITY.md` for reporting vulnerabilities.

The application must implement:

- Secure authentication (Argon2id, MFA, OIDC)
- Role-based access control
- Envelope-encrypted database credentials with rotatable keys
- TLS support for remote connections
- Connection timeouts
- Query execution limits
- Tamper-evident audit logging
- Read-only access mode, enforced by the database
- Protection against SQL injection
- Protection against NoSQL injection
- Network destination allowlist and SSRF protection
- Explicit confirmation for destructive operations
- Signed releases with SBOMs

Database credentials must never be returned through frontend API responses.

Arbitrary database connections must not be allowed to bypass network security
restrictions.

## 9. Development Roadmap

The authoritative phase definitions are in `PRD.md` §13. The active phase is
tracked in `docs/STATUS.md`.

| Phase | Name                                      | Status      | Highlights                                                              |
| ----- | ----------------------------------------- | ----------- | ----------------------------------------------------------------------- |
| 0     | Foundation                                | Done        | CI, Docker, skeletons, app DB, migrations, observability baseline       |
| 1     | Core platform and PostgreSQL (read-only)  | Done        | Auth, MFA, RBAC, audit core, connections, explorer, data browser        |
| 2     | MySQL and MongoDB (read-only)             | Done        | Adapters, explorer, document browser                                    |
| 3     | Data editor and export                    | Done        | Insert/update/delete, concurrency control, bulk ops, export, masking    |
| 4     | Query workspaces                          | Done        | SQL and MongoDB workspaces, history, saved queries, cancellation        |
| 5     | Administration, import, governance        | Done        | DB/schema/index admin, import, OIDC, API tokens, approvals, SIEM export |
| 6     | Extended administration                   | Done        | Database users, roles, and privileges                                  |
| 7     | MCP and AI integration                    | Planned     | MCP server, read-only tools, AI-assisted queries and analysis           |

Detailed status, deferred items and known limits: [docs/STATUS.md](docs/STATUS.md).
Architecture decisions: [docs/adr/](docs/adr/).

## 10. Development Principles

- Keep the application modular
- Separate database-specific implementations
- Never force MongoDB into a relational abstraction
- Keep database operations in the backend
- Never expose database credentials to the frontend
- Use pagination for large datasets
- Prefer transactions for supported write operations
- Keep application authentication separate from managed database authentication
- Enforce safety (read-only, limits) in the database and backend, never only in
  the UI
- Add tests for every significant feature
- Record architectural decisions as ADRs
- Preserve backward compatibility where practical
