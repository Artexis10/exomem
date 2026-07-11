## ADDED Requirements

### Requirement: Canonical authored relations participate in memory contracts

The system SHALL treat resolved canonical note-level `## Relations` edges and resolved block-level semantic relations as observed typed relations when inferring, validating, or diffing memory contracts. It MUST NOT treat generic wikilinks or unregistered relation labels as canonical contract relations.

#### Scenario: Canonical note relation is inferred

- **WHEN** every eligible page in a contract corpus contains a canonical `## Relations` edge of the same registered type
- **THEN** contract inference reports that relation type with full occurrence frequency and applies the existing conservative requiredness rule

#### Scenario: Canonical relation satisfies validation

- **WHEN** a saved contract requires a registered relation and a matching page expresses it through canonical `## Relations` syntax
- **THEN** contract validation considers the requirement satisfied

#### Scenario: Relation drift includes canonical syntax

- **WHEN** canonical note-level relation usage is added to or removed from the current corpus
- **THEN** contract diff reports the corresponding relation change without modifying any page or contract

### Requirement: Similarity-only suggestions remain semantically neutral

The system SHALL represent candidates produced only by shared-source or embedding-proximity measurement as the registered symmetric relation `relates_to`. It MUST NOT propose `refines`, `supports`, `contradicts`, or another directional epistemic relation unless the candidate method observes evidence for that meaning.

#### Scenario: Embedding proximity does not assert refinement

- **WHEN** an embedding-neighbour candidate is returned without directional evidence
- **THEN** its proposed relation type is `relates_to`, its method remains `embedding_proximity`, and its similarity evidence is retained

#### Scenario: Shared source does not assert refinement

- **WHEN** two notes cite the same source and no directional relation is observed
- **THEN** the shared-source candidate proposes `relates_to` and identifies the shared source in its evidence

#### Scenario: Explicit evidence keeps its observed semantics

- **WHEN** a candidate comes from an explicit wikilink or frontmatter source field
- **THEN** it retains the existing `links_to` or `derived_from` relation type respectively

### Requirement: Relation suggestions remain proposal-only

Changing similarity candidate types SHALL NOT write Markdown, mutate accepted graph edges, change retrieval ranking, invoke a reasoning model, or alter candidate ordering beyond deterministic deduplication caused by the corrected type.

#### Scenario: Neutral suggestion call is non-mutating

- **WHEN** relation suggestions return shared-source or embedding-proximity candidates
- **THEN** the response reports `mutated=false` and the vault and graph sidecar remain unchanged
