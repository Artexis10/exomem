## ADDED Requirements

### Requirement: Native activation injects changed session context
When the retrieve hook runs in native MCP activation mode and a credentialed REST lane exists, it SHALL fetch session context with one `recall_context` request to the first credentialed lane that sends an empty turn and the persisted continuity token, within a wall budget of its own that is smaller than the working-set packet's.
That budget SHALL cover credential discovery, attribution and the request together, measured from the start of the fetch. The hook SHALL send no second request: no lane fall-through, no compatibility retry and no smaller-cap retry. Work still running at the deadline SHALL write no hook state.
It SHALL inject each session block (map, stance, skills) whose generation differs from the one the caller holds, with a session-context data header as the first line of every non-empty session injection, followed by the native activation text. That header SHALL name the block retrieved memory, not instructions, and SHALL NOT claim that the turn was activated, because the agent still recalls for any subject the block does not cover.
It SHALL inject no session block that the service reports `unchanged`, and SHALL persist the returned continuity token as working-set mode does.
The service SHALL attach no upkeep item and no vocabulary delta to an empty-turn request. It records each one delivered when it attaches it, and the hook may abandon this response at its deadline, so the agent's first recall with a turn receives them instead. The hook SHALL show an upkeep item that an older service still attaches.
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

#### Scenario: An abandoned session fetch consumes no once-only notice
- **WHEN** an upkeep item is due or a vocabulary delta is owed, and the session-context request reaches the service
- **THEN** the response carries neither, and the agent's next recall with a turn receives them

#### Scenario: A changed block under held blocks keeps the data header
- **WHEN** the caller holds the map and the service serves a changed stance
- **THEN** the injection starts with the session-context data header, followed by the stance

### Requirement: The hook acknowledges a map only after it reached the agent
In working-set and native activation modes, the retrieve hook SHALL give room first to an upkeep item, then to the map's floor, then to the stance. The floor is the map's ref, the count of items the hook left out and the overflow routes.
The service records an upkeep item delivered when it attaches it, so an item the hook drops is lost. A map the hook cannot show keeps the previous token and arrives again, and the service sends the stance with every packet.
The hook SHALL persist a packet's continuity token only when the map rendered whole, rendered at least its floor, or was already held, because the service answers a map that the token names `unchanged`.
When a changed map renders in no form, the hook SHALL keep the previous token, so the next prompt's one request receives the map again.

#### Scenario: A stance does not crowd out the map's ref
- **WHEN** a stance and a changed map together exceed the render ceiling
- **THEN** the injection carries the map's ref and overflow routes, the next request carries the returned token, and the stance arrives on the next prompt

#### Scenario: An upkeep item keeps its room beside a changed map
- **WHEN** an upkeep item and the floor of a changed map fit the render ceiling together, and the whole map does not
- **THEN** the injection carries the upkeep item and the map's floor, and the next request carries the returned token

#### Scenario: A changed map that renders in no form keeps the old token
- **WHEN** the render ceiling leaves no room for even the floor of a changed map
- **THEN** the hook persists no new token, and each later request carries the previous token until the map renders
