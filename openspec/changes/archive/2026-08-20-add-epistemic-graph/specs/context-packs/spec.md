## ADDED Requirements

### Requirement: Opt-In Typed Graph Neighborhoods In Packs
The system SHALL allow `find(pack=true)` to include a typed graph neighborhood when graph enrichment is explicitly requested and the epistemic graph sidecar is available. This enrichment SHALL be additive: `find(pack=false)` remains unchanged, default pack behavior remains compatible with the existing pack contract, and `find` hit ordering remains unchanged.

#### Scenario: Pack without graph request stays compatible
- **WHEN** `find` is called with `pack=true` and graph enrichment is not requested
- **THEN** the pack response preserves the existing pack fields and behavior
- **AND** no graph-specific fields are required for the call to succeed

#### Scenario: Graph-enriched pack includes typed relations
- **WHEN** `find` is called with `pack=true` and graph enrichment requested over a vault with an available graph sidecar
- **THEN** the returned pack includes graph neighborhood data with typed nodes, typed edges, relation types, and provenance for packed paths
- **AND** the hits list, hit ordering, and existing pack claims remain unchanged

### Requirement: Graph Pack Enrichment Soft-Fails
Graph enrichment in packs SHALL soft-fail when the graph sidecar is missing, stale, disabled, or schema-incompatible. The pack response SHALL remain useful through existing structural claims, wikilink neighborhoods, and contradiction/supersession fields, and SHALL report graph availability instead of raising an unhandled error.

#### Scenario: Missing sidecar falls back to existing pack
- **WHEN** `find(pack=true)` requests graph enrichment but no graph sidecar exists
- **THEN** the pack response is produced using the existing non-graph pack assembly
- **AND** the response indicates graph enrichment was unavailable

#### Scenario: Graph enrichment does not mutate files
- **WHEN** graph-enriched pack assembly runs over a vault
- **THEN** no file under the vault is created, modified, moved, or deleted
- **AND** no graph relation suggestion is persisted as an accepted fact
