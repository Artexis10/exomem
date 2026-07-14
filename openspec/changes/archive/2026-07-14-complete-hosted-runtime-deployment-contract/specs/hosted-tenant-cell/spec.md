## MODIFIED Requirements

### Requirement: Cell Ingress Is Private And Authenticated

A hosted cell SHALL accept command, lifecycle, readiness-detail, Studio, MCP, contract-discovery, and version 1 transfer traffic only from the trusted hosted gateway or an explicitly authorized operator channel, with service authentication validated before vault resolution. The sole public exception SHALL be PUT plus bodyless OPTIONS on the dedicated version 2 upload path and GET plus bodyless OPTIONS on the dedicated version 2 download path at the configured transfer hostname. Data methods SHALL authorize only a valid exact-operation one-time capability; OPTIONS SHALL return only exact configured-origin policy without accepting/consuming a grant. A version 2 capability MUST NOT authenticate another route, and the public routes MUST reject service bearers, cookies, selectors, and any authority other than their capability. Authentication failures MUST return no vault-derived titles, paths, counts, content, existence facts, or tenant metadata.

#### Scenario: Direct unauthenticated request reaches a private cell route

- **WHEN** a caller reaches a command, lifecycle, readiness-detail, Studio, MCP, contract, version 1 transfer, or unknown route without valid internal gateway or operator authentication
- **THEN** the request is rejected before resolving vault content or dispatching a command
- **AND** the response contains no tenant-derived data

#### Scenario: Valid transfer capability reaches another route

- **WHEN** a caller presents a valid version 2 upload or download capability to any route, method, hostname, or operation other than the exact dedicated transfer target
- **THEN** the cell rejects it without treating the capability as a gateway session
- **AND** no private route inherits the public capability exception

#### Scenario: Browser preflights a dedicated transfer path

- **WHEN** the configured origin sends bodyless OPTIONS to the exact upload or download path
- **THEN** only the route-specific CORS policy is returned without grant consumption
- **AND** OPTIONS on any other application path receives no public exception

#### Scenario: Internal credential belongs to another cell

- **WHEN** a validly formed internal credential is bound to a different cell identity or audience
- **THEN** the cell rejects it as unauthorized
- **AND** it does not disclose whether the requested command or artifact exists

### Requirement: Provisioning Is Idempotent And Non-Destructive

Hosted-cell initialization SHALL be exposed through the normative version 1 JSON operator contract and SHALL create or converge a tenant vault safely. Binding version 2 SHALL persist the opaque cell and logical vault identities, normalized absolute vault/state/log roots, root kind, and runtime UID/GID; immutable release/protocol SHALL be validated deployment proof rather than persistent ownership. UID and GID MUST each be nonzero integers in the supported bound. The initializer SHALL create private roots/markers, bootstrap the security authority from one high-entropy injected active credential, and validate actual no-follow ownership/modes before success. It MUST NOT overwrite or reinitialize canonical vault data, adopt or chown unowned/foreign data, accept a conflicting binding, or report ready before every required write is durable. Output SHALL follow the checked-in redacted schema and exclude credentials, user content, and private paths.

#### Scenario: Fresh cell is provisioned by the hosted operator command

- **WHEN** the initializer receives empty assigned roots, distinct valid cell/vault identities, expected release/protocol, non-root UID/GID, one generated active credential, and a new operation ID
- **THEN** it atomically creates one generic Exomem vault, private state/log roots, matching v2 ownership markers, and revision-one credential state
- **AND** it returns only the contract/binding versions, opaque identities, lifecycle status, release/protocol, runtime UID/GID, credential version, and enabled capability flags

#### Scenario: Provisioning request is retried

- **WHEN** the same operation ID and request digest retry after complete or partially durable matching initialization
- **THEN** the operation converges or returns the recorded proof without changing canonical file bytes or ownership identity
- **AND** it does not create a second vault, credential bootstrap, or initialization record

