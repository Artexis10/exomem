## ADDED Requirements

### Requirement: Compiled writes surface unregistered relation vocabulary

When a compiled write contains an explicit relation row whose label resolves with registry status `unregistered`, the write result SHALL include a non-blocking signal inside the existing `write_feedback.relations` structure. The signal SHALL name every distinct unregistered label and SHALL identify `schema_memory(operation="infer", subject="relations")` as the governed promotion route. The existing `write_feedback.next_actions` list SHALL include that route.

The system SHALL NOT add a new top-level response key, and a registered relation row SHALL NOT produce the unregistered-vocabulary signal.

#### Scenario: A separately connected write returns unregistered vocabulary feedback

- **WHEN** a compiled write contains an unregistered relation row and a separate governed connection satisfies relation disposition
- **THEN** the public write succeeds
- **THEN** `write_feedback.relations` names the label and promotion route
- **AND** `write_feedback.next_actions` names the same promotion route
- **AND** the unregistered observation remains non-blocking and non-qualifying

#### Scenario: Registered relation produces no promotion signal

- **WHEN** every explicit relation row resolves through the active registry
- **THEN** write feedback contains no unregistered-vocabulary signal
