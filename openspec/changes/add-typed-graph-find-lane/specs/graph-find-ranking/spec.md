# graph-find-ranking

## ADDED Requirements

### Requirement: Typed-graph candidate expansion in the find graph lane
When the typed epistemic-graph sidecar is available, the `find` graph fusion
lane SHALL expand query-relevant seeds through typed graph edges read from the
sidecar (`Knowledge Base/.graph.sqlite`) instead of per-page outbound wikilink
resolution. Expansion SHALL be bidirectional (outbound and inbound edges),
SHALL read edges for all seeds in batched SQL (no per-seed page parsing),
SHALL order expansion targets by relation family precedence derived from the
governed relation registry (epistemic and provenance families before plain
`links_to`), and SHALL skip unresolved-placeholder targets. The lane SHALL
keep its existing `LANE_ORDER` slot, seed selection rules, and
`graph_seed_cap`; no new `RankingConfig` fields are introduced.

#### Scenario: Typed neighbour surfaces for a conceptual query
- **WHEN** a query's top vector/BM25 seeds include a note with an authored
  typed relation (e.g. `evidenced_by [[Target]]`) whose target does not match
  the query lexically or semantically
- **THEN** the target appears in the graph lane ranking and can enter the
  fused results, ordered ahead of targets connected only by plain `links_to`

#### Scenario: Inbound edges count
- **WHEN** a seed page is the destination of a typed edge from another page
- **THEN** the edge's source page is eligible for graph-lane expansion exactly
  as an outbound target is

#### Scenario: Unresolved placeholder excluded
- **WHEN** a seed has a typed relation to a target that does not resolve to an
  existing page (placeholder node)
- **THEN** the placeholder contributes no graph-lane candidate

### Requirement: Byte-identical wikilink fallback
When the typed sidecar is unavailable — disabled via
`EXOMEM_DISABLE_GRAPH_INDEX`, not yet built, or invalidated by
schema/registry/extension drift — the graph lane SHALL fall back to the
pre-existing outbound-wikilink expansion and the resulting `find` ordering
SHALL be byte-identical to the pre-change behavior for the same inputs.

#### Scenario: Sidecar disabled reproduces legacy ordering
- **WHEN** `EXOMEM_DISABLE_GRAPH_INDEX` is set and a query runs against a
  fixture vault
- **THEN** the full fused ordering equals the ordering produced by the
  pre-change wikilink lane for the same vault and query

#### Scenario: Drift-invalidated sidecar falls back without error
- **WHEN** the sidecar exists but its registry hash no longer matches the
  active relation registry
- **THEN** the lane serves the wikilink fallback, records no lane failure, and
  the response carries no graph-provenance annotations

### Requirement: Graph-provenance annotation on expanded hits
A hit that entered the candidate set via typed graph-lane expansion SHALL
carry a graph-provenance annotation in the `find` result envelope naming the
relation type, edge direction, and the seed page that led to it. The
annotation SHALL be additive: hits not produced by graph expansion are
unchanged, and no existing envelope field changes shape. Fallback
(wikilink-expanded) hits SHALL NOT carry typed annotations.

#### Scenario: Annotated typed hit
- **WHEN** a hit entered results through a typed edge `supports` from seed
  page S
- **THEN** the hit's envelope includes a graph-provenance annotation with
  relation type `supports`, its direction, and seed S

#### Scenario: Non-graph hits unchanged
- **WHEN** a hit ranked purely via vector/BM25/keyword lanes
- **THEN** its envelope contains no graph-provenance annotation and is
  byte-identical to the pre-change shape

### Requirement: Latency ceilings hold with the typed lane
Typed-graph expansion SHALL hold the existing retrieval latency gate
unchanged: the graph lane stage within `CEIL_GRAPH_MS` and end-to-end `find`
within `CEIL_TOTAL_MS` on the gate's synthetic vault, with gate thresholds
not raised by this change.

#### Scenario: Latency gate passes unmodified
- **WHEN** `tests/test_latency_gate.py` runs on the 2000-note synthetic vault
  with the typed lane active
- **THEN** all existing ceilings pass without threshold edits

### Requirement: Golden coverage for typed expansion and fallback
The golden retrieval suite SHALL include a typed-relation fixture tier that
locks the typed lane's ordering wins, while the pre-existing golden queries
SHALL continue to pass unchanged in fallback mode.

#### Scenario: Typed golden tier
- **WHEN** the golden suite runs against fixtures containing authored typed
  relations and a built sidecar
- **THEN** queries whose correct answers are typed-graph neighbours rank them
  within the golden expectations

#### Scenario: Legacy goldens guard fallback
- **WHEN** the golden suite runs with the sidecar unavailable
- **THEN** the pre-change golden expectations pass without modification
