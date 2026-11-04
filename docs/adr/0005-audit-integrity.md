# ADR-0005: Audit log integrity

Status: Accepted

## Decision

- `audit_events` is append-only: database triggers reject `UPDATE`, `DELETE` and `TRUNCATE`.
- Each record stores `prev_hash` and `hash = SHA-256(prev_hash || canonical JSON of the record)`.
  Appends are serialised with a Postgres advisory lock so the chain stays linear across replicas.
- Audit writes use their own transaction, so failed and denied operations are still recorded.
- `GET /api/v1/audit/verify` and `python -m app.cli verify-audit` recompute the chain and report
  the first broken record (modified content or a removed link).
- Records hold identifiers, operations, result, counts and filter *columns/operators* only: never
  passwords, secrets, filter values or row contents (tested).

## Consequences

- Triggers stop the application role, not a database superuser. A superuser who disables the
  triggers can still be detected by the chain (tested), but cannot be prevented; operators should
  run the application with a non-superuser role and forward events to an external sink
  (SIEM forwarding is Phase 5).
