# ADR-0011: Administration, environment policies and four-eyes approvals

Status: Accepted

## Context

Phase 5 adds structural administration (databases, schemas, collections, indexes) and the
governance PRD 5.12 asks for: selected operations need a second person's approval. Approvals
must not become a way to run something other than what was reviewed.

## Decision

- **Administration** runs on a dedicated, non-pooled, autocommit connection (`CREATE/DROP
  DATABASE` cannot run in a transaction on PostgreSQL; MySQL DDL commits implicitly). Names of
  *new* objects must match a conservative pattern and are dialect-quoted; names of *existing*
  objects must appear in fetched metadata. System objects and the connection's own database cannot
  be dropped; drops use the restrictive form (`RESTRICT`, no `CASCADE`). Dropping a database,
  schema or collection always requires the object's name to be typed back (in every environment);
  index drops follow the production rule.
- **Policies** list operation keys that need approval, per connection or per environment (a
  connection policy replaces the environment one). Gated: the administration operations, bulk
  delete/update, import commit. **`query.mongo_write_stage`** (`$out`/`$merge`) is *always*
  approved, whatever the policy.
- **Workflow.** A gated request without approval stores an `approvals` row (in its own
  transaction, so it survives the rejected request) and answers `428 approval_required` with the
  approval id. A user holding `governance.approve` on that connection, other than the requester,
  approves or rejects. The requester then repeats the **same** request with `Approval-Id`.
- **Binding.** The approval is bound to the requester, the connection, the operation and a digest
  of *every* parameter including the target (an early version omitted the target name; a test
  caught that an approval for one database name authorised another). It expires (24 h) and is
  consumed by an atomic `UPDATE ... WHERE status = 'approved' ...` **inside the request's
  transaction**: a failed execution rolls the consumption back, a successful one cannot be
  replayed (concurrent replays serialise on the row lock).
- **MongoDB write stages** are validated separately: only one trailing `$out`/`$merge`, only into
  a collection of the queried database, `$merge` options from an allowlist (no pipeline-valued
  `whenMatched`, no `let`). They need `query.execute_write` on a writable connection, the target
  name typed on production, and an approval.

## Consequences

- With a single administrator, approval-gated operations cannot run: that is the point of
  four-eyes. Policies are opt-in, except for write stages.
- An approval covers one exact request; changing any parameter needs a new approval. Bulk
  operations additionally re-preview after approval (the preview token lives 10 minutes).
- `approval.request|approve|reject|cancel|consume` and `governance.policy.update` are audited.
