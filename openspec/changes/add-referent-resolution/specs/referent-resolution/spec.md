# referent-resolution

## ADDED Requirements

### Requirement: Deterministic Referent Cue Detection
The system SHALL detect entity cues from a closed person-noun set and the existing entity-type registry aliases, SHALL bind counts only within three preceding tokens, and SHALL run no model.

#### Scenario: Counted plural cue
- **WHEN** a query says "my two coastal friends"
- **THEN** the cue identifies person referents with expected count two

### Requirement: Evidence Kinds Are Categorical And Transient
Evidence SHALL use only exact-name, fuzzy-name, retrieval, graph, and attribute categories, SHALL contain no confidence floats, and SHALL never be written to the vault.

#### Scenario: Evidence block emitted
- **WHEN** an entity is evaluated from authored recall state
- **THEN** its evidence is categorical, integer/string valued, and response-only

### Requirement: Resolution Requires Exact Name Or Two Independent Evidence Kinds
An active entity SHALL resolve by exact title/alias alone, or by cue-type match plus at least two distinct non-exact evidence kinds. One kind SHALL remain a candidate; inactive or mismatched entities SHALL be dropped unless exact-name rules apply.

#### Scenario: Retrieval alone abstains
- **WHEN** a person entity appears only as a recall hit
- **THEN** it is a candidate and is not resolved

### Requirement: Expected Count Yields Resolved Partial Ambiguous Or Unresolved
The resolver SHALL emit resolved at exact count, partial with an unresolved count below it, ambiguous above it, and unresolved when no entity resolves.

#### Scenario: One of two represented
- **WHEN** one person resolves and the query expected two
- **THEN** status is partial and unresolved_count is one

### Requirement: Referents Never Reorder Or Alter Hits
The referent stage SHALL run after release annotation, SHALL never enter the hit cache, and SHALL omit itself without changing hits on disablement or error.

#### Scenario: Kill switch
- **WHEN** EXOMEM_DISABLE_REFERENTS is enabled
- **THEN** hits are byte-identical and no referents block or timing stage appears

### Requirement: Graph Corroboration Is Optional And Ablatable
Graph evidence SHALL use at most the first ten released non-superseded anchors and one sidecar neighbor call, and SHALL be absent when graph is false or unavailable.

#### Scenario: Graph off
- **WHEN** the existing graph argument is false
- **THEN** no graph evidence appears while non-graph resolution remains available

### Requirement: Entity Registry Enumeration Avoids Corpus Walks
The entity registry SHALL cache immutable records by vault root and KB projection key and rebuild only when that key changes.

#### Scenario: Warm checkpoint
- **WHEN** two cue queries share one freshness key
- **THEN** entity pages are enumerated only on the first query

### Requirement: Referent Benchmark Fixture And Floors
A deterministic synthetic graph-on/off fixture SHALL enforce set accuracy at least 0.9, false-resolution rate zero, abstention and partial accuracy one, and graph incremental value at least one.

#### Scenario: Benchmark check
- **WHEN** the benchmark runs with --check
- **THEN** it exits successfully only when every aggregate floor holds
