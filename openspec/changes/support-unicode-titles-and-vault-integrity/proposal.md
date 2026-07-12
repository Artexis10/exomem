## Why

Exomem currently treats a page's filename as its fallback identity and transliterates non-Latin titles through a language-blind library, which can destroy the only readable form of a title and produce incorrect mixed-language slugs. A Japanese-vault health check also exposed adjacent integrity gaps in index reconciliation, imported YAML, multi-file mutation rollback, and install diagnostics that are still present after the public command rename.

## What Changes

- Preserve every user-supplied display title as Unicode frontmatter and a canonical H1, independent of the filename.
- Add an optional explicit ASCII filename slug while keeping existing filenames stable and retaining backward-compatible automatic slugging when no slug is supplied.
- Resolve display titles consistently across find/search, fetch/get, indexes, and related product surfaces.
- Serialize imported provenance fields as valid YAML for paths containing colons or other YAML-significant characters.
- Make index counts complete and make reconcile refresh Sources, Notes, and Entities from disk.
- Make multi-file markdown batches rollback-safe, including move/link-rewrite flows.
- Infer the doctor profile from installed capabilities when no profile is configured and treat an uninstalled client as skipped rather than a failed hook installation.
- Correct slug documentation and document Unicode title/ASCII slug behavior.
- Do not rename or rewrite existing user pages automatically; migration or repair remains explicit.

## Capabilities

### New Capabilities

- `unicode-page-identity`: Language-agnostic display-title preservation, explicit filename slugs, and one canonical title-resolution contract.
- `transactional-vault-writes`: Rollback guarantees for multi-file markdown mutations and move/link rewrite operations.

### Modified Capabilities

- `live-index-freshness`: Reconcile and normal writers keep complete Sources, Notes, and Entities counts aligned with disk.
- `install-readiness`: Default doctor profile inference reflects installed capabilities rather than always reporting lean.
- `retrieve-inject-hook`: Multi-client hook health checks distinguish an uninstalled client from a broken installation.
- `command-surface`: The optional slug input and title contract remain consistent across MCP, REST, and CLI surfaces.

## Impact

Affected areas include vault serialization and batch writes, compiled-note rendering, corpus parsing and product read surfaces, adoption/import rendering, index/reconcile logic, doctor and hook diagnostics, generated command schemas, scaffold documentation, and their regression tests. No heavy model or server-side reasoning capability is added, and existing page paths remain unchanged unless a caller explicitly supplies a slug for a newly created page.
