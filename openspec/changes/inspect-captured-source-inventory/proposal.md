# Captured Source inventory

## Why

A service must verify its cell before sending credentials and reconcile captured revisions from complete, bounded observations.
Canonical scans, queue absence and request timeouts do not prove current inventory, exact indexing or a latest possible commit.
This change records those cross-service contracts so callers cannot mistake incomplete observation for absence.

## What Changes

- Add content-free cell discovery with configured cell identity, software identity, implemented operations and their actual limits.
- Add authenticated keyset-paged inventory by identity system and optional record, with publication completeness and exact indexing states.
- Derive inventory from the existing corpus publisher and exact embedding receipts, preserving canonical companion classification.
- Return unknown or pending state when proof is incomplete; reuse maintained background recovery without scanning on each request.
- Add explicit capture reservation and settlement under the existing canonical writer boundary; current timeouts remain acknowledgement limits.

The operations are explicit service requests. They enable no new model role, automatic content action or creator activation.
Optional proof reconstruction uses existing bounded background work; ordinary requests retain their current behavior.

## Capabilities

### New Capabilities

- `cell-service-identity`: public, content-free identity and implemented service capability discovery.
- `captured-source-inventory`: authorized canonical capture inventory and exact indexing observations with bounded traversal.

### Modified Capabilities

- `durable-source-capture`: optional server-issued attempts with exact input binding and scoped admission closure.

## Impact

The change affects the Cloud registration seam, command registry, Source classification, corpus publication and embedding publication checks.
It adds no independent persistent catalogue, canonical storage schema, model dependency, Source rewrite or lifecycle deletion.
Capture attempts extend the existing private per-replica runtime ledger with their own bounded lifecycle.
Shared writer and store changes require agreement with their current owner.
Q's off-by-default retrieval backing consumes the contract; its activation retains the existing separate authority.
