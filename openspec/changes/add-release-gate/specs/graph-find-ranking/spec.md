# graph-find-ranking

## ADDED Requirements

### Requirement: Graph expansion respects release decisions

The graph lane SHALL NOT surface a hit whose only provenance is expansion from a
seed item released below notice for the requesting audience — such hits SHALL be
dropped unless they matched a retrieval lane on their own. Graph provenance
annotations (seed page, relation match, supersession pointers) SHALL be stripped
from any returned hit when they name a sub-notice item, and `graph-context` SHALL
apply the same guard to its seeds, nodes, and edge endpoints.

#### Scenario: Withheld seed does not smuggle neighbours

- **WHEN** a restricted page is a strong match and its graph neighbour would
  surface only via expansion from it
- **THEN** the neighbour is dropped, and it returns only if it matched a lexical
  or vector lane on its own

#### Scenario: Seed annotations never name withheld pages

- **WHEN** a permitted hit carries a graph provenance annotation referencing a
  sub-notice page
- **THEN** the annotation is stripped from the response

#### Scenario: Ungoverned expansion unchanged

- **WHEN** no governance rule matches any seed or neighbour
- **THEN** graph expansion and its annotations are identical to current behavior
