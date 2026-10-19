# Design

## Context

Seeded graph context returns no global map when it has no seed. Existing raw node and edge helpers fetch the complete graph.
See proposal.md for motivation. The existing graph reader and governance boundary already define admitted interpretations and disclosure.

## Goals / Non-Goals

The new operation supplies one bounded page map for the web viewer. Search and note reading reuse existing assistant operations.
It adds no graph database, layout engine, aggregates, reasoning model, activation behavior or new disclosure authority.

## Decisions

### Add one leaf and additive registrations

Implement `viewer_map.py::op_viewer_map` and register `viewer_map` in both canonical and product command specifications.
Use the existing structure projector and graph outcome adapter. Regenerate discovery artifacts through the existing schema generator.
Changing seeded graph context would alter established consumers; a separate leaf preserves that contract.

### Reuse a validated snapshot and admitted interpretations

Open through `EpistemicGraphIndex` with cold source proof disabled. Reuse `GraphView`, selected registry traversal and disclosure guards.
Add an optional mode to the existing snapshot opener that also disables cold projection reads; retain existing defaults for other callers.
This mode derives projection identity from one live checkpoint and rechecks that checkpoint before returning.
Keep the existing marker, publication, source-proof and graph-sync validation. A missing live checkpoint returns unavailable without a disk census.
An inherited snapshot still needs the existing remembered source proof; startup adoption alone does not establish that proof.
After validation, borrow the resident resolver at the matching current live projection, rather than requiring the writer's checkpoint lineage.
A missing resident resolver produces partial edge coverage, unknown exact totals and provisional ranking; never build it on the request path.
Admit file rows, edge authors and endpoints before reducing. Collapse semantic-unit endpoints to their page identities.
Rank by admitted incoming cross-page edges, then graph key. Keep isolated admitted pages.
Vocabulary uses instance and definition identity for drawn items only. It must not merge meanings by label.
Optional presentation roles come from registry data, never relation-name matching. They do not implement the separate display-label contract.
Unknown roles retain their identity and use neutral presentation. Stored page types and dates need no source-file reads.
Apply the final graph disclosure guard before reporting drawn counts.

### Separate scan completeness from output truncation

The typed result carries complete, partial or unavailable state, snapshot identity, observation time, nodes, edges and counts.
Exact totals cover eligible current graph file rows, rather than the separate activity count.
Complete scans can claim global ranking; interrupted scans report provisional ranking and null exact totals.
Use paged reads, explicit inspection limits and the existing request budget below the web bridge's five-second deadline.
Do not use full-fetch helpers or a Markdown census. Keep limits configurable within the bounded protocol.
Charge semantic page and edge limits only after admission; an extra admitted item establishes overflow.
Preflight every admitted author against one resident checkpoint before preparing changed sources.
Bound source size, summary size and preparation work uniformly, independent of hidden targets.
Reuse unchanged summaries; prepare changed authors through opt-in parser and graph checkpoints.
Stop interrupted authors before caching or drawing their replacement edges, and report partial coverage with unknown exact totals.
These bounds preserve the transport deadline; false early stops cost a visibly partial map.
They do not promise timing noninterference when hidden database work consumes the wall-clock budget.

### Keep identity claims precise

Retain graph node and edge keys, plus persistent memory references when available.
Path-derived keys remain stable on an unchanged snapshot. They do not promise rename persistence.

## Risks / Trade-offs

- Large reductions can exceed the request budget. Return truthful partial coverage; add aggregates only if measured volume proves the need.
- Hidden content can affect totals or ranking if filtering occurs late. Test hidden-versus-absent invariance through the governed operation.
- Source registries can share spellings with different meanings. Preserve instance provenance rather than flattening vocabulary.

## Migration Plan

1. Verify the new governed operation and generated schemas; existing operations retain their contracts.
2. Merge the reviewed source through the ordinary source delivery path; publication remains with its current owner.
3. Roll out the qualified cell image through the existing deployment owner and approved window; source presence proves no deployment.
4. Enable the fixed owner-bound web bridge only after its operation works on the selected cell image.

Rollback the new caller independently. Old callers continue to use their existing operations and graph snapshot.
