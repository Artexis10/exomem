## ADDED Requirements

### Requirement: Guidance teaches linking the identities a write names
Bootstrap guidance at the `balanced` and `maximal` levels, and the shipped skill scaffold,
SHALL instruct the agent to wikilink the identities a durable write names whether or not
a page exists yet, to resolve and create an Entity in the same turn when a note is about
an identity that has none, and to treat a returned `entity_candidate` as a prompt to
resolve before creating and to hydrate before duplicating. The guidance SHALL NOT make
recurrence a condition of creating an Entity, and SHALL leave the judgement that an
identity is stable and useful with the agent. Compact guidance SHALL stay under its byte
ceiling at every level.

#### Scenario: A hookless agent links without being asked
- **WHEN** an agent with only the bootstrap guidance and the tool schema writes a note
  that names an organisation with no page
- **THEN** the guidance tells it to wikilink that organisation, with no user instruction

#### Scenario: A note about a new identity creates it at once
- **WHEN** an agent writes a note that is about a piece of equipment with no Entity
- **THEN** the guidance directs it to resolve and then create the Entity in that turn,
  within its confirmation rules, without waiting for a second mention

#### Scenario: Guidance stays inside its budget
- **WHEN** compact guidance is rendered at any level and surface with the added line
- **THEN** it is under the compact byte ceiling
