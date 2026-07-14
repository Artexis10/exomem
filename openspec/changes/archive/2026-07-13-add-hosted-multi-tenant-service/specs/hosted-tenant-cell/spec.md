## ADDED Requirements

### Requirement: Hosted Cell Mode Is Explicit And Additive

The system SHALL enter hosted-cell mode only through explicit operator configuration. Without that configuration, ordinary single-vault CLI, MCP, REST, Studio, OAuth, and setup behavior MUST remain unchanged and MUST NOT require a control plane, tenant identity, hosted secret, or external storage service.

#### Scenario: Ordinary local startup

- **WHEN** Exomem starts without hosted-cell mode enabled
- **THEN** it resolves and serves the configured local vault using the existing product behavior
- **AND** no hosted-cell readiness, gateway authentication, or tenant provisioning dependency is imposed

#### Scenario: Hosted mode is explicitly selected

- **WHEN** an operator starts Exomem with a complete hosted-cell configuration
- **THEN** the process applies the hosted-cell isolation, privacy, and readiness contract before serving tenant traffic

### Requirement: One Cell Serves Exactly One Vault

A hosted cell SHALL bind exactly one immutable cell identity to exactly one canonical vault root for the lifetime of the process. The cell MUST NOT accept a tenant ID, vault path, account ID, or storage root from a public request and MUST NOT switch vaults after startup.

#### Scenario: Public request attempts to select a tenant

- **WHEN** a request supplies a tenant or vault selector in its path, query, header, body, or tool arguments
- **THEN** the cell ignores or rejects that selector before command dispatch
- **AND** it continues to address only the vault bound by trusted startup configuration

#### Scenario: Process receives traffic for another tenant

- **WHEN** trusted gateway context does not match the cell identity established at startup
- **THEN** the cell rejects the request without invoking a command leaf or revealing whether another tenant exists

### Requirement: Hosted Configuration Has No Ambient Override

Hosted-cell startup SHALL consume an explicit operator-supplied configuration and MUST NOT load a repository, working-directory, user-home, or vault-local `.env` file that can override the assigned cell identity, vault root, runtime root, gateway trust, or privacy controls. Required values MUST be validated before listeners or background workers start.

#### Scenario: Vault contains a conflicting dotenv file

- **WHEN** a hosted tenant vault contains `.env` values naming another path, endpoint, token, or logging mode
- **THEN** hosted startup does not load or apply those values
- **AND** the trusted operator configuration remains authoritative

#### Scenario: Required hosted setting is missing

- **WHEN** the assigned cell identity, vault root, runtime root, or gateway trust configuration is absent or invalid
- **THEN** startup or readiness fails closed with a bounded machine-readable diagnostic
- **AND** the cell does not accept tenant data requests

### Requirement: Every Runtime Resource Is Tenant-Isolated

The cell SHALL use tenant-specific locations and namespaces for canonical vault data, temporary files, upload state, media jobs, idempotency records, OAuth or transport state, caches, runtime databases, logs, and secrets. Hosted readiness MUST reject known shared writable paths or namespaces that could mix two cells, while rebuildable sidecars MUST remain attributable to exactly one vault.

#### Scenario: Two cells are assigned the same writable runtime root

- **WHEN** hosted readiness detects that two distinct cell identities would share mutation-owned runtime state
- **THEN** at least the unsafe cell remains not ready
- **AND** no tenant request is served from the ambiguous configuration

#### Scenario: Derived state is rebuilt

- **WHEN** one tenant's rebuildable search or media sidecar is deleted and regenerated
- **THEN** only that tenant's canonical vault is used as input
- **AND** no path, cache entry, vector, transcript, or job record from another cell is read or written

### Requirement: Cell Ingress Is Private And Authenticated

A hosted cell SHALL accept data-plane traffic only from the trusted hosted gateway or an explicitly authorized operator channel. Gateway-to-cell authentication MUST be validated before command, transfer, Studio-data, or readiness-detail access, and authentication failures MUST return no vault-derived titles, paths, counts, content, or tenant metadata.

#### Scenario: Direct unauthenticated request reaches a cell

- **WHEN** a caller reaches the cell without valid internal gateway or operator authentication
- **THEN** the request is rejected before resolving vault content or dispatching a command
- **AND** the response contains no tenant-derived data

#### Scenario: Internal credential belongs to another cell

- **WHEN** a validly formed internal credential is bound to a different cell identity or audience
- **THEN** the cell rejects it as unauthorized
- **AND** it does not disclose whether the requested command or artifact exists

### Requirement: Readiness Proves Safe Serviceability

The cell SHALL expose bounded liveness and readiness signals for private operational use. Liveness SHALL report only that the process can answer; readiness SHALL become healthy only after the canonical vault binding, scaffold/schema access, process-safe mutation boundary, tenant runtime directories, gateway trust, required secrets, and enabled worker safety checks pass. Neither signal MUST expose vault content or secret values.

#### Scenario: Process is live but its vault lock is unsafe

- **WHEN** the process is running but cannot prove safe mutation serialization for its assigned vault
- **THEN** liveness can remain healthy while readiness is unhealthy
- **AND** the gateway does not route tenant traffic to that cell

#### Scenario: Readiness output is inspected

- **WHEN** an authorized operator requests cell readiness
- **THEN** the response identifies check names, status, stable error codes, and remediation-safe metadata
- **AND** it excludes note titles, vault-relative paths, query text, credentials, encryption keys, and raw configuration values

### Requirement: Provisioning Is Idempotent And Non-Destructive

