# ADR-0012: OIDC single sign-on and group-to-role mapping

Status: Accepted (SAML and LDAP remain future work)

## Decision

- **Flow.** Authorization code with PKCE (S256), `state` and `nonce`. State, nonce and verifier
  live in the application database (`oidc_login_states`; the state is stored hashed, single use,
  10 minutes) so any replica can finish a login. The session afterwards is the normal server-side
  session cookie.
- **ID token verification** uses PyJWT with an algorithm allowlist (RS/ES/PS families; `none` and
  HMAC are refused, which prevents key-confusion attacks), the issuer's JWKS (refetched once for an
  unknown `kid`), and checks `iss`, `aud`, `exp`, `iat`, `sub` and `nonce`. The provider's
  discovery `issuer` must equal the configured one.
- **Network.** Discovery, JWKS and token endpoints must be HTTPS (plain HTTP only with the
  development loopback flag) and pass the outbound network policy (ADR-0007) like any other
  destination, so an identity provider on a public address has to be allowlisted. Redirects are not
  followed. Known limit: the HTTP client resolves the name itself after validation (it does not pin
  the address as database connections do).
- **Identity.** Users are keyed by `(provider, sub)`. An SSO login never attaches to a pre-existing
  local account by e-mail address (account-takeover vector) and creates a new user with an
  unusable random password.
- **Roles.** The role is recomputed from the groups claim at *every* login (ordered mappings,
  first match wins, optional default role, otherwise login is refused). **Super Admin can never be
  assigned by SSO**, and an existing Super Admin cannot be demoted by it. A role change ends the
  user's other sessions. Deprovisioning takes effect at the next login; existing sessions live
  until their idle/absolute expiry, and administrators can deactivate users.
- **MFA.** The identity provider is trusted to perform it; SSO sessions start with
  `mfa_state = ok`.
- The client secret is envelope-encrypted in `app_settings` and never returned.

## Consequences

- `DBW_PUBLIC_URL` is required to enable SSO (it forms the redirect URI).
- Local password login stays available (break-glass administrators). Disabling it for non-admins
  is a possible follow-up.
