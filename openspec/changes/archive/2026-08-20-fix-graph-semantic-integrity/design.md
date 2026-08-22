## Context

Exomem has two authoring layers for durable typed relationships: block-level relation metadata indexed as `semantic_relation` and canonical note-level `## Relations` bullets indexed as `markdown_relation`. Memory contracts currently retain only the former origin, so the recommended note-level syntax disappears from schema inference, validation, and diff.

Relation suggestions have a separate integrity defect. Explicit wikilinks and frontmatter sources have observed semantics, but shared-source and embedding-neighbour candidates are currently emitted as `refines`. Those measurements establish adjacency or similarity, not a directional refinement claim. Suggestions are proposal-only, which prevents durable corruption, but the proposed type still misleads the reviewing agent.

The change must preserve the pure-substrate boundary: deterministic measurements may surface candidates, but semantic direction remains an authored, reviewed decision.

## Goals / Non-Goals

**Goals:**

- Make canonical note-level and block-level typed relations participate consistently in memory-contract inference, validation, and diff.
- Keep similarity-only suggestions useful while representing their semantics conservatively.
- Preserve response shape, ordering, evidence payloads, and proposal-only/non-mutating behavior.
- Cover both fixes with focused regression tests.

**Non-Goals:**

- No new relation vocabulary, schema language, graph node type, model, or sidecar migration.
- No automatic relation acceptance or conversion of generic links into durable typed edges.
- No change to frontmatter-derived provenance semantics, graph traversal, or retrieval ranking.
- No broader schema expansion such as relation cardinality or target-type constraints; those remain evidence-driven follow-on work.

## Decisions

### Treat both authored semantic origins as contract relations

Memory-contract relation collection will accept edges whose origin is either `semantic_relation` or `markdown_relation`, provided the edge resolved to a canonical relation type. This is the narrowest change that aligns contracts with the documented authoring contract while retaining legacy/block behavior.

Including every graph origin was rejected. Frontmatter provenance and generic wikilinks have distinct authoring and validation semantics and broadening contracts to them would create unrelated schema drift.

### Use `relates_to` for similarity-only candidates

Shared-source and embedding-proximity candidates will retain their methods and evidence but propose the symmetric core relation `relates_to`. Explicit wikilinks remain `links_to`; frontmatter sources remain `derived_from`.

Returning `relation_type=null` would be semantically pure but needlessly breaks callers that expect a registered candidate type. `relates_to` is the existing governed, symmetric fallback for a potentially meaningful association. It remains a proposal requiring review; callers may replace it with a more precise relation only when the note content supports that choice.

### Test public behavior through existing command leaves

Regression tests will exercise contract inference/validation through `schema_memory` behavior and suggestion output through the existing graph helper/command path. No surface-specific logic or new API is introduced.

## Risks / Trade-offs

- [Risk] `relates_to` candidates may still be low-value semantic neighbours. -> Preserve similarity evidence and proposal-only behavior; graph-value benchmarking and acceptance rates decide whether the candidate method remains useful.
- [Risk] Existing consumers expect `refines` from proximity. -> The old value is semantically incorrect; keep the response shape and method stable while changing only the proposed type.
- [Risk] Canonical relations alter inferred contract output for corpora that already use them. -> This is the intended correction; conservative requiredness rules and explicit contract saving remain unchanged.

## Migration Plan

No data migration is required. Deploy the code and rebuild nothing: contracts are inferred from Markdown on demand, and suggestions are response-only. Rollback restores the two prior classifications without modifying Markdown or sidecars.

## Open Questions

None for this slice. Relation target types, cardinality, and first-class semantic-block retrieval should be considered only after corpus activation and graph-value measurement.
