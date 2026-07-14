## ADDED Requirements

### Requirement: Recovery backups use the portable snapshot contract
The system durability worker SHALL start every 30 minutes with retry margin and SHALL serialize under the per-cell operation lock. It SHALL close and verify both external routes, quiesce/drain the cell, create the runtime portable archive, stream it to bounded system scratch, verify archive digest, manifest digest, size, and a minimum plausibility floor, release/resume the cell, and only then encrypt/upload from the verified scratch copy. It MUST NOT use an uncoordinated live filesystem archive.

#### Scenario: Backup succeeds without holding upload time in quiescence
- **WHEN** a scheduled backup prepares and verifies its local scratch copy
- **THEN** the cell checkpoint releases and routes reopen before encryption and remote upload continue

#### Scenario: Snapshot verification fails
- **WHEN** the archive is implausibly small or any size/digest/manifest check differs
- **THEN** no successful recovery point is recorded, the checkpoint is safely released or escalated, and an alertable failure is persisted

### Requirement: Verified backup age enforces the one-hour RPO
RPO SHALL be measured from the newest successfully encrypted, uploaded, and remotely verified object, not from job start time. Backup age SHALL warn at 45 minutes and become an alpha-blocking alert before 60 minutes. Failed work SHALL retry without overlapping another cell operation.

#### Scenario: Scheduled run fails near the RPO boundary
- **WHEN** the newest verified object reaches 45 minutes and the next run fails
- **THEN** the system warns, retries with bounded backoff, and blocks new alpha invitations before age reaches 60 minutes

### Requirement: Backup quiescence is bounded and measured
The owner proof SHALL measure route-closure through resume for a representative near-5-GiB vault and SHALL require it to complete within two minutes. If the target cannot be met, the system MUST lower the alpha storage entitlement or revise the snapshot contract before invitations rather than silently accepting recurring longer downtime.

#### Scenario: Representative backup exceeds quiescence target
- **WHEN** a near-limit owner/canary vault remains unavailable for more than two minutes during snapshot staging
- **THEN** the invitation gate fails and records the required entitlement or snapshot redesign decision

### Requirement: Recovery objects are encrypted and provider-protected
Every recovery archive SHALL use envelope AES-256-GCM with a unique data key and authenticated metadata. The object metadata/manifest SHALL authenticate the immutable opaque tenant ID, cell/candidate ID, operation ID, and fence generation needed for provider rediscovery. The wrapped key and opaque provider reference SHALL be stored outside the object. The system backup credential SHALL allow upload/list but no delete; a separate privileged restore/deletion job SHALL hold the required read/delete credential only while running. B2 objects SHALL use seven-day Object Lock and 30-day normal retention.

#### Scenario: B2 credential is exposed alone
- **WHEN** an attacker obtains only the runtime upload/list B2 credential
- **THEN** they cannot read plaintext vault content, delete retained recovery objects, or unwrap archive keys

#### Scenario: Remote object is independently verified
- **WHEN** upload reports success
- **THEN** the worker confirms provider object identity/size and records success only with matching encrypted metadata

### Requirement: User exports remain opaque and short-lived
User export SHALL use the same verified portable/encrypted provider path but SHALL retain its product-specific TTL. Substrate and the browser SHALL receive only opaque export/release references and a presigned HTTPS download URL valid for at most 15 minutes; they MUST NOT receive cell-local paths, B2 credentials, or unwrapped keys.

An export `expiresAt` SHALL be canonical RFC3339 UTC, future, and no more than 30 days away when its idempotency key is first accepted. An exact replay of an already accepted key and canonical input SHALL continue or return its stored result after that time passes. A brand-new expired request SHALL be rejected before an operation or provider artifact is created.

#### Scenario: Owner requests export download
- **WHEN** an available owner-scoped export is downloaded
- **THEN** the browser receives a short-lived provider URL and the local cell archive remains undisclosed

#### Scenario: Export response is lost across product expiry
- **WHEN** an export key and canonical request are accepted before `expiresAt`, and the exact request is replayed while pending or after completion once `expiresAt` has passed
- **THEN** the provider continues the same durable operation or returns the same result without creating a second artifact

