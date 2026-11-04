# DB Wrench — Agent Skills Specification

Version: 1.1

## 1. Purpose

This document defines reusable skills for AI coding agents working on the DB
Wrench project.

Skills establish standardized development workflows and ensure consistent
implementation across different AI coding assistants.

The primary supported environments are:

- Claude Code
- GitHub Copilot
- OpenAI Codex

Skills are implemented as provider-compatible `SKILL.md` files.

## 2. General Skill Principles

Every skill must:

- Have a clearly defined responsibility
- Follow AGENTS.md
- Respect PRD.md and stay within `docs/STATUS.md`
- Reuse existing application services
- Avoid duplicating business logic
- Include appropriate validation
- Consider security implications
- Produce predictable results

Skills must not bypass application authorization or database security
restrictions.

## 3. Development Skills

Each skill is a `SKILL.md` with YAML frontmatter containing `name` and a
trigger-oriented `description` (what the skill does and when to use it), so that
agents can select it automatically.

### Skill 1: architecture-analysis

Purpose:

Analyze the existing application architecture before implementing substantial
changes.

Workflow:

- Check `docs/STATUS.md` and relevant ADRs
- Inspect project structure
- Review relevant documentation
- Identify affected modules
- Identify existing implementations
- Identify dependencies
- Identify database-specific implications
- Propose an implementation approach (and an ADR if it deviates from AGENTS.md)

Output:

- Architecture summary
- Affected modules
- Dependencies
- Potential risks
- Recommended implementation

### Skill 2: backend-development

Purpose:

Implement backend functionality using FastAPI.

Workflow:

- Review the requirements
- Identify the relevant backend module
- Inspect existing service and API patterns
- Define request and response models
- Implement business logic
- Implement API endpoints (full-path resource addressing, problem+json errors)
- Add authorization
- Emit audit events
- Add automated tests
- Update API documentation

Requirements:

- Use type hints
- Use Pydantic validation
- Keep endpoints focused
- Separate business logic from HTTP handling
- Handle database errors explicitly

### Skill 3: frontend-development

Purpose:

Implement frontend functionality using React and TypeScript.

Workflow:

- Review the relevant feature requirements
- Inspect existing components
- Follow established UI conventions
- Implement reusable components
- Integrate backend APIs
- Handle loading and error states
- Add frontend tests
- Verify responsive behavior and accessibility

Requirements:

- Use TypeScript
- Avoid unnecessary component duplication
- Keep API access in service modules
- Validate user input
- Render database content as text only
- Show environment indicators on data-bearing views
- Provide clear feedback for destructive operations

### Skill 4: database-adapter-development

Purpose:

Implement or extend a database adapter.

Workflow:

- Identify the database engine
- Review the adapter interface
- Identify supported database capabilities
- Implement connection management (including network policy checks)
- Implement metadata inspection
- Implement data access
- Implement cancellation
- Implement database-specific operations
- Add integration tests for every supported version
- Document unsupported capabilities

Important:

- PostgreSQL and MySQL may share relational abstractions
- MongoDB must use a separate document-oriented interface
- Never force incompatible database functionality into a shared abstraction

### Skill 5: database-explorer

Purpose:

Implement database navigation and metadata inspection.

Workflow:

- Identify the selected connection
- Retrieve available databases
- Retrieve schemas or collections
- Retrieve tables or collection metadata
- Retrieve structure information
- Display results in the frontend
- Handle permissions and connection failures

Output:

- Database navigation
- Object metadata
- Structure information

### Skill 6: data-editor

Purpose:

Implement safe database data editing.

Workflow:

- Identify the target database object
- Inspect its structure
- Identify a reliable record or document identifier
- Validate submitted values
- Check operation permissions and connection read-only flag
- Verify the expected version (optimistic concurrency)
- Execute the requested operation
- Apply transaction handling where supported
- Record an audit event
- Return execution results

Important:

- Never perform ambiguous relational updates or deletes when a reliable row
  identifier is unavailable
- Preserve MongoDB BSON types
- Preserve 64-bit integer and decimal precision across the API

### Skill 7: sql-workspace

