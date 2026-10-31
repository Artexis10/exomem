# runtime-readiness-contract Specification

## Purpose
Define the runtime readiness route that operators and coordinated replicas use to tell a live process from one that is safe to serve and to take over the writer role.

## Requirements

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
- **THEN** both report the same runtime contract and transport
- **AND** the release mismatch remains visible for operator diagnosis

### Requirement: Readiness reflects takeover safety
Standalone Exomem SHALL remain ready without multi-host coordination. A coordinated replica SHALL report takeover eligibility only when its coordinator is healthy, its replica identity is configured, and its role is authoritatively known. A served recall-catalogue lag, which `install-readiness` defines, SHALL NOT change takeover eligibility and SHALL NOT add a reason code.

#### Scenario: Healthy coordinated follower
- **WHEN** a follower can confirm the current lease state
- **THEN** readiness returns 200 and reports takeover eligibility true

#### Scenario: Coordinator is unavailable
- **WHEN** a coordinated replica cannot confirm lease state
- **THEN** readiness returns 503 and reports takeover eligibility false

#### Scenario: A served catalogue lag leaves takeover eligibility unchanged
- **WHEN** a proven replica with a healthy coordinator reports its recall catalogue as `lagging`
- **THEN** readiness returns 200, reports takeover eligibility true, and lists no reason codes
