# referent-resolution Specification

## Purpose
TBD - created by archiving change add-referent-resolution. Update Purpose after archive.

## Requirements

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

### Requirement: Evidence Kinds Are Categorical And Transient
Evidence SHALL use only exact-name, fuzzy-name, retrieval, graph, and attribute categories, SHALL contain no confidence floats, and SHALL never be written to the vault.

#### Scenario: Evidence block emitted
- **WHEN** an entity is evaluated from authored recall state
- **THEN** its evidence is categorical, integer/string valued, and response-only

### Requirement: Resolution Requires Exact Name Or Two Independent Evidence Kinds
An active entity SHALL resolve by exact title/alias alone, or by cue-type match plus at least two distinct non-exact evidence kinds. A cue's qualifiers SHALL be the deduplicated contiguous run of non-stopword, non-count tokens immediately preceding the cue noun within a three-token window. When qualifiers are non-empty, a non-exact resolution SHALL additionally require qualifier-bearing evidence: attribute evidence matching at least one qualifier, or graph evidence seeded by an active top-ten anchor whose title or body matches at least one qualifier using the attribute stem/prefix rules. The wider descriptor set SHALL continue to supply attribute evidence but SHALL NOT activate the gate. One kind or evidence that fails the qualifier gate SHALL remain a candidate; inactive or mismatched entities SHALL be dropped unless exact-name rules apply. Fuzzy-name evidence SHALL NOT be qualifier-bearing by itself. When qualifiers are empty, including post-nominal phrasing, the pre-change exact-name-or-two-kinds rule SHALL apply unchanged.

#### Scenario: Retrieval alone abstains
- **WHEN** a person entity appears only as a recall hit
- **THEN** it is a candidate and is not resolved

#### Scenario: Counted qualifier distractor remains unresolved across trailing topic words
- **WHEN** "my two verdant friends route timing" expects two friends and carries the qualifier "verdant"
- **AND** one friend has qualifier-bearing attribute evidence
- **AND** another friend has cue-noun attribute evidence plus graph evidence from a route-and-timing anchor without "verdant"
- **THEN** only the qualifier-bearing friend resolves
- **AND** the other friend remains a candidate
- **AND** status is partial with unresolved_count one

#### Scenario: Qualifier-bearing graph anchor qualifies
- **WHEN** a non-exact candidate has two independent evidence kinds including graph evidence
- **AND** the graph seed anchor title or body carries a cue qualifier under the attribute stem/prefix rules
- **THEN** the graph evidence satisfies the qualifier gate

#### Scenario: Unqualified cue preserves two-kind resolution
- **WHEN** a cue has no qualifiers
- **AND** a type-matching entity has two distinct non-exact evidence kinds
- **THEN** the entity resolves under the existing two-kind rule

#### Scenario: Post-nominal words fall back to the existing rule
- **WHEN** qualifying or topical words occur only after the cue noun
- **THEN** they remain wide descriptors but do not become qualifiers
- **AND** non-exact promotion uses the existing two-kind rule

#### Scenario: Exact name bypasses qualifier gate
- **WHEN** an active entity title or alias exactly matches the query
- **THEN** it resolves regardless of qualifier-bearing evidence
- **AND** graph evidence retains the first edge under the pre-change deterministic order

#### Scenario: Fuzzy name alone does not bear a qualifier
- **WHEN** a fuzzy entity-name match and one other non-qualifier evidence kind are present for a cue with qualifiers
- **THEN** the entity remains a candidate unless attribute or graph evidence bears a cue qualifier

#### Scenario: Either genuine qualifier can satisfy the gate
- **WHEN** a cue has the pre-nominal qualifiers "japanese" and "hiking"
- **AND** separate candidates have two evidence kinds and attribute evidence matching either qualifier
- **THEN** either qualifier satisfies the gate for its candidate

### Requirement: Expected Count Yields Resolved Partial Ambiguous Or Unresolved
The resolver SHALL emit resolved at exact count, partial with an unresolved count below it, ambiguous above it, and unresolved when no entity resolves.

#### Scenario: One of two represented
- **WHEN** one person resolves and the query expected two
- **THEN** status is partial and unresolved_count is one

#### Scenario: Candidate envelope remains bounded
- **WHEN** more than 25 entities have candidate or resolved evidence
- **THEN** each list contains its first 25 paths in deterministic order
- **AND** omitted_candidate_count reports the total omitted remainder

#### Scenario: Active release gate hides vault-wide counters
- **WHEN** policy or lifecycle tombstones activate the release gate
- **THEN** the referents block carries neither reasons nor omitted_candidate_count
- **AND** those keys remain absent regardless of whether this query withheld a match

### Requirement: Referents Never Reorder Or Alter Hits
The referent stage SHALL run after release annotation, SHALL never enter the hit cache, and SHALL omit itself without changing hits on disablement or error.

#### Scenario: Kill switch
- **WHEN** EXOMEM_DISABLE_REFERENTS is enabled
- **THEN** hits are byte-identical and no referents block or timing stage appears

### Requirement: Graph Corroboration Is Optional And Ablatable
Graph evidence SHALL use the top ten released hits as its bounded prefix, SHALL ignore any superseded hit inside that prefix, and SHALL make one sidecar neighbor call. It SHALL be absent when graph is false or unavailable.

#### Scenario: Graph off
- **WHEN** the existing graph argument is false
- **THEN** no graph evidence appears while non-graph resolution remains available

### Requirement: Entity Registry Enumeration Avoids Corpus Walks
The entity registry SHALL enumerate every active core and vault-defined entity folder and SHALL cache immutable records by vault root, entity registry identity, and KB projection key, rebuilding only when one of those keys changes.

#### Scenario: Warm checkpoint
- **WHEN** two cue queries share one freshness key and entity registry identity
- **THEN** entity pages are enumerated only on the first query

#### Scenario: Extension registry change invalidates enumeration
- **WHEN** `_Schema/entity-types.yaml` changes its content hash
- **THEN** the next query rebuilds enumeration and includes the active extension folders

### Requirement: Referent Benchmark Fixture And Floors
A deterministic synthetic graph-on/off fixture SHALL enforce set accuracy at least 0.9, false-resolution rate zero, abstention and partial accuracy one, and graph incremental value at least one.

#### Scenario: Benchmark check
- **WHEN** the benchmark runs with --check
- **THEN** it exits successfully only when every aggregate floor holds
