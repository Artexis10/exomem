# relation-filtered-recall

## ADDED Requirements

### Requirement: Relation Participant Selection

`find` and `ask_memory` SHALL accept `relations` (a list of relation keys),
`relation_of` (an optional anchor page path or memory identifier), and
`relation_direction` (`outbound`, `inbound`, or `any`; default `any`). A KB page
qualifies when it participates in at least one typed edge whose canonical
`relation_type` equals a requested key or whose `parent_relation` equals it
(extension roll-up). Keys within `relations` combine as OR. With `relation_of`
alone (no `relations`), any typed edge qualifies. Direction SHALL be
anchor-relative when an anchor is present and candidate-relative otherwise, and
SHALL have no effect for symmetric core relations. The anchor page SHALL be
excluded from results. Block-level edge endpoints SHALL resolve to their owning
page; unresolved-placeholder endpoints SHALL NOT qualify a page.

#### Scenario: Recall pages contradicting an anchor

- **WHEN** `find` runs with `relations=["contradicts"]` and `relation_of` set to a page with two typed `contradicts` edges
- **THEN** exactly the counterpart pages of those edges qualify, the anchor is excluded, and other lanes' filters continue to apply

#### Scenario: Extension relations roll up to their core parent

- **WHEN** a vault defines a namespaced extension relation parented on `implements` and `find` runs with `relations=["implements"]`
- **THEN** pages connected by the extension relation qualify alongside pages connected by core `implements`

### Requirement: Relation Keys Resolve Through The Governed Registry

Requested relation keys SHALL be canonicalized through the relation registry:
canonical keys and aliases resolve; deprecated keys resolve, still match, and
produce a bounded advisory finding naming the replacement. A key that resolves
to nothing in the closed governed vocabulary SHALL raise a typed
`INVALID_RELATION_FILTER` error naming the offending key with a bounded list of
nearest canonical suggestions — it MUST NOT silently return an empty result.
Matching against `raw_relation` text of unregistered edges is out of scope and
MUST NOT occur.

#### Scenario: Typo is rejected with suggestions

- **WHEN** `find` runs with `relations=["implments"]`
- **THEN** the request fails with `INVALID_RELATION_FILTER` naming `implments` and suggesting `implements`, and no retrieval work is performed

#### Scenario: Deprecated key still matches with advice

- **WHEN** a registry marks a relation key deprecated with a replacement and a request uses it
- **THEN** matching edges qualify and the response carries an advisory finding naming the replacement

### Requirement: Relation Filters Never Return A False Empty

Relation-filtered recall SHALL return authoritative results only when the typed
sidecar snapshot is current for the active schema and registry identity; an
empty participant set from a current sidecar is authoritative. When the sidecar
is missing or stale, the request SHALL fail with the existing typed
`RETRIEVAL_INDEX_WARMING` outcome (status `warming`) carrying bounded retry
metadata, MUST NOT be cached, and SHALL schedule at most one concurrent
background rebuild. When the graph index is disabled, the request SHALL fail
with `RETRIEVAL_INDEX_WARMING` (status `temporarily_unavailable`, reason
`graph_index_disabled`) and SHALL NOT schedule a rebuild. No sidecar state may
produce a silent empty result or an unbounded foreground corpus or edge scan.

#### Scenario: Stale sidecar warms instead of lying

- **WHEN** the sidecar predates the current schema or registry identity and a relation-filtered request arrives
- **THEN** the request returns the warming outcome with retry metadata, is not cached, and a single background rebuild is scheduled

#### Scenario: Disabled graph is explicit

- **WHEN** `EXOMEM_DISABLE_GRAPH_INDEX` is set and a relation-filtered request arrives
- **THEN** the request returns the temporarily-unavailable outcome naming the disabled graph index and no rebuild is scheduled

#### Scenario: Authoritative empty

- **WHEN** the sidecar is current and no edge matches the requested relations
- **THEN** the request succeeds with an empty result set

### Requirement: Relation Filters Compose With Existing Recall

The relation participant set SHALL intersect the existing eligible-paths filter
seam so it composes AND with categories, kinds, page types, tags, and
structured filters, gates every ranking lane, and applies to empty-query
recall using the documented filtered-most-recent ordering. Unit-level results
SHALL qualify through their parent page in this change (per-unit edge anchoring
is deferred and documented). The filter is eligibility, not lane fusion: it
SHALL apply identically when `graph=false`. Relation-qualified hits SHALL carry
an additive `relation_match` annotation (relation type, direction, counterpart
path, matched-via) distinct from graph-lane provenance.

#### Scenario: Relation filter intersects a category filter

- **WHEN** `find` runs with `categories=["decision"]` and `relations=["supersedes"]`
- **THEN** only pages that both carry a decision-category unit and participate in a `supersedes` edge qualify

#### Scenario: Empty-query relation recall is bounded

- **WHEN** `ask_memory` runs with an empty query and `relations=["contradicts"]`
- **THEN** results are the participating pages in filtered-most-recent order with no unbounded corpus walk

#### Scenario: Graph lane off, filter still on

- **WHEN** `find` runs with `graph=false` and a relation filter on a current sidecar
- **THEN** the filter applies identically and results carry `relation_match` annotations

### Requirement: Relation Filter Freshness Invalidates Correctly

When a relation filter is active, the find freshness key SHALL incorporate the
graph sidecar cache token and the relation-registry identity in every retrieval
mode, including keyword and empty-query modes; request cache keys SHALL
incorporate the three relation parameters. A sidecar rebuild or registry change
SHALL therefore invalidate relation-filtered cached results.

#### Scenario: Rebuild invalidates a cached relation recall

- **WHEN** a relation-filtered result is cached and the sidecar subsequently republishes with a new generation
- **THEN** the next identical request recomputes rather than serving the stale cache entry
