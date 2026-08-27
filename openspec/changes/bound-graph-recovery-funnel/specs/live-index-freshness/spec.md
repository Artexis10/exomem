## MODIFIED Requirements

### Requirement: Bounded write-time index maintenance

A component deferral that has already durably recorded path-exact receipts covering the batch SHALL count as batch-report success and SHALL NOT seed a full-component refresh demand — for the embeddings component (semantic receipts) AND for the epistemic-graph component (per-path graph receipts), including while graph synchronization is in recovery. The system SHALL mint a durable full-component refresh demand from a write-time outcome only for a component whose incomplete work has no durable path coverage, and SHALL count both outcomes — covered deferrals accepted and uncovered deferrals escalated — in stable content-free telemetry.

#### Scenario: A warm-up-deferred embedding batch does not seed a whole-vault rebuild

- **WHEN** a batch atomic write's embeddings component defers because the embedding model is warming
- **AND** the warm-up deferral has already recorded durable semantic receipts naming exactly that batch's paths
- **THEN** the batch report treats the embeddings component as succeeded-deferred
- **AND** no full-component refresh receipt is minted for the batch
- **AND** the already-queued semantic replay remains the sole durable demand for those paths

#### Scenario: A durably covered graph deferral during recovery does not mint

- **WHEN** graph synchronization is in a recovery-required state
- **AND** a batch atomic write's graph component defers after durably recording per-path graph receipts covering the batch's graph-input paths
- **THEN** the batch report treats the graph component as succeeded-deferred
- **AND** no full-component refresh receipt is minted for the batch
- **AND** the queued graph replay remains the sole durable demand for those paths

#### Scenario: An uncovered deferral still fails closed

- **WHEN** a component defers without durable path-exact coverage of the batch
- **THEN** the batch report fails and a durable full-component refresh demand is minted
- **AND** the escalation increments its telemetry counter

## ADDED Requirements

### Requirement: Persistent graph recovery is an alarmed, bounded condition

The system SHALL measure how long graph synchronization has continuously required recovery and surface it as stable content-free telemetry in the readiness payload. Diagnostics SHALL fail — not merely warn — when recovery has been required beyond a configured bound, and SHALL fail immediately when graph work is disabled while a durable recovery checkpoint exists, including when the disabling kill switch is injected by an interpreter startup file rather than the process environment.

#### Scenario: Recovery outlasting its bound raises a diagnostic failure

- **WHEN** graph synchronization has continuously required recovery for longer than the configured bound
- **THEN** the readiness payload exposes the recovery age
- **AND** the doctor reports a failing finding naming the age and the remediation

#### Scenario: An unrecoverable configuration fails diagnostics immediately

- **WHEN** graph scheduling or the graph index is disabled while a durable graph recovery checkpoint exists
- **THEN** the doctor reports a failing finding identifying the disabling source, including a site-packages startup file that injects the kill switch

### Requirement: The out-of-process index drain refuses contended graph ownership

The out-of-process index drain SHALL detect that a live service owns graph work for the target vault before taking any graph claim, and SHALL refuse to start with a remediation naming the stop-window procedure instead of contending for the claim.

#### Scenario: Drain against a live graph-active service refuses

- **WHEN** the out-of-process index drain is invoked for a vault whose running service currently performs graph work
- **THEN** the drain exits without taking the graph claim
- **AND** its message names the stop-window procedure as the remediation

### Requirement: Graph startup validation is bounded

A service restart that finds a coherent durable graph checkpoint SHALL admit graph reads without rebuilding the whole graph; the startup pass validates against durable state and reserves the whole-vault rebuild for genuine incoherence (missing or malformed checkpoint, digest mismatch, or a recorded crash marker). Read suspension during startup validation SHALL be bounded by the validation itself, not by a full rebuild.

#### Scenario: Restart with a coherent graph admits without a full rebuild

- **WHEN** the service restarts and the durable graph checkpoint matches the published sidecar's digest and generation
- **THEN** graph reads are admitted after the O(1) validation
- **AND** no whole-vault graph rebuild is scheduled by the startup pass

#### Scenario: Genuine incoherence still rebuilds

- **WHEN** the service restarts and the durable graph checkpoint is missing, malformed, or does not match the published sidecar
- **THEN** the startup pass suspends reads and schedules the whole-vault rebuild
