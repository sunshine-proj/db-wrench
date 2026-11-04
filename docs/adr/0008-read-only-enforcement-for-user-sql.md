# ADR-0008: Read-only enforcement for user-authored SQL

Status: Accepted and implemented (Phase 2 mitigation, Phase 4 requirements; see ADR-0010).

## Finding

Verified against PostgreSQL 17: in a transaction opened `READ ONLY`, a user statement
`SET TRANSACTION READ WRITE` **succeeds when it is the first statement** and re-enables writes and
non-transactional side effects (`nextval()` etc.). It is refused only after some query has run
("transaction read-write mode must be set before any query"). With no bound parameters, psycopg
also sends the simple-query protocol, which accepts several statements in one call.

Phases 1-2 are not exposed: the adapters only execute SQL the application builds (identifiers
validated and quoted, values bound). The finding matters once users can type SQL.

## Decision

- Done: every PostgreSQL transaction is primed with `SELECT 1`, so the mode switch is refused by the
  server itself (regression test: `test_postgres_read_only_cannot_be_switched_off_by_sql`).
- Phase 4 requirements for the SQL workspace:
  1. prefer a **read-only database account / role** for read-only execution; session settings
     alone are not a security boundary;
  2. execute exactly one statement per call; run with **no bound parameters** only through a path
     that rejects multiple statements;
  3. MySQL: execute inside an explicit `START TRANSACTION READ ONLY` (characteristics cannot be
     changed in a transaction) on a **fresh or reset connection**, because
     `SET SESSION transaction_read_only = 0` on a pooled connection would persist; add a test;
  4. statement inspection (reject `SET`, `BEGIN`, `DO`, `CALL`...) is allowed as a UX hint and
     defence in depth, never as the enforcement mechanism;
  5. statement timeout, result/row limits and cancellation stay mandatory (AGENTS.md section 9).

## Phase 4 outcome

All five requirements are implemented and tested: a read-only DB role remains the recommended
strongest control; execution is one statement per call (scripts need a permission); MySQL runs
inside an explicit `START TRANSACTION READ ONLY` on a fresh connection (flipping
`transaction_read_only` from inside it does not enable writes: tested); the classifier is policy
only; timeouts, limits and cancellation are in place.
