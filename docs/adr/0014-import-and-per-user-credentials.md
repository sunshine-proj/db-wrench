# ADR-0014: Import with preview, and per-user managed-database credentials

Status: Accepted

## Import

- CSV into tables, JSON array or NDJSON into collections; limits `DBW_IMPORT_MAX_ROWS` (100,000),
  `DBW_IMPORT_MAX_DOCUMENTS` (50,000) and the 8 MB request limit. Needs `export.import` plus
  `data.insert`.
- **Preview really executes.** Relational preview inserts every row inside a transaction that is
  always rolled back (each row behind a savepoint), so type errors, constraints, foreign keys and
  triggers are reported exactly as a commit would meet them, with row numbers. Sequences/identity
  values may advance. Commit is all-or-nothing in one transaction.
- MongoDB preview validates every document (same validator as single inserts) and checks `_id`
  collisions with the collection; commit uses unordered `insert_many` and is **not atomic**
  (reported plainly). Inserts only: no upsert.
- A signed token (10 minutes) binds user, target, the digest of content + options and the row
  count; the client resends the content on commit. Production needs the object's name; policy can
  require an approval. Masked/sensitive columns cannot be written without `data.read_sensitive`.
- Audit records counts, columns and a content digest, never row data.

## Per-user credentials

- `credential_model = per_user`: each user stores their own database login
  (`user_credentials`, envelope-encrypted with a user+connection context, rewrapped on key
  rotation). It is verified against the server before it is stored and is used only for that
  user's requests (the shared service account of a per-user connection is never used).
  Adapters are cached per user (derived id) and invalidated with their parent connection.
- Without credentials a request fails with `409 credentials_required`. API tokens use the owner's
  credentials.
- PyMySQL crashes on the first login of a `caching_sha2_password` account over plain TCP (fresh
  per-user logins hit this); the adapter retries once for exactly that signature.
