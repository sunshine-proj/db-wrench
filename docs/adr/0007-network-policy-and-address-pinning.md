# ADR-0007: Outbound network policy and address pinning

Status: Accepted

## Decision

Every managed-database destination is validated **on every new physical connection**:

1. all addresses the host resolves to are checked (one bad answer rejects the lookup);
2. link-local, cloud-metadata (incl. IPv4-mapped IPv6), unspecified and multicast ranges are
   **never** allowed, even if allowlisted;
3. loopback is denied unless the deployment sets `DBW_NETWORK_ALLOW_LOOPBACK` (development and
   test only; the application refuses to start in production with it enabled);
4. otherwise the address must be in an allowed CIDR or the host name must match an allowed
   pattern. The default allows private ranges only; administrators edit the policy in the UI.

The connection then uses the **validated address** (PostgreSQL `hostaddr`, with the host name
kept for certificate verification), so a later DNS change cannot redirect it.

## Known limits (documented, not hidden)

- **MySQL and MongoDB with `verify-full` TLS** connect by host name (their drivers verify the
  certificate against the connect host); the resolution is validated immediately before, leaving
  a small DNS-rebinding window. Use `verify-ca` (pinned) or PostgreSQL where that matters.
- **MongoDB** runs with `directConnection=True`. Topology discovery would make PyMongo follow
  member addresses advertised by the server, bypassing this policy, so replica sets are reached
  through the member you name. Discovery needs an address-filtering hook and is future work.
