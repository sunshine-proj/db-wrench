# ADR-0022: Elasticsearch and OpenSearch (search family)

Status: Accepted (extends ADR-0017)

## Context

A search engine stores JSON documents in indices and is queried with a JSON DSL. It is neither
relational nor MongoDB-like, so it gets a new `search` adapter family and API surface
(`/api/v1/connections/{cid}/search/...`). Verified against Elasticsearch 7.17 and 8.15 (security
enabled) and OpenSearch 2.17.

## Decision

- **Contract first.** `SearchAdapter` (indices, index detail, document search/get/put/delete, index
  create/delete, validated `_search`) is defined before the adapter; `family_of` routes it.
- **Transport and pinning.** REST over `httpx`, Basic auth (or an API key when no user name is
  given). Like ClickHouse, the destination is validated and the request sent to the validated address
  on every call, with the original host as `Host`/SNI. Redirects are not followed.
- **Names are never trusted.** Index names must appear in the cluster's own listing (30 s cache,
  cleared on create/delete); wildcards, `_all` and hidden/system indices (`.`-prefixed) are refused.
  New index names match `^[a-z0-9][a-z0-9_-]*$`. Document ids travel as opaque ids and are
  percent-encoded in paths. Only a fixed set of endpoints is called.
- **The console can only search.** It calls `POST /<index>/_search` and nothing else, so it cannot
  write by construction. What a search body can *reach* is validated (`search/dsl.py`): only known
  top-level options; no key containing `script` (scripts, `script_score`, `bucket_script`, ...), no
  `runtime_mappings`, `wrapper`, `percolate`, `indexed_shape`, `pit`, `scroll`, `_index`, `stored_fields`,
  `indices_boost`, `ext`, and no `terms` lookup shape (`index`+`id`+`path`) that would read another
  index; limits on body size, nesting, `size`, `from + size` (10,000), `track_total_hits` and
  aggregation `size`. A denylist walk is weaker than an allowlist of query types, which would break
  too many legitimate queries; the strongest control is a **read-only role** on the account.
- **Editing uses the engine's native optimistic concurrency**: replace and delete send
  `if_seq_no`/`if_primary_term` (409 on mismatch), writes use `refresh=wait_for`. Documents are limited
  to 1 MiB and must be JSON objects. Production deletes need the index name typed.
- **Administration:** create (shards, replicas, validated mappings) and delete indices; deletion
  always needs the index name typed. Aliases, templates, ILM, snapshots, data streams and users are not
  managed.
- **Browsing** uses `from`/`size` (an extra hit tells whether another page exists) within the
  10,000-document window, optional `simple_query_string` text search and sorting on mapped fields.
  Deep paging (PIT / `search_after`) would need signed cursors bound to the user and index and is left
  out on purpose.

## Consequences

- No cancellation (searches are bounded by the timeout), no import/export, no sensitive-field masking
  for search connections.
- Data streams and aliases to multiple indices are not browsed; aliases are shown on their index.
- Four-eyes approval policies do not cover search operations yet.
