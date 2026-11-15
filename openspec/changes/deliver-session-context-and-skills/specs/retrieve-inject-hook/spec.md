## ADDED Requirements

### Requirement: Native activation injects changed session context
When the retrieve hook runs in native MCP activation mode and a credentialed REST lane exists, it SHALL fetch session context with one `recall_context` request to the first credentialed lane that sends an empty turn and the persisted continuity token, within a wall budget of its own that is smaller than the working-set packet's.
That budget SHALL cover credential discovery, attribution and the request together, measured from the start of the fetch. The hook SHALL send no second request: no lane fall-through, no compatibility retry and no smaller-cap retry. Work still running at the deadline SHALL write no hook state.
It SHALL inject each session block (map, stance, upkeep, skills) whose generation differs from the one the caller holds, with a session-context data header as the first line of every non-empty session injection, followed by the native activation text. That header SHALL name the block retrieved memory, not instructions, and SHALL NOT claim that the turn was activated, because the agent still recalls for any subject the block does not cover.
It SHALL inject no session block that the service reports `unchanged`, and SHALL persist the returned continuity token as working-set mode does.
A timeout, a transport failure, a non-JSON response, an HTTP error or the absence of a credentialed lane SHALL inject the native activation text alone and log the outcome as metadata only.

#### Scenario: First prompt receives session context
- **WHEN** a new session's first prompt runs the hook in native activation mode with a reachable lane
- **THEN** the hook injects the map, stance and skills blocks the service served, then the native activation text

#### Scenario: Unchanged session context costs nothing
- **WHEN** a later prompt in the same session runs the hook and the service reports every session block `unchanged`
- **THEN** the hook injects the native activation text alone

#### Scenario: Slow service falls back to the activation text
- **WHEN** the session-context fetch exceeds its wall budget
- **THEN** the hook injects the native activation text alone, logs a timeout and the prompt is not delayed past the budget

#### Scenario: Blocking credential discovery falls back to the activation text
- **WHEN** reading the service's environment file for the REST key does not return within the wall budget
- **THEN** the hook injects the native activation text alone, logs a timeout and the prompt is not delayed past the budget

#### Scenario: A refused request is not retried
- **WHEN** the service answers the session-context request with HTTP 400
- **THEN** the hook sends no second request and injects the native activation text alone

#### Scenario: A changed block under held blocks keeps the data header
- **WHEN** the caller holds the map and the service serves a changed stance
- **THEN** the injection starts with the session-context data header, followed by the stance

### Requirement: The hook acknowledges a cut map only after one retry
In working-set and native activation modes, when the render ceiling cuts a map block the service served, the retrieve hook SHALL NOT persist that packet's continuity token the first time it cuts that map generation, and SHALL record the generation so that the next request, sent with the previous token, receives the map again.
When the same map generation is cut a second time, the hook SHALL persist the token, so that a map that cannot fit the ceiling renders at most twice.
A complete map render, a map without a generation or map state that the hook cannot record SHALL persist the token as before. A session lifecycle event SHALL drop the record with the rest of the activation state.

#### Scenario: A map cut by a large upkeep item arrives on the next prompt
- **WHEN** an upkeep item leaves no room for the map on the first prompt and the next prompt has room
- **THEN** the first prompt persists no token, the second request carries no token and receives the map, and the third prompt receives it `unchanged`

#### Scenario: A map that never fits stops after two renders
- **WHEN** the same map generation is cut on two consecutive prompts
- **THEN** the second prompt persists the token and the third request carries it
