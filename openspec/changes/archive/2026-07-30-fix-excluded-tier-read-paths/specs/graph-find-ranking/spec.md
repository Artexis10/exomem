# graph-find-ranking

## ADDED Requirements

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
