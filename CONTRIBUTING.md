# Contributing to DB Wrench

## Before you start

1. Read `docs/STATUS.md` — only work within the active phase unless agreed.
2. Read the relevant sections of `PRD.md` and `AGENTS.md`.
3. For architectural changes, propose an ADR in `docs/adr/` first.

## Workflow

- Branch from `main`: `feat/…`, `fix/…`, `docs/…`.
- Use Conventional Commits.
- Open a pull request; at least one review is required. Changes touching
  authentication, authorization, credentials, network policy, or the audit
  module require a security-focused review.

## Checks

```bash
make setup
make lint
make test
make test-int
make test-sec
```

All checks must pass. Follow the Definition of Done in `AGENTS.md` §15.

## Licensing and releases

The project license is to be selected by the maintainers and recorded in
`LICENSE` before the first release. Releases follow Semantic Versioning; each
release publishes a changelog, an SBOM, and signed container images.
