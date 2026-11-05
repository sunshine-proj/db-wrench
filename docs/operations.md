# Operations guide

## Deploying

**Evaluation / development** (HTTP on localhost):

```bash
./scripts/init-env.sh              # creates .env with fresh secrets (mode 600)
docker compose up -d --build
docker compose logs backend | grep "setup token"   # first run only
```

Open <http://localhost:8080>, paste the setup token and create the first Super Admin. If the token
was missed: `docker compose exec backend python -m app.cli reset-setup-token` (only works while no
user exists).

**Production** (TLS, secrets as files, read-only containers, resource limits):

```bash
./scripts/init-secrets.sh                      # postgres password, database URL, encryption key
cp fullchain.pem secrets/tls_cert.pem && cp privkey.pem secrets/tls_key.pem
export DBW_VERSION=1.0.0                       # pin the release you deploy
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The backend refuses to start in production without secure cookies, with the loopback override, or
with a database schema that does not match the release. Containers run as non-root with
read-only root filesystems and all capabilities dropped; only the proxy is published.

## Reaching your databases

The application database and the frontend sit on an *internal* Docker network with no internet.
The backend additionally joins `egress` to reach managed databases. Every destination is then
checked against the **network policy** (Network & keys page, or `DBW_NETWORK_ALLOW_CIDRS`):

| Destination | Result |
| --- | --- |
| Link-local, cloud metadata (169.254.0.0/16, fd00:ec2::254, ...) | never allowed |
| Loopback | denied (development flag only, never in production) |
| Private ranges (10/8, 172.16/12, 192.168/16, fc00::/7) | allowed by default |
| Anything else | denied until a CIDR or host pattern is allowlisted |

To reach a database **on the Docker host**, use `host.docker.internal` (the default compose file
maps it; its address is in a private range). The production overlay removes that mapping; add it
back deliberately if you need it. Databases in other containers: attach them to the `egress`
network and use the container name.

TLS to managed databases: `disable`, `require` (encrypted, unverified), `verify-ca`, `verify-full`
(see ADR-0007 for the driver-specific limits).

## Secrets and encryption keys

- `DBW_ENCRYPTION_KEYS` holds key-encryption keys (`id:base64`, 32 bytes each). **Back it up
  separately from the database backup.** Without the key, stored connection passwords and TOTP
  secrets cannot be decrypted; users would have to re-enter them.
- **Rotate:** add a new key to `DBW_ENCRYPTION_KEYS` (keep the old one listed), set
  `DBW_ACTIVE_KEK_ID` to the new id, restart, then run *Rotate encryption keys* in the UI or
  `python -m app.cli rotate-keys`. Remove the old key only after rotation reports zero remaining.
- Use `DBW_<NAME>_FILE` (Docker secrets) instead of plain environment variables in production.

## Backup and restore (application database)

DB Wrench stores users, roles, connection metadata, encrypted credentials, grants and the audit
log in its own PostgreSQL database. Managed databases are **not** backed up by DB Wrench.

```bash
# backup
docker compose exec -T db pg_dump -U dbwrench -Fc dbwrench > dbwrench-$(date +%F).dump
# restore into an empty database (stop the backend first)
docker compose stop backend proxy
docker compose exec -T db pg_restore -U dbwrench -d dbwrench --clean --if-exists < dbwrench.dump
docker compose start backend proxy
python -m app.cli verify-audit      # (via docker compose exec backend) confirm the chain is intact
```

Restore the matching `DBW_ENCRYPTION_KEYS` alongside the dump.

## Upgrading

1. Back up the application database and the encryption keys.
2. Pull/build the new release and set `DBW_VERSION`.
3. `docker compose ... up -d`. The `migrate` service runs `alembic upgrade head` once and exits;
   the backend starts only after it succeeds, and refuses to start on a schema mismatch.
4. Verify `/health/ready` (inside the network) and sign in. Roll back by restoring the backup and
   the previous image; migrations are not automatically downgraded in production.

Run more than one backend replica behind the proxy for availability: the backend is stateless
(sessions, rate-limit counters and audit state live in the application database).

## Monitoring

- `GET /health/live` and `/health/ready` (readiness checks the database and schema revision).
  Both are hidden at the public proxy; use them from inside the network (Compose healthchecks do).
- `GET /metrics` (Prometheus) when `DBW_METRICS_ENABLED=true`; also hidden at the public proxy;
  scrape it from the internal network.
- Logs are structured JSON on stdout with a `request_id` that also appears in API errors and audit
  records. Credentials and row contents are never logged.
- **Tracing** (opt-in, ADR-0015): build the image with `DBW_EXTRAS="--extra otel"`, then set
  `DBW_OTEL_ENABLED=true` and `DBW_OTEL_ENDPOINT` (OTLP/HTTP, e.g. `https://collector:4318`);
  `DBW_OTEL_SAMPLE_RATIO` and `DBW_OTEL_SERVICE_NAME` tune it. Startup fails if the extra or the
  endpoint is missing. Spans never contain bodies, query strings, cookies, statements or row data.
- **Audit forwarding** (syslog/SIEM) is configured under *Integrations*; see below.