#### Scenario: First export request is already expired
- **WHEN** no accepted idempotency claim exists and a request arrives with `expiresAt` in the past
- **THEN** the provider rejects it definitively without creating an operation or provider artifact

### Requirement: Restore publishes into a new candidate identity
Restore SHALL stop the candidate runtime, fetch/decrypt the provider object, verify ciphertext, expected size/digests, manifest/schema/source identity, release compatibility, path safety, and absence of forbidden hosted state, and invoke the version-pinned offline restore helper. Publication SHALL be atomic into an empty candidate vault, SHALL recreate destination binding markers and credentials, and SHALL rebuild derived state before authenticated readiness.

#### Scenario: Source binding is present in archive
- **WHEN** an archive attempts to restore source hosted runtime state or a source binding marker
- **THEN** validation fails and the candidate remains unbound/unrouted

#### Scenario: Clean candidate restore passes product checks
- **WHEN** a valid backup is restored into a different candidate
- **THEN** capture, recall, epistemic review, export, restart, and candidate identity checks pass

### Requirement: Operational databases have encrypted recovery and rediscovery
Every 30 minutes, a dedicated read-only backup job SHALL take a transactionally consistent logical export of the complete Substrate application database—including user, authentication/account ownership, billing, and hosted records—and the provisioner schema, encrypt the dump under the escrowed recovery key, upload it to a protected B2 prefix, and verify it. A restore SHALL recover owner login and tenant resolution plus pending operations, highest fences, resources, wrapped keys, credentials, and checkpoints. Post-restore reconciliation SHALL scan Kubernetes, HCloud labels/IDs, Traefik routes, and B2 prefixes for immutable tenant/cell/operation/fence metadata, compute the maximum provider-observed fence before accepting any mutation, and adopt or quarantine side effects newer than the dump without lowering it.

#### Scenario: Database restore is older than provider state
- **WHEN** a restored dump predates a created volume, route, or backup object
- **THEN** reconciliation discovers its operation/fence metadata, computes the higher provider maximum, never assigns it to another tenant, and rejects a lower-fence replay before adopting or quarantining it

#### Scenario: Pending operation survives database recovery
- **WHEN** a scratch restore contains a pending export/restore and wrapped key
- **THEN** the worker can resume or safely quarantine the operation and decrypt authorized recovery material

#### Scenario: Owner identity survives control-plane database loss
- **WHEN** the complete database recovery set is restored into an empty scratch environment
- **THEN** the owner can authenticate, resolve the same tenant and cell, and complete representative capture and recall after reconciliation

### Requirement: Deletion separates immediate service revocation from retained recovery expiry
Deletion SHALL immediately revoke sessions/routes, stop billing, quiesce, optionally prepare the promised final export, and destroy online compute, writable volume, active route, and application credentials. The lifecycle SHALL remain deleting/retained while Object Lock prevents backup deletion. At lock expiry, the privileged deletion worker SHALL override the normal 30-day lifecycle, delete and independently verify all recovery/export objects absent, destroy wrapped keys, and only then return final destroy proofs and permit `deleted`.

#### Scenario: Delete request arrives with locked backup
- **WHEN** a tenant has a recovery object with unexpired seven-day lock
- **THEN** online service/data is removed immediately, the 14-action destroy remains non-attempt-consuming pending, and `deleted` is not emitted

#### Scenario: Lock expires before 30-day lifecycle
- **WHEN** the last tenant recovery lock expires during deletion
- **THEN** the deletion worker removes the object immediately rather than waiting for normal 30-day lifecycle, verifies absence, destroys its wrapped key, and completes proof

### Requirement: Recovery objectives are explicit and demonstrated
The private alpha SHALL target RPO 0/RTO 5 minutes for pod failure, RPO 0/RTO 60 minutes for node loss with intact volume, and RPO at most one hour/RTO four hours for volume or operational-database loss. Measurements SHALL be retained as release evidence; failure SHALL block invitations.

#### Scenario: Node is treated as permanently lost
- **WHEN** the recovery drill rebuilds a node and reattaches the retained original volumes
- **THEN** acknowledged canonical writes are present and cells become ready within 60 minutes

#### Scenario: Volume is treated as permanently lost
- **WHEN** a cell restores from the latest verified B2 recovery object
- **THEN** the recovered point is no older than one hour and the candidate becomes product-ready within four hours
