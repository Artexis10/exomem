## ADDED Requirements

### Requirement: Reconcile Refreshes Complete Navigation Counts

The reconcile operation SHALL recompute Sources, Notes, and Entities totals and known per-type counts from disk. It SHALL update the top-level index and each existing Sources, Notes, and Entities sub-index while preserving curated descriptions and recent-activity content.

#### Scenario: Sources index drift is reconciled

- **WHEN** source files were added or removed outside Exomem and `reconcile` runs
- **THEN** `Sources/index.md` by-type counts and the top-level Sources total match on-disk source files

#### Scenario: Top index exposes real totals

- **WHEN** the vault contains notes and entities across several types
- **THEN** the top index reports total Notes and total Entities counts
- **AND** any retained per-type count rows match their corresponding on-disk types

### Requirement: Writers Insert Missing Total Count Rows

Normal governed writers that refresh navigation SHALL update existing count rows and SHALL insert missing `Sources`, `Notes`, or `Entities` total rows into a valid Counts section rather than silently leaving incomplete totals.

#### Scenario: Legacy scaffold has only subtype rows

- **WHEN** a legacy top index contains `Notes (insight)` and `Entities (concept)` but no total rows
- **AND** a governed writer refreshes counts
- **THEN** total Notes and Entities rows are inserted with correct on-disk totals
- **AND** the existing subtype rows remain accurate
