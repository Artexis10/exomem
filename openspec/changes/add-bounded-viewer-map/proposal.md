# Proposal

## Why

The approved web viewer needs a global map with truthful coverage. Seeded graph context cannot provide global counts or the most-linked pages.

## What Changes

- Add one governed read-only global map operation.
- Bound both inspection work and output, and distinguish partial scans from output truncation.
- Reuse existing graph snapshots, interpretations, traversal profiles and disclosure projection.
- Keep assistant retrieval and memory reading as the viewer's search and read operations.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `epistemic-graph`: Add a bounded global map with admitted page counts and explicit coverage.

## Impact

The change adds a map reader and additive canonical/product command registrations, plus existing governance projector mappings and generated discovery artifacts.
It does not change activation, admission, disclosure policy or the seeded graph-context contract.
The web control service consumes the map through a separate fixed owner-bound bridge. Cell deployment remains a separate release step.