Purpose:

Implement SQL editor and query execution functionality.

Workflow:

- Identify the active connection
- Validate user permissions
- Validate execution settings
- Execute the SQL query (read-only transaction when the mode is read-only)
- Apply execution timeouts
- Limit result sizes
- Support cancellation
- Return structured results
- Record query history and audit information

Security:

- Prefer parameterized queries
- Validate identifiers
- Apply query limits
- Enforce read-only at the database level, never by SQL text inspection alone
- Protect against unauthorized destructive operations

### Skill 8: mongodb-workspace

Purpose:

Implement MongoDB query and aggregation functionality.

Workflow:

- Identify the active MongoDB connection
- Identify the database and collection
- Validate filters and projections against the operator allowlist
- Validate aggregation stages and authorize every referenced collection
- Validate sorting and pagination
- Execute the requested operation with `maxTimeMS`
- Preserve BSON types
- Return structured document results
- Record relevant execution information

Support:

- Filters
- Projections
- Sorting
- Aggregation pipelines
- Document inspection

### Skill 9: security-review

Purpose:

Review application changes for security vulnerabilities.

Review areas:

- Authentication
- Authorization
- Credential storage and key handling
- SQL injection
- NoSQL injection
- SSRF and network policy
- CSRF
- XSS
- Session security
- Query execution restrictions and read-only enforcement
- Audit logging and integrity
- Sensitive information exposure (masking, history, exports)
- Supply chain (dependencies, images)

Output:

A prioritized report containing:

- Finding
- Severity
- Affected component
- Explanation
- Recommended correction

Critical findings must be addressed before release.

### Skill 10: docker-deployment

Purpose:

Implement and validate Docker deployment functionality.

Workflow:

- Inspect existing Docker configuration
- Review service dependencies
- Configure container networking
- Configure persistent volumes
- Configure environment variables and secrets (`*_FILE`)
- Add liveness and readiness health checks
- Verify startup dependencies and migration handling
- Test container restart behavior
- Verify deployment documentation

Never embed production credentials in Docker images or committed configuration
files.

### Skill 11: test-and-validation

Purpose:

Execute project validation and automated tests.

Workflow:

- Identify affected modules
- Run `make lint`
- Run `make test`
- Run `make test-int` for affected engines/versions
- Run `make test-sec`
- Collect failures
- Generate a validation summary

Use isolated test databases and containers.

Never run destructive tests against production systems.

### Skill 12: documentation

Purpose:

Create and maintain technical documentation.

Documentation may include:

- README.md
- API documentation
- Architecture documentation and ADRs
- Deployment, backup, and upgrade instructions
- Configuration references
- Database adapter documentation
- Security guidelines
- `docs/STATUS.md`

Documentation must reflect actual implemented behavior.

### Skill 13: database-migration-app

Purpose:

Create and verify Alembic migrations for the application database.

Workflow:

- Modify the SQLAlchemy models
- Generate and review the migration
- Verify upgrade from the previous release schema
- Verify downgrade where supported
- Check effects on audit tables (append-only guarantees)
- Update documentation

Rules:

- Never edit a released migration
- Migrations must be safe to run with multiple replicas (run once via the
  migration job)

### Skill 14: api-endpoint-checklist

Purpose:

Ensure every new endpoint meets the platform contract.

Checklist:

- Full-path resource addressing resolved against adapter metadata
- Input validation
- Authentication and permission check
- Connection flags (read-only, enabled) and environment policy
- Execution limits and pagination
- Problem+json errors without internal details
- Audit event
- Idempotency key and optimistic concurrency where applicable
- OpenAPI schema updated; no unintended breaking change
- Tests: success, validation failure, authorization failure, audit emission

### Skill 15: release

Purpose:

Prepare and verify a release.

Workflow:

- Confirm all phase exit criteria in `docs/STATUS.md`
- Run the full CI matrix and security suite
- Generate changelog and SBOM
- Scan and sign container images
- Verify upgrade from the previous release
- Verify documentation

## 4. Operational Skills (Product Behavior)

