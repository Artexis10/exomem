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
Each list SHALL hold entity families from the entity-type registry or the closed token `any_entity`.
An extension SHALL inherit its parent's endpoints, and a narrower declaration SHALL stay inside them.
The core pack SHALL mark exactly one active relation `generic: true`, and extensions SHALL NOT inherit the marker.
Code SHALL read genericity and endpoints from these attributes and SHALL NOT compare relation keys for them.
Endpoints SHALL never refuse a write; an edge outside them SHALL carry a non-blocking `endpoint_mismatch` finding.

#### Scenario: An extension narrows its parent's endpoints

- **WHEN** an extension under `owns` declares `subject: [person]` and `object: [animal]`, and `owns` declares `object: [any_entity]`
- **THEN** the save validates and the extension's endpoints are its own

#### Scenario: A wider declaration is reported

- **WHEN** an extension declares an object family outside its parent's declared object families
- **THEN** validation reports a stable finding and the save is refused

#### Scenario: A mismatched edge is kept

- **WHEN** an agent authors an `owns` edge whose subject family is outside the declared subject families
- **THEN** the edge is written and its receipt carries `endpoint_mismatch`
