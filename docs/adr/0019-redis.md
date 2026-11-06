# ADR-0019: Redis and Valkey (key-value family)

Status: Accepted (extends ADR-0017)

## Context

Redis is neither relational nor a document store, so it gets its own adapter family
(`keyvalue`, AGENTS.md 2.2) and its own API surface (`/api/v1/connections/{cid}/redis/...`).
The same code serves Valkey. Verified against Redis 6.2 and 7.4 and Valkey 8.

## Decision

- **Contract first.** `KeyValueAdapter` (databases, `scan_keys`, `get_key`, `put_key`, `delete_key`,
  `set_ttl`, `run_command`) is defined before the adapter, with its own models. `family_of(db_type)`
  now decides which endpoints may open an adapter; the relational and MongoDB routes refuse a Redis
  connection and vice versa.
- **Standalone servers only.** No Cluster, Sentinel or replica discovery: following addresses the
  server advertises would bypass the network policy (same reasoning as MongoDB, ADR-0007). Every
  new TCP connection re-resolves and re-validates the destination and connects to the validated
  address (`verify-full` connects by name, like MySQL). One bounded pool per logical database.
- **Addressing.** Logical databases are addressed by index and validated against the server's
  `databases` setting. A key is addressed by an opaque, URL-safe id of its raw bytes (binary keys
  work); the server checks it exists. Patterns are passed as arguments, never interpolated.
- **Browsing.** `SCAN` with `MATCH`/`TYPE`, a bounded number of rounds per page (a sparse pattern
  cannot pin the server), duplicates removed, `DBSIZE` as the estimate. Values are read per type
  with cursor paging (`HSCAN`, `SSCAN`, `LRANGE`, `ZRANGE`, `XRANGE`); text that is not UTF-8
  becomes a binary marker; everything is rendered as text.
- **Editing.** Create or replace a whole `string`/`hash`/`list`/`set`/`zset` value with optimistic
  concurrency: the version is the SHA-256 of `DUMP`, checked under `WATCH`/`MULTI` (a concurrent
  change is a 409). Replace keeps the TTL unless told otherwise and cannot change the type. Limits:
  1,000 elements, 1 MiB per string; streams, binary values and larger values are read-only.
  Delete uses the same version check and typed confirmation on production connections.
- **Read-only enforcement (the honest version).** Redis has no read-only session. Layers: the
  adapter refuses writes on read-only connections; the console only runs **allowlisted** commands
  (no scripting, `CONFIG`, `DEBUG`, `FLUSH*`, `KEYS`, blocking commands, replication, `ACL`,
  `SELECT`, `MIGRATE`, `RESTORE`, `COPY`...) with sub-command allowlists (`OBJECT ENCODING|FREQ|...`,
  `MEMORY USAGE`) and `STORE`/`BLOCK` options refused; and for every command the **server's own
  `COMMAND INFO` flags** must agree (a "read" command the server flags as `write`/`admin`/`blocking`
  is refused, a "write" command must be flagged `write`). If the account may not call `COMMAND`,
  only the allowlist applies. The strongest control is an **ACL-limited account** (`+@read`):
  its refusal (`NOPERM`) is surfaced as a normal query error, and a test proves it holds.
- **Permissions and audit** reuse the existing matrix: `data.read` for keys, `data.insert|update|
  delete` for the key editor, `query.execute_read|write` for the console; destructive commands
  (`DEL`, `UNLINK`, overwriting `SET`, ...) need an explicit confirmation, production needs the
  connection name typed. Audit records command name and argument count, never keys or values.

## Consequences

- No query history, saved commands, cancellation, import/export or sensitive-field masking for Redis
  yet; connections that rely on masking should not be pointed at Redis.
- Multi-member edits (adding one hash field) go through the console or a whole-value replace.
- Approval policies (four-eyes) do not cover Redis operations yet.
