## ADDED Requirements

### Requirement: Guidance teaches linking the identities a write names
Bootstrap guidance at the `balanced` and `maximal` levels SHALL instruct the agent to
wikilink the identities a durable write names whether or not a page exists yet, and
SHALL name a central entity beside a recurring one as worth capturing, so that
recurrence is never a condition of creating an Entity. The judgement that an identity is
stable and useful SHALL stay with the agent. The shipped skill scaffold SHALL carry the
full doctrine: link what a write names, resolve and create an Entity in the same turn
when a note is about an identity that has none, and treat a returned `entity_candidate`
as a prompt to resolve before creating and to hydrate before duplicating. Compact
guidance SHALL stay under its byte ceiling at every level and SHALL keep its warning
margin at the default level; the added words SHALL be paid for by tightening existing
sentences without changing what they instruct.

#### Scenario: A hookless agent links without being asked
- **WHEN** an agent with only the bootstrap guidance and the tool schema writes a note
  that names an organisation with no page
- **THEN** the guidance tells it to wikilink that organisation, with no user instruction

#### Scenario: A central identity needs no second mention
- **WHEN** balanced guidance describes what is worth capturing
- **THEN** it names a central entity as well as a recurring one, and nowhere requires an
  entity to recur before it may be created

#### Scenario: Guidance stays inside its budget
- **WHEN** compact guidance is rendered at any level and surface with the added words
- **THEN** it is under the compact byte ceiling, and at the default level it keeps the
  512-byte warning margin on every surface
