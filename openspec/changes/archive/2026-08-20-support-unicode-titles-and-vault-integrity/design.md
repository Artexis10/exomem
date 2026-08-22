## Context

The public product commands (`remember`, `capture_source`, `ask_memory`, and peers) wrap the older operation leaves (`note`, `add`, `find`, `fetch`). The rename changed the interface vocabulary, not the underlying title, slug, serialization, index, or mutation behavior. Today a non-Latin title is passed to `python-slugify` with ASCII output, compiled notes omit both `title:` and an automatic H1, and readers use incompatible title fallbacks. A language-blind CJK transliterator can therefore replace the only durable human-readable title with Mandarin pinyin.

The same health check exposed adjacent deterministic integrity gaps: hand-rendered YAML scalars, incomplete count reconciliation, non-rollback batch replacement, capability-blind doctor inference, and multi-client hook checks that classify absent clients as broken. The implementation must stay language-agnostic, preserve existing paths, keep every public surface on the shared leaves, and add no model-based language detection or translation.

## Goals / Non-Goals

**Goals:**

- Preserve arbitrary Unicode display titles losslessly in every newly written governed Markdown page.
- Separate display identity from an optional caller-controlled ASCII filename slug.
- Make title resolution, counts, imports, multi-file mutation, and install diagnostics deterministic and consistent.
- Keep current files and wikilinks stable; upgrades must not rename existing pages.
- Cover the behavior through leaf, product-command, CLI/REST/MCP-schema, and regression tests as appropriate.

**Non-Goals:**

- Guess language, pronunciation, romaji, translation, or an English title server-side.
- Bulk-rename existing pages or silently synthesize titles that were never stored.
- Introduce a new migration command in this change; existing pages can be repaired explicitly in a later reviewed workflow.
- Promise crash-safe transactions across separate filesystems or a process kill during rollback.

## Decisions

### Store a canonical Unicode title and H1

Every new governed page stores the exact caller-supplied title as a YAML-safe `title:` scalar and renders a canonical H1. Writers avoid duplicating the H1 when a compatible heading is already supplied and normalize the writer-owned heading to the canonical title. Readers use one helper with the precedence `frontmatter title -> first H1 -> filename stem`. Frontmatter is authoritative because it is structured, available to resolvers, and does not depend on body formatting.

Alternative: preserve only an H1. Rejected because fetch/search metadata and wikilink resolution already consume frontmatter and would still need divergent body parsing.

### Keep automatic slugs compatible and add explicit ASCII slugs

New write surfaces accept optional `slug`. It is normalized and validated as lowercase ASCII kebab-case with the existing 100-character cap. When supplied, it alone determines the filename component; the Unicode title remains untouched. When omitted, existing automatic slug behavior remains for compatibility, but the readable title is now durable and the writer warns when a non-ASCII title required lossy transliteration.

Alternative: default to Unicode filenames. Rejected as an immediate default because it changes paths, portability expectations, and caller behavior. Alternative: add Japanese transliteration. Rejected because Kanji readings are contextual and the product must support every language without language-specific dependencies.

### Serialize metadata through shared YAML scalar helpers

Hand-rendered user/path/title scalars use the vault YAML scalar serializer. This includes `title`, `imported_from`, and other newly touched fields. The writer does not interpolate untrusted scalar text directly into frontmatter.

### Make count reconciliation complete

The top index gains true total rows for Sources, Notes, and Entities while retaining any existing per-type rows. Count refresh inserts missing total rows and rewrites all known rows. `reconcile` also rebuilds `Sources/index.md` by-type counts, not only Notes/Entities sub-indexes. Navigation files and excluded/private subtrees remain outside counts.

### Roll back multi-file replacements and make move a reversible rename

`batch_atomic_write` snapshots pre-write bytes and metadata needed to restore every destination changed in the commit phase. If any replace fails, it restores prior files and removes newly created destinations before re-raising. `move_file` performs a same-filesystem rename, then applies rollback-safe inbound-link updates; if link rewriting fails it renames the file back. Sidecar/watcher notifications occur only after the filesystem transaction succeeds.

Alternative: journal files on disk. Rejected for this bounded local transaction because in-memory snapshots are simpler and the batches are Markdown-sized. A future crash-recovery journal remains possible.

### Infer capabilities without loading models

Doctor keeps explicit `--profile` and `EXOMEM_PROFILE` precedence. Otherwise it uses import/spec and executable availability only: media when the media dependency set is present, hybrid when embeddings dependencies are present, and lean otherwise. It does not load models or download assets during inference.

### Treat absent clients as skipped

The default multi-client hook check first determines whether each client has any installation/config footprint. A client with none is reported as skipped and does not fail the aggregate. An explicitly selected client, or a client with partial/stale configuration, is still checked strictly and can fail.

## Risks / Trade-offs

- [Automatic pinyin filenames remain possible when callers omit `slug`] -> Preserve the Unicode title, emit a lossy-slug warning, document explicit slugs, and avoid a breaking filename-policy change.
- [Adding `title:` and an H1 changes newly generated file bytes and fixtures] -> Update golden expectations deliberately and keep parsers backward-compatible with legacy title-less files.
- [Rollback itself can fail under severe filesystem errors] -> Attempt every restoration, report both the original and rollback failures, and leave reconcile guidance; no partial state is silently reported as success.
- [Capability inference can overestimate a broken optional install] -> Inference chooses which checks run; the checks still fail with actionable remediation if dependencies are incomplete.
- [Total count rows alter scaffold output] -> Preserve curated prose and per-type rows, modifying only count tokens and inserting clearly named totals.

## Migration Plan

1. Ship reader compatibility first: legacy files continue to resolve through H1 or filename fallback.
2. Ship writer changes for new pages only; no upgrade-time vault mutation occurs.
3. Reconcile inserts/repairs count totals on the next explicit reconcile or normal governed write that refreshes the relevant index.
4. Existing filenames and wikilinks remain unchanged. Users may later run an explicit reviewed repair/migration workflow if recoverable titles are available from logs or other provenance.
5. Rollback is a normal package downgrade because no required schema migration is introduced; pages created by the new version remain valid Markdown/YAML for older versions.

## Open Questions

None blocking. A later change can decide whether a configurable Unicode-filename default or a title-recovery migration is desirable after real multilingual usage.
