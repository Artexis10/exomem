## ADDED Requirements

### Requirement: Relations carry a display label rendered by one formatter

A relation entry SHALL carry an optional `label` as registry data.
When the label is absent, a core relation's label SHALL be its key, and an extension's label SHALL be the segment after its namespace.
One registry formatter SHALL render a core relation as its label and an extension as `<label> (a kind of <parent label>)`.
It SHALL add the namespace only when two active entries share a label, and SHALL mark a deprecated entry.
Every response that emits a relation key SHALL also emit `relation_label` from that formatter, and prose renderings SHALL use the label.
Every existing key field SHALL keep the namespaced key as the machine identity, and a label SHALL resolve as an alias in calls.

#### Scenario: An extension reads as a kind of its parent

- **WHEN** graph context returns an edge of the extension `vault.has_pet` with parent `owns`
- **THEN** the edge carries `relation_type: vault.has_pet` and `relation_label: has_pet (a kind of owns)`

#### Scenario: A shared label is qualified

- **WHEN** two active extensions in different namespaces share the label `applies`
- **THEN** each rendering of either adds its namespace, and a core relation's rendering is unchanged

### Requirement: Endpoint families and the generic marker are registry data

A relation entry MAY declare `endpoints` with `subject` and `object` lists.
Each list SHALL use the typed grammar `entity:<type>`, `entity:*`, or `role:<note role>`.
Entity types SHALL come from the entity-type registry, and roles SHALL come from the note-type schema.
An extension SHALL inherit its parent's endpoints, and a narrower declaration SHALL stay inside them.
The core pack SHALL mark exactly one active relation `generic: true`, and extensions SHALL NOT inherit the marker.
Code SHALL read genericity and endpoints from these attributes and SHALL NOT compare relation keys for them.
An endpoint mismatch SHALL remain a non-blocking finding.
The generic-relation barrier SHALL use fitting endpoint declarations as specified by the relation-vocabulary requirement.

#### Scenario: An extension narrows its parent's endpoints

- **WHEN** an extension under `owns` narrows `entity:*` to `entity:animal` on its object side
- **THEN** the save validates and the extension's endpoints are its own

#### Scenario: A wider declaration is reported

- **WHEN** an extension declares an object family outside its parent's declared object families
- **THEN** validation reports a stable finding and the save is refused

#### Scenario: A mismatched edge is kept

- **WHEN** an agent authors an `owns` edge whose subject family is outside the declared subject families
- **THEN** the edge is written and its receipt carries `endpoint_mismatch`

### Requirement: New authored-edge cohorts report their provenance and coverage

The census SHALL preserve its historical graph cohort and report a separate new authored-edge cohort split by endpoint class.
The cohort SHALL use server UTC time and exact governed write additions from the existing atomic operation log, including rotation archives.
Spelling-only edits, inherited predecessor edges, replay and graph rebuilds SHALL NOT create additions.
Page creation dates and graph rebuild timestamps SHALL NOT substitute for edge authoring time.

Every metric SHALL state its numerator, denominator, window, source and coverage.
Metrics SHALL cover generic share, unreasoned generic edges, exact-normalized duplicate reasons, referent outcomes and placement by actual basis.
A general mutation rationale SHALL NOT count as an edge-review reason.
Missing history, unsupported writers or missing instrumentation SHALL remain partial or unavailable, never counted zero.
Referent outcomes SHALL require correlated observations; a cleared review item SHALL NOT count as acted.

The default review target SHALL be fifteen percent generic among new authored edges that touch an entity over thirty UTC days.
The vault MAY configure the target and window; crossing the target SHALL emit only an audit or upkeep signal.
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
