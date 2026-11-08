## ADDED Requirements

### Requirement: New authored-edge cohorts report their provenance and coverage

The census SHALL preserve its historical graph cohort and report a separate new authored-edge cohort split by endpoint class.
The cohort SHALL use server UTC time and exact governed write additions from the existing atomic operation log, including rotation archives.
Spelling-only edits, inherited predecessor edges, replay and graph rebuilds SHALL NOT create additions.
Page creation dates and graph rebuild timestamps SHALL NOT substitute for edge authoring time.

Every metric SHALL state its numerator, denominator, window, source and coverage.
Metrics SHALL cover generic share, referent outcomes and placement by actual basis.
Among the new edges that writers authored as a relation, meaning a `## Relations` bullet or a semantic relation and never a body wikilink, metrics SHALL also cover `specific_share`, the share that names a specific relation, and `plain_link_share`, the share that is a plain link.
Events logged before the generic-relation refusal keep their recorded edge reasons in the existing reason metrics, and no later event records a reason.
A general mutation rationale SHALL NOT count as an edge-review reason.
Missing history, unsupported writers, an event that names no relation, or missing instrumentation SHALL remain partial or unavailable, never counted zero.
Referent outcomes SHALL require correlated observations; a cleared review item SHALL NOT count as acted.

The default review target SHALL be fifteen percent generic among new authored edges that touch an entity over thirty UTC days.
The vault MAY configure the target and window; crossing the target SHALL emit only an audit or upkeep signal.
A write cannot add a generic relation, so the generic share covers cohorts that the refusal does not cover, such as bulk imports, and events logged before it.
Note-to-note edges SHALL have no target, and the census SHALL NOT refuse a write.

#### Scenario: An edit and graph rebuild preserve event identity

- **WHEN** a governed edit adds an edge to an old page, a replay repeats that edit, and the graph rebuilds
- **THEN** the new cohort counts one authored addition and the historical graph cohort keeps its existing meaning

#### Scenario: Rotation retains authored history

- **WHEN** a recorded edge addition moves into a rotation archive within the selected window
- **THEN** the census still includes it once with its recorded time and endpoint class

#### Scenario: Uninstrumented history is unknown

- **WHEN** retained history lacks the structured data required by a metric
- **THEN** that metric reports partial or unavailable coverage with its source instead of a measured zero

#### Scenario: The share of specific and plain-link edges is reported

- **WHEN** the window holds new authored relation edges, some specific and some plain links, and a body wikilink
- **THEN** `specific_share` and `plain_link_share` count only the authored relation edges and report their numerator, denominator and coverage

#### Scenario: A log that cannot classify an edge reports unavailable

- **WHEN** a logged edge names no relation
- **THEN** the specific and plain-link shares exclude it from their denominator and report their coverage, and report unavailable when no edge can be classified

#### Scenario: The configured target creates an audit review item

- **WHEN** the recorded generic share for edges that touch an entity exceeds the configured target
- **THEN** the `relation_generic_share` audit check returns an informational review item with the measurement, window, source and coverage
- **AND** raising the target above that share removes the item without changing any write permission
