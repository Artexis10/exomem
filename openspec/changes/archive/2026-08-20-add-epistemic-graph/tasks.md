## 1. Semantic Block Tests

- [x] 1.1 Add fixture coverage for Markdown sections that extract `finding`, `decision`, `risk`, and `action` blocks without mutating files
- [x] 1.2 Add fixture coverage for page-type/frontmatter extraction into page-level semantic blocks
- [x] 1.3 Add tests for stable block keys, source paths, anchors/spans, and source hashes across repeated extraction
- [x] 1.4 Add tests that unknown headings and malformed optional relation text degrade without extraction failure
- [x] 1.5 Add tests that default extraction imports no optional model dependency and model-backed suggestions soft-fail when unavailable

## 2. Semantic Block Implementation

- [x] 2.1 Add a deterministic semantic block extraction module with typed block dataclasses or equivalent structured results
- [x] 2.2 Implement heading/list/frontmatter recognition for the initial block kind vocabulary
- [x] 2.3 Reuse existing Markdown/frontmatter helpers to ignore fenced code and preserve current search behavior
- [x] 2.4 Return source-spanned block provenance with stable keys and source hashes
- [x] 2.5 Add optional model-backed block suggestion seams that are default-off and response-only

## 3. Graph Sidecar Tests

- [x] 3.1 Add tests for full graph rebuild from notes with `sources`, `supersedes`, wikilinks, and semantic sections
- [x] 3.2 Add tests that deleting and rebuilding the sidecar returns equivalent graph context
- [x] 3.3 Add tests for typed edge provenance, supported relation labels, and ignored unsupported labels
- [x] 3.4 Add tests that default graph indexing invokes no reasoning/generative model
- [x] 3.5 Add tests that optional model suggestion failure does not break deterministic graph context

## 4. Graph Sidecar Implementation

- [x] 4.1 Add the graph sidecar schema, schema-version metadata, and storage path resolution
- [x] 4.2 Implement full rebuild from Markdown files, semantic blocks, frontmatter, wikilinks, sources/evidence, supersession, and media metadata
- [x] 4.3 Implement per-path graph row refresh/delete helpers for changed or removed Markdown files
- [x] 4.4 Implement graph lookup/traversal helpers with depth, relation-type, node-type, and cap controls
- [x] 4.5 Implement deterministic relation suggestion methods for shared sources, shared entities, co-citation, wikilinks, supersession, and optional embeddings

## 5. Command Surface Tests

- [x] 5.1 Add leaf tests for `graph_context` returning bounded read-only neighborhoods with truncation
- [x] 5.2 Add leaf tests for `suggest_relations` returning proposal-only candidate edges without mutating vault files
- [x] 5.3 Add registry/surface tests proving `graph_context` and `suggest_relations` are exposed through MCP, REST, CLI, and OpenAPI from one registry entry
- [x] 5.4 Add tests for graph-unavailable responses that soft-fail with availability metadata

## 6. Command Surface Implementation

- [x] 6.1 Add `graph_context` and `suggest_relations` leaf functions over the graph helpers
- [x] 6.2 Register both operations in `commands.py` with read-only/destructive annotations and parameter schemas
- [x] 6.3 Wire CLI/REST/OpenAPI generation through the existing command registry without per-surface logic duplication
- [x] 6.4 Update MCP schema snapshots and fixture schemas for the new tools

## 7. Context Pack Integration

- [x] 7.1 Add tests that `find(pack=true)` without graph enrichment preserves the existing pack contract
- [x] 7.2 Add tests that graph-enriched packs include typed graph neighborhood data without changing hits or hit order
- [x] 7.3 Add tests that missing, stale, disabled, or schema-incompatible graph sidecars fall back to existing pack assembly with availability metadata
- [x] 7.4 Implement graph enrichment in `context_pack.py` behind explicit request and graph availability checks
- [x] 7.5 Keep `find(pack=false)` and default pack assembly behavior unchanged

## 8. Freshness, Audit, And Reconcile

- [x] 8.1 Add tests that single-file Markdown edits refresh only graph rows contributed by the affected path
- [x] 8.2 Add tests that incremental graph updates match a full rebuild over the same vault state
- [x] 8.3 Add tests that graph drift is surfaced by audit and repaired by reconcile without mutating Markdown
- [x] 8.4 Add tests that disabled graph indexing makes drift checks a no-op with no optional dependency load
- [x] 8.5 Wire writer hooks, watcher events, and reconcile cleanup into graph sidecar refresh/invalidation
- [x] 8.6 Add graph drift audit category and reconcile repair path

## 9. Documentation And Validation

- [x] 9.1 Update scaffold/operations documentation for `graph_context`, `suggest_relations`, graph enrichment, and pure-substrate constraints
- [x] 9.2 Document optional model-backed graph/block suggestion flags as default-off and soft-failing
- [x] 9.3 Run `uv run python -m pytest -q` with `EXOMEM_DISABLE_EMBEDDINGS=1`
- [x] 9.4 Run focused torch/model-backed tests desk-side when optional graph suggestion extras are enabled
- [x] 9.5 Run `ruff check`
- [x] 9.6 Run `openspec validate add-epistemic-graph`
