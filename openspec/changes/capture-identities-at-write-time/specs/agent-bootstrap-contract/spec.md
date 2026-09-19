## ADDED Requirements

### Requirement: Guidance carries the identity-declaration doctrine
Bootstrap guidance at the `balanced` and `maximal` levels, and the shipped skill scaffold,
SHALL instruct the agent to declare in `mentions` the identities a durable write names, to
mark one `central` when the note is about it, to resolve before creating when a write
returns `entity_candidate`, and to hydrate an existing Entity before creating a second
one. The guidance SHALL NOT require an identity to be recurring before it may be
declared. Compact guidance SHALL stay under its byte ceiling at every level.

#### Scenario: A hookless agent declares without being asked
- **WHEN** an agent with only the bootstrap guidance and the tool schema writes a note
  that names an organisation
- **THEN** the guidance and the `mentions` argument description together tell it to
  declare that organisation, with no user instruction

#### Scenario: Guidance stays inside its budget
- **WHEN** compact guidance is rendered at any level and surface with the doctrine line
- **THEN** it is under the compact byte ceiling