#### Scenario: Matching version 1 roots are migrated

- **WHEN** a privileged initializer finds a valid matching v1 binding and a bounded tree containing only allowed no-follow entry types
- **THEN** it may idempotently converge private ownership and publish matching v2 markers
- **AND** partial chown or marker-write interruption can be retried without adopting an unbound tree

#### Scenario: Existing data, ownership, or binding is incompatible

- **WHEN** a root is non-empty and unowned, foreign-bound, symlinked, multiply-linked, contains a device/FIFO/socket, has an unsupported UID/GID, or carries a conflicting cell/vault/layout binding
- **THEN** provisioning fails closed with a stable conflict or operator-review code
- **AND** it does not rewrite, chown, delete, or publish data to force readiness

#### Scenario: Runtime identity or actual ownership differs from the marker

- **WHEN** a hosted process UID/GID, root owner/mode, or marker owner/mode differs from binding v2
- **THEN** readiness fails before tenant traffic is admitted
- **AND** the non-privileged process does not silently migrate persistent ownership

## ADDED Requirements

### Requirement: Cell Security State Has Its Own Durable Authority

Credential transition state and consumed transfer JTIs SHALL live in one cell-bound, private, process-safe SQLite security authority beneath the state root. This state is security metadata rather than canonical or mutation-owned vault state and SHALL use its own bounded transactions, full synchronous durability, schema checks, compare-and-swap revisions, and uniqueness constraints. It MUST NOT require or bypass the canonical vault mutation boundary. Failure to prove security-state durability SHALL keep service authentication or new transfer admission unavailable. Lifecycle admission SHALL still count every public/private transfer and drain it before quiescence or deletion sealing.

#### Scenario: Two processes consume one JTI concurrently

- **WHEN** concurrent requests attempt to consume the same valid JTI
- **THEN** exactly one durable transaction succeeds and every other request is rejected before body read or file open
- **AND** the result remains replay-resistant after process restart

#### Scenario: Security authority is unavailable or full

- **WHEN** SQLite integrity, locking, fsync, schema, cell binding, or bounded-capacity checks cannot prove safe state
- **THEN** authentication readiness or new transfer admission fails with a stable content-free code
- **AND** the system does not assume a credential transition or JTI consumption occurred

#### Scenario: Public transfer overlaps quiescence or sealing

- **WHEN** quiescence begins while a version 2 upload/download is admitted
- **THEN** the transfer remains counted until upload abort/commit or download close, and the bounded drain waits for it
- **AND** later transfers are rejected before JTI consumption while canonical upload finalization still uses the vault mutation boundary

### Requirement: Service Credentials Rotate Through Durable Overlap

A hosted cell SHALL implement the normative bootstrap, stage, authenticated-proof, promote, abort, and finalize state machine from the checked-in operator contract. Plaintext credentials SHALL come only from the fixed native Kubernetes AtomicWriter Secret file `/run/exomem/credentials/credentials.json`; the Secret SHALL be mounted read-only only in the single Exomem container with `defaultMode: 0444`, no pod `fsGroup`, and a root-owned resolved regular file, and a caller MUST NOT select another path. The loader SHALL accept only the bounded kubelet-owned symlink topology confined beneath that mount and read one validated descriptor generation. Each value MUST be an unpadded base64url encoding of 32 uniformly random bytes. Durable state SHALL contain only opaque versions, SHA-256 digests, phase, active/pending/preferred versions, rotation identity, compare-and-swap revision, and content-free proof metadata. Both FastMCP and every private route MUST use the same dynamic authority; authentication SHALL accept exactly the recorded active/pending versions during overlap and only the finalized active version afterward with constant-time digest comparison.

#### Scenario: Initial credential is bootstrapped

- **WHEN** init/restore finds no credential state and the bundle contains exactly the requested active version/value
- **THEN** it atomically records stable revision-one digest state and accepts that version
- **AND** retry with identical operation input replays while any changed initial version or digest conflicts

