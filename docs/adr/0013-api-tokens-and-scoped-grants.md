# ADR-0013: API tokens and grants below connection level

Status: Accepted

## API tokens

- `dbw_` + 256 random bits, shown once, stored as SHA-256. They expire (1-365 days), can be revoked,
  and stop working immediately when the owner is deactivated or must change their password.
- **Never more powerful than the owner:** effective permissions on a connection are the
  *intersection* of the token's scopes and the owner's own rights (read-only connections included),
  optionally limited to chosen connections.
- Scopes are connection-scope permissions only; `connection.edit`, `connection.delete`,
  `connection.manage_access` and `governance.approve` are not available (a human approves). Tokens
  hold no application permissions.
- **Surface:** bearer tokens are accepted only below `/api/v1/connections` and `/api/v1/approvals`;
  every other endpoint (users, roles, settings, tokens, audit, auth) is session-only. No cookie is
  involved, so no CSRF token is needed. Audit records of token requests carry `api_token_id`.
- The MCP phase will use the same tokens.

## Scoped grants

- A `scope_grants` row gives a user a role on a database, a schema (PostgreSQL) or a
  table/collection. Scoped permissions **add** to connection-level ones; deny by default.
- Only object-level permissions are scopable (metadata, data, export/import, schema and index
  administration). Free-form SQL/aggregation, database creation/removal, access management and
  governance stay connection-level because they cannot be confined to an object.
- Resolution: a grant covers a target when it is at least as broad (a table grant never authorises a
  schema-wide action). When *listing* a container, any grant inside it makes it visible and the
  listing is filtered to what the grants cover. A user with only scoped grants sees the connection
  (implied `connection.view`) and nothing else.
- Every object-addressed route passes its target to the access check. A route that forgets only
  denies scoped users, never over-grants. Granters cannot hand out more than they hold; read-only
  connections strip scoped write permissions too.