Hosted-cell initialization SHALL create a missing tenant vault from the generic Exomem scaffold and SHALL be safe to retry. It MUST NOT overwrite or reinitialize an existing canonical vault, replace a foreign scaffold silently, or report ready before required initialization is durably complete. Provisioning output SHALL be machine-readable and MUST exclude secrets.

#### Scenario: Fresh cell is provisioned

- **WHEN** initialization targets an empty assigned storage root
- **THEN** it creates one valid generic Exomem vault and returns its cell identity, lifecycle status, runtime version, and enabled capability flags
- **AND** it returns no gateway token, encryption key, or user content

#### Scenario: Provisioning request is retried

- **WHEN** the same trusted provisioning identity retries initialization after the cell already became ready
- **THEN** the operation returns the existing ready cell state without replacing canonical files
- **AND** it does not create a second vault or duplicate initialization logs

#### Scenario: Existing data is incompatible

- **WHEN** the assigned storage root contains a non-empty vault that cannot pass Exomem integrity or scaffold checks
- **THEN** provisioning fails closed with a repair or operator-review state
- **AND** it does not rewrite the existing data to force readiness

### Requirement: Hosted Observability Is Content-Redacted

Hosted-cell logs, metrics, traces, and error reports SHALL exclude query text, note or source bodies, uploaded filenames where user-controlled, vault-relative paths, authorization values, session tokens, transfer tokens, Paddle or control-plane secrets, and raw tool arguments. They SHALL use an opaque cell identifier and SHALL limit recorded operational metadata to content-free fields such as command name, duration, result class, error code, byte count, and resource usage.

#### Scenario: Recall command is traced

- **WHEN** a hosted `ask_memory` or `find` call succeeds or fails
- **THEN** observability records the opaque cell identity, operation, duration, and result status without recording the query or returned paths
- **AND** the same content-free policy applies to exception logging

#### Scenario: Transfer fails authentication

- **WHEN** an upload or download request fails authentication or validation
- **THEN** logs do not include its bearer token, transfer token, original filename, multipart body, or requested vault path

### Requirement: Optional Compute Is Entitlement-Bound And Soft-Failing

Embeddings, media extraction, diarization, vision, and other optional compute SHALL remain disabled in hosted cells unless trusted startup configuration grants the matching capability and the worker passes resource and mutation-safety readiness. Public requests MUST NOT enable or raise a tenant's compute tier. When optional compute is absent, exhausted, or fails, lexical retrieval and durable capture SHALL remain available whenever their core safety checks pass.

#### Scenario: Tenant without media entitlement uploads a supported file

- **WHEN** trusted cell configuration does not grant media processing
- **THEN** the cell does not start or invoke the media worker for that tenant
- **AND** it preserves the upload durably if the core upload contract permits it and reports processing unavailable without loading a model

#### Scenario: Entitled optional worker fails

- **WHEN** an entitled embedding or media worker cannot load, exceeds its resource bound, or fails during processing
- **THEN** the cell reports that optional capability degraded without making the whole cell unready
- **AND** it continues to serve safe lexical retrieval and durable capture

### Requirement: Hosted Cells Remain Pure Substrate

A hosted cell MUST NOT add or invoke a server-side reasoning or generative model. Optional embedding, OCR, ASR, CLIP, diarization, and frozen transduction models SHALL only measure or transform tenant-provided material deterministically and MUST NOT make subscription, routing, deletion, authority, confidence, or epistemic decisions.

#### Scenario: Cell processes tenant material

- **WHEN** an entitled optional measurement worker processes a document, image, audio file, or video
- **THEN** its output is limited to deterministic extraction, transcription, representation, or measurement used by existing Exomem behavior
- **AND** no model decides whether a tenant is entitled, which cell receives a request, or what knowledge is authoritative

### Requirement: Cell Lifecycle Supports Quiesce And Safe Shutdown

The cell SHALL support an operator-controlled quiescing state that removes it from gateway readiness, rejects new mutations, allows bounded in-flight work to finish or cancel safely, and stops background writers before reporting quiesced. Shutdown, snapshot, restore, and deletion orchestration MUST use that state rather than destroying storage beneath an active process.

#### Scenario: Control plane prepares a snapshot

- **WHEN** an authorized operator requests the cell to quiesce
- **THEN** the cell becomes not ready for new gateway traffic, drains its active mutation boundary, and stops background writers
- **AND** it reports quiesced only after no mutation can change the snapshot source

#### Scenario: New request arrives while quiescing

- **WHEN** the gateway sends a new mutation after quiescing begins
- **THEN** the cell rejects it with a stable retryable lifecycle error before invoking the command leaf
- **AND** the request cannot delay or invalidate the pending lifecycle operation

### Requirement: Cell Failures Are Tenant-Contained

Failure, restart, resource exhaustion, or optional-worker degradation in one hosted cell MUST NOT corrupt another tenant's vault, change another cell's readiness, disclose another cell's operational details, or require restarting the public gateway. A replacement process SHALL rebind only the affected cell after normal readiness checks.

#### Scenario: One tenant exhausts a resource limit

- **WHEN** tenant A reaches a configured CPU, memory, storage, upload, or worker limit
- **THEN** tenant A receives a bounded quota or degradation response
- **AND** tenant B's readiness, mutation boundary, and command latency are not changed by shared in-process state

#### Scenario: Cell process restarts

- **WHEN** one tenant cell exits and a replacement process starts
- **THEN** the gateway keeps other tenant cells available
- **AND** the replacement serves the affected tenant only after its immutable binding and readiness checks succeed