#### Scenario: Pending credential is staged and survives restart

- **WHEN** a stable cell stages a distinct injected pending version/value at the expected revision
- **THEN** state moves atomically to staged, both recorded versions authenticate, and a restart reconstructs the same accepted set
- **AND** missing, weak, duplicate, or digest-mismatched injected values keep service authentication unready without exposing a secret

#### Scenario: Pending credential proves health and is promoted

- **WHEN** the pending version authenticates a complete probe and records proof bound to the cell, digest, rotation, request, release, protocol, worker policy, success result, and current time
- **THEN** promote succeeds only with the matching fresh proof and expected revision, makes pending preferred, and retains prior active acceptance
- **AND** changed pending state, rotation, or proof older than 300 seconds invalidates promotion/finalization

#### Scenario: Rotation is aborted before finalization

- **WHEN** a staged or promoted rotation aborts at the expected revision while the original active value still matches
- **THEN** state atomically returns to stable on the original active version and rejects the abandoned pending version on the next request
- **AND** crashes/retries preserve either the complete old or complete aborted state

#### Scenario: Rotation is finalized

- **WHEN** a promoted pending version has matching fresh proof and finalization succeeds at the expected revision
- **THEN** it becomes the sole stable active version, and old digest/proof/rotation metadata is removed atomically
- **AND** the prior plaintext token is rejected by MCP and every private route on the next authentication decision without changing tenant data or requiring restart

#### Scenario: Unsafe or concurrent transition is attempted

- **WHEN** versions/secrets repeat, proof is absent/stale/foreign, action/phase is invalid, expected revision differs, or the operation ID is reused with another request digest
- **THEN** the transition fails closed with a stable conflict code
- **AND** the last committed accepted credential set remains unchanged

### Requirement: Operator Probe Proves The Authenticated Runtime Contract

The hosted image SHALL provide the normative content-free probe helper against only `http://127.0.0.1:<validated-port>/private/exomem/v1/ready`. It SHALL disable proxy/netrc/environment transport, DNS, and redirects; enforce bounded connect/read/total time, response bytes, exact JSON media type, and exact versioned response schema; and validate cell/vault identity, release, protocol, authenticated credential version, current security revision, service authentication, mutation authority, active admission, and worker-policy digest. Every invocation SHALL perform a fresh HTTP request; operation idempotency may replay only matching proof persistence and MUST NOT substitute cached health. Output/errors MUST NOT contain credentials, URL, response body, tenant content, vault paths, or raw configuration.

#### Scenario: Expected cell passes the probe

- **WHEN** literal loopback returns the exact bounded readiness proof using the selected active or pending credential at the expected security-state revision
- **THEN** the helper emits the stable JSON success proof including current security revision and, for pending, transactionally records proof bound to the current rotation
- **AND** each invocation uses a fresh canonical request ID and random opaque principal rather than replaying stored headers

#### Scenario: Identical probe operation is retried

- **WHEN** a prior pending-proof operation is retried with identical input
- **THEN** the helper re-runs loopback authentication and all current readiness checks before returning success
- **AND** it reuses durable proof persistence only if security revision, rotation, digest, release, protocol, worker policy, and readiness still match

#### Scenario: Transport could leave literal loopback

- **WHEN** configuration or environment attempts another host/scheme/path/port, DNS resolution, userinfo, query/fragment, proxy, netrc, or redirect
- **THEN** the helper rejects or ignores that transport before sending a credential
- **AND** no proof is recorded

#### Scenario: Authentication or bounded proof differs

- **WHEN** authentication fails, time/size/media bounds are exceeded, JSON is malformed/overbroad, or any cell/release/protocol/credential/admission/worker field differs
- **THEN** the helper exits unsuccessfully with a stable redacted code
- **AND** Kubernetes/provisioner does not mark the cell ready or complete a credential transition