## Data editing, exports and queries

- **Bulk operations** act on at most `DBW_BULK_MAX_ROWS` (10,000) rows and only after a preview;
  the preview token is valid for 10 minutes and is bound to the user, the selection, the values
  and the previewed count. Exports stream up to `DBW_EXPORT_MAX_ROWS` (1,000,000) rows.
- **Query limits:** default statement timeout `DBW_STATEMENT_TIMEOUT_S` (30), request maximum
  `DBW_MAX_STATEMENT_TIMEOUT_S` (300), per-user concurrency `DBW_CONCURRENT_QUERIES_PER_USER` (5).
  Operations are stored in the application database; if a replica dies mid-query its operation row
  is swept after the maximum timeout.
- **History retention** and whether literal values are stored are set under *Network & keys*
  (default 90 days, literals kept). A background task purges expired entries hourly
  (`DBW_HISTORY_PURGE_INTERVAL_S`).
- **Open adapters** are capped (`DBW_ADAPTER_CACHE_MAX`, default 100) with least-recently-used
  eviction, so many connections cannot exhaust database connection slots.
- Prefer giving DB Wrench's *managed-database accounts* only the privileges users should have: for
  read-only connections use a read-only database role, which is the strongest guarantee (ADR-0008).

## Single sign-on (OIDC)

1. Set `DBW_PUBLIC_URL` to the externally visible base URL (for example
   `https://wrench.example.com`) and restart. The redirect URI to register at the identity
   provider is `<DBW_PUBLIC_URL>/api/v1/auth/oidc/callback` (shown under *Integrations*).
2. The identity provider must be reachable under the outbound network policy: allowlist its host
   or CIDR under *Network & keys* (ADR-0007). Discovery, JWKS and token endpoints must be HTTPS.
3. Under *Integrations* enter issuer, client id and secret, the groups claim, and the
   `group=Role` mappings (first match wins; Super Admin cannot be mapped). Without a matching group
   and a default role, login is refused.
4. Users are created on first login and keyed by the provider's immutable `sub`; they are never
   merged with local accounts by e-mail. The role is recomputed at every login. Removing a user from
   a group takes effect at their next login; deactivate the user to cut access immediately.
   Keep at least one local Super Admin as break-glass access.

## API tokens

Users with `app.manage_tokens` create scoped, expiring tokens under *API tokens*. Send them as
`Authorization: Bearer dbw_...`. A token can only act on `/api/v1/connections/**` and
`/api/v1/approvals/**`, never exceeds its owner's rights, and stops working when the owner is
deactivated. The secret is shown once; only its hash is stored. Audit records of token requests carry
the token id.

## Governance and approvals

*Governance* (administrators) lists which operations need a second person's approval per
environment or connection. A gated request answers `428 approval_required` with an approval id;
a different user with `governance.approve` on that connection decides under *Approvals*; the
requester then repeats the identical request with an `Approval-Id` header (the UI offers it under
"Use approval"). Approvals expire after 24 hours and work once. MongoDB `$out`/`$merge` always
need an approval.

## Audit export and SIEM forwarding

- *Audit log -> Export* downloads CSV/JSON/NDJSON (`audit.export`). Each record carries `prev_hash`
  and `hash`. To verify an export offline, start with `prev = "0" * 64` and for each record check
  `record.prev_hash == prev` and `record.hash == sha256(prev + canonical_json(body))`, where
  `body` is the record's `ts, actor_user_id, actor_username, ip, request_id, action, connection_id,
  target, result, detail` and `canonical_json` sorts keys and uses separators `(",", ":")`.
- *Integrations -> Audit forwarding* sends events as RFC 5424 (or JSON) over UDP, TCP or TLS. Only
  one replica forwards at a time; the position is kept in the database, so an outage delays delivery
  instead of losing events (at-least-once: de-duplicate on the `id`). The SIEM host must be allowed by
  the network policy. Metrics: `dbw_audit_forwarded_total`, `dbw_audit_forward_errors_total`.

## Importing data

*Import* on a table (CSV) or collection (JSON/NDJSON) validates first: a relational preview runs the
import in a rolled-back transaction (identity/sequence values may advance), so every error is
reported with its row number; commit is all-or-nothing. MongoDB imports insert only and are not
atomic. Limits: `DBW_IMPORT_MAX_ROWS`, `DBW_IMPORT_MAX_DOCUMENTS`, 8 MB per request.

## Personal database credentials and scoped access

- Connections set to *Each user's own login* store one encrypted credential per user; users add
  theirs under the connection's *Your credentials*. The shared service account is not used.
- *Scoped access* (connections page) grants a role on one database, schema or table/collection.
  It adds to connection-level access and covers object-level actions only; free-form SQL and
  aggregation, creating/dropping databases and managing access remain connection-level (ADR-0013).

## Command line

`docker compose exec backend python -m app.cli <command>`

| Command | Purpose |
| --- | --- |
| `reset-setup-token` | issue a new first-run token while no user exists |
| `verify-audit` | recompute the audit hash chain (exit code 2 if broken) |
| `rotate-keys` | re-wrap stored data keys under the active key |
