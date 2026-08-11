## Why

A full epistemic graph rebuild currently runs inside the vault mutation boundary, so one missing, corrupt, schema-mismatched, or registry-invalidated sidecar blocks every vault mutation for the full rebuild. Measured first-write cost is 7.7 seconds at 500 pages and 32.4 seconds at 2,000 pages; CI has observed a 172-second boundary hold at 8,000 pages. A client timeout does not cancel that server work, so the live boundary looks abandoned even while the origin thread is still rebuilding.

The graph must still be rebuilt across the complete vault before the triggering write returns. Removing that contract was attempted in #346 and correctly rejected. The missing protocol is a durable handoff: canonical bytes and an exact graph-sync checkpoint publish together, the mutation boundary releases, and only then may the request wait for derived graph work.

## What Changes

- Publish one content-free graph-sync checkpoint in the same guarded canonical batch as every graph-relevant vault mutation.
- Release the vault mutation boundary before starting or joining full graph work; a request may still wait off-boundary so its terminal preserves the existing graph-available contract.
- Build a full graph into a temporary database, stabilize it against the exact durable checkpoint/current vault, then acquire the boundary only for the final checkpoint recheck and atomic sidecar swap.
- Make concurrent rebuilds single-flight per vault. Later canonical batches can publish new checkpoints while callers join the same rebuild; the builder retries until it consumes the latest stable checkpoint.
- Persist the consumed checkpoint identity in the graph sidecar and recover a current-but-unacknowledged checkpoint after crash/restart or reconcile.
- Return a committed canonical terminal with explicit derived-graph failure when stabilization is exhausted, so retry cannot duplicate the canonical write and graph availability is never fabricated.
- Sweep abandoned temporary rebuild databases during reconcile.

## Capabilities

### New Capabilities

- `graph-rebuild-availability`: durable canonical-to-graph handoff, off-boundary single-flight rebuild, crash recovery, and non-partial publication.

### Modified Capabilities

None.

## Impact

Affected areas are canonical batch publication/post-commit handoff, `src/exomem/index_sync.py`, `src/exomem/deferred_index.py`, `src/exomem/epistemic_graph.py`, writer terminal coordination, reconcile/startup recovery, and graph freshness/boundary tests.

No user-authored Markdown schema, MCP selector, OAuth flow, or graph query shape changes. The graph SQLite schema adds only derived checkpoint metadata. A hidden content-free checkpoint sidecar is operational state, not user knowledge or a semantic index candidate.
