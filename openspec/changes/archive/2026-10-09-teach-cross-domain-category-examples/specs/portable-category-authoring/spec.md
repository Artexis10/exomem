## MODIFIED Requirements

### Requirement: Rich Semantic Teaching Examples

The canonical full guidance SHALL include compact paired examples and a rich example
demonstrating category, governed kind, tags, stable identifier, and typed relations.
The role and domain compact examples and the rich example SHALL use non-software life
domains, and the rich example SHALL retain identical feature coverage (governed kind,
stable identifier, tags, typed relation with a governed-relative wikilink target, and
category defaulting to kind). It SHALL discourage a redundant explicit rich category
when category equals kind, and SHALL encourage several non-duplicative observations and
relations only when the note actually contains them.

#### Scenario: Rich kind and category do not duplicate accidentally

- **WHEN** an agent writes a rich `## Decision` whose intended category is also `decision`
- **THEN** guidance tells it to omit redundant `- category: decision`
- **AND** inference and retrieval still treat the defaulted category as core `decision`

#### Scenario: Primary examples are not software-flavored

- **WHEN** the canonical contract renders its role example, domain example, and rich example
- **THEN** none of them uses a software or infrastructure domain
- **AND** each still parses to valid semantic content demonstrating the same contract features as before

## ADDED Requirements

### Requirement: Cross-Domain Teaching Breadth

The canonical contract SHALL carry a bounded `breadth` example set of exactly four
compact lines spanning at least three distinct non-software life domains plus exactly
one software line. Every breadth line MUST parse to exactly one valid semantic unit; at
least two MUST resolve to a core category and at least one MUST resolve `unregistered`,
demonstrating both role-first selection and the open-vocabulary domain escape. The
breadth set SHALL render in the projected contract block and in every bootstrap profile,
and MUST NOT appear in the bounded per-tool write guidance. Teaching breadth MUST remain
advisory: no ranking boost, no write rejection, and no registry mutation.

#### Scenario: Breadth set spans life domains and stays parseable

- **WHEN** the canonical contract renders its breadth examples
- **THEN** the set contains exactly four compact lines covering at least three distinct non-software domains and exactly one software line
- **AND** each line parses to exactly one valid semantic unit

#### Scenario: Breadth proves the open-vocabulary escape

- **WHEN** the breadth examples resolve through the category registry
- **THEN** at least two resolve with status `core`
- **AND** at least one resolves with status `unregistered` without rejection or coercion

#### Scenario: Breadth ships in bootstrap but not per-tool guidance

- **WHEN** bootstrap renders either the full or compact profile and a write tool renders its bounded guidance
- **THEN** both bootstrap profiles include the breadth examples
- **AND** the bounded per-tool guidance does not
