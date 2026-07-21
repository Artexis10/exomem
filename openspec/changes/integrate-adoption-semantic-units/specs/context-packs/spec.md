# Delta: context-packs — adoption work-item consumer

## ADDED Requirements

### Requirement: Adoption work items are a governed pack consumer

Semantic unit context packs SHALL be available to `adoption_studio(action="work-item")` as a read-only consumer: pack assembly for a governed Source under adoption SHALL reuse the same pack construction and bounding rules as the primary pack surface, with the work item's caps applied on top. Pack assembly SHALL never mutate units, indexes, or pages.

#### Scenario: Pack construction is shared, bounds are the consumer's

- **WHEN** a work item assembles the pack for a bound source
- **THEN** the pack's content matches what the primary pack surface would return for that source
- **AND** the stricter of the two bounds (pack surface vs work item caps) applies

#### Scenario: Assembly is read-only

- **WHEN** packs are assembled for a work item
- **THEN** no vault file, index, or unit record is written
