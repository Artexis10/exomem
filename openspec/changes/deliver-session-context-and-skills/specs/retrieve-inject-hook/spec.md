## ADDED Requirements

### Requirement: Native activation injects changed session context
When the retrieve hook runs in native MCP activation mode and a REST or CLI lane is reachable, it SHALL fetch session context with one `recall_context` call that sends an empty turn and the persisted continuity token, within a wall budget of its own that is smaller than the working-set packet's, without retry.
It SHALL inject, under the existing retrieved-memory data header, each session block (map, stance, skills) whose generation differs from the one the caller holds, followed by the native activation text.
It SHALL inject no session block that the service reports `unchanged`, and SHALL persist the returned continuity token as working-set mode does.
A timeout, a transport failure, a non-JSON response or the absence of a reachable lane SHALL inject the native activation text alone and log the outcome as metadata only.

#### Scenario: First prompt receives session context
- **WHEN** a new session's first prompt runs the hook in native activation mode with a reachable lane
- **THEN** the hook injects the map, stance and skills blocks the service served, then the native activation text

#### Scenario: Unchanged session context costs nothing
- **WHEN** a later prompt in the same session runs the hook and the service reports every session block `unchanged`
- **THEN** the hook injects the native activation text alone

#### Scenario: Slow service falls back to the activation text
- **WHEN** the session-context fetch exceeds its wall budget
- **THEN** the hook injects the native activation text alone, logs a timeout and the prompt is not delayed past the budget
