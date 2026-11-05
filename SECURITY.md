# Security Policy

## Reporting a vulnerability

Do not open public issues for security vulnerabilities.

Report privately to the maintainers using the contact configured for this
repository (security advisories or the designated security mailbox). Include a
description, affected version, reproduction steps, and impact.

Targets: acknowledgement within 3 business days, triage within 7, and a fix or
mitigation plan for confirmed critical issues within 30 days.

## Supported versions

Security fixes are provided for the latest minor release and the previous minor
release.

## Scope

In scope: authentication, authorization, credential storage, SSRF and network
policy bypass, SQL/NoSQL injection, read-only enforcement bypass, audit
tampering, sensitive-data exposure, single sign-on (token validation, account
linking), API token scoping, approval-workflow bypass, and import / MongoDB write-stage abuse.

## Hardening reference

See AGENTS.md §7 and PRD.md §8 for the security requirements the project is
built against.
