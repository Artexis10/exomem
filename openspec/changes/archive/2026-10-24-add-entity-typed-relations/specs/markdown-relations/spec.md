## ADDED Requirements

### Requirement: Entity creation can author typed relations
Entity creation SHALL accept optional typed relations as `{kind, target}` pairs and SHALL write each pair as one canonical `- kind [[target]]` bullet under the entity's `## Relations` section. Bare `connections` SHALL keep their `relates_to` rendering and their existing input forms. Typed targets SHALL resolve through the same writer resolver and visibility rules as `connections`. Identical pairs SHALL collapse, and different kinds for one target SHALL remain distinct edges. A pair that is malformed, or that would not parse back as exactly one canonical relation bullet with the same kind, SHALL refuse the creation before any write. Registration and scope of the kind SHALL be decided by the existing creation preflight and its relation findings, with no stricter label policy. Typed relations supplied to any other connection operation SHALL be refused rather than ignored.

#### Scenario: Two predicates for one target survive creation
- **WHEN** an entity is created with typed relations `supports` and `part_of` to the same target, a repeated `supports` pair, and that target in `connections`
- **THEN** the page lists `supports`, `part_of` and `relates_to` bullets to that target once each
- **AND** the graph indexes each bullet as its own typed edge

#### Scenario: A value that would add a line is refused
- **WHEN** a typed relation's kind contains a line break or a second wikilink
- **THEN** the creation is refused with an invalid-relation error naming the pair's position
- **AND** no entity page is written

#### Scenario: An unregistered kind keeps the existing finding
- **WHEN** a typed relation names a kind that the selected registry does not define
- **THEN** the entity is created with that bullet
- **AND** the write reports the creation preflight's unregistered-relation finding

#### Scenario: Bare connections stay generic
- **WHEN** an entity is created with `connections` only
- **THEN** each connection is written as a `relates_to` bullet exactly as before
