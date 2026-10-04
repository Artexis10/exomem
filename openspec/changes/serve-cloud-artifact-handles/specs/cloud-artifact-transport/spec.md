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

Cells SHALL reach only the configured internal broker on its fixed port for artifact retrieval. Runtime DNS, public internet, metadata and other-cell access MUST remain denied. The broker SHALL have no vault mount, database credential, cell-token root or Kubernetes API access. Gateway public egress MUST remain denied.

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
