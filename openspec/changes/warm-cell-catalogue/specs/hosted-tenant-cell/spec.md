# hosted-tenant-cell Specification Delta

## ADDED Requirements

### Requirement: A cell adopts a staged catalogue at boot instead of rebuilding

Before rebuilding its retrieval catalogue, a hosted cell SHALL look for a staged sibling catalogue on its volume whose stored identity equals the identity the cell's own code computes. On a match the cell SHALL validate the staged file, rename it into the live sidecar's place in one atomic operation, and serve from it, reconciling the freshness lag between the staged checkpoint and the live vault through the ordinary delta path. Without a match, or on any validation failure, the cell SHALL rebuild as it does today.

#### Scenario: The warm-built catalogue is adopted

- **WHEN** a cell boots on an image whose computed catalogue identity equals a staged sibling's stored identity
- **THEN** the cell renames the sibling into the live sidecar's place and serves from it
- **AND** the freshness lag the staged checkpoint carries is caught up by delta reconcile, not a full rebuild

#### Scenario: A staged sibling fails validation

- **WHEN** a staged sibling exists for the cell's computed identity but cannot be opened, or fails the catalogue's schema or current checks
- **THEN** the cell discards it and rebuilds cold

#### Scenario: No staged sibling

- **WHEN** no staged sibling carries the cell's computed catalogue identity
- **THEN** the cell rebuilds the catalogue on first start exactly as it does today

### Requirement: A booting cell sweeps dead staged siblings

At boot a hosted cell SHALL remove staged siblings whose stored identity matches neither the live sidecar's nor the identity its own code computes.

#### Scenario: A superseded target's sibling is removed

- **WHEN** a staged sibling belongs to a target the cell never booted into
- **THEN** the cell removes it at boot
