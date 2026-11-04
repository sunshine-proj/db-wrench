# ADR-0004: Authentication and sessions

Status: Accepted (OIDC, API tokens and SAML/LDAP are Phase 5)

## Decision

- Passwords: Argon2id; a dummy hash is verified for unknown users to equalise timing; policy
  enforced server-side (length, character classes, no username).
- Sessions: **server-side**, stored hashed (SHA-256) in the application database, delivered in an
  `HttpOnly`, `SameSite=Lax`, `Secure` (production) cookie; idle and absolute expiry; the token is
  rotated on privilege change (MFA verification/enrollment). Users can list and revoke sessions;
  password change, role change, deactivation and admin reset revoke sessions.
- CSRF: a per-session token returned by the session endpoints and required in `X-CSRF-Token` on
  every state-changing request (synchronizer pattern), plus `Origin` validation. This replaces
  the "double-submit" wording in earlier drafts: it is stricter and needs no extra cookie.
- Brute force: per-account progressive lockout and a per-IP failure window in the database, plus
  proxy-level rate limiting on `/api/v1/auth/`. Failures return one generic message.
- MFA: TOTP with replay protection (accepted time-step is stored) and ten single-use recovery
  codes; roles can require MFA (users are held in an enrollment-only session until enrolled).
- First run: no default credentials. A one-time setup token is created when no user exists,
  logged once, stored hashed and consumed by `POST /api/v1/auth/setup`.
- Client IP: taken from `X-Forwarded-For` only when `DBW_TRUSTED_PROXY_HOPS > 0`, counting from
  the right (the proxy overwrites the header, never appends).
