## ADDED Requirements

### Requirement: Liveness and readiness are separate contracts
Exomem SHALL preserve the unauthenticated `/health` liveness route and SHALL expose an unauthenticated `GET /health/ready` route for runtime admission. Readiness MUST NOT expose vault paths, vault identifiers, credentials, tokens, or vault content.

#### Scenario: Coordinator outage does not change liveness
- **WHEN** a coordinated replica cannot reach its writer coordinator
- **THEN** `/health` still returns its normal successful liveness response
- **AND** `/health/ready` returns 503 with content-free reason codes

### Requirement: Readiness reports behavioral compatibility
The readiness payload SHALL report service identity, release identity, an integer runtime compatibility contract, HTTP transport identity, replica identity, coordination state, takeover eligibility, and stable reason codes. Release identity SHALL be diagnostic and SHALL NOT itself determine compatibility.

#### Scenario: Compatible releases differ
- **WHEN** two replicas run different Exomem releases that advertise the same supported runtime contract and transport
- **THEN** both can be admitted for a rolling deployment
- **AND** the release mismatch remains visible for operator diagnosis

#### Scenario: Runtime contract is unsupported
- **WHEN** a replica advertises a runtime contract outside the deployment's supported set
- **THEN** the replica is not eligible to receive an HA tool call

### Requirement: Readiness reflects takeover safety
Standalone Exomem SHALL remain ready without multi-host coordination. A coordinated replica SHALL report takeover eligibility only when its coordinator is healthy, its replica identity is configured, and its role is authoritatively known.

#### Scenario: Healthy coordinated follower
- **WHEN** a follower can confirm the current lease state
- **THEN** readiness returns 200 and reports takeover eligibility true

#### Scenario: Coordinator is unavailable
- **WHEN** a coordinated replica cannot confirm lease state
- **THEN** readiness returns 503 and reports takeover eligibility false

### Requirement: HA edge admits a runtime before tool-call routing
The HA edge SHALL validate readiness status, runtime contract, stateless HTTP transport, expected replica identity, takeover eligibility, and configured coordination requirements before admitting a replica for MCP tool calls.

#### Scenario: Stale service lacks the readiness contract
- **WHEN** a candidate origin does not serve a valid readiness payload
- **THEN** the edge does not forward the tool call to that origin

#### Scenario: Origin mapping is swapped
- **WHEN** an origin's readiness replica identity does not match the configured replica identity for that origin
- **THEN** the edge rejects that origin for tool calls

### Requirement: Runtime admission is bound to the writer generation
The HA edge SHALL bind a successful runtime admission to the active lease holder and fencing token. It SHALL re-evaluate stored admission against current policy and SHALL require new admission after lease expiry, release, or ownership change.

#### Scenario: Steady-state writer remains admitted
- **WHEN** successive tool calls observe the same active holder and fencing token
- **THEN** the edge reuses the stored compatible admission without another origin readiness probe

#### Scenario: Ownership changes
- **WHEN** a different replica acquires a newer fencing token
- **THEN** the previous admission is discarded
- **AND** the new holder must pass runtime admission

### Requirement: Ineligible active writers fail closed
The HA edge SHALL return 503 when an active holder cannot be admitted and SHALL NOT replay the tool call to another replica.

#### Scenario: Active holder becomes unverifiable
- **WHEN** the active holder has no valid stored admission and its readiness probe fails
- **THEN** the edge returns 503 without invoking either replica's MCP tool endpoint

### Requirement: No-holder selection chooses one eligible runtime
When no writer lease is active, the HA edge SHALL probe configured replicas concurrently, select one eligible runtime in deterministic preference order, and forward the tool call exactly once.

#### Scenario: Preferred replica is incompatible
- **WHEN** no holder exists, the preferred replica is live but incompatible, and the passive replica is compatible
- **THEN** the edge sends the tool call only to the compatible passive replica

#### Scenario: No replica is eligible
- **WHEN** no holder exists and every configured replica fails readiness admission
- **THEN** the edge returns 503 without forwarding the tool call

### Requirement: Compatibility rollout is deployment-owned
The deployment contract SHALL support an explicit set of accepted runtime contracts and SHALL document expand-roll-contract rollout. Exomem SHALL NOT auto-update replicas or require a specific replication product.

#### Scenario: Incompatible contract migration
- **WHEN** a new release requires runtime contract 2 while contract 1 replicas are still serving
- **THEN** deployment infrastructure can temporarily admit contracts 1 and 2
- **AND** remove contract 1 only after the replica rollout completes
