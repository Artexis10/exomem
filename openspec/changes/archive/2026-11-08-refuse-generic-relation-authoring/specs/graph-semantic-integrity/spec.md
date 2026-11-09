## MODIFIED Requirements

### Requirement: Similarity-only suggestions remain semantically neutral

The system SHALL represent candidates produced only by shared-source or embedding-proximity measurement as candidates that name no relation.
Such a candidate SHALL carry the ranked specific relations that fit its pages and the plain link, and accepting it SHALL need a chosen relation or the plain link.
It MUST NOT propose `refines`, `supports`, `contradicts`, or another directional epistemic relation unless the candidate method observes evidence for that meaning.
The candidate's internal identity SHALL NOT change, so a review decision recorded against it still applies.

#### Scenario: Embedding proximity does not assert refinement

- **WHEN** an embedding-neighbour candidate is returned without directional evidence
- **THEN** it names no relation, its method remains `embedding_proximity`, and its similarity evidence is retained
- **AND** it carries the ranked options and the plain link

#### Scenario: Shared source does not assert refinement

- **WHEN** two notes cite the same source and no directional relation is observed
- **THEN** the shared-source candidate names no relation and identifies the shared source in its evidence

#### Scenario: Accepting needs a choice

- **WHEN** an agent accepts a similarity-only candidate without naming a relation
- **THEN** the call is refused with `RELATION_CHOICE_REQUIRED` and the page is unchanged

#### Scenario: Explicit evidence keeps its observed semantics

- **WHEN** a candidate comes from an explicit wikilink or frontmatter source field
- **THEN** it retains the existing `links_to` or `derived_from` relation type respectively

### Requirement: Structural Co-Participation Suggestions Remain Semantically Neutral

A structural suggestion method that observes only co-participation — two pages
carrying the same open question, or two pages whose units answer or resolve the
same target — SHALL name no relation, and SHALL carry the ranked specific
relations and the plain link as a similarity-only candidate does. It MUST NOT
propose `duplicates`, `refines`, `supports`, `contradicts`, or any other
directional epistemic relation, because a page-level target cannot express a
claim that holds only between two semantic units.

#### Scenario: Shared open question does not assert duplication

- **WHEN** two pages carry the same normalized open question and no directional relation between the pages is observed
- **THEN** the shared-open-question candidate names no relation and identifies the shared question and the other page's question unit in its evidence

#### Scenario: Shared resolution target does not assert refinement

- **WHEN** two pages each carry a unit-level `answers` or `resolves` edge to the same target
- **THEN** the shared-resolution-target candidate names no relation and identifies the shared target and both sides' relation kinds in its evidence
