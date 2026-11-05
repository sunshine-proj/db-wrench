# ADR-0016: Database users, roles and privileges

Status: Accepted

## Context

Phase 6 manages the accounts and privileges *inside* the managed databases (PRD 5.8, "Extended
Administration"). Application permissions stay separate from database privileges (AGENTS.md 7).
This is the most privilege-sensitive surface so far: a tool that can run `GRANT` can hand out more
than it was meant to.

## Decision

- **Engine-specific, one contract.** PostgreSQL roles (login or group, membership), MySQL
  `user@host` accounts, and MongoDB users and custom roles per database. The relational adapters
  share a contract (`list_principals`, `create/alter/drop_principal`, `list_principal_grants`,
  `change_privileges`, `change_membership`); MongoDB has its own document-oriented one. MySQL
  roles are accounts in `mysql.user` and show up as users.
- **Allowlists, never pass-through.** Privileges come from per-engine, per-object-kind allowlists;
  `ALL` expands to the allowlist, never to the engine's own `ALL`. Never grantable: superuser,
  replication, bypassrls, `GRANT OPTION`, global (`*.*`) grants, `FILE`, `SUPER`, `PROCESS`,
  `RELOAD`, `SHUTDOWN`, `CREATE USER`, cluster-wide MongoDB roles (`root`, `readAnyDatabase`, ...),
  roles or privileges on `admin`/`local`/`config`, and PostgreSQL predefined `pg_*` roles. MongoDB
  roles assignable are `read`, `readWrite`, `dbAdmin`, `dbOwner`, `userAdmin` plus custom roles of
  that database; custom-role privileges use a fixed action list on a database or collection.
- **Protected principals.** Superusers, system accounts, `pg_*`, `root`, MongoDB users of system
  databases and **the account DB Wrench connects with** can be listed but never altered, dropped
  or granted to (no self-lockout, no tampering with the account that holds the credentials).
- **Names and targets.** New names match a conservative pattern and are dialect-quoted (`psycopg`
  `sql.Identifier`, backticks, bound parameters for MySQL account names); existing principals,
  databases, schemas, tables and collections must exist in fetched metadata.
- **Passwords** are write-only: min 12 characters, never stored, returned, audited, logged or put in
  an approval summary. PostgreSQL passwords are SCRAM-hashed client-side
  (`pgconn.encrypt_password`) so the plaintext never appears in server logs; a login role needs one.
  MySQL passwords travel as bound parameters. MongoDB passwords are sent to `createUser`/`updateUser`.
- **Authorization.** New read permission `admin.view_users` (connection scope, not a write, so
  inspection works on read-only connections); `admin.manage_users` (create/alter/drop) and
  `admin.manage_privileges` (grants, revocations, membership, role privileges) are stripped on
  read-only connections. All of it is connection-level: scoped grants (ADR-0013) never reach it.
- **Governance.** Dropping a principal always needs its name typed; privilege and membership
  changes need the name typed on production. Five policy operations (`admin.create_principal`,
  `admin.alter_principal`, `admin.drop_principal`, `admin.grant_privileges`,
  `admin.revoke_privileges`) can require four-eyes approval (ADR-0011); the digest binds the
  principal, host and privileges (not the password, which would otherwise have to be stored).
- All writes use the dedicated autocommit connection of ADR-0011 and are audited
  (`admin.principal.*`, `admin.privilege.*`, `admin.membership.*`).

## Consequences

- Objects a role owns block `DROP ROLE` (the server error is shown); reassigning ownership is done
  in the SQL workspace.
- MySQL grants cover a database or a table; column- and routine-level grants and global privileges
  need SQL. PostgreSQL default privileges and per-column grants likewise.
- Listing MySQL accounts needs read access to `mysql.user` for the connection's own account.
- PostgreSQL grants are listed per database (objects live in one database); the UI starts with the
  connection's database.
