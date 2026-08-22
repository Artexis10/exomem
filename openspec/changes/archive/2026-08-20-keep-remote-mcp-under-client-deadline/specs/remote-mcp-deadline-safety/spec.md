## ADDED Requirements

### Requirement: MCP tool calls use a dedicated execution deadline
The HA edge SHALL use a separately configurable MCP tool-call timeout whose default accommodates the supported Exomem execution envelope without changing the short connectivity timeout.

#### Scenario: Slow successful tool call
- **WHEN** the active Exomem origin completes a `tools/call` request after the short connectivity timeout but before the MCP tool-call timeout
- **THEN** the edge returns that successful response to the client
- **AND** the request is not forwarded to another replica

### Requirement: Active-writer tool calls are never replayed cross-replica
The HA edge SHALL route an MCP `tools/call` request only to the coordinator's current lease holder while that holder is active.

#### Scenario: Active origin times out ambiguously
- **WHEN** a tool call to the active writer exceeds the MCP tool-call timeout
- **THEN** the edge returns an error without replaying the request to the passive origin

#### Scenario: Active origin returns an application error
- **WHEN** the active writer returns any completed HTTP response for a tool call
- **THEN** the edge returns that response without invoking the passive origin

### Requirement: No-holder takeover chooses one healthy origin
When the writer lease has no active holder, the HA edge SHALL select one healthy replica before forwarding a tool call and SHALL forward that call exactly once.

#### Scenario: Desktop is offline after lease expiry
- **WHEN** no holder exists, the desktop health probe fails, and the laptop probe succeeds
- **THEN** the edge sends the tool call only to the laptop
- **AND** the laptop can acquire the writer lease for a mutation

#### Scenario: Neither replica is healthy
- **WHEN** no holder exists and both origin probes fail
- **THEN** the edge returns 503 without forwarding the tool call

### Requirement: Safe transport traffic retains fast fallback
The HA edge SHALL retain short-timeout active/passive fallback for MCP traffic that is not a `tools/call` request and for OAuth/discovery traffic.

#### Scenario: Initialization origin unavailable
- **WHEN** the preferred origin is unavailable during MCP initialization or tool listing
- **THEN** the edge attempts the passive origin using the short connectivity timeout

### Requirement: HA deployment requires replica transport parity
The deployment contract SHALL require every admitted replica to run a restart-safe stateless Exomem release before the stable connector route is considered healthy.

#### Scenario: Passive replica is out of date
- **WHEN** the repo release and installed service package differ on a replica
- **THEN** deployment verification reports the replica as incomplete
- **AND** operators are instructed to upgrade and restart it before HA testing
