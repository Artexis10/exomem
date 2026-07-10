## Why

Exomem's broad epistemic corpus spans domains whose useful relation vocabulary
cannot fit one permanent global enum, while unconstrained free-form labels would
fragment meaning and make traversal unreliable. The graph needs a governed
extension model that preserves a portable epistemic core, learns what each
corpus actually uses, and lets callers select deterministic traversal lenses
without turning relation labels into unreviewed truth.

## What Changes

- Replace duplicated hard-coded relation enums with one versioned core relation
  registry shared by semantic parsing, graph indexing, validation, and context.
- Add optional vault- and project-scoped relation extensions using namespaced
  identifiers and explicit mappings to a core parent relation.
- Preserve unregistered observed relation labels in derived graph state with
  source provenance and audit findings instead of silently dropping them.
- Infer relation frequencies and extension candidates from a selected corpus,
  but require explicit, hash-guarded adoption before an extension becomes
  registered.
- Add built-in and user-governed traversal profiles for epistemic, provenance,
  causal, decision, and unrestricted graph context.
- Extend schema governance and context routes through the shared product
  registry; normal writes remain permissive and existing relation syntax stays
  compatible.
- Keep deterministic parsing and traversal inside the pure substrate. Any
  optional model-backed parent suggestion is default-off, response-only,
  soft-failing, and can never register a relation automatically.

## Capabilities

### New Capabilities

- `epistemic-relation-registry`: Versioned core relations, governed scoped
  extensions, alias/deprecation behavior, unknown-observation preservation,
  corpus inference, and registry validation.
- `graph-traversal-profiles`: Built-in and governed traversal lenses that select
  relation families, directions, priorities, depth, and hard bounds without
  changing stored knowledge or default retrieval ranking.

### Modified Capabilities

- `context-packs`: Context assembly can apply a traversal profile and reports
  canonical relation definitions, extension ancestry, unknown relations, and
  profile truncation.
- `live-index-freshness`: Registry/profile changes invalidate derived graph
  state and are detectable and repairable through audit/reconcile.
- `command-surface`: Schema governance and context parameters remain identical
  across MCP, REST, CLI, OpenAPI, annotations, and generated capability docs.

## Impact

The change affects semantic relation parsing, epistemic graph schema/rebuilds,
schema inference and persistence, context assembly, audit/reconcile, the command
registry, generated schemas/docs, the generic scaffold, and graph-quality tests.
It adds generic governed YAML under `Knowledge Base/_Schema/` while leaving
Markdown canonical and graph databases derived. The change builds on merged PR
#182 (`close technical memory gaps`) for stable references, unified context, and
schema-governance surface integration.
