## MODIFIED Requirements

### Requirement: Deterministic Referent Cue Detection
The system SHALL detect entity cues from a closed person-noun set, resolver-local supplementary nouns, and every active core or vault-defined entity type's aliases, cue nouns, ID, label, and folder. Cue maps SHALL be cached by entity registry identity, SHALL give registry values precedence on conflicts, SHALL bind counts only within three preceding tokens, and SHALL run no model.

#### Scenario: Counted plural cue
- **WHEN** a query says "my two coastal friends"
- **THEN** the cue identifies person referents with expected count two

#### Scenario: Every active registry entity type stays type-constrained
- **WHEN** the cue noun names any active core or vault-defined entity type
- **THEN** candidates are restricted to that entity type unless exact-name rules apply
- **AND** organization, library, decision, concept, person, and a synthetic `place` cue are covered by tests or the unchanged benchmark

#### Scenario: Every registry entity type stays type-constrained
- **WHEN** the cue noun names any entity type in the registry
- **THEN** candidates are restricted to that entity type unless exact-name rules apply
- **AND** organization, library, decision, concept, and person cues are covered by the benchmark


### Requirement: Entity Registry Enumeration Avoids Corpus Walks
The entity registry SHALL enumerate every active core and vault-defined entity folder and SHALL cache immutable records by vault root, entity registry identity, and KB projection key, rebuilding only when one of those keys changes.

#### Scenario: Warm checkpoint
- **WHEN** two cue queries share one freshness key and entity registry identity
- **THEN** entity pages are enumerated only on the first query

#### Scenario: Extension registry change invalidates enumeration
- **WHEN** `_Schema/entity-types.yaml` changes its content hash
- **THEN** the next query rebuilds enumeration and includes the active extension folders
