## Purpose

Define verifiable storage, key-custody, recovery and plaintext-access boundaries for private hosted vaults and their supporting platform services.

## ADDED Requirements

### Requirement: Hosted privacy claims identify the actual plaintext boundary

Hosted deployment evidence SHALL identify every processor that receives tenant plaintext, including TLS-terminating intermediaries and optional inference providers. Product claims MUST distinguish encrypted hosted processing from protection against the service operator or compute-host administrator. An unavailable processing backend MUST NOT trigger an undisclosed provider fallback.

#### Scenario: Content crosses an intermediary

- **WHEN** a configured proxy terminates TLS for a tenant content request
- **THEN** it is included in the deployment's plaintext processor inventory even if the origin connection is also encrypted
- **AND** the deployment is not described as end-to-end encrypted against that intermediary

#### Scenario: Selected inference backend is unavailable

- **WHEN** the authorized processing profile cannot run
- **THEN** its job remains visibly queued, blocked or failed
- **AND** no other provider receives content without the required owner selection

#### Scenario: Content routes directly but authorization remains externally hosted

- **WHEN** MCP and media bypass content proxies while the authorization issuer or browser application remains externally hosted
- **THEN** the processor inventory retains those services' authentication-material and code-delivery trust
- **AND** direct content routing is not described as cryptographic protection against those services or the compute operator

### Requirement: Persistent plaintext cannot escape encrypted storage

Hosted canonical data, derived state, credentials, logs, processing scratch, recovery staging and sensitive platform state SHALL persist only on verified encrypted storage. Memory-backed scratch SHALL be bounded and MUST NOT spill through unencrypted swap or dumps. Failure to establish protection SHALL block the affected operation before plaintext is written.

#### Scenario: Recovery uses a plaintext disk temporary directory

- **WHEN** a backup, restore, export or database job resolves scratch to an unverified disk-backed path
- **THEN** admission fails with a content-free diagnostic before writing sensitive bytes
- **AND** a restrictive file mode alone does not satisfy encryption readiness

#### Scenario: Memory scratch would exceed safe capacity

- **WHEN** a job cannot fit its bounded scratch within admitted memory
- **THEN** it uses separately admitted encrypted scratch or remains blocked
- **AND** it does not consume the memory reserved for interactive service and recovery

### Requirement: Tenant decryption authority is separately scoped

Each tenant SHALL have distinct volume-unlock and application wrapping credentials scoped to its data. Ordinary tenant and worker credentials MUST NOT unlock or decrypt another tenant's data. Key custody and rotation SHALL preserve the existing governed source and recoverability contracts; key separation MUST NOT be represented as protection against a trusted host administrator.

#### Scenario: Tenant credential is used against another tenant

- **WHEN** a tenant's volume-unlock or application wrapping credential is presented for another tenant's storage
- **THEN** access fails without revealing the other tenant's plaintext or object existence

#### Scenario: Key migration is interrupted

- **WHEN** a migration stops after new key material is prepared but before restoration and activation are proved
- **THEN** existing recoverable data and required old key custody remain intact
- **AND** retry resumes the recorded transition without implicitly rotating unrelated tenants

#### Scenario: Execution principal requests broader decryption authority

- **WHEN** an ordinary cell, backup, delivery or media principal requests another tenant's key, a wrapping root, or an object outside its authoritative operation grant
- **THEN** the custody boundary rejects the request without listing keys or disclosing plaintext
- **AND** only the dedicated custody principal can generate, escrow or retire wrapping/unlock material

#### Scenario: Cluster state is lost while tenant volumes and backups remain

- **WHEN** an authorized recovery starts in a clean cluster without the old namespace or etcd state
- **THEN** separately escrowed tenant identity and key bindings restore access to that tenant's retained volume and objects
- **AND** recovery of one tenant does not confer decryption authority for another

### Requirement: Object storage receives application ciphertext and opaque metadata

Sensitive vault, delivery and platform-recovery objects SHALL be authenticated-encrypted before leaving the controlled service. Provider-side encryption SHALL remain enabled as an additional layer. Object names and provider-visible metadata MUST exclude filenames, paths, note content and plaintext keys. Existing historical objects SHALL be included in protection and retention evidence.

#### Scenario: Provider storage credentials are exposed

- **WHEN** an actor obtains object-store read credentials without the independently held application decryption authority
- **THEN** sensitive object bodies and manifests remain unreadable
- **AND** provider metadata does not disclose user-controlled filenames or content

#### Scenario: Encryption defaults change

- **WHEN** a new bucket or upload encryption default is enabled
- **THEN** deployment evidence separately accounts for historical versions
- **AND** no claim implies that changing a default retroactively protected retained objects

#### Scenario: Platform snapshot is restored without its old cluster

- **WHEN** a clean cluster restores the platform from object storage
- **THEN** etcd snapshots and control-plane recovery data are decrypted only through separately held platform recovery authority
- **AND** no native or parallel backup uploader leaves provider-readable sensitive bodies

### Requirement: Portable export delivery does not persist provider-readable plaintext

Hosted export delivery SHALL retain authorization, lifecycle admission and portable canonical integrity while decrypting only through the authorized delivery path. It MUST NOT create a provider-readable plaintext delivery object. Decryption SHALL authenticate each released chunk and validate stream ordering and completion. Cancellation and failures SHALL clean up scratch and release admission.

#### Scenario: User downloads an export

- **WHEN** the authorized owner requests a completed export
- **THEN** the recipient receives the portable archive through a verified encrypted connection
- **AND** stored delivery bodies remain application-encrypted until the authorized service decrypts them

#### Scenario: Ciphertext or framing is corrupted

- **WHEN** a stored export chunk, ordering marker or completion record fails integrity validation
- **THEN** the service aborts delivery and reports failure
- **AND** it neither releases the failing chunk's plaintext nor reports a complete verified export

### Requirement: Deletion and retention cover keys and derived copies

Deletion SHALL revoke active access and processing leases and remove live derived copies under the existing deletion contract. Retained snapshots, object locks and required recovery keys SHALL have an explicit expiry and retirement policy. A deployment MUST NOT claim immediate erasure of provider-locked history or discard recovery keys still required by retained snapshots.

#### Scenario: Deleted artifact has an in-flight worker and retained backup

- **WHEN** deletion is sealed while processing and retained backup versions exist
- **THEN** new worker retrieval and result publication are denied and live worker cleanup is tracked
- **AND** status distinguishes revoked live access from historical retention that has not yet expired

### Requirement: Privacy activation requires evidence from the deployed paths

The strengthened privacy profile SHALL require current evidence for actual TLS peers, encrypted mounts, scratch placement, secret scoping, network policy and restore behavior. Rendered configuration and unit tests alone MUST NOT establish live readiness. Evidence and diagnostics MUST exclude tenant content and secrets.

#### Scenario: Configuration exists without deployment proof

- **WHEN** manifests declare encryption but actual mounts or connections have not been verified
- **THEN** readiness evidence identifies the unverified protection
- **AND** the deployment does not advertise the strengthened guarantee or admit new users under it

#### Scenario: Historical provider-readable versions remain

- **WHEN** an exact-version inventory finds sensitive provider-readable bodies, including hidden versions or unfinished multipart uploads
- **THEN** the strengthened historical object-store privacy guarantee remains inactive until those versions are removed or proved application-encrypted and obsolete access credentials are revoked
- **AND** replacing the current version, hiding an old version or waiting for a future lock expiry does not satisfy activation
