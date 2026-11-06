# ADR-0021: SQLite

Status: Accepted (extends ADR-0017)

## Context

SQLite is a library and a file, not a server. The usual network-policy controls do not apply;
instead the **file path** is the thing an attacker controls. A multi-user web tool that opens
arbitrary paths is a local-file-read/write primitive, so SQLite support is deliberately narrow.

## Decision

- **Opt-in and confined.** SQLite is disabled until the administrator sets `DBW_SQLITE_ROOT` to an
  existing absolute directory (startup fails on a bad value). A connection stores a file path
  *relative to that root* in its `database` field; host, port, credentials and TLS are fixed
  placeholders. The path must be in canonical relative form (no absolute path, `..`, `./`, `//`,
  backslash, drive letter, NUL or `~`), is resolved through symlinks and must stay inside the root,
  must be an existing regular file and must start with the SQLite magic header. **Databases are
  never created.** The path is re-checked after opening (`PRAGMA database_list`).
- **Read-only is the engine's.** Read-only connections open with `mode=ro` plus `PRAGMA query_only`,
  both enforced by SQLite. Writable connections use `mode=rw`.
- **Authorizer on every connection.** `sqlite3`'s authorizer denies `ATTACH`/`DETACH`, virtual-table
  creation, file and extension functions (`load_extension`, `readfile`, `writefile`, ...), reads of
  attached/other schemas and every pragma except a small introspection/read allowlist. `VACUUM`
  (which can write `INTO` any path) is refused by text, since it has no authorizer action.
  `trusted_schema` is off. Extension loading is not enabled.
- **Limits and cancellation.** A progress handler enforces the statement timeout and a
  cancellation flag (`interrupt()`); cancellation works on the replica running the statement (the
  connection is in-process), elsewhere it is best effort.
- **Features.** database "main" -> tables and views; keyset/offset browsing (INTEGER PRIMARY KEY is
  an identity, composite keys work); NULL-safe `IS`, `LIKE ... ESCAPE '\'`; insert/update/delete
  and bulk edits with the same optimistic concurrency; import; CSV/JSON export; SQL workspace with
  transactional rollback; create/drop index. No database create/drop, no users.

## Consequences

- The root must not be writable by untrusted users: a path check followed by an open is not atomic
  (symlink swap race). In containers, mount the data directory read-only unless writes are needed.
- Dynamic typing: the declared column type is shown, values keep their stored type; `rowid`-only
  tables without a primary key are read-only.
- Writers use a rollback journal or WAL beside the file, so the directory must be writable for
  writable connections.
