# Proposal

## Why

Service callers cannot reconcile a Source capture after losing its response once the bounded mutation replay window expires.
Durable caller identity must survive restart and remain independent of derived indexes.

## What Changes

- Accept optional text capture identity: an opaque system, record identifier and revision.
- Store identity and the original normalized capture digest in the initial canonical Source write.
- Reuse the stable Source reference indefinitely for identical authored inputs; refuse conflicting inputs without writes.
- Expose caller-supplied mutation replay keys through the MCP adapter using the existing retry engine and verified principal scope.
- Require existing whole-namespace content authority for identity lookup and recheck it before commit.
- Preserve unkeyed captures, attachment adoption, open source kinds and independent immutable revisions.

## Capabilities

### New Capabilities

- `durable-source-capture`: Vault-local caller identity for safe reconciliation of text captures after bounded replay expires.

### Modified Capabilities

- `retry-safe-mutations`: MCP callers can supply explicit replay keys scoped to verified normalized principals.

## Impact

Capture commands, canonical Source rendering, MCP adapter plumbing, generated discovery projections and focused boundary tests change.
The implementation reuses guarded storage, the mutation guard and the retry engine; it adds no journal, cache or model calls.
