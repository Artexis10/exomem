## Purpose

Retrieve client-supplied file handles for isolated Cloud cells without granting tenant runtimes general network access or replacing governed artifact persistence.

## ADDED Requirements

### Requirement: Fetch authority comes from the authenticated tool call

The gateway SHALL authorize broker retrieval only for actual authenticated `capture_source` or `preserve_artifacts` calls carrying file handles. A short-lived signed grant SHALL bind the routed cell, operation, exact ordered descriptors, request identity and limits. It MUST NOT appear in model-visible arguments, logs or public responses.

#### Scenario: A supplied handle is fetched
- **WHEN** the authenticated call names a public HTTPS file handle
- **THEN** only that exact descriptor is authorized for the selected cell and operation
- **AND** client-supplied internal grant headers are not forwarded

#### Scenario: Fetch authority is altered
- **WHEN** a descriptor, cell, operation, signature or expiry does not match the grant
- **THEN** the broker refuses before making any outbound request

#### Scenario: Inspection cannot complete within its bounds
- **WHEN** a selected cell's MCP request exceeds the authority-inspection byte or time budget
- **THEN** the gateway forwards the original request bytes without a fetch grant
- **AND** an ordinary large or slow save remains available

### Requirement: Runtime networking remains confined

Cells SHALL reach only the configured internal broker on its fixed port for artifact retrieval. Runtime DNS, public internet, metadata and other-cell access MUST remain denied. The broker SHALL have no vault mount, database credential, cell-token root or Kubernetes API access. Gateway public egress MUST remain denied. *Amended 2026-10-24 by `add-cloud-transfer-tools`:* only when the platform chart sets `cloudGateway.transferTools.enabled` (default `false`), the gateway MAY also egress on TCP 443 to public addresses, excluding every `cells.jobEgressExcept` range, to reach the Substrate web API. Cells, the broker and artifact transport are unaffected.

#### Scenario: A cell attempts unrelated network access
- **WHEN** a runtime tries DNS, direct HTTPS, metadata or another cell
- **THEN** network policy refuses it
- **AND** a permitted broker connection remains available

### Requirement: Broker retrieval is bounded and ephemeral

The broker SHALL reuse canonical safe retrieval and fetch only public HTTPS/443. A grant SHALL allow at most eight handle attempts and 100 MiB of accepted artifacts. Concurrency, lifetime, request size, temporary storage and grant records MUST be bounded. Temporary bytes SHALL be private and removed after completion, failure or cancellation.

#### Scenario: A batch reuses fetch authority
- **WHEN** a consumed index is requested again or the grant exceeds its aggregate allowance
- **THEN** the broker refuses the attempt without additional retrieval

#### Scenario: Broker resources are occupied
- **WHEN** a new transfer would exceed the broker's active-transfer or live-grant bounds
- **THEN** it returns a bounded unavailable result without evicting an active grant or changing stored vault data

### Requirement: Transport rollout preserves existing custody

Transport SHALL be default-off and activated only with a compatible gateway, broker, cell runtime, verified endpoint and confined network/admission policy. Missing or unavailable transport SHALL report a truthful file failure, never fall back to unrestricted networking. Activation MUST NOT change identities, purposes, preferences, stored data or fleet admission.

#### Scenario: Transport is unavailable
- **WHEN** a configured broker cannot serve a valid handle
- **THEN** the file reports failure with no claimed stored path or hash
- **AND** ordinary recall remains available

### Requirement: One fleet switch can activate transport for every cell

After the broker has served an explicitly listed set of cells, an operator MAY activate transport for every cell by one fleet switch. The switch SHALL select every existing cell and every cell created later. The cell render, network and admission policy, and gateway grant issuance SHALL all select cells from it. With the switch off, selection SHALL be exactly the explicit list.

#### Scenario: A cell is created after the switch is on
- **WHEN** a cell is created while the fleet switch is on and the cell is not listed
- **THEN** its runtime receives the broker URL and the single broker egress rule with no per-cell edit
- **AND** the gateway signs grants for it

#### Scenario: The switch is turned on beside a list
- **WHEN** the switch is turned on while some cells are already listed
- **THEN** only the cells not already listed re-render and restart
- **AND** each listed cell keeps its render digest, environment and network policy

#### Scenario: The switch is off
- **WHEN** the fleet switch is off
- **THEN** only listed cells receive the broker URL and egress rule
- **AND** every other cell keeps its render digest, environment and zero-egress policy

#### Scenario: The switch does not widen the edge
- **WHEN** the fleet switch is on
- **THEN** admission accepts, for any cell, only the exact broker egress rule on TCP 8767 and refuses any wider egress
- **AND** the broker accepts only cell runtime pods, never backup or restore Job pods
