## MODIFIED Requirements

### Requirement: Simple Product Actions

The system SHALL define a small set of simple product actions that route common knowledge-base intents to canonical Exomem operations without duplicating command logic. The action catalog SHALL be capability-complete over the product command registry, and SHALL be available to agent surfaces as well as CLI-family surfaces.

#### Scenario: Action catalog names the simple actions

- **WHEN** the action catalog is requested by bootstrap, docs generation, or CLI help
- **THEN** it lists `ask`, `remember`, `capture`, `review`, `connect`, `adopt`, `maintain`, `record`, and `plan`
- **AND** each action identifies its canonical operation route and default safety posture
- **AND** the catalog is generated from the action registry, so an action added to the registry appears without editing a second list

#### Scenario: Canonical operations remain the source of truth

- **WHEN** a simple action invokes an Exomem operation
- **THEN** it uses the existing registry leaf and validation path
- **AND** it does not bypass guarded fields, destructive-operation metadata, schema validation, or vault path checks

#### Scenario: Every product command is reachable from an action

- **WHEN** the action catalog is resolved with no surface restriction
- **THEN** every command in the product command registry is named by at least one action's primary route, alternate route, or `advanced` list, except commands that are themselves the catalog entry point
- **AND** a product command that no action reaches fails the surface-contract check rather than being silently unreachable

#### Scenario: Action catalog is delivered to an agent surface

- **WHEN** an agent surface requests the action catalog through its bootstrap contract
- **THEN** the catalog resolves against exactly the commands that surface exports
- **AND** an action with no surviving route is reported unavailable with its reason rather than omitted
- **AND** the action names are not themselves callable on that surface; making them callable tools is a separate change, because it alters the surface's callable command set and therefore its published identity

#### Scenario: Withheld command degrades its action

- **WHEN** the catalog is resolved against a surface that does not export a command named in an action's `advanced` list
- **THEN** that entry is filtered from the action
- **AND** the action stays available when any route survives, and is marked unavailable when none does
