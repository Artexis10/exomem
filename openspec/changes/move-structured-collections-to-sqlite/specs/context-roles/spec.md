## ADDED Requirements

### Requirement: Collection types surface through their kind's roles
The context compiler SHALL serve structured-collection items through one generic `collections` lane, driven by the collection type registry, with no per-type code. A role MAY name `collection_kinds` as a source beside its lane. The shipped registry SHALL map:
- `current_state` to `observed`;
- `active_plans` to `intended`;
- `methods` to `procedural`, beside its existing semantic-unit categories;
- `resources` to `reference`.

Activation SHALL derive anchors from the type registry: a `collection` anchor per collection, and an `item` anchor per item of a type whose surfacing rule declares `match_fields`, with intended items keeping the `plan` anchor kind.

A type's surfacing rule SHALL only scope selection within its kind's roles:
- bounded cues evaluated by the same deterministic matcher select those roles and make the type a candidate source;
- declared anchor kinds make it a candidate source when such an anchor resolves;
- a per-type item cap applies inside the role budget;
- only items in served states, at the version the kind serves, are emitted.

Selection SHALL make no model call; retrieval scorers MAY only rank. A type declaration SHALL NOT edit the role registry. The packet's `generation` block SHALL carry `collection_types_hash` beside `roles_hash`, so a changed surfacing rule is visible in every packet built from it.

#### Scenario: A how-to turn is served the current recipe revision
- **WHEN** a turn asks how to make a dish that resolves to the item anchor of a current recipe with three revisions
- **THEN** `methods` is selected, and the `collections` lane serves only the current revision of that recipe within the type's cap

#### Scenario: A retired or superseded item is not served
- **WHEN** the resolved recipe is in a state outside the type's served states, or a turn matches only a superseded revision's text
- **THEN** the lane serves no item for it and records `no_material`

#### Scenario: An observed collection serves the newest runs
- **WHEN** a turn asks how a dish turned out last time and resolves to a recipe
- **THEN** an observed-kind role is selected, and the lane serves the newest execution rows linked to that recipe, newest first

#### Scenario: Surfacing change is visible in the packet
- **WHEN** a type's surfacing cues change through `schema_memory`
- **THEN** the next packet's `generation.collection_types_hash` differs while `roles_hash` is unchanged

## MODIFIED Requirements

### Requirement: Versioned context-role registry
The product SHALL ship a versioned context-role registry (`context-roles.yaml`) in the
skill scaffold and the Claude Code plugin, loaded by the server, declaring for each
role an identifier, a description, the retrieval lane it maps to, the semantic-unit
category set it selects, the structured-collection kinds it may serve, and the anchor
kinds for which it is a default. The initial vocabulary SHALL be `identity,
preferences, constraints, resources, current_state, recent_change, active_plans,
methods, precedents, people, location, baseline, evidence, open_questions`. The lanes
SHALL be `units, collections, entity, graph, evidence`, with `records` and `planning`
accepted as aliases of `collections`. The anchor kinds SHALL be `entity, resource,
hub, collection, plan, item, project`. A vault override MAY add roles, categories or
cues and MAY narrow defaults, but SHALL NOT remove or rename a shipped role. A registry
that fails to load SHALL fall back to the shipped registry and report the failure in
the packet's `generation` block.

#### Scenario: Override adds a role without removing one
- **WHEN** a vault override declares a new role `logistics` and omits `people`
- **THEN** the effective registry contains both `logistics` and `people`

#### Scenario: Broken override falls back
- **WHEN** the vault override is not valid YAML
- **THEN** activation uses the shipped registry and the packet reports
  `generation.roles_source = "shipped"` with a load warning

#### Scenario: A legacy lane name in an override still works
- **WHEN** a vault override names `lane: records` for a role
- **THEN** the role runs through the `collections` lane with `collection_kinds: [observed]`
