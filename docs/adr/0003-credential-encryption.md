# ADR-0003: Credential envelope encryption

Status: Accepted

## Decision

Stored secrets (connection passwords, TOTP secrets) use **envelope encryption**:

- a fresh random 256-bit data key (DEK) per secret encrypts the payload with AES-256-GCM;
- the DEK is wrapped with a key-encryption key (KEK) with AES-256-GCM;
- the associated data is a context string (`connection:<id>`, `mfa:<user id>`), so a ciphertext
  cannot be moved to another record;
- the KEK identifier is stored beside the ciphertext, which makes **rotation** a re-wrap of the
  DEK only (`POST /api/v1/settings/encryption/rotate` or `python -m app.cli rotate-keys`).

KEKs come from a `KeyProvider`. The shipped provider reads `DBW_ENCRYPTION_KEYS`
(`id:base64,id2:base64`, 32 bytes each, `*_FILE` supported) and `DBW_ACTIVE_KEK_ID`. An external
KMS / Vault provider can be added behind the same interface without a data migration.

## Consequences

- Losing every KEK that wrapped a secret makes it unrecoverable (documented in
  `docs/operations.md`); keep retired keys listed until rotation has completed.
- Passwords are never returned by the API, logged, or written to audit records (tested).
- Per-user managed-database credentials (`credential_model = per_user`) are modelled but not yet
  implemented; only `shared` is accepted today.
