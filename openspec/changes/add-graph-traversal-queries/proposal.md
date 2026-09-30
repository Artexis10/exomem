## Why

Agents need to ask how knowledge, evidence and collection rows connect, across multiple typed hops, with a quotable explanation. Existing graph recall exposes narrower neighbourhoods and relation filters than a path/pattern query engine. The derived graph already supplies epistemic semantics and convergence proofs; extending its query model preserves those advantages while making its capabilities useful to agents and the context compiler.

## What Changes

- Extend sibling [`add-collection-query-engine`](../add-collection-query-engine/proposal.md)'s engine-neutral IR with bounded typed traversal, shortest/all paths, pattern matching, path aggregation and joins through declared collection/page links.
- Compile SQLite recursive CTEs over admitted nodes/edges, applying release decisions at every hop; a withheld node, edge or edge-author page breaks a path as if absent.
- Return ordered paths with relation definitions, direction, source/evidence and authored currency as answer provenance, never infer a relationship beyond the stored edges.
- Make dynamic ontology a versioned first-class projection: stable relation/category/entity-kind identities, audience-qualified transitive subtypes and is-a/part-of hierarchies, rename/merge compatibility and retired-type historical matching without edge coalescing. Derive closure only over admitted terms/hierarchy assertions/author/source obligations; separate private projection freshness from released-dependency cursor validity.
- Move derived graph nodes/edges, replay metadata and vocabulary projection into the per-vault SQLite collection store, retaining Markdown/registry authority, freshness, convergence and fail-closed replay.
- Expose every graph/ontology capability through existing MCP tool names, schema_memory query-engine/ontology inspect reads, mixed-view inventory/inspect and generic skill/bootstrap route stubs, preserving connect_memory's text query via a separate operation-validated query_request envelope and the shared query lifecycle. Add graph-aware activation within the collection change's shared 30 ms stage budget.
- Benchmark against a pinned Neo4j baseline on the same invented graph, requiring matched query/path results and latency plus better governed epistemic/context task outcomes. Specify honest limits and a backend-neutral path to hosted billion-edge tenants.

## Capabilities

### New Capabilities

- `graph-traversal-queries`: governed typed traversal, paths/patterns/aggregation, ontology identity/versioning, unified store projection, agent/context integration and comparative performance gates.

### Modified Capabilities

None. Existing neighbourhood/recall/replay contracts remain binding. This capability extends their logical operations and migrates derived placement only after equivalent freshness and semantic proofs.

## Impact

- Depends on `add-collection-query-engine` Q1 for typed IR/admission/limits and Q5 for integrated lifecycle/views; the first real-vault NEW Records summary slice S1 does not depend on this change; storage migration uses parent `move-structured-collections-to-sqlite` P1a and public declarations wait for P4/P5. Graph queries can remain dark until those interfaces ship.
- Affects `epistemic_graph.py`, `graph_sync.py`, traversal/relation/entity registries, shared query compiler/store migrations, `connect_memory`, context working-set consumers, skill/bootstrap and graph/activation benchmarks. This lane creates planning artifacts only.
- One live store per vault, pure substrate, no raw SQL/Cypher, no new tool names, no cross-vault traversal, no server database shipped here. Knowledge and vocabulary authoring sources stay governed Markdown/registries; graph/vocabulary rows are rebuildable projections.
- Existing frozen adapter contracts, schema/core byte budgets, publication/lease/portability rules and #1453 authored-currency semantics remain mandatory. This is a sibling delivery phase of the same query-engine direction, not an unrelated storage redesign.
