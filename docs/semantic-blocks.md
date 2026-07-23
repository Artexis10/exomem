# Semantic blocks

Semantic blocks are optional Markdown sections that make Exomem notes easier to
parse without making them harder to read. They are normal headings plus optional
metadata bullets. The body remains ordinary Markdown.

This is not a separate DSL. The goal is governed cognition: claims, evidence,
decisions, assumptions, risks, records, cases, and actions can be named,
related, validated, and reused by Exomem tooling. Compact observations, rich
units, category/kind governance, retrieval, and exact unit reads are documented
in [first-class semantic language](semantic-language.md).

## Block shape

Use a heading whose text is a supported block type:

```markdown
## Claim
- id: retrieval-owned-files
- relations: evidenced_by: [[Knowledge Base/Sources/Sessions/2026-07-07-session]]

Retrieval works best when durable conclusions live in owned Markdown files.
```

Metadata is optional. Leading bullets of the form `- key: value` are parsed as
metadata and removed from the block body. Any other body content stays Markdown:
paragraphs, bullets, wikilinks, tables, and code blocks.

The first-class rich metadata fields are `category`, `tags`, and `context`.
`tags` is a comma-separated list of plain tokens (no `#` prefix); tokens are
trimmed, Unicode-normalized, case-folded, and de-duplicated in first-authored
order. `context` is trimmed, non-empty, single-line Unicode. Invalid values stay
visible in generic parsed metadata for diagnosis but do not partially populate
the first-class field.

When `category` is omitted, the heading supplies the authored category identity:
the exact heading is `category_raw` and its normalized key is `category_key`.
Reviewed category aliases may resolve that key; otherwise the public category
falls back to the governed heading kind. An explicit `- category:` overrides the
heading-derived category identity. Category, governed kind, tags, context, and
typed relations are separate dimensions.

Headings inside fenced code blocks are ignored by the parser.

## Block types

Supported types:

- `claim`
- `finding`
- `evidence`
- `decision`
- `assumption`
- `inference`
- `constraint`
- `risk`
- `open_question`
- `hypothesis`
- `result`
- `metric`
- `failure`
- `pattern`
- `record`
- `case`
- `timeline_event`
- `requirement`
- `action`
- `definition`
- `procedure`

Heading labels normalize spaces and hyphens to underscores, so these are
equivalent:

```markdown
## Open Question
## open-question
## open_question
```

Unknown headings remain normal Markdown and are not validation errors.

## Note relations

Use a canonical `## Relations` section for directional note-to-note edges. Each
bullet has one governed lower-snake-case relation type and one wikilink:

```markdown
## Relations
- refines [[Knowledge Base/Notes/Insights/Earlier Conclusion]]
- depends_on [[Knowledge Base/Entities/Decisions/Architecture Decision]]
- relates_to [[Knowledge Base/Notes/Research/Project/Adjacent Finding]]
```

These links remain visible and editable in Obsidian. Exomem indexes the declared
edge type instead of a redundant generic edge. Inline references elsewhere in
the note remain generic `links_to` connections.

Typed bullets written outside `## Relations` remain index-compatible for older
notes, but new notes should use the canonical section so validation and review
can distinguish governed relations from incidental list prose.

## Block relations

When a relation belongs to a specific claim, finding, decision, or piece of
evidence rather than the whole note, put it in that semantic block's
`relations` metadata bullet. Use comma-separated `relation: target` entries:

```markdown
## Risk
- id: schema-dsl-risk
- relations: mitigates: [[Decision#Use headings]], blocks: [[Requirement#Plain Markdown]]

A custom grammar would make notes harder to read and harder to edit by hand.
```

Supported relations:

- `supports`
- `contradicts`
- `refines`
- `duplicates`
- `supersedes`
- `derived_from`
- `evidenced_by`
- `depends_on`
- `implements`
- `mitigates`
- `causes`
- `caused_by`
- `blocks`
- `resolves`
- `answers`
- `raises_question`
- `used_for`
- `observed_in`
- `mentions`
- `about_entity`
- `relates_to`
- `links_to`
- `cites`
- `tests`
- `owns`

Unsupported relation names and malformed relation entries are validation
errors. Duplicate block IDs are warnings because the file remains readable, but
references become ambiguous.

## Examples

```markdown
## Evidence
- id: receipt-photo
- category: purchase records
- tags: warranty, hardware/receipts
- context: replacement request
- relations: supports: [[Case#Laptop warranty]]

Photo of the purchase receipt preserved for the warranty claim.

## Decision
- id: use-markdown-headings
- relations: resolves: [[Open Question#Block syntax]], mitigates: [[Risk#DSL sprawl]]

Use ordinary headings plus optional metadata bullets for v1 semantic blocks.

## Action
- id: add-tests
- relations: implements: [[Requirement#Validation Result Shape]], owns: [[Hugo]]

Add parser, validation, claim extraction, and context-pack tests.
```

## Integration

Rich blocks are governed semantic units and may carry typed relations. Compact
`- [category] ... ^anchor` observations remain ordinary one-line Markdown and
cannot imply typed graph edges; use rich form or a canonical note-level
relation when direction matters.

`claims.extract_claim_text` prefers the first semantic `claim` block when one is
present, then falls back to the legacy section-based extraction.

`context_pack.assemble_pack` includes a `semantic_blocks` map keyed by packed
page path when pages contain supported blocks. This is additive: existing pack
fields and find ordering are unchanged.