These describe workflows the application will eventually expose to users and AI
agents through MCP. They are product specifications, not development workflows;
they are implemented in Phase 7 (PRD §11) and do not require `SKILL.md` files
until then. All operational skills run through the same authorization and audit
paths as the UI, and treat returned data as untrusted.

### Operational 1: database-inspection

Purpose:

Inspect database structures and metadata.

Workflow:

- Select an authorized connection
- Inspect available databases
- Inspect schemas or collections
- Inspect tables or document structures
- Retrieve indexes and constraints where supported
- Produce a structured summary

Default access mode: Read-only.

### Operational 2: database-query-analysis

Purpose:

Analyze a SQL or MongoDB query.

Workflow:

- Identify database engine
- Parse or validate query structure where possible
- Identify referenced objects
- Identify potentially expensive operations
- Identify security concerns
- Provide an explanation

Do not execute queries automatically as part of analysis.

### Operational 3: database-data-analysis

Purpose:

Analyze selected database data.

Workflow:

- Identify the requested analysis
- Select authorized data sources
- Retrieve only necessary fields (sensitive fields masked)
- Apply result limits
- Analyze returned data
- Generate a structured report

Default access mode: Read-only.

### Operational 4: database-administration

Purpose:

Perform authorized database administration.

Operations may include:

- Database creation
- Database deletion
- Schema management
- Collection management
- Index management
- User management
- Permission management

Requirements:

- Verify permissions
- Check database capabilities
- Require explicit confirmation for destructive operations
- Route policy-designated operations through the approval workflow
- Record audit events
- Return execution results

### Operational 5: database-documentation

Purpose:

Generate technical documentation from database metadata.

Workflow:

- Inspect schemas or collections
- Retrieve table and field definitions
- Identify relationships and indexes
- Generate documentation
- Export Markdown or JSON

Avoid retrieving unnecessary production data.

## 5. Future Skills

Potential future skills:

| Skill                | Purpose                                       |
| -------------------- | --------------------------------------------- |
| database-migration   | Manage managed-database migrations            |
| query-optimization   | Analyze and improve query performance         |
| index-recommendation | Suggest useful indexes                        |
| schema-comparison    | Compare database structures                   |
| data-quality         | Detect inconsistencies and anomalies          |
| backup-management    | Manage backup operations                      |
| database-monitoring  | Monitor database performance                  |
| mcp-integration      | Expose database tools through MCP             |
| ai-sql-assistant     | Generate SQL with contextual schema knowledge |
| ai-mongodb-assistant | Generate MongoDB queries and pipelines        |

## 6. Skill Directory Structure

`skills/` is the single source of truth. Provider-specific locations are thin
links or generated copies created by `scripts/sync-skills` (for example
`.claude/skills/`, `.github/`, and the Codex equivalent); never edit the copies.

```text
skills/
    architecture-analysis/SKILL.md
    backend-development/SKILL.md
    frontend-development/SKILL.md
    database-adapter-development/SKILL.md
    database-explorer/SKILL.md
    data-editor/SKILL.md
    sql-workspace/SKILL.md
    mongodb-workspace/SKILL.md
    security-review/SKILL.md
    docker-deployment/SKILL.md
    test-and-validation/SKILL.md
    documentation/SKILL.md
    database-migration-app/SKILL.md
    api-endpoint-checklist/SKILL.md
    release/SKILL.md
```

Each file starts with:

```yaml
---
name: <skill-name>
description: <what it does and when to use it>
---
```

## 7. Standard Skill Output

Development skills that produce a report (architecture-analysis,
security-review, test-and-validation) should provide:

- OBJECTIVE
- ANALYSIS
- IMPLEMENTATION
- SECURITY
- TESTING
- FILES
- REMAINING ISSUES

Other development skills should end with a short summary of changes, tests run,
and remaining issues.

Operational skills should provide:

- OPERATION
- CONNECTION
- OBJECTS
- RESULTS
- WARNINGS
- AUDIT

## 8. Skill Development Rules

- Keep skills focused
- Avoid duplicating shared instructions
- Reference AGENTS.md for general development rules
- Keep descriptions specific so skills trigger reliably
