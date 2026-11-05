# ADR-0001: Record architecture decisions

Status: Accepted

## Context

AGENTS.md requires documented justification for significant architectural
decisions and deviations.

## Decision

Architecture decisions are recorded as numbered Markdown files in `docs/adr/`
using the sections Status, Context, Decision, and Consequences.

## Consequences

- Deviations from AGENTS.md need an ADR before implementation.
- Decision records:
  - ADR-0002: Concurrency model
  - ADR-0003: Credential envelope encryption
  - ADR-0004: Authentication and sessions
  - ADR-0005: Audit log integrity
  - ADR-0006: Frontend stack choices
  - ADR-0007: Outbound network policy and address pinning
  - ADR-0008: Read-only enforcement for user-authored SQL
  - ADR-0009: Sensitive-field masking
  - ADR-0010: Query execution, operations and cancellation
  - ADR-0011: Administration, environment policies and four-eyes approvals
  - ADR-0012: OIDC single sign-on and group-to-role mapping
  - ADR-0013: API tokens and grants below connection level
  - ADR-0014: Import with preview, and per-user managed-database credentials
  - ADR-0015: Audit export, SIEM forwarding and OpenTelemetry tracing
  - ADR-0016: Database users, roles and privileges
