# graph-find-ranking Specification

## Purpose
TBD - created by archiving change fix-excluded-tier-read-paths. Update Purpose after archive.

## Requirements

### Requirement: Graph context excludes excluded-tier pages

The graph-context lane (`connect_memory(operation="graph-context")` and the
graph expansion feeding `find`) SHALL apply the `excluded` access tier at three
points: seed resolution, node materialization, and edge endpoints. An
`excluded`-tier page SHALL NOT be usable as a seed, SHALL NOT surface as a
neighbour node, and SHALL NOT appear as either endpoint of a returned edge. This
brings the graph lane to parity with `find` hit assembly, which already filters
excluded pages.

#### Scenario: Excluded page is never a seed

- **WHEN** graph-context is requested seeded on an excluded page (by path or via
  a query that would resolve to it)
- **THEN** the lane treats the seed as absent and returns no neighbourhood
  derived from it

#### Scenario: Excluded page is never a neighbour or edge endpoint

- **WHEN** an excluded page is a graph neighbour of a permitted seed
- **THEN** it is omitted from the returned nodes and no edge naming it is returned

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

### Requirement: Referent corroboration respects release decisions
The optional referent composition stage MAY reuse the typed sidecar for one-hop corroboration over the top ten released hits, SHALL ignore superseded hits inside that prefix, SHALL drop withheld entity and seed paths, and SHALL not modify graph-lane scoring or hit ordering.

#### Scenario: Withheld anchor
- **WHEN** graph evidence names an anchor withheld for the current audience
- **THEN** that evidence is removed before the referents block is emitted
