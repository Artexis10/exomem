# graph-find-ranking

## ADDED Requirements

### Requirement: Absent Relation Filter Is Byte-Identical

When `relations`, `relation_of`, and `relation_direction` are absent, `find`
responses SHALL be byte-identical to pre-change behavior for the same inputs in
every sidecar state (available, missing, stale, disabled), including the
mandated wikilink fallback ordering.

#### Scenario: No filter, no change

- **WHEN** a query runs without relation parameters against a fixture vault in each sidecar state
- **THEN** the full response equals the pre-change response for that state

### Requirement: Sidecar Schema Bump Preserves Fallback Parity And Ceilings

The relation-index schema bump SHALL be additive: a pre-bump sidecar fails the
identity snapshot and serves the byte-identical wikilink fallback (no lane
failure, no typed annotations) until a full rebuild converges it, and the
existing latency-gate ceilings (`CEIL_GRAPH_MS`, `CEIL_TOTAL_MS`) SHALL pass
without threshold edits. A new dedicated ceiling SHALL bound the relation-filter
path instead of raising existing ceilings.

#### Scenario: Old sidecar falls back then heals

- **WHEN** a pre-bump sidecar is present and a query runs without relation filters
- **THEN** the wikilink fallback ordering is served, and after a rebuild the typed lane resumes with the new schema

### Requirement: Relation Match Annotation Is Distinct From Graph Provenance

The `relation_match` annotation SHALL be additive and separate from the
graph-provenance annotation: graph-provenance continues to mean "entered the
candidate set via typed graph-lane expansion", while `relation_match` means
"qualified through the relation filter". A hit may carry both, either, or
neither; no existing envelope field changes shape.

#### Scenario: Filtered hit ranked by vector lane

- **WHEN** a relation-filtered query ranks a qualifying page purely via the vector lane
- **THEN** the hit carries `relation_match` and no graph-provenance annotation
