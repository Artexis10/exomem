## Why

The shared public-MCP Markdown workflow still takes 53.60 seconds at 8,000
notes on the current implementation. Validation and mutation preflight account
for most of that delay. Graph maintenance also rereads unrelated Markdown to
discover links affected by a newly appearing target. Both costs grow with the
vault even when a request changes only a few notes.

Basic Memory demonstrates the value of retaining parsed state and coalescing
derived work. Exomem should apply that approach while preserving its canonical
file durability, semantic authoring checks, policy boundaries, and media custody.

## What Changes

- Reuse proven current writer metadata during validation and edits instead of
  rediscovering it from every Markdown file.
- Persist raw link dependencies in the existing disposable SQLite graph index,
  including unresolved links, so topology repair can select affected sources
  without reading unrelated Markdown bodies.
- Retain exact checkpoint, source-version, policy, and publication proofs;
  missing or stale projections continue to require recovery.
- Track reproducible before/after public-MCP measurements against the pinned
  Basic Memory release, with startup, foreground closure, and graph convergence
  reported separately. Set explicit shared-workload parity criteria in the design.

## Capabilities

### New Capabilities

- `proportional-write-projections`: Reuse current parsed writer metadata and
  maintain internal raw-link dependencies for bounded write and topology work.

### Modified Capabilities

- `durable-closure-performance`: Require repeatable paired measurements and
  deterministic work-count checks for the optimized shared Markdown workflow.

## Impact

The change affects writer resolver preparation, graph SQLite schema and
incremental maintenance, focused correctness/performance tests, and benchmark
evidence. Markdown remains canonical and successful commits retain the existing
durability contract. The internal dependency projection does not add public
placeholder nodes or expose policy-excluded titles through recall. Existing
deferred queues and generation fencing remain the scheduling authority.

No model inference, hosted API changes, dependency upgrades, live-cell migration,
or deployment is required. The graph sidecar is rebuildable derived state.
