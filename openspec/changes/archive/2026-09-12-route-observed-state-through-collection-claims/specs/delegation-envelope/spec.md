## ADDED Requirements

### Requirement: Collection creation is restructure execution
Creating a Records or Planning collection SHALL be an action of the `restructure_execution` class: confirm-required at every prominence level, satisfiable by one inline confirmation of the named proposal in the conversation, never by a standing delegation. Appending to an existing collection SHALL remain `proactive_capture`. The `records_routing` advisory and the `collection_candidate` finding SHALL be advisory material under `structural_suggestions` and SHALL never create, append, move or delete anything. Bootstrap SHALL serve this classification with the envelope.

#### Scenario: A candidate never creates a collection on its own
- **WHEN** a strong `collection_candidate` is served at prominence `maximal`
- **THEN** no collection exists until the agent proposes it and the user confirms in the conversation

#### Scenario: The routing advisory never appends
- **WHEN** a `records_routing` advisory is returned on a committed write
- **THEN** no record has been appended by the runtime; the agent's subsequent append follows the served `proactive_capture` disposition

#### Scenario: Standing delegation for creation is refused by name
- **WHEN** a user asks that collections be created automatically from now on
- **THEN** the request is refused as a standing delegation above the v1 ceiling, and the one-action confirmation remains available
