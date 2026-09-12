## ADDED Requirements

### Requirement: Collection manifests may declare and derive claimed domain vocabulary
A collection manifest MAY carry an optional `claims` block with bounded lists of `tags`, `terms`, `entity_types` and `evidence_kinds` (at most 24 entries each), validated with the manifest and round-tripped unchanged through inspection and `describe`. The system SHALL compute a collection's effective claims as the declared claims plus derived claims taken from the collection's own items: observed values of enum fields, observed values of string fields with a bounded number of distinct values, and values of a declared array-of-string tags field. Effective claims SHALL be normalised deterministically, SHALL exclude breadth vocabulary, SHALL be maintained as a projection rebuilt by reconcile and folded incrementally by record writes without re-reading the collection, and SHALL be disclosed only for manifests the requesting audience may read. A collection whose effective claims hold fewer than two terms, or whose lifecycle is not active, SHALL NOT be a routing target.

#### Scenario: Declared claims round-trip
- **WHEN** a manifest declares `claims` with tags and terms
- **THEN** validation accepts it, inspection returns the block unchanged, and `describe` documents its shape and bounds

#### Scenario: Oversized or malformed claims refuse
- **WHEN** a `claims` list exceeds 24 entries or carries a non-string entry
- **THEN** manifest validation refuses naming the list and the offending entry

#### Scenario: An existing collection derives claims without editing
- **WHEN** a manifest without `claims` holds items whose enum field takes three values and whose tags field carries recurring tags
- **THEN** after reconcile the collection's effective claims contain those values and tags, and a free-text field with many distinct values contributes nothing

#### Scenario: A record write folds its values into the projection
- **WHEN** a record is appended with an enum value now recurring on two items
- **THEN** the effective claims include the value without the collection being re-read, and a later reconcile produces the same set

#### Scenario: Withheld manifests contribute no claims
- **WHEN** governance withholds a manifest from the requesting audience
- **THEN** no advisory, count or candidate served to that audience names that collection or reveals its claims
