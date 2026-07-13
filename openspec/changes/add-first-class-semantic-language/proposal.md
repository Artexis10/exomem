## Why

Exomem's governed relations and semantic blocks are deeper than a generic note graph, but the core product still lacks a lightweight, category-addressable observation unit that agents can write, retrieve, validate, and connect directly. This leaves Basic Memory ahead on first-class observation ergonomics and attachable schema behavior, and it makes Exomem's semantic language less available to tooling than its underlying graph architecture warrants.

## What Changes

- Add a compact observation grammar compatible with `- [category] content #tags (context)` while preserving ordinary Markdown and excluding task checkboxes.
- Normalize compact observations and existing rich semantic blocks into one derived semantic-unit model with parent memory identity, source anchor, raw/authored-key/resolved category identity, governed kind, tags, context, provenance, lifecycle state, and typed relations where authored.
- Keep categories open by default, preserve raw labels, and add optional proposal-first category governance through aliases, deprecation, scope, and saved memory contracts. Existing governed semantic-block kinds remain the stronger epistemic axis.
- Index semantic units as first-class lexical, vector, and graph records without replacing Markdown as the source of truth or adding a server-side reasoning model.
- Extend recall with exact category and kind filters plus explicit page, unit, and mixed result levels. Category-only recall returns matching units and cites their parent page and anchor.
- Extend read/context responses with bounded semantic units and make unit references addressable through durable parent-memory references plus anchors.
- Enforce one pure semantic write contract across remember, replace, edit, Tier-2 writes, adoption, watcher, reconcile, and direct-editor drift, with explicit precommit/posthoc lifecycle applicability: valid unit syntax, saved schema policy, and a current relation-review disposition for governed compiled pages.
- Add an atomic validate-then-commit protocol for reviewed-none creation, so disconnected pages can be created without fake edges and without allowing a review decision to drift from the exact page identity/content it approved.
- Preserve existing vaults without bulk rewrites. Existing compact observations are indexed opportunistically; existing relation/category debt enters review and migration queues; genuinely empty vaults bootstrap without fabricated placeholders.
- Extend memory-schema inference, validation, and diff to categories and semantic-unit kinds, and allow explicitly saved contracts to run in off, warn, or strict writer modes. Out-of-band edits are never destroyed; strict violations are surfaced during watcher/reconcile and remain repairable.
- Add a deterministic direct benchmark that runs Exomem and a sibling Basic Memory checkout against isolated, product-native temporary corpora. It predeclares contender-neutral outcomes and permits only a scoped, revision-bound semantic-governance advantage claim from recorded evidence—never overall product superiority.
- Update the portable agent contract and generated MCP, REST, CLI, OpenAPI, and capability documentation so agents consistently speak the semantic language instead of treating it as hidden Markdown convention.

## Capabilities

### New Capabilities

- `semantic-unit-language`: Compact observations, rich semantic blocks, normalization, identity, category governance, and Markdown compatibility.
- `semantic-unit-retrieval`: First-class unit indexing, exact category/kind filters, result levels, citations, and graph/context participation.
- `semantic-write-contract`: Shared write/edit/reconcile validation, relation-review disposition, saved-schema enforcement, adoption, and migration behavior.
- `semantic-language-benchmark`: Isolated Basic Memory comparison fixtures and a no-regression/scoped semantic-governance outcome gate for the core semantic product.

### Modified Capabilities

- `agent-bootstrap-contract`: Teach agents the compact/rich semantic language, category versus kind, canonical relations, and review-before-governance behavior.
- `command-surface`: Expose semantic-unit filters, result levels, schema controls, and response fields consistently across MCP, REST, CLI, OpenAPI, and generated docs.
- `context-packs`: Include bounded, cited semantic units and their authored relations without duplicating parent-page context.
- `live-index-freshness`: Keep semantic-unit lexical, vector, and graph records generation-stamped and reject stale/mixed records across file creates, edits, moves, deletes, watcher events, and reconcile.

## Impact

- Affects Markdown parsing, semantic blocks, relation validation, memory schemas, note/write feedback, writer lifecycle paths, watcher/reconcile, lexical and embedding sidecars, epistemic graph nodes, recall ranking/result envelopes, context packs, product command registration, CLI/REST/MCP schemas, scaffold guidance, audits, adoption, and benchmarks.
- Adds a rebuildable derived semantic-unit index/schema migration; Markdown remains canonical and existing pages remain readable without migration.
- Introduces no required model. Lexical parsing/filtering is deterministic; optional embeddings reuse Exomem's existing measurement-only embedding path and soft-fail to lexical retrieval when unavailable.
- Requires isolated direct-contender tests against the sibling Basic Memory checkout for end-to-end claims; no live user vault is ever handed to Basic Memory.
