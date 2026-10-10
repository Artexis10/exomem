## ADDED Requirements

### Requirement: A generic edge between entities offers fitting predicates

When a committed write authors the registry's generic relation between two entity pages, its `relation_advisory` SHALL carry `specific_options`.
The options SHALL be at most 4 active predicates whose declared endpoints contain both endpoint families, each with its display label, description and `add-relation` route.
A predicate without declared endpoints SHALL NOT be offered.
The generic edge SHALL stay committed, the offer SHALL refuse nothing, and the existing write-advisory fingerprints SHALL dismiss it.
The offer SHALL use the two endpoint pages' families and the in-memory registry only, with no corpus scan.

#### Scenario: A generic edge from a person to an animal is offered ownership

- **WHEN** an agent writes `relates_to` from a person entity to an animal entity and `owns` declares `subject: [person]` and `object: [any_entity]`
- **THEN** the response's `relation_advisory.specific_options` lists `owns` with its label and route
- **AND** the `relates_to` edge stays written

#### Scenario: A note's generic edge is not offered predicates

- **WHEN** an agent writes `relates_to` from a research note to an entity
- **THEN** the response carries no `specific_options`

#### Scenario: Honest generic stays honest

- **WHEN** no predicate's declared endpoints fit the two entity families
- **THEN** the response carries no `specific_options` and asks for no new relation
