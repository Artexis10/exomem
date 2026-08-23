## ADDED Requirements

### Requirement: The vault projector exposes structured collections

The neutral vault projection SHALL include a `collections` section listing every structured collection the projecting audience may see: its profile, its manifest reference, and per item its key, its lifecycle and status where the profile declares them, and its natural-key values. The section SHALL be additive and versioned: a vault without collections SHALL project byte-identically to the previous projection apart from the empty section, and no family, assertion, predicate or gate SHALL change with it. Registering a lifecycle-routing family remains a §7 amendment and is not performed by this delivery.

#### Scenario: A Planning and Records pair is visible

- **WHEN** a seeded vault holds one Planning collection and one Records collection
- **THEN** the projection lists both with their items, and a comparator can diff two projections on item keys, statuses and values without reading the vault

#### Scenario: Nothing else moves

- **WHEN** the projector runs over a vault with no structured collections
- **THEN** every pre-existing section is unchanged and the registry, receipts and drift check report no difference
