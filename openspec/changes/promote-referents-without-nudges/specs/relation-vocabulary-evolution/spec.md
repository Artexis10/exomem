## ADDED Requirements

### Requirement: A generic edge between entities offers fitting predicates

When a committed write authors the registry's generic relation between two entity pages, its `relation_advisory` SHALL carry `specific_options`.
The options SHALL be at most 4 active predicates whose declared endpoints contain both endpoint families, each with its display label and description.
Each option's route SHALL replace the generic bullet in place with one hash-guarded edit, so that accepting an option leaves one edge, not two.
A predicate without declared endpoints SHALL NOT be offered.
The generic edge SHALL stay committed, the offer SHALL refuse nothing, and the existing write-advisory fingerprints SHALL dismiss it.
The offer SHALL use the two endpoint pages' families and the in-memory registry only, with no corpus scan.

#### Scenario: A generic edge from a person to a vessel is offered ownership

- **WHEN** an agent writes `relates_to` from a person entity to a vessel entity and `owns` declares `subject: [person]` and `object: [any_entity]`
- **THEN** the response's `relation_advisory.specific_options` lists `owns` with its label and route
- **AND** the `relates_to` edge stays written

#### Scenario: Accepting an option replaces the generic edge

- **WHEN** the agent runs the `owns` option's route
- **THEN** the page holds one `owns` bullet to that entity and no `relates_to` bullet to it

#### Scenario: A note's generic edge is not offered predicates

- **WHEN** an agent writes `relates_to` from a research note to an entity
- **THEN** the response carries no `specific_options`

#### Scenario: Honest generic stays honest

- **WHEN** no predicate's declared endpoints fit the two entity families
- **THEN** the response carries no `specific_options` and asks for no new relation
