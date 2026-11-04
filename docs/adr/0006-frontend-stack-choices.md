# ADR-0006: Frontend stack choices

Status: Accepted

- **Data grid:** TanStack Table v8 with TanStack Virtual (rows are virtualised above 100). AG Grid
  Enterprise is excluded for licensing reasons. v8 is pinned deliberately: the v9 line is a major
  change and is not yet needed.
- **TypeScript 6.x:** pinned because `typescript-eslint` does not yet support TypeScript 7.
- **Components:** shadcn/ui conventions (Radix primitives + Tailwind tokens) written in-repo.
- **Editor:** Monaco, bundled locally (no CDN) and lazy-loaded only when a workspace opens (the
  main bundle is unaffected). Workers are separate same-origin files.
- **i18n:** a typed message catalogue (`messages.en.ts`); a missing key is a compile error.
- **CSP:** `default-src 'self'`; scripts and workers are same-origin only (no inline scripts, no
  CDNs). **`style-src` additionally allows `'unsafe-inline'`** because Monaco injects `<style>`
  elements for themes and decorations. This was verified necessary and is limited to styles;
  script injection stays blocked. Revisit if Monaco gains nonce support.
- **Safety rule:** database content is rendered as text only; ESLint forbids
  `dangerouslySetInnerHTML` and component tests assert that markup in cells stays inert.
