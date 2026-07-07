# add-semantic-block-schema — design

## Context

Exomem already extracts claims structurally and assembles context packs from
Markdown, frontmatter, wikilinks, and existing sidecars. That code proves the
right direction: deterministic structure from the user's own files, not a
server-side reasoning layer.

The missing piece is a reusable semantic block layer that can parse a broader
epistemic vocabulary from normal Markdown and expose it to existing machinery.
This must stay readable in Obsidian and plain text, and must not require users
to adopt a fenced directive language or a graph database.

## Goals / Non-Goals

**Goals:**

- Parse typed semantic blocks from ordinary Markdown headings.
- Validate a fixed first vocabulary of block types and relation names.
- Preserve Markdown compatibility: semantic metadata is optional plain bullets,
  and section body remains normal Markdown.
- Let claim extraction and context packs reuse parsed blocks without changing
  public tool surfaces.
- Keep behavior deterministic, local, and model-free.

**Non-Goals:**

- No Basic Memory-compatible DSL or import clone.
- No new REST/MCP/CLI operation.
- No new sidecar, graph database, migration, or dependency.
- No automatic inference of block types or relations.
- No authority/confidence scoring.

## Decisions

1. **Use headings as the block boundary.**
   A semantic block is an ATX heading whose normalized heading text matches a
   supported block type, such as `## Claim`, `### Open Question`, or
   `## Timeline Event`. This keeps the file plain Markdown and lets unknown
   headings remain ordinary document structure. A heavier directive syntax was
   rejected because it would make the layer less readable and less compatible
   with existing notes.

2. **Use optional metadata bullets, not inline mini-languages.**
   The parser reads leading bullets of the form `- key: value` immediately below
   the block heading. `id` is a conventional identifier. `relations` carries
   comma-separated typed relation entries such as
   `supports: [[Knowledge Base/Notes/X#Claim]]`. Unrecognized metadata keys are
   preserved; only relation names are validated. This gives agents structured
   handles without turning note bodies into a DSL.

3. **Fence-aware, deterministic parsing.**
   Headings and metadata-looking text inside fenced code blocks are ignored.
   The parser records heading level, title, source line, metadata, relations,
   and body exactly enough for tests and downstream consumers. It does not call
   any model or inspect embeddings.

4. **Validation is strict where structure is explicit.**
   Unsupported relation names and malformed relation entries are errors.
   Duplicate block IDs are warnings because duplicates make references
   ambiguous but should not make old Markdown unreadable. Unknown ordinary
   headings are not errors because semantic blocks are opt-in.

5. **Integration is additive.**
   `claims.extract_claim_text` prefers the first parsed `claim` block, then uses
   its existing section-based fallback. `context_pack.assemble_pack` adds a
   `semantic_blocks` field for pages with parsed blocks while preserving
   `claims`, `neighborhood`, `contradictions`, caps, no-mutation behavior, and
   find ordering.

## Risks / Trade-offs

- **Heading ambiguity:** a normal `## Claim` heading becomes semantic. Mitigation:
  that is acceptable because the block body remains unchanged Markdown and the
  type is explicitly named by the author.
- **Relation grammar too small:** comma-separated `relation: target` cannot
  express rich relation metadata. Mitigation: keep v1 parseable and extend
  later with additional preserved metadata keys if needed.
- **Context pack payload growth:** exposing all blocks can add output size.
  Mitigation: only include the field when blocks exist, and use compact block
  dictionaries.
- **Validator overreach:** rejecting unknown headings would break normal notes.
  Mitigation: only recognized semantic headings become blocks.

## Migration Plan

No migration is required. Existing Markdown remains valid. Notes gain semantic
structure only when authors add supported headings and optional metadata.

Rollback is removing the integration calls; the parser module has no persistent
state.

## Open Questions

- Whether future increments should add stable cross-file block references beyond
  plain Markdown anchors and IDs.
- Whether context packs should later cap `semantic_blocks` separately from the
  existing pack caps.
