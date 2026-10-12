## Why

The relation-debt audit and retrieval graph already treat resolved body wikilinks as
real outbound connectivity, but the write-time relation disposition does not. The
remaining mismatch forces an unnecessary reviewed-none round trip for pages that are
already connected.

## What Changes

- Let a resolved outbound body wikilink to a connectable governed page satisfy the
  existing connectivity lane without constructing a `RelationFact`.
- Report the satisfaction as `qualifying_signal="connectivity"` and retain the
  non-blocking `RELATION_TYPED_EDGE_ABSENT` warning.
- Keep unresolved, ambiguous, inbound, inactive-target, and frontmatter-provenance links
  non-qualifying, and preserve the separate empty-corpus bootstrap set.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `semantic-write-contract`: Resolved outbound body wikilinks become a disposition
  signal through the existing connectivity lane.

## Impact

The change is confined to `src/exomem/semantic_contract.py`, focused connectivity-lane
tests, and this OpenSpec change. It adds no relation kind, registry lookup, dependency,
model, storage, graph edge, or tool-surface change. The write-latency and governance
overhead gates measure the implementation before acceptance.
